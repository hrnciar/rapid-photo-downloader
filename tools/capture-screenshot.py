#!/usr/bin/env python3

# Copyright (C) 2020 Damon Lynch <damonlynch@gmail.com>

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
# along with Rapid Photo Downloader. If not,
# see <http://www.gnu.org/licenses/>.


"""
Capture screenshots of Rapid Photo Downloader.
"""

__author__ = 'Damon Lynch'
__copyright__ = "Copyright 2020, Damon Lynch"
__title__ = __file__
__description__ = 'Capture screenshots of Rapid Photo Downloader.'

import subprocess
import argparse
import os
import shutil
import sys
import shlex
import glob

from PyQt5.QtGui import QImage, QColor, QGuiApplication, QPainter, QPen
from PyQt5.QtCore import QRect, Qt

# Position of window
window_x = 920
window_y = 100
# Height of titlebar in default Ubuntu 19.10 theme
titlebar_height = 37
# Window width an height
width = 1600
height = 900

# Color of top and left window borders in default Ubuntu 19.10 theme
top_border_color = QColor(163, 160, 158, 255)
left_border_color = QColor(159, 156, 154, 255)

wmctrl = shutil.which('wmctrl')
gm = shutil.which('gm')
gnome_screenshot = shutil.which('gnome-screenshot')


pictures_directory = os.path.join(os.path.expanduser('~'), 'Pictures')


def parser_options(formatter_class=argparse.HelpFormatter) -> argparse.ArgumentParser:
    """
    Construct the command line arguments for the script

    :return: the parser
    """

    parser = argparse.ArgumentParser(
        prog=__title__, formatter_class=formatter_class, description=__description__
    )

    parser.add_argument('file', help='Name of screenshot')
    parser.add_argument(
        '--screenshot', action='store_true', default=False,
        help="Screenshot that needs to be cropped, from some other tool"
    )
    parser.add_argument(
        '--titlebar', action='store_true', default=False,
        help="When moving window, move the image down by the titlebar height"
    )
    return parser


def check_requirements() -> None:
    """
    Ensure program requirements are installed
    """

    global wmctrl
    global gm
    global gnome_screenshot

    for program, package in ((wmctrl, 'wmctrl'), (gm, 'graphicsmagick'),
                             (gnome_screenshot, 'gnome-screenshot')):
        if program is None:
            print("Installing {}".format(package))
            cmd = 'sudo apt -y install {}'.format(package)
            args = shlex.split(cmd)
            subprocess.run(args)

    wmctrl = shutil.which('wmctrl')
    gm = shutil.which('gm')
    gnome_screenshot = shutil.which('gnome-screenshot')


def get_program_name() -> str:
    """
    Get program title, if it's not English

    Getting translated names automatically does not work. Not sure why.

    :return: name in title bar of Rapid Photo Downloader
    """

    cmd = '{wmctrl} -l'.format(wmctrl=wmctrl)
    args = shlex.split(cmd)
    result = subprocess.run(args, capture_output=True, universal_newlines=True)
    if result.returncode == 0:
        window_list = result.stdout
    else:
        print("Could not get window list")
        sys.exit(1)

    if "Rapid Photo Downloader" in window_list:
        return "Rapid Photo Downloader"

    names = (
        'Rapid foto allalaadija',
        'Gyors Fotó Letöltő',
        '高速写真ダウンローダ',
        'Rapid-Fotoübertragung',
    )

    for title in names:
        if title in window_list:
            return title

    print(
        "Could not determine localized program title.\n"
        "Add it to the script using output from wmctrl -l.\n"
    )
    sys.exit(1)


