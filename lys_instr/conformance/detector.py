import time
import itertools
import threading

import numpy as np

from ._common import ConformanceReport, SkipCheck, check, processEvents, waitUntil, Workers


def checkDetector(detector, exposure=None, iterations=2, timeout=60, stopDelay=0.5, threads=4):
    """
    Check whether a detector implementation satisfies the requirements of lys_instr.

    The detector acquires data several times. Do not connect it to a DataStorage while checking.

    Args:
        detector (MultiDetectorInterface): The detector to be checked.
        exposure (float | None): Exposure time used for the checks. If None, the current exposure is used.
        iterations (int): Number of iterations used to check repeated acquisition (``startAcq(iter=iterations)``).
        timeout (float): Timeout in seconds for each acquisition.
        stopDelay (float): Time in seconds after which ``stop()`` is called during continuous acquisition.
        threads (int): Number of threads used to check concurrent calls.

    Returns:
        ConformanceReport: The results of the checks.
    """
    checker = _DetectorChecker(detector, exposure, iterations, timeout, stopDelay, threads)
    report = ConformanceReport(f"Detector ({type(detector).__name__})")
    report.run("interface", checker.checkInterface)
    report.run("alive", checker.checkAlive)
    report.run("exposure", checker.checkExposure)
    report.run("acquire", checker.checkAcquire)
    report.run("all frames delivered before idle", checker.checkDeliveredBeforeIdle)
    report.run("repeated acquisition", checker.checkRepeat)
    report.run("concurrent _get() is atomic", checker.checkConcurrentGet)
    report.run("concurrent calls from threads", checker.checkConcurrentCalls)
    report.run("stop", checker.checkStop)
    report.run("stop while idle", checker.checkStopIdle)
    report.run("acquire from another thread", checker.checkAcquireFromThread)
    return report


