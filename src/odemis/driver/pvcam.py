# -*- coding: utf-8 -*-
'''
Created on 22 Feb 2013

@author: Éric Piel

Copyright © 2012-2013 Éric Piel, Delmic

This file is part of Open Odemis.

Odemis is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 2 of the License, or (at your option) any later version.

Odemis is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.

You should have received a copy of the GNU General Public License along with Odemis. If not, see http://www.gnu.org/licenses/.
'''

# This tries to support the PVCam SDK from Roper/Princeton Instruments/Photometrics.
# However, the library is slightly different between the companies, and this has
# only been tested on Linux for the PI PIXIS (USB) camera.
#
# Note that libpvcam is only provided for x86 32-bits

from __future__ import division
from . import pvcam_h as pv
from ctypes import *
from odemis import __version__, model, util
from odemis.model._dataflow import MD_BPP
import gc
import logging
import math
import numpy
import os
import threading
import time
import weakref

# This python file is automatically generated from the pvcam.h include file by:
# h2xml pvcam.h -c -I . -o pvcam_h.xml
# xml2py pvcam_h.xml -o pvcam_h.py


class PVCamError(Exception):
    def __init__(self, errno, strerror):
        self.args = (errno, strerror)
        
    def __str__(self):
        return self.args[1]


# TODO: on Windows, should be a WinDLL?
class PVCamDLL(CDLL):
    """
    Subclass of CDLL specific to PVCam library, which handles error codes for
    all the functions automatically.
    It works by setting a default _FuncPtr.errcheck.
    """
    
    def __init__(self):
        if os.name == "nt":
            WinDLL.__init__('libpvcam.dll') # TODO check it works
        else:
            # Global so that other libraries can access it
            # need to have firewire loaded, even if not used
            self.raw1394 = CDLL("libraw1394.so", RTLD_GLOBAL)
            #self.pthread = CDLL("libpthread.so.0", RTLD_GLOBAL) # python already loads libpthread
            CDLL.__init__(self, "libpvcam.so", RTLD_GLOBAL)
            try:
                self.pl_pvcam_init()
            except PVCamError:
                pass # if opened several times, initialisation fails but it's all fine


    def pv_errcheck(self, result, func, args):
        """
        Analyse the return value of a call and raise an exception in case of 
        error.
        Follows the ctypes.errcheck callback convention
        """
        if not result: # functions return (rs_bool = int) False on error
            try:
                err_code = self.pl_error_code()
            except Exception:
                raise PVCamError(0, "Call to %s failed" % func.__name__)
            res = False
            try:
                err_mes = create_string_buffer(pv.ERROR_MSG_LEN)
                res = self.pl_error_message(err_code, err_mes)
            except Exception:
                pass
            
            if res:
                raise PVCamError(result, "Call to %s failed with error code %d: %s" %
                                 (func.__name__, err_code, err_mes.value))
            else:
                raise PVCamError(result, "Call to %s failed with unknown error code %d" %
                                 (func.__name__, err_code))
        return result

    def __getitem__(self, name):
        func = CDLL.__getitem__(self, name)
        func.__name__ = name
        if not name in self.err_funcs:
            func.errcheck = self.pv_errcheck
        return func
    
    # names of the functions which are used in case of error (so should not
    # have their result checked
    err_funcs = ("pl_error_code", "pl_error_message", "pl_exp_check_status")
    
    def reinit(self):
        """
        Does a fast uninit/init cycle
        """
        try:
            self.pl_pvcam_uninit()
        except PVCamError:
            pass # whatever
        try:
            self.pl_pvcam_init()
        except PVCamError:
            pass # whatever

    def __del__(self):
        self.pl_pvcam_uninit()

# all the values that say the acquisition is in progress 
STATUS_IN_PROGRESS = [pv.ACQUISITION_IN_PROGRESS, pv.EXPOSURE_IN_PROGRESS, 
                      pv.READOUT_IN_PROGRESS]
TEMP_CAM_GONE = 2550 # temperature value that hints that the camera is gone

