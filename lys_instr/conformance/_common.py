import time
import threading
import traceback

from lys.Qt import QtCore


class CheckResult:
    """
    Result of a single check.

    Attributes:
        name (str): Name of the check.
        status (str): ``"passed"``, ``"failed"`` or ``"skipped"``.
        message (str): Details of the result.
    """

    def __init__(self, name, status, message=""):
        self.name = name
        self.status = status
        self.message = message

    def __repr__(self):
        return f"[{self.status.upper()}] {self.name}" + (f": {self.message}" if self.message else "")


class ConformanceReport:
    """
    Results of conformance checks for a device.
    """

    def __init__(self, title):
        self.title = title
        self.results = []

    def run(self, name, func, *args, **kwargs):
        """
        Run a check and record the result.

        The check passes if ``func`` returns normally (its return value is used as the message).
        ``SkipCheck`` marks the check as skipped, and any other exception marks it as failed.
        """
        try:
            msg = func(*args, **kwargs)
            self.results.append(CheckResult(name, "passed", msg or ""))
        except SkipCheck as e:
            self.results.append(CheckResult(name, "skipped", str(e)))
        except AssertionError as e:
            self.results.append(CheckResult(name, "failed", str(e)))
        except Exception as e:
            self.results.append(CheckResult(name, "failed", f"{type(e).__name__}: {e}\n{traceback.format_exc()}"))

    @property
    def passed(self):
        """
        True if no check failed.
        """
        return all(r.status != "failed" for r in self.results)

    @property
    def failures(self):
        """
        Failed checks.

        Returns:
            list[CheckResult]: The failed checks.
        """
        return [r for r in self.results if r.status == "failed"]

    def raiseIfFailed(self):
        """
        Raise ``AssertionError`` if any check failed.
        """
        if not self.passed:
            raise AssertionError(str(self))

    def __str__(self):
        lines = [f"{self.title}: {'PASSED' if self.passed else 'FAILED'}"]
        lines += ["  " + repr(r) for r in self.results]
        return "\n".join(lines)


class SkipCheck(Exception):
    """
    Raised in a check to mark it as skipped.
    """


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def processEvents(duration=0):
    """
    Process Qt events for ``duration`` seconds.
    """
    end = time.perf_counter() + duration
    while True:
        QtCore.QCoreApplication.processEvents()
        if time.perf_counter() >= end:
            return
        time.sleep(0.005)


def waitUntil(condition, timeout):
    """
    Process Qt events until ``condition()`` becomes True.

    Returns:
        bool: True if the condition is satisfied within ``timeout`` seconds.
    """
    end = time.perf_counter() + timeout
    while not condition():
        if time.perf_counter() >= end:
            return False
        QtCore.QCoreApplication.processEvents()
        time.sleep(0.005)
    return True


class Workers:
    """
    Run functions repeatedly in background threads and collect exceptions.

    Args:
        funcs (list[callable]): Functions called repeatedly. Each thread calls all functions in turn.
        n (int): Number of threads.
    """

    def __init__(self, funcs, n):
        self._funcs = funcs
        self._stop = threading.Event()
        self.errors = []
        self.calls = 0
        self._lock = threading.Lock()
        self._threads = [threading.Thread(target=self._loop, daemon=True) for _ in range(n)]

    def _loop(self):
        while not self._stop.is_set():
            for f in self._funcs:
                try:
                    f()
                except Exception as e:
                    with self._lock:
                        self.errors.append(f"{type(e).__name__}: {e}")
                with self._lock:
                    self.calls += 1
            # Yield to the other threads, so that the device is not starved by the busy loop (the calls still overlap).
            time.sleep(0.0005)

    def __enter__(self):
        for t in self._threads:
            t.start()
        return self

    def __exit__(self, *args):
        self._stop.set()
        for t in self._threads:
            t.join(10)
