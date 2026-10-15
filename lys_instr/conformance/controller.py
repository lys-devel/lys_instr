import time
import threading

from lys.Qt import QtCore
from ._common import ConformanceReport, SkipCheck, check, processEvents, waitUntil, Workers


def checkMotor(motor, axis, start, target, tolerance, timeout=30, stopTarget=None, stopDelay=0.2, threads=4):
    """
    Check whether a motor implementation satisfies the requirements of lys_instr.

    The motor is moved between ``start`` and ``target``, and is left at ``start`` after the checks.

    Args:
        motor (MultiMotorInterface): The motor to be checked.
        axis (str): Name of the axis to be moved.
        start (float): Position where the checks start.
        target (float): Position to move to. It should be far enough from ``start`` so that the motion takes some time (if the motion is not instant).
        tolerance (float): Allowed difference between the target and the position after the motion.
        timeout (float): Timeout in seconds for each motion.
        stopTarget (float | None): Position far from ``start`` used to check ``stop()``. The motion should take longer than ``stopDelay``. If None, the stop check is skipped.
        stopDelay (float): Time in seconds after which ``stop()`` is called during the motion to ``stopTarget``.
        threads (int): Number of threads used to check concurrent calls.

    Returns:
        ConformanceReport: The results of the checks.
    """
    low, high = min(start, target), max(start, target)
    checker = _ControllerChecker(
        motor, axis, start, target, timeout, threads,
        equal=lambda a, b: abs(a - b) <= tolerance,
        valid=lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
        between=lambda v: low - tolerance <= v <= high + tolerance)
    report = ConformanceReport(f"Motor '{axis}' ({type(motor).__name__})")
    checker.run(report)
    report.run("stop", checker.checkStop, stopTarget, stopDelay, lambda a, b: abs(a - b) <= tolerance)
    checker.restore(report)
    return report


def checkSwitch(switch, axis, start, target, timeout=30, threads=4):
    """
    Check whether a switch implementation satisfies the requirements of lys_instr.

    The switch is changed between ``start`` and ``target``, and is left at ``start`` after the checks.

    Args:
        switch (MultiSwitchInterface): The switch to be checked.
        axis (str): Name of the axis to be changed.
        start (str): Label where the checks start.
        target (str): Label to change to (different from ``start``).
        timeout (float): Timeout in seconds for each change.
        threads (int): Number of threads used to check concurrent calls.

    Returns:
        ConformanceReport: The results of the checks.
    """
    checker = _ControllerChecker(
        switch, axis, start, target, timeout, threads,
        equal=lambda a, b: a == b,
        valid=lambda v: v in switch.labelNames,
        between=lambda v: v in (start, target))
    report = ConformanceReport(f"Switch '{axis}' ({type(switch).__name__})")
    report.run("labels", checker.checkLabels)
    checker.run(report)
    checker.restore(report)
    return report


