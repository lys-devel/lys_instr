import unittest
import time
import os
import tempfile
import numpy as np

from PyQt5 import QtTest, QtWidgets
from lys_instr.DataStorage import DataStorage
from lys_instr.gui import DataStorageGUI
from lys_instr.dummy import MultiDetectorDummy

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def waitUntil(cond, timeout=10):
    start = time.time()
    while not cond() and (time.time() - start < timeout):
        QtTest.QTest.qWait(10)


class TestDataStorage(unittest.TestCase):

    def test_init(self):
        storage = DataStorage()
        self.assertEqual(storage.base, ".", "Default base directory should be '.'.")
        self.assertEqual(storage.folder, "folder", "Default folder should be 'folder'.")
        self.assertEqual(storage.name, "data", "Default name should be 'data'.")
        self.assertTrue(storage.enabled, "Storage should be enabled by default.")
        self.assertTrue(storage.numbered, "Storage should use numbering by default.")

    def test_getNumber(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = DataStorage()
            storage.base = tmpdir
            storage.folder = "newFolder"
            storage.name = "newName"
            n1 = storage.getNumber()
            storage.reserve(shape=(2, 2, 2, 2))
            n2 = storage.getNumber()
            self.assertEqual(n2, n1 + 1, "getNumber should increment after reserving a file.")

    def test_enabled_false(self):
        storage = DataStorage()
        storage.enabled = False
        storage.reserve(shape=(2, 2, 2, 2))
        self.assertIsNone(storage._arr, "No array should be reserved when storage is disabled.")

    def test_numbered_false(self):
        storage = DataStorage()
        storage.numbered = False
        n = storage.getNumber()
        self.assertIsNone(n, "getNumber should return None when numbering is disabled.")

    def test_reserve_update_save(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = DataStorage()
            storage.base = tmpdir
            storage.folder = "newFolder"
            storage.name = "newName"

            n = storage.getNumber()
            storage.reserve(shape=(2, 2, 2, 2), fillValue=5)
            data = {(0, 0): np.ones((2, 2))}
            storage.update(data)
            axes = [np.arange(2), np.arange(2), np.arange(2), np.arange(2)]
            arrSaving = storage._arr
            storage.save(axes)
            self.assertTrue(storage.saving, "Storage should be saving after save() is called.")

            timeout = 5  # seconds
            start = time.time()
            while storage.saving and (time.time() - start < timeout):
                QtTest.QTest.qWait(10)
            self.assertFalse(storage.saving, "Save thread did not finish in time.")

            folder = os.path.join(storage.base, storage.folder)
            expectedFile = os.path.join(folder, f"{storage.name}_{n}.npz")
            self.assertTrue(os.path.exists(expectedFile), f"Expected file {expectedFile} does not exist.")

            with np.load(expectedFile) as npz:
                arr_keys = npz.files
                self.assertTrue(len(arr_keys) > 0, "No arrays found in saved file.")
                arrFromFile = npz[arr_keys[0]]
                self.assertTrue(np.array_equal(arrFromFile, arrSaving), "Saved array does not match the storage buffer.")

    def test_update_without_reservation(self):
        storage = DataStorage()
        storage.update({(0,): np.ones(2)})
        storage.enabled = False
        storage.reserve(shape=(2, 2))
        storage.enabled = True
        storage.update({(0,): np.ones(2)})
        self.assertFalse(storage.saving)

    def test_disabled_during_acquisition(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = DataStorage()
            storage.base = tmpdir
            storage.reserve(shape=(2, 2))
            storage.enabled = False
            storage.update({(0,): np.ones(2), (1,): np.ones(2)})
            waitUntil(lambda: not storage.saving)
            self.assertFalse(storage.saving, "Reserved data should be saved even if disabled after reserve().")
            self.assertTrue(os.path.exists(os.path.join(tmpdir, "folder", "data_0.npz")))

    def test_incomplete_reservation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = DataStorage()
            storage.base = tmpdir
            storage.reserve(shape=(2, 2))
            storage.update({(0,): np.ones(2)})
            storage.name = "second"
            storage.reserve(shape=(2, 2))
            storage.update({(0,): np.ones(2), (1,): np.ones(2)})
            waitUntil(lambda: not storage.saving)
            self.assertEqual(sorted(os.listdir(os.path.join(tmpdir, "folder"))), ["data_0.npz", "second_0.npz"])

    def test_getNumber_while_saving(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = DataStorage()
            storage.base = tmpdir
            storage.reserve(shape=(2, 2))
            storage.update({(0,): np.ones(2), (1,): np.ones(2)})
            self.assertEqual(storage.getNumber(), 1)
            waitUntil(lambda: not storage.saving)

    def test_signals(self):
        storage = DataStorage()
        path, enabled = [], []
        storage.pathChanged.connect(lambda: path.append(True))
        storage.enabledChanged.connect(enabled.append)
        storage.base = "a"
        storage.folder = "b"
        storage.name = "c"
        storage.numbered = False
        storage.name = "c"  # unchanged, should not emit
        storage.enabled = False
        storage.enabled = False  # unchanged, should not emit
        self.assertEqual(len(path), 4)
        self.assertEqual(enabled, [False])

    def test_detector(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = DataStorage()
            storage.base = tmpdir
            detector = MultiDetectorDummy(indexShape=(2,), frameShape=(3,), exposure=0.01)
            storage.connect(detector)

            detector.startAcq()
            waitUntil(lambda: not detector.isBusy and not storage.saving)
            self.assertEqual(os.listdir(os.path.join(tmpdir, "folder")), ["data_0.npz"])

            # Streaming should not be saved.
            detector.startAcq(iter=-1)
            QtTest.QTest.qWait(300)
            self.assertFalse(storage.saving)
            detector.stop()
            waitUntil(lambda: not detector.isBusy and not storage.saving)
            self.assertEqual(os.listdir(os.path.join(tmpdir, "folder")), ["data_0.npz"])

            detector.startAcq()
            waitUntil(lambda: not detector.isBusy and not storage.saving)
            self.assertEqual(sorted(os.listdir(os.path.join(tmpdir, "folder"))), ["data_0.npz", "data_1.npz"])
            detector.kill()

    def test_reserve_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            open(os.path.join(tmpdir, "afile"), "w").close()
            storage = DataStorage()
            errors = []
            storage.errorOccurred.connect(errors.append)
            storage.base = os.path.join(tmpdir, "afile")
            with self.assertRaises(OSError):
                storage.reserve(shape=(2, 2))
            self.assertEqual(len(errors), 1)
            self.assertFalse(storage.saving, "Nothing should be reserved when the folder cannot be created.")

            # The next acquisition with a valid path should be saved to its own path.
            storage.base = tmpdir
            storage.reserve(shape=(2, 2))
            storage.update({(0,): np.ones(2), (1,): np.ones(2)})
            waitUntil(lambda: not storage.saving)
            self.assertEqual(os.listdir(os.path.join(tmpdir, "folder")), ["data_0.npz"])

    def test_save_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = DataStorage()
            errors = []
            storage.errorOccurred.connect(errors.append)
            storage.base = tmpdir
            storage.reserve(shape=(2, 2))
            os.chmod(os.path.join(tmpdir, "folder"), 0o555)
            try:
                storage.update({(0,): np.ones(2), (1,): np.ones(2)})
                waitUntil(lambda: not storage.saving)
            finally:
                os.chmod(os.path.join(tmpdir, "folder"), 0o755)
            self.assertFalse(storage.saving)
            self.assertEqual(len(errors), 1)
            self.assertIn("data_0.npz", errors[0])


    def test_pathErrors(self):
        invalid = [
            {"base": ""}, {"base": "  "}, {"base": " a"},
            {"name": ""}, {"name": "   "}, {"name": " a"}, {"name": "a "}, {"name": "a."}, {"name": "a/b"}, {"name": "a\\b"},
            {"name": "a:b"}, {"name": "a*"}, {"name": "a?"}, {"name": 'a"'}, {"name": "a<b>"}, {"name": "a|b"}, {"name": "a\tb"},
            {"name": "CON"}, {"name": "nul.txt"}, {"name": "COM1"},
            {"folder": " "}, {"folder": "a /b"}, {"folder": "a//b"}, {"folder": "a:b"}, {"folder": "AUX"},
        ]
        for props in invalid:
            with self.subTest(props=props), tempfile.TemporaryDirectory() as tmpdir:
                storage = DataStorage()
                storage.base = tmpdir
                for key, value in props.items():
                    setattr(storage, key, value)
                self.assertIn(list(props.keys())[0], storage.pathErrors())
                errors = []
                storage.errorOccurred.connect(errors.append)
                with self.assertRaises(ValueError):
                    storage.reserve(shape=(2,))
                self.assertEqual(len(errors), 1)
                self.assertFalse(storage.saving)
                self.assertEqual(os.listdir(tmpdir), [])

        valid = [{}, {"folder": ""}, {"folder": "a/b"}, {"folder": "a\\b"}, {"name": "my data"}, {"name": "x=1.5_[1]"}, {"name": "CONFIG"}]
        for props in valid:
            with self.subTest(props=props), tempfile.TemporaryDirectory() as tmpdir:
                storage = DataStorage()
                storage.base = tmpdir
                for key, value in props.items():
                    setattr(storage, key, value)
                self.assertEqual(storage.pathErrors(), {})
                storage.reserve(shape=(2,))
                storage.update({(0,): 1.0, (1,): 1.0})
                waitUntil(lambda: not storage.saving)
                self.assertEqual(len(os.listdir(tmpdir)), 1)


class TestDataStorageGUI(unittest.TestCase):

    def setUp(self):
        # DataStorageGUI writes .lastPath.json in the current directory
        self._cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def test_init_from_obj(self):
        storage = DataStorage()
        storage.base = "base"
        storage.numbered = False
        gui = DataStorageGUI(storage)
        self.assertEqual((storage.base, storage.folder, storage.name), ("base", "folder", "data"), "GUI init should not overwrite the storage.")
        self.assertEqual((gui._base.text(), gui._folder.text(), gui._name.text()), ("base", "folder", "data"))
        self.assertFalse(gui._numberedCheck.isChecked())

    def test_gui_to_obj(self):
        storage = DataStorage()
        gui = DataStorageGUI(storage)
        gui._base.setText("x")
        gui._folder.setText("y")
        gui._name.setText("z")
        gui._numberedCheck.setChecked(False)
        gui._enabledCheck.setChecked(False)
        self.assertEqual((storage.base, storage.folder, storage.name), ("x", "y", "z"))
        self.assertFalse(storage.numbered)
        self.assertFalse(storage.enabled)

    def test_obj_to_gui(self):
        storage = DataStorage()
        gui = DataStorageGUI(storage)
        storage.base = self._tmp.name
        storage.folder = "f"
        storage.name = "n"
        self.assertEqual((gui._base.text(), gui._folder.text(), gui._name.text()), (self._tmp.name, "f", "n"))
        self.assertEqual(gui._number.value(), 0)
        storage.enabled = False
        self.assertFalse(gui._enabledCheck.isChecked())
        storage.numbered = False
        self.assertFalse(gui._numberedCheck.isChecked())
        self.assertEqual(gui._number.text(), "")
        storage.numbered = True
        self.assertEqual(gui._number.text(), "0")

    def test_obj_not_reverted_by_gui(self):
        storage = DataStorage()
        gui = DataStorageGUI(storage)
        storage.base = self._tmp.name
        storage.name = "fromCode"
        storage.reserve(shape=(2,))
        self.assertEqual(storage.name, "fromCode")
        self.assertEqual(os.path.basename(storage._paths[-1]), "fromCode_0.npz")

    def test_error_display(self):
        storage = DataStorage()
        gui = DataStorageGUI(storage)
        open("afile", "w").close()
        storage.base = "afile"
        with self.assertRaises(OSError):
            storage.reserve(shape=(2,))
        self.assertTrue(gui._savingState.text().startswith("[Error]"))

    def test_invalid_path_display(self):
        storage = DataStorage()
        gui = DataStorageGUI(storage)
        gui._name.setText("a:b")
        self.assertIn("red", gui._name.styleSheet())
        self.assertEqual(gui._base.styleSheet(), "")
        self.assertTrue(gui._savingState.text().startswith("[Invalid path]"))
        storage.name = "ab"
        self.assertEqual(gui._name.styleSheet(), "")
        self.assertEqual(gui._savingState.text(), "[Status] Waiting")

    def test_number(self):
        os.makedirs("folder")
        for i in range(150):
            open(os.path.join("folder", f"data_{i}.npz"), "w").close()
        storage = DataStorage()
        gui = DataStorageGUI(storage)
        self.assertEqual(gui._number.value(), 150)

        # Number should follow reservation and saving.
        storage.reserve(shape=(2, 2))
        self.assertEqual(gui._number.value(), 151)
        storage.update({(0,): np.ones(2), (1,): np.ones(2)})
        waitUntil(lambda: not storage.saving)
        self.assertEqual(gui._number.value(), 151)
        self.assertTrue(os.path.exists(os.path.join("folder", "data_150.npz")))
