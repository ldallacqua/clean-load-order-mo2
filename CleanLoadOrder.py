# Cleans the whole load order in one go with SSEEdit's Quick Auto Clean: removes ITMs and undeletes references.
# Skips what LOOT knows to be clean or marks as "do not clean", and what already went through here and has not changed since.
# Copies each plugin to <MO2>/cleaning-backups before touching it; only the copies of plugins that changed are kept.
# Install: copy this file to <MO2>/plugins/ and restart MO2. It shows up in the tools menu as Clean Load Order.
import ctypes
import glob
import json
import os
import re
import shutil
import time
import zlib
from ctypes import wintypes

from PyQt6.QtCore import QEventLoop, Qt, QTimer
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QMessageBox, QProgressDialog

import mobase

GAME = "Skyrim Special Edition"
NAME = "Clean Load Order"
# Skyrim.esm has no master to compare against; the others are generated patches, where ITMs are on purpose
NEVER_CLEAN = {"skyrim.esm", "dyndolod.esp", "occlusion.esp", "synthesis.esp", "bashed patch, 0.esp"}
# Limit per plugin: an xEdit error window, such as a missing master, would otherwise wait forever
TIMEOUT_SECONDS = 600
NAG_TITLE = "A message from the developer"

WAIT_OBJECT_0 = 0
WM_CLOSE = 0x0010
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.FindWindowW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR)
user32.FindWindowW.restype = wintypes.HWND
user32.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

ML_NAME = re.compile(r"^  (?:# )?- name: (.+?)\s*$")
ML_KEY = re.compile(r"^    (\w+):")
ML_CRC = re.compile(r"crc:\s*0x([0-9A-Fa-f]+)")
ML_KEEP = re.compile(r"#.*\bdo not clean\b", re.IGNORECASE)


class Abort(Exception):
    """Something went wrong in a way that would affect the next plugins."""


def read_masterlist(text):
    """masterlist.yaml -> {lowercase name: {dirty, clean, keep}}. LOOT's "do not clean" notes only exist as comments."""
    entries, entry, section = {}, None, None
    for line in text.splitlines():
        match = ML_NAME.match(line)
        if match:
            name = match[1].strip("'\"").replace("''", "'").lower()
            entry = entries.setdefault(name, {"dirty": set(), "clean": set(), "keep": False})
            section = None
        elif entry is not None:
            if ML_KEEP.search(line):
                entry["keep"] = True
            match = ML_KEY.match(line)
            if match:
                section = match[1]
            match = ML_CRC.search(line)
            if match and section in ("dirty", "clean"):
                entry[section].add(int(match[1], 16))
    return entries


def classify(name, crc, masterlist, seen):
    """Returns "dirty" or "unknown" for what should be cleaned; any other value is the reason to skip."""
    key = name.lower()
    if key in NEVER_CLEAN:
        return "never cleaned"
    if seen.get(key) == crc:
        return "already checked"
    entry = masterlist.get(key)
    if entry is None:
        return "unknown"
    if entry["keep"]:
        return "LOOT: do not clean"
    if crc in entry["clean"]:
        return "LOOT: clean"
    return "dirty" if crc in entry["dirty"] else "unknown"


def parse_log(text):
    """Reads one xEdit session log. The data folder is checked because, without -D:, xEdit uses the registry's install."""
    result = {"data": None, "finished": "Quick Clean mode finished" in text, "itm": 0, "udr": 0, "nav": 0}
    match = re.search(r"Data Path: (.+)", text)
    if match:
        result["data"] = match[1].strip()
    report = text.rpartition("LOOT Masterlist Entries")[2]
    for key in ("itm", "udr", "nav"):
        match = re.search(rf"^\s*{key}: (\d+)", report, re.MULTILINE)
        if match:
            result[key] = int(match[1])
    return result


def crc32(path):
    value = 0
    with open(path, "rb") as file:
        while chunk := file.read(1 << 20):
            value = zlib.crc32(chunk, value)
    return value & 0xFFFFFFFF


