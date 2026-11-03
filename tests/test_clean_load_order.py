"""Runs the plugin against a fake xEdit, so nothing here touches a real game.

Needs PyQt6 (`pip install PyQt6`), or point MO2_DIR at an MO2 install on Python 3.12 to use the PyQt6 it ships. Windows only.
Run with: python -m unittest discover tests
"""
import ctypes  # noqa: F401  Loaded before MO2's folders join the DLL search, so its own libraries resolve first
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest

MO2_DIR = os.environ.get("MO2_DIR")
if MO2_DIR:
    sys.path.insert(0, os.path.join(MO2_DIR, "plugins", "plugin_python", "libs"))
    os.add_dll_directory(MO2_DIR)
    os.add_dll_directory(os.path.join(MO2_DIR, "dlls"))

from PyQt6.QtCore import QCoreApplication

mobase = types.ModuleType("mobase")
mobase.IPluginTool = type("IPluginTool", (), {"_parentWidget": lambda self: None})
mobase.IOrganizer = object
sys.modules["mobase"] = mobase

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("clean_load_order", os.path.join(ROOT, "CleanLoadOrder.py"))
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)

app = QCoreApplication([])

MASTERLIST = """
plugins:
  - name: 'Update.esm'
    group: *dlcGroup
    dirty:
      - <<: *quickClean
        crc: 0x17AB5E20
        itm: 334
    clean:
      - crc: 0x0000ABCD
  - name: 'Navigator-NavFixes.esl'
    group: *fixesGroup
    # Do not clean - ITMs are intentional according to the author.
  - name: 'Author''s Mod.esp'
    clean:
      - crc: 0x12345678
"""

# Behaves according to the first word of the plugin's name
FAKE_XEDIT = r"""
import json, os, re, sys, time
joined = " ".join(sys.argv[1:])
def option(key):
    return re.search("-" + key + ":(\\S+)", joined)[1]
name = sys.argv[-1]
path = json.load(open(os.environ["FAKE_MAP"]))[name]
mode = name.split("_")[0]
head = "Using Skyrim Special Edition Data Path: " + (r"C:\Steam\Data" if mode == "wrongdata" else option("D")) + "\\\n"
log = option("R")
done = "Quick Clean mode finished\n"
if mode == "nolog":
    sys.exit(0)
if mode == "hang":
    open(log, "w").write(head)
    time.sleep(60)
elif mode == "vanish":
    os.remove(path)
    open(log, "w").write(head + done)
elif mode == "inplace":
    open(path, "ab").write(b"x")
    open(log, "w").write(head + "LOOT Masterlist Entries\n    dirty:\n        itm: 3\n        udr: 2\n        nav: 1\n" + done)
elif mode == "overwrite":
    open(os.path.join(os.environ["FAKE_OVERWRITE"], name), "wb").write(b"CLEANED")
    open(log, "w").write(head + "LOOT Masterlist Entries\n    dirty:\n        itm: 5\n" + done)
else:
    open(log, "w").write(head + "LOOT Masterlist Entries\n    clean:\n" + done)
"""


class FakeSignal:
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def disconnect(self, slot):
        self.slots.remove(slot)


class FakeProgress:
    def __init__(self, *args):
        self.canceled = FakeSignal()

    def __getattr__(self, name):
        return lambda *args: None

    def close(self):
        # Like the real QProgressDialog, closing emits canceled
        for slot in self.canceled.slots:
            slot()


class FakeBox:
    warnings = []

    class Icon:
        Warning = Information = Question = 0

    def __init__(self, *args):
        pass

    def __getattr__(self, name):
        return lambda *args: None

    warning = staticmethod(lambda parent, title, text: FakeBox.warnings.append(text))


plugin.QProgressDialog = FakeProgress
plugin.QMessageBox = FakeBox


