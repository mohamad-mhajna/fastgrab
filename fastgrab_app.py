#!/usr/bin/env python3
"""
FastGrab desktop app: a Prism-style window on top of the fastgrab engine.

    python fastgrab_app.py

How it works (same architecture as Prism, but zero dependencies):
  * the UI is a single HTML page (ui/index.html) shown in its own app window
    (Edge or Chrome in "--app" mode, which every Windows PC already has)
  * this script runs a small local HTTP API on 127.0.0.1 that the page talks to
  * the API is protected by a random per-run token and a Host check, so websites
    you visit can't control your downloads
  * downloads that were running when you closed the window resume on next start
"""
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import fastgrab
import fastgrab_video

APP_NAME = "FastGrab"
MAX_ACTIVE = 3                      # downloads running at the same time; the rest queue


def resource_dir():
    """Folder with ui/ (works from source and from a PyInstaller .exe)."""
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def data_dir():
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        d = os.path.join(base, "FastGrab")
    else:
        d = os.path.join(os.path.expanduser("~"), ".config", "fastgrab")
    os.makedirs(d, exist_ok=True)
    return d


def default_download_dir():
    d = os.path.join(os.path.expanduser("~"), "Downloads")
    return d if os.path.isdir(d) else os.path.expanduser("~")


# --------------------------------------------------------------------- items --

class Item:
    """One row in the downloads list."""

    def __init__(self, url, out_dir, connections, id=None, **saved):
        self.id = id or uuid.uuid4().hex[:12]
        self.url = url
        self.out_dir = out_dir
        self.connections = connections
        self.added = saved.get("added", time.time())
        self.finished = saved.get("finished")
        self.dl = None              # fastgrab.Download while running
        self.thread = None
        # last known values, used when no Download object is alive
        self.status = saved.get("status", "queued")
        self.filename = saved.get("filename")
        self.size = saved.get("size")
        self.downloaded = saved.get("downloaded", 0)
        self.path = saved.get("path")
        self.message = saved.get("message", "")
        self.resumable = saved.get("resumable")
        self.resume_on_start = saved.get("resume_on_start", False)
        self.kind = saved.get("kind", "file")          # "file" or "video"
        self.choice = saved.get("choice", "best")       # video quality
        self.site = saved.get("site")
        self.thumbnail = saved.get("thumbnail")
        self.parts = saved.get("parts", [])             # video temp files (for Cancel)

    def sync_from_dl(self):
        if self.dl is None:
            return
        s = self.dl.snapshot()
        self.status = s["status"]
        self.message = s["message"]
        for k in ("filename", "size", "path", "resumable"):
            if s[k] is not None:
                setattr(self, k, s[k])
        self.downloaded = s["downloaded"]
        if self.kind == "video":
            self.parts = sorted(set(self.parts) | self.dl.parts)

    def to_saved(self):
        return {"id": self.id, "url": self.url, "out_dir": self.out_dir,
                "connections": self.connections, "added": self.added,
                "finished": self.finished, "status": self.status,
                "filename": self.filename, "size": self.size,
                "downloaded": self.downloaded, "path": self.path,
                "message": self.message, "resumable": self.resumable,
                "resume_on_start": self.resume_on_start, "kind": self.kind,
                "choice": self.choice, "site": self.site, "thumbnail": self.thumbnail,
                "parts": self.parts}

    def view(self):
        """JSON for the UI."""
        if self.dl is not None:
            s = self.dl.snapshot()
            if s["filename"] is None:
                s["filename"] = self.filename or guess_name(self.url)
        else:
            s = {"status": self.status, "message": self.message,
                 "filename": self.filename or guess_name(self.url), "size": self.size,
                 "downloaded": self.downloaded, "speed": 0, "eta": None, "active": 0,
                 "connections": self.connections, "resumable": self.resumable,
                 "path": self.path, "segments": []}
        s.update(id=self.id, url=self.url, out_dir=self.out_dir,
                 added=self.added, finished=self.finished, kind=self.kind,
                 choice=self.choice, site=self.site, thumbnail=self.thumbnail)
        return s


