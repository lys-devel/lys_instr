import unittest
import os
import json
import tempfile
import numpy as np

from PyQt5 import QtWidgets
from lys_instr import DataStorage, dummy
from lys_instr.gui.MultiScan import ScanWidget
from lys_instr.gui.Memory import ControllerMemory
from lys_instr.gui import MultiMotorGUI

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class _TempDirTestCase(unittest.TestCase):

    def setUp(self):
        # Widgets save files under .lys in the current directory
        self._cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)
        self.devices = []

    def tearDown(self):
        for d in self.devices:
            d.kill()
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def motor(self, *axes):
        m = dummy.MultiMotorDummy(*axes)
        self.devices.append(m)
        return m


class TestGetNumber(_TempDirTestCase):

    def test_gap_and_reserved(self):
        storage = DataStorage()
        self.assertEqual(storage.getNumber(), 0, "Missing folder should give 0.")
        os.makedirs("folder")
        for i in [0, 1, 3]:
            open(os.path.join("folder", f"data_{i}.npz"), "w").close()
        open(os.path.join("folder", "other_2.npz"), "w").close()
        self.assertEqual(storage.getNumber(), 2, "The smallest unused number should be returned.")
        storage.reserve(shape=(1,))
        self.assertEqual(storage.getNumber(), 4)

    def test_many_files(self):
        os.makedirs("folder")
        for i in range(3000):
            open(os.path.join("folder", f"data_{i}.npz"), "w").close()
        storage = DataStorage()
        calls = []
        original = os.path.exists
        os.path.exists = lambda p: calls.append(p) or original(p)
        try:
            self.assertEqual(storage.getNumber(), 3000)
        finally:
            os.path.exists = original
        self.assertEqual(calls, [], "Each candidate should not be checked by os.path.exists.")


class TestMemory(_TempDirTestCase):

    def test_api(self):
        motor = self.motor("x", "y")
        memory = ControllerMemory([motor], "memory.json")
        motor.set(x=1, y=2, wait=True)
        label1 = memory.savePosition("first")
        motor.set(x=3, y=4, wait=True)
        label2 = memory.savePosition()
        self.assertEqual((label1, label2), ("1", "2"))
        self.assertEqual(memory.positions[0], {"label": "1", "position": {"x": 1, "y": 2}, "memo": "first"})
        self.assertEqual(memory._positionList.topLevelItemCount(), 2, "GUI should follow the saved positions.")

        memory.loadPosition("1", wait=True)
        self.assertEqual(motor.get(), {"x": 1, "y": 2})

        memory.setMemo("2", "second")
        self.assertEqual(memory._positionList.topLevelItem(1).text(2), "second")

        memory.deletePosition("1")
        self.assertEqual([p["label"] for p in memory.positions], ["2"])
        self.assertEqual(memory._positionList.topLevelItemCount(), 1)
        self.assertEqual(memory.savePosition(), "1", "Deleted labels should be reused.")

        with self.assertRaises(ValueError):
            memory.loadPosition("99")
        with self.assertRaises(ValueError):
            memory.deletePosition("2", "99")
        self.assertEqual(len(memory.positions), 2, "Nothing should be deleted if any label is not found.")

        # Saved positions are restored from the file.
        memory2 = ControllerMemory([motor], "memory.json")
        self.assertEqual(memory2.positions, memory.positions)

    def test_without_path(self):
        motor = self.motor("x")
        memory = ControllerMemory([motor])
        memory.savePosition()
        self.assertEqual(len(memory.positions), 1)
        self.assertFalse(os.path.exists(".lys"), "No file should be written without path.")

    def test_missing_axis(self):
        os.makedirs(os.path.join(".lys", ".lys_instr"))
        with open(os.path.join(".lys", ".lys_instr", "memory.json"), "w") as f:
            json.dump([{"label": "1", "position": {"x": 5}, "memo": ""}], f)
        motor = self.motor("x", "y")
        memory = ControllerMemory([motor], "memory.json")
        memory.loadPosition("1", wait=True)
        self.assertEqual(motor.get(), {"x": 5, "y": 0})

    def test_motor_gui(self):
        motor = self.motor("x")
        self.assertIsNone(MultiMotorGUI(motor).memory)
        self.assertIsInstance(MultiMotorGUI(motor, memory="bottom").memory, ControllerMemory)


class TestScanAPI(_TempDirTestCase):

    def setUp(self):
        super().setUp()
        self.storage = DataStorage()
        self.detector = dummy.MultiDetectorDummy(indexShape=(1,), frameShape=(2,), exposure=0.01)
        self.devices.append(self.detector)
        self.storage.connect(self.detector)
        self.switch = dummy.MultiSwitchDummy("sw")
        self.devices.append(self.switch)
        self.scan = ScanWidget(self.storage, [self.motor("x", "y")], [self.switch], {"det": self.detector, "det2": self.detector})

    def test_scan_list(self):
        self.scan.setScanList([
            {"name": "y", "range": (0, 1, 3)},
            {"name": "x", "range": "0, 0.5, 2"},
            {"name": "x", "range": [1, 2], "mode": "Free"},
            {"name": "sw"},
            {"name": "loop", "range": (0, 1, 2)},
        ])
        rows = self.scan.scanList
        self.assertEqual(rows[0], {"type": "motorScan", "name": "y", "mode": "Linear", "range": (0.0, 1.0, 3)})
        self.assertEqual(rows[1], {"type": "motorScan", "name": "x", "mode": "Free", "range": "0, 0.5, 2"})
        self.assertEqual(rows[2]["range"], "1, 2")
        self.assertEqual((rows[3]["type"], rows[3]["mode"]), ("switchScan", "Iteration"))
        self.assertEqual(list(self.scan._list[1].scanRange), [0.0, 0.5, 2.0])
        self.assertEqual(len(self.scan._list), 5, "GUI should follow the scan list.")

        # The scan list is restored by another widget (e.g. after restart).
        scan2 = ScanWidget(self.storage, [self.motor("x", "y")], [self.switch], {"det": self.detector})
        self.assertEqual(scan2.scanList, rows)

        with self.assertRaises(ValueError):
            self.scan.setScanList([{"name": "z", "range": (0, 1, 2)}])
        with self.assertRaises(ValueError):
            self.scan.setScanList([{"name": "x", "range": (0, 1)}])
        self.assertEqual(self.scan.scanList, rows, "Scan list should not change on error.")

    def test_settings(self):
        self.scan.setFileName("pt_{1}")
        self.assertEqual(self.scan.fileName, "pt_{1}")
        self.assertFalse(self.scan._nameBox._check.isChecked())
        self.scan.setFileName(None)
        self.assertTrue(self.scan._nameBox._check.isChecked())
        self.scan.setDetector("det2")
        self.assertEqual(self.scan._detectorsBox.currentText(), "det2")
        with self.assertRaises(ValueError):
            self.scan.setDetector("unknown")
        self.scan.setExposure(0.5)
        self.assertEqual(self.scan._exposure.value(), 0.5)

    def test_start(self):
        events = []
        self.scan.scanStarted.connect(lambda: events.append("started"))
        self.scan.scanFinished.connect(lambda: events.append("finished"))
        self.scan.setScanList([{"name": "x", "range": (0, 1, 3)}])
        self.scan.setFileName("pt_[1]")
        self.scan.setExposure(0.01)
        self.scan.start()
        self.assertTrue(self.scan.isRunning)
        self.assertFalse(self.scan._startBtn.isEnabled(), "GUI should follow the scan state.")
        with self.assertRaises(RuntimeError):
            self.scan.start()
        with self.assertRaises(RuntimeError):
            self.scan.setScanList([])
        self.assertTrue(self.scan.waitForFinished(30))
        self.assertTrue(self.storage.waitForSaved(5))
        self.assertEqual(sorted(os.listdir("folder")), ["pt_0.npz", "pt_1.npz", "pt_2.npz"])
        self.assertEqual(events, ["started", "finished"])

    def test_file_names(self):
        # File names use the target values and indices set by the scan, without reading the motor positions.
        motor = self.scan._motorScanners["x"]
        calls = []
        original = motor._get
        import threading
        # Count the calls in the main thread (the monitoring thread reads positions while moving).
        motor._get = lambda: (threading.current_thread() is threading.main_thread() and calls.append(1)) or original()
        try:
            self.scan.setScanList([{"name": "x", "range": (0, 0.5, 3)}, {"name": "loop", "range": (0, 1, 2)}, {"name": "y", "range": (1, 0, 2)}])
            self.scan.setFileName("y{3}_[3]_l{2}_[2]_x{1}_[1]")
            self.scan.setExposure(0.01)
            names = []
            self.storage.pathChanged.connect(lambda: names.append(self.storage.name))
            self.scan.start()
            self.assertTrue(self.scan.waitForFinished(60))
        finally:
            motor._get = original
        expected = [f"y1_{k}_l{l}_{l}_x{x:.5g}_{i}" for k in range(2) for l in range(2) for i, x in enumerate([0, 0.5, 1])]
        self.assertEqual([n for n in names if n.startswith("y")], expected)
        self.assertEqual(calls, [], "Motor positions should not be read to make file names.")
        self.assertTrue(self.storage.waitForSaved(5))
        self.assertEqual(len(os.listdir("folder")), 12)

    def test_stop(self):
        self.scan.setScanList([{"name": "x", "range": (0, 1, 100)}])
        self.scan.setFileName("pt_[1]")
        self.scan.start()
        self.scan.stop()
        self.assertTrue(self.scan.waitForFinished(30))
        self.assertFalse(self.scan.isRunning)
        self.assertTrue(self.scan._startBtn.isEnabled())

    def test_waitForFinished_timeout(self):
        self.scan.setScanList([{"name": "x", "range": (0, 1, 100)}])
        self.scan.setFileName("pt_[1]")
        self.scan.start()
        self.assertFalse(self.scan.waitForFinished(0.1))
        self.assertTrue(self.scan.stopAndWait())


class TestThreading(_TempDirTestCase):

    def test_stop_does_not_freeze(self):
        # stop() should process events while waiting for the acquisition thread.
        from PyQt5 import QtCore, QtTest
        import time
        detector = dummy.MultiDetectorDummy(indexShape=(3,), frameShape=(2,), exposure=0.5)
        self.devices.append(detector)
        detector.startAcq()
        QtTest.QTest.qWait(100)
        ticks = []
        timer = QtCore.QTimer(interval=20)
        timer.timeout.connect(lambda: ticks.append(1))
        timer.start()
        start = time.perf_counter()
        detector.stop()
        elapsed = time.perf_counter() - start
        timer.stop()
        self.assertFalse(detector.isBusy, "stop() should return after the acquisition is finished.")
        self.assertGreater(len(ticks), elapsed / 0.02 / 3, "Events should be processed while stopping.")

    def test_get_in_acquisition_thread(self):
        # Frames are read from the device in the acquisition thread, and all frames are delivered.
        import threading
        detector = dummy.MultiDetectorDummy(indexShape=(20,), frameShape=(2,), exposure=0.01)
        self.devices.append(detector)
        where = []
        original = detector._get
        detector._get = lambda: where.append(threading.current_thread() is threading.main_thread()) or original()
        received = {}
        detector.dataAcquired.connect(received.update)
        data = detector.startAcq(wait=True, output=True)
        self.assertEqual(len(data), 20)
        self.assertEqual(len(received), 20)
        self.assertNotIn(True, where, "_get() should not be called in the main thread during acquisition.")
        self.assertEqual(detector.receivers(detector.updated), 0, "Connections for the acquisition should be removed.")

    def test_start_stop_repeatedly(self):
        from PyQt5 import QtTest
        detector = dummy.MultiDetectorDummy(indexShape=(5,), frameShape=(2,), exposure=0.01)
        self.devices.append(detector)
        for i in range(30):
            detector.startAcq()
            if i % 2:
                QtTest.QTest.qWait(10)
            detector.stop()
        self.assertFalse(detector.isBusy)
        data = detector.startAcq(wait=True, output=True)
        self.assertEqual(len(data), 5)

    def test_set_not_blocked_by_polling(self):
        import time

        class SlowMotor(dummy.MultiMotorDummy):
            def _isBusy(self):
                time.sleep(0.3)
                return super()._isBusy()

        motor = SlowMotor("x", speed=10)
        self.devices.append(motor)
        from PyQt5 import QtTest
        QtTest.QTest.qWait(400)
        latency = []
        for i in range(3):
            start = time.perf_counter()
            motor.set(x=i + 1)
            latency.append(time.perf_counter() - start)
            motor.waitForReady()
        self.assertLess(max(latency), 0.1, "set() should not wait for the polling communication.")

        # set(wait=True) should wait until the motion is completed even with slow polling.
        motor.set(x=10, wait=True)
        self.assertAlmostEqual(motor.get()["x"], 10)
        self.assertFalse(motor.isBusy["x"])

    def test_offset_set_wait(self):
        motor = self.motor("x")
        motor.offset["x"] = 1
        motor.set(x=2, wait=True)
        self.assertAlmostEqual(motor.get()["x"], 2)
        self.assertAlmostEqual(motor._get()["x"], 3)
        motor.offset["x"] = 0
