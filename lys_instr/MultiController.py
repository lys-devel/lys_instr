import logging
import os
import weakref

import numpy as np
from .Interfaces import HardwareInterface, lock
from lys.Qt import QtCore


class _AxisInfo():
    """
    Per-axis state container.

    Holds the current busy/alive state and an optional target value for a single motor axis.
    """

    def __init__(self, busy=False, alive=True):
        """
        Initialize the axis state.

        Args:
            busy (bool, optional): Initial busy state. Defaults to False.
            alive (bool, optional): Initial alive state. Defaults to True.
        """
        self.busy = busy
        self.alive = alive
        self.target = None

class MultiControllerInterface(HardwareInterface):
    """
    Abstract interface for multi-axis controllers (e.g., motors, switches, or similar).

    This class provides background polling, state management, and Qt signals for axis value and state updates.
    Subclasses must implement device-specific methods: ``_get()``, ``_set()``, ``_isBusy()``, and ``_isAlive()``. 
    ``_get()``, ``_set()``, and ``_isBusy()`` should raise ``RuntimeError`` on communication errors; 
    ``_isAlive()`` should return the current alive state without raising ``RuntimeError`` that interrupts monitoring.

    Requirements for subclasses:

    - **Thread safety**: All methods (``_set()``, ``_get()``, ``_stop()``, ``_isBusy()``, ``_isAlive()`` and any other methods) may be called from any thread at any time, including concurrently.
      For example, ``_get()``, ``_isBusy()`` and ``_isAlive()`` are called by the background monitoring thread while ``set()``, ``get()`` and ``stop()`` are called by the GUI, scans or scripts.
      Subclasses must serialize the communication with the instrument (e.g., by a lock shared by all objects using the same connection),
      including sequences of commands that must not be interleaved.
    - **Busy state**: ``_isBusy()`` must report ``True`` for an axis as soon as ``_set()`` returns, until the motion is completed.
      If the instrument reports the motion only after a delay, subclasses must keep the busy state by themselves (e.g., set a flag in ``_set()`` and clear it when the instrument reports completion).
      If the value is applied before ``_set()`` returns, ``_isBusy()`` may always return ``False``.
      ``set(wait=True)``, ``waitForReady()`` and scans rely on this to wait for the motion.
    """

    #: Signal (dict) emitted when axis values change.
    valueChanged = QtCore.pyqtSignal(dict)

    #: Signal (dict) emitted when busy state changes.
    busyStateChanged = QtCore.pyqtSignal(dict)

    #: Signal (dict) emitted when alive state changes.
    aliveStateChanged = QtCore.pyqtSignal(dict)

    def __init__(self, *axisNamesAll, **kwargs):
        """
        Initialize the interface with the given axis names.

        Args:
            *axisNamesAll: Names of all axes to manage.
            **kwargs: Additional keyword arguments passed to the base class.
        """
        super().__init__(**kwargs)
        self._info = {name: _AxisInfo() for name in axisNamesAll}
        self._mutex = QtCore.QMutex()
        self._generation = 0   # incremented every time set() is called

    def _loadState(self):
        """
        Poll the device and update the state of all axes.

        Emit the ``busyStateChanged`` and ``aliveStateChanged`` signals if any axis state has changed.
        Log any runtime errors that occur during state loading.

        The device is accessed without holding the instance mutex, so that ``set()`` called from other threads (e.g., the GUI) is not blocked during the communication.
        The states are updated and the signals are emitted while holding the mutex.
        """
        with QtCore.QMutexLocker(self._mutex):
            generation = self._generation
            known = {name: info.busy for name, info in self._info.items()}

        try:
            bs = self._isBusy()
            changed = [name for name, b in bs.items() if b != known[name]]
            vs = self.get() if any(bs.values()) or len(changed) > 0 else None
        except RuntimeError as e:
            logging.warning(f"Runtime error in _loadState: {e}")
            bs = None
        al = self._isAlive()

        with QtCore.QMutexLocker(self._mutex):
            if bs is not None:
                # Emit valueChanged signal if any axis is busy
                if vs is not None:
                    self.valueChanged.emit({name: vs[name] for name, b in bs.items() if b or name in changed})

                # Update busy state log and emit busyStateChanged signal if any axis has changed its busy state.
                # The busy state is discarded if set() was called during the communication, because it may be older than the command.
                if generation == self._generation:
                    busyUpdate = {name: b for name, b in bs.items() if b != self._info[name].busy}
                    if busyUpdate:
                        for name, b in busyUpdate.items():
                            self._info[name].busy = b
                            if b is False:
                                self._info[name].target = None
                        self.busyStateChanged.emit(busyUpdate)

            # Update alive state if any axis has changed its alive state
            aliveUpdate = {name: a for name, a in al.items() if a != self._info[name].alive}
            if aliveUpdate:
                for name, a in aliveUpdate.items():
                    self._info[name].alive = a
                self.aliveStateChanged.emit(al)

    def set(self, wait=False, lock=True, **kwargs):
        """
        Set target values for one or more axes.

        For each axis specified in ``kwargs``, set its target value, e.g., ``set(x=1.0, y=2.0)``.
        Optionally wait until all axes become idle after setting.

        Args:
            wait (bool, optional): If True, block until all axes become idle after setting. Defaults to False.
            lock (bool, optional): If True, acquire the instance mutex while applying targets. Defaults to True.
            **kwargs: Axis-value pairs to set, e.g., x=1.0, y=2.0.

        Raises:
            ValueError: If any provided axis name is invalid.
        """
        if lock:
            with QtCore.QMutexLocker(self._mutex):
                self._set_impl(**kwargs)
        else:
            self._set_impl(**kwargs)

        if wait:
            self.waitForReady()

    def _set_impl(self, **kwargs):
        """
        Apply target values to axes.

        Validate provided axis names, update per-axis state (target and busy flags), emit any busy-state updates,
        and finally call the subclass-implemented ``_set`` method to perform the hardware operation.

        Args:
            **kwargs (float): Axis-value pairs to set (e.g., x=1.0, y=2.0).

        Raises:
            ValueError: If any provided axis name is invalid.
        """
        # Validate axis names
        invalid = [name for name in kwargs if name not in self._info]
        if invalid:
            raise ValueError(f"Axis name(s) {invalid} not recognized. Available axes: {self.nameList}")

        # Update busy state for each axis in kwargs and emit busy state only for axes that are now busy
        updated = {name: True for name in kwargs if not self._info[name].busy}
        for name in kwargs.keys():
            self._info[name].target = kwargs[name]
            self._info[name].busy = True
        if len(updated) > 0:
            self.busyStateChanged.emit(updated)

        # Set actual values for the axes in kwargs
        self._set(**kwargs)
        self._generation += 1

    def get(self, type=dict):
        """
        Get the current values of all axes in the specified data type.

        Args:
            type (type, optional): Output type (`dict`, `list`, or `np.ndarray`). Defaults to `dict`.

        Returns:
            dict or list or np.ndarray: Axis values in the requested format.

        Raises:
            TypeError: If an unsupported output type is requested.
        """
        valueDict = self._get()
        if type is dict:
            return valueDict
        elif type is list:
            return [valueDict[name] for name in self.nameList]
        elif type is np.ndarray:
            return np.array([valueDict[name] for name in self.nameList])
        else:
            raise TypeError("Unsupported type: {}".format(type))

    def stop(self):
        """
        Stop all axes.

        Call the instance-specific ``_stop()`` method to perform the actual stopping logic.
        """
        self._stop()

    def waitForReady(self):
        """
        Block further interaction until all axes are idle.

        Returns:
            None
        """
        loop = QtCore.QEventLoop()

        # The busy state updated by the monitoring thread is used, so that the device is not accessed every time.
        def on_busy_changed():
            if not any(info.busy for info in self._info.values()) and loop.isRunning():
                loop.quit()

        with QtCore.QMutexLocker(self._mutex):
            if any(self._isBusy().values()) is False:
                return
            # The busy state is updated by the monitoring thread, so do not wait if it is not running (e.g. killed).
            if not self.isRunning():
                logging.warning("The monitoring thread is not running. Cannot wait for the device to be ready.")
                return
            self.busyStateChanged.connect(on_busy_changed, QtCore.Qt.QueuedConnection)
            self.finished.connect(loop.quit, QtCore.Qt.QueuedConnection)
        try:
            loop.exec_()
        finally:
            self.busyStateChanged.disconnect(on_busy_changed)
            self.finished.disconnect(loop.quit)
        
    @property
    def isBusy(self):
        """
        Current busy state of all axes.

        Returns:
            dict[str, bool]: Mapping of axis names to their busy state.

        Raises:
            RuntimeError: If the device is not responding or a communication error occurs.
        """
        with QtCore.QMutexLocker(self._mutex):
            return self._isBusy()

    @property
    def isAlive(self):
        """
        Current alive state of all axes.

        Returns:
            dict[str, bool]: Mapping of axis names to their alive state.
        """
        return self._isAlive()

    @property
    def nameList(self):
        """
        List of all axis names.

        Returns:
            list[str]: Axis names in the order they were registered.
        """
        return list(self._info.keys())
    
    @property
    def target(self):
        """
        Current target values of all axes.

        Returns:
            dict[str, float]: Mapping of axis names to their target values.
        """
        return {name: info.target for name, info in self._info.items() if info.target is not None}

    def settingsWidget(self):
        """
        Return a device-specific settings dialog.

        Subclasses should override this to provide a QDialog for device settings.
        The base implementation returns ``None``.

        Returns:
            QDialog | None: Settings dialog, or ``None`` if not provided by the base class.
        """
        return None

    def _isBusy(self):
        """
        Should be implemented in subclasses to provide device-specific logic for determining busy state.

        The busy state must be ``True`` as soon as ``_set()`` returns, until the motion is completed (see the class documentation).
        This method may be called from any thread.

        Returns:
            dict[str, bool]: Mapping of axis names to their busy state.

        Raises:
            NotImplementedError: If the subclass does not implement this method.
        """
        raise NotImplementedError("Subclasses must implement this method.")

    def _isAlive(self):
        """
        Should be implemented in subclasses to provide device-specific logic for determining alive state.

        Returns:
            dict[str, bool]: Mapping of axis names to their alive state.

        Raises:
            NotImplementedError: If the subclass does not implement this method.
        """
        raise NotImplementedError("Subclasses must implement this method.")

    def _get(self):
        """
        Should be implemented in subclasses to provide device-specific logic for getting axis values.

        Returns:
            dict[str, float]: Mapping of axis names to their current values.

        Raises:
            NotImplementedError: If the subclass does not implement this method.
        """
        raise NotImplementedError("Subclasses must implement this method.")

    def _set(self, **kwargs):
        """
        Should be implemented in subclasses to provide device-specific logic for setting axis values.

        Args:
            kwargs (dict[str, float]): Axis-value pairs to set, e.g., x=1.0, y=2.0.

        Raises:
            NotImplementedError: If the subclass does not implement this method.
        """
        raise NotImplementedError("Subclasses must implement this method.")


