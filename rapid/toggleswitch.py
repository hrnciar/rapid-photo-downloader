# Copyright (C) 2016 Damon Lynch <damonlynch@gmail.com>

# This file is part of Rapid Photo Downloader.
#
# Rapid Photo Downloader is free software: you can redistribute it and/or
# modify it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Rapid Photo Downloader is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Rapid Photo Downloader.  If not,
# see <http://www.gnu.org/licenses/>.

"""
Toggle Switch reminiscent of Android On/off switches:
https://www.google.com/design/spec/components/selection-controls.html

Visual style is like a rounded square, close to how Gnome
handles it, but without the "ON"/"OFF text.

Inspiration:
http://stackoverflow.com/questions/14780517/toggle-switch-in-qt
http://thesmithfam.org/blog/2010/03/10/fancy-qslider-stylesheet/
"""

__author__ = 'Damon Lynch'
__copyright__ = "Copyright 2016, Damon Lynch"

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QPalette, QColor, QFont,QFontMetrics
from PyQt5.QtWidgets import QSlider, QApplication


class QToggleSwitch(QSlider):
    def __init__(self, parent=None):
        super().__init__(Qt.Horizontal, parent)

        self.base_height = QFontMetrics(QFont()).height()

        width = self.base_height * 3
        self.widgetWidth = width
        self.handleWidth = width // 2
        self.sliderRange = width
        self.sliderMidPoint = width // 2
        self.setRange(0, self.sliderRange)

        self.setMaximumWidth(self.widgetWidth)
        self.setFixedHeight(self.base_height + 6)

        self.setStyleSheet(self.stylesheet())

        self.actionTriggered.connect(self.onActionTriggered)
        self.sliderReleased.connect(self.onSliderRelease)

    def stylesheet(self):
        shading_intensity = 104
        windowColor = (QPalette().color(QPalette().Window))  # type: QColor

        baseName = (QPalette().color(QPalette().Base)).name()

        handleLightName = (QPalette().color(QPalette().Light)).name() # type: QColor
        handleDarkName = (QPalette().color(QPalette().Dark)).name()  # type: QColor
        handleHoverLightName = (QPalette().color(QPalette().Light)).lighter(shading_intensity).name()
        handleHoverDarkName = (QPalette().color(QPalette().Dark)).darker(shading_intensity).name()

        insetDarkName = windowColor.darker(108).name()
        insetLightName = windowColor.darker(102).name()

        highlightColor = (QPalette().color(QPalette().Highlight))  # type: QColor
        highlightLightName = highlightColor.lighter(110).name()
        highlightDarkName = highlightColor.darker(130).name()

        return """
            QSlider::groove:horizontal {
                border: 1px solid #bbb;
                background: %(baseName)s;
                height: %(height)s px;
                border-radius: 6px;
            }

            QSlider::sub-page:horizontal {
            background: qlineargradient(x1: 0, y1: 0.2, x2: 1, y2: 1,
                stop: 0 %(highlightDarkName)s, stop: 1 %(highlightLightName)s);
            border: 1px solid #777;
            height: 10px;
            border-radius: 6px;
            }

            QSlider::add-page:horizontal {
            background: qlineargradient(x1: 0, y1: 0.2, x2: 1, y2: 1,
                stop: 0 %(insetDarkName)s, stop: 1 %(insetLightName)s);
            border: 1px solid #777;
            height: 10px;
            border-radius: 6px;
            }

            QSlider::handle:horizontal {
            background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                stop:0 %(handleLightName)s, stop:1 %(handleDarkName)s);
            border: 1px solid #777;
            width: %(buttonWidth)s px;
            margin-top: -1px;
            margin-bottom: -1px;
            border-radius: 6px;
            }

            QSlider::handle:horizontal:hover {
            background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                stop:0 %(handleHoverLightName)s, stop:1 %(handleHoverDarkName)s);
            border: 1px solid #444;
            border-radius: 6px;
            }

            QSlider::sub-page:horizontal:disabled {
            background: #bbb;
            border-color: #999;
            }

            QSlider::add-page:horizontal:disabled {
            background: #eee;
            border-color: #999;
            }

            QSlider::handle:horizontal:disabled {
            background: #eee;
            border: 1px solid #aaa;
            border-radius: 6px;
            }
        """ % dict(buttonWidth=self.handleWidth,
                   handleLightName=handleLightName,
                   handleDarkName=handleDarkName,
                   handleHoverLightName=handleHoverLightName,
                   handleHoverDarkName=handleHoverDarkName,
                   baseName=baseName,
                   highlightDarkName=highlightDarkName,
                   highlightLightName=highlightLightName,
                   height=self.base_height,
                   insetDarkName=insetDarkName,
                   insetLightName=insetLightName)

    def onActionTriggered(self, action: int) -> None:
        if action != 7:
            if action % 2:
                self.setValue(self.sliderRange)
            else:
                self.setValue(0)

    def onSliderRelease(self) -> None:
        if self.sliderPosition() >= self.sliderMidPoint:
            self.setValue(self.sliderRange)
        else:
            self.setValue(0)

    def on(self) -> bool:
        return self.value() == self.sliderRange

    def setOn(self, on: bool=True) -> None:
        if on:
            self.setValue(self.sliderRange)
        else:
            self.setValue(0)

if __name__ == "__main__":
    import sys
    app = QApplication(sys.argv)
    b = QToggleSwitch()
    b.show()
    sys.exit(app.exec_())