def quoted(value):
    # MO2 joins the arguments with spaces, so only values with spaces get quotes
    return f'"{value}"' if " " in value else value


def same_path(a, b):
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def close_nag():
    """Closes xEdit's donation message, which shows up from time to time and would hold up the queue."""
    window = user32.FindWindowW(None, NAG_TITLE)
    if window:
        user32.PostMessageW(window, WM_CLOSE, 0, 0)


class CleanLoadOrder(mobase.IPluginTool):
    def __init__(self):
        super().__init__()
        self._organizer = None
        self._canceled = False

    def init(self, organizer: mobase.IOrganizer):
        self._organizer = organizer
        return True

    def name(self):
        return NAME

    def author(self):
        return "Lucas Dall'Acqua"

    def description(self):
        return "Runs SSEEdit's Quick Auto Clean on every active plugin that needs it, in one click."

    def version(self):
        return mobase.VersionInfo(1, 0, 0, mobase.ReleaseType.FINAL)

    def requirements(self):
        return [mobase.PluginRequirementFactory.gameDependency(GAME)]

    def settings(self):
        return [mobase.PluginSetting("xedit", "Path to SSEEdit.exe. Empty: use the one registered in MO2's executables.", "")]

    def displayName(self):
        return NAME

    def tooltip(self):
        return self.description()

    def icon(self):
        return QIcon()

    def display(self):
        parent = self._parentWidget()
        xedit = self._find_xedit()
        if not xedit:
            QMessageBox.warning(parent, NAME, "Could not find SSEEdit.exe. Add it to MO2's executables.")
            return
        masterlist = self._masterlist()
        if not masterlist:
            QMessageBox.warning(
                parent, NAME,
                "LOOT's masterlist is missing or empty. Without it, plugins marked \"do not clean\" can't be recognized "
                "and would be cleaned. Update the masterlist in LOOT and try again.",
            )
            return
        seen = self._load_seen()
        todo, skipped = self._plan(seen, masterlist)
        if not todo:
            QMessageBox.information(parent, NAME, f"Nothing to clean: all {len(skipped)} active plugins are already taken care of.")
            return
        if not self._confirm(parent, todo, skipped):
            return
        results, folder, error = self._run(parent, xedit, todo, seen)
        self._save_seen(seen)
        self._organizer.refresh()
        self._report(parent, results, folder, error)

    def _find_xedit(self):
        path = self._organizer.pluginSetting(NAME, "xedit")
        if path and os.path.isfile(path):
            return os.path.normpath(path)
        ini = os.path.join(self._organizer.basePath(), "ModOrganizer.ini")
        with open(ini, encoding="utf-8", errors="replace") as file:
            text = file.read()
        for match in re.finditer(r"^\d+\\binary=(.*[\\/]SSEEdit[^\\/]*\.exe)\s*$", text, re.MULTILINE | re.IGNORECASE):
            if os.path.isfile(match[1]):
                return os.path.normpath(match[1])
        return None

    def _seen_path(self):
        return os.path.join(self._organizer.pluginDataPath(), "CleanLoadOrder.json")

    def _load_seen(self):
        try:
            with open(self._seen_path(), encoding="utf-8") as file:
                return json.load(file)
        except (OSError, ValueError):
            return {}

    def _save_seen(self, seen):
        os.makedirs(os.path.dirname(self._seen_path()), exist_ok=True)
        with open(self._seen_path(), "w", encoding="utf-8") as file:
            json.dump(seen, file, indent=1)

    def _masterlist(self):
        path = os.path.join(os.environ.get("LOCALAPPDATA", ""), "LOOT", "games", GAME, "masterlist.yaml")
        try:
            with open(path, encoding="utf-8") as file:
                return read_masterlist(file.read())
        except OSError:
            return {}

    def _plan(self, seen, masterlist):
        """In load order, so that each master is cleaned before the plugins that depend on it."""
        plugins = self._organizer.pluginList()
        names = [n for n in plugins.pluginNames() if plugins.state(n) == mobase.PluginState.ACTIVE]
        todo, skipped = [], []
        for name in sorted(names, key=plugins.loadOrder):
            path = os.path.normpath(self._organizer.resolvePath(name))
            # Checked before the CRC so that Skyrim.esm, at 240 MB, is not read for nothing
            reason = "never cleaned" if name.lower() in NEVER_CLEAN else classify(name, crc32(path), masterlist, seen)
            if reason in ("dirty", "unknown"):
                todo.append({"name": name, "path": path, "kind": reason})
            else:
                skipped.append((name, reason))
        return todo, skipped

    def _confirm(self, parent, todo, skipped):
        dirty = sum(item["kind"] == "dirty" for item in todo)
        box = QMessageBox(parent)
        box.setWindowTitle(NAME)
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(f"Clean {len(todo)} plugins?")
        box.setInformativeText(
            f"{dirty} are dirty according to LOOT and LOOT has no data on {len(todo) - dirty}. "
            f"{len(skipped)} will be skipped.\n\n"
            "SSEEdit will open and close once per plugin. You can cancel between plugins. "
            "Each plugin that changes gets a copy in cleaning-backups."
        )
        lines = [f"{item['name']} ({'dirty' if item['kind'] == 'dirty' else 'check'})" for item in todo]
        lines += [f"SKIP {name} ({reason})" for name, reason in skipped]
        box.setDetailedText("\n".join(lines))
        clean = box.addButton("Clean", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        return box.clickedButton() == clean

    def _run(self, parent, xedit, todo, seen):
        folder = os.path.normpath(os.path.join(self._organizer.basePath(), "cleaning-backups", time.strftime("%Y-%m-%d_%H-%M-%S")))
        os.makedirs(os.path.join(folder, "logs"), exist_ok=True)
        os.makedirs(os.path.join(folder, "xedit"), exist_ok=True)
        self._canceled = False
        progress = QProgressDialog("", "Cancel", 0, len(todo), parent)
        progress.setWindowTitle(NAME)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoReset(False)
        progress.setAutoClose(False)
        progress.canceled.connect(self._cancel)
        nag = QTimer()
        nag.timeout.connect(close_nag)
        nag.start(500)
        results, error = [], None
        try:
            for index, item in enumerate(todo):
                if self._canceled:
                    break
                progress.setLabelText(f"{index + 1} of {len(todo)}: {item['name']}")
                progress.setValue(index)
                result = self._clean(xedit, item, folder)
                results.append(result)
                if result["ok"]:
                    seen[item["name"].lower()] = result["crc"]
        except Abort as abort:
            error = str(abort)
        finally:
            nag.stop()
            # Closing a QProgressDialog also emits canceled, even when nobody clicked Cancel
            progress.canceled.disconnect(self._cancel)
            progress.close()
        # After a wrong-folder abort these are the only originals of whatever xEdit cleaned there
        if not error:
            shutil.rmtree(os.path.join(folder, "xedit"), ignore_errors=True)
        return results, folder, error

    def _cancel(self):
        self._canceled = True

    def _wait(self, handle):
        """Waits for xEdit without freezing MO2's window. Returns False if it had to be terminated."""
        deadline = time.monotonic() + TIMEOUT_SECONDS
        loop = QEventLoop()
        finished = [True]

        def tick():
            if kernel32.WaitForSingleObject(handle, 0) == WAIT_OBJECT_0:
                loop.quit()
            elif time.monotonic() > deadline:
                kernel32.TerminateProcess(handle, 1)
                finished[0] = False
                loop.quit()

        timer = QTimer()
        timer.timeout.connect(tick)
        timer.start(250)
        loop.exec()
        timer.stop()
        return finished[0]

    def _clean(self, xedit, item, folder):
        name, path = item["name"], item["path"]
        data = os.path.normpath(self._organizer.managedGame().dataDirectory().absolutePath())
        overwrite = os.path.join(os.path.normpath(self._organizer.overwritePath()), name)
        overwrite_existed = os.path.isfile(overwrite)
        before = crc32(path)
        backup = os.path.join(folder, name)
        shutil.copy2(path, backup)
        log = os.path.join(folder, "logs", re.sub(r"[^\w.-]", "_", name) + ".log")
        args = [
            "-qac", "-autoexit", "-autoload", "-DontCache",
            "-D:" + quoted(data),
            # The trailing separator matters: xEdit glues the file name straight onto this path
            "-B:" + quoted(os.path.join(folder, "xedit", "")),
            "-R:" + quoted(log),
            f'"{name}"',
        ]
        handle = self._organizer.startApplication(xedit, args, os.path.dirname(xedit))
        if not handle:
            raise Abort(f"MO2 could not start SSEEdit for {name}.")
        finished = self._wait(handle)
        if not os.path.isfile(log):
            raise Abort(f"SSEEdit produced no log for {name}. The original copy is at {backup}.")
        with open(log, encoding="utf-8", errors="replace") as file:
            parsed = parse_log(file.read())
        if not parsed["data"] or not same_path(parsed["data"], data):
            raise Abort(
                f"SSEEdit used the folder {parsed['data']} instead of {data} when opening {name}. Stopped here. "
                f"If it cleaned anything in that folder, the original is in {os.path.join(folder, 'xedit')}."
            )
        # Depending on where the plugin lives, the VFS may leave the saved file in Overwrite
        if not overwrite_existed and os.path.isfile(overwrite) and not same_path(overwrite, path):
            shutil.copy2(overwrite, path)
            os.remove(overwrite)
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            shutil.copy2(backup, path)
            raise Abort(f"{name} disappeared after cleaning. Restored the original and stopped here.")
        for directory in {os.path.dirname(path), data, os.path.dirname(overwrite)}:
            for leftover in glob.glob(os.path.join(glob.escape(directory), glob.escape(name) + ".save.*")):
                os.remove(leftover)
        after = crc32(path)
        ok = finished and parsed["finished"]
        if after == before:
            os.remove(backup)
        if ok:
            os.remove(log)
        return {"name": name, "path": path, "ok": ok, "crc": after, "changed": after != before,
                "timeout": not finished, "itm": parsed["itm"], "udr": parsed["udr"], "nav": parsed["nav"]}

    def _report(self, parent, results, folder, error):
        changed = [r for r in results if r["changed"]]
        failed = [r for r in results if not r["ok"]]
        navmeshes = [r for r in results if r["nav"]]
        summary = [
            f"{len(changed)} plugins cleaned: {sum(r['itm'] for r in changed)} ITMs removed, "
            f"{sum(r['udr'] for r in changed)} deleted references undeleted.",
            f"{len(results) - len(changed) - len(failed)} had nothing to clean.",
        ]
        if navmeshes:
            summary.append(f"{len(navmeshes)} have deleted navmeshes, which Quick Auto Clean does not fix.")
        if failed:
            summary.append(f"{len(failed)} failed. Their logs are in {os.path.join(folder, 'logs')}.")
        if self._canceled:
            summary.append("Canceled before the end. Running it again picks up where it stopped.")
        if error:
            summary.insert(0, f"STOPPED BEFORE THE END: {error}")
        lines = list(summary) + ["", "Originals: " + folder, ""]
        lines += [f"CLEANED {r['name']}: {r['itm']} ITMs, {r['udr']} UDRs. Location: {r['path']}" for r in changed]
        lines += [f"NAVMESH {r['name']}: {r['nav']}" for r in navmeshes]
        lines += [f"FAILED {r['name']}" + (" (timed out)" if r["timeout"] else "") for r in failed]
        with open(os.path.join(folder, "report.txt"), "w", encoding="utf-8") as file:
            file.write("\n".join(lines) + "\n")
        logs = os.path.join(folder, "logs")
        if not os.listdir(logs):
            os.rmdir(logs)
        box = QMessageBox(parent)
        box.setWindowTitle(NAME)
        box.setIcon(QMessageBox.Icon.Warning if error or failed else QMessageBox.Icon.Information)
        box.setText("\n".join(summary))
        box.setInformativeText("Report and originals in " + folder)
        box.setDetailedText("\n".join(lines))
        box.exec()


def createPlugin():
    return CleanLoadOrder()