class MultiSwitchInterface(MultiControllerInterface):
    """
    Interface for switch-type multi-axis controllers (referred to as switches for short).

    Convenience subclass for devices where each axis behaves like a switch with discrete label positions.
    """

    def __init__(self, labelNames, *axisNamesAll, **kwargs):
        """
        Initialize the interface with given label and axis names.

        Args:
            labelNames (Iterable[str]): Labels associated with the switch axes.
            *axisNamesAll: Axis names managed by the controller.
            **kwargs: Additional keyword arguments passed to the base class.
        """
        super().__init__(*axisNamesAll, **kwargs)
        self._labels = labelNames

    def test(self, axis, start, target, timeout=30, threads=4):
        """
        Check whether this switch implementation satisfies the requirements of lys_instr (e.g., thread safety and busy state).

        The switch is changed between ``start`` and ``target``. See ``lys_instr.conformance.checkSwitch`` for details.

        Args:
            axis (str): Name of the axis to be changed.
            start (str): Label where the checks start. The switch is left at this label.
            target (str): Label to change to.
            timeout (float): Timeout in seconds for each change.
            threads (int): Number of threads used to check concurrent calls.

        Returns:
            ConformanceReport: The results of the checks. Print it to see the details.
        """
        from .conformance import checkSwitch
        return checkSwitch(self, axis, start, target, timeout=timeout, threads=threads)

    @property
    def labelNames(self):
        """
        List of label names associated with the switch axes.

        Returns:
            list[str]: Label names in the order corresponding to axis registration.
        """
        return self._labels
    

