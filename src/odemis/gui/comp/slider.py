#-*- coding: utf-8 -*-
"""
@author: Rinze de Laat

Copyright © 2012 Rinze de Laat, Delmic

This file is part of Odemis.

Odemis is free software: you can redistribute it and/or modify it under the
terms of the GNU General Public License as published by the Free Software
Foundation, either version 2 of the License, or (at your option) any later
version.

Odemis is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR PURPOSE. See the GNU General Public License for more details.

You should have received a copy of the GNU General Public License along with
Odemis. If not, see http://www.gnu.org/licenses/.

"""

import wx

from wx.lib.agw.aui.aui_utilities import StepColour

import odemis.gui

from odemis.gui.log import log
from odemis.gui.img.data import getsliderBitmap, getslider_disBitmap
from .text import UnitFloatCtrl, UnitIntegerCtrl


class Slider(wx.PyPanel):
    """
    Custom Slider class
    """

    def __init__(self, parent, id=wx.ID_ANY, value=0.0, val_range=(0.0, 1.0),
                 size=(-1, -1), pos=wx.DefaultPosition, style=wx.NO_BORDER,
                 name="Slider", scale=None):

        """
        @param parent: Parent window. Must not be None.
        @param id:     Slider identifier. A value of -1 indicates a default
                       value.
        @param pos:    Slider position. If the position (-1, -1) is specified
                       then a default position is chosen.
        @param size:   Slider size. If the default size (-1, -1) is specified
                       then a default size is chosen.
        @param style:  use wx.Panel styles
        @param name:   Window name.
        @param scale:  linear (default) or
        """

        wx.PyPanel.__init__(self, parent, id, pos, size, style, name)

        self.current_value = value
        self.value_range = val_range

        self.range_span = float(val_range[1] - val_range[0])
        #event.GetX() position or Horizontal position across Panel
        self.x = 0
        #position of pointer
        self.pointerPos = 0

        #Get Pointer's bitmap
        self.bitmap = getsliderBitmap()
        self.bitmap_dis = getslider_disBitmap()

        # Pointer dimensions
        self.handle_width, self.handle_height = self.bitmap.GetSize()
        self.half_h_width = self.handle_width / 2
        self.half_h_height = self.handle_height / 2

        if scale == "cubic":
            self._percentage_to_val = self._cubic_perc_to_val
            self._val_to_percentage = self._cubic_val_to_perc
        else:
            self._percentage_to_val = lambda r0, r1, p: (r1 - r0) * p + r0
            self._val_to_percentage = lambda r0, r1, v: (float(v) - r0) / (r1 - r0)

        #Events
        self.Bind(wx.EVT_PAINT, self.OnPaint)
        self.Bind(wx.EVT_MOTION, self.OnMotion)
        self.Bind(wx.EVT_LEFT_DOWN, self.OnLeftDown)
        self.Bind(wx.EVT_LEFT_UP, self.OnLeftUp)
        self.Bind(wx.EVT_SIZE, self.OnSize)

    @staticmethod
    def _cubic_val_to_perc(r0, r1, v):
        """ Transform the value v into a fraction [0..1] of the [r0..r1] range
        using an inverse cube
        """
        assert(r0 < r1)
        p = abs((v - r0) / (r1 - r0))
        p = p**(1/3.0)
        return p

    @staticmethod
    def _cubic_perc_to_val(r0, r1, p):
        """ Transform the fraction p into a value with the range [r0..r1] using
        a cube.
        """
        assert(r0 < r1)
        p = p**3
        v = (r1 - r0) * p + r0
        return v

    def GetMin(self):
        """ Return this minumum value of the range """
        return self.value_range[0]

    def GetMax(self):
        """ Return the maximum value of the range """
        return self.value_range[1]

    def OnPaint(self, event=None):
        dc = wx.BufferedPaintDC(self)
        width, height = self.GetWidth(), self.GetHeight()
        _, half_height = width / 2, height / 2

        bgc = self.Parent.GetBackgroundColour()
        dc.SetBackground(wx.Brush(bgc, wx.SOLID))
        dc.Clear()

        fgc = self.Parent.GetForegroundColour()

        if not self.Enabled:
            fgc = StepColour(fgc, 50)


        dc.SetPen(wx.Pen(fgc, 1))

        # Main line
        dc.DrawLine(self.half_h_width, half_height,
                    width - self.half_h_width, half_height)


        dc.SetPen(wx.Pen("#DDDDDD", 1))

        # ticks
        steps = [v / 10.0 for v in range(1, 10)]
        for s in steps:
            v = (self.range_span * s) + self.value_range[0]
            pix_x = self._val_to_pixel(v) + self.half_h_width
            dc.DrawLine(pix_x, half_height - 1,
                        pix_x, half_height)


        if self.Enabled:
            dc.DrawBitmap(self.bitmap,
                          self.pointerPos,
                          half_height - self.half_h_height,
                          True)
        else:
            dc.DrawBitmap(self.bitmap_dis,
                          self.pointerPos,
                          half_height - self.half_h_height,
                          True)

        event.Skip()

    def OnLeftDown(self, event=None):
        #Capture Mouse
        self.CaptureMouse()
        self.getPointerLimitPos(event.GetX())
        self.Refresh()


    def OnLeftUp(self, event=None):
        #Release Mouse
        if self.HasCapture():
            self.ReleaseMouse()
        event.Skip()


    def getPointerLimitPos(self, xPos):
        #limit movement if X position is greater then self.width
        if xPos > self.GetWidth() - self.half_h_width:
            self.pointerPos = self.GetWidth() - self.handle_width
        #limit movement if X position is less then 0
        elif xPos < self.half_h_width:
            self.pointerPos = 0
        #if X position is between 0-self.width
        else:
            self.pointerPos = xPos - self.half_h_width

        #calculate value, based on pointer position
        self.current_value = self._pixel_to_val()


    def OnMotion(self, event=None):
        """ Mouse motion event handler """
        if self.GetCapture():
            self.getPointerLimitPos(event.GetX())
            self.Refresh()

    def OnSize(self, event=None):
        """
        If Panel is getting resize for any reason then calculate pointer's position
        based on it's new size
        """
        self.pointerPos = self._val_to_pixel()
        self.Refresh()

    def _val_to_pixel(self, val=None):
        val = self.current_value if val is None else val
        slider_width = self.GetWidth() - self.handle_width
        prcnt = self._val_to_percentage(self.value_range[0],
                                        self.value_range[1],
                                        val)
        return int(abs(slider_width * prcnt))

    def _pixel_to_val(self):
        prcnt = float(self.pointerPos) / (self.GetWidth() - self.handle_width)
        #return int((self.value_range[1] - self.value_range[0]) * prcnt + self.value_range[0])
        #return (self.value_range[1] - self.value_range[0]) * prcnt + self.value_range[0]
        return self._percentage_to_val(self.value_range[0],
                                       self.value_range[1],
                                       prcnt)

    def SetValue(self, value):
        if value < self.value_range[0]:
            self.current_value = self.value_range[0]
        elif value > self.value_range[1]:
            self.current_value = self.value_range[1]
        else:
            self.current_value = value

        self.pointerPos = self._val_to_pixel()

        self.Refresh()

    def GetWidth(self):
        return self.GetSize()[0]

    def GetHeight(self):
        return self.GetSize()[1]

    def GetValue(self):
        return self.current_value

    def GetRange(self, range):
        self.value_range = range