class PVCam(model.DigitalCamera):
    """
    Represents one PVCam camera and provides all the basic interfaces typical of
    a CCD/CMOS camera.
    This implementation is for the Roper/PI/Photometrics PVCam library... or at
    least for the PI version.
    
    This is tested on Linux with SDK 2.7, using the documentation found here:
    ftp://ftp.piacton.com/Public/Manuals/Princeton%20Instruments/PVCAM%202.7%20Software%20User%20Manual.pdf

    Be aware that the library resets almost all the values to their default 
    values after initialisation. The library doesn't call the dimensions 
    horizontal/vertical but serial/parallel (because the camera could be rotated).
    But we stick to: horizontal = serial (max = width - 1)
                     vertical = parallel (max = height - 1)
    
    It offers mostly a couple of VigilantAttributes to modify the settings, and a 
    DataFlow to get one or several images from the camera.
    
    It also provides low-level methods corresponding to the SDK functions.
    """
    
    def __init__(self, name, role, device, **kwargs):
        """
        Initialises the device
        device (int): number of the device to open, as defined in pvcam, cf scan()
        Raise an exception if the device cannot be opened.
        """
        self.pvcam = PVCamDLL()

        # TODO: allow device to be a string, in which case it will look for 
        # the given name => might be easier to find the right camera on systems
        # will multiple cameras.
        model.DigitalCamera.__init__(self, name, role, **kwargs)

        # so that it's really not possible to use this object in case of error
        self._handle = None
        self._temp_timer = None
        try:
            self._name = self.cam_get_name(device) # for reinit
        except PVCamError:
            raise IOError("Failed to find PI PVCam camera %d" % device)

        try:        
            self._handle = self.cam_open(self._name, pv.OPEN_EXCLUSIVE)
            # raises an error if camera has a problem
            self.pvcam.pl_cam_get_diags(self._handle)
        except PVCamError:
            raise IOError("Failed to open PVCam camera %d (%s)" % (device, self._name))
        
        logging.info("Opened device %d successfully", device)
        
        
        # Describe the camera
        # up-to-date metadata to be included in dataflow
        self._metadata = {model.MD_HW_NAME: self.getModelName()}
        
        # odemis + drivers
        self._swVersion = "%s (%s)" % (__version__.version, self.getSwVersion()) 
        self._metadata[model.MD_SW_VERSION] = self._swVersion
        self._hwVersion = self.getHwVersion()
        self._metadata[model.MD_HW_VERSION] = self._hwVersion
        
        resolution = self.GetSensorSize()
        self._metadata[model.MD_SENSOR_SIZE] = resolution
        
        # setup everything best (fixed)
        self._prev_settings = [None, None, None, None] # image, exposure, readout, gain
        # Bit depth is between 6 and 16, but data is _always_ uint16
        self._shape = resolution + (2**self.get_param(pv.PARAM_BIT_DEPTH),)
        
        # put the detector pixelSize
        psize = self.GetPixelSize()
        self.pixelSize = model.VigilantAttribute(psize, unit="m", readonly=True)
        self._metadata[model.MD_SENSOR_PIXEL_SIZE] = psize
        
        # Strong cooling for low (image) noise
        try:
            # target temp
            ttemp = self.get_param(pv.PARAM_TEMP_SETPOINT) / 100 # C
            ranges = self.GetTemperatureRange()
            self.targetTemperature = model.FloatContinuous(ttemp, ranges, unit="C",
                                                           setter=self.setTargetTemperature)
            
            temp = self.GetTemperature()
            self.temperature = model.FloatVA(temp, unit="C", readonly=True)
            self._metadata[model.MD_SENSOR_TEMP] = temp
            self._temp_timer = util.RepeatingTimer(100, self.updateTemperatureVA, # DEBUG -> 10
                                              "PVCam temperature update")
            self._temp_timer.start()
        except PVCamError:
            logging.debug("Camera doesn't seem to provide temperature information")
            
        # TODO: fan speed (but it seems PIXIS cannot change it anyway)        
            # max speed
