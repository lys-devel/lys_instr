import os
import json
from lys.Qt import QtWidgets, QtCore


class ControllerMemory(QtWidgets.QWidget):
    """
    Memory panel for controller positions.

    Displays saved controller positions, allows saving/loading/deleting entries, and stores positions in a JSON file under the local ``.lys`` directory.
    """

    def __init__(self, objs, path=None, parent=None):
        """
        Create the memory panel and load saved positions.

        Args:
            objs (Sequence): Sequence of controller objects used to read/write positions.
            path (str | None): Optional file name to use for saved positions (stored under ``.lys/.lys_instr``).
            parent (QWidget | None): Optional parent widget.
        """
        super().__init__(parent)
        self._objs = objs
        self.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Expanding)
        self.__initLayout()

        self._savedPositions = []
        self._import(path)
        self._updateMemory()

    def __initLayout(self):
        """
        Create and arrange widgets for the memory panel.
        """
        self._positionList = QtWidgets.QTreeWidget()
        self._positionList.setColumnCount(3)
        self._positionList.setHeaderLabels(["Label", "Position", "Memo"])
        self._positionList.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self._positionList.itemSelectionChanged.connect(lambda: self._updateMemoryBtns(load, delete))
        self._positionList.setIndentation(0)
        self._positionList.setEditTriggers(QtWidgets.QAbstractItemView.DoubleClicked | QtWidgets.QAbstractItemView.SelectedClicked)
        self._positionList.itemChanged.connect(self._memoEdited)
        self._positionList.header().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        self._positionList.header().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeToContents)
        self._positionList.header().setSectionResizeMode(2, QtWidgets.QHeaderView.Stretch)
        self._positionList.setItemDelegateForColumn(0, _NoEditDelegate(self._positionList))
        self._positionList.setItemDelegateForColumn(1, _NoEditDelegate(self._positionList))

        save = QtWidgets.QPushButton("Save", clicked=self._save)
        save.setEnabled(True)
        load = QtWidgets.QPushButton("Load", clicked=self._load)
        load.setEnabled(False)
        delete = QtWidgets.QPushButton("Delete", clicked=self._delete)
        delete.setEnabled(False)

        self._memoryBtnsLayout = QtWidgets.QHBoxLayout()
        self._memoryBtnsLayout.addWidget(save)
        self._memoryBtnsLayout.addWidget(load)
        self._memoryBtnsLayout.addWidget(delete)

        layout = QtWidgets.QVBoxLayout()
        layout.addWidget(QtWidgets.QLabel("Memory"))
        layout.addWidget(self._positionList)
        layout.addLayout(self._memoryBtnsLayout)
        self.setLayout(layout)

    def _import(self, path):
        """
        Import saved positions from disk and set the memory file path.

        Args:
            path (str | None): Optional file name in the local memory directory (``.lys/.lys_instr``) to load. If ``None``, no file is loaded.
        """
        if path is None:
            self._path = None
            return

        # Load memory file
        dir = os.path.join(".lys", ".lys_instr")
        os.makedirs(dir, exist_ok=True)
        self._path = os.path.join(dir, path)

        if os.path.exists(self._path):
            with open(self._path, "r") as f:
                self._savedPositions = json.load(f)

    def _export(self):
        """
        Write the current saved positions to the configured memory file.
        """
        if self._path is None:
            return
        with open(self._path, "w") as f:
            json.dump(self._savedPositions, f)

    @property
    def positions(self):
        """
        Saved positions.

        Returns:
            list[dict]: Copy of the saved positions. Each item has keys ``"label"``, ``"position"`` (dict of axis name to value) and ``"memo"``.
        """
        return [{"label": item["label"], "position": dict(item["position"]), "memo": item["memo"]} for item in self._savedPositions]

    def savePosition(self, memo=""):
        """
        Save the current positions of all axes.

        Args:
            memo (str): Memo of the saved position.

        Returns:
            str: Label of the saved position.
        """
        labels = {item["label"] for item in self._savedPositions}
        i = 1
        while f"{i}" in labels:
            i += 1
        position = {}
        for obj in self._objs:
            position.update(obj.get())
        self._savedPositions.append({"label": f"{i}", "position": position, "memo": memo})
        self._export()
        self._updateMemory()
        return f"{i}"

    def loadPosition(self, label, wait=False):
        """
        Move the axes to a saved position.

        Axes that are not included in the saved position are not moved.

        Args:
            label (str): Label of the saved position.
            wait (bool): If True, block until all axes become idle.

        Raises:
            ValueError: If ``label`` is not found.
        """
        loadedValues = self._find(label)["position"]
        moved = []
        for obj in self._objs:
            value = {name: loadedValues[name] for name in obj.nameList if name in loadedValues}
            if len(value) > 0:
                obj.set(**value)
                moved.append(obj)
        if wait:
            for obj in moved:
                obj.waitForReady()

    def deletePosition(self, *labels):
        """
        Delete saved positions.

        Args:
            *labels (str): Labels of the saved positions to be deleted.

        Raises:
            ValueError: If any of ``labels`` is not found.
        """
        labels = [str(label) for label in labels]
        for label in labels:
            self._find(label)
        self._savedPositions = [item for item in self._savedPositions if item["label"] not in labels]
        self._export()
        self._updateMemory()

    def setMemo(self, label, memo):
        """
        Set the memo of a saved position.

        Args:
            label (str): Label of the saved position.
            memo (str): The memo.

        Raises:
            ValueError: If ``label`` is not found.
        """
        self._find(label)["memo"] = memo
        self._export()
        self._updateMemory()

    def _find(self, label):
        for item in self._savedPositions:
            if item["label"] == str(label):
                return item
        raise ValueError(f"Saved position '{label}' is not found. Available labels: {[item['label'] for item in self._savedPositions]}")

    def _save(self):
        """
        Save the current axis positions (Save button).
        """
        self.savePosition()

    def _load(self):
        """
        Move the axes to the selected saved position (Load button).
        """
        selections = self._positionList.selectedItems()
        if not selections:
            return
        self.loadPosition(selections[0].text(0))

    def _delete(self):
        """
        Delete the selected saved positions (Delete button).
        """
        self.deletePosition(*{i.text(0) for i in self._positionList.selectedItems()})

    def _memoEdited(self, item, column):
        """
        Handle edits to the memo field in the memory panel.

        Args:
            item (QTreeWidgetItem): The edited item.
            column (int): The column index that was edited.
        """
        if column == 2:
            self._find(item.text(0))["memo"] = item.text(2)
            self._export()

    def _updateMemory(self):
        """
        Update the memory panel with the latest saved positions.
        """
        self._positionList.clear()
        for itemDict in self._savedPositions:
            displayedPosition = ", ".join(f"{v:.3f}" for v in itemDict["position"].values())
            item = QtWidgets.QTreeWidgetItem([itemDict["label"], displayedPosition, itemDict["memo"]])

            # Protect columns 0 and 1 from editing
            item.setFlags(item.flags() | QtCore.Qt.ItemIsEditable)
            self._positionList.addTopLevelItem(item)

        for col in range(self._positionList.columnCount()):
            self._positionList.resizeColumnToContents(col)

    def _updateMemoryBtns(self, loadBtn, deleteBtn):
        """
        Enable or disable memory panel buttons based on selection.

        Args:
            loadBtn (QPushButton): The load button.
            deleteBtn (QPushButton): The delete button.
        """
        selected = len(self._positionList.selectedItems()) > 0
        loadBtn.setEnabled(selected)
        deleteBtn.setEnabled(selected)


class _NoEditDelegate(QtWidgets.QStyledItemDelegate):
    """
    Delegate to prevent editing of certain columns in a QTreeWidget.
    """

    def createEditor(self, parent, option, index):
        """
        Prevent editing by always returning None.

        Returns:
            ``None``
        """
        return None