class TextSlider(Slider):
    """ A Slider with an extra linkes text field showing the current value """

    def __init__(self, parent, id=wx.ID_ANY, value=0.0, val_range=(0.0, 1.0),
                 size=(-1, -1), pos=wx.DefaultPosition, style=wx.NO_BORDER,
                 name="Slider", scale=None, t_size=(60, -1), unit=""):
        Slider.__init__(self, parent, id, value, val_range, size,
                        pos, style, name, scale)

        # A text control linked to the slider
        self.linked_field = None

        if isinstance(value, int):
            log.debug("Adding int field to slider")
            klass = UnitIntegerCtrl
        else:
            log.debug("Adding float field to slider")
            klass = UnitFloatCtrl

        self.linked_field = klass(self, -1,
                                  value,
                                  style=wx.NO_BORDER|wx.ALIGN_RIGHT,
                                  size=t_size,
                                  min_val=val_range[0],
                                  max_val=val_range[1],
                                  unit=unit,
                                  accuracy=2)

        self.linked_field.SetForegroundColour(odemis.gui.FOREGROUND_COLOUR_EDIT)
        self.linked_field.SetBackgroundColour(odemis.gui.BACKGROUND_COLOUR)

        self.linked_field.set_change_callback(self.SetValue)

    def getPointerLimitPos(self, xPos):
        Slider.getPointerLimitPos(self, xPos)
        self._update_linked_field(self.current_value)


    def _update_linked_field(self, value):
        """ Update any linked field to the same value as this slider
        """
        if self.linked_field.GetValue() != value:
            if hasattr(self.linked_field, 'SetValueStr'):
                self.linked_field.SetValueStr(value)
            else:
                self.linked_field.SetValue(value)

    def set_linked_field(self, text_ctrl):
        self.linked_field = text_ctrl

    def OnLeftUp(self, event=None):
        #Release Mouse
        if self.HasCapture():
            self.ReleaseMouse()
            self._update_linked_field(self.current_value)

        event.Skip()

    def GetWidth(self):
        return Slider.GetWidth(self) - self.linked_field.GetSize()[0]

    def OnPaint(self, event=None):
        t_x = self.GetSize()[0] - self.linked_field.GetSize()[0]
        t_y = -2
        self.linked_field.SetPosition((t_x, t_y))

        Slider.OnPaint(self, event)
