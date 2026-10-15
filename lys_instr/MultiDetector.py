import logging

from lys.Qt import QtCore
from .Interfaces import HardwareInterface


class AcquisitionRejectedError(RuntimeError):
    """
    Raised by ``DetectorInterface.startAcq()`` when a listener of ``startRequested`` rejects the acquisition.
    """


class _AcqThread(QtCore.QThread):
    """
    Acquisition thread for ``DetectorInterface``.

    Runs the detector's acquisition loop as a worker thread and emits signals when new data is acquired.
    """

    #: Signal (dict) emitted when new data is acquired.
    dataAcquired = QtCore.pyqtSignal(dict)

    def __init__(self, detector, iter=1):
        """
        Initialize the acquisition thread for a detector.

        Args:
            detector (DetectorInterface): The detector instance to run acquisition for.
            iter (int, optional): Number of acquisition iterations for this thread. Defaults to 1.
        """
        super().__init__()
        self._detector = detector
        self._iteration = iter

    def run(self, *args, **kwargs):
        """
        Run the detector's acquisition loop.

        Overrides the ``run()`` method of QThread and is called when the worker thread is started.
        """
        # Get the data in this thread (not in the main thread) only while the acquisition is running,
        # so that reading the data from the device does not block the GUI.
        self._detector.updated.connect(self._onUpdated, QtCore.Qt.DirectConnection)
        try:
            self._detector._run(self._iteration)
        finally:
            self._detector.updated.disconnect(self._onUpdated)
        self._onUpdated()

    def _onUpdated(self):
        """
        Emit the ``dataAcquired`` signal with the latest acquired data.

        Called in response to the detector's ``updated`` signal.
        """
        self.dataAcquired.emit(self._detector._get())