class _DetectorChecker:
    def __init__(self, detector, exposure, iterations, timeout, stopDelay, threads):
        self._det = detector
        self._exposure = exposure
        self._iterations = iterations
        self._timeout = timeout
        self._stopDelay = stopDelay
        self._threads = threads

    @property
    def _indices(self):
        return set(itertools.product(*[range(n) for n in self._det.indexShape]))

    def _waitIdle(self):
        check(waitUntil(lambda: not self._det.isBusy, self._timeout), f"Acquisition did not finish within {self._timeout} s.")
        processEvents(0.05)

    def _checkFrames(self, data, name):
        check(isinstance(data, dict), f"{name} should be a dict: {type(data)}")
        for idx, frame in data.items():
            check(isinstance(idx, tuple) and idx in self._indices, f"{name} contains an invalid index {idx!r} for indexShape {self._det.indexShape}.")
            check(np.shape(frame) == tuple(self._det.frameShape), f"Frame {idx!r} has shape {np.shape(frame)}, but frameShape is {self._det.frameShape}.")
            check(np.issubdtype(np.asarray(frame).dtype, np.number), f"Frame {idx!r} is not numeric: {np.asarray(frame).dtype}")

    def checkInterface(self):
        d = self._det
        for name in ["frameShape", "indexShape"]:
            shape = getattr(d, name)
            check(isinstance(shape, tuple) and all(isinstance(n, (int, np.integer)) and n > 0 for n in shape), f"{name} should be a tuple of positive int: {shape!r}")
        check(tuple(d.dataShape) == tuple(d.indexShape) + tuple(d.frameShape), f"dataShape {d.dataShape} should be indexShape + frameShape.")
        axes = d.axes
        check(len(axes) == len(d.dataShape), f"axes should have {len(d.dataShape)} elements: {len(axes)}")
        for i, (axis, n) in enumerate(zip(axes, d.dataShape)):
            check(len(axis) == n, f"axes[{i}] has {len(axis)} elements, but dataShape[{i}] is {n}.")
        check(not d.isBusy, "The detector is busy before the checks.")

    def checkAlive(self):
        alive = self._det._isAlive()
        check(isinstance(alive, bool), f"_isAlive() should return bool: {alive!r}")
        check(alive, "The detector is not alive. The other checks may fail.")

    def checkExposure(self):
        if self._det.exposure is None:
            raise SkipCheck("The detector does not support exposure.")
        if self._exposure is None:
            return f"current exposure {self._det.exposure} is used"
        self._det.exposure = self._exposure
        check(self._det.exposure == self._exposure, f"exposure is {self._det.exposure} after setting {self._exposure}.")

    def checkAcquire(self):
        t = time.perf_counter()
        data = self._det.startAcq(wait=True, output=True)
        elapsed = time.perf_counter() - t
        check(elapsed <= self._timeout, f"startAcq(wait=True) took {elapsed:.2f} s.")
        check(not self._det.isBusy, "The detector is busy after startAcq(wait=True) returned.")
        self._checkFrames(data, "Acquired data")
        check(set(data.keys()) == self._indices, f"Acquired data has {len(data)} frames, but {len(self._indices)} frames are expected.")
        return f"{elapsed:.2f} s"

    def _collect(self, iter):
        delivered = []
        atIdle = []

        def dataAcquired(data):
            delivered.append(data)

        def busyChanged(b):
            if not b:
                atIdle.append(sum(len(d) for d in delivered))
        self._det.dataAcquired.connect(dataAcquired)
        self._det.busyStateChanged.connect(busyChanged)
        try:
            self._det.startAcq(iter=iter)
            check(self._det.isBusy, "The detector is not busy right after startAcq().")
            self._waitIdle()
        finally:
            self._det.dataAcquired.disconnect(dataAcquired)
            self._det.busyStateChanged.disconnect(busyChanged)
        return delivered, atIdle

    def checkDeliveredBeforeIdle(self):
        delivered, atIdle = self._collect(1)
        for d in delivered:
            self._checkFrames(d, "Delivered data")
        n = len(self._indices)
        check(len(atIdle) == 1 and atIdle[0] == n,
              f"{atIdle[0] if atIdle else None} of {n} frames were delivered when the detector became idle. "
              "_run() must not return until all frames are acquired and stored.")

    def checkRepeat(self):
        delivered, _ = self._collect(self._iterations)
        n = len(self._indices) * self._iterations
        total = sum(len(d) for d in delivered)
        check(total == n, f"{total} frames were delivered for startAcq(iter={self._iterations}), but {n} frames are expected. "
                          "Each frame should be delivered (emit updated) before the next frame of the same index is stored.")

    def checkConcurrentGet(self):
        taken = []
        lock = threading.Lock()

        def get():
            d = self._det._get()
            with lock:
                taken.append(d)
        with Workers([get], self._threads):
            delivered, _ = self._collect(self._iterations)
        n = len(self._indices) * self._iterations
        total = sum(len(d) for d in delivered) + sum(len(d) for d in taken)
        check(total == n, f"{total} frames were obtained in total by _get() from {self._threads} threads and the acquisition, but {n} frames are expected. "
                          "Reading and clearing the buffer in _get() must be atomic with respect to _run().")
        return f"{sum(len(d) for d in taken)} frames taken by other threads"

    def checkConcurrentCalls(self):
        d = self._det

        def calls():
            d._isAlive()
            d.frameShape
            d.indexShape
            d.axes
            d.exposure
            d.isBusy
        with Workers([calls], self._threads) as workers:
            self._collect(self._iterations)
        check(len(workers.errors) == 0, f"Concurrent calls raised exceptions: {workers.errors[:5]}")
        return f"{workers.calls} calls"

    def checkStop(self):
        stopped = []
        self._det.stopped.connect(lambda: stopped.append(True))
        self._det.startAcq(iter=-1)
        processEvents(self._stopDelay)
        check(self._det.isBusy, f"Continuous acquisition finished within stopDelay ({self._stopDelay} s).")
        t = time.perf_counter()
        self._det.stop()
        elapsed = time.perf_counter() - t
        check(not self._det.isBusy, "The detector is busy after stop() returned.")
        check(elapsed <= self._timeout, f"stop() took {elapsed:.2f} s.")
        check(waitUntil(lambda: len(stopped) > 0, 5), "The stopped signal was not emitted.")
        return f"stop() took {elapsed:.2f} s"

    def checkStopIdle(self):
        self._det.stop()
        processEvents(0.05)
        check(not self._det.isBusy, "The detector is busy after stop() while idle.")

    def checkAcquireFromThread(self):
        result = {}

        def acquire():
            try:
                result["data"] = self._det.startAcq(wait=True, output=True)
            except Exception as e:
                result["error"] = f"{type(e).__name__}: {e}"
        thread = threading.Thread(target=acquire)
        thread.start()
        check(waitUntil(lambda: not thread.is_alive(), self._timeout), f"startAcq(wait=True) from another thread did not return within {self._timeout} s.")
        check("error" not in result, f"startAcq() from another thread raised: {result.get('error')}")
        self._checkFrames(result["data"], "Acquired data")
        check(set(result["data"].keys()) == self._indices, f"Acquired data has {len(result['data'])} frames, but {len(self._indices)} frames are expected.")
        self._waitIdle()