class _ControllerChecker:
    def __init__(self, obj, axis, start, target, timeout, threads, equal, valid, between):
        self._obj = obj
        self._axis = axis
        self._start = start
        self._target = target
        self._timeout = timeout
        self._threads = threads
        self._equal = equal
        self._valid = valid
        self._between = between

    def run(self, report):
        report.run("interface", self.checkInterface)
        report.run("alive", self.checkAlive)
        report.run("move and wait", self.checkMoveAndWait)
        report.run("busy right after set", self.checkBusyAfterSet)
        report.run("not idle before reaching target", self.checkIdleAfterReached)
        report.run("busy state signals", self.checkSignals)
        report.run("set from another thread", self.checkSetFromThread)
        report.run("concurrent calls from threads", self.checkConcurrentCalls)

    def restore(self, report):
        report.run("restore start position", self._moveTo, self._start)

    def _value(self):
        return self._obj.get()[self._axis]

    def _moveTo(self, value):
        self._obj.set(**{self._axis: value})
        check(waitUntil(lambda: not self._obj._isBusy()[self._axis], self._timeout), f"Motion to {value!r} did not finish within {self._timeout} s.")
        check(self._equal(self._value(), value), f"Position {self._value()!r} after the motion differs from {value!r}.")

    def checkLabels(self):
        labels = self._obj.labelNames
        check(len(labels) > 0 and all(isinstance(label, str) for label in labels), f"labelNames should be a non-empty list of str: {labels!r}")
        check(self._start in labels and self._target in labels and self._start != self._target, f"start and target should be different labels in {labels!r}.")

    def checkInterface(self):
        names = self._obj.nameList
        check(self._axis in names, f"Axis '{self._axis}' is not in nameList {names}.")
        for method in ["_get", "_isBusy", "_isAlive"]:
            res = getattr(self._obj, method)()
            check(isinstance(res, dict) and set(res.keys()) == set(names), f"{method}() should return a dict for all axes {names}: {res!r}")
            if method == "_get":
                check(all(self._valid(v) for v in res.values()), f"_get() returned invalid values: {res!r}")
            else:
                check(all(isinstance(v, bool) for v in res.values()), f"{method}() should return bool values: {res!r}")

    def checkAlive(self):
        check(self._obj._isAlive()[self._axis] is True, f"Axis '{self._axis}' is not alive. The other checks may fail.")

    def checkMoveAndWait(self):
        t = time.perf_counter()
        self._obj.set(**{self._axis: self._start}, wait=True)
        elapsed = time.perf_counter() - t
        check(elapsed <= self._timeout, f"set(wait=True) took {elapsed:.2f} s.")
        check(not self._obj._isBusy()[self._axis], "_isBusy() is True after set(wait=True) returned.")
        check(self._equal(self._value(), self._start), f"Position {self._value()!r} differs from {self._start!r} after set(wait=True).")

    def checkBusyAfterSet(self):
        self._obj.set(**{self._axis: self._target})
        busy = self._obj._isBusy()[self._axis]
        value = self._value()
        try:
            check(busy or self._equal(value, self._target),
                  f"_isBusy() is False right after set() returned, but the position {value!r} has not reached {self._target!r}. "
                  "_isBusy() must be True as soon as _set() returns, until the motion is completed.")
        finally:
            waitUntil(lambda: not self._obj._isBusy()[self._axis], self._timeout)
        return "busy" if busy else "applied before set() returned"

    def checkIdleAfterReached(self):
        self._obj.set(**{self._axis: self._start}, wait=True)
        self._obj.set(**{self._axis: self._target})
        end = time.perf_counter() + self._timeout
        samples = 0
        while time.perf_counter() < end:
            busy = self._obj._isBusy()[self._axis]
            value = self._value()
            samples += 1
            if not busy:
                check(self._equal(value, self._target), f"_isBusy() became False at {value!r} before reaching {self._target!r}.")
                return f"{samples} samples"
            check(self._between(value), f"Position {value!r} is out of the range between {self._start!r} and {self._target!r} during the motion.")
            processEvents(0.01)
        raise AssertionError(f"Motion did not finish within {self._timeout} s.")

    def checkSignals(self):
        self._obj.set(**{self._axis: self._target}, wait=True)
        # Wait until the monitoring thread reports the end of the previous motion.
        waitUntil(lambda: not self._obj._info[self._axis].busy, self._timeout)
        states = []

        def busyChanged(b):
            if self._axis in b:
                states.append(b[self._axis])
        self._obj.busyStateChanged.connect(busyChanged)
        try:
            self._obj.set(**{self._axis: self._start}, wait=True)
            check(waitUntil(lambda: len(states) > 0 and states[-1] is False, 5), f"busyStateChanged did not report the end of the motion: {states}")
        finally:
            self._obj.busyStateChanged.disconnect(busyChanged)
        check(states[0] is True, f"busyStateChanged should report True first: {states}")

    def checkSetFromThread(self):
        errors = []

        def move():
            try:
                self._obj.set(**{self._axis: self._target})
            except Exception as e:
                errors.append(f"{type(e).__name__}: {e}")
        thread = threading.Thread(target=move)
        thread.start()
        thread.join(self._timeout)
        check(len(errors) == 0, f"set() from another thread raised: {errors}")
        check(waitUntil(lambda: not self._obj._isBusy()[self._axis], self._timeout), "Motion started from another thread did not finish.")
        check(self._equal(self._value(), self._target), f"Position {self._value()!r} differs from {self._target!r}.")

    def checkConcurrentCalls(self):
        invalid = []
        names = set(self._obj.nameList)

        def get():
            v = self._obj.get()
            if set(v.keys()) != names or not self._valid(v[self._axis]) or not self._between(v[self._axis]):
                invalid.append(f"get() = {v!r}")

        def isBusy():
            v = self._obj._isBusy()
            if set(v.keys()) != names or not all(isinstance(b, bool) for b in v.values()):
                invalid.append(f"_isBusy() = {v!r}")

        def isAlive():
            v = self._obj._isAlive()
            if set(v.keys()) != names or not v[self._axis]:
                invalid.append(f"_isAlive() = {v!r}")

        with Workers([get, isBusy, isAlive], self._threads) as workers:
            for value in [self._target, self._start, self._target, self._start]:
                self._moveTo(value)
        check(len(workers.errors) == 0, f"Concurrent calls raised exceptions: {workers.errors[:5]}")
        check(len(invalid) == 0, f"Concurrent calls returned invalid values: {invalid[:5]}")
        return f"{workers.calls} calls"

    def checkStop(self, stopTarget, stopDelay, equal):
        if stopTarget is None:
            raise SkipCheck("stopTarget is not given.")
        self._obj.set(**{self._axis: self._start}, wait=True)
        self._obj.set(**{self._axis: stopTarget})
        processEvents(stopDelay)
        check(self._obj._isBusy()[self._axis], f"Motion to {stopTarget!r} finished within stopDelay. Use a farther stopTarget or a shorter stopDelay.")
        self._obj.stop()
        check(waitUntil(lambda: not self._obj._isBusy()[self._axis], self._timeout), f"Motion did not stop within {self._timeout} s after stop().")
        value = self._value()
        check(not equal(value, stopTarget), f"Position {value!r} reached {stopTarget!r} although stop() was called.")
        return f"stopped at {value!r}"