class MasterlistTest(unittest.TestCase):
    def setUp(self):
        self.masterlist = plugin.read_masterlist(MASTERLIST)

    def test_dirty_crc_is_cleaned(self):
        self.assertEqual(plugin.classify("Update.esm", 0x17AB5E20, self.masterlist, {}), "dirty")

    def test_clean_crc_is_skipped(self):
        self.assertEqual(plugin.classify("Update.esm", 0xABCD, self.masterlist, {}), "LOOT: clean")

    def test_other_crc_is_unknown(self):
        self.assertEqual(plugin.classify("Update.esm", 1, self.masterlist, {}), "unknown")

    def test_do_not_clean_comment_is_respected(self):
        self.assertEqual(plugin.classify("Navigator-NavFixes.esl", 1, self.masterlist, {}), "LOOT: do not clean")

    def test_quoted_apostrophe_in_name(self):
        self.assertEqual(plugin.classify("Author's Mod.esp", 0x12345678, self.masterlist, {}), "LOOT: clean")

    def test_unlisted_plugin_is_unknown(self):
        self.assertEqual(plugin.classify("nothere.esp", 1, self.masterlist, {}), "unknown")

    def test_already_checked_is_skipped(self):
        self.assertEqual(plugin.classify("nothere.esp", 1, self.masterlist, {"nothere.esp": 1}), "already checked")

    def test_changed_plugin_is_checked_again(self):
        self.assertEqual(plugin.classify("nothere.esp", 2, self.masterlist, {"nothere.esp": 1}), "unknown")

    def test_never_clean_wins(self):
        self.assertEqual(plugin.classify("Skyrim.esm", 1, self.masterlist, {}), "never cleaned")


class ParseLogTest(unittest.TestCase):
    def test_counts_come_from_the_report(self):
        result = plugin.parse_log("Using Skyrim Special Edition Data Path: C:\\Game\\Data\\\nLOOT Masterlist Entries\n    dirty:\n        itm: 2\n        udr: 1\nQuick Clean mode finished.\n")
        self.assertEqual((result["data"], result["finished"], result["itm"], result["udr"], result["nav"]), ("C:\\Game\\Data\\", True, 2, 1, 0))

    def test_unfinished_log(self):
        self.assertFalse(plugin.parse_log("Using Skyrim Special Edition Data Path: C:\\Game\\Data\\\n")["finished"])


class Organizer:
    def __init__(self, root, script):
        self.root = root
        self.script = script
        self.calls = []
        self.processes = []
        self.game = types.SimpleNamespace(dataDirectory=lambda: types.SimpleNamespace(absolutePath=lambda: os.path.join(root, "data").replace("\\", "/")))

    def managedGame(self):
        return self.game

    def overwritePath(self):
        return os.path.join(self.root, "overwrite").replace("\\", "/")

    def basePath(self):
        # MO2 returns forward slashes
        return os.path.join(self.root, "mo2").replace("\\", "/")

    def startApplication(self, executable, args, cwd):
        self.calls.append(args)
        process = subprocess.Popen(f'"{sys.executable}" "{self.script}" ' + " ".join(args))  # MO2 joins the arguments with spaces
        self.processes.append(process)
        return int(process._handle)