def extract_image(image: str) -> QImage:
    """"
    Get the program window from the screenshot by detecting its borders
    and knowing its size ahead of time
    """

    qimage = QImage(image)
    assert not qimage.isNull()

    # print("{}: {}x{}".format(image, qimage.width(), qimage.height()))

    y = qimage.height() // 2
    left = -1
    lightness = left_border_color.lightness()
    for x in range(0, qimage.width()):
        if qimage.pixelColor(x, y).lightness() <= lightness:
            left = x
            break

    if left < 0:
        sys.stderr.write('Could not locate left window border\n')
        sys.exit(1)

    x = qimage.width() // 2
    top = -1
    lightness = top_border_color.lightness()
    for y in range(0, qimage.height()):
        if qimage.pixelColor(x, y).lightness() <= lightness:
            top = y
            break

    if top < 0:
        sys.stderr.write('Could not locate top window border\n')
        sys.exit(1)

    return qimage.copy(QRect(left, top, width, height))


def add_border(image: str) -> QImage:
    """
    Add border to screenshot that was taken away by screenshot utility
    :param image: image without borders
    :return: image with borders
    """

    qimage = QImage(image)
    painter = QPainter()
    painter.begin(qimage)
    pen = QPen()
    pen.setColor(left_border_color)
    pen.setWidth(1)
    pen.setStyle(Qt.SolidLine)
    pen.setJoinStyle(Qt.MiterJoin)
    rect = QRect(0, 0, qimage.width()-1, qimage.height()-1)
    painter.setPen(pen)
    painter.drawRect(rect)
    painter.end()
    return qimage


def add_transparency(qimage: QImage) -> QImage:
    """
    Add transparent window borders according to Ubuntu 19.10 titlebar style
    :param qimage: image with non transparent top left and right corners
    :return: image with transparent top left and right corners
    """

    if not qimage.hasAlphaChannel():
        assert qimage.format() == QImage.Format_RGB32
        transparent = QImage(qimage.size(), QImage.Format_ARGB32_Premultiplied)
        transparent.fill(Qt.black)
        painter = QPainter()
        painter.begin(transparent)
        painter.drawImage(0, 0, qimage)
        painter.end()
        qimage = transparent

    image_width = qimage.width()
    y = -1
    for width in (5, 3, 2, 1):
        y += 1
        for x in range(width):
            color = qimage.pixelColor(x, y)
            color.setAlpha(0)
            qimage.setPixelColor(x, y, color)
            qimage.setPixelColor(image_width - x - 1, y, color)

    return qimage


if __name__ == '__main__':
    check_requirements()

    parser = parser_options()
    parserargs = parser.parse_args()

    app = QGuiApplication(sys.argv + ['-platform',  'offscreen'])

    filename = "{}.png".format(parserargs.file)

    image = os.path.join(pictures_directory, filename)

    if not parserargs.screenshot:
        program_name = get_program_name()
        print("Working with", program_name)

        extra = titlebar_height if parserargs.titlebar else 0

        # Adjust width and height allowing for 1px border round outside of window
        resize = "{program} -r '{program_name}' -e 0,{x},{y},{width},{height}".format(
            x=window_x + 1, y=window_y + 1 + extra,
            width=width - 2, height=height - titlebar_height - 2,
            program=wmctrl, program_name=program_name
        )
        capture = "{program} import -window root -crop {width}x{height}+{x}+{y} -quality 90 " \
                  "{file}".format(
            x=window_x, y=window_y, width=width, height=height, file=image,
            program=gm
        )
        remove_offset = "{program} convert +page {file} {file}".format(
            file=image, program=gm

        )
        cmds = (resize, capture, remove_offset)
        for cmd in cmds:
            args = shlex.split(cmd)
            if subprocess.run(args).returncode != 0:
                print("Failed to complete tasks")
                sys.exit(1)

        qimage = add_border(image)

    else:

        screenshot = glob.glob(os.path.join(pictures_directory, "Screenshot*.png"))
        if len(screenshot) == 1:
            os.rename(screenshot[0], image)
        else:
            cmd = "{program} -a -f {path}".format(program=gnome_screenshot, path=image)
            args = shlex.split(cmd)
            if subprocess.run(args).returncode != 0:
                print("Failed to capture screenshot")
                sys.exit(1)

        qimage = extract_image(image)

    qimage = add_transparency(qimage)
    qimage.save(image)
    
    cmd = '/usr/bin/eog {}'.format(image)
    args = shlex.split(cmd)
    subprocess.run(args)






