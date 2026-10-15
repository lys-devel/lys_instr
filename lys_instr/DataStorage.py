import os
import numpy as np
import logging
from lys import Wave
from lys.Qt import QtCore

# Characters and names that cannot be used in file and folder names on Windows.
_INVALID_CHARS = set('<>:"|?*') | {chr(i) for i in range(32)}
_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}


def _checkComponent(text):
    """
    Return the reason why ``text`` cannot be used as a file or folder name, or ``None`` if it is valid.
    """
    if text.strip() == "":
        return "must not be empty"
    if text != text.strip():
        return "must not start or end with spaces"
    if text.endswith("."):
        return "must not end with '.'"
    invalid = sorted(set(text) & _INVALID_CHARS)
    if invalid:
        return f"must not contain {' '.join(repr(c) for c in invalid)}"
    if text.split(".")[0].upper() in _RESERVED_NAMES:
        return f"'{text}' is a reserved name"
    return None


class DataStorage(QtCore.QObject):
    """
    Threaded, asynchronous storage and file management for multi-dimensional data.

    This class reserves disk-backed arrays for incoming frames, buffers updates, and saves buffered data to disk using a background worker thread so the application remains responsive.
    It emits Qt signals to report saving state and to request metadata tags for saved files.
    """

    #: Signal (bool) emitted when saving state changes.
    savingStateChanged = QtCore.pyqtSignal(bool)

    #: Signal (dict) emitted to request metadata tags.
    tagRequest = QtCore.pyqtSignal(dict)

    #: Signal emitted when the next available file number changes.
    numberChanged = QtCore.pyqtSignal()

    #: Signal emitted when the save path (base, folder, name, or numbered) changes.
    pathChanged = QtCore.pyqtSignal()

    #: Signal (bool) emitted when the enabled state changes.
    enabledChanged = QtCore.pyqtSignal(bool)

    #: Signal (str) emitted when reserving or saving a file fails.
    errorOccurred = QtCore.pyqtSignal(str)

    def __init__(self, **kwargs):
        """
        Initialize the data storage instance.

        Set default storage options and prepare internal buffers.

        Args:
            **kwargs: Additional keyword arguments passed to the base class.
        """
        super().__init__(**kwargs)
        self._base = "."
        self._folder = "folder"
        self._name = "data"
        self._enabled = True
        self._numbered = True
        self._threads = []
        self._tags = []
        self._paths = []
        self._arr = None
        self._notes = None
        self._counter = 0
        self._axes_cache = None

    @property
    def base(self):
        """
        Base directory for saving data files.

        Returns:
            str: Base directory.
        """
        return self._base

    @base.setter
    def base(self, value):
        """
        Set the base directory for saving data files.
        """
        if value != self._base:
            self._base = value
            self.pathChanged.emit()
            self.numberChanged.emit()

    @property
    def folder(self):
        """
        Data folder name under the base directory.

        Returns:
            str: Data folder name.
        """
        return self._folder

    @folder.setter
    def folder(self, value):
        """
        Set the data folder name under the base directory.
        """
        if value != self._folder:
            self._folder = value
            self.pathChanged.emit()
            self.numberChanged.emit()

    @property
    def name(self):
        """
        Base file name used when saving data files.

        Returns:
            str: File name.
        """
        return self._name

    @name.setter
    def name(self, value):
        """
        Set the base file name used for saved data files.
        """
        if value != self._name:
            self._name = value
            self.pathChanged.emit()
            self.numberChanged.emit()

    @property
    def numbered(self):
        """
        Whether automatic file numbering is enabled.

        Returns:
            bool: True if automatic file numbering is enabled, False otherwise.
        """
        return self._numbered

    @numbered.setter
    def numbered(self, value):
        """
        Set whether automatic file numbering is enabled.
        """
        if value != self._numbered:
            self._numbered = value
            self.pathChanged.emit()
            self.numberChanged.emit()

    @property
    def enabled(self):
        """
        Whether the data storage instance is enabled.

        Returns:
            bool: True if enabled, False otherwise.
        """
        return self._enabled

    @enabled.setter
    def enabled(self, value):
        """
        Set whether the data storage instance is enabled.
        """
        if value != self._enabled:
            self._enabled = value
            self.enabledChanged.emit(value)

    def pathErrors(self):
        """
        Check whether the current save path can be used.

        ``base`` must not be empty. ``folder`` may be empty (files are saved directly under ``base``) and may contain sub folders separated by '/' or '\\'.
        ``name`` must not be empty or contain path separators.
        Leading and trailing spaces, and characters or names that are invalid on Windows are not allowed.

        Returns:
            dict[str, str]: Mapping from invalid property names (``"base"``, ``"folder"``, ``"name"``) to the reasons. Empty if the path is valid.
        """
        errors = {}
        if self.base.strip() == "":
            errors["base"] = "must not be empty"
        elif self.base != self.base.strip():
            errors["base"] = "must not start or end with spaces"

        if self.folder != "":
            for part in self.folder.replace("\\", "/").split("/"):
                reason = _checkComponent(part)
                if reason is not None:
                    errors["folder"] = reason
                    break

        if "/" in self.name or "\\" in self.name:
            errors["name"] = "must not contain '/' or '\\'"
        else:
            reason = _checkComponent(self.name)
            if reason is not None:
                errors["name"] = reason
        return errors

    def getNumber(self):
        """
        Return the next available file number for saving.

        If automatic numbering is enabled the returned number will be appended to the base file name (for example: ``<name>_<number>.npz``).
        If numbering is disabled this method returns ``None``.

        Returns:
            int | None: Next available file number, or ``None`` if numbering is disabled.
        """
        if not self.numbered:
            return None

        reserved = set([thread.path for thread in self._threads] + self._paths)
        i = 0
        while True:
            candidate = os.path.join(self.base, self.folder, f"{self.name}_{i}.npz")
            if not os.path.exists(candidate) and candidate not in reserved:
                return i
            i += 1

    def connect(self, detector):
        """
        Connect this data storage instance to a detector.

        Args:
            detector (``MultiDetectorInterface``): Detector that emits ``dataAcquired`` and ``busyStateChanged`` signals.
        """
        detector.dataAcquired.connect(lambda data: self.update(data, detector=detector))
        detector.busyStateChanged.connect(lambda b: self._busyStateChanged(detector, b))
        detector.stopped.connect(lambda: self._stopped(detector))

    def _busyStateChanged(self, detector, busy):
        """
        Reserve storage when the detector becomes busy, except for continuous runs (streaming) that are not saved.

        Args:
            detector (``MultiDetectorInterface``): Detector that the data storage instance is connected to.
            busy (bool): True when acquisition starts.
        """
        if busy and detector.iteration >= 0:
            self.reserve(detector.dataShape)

        self._axes_cache = detector.axes

    def _stopped(self, detector):
        """
        Save any remaining buffered data when the detector stops.

        Args:
            detector (``MultiDetectorInterface``): Detector that the data storage instance is connected to.
        """
        self.save(detector.axes)

    def reserve(self, shape, fillValue=None):
        """
        Reserve storage for a new data array with the specified shape.

        Allocate and initialize an internal NumPy array with the given shape,
        record a file path and tag for the upcoming save, emit ``tagRequest`` to request metadata, 
        and update saving state via the ``savingStateChanged`` signal.

        Args:
            shape (tuple, optional): Shape of the data array to reserve.
            fillValue (float | None, optional): Value to initialize the array with. If ``None`` the array is initialized with NaNs. Defaults to ``None``.

        Returns:
            ``None``

        Raises:
            ValueError: If the save path is invalid (see ``pathErrors()``).
            OSError: If the data folder cannot be created.
            In both cases nothing is reserved and ``errorOccurred`` is emitted.
        """
        # Save the previous reservation if it was not completed, so that its path is not used for the new data.
        if self._arr is not None:
            self.save(self._axes_cache)

        self._counter = 0
        if not self.enabled:
            self.savingStateChanged.emit(self.saving)
            return

        errors = self.pathErrors()
        if errors:
            msg = "Invalid save path: " + ", ".join(f"{key} {reason}" for key, reason in errors.items())
            logging.error(msg)
            self.savingStateChanged.emit(self.saving)
            self.errorOccurred.emit(msg)
            raise ValueError(msg)

        tag = {"Notes": self._notes}
        self.tagRequest.emit(tag)

        number = self.getNumber()
        numberedName = f"{self.name}_{number}.npz" if number is not None else f"{self.name}.npz"
        folder = os.path.join(self.base, self.folder)
        path = os.path.join(folder, numberedName)

        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as e:
            msg = f"Failed to create data folder '{folder}': {e}"
            logging.error(msg)
            self.savingStateChanged.emit(self.saving)
            self.errorOccurred.emit(msg)
            raise

        self._tags.append(tag)
        self._paths.append(path)

        self._arr = np.full(shape, np.nan if fillValue is None else fillValue, dtype=float)
        self.savingStateChanged.emit(self.saving)
        self.numberChanged.emit()

    def update(self, data, detector=None):
        """
        Update the buffered data array with new values.

        Each entry in ``data`` maps an index tuple to a frame array; the buffer is updated in-place at those indices.
        Data is ignored if there is no reserved buffer, e.g. when storage was disabled at ``reserve()`` or the buffer has already been saved.

        Args:
            data (dict[tuple, np.ndarray]): Mapping from index tuples to frame arrays used to update the buffer.
            detector (``MultiDetectorInterface``): Detector instance to query for axes information.
        """
        for idx, value in data.items():
            if self._arr is None:
                return
            self._arr[idx] = value
            self._counter += 1

            if idx == () or self._counter >= np.prod(self._arr.shape[0:len(idx)]):
                axes = detector.axes if detector is not None else self._axes_cache
                self.save(axes)

    def save(self, axes):
        """
        Save the buffered data array asynchronously to disk.

        This method constructs a ``lys.Wave`` from the buffered array and provided coordinate arrays, attaches the queued metadata tag to ``lys.Wave.note``, 
        and enqueues the Wave for export on a background worker thread.
        Actual file write occurs in a ``_SaveThread``.

        Args:
            axes (Sequence[np.ndarray]): Coordinate arrays for each data axis used to construct the ``Wave``.
        """
        # Whether to save is decided at reserve(), so the enabled state is not checked here.
        if self._arr is None:
            return

        self._counter = 0

        dataToSave = self._arr
        self._arr = None

        path = self._paths.pop(0)
        note = self._tags.pop(0)

        thread = _SaveThread(dataToSave, axes, note, path)
        thread.finished.connect(self._savingFinished)
        self._threads.append(thread)
        thread.start()
        self.savingStateChanged.emit(self.saving)

    def _savingFinished(self):
        """
        Handle completion of a save thread.

        Remove the finished save thread from the internal thread list and emit ``savingStateChanged`` so listeners can update their state.
        """
        # finished is emitted just before the thread exits, so wait for it before releasing the reference.
        thread = self.sender()
        thread.wait()
        self._threads.remove(thread)
        self.savingStateChanged.emit(self.saving)
        self.numberChanged.emit()
        if thread.error is not None:
            msg = f"Failed to save '{thread.path}': {thread.error}"
            logging.error(msg)
            self.errorOccurred.emit(msg)

    @property
    def saving(self):
        """
        Whether a save operation is in progress.

        Returns:
            bool: True if a save operation is in progress, False otherwise.
        """
        return bool(self._threads or self._paths)


