"""
Auto-update tests against a fake PyPI + GitHub server (nothing leaves the machine):
  * yt-dlp is downloaded, checksum-verified, unpacked into the addons folder and
    actually imported from there (also ahead of an installed copy)
  * a wrong checksum is rejected and nothing changes
  * a FastGrab source update replaces the files, keeps a backup, and the app
    restarts itself on the same port and token with the new version
"""
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def make_wheel(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return buf.getvalue()


class FakeServer:
    """Serves /pypi/<pkg>/json, /pypi/<pkg>/<ver>/json, /gh/... and /files/<name>."""

    def __init__(self):
        self.routes = {}
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = fake.routes.get(self.path.split("?")[0])
                if body is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                if not isinstance(body, bytes):
                    body = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def file(self, name, data):
        self.routes[f"/files/{name}"] = data
        return f"{self.base}/files/{name}"

    def pypi(self, pkg, version, wheel, sha=None, requires=()):
        url = self.file(f"{pkg}-{version}-py3-none-any.whl", wheel)
        j = {"info": {"version": version, "requires_dist": list(requires)},
             "urls": [{"packagetype": "bdist_wheel", "filename": f"{pkg}-{version}-py3-none-any.whl",
                       "url": url, "digests": {"sha256": sha or hashlib.sha256(wheel).hexdigest()}}]}
        self.routes[f"/pypi/{pkg}/json"] = j
        self.routes[f"/pypi/{pkg}/{version}/json"] = j

    def release(self, version, assets):
        sums = "".join(f"{hashlib.sha256(d).hexdigest()}  {n}\n" for n, d in assets.items())
        assets = {**assets, "SHA256SUMS.txt": sums.encode()}
        self.routes["/gh/repos/mohamadar8162-design/fastgrab/releases/latest"] = {
            "tag_name": f"v{version}", "html_url": f"{self.base}/release", "body": "notes",
            "assets": [{"name": n, "browser_download_url": self.file(n, d)} for n, d in assets.items()]}


FAKE_YTDLP = {"yt_dlp/__init__.py": "FAKE = True\n",
              "yt_dlp/utils.py": "class DownloadCancelled(Exception): pass\n",
              "yt_dlp/version.py": "__version__ = '2099.1.1'\n",
              "yt_dlp/sub.py": "WHERE = 'addon'\n"}


class VideoAddonUpdate(unittest.TestCase):
    def setUp(self):
        self.srv = FakeServer()
        self.home = tempfile.mkdtemp()
        self.env = dict(os.environ, HOME=self.home, USERPROFILE=self.home, APPDATA=self.home,
                        FASTGRAB_PYPI=self.srv.base + "/pypi")

    def run_py(self, code):
        r = subprocess.run([sys.executable, "-c", code], env=self.env, cwd=ROOT,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def test_installs_verifies_and_loads_newer_ytdlp(self):
        ejs = make_wheel({"yt_dlp_ejs/__init__.py": "V = '9.9'\n"})
        self.srv.pypi("yt-dlp-ejs", "9.9", ejs)
        self.srv.pypi("yt-dlp", "2099.1.1", make_wheel(FAKE_YTDLP),
                      requires=['yt-dlp-ejs==9.9; extra == "default"'])
        out = self.run_py("import fastgrab_update as u, json; print(json.dumps(u.update_video_support()))")
        self.assertEqual(json.loads(out), {"updated": True, "version": "2099.1.1"})
        # a fresh process imports yt_dlp (and its submodules) from the addon folder
        out = self.run_py("import fastgrab_video as v; import yt_dlp.sub as s, yt_dlp_ejs as e;"
                          "print(v.ADDONS['yt-dlp'], getattr(v.yt_dlp,'FAKE',False), s.WHERE, e.V)")
        self.assertEqual(out, "2099.1.1 True addon 9.9")
        # nothing newer on PyPI now: no second download
        out = self.run_py("import fastgrab_update as u, json; print(json.dumps(u.update_video_support()))")
        self.assertFalse(json.loads(out)["updated"])

    def test_broken_update_falls_back(self):
        try:
            import yt_dlp  # noqa: F401
        except ImportError:
            self.skipTest("needs a real yt-dlp installed to fall back to")
        self.srv.pypi("yt-dlp", "2099.1.1", make_wheel({"yt_dlp/__init__.py": "raise ImportError('broken build')\n"}))
        self.run_py("import fastgrab_update as u; u.update_video_support()")
        out = self.run_py("import fastgrab_video as v; print(v.AVAILABLE, v.ADDONS, v.version() != '2099.1.1')")
        self.assertEqual(out, "True {} True")

    def test_rejects_wrong_checksum(self):
        self.srv.pypi("yt-dlp", "2099.1.1", make_wheel(FAKE_YTDLP), sha="0" * 64)
        r = subprocess.run([sys.executable, "-c", "import fastgrab_update as u; u.update_video_support()"],
                           env=self.env, cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("checksum", r.stderr)
        out = self.run_py("import fastgrab_update as u; print(u._read_active())")
        self.assertEqual(out, "{}")


class AppSelfUpdate(unittest.TestCase):
    """Copy FastGrab into a temp folder, run it, publish a fake v9.9.9 and let the
    app update and restart itself; the same token must keep working."""

    def test_update_and_restart(self):
        srv = FakeServer()
        home = tempfile.mkdtemp()
        app = tempfile.mkdtemp()
        for rel in ("fastgrab.py", "fastgrab_app.py", "fastgrab_video.py", "fastgrab_update.py",
                    "ui/index.html"):
            os.makedirs(os.path.join(app, os.path.dirname(rel)), exist_ok=True)
            shutil.copy(os.path.join(ROOT, rel), os.path.join(app, rel))
        # the "new release": same files, but version 9.9.9
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for rel in ("fastgrab.py", "fastgrab_app.py", "fastgrab_video.py", "fastgrab_update.py",
                        "ui/index.html"):
                with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
                    text = fh.read()
                if rel == "fastgrab.py":
                    import re
                    text = re.sub(r'__version__ = "[^"]+"', '__version__ = "9.9.9"', text)
                z.writestr(rel, text)
        srv.release("9.9.9", {"fastgrab-source.zip": buf.getvalue()})

        env = dict(os.environ, HOME=home, USERPROFILE=home, APPDATA=home,
                   FASTGRAB_GITHUB_API=srv.base + "/gh", FASTGRAB_NO_AUTO_UPDATE="1")
        proc = subprocess.Popen([sys.executable, os.path.join(app, "fastgrab_app.py"), "--no-window",
                                 "--port", "0"], env=env, cwd=app,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        data = os.path.join(home, "FastGrab") if os.name == "nt" else os.path.join(home, ".config", "fastgrab")
        try:
            r = None
            for _ in range(900):
                try:
                    with open(os.path.join(data, "running.json")) as f:
                        r = json.load(f)
                    break
                except (OSError, ValueError):
                    time.sleep(0.1)
            base, tok = f"http://127.0.0.1:{r['port']}", r["token"]

            def call(path, body=None):
                req = urllib.request.Request(base + path, headers={"X-Token": tok, "Content-Type": "application/json"},
                                             data=None if body is None else json.dumps(body).encode())
                with OPENER.open(req, timeout=10) as resp:
                    return json.load(resp)

            def wait(cond, timeout=30):
                end = time.time() + timeout
                while time.time() < end:
                    try:
                        st = call("/api/state")
                        if cond(st):
                            return st
                    except OSError:
                        pass
                    time.sleep(0.3)
                self.fail("timed out")

            st = call("/api/state")
            old_version = st["version"]
            call("/api/update/check", {"video": False, "app": True})
            st = wait(lambda s: s["update"]["app"]["latest"] == "9.9.9" and not s["update"]["busy"])
            self.assertTrue(st["update"]["app"]["available"])
            call("/api/update/install", {})
            st = wait(lambda s: s["update"]["app"]["installed"] and not s["update"]["busy"])
            self.assertIsNone(st["update"]["error"])
            self.assertTrue(os.path.exists(os.path.join(app, ".previous", "fastgrab.py")), "backup kept")
            call("/api/restart", {})
            try:
                st = wait(lambda s: s["version"] == "9.9.9", timeout=60)   # same port + token
            except AssertionError:
                log = os.path.join(data, "restart.log")
                raise AssertionError("restart failed; restart.log:\n" +
                                     (open(log).read()[-3000:] if os.path.exists(log) else "(none)")) from None
            self.assertNotEqual(old_version, "9.9.9")
            with open(os.path.join(data, "running.json")) as f:
                new_pid = json.load(f)["pid"]
            self.assertNotEqual(new_pid, proc.pid)
        finally:
            proc.kill()
            try:
                with open(os.path.join(data, "running.json")) as f:
                    pid = json.load(f)["pid"]
                if pid != proc.pid:
                    os.kill(pid, 9)
            except Exception:
                pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
