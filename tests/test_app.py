"""
Tests for the FastGrab desktop app's backend (the local API the window talks to).
Runs the app without opening a window. Set FASTGRAB_APP_CMD to test a built .exe.
"""
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_fastgrab import ROOT, Server, sha256  # noqa: E402

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class AppBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        env = dict(os.environ, HOME=cls.tmp, USERPROFILE=cls.tmp, APPDATA=cls.tmp)
        cmd = os.environ.get("FASTGRAB_APP_CMD")
        cmd = shlex.split(cmd, posix=os.name != "nt") if cmd else [sys.executable, os.path.join(ROOT, "fastgrab_app.py")]
        cls.log = open(os.path.join(cls.tmp, "app.log"), "w+")
        cls.proc = subprocess.Popen(cmd + ["--no-window"], env=env, cwd=ROOT,
                                    stdout=cls.log, stderr=subprocess.STDOUT)
        data = os.path.join(cls.tmp, "FastGrab") if os.name == "nt" else os.path.join(cls.tmp, ".config", "fastgrab")
        info = os.path.join(data, "running.json")
        r = None
        for _ in range(300):
            if cls.proc.poll() is not None:
                break
            if os.path.exists(info):
                try:
                    with open(info) as f:
                        r = json.load(f)
                    break
                except ValueError:
                    pass
            time.sleep(0.1)
        if r is None:
            cls.proc.kill()
            cls.log.seek(0)
            raise RuntimeError(f"app did not start (looked for {info}):\n{cls.log.read()[-3000:]}")
        cls.base = f"http://127.0.0.1:{r['port']}"
        cls.token = r["token"]
        cls.downloads = os.path.join(cls.tmp, "Downloads")
        os.makedirs(cls.downloads, exist_ok=True)
        cls.srv = Server(size_mb=12, rate_kb=1024)

    @classmethod
    def tearDownClass(cls):
        cls.proc.kill()
        cls.proc.wait()
        cls.log.close()
        cls.srv.close()

    def call(self, path, body=None, token=True, host=None):
        h = {"X-Token": self.token} if token else {}
        if host:
            h["Host"] = host
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, headers=h)
        try:
            with OPENER.open(req, timeout=15) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            e.msg = f"{e.msg}: {e.read().decode(errors='replace')}"
            raise

    def item(self, id):
        return next(i for i in self.call("/api/state")["items"] if i["id"] == id)

    def wait_status(self, id, status, timeout=60):
        end = time.time() + timeout
        while time.time() < end:
            it = self.item(id)
            if it["status"] == status:
                return it
            time.sleep(0.2)
        self.fail(f"never reached {status}: {it}")


class AppTest(AppBase):
    def test_1_download_pause_resume(self):
        id = self.call("/api/add", {"url": self.srv.url(), "dir": self.downloads, "connections": 8})["ids"][0]
        it = self.wait_status(id, "downloading")
        time.sleep(1.5)
        it = self.item(id)
        self.assertGreaterEqual(len(it["segments"]), 4, "segments should be visible to the UI")
        self.call("/api/action", {"id": id, "action": "pause"})
        it = self.wait_status(id, "paused")
        self.assertTrue(0 < it["downloaded"] < it["size"])
        self.call("/api/action", {"id": id, "action": "resume"})
        it = self.wait_status(id, "completed")
        self.assertEqual(sha256(it["path"]), self.srv.sha)

    def test_2_same_name_twice_gets_unique_files(self):
        ids = self.call("/api/add", {"urls": [self.srv.url("/same.bin"), self.srv.url("/same.bin")],
                                     "dir": self.downloads})["ids"]
        paths = [self.wait_status(i, "completed")["path"] for i in ids]
        self.assertEqual(len(set(paths)), 2)
        for p in paths:
            self.assertEqual(sha256(p), self.srv.sha)

    def test_3_cancel_deletes_only_partial_files(self):
        id = self.call("/api/add", {"url": self.srv.url("/cancel-me.bin"), "dir": self.downloads})["ids"][0]
        self.wait_status(id, "downloading")
        time.sleep(1)
        self.call("/api/action", {"id": id, "action": "cancel"})
        self.assertFalse(any(i["id"] == id for i in self.call("/api/state")["items"]))
        left = [f for f in os.listdir(self.downloads) if f.startswith("cancel-me")]
        self.assertEqual(left, [])

    def test_4_rejects_bad_links(self):
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.call("/api/add", {"url": "file:///etc/passwd"})
        self.assertEqual(e.exception.code, 400)

    def test_5_api_is_protected(self):
        for kw in ({"token": False}, {"host": "evil.example"}):
            with self.assertRaises(urllib.error.HTTPError) as e:
                self.call("/api/state", **kw)
            self.assertEqual(e.exception.code, 403)


try:
    import yt_dlp  # noqa: F401
    HAVE_VIDEO = True
except ImportError:
    HAVE_VIDEO = False


@unittest.skipUnless(HAVE_VIDEO or os.environ.get("FASTGRAB_APP_CMD"), "yt-dlp not installed")
class VideoTest(AppBase):
    """Video mode, using yt-dlp's generic extractor on a local page with a <video>."""

    def test_6_probe_and_download_video(self):
        info = self.call("/api/probe", {"url": self.srv.url("/watch")})
        self.assertIn("best", info["choices"])
        self.assertEqual(info["title"].split(" (")[0], "Test Clip")
        id = self.call("/api/add", {"url": self.srv.url("/watch?v=1"), "dir": self.downloads,
                                    "kind": "video", "choice": "best"})["ids"][0]
        self.wait_status(id, "downloading")
        time.sleep(1.5)
        self.call("/api/action", {"id": id, "action": "pause"})
        it = self.wait_status(id, "paused")
        self.assertTrue(0 < it["downloaded"] < it["size"], it)
        self.call("/api/action", {"id": id, "action": "resume"})
        it = self.wait_status(id, "completed", timeout=90)
        self.assertEqual(it["kind"], "video")
        self.assertEqual(sha256(it["path"]), self.srv.sha)

    def test_7_web_page_link_falls_back_to_video(self):
        id = self.call("/api/add", {"url": self.srv.url("/watch?v=2"), "dir": self.downloads})["ids"][0]
        it = self.wait_status(id, "completed", timeout=90)
        self.assertEqual(it["kind"], "video")
        self.assertEqual(sha256(it["path"]), self.srv.sha)

    def test_8_cancel_video_removes_temp_files(self):
        id = self.call("/api/add", {"url": self.srv.url("/watch?v=3"), "dir": self.downloads,
                                    "kind": "video"})["ids"][0]
        self.wait_status(id, "downloading")
        time.sleep(1.5)
        self.call("/api/action", {"id": id, "action": "cancel"})
        time.sleep(0.5)
        left = [f for f in os.listdir(self.downloads) if "watch-3" in f]
        self.assertEqual(left, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