def guess_name(url):
    name = os.path.basename(urllib.parse.urlparse(url).path)
    return urllib.parse.unquote(name) or url


# ------------------------------------------------------------------- manager --

class Manager:
    def __init__(self, store_path):
        self.store_path = store_path
        self.lock = threading.RLock()
        self.items = {}
        self.settings = {"dir": default_download_dir(), "connections": 8}
        self.load()

    # -- persistence -------------------------------------------------------------
    def load(self):
        try:
            with open(self.store_path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        self.settings.update(data.get("settings", {}))
        for d in data.get("items", []):
            it = Item(**d)
            if it.status in ("downloading", "connecting", "queued"):
                it.resume_on_start = True       # app was closed mid-download
                it.status = "paused"
            self.items[it.id] = it

    def save(self):
        with self.lock:
            for it in self.items.values():
                it.sync_from_dl()
            data = {"settings": self.settings,
                    "items": [it.to_saved() for it in self.items.values()]}
        tmp = self.store_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, self.store_path)

    def resume_interrupted(self):
        for it in list(self.items.values()):
            if it.resume_on_start:
                it.resume_on_start = False
                self.resume(it.id)

    # -- actions -------------------------------------------------------------------
    def add(self, url, out_dir=None, connections=None, kind=None, choice=None, meta=None):
        url = (url or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            raise ValueError("Only http:// and https:// links are supported")
        out_dir = out_dir or self.settings["dir"]
        connections = int(connections or self.settings["connections"])
        connections = max(1, min(32, connections))
        os.makedirs(out_dir, exist_ok=True)
        it = Item(url, out_dir, connections)
        if kind == "video" or (kind is None and fastgrab_video.is_video_url(url)):
            it.kind = "video"
            it.choice = choice if choice in fastgrab_video.QUALITIES else "best"
            meta = meta or {}
            it.filename = meta.get("title")
            it.site = meta.get("site")
            it.thumbnail = meta.get("thumbnail")
        with self.lock:
            self.items[it.id] = it
        self.schedule()
        self.save()
        return it.id

    def unique_name(self, it, name):
        """Pick a name no other download in the same folder uses and no file has."""
        base, ext = os.path.splitext(name)
        with self.lock:
            taken = {o.filename for o in self.items.values()
                     if o is not it and o.out_dir == it.out_dir and o.status != "completed"}
            cand, n = name, 1
            while cand in taken or os.path.exists(os.path.join(it.out_dir, cand)):
                cand = f"{base} ({n}){ext}"
                n += 1
            it.filename = cand
        return cand

    def _start(self, it):
        it.status = "connecting"
        if it.kind == "video":
            it.dl = fastgrab_video.VideoDownload(it.url, it.out_dir, it.choice, it.connections)
        elif it.filename and it.downloaded:     # resuming: keep the same name and .fgpart
            it.dl = fastgrab.Download(it.url, it.out_dir, it.connections, quiet=True,
                                      filename=it.filename)
        else:
            it.dl = fastgrab.Download(it.url, it.out_dir, it.connections, quiet=True,
                                      name_hook=lambda n, it=it: self.unique_name(it, n))

        def run():
            try:
                it.dl.start()
            except fastgrab.WebPageError:
                if fastgrab_video.AVAILABLE:   # a page, not a file: try it as a video page
                    with self.lock:
                        it.kind, it.choice, it.filename = "video", "best", None
                        it.dl = fastgrab_video.VideoDownload(it.url, it.out_dir, "best",
                                                             it.connections)
                    it.dl.start()
                else:
                    it.dl.status = "failed"
                    it.dl.message = ("This link opens a web page, not a file. Install video "
                                     "support (top bar) to download videos from websites.")
            except Exception as e:           # probe failed, bad URL, disk error...
                it.dl.status = "failed"
                it.dl.message = str(e) or e.__class__.__name__
            with self.lock:
                it.sync_from_dl()
                if it.status == "completed":
                    it.finished = time.time()
                    if it.kind == "video":
                        it.parts = []
                it.dl = None
            self.save()
            self.schedule()

        it.thread = threading.Thread(target=run, daemon=True)
        it.thread.start()

    def schedule(self):
        """Start queued items while fewer than MAX_ACTIVE are running."""
        with self.lock:
            running = sum(1 for i in self.items.values() if i.dl is not None)
            for it in sorted(self.items.values(), key=lambda i: i.added):
                if running >= MAX_ACTIVE:
                    break
                if it.status == "queued" and it.dl is None:
                    self._start(it)
                    running += 1

    def pause(self, id, wait=True):
        it = self.items[id]
        if it.dl is not None:
            it.dl.stop.set()
            if wait and it.thread:
                it.thread.join(timeout=30)
        elif it.status == "queued":
            it.status = "paused"
        self.save()

    def resume(self, id):
        it = self.items[id]
        if it.dl is not None and it.dl.stop.is_set() and it.thread:
            it.thread.join(timeout=30)        # a pause is still finishing: let it end first
        if it.dl is None and it.status in ("paused", "failed"):
            it.status = "queued"
            it.message = ""
            self.schedule()
            self.save()

    def cancel(self, id):
        """Stop and delete the unfinished file (only files fastgrab created)."""
        it = self.items[id]
        self.pause(id)
        if it.kind == "video" and it.status != "completed":
            v = fastgrab_video.VideoDownload(it.url, it.out_dir)
            v.parts = set(it.parts)
            v.cleanup()
        elif it.status != "completed" and it.filename:
            part = os.path.join(it.out_dir, it.filename) + ".fgpart"
            for p in (part, part + ".json", part + ".json.tmp"):
                if os.path.exists(p):
                    os.remove(p)
        with self.lock:
            del self.items[id]
        self.save()

    def remove(self, id):
        """Remove from the list. A finished file on disk is kept."""
        it = self.items[id]
        if it.status != "completed":
            return self.cancel(id)
        with self.lock:
            del self.items[id]
        self.save()

    def clear_completed(self):
        with self.lock:
            for id in [i.id for i in self.items.values() if i.status == "completed"]:
                del self.items[id]
        self.save()

    def pause_all(self):
        for it in list(self.items.values()):
            if it.dl is not None:
                it.dl.stop.set()
        for it in list(self.items.values()):
            if it.thread:
                it.thread.join(timeout=30)

    def state(self):
        with self.lock:
            items = [it.view() for it in self.items.values()]
        items.sort(key=lambda v: v["added"], reverse=True)
        return {"items": items, "settings": self.settings, "version": fastgrab.__version__,
                "video": {**fastgrab_video.status(), **VIDEO_INSTALL}}


# --------------------------------------------------------------- video setup --

VIDEO_INSTALL = {"installing": False, "install_error": None}


def install_video_support():
    """pip-install yt-dlp (+ FFmpeg) for this Python. Only when running from source."""
    if getattr(sys, "frozen", False):
        VIDEO_INSTALL["install_error"] = "Video support is built into the .exe version."
        return
    VIDEO_INSTALL.update(installing=True, install_error=None)
    try:
        flags = 0x08000000 if os.name == "nt" else 0
        r = subprocess.run([sys.executable, "-m", "pip", "install", "--user", "--upgrade",
                            "yt-dlp[default]", "imageio-ffmpeg"],
                           capture_output=True, text=True, timeout=600, creationflags=flags)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).strip().splitlines()[-1])
        import importlib
        import site
        site.addsitedir(site.getusersitepackages())
        importlib.invalidate_caches()
        importlib.reload(fastgrab_video)
        fastgrab_video.tools(refresh=True)
        if not fastgrab_video.AVAILABLE:
            raise RuntimeError("Installed, but please restart FastGrab to finish.")
    except Exception as e:
        VIDEO_INSTALL["install_error"] = str(e)[:300]
    finally:
        VIDEO_INSTALL["installing"] = False