class _SaveThread(QtCore.QThread):
    """
    Background thread that writes a ``lys.Wave`` to disk.

    The thread calls ``lys.Wave.export`` on the provided ``Wave`` when started.
    It is used by ``DataStorage`` to perform non-blocking file writes so the main application thread remains responsive.
    """

    def __init__(self, data, axes, note, path):
        """
        Initialize the save thread.

        Args:
            data (np.ndarray): The data to write to disk.
            axes (Sequence): The axes that the data belongs to.
            note (str): The note to store with the data.
            path (str): Destination file path for the exported Wave.
        """
        super().__init__()
        self._data = data
        self._axes = axes
        self._note = note
        self._path = path
        self._error = None

    @property
    def error(self):
        """
        Exception raised while saving, or ``None`` if saving succeeded.

        Returns:
            Exception | None: The exception raised while saving.
        """
        return self._error

    @property
    def path(self):
        """
        Destination file path.

        Returns:
            str: Destination file path.
        """
        return self._path

    def run(self):
        """
        Run the save thread and export the Wave to disk.

        Calls ``lys.Wave.export`` on ``self.wave`` to write the Wave to ``self.path``.
        This runs in the worker thread so the main application thread is not blocked by file I/O.
        """
        try:
            wave = Wave(self._data, *(self._axes or []))
            wave.note = self._note
            wave.export(self._path)
        except Exception as e:
            self._error = e