class DetectorInterface(HardwareInterface):
    """
    Abstract interface for detector devices.

    This class provides background polling, threaded (worker) acquisition, and Qt signal management for detector state and data updates.
    Subclasses must implement device-specific methods: ``_run()``, ``_get()``, ``_stop()``, and ``_isAlive()``.
    ``_get()`` and ``_stop()`` should raise ``RuntimeError`` if the device is not responding or a communication error occurs.
    ``_isAlive()`` should always return the current alive state and should not raise ``RuntimeError`` that interrupts monitoring.
    The ``updated`` signal is emitted by the acquisition thread when new data is available.

    Requirements for subclasses:

    - **Thread safety**: All methods (``_run()``, ``_get()``, ``_stop()``, ``_isAlive()``, properties and any other methods) may be called from any thread at any time, including concurrently.
      For example, ``_run()`` runs in the acquisition thread, ``_get()`` may be called from the acquisition thread and other threads while ``_run()`` is running,
      ``_stop()`` is called by the GUI, scans or scripts, and ``_isAlive()`` is called by the background monitoring thread.
      Subclasses must protect the shared state and the communication with the instrument by locks.
      In particular, reading and clearing the buffer of acquired frames in ``_get()`` must be atomic with respect to ``_run()`` adding frames.
    - **Busy state**: The detector is regarded as busy while ``_run()`` is running.
      ``_run()`` must not return until the acquisition is completed (all frames are acquired and stored so that ``_get()`` returns them), or until ``_stop()`` is called.
      After ``_stop()`` is called, ``_run()`` must return promptly.
    """

    #: Signal (bool) emitted when alive state changes.
    aliveStateChanged = QtCore.pyqtSignal(bool)

    #: Signal (bool) emitted when busy state changes.
    busyStateChanged = QtCore.pyqtSignal(bool)

    #: Signal (dict) emitted when data is acquired.
    #: It is emitted from the acquisition thread (or the thread calling ``stop()``).
    #: Receivers connected with the default (auto) connection are called in their own threads.
    dataAcquired = QtCore.pyqtSignal(dict)

    #: Signal emitted by the acquisition thread when new data is acquired.
    updated = QtCore.pyqtSignal()

    #: Signal emitted when acquisition is stopped.
    stopped = QtCore.pyqtSignal()

    #: Signal (dict) emitted before acquisition starts with a request ``{"iter": int, "errors": list[str]}``.
    #: Listeners can reject the acquisition by appending reasons to ``errors``.
    #: Connect with ``QtCore.Qt.DirectConnection`` so that the check is finished before acquisition starts.
    startRequested = QtCore.pyqtSignal(object)

    def __init__(self, exposure=1, **kwargs):
        """
        Initialize the interface.

        Args:
            exposure (float or None): Initial exposure time.
            **kwargs: Additional keyword arguments passed to the base class.
        """
        super().__init__(**kwargs)
        self._exposure = exposure
        self._mutex = QtCore.QMutex()
        self._busy = False
        self._iteration = 1
        self._thread = None

    def _loadState(self):
        """
        Poll the device and update its state.

        Emit the ``aliveStateChanged`` signal if the alive state has changed.
        """
        if not hasattr(self, "_alive"):
            self._alive = True

        al = self._isAlive()
        if self._alive != al:
            self._alive = al
            self.aliveStateChanged.emit(al)

    def startAcq(self, iter=1, wait=False, output=False):
        """
        Start acquisition in an acquisition thread.

        If both `wait` and `output` are True, the method blocks until acquisition completes and returns the acquired data.

        Args:
            iter (int): Number of iterations.
            wait (bool, optional): If True, blocks until acquisition is complete. Defaults to False.
            output (bool, optional): If True, returns acquired data as a dictionary. Defaults to False.

        Returns:
            dict[tuple, np.ndarray] | None: Acquired data that maps index tuples to frames when ``output`` is True; otherwise ``None``.

        Raises:
            AcquisitionRejectedError: If a listener of ``startRequested`` rejects the acquisition (e.g. invalid save path).
        """
        if self._busy:
            logging.warning("Detector is busy. Cannot start new acquisition.")
            return

        request = {"iter": iter, "errors": []}
        self.startRequested.emit(request)
        if request["errors"]:
            msg = "Acquisition was rejected: " + "; ".join(request["errors"])
            logging.error(msg)
            raise AcquisitionRejectedError(msg)

        self._busy = True
        self._iteration = iter
        self.busyStateChanged.emit(True)

        # The previous thread is kept until here so that it is not destroyed while finishing.
        if self._thread is not None and self._thread.isRunning():
            self._thread.wait()
        self._thread = _AcqThread(self, iter=iter)
        self._thread.dataAcquired.connect(self.dataAcquired.emit, type=QtCore.Qt.DirectConnection)
        self._thread.finished.connect(self._onAcqFinished, type=QtCore.Qt.DirectConnection)
        if wait and output:
            buffer = {}
            self._thread.dataAcquired.connect(buffer.update, type=QtCore.Qt.DirectConnection)

        thread = self._thread
        self._thread.start()

        if wait:
            self.waitForReady()
            if output:
                thread.dataAcquired.disconnect(buffer.update)
                return buffer

    def _onAcqFinished(self):
        """
        Clean up after acquisition is finished.

        Update the busy state and emit the ``busyStateChanged`` signal.
        The reference to the thread is kept, because this method is called in the thread itself before it finishes.
        """
        with QtCore.QMutexLocker(self._mutex):
            self._busy = False
            self.busyStateChanged.emit(False)

    def waitForReady(self):
        """
        Block further interaction until the device is no longer busy.

        Returns:
            None
        """
        loop = QtCore.QEventLoop()

        def on_busy_changed(b):
            if not b and loop.isRunning():
                loop.quit()

        with QtCore.QMutexLocker(self._mutex):
            if self._busy is False:
                return
            self.busyStateChanged.connect(on_busy_changed, QtCore.Qt.QueuedConnection)
        try:
            loop.exec_()
        finally:
            self.busyStateChanged.disconnect(on_busy_changed)

    def stop(self):
        """
        Stop the acquisition and emit the latest acquired data.

        This method waits for the acquisition worker thread to finish if it is running.
        Qt events are processed while waiting, so that the GUI is not frozen.
        """
        if not self._busy:
            return
        
        self._stop()

        thread = self._thread
        if thread is not None and thread.isRunning() and QtCore.QThread.currentThread() is not thread:
            self.waitForReady()
        self.dataAcquired.emit(self._get())
        # The data emitted by the acquisition thread is delivered through the event queue.
        # Emit stopped through the queue too, so that the listeners (e.g. DataStorage) receive all data before it.
        QtCore.QMetaObject.invokeMethod(self, "_emitStopped", QtCore.Qt.QueuedConnection)

    @QtCore.pyqtSlot()
    def _emitStopped(self):
        self.stopped.emit()

    @property
    def exposure(self):
        """
        Exposure time.

        Returns:
            float | None: The exposure time.
        """
        return self._exposure

    @exposure.setter
    def exposure(self, value):
        """
        Set the exposure time.

        Args:
            value (float | None): Exposure time to set, or ``None`` to indicate unsupported.
        """
        self._exposure = value

    @property
    def isBusy(self):
        """
        Current busy state of the detector.

        This property reflects the internal busy flag, which is True during acquisition.

        Returns:
            bool: True if the detector is busy, False otherwise.
        """
        return self._busy

    @property
    def iteration(self):
        """
        Number of iterations of the current (or last) acquisition.

        Returns:
            int: Number of iterations. -1 means continuous run (streaming).
        """
        return self._iteration

    @property
    def isAlive(self):
        """
        Current alive state of the detector.

        This property should be implemented in subclasses to provide device-specific logic.

        Returns:
            bool: True if the detector is alive, False otherwise.
        """
        return self._isAlive()

    def _get(self):
        """
        Should be implemented in subclasses to provide device-specific logic for getting acquired data.

        Return the frames acquired since the previous call and remove them from the buffer.
        This method may be called from any thread while ``_run()`` is running, so it must be atomic with respect to ``_run()`` adding frames.

        Returns:
            dict[tuple, np.ndarray]: Mapping of index tuples to their data frames.

        Raises:
            NotImplementedError: If the subclass does not implement this method.
        """
        raise NotImplementedError("Subclasses must implement this method.")

    def _stop(self):
        """
        Should be implemented in subclasses to provide device-specific logic for stopping acquisition.

        This method may be called from any thread. ``_run()`` must return promptly after this method is called.

        Raises:
            NotImplementedError: If the subclass does not implement this method.
        """
        raise NotImplementedError("Subclasses must implement this method.")

    def _isAlive(self):
        """
        Should be implemented in subclasses to provide device-specific logic for returning alive state.

        Raises:
            NotImplementedError: If the subclass does not implement this method.
        """
        raise NotImplementedError("Subclasses must implement this method.")

    def _run(self, iter=1):
        """
        Should be implemented in subclasses to provide device-specific logic for running the acquisition.

        This method runs in the acquisition thread and must not return until the acquisition is completed or ``_stop()`` is called.
        Emit ``updated`` when new frames are stored.

        Args:
            iter(int): Number of iterations. -1 means continuous run.

        Raises:
            NotImplementedError: If the subclass does not implement this method.
        """
        raise NotImplementedError("Subclasses must implement this method.")

    def settingsWidget(self):
        """
        Return a device-specific settings dialog.

        Subclasses should override this to provide a QDialog for device settings.

        Raises:
            NotImplementedError: If the subclass does not implement this method.
        """
        raise NotImplementedError("Subclasses must implement this method.")


