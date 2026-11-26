"""
Tests for closing the window: keep downloading in the background / tray icon.

The app runs with --no-window --watchdog, and the tests play the window's part by
polling /api/state the way ui/index.html does. FASTGRAB_WINDOW_GRACE makes
"the window is closed for good" 2 seconds instead of 30.
The Windows-only tests drive the real tray icon through Windows messages.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_fastgrab import ROOT, Server  # noqa: E402

sys.path.insert(0, ROOT)
import fastgrab_app  # noqa: E402

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
WIN = os.name == "nt"


class Pure(unittest.TestCase):
    def test_tooltip(self):
        self.assertEqual(fastgrab_app.tray_tip("en", (0, 0, 0, 0)), "FastGrab")
        self.assertEqual(fastgrab_app.tray_tip("en", (2, 1, 0, 5.5 * 1024 * 1024)),
                         "FastGrab - 2 downloading · 5.5 MB/s · 1 waiting")
        self.assertEqual(fastgrab_app.tray_tip("en", (0, 0, 3, 0)), "FastGrab - 3 paused")
        self.assertIn("قيد التنزيل", fastgrab_app.tray_tip("ar", (1, 0, 0, 100)))
        self.assertIn("בהורדה", fastgrab_app.tray_tip("he", (1, 0, 0, 100)))
        self.assertEqual(fastgrab_app.tray_tip("xx", (0, 0, 1, 0)), "FastGrab - 1 paused")
        self.assertLessEqual(len(fastgrab_app.tray_tip("ar", (10, 10, 10, 10 ** 12))), 127)

    def test_texts_complete(self):
        keys = set(fastgrab_app.TRAY_TEXT["en"])
        for lang in fastgrab_app.LANGS:
            self.assertEqual(set(fastgrab_app.TRAY_TEXT[lang]), keys, lang)

    def test_sizes(self):
        self.assertEqual(fastgrab_app.fmt_size(0), "0 B")
        self.assertEqual(fastgrab_app.fmt_size(1536), "1.5 KB")
        self.assertEqual(fastgrab_app.fmt_size(3 * 1024 ** 3), "3.0 GB")


class Running:
    """One app process with its own home folder."""

    def __init__(self, *flags, grace=2, env=None, settings=None):
        self.tmp = tempfile.mkdtemp()
        self.data = os.path.join(self.tmp, "FastGrab") if WIN else os.path.join(self.tmp, ".config", "fastgrab")
        os.makedirs(self.data, exist_ok=True)
        if settings:
            with open(os.path.join(self.data, "downloads.json"), "w") as f:
                json.dump({"settings": settings, "items": []}, f)
        self.env = dict(os.environ, HOME=self.tmp, USERPROFILE=self.tmp, APPDATA=self.tmp,
                        FASTGRAB_NO_AUTO_UPDATE="1", FASTGRAB_DRY_POWER="1", FASTGRAB_NO_BROWSER="1",
                        FASTGRAB_WINDOW_GRACE=str(grace), **(env or {}))
        self.log = open(os.path.join(self.tmp, "app.log"), "w+")
        self.proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "fastgrab_app.py"),
                                      "--no-window", "--port", "0", *flags],
                                     env=self.env, cwd=ROOT, stdout=self.log, stderr=subprocess.STDOUT)
        info = os.path.join(self.data, "running.json")
        r = None
        for _ in range(900):
            if self.proc.poll() is not None:
                break
            try:
                with open(info) as f:
                    r = json.load(f)
                break
            except (OSError, ValueError):
                time.sleep(0.1)
        if r is None:
            raise RuntimeError("app did not start:\n" + self.logtext())
        self.base, self.token = f"http://127.0.0.1:{r['port']}", r["token"]
        self.downloads = os.path.join(self.tmp, "Downloads")
        os.makedirs(self.downloads, exist_ok=True)

    def logtext(self):
        self.log.flush()
        self.log.seek(0)
        return self.log.read()[-4000:]

    def call(self, path, body=None, client=None):
        h = {"X-Token": self.token, "Content-Type": "application/json"}
        if client:
            h["X-Client"] = client
        req = urllib.request.Request(self.base + path, headers=h,
                                     data=None if body is None else json.dumps(body).encode())
        with OPENER.open(req, timeout=15) as r:
            return json.load(r)

    def window(self, client="w1"):
        """Pretend to be an open window (polls like the page) until .close()."""
        stop = threading.Event()

        def run():
            while not stop.is_set():
                try:
                    self.call("/api/state", client=client)
                except OSError:
                    pass
                stop.wait(0.5)
        threading.Thread(target=run, daemon=True).start()
        return stop

    def wait(self, cond, timeout=60, what="condition"):
        end = time.time() + timeout
        while time.time() < end:
            st = self.call("/api/state")
            if cond(st):
                return st
            time.sleep(0.25)
        raise AssertionError(f"timed out waiting for {what}; log:\n{self.logtext()}")

    def exited(self, timeout):
        try:
            self.proc.wait(timeout)
            return True
        except subprocess.TimeoutExpired:
            return False

    def stop(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        self.log.close()


class CloseWindow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = Server(size_mb=6, rate_kb=300)      # ~5 s with 4 connections

    @classmethod
    def tearDownClass(cls):
        cls.srv.close()

    def test_old_behaviour_when_turned_off(self):
        """Setting off: closing the window quits after the grace time, and the
        running download is saved to continue next time."""
        app = Running("--watchdog", settings={"close_to_tray": False})
        try:
            w = app.window()
            id = app.call("/api/add", {"url": self.srv.url("/off.bin"), "dir": app.downloads,
                                       "connections": 2})["ids"][0]
            app.wait(lambda s: s["items"][0]["status"] == "downloading", what="download start")
            w.set()                                   # close the window
            self.assertTrue(app.exited(20), "app should quit once the window is closed")
            with open(os.path.join(app.data, "downloads.json"), encoding="utf-8") as f:
                saved = json.load(f)
            item = next(i for i in saved["items"] if i["id"] == id)
            self.assertTrue(item["resume_on_start"], item)
            self.assertFalse(os.path.exists(os.path.join(app.data, "running.json")))
            again = subprocess.Popen([sys.executable, os.path.join(ROOT, "fastgrab_app.py"), "--no-window",
                                      "--port", "0"], env=app.env, cwd=ROOT,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                path = os.path.join(app.downloads, "off.bin")
                for _ in range(600):
                    if os.path.exists(path):
                        break
                    time.sleep(0.1)
                import hashlib
                with open(path, "rb") as f:
                    self.assertEqual(hashlib.sha256(f.read()).hexdigest(), self.srv.sha,
                                     "continued by itself on the next start")
            finally:
                again.kill()
                again.wait()
        finally:
            app.stop()

    @unittest.skipIf(WIN, "Windows uses the tray icon instead (WindowsTray)")
    def test_keeps_downloading_then_quits_without_tray(self):
        """No tray icon (macOS/Linux): closing the window doesn't stop the download;
        the app quits by itself once the queue is done."""
        app = Running("--watchdog")
        try:
            w = app.window()
            st = app.call("/api/state", client="w1")
            self.assertTrue(st["settings"]["close_to_tray"], "on by default")
            self.assertFalse(st["tray"])
            app.call("/api/add", {"url": self.srv.url("/bg.bin"), "dir": app.downloads, "connections": 4})
            app.wait(lambda s: s["items"][0]["status"] == "downloading", what="download start")
            w.set()
            time.sleep(4)                             # past the grace time
            self.assertIsNone(app.proc.poll(), "must keep running while downloading:\n" + app.logtext())
            self.assertTrue(app.exited(60), "should quit after the download finished")
            with open(os.path.join(app.data, "downloads.json"), encoding="utf-8") as f:
                saved = json.load(f)
            self.assertEqual(saved["items"][0]["status"], "completed")
            with open(os.path.join(app.downloads, "bg.bin"), "rb") as f:
                import hashlib
                self.assertEqual(hashlib.sha256(f.read()).hexdigest(), self.srv.sha)
        finally:
            app.stop()

    def test_stays_while_window_open(self):
        app = Running("--watchdog")
        try:
            w = app.window()
            time.sleep(4)
            self.assertIsNone(app.proc.poll())
            w.set()
            if not WIN:
                self.assertTrue(app.exited(20), "nothing to do and no tray: should quit")
        finally:
            app.stop()

    def test_one_window_at_a_time(self):
        """Opening FastGrab again (tray click / Start menu) opens a new window and the
        old one closes itself once the new one is up."""
        app = Running()
        try:
            old = app.window("old")
            time.sleep(1.2)
            app.call("/api/show-window", {})
            with open(os.path.join(app.data, "opened-windows.log")) as f:
                self.assertIn(app.token, f.read())
            self.assertFalse(app.call("/api/state", client="old")["close_window"],
                             "the old window stays until the new one is really there")
            self.assertFalse(app.call("/api/state", client="new")["close_window"])
            self.assertTrue(app.call("/api/state", client="old")["close_window"])
            self.assertFalse(app.call("/api/state", client="new")["close_window"])
            old.set()
            app.call("/api/show-window", {})          # within 2 s: a double click, ignored
            with open(os.path.join(app.data, "opened-windows.log")) as f:
                self.assertEqual(len(f.read().splitlines()), 1)
            # a page reload (new id) without a show request replaces nothing
            self.assertFalse(app.call("/api/state", client="reloaded")["close_window"])
            self.assertFalse(app.call("/api/state", client="new")["close_window"])
        finally:
            app.stop()

    def test_second_launch_shows_existing(self):
        """Starting FastGrab while it runs (in the tray) asks it to show its window
        instead of starting a second copy."""
        app = Running()
        try:
            r = subprocess.run([sys.executable, os.path.join(ROOT, "fastgrab_app.py")],
                               env=app.env, cwd=ROOT, capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 0, r.stderr)
            with open(os.path.join(app.data, "opened-windows.log")) as f:
                self.assertIn(app.token, f.read())
            with open(os.path.join(app.data, "running.json")) as f:
                self.assertEqual(json.load(f)["pid"], app.proc.pid, "no second copy")
        finally:
            app.stop()

    def test_pause_all_resume_all(self):
        app = Running()
        try:
            app.call("/api/settings", {"max_active": 2})
            ids = [app.call("/api/add", {"url": self.srv.url(f"/pa{i}.bin"), "dir": app.downloads,
                                         "connections": 2})["ids"][0] for i in range(4)]
            app.wait(lambda s: sum(i["status"] == "downloading" for i in s["items"]) == 2,
                     what="two downloading")
            app.call("/api/tray", {"action": "pause-all"})
            st = app.wait(lambda s: all(i["status"] == "paused" for i in s["items"]), what="all paused")
            self.assertEqual(len(st["items"]), 4)
            time.sleep(1)
            self.assertTrue(all(i["status"] == "paused" for i in app.call("/api/state")["items"]),
                            "a paused queue must not start by itself")
            app.call("/api/tray", {"action": "resume-all"})
            app.wait(lambda s: all(i["status"] == "completed" for i in s["items"]), timeout=120,
                     what="all completed")
            for i in range(4):
                self.assertTrue(os.path.exists(os.path.join(app.downloads, f"pa{i}.bin")))
            self.assertEqual(len(ids), 4)
        finally:
            app.stop()

    def test_settings_saved(self):
        app = Running()
        try:
            s = app.call("/api/settings", {"close_to_tray": False, "keep_awake": False})
            self.assertFalse(s["close_to_tray"])
            self.assertFalse(s["keep_awake"])
            with open(os.path.join(app.data, "downloads.json"), encoding="utf-8") as f:
                saved = json.load(f)["settings"]
            self.assertFalse(saved["close_to_tray"])
            self.assertFalse(saved["keep_awake"])
            st = app.call("/api/state")
            self.assertEqual(st["windows"], WIN)
            self.assertFalse(st["awake"])
        finally:
            app.stop()


@unittest.skipUnless(WIN, "Windows tray icon")
class WindowsTray(unittest.TestCase):
    """The real icon, driven with the same Windows messages a mouse click sends."""

    def test_icon_in_process(self):
        calls = []
        done = threading.Event()

        def record(name):
            def f():
                calls.append(name)
                done.set()
            return f
        texts = lambda: {**fastgrab_app.TRAY_TEXT["he"], "rtl": True, "can_pause": True, "can_resume": False}
        tray = fastgrab_app.Tray(os.path.join(ROOT, "ui", "fastgrab.ico"), record("open"), record("pause"),
                                 record("resume"), record("quit"), texts)
        self.assertTrue(tray.start(), f"icon not added: {tray.last_error}")
        try:
            self.assertTrue(tray.own_icon, "FastGrab's own icon should load")
            u32 = fastgrab_app.user32
            for event, cmd, name in ((0x0202, None, "open"),            # left click
                                     (0x0405, None, "open"),            # clicked the notification
                                     (None, fastgrab_app.CMD_PAUSE_ALL, "pause"),
                                     (None, fastgrab_app.CMD_RESUME_ALL, "resume"),
                                     (None, fastgrab_app.CMD_QUIT, "quit")):
                done.clear()
                if event:
                    u32.PostMessageW(tray.hwnd, fastgrab_app.WM_TRAY, 1, event)
                else:
                    u32.PostMessageW(tray.hwnd, fastgrab_app.WM_COMMAND, cmd, 0)
                self.assertTrue(done.wait(5), name)
                self.assertEqual(calls[-1], name)
            tray.update("FastGrab - 1 downloading · 2.0 MB/s")
            self.assertEqual(tray.tip, "FastGrab - 1 downloading · 2.0 MB/s")
            self.assertTrue(tray.notify("Download complete", "file.bin"))
            rows = tray.menu_items()
            self.assertEqual([r[0] for r in rows if r], [1001, 1002, 1003, 1004])
            self.assertEqual(rows[3][2], False, "Resume all greyed out when nothing is paused")
            # Explorer restarted: the icon must come back
            u32.PostMessageW(tray.hwnd, tray.taskbar_created, 0, 0)
            time.sleep(0.5)
            self.assertTrue(tray.shown)
            self.assertIsNone(tray.last_error)
        finally:
            tray.stop()
        self.assertFalse(tray.thread.is_alive(), "message loop should end")
        self.assertFalse(tray.shown)
        tray.stop()                                   # twice is fine

    def test_power_helpers(self):
        self.assertTrue(fastgrab_app.no_power_throttling())
        t = threading.Thread(target=lambda: (fastgrab_app.keep_awake(True), fastgrab_app.keep_awake(False)))
        t.start()
        t.join()
        self.assertTrue(fastgrab_app.keep_awake(False))

    def test_app_with_tray(self):
        """App with its tray icon: closing the window keeps it running, the menu's
        Quit exits and removes the icon."""
        srv = Server(size_mb=4, rate_kb=300)
        app = Running("--watchdog", "--tray")
        try:
            w = app.window()
            st = app.wait(lambda s: s["tray"], timeout=20, what="tray icon")
            app.call("/api/add", {"url": srv.url("/t.bin"), "dir": app.downloads, "connections": 2})
            app.wait(lambda s: s["awake"], what="keep-awake while downloading")
            w.set()                                   # close the window
            app.wait(lambda s: s["items"][0]["status"] == "completed", what="download done")
            time.sleep(3)                             # past the grace time
            self.assertIsNone(app.proc.poll(), "must stay in the tray:\n" + app.logtext())
            st = app.wait(lambda s: not s["awake"], what="sleep allowed again")
            with open(os.path.join(app.data, "downloads.json"), encoding="utf-8") as f:
                self.assertTrue(json.load(f)["settings"]["tray_hint_shown"], "hint shown once")
            hwnd = fastgrab_app.user32.FindWindowW(f"FastGrabTray{app.proc.pid}", None)
            self.assertTrue(hwnd, "tray window not found")
            fastgrab_app.user32.PostMessageW(hwnd, fastgrab_app.WM_COMMAND, fastgrab_app.CMD_QUIT, 0)
            self.assertTrue(app.exited(30), "Quit from the tray menu should exit")
            self.assertEqual(app.proc.returncode, 0)
            self.assertFalse(os.path.exists(os.path.join(app.data, "running.json")))
            self.assertIsNotNone(st)
        finally:
            app.stop()
            srv.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
