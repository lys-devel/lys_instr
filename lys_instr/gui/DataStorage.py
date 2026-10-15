import qtawesome as qta
import json

from lys.Qt import QtCore, QtWidgets
from .widgets import FolderButton


class DataStorageGUI(QtWidgets.QWidget):
    """
    GUI widget for configuring data storage options.

    Provide controls to select base folder, data folder, file name, numbering, and enable/disable saving.
    Update the storage backend and show a saving-status indicator.
    Changes made to the storage object programmatically are reflected in the widgets.
    """

    def __init__(self, obj):
        """
        Create the storage GUI and bind it to the storage object.

        Args:
            obj (DataStorage): The storage backend object.
        """
        super().__init__()
        self._obj = obj
        self._settingPath = False

        self._initLayout()
        self._syncFromObj()
        self._obj.pathChanged.connect(self._syncFromObj)
        self._obj.enabledChanged.connect(self._syncFromObj)
        self._obj.numberChanged.connect(self._updateNumber)
        self._obj.savingStateChanged.connect(self._savingStateChanged)
        self._obj.errorOccurred.connect(self._errorOccurred)

    def _initLayout(self):
        """
        Create and arrange the widgets for the storage configuration panel.
        """
        # Widgets for data saving
        browse = FolderButton(clicked=self._browse)
        browse.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Preferred)

        self._base = QtWidgets.QLineEdit(objectName="DataStorage_base")
        self._folder = QtWidgets.QLineEdit(objectName="DataStorage_folder")
        self._name = QtWidgets.QLineEdit(objectName="DataStorage_name")

        self._savedIndicator = QtWidgets.QLabel()
        self._savedIndicator.setPixmap(qta.icon("ri.check-line", color="green").pixmap(24, 24))
        self._savedIndicator.setAlignment(QtCore.Qt.AlignCenter)

        self._savingState = QtWidgets.QLabel("[Status] Waiting")

        self._numberedCheck = QtWidgets.QCheckBox("Numbered", checked=True, objectName="DataStorage_numbered")
        self._enabledCheck = QtWidgets.QCheckBox("Enabled", checked=True, objectName="DataStorage_enabled")

        self._number = QtWidgets.QSpinBox()
        self._number.setRange(0, 2**31 - 1)
        self._number.setReadOnly(True)
        self._number.setButtonSymbols(QtWidgets.QSpinBox.NoButtons)

        # Connect signals
        self._base.textChanged.connect(self._pathChanged)
        self._folder.textChanged.connect(self._pathChanged)
        self._name.textChanged.connect(self._pathChanged)
        self._numberedCheck.toggled.connect(self._pathChanged)
        self._enabledCheck.toggled.connect(self._pathChanged)

        # Layout setup
        pathLayout = QtWidgets.QGridLayout()
        pathLayout.setAlignment(QtCore.Qt.AlignTop)
        pathLayout.addWidget(browse, 1, 0)
        pathLayout.addWidget(QtWidgets.QLabel("Base folder"), 0, 1)
        pathLayout.addWidget(QtWidgets.QLabel("Data folder"), 0, 2)
        pathLayout.addWidget(QtWidgets.QLabel("File name"), 0, 3)
        pathLayout.addWidget(self._numberedCheck, 0, 4)
        pathLayout.addWidget(self._base, 1, 1)
        pathLayout.addWidget(self._folder, 1, 2)
        pathLayout.addWidget(self._name, 1, 3)
        pathLayout.addWidget(self._number, 1, 4)
        pathLayout.addWidget(self._savedIndicator, 2, 0)
        pathLayout.addWidget(self._savingState, 2, 1, 1, 3)
        pathLayout.addWidget(self._enabledCheck, 2, 4)

        mainLayout = QtWidgets.QVBoxLayout()
        mainLayout.addLayout(pathLayout)
        self.setLayout(mainLayout)

    def _pathChanged(self):
        """
        Handle changes to path-related widgets and propagate them to the storage object.
        """
        if self._settingPath:
            return

        self._settingPath = True

        # Update storage object properties from GUI fields
        self._obj.base = self._base.text()
        self._obj.folder = self._folder.text()
        self._obj.name = self._name.text()
        self._obj.numbered = self._numberedCheck.isChecked()
        self._obj.enabled = self._enabledCheck.isChecked()

        self._updateNumber()
        self._updatePathState()

        # Save last path info
        lastPath = {"base": self._base.text(), "folder": self._folder.text(), "name": self._name.text()}
        with open(".lastPath.json", "w") as f:
            json.dump(lastPath, f)

        self._settingPath = False

    def _syncFromObj(self):
        """
        Update the widgets from the storage object when its properties are changed programmatically.
        """
        if self._settingPath:
            return

        self._settingPath = True
        for edit, value in [(self._base, self._obj.base), (self._folder, self._obj.folder), (self._name, self._obj.name)]:
            if edit.text() != value:
                edit.setText(value)
        self._numberedCheck.setChecked(self._obj.numbered)
        self._enabledCheck.setChecked(self._obj.enabled)
        self._updateNumber()
        self._updatePathState()
        self._settingPath = False

    def _updateNumber(self):
        """
        Show the next file number, or clear it if numbering is disabled.
        """
        number = self._obj.getNumber()
        self._number.setValue(number) if number is not None else self._number.clear()

    def _updatePathState(self):
        """
        Highlight invalid path fields and show the reason while not saving.
        """
        errors = self._obj.pathErrors()
        for key, edit in [("base", self._base), ("folder", self._folder), ("name", self._name)]:
            edit.setStyleSheet("border: 1px solid red;" if key in errors else "")
            edit.setToolTip(errors.get(key, ""))
        if not self._obj.saving:
            self._savingStateChanged(False)

    def _browse(self):
        """
        Open a dialog to choose the base folder and update the Base field.
        """
        baseStr = QtWidgets.QFileDialog.getExistingDirectory(self, "Select base folder", self._base.text())
        if baseStr:
            self._base.setText(baseStr)

    def _savingStateChanged(self, saving):
        """
        Update the GUI to reflect the current saving state.

        Args:
            saving (bool): True when saving is in progress, False otherwise.
        """
        if saving:
            text = f"[Status] {len(self._obj._paths)} files reserved, {len(self._obj._threads)} files being saved."
            self._savingState.setText(text)
        else:
            self._savingState.setText("[Status] Waiting")
        self._savingState.setToolTip("")

        icon = qta.icon("ri.loader-2-line", color="orange") if saving else qta.icon("ri.check-line", color="green")
        self._savedIndicator.setPixmap(icon.pixmap(24, 24))

        errors = self._obj.pathErrors()
        if not saving and errors:
            msg = ", ".join(f"{key} {reason}" for key, reason in errors.items())
            self._savingState.setText(f"[Invalid path] {msg}")
            self._savingState.setToolTip(msg)
            self._savedIndicator.setPixmap(qta.icon("ri.error-warning-line", color="red").pixmap(24, 24))

    def _errorOccurred(self, msg):
        """
        Show an error message in the status label until the saving state changes.

        Args:
            msg (str): The error message.
        """
        self._savingState.setText(f"[Error] {msg}")
        self._savingState.setToolTip(msg)
        self._savedIndicator.setPixmap(qta.icon("ri.error-warning-line", color="red").pixmap(24, 24))