# ------------------------------------------------------------------ desktop --

def open_path(path, select=False):
    if not path or not os.path.exists(path):
        raise FileNotFoundError(path)
    if os.name == "nt":
        if select:
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        else:
            os.startfile(path)  # noqa: S606 - opens with the user's default app
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path] if select else ["open", path])
    else:
        subprocess.Popen(["xdg-open", os.path.dirname(path) if select else path])


_pick_lock = threading.Lock()


def pick_folder(initial):
    """Native folder dialog via tkinter (bundled with Python on Windows/macOS)."""
    with _pick_lock:
        try:
            import tkinter
            from tkinter import filedialog
        except ImportError:
            return None
        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            chosen = filedialog.askdirectory(initialdir=initial, title="Save downloads to")
        finally:
            root.destroy()
        return chosen or None


def find_app_browser():
    """Edge or Chrome, which can show a page as a standalone app window."""
    cands = []
    if os.name == "nt":
        for base in (os.environ.get("PROGRAMFILES(X86)"), os.environ.get("PROGRAMFILES"),
                     os.environ.get("LOCALAPPDATA")):
            if base:
                cands += [os.path.join(base, r"Microsoft\Edge\Application\msedge.exe"),
                          os.path.join(base, r"Google\Chrome\Application\chrome.exe")]
    elif sys.platform == "darwin":
        cands += ["/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    for name in ("microsoft-edge", "google-chrome", "chromium", "chromium-browser"):
        p = shutil.which(name)
        if p:
            cands.append(p)
    return next((c for c in cands if c and os.path.exists(c)), None)


def open_window(url):
    exe = find_app_browser()
    if exe:
        profile = os.path.join(data_dir(), "window-profile")
        subprocess.Popen([exe, f"--app={url}", "--window-size=1240,800",
                          f"--user-data-dir={profile}", "--no-first-run",
                          "--no-default-browser-check",
                          # keep the live view updating while minimized
                          "--disable-background-timer-throttling",
                          "--disable-renderer-backgrounding",
                          "--disable-backgrounding-occluded-windows"])
    else:
        import webbrowser
        webbrowser.open(url)


# ---------------------------------------------------------------------- http --

class App:
    def __init__(self, port=0):
        self.token = secrets.token_urlsafe(24)
        self.manager = Manager(os.path.join(data_dir(), "downloads.json"))
        self.last_seen = None
        with open(os.path.join(resource_dir(), "ui", "index.html"), encoding="utf-8") as f:
            self.page = f.read()
        app = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _host_ok(self):
                host = self.headers.get("Host", "")
                return host in (f"127.0.0.1:{app.port}", f"localhost:{app.port}")

            def _send(self, code, body, ctype="application/json"):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if not self._host_ok():
                    return self._send(403, {"error": "bad host"})
                u = urllib.parse.urlparse(self.path)
                q = urllib.parse.parse_qs(u.query)
                if u.path == "/":
                    if q.get("t", [""])[0] != app.token:
                        return self._send(403, b"Forbidden", "text/plain")
                    return self._send(200, app.page.encode("utf-8"), "text/html; charset=utf-8")
                if self.headers.get("X-Token") != app.token:
                    return self._send(403, {"error": "bad token"})
                if u.path == "/api/state":
                    app.last_seen = time.time()
                    return self._send(200, app.manager.state())
                if u.path == "/api/ping":
                    return self._send(200, {"ok": True})
                self._send(404, {"error": "not found"})

            def do_POST(self):
                if not self._host_ok() or self.headers.get("X-Token") != app.token:
                    return self._send(403, {"error": "forbidden"})
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    return self._send(400, {"error": "bad json"})
                m = app.manager
                try:
                    p = urllib.parse.urlparse(self.path).path
                    if p == "/api/add":
                        ids = [m.add(u, body.get("dir"), body.get("connections"),
                                     body.get("kind"), body.get("choice"), body.get("meta"))
                               for u in body.get("urls", [body.get("url")]) if u]
                        return self._send(200, {"ids": ids})
                    if p == "/api/probe":
                        url = (body.get("url") or "").strip()
                        if not url.lower().startswith(("http://", "https://")):
                            return self._send(400, {"error": "Not a link"})
                        return self._send(200, fastgrab_video.probe(url))
                    if p == "/api/install-video":
                        if not VIDEO_INSTALL["installing"]:
                            threading.Thread(target=install_video_support, daemon=True).start()
                        return self._send(200, {"ok": True})
                    if p == "/api/action":
                        id, act = body["id"], body["action"]
                        if act == "pause":
                            threading.Thread(target=m.pause, args=(id,), daemon=True).start()
                        elif act == "resume":
                            m.resume(id)
                        elif act == "cancel":
                            m.cancel(id)
                        elif act == "remove":
                            m.remove(id)
                        elif act in ("open", "folder"):
                            it = m.items[id]
                            open_path(it.path, select=(act == "folder"))
                        else:
                            return self._send(400, {"error": "unknown action"})
                        return self._send(200, {"ok": True})
                    if p == "/api/clear-completed":
                        m.clear_completed()
                        return self._send(200, {"ok": True})
                    if p == "/api/settings":
                        if "dir" in body and body["dir"]:
                            m.settings["dir"] = body["dir"]
                        if "connections" in body:
                            m.settings["connections"] = max(1, min(32, int(body["connections"])))
                        m.save()
                        return self._send(200, m.settings)
                    if p == "/api/pick-folder":
                        chosen = pick_folder(m.settings["dir"])
                        if chosen:
                            m.settings["dir"] = os.path.normpath(chosen)
                            m.save()
                        return self._send(200, {"dir": m.settings["dir"], "picked": bool(chosen)})
                    self._send(404, {"error": "not found"})
                except KeyError:
                    self._send(404, {"error": "no such download"})
                except Exception as e:
                    if sys.stderr:
                        import traceback
                        traceback.print_exc()
                    self._send(400, {"error": str(e) or e.__class__.__name__})

        ThreadingHTTPServer.daemon_threads = True
        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/?t={self.token}"

    def watchdog(self, grace=30):
        """Quit (after pausing downloads safely) once the window has been closed."""
        while True:
            time.sleep(1)
            if self.last_seen and time.time() - self.last_seen > grace:
                self.manager.pause_all()
                self.manager.save()
                try:
                    os.remove(os.path.join(data_dir(), "running.json"))
                except OSError:
                    pass
                os._exit(0)

    def serve(self, window=True):
        with open(os.path.join(data_dir(), "running.json"), "w") as f:
            json.dump({"port": self.port, "token": self.token, "pid": os.getpid()}, f)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.manager.resume_interrupted()
        self.manager.schedule()
        if window:
            open_window(self.url)
            threading.Thread(target=self.watchdog, daemon=True).start()
        if sys.stdout:                    # None in a windowed .exe
            print(f"{APP_NAME} is running at {self.url}", flush=True)
        try:
            while True:
                time.sleep(2)
                self.manager.save()
        except KeyboardInterrupt:
            self.manager.pause_all()
            self.manager.save()


def already_running():
    """If FastGrab is already open, return its URL instead of starting a second copy."""
    try:
        with open(os.path.join(data_dir(), "running.json")) as f:
            r = json.load(f)
        req = urllib.request.Request(f"http://127.0.0.1:{r['port']}/api/ping",
                                     headers={"X-Token": r["token"]})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        opener.open(req, timeout=1).read()
        return f"http://127.0.0.1:{r['port']}/?t={r['token']}"
    except Exception:
        return None


def main():
    window = "--no-window" not in sys.argv
    existing = already_running() if window else None
    if existing:
        open_window(existing)
        return
    App().serve(window=window)


if __name__ == "__main__":
    main()