class MultiDetectorInterface(DetectorInterface):
    """
    Abstract interface for multi-dimensional detector devices.

    This class extends ``DetectorInterface`` to support detectors that acquire data with multi-dimensional indices.
    Subclasses should implement device-specific acquisition and property reporting logic.
    """

    def test(self, exposure=None, iterations=2, timeout=60, stopDelay=0.5, threads=4):
        """
        Check whether this detector implementation satisfies the requirements of lys_instr (e.g., thread safety and busy state).

        The detector acquires data several times. Do not connect it to a DataStorage while checking.
        See ``lys_instr.conformance.checkDetector`` for details.

        Args:
            exposure (float | None): Exposure time used for the checks. If None, the current exposure is used.
            iterations (int): Number of iterations used to check repeated acquisition.
            timeout (float): Timeout in seconds for each acquisition.
            stopDelay (float): Time in seconds after which ``stop()`` is called during continuous acquisition.
            threads (int): Number of threads used to check concurrent calls.

        Returns:
            ConformanceReport: The results of the checks. Print it to see the details.
        """
        from .conformance import checkDetector
        return checkDetector(self, exposure=exposure, iterations=iterations, timeout=timeout, stopDelay=stopDelay, threads=threads)

    @property
    def axes(self):
        """
        Axis coordinates for the full data.

        Returns:
            list[numpy.ndarray]: Coordinate arrays corresponding to each axis of the index grid.

        Raises:
            NotImplementedError: If the subclass does not implement this property.
        """
        raise NotImplementedError("Subclasses must implement this property.")

    @property
    def frameDim(self):
        """
        Number of dimensions for a single frame of data.

        Returns:
            int: Number of dimensions for a single frame.
        """
        return len(self.frameShape)

    @property
    def indexDim(self):
        """
        Dimension of the index grid.

        Returns:
            int: Dimension of the index grid.
        """
        return len(self.indexShape)

    @property
    def frameShape(self):
        """
        Shape of a single frame.

        Returns:
            tuple[int, ...]: Dimensions of a single frame.

        Raises:
            NotImplementedError: If the subclass does not implement this property.
        """
        raise NotImplementedError("Subclasses must implement this property.")

    @property
    def indexShape(self):
        """
        Shape of the index tuples.

        Returns:
            tuple[int, ...]: Shape of the index tuples.

        Raises:
            NotImplementedError: If the subclass does not implement this property.
        """
        raise NotImplementedError("Subclasses must implement this property.")

    @property
    def dataShape(self):
        """
        Shape of the full data.

        Returns:
            tuple[int, ...]: Combined shape of the full dataset.
        """
        return tuple([*self.indexShape, *self.frameShape])
