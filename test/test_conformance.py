import unittest
import time
import threading

from PyQt5 import QtWidgets
from lys_instr import dummy

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _status(report):
    return {r.name: r.status for r in report.results}


class TestDummyConformance(unittest.TestCase):
    """
    The dummy devices should satisfy the requirements.
    """

    def test_motor(self):
        motor = dummy.MultiMotorDummy("x", "y")
        try:
            report = motor.test("x", start=0, target=1, tolerance=1e-6, stopTarget=100)
            report.raiseIfFailed()
            self.assertEqual(_status(report)["stop"], "passed")
            self.assertAlmostEqual(motor.get()["x"], 0, msg="The motor should be left at start.")
        finally:
            motor.kill()

    def test_switch(self):
        switch = dummy.MultiSwitchDummy("sw")
        try:
            switch.test("sw", start="OFF", target="HIGH").raiseIfFailed()
            self.assertEqual(switch.get()["sw"], "OFF")
        finally:
            switch.kill()

    def test_detector(self):
        for kw in [dict(indexShape=(), frameShape=(4, 4)), dict(indexShape=(5,), frameShape=(3,)), dict(indexShape=(3, 2), frameShape=(2,))]:
            with self.subTest(**kw):
                detector = dummy.MultiDetectorDummy(exposure=0.01, **kw)
                try:
                    detector.test(iterations=3).raiseIfFailed()
                finally:
                    detector.kill()


class _LaggingMotor(dummy.MultiMotorDummy):
    """Reports busy only after a delay (violates the busy requirement)."""

    def _set(self, **target):
        self._lagUntil = time.perf_counter() + 0.3
        self._pending = target

    def _isBusy(self):
        pending = getattr(self, "_pending", None)
        if pending and time.perf_counter() >= self._lagUntil:
            super()._set(**pending)
            self._pending = None
        return super()._isBusy()


class _EarlyIdleMotor(dummy.MultiMotorDummy):
    """Reports idle before reaching the target."""

    def _isBusy(self):
        return {name: d.busy and abs(d._target - d.position) > 0.3 for name, d in self._data.items()}


class _UnsafeMotor(dummy.MultiMotorDummy):
    """Fails when it is accessed from several threads at the same time (violates thread safety)."""

    def __init__(self, *args, **kwargs):
        self._active = 0
        super().__init__(*args, **kwargs)

    def _get(self):
        self._active += 1
        try:
            if self._active > 1:
                raise RuntimeError("communication collision")
            time.sleep(0.001)
            return super()._get()
        finally:
            self._active -= 1


class _UnstoppableMotor(dummy.MultiMotorDummy):
    def _stop(self):
        pass


class _EarlyReturnDetector(dummy.MultiDetectorDummy):
    """_run() returns before the frames are stored."""

    def _run(self, iter=1):
        frames = list(self._obj)

        def store():
            for idx, data in frames:
                time.sleep(0.01)
                with self._lock:
                    self._data[idx] = data
        threading.Thread(target=store).start()


class _NonAtomicDetector(dummy.MultiDetectorDummy):
    """_get() is not atomic with respect to _run()."""

    def _get(self):
        data = self._data.copy()
        time.sleep(0.001)
        self._data.clear()
        return data


class TestViolations(unittest.TestCase):
    """
    The checks should detect violations of the requirements.
    """

    def _motor(self, cls, **kwargs):
        motor = cls("x", **kwargs)
        self.addCleanup(motor.kill)
        return motor

    def test_busy_after_set(self):
        report = self._motor(_LaggingMotor).test("x", start=0, target=1, tolerance=1e-6, timeout=5)
        self.assertEqual(_status(report)["busy right after set"], "failed", str(report))

    def test_early_idle(self):
        report = self._motor(_EarlyIdleMotor, speed=2).test("x", start=0, target=1, tolerance=1e-6, timeout=5)
        self.assertEqual(_status(report)["not idle before reaching target"], "failed", str(report))

    def test_thread_safety(self):
        report = self._motor(_UnsafeMotor).test("x", start=0, target=1, tolerance=1e-6, timeout=5)
        self.assertEqual(_status(report)["concurrent calls from threads"], "failed", str(report))

    def test_stop(self):
        report = self._motor(_UnstoppableMotor, speed=100).test("x", start=0, target=1, tolerance=1e-6, timeout=5, stopTarget=50, stopDelay=0.1)
        self.assertEqual(_status(report)["stop"], "failed", str(report))

    def test_stop_skipped(self):
        report = self._motor(dummy.MultiMotorDummy).test("x", start=0, target=1, tolerance=1e-6)
        self.assertEqual(_status(report)["stop"], "skipped")
        self.assertTrue(report.passed)

    def test_detector_early_return(self):
        detector = _EarlyReturnDetector(indexShape=(5,), frameShape=(2,), exposure=0.01)
        self.addCleanup(detector.kill)
        report = detector.test(timeout=5)
        self.assertEqual(_status(report)["all frames delivered before idle"], "failed", str(report))

    def test_detector_non_atomic_get(self):
        detector = _NonAtomicDetector(indexShape=(20,), frameShape=(2,), exposure=0.005)
        self.addCleanup(detector.kill)
        report = detector.test(iterations=5, timeout=10)
        self.assertEqual(_status(report)["concurrent _get() is atomic"], "failed", str(report))