class CleaningFlowTest(unittest.TestCase):
    PLUGINS = {
        "inplace_a.esp": "mods/Mod A", "overwrite_b.esp": "mods/Mod A", "none_c.esp": "data", "hang_d.esp": "data",
        "wrongdata_e.esp": "data", "vanish_f.esp": "mods/Mod A", "nolog_g.esp": "data",
    }

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="clean_load_order_")
        for folder in ("data", "overwrite", "mods/Mod A", "mo2"):
            os.makedirs(os.path.join(self.root, folder))
        self.paths = {}
        for name, folder in self.PLUGINS.items():
            path = os.path.normpath(os.path.join(self.root, folder, name))
            with open(path, "wb") as file:
                file.write(name.encode())
            self.paths[name] = path
        script = os.path.join(self.root, "fake_xedit.py")
        with open(script, "w") as file:
            file.write(FAKE_XEDIT)
        map_file = os.path.join(self.root, "map.json")
        with open(map_file, "w") as file:
            json.dump(self.paths, file)
        os.environ["FAKE_MAP"] = map_file
        os.environ["FAKE_OVERWRITE"] = os.path.join(self.root, "overwrite")
        self.organizer = Organizer(self.root, script)
        self.tool = plugin.CleanLoadOrder()
        self.tool.init(self.organizer)
        self.timeout = plugin.TIMEOUT_SECONDS
        plugin.TIMEOUT_SECONDS = 3

    def tearDown(self):
        plugin.TIMEOUT_SECONDS = self.timeout
        for process in self.organizer.processes:
            process.kill()
            process.wait()
        shutil.rmtree(self.root, ignore_errors=True)

    def run_tool(self, *names, seen=None):
        items = [{"name": name, "path": self.paths[name], "kind": "unknown"} for name in names]
        return self.tool._run(None, "C:/fake/SSEEdit.exe", items, {} if seen is None else seen)

    def read(self, path):
        with open(path, "rb") as file:
            return file.read()

    def test_cleaned_in_place(self):
        results, folder, error = self.run_tool("inplace_a.esp")
        self.assertIsNone(error)
        self.assertTrue(results[0]["ok"] and results[0]["changed"])
        self.assertEqual((results[0]["itm"], results[0]["udr"], results[0]["nav"]), (3, 2, 1))
        self.assertEqual(self.read(os.path.join(folder, "inplace_a.esp")), b"inplace_a.esp")

    def test_file_left_in_overwrite_goes_back_to_its_mod(self):
        self.run_tool("overwrite_b.esp")
        self.assertEqual(self.read(self.paths["overwrite_b.esp"]), b"CLEANED")
        self.assertFalse(os.path.exists(os.path.join(self.root, "overwrite", "overwrite_b.esp")))

    def test_unchanged_plugin_keeps_no_backup(self):
        results, folder, _ = self.run_tool("none_c.esp")
        self.assertFalse(results[0]["changed"])
        self.assertFalse(os.path.exists(os.path.join(folder, "none_c.esp")))

    def test_only_successful_plugins_are_remembered(self):
        seen = {}
        self.run_tool("none_c.esp", "hang_d.esp", seen=seen)
        self.assertIn("none_c.esp", seen)
        self.assertNotIn("hang_d.esp", seen)

    def test_hung_xedit_is_terminated_and_the_queue_continues(self):
        results, folder, error = self.run_tool("hang_d.esp", "none_c.esp")
        self.assertIsNone(error)
        self.assertTrue(results[0]["timeout"])
        self.assertFalse(results[0]["ok"])
        self.assertTrue(results[1]["ok"])
        self.assertEqual(os.listdir(os.path.join(folder, "logs")), ["hang_d.esp.log"])

    def test_finishing_is_not_reported_as_canceled(self):
        self.run_tool("none_c.esp")
        self.assertFalse(self.tool._canceled)

    def test_backup_argument_ends_with_a_separator(self):
        self.run_tool("none_c.esp")
        self.assertTrue(self.organizer.calls[0][5].endswith(os.sep))

    def test_no_stray_xedit_files_are_left(self):
        _, folder, _ = self.run_tool("inplace_a.esp")
        self.assertEqual([name for name in os.listdir(folder) if name.startswith("xedit")], [])

    def test_folder_path_is_normalized(self):
        _, folder, _ = self.run_tool("none_c.esp")
        self.assertNotIn("/", folder)

    def test_wrong_data_folder_stops_everything(self):
        results, _, error = self.run_tool("wrongdata_e.esp", "none_c.esp")
        self.assertIn("C:\\Steam\\Data", error)
        self.assertEqual((results, len(self.organizer.calls)), ([], 1))

    def test_vanished_plugin_is_restored(self):
        results, _, error = self.run_tool("vanish_f.esp", "none_c.esp")
        self.assertIn("disappeared", error)
        self.assertEqual(self.read(self.paths["vanish_f.esp"]), b"vanish_f.esp")
        self.assertEqual(results, [])

    def test_missing_log_stops_everything(self):
        results, _, error = self.run_tool("nolog_g.esp", "none_c.esp")
        self.assertIn("no log", error)
        self.assertEqual((results, len(self.organizer.calls)), ([], 1))

    def test_empty_masterlist_refuses_to_run(self):
        FakeBox.warnings.clear()
        self.tool._find_xedit = lambda: "C:/fake/SSEEdit.exe"
        self.tool._masterlist = lambda: plugin.read_masterlist("")
        self.tool._plan = lambda *args: self.fail("should not plan without a masterlist")
        self.tool.display()
        self.assertEqual(len(FakeBox.warnings), 1)
        self.assertIn("masterlist", FakeBox.warnings[0])


if __name__ == "__main__":
    unittest.main()
