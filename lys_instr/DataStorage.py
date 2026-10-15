import os
import datetime
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

    #: Signal (bool) emitted when the overwrite option changes.
    overwriteChanged = QtCore.pyqtSignal(bool)

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
        self._overwrite = True
        self._threads = []
        self._reservations = {}   # key: connected detector, or None for reserve() called directly
        self._notes = None
        self._saveError = None

        # Wait for the files being written when the application quits.
        app = QtCore.QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._waitThreads)

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

    @property
    def overwrite(self):
        """
        Whether an existing file may be overwritten when automatic numbering is disabled.

        Returns:
            bool: True if existing files may be overwritten.
        """
        return self._overwrite

    @overwrite.setter
    def overwrite(self, value):
        """
        Set whether an existing file may be overwritten when automatic numbering is disabled.
        """
        if value != self._overwrite:
            self._overwrite = value
            self.overwriteChanged.emit(value)

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

    def _pathErrorMessage(self, errors):
        return "Invalid save path: " + ", ".join(f"{key} '{getattr(self, key)}' {reason}" for key, reason in errors.items())

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

        reserved = self._reservedPaths()
        i = 0
        while True:
            candidate = os.path.join(self.base, self.folder, f"{self.name}_{i}.npz")
            if not os.path.exists(candidate) and candidate not in reserved:
                return i
            i += 1

    def _reservedPaths(self):
        return {thread.path for thread in self._threads} | {r.path for r in self._reservations.values()}

    def _nextPath(self):
        """
        Return the path of the file to be saved next.
        """
        number = self.getNumber()
        numberedName = f"{self.name}_{number}.npz" if number is not None else f"{self.name}.npz"
        return os.path.join(self.base, self.folder, numberedName)

    def _overwriteError(self, path):
        """
        Return the error message if ``path`` must not be overwritten, otherwise ``None``.
        """
        if not self.numbered and not self.overwrite and (os.path.exists(path) or path in self._reservedPaths()):
            return f"File '{path}' already exists (overwrite is disabled)"
        return None

    def connect(self, detector):
        """
        Connect this data storage instance to a detector.

        Args:
            detector (``MultiDetectorInterface``): Detector that emits ``dataAcquired`` and ``busyStateChanged`` signals.
        """
        detector.dataAcquired.connect(lambda data: self.update(data, detector=detector))
        detector.busyStateChanged.connect(lambda b: self._busyStateChanged(detector, b))
        detector.stopped.connect(lambda: self._stopped(detector))
        detector.startRequested.connect(self._checkStart, QtCore.Qt.DirectConnection)

    def _checkStart(self, request):
        """
        Reject acquisition of the connected detector if the data cannot be saved.

        The acquisition is rejected if the save path is invalid, the data folder cannot be created or is not writable,
        the file already exists while overwrite is disabled, or the previous save failed.
        The previous save failure is reported only once, so the next acquisition can be started after the error is confirmed.

        Args:
            request (dict): Request emitted by ``startRequested``. Reasons are appended to ``request["errors"]``.
        """
        if request["iter"] < 0 or not self.enabled:
            return

        errors = self.pathErrors()
        if errors:
            msg = self._pathErrorMessage(errors)
        else:
            folder = os.path.join(self.base, self.folder)
            try:
                os.makedirs(folder, exist_ok=True)
                msg = None if os.access(folder, os.W_OK) else f"Data folder '{folder}' is not writable"
            except OSError as e:
                msg = f"Failed to create data folder '{folder}': {e}"
            if msg is None:
                msg = self._overwriteError(self._nextPath())
        if msg is None and self._saveError is not None:
            msg = f"Previous save failed. {self._saveError}"
        self._saveError = None

        if msg is not None:
            request["errors"].append(msg)
            self.errorOccurred.emit(msg)

    def _busyStateChanged(self, detector, busy):
        """
        Reserve storage when the detector becomes busy, except for continuous runs (streaming) that are not saved.

        Args:
            detector (``MultiDetectorInterface``): Detector that the data storage instance is connected to.
            busy (bool): True when acquisition starts.
        """
        if busy and detector.iteration >= 0:
            self._reserve(detector, detector.dataShape, repeat=detector.iteration, axes=detector.axes, exposure=detector.exposure)

    def _stopped(self, detector):
        """
        Save any remaining buffered data when the detector stops.

        Args:
            detector (``MultiDetectorInterface``): Detector that the data storage instance is connected to.
        """
        self._save(detector, detector.axes)

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
            ValueError: If the save path is invalid (see ``pathErrors()``) or the file must not be overwritten.
            OSError: If the data folder cannot be created.
            In both cases nothing is reserved and ``errorOccurred`` is emitted.
        """
        self._reserve(None, shape, fillValue)

    def _reserve(self, key, shape, fillValue=None, repeat=1, axes=None, exposure=None):
        """
        Reserve storage for ``key`` (a connected detector, or None for direct calls).

        Args:
            repeat (int): Number of repetitions. If larger than 1, the data of all repetitions are saved in a single file with an additional leading axis.
        """
        # Save the previous reservation if it was not completed, so that its path is not used for the new data.
        if key in self._reservations:
            previous = self._reservations[key]
            self._save(key, previous.axes)

        if not self.enabled:
            self.savingStateChanged.emit(self.saving)
            return

        errors = self.pathErrors()
        if errors:
            self._reserveFailed(ValueError(self._pathErrorMessage(errors)))

        path = self._nextPath()
        msg = self._overwriteError(path)
        if msg is not None:
            self._reserveFailed(ValueError(msg))

        folder = os.path.dirname(path)
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as e:
            self._reserveFailed(OSError(f"Failed to create data folder '{folder}': {e}"))

        tag = {"Notes": self._notes, "Time": datetime.datetime.now().isoformat(timespec="seconds"), "Exposure": exposure}
        self.tagRequest.emit(tag)

        self._reservations[key] = _Reservation(path, tag, shape, fillValue, max(repeat, 1), axes)
        self.savingStateChanged.emit(self.saving)
        self.numberChanged.emit()

    def _reserveFailed(self, error):
        logging.error(str(error))
        self.savingStateChanged.emit(self.saving)
        self.errorOccurred.emit(str(error))
        raise error

    def update(self, data, detector=None):
        """
        Update the buffered data array with new values.

        Each entry in ``data`` maps an index tuple to a frame array; the buffer is updated in-place at those indices.
        The buffer is saved when frames for all indices (of all repetitions) have been received.
        Data is ignored if there is no reserved buffer, e.g. when storage was disabled at ``reserve()`` or the buffer has already been saved.

        Args:
            data (dict[tuple, np.ndarray]): Mapping from index tuples to frame arrays used to update the buffer.
            detector (``MultiDetectorInterface``): Detector instance to query for axes information.
        """
        reservation = self._reservations.get(detector)
        if reservation is None:
            return
        if reservation.update(data):
            self._save(detector, detector.axes if detector is not None else reservation.axes)

    def save(self, axes):
        """
        Save the buffered data array asynchronously to disk.

        This method constructs a ``lys.Wave`` from the buffered array and provided coordinate arrays, attaches the queued metadata tag to ``lys.Wave.note``, 
        and enqueues the Wave for export on a background worker thread.
        Actual file write occurs in a ``_SaveThread``.

        Args:
            axes (Sequence[np.ndarray]): Coordinate arrays for each data axis used to construct the ``Wave``.
        """
        self._save(None, axes)

    def _save(self, key, axes):
        reservation = self._reservations.pop(key, None)
        if reservation is None:
            return

        # Stopped is True if the data was saved before all frames were received.
        note = reservation.tag
        note["Stopped"] = not reservation.completed
        if reservation.repeat > 1:
            axes = [np.arange(reservation.repeat), *axes] if axes else None

        thread = _SaveThread(reservation.data, axes, note, reservation.path)
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
            self._saveError = msg
            self.errorOccurred.emit(msg)

    def waitForSaved(self, timeout=None):
        """
        Block until all reserved data are saved, while processing Qt events.

        Reserved data is saved when all frames are received or the detector is stopped.
        Specify ``timeout`` if the acquisition may not finish, otherwise this method does not return.

        Args:
            timeout (float | None): Timeout in seconds. If ``None``, wait without timeout.

        Returns:
            bool: True if all data are saved, False if timed out.
        """
        if not self.saving:
            return True
        loop = QtCore.QEventLoop()

        def onSavingStateChanged(saving):
            if not saving:
                loop.quit()

        self.savingStateChanged.connect(onSavingStateChanged)
        if timeout is not None:
            QtCore.QTimer.singleShot(int(timeout * 1000), loop.quit)
        loop.exec_()
        self.savingStateChanged.disconnect(onSavingStateChanged)
        return not self.saving

    def _waitThreads(self):
        """
        Wait for the files being written. Called when the application quits.
        """
        for thread in list(self._threads):
            thread.wait()

    @property
    def saving(self):
        """
        Whether a save operation is in progress.

        Returns:
            bool: True if data is reserved or being written, False otherwise.
        """
        return bool(self._threads or self._reservations)

    @property
    def reservedCount(self):
        """
        Number of files reserved but not yet passed to save threads.

        Returns:
            int: Number of reserved files.
        """
        return len(self._reservations)

    @property
    def savingCount(self):
        """
        Number of files currently being written.

        Returns:
            int: Number of files being written.
        """
        return len(self._threads)


class _Reservation:
    """
    Buffer and destination of a single data file.

    Frames for each index are stored in ``data``. Received indices are recorded so that the reservation is completed when all frames have been received.
    If ``repeat`` is larger than 1, the data of each repetition is stored along an additional leading axis.
    """

    def __init__(self, path, tag, shape, fillValue=None, repeat=1, axes=None):
        self.path = path
        self.tag = tag
        self.repeat = repeat
        self.axes = axes
        shape = tuple(shape)
        fullShape = shape if repeat == 1 else (repeat, *shape)
        self.data = np.full(fullShape, np.nan if fillValue is None else fillValue, dtype=float)
        self._shape = shape
        self._received = None
        self._rep = 0

    @property
    def completed(self):
        """
        Whether frames for all indices of all repetitions have been received.
        """
        return self._rep >= self.repeat

    def update(self, data):
        """
        Store frames.

        Args:
            data (dict[tuple, np.ndarray]): Mapping from index tuples to frames.

        Returns:
            bool: True if the reservation is completed.
        """
        for idx, value in data.items():
            if self.completed:
                break
            if self._received is None:
                self._received = np.zeros(self._shape[:len(idx)], dtype=bool)
            target = self.data if self.repeat == 1 else self.data[self._rep]
            target[idx] = value
            self._received[idx] = True
            if self._received.all():
                self._rep += 1
                self._received[...] = False
        return self.completed


class _SaveThread(QtCore.QThread):
    """
    Background thread that writes a ``lys.Wave`` to disk.

    The data is first written to a temporary file in the same folder and then renamed, so that an incomplete file is never left with the destination name.
    """

    def __init__(self, data, axes, note, path):
        """
        Initialize the save thread.

        Args:
            data (np.ndarray): The data to write to disk.
            axes (Sequence): The axes that the data belongs to.
            note (dict): The note to store with the data.
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
        """
        folder, filename = os.path.split(self._path)
        tmp = os.path.join(folder, "." + os.path.splitext(filename)[0] + ".saving.npz")
        try:
            wave = Wave(self._data, *(self._axes or []))
            wave.note = self._note
            wave.export(tmp)
            os.replace(tmp, self._path)
        except Exception as e:
            self._error = e
            if os.path.exists(tmp):
                os.remove(tmp)