#            self.fanSpeed = model.FloatContinuous(1.0, [0.0, 1.0], unit="",
#                                        setter=self.setFanSpeed) # ratio to max speed
#            self.setFanSpeed(1.0)
        
        self._setStaticSettings()

        # gain
        # The PIXIS has 3 gains (x1 x2 x4) + 2 output amplifiers (~x1 x4)
        # => we fix the OA to low noise (x1), so it's just the gain to change,
        # but we could also allow the user to pick the gain as a multiplication of
        # gain and OA? 
        self._gains = self._getAvailableGains()
        gain_choices = set(self._gains.values())
        self._gain = min(gain_choices) # default to low gain (low noise)
        self.gain = model.FloatEnumerated(self._gain, gain_choices, unit="",
                                          setter=self._setGain)
        self._setGain(self._gain)
        
        # read out rate 
        self._readout_rates = self._getAvailableReadoutRates() # needed by _setReadoutRate()
        ror_choices = set(self._readout_rates.values())
        self._readout_rate = max(ror_choices) # default to fast acquisition
        self.readoutRate = model.FloatEnumerated(self._readout_rate, ror_choices,
                                                 unit="Hz", setter=self._setReadoutRate)
        self._setReadoutRate(self._readout_rate)
        
        # binning is (horizontal, vertical), but odemis
        # only supports same value on both dimensions (for simplification)
        self._binning = (1,1) # px
        self._image_rect = (0, resolution[0]-1, 0, resolution[1]-1)
        minr = self.GetMinResolution()
        # need to be before binning, as it is modified when changing binning         
        self.resolution = model.ResolutionVA(resolution, [minr, resolution], 
                                             setter=self._setResolution)
        self._setResolution(resolution)
        
        bin_choices = set(range(1, min(resolution))) # for safety: the min of both axes
        self.binning = model.IntEnumerated(self._binning[0], bin_choices,
                                           unit="px", setter=self._setBinning)
        
        # default values try to get live microscopy imaging more likely to show something
        try:
            minexp = self.get_param(pv.PARAM_EXP_MIN_TIME) #s
        except PVCamError:
            # attribute doesn't exist
            minexp = 0 # same as the resolution
        minexp = min(1e-3, minexp) # we've set the exposure resolution at ms
        # exposure is represented by unsigned int
        maxexp = (2**32 -1) * 1e-3 #s
        range_exp = (minexp, maxexp) # s
        self._exposure_time = 1.0 # s
        self.exposureTime = model.FloatContinuous(self._exposure_time, range_exp,
                                                  unit="s", setter=self._setExposureTime)
        
        self.acquisition_lock = threading.Lock()
        self.acquire_must_stop = threading.Event()
        self.acquire_thread = None
        
        self.data = PVCamDataFlow(self)
        logging.debug("Camera component ready to use.")
    
    def _setStaticSettings(self):
        """
        Set up all the values that we don't need to change after.
        Should only be called at initialisation
        """
        # Set the output amplifier to lowest noise
        try:
            # Try to set to low noise, if existing, otherwise: default value
            aos = self.get_enum_available(pv.PARAM_READOUT_PORT)
            if pv.READOUT_PORT_LOW_NOISE in aos:
                self.set_param(pv.PARAM_READOUT_PORT, pv.READOUT_PORT_LOW_NOISE)
            else:
                ao = self.get_param(pv.PARAM_READOUT_PORT, pv.ATTR_DEFAULT)
                self.set_param(pv.PARAM_READOUT_PORT, ao)
            self._output_amp = self.get_param(pv.PARAM_READOUT_PORT)
        except PVCamError:
            pass # maybe doesn't even have this parameter
        
        # TODO change PARAM_COLOR_MODE to greyscale? => probably always default

        # Set to simple acquisition mode
        self.set_param(pv.PARAM_PMODE, pv.PMODE_NORMAL)
        # In PI cameras, this is fixed (so read-only)
        if self.get_param_access(pv.PARAM_CLEAR_MODE) == pv.ACC_READ_WRITE:
            self.set_param(pv.PARAM_CLEAR_MODE, pv.CLEAR_PRE_SEQUENCE)
        
        # set the exposure resolution. (choices are us, ms or s) => ms is best
        # for life imaging (us allows up to 71min)
        self.set_param(pv.PARAM_EXP_RES_INDEX, pv.EXP_RES_ONE_MILLISEC)
        # TODO: autoadapt according to the exposure requested?
    
    def getMetadata(self):
        return self._metadata
    
    def updateMetadata(self, md):
        """
        Update the metadata associated with every image acquired to these
        new values. It's accumulative, so previous metadata values will be kept
        if they are not given.
        md (dict string -> value): the metadata
        """
        self._metadata.update(md)
    
    # low level methods, wrapper to the actual SDK functions
    
    def Reinitialize(self):
        """
        Waits for the camera to reappear and reinitialise it. Typically
        useful in case the user switched off/on the camera.
        """
        # stop trying to read the temperature while we reinitialize
        if self._temp_timer is not None:
            self._temp_timer.cancel()
            self._temp_timer = None
        
        try:
            self.pvcam.pl_cam_close(self._handle)
        except PVCamError:
            pass
        self._handle = None
        
        # PVCam only update the camera list after uninit()/init()
        while True:
            logging.info("Waiting for the camera to reappear")
            self.pvcam.reinit()
            try:
                self._handle = self.cam_open(self._name, pv.OPEN_EXCLUSIVE)
                # succeded!
                break
            except PVCamError:
                time.sleep(1)
        
        # reinitialise the sdk
        logging.info("Trying to reinitialise the camera %s...", self._name)
        try:
            self.pvcam.pl_cam_get_diags(self._handle)
        except PVCamError:
            logging.info("Reinitialisation failed")
            raise
            
        logging.info("Reinitialisation successful")
        
        # put back the settings
        self._prev_settings = [None, None, None, None]
        self._setStaticSettings()
        self.setTargetTemperature(self.targetTemperature.value)
    
        self._temp_timer = util.RepeatingTimer(10, self.updateTemperatureVA,
                                         "PVCam temperature update")
        self._temp_timer.start()
        
    def cam_get_name(self, num):
        """
        return the name, from the device number
        num (int >= 0): camera number
        return (string): name
        """
        assert(num >= 0)
        cam_name = create_string_buffer(pv.CAM_NAME_LEN)
        self.pvcam.pl_cam_get_name(num, cam_name)
        return cam_name.value
    
    def cam_open(self, name, mode):
        """
        Reserve and initializes the camera hardware
        name (string): camera name
        mode (int): open mode
        returns (int): handle
        """
        handle = c_int16()
        self.pvcam.pl_cam_open(name, byref(handle), mode)
        return handle
    
    pv_type_to_ctype = {
         pv.TYPE_INT8: c_int8,
         pv.TYPE_INT16: c_int16,
         pv.TYPE_INT32: c_int32,
         pv.TYPE_UNS8: c_uint8,
         pv.TYPE_UNS16: c_uint16,
         pv.TYPE_UNS32: c_uint32,
         pv.TYPE_UNS64: c_uint64,
         pv.TYPE_FLT64: c_double, # hopefully true on all platforms?
         pv.TYPE_BOOLEAN: c_byte,
         pv.TYPE_ENUM: c_uint32, 
         }
    def get_param(self, param, value=pv.ATTR_CURRENT):
        """
        Read the current (or other) value of a parameter.
        Note: for the enumerated parameters, this it the actual value, not the 
        index.
        param (int): parameter ID (cf pv.PARAM_*)
        value (int from pv.ATTR_*): which value to read (current, default, min, max, increment)
        return (value): the value of the parameter, whose type depend on the parameter
        """
        assert(value in (pv.ATTR_DEFAULT, pv.ATTR_CURRENT, pv.ATTR_MIN,
                         pv.ATTR_MAX, pv.ATTR_INCREMENT))
        
        # find out the type of the parameter
        tp = c_uint16()
        self.pvcam.pl_get_param(self._handle, param, pv.ATTR_TYPE, byref(tp))
        if tp.value == pv.TYPE_CHAR_PTR:
            # a string => need to find out the length
            count = c_uint32()
            self.pvcam.pl_get_param(self._handle, param, pv.ATTR_COUNT, byref(count))
            content = create_string_buffer(count.value)
        elif tp.value in self.pv_type_to_ctype:
            content = self.pv_type_to_ctype[tp.value]()
        elif tp.value in (pv.TYPE_VOID_PTR, pv.TYPE_VOID_PTR_PTR):
            raise ValueError("Cannot handle arguments of type pointer")
        else:
            raise NotImplementedError("Argument of unknown type %d", tp.value)
        logging.debug("Reading parameter %x of type %r", param, content)
        
        # read the parameter
        self.pvcam.pl_get_param(self._handle, param, value, byref(content))
        return content.value
    
    def get_param_access(self, param):
        """
        gives the access rights for a given parameter.
        param (int): parameter ID (cf pv.PARAM_*)
        returns (int): value as in pv.ACC_*
        """
        rights = c_uint16()
        self.pvcam.pl_get_param(self._handle, param, pv.ATTR_ACCESS, byref(rights))
        return rights.value
    
    def set_param(self, param, value):
        """
        Write the current value of a parameter. 
        Note: for the enumerated parameter, this is the actual value to set, not
        the index.
        param (int): parameter ID (cf pv.PARAM_*)
        value (should be of the right type): value to write
        Warning: it seems to not always complain if the value written is incorrect,
        just using default instead.
        """
        # find out the type of the parameter
        tp = c_uint16()
        self.pvcam.pl_get_param(self._handle, param, pv.ATTR_TYPE, byref(tp))
        if tp.value == pv.TYPE_CHAR_PTR:
            content = str(value)
        elif tp.value in self.pv_type_to_ctype:
            content = self.pv_type_to_ctype[tp.value](value)
        elif tp.value in (pv.TYPE_VOID_PTR, pv.TYPE_VOID_PTR_PTR):
            raise ValueError("Cannot handle arguments of type pointer")
        else:
            raise NotImplementedError("Argument of unknown type %d", tp.value)

        logging.debug("Writing parameter %x as %r", param, content)        
        self.pvcam.pl_set_param(self._handle, param, byref(content))
    
    def get_enum_available(self, param):
        """
        Get all the available values for a given enumerated parameter.
        param (int): parameter ID (cf pv.PARAM_*), it must be an enumerated one
        return (dict (int -> string)): value to description
        """
        count = c_uint32()
        self.pvcam.pl_get_param(self._handle, param, pv.ATTR_COUNT, byref(count))
        
        ret = {} # int -> str
        for i in range(count.value):
            length = c_uint32()
            content = c_uint32()
            self.pvcam.pl_enum_str_length(self._handle, param, i, byref(length))
            desc = create_string_buffer(length.value)
            self.pvcam.pl_get_enum_param(self._handle, param, i, byref(content),
                                         desc, length)
            ret[content.value] = desc.value
        return ret
    
    def exp_check_status(self):
        """
        Checks the status of the current exposure (acquisition)
        returns (int): status as in pv.* (cf documentation)
        """
        status = c_int16()
        byte_cnt = c_uint32() # number of bytes already acquired: unused
        self.pvcam.pl_exp_check_status(self._handle, byref(status), byref(byte_cnt))
        return status.value
        
    def _int2version(self, raw):
        """
        Convert a raw value into version, according to the pvcam convention
        raw (int)
        returns (string)
        """
        ver = []
        ver.insert(0, raw & 0x0f) # lowest 4 bits = trivial version
        raw >>= 4
        ver.insert(0, raw & 0x0f) # next 4 bits = minor version
        raw >>= 4
        ver.insert(0, raw & 0xff) # highest 8 bits = major version
        return '.'.join(str(x) for x in ver)
        
    def getSwVersion(self):
        """
        returns a simplified software version information
        or None if unknown
        """
        try:
            ddi_ver = c_uint16()
            self.pvcam.pl_ddi_get_ver(byref(ddi_ver))
            interface = self._int2version(ddi_ver.value)
        except PVCamError:
            interface = "unknown"
        
        try:
            pv_ver = c_uint16()
            self.pvcam.pl_pvcam_get_ver(byref(pv_ver))
            sdk = self._int2version(pv_ver.value)
        except PVCamError:
            sdk = "unknown"
        
        try:
            driver = self._int2version(self.get_param(pv.PARAM_DD_VERSION))
        except PVCamError:
            driver = "unknown"

        return "driver: %s, interface: %s, SDK: %s" % (driver, interface, sdk)

    def getHwVersion(self):
        """
        returns a simplified hardware version information
        """
        versions = {pv.PARAM_CAM_FW_VERSION: "firmware",
                    # Fails on PI pvcam (although PARAM_DD_VERSION manages to
                    # read the firmware version inside the kernel)
                    pv.PARAM_PCI_FW_VERSION: "firmware board",
                    pv.PARAM_CAM_FW_FULL_VERSION: "firmware (full)",
                    pv.PARAM_CAMERA_TYPE: "camera type",
                    }
        ret = ""
        for pid, name in versions.items():
            try:
                value = self.get_param(pid)
                ret += "%s: %s " % (name, value)
            except PVCamError:
