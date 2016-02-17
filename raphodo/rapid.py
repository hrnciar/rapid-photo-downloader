#!/usr/bin/env python3

# Copyright (C) 2011-2016 Damon Lynch <damonlynch@gmail.com>

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
Primary logic for Rapid Photo Downloader.

QT related function and variable names use CamelCase.
Everything else should follow PEP 8.
Project line length: 100 characters (i.e. word wrap at 99)
"""

__author__ = 'Damon Lynch'
__copyright__ = "Copyright 2011-2016, Damon Lynch"

import sys
import logging

import shutil
import datetime
import locale
locale.setlocale(locale.LC_ALL, '')
import pickle
from collections import namedtuple
import platform
import argparse
from typing import Optional, Tuple, List, Dict
from time import sleep

from gettext import gettext as _

import gi
gi.require_version('Notify', '0.7')
from gi.repository import Notify

try:
    gi.require_version('Unity', '7.0')
    from gi.repository import Unity
    have_unity = True
except ImportError:
    have_unity = False

import zmq
import psutil
import gphoto2 as gp
import sortedcontainers
from PyQt5 import QtCore, QtWidgets, QtGui
from PyQt5.QtCore import (QThread, Qt, QStorageInfo, QSettings, QPoint,
                          QSize, QTimer, QTextStream, QModelIndex,
                          QRect, QItemSelection, QItemSelectionModel, pyqtSlot,
                          QObjectCleanupHandler)
from PyQt5.QtGui import (QIcon, QPixmap, QImage, QFont, QColor, QPalette, QFontMetrics,
                         QGuiApplication, QPainter, QMoveEvent)
from PyQt5.QtWidgets import (QAction, QApplication, QMainWindow, QMenu,
                             QPushButton, QWidget, QDialogButtonBox,
                             QProgressBar, QSplitter,
                             QHBoxLayout, QVBoxLayout, QDialog, QLabel,
                             QComboBox, QGridLayout, QCheckBox, QSizePolicy,
                             QMessageBox, QDesktopWidget, QAbstractItemView, QSplashScreen)
from PyQt5.QtNetwork import QLocalSocket, QLocalServer

from storage import (ValidMounts, CameraHotplug, UDisks2Monitor,
                     GVolumeMonitor, have_gio, has_non_empty_dcim_folder,
                     mountPaths, get_desktop_environment,
                     gvfs_controls_mounts, get_default_file_manager)
from interprocess import (PublishPullPipelineManager,
                          PushPullDaemonManager,
                          ScanArguments,
                          CopyFilesArguments,
                          RenameAndMoveFileData,
                          BackupArguments,
                          BackupResults,
                          CopyFilesResults,
                          RenameAndMoveFileResults,
                          ScanResults,
                          BackupFileData,
                          OffloadData,
                          OffloadResults)
from devices import (Device, DeviceCollection, BackupDevice,
                     BackupDeviceCollection)
from preferences import (Preferences, ScanPreferences)
from constants import (BackupLocationType, DeviceType, ErrorType,
                       FileType, DownloadStatus, RenameAndMoveStatus,
                       photo_rename_test, ApplicationState,
                       PROGRAM_NAME, job_code_rename_test, CameraErrorCode,
                       photo_rename_simple_test, ThumbnailBackgroundName, emptyViewHeight,
                       DeviceState, BorderName)
import constants
from thumbnaildisplay import (ThumbnailView, ThumbnailListModel, ThumbnailDelegate, DownloadTypes,
                              DownloadStats, ThumbnailSortFilterProxyModel)
from devicedisplay import (DeviceModel, DeviceView, DeviceDelegate)
from proximity import (TemporalProximityModel, TemporalProximityView,
                       TemporalProximityDelegate, TemporalProximityGroups)
from utilities import (same_file_system, make_internationalized_list,
                       thousands, addPushButtonLabelSpacer,
                       format_size_for_user)
from rpdfile import (RPDFile, file_types_by_number, PHOTO_EXTENSIONS,
                     VIDEO_EXTENSIONS, FileTypeCounter, OTHER_PHOTO_EXTENSIONS, FileSizeSum)
import downloadtracker
from cache import ThumbnailCacheSql
from metadataphoto import exiv2_version, gexiv2_version
from metadatavideo import EXIFTOOL_VERSION
from camera import gphoto2_version, python_gphoto2_version
from rpdsql import DownloadedSQL
from generatenameconfig import *
from rotatedpushbutton import RotatedButton
from toppushbutton import TopPushButton
from filebrowse import FileSystemView, FileSystemModel
from toggleview import QToggleView
import __about__

BackupMissing = namedtuple('BackupMissing', 'photo, video')

# Avoid segfaults at exit. Recommended by Kovid Goyal:
# https://www.riverbankcomputing.com/pipermail/pyqt/2016-February/036932.html
app = None  # type: 'QtSingleApplication'

class RenameMoveFileManager(PushPullDaemonManager):
    message = QtCore.pyqtSignal(bool, RPDFile, int, QPixmap)
    sequencesUpdate = QtCore.pyqtSignal(int, list)
    def __init__(self, context: zmq.Context, logging_level: int):
        super().__init__(context, logging_level)
        self._process_name = 'Rename and Move File Manager'
        self._process_to_run = 'renameandmovefile.py'

    def rename_file(self, data: RenameAndMoveFileData):
        self.send_message_to_worker(data)

    def process_sink_data(self):
        data = pickle.loads(self.content) # type: RenameAndMoveFileResults
        if data.move_succeeded is not None:
            if data.png_data is not None:
                thumbnail = QImage.fromData(data.png_data)
                thumbnail = QPixmap.fromImage(thumbnail)
            else:
                thumbnail = QPixmap()
            self.message.emit(data.move_succeeded, data.rpd_file,
                              data.download_count, thumbnail)
        else:
            assert data.stored_sequence_no is not None
            assert data.downloads_today is not None
            assert isinstance(data.downloads_today, list)
            self.sequencesUpdate.emit(data.stored_sequence_no,
                                      data.downloads_today)


class OffloadManager(PushPullDaemonManager):
    message = QtCore.pyqtSignal(TemporalProximityGroups)
    def __init__(self, context: zmq.Context, logging_level: int):
        super().__init__(context, logging_level)
        self._process_name = 'Offload Manager'
        self._process_to_run = 'offload.py'

    def assign_work(self, data: OffloadData):
        self.send_message_to_worker(data)

    def process_sink_data(self):
        data = pickle.loads(self.content) # type: OffloadResults
        if data.proximity_groups is not None:
            self.message.emit(data.proximity_groups)


class ScanManager(PublishPullPipelineManager):
    message = QtCore.pyqtSignal(bytes)
    def __init__(self, context: zmq.Context, logging_level: int):
        super().__init__(context, logging_level)
        self._process_name = 'Scan Manager'
        self._process_to_run = 'scan.py'

    def process_sink_data(self):
        self.message.emit(self.content)


class BackupManager(PublishPullPipelineManager):
    """
    Each backup "device" (it could be an external drive, or a user-
    specified path on the local file system) has associated with it one
    worker process. For example if photos and videos are both being
    backed up to the same external hard drive, one worker process
    handles both the photos and the videos. However if photos are being
    backed up to one drive, and videos to another, there would be a
    worker process for each drive (2 in total).
    """
    message = QtCore.pyqtSignal(int, bool, bool, RPDFile)
    bytesBackedUp = QtCore.pyqtSignal(bytes)

    def __init__(self, context: zmq.Context, logging_level: int) -> None:
        super().__init__(context, logging_level)
        self._process_name = 'Backup Manager'
        self._process_to_run = 'backupfile.py'

    def add_device(self, device_id: int, backup_arguments: BackupArguments) -> None:
        self.start_worker(device_id, backup_arguments)

    def remove_device(self, device_id: int) -> None:
        self.stop_worker(device_id)

    def backup_file(self, data: BackupFileData, device_id: int) -> None:
        self.send_message_to_worker(data, device_id)

    def process_sink_data(self) -> None:
        data = pickle.loads(self.content) # type: BackupResults
        if data.total_downloaded is not None:
            assert data.scan_id is not None
            assert data.chunk_downloaded >= 0
            assert data.total_downloaded >= 0
            # Emit the unpickled data, as when PyQt converts an int to a
            # C++ int, python ints larger that the maximum C++ int are
            # corrupted
            self.bytesBackedUp.emit(self.content)
        else:
            assert data.backup_succeeded is not None
            assert data.do_backup is not None
            assert data.rpd_file is not None
            self.message.emit(data.device_id, data.backup_succeeded,
                              data.do_backup, data.rpd_file)


class CopyFilesManager(PublishPullPipelineManager):
    message = QtCore.pyqtSignal(bool, RPDFile, int)
    tempDirs = QtCore.pyqtSignal(int, str,str)
    bytesDownloaded = QtCore.pyqtSignal(bytes)

    def __init__(self, context: zmq.Context, logging_level: int) -> None:
        super().__init__(context, logging_level)
        self._process_name = 'Copy Files Manager'
        self._process_to_run = 'copyfiles.py'

    def process_sink_data(self) -> None:
        data = pickle.loads(self.content) # type: CopyFilesResults
        if data.total_downloaded is not None:
            assert data.scan_id is not None
            assert data.chunk_downloaded >= 0
            assert data.total_downloaded >= 0
            # Emit the unpickled data, as when PyQt converts an int to a
            # C++ int, python ints larger that the maximum C++ int are
            # corrupted
            self.bytesDownloaded.emit(self.content)

        elif data.copy_succeeded is not None:
            assert data.rpd_file is not None
            assert data.download_count is not None
            self.message.emit(data.copy_succeeded, data.rpd_file,
                              data.download_count)

        else:
            assert (data.photo_temp_dir is not None or
                    data.video_temp_dir is not None)
            assert data.scan_id is not None
            self.tempDirs.emit(data.scan_id, data.photo_temp_dir,
                               data.video_temp_dir)

class JobCodeDialog(QDialog):
    def __init__(self, parent, job_codes: list) -> None:
        super().__init__(parent)
        self.rapidApp = parent # type: RapidWindow
        instructionLabel = QLabel(_('Enter a new Job Code, or select a '
                                    'previous one'))
        self.jobCodeComboBox = QComboBox()
        self.jobCodeComboBox.addItems(job_codes)
        self.jobCodeComboBox.setEditable(True)
        self.jobCodeComboBox.setInsertPolicy(QComboBox.InsertAtTop)
        jobCodeLabel = QLabel(_('&Job Code:'))
        jobCodeLabel.setBuddy(self.jobCodeComboBox)
        self.rememberCheckBox = QCheckBox(_("&Remember this choice"))
        self.rememberCheckBox.setChecked(parent.prefs.remember_job_code)
        buttonBox = QDialogButtonBox(QDialogButtonBox.Ok|
                                     QDialogButtonBox.Cancel)
        grid = QGridLayout()
        grid.addWidget(instructionLabel, 0, 0, 1, 2)
        grid.addWidget(jobCodeLabel, 1, 0)
        grid.addWidget(self.jobCodeComboBox, 1, 1)
        grid.addWidget(self.rememberCheckBox, 2, 0, 1, 2)
        grid.addWidget(buttonBox, 3, 0, 1, 2)
        grid.setColumnStretch(1, 1)
        self.setLayout(grid)
        self.setWindowTitle(_('Enter a Job Code'))

        buttonBox.accepted.connect(self.accept)
        buttonBox.rejected.connect(self.reject)

    @pyqtSlot()
    def accept(self):
        self.job_code = self.jobCodeComboBox.currentText()
        self.remember = self.rememberCheckBox.isChecked()
        self.rapidApp.prefs.remember_job_code = self.remember
        super().accept()


class JobCode:
    def __init__(self, parent):
        self.rapidApp = parent
        self.job_code = ''
        self.need_job_code_for_naming = parent.prefs.any_pref_uses_job_code()
        self.prompting_for_job_code = False

    def get_job_code(self):
        if not self.prompting_for_job_code:
            self.prompting_for_job_code = True
            dialog = JobCodeDialog(self.rapidApp,
                                   self.rapidApp.prefs.job_codes)
            if dialog.exec():
                self.prompting_for_job_code = False
                job_code = dialog.job_code
                if job_code:
                    if dialog.remember:
                        # If the job code is already in the
                        # preference list, move it to the front
                        job_codes = self.rapidApp.prefs.job_codes.copy()
                        while job_code in job_codes:
                            job_codes.remove(job_code)
                        # Add the just chosen Job Code to the front
                        self.rapidApp.prefs.job_codes = [job_code] + job_codes
                    self.job_code = job_code
                    self.rapidApp.startDownload()
            else:
                self.prompting_for_job_code = False


    def need_to_prompt_on_auto_start(self):
        return not self.job_code and self.need_job_code_for_naming

    def need_to_prompt(self):
        return self.need_job_code_for_naming and not self.prompting_for_job_code

class ThisComputerWidget(QWidget):
    def __init__(self, objectName: str,
                 view: DeviceView,
                 fileSystemView: FileSystemView,
                 parent: QWidget=None) -> None:

        super().__init__(parent)
        self.setObjectName(objectName)
        layout = QVBoxLayout()
        layout.setContentsMargins(1, 1, 1, 1)
        layout.setSpacing(0)
        self.setLayout(layout)

        # TODO specify border color value from derived value or create new style
        style = 'QWidget#%(objectName)s {border: 1px solid %(borderName)s;}' % dict(
            objectName=objectName, borderName=BorderName)
        self.setStyleSheet(style)

        self.view = view
        self.fileSystemView = fileSystemView
        layout.addWidget(self.view)
        layout.addStretch()
        layout.addWidget(self.fileSystemView, 5)
        self.view.setStyleSheet('QListView {border: 0px solid red;}')
        self.fileSystemView.setStyleSheet('FileSystemView {border: 0px solid red;}')

    def setViewVisible(self, visible: bool) -> None:
        self.view.setVisible(visible)

class RapidWindow(QMainWindow):
    def __init__(self, auto_detect: Optional[bool]=None,
                 this_computer_path: Optional[str]=None,
                 photo_download_folder: Optional[str]=None,
                 video_download_folder: Optional[str]=None,
                 backup: Optional[bool]=None,
                 backup_auto_detect: Optional[bool]=None,
                 photo_backup_identifier: Optional[str]=None,
                 video_backup_identifier: Optional[str]=None,
                 photo_backup_location: Optional[str]=None,
                 video_backup_location: Optional[str]=None,
                 ignore_other_photo_types: Optional[bool]=None,
                 thumb_cache: Optional[bool]=None,
                 parent=None) -> None:

        self.do_init = QtCore.QEvent.registerEventType()
        super().__init__(parent)
        # Process Qt events - in this case, possible closing of splash screen
        app.processEvents()

        # Three values to handle window position quirks under X11:
        self.window_show_requested_time = None  # type: datetime.datetime
        self.window_move_triggered_count = 0
        self.windowPositionDelta = QPoint(0, 0)

        self.setFocusPolicy(Qt.StrongFocus)

        self.ignore_other_photo_types = ignore_other_photo_types
        self.application_state = ApplicationState.normal
        self.prompting_for_user_action = {}  # type: Dict[Device, QMessageBox]

        for version in get_versions():
            logging.info('%s', version)

        self.context = zmq.Context()

        self.setWindowTitle(_("Rapid Photo Downloader"))
        self.readWindowSettings(app)
        self.prefs = Preferences()

        if thumb_cache is not None:
            logging.debug("Use thumbnail cache: %s", thumb_cache)
            self.prefs.use_thumbnail_cache = thumb_cache

        self.setupWindow()

        if auto_detect is not None:
            self.prefs.device_autodetection = auto_detect
        else:
            logging.info("Device autodetection: %s", self.prefs.device_autodetection)

        if this_computer_path is not None:
            if this_computer_path:
                self.prefs.this_computer_source = True
                self.prefs.this_computer_path = this_computer_path
            else:
                self.prefs.this_computer_source = False
                self.prefs.this_computer_path = ''
        elif self.prefs.this_computer_source:
            logging.info("This Computer path: %s", self.prefs.this_computer_path)
        else:
            logging.info("This Computer is not used as a download source")

        if photo_download_folder is not None:
            self.prefs.photo_download_folder = photo_download_folder
        else:
            logging.info("Photo download location: %s", self.prefs.photo_download_folder)
        if video_download_folder is not None:
            self.prefs.video_download_folder = video_download_folder
        else:
            logging.info("Video download location: %s", self.prefs.video_download_folder)

        if backup is not None:
            self.prefs.backup_files = backup
        else:
            logging.info("Backing up files: %s", self.prefs.backup_files)
            
        if backup_auto_detect is not None:
            self.prefs.backup_device_autodetection = backup_auto_detect
        elif self.prefs.backup_files:
            logging.info("Backup device auto detection: %s", self.prefs.backup_device_autodetection)
            
        if photo_backup_identifier is not None:
            self.prefs.photo_backup_identifier = photo_backup_identifier
        elif self.prefs.backup_files and self.prefs.backup_device_autodetection:
            logging.info("Photo backup identifier: %s", self.prefs.photo_backup_identifier)

        if video_backup_identifier is not None:
            self.prefs.video_backup_identifier = video_backup_identifier
        elif self.prefs.backup_files and self.prefs.backup_device_autodetection:
            logging.info("video backup identifier: %s", self.prefs.video_backup_identifier)
            
        if photo_backup_location is not None:
            self.prefs.backup_photo_location = photo_backup_location
        elif self.prefs.backup_files and not self.prefs.backup_device_autodetection:
            logging.info("Photo backup location: %s", self.prefs.backup_photo_location)

        if video_backup_location is not None:
            self.prefs.backup_video_location = video_backup_location
        elif self.prefs.backup_files and not self.prefs.backup_device_autodetection:
            logging.info("video backup location: %s", self.prefs.backup_video_location)

        self.prefs.auto_download_at_startup = False
        self.prefs.verify_file = False
        self.prefs.photo_rename = photo_rename_test
        # self.prefs.photo_rename = photo_rename_simple_test
        # self.prefs.photo_rename = job_code_rename_test
        self.prefs.backup_files = False
        self.prefs.backup_device_autodetection = True

        centralWidget = QWidget()

        # Don't call processEvents() after initiating 0MQ, as it can
        # cause "Interrupted system call" errors
        app.processEvents()

        self.thumbnailView = ThumbnailView(self)
        self.thumbnailModel = ThumbnailListModel(parent=self, logging_level=logging_level)
        self.thumbnailProxyModel = ThumbnailSortFilterProxyModel(self)
        self.thumbnailProxyModel.setSourceModel(self.thumbnailModel)
        self.thumbnailView.setModel(self.thumbnailProxyModel)
        self.thumbnailView.setItemDelegate(ThumbnailDelegate(self))

        self.temporalProximityView = TemporalProximityView()
        self.temporalProximityModel = TemporalProximityModel(self)
        self.temporalProximityView.setModel(self.temporalProximityModel)
        self.temporalProximityDelegate = TemporalProximityDelegate(self)
        self.temporalProximityView.setItemDelegate(self.temporalProximityDelegate)
        self.temporalProximityView.selectionModel().selectionChanged.connect(
                                                self.proximitySelectionChanged)

        # Devices are cameras and external partitions
        self.devices = DeviceCollection()
        self.deviceView = DeviceView(self)
        self.deviceModel = DeviceModel(self)
        self.deviceView.setModel(self.deviceModel)
        self.deviceView.setItemDelegate(DeviceDelegate(self))

        # This computer is any local path
        self.thisComputerView = DeviceView(self)
        self.thisComputerModel = DeviceModel(self)
        self.thisComputerView.setModel(self.thisComputerModel)
        self.thisComputerView.setItemDelegate(DeviceDelegate(self))

        self._mapModel = {DeviceType.path: self.thisComputerModel,
                         DeviceType.camera: self.deviceModel,
                         DeviceType.volume: self.deviceModel}
        self._mapView = {DeviceType.path: self.thisComputerView,
                         DeviceType.camera: self.deviceView,
                         DeviceType.volume: self.deviceView}

        self.fileSystemModel = FileSystemModel(self)
        self.fileSystemView = FileSystemView(self)
        self.fileSystemView.setModel(self.fileSystemModel)
        self.fileSystemView.hideColumns()
        self.fileSystemView.setRootIndex(self.fileSystemModel.index('/'))
        if self.prefs.this_computer_path:
            deviceLocationIndex = self.fileSystemModel.index(self.prefs.this_computer_path)
            self.fileSystemView.setExpanded(deviceLocationIndex, True)
        self.fileSystemView.activated.connect(self.thisComputerPathChosen)
        self.fileSystemView.clicked.connect(self.thisComputerPathChosen)

        self.createActions()
        self.createLayoutAndButtons(centralWidget)
        self.createMenus()

        # a main-window application must have one and only one central widget
        self.setCentralWidget(centralWidget)

        # defer full initialisation (slow operation) until gui is visible
        QtWidgets.QApplication.postEvent(
            self, QtCore.QEvent(self.do_init), QtCore.Qt.LowEventPriority - 1)

    def mapModel(self, scan_id: int) -> DeviceModel:
        return self._mapModel[self.devices[scan_id].device_type]

    def mapView(self, scan_id: int) -> DeviceView:
        return self._mapView[self.devices[scan_id].device_type]

    def readWindowSettings(self, app: 'QtSingleApplication'):
        settings = QSettings()
        settings.beginGroup("MainWindow")
        desktop = app.desktop() # type: QDesktopWidget

        # Calculate window sizes
        available = desktop.availableGeometry(desktop.primaryScreen())  # type: QRect
        screen = desktop.screenGeometry(desktop.primaryScreen())  # type: QRect
        default_width = available.width() // 2
        default_x = screen.width() - default_width
        default_height = available.height()
        default_y = screen.height() - default_height
        pos = settings.value("windowPosition", QPoint(default_x, default_y))
        size = settings.value("windowSize", QSize(default_width, default_height))
        settings.endGroup()
        self.resize(size)
        self.move(pos)

    def writeWindowSettings(self):
        settings = QSettings()
        settings.beginGroup("MainWindow")
        windowPos = self.pos() + self.windowPositionDelta
        if windowPos.x() < 0:
            windowPos.setX(0)
        if windowPos.y() < 0:
            windowPos.setY(0)
        settings.setValue("windowPosition", windowPos)
        settings.setValue("windowSize", self.size())
        settings.setValue("horizontalSplitterSizes", self.horizontalSplitter.saveState())
        settings.setValue("sourceButtonPressed", self.sourceButton.isChecked())
        settings.setValue("proximityButtonPressed", self.proximityButton.isChecked())
        settings.setValue("leftPanelSplitterSizes", self.leftPanelSplitter.saveState())
        settings.endGroup()

    def moveEvent(self, event: QMoveEvent) -> None:
        """
        Handle quirks in window positioning.

        X11 has a feature where the window managager can decorate the
        windows. A side effect of this is that the position returned by
        window.pos() can be different between restoring the position
        from the settings, and saving the position at application exit, even if
        the user never moved the window.
        """

        super().moveEvent(event)
        self.window_move_triggered_count += 1

        if self.window_show_requested_time is None:
            pass
            # self.windowPositionDelta = QPoint(0, 0)
        elif self.window_move_triggered_count == 2:
            if (datetime.datetime.now() - self.window_show_requested_time).total_seconds() < 1.0:
                self.windowPositionDelta = event.oldPos() - self.pos()
                logging.debug("Window position quirk delta: %s", self.windowPositionDelta)
            self.window_show_requested_time = None

    def setupWindow(self):
        self.basic_status_message = None
        status = self.statusBar()
        self.downloadProgressBar = QProgressBar()
        self.downloadProgressBar.setMaximumWidth(QFontMetrics(QGuiApplication.font()).height() * 9)
        status.addPermanentWidget(self.downloadProgressBar, .1)

    def event(self, event):
        # Code borrowed from Jim Easterbrook
        if event.type() != self.do_init:
            return QtWidgets.QMainWindow.event(self, event)
        event.accept()
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            self.initialise()
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        return True

    def initialise(self):
        # Setup notification system
        self.have_libnotify = Notify.init('rapid-photo-downloader')

        self.file_manager = get_default_file_manager()

        self.program_svg = ':/rapid-photo-downloader.svg'
        # Initialise use of libgphoto2
        self.gp_context = gp.Context()

        self.validMounts = ValidMounts(onlyExternalMounts=self.prefs.only_external_mounts)

        self.job_code = JobCode(self)

        desktop_env = get_desktop_environment()
        self.unity_progress = desktop_env.lower() == 'unity' and have_unity
        if self.unity_progress:
            self.deskop_launcher = Unity.LauncherEntry.get_for_desktop_id(
                "rapid-photo-downloader.desktop")
            if self.deskop_launcher is None:
                self.unity_progress = False

        logging.debug("Desktop environment: %s", desktop_env)
        logging.debug("Have GIO module: %s", have_gio)
        self.gvfsControlsMounts = gvfs_controls_mounts() and have_gio
        if have_gio:
            logging.debug("Using GIO: %s", self.gvfsControlsMounts)

        if not self.gvfsControlsMounts:
            # Monitor when the user adds or removes a camera
            self.cameraHotplug = CameraHotplug()
            self.cameraHotplugThread = QThread()
            self.cameraHotplug.moveToThread(self.cameraHotplugThread)
            self.cameraHotplug.cameraAdded.connect(self.cameraAdded)
            self.cameraHotplug.cameraRemoved.connect(self.cameraRemoved)
            # Start the monitor only on the thread it will be running on
            self.cameraHotplug.startMonitor()
            self.cameraHotplug.enumerateCameras()

            if self.cameraHotplug.cameras:
                logging.debug("Camera Hotplug found %d cameras:", len(self.cameraHotplug.cameras))
                for port, model in self.cameraHotplug.cameras.items():
                    logging.debug("%s at %s", model, port)

            # Monitor when the user adds or removes a partition
            self.udisks2Monitor = UDisks2Monitor(self.validMounts)
            self.udisks2MonitorThread = QThread()
            self.udisks2Monitor.moveToThread(self.udisks2MonitorThread)
            self.udisks2Monitor.partitionMounted.connect(self.partitionMounted)
            self.udisks2Monitor.partitionUnmounted.connect(
                self.partitionUmounted)
            # Start the monitor only on the thread it will be running on
            self.udisks2Monitor.startMonitor()

        #Track the unmounting of cameras by port and model
        self.cameras_to_unmount = {}

        if self.gvfsControlsMounts:
            self.gvolumeMonitor = GVolumeMonitor(self.validMounts)
            self.gvolumeMonitor.cameraUnmounted.connect(self.cameraUnmounted)
            self.gvolumeMonitor.cameraMounted.connect(self.cameraMounted)
            self.gvolumeMonitor.partitionMounted.connect(self.partitionMounted)
            self.gvolumeMonitor.partitionUnmounted.connect(self.partitionUmounted)
            self.gvolumeMonitor.volumeAddedNoAutomount.connect(self.noGVFSAutoMount)
            self.gvolumeMonitor.cameraPossiblyRemoved.connect(self.cameraRemoved)

        # Track the creation of temporary directories
        self.temp_dirs_by_scan_id = {}

        # Track which downloads are running
        self.active_downloads_by_scan_id = set()

        # Track the time a download commences
        self.download_start_time = None

        # Whether a system wide notification message should be shown
        # after a download has occurred in parallel
        self.display_summary_notification = False

        self.download_tracker = downloadtracker.DownloadTracker()

        # Values used to display how much longer a download will take
        self.time_remaining = downloadtracker.TimeRemaining()
        self.time_check = downloadtracker.TimeCheck()

        # Offload process is used to offload work that could otherwise
        # cause this process and thus the GUI to become unresponsive
        self.offloadThread = QThread()
        self.offloadmq = OffloadManager(self.context, logging_level)
        self.offloadmq.moveToThread(self.offloadThread)

        self.offloadThread.started.connect(self.offloadmq.run_sink)
        self.offloadmq.message.connect(self.proximityGroupsGenerated)

        QTimer.singleShot(0, self.offloadThread.start)
        self.offloadmq.start()

        self.renameThread = QThread()
        self.renamemq = RenameMoveFileManager(self.context, logging_level)
        self.renamemq.moveToThread(self.renameThread)

        self.renameThread.started.connect(self.renamemq.run_sink)
        self.renamemq.message.connect(self.fileRenamedAndMoved)
        self.renamemq.sequencesUpdate.connect(self.updateSequences)
        self.renamemq.workerFinished.connect(self.fileRenamedAndMovedFinished)

        QTimer.singleShot(0, self.renameThread.start)
        # Immediately start the only daemon process rename and move files
        # worker
        self.renamemq.start()

        # Setup the scan processes
        self.scanThread = QThread()
        self.scanmq = ScanManager(self.context, logging_level)
        self.scanmq.moveToThread(self.scanThread)

        self.scanThread.started.connect(self.scanmq.run_sink)
        self.scanmq.message.connect(self.scanMessageReceived)
        self.scanmq.workerFinished.connect(self.scanFinished)

        # call the slot with no delay
        QTimer.singleShot(0, self.scanThread.start)

        # Setup the copyfiles process
        self.copyfilesThread = QThread()
        self.copyfilesmq = CopyFilesManager(self.context, logging_level)
        self.copyfilesmq.moveToThread(self.copyfilesThread)

        self.copyfilesThread.started.connect(self.copyfilesmq.run_sink)
        self.copyfilesmq.message.connect(self.copyfilesDownloaded)
        self.copyfilesmq.bytesDownloaded.connect(self.copyfilesBytesDownloaded)
        self.copyfilesmq.tempDirs.connect(self.tempDirsReceivedFromCopyFiles)
        self.copyfilesmq.workerFinished.connect(self.copyfilesFinished)

        QTimer.singleShot(0, self.copyfilesThread.start)

        self.backup_manager_started = False
        self.backup_devices = BackupDeviceCollection()
        if self.prefs.backup_files:
            self.startBackupManager()
            self.setupBackupDevices()
        else:
            self.download_tracker.set_no_backup_devices(0, 0)

        prefs_valid, msg = self.prefs.check_prefs_for_validity()
        if not prefs_valid:
            self.notifyPrefsAreInvalid(details=msg)
            self.auto_start_is_on = False
        else:
            self.auto_start_is_on = self.prefs.auto_download_at_startup

        self.setDownloadActionSensitivity()
        self.searchForCameras()
        self.setupNonCameraDevices()
        self.setupManualPath()
        self.updateSourceButton()
        self.displayMessageInStatusBar()

        settings = QSettings()
        settings.beginGroup("MainWindow")

        if self.prefs.this_computer_path:
            index = self.fileSystemModel.index(self.prefs.this_computer_path)
            selection = self.fileSystemView.selectionModel()
            selection.select(index, QItemSelectionModel.ClearAndSelect|QItemSelectionModel.Rows)
            self.fileSystemView.scrollTo(index, QAbstractItemView.PositionAtCenter)

        self.window_show_requested_time = datetime.datetime.now()
        self.show()

        self.proximityButton.setChecked(settings.value("proximityButtonPressed", True, bool))
        self.proximityButtonClicked()
        self.sourceButton.setChecked(settings.value("sourceButtonPressed", True, bool))
        self.sourceButtonClicked()

    def startBackupManager(self):
        if not self.backup_manager_started:
            self.backupThread = QThread()
            self.backupmq = BackupManager(self.context, logging_level)
            self.backupmq.moveToThread(self.backupThread)

            self.backupThread.started.connect(self.backupmq.run_sink)
            self.backupmq.message.connect(self.fileBackedUp)
            self.backupmq.bytesBackedUp.connect(self.backupFileBytesBackedUp)

            QTimer.singleShot(0, self.backupThread.start)

            self.backup_manager_started = True

    def updateSourceButton(self) -> None:
        text, icon = self.devices.get_main_window_display_name_and_icon()
        self.sourceButton.setText(addPushButtonLabelSpacer(text))
        self.sourceButton.setIcon(icon)

    @pyqtSlot()
    def sourceButtonClicked(self) -> None:
        self.devicePanel.setVisible(self.sourceButton.isChecked())
        self.leftPanelSplitter.setVisible(not (self.devicePanel.isHidden() and
                                               self.temporalProximityView.isHidden()))

    @pyqtSlot()
    def proximityButtonClicked(self) -> None:
        self.temporalProximityView.setVisible(self.proximityButton.isChecked())
        self.leftPanelSplitter.setVisible(not (self.devicePanel.isHidden() and
                                               self.temporalProximityView.isHidden()))

    def createActions(self):
        self.sourceAct = QAction(_('&Source'), self, shortcut="Ctrl+s",
                                 triggered=self.doSourceAction)

        self.downloadAct = QAction(_("&Download"), self,
                                   shortcut="Ctrl+Return",
                                   triggered=self.doDownloadAction)

        self.refreshAct = QAction(_("&Refresh..."), self, shortcut="Ctrl+R",
                                  triggered=self.doRefreshAction)

        self.preferencesAct = QAction(_("&Preferences"), self,
                                      shortcut="Ctrl+P",
                                      triggered=self.doPreferencesAction)

        self.quitAct = QAction(_("&Quit"), self, shortcut="Ctrl+Q",
                               triggered=self.close)

        self.checkAllAct = QAction(_("&Check All"), self, shortcut="Ctrl+A",
                                   triggered=self.doCheckAllAction)

        self.checkAllPhotosAct = QAction(_("Check All Photos"), self,
                                         shortcut="Ctrl+T",
                                         triggered=self.doCheckAllPhotosAction)

        self.checkAllVideosAct = QAction(_("Check All Videos"), self,
                                         shortcut="Ctrl+D",
                                         triggered=self.doCheckAllVideosAction)

        self.uncheckAllAct = QAction(_("&Uncheck All"), self,
                                     shortcut="Ctrl+L",
                                     triggered=self.doUncheckAllAction)

        self.errorLogAct = QAction(_("Error Log"), self, enabled=False,
                                   checkable=True,
                                   triggered=self.doErrorLogAction)

        self.clearDownloadsAct = QAction(_("Clear Completed Downloads"), self,
                                         triggered=self.doClearDownloadsAction)

        self.previousFileAct = QAction(_("Previous File"), self, shortcut="[",
                                       triggered=self.doPreviousFileAction)

        self.nextFileAct = QAction(_("Next File"), self, shortcut="]",
                                   triggered=self.doNextFileAction)

        self.helpAct = QAction(_("Get Help Online..."), self, shortcut="F1",
                               triggered=help)

        self.reportProblemAct = QAction(_("Report a Problem..."), self,
                                        triggered=self.doReportProblemAction)

        self.makeDonationAct = QAction(_("Make a Donation..."), self,
                                       triggered=self.doMakeDonationAction)

        self.translateApplicationAct = QAction(_("Translate this Application..."),
                           self, triggered=self.doTranslateApplicationAction)

        self.aboutAct = QAction(_("&About..."), self, triggered=self.doAboutAction)

    def createLayoutAndButtons(self, centralWidget):

        settings = QSettings()
        settings.beginGroup("MainWindow")

        self.devicePanel = None

        verticalLayout = QVBoxLayout()

        topBar = QHBoxLayout()
        self.sourceButton = TopPushButton(addPushButtonLabelSpacer(_('Select Source')), self)
        self.sourceButton.clicked.connect(self.sourceButtonClicked)

        self.destinationButton = TopPushButton(addPushButtonLabelSpacer(_('Destination')), self)
        self.destinationButton.setIcon(QIcon(':/icons/folder.svg'))

        topBar.addWidget(self.sourceButton)
        topBar.addWidget(self.destinationButton, 0, Qt.AlignRight)
        verticalLayout.addLayout(topBar)

        verticalLayout.setContentsMargins(0, 0, 0, 0)

        centralWidget.setLayout(verticalLayout)

        centralLayout = QHBoxLayout()
        centralLayout.setContentsMargins(0, 0, 0, 0)

        leftBar = QVBoxLayout()
        leftBar.setContentsMargins(0, 0, 0, 0)

        self.proximityButton = RotatedButton(_('Timeline'), self, RotatedButton.leftSide)

        self.proximityButton.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.proximityButton.clicked.connect(self.proximityButtonClicked)
        leftBar.addWidget(self.proximityButton)
        leftBar.addStretch()

        centralLayout.addLayout(leftBar)

        self.horizontalSplitter = QSplitter()
        self.horizontalSplitter.setOrientation(Qt.Horizontal)

        self.leftPanelSplitter = QSplitter()
        self.leftPanelSplitter.setOrientation(Qt.Vertical)

        # Devices Header and View
        tip = _('Turn on or off the use of devices attached to this computer as download sources')
        self.deviceToggleView = QToggleView(label=_('Devices'),
                                            toggleToolTip=tip,
                                            headerColor=QColor(ThumbnailBackgroundName),
                                            headerFontColor=QColor(Qt.white),
                                            on=self.prefs.device_autodetection,
                                            parent=self)
        self.deviceToggleView.addWidget(self.deviceView)
        self.deviceToggleView.valueChanged.connect(self.deviceToggleViewValueChange)
        self.deviceToggleView.setSizePolicy(QSizePolicy.MinimumExpanding, QSizePolicy.Maximum)

        # This Computer Header and View

        tip = _('Turn on or off the use of a folder on this computer as a download source')
        self.thisComputerToggleView = QToggleView(label=_('This Computer'),
                                                  toggleToolTip=tip,
                                                  headerColor=QColor(ThumbnailBackgroundName),
                                                  headerFontColor=QColor(Qt.white),
                                                  on=bool(self.prefs.this_computer_source),
                                                  parent=self)
        self.thisComputerToggleView.valueChanged.connect(self.thisComputerToggleValueChanged)

        self.thisComputer = ThisComputerWidget('thisComputer', self.thisComputerView,
                                           self.fileSystemView, self)
        if self.prefs.this_computer_source:
            self.thisComputer.setViewVisible(bool(self.prefs.this_computer_path))

        self.thisComputerToggleView.addWidget(self.thisComputer)
        self.thisComputerToggleView.setSizePolicy(QSizePolicy.MinimumExpanding,
                                                  QSizePolicy.MinimumExpanding)

        self.resizeDeviceView(self.deviceView)
        self.resizeDeviceView(self.thisComputerView)
        self.layoutDevices()

        self.leftPanelSplitter.addWidget(self.temporalProximityView)
        self.temporalProximityView.setSizePolicy(QSizePolicy.Preferred,
                                                 QSizePolicy.MinimumExpanding)
        self.leftPanelSplitter.setCollapsible(0, False)
        self.leftPanelSplitter.setCollapsible(1, False)
        self.leftPanelSplitter.setStretchFactor(0, 0)
        self.leftPanelSplitter.setStretchFactor(1, 1)

        self.horizontalSplitter.addWidget(self.leftPanelSplitter)
        self.horizontalSplitter.addWidget(self.thumbnailView)
        self.horizontalSplitter.setStretchFactor(0, 0)
        self.horizontalSplitter.setStretchFactor(1, 2)
        self.horizontalSplitter.setCollapsible(0, False)
        self.horizontalSplitter.setCollapsible(1, False)

        centralLayout.addWidget(self.horizontalSplitter)

        splitterSetting = settings.value("horizontalSplitterSizes")
        if splitterSetting is not None:
            self.horizontalSplitter.restoreState(splitterSetting)
        else:
            self.horizontalSplitter.setSizes([200, 400])

        splitterSetting = settings.value("leftPanelSplitterSizes")
        if splitterSetting is not None:
            self.leftPanelSplitter.restoreState(splitterSetting)
        else:
            self.horizontalSplitter.setSizes([200, 400])

        rightBar = QVBoxLayout()
        rightBar.setContentsMargins(0, 0, 0, 0)

        self.backupButton = RotatedButton(_('Back Up'), self, RotatedButton.rightSide)
        self.renameButton = RotatedButton(_('Rename'), self, RotatedButton.rightSide)
        self.jobcodeButton = RotatedButton(_('Job Code'), self, RotatedButton.rightSide)
        self.backupButton.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.renameButton.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.jobcodeButton.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        rightBar.addWidget(self.backupButton)
        rightBar.addWidget(self.renameButton)
        rightBar.addWidget(self.jobcodeButton)
        rightBar.addStretch()
        centralLayout.addLayout(rightBar)

        verticalLayout.addLayout(centralLayout)

        # Help and Download buttons
        horizontalLayout = QHBoxLayout()
        horizontalLayout.setContentsMargins(7, 7, 7, 7)
        verticalLayout.addLayout(horizontalLayout, 0)
        self.downloadButton = QPushButton(self.downloadAct.text())
        self.downloadButton.addAction(self.downloadAct)
        self.downloadButton.setDefault(True)
        self.downloadButton.clicked.connect(self.downloadButtonClicked)
        self.download_action_is_download = True
        buttons = QDialogButtonBox(QDialogButtonBox.Help)
        buttons.addButton(self.downloadButton, QDialogButtonBox.ApplyRole)
        horizontalLayout.addWidget(buttons)

    def layoutDevices(self) -> None:
        """
        Layout Devices/This Computer in left panel.

        It turns out to be trickier than what might appear at first
        glance.

        The problem is that the widgets are not laid out again
        properly when changes are made to them (i.e. toggled or
        resized). For example, they are not positioned correctly until
        the containing widget is resized. Therefore the widget that
        contains  them, self.devicePanel, must be recreated each time
        there is a significant change in the child widgets.
        """

        if self.devicePanel is not None:
            self.devicePanel.setParent(None)
            self.devicePanel.deleteLater()
            # QObjectCleanupHandler().add(self.devicePanel)

        self.devicePanel = QWidget()
        self.devicePanel.setObjectName('devicePanel')
        self.devicePanel.setStyleSheet('QWidget#devicePanel {border: 1px solid %(borderName)s;}'
                                       % dict(borderName=BorderName))

        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        devices_stretch = 0
        bottom_stretch = 0
        if self.thisComputerToggleView.on():
            this_computer_stretch = 1
        else:
            this_computer_stretch = 0
            bottom_stretch = 1

        layout.addWidget(self.deviceToggleView, devices_stretch)
        layout.addWidget(self.thisComputerToggleView, this_computer_stretch)
        if bottom_stretch > 0:
            layout.addStretch(bottom_stretch)

        self.devicePanel.setLayout(layout)
        self.leftPanelSplitter.insertWidget(0, self.devicePanel)

    def setDownloadActionSensitivity(self) -> None:
        """
        Sets sensitivity of Download action to enable or disable it.
        Affects download button and menu item.
        """
        if not self.downloadIsRunning():
            enabled = False
            # Don't enable starting a download while devices are being scanned
            if len(self.scanmq) == 0:
                enabled = self.thumbnailModel.filesAreMarkedForDownload()

            self.downloadAct.setEnabled(enabled)
            self.downloadButton.setEnabled(enabled)

    def setDownloadActionLabel(self, is_download: bool):
        """
        Toggles action and download button text between pause and
        download
        """
        self.download_action_is_download = is_download
        if self.download_action_is_download:
            text = _("Download")
        else:
            text = _("Pause")
        self.downloadAct.setText(text)
        self.downloadButton.setText(text)

    def createMenus(self):
        self.fileMenu = QMenu("&File", self)
        self.fileMenu.addAction(self.downloadAct)
        self.fileMenu.addAction(self.refreshAct)
        self.fileMenu.addAction(self.preferencesAct)
        self.fileMenu.addAction(self.quitAct)

        self.selectMenu = QMenu("&Select", self)
        self.selectMenu.addAction(self.checkAllAct)
        self.selectMenu.addAction(self.checkAllPhotosAct)
        self.selectMenu.addAction(self.checkAllVideosAct)
        self.selectMenu.addAction(self.uncheckAllAct)



        self.viewMenu = QMenu("&View", self)
        self.viewMenu.addAction(self.errorLogAct)
        self.viewMenu.addAction(self.clearDownloadsAct)
        self.viewMenu.addSeparator()
        self.viewMenu.addAction(self.previousFileAct)
        self.viewMenu.addAction(self.nextFileAct)

        self.helpMenu = QMenu("&Help", self)
        self.helpMenu.addAction(self.helpAct)
        self.helpMenu.addAction(self.reportProblemAct)
        self.helpMenu.addAction(self.makeDonationAct)
        self.helpMenu.addAction(self.translateApplicationAct)
        self.helpMenu.addAction(self.aboutAct)

        self.menuBar().addMenu(self.fileMenu)
        self.menuBar().addMenu(self.selectMenu)
        self.menuBar().addMenu(self.viewMenu)
        self.menuBar().addMenu(self.helpMenu)

    def doSourceAction(self):
        self.sourceButton.animateClick()

    def doDownloadAction(self):
        self.downloadButton.animateClick()

    def doRefreshAction(self):
        pass

    def doPreferencesAction(self):
        pass

    def doCheckAllAction(self):
        self.thumbnailModel.checkAll(check_all=True)

    def doCheckAllPhotosAction(self):
        self.thumbnailModel.checkAll(check_all=True, file_type=FileType.photo)

    def doCheckAllVideosAction(self):
        self.thumbnailModel.checkAll(check_all=True, file_type=FileType.video)

    def doUncheckAllAction(self):
        self.thumbnailModel.checkAll(check_all=False)

    def doErrorLogAction(self):
        pass

    def doClearDownloadsAction(self):
        pass

    def doPreviousFileAction(self):
        pass

    def doNextFileAction(self):
        pass

    def doHelpAction(self):
        pass

    def doReportProblemAction(self):
        pass

    def doMakeDonationAction(self):
        pass

    def doTranslateApplicationAction(self):
        pass

    def doAboutAction(self):
        pass

    @pyqtSlot(bool)
    def thisComputerToggleValueChanged(self, on: bool) -> None:
        """
        Respond to This Computer Toggle Switch

        :param on: whether swich is on or off
        """

        self.layoutDevices()
        if on:
            self.thisComputer.setViewVisible(bool(self.prefs.this_computer_path))

        self.prefs.this_computer_source = on
        if not on:
            path = self.prefs.this_computer_path
            if path:
                scan_id = list(self.devices.this_computer)[0]
                self.removeDevice(scan_id)
            self.prefs.this_computer_path = ''
            self.fileSystemView.clearSelection()
        else:
            pass
            # TODO there is no path to scan - let the user know?

    @pyqtSlot(bool)
    def deviceToggleViewValueChange(self, on: bool) -> None:
        """
        Respond to Devices Toggle Switch

        :param on: whether swich is on or off
        """

        self.layoutDevices()
        self.prefs.device_autodetection = on
        if not on:
            for scan_id in list(self.devices.volumes_and_cameras):
                self.removeDevice(scan_id)
        else:
            self.searchForCameras()
            self.setupNonCameraDevices()

    @pyqtSlot(QModelIndex)
    def thisComputerPathChosen(self, index: QModelIndex) -> None:
        """
        Handle user selecting new device location path.

        Called after single click or folder being activated.

        :param index: cell clicked
        """

        path = self.fileSystemModel.filePath(index)
        if path != self.prefs.this_computer_path:
            if self.prefs.this_computer_path:
                scan_id = self.devices.scan_id_from_path(self.prefs.this_computer_path,
                                                         DeviceType.path)
                if scan_id is not None:
                    logging.debug("Removing path from device view %s",
                                  self.prefs.this_computer_path)
                    self.removeDevice(scan_id=scan_id, stop_worker=True)
            self.prefs.this_computer_path = path
            self.thisComputerView.show()
            self.setupManualPath()

    @pyqtSlot()
    def downloadButtonClicked(self) -> None:
        if False: #self.copy_files_manager.paused:
            logging.debug("Download resumed")
            self.resumeDownload()
        else:
            logging.debug("Download activated")

            if self.download_action_is_download:
                if self.job_code.need_to_prompt():
                    self.job_code.get_job_code()
                else:
                    self.startDownload()
            else:
                self.pauseDownload()

    def pauseDownload(self):

        self.copyfilesmq.pause()

        # set action to display Download
        if not self.download_action_is_download:
            self.setDownloadActionLabel(is_download = True)

        self.time_check.pause()

    def resumeDownload(self):
        for scan_id in self.active_downloads_by_scan_id:
            self.time_remaining.set_time_mark(scan_id)

        self.time_check.set_download_mark()

        self.copyfilesmq.resume()

    def downloadIsRunning(self) -> bool:
        """
        :return True if a file is currently being downloaded, renamed
        or backed up, else False
        """
        if len(self.active_downloads_by_scan_id) == 0:
            if self.prefs.backup_files:
                if self.download_tracker.all_files_backed_up():
                    return False
                else:
                    return True
            else:
                return False
        else:
            return True

    def startDownload(self, scan_id: int=None) -> None:
        """
        Start download, renaming and backup of files.

        :param scan_id: if specified, only files matching it will be
        downloaded
        """

        self.download_files = self.thumbnailModel.getFilesMarkedForDownload(scan_id)
        camera_unmount_called = False
        self.camera_unmounts_needed = set()
        if self.gvfsControlsMounts:
            mount_points = {}
            for scan_id in self.download_files.camera_access_needed:
                if self.download_files.camera_access_needed[scan_id]:
                    device = self.devices[scan_id]
                    model = device.camera_model
                    port = device.camera_port
                    mount_point = self.gvolumeMonitor.cameraMountPoint(
                            model, port)
                    if mount_point is not None:
                        self.camera_unmounts_needed.add((model, port))
                        mount_points[(model, port)] = mount_point
            if len(self.camera_unmounts_needed):
                logging.debug("%s camera(s) need to be unmounted before the download begins",
                              len(self.camera_unmounts_needed))
                camera_unmount_called = True
                for model, port in self.camera_unmounts_needed:
                    self.gvolumeMonitor.unmountCamera(model, port,
                          download_starting=True,
                          mount_point=mount_points[(model, port)])

        if not camera_unmount_called:
            self.startDownloadPhase2()

    def startDownloadPhase2(self) -> None:
        download_files = self.download_files

        invalid_dirs = self.invalidDownloadFolders(
            download_files.download_types)

        if invalid_dirs:
            if len(invalid_dirs) > 1:
                msg = _("These download folders are invalid:\n%("
                        "folder1)s\n%(folder2)s")  % {
                        'folder1': invalid_dirs[0], 'folder2': invalid_dirs[1]}
            else:
                msg = _("This download folder is invalid:\n%s") % invalid_dirs[0]
            self.log_error(ErrorType.critical_error, _("Download cannot "
                                                       "proceed"), msg)
        else:
            missing_destinations = self.backupDestinationsMissing(
                download_files.download_types)
            if missing_destinations is not None:
                # Warn user that they have specified that they want to
                # backup a file type, but no such folder exists on backup
                # devices
                if not missing_destinations[0]:
                    logging.warning("No backup device contains a valid "
                                    "folder for backing up photos")
                    msg = _("No backup device contains a valid folder for "
                            "backing up %(filetype)s") % {'filetype': _(
                            'photos')}
                else:
                    logging.warning("No backup device contains a valid "
                                    "folder for backing up videos")
                    msg = _("No backup device contains a valid folder for "
                            "backing up %(filetype)s") % {'filetype': _(
                            'videos')}

                self.logError(ErrorType.warning, _("Backup problem"), msg)

            # set time download is starting if it is not already set
            # it is unset when all downloads are completed
            if self.download_start_time is None:
                self.download_start_time = datetime.datetime.now()

            # Set status to download pending
            self.thumbnailModel.markDownloadPending(download_files.files)

            # disable refresh and preferences change while download is
            # occurring
            self.enablePrefsAndRefresh(enabled=False)

            # notify renameandmovefile process to read any necessary values
            # from the program preferences
            data = RenameAndMoveFileData(
                message=RenameAndMoveStatus.download_started)
            self.renamemq.send_message_to_worker(data)

            # Maximum value of progress bar may have been set to the number
            # of thumbnails being generated. Reset it to use a percentage.
            self.downloadProgressBar.setMaximum(100)

            for scan_id in download_files.files:
                files = download_files.files[scan_id]
                # if generating thumbnails for this scan_id, stop it
                if self.thumbnailModel.terminateThumbnailGeneration(scan_id):
                    generate_thumbnails = self.thumbnailModel.markThumbnailsNeeded(files)
                else:
                    generate_thumbnails = False

                self.downloadFiles(files, scan_id,
                                   download_files.download_stats[scan_id],
                                   generate_thumbnails)

            self.setDownloadActionLabel(is_download=False)

    def downloadFiles(self, files: list,
                      scan_id: int,
                      download_stats: DownloadStats,
                      generate_thumbnails: bool) -> None:
        """

        :param files: list of the files to download
        :param scan_id: the device from which to download the files
        :param download_stats: count of files and their size
        :param generate_thumbnails: whether thumbnails must be
        generated in the copy files process.
        """

        model = self.mapModel(scan_id)
        model.setDeviceState(scan_id, DeviceState.downloading)

        if download_stats.no_photos > 0:
            photo_download_folder = self.prefs.photo_download_folder
        else:
            photo_download_folder = None

        if download_stats.no_videos > 0:
            video_download_folder = self.prefs.video_download_folder
        else:
            video_download_folder = None

        self.download_tracker.init_stats(scan_id=scan_id, stats=download_stats)
        download_size = download_stats.photos_size_in_bytes + \
                        download_stats.videos_size_in_bytes

        if self.prefs.backup_files:
            download_size += ((self.backup_devices.no_photo_backup_devices *
                               download_stats.photos_size_in_bytes) + (
                               self.backup_devices.no_video_backup_devices *
                               download_stats.videos_size_in_bytes))

        self.time_remaining[scan_id] = download_size
        self.time_check.set_download_mark()

        self.active_downloads_by_scan_id.add(scan_id)

        if len(self.active_downloads_by_scan_id) > 1:
            # Display an additional notification once all devices have been
            # downloaded from that summarizes the downloads.
            self.display_summary_notification = True

        if self.auto_start_is_on and self.prefs.generate_thumbnails:
            for rpd_file in files:
                rpd_file.generate_thumbnail = True
            generate_thumbnails = True

        verify_file = self.prefs.verify_file
        if verify_file:
            # since a file might be modified in the file modify process,
            # if it will be backed up, need to refresh the md5 once it has
            # been modified
            refresh_md5_on_file_change = self.prefs.backup_files
        else:
            refresh_md5_on_file_change = False

        # Initiate copy files process

        device = self.devices[scan_id]
        copyfiles_args = CopyFilesArguments(scan_id,
                                device,
                                photo_download_folder,
                                video_download_folder,
                                files,
                                verify_file,
                                generate_thumbnails)

        self.copyfilesmq.start_worker(scan_id, copyfiles_args)

    @pyqtSlot(int, str, str)
    def tempDirsReceivedFromCopyFiles(self, scan_id: int,
                                      photo_temp_dir: str,
                                      video_temp_dir: str) -> None:
        self.temp_dirs_by_scan_id[scan_id] = list(filter(None,[photo_temp_dir,
                                                  video_temp_dir]))

    def cleanAllTempDirs(self):
        """
        Deletes temporary files and folders used in all downloads.
        """
        for scan_id in self.temp_dirs_by_scan_id:
            self.cleanTempDirsForScanId(scan_id, remove_entry=False)
        self.temp_dirs_by_scan_id = {}

    def cleanTempDirsForScanId(self, scan_id: int, remove_entry: bool=True):
        """
        Deletes temporary files and folders used in download.

        :param scan_id: the scan id associated with the temporary
         directory
        :param remove_entry: if True, remove the scan_id from the
         dictionary tracking temporary directories
        """

        home_dir = os.path.expanduser("~")
        for d in self.temp_dirs_by_scan_id[scan_id]:
            assert d != home_dir
            if os.path.isdir(d):
                try:
                    shutil.rmtree(d, ignore_errors=True)
                except:
                    logging.error("Unknown error deleting temporary directory %s", d)
        if remove_entry:
            del self.temp_dirs_by_scan_id[scan_id]

    @pyqtSlot(bool, RPDFile, int)
    def copyfilesDownloaded(self, download_succeeded: bool,
                            rpd_file: RPDFile,
                            download_count: int) -> None:

        self.download_tracker.set_download_count_for_file(rpd_file.unique_id,
                                                 download_count)
        self.download_tracker.set_download_count(rpd_file.scan_id,
                                                 download_count)
        rpd_file.download_start_time = self.download_start_time
        rpd_file.job_code = self.job_code.job_code
        data = RenameAndMoveFileData(rpd_file=rpd_file,
                                     download_count=download_count,
                                     download_succeeded=download_succeeded)
        self.renamemq.rename_file(data)

    @pyqtSlot(bytes)
    def copyfilesBytesDownloaded(self, pickled_data: bytes) -> None:
        data = pickle.loads(pickled_data) # type: CopyFilesResults
        scan_id = data.scan_id
        total_downloaded = data.total_downloaded
        chunk_downloaded = data.chunk_downloaded
        assert total_downloaded >= 0
        assert chunk_downloaded >= 0
        self.download_tracker.set_total_bytes_copied(scan_id,
                                                     total_downloaded)
        self.time_check.increment(bytes_downloaded=chunk_downloaded)
        # TODO update right model right way
        self.time_remaining.update(scan_id, bytes_downloaded=chunk_downloaded)

    @pyqtSlot()
    def copyfilesFinished(self) -> None:
        pass

    @pyqtSlot(bool, RPDFile, int, QPixmap)
    def fileRenamedAndMoved(self, move_succeeded: bool, rpd_file: RPDFile,
                            download_count: int, thumbnail: QPixmap) -> None:

        if not thumbnail.isNull():
            logging.debug("Updating GUI thumbnail for {} with unique id {}".format(
                rpd_file.download_full_file_name, rpd_file.unique_id))
            self.thumbnailModel.thumbnailReceived(rpd_file, thumbnail)

        if rpd_file.status == DownloadStatus.downloaded_with_warning:
            self.logError(ErrorType.warning, rpd_file.error_title,
                           rpd_file.error_msg, rpd_file.error_extra_detail)

        if self.prefs.backup_files:
            if self.backup_devices.backup_possible(rpd_file.file_type):
                self.backupFile(rpd_file, move_succeeded, download_count)
            else:
                self.fileDownloadFinished(move_succeeded, rpd_file)
        else:
            self.fileDownloadFinished(move_succeeded, rpd_file)

    def backupFile(self, rpd_file: RPDFile, move_succeeded: bool,
                   download_count: int) -> None:
        if self.prefs.backup_device_autodetection:
            if rpd_file.file_type == FileType.photo:
                path_suffix = self.prefs.photo_backup_identifier
            else:
                path_suffix = self.prefs.video_backup_identifier
        else:
            path_suffix = None
        if rpd_file.file_type == FileType.photo:
            logging.debug("Backing up photo %s", rpd_file.download_name)
        else:
            logging.debug("Backing up video %s", rpd_file.download_name)

        for path in self.backup_devices:
            backup_type = self.backup_devices[path].backup_type
            do_backup = (
                (backup_type == BackupLocationType.photos_and_videos) or
                (rpd_file.file_type == FileType.photo and backup_type ==
                 BackupLocationType.photos) or
                (rpd_file.file_type == FileType.video and backup_type ==
                 BackupLocationType.videos))
            if do_backup:
                logging.debug("Backing up to %s", path)
            else:
                logging.debug("Not backing up to %s", path)
            # Even if not going to backup to this device, need to send it
            # anyway so progress bar can be updated. Not this most efficient
            # but the code is much more simple
            # TODO: check if this is still correct with new code!

            device_id = self.backup_devices.device_id(path)
            data = BackupFileData(rpd_file, move_succeeded, do_backup,
                                  path_suffix,
                                  self.prefs.backup_duplicate_overwrite,
                                  self.prefs.verify_file, download_count,
                                  self.prefs.save_fdo_thumbnails)
            self.backupmq.backup_file(data, device_id)

    @pyqtSlot(int, bool, bool, RPDFile)
    def fileBackedUp(self, device_id: int, backup_succeeded: bool, do_backup: bool,
                     rpd_file: RPDFile) -> None:

        # Only show an error message if there is more than one device
        # backing up files of this type - if that is the case,
        # do not want to rely on showing an error message in the
        # function file_download_finished, as it is only called once,
        # when all files have been backed up
        if not backup_succeeded and self.backup_devices.multiple_backup_devices(
                rpd_file.file_type) and do_backup:
            # TODO implement error notification on backups
            pass
            # self.log_error(config.SERIOUS_ERROR,
            #     rpd_file.error_title,
            #     rpd_file.error_msg, rpd_file.error_extra_detail)

        if do_backup:
            self.download_tracker.file_backed_up(rpd_file.scan_id,
                                                 rpd_file.unique_id)
            if self.download_tracker.file_backed_up_to_all_locations(
                    rpd_file.unique_id, rpd_file.file_type):
                logging.debug("File %s will not be backed up to any more "
                            "locations", rpd_file.download_name)
                self.fileDownloadFinished(backup_succeeded, rpd_file)

    @pyqtSlot(bytes)
    def backupFileBytesBackedUp(self, pickled_data: bytes) -> None:
        data = pickle.loads(pickled_data) # type: BackupResults
        scan_id = data.scan_id
        chunk_downloaded = data.chunk_downloaded
        self.download_tracker.increment_bytes_backed_up(scan_id,
                                                     chunk_downloaded)
        self.time_check.increment(bytes_downloaded=chunk_downloaded)
        percent_complete = self.download_tracker.get_percent_complete(scan_id)
        # TODO update right model right way
        self.deviceModel.updateDownloadProgress(scan_id, percent_complete, '')
        self.time_remaining.update(scan_id, bytes_downloaded=chunk_downloaded)

    @pyqtSlot(int, list)
    def updateSequences(self, stored_sequence_no: int, downloads_today: List[str]) -> None:
        """
        Called at conclusion of a download, with values coming from
        renameandmovefile process
        """
        self.prefs.stored_sequence_no = stored_sequence_no
        self.prefs.downloads_today = downloads_today
        self.prefs.sync()
        logging.debug("Saved sequence values to preferences")
        if self.application_state == ApplicationState.exiting:
            self.close()

    @pyqtSlot()
    def fileRenamedAndMovedFinished(self) -> None:
        pass

    def updateFileDownloadDeviceProgress(self, scan_id: int,
                                         unique_id: str,
                                         file_type: FileType) -> tuple:
        """
        Increments the progress bar for an individual device.

        Returns if the download is completed for that scan_pid
        It also returns the number of files remaining for the scan_pid, BUT
        this value is valid ONLY if the download is completed
        """

        # TODO redo this code to account for new device view
        files_downloaded = self.download_tracker.get_download_count_for_file(unique_id)
        files_to_download = self.download_tracker.get_no_files_in_download(
                scan_id)
        file_types = self.download_tracker.get_file_types_present(scan_id)
        completed = files_downloaded == files_to_download
        if self.prefs.backup_files and completed:
            completed = self.download_tracker.all_files_backed_up(scan_id)

        if completed:
            files_remaining = self.thumbnailModel.getNoFilesRemaining(scan_id)
        else:
            files_remaining = 0

        if completed and files_remaining:
            # e.g.: 3 of 205 photos and videos (202 remaining)
            progress_bar_text = _("%(number)s of %(total)s %(filetypes)s (%("
                                  "remaining)s remaining)") % {
                                  'number':  thousands(files_downloaded),
                                  'total': thousands(files_to_download),
                                  'filetypes': file_types,
                                  'remaining': thousands(files_remaining)}
        else:
            # e.g.: 205 of 205 photos and videos
            progress_bar_text = _("%(number)s of %(total)s %(filetypes)s") % \
                                 {'number':  thousands(files_downloaded),
                                  'total': thousands(files_to_download),
                                  'filetypes': file_types}
        percent_complete = self.download_tracker.get_percent_complete(scan_id)
        # TODO update right model right way
        # self.deviceModel.updateDownloadProgress(scan_id=scan_id,
        #                                 percent_complete=percent_complete,
        #                                 progress_bar_text=progress_bar_text)

        percent_complete = self.download_tracker.get_overall_percent_complete()
        self.downloadProgressBar.setValue(round(percent_complete*100))
        if self.unity_progress:
            self.deskop_launcher.set_property('progress', percent_complete)
            self.deskop_launcher.set_property('progress_visible', True)

        return (completed, files_remaining)

    def fileDownloadFinished(self, succeeded: bool, rpd_file: RPDFile) -> None:
        """
        Called when a file has been downloaded i.e. copied, renamed,
        and backed up
        """
        scan_id = rpd_file.scan_id
        unique_id = rpd_file.unique_id
        # Update error log window if neccessary
        if not succeeded and not self.backup_devices.multiple_backup_devices(
                rpd_file.file_type):
            self.logError(ErrorType.serious_error, rpd_file.error_title,
                           rpd_file.error_msg, rpd_file.error_extra_detail)
        elif self.prefs.move:
            # record which files to automatically delete when download
            # completes
            self.download_tracker.add_to_auto_delete(rpd_file)

        self.thumbnailModel.updateStatusPostDownload(rpd_file)
        self.download_tracker.file_downloaded_increment(scan_id,
                                                        rpd_file.file_type,
                                                        rpd_file.status)

        completed, files_remaining = self.updateFileDownloadDeviceProgress(scan_id, unique_id,
                                                       rpd_file.file_type)

        if self.downloadIsRunning():
            self.updateTimeRemaining()

        if completed:
            # Last file for this scan id has been downloaded, so clean temp
            # directory
            self.mapModel(scan_id).setDeviceState(scan_id, DeviceState.scanned)

            logging.debug("Purging temp directories")
            self.cleanTempDirsForScanId(scan_id)
            if self.prefs.move:
                logging.debug("Deleting downloaded source files")
                self.deleteSourceFiles(scan_id)
                self.download_tracker.clear_auto_delete(scan_id)
            self.active_downloads_by_scan_id.remove(scan_id)
            del self.time_remaining[scan_id]
            self.notifyDownloadedFromDevice(scan_id)
            if files_remaining == 0 and self.prefs.auto_unmount:
                self.unmountVolume(scan_id)

            if not self.downloadIsRunning():
                logging.debug("Download completed")
                self.enablePrefsAndRefresh(enabled=True)
                self.notifyDownloadComplete()
                self.downloadProgressBar.reset()
                if self.unity_progress:
                    self.deskop_launcher.set_property('progress_visible', False)

                # Update prefs with stored sequence number and downloads today
                # values
                data = RenameAndMoveFileData(
                    message=RenameAndMoveStatus.download_completed)
                self.renamemq.send_message_to_worker(data)

                if ((self.prefs.auto_exit and self.download_tracker.no_errors_or_warnings())
                                                or self.prefs.auto_exit_force):
                    if not self.thumbnailModel.filesRemainToDownload():
                        self.quit()

                self.download_tracker.purge_all()
                # self.speed_label.set_label(" ")

                self.displayMessageInStatusBar()

                self.setDownloadActionLabel(is_download=True)
                self.setDownloadActionSensitivity()

                self.job_code.job_code = ''
                self.download_start_time = None

    def updateTimeRemaining(self):
        update, download_speed = self.time_check.check_for_update()
        if update:
            # TODO implement label showing download speed
            # self.speedLabel.set_text(download_speed)

            time_remaining = self.time_remaining.time_remaining()
            if time_remaining:
                secs =  int(time_remaining)

                if secs == 0:
                    message = ""
                elif secs == 1:
                    message = _("About 1 second remaining")
                elif secs < 60:
                    message = _("About %i seconds remaining") % secs
                elif secs == 60:
                    message = _("About 1 minute remaining")
                else:
                    # Translators: in the text '%(minutes)i:%(seconds)02i',
                    # only the : should be translated, if needed.
                    # '%(minutes)i' and '%(seconds)02i' should not be
                    # modified or left out. They are used to format and
                    # display the amount
                    # of time the download has remainging, e.g. 'About 5:36
                    # minutes remaining'
                    message = _(
                        "About %(minutes)i:%(seconds)02i minutes remaining") % {
                              'minutes': secs / 60, 'seconds': secs % 60}

                self.statusBar().showMessage(message)

    def enablePrefsAndRefresh(self, enabled: bool) -> None:
        """
        Disable the user being to access the refresh command or change
        program preferences while a download is occurring.

        :param enabled: if True, then the user is able to activate the
        preferences and refresh commands.

        """
        self.refreshAct.setEnabled(enabled)
        self.preferencesAct.setEnabled(enabled)

    def unmountVolume(self, scan_id: int) -> None:
        """
        Cameras are already unmounted, so no need to unmount them!
        :param scan_id: the scan id of the device to be umounted
        """
        device = self.devices[scan_id] # type: Device

        if device.device_type == DeviceType.volume:
            #TODO implement device unmounting
            if self.gvfsControlsMounts:
                #self.gvolumeMonitor.
                pass
            else:
                #self.udisks2Monitor.
                pass

    def deleteSourceFiles(self, scan_id: int)  -> None:
        """
        Delete files from download device at completion of download
        """
        # TODO delete from cameras and from other devics
        # TODO should assign this to a process or a thread, and delete then
        to_delete = self.download_tracker.get_files_to_auto_delete(scan_id)

    def notifyDownloadedFromDevice(self, scan_id: int):
        """
        Display a system notification to the user using libnotify
        that the files have been downloaded from the device
        :param scan_id: identifies which device
        """
        device = self.devices[scan_id]

        if device.device_type == DeviceType.path:
            notification_name = _('Rapid Photo Downloader')
        else:
            notification_name  = device.name()

        if device.icon_name is not None:
            icon = device.icon_name
        else:
            icon = None

        no_photos_downloaded = self.download_tracker.get_no_files_downloaded(
                                            scan_id, FileType.photo)
        no_videos_downloaded = self.download_tracker.get_no_files_downloaded(
                                            scan_id, FileType.video)
        no_photos_failed = self.download_tracker.get_no_files_failed(
                                            scan_id, FileType.photo)
        no_videos_failed = self.download_tracker.get_no_files_failed(
                                            scan_id, FileType.video)
        no_files_downloaded = no_photos_downloaded + no_videos_downloaded
        no_files_failed = no_photos_failed + no_videos_failed
        no_warnings = self.download_tracker.get_no_warnings(scan_id)

        file_types = file_types_by_number(no_photos_downloaded,
                                               no_videos_downloaded)
        file_types_failed = file_types_by_number(no_photos_failed,
                                                      no_videos_failed)
        message = _("%(noFiles)s %(filetypes)s downloaded") % {
            'noFiles': no_files_downloaded, 'filetypes': file_types}

        if no_files_failed:
            message += "\n" + _(
                "%(noFiles)s %(filetypes)s failed to download") % {
                              'noFiles': no_files_failed,
                              'filetypes': file_types_failed}

        if no_warnings:
            message = "%s\n%s " % (message, no_warnings) + _("warnings")

        message_shown = False
        if self.have_libnotify:
            if icon is not None:
                # summary, body, icon (icon theme icon name or filename)
                n = Notify.Notification.new(notification_name, message, icon)
            else:
                n = Notify.Notification.new(notification_name, message)
            try:
                message_shown =  n.show()
            except:
                logging.error("Unable to display message using notification "
                          "system")
            if not message_shown:
                logging.info("{}: {}".format(notification_name, message))


    def notifyDownloadComplete(self) -> None:
        if self.display_summary_notification:
            message = _("All downloads complete")

            # photo downloads
            photo_downloads = self.download_tracker.total_photos_downloaded
            if photo_downloads:
                filetype = file_types_by_number(photo_downloads, 0)
                message += "\n" + _("%(number)s %(numberdownloaded)s") % \
                                  {'number': photo_downloads,
                                   'numberdownloaded': _(
                                       "%(filetype)s downloaded") % \
                                                       {'filetype': filetype}}

            # photo failures
            photo_failures = self.download_tracker.total_photo_failures
            if photo_failures:
                filetype = file_types_by_number(photo_failures, 0)
                message += "\n" + _("%(number)s %(numberdownloaded)s") % \
                                  {'number': photo_failures,
                                   'numberdownloaded': _(
                                       "%(filetype)s failed to download") % \
                                                       {'filetype': filetype}}

            # video downloads
            video_downloads = self.download_tracker.total_videos_downloaded
            if video_downloads:
                filetype = file_types_by_number(0, video_downloads)
                message += "\n" + _("%(number)s %(numberdownloaded)s") % \
                                  {'number': video_downloads,
                                   'numberdownloaded': _(
                                       "%(filetype)s downloaded") % \
                                                       {'filetype': filetype}}

            # video failures
            video_failures = self.download_tracker.total_video_failures
            if video_failures:
                filetype = file_types_by_number(0, video_failures)
                message += "\n" + _("%(number)s %(numberdownloaded)s") % \
                                  {'number': video_failures,
                                   'numberdownloaded': _(
                                       "%(filetype)s failed to download") % \
                                                       {'filetype': filetype}}

            # warnings
            warnings = self.download_tracker.total_warnings
            if warnings:
                message += "\n" + _("%(number)s %(numberdownloaded)s") % \
                            {'number': warnings,
                            'numberdownloaded': _("warnings")}

            message_shown = False
            if self.have_libnotify:
                n = Notify.Notification.new(_('Rapid Photo Downloader'),
                                message,
                                self.program_svg)
                try:
                    message_shown = n.show()
                except:
                    logging.error("Unable to display message using "
                                "notification system")
            if not message_shown:
                logging.info(message)

            # don't show summary again unless needed
            self.display_summary_notification = False

    def invalidDownloadFolders(self, downloading: DownloadTypes) -> list:
        """
        Checks validity of download folders based on the file types the
        user is attempting to download.

        :return list of the invalid directories, if any, or empty list.
        :rtype list[str]
        """
        invalid_dirs = []
        if downloading.photos:
            if not self.isValidDownloadDir(self.prefs.photo_download_folder,
                                                        is_photo_dir=True):
                invalid_dirs.append(self.prefs.photo_download_folder)
        if downloading.videos:
            if not self.isValidDownloadDir(self.prefs.video_download_folder,
                                                        is_photo_dir=False):
                invalid_dirs.append(self.prefs.video_download_folder)
        return invalid_dirs

    def isValidDownloadDir(self, path, is_photo_dir: bool,
                           show_error_in_log=False) -> bool:
        """
        Checks directory following conditions:
        Does it exist? Is it writable?

        :param show_error_in_log: if  True, then display warning in log
        window
        :type show_error_in_log: bool
        :param is_photo_dir: if true the download directory is for
        photos, else for videos
        :return True if directory is valid, else False
        """
        valid = False
        if is_photo_dir:
            download_folder_type = _("Photo")
        else:
            download_folder_type = _("Video")

        if not os.path.isdir(path):
            logging.error("%s download folder does not exist: %s",
                         download_folder_type, path)
            if show_error_in_log:
                severity = ErrorType.warning
                problem = _("%(file_type)s download folder is invalid") % {
                            'file_type': download_folder_type}
                details = _("Folder: %s") % path
                self.log_error(severity, problem, details)
        elif not os.access(path, os.W_OK):
            logging.error("%s is not writable", path)
            if show_error_in_log:
                severity = ErrorType.warning
                problem = _("%(file_type)s download folder is not writable") \
                            % {'file_type': download_folder_type}
                details = _("Folder: %s") % path
                self.log_error(severity, problem, details)
        else:
            valid = True
        return valid

    def notifyPrefsAreInvalid(self, details):
        title = _("Program preferences are invalid")
        logging.critical(title)
        self.log_error(severity=ErrorType.critical_error, problem=title,
                       details=details)

    def logError(self, severity, problem, details, extra_detail=None) -> None:
        """
        Display error and warning messages to user in log window
        """
        #TODO implement error log window
        pass
        # self.error_log.add_message(severity, problem, details, extra_detail)

    def backupDestinationsMissing(self, downloading: DownloadTypes) -> BackupMissing:
        """
        Checks if there are backup destinations matching the files
        going to be downloaded
        :param downloading: the types of file that will be downloaded
        :return: None if no problems, or BackupMissing
        """
        photo_missing = video_missing = False
        if self.prefs.backup_files and self.prefs.backup_device_autodetection:
            if downloading.photos and not self.backup_devices.backup_possible(FileType.photo):
                photo_missing = True
            if downloading.videos and not self.backup_devices.backup_possible(FileType.video):
                video_missing = True
            if not(photo_missing or video_missing):
                return None
            else:
                return BackupMissing(photo=photo_missing, video=video_missing)
        return None

    @pyqtSlot(bytes)
    def scanMessageReceived(self, pickled_data: bytes) -> None:
        """
        Process data received from the scan process.

        The data is pickled because PyQt converts the Python int into
        a C++ int, which unlike Pyhon has an upper limit. Unpickle it
        too early, and the int wraps around to become a negative
        number.
        """

        data = pickle.loads(pickled_data) # type: ScanResults
        if data.rpd_files is not None:
            # Update scan running totals
            scan_id = data.rpd_files[0].scan_id
            if scan_id not in self.devices:
                return
            device = self.devices[scan_id]
            device.file_type_counter = data.file_type_counter
            device.file_size_sum = data.file_size_sum
            self.mapModel(scan_id).updateDeviceScan(scan_id)

            for rpd_file in data.rpd_files:
                self.thumbnailModel.addFile(rpd_file, generate_thumbnail=not
                                            self.auto_start_is_on)
        else:
            scan_id = data.scan_id
            if scan_id not in self.devices:
                return
            if data.error_code is not None:
                # An error occurred
                error_code = data.error_code
                device = self.devices[scan_id]
                camera_model = device.display_name
                if error_code == CameraErrorCode.locked:
                    title =_('Files inaccessible')
                    message = _('All files on the %(camera)s are inaccessible. It may be locked or '
                                'not configured for file transfers using MTP. You can '
                                'unlock it and try again. On some models you also need to change '
                                'the setting "USB for charging" to "USB for file transfers". '
                                  'Alternatively, you can ignore this device.') % {
                        'camera': camera_model}
                else:
                    assert error_code == CameraErrorCode.inaccessible
                    title = _('Device inaccessible')
                    message = _('The %(camera)s appears to be in use by another application. You '
                                'can close any other application (such as a file browser) that is '
                                'using it and try again. If that '
                                'does not work, unplug the %(camera)s from the computer and plug '
                                'it in again. Alternatively, you can ignore '
                                'this device.') % {'camera':camera_model}
                msgBox = QMessageBox(QMessageBox.Warning, title, message,
                                QMessageBox.NoButton, self)
                msgBox.setIconPixmap(self.devices[scan_id].get_pixmap(QSize(30,30)))
                msgBox.addButton(_("&Try Again"), QMessageBox.AcceptRole)
                msgBox.addButton("&Ignore This Device", QMessageBox.RejectRole)
                self.prompting_for_user_action[device] = msgBox
                role = msgBox.exec_()
                if role == QMessageBox.AcceptRole:
                    self.scanmq.resume(worker_id=scan_id)
                else:
                    self.scanmq.stop_worker(worker_id=scan_id)
                    self.removeDevice(scan_id=scan_id, stop_worker=False)
                del self.prompting_for_user_action[device]
            else:
                # Update GUI display with canonical camera display name
                device = self.devices[scan_id]
                device.update_camera_attributes(display_name=data.optimal_display_name,
                                                storage_space=data.storage_space)
                self.updateSourceButton()
                self.deviceModel.updateDeviceNameAndStorage(scan_id, device)
                self.resizeDeviceView(self.deviceView)

    @pyqtSlot(int)
    def scanFinished(self, scan_id: int) -> None:
        if scan_id not in self.devices:
            return
        device = self.devices[scan_id]
        results_summary, file_types_present  = device.file_type_counter.summarize_file_count()
        self.download_tracker.set_file_types_present(scan_id, file_types_present)
        model = self.mapModel(scan_id)
        model.updateDeviceScan(scan_id)
        self.setDownloadActionSensitivity()

        self.displayMessageInStatusBar(update_only_marked=True)

        self.generateTemporalProximityTableData()

        if (not self.auto_start_is_on and  self.prefs.generate_thumbnails):
            # Generate thumbnails for finished scan
            model.setDeviceState(scan_id, DeviceState.scanned)
            self.thumbnailModel.generateThumbnails(scan_id, self.devices[scan_id])
        elif self.auto_start_is_on:
            if self.job_code.need_to_prompt_on_auto_start():
                model.setDeviceState(scan_id, DeviceState.scanned)
                self.job_code.get_job_code()
            else:
                self.startDownload(scan_id=scan_id)

    def quit(self) -> None:
        """
        Convenience function to quit the application.

        Issues a signal to initiate the quit. The signal will be acted
        on when Qt gets the chance.
        """
        QTimer.singleShot(0, self.close)

    @pyqtSlot(QItemSelection, QItemSelection)
    def proximitySelectionChanged(self, current: QItemSelection, previous: QItemSelection) -> None:
        """
        Respond to user selections in Temporal Proximity Table.

        User can select / deselect individual cells. Need to:
        1. Automatically update selection to include parent or child
           cells in some cases
        2. Filter display of thumbnails
        """
        self.temporalProximityView.updateSelection()

        groups = self.temporalProximityModel.groups

        selected_rows_col2 = [i.row() for i in self.temporalProximityView.selectedIndexes()
                              if i.column() == 2]
        selected_rows_col1 = [i.row() for i in self.temporalProximityView.selectedIndexes()
                              if i.column() == 1 and
                              groups.row_span_for_column_starts_at_row[(
                              i.row(), 2)] not in selected_rows_col2]

        if selected_rows_col2 or selected_rows_col1:
            self.thumbnailProxyModel.selected_rows = groups.selected_thumbnail_rows(
                    selected_rows_col1, selected_rows_col2)
            self.thumbnailProxyModel.invalidateFilter()
        else:
            self.thumbnailProxyModel.selected_rows = set()
            self.thumbnailProxyModel.invalidateFilter()

    def generateTemporalProximityTableData(self) -> None:
        # Convert the thumbnail rows to a regular list, because it's going
        # to be pickled.
        rows = list(self.thumbnailModel.rows)
        rpd_files = self.thumbnailModel.rpd_files
        file_types = [rpd_files[row.id_value].file_type for row in rows]
        # TODO assign a user-defined value to the proximity
        proximity_seconds=3600
        data = OffloadData(thumbnail_rows=rows,
                           thumbnail_types=file_types,
                           proximity_seconds=proximity_seconds)
        self.offloadmq.assign_work(data)

    @pyqtSlot(TemporalProximityGroups)
    def proximityGroupsGenerated(self, proximity_groups: TemporalProximityGroups) -> None:

        self.temporalProximityModel.groups = proximity_groups
        depth = proximity_groups.depth()
        self.temporalProximityDelegate.depth = depth
        if depth == 1:
            self.temporalProximityView.hideColumn(0)
        else:
            self.temporalProximityView.showColumn(0)
        self.temporalProximityView.clearSpans()
        self.temporalProximityDelegate.reset()
        self.temporalProximityDelegate.row_span_for_column_starts_at_row = \
            proximity_groups.row_span_for_column_starts_at_row
        for column, row, row_span in proximity_groups.spans:
            self.temporalProximityView.setSpan(row, column, row_span, 1)

        self.temporalProximityModel.endResetModel()

        self.temporalProximityView.resizeRowsToContents()
        self.temporalProximityView.resizeColumnsToContents()

    def closeEvent(self, event) -> None:
        if self.application_state == ApplicationState.normal:
            self.application_state = ApplicationState.exiting
            self.scanmq.stop()
            self.thumbnailModel.thumbnailmq.stop()
            self.copyfilesmq.stop()

            if self.downloadIsRunning():
                logging.debug("Exiting while download is running. Cleaning "
                              "up...")
                # Update prefs with stored sequence number and downloads today
                # values
                data = RenameAndMoveFileData(
                    message=RenameAndMoveStatus.download_completed)
                self.renamemq.send_message_to_worker(data)
                # renameandmovefile process will send a message with the
                # updated sequence values. When that occurs,
                # this application will save the sequence values to the
                # program preferences, resume closing and this close event
                # will again be called, but this time the application state
                # flag will indicate the need to resume below.
                event.ignore()
                return
                # Incidentally, it's the renameandmovefile process that
                # updates the SQL database with the file downloads,
                # so no need to update or close it in this main process

        self.writeWindowSettings()

        self.offloadmq.stop()
        self.offloadThread.quit()
        if not self.offloadThread.wait(500):
            self.offloadmq.forcefully_terminate()

        self.renamemq.stop()
        self.renameThread.quit()
        if not self.renameThread.wait(500):
            self.renamemq.forcefully_terminate()

        self.scanThread.quit()
        if not self.scanThread.wait(2000):
            self.scanmq.forcefully_terminate()

        self.copyfilesThread.quit()
        if not self.copyfilesThread.wait(1000):
            self.copyfilesmq.forcefully_terminate()

        if self.backup_manager_started:
            self.backupmq.stop()
            self.backupThread.quit()
            if not self.backupThread.wait(1000):
                self.backupmq.forcefully_terminate()

        if not self.gvfsControlsMounts:
            self.udisks2MonitorThread.quit()
            self.udisks2MonitorThread.wait()
            self.cameraHotplugThread.quit()
            self.cameraHotplugThread.wait()

        self.cleanAllTempDirs()
        self.devices.delete_cache_dirs()
        tc = ThumbnailCacheSql()
        tc.cleanup_cache()
        Notify.uninit()

        event.accept()

    def getIconsAndEjectableForMount(self, mount: QStorageInfo) -> Tuple[List[str], bool]:
        """
        Given a mount, get the icon names suggested by udev or
        GVFS, and  determine whether the mount is ejectable or not.
        :param mount:  the mount to check
        :return: icon names and eject boolean
        :rtype Tuple[str, bool]
        """
        if self.gvfsControlsMounts:
            iconNames, canEject = self.gvolumeMonitor.getProps(
                mount.rootPath())
        else:
            # get the system device e.g. /dev/sdc1
            systemDevice = bytes(mount.device()).decode()
            iconNames, canEject = self.udisks2Monitor.get_device_props(
                systemDevice)
        return (iconNames, canEject)

    def addToDeviceDisplay(self, device: Device, scan_id: int) -> None:
        self.mapModel(scan_id).addDevice(scan_id, device)
        self.resizeDeviceView(self.mapView(scan_id))

    def resizeDeviceView(self, view: DeviceView) -> None:
        """
        Sets the maximum height for the device view table to match the
        number of devices being displayed
        """
        if view.model().rowCount() > 0:
            height = view.sizeHint().height()
            view.setMaximumHeight(height)
        else:
            view.setMaximumHeight(emptyViewHeight)

    @pyqtSlot()
    def cameraAdded(self) -> None:
        if not self.prefs.device_autodetection:
            logging.debug("Ignoring camera as device auto detection is off")
        else:
            logging.debug("Assuming camera will not be mounted: "
                          "immediately proceeding with scan")
        self.searchForCameras()

    @pyqtSlot()
    def cameraRemoved(self) -> None:
        """
        Handle the possible removal of a camera by comparing the
        cameras the OS knows about compared to the cameras we are
        tracking. Remove tracked cameras if they are not on the OS.

        We need this brute force method because I don't know if it's
        possible to query GIO or udev to return the info needed by
        libgphoto2
        """
        sc = self.gp_context.camera_autodetect()
        system_cameras = [(model, port) for model, port in sc if not
                          port.startswith('disk:')]
        kc = self.devices.cameras.items()
        known_cameras = [(model, port) for port, model in kc]
        removed_cameras = set(known_cameras) - set(system_cameras)
        for model, port in removed_cameras:
            scan_id = self.devices.scan_id_from_camera_model_port(model, port)
            self.removeDevice(scan_id=scan_id, stop_worker=True)

        if removed_cameras:
            self.setDownloadActionSensitivity()

    @pyqtSlot()
    def noGVFSAutoMount(self) -> None:
        """
        In Gnome like environment we rely on Gnome automatically
        mounting cameras and devices with file systems. But sometimes
        it will not automatically mount them, for whatever reason.
        Try to handle those cases.
        """
        #TODO Implement noGVFSAutoMount()
        print("Implement noGVFSAutoMount()")

    @pyqtSlot()
    def cameraMounted(self):
        if have_gio:
            self.searchForCameras()

    def unmountCamera(self, model: str, port: str) -> bool:
        if self.gvfsControlsMounts:
            self.cameras_to_unmount[port] = model
            if self.gvolumeMonitor.unmountCamera(model, port):
                return True
            else:
                del self.cameras_to_unmount[port]
        return False

    @pyqtSlot(bool, str, str, bool)
    def cameraUnmounted(self, result: bool, model: str, port: str, download_started: bool) -> None:
        if not download_started:
            assert self.cameras_to_unmount[port] == model
            del self.cameras_to_unmount[port]
            if result:
                self.startCameraScan(model, port)
            else:
                logging.debug("Not scanning %s because it could not be "
                              "unmounted", model)
        else:
            assert (model, port) in self.camera_unmounts_needed
            if result:
                self.camera_unmounts_needed.remove((model, port))
                if not len(self.camera_unmounts_needed):
                    self.startDownloadPhase2()
            else:
                #TODO report error to user!!
                pass

    def searchForCameras(self) -> None:
        if self.prefs.device_autodetection:
            cameras = self.gp_context.camera_autodetect()
            for model, port in cameras:
                if port in self.cameras_to_unmount:
                    assert self.cameras_to_unmount[port] == model
                    logging.debug("Already unmounting %s", model)
                elif self.devices.known_camera(model, port):
                    logging.debug("Camera %s is known", model)
                elif model in self.prefs.camera_blacklist:
                    logging.debug("Ignoring blacklisted camera %s", model)
                elif not port.startswith('disk:'):
                    logging.debug("Detected %s on port %s", model, port)
                    # libgphoto2 cannot access a camera when it is mounted
                    # by another process, like Gnome's GVFS or any other
                    # system. Before attempting to scan the camera, check
                    # to see if it's mounted and if so, unmount it.
                    # Unmounting is asynchronous.
                    if not self.unmountCamera(model, port):
                        self.startCameraScan(model, port)

    def startCameraScan(self, model: str, port: str) -> None:
        device = Device()
        device.set_download_from_camera(model, port)
        self.startDeviceScan(device)

    def startDeviceScan(self, device: Device) -> None:
        scan_id = self.devices.add_device(device)
        self.addToDeviceDisplay(device, scan_id)
        self.updateSourceButton()
        scan_preferences = ScanPreferences(self.prefs.ignored_paths)
        scan_arguments = ScanArguments(scan_preferences=scan_preferences,
                           device=device,
                           ignore_other_types=self.ignore_other_photo_types)
        self.scanmq.start_worker(scan_id, scan_arguments)
        self.setDownloadActionSensitivity()

    def partitionValid(self, mount: QStorageInfo) -> bool:
        """
        A valid partition is one that is:
        1) available
        2) if devices without DCIM folders are to be scanned (e.g.
        Portable Storage Devices), then the path should not be
        blacklisted
        :param mount: the mount point to check
        :return: True if valid, False otherwise
        """
        if mount.isValid() and mount.isReady():
            path = mount.rootPath()
            if (path in self.prefs.path_blacklist and
                    self.scanEvenIfNoDCIM()):
                logging.info("blacklisted device %s ignored",
                             mount.displayName())
                return False
            else:
                return True
        return False

    def shouldScanMountPath(self, path: str) -> bool:
        if self.prefs.device_autodetection:
            if (self.prefs.device_without_dcim_autodetection or
                    has_non_empty_dcim_folder(path)):
                return True
        return False

    def prepareNonCameraDeviceScan(self, device: Device) -> None:
        if not self.devices.known_device(device):
            if (self.scanEvenIfNoDCIM() and
                    not device.path in self.prefs.path_whitelist):
                # prompt user to see if device should be used or not
                pass
                #self.get_use_device(device)
            else:
                self.startDeviceScan(device)
                # if mount is not None:
                #     self.mounts_by_path[path] = scan_pid

    @pyqtSlot(str, list, bool)
    def partitionMounted(self, path: str, iconNames: List[str], canEject: bool) -> None:
        """
        Setup devices from which to download from and backup to, and
        if relevant start scanning them

        :param path: the path of the mounted partition
        :param iconNames: a list of names of icons used in themed icons
        associated with this partition
        :param canEject: whether the partition can be ejected or not
        """

        assert path in mountPaths()

        if self.monitorPartitionChanges():
            mount = QStorageInfo(path)
            if self.partitionValid(mount):
                backup_file_type = self.isBackupPath(path)

                if backup_file_type is not None:
                    if path not in self.backup_devices:
                        device = BackupDevice(mount=mount,
                                              backup_type=backup_file_type)
                        self.backup_devices[path] = device
                        self.addDeviceToBackupManager(path)
                        self.download_tracker.set_no_backup_devices(
                            self.backup_devices.no_photo_backup_devices,
                            self.backup_devices.no_video_backup_devices)
                        self.displayMessageInStatusBar()

                elif self.shouldScanMountPath(path):
                    self.auto_start_is_on = self.prefs.auto_download_upon_device_insertion
                    device = Device()
                    device.set_download_from_volume(path, mount.displayName(),
                                                    iconNames, canEject, mount)
                    self.prepareNonCameraDeviceScan(device)

    @pyqtSlot(str)
    def partitionUmounted(self, path: str) -> None:
        """
        Handle the unmounting of partitions by the system / user.

        :param path: the path of the partition just unmounted
        """
        if not path:
            return

        if self.devices.known_path(path, DeviceType.volume):
            # four scenarios -
            # the mount is being scanned
            # the mount has been scanned but downloading has not yet started
            # files are being downloaded from mount
            # files have finished downloading from mount
            scan_id = self.devices.scan_id_from_path(path, DeviceType.volume)
            self.removeDevice(scan_id=scan_id, stop_worker=True)

        elif path in self.backup_devices:
            device_id = self.backup_devices.device_id(path)
            self.backupmq.remove_device(device_id)
            del self.backup_devices[path]
            self.displayMessageInStatusBar()
            self.download_tracker.set_no_backup_devices(
                self.backup_devices.no_photo_backup_devices,
                self.backup_devices.no_video_backup_devices)

        self.setDownloadActionSensitivity()

    def removeDevice(self, scan_id: int, stop_worker: bool=True) -> None:
        assert scan_id is not None
        if scan_id in self.devices:
            device = self.devices[scan_id]
            if device in self.prompting_for_user_action:
                self.prompting_for_user_action[device].reject()
            if self.thumbnailModel.clearAll(scan_id=scan_id, keep_downloaded_files=True):
                self.generateTemporalProximityTableData()
            self.mapModel(scan_id).removeDevice(scan_id)
            if scan_id in self.scanmq.workers:
                self.scanmq.stop_worker(scan_id)
            if scan_id in self.copyfilesmq.workers:
                self.copyfilesmq.stop_worker(scan_id)
            # TODO what about stopping possible thumbnailing?
            view = self.mapView(scan_id)
            del self.devices[scan_id]
            self.resizeDeviceView(view)
            self.updateSourceButton()

    def setupBackupDevices(self):
        """
        Setup devices to back up to.

        Includes both auto detected back up devices, and manually
        specified paths.
        """
        if self.prefs.backup_device_autodetection:
            for mount in self.validMounts.mountedValidMountPoints():
                if self.partitionValid(mount):
                    path = mount.rootPath()
                    backup_type = self.isBackupPath(path)
                    if backup_type is not None:
                        self.backup_devices[path] = BackupDevice(mount=mount,
                                                     backup_type=backup_type)
                        self.addDeviceToBackupManager(path)
        else:
            self.setupManualBackup()
            for path in self.backup_devices:
                self.addDeviceToBackupManager(path)

        self.download_tracker.set_no_backup_devices(
            self.backup_devices.no_photo_backup_devices,
            self.backup_devices.no_video_backup_devices)

    def setupNonCameraDevices(self) -> None:
        """
        Setup devices from which to download and initiates their scan.
        """

        if not self.prefs.device_autodetection:
            return

        mounts = [] # type: List[QStorageInfo]
        for mount in self.validMounts.mountedValidMountPoints():
            if self.partitionValid(mount):
                path = mount.rootPath()
                if path not in self.backup_devices and self.shouldScanMountPath(path):
                    logging.debug("Will scan %s", mount.displayName())
                    mounts.append(mount)
                else:
                    logging.debug("Will not scan %s", mount.displayName())

        for mount in mounts:
            icon_names, can_eject = self.getIconsAndEjectableForMount(mount)
            device = Device()
            device.set_download_from_volume(mount.rootPath(),
                                          mount.displayName(),
                                          icon_names,
                                          can_eject,
                                          mount)
            self.prepareNonCameraDeviceScan(device)

    def setupManualPath(self) -> None:
        """
        Setup This Computer path from which to download and initiates scan.
        :return:
        """

        if not self.prefs.this_computer_source:
            return

        if self.prefs.this_computer_path:
            if not self.confirmManualDownloadLocation():
                return

            # user manually specified the path from which to download
            path = self.prefs.this_computer_path

            if path:
                if os.path.isdir(path) and os.access(path, os.R_OK):
                    logging.debug("Using This Computer path %s", path)
                    device = Device()
                    device.set_download_from_path(path)
                    self.startDeviceScan(device)
                else:
                    logging.error("This Computer download path is invalid: %s", path)
            else:
                logging.warning("This Computer download path is not specified")

    def addDeviceToBackupManager(self, path: str) -> None:
        device_id = self.backup_devices.device_id(path)
        backup_args = BackupArguments(path, self.backup_devices.name(path))
        self.backupmq.add_device(device_id, backup_args)

    def setupManualBackup(self) -> None:
        """
        Setup backup devices that the user has manually specified.

        Depending on the folder the user has chosen, the paths for
        photo and video backup will either be the same or they will
        differ.

        Because the paths are manually specified, there is no mount
        associated with them.
        """

        backup_photo_location = self.prefs.backup_photo_location
        backup_video_location = self.prefs.backup_video_location

        if not self.manualBackupPathAvailable(backup_photo_location):
            logging.warning("Photo backup path unavailable: %s",
                            backup_photo_location)
        if not self.manualBackupPathAvailable(backup_video_location):
            logging.warning("Video backup path unavailable: %s",
                            backup_video_location)

        if backup_photo_location != backup_video_location:
            backup_photo_device =  BackupDevice(mount=None,
                                backup_type=BackupLocationType.photos)
            backup_video_device = BackupDevice(mount=None,
                                backup_type=BackupLocationType.videos)
            self.backup_devices[backup_photo_location] = backup_photo_device
            self.backup_devices[backup_video_location] = backup_video_device

            logging.info("Backing up photos to %s", backup_photo_location)
            logging.info("Backing up videos to %s", backup_video_location)
        else:
            # videos and photos are being backed up to the same location
            backup_device = BackupDevice(mount=None,
                     backup_type=BackupLocationType.photos_and_videos)
            self.backup_devices[backup_photo_location] = backup_device

            logging.info("Backing up photos and videos to %s",
                         backup_photo_location)

    def isBackupPath(self, path: str) -> BackupLocationType:
        """
        Checks to see if backups are enabled and path represents a
        valid backup location. It must be writeable.

        Checks against user preferences.

        :return The type of file that should be backed up to the path,
        else if nothing should be, None
        """
        if self.prefs.backup_files:
            if self.prefs.backup_device_autodetection:
                # Determine if the auto-detected backup device is
                # to be used to backup only photos, or videos, or both.
                # Use the presence of a corresponding directory to
                # determine this.
                # The directory must be writable.
                photo_path = os.path.join(path,
                                          self.prefs.photo_backup_identifier)
                p_backup = os.path.isdir(photo_path) and os.access(
                    photo_path, os.W_OK)
                video_path = os.path.join(path,
                                          self.prefs.video_backup_identifier)
                v_backup = os.path.isdir(video_path) and os.access(
                    video_path, os.W_OK)
                if p_backup and v_backup:
                    logging.info("Photos and videos will be backed up to "
                                 "%s", path)
                    return BackupLocationType.photos_and_videos
                elif p_backup:
                    logging.info("Photos will be backed up to %s", path)
                    return BackupLocationType.photos
                elif v_backup:
                    logging.info("Videos will be backed up to %s", path)
                    return BackupLocationType.videos
            elif path == self.prefs.backup_photo_location:
                # user manually specified the path
                if self.manualBackupPathAvailable(path):
                    return BackupLocationType.photos
            elif path == self.prefs.backup_video_location:
                # user manually specified the path
                if self.manualBackupPathAvailable(path):
                    return BackupLocationType.videos
            return None

    def manualBackupPathAvailable(self, path: str) -> bool:
        return os.access(path, os.W_OK)

    def clearNonRunningDownloads(self):
        """
        Clears the display of downloads that are currently not running
        """

        #TODO implement once UI is more complete
        # Stop any processes currently scanning or creating thumbnails
        pass

        # Remove them from the user interface
        # for scan_pid in self.device_collection.get_all_displayed_processes():
        #     if scan_pid not in self.download_active_by_scan_pid:
        #         self.device_collection.remove_device(scan_pid)
        #         self.thumbnails.clear_all(scan_pid=scan_pid)

    def monitorPartitionChanges(self) -> bool:
        """
        If the user is downloading from a manually specified location,
        and is not using any automatically detected backup devices,
        then there is no need to monitor for devices with filesystems
        being added or removed
        :return: True if should monitor, False otherwise
        """
        return (self.prefs.device_autodetection or
                self.prefs.backup_device_autodetection)

    def confirmManualDownloadLocation(self) -> bool:
        """
        Queries the user to ask if they really want to download from locations
        that could take a very long time to scan. They can choose yes or no.

        Returns True if yes or there was no need to ask the user, False if the
        user said no.
        """
        #TODO implement confirmManualDownloadLocation()
        return True

    def scanEvenIfNoDCIM(self) -> bool:
        """
        Determines if partitions should be scanned even if there is
        no DCIM folder present in the base folder of the file system.

        This is necessary when both portable storage device automatic
        detection is on, and downloading from automatically detected
        partitions is on.
        :return: True if scans of such partitions should occur, else
        False
        """
        return (self.prefs.device_autodetection and
                self.prefs.device_without_dcim_autodetection)

    def displayMessageInStatusBar(self, update_only_marked: bool=False) -> None:
        """
        Displays message on status bar:
        1. files selected for download (if available).
        2. the amount of space free on the filesystem the files will be
           downloaded to.
        3. backup volumes / path being used.

        :param update_only_marked: if True, refreshes only the number
         of files marked for download, not regnerating other
         components of the existing status message
        """

        if self.basic_status_message is None or not update_only_marked:
            self.basic_status_message = self.generateBasicStatusMessage()

        files_avilable = self.thumbnailModel.getNoFilesAvailableForDownload()

        if sum(files_avilable.values()) != 0:
            files_to_download = self.thumbnailModel.getNoFilesMarkedForDownload()
            files_avilable_sum = files_avilable.summarize_file_count()[0]
            size = self.thumbnailModel.getSizeOfFilesMarkedForDownload()
            size = format_size_for_user(size)
            if files_to_download:
                files_selected = _('%(number)s of %(available files)s '
                                   '(%(size)s)') % {
                                   'number': thousands(files_to_download),
                                   'available files': files_avilable_sum,
                                   'size': size}
            else:
                files_selected = _('%(number)s of %(available files)s') % {
                                   'number': thousands(files_to_download),
                                   'available files': files_avilable_sum}
            msg = _('%(files_selected)s. %(freespace_and_backups)s') % {
                'freespace_and_backups': self.basic_status_message,
                'files_selected': files_selected}
        else:
            msg = self.basic_status_message
        self.statusBar().showMessage(msg)

    def generateBasicStatusMessage(self) -> str:
        photo_dir = self.isValidDownloadDir(
            path=self.prefs.photo_download_folder,
            is_photo_dir=True,
            show_error_in_log=True)
        video_dir = self.isValidDownloadDir(
            path=self.prefs.video_download_folder,
            is_photo_dir=False,
            show_error_in_log=True)
        if photo_dir and video_dir:
            same_fs = same_file_system(self.prefs.photo_download_folder,
                                       self.prefs.video_download_folder)
        else:
            same_fs = False

        dirs = []
        if photo_dir:
            dirs.append(self.prefs.photo_download_folder)
        if video_dir and not same_fs:
            dirs.append(self.prefs.video_download_folder)

        if len(dirs) == 1:
            free = format_size_for_user(size_in_bytes=shutil.disk_usage(dirs[0]).free)
            # Free space available on the filesystem for downloading to
            # Displayed in status bar message on main window
            # e.g. 14.7GB free
            msg = _("%(free)s free on destination.") % {'free': free}
        elif len(dirs) == 2:
            free1, free2 = (format_size_for_user(size_in_bytes=shutil.disk_usage(
                path).free) for path in dirs)
            # Free space available on the filesystem for downloading to
            # Displayed in status bar message on main window
            # e.g. Free space: 21.3GB (photos); 14.7GB (videos).
            msg = _('Free space on destination drives: %(photos)s (photos); '
                    '%(videos)s (videos).') % {'photos': free1, 'videos': free2}
        else:
            msg = ''

        if self.prefs.backup_files:
            if not self.prefs.backup_device_autodetection:
                if self.prefs.backup_photo_location ==  self.prefs.backup_video_location:
                    # user manually specified the same location for photos
                    # and video backups
                    msg2 = _('Backing up photos and videos to %(path)s') % {
                        'path':self.prefs.backup_photo_location}
                else:
                    # user manually specified different locations for photo
                    # and video backups
                    msg2 = _('Backing up photos to %(path)s and videos to %(path2)s')  % {
                             'path': self.prefs.backup_photo_location,
                             'path2': self.prefs.backup_video_location}
            else:
                msg2 = self.displayBackupMounts()

            if msg:
                msg = _("%(freespace)s %(backuppaths)s.") % {'freespace':
                                                  msg, 'backuppaths': msg2}
            else:
                msg = msg2

        return msg.rstrip()

    def displayBackupMounts(self) -> str:
        """
        Create a message to be displayed to the user showing which
        backup mounts will be used
        :return the string to be displayed
        """
        message =  ''

        backup_device_names = [self.backup_devices.name(path) for path in
                          self.backup_devices]
        message = make_internationalized_list(backup_device_names)

        if len(backup_device_names) > 1:
            message = _("Using backup devices %(devices)s") % dict(
                devices=message)
        elif len(backup_device_names) == 1:
            message = _("Using backup device %(device)s")  % dict(
                device=message)
        else:
            message = _("No backup devices detected")
        return message


class QtSingleApplication(QApplication):
    """
    Taken from
    http://stackoverflow.com/questions/12712360/qtsingleapplication
    -for-pyside-or-pyqt
    """

    messageReceived = QtCore.pyqtSignal(str)

    def __init__(self, programId: str, *argv) -> None:
        super().__init__(*argv)
        self._id = programId
        self._activationWindow = None # type: RapidWindow
        self._activateOnMessage = False # type: bool

        # Is there another instance running?
        self._outSocket = QLocalSocket() # type: QLocalSocket
        self._outSocket.connectToServer(self._id)
        self._isRunning = self._outSocket.waitForConnected() # type: bool

        self._outStream = None # type: QTextStream
        self._inSocket  = None
        self._inStream  = None # type: QTextStream
        self._server    = None

        if self._isRunning:
            # Yes, there is.
            self._outStream = QTextStream(self._outSocket)
            self._outStream.setCodec('UTF-8')
        else:
            # No, there isn't, at least not properly.
            # Cleanup any past, crashed server.
            error = self._outSocket.error()
            if error == QLocalSocket.ConnectionRefusedError:
                self.close()
                QLocalServer.removeServer(self._id)
            self._outSocket = None
            self._server = QLocalServer()
            self._server.listen(self._id)
            self._server.newConnection.connect(self._onNewConnection)

    def close(self) -> None:
        if self._inSocket:
            self._inSocket.disconnectFromServer()
        if self._outSocket:
            self._outSocket.disconnectFromServer()
        if self._server:
            self._server.close()

    def isRunning(self) -> bool:
        return self._isRunning

    def id(self) -> str:
        return self._id

    def activationWindow(self) -> RapidWindow:
        return self._activationWindow

    def setActivationWindow(self, activationWindow: RapidWindow,
                            activateOnMessage: bool = True) -> None:
        self._activationWindow = activationWindow
        self._activateOnMessage = activateOnMessage

    def activateWindow(self) -> None:
        if not self._activationWindow:
            return
        self._activationWindow.setWindowState(
            self._activationWindow.windowState() & ~Qt.WindowMinimized)
        self._activationWindow.raise_()
        self._activationWindow.activateWindow()

    def sendMessage(self, msg) -> bool:
        if not self._outStream:
            return False
        self._outStream << msg << '\n'
        self._outStream.flush()
        return self._outSocket.waitForBytesWritten()

    def _onNewConnection(self) -> None:
        if self._inSocket:
            self._inSocket.readyRead.disconnect(self._onReadyRead)
        self._inSocket = self._server.nextPendingConnection()
        if not self._inSocket:
            return
        self._inStream = QTextStream(self._inSocket)
        self._inStream.setCodec('UTF-8')
        self._inSocket.readyRead.connect(self._onReadyRead)
        if self._activateOnMessage:
            self.activateWindow()

    def _onReadyRead(self) -> None:
        while True:
            msg = self._inStream.readLine()
            if not msg: break
            self.messageReceived.emit(msg)


def get_versions() -> List[str]:
    versions = [
        'Rapid Photo Downloader: {}'.format(__about__.__version__),
        'Platform: {}'.format(platform.platform()),
        'Python: {}'.format(platform.python_version()),
        'Qt: {}'.format(QtCore.QT_VERSION_STR),
        'PyQt: {}'.format(QtCore.PYQT_VERSION_STR),
        'ZeroMQ: {}'.format(zmq.zmq_version()),
        'Python ZeroMQ: {}'.format(zmq.pyzmq_version()),
        'gPhoto2: {}'.format(gphoto2_version()),
        'Python gPhoto2: {}'.format(python_gphoto2_version()),
        'ExifTool: {}'.format(EXIFTOOL_VERSION),
        'GExiv2: {}'.format(gexiv2_version()),
        'psutil: {}'.format('.'.join((str(v) for v in psutil.version_info))),
        'sortedcontainers: {}'.format(sortedcontainers.__version__)]
    v = exiv2_version()
    if v:
        versions.append('Exiv2: {}'.format(v))
    return versions

def darkFusion(app: QApplication):
    app.setStyle("Fusion")

    dark_palette = QPalette()

    dark_palette.setColor(QPalette.Window, QColor(53, 53, 53))
    dark_palette.setColor(QPalette.WindowText, Qt.white)
    dark_palette.setColor(QPalette.Base, QColor(25, 25, 25))
    dark_palette.setColor(QPalette.AlternateBase, QColor(53, 53, 53))
    dark_palette.setColor(QPalette.ToolTipBase, Qt.white)
    dark_palette.setColor(QPalette.ToolTipText, Qt.white)
    dark_palette.setColor(QPalette.Text, Qt.white)
    dark_palette.setColor(QPalette.Button, QColor(53, 53, 53))
    dark_palette.setColor(QPalette.ButtonText, Qt.white)
    dark_palette.setColor(QPalette.BrightText, Qt.red)
    dark_palette.setColor(QPalette.Link, QColor(42, 130, 218))
    dark_palette.setColor(QPalette.Highlight, QColor(42, 130, 218))
    dark_palette.setColor(QPalette.HighlightedText, Qt.black)

    app.setPalette(dark_palette)
    style = """
    QToolTip { color: #ffffff; background-color: #2a82da; border: 1px solid white; }
    """
    app.setStyleSheet(style)

class SplashScreen(QSplashScreen):
    def drawContents(self, painter: QPainter):
        painter.save()
        painter.setPen(QColor(Qt.black))
        painter.drawText(18, 64, __about__.__version__)
        painter.restore()

def parser_options(formatter_class=argparse.HelpFormatter):
    parser = argparse.ArgumentParser(prog=__about__.__title__,
                                     description=__about__.__summary__,
                                     formatter_class=formatter_class)

    parser.add_argument('--version', action='version', version=
        '%(prog)s {}'.format(__about__.__version__))
    parser.add_argument('--detailed-version', action='store_true',
        help="show version numbers of program and its libraries and exit")
    parser.add_argument("-v", "--verbose",  action="store_true", dest="verbose",
         help=_("display program information when run from the command line"))
    parser.add_argument("--debug", action="store_true", dest="debug",
         help=_("display debugging information when run from the command line"))
    parser.add_argument("-e",  "--extensions", action="store_true",
         dest="extensions",
         help=_("list photo and video file extensions the program recognizes "
                "and exit"))
    parser.add_argument("-a", "--auto-detect", choices=['on','off'],
        dest="auto_detect", help=_("turn on or off the automatic detection of devices from which "
       "to download"))
    parser.add_argument("-t", "--this-computer", type=str,
        metavar=_("PATH"), dest="this_computer_path",
        help=_("the PATH from which to download (set PATH to '' to turn off)"))
    parser.add_argument("--photo-destination", type=str,
        metavar=_("PATH"), dest="photo_location",
        help=_("the PATH where photos will be downloaded"))
    parser.add_argument("--video-destination", type=str,
        metavar=_("PATH"), dest="video_location",
        help=_("the PATH where videos will be downloaded"))
    parser.add_argument("-b", "--backup", choices=['on','off'],
        dest="backup", help=_("turn on or off the backing up of photos and videos while "
                              "downloading"))
    parser.add_argument("--backup-auto-detect", choices=['on','off'],
        dest="backup_auto_detect",
        help=_("turn on or off the automatic detection of backup devices"))
    parser.add_argument("--photo-backup-identifier", type=str,
        metavar=_("FOLDER"), dest="photo_backup_identifier",
        help=_("the FOLDER in which backups are stored on the automatically detected photo backup "
               "device, with the folder's name being used to identify whether or not the device "
               "is used for backups. For each device you wish to use for backing photos up to, "
               "create a folder on it with this name."))
    parser.add_argument("--video-backup-identifier", type=str,
        metavar=_("FOLDER"), dest="video_backup_identifier",
        help=_("the FOLDER in which backups are stored on the automatically detected video backup "
               "device, with the folder's name being used to identify whether or not the device "
               "is used for backups. For each device you wish to use for backing up videos to, "
               "create a folder on it with this name."))
    parser.add_argument("--photo-backup-location", type=str,
        metavar=_("PATH"), dest="photo_backup_location",
        help=_("the PATH where photos will be backed up when automatic "
        "detection of backup devices is turned off"))
    parser.add_argument("--video-backup-location", type=str,
        metavar=_("PATH"), dest="video_backup_location",
        help=_("the PATH where videos will be backed up when automatic "
        "detection of backup devices is turned off"))
    parser.add_argument("--ignore-other-photo-file-types", action="store_true", dest="ignore_other",
                        help=_('ignore photos with the following extensions: %s') %
                        make_internationalized_list([s.upper() for s in OTHER_PHOTO_EXTENSIONS]))
    parser.add_argument("--thumbnail-cache", dest="thumb_cache",
                        choices=['on','off'],
                        help=_("turn on or off use of the Rapid Photo Downloader Thumbnail Cache"))
    parser.add_argument("--delete-thumbnail-cache", dest="delete_thumb_cache",
                        action="store_true",
                        help=_("Delete all thumbnails in the Rapid Photo Downloader Thumbnail "
                               "Cache"))
    parser.add_argument("--reset", action="store_true", dest="reset",
                 help=_("reset all program settings and caches and exit"))
    return parser

def main():

    parser = parser_options()

    args = parser.parse_args()
    if args.detailed_version:
        print('\n'.join(get_versions()))
        sys.exit(0)

    if args.extensions:
        photos = list((ext.upper() for ext in PHOTO_EXTENSIONS))
        videos = list((ext.upper() for ext in VIDEO_EXTENSIONS))
        extensions = ((photos, _("Photos")),
                      (videos, _("Videos")))
        for exts, file_type in extensions:
            extensions = make_internationalized_list(exts)
            print('{}: {}'.format(file_type, extensions))
        sys.exit(0)

    global logging_level

    if args.debug:
        logging_level = logging.DEBUG
    elif args.verbose:
        logging_level = logging.INFO
    else:
        logging_level = logging.ERROR

    logging.basicConfig(format=constants.logging_format,
                    datefmt=constants.logging_date_format,
                    level=logging_level)

    if args.auto_detect:
        auto_detect= args.auto_detect == 'on'
        if auto_detect:
            logging.info("Device auto detection turned on from command line")
        else:
            logging.info("Device auto detection turned off from command line")
    else:
        auto_detect=None
        
    if args.this_computer_path:
        this_computer_path=args.this_computer_path
        if this_computer_path[-1]=='/':
            this_computer_path = this_computer_path[:-1]
        logging.info("This computer path set from command line: %s", this_computer_path)
    else:
        this_computer_path=None
        
    if args.photo_location:
        photo_location=args.photo_location
        if photo_location[-1]=='/':
            photo_location = photo_location[:-1]
        logging.info("Photo location set from command line: %s", photo_location)
    else:
        photo_location=None
        
    if args.video_location:
        video_location=args.video_location
        if video_location[-1]=='/':
            video_location = video_location[:-1]
        logging.info("video location set from command line: %s", video_location)
    else:
        video_location=None

    if args.backup:
        backup = args.backup == 'on'
        if backup:
            logging.info("Backup turned on from command line")
        else:
            logging.info("Backup turned off from command line")
    else:
        backup=None

    if args.backup_auto_detect:
        backup_auto_detect = args.backup_auto_detect == 'on'
        if backup_auto_detect:
            logging.info("Automatic detection of backup devices turned on from command line")
        else:
            logging.info("Automatic detection of backup devices turned off from command line")
    else:
        backup_auto_detect=None

    if args.photo_backup_identifier:
        photo_backup_identifier=args.photo_backup_identifier
        logging.info("Photo backup identifier set from command line: %s", photo_backup_identifier)
    else:
        photo_backup_identifier=None

    if args.video_backup_identifier:
        video_backup_identifier=args.video_backup_identifier
        logging.info("Video backup identifier set from command line: %s", video_backup_identifier)
    else:
        video_backup_identifier=None

    if args.photo_backup_location:
        photo_backup_location=args.photo_backup_location
        logging.info("Photo backup location set from command line: %s", photo_backup_location)
    else:
        photo_backup_location=None

    if args.video_backup_location:
        video_backup_location=args.video_backup_location
        logging.info("Video backup location set from command line: %s", video_backup_location)
    else:
        video_backup_location=None

    if args.thumb_cache:
        thumb_cache = args.thumb_cache == 'on'
    else:
        thumb_cache = None

    appGuid = '8dbfb490-b20f-49d3-9b7d-2016012d2aa8'

    # See note above regarding avoiding crashes
    global app
    app = QtSingleApplication(appGuid, sys.argv)
    if app.isRunning():
        print('Rapid Photo Downloader is already running')
        sys.exit(0)

    app.setOrganizationName("Rapid Photo Downloader")
    app.setOrganizationDomain("damonlynch.net")
    app.setApplicationName("Rapid Photo Downloader")
    app.setWindowIcon(QtGui.QIcon(':/rapid-photo-downloader.svg'))

    # darkFusion(app)
    # app.setStyle('Fusion')

    # Resetting preferences must occur after QApplication is instantiated
    if args.reset:
        prefs = Preferences()
        prefs.reset()
        prefs.sync()
        d = DownloadedSQL()
        d.update_table(reset=True)
        cache = ThumbnailCacheSql()
        cache.purge_cache()
        print(_("All settings and caches have been reset"))
        sys.exit(0)

    if args.delete_thumb_cache:
        cache = ThumbnailCacheSql()
        cache.purge_cache()
        print(_("Thumbnail Cache has been reset"))
        sys.exit(0)

    splash = SplashScreen(QPixmap(':/splashscreen.png'), Qt.WindowStaysOnTopHint)
    splash.show()
    app.processEvents()

    rw = RapidWindow(auto_detect=auto_detect, this_computer_path=this_computer_path,
                     photo_download_folder=photo_location,
                     video_download_folder=video_location,
                     backup=backup,
                     backup_auto_detect=backup_auto_detect,
                     photo_backup_identifier=photo_backup_identifier,
                     video_backup_identifier=video_backup_identifier,
                     photo_backup_location=photo_backup_location,
                     video_backup_location=video_backup_location,
                     ignore_other_photo_types=args.ignore_other,
                     thumb_cache=thumb_cache)

    splash.finish(rw)

    app.setActivationWindow(rw)
    code = app.exec_()

    sys.exit(code)

if __name__ == "__main__":
    main()