class OffsettableMultiMotorInterface(MultiControllerInterface):
    """
    Interface for motor-type multi-axis controllers (referred to as motors for short) with offset functionality.

    Convenience subclass for devices where each axis exposes a continuous position value.
    This class adds a per-axis offsets dictionary that is applied to ``get()``/``set()`` operations and can be persisted to disk.
    """

    #: Signal emitted when any axis offset value changes.
    offsetChanged = QtCore.pyqtSignal()

    class offsetDict(dict):
        """
        Dictionary subclass that stores per-axis offsets and emits a signal when any value changes.
        """

        #: Signal emitted when any axis offset value changes.
        valueChanged = QtCore.pyqtSignal()

        def __init__(self, axesNames, parent):
            """
            Initialize the offsets dictionary for the supplied axis names.

            The dictionary is pre-populated with zeros for each axis and keeps a weak reference to the parent so that changes can notify the parent object.

            Args:
                axesNames (Iterable[str]): Axis names to initialize with zero.
                parent: Parent instance which owns this dict.
            """
            super().__init__({name: 0 for name in axesNames})
            self._parent = weakref.ref(parent)

        def __setitem__(self, key, value):
            """
            Set an offset value and notify the parent.

            Emit the parent's ``offsetChanged`` signal after updating the dictionary so listeners can react.

            Args:
                key (str): Axis name.
                value (float): Offset value for the axis.
            """
            super().__setitem__(key, value)
            self._parent().offsetChanged.emit()

    def __init__(self, *axesNames, autoSave=True, **kwargs):
        """
        Initialize offset support for the multi-axis interface.

        Create an internal per-axis offsets dictionary, optionally load persisted offsets, and wire change notifications for automatic updates.

        Args:
            *axesNames (Iterable[str]): Axis names managed by the controller.
            autoSave (bool): If True, load persisted offsets and save on changes. Defaults to True.
            **kwargs: Additional keyword arguments passed to the base class.
        """
        super().__init__(*axesNames, **kwargs)
        self._offsetDict = self.offsetDict(axesNames, self)
        if autoSave:
            self.load()
            self.offsetChanged.connect(self.save)
        self.offsetChanged.connect(lambda: self.valueChanged.emit(self.get()))

    def _valueChanged(self):
        """
        Notify listeners that current axis values changed (offsets applied).

        Emit the ``valueChanged`` signal with the current output of ``get()`` (offsets applied).
        """
        self.valueChanged.emit(self.get())

    def set(self, wait=False, lock=True, **kwargs):
        """
        Set target values for axes in user coordinates (stored offsets subtracted).

        Adjust each provided value by adding the corresponding per-axis offset before delegating to the parent implementation. 
        For example, calling ``set(x=1.0)`` will result in the value ``1.0 + self.offset.get('x', 0)`` being sent to the underlying controller.

        Args:
            wait (bool, optional): If True, block until all axes become idle after setting. Defaults to False.
            lock (bool, optional): If True, acquire the instance mutex while applying targets. Defaults to True.
            **kwargs (float): Axis-value pairs in user coordinates.

        Returns:
            None
        """
        kwargs = {key: value + self.offset.get(key, 0) for key, value in kwargs.items()}
        super().set(wait=wait, lock=lock, **kwargs)

    def get(self, type=dict):
        """
        Get current axis values in user coordinates (stored offsets subtracted).

        Args:
            type (type, optional): Output container type (dict, list, np.ndarray). Defaults to dict.

        Returns:
            dict | list | np.ndarray: Axis values in user coordinates (stored offsets subtracted).
        
        Raises:
            TypeError: If an unsupported output type is requested.
        """
        valueDict = {key: value - self.offset.get(key, 0) for key, value in super().get().items()}
        if type is dict:
            return valueDict
        elif type is list:
            return [valueDict[name] for name in self.nameList]
        elif type is np.ndarray:
            return np.array([valueDict[name] for name in self.nameList])
        else:
            raise TypeError("Unsupported type: {}".format(type))

    @property
    def offset(self):
        """
        Dictionary mapping axis names to respective offsets.
        """
        return self._offsetDict

    def save(self, path=".lys/lys_instr/motorOffsets"):
        """
        Persist per-axis offsets to disk.

        Args:
            path (str): Filesystem path to save offsets to. Defaults to ``.lys/lys_instr/motorOffsets``.
        """
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path):
            with open(path, "r") as file:
                txt = file.read()
            d = eval(txt)
        else:
            d = {}
        d.update(self.offset)
        with open(path, "w") as file:
            file.write(str(d))

    def load(self, path=".lys/lys_instr/motorOffsets"):
        """
        Load persisted per-axis offsets into memory.

        Args:
            path (str): Filesystem path to load offsets from. Defaults to ``.lys/lys_instr/motorOffsets``.
        """
        if not os.path.exists(path):
            return
        with open(path, "r") as file:
            txt = file.read()
            d = eval(txt)
            for key in self.offset.keys():
                self.offset[key] = d.get(key, 0)