#                logging.exception("param %x cannot be accessed", pid)
                pass # skip
        
        # TODO: if we really want, we can try to look at the product name if it's
        # USB: from the name, find in in /dev/ -> read major/minor
        # -> /sys/dev/char/$major:$minor/device
        # -> read symlink canonically, remove last directory 
        # -> read "product" file 
        
        if ret == "":
            ret = "unknown"
        return ret
        
    def getModelName(self):
        """
        returns (string): name of the camara
        """
        model_name = "Princeton Instruments camera"
        
        try:
            model_name += " with CCD '%s'" % self.get_param(pv.PARAM_CHIP_NAME) 
        except PVCamError:
            pass # unknown
        
        try:
            model_name += " (s/n: %s)" % self.get_param(pv.PARAM_SERIAL_NUM)
        except PVCamError:
            pass # unknown
        
        return model_name
    
    def GetSensorSize(self):
        """
        return 2-tuple (int, int): width, height of the detector in pixel
        """
        width = self.get_param(pv.PARAM_SER_SIZE, pv.ATTR_DEFAULT)
        height = self.get_param(pv.PARAM_PAR_SIZE, pv.ATTR_DEFAULT)
        return width, height
    
    def GetMinResolution(self):
        """
        return 2-tuple (int, int): width, height of the minimum possible resolution
        """
        width = self.get_param(pv.PARAM_SER_SIZE, pv.ATTR_MIN)
        height = self.get_param(pv.PARAM_PAR_SIZE, pv.ATTR_MIN)
        return width, height
    
    def GetPixelSize(self):
        """
        return 2-tuple float, float: width, height of one pixel in m
        """
        # values from the driver are in nm
        width = self.get_param(pv.PARAM_PIX_SER_DIST, pv.ATTR_DEFAULT) * 1e-9
        height = self.get_param(pv.PARAM_PIX_PAR_DIST, pv.ATTR_DEFAULT) * 1e-9
        return width, height

    def GetTemperature(self):
        """
        returns (float) the current temperature of the captor in C
        """
        # FIXME: might need the lock (cannot be done during acquisition)
        # it's in 1/100 of C
        temp = self.get_param(pv.PARAM_TEMP) / 100
        return temp
    
    def GetTemperatureRange(self):
        mint = self.get_param(pv.PARAM_TEMP_SETPOINT, pv.ATTR_MIN) / 100
        maxt = self.get_param(pv.PARAM_TEMP_SETPOINT, pv.ATTR_MAX) / 100
        return mint, maxt
    
    # High level methods
    def setTargetTemperature(self, temp):
        """
        Change the targeted temperature of the CCD. The cooler the less dark noise.
        temp (-300 < float < 100): temperature in C, should be within the allowed range
        """
        assert((-300 <= temp) and (temp <= 100))
        # it's in 1/100 of C
        # TODO: use increment?
        self.set_param(pv.PARAM_TEMP_SETPOINT, int(round(temp * 100)))
        
        # Turn off the cooler if above room temperature
        try:
            # Note: doesn't seem to have any effect on the PIXIS
            if temp >= 20:
                self.set_param(pv.PARAM_HEAD_COOLING_CTRL, pv.HEAD_COOLING_CTRL_OFF)
                self.set_param(pv.PARAM_COOLING_FAN_CTRL, pv.COOLING_FAN_CTRL_OFF)
            else:
                self.set_param(pv.PARAM_HEAD_COOLING_CTRL, pv.HEAD_COOLING_CTRL_ON)
                self.set_param(pv.PARAM_COOLING_FAN_CTRL, pv.COOLING_FAN_CTRL_ON)
        except PVCamError:
            pass

        temp = self.get_param(pv.PARAM_TEMP_SETPOINT) / 100
        return float(temp)
        
    def updateTemperatureVA(self):
        """
        to be called at regular interval to update the temperature
        """
        if self._handle is None:
            # might happen if terminate() has just been called
            logging.info("No temperature update, camera is stopped")
            return
        
        temp = self.GetTemperature()
        self._metadata[model.MD_SENSOR_TEMP] = temp
        # it's read-only, so we change it only via _value
        self.temperature._value = temp
        self.temperature.notify(self.temperature.value)
        logging.debug("temp is %s", temp)
        
    def _getAvailableGains(self):
        """
        Find the gains supported by the device
        returns (dict of int -> float): index -> multiplier
        """
        # Gains are special: they do not use a enum type, just min/max
        ming = self.get_param(pv.PARAM_GAIN_INDEX, pv.ATTR_MIN)
        maxg = self.get_param(pv.PARAM_GAIN_INDEX, pv.ATTR_MAX)
        gains = {}
        for i in range(ming, maxg + 1):
            # seems to be correct for PIXIS and ST133
            gains[i] = 2**(i-1) 
        return gains
        
    def _setGain(self, value):
        """
        VA setter for gain (just save)
        """
        self._gain = value
        return value
        
    def _getAvailableReadoutRates(self):
        """
        Find the readout rates supported by the device
        returns (dict int -> float): for each index: frequency in Hz
        Note: this is for the current output amplifier and bit depth
        """
        # It depends on the port (output amplifier), bit depth, which we 
        # consider both fixed. 
        # PARAM_PIX_TIME (ns): the time per pixel
        # PARAM_SPDTAB_INDEX: the speed index
        # The only way to find out the rate of a speed, is to set the speed, and
        # see the new time per pixel. 
        # Note: setting the spdtab idx resets the gain

        mins = self.get_param(pv.PARAM_SPDTAB_INDEX, pv.ATTR_MIN)
        maxs = self.get_param(pv.PARAM_SPDTAB_INDEX, pv.ATTR_MAX)
        # save the current value
        current_spdtab = self.get_param(pv.PARAM_SPDTAB_INDEX)
        current_gain = self.get_param(pv.PARAM_GAIN_INDEX)
        
        rates = {}
        for i in range(mins, maxs + 1):
            # Try with this given speed tab
            self.set_param(pv.PARAM_SPDTAB_INDEX, i)
            pixel_time = self.get_param(pv.PARAM_PIX_TIME) # ns
            if pixel_time == 0:
                logging.warning("Camera reporting pixel readout time of 0 ns!")
                pixel_time = 1
            rates[i] = 1 / (pixel_time * 1e-9)

        # restore the current values
        self.set_param(pv.PARAM_SPDTAB_INDEX, current_spdtab)
        self.set_param(pv.PARAM_GAIN_INDEX, current_gain)
        return rates
    
    def _setReadoutRate(self, value):
        """
        VA setter for readout rate (just save)
        """
        self._readout_rate = value
        return value
    
    def _setBinning(self, value):
        """
        Called when "binning" VA is modified. It also updates the resolution so
        that the AOI is approximately the same.
        value (int): how many pixels horizontally and vertically
         are combined to create "super pixels"
        Note: super pixels are always square (although hw doesn't require this)
        """
        # TODO: support non square binning (for spectroscopy)
        
        prev_binning = self._binning
        self._binning = (value, value)
        
        # adapt resolution so that the AOI stays the same
        change = (prev_binning[0] / value,
                  prev_binning[1] / value)
        old_resolution = self.resolution.value
        new_resolution = (int(round(old_resolution[0] * change[0])),
                          int(round(old_resolution[1] * change[1])))
        
        self.resolution.value = new_resolution # will automatically call _storeSize
        return self._binning[0]
    
    def _storeSize(self, size):
        """
        Check the size is correct (it should) and store it ready for SetImage
        size (2-tuple int): Width and height of the image. It will be centred
         on the captor. It depends on the binning, so the same region has a size 
         twice smaller if the binning is 2 instead of 1. It must be a allowed
         resolution.
        """
        full_res = self._shape[:2]
        resolution = (int(full_res[0] // self._binning[0]),
                      int(full_res[1] // self._binning[1])) 
        assert((1 <= size[0]) and (size[0] <= resolution[0]) and
               (1 <= size[1]) and (size[1] <= resolution[1]))
        
        # Region of interest
        # center the image
        lt = ((resolution[0] - size[0]) // 2,
              (resolution[1] - size[1]) // 2)
        
        # the rectangle is defined in normal pixels (not super-pixels) from (0,0)
        self._image_rect = (lt[0] * self._binning[0], (lt[0] + size[0]) * self._binning[0] - 1,
                            lt[1] * self._binning[1], (lt[1] + size[1]) * self._binning[1] - 1)
    
    def _setResolution(self, value):
        new_res = self.resolutionFitter(value)
        self._storeSize(new_res)
        return new_res
    
    def resolutionFitter(self, size_req):
        """
        Finds a resolution allowed by the camera which fits best the requested
          resolution. 
        size_req (2-tuple of int): resolution requested
        returns (2-tuple of int): resolution which fits the camera. It is equal
         or bigger than the requested resolution
        """
        # find maximum resolution (with binning)
        resolution = self._shape[:2]
        max_size = (int(resolution[0] // self._binning[0]),
                    int(resolution[1] // self._binning[1]))
        min_res = self.resolution.range[0]
        min_size = (int(math.ceil(min_res[0] / self._binning[0])),
                    int(math.ceil(min_res[1] / self._binning[1])))
        
        # smaller than the whole sensor
        size = (min(size_req[0], max_size[0]), min(size_req[1], max_size[1]))
        
        # bigger than the minimum
        size = (max(min_size[0], size[0]), max(min_size[0], size[1]))
        
        return size

    def _setExposureTime(self, value):
        """
        Set the exposure time. It's automatically adapted to a working one.
        exp (0<float): exposure time in seconds
        returns the new exposure time
        """
        assert(0 < value)
        
        # The checks done in the VA should be enough
        # we cache it until just before the next acquisition  
        self._exposure_time = value
        return self._exposure_time
    
    def _need_update_settings(self):
        """
        returns (boolean): True if _update_settings() needs to be called
        """
        new_image_settings = self._binning + self._image_rect
        new_settings = [new_image_settings, self._exposure_time,
                        self._readout_rate, self._gain]
        return new_settings != self._prev_settings
        
    def _update_settings(self):
        """
        Commits the settings to the camera. Only the settings which have been
        modified are updated.
        Note: acquisition_lock must be taken, and acquisition must _not_ going on.
        returns (exposure, region, size):
                exposure: (float) exposure time in second
                region (pv.rgn_type): the region structure that can be used to set up the acquisition
                size (2-tuple of int): the size of the data array that will get acquired
        """
        [prev_image_settings, prev_exp_time,
                prev_readout_rate, prev_gain] = self._prev_settings

        if prev_readout_rate != self._readout_rate:
            logging.debug("Updating readout rate settings to %f Hz", self._readout_rate) 
            i = util.index_closest(self._readout_rate, self._readout_rates)
            self.set_param(pv.PARAM_SPDTAB_INDEX, i)
                
            self._metadata[model.MD_READOUT_TIME] = 1.0 / self._readout_rate # s
            # rate might affect the BPP (although on the PIXIS, it's always 16)
            self._metadata[MD_BPP] = self.get_param(pv.PARAM_BIT_DEPTH)
            
            # If readout rate is changed, gain is reset => force update
            prev_gain = None

        if prev_gain != self._gain:
            logging.debug("Updating gain to %f", self._gain)
            i = util.index_closest(self._gain, self._gains)
            self.set_param(pv.PARAM_GAIN_INDEX, i)
            self._metadata[model.MD_GAIN] = self._gain

        # prepare image (region)
        region = pv.rgn_type()
        # region is 0 indexed 
        region.s1, region.s2, region.p1, region.p2 = self._image_rect
        region.sbin, region.pbin = self._binning
        self._metadata[model.MD_BINNING] = self._binning[0] # H and V should be equal
        new_image_settings = self._binning + self._image_rect
        size = ((self._image_rect[1] - self._image_rect[0] + 1) // self._binning[0],
                (self._image_rect[3] - self._image_rect[2] + 1) // self._binning[1])

        # nothing special for the exposure time    
        self._metadata[model.MD_EXP_TIME] = self._exposure_time

        self._prev_settings = [new_image_settings, self._exposure_time, 
                               self._readout_rate, self._gain]
        
        return self._exposure_time, region, size
    
    def _allocate_buffer(self, length):
        """
        length (int): number of bytes requested by pl_exp_setup
        returns a cbuffer of the right type for an image
        """
        cbuffer = (c_uint16 * (length // 2))() # empty array
        return cbuffer
    
    def _buffer_as_array(self, cbuffer, size, metadata=None):
        """
        Converts the buffer allocated for the image as an ndarray. zero-copy
        size (2-tuple of int): width, height
        return an ndarray
        """
        p = cast(cbuffer, POINTER(c_uint16))
        ndbuffer = numpy.ctypeslib.as_array(p, (size[1], size[0])) # numpy shape is H, W 
        dataarray = model.DataArray(ndbuffer, metadata)
        return dataarray
        
    def acquireOne(self):
        """
        Set up the camera and acquire one image at the best quality for the given
          parameters.
        return (DataArray): an array containing the image with the metadata
        """
        # TODO: not used, not working
        with self.acquisition_lock:
            self.select()
            
            self.atcore.SetAcquisitionMode(1) # 1 = Single scan
            # Seems exposure needs to be re-set after setting acquisition mode
            self._prev_settings[1] = None # 1 => exposure time
            self._update_settings()
            metadata = dict(self._metadata) # duplicate
                        
            # Acquire the image
            self.atcore.StartAcquisition()
            
            size = self.resolution.value
            exposure, accumulate, kinetic = self.GetAcquisitionTimings()
            logging.debug("Accumulate time = %f, kinetic = %f", accumulate, kinetic)
            self._metadata[model.MD_EXP_TIME] = exposure
            readout = size[0] * size[1] * self._metadata[model.MD_READOUT_TIME] # s
            # kinetic should be approximately same as exposure + readout => play safe
            duration = max(kinetic, exposure + readout)
            self.WaitForAcquisition(duration + 1)
            
            cbuffer = self._allocate_buffer(size)
            self.atcore.GetMostRecentImage16(cbuffer, size[0] * size[1])
            array = self._buffer_as_array(cbuffer, size, metadata)
        
            self.atcore.FreeInternalMemory() # TODO not sure it's needed
            return array
    
    def start_flow(self, callback):
        """
        Set up the camera and acquireOne a flow of images at the best quality for the given
          parameters. Should not be called if already a flow is being acquired.
        callback (callable (DataArray) no return):
         function called for each image acquired
        """
        # if there is a very quick unsubscribe(), subscribe(), the previous
        # thread might still be running
        self.wait_stopped_flow() # no-op is the thread is not running
        self.acquisition_lock.acquire()
        
        # Set up thread
        self.acquire_thread = threading.Thread(target=self._acquire_thread_run,
                name="PVCam acquire flow thread",
                args=(callback,))
        self.acquire_thread.start()

    def _acquire_thread_run(self, callback):
        """
        The core of the acquisition thread. Runs until acquire_must_stop is set.
        """
        need_reinit = True
        retries = 0
        cbuffer = None
        try:
            while not self.acquire_must_stop.is_set():
                # need to stop acquisition to update settings
                if need_reinit or self._need_update_settings():
                    try:
                        # cancel acquisition if it's still going on
                        if self.exp_check_status() in STATUS_IN_PROGRESS:
                            self.pvcam.pl_exp_abort(self._handle, pv.CCS_HALT)
                            time.sleep(0.1)
                    except PVCamError:
                        pass # already done?
                    
                    # finish the seq if it was started
                    if cbuffer:
                        self.pvcam.pl_exp_finish_seq(self._handle, cbuffer, 0)
                        self.pvcam.pl_exp_uninit_seq()
                        
                    # The only way I've found to detect the camera is not 
                    # responding is to check for weird camera temperature
                    if self.get_param(pv.PARAM_TEMP) == TEMP_CAM_GONE:
                        self.Reinitialize() #returns only once the camera is working again 
                    
                    # With circular buffer, we could go about up to 10% faster, but
                    # everything is more complex (eg: odd buffer size will block the
                    # PIXIS), and not all hardware supports it. It would allow
                    # to do memory allocation during the acquisition, but would
                    # require a memcpy afterwards. So we keep it simple.
                    
                    exposure, region, size = self._update_settings()
                    self.pvcam.pl_exp_init_seq()
                    blength = c_uint32()
                    exp_ms = int(math.ceil(exposure * 1e3)) # ms
                    # 1 image, with 1 region
                    self.pvcam.pl_exp_setup_seq(self._handle, 1, 1, byref(region),
                                                pv.TIMED_MODE, exp_ms, byref(blength))
                    logging.debug("acquisition setup report buffer size of %d", blength.value)
                    cbuffer = self._allocate_buffer(blength.value) # TODO shall allocate a new buffer every time?
                    
                    readout_sw = size[0] * size[1] * self._metadata[model.MD_READOUT_TIME] # s
                    # tends to be very slightly bigger:
                    readout = self.get_param(pv.PARAM_READOUT_TIME) * 1e-3 # s
                    logging.debug("Computed readout time of %g s, while sdk says %g s",
                                  readout_sw, readout)
                    
                    duration = exposure + readout
                    need_reinit = False
        
                # Acquire the images
                metadata = dict(self._metadata) # duplicate
                logging.debug("starting acquisition")
                self.pvcam.pl_exp_start_seq(self._handle, cbuffer)
                metadata[model.MD_ACQ_DATE] = time.time() # time at the beginning
                
                # first we wait ourselves the 80% of expected time (which 
                # might be very long) while detecting requests for stop. 80%, 
                # because the SDK is sometimes quite pessimistic.
                must_stop = self.acquire_must_stop.wait(duration * 0.8)
                if must_stop:
                    break
                
                # then wait a bounded time to ensure the image is acquired
                try:
                    timeout = time.time() + 1
                    status = self.exp_check_status()
                    while status in STATUS_IN_PROGRESS:
                        logging.debug("status is %d", status)
                        if time.time() > timeout:
                            raise IOError("Timeout")
                        # check if we should stop
                        must_stop = self.acquire_must_stop.wait(0.01)
                        if must_stop:
                            break
                        status = self.exp_check_status()
                    
                    if must_stop:
                        break
                    if status != pv.READOUT_COMPLETE:
                        raise IOError("Acquisition status is unexpected %d" % status)

                except (IOError, PVCamError) as exp:
                    if retries > 5:
                        logging.error("Too many failures to acquire an image")
                        raise

                    logging.exception("trying again to acquire image after error")
                    try:
                        self.pvcam.pl_exp_abort(self._handle, pv.CCS_HALT)
                    except PVCamError:
                        pass
                    retries += 1
                    time.sleep(0.1)
                    need_reinit = True
                    continue
                
                array = self._buffer_as_array(cbuffer, size, metadata)
                retries = 0
                callback(array)
             
                # force the GC to non-used buffers, for some reason, without this
                # the GC runs only after we've managed to fill up the memory
                gc.collect()
        except:
            logging.exception("Unexpected failure during image acquisition")
        finally:
            # ending cleanly
            try:
                if self.exp_check_status() in STATUS_IN_PROGRESS:
                    logging.debug("aborting acquisition")
                    self.pvcam.pl_exp_abort(self._handle, pv.CCS_HALT)
            except PVCamError:
                logging.exception("Failed to abort acquisition")
                pass # status reported an error
            
            try:
                if cbuffer:
                    self.pvcam.pl_exp_finish_seq(self._handle, cbuffer, 0)
            except PVCamError:
                logging.exception("Failed to finish the acquisition properly")
        
            try:
                self.pvcam.pl_exp_uninit_seq()
            except PVCamError:
                logging.exception("Failed to finish the acquisition properly")
            
            self.acquisition_lock.release()
            logging.debug("Acquisition thread closed")
            self.acquire_must_stop.clear()
        
    
    def req_stop_flow(self):
        """
        Cancel the acquisition of a flow of images: there will not be any notify() after this function
        Note: the thread should be already running
        Note: the thread might still be running for a little while after!
        """
        assert not self.acquire_must_stop.is_set()
        self.acquire_must_stop.set()
        try:
            logging.debug("aborting acquisition from separate thread")
            self.pvcam.pl_exp_abort(self._handle, pv.CCS_HALT)
        except PVCamError:
            # probably complaining it's not possible because the acquisition is 
            # already over, so nothing to do
            logging.exception("Failed to abort acquisition")
            pass
          
    def wait_stopped_flow(self):
        """
        Waits until the end acquisition of a flow of images. Calling from the
         acquisition callback is not permitted (it would cause a dead-lock).
        """
        # "if" is to not wait if it's already finished 
        if self.acquire_must_stop.is_set():
            self.acquire_thread.join(10) # 10s timeout for safety
            if self.acquire_thread.isAlive():
                raise OSError("Failed to stop the acquisition thread")
    
    def terminate(self):
        """
        Must be called at the end of the usage
        """
        if self._temp_timer is not None:
            self._temp_timer.cancel()
            self._temp_timer = None
        
        if self._handle is not None:
            # don't touch the temperature target/cooling

            logging.debug("Shutting down the camera")
            self.pvcam.pl_cam_close(self._handle)
            self._handle = None
            del self.pvcam
            
    def __del__(self):
        self.terminate()
        
    def selfTest(self):
        """
        Check whether the connection to the camera works.
        return (boolean): False if it detects any problem
        """
        # TODO: this is pretty weak, because if we've managed to init, all
        # this already passed before
        try:
            resolution = self.GetSensorSize()
        except Exception as err:
            logging.exception("Failed to read camera resolution: " + str(err))
            return False
        
        try:
            # raises an error if camera has a problem
            self.pvcam.pl_cam_get_diags(self._handle)
        except Exception as err:
            logging.exception("Camera reports a problem: " + str(err))
            return False

        # TODO: try to acquire an image too?
        
        return True
        
    @staticmethod
    def scan():
        """
        List all the available cameras.
        Note: it's not recommended to call this method when cameras are being used
        return (list of 2-tuple: name (strin), device number (int))
        """
        pvcam = PVCamDLL()
        num_cam = c_short()
        pvcam.pl_cam_get_total(byref(num_cam))
        logging.debug("Found %d devices.", num_cam.value)
        
        cameras = []
        for i in range(num_cam.value):
            cam_name = create_string_buffer(pv.CAM_NAME_LEN)
            try:
                pvcam.pl_cam_get_name(i, cam_name)
            except PVCamError:
                logging.exception("Couldn't access camera %d", i)

            # TODO: append the resolution to the name of the camera?            
            cameras.append((cam_name.value, {"device": i}))
        
        return cameras

class PVCamDataFlow(model.DataFlow):
    def __init__(self, camera):
        """
        camera: PVCam instance ready to acquire images
        """
        model.DataFlow.__init__(self)
        self.component = weakref.proxy(camera)
        
#    def get(self):
#        # TODO if camera is already acquiring, subscribe and wait for the coming picture with an event
#        # but we should make sure that VA have not been updated in between. 
##        data = self.component.acquireOne()
#        # TODO we should avoid this: get() and acquire() simultaneously should be handled by the framework
#        # If some subscribers arrived during the acquire()
#        # FIXME
##        if self._listeners:
##            self.notify(data)
##            self.component.acquireFlow(self.notify)
##        return data
#
#        # FIXME
#        # For now we simplify by considering it as just a 1-image subscription

    
    # start/stop_generate are _never_ called simultaneously (thread-safe)
    def start_generate(self):
        try:
            self.component.start_flow(self.notify)
        except ReferenceError:
            # camera has been deleted, it's all fine, we'll be GC'd soon
            pass
    
    def stop_generate(self):
        try:
            self.component.req_stop_flow()
            # we cannot wait for the thread to stop because:
            # * it would be long
            # * we can be called inside a notify(), which is inside the thread => would cause a dead-lock
        except ReferenceError:
            # camera has been deleted, it's all fine, we'll be GC'd soon
            pass
            
    def notify(self, data):
        model.DataFlow.notify(self, data)

# vim:tabstop=4:shiftwidth=4:expandtab:spelllang=en_gb:spell:
