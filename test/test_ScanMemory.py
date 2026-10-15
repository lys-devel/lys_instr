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