class MultiMotorInterface(OffsettableMultiMotorInterface):
    """
    Multi-axis motor interface.

    Thin subclass of OffsettableMultiMotorInterface kept for semantic clarity and for future device-specific extensions.
    """

    def test(self, axis, start, target, tolerance, timeout=30, stopTarget=None, stopDelay=0.2, threads=4):
        """
        Check whether this motor implementation satisfies the requirements of lys_instr (e.g., thread safety and busy state).

        The motor is moved between ``start`` and ``target``. See ``lys_instr.conformance.checkMotor`` for details.

        Args:
            axis (str): Name of the axis to be moved.
            start (float): Position where the checks start. The motor is left at this position.
            target (float): Position to move to.
            tolerance (float): Allowed difference between the target and the position after the motion.
            timeout (float): Timeout in seconds for each motion.
            stopTarget (float | None): Position far from ``start`` used to check ``stop()``. If None, the stop check is skipped.
            stopDelay (float): Time in seconds after which ``stop()`` is called during the motion to ``stopTarget``.
            threads (int): Number of threads used to check concurrent calls.

        Returns:
            ConformanceReport: The results of the checks. Print it to see the details.
        """
        from .conformance import checkMotor
        return checkMotor(self, axis, start, target, tolerance, timeout=timeout, stopTarget=stopTarget, stopDelay=stopDelay, threads=threads)
