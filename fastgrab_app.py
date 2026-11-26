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
  * the browser extension talks to /ext/* with its own pairing key
  * downloads that were running when you closed the window resume on next start
"""
import ctypes
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
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import fastgrab
import fastgrab_update
import fastgrab_video
from fastgrab_update import data_dir

APP_NAME = "FastGrab"
DEFAULT_PORT = 47821                # fixed, so the browser extension can find the app
LANGS = ("en", "ar", "he")
WHEN_DONE = ("none", "quit", "sleep", "shutdown")
POWER_COUNTDOWN = int(os.environ.get("FASTGRAB_POWER_COUNTDOWN", 60))  # seconds to cancel
NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def resource_dir():
    """Folder with ui/ (works from source and from a PyInstaller .exe)."""
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def app_dir():
    return os.path.dirname(os.path.abspath(__file__))


def default_download_dir():
    d = os.path.join(os.path.expanduser("~"), "Downloads")
    return d if os.path.isdir(d) else os.path.expanduser("~")


def hhmm(s, default):
    try:
        h, m = (int(x) for x in str(s).split(":"))
        if 0 <= h < 24 and 0 <= m < 60:
            return f"{h:02d}:{m:02d}"
    except (ValueError, TypeError):
        pass
    return default


def in_window(now, start, stop):
    """Is the clock time `now` (minutes since midnight) inside [start, stop)?
    Windows that cross midnight (e.g. 23:00-07:00) are supported; start == stop
    means all day."""
    s = int(start[:2]) * 60 + int(start[3:])
    e = int(stop[:2]) * 60 + int(stop[3:])
    if s == e:
        return True
    return s <= now < e if s < e else (now >= s or now < e)


def now_minutes():
    fake = os.environ.get("FASTGRAB_FAKE_CLOCK")       # tests: a file holding "HH:MM"
    if fake and os.path.exists(fake):
        t = open(fake).read().strip()
        return int(t[:2]) * 60 + int(t[3:5])
    n = datetime.now()
    return n.hour * 60 + n.minute


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
        self.dl = None              # fastgrab.Download / VideoDownload while running
        self.thread = None
        self.headers = {}           # from the browser extension (cookies etc.), never saved
        self.force = saved.get("force", False)        # "Start now": ignores the schedule
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
        self.source = saved.get("source", "app")        # "app" or "browser"

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
                "parts": self.parts, "force": self.force, "source": self.source}

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
                 choice=self.choice, site=self.site, thumbnail=self.thumbnail,
                 force=self.force, source=self.source)
        return s


def guess_name(url):
    name = os.path.basename(urllib.parse.urlparse(url).path)
    return urllib.parse.unquote(name) or url


# ------------------------------------------------------------------- manager --

DEFAULT_SETTINGS = {
    "dir": None, "connections": 8, "max_active": 3, "lang": "en",
    "schedule": {"enabled": False, "start": "02:00", "stop": "08:00"},
    "when_done": "none",
    "auto_update_video": True, "check_app_updates": True, "last_update_check": 0,
    "ext_key": None,
}


class Manager:
    def __init__(self, store_path):
        self.store_path = store_path
        self.lock = threading.RLock()
        self.items = {}
        self.settings = json.loads(json.dumps(DEFAULT_SETTINGS))
        self.settings["dir"] = default_download_dir()
        self.power = {"action": None, "at": None, "log": []}   # pending sleep/shutdown
        self._was_busy = False
        self._was_in_window = None
        self.events = []            # one-off messages for the UI (pending videos from the extension)
        self.load()
        if not self.settings.get("ext_key"):
            self.settings["ext_key"] = secrets.token_urlsafe(24)

    # -- persistence -------------------------------------------------------------
    def load(self):
        try:
            with open(self.store_path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        saved = data.get("settings", {})
        for k, v in saved.items():
            if k == "schedule" and isinstance(v, dict):
                self.settings["schedule"].update(v)
            elif k in DEFAULT_SETTINGS:
                self.settings[k] = v
        for d in data.get("items", []):
            it = Item(**d)
            if it.status in ("downloading", "connecting"):
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

    # -- settings ------------------------------------------------------------------
    def update_settings(self, body):
        s = self.settings
        if body.get("dir"):
            s["dir"] = os.path.normpath(str(body["dir"]))
        if "connections" in body:
            s["connections"] = max(1, min(32, int(body["connections"])))
        if "max_active" in body:
            s["max_active"] = max(1, min(10, int(body["max_active"])))
        if body.get("lang") in LANGS:
            s["lang"] = body["lang"]
        if isinstance(body.get("schedule"), dict):
            sc = body["schedule"]
            s["schedule"] = {"enabled": bool(sc.get("enabled", s["schedule"]["enabled"])),
                             "start": hhmm(sc.get("start", s["schedule"]["start"]), "02:00"),
                             "stop": hhmm(sc.get("stop", s["schedule"]["stop"]), "08:00")}
        if body.get("when_done") in WHEN_DONE:
            s["when_done"] = body["when_done"]
            if body["when_done"] == "none":
                self.cancel_power()
        for k in ("auto_update_video", "check_app_updates"):
            if k in body:
                s[k] = bool(body[k])
        if body.get("new_ext_key"):
            s["ext_key"] = secrets.token_urlsafe(24)
        self.save()
        self.schedule()
        return s

    # -- actions -------------------------------------------------------------------
    def add(self, url, out_dir=None, connections=None, kind=None, choice=None, meta=None,
            headers=None, source="app"):
        url = (url or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            raise ValueError("Only http:// and https:// links are supported")
        out_dir = out_dir or self.settings["dir"]
        connections = int(connections or self.settings["connections"])
        connections = max(1, min(32, connections))
        os.makedirs(out_dir, exist_ok=True)
        it = Item(url, out_dir, connections, source=source)
        it.headers = {k: v for k, v in (headers or {}).items()
                      if k in ("Cookie", "Referer", "User-Agent") and isinstance(v, str)}
        meta = meta or {}
        if kind == "video" or (kind is None and fastgrab_video.is_video_url(url)):
            it.kind = "video"
            it.choice = choice if choice in fastgrab_video.QUALITIES else "best"
            it.filename = meta.get("title")
            it.site = meta.get("site")
            it.thumbnail = meta.get("thumbnail")
        elif meta.get("filename"):
            it.filename = None          # the server's name wins; kept only as a display hint
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
        it.message = ""
        if it.kind == "video":
            it.dl = fastgrab_video.VideoDownload(it.url, it.out_dir, it.choice, it.connections)
        elif it.filename and it.downloaded:     # resuming: keep the same name and .fgpart
            it.dl = fastgrab.Download(it.url, it.out_dir, it.connections, headers=it.headers,
                                      quiet=True, filename=it.filename)
        else:
            it.dl = fastgrab.Download(it.url, it.out_dir, it.connections, headers=it.headers,
                                      quiet=True,
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
                    it.dl.message = "webpage"
            except Exception as e:           # probe failed, bad URL, disk error...
                it.dl.status = "failed"
                it.dl.message = str(e) or e.__class__.__name__
            with self.lock:
                it.sync_from_dl()
                if it.status == "completed":
                    it.finished = time.time()
                    it.force = False
                    if it.kind == "video":
                        it.parts = []
                it.dl = None
            self.save()
            self.schedule()

        it.thread = threading.Thread(target=run, daemon=True)
        it.thread.start()

    def schedule_open(self):
        sc = self.settings["schedule"]
        return not sc["enabled"] or in_window(now_minutes(), sc["start"], sc["stop"])

    def schedule(self):
        """Start queued items while fewer than max_active are running (and the
        download schedule, if any, allows it)."""
        open_now = self.schedule_open()
        with self.lock:
            running = sum(1 for i in self.items.values() if i.dl is not None)
            for it in sorted(self.items.values(), key=lambda i: i.added):
                if running >= self.settings["max_active"]:
                    break
                if it.status == "queued" and it.dl is None and (open_now or it.force):
                    self._start(it)
                    running += 1

    def tick(self):
        """Called every couple of seconds: schedule window edges and when-done actions."""
        sc = self.settings["schedule"]
        open_now = self.schedule_open()
        if sc["enabled"] and self._was_in_window and not open_now:
            # the window just closed: pause scheduled downloads, they continue next time
            for it in list(self.items.values()):
                if it.dl is not None and not it.force:
                    it.dl.stop.set()
                    if it.thread:
                        it.thread.join(timeout=30)
                    with self.lock:
                        if it.status == "paused":
                            it.status = "queued"
            self.save()
        self._was_in_window = open_now
        self.schedule()

        with self.lock:
            busy = any(i.status in ("downloading", "connecting") or i.dl is not None or
                       (i.status == "queued" and (open_now or i.force))
                       for i in self.items.values())
            waiting = any(i.status == "queued" for i in self.items.values())
        action = self.settings["when_done"]
        if action != "none" and self._was_busy and not busy and not waiting \
                and not self.power["action"]:
            self.power.update(action=action, at=time.time() + POWER_COUNTDOWN)
        if busy and self.power["action"]:
            self.cancel_power()                 # something new started: don't shut down
        self._was_busy = busy
        if self.power["action"] and time.time() >= self.power["at"]:
            act = self.power["action"]
            self.power.update(action=None, at=None)
            self.settings["when_done"] = "none"   # one time only, like IDM
            self.save()
            return act
        return None

    def event(self, ev):
        with self.lock:
            self.events.append({**ev, "id": uuid.uuid4().hex[:8], "time": time.time()})
            del self.events[:-50]

    def cancel_power(self):
        self.power.update(action=None, at=None)

    def pause(self, id, wait=True):
        it = self.items[id]
        it.force = False
        if it.dl is not None:
            it.dl.stop.set()
            if wait and it.thread:
                it.thread.join(timeout=30)
        elif it.status == "queued":
            it.status = "paused"
        self.save()

    def resume(self, id, force=False):
        it = self.items[id]
        if it.dl is not None and it.dl.stop.is_set() and it.thread:
            it.thread.join(timeout=30)        # a pause is still finishing: let it end first
        if force:
            it.force = True
        if it.dl is None and it.status in ("paused", "failed", "queued"):
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
        p = self.power
        settings = {k: v for k, v in self.settings.items() if k != "last_update_check"}
        return {"items": items, "settings": settings, "version": fastgrab.__version__,
                "video": {**fastgrab_video.status(), **VIDEO_INSTALL},
                "schedule_open": self.schedule_open(),
                "power": {"action": p["action"],
                          "seconds": max(0, round(p["at"] - time.time())) if p["at"] else None,
                          "log": p["log"][-5:]},
                "events": self.events[-20:]}


# ------------------------------------------------------------- power actions --

def do_power(action, log):
    """Sleep or shut down the PC. FASTGRAB_DRY_POWER=1 only records it (tests)."""
    log.append({"action": action, "time": time.time()})
    if os.environ.get("FASTGRAB_DRY_POWER") or action == "quit":
        return
    if os.name == "nt":
        if action == "shutdown":
            subprocess.Popen(["shutdown", "/s", "/t", "0"], creationflags=NO_WINDOW)
        elif action == "sleep":
            ctypes.windll.powrprof.SetSuspendState(0, 0, 0)
    elif sys.platform == "darwin":
        subprocess.Popen(["pmset", "sleepnow"] if action == "sleep"
                         else ["osascript", "-e", 'tell app "System Events" to shut down'])
    else:
        subprocess.Popen(["systemctl", "suspend" if action == "sleep" else "poweroff"])


# --------------------------------------------------------------- video setup --

VIDEO_INSTALL = {"installing": False, "install_error": None}


def install_video_support():
    """First-time install of video support. From source: pip installs yt-dlp with its
    helper packages and FFmpeg. In the .exe it's built in; the updater keeps it fresh."""
    VIDEO_INSTALL.update(installing=True, install_error=None)
    try:
        import importlib
        if getattr(sys, "frozen", False):
            fastgrab_update.update_video_support()
            fastgrab_update.activate()
        else:
            r = subprocess.run([sys.executable, "-m", "pip", "install", "--user", "--upgrade",
                                "yt-dlp[default]", "imageio-ffmpeg"],
                               capture_output=True, text=True, timeout=900,
                               creationflags=NO_WINDOW)
            if r.returncode != 0:
                raise RuntimeError((r.stderr or r.stdout).strip().splitlines()[-1])
            import site
            site.addsitedir(site.getusersitepackages())
        importlib.invalidate_caches()
        if not fastgrab_video.AVAILABLE:
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


def extension_dir():
    """A stable copy of the browser extension in FastGrab's data folder, which the
    user loads into Chrome/Edge (the .exe's own files live in a temp folder)."""
    src = os.path.join(resource_dir(), "extension")
    dst = os.path.join(data_dir(), "browser-extension")
    try:
        def ver(d):
            with open(os.path.join(d, "manifest.json"), encoding="utf-8") as f:
                return json.load(f).get("version")
        if os.path.isdir(src) and (not os.path.isdir(dst) or ver(src) != ver(dst)):
            tmp = dst + ".tmp"
            shutil.rmtree(tmp, ignore_errors=True)
            shutil.copytree(src, tmp)
            shutil.rmtree(dst, ignore_errors=True)
            os.replace(tmp, dst)
    except OSError:
        pass
    return dst


# ---------------------------------------------------------------------- http --

class App:
    def __init__(self, port=DEFAULT_PORT, token=None, watchdog=False):
        self.token = token or secrets.token_urlsafe(24)
        self.manager = Manager(os.path.join(data_dir(), "downloads.json"))
        self.updater = fastgrab_update.Updater(fastgrab.__version__, app_dir())
        self.last_seen = None
        self.ext_seen = None
        self.use_watchdog = watchdog
        with open(os.path.join(resource_dir(), "ui", "index.html"), encoding="utf-8") as f:
            self.page = f.read()
        app = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def handle(self):
                try:
                    super().handle()
                except ConnectionError:
                    pass

            def _host_ok(self):
                host = self.headers.get("Host", "")
                return host in (f"127.0.0.1:{app.port}", f"localhost:{app.port}")

            def _ext_ok(self):
                key = app.manager.settings.get("ext_key")
                return bool(key) and secrets.compare_digest(self.headers.get("X-FastGrab-Key", ""), key)

            def _send(self, code, body, ctype="application/json"):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n) or b"{}")

            def do_GET(self):
                if not self._host_ok():
                    return self._send(403, {"error": "bad host"})
                u = urllib.parse.urlparse(self.path)
                q = urllib.parse.parse_qs(u.query)
                if u.path == "/ext/ping":
                    if not self._ext_ok():
                        return self._send(403, {"error": "pairing key needed"})
                    app.ext_seen = time.time()
                    return self._send(200, {"ok": True, "app": APP_NAME,
                                            "version": fastgrab.__version__})
                if u.path == "/":
                    if q.get("t", [""])[0] != app.token:
                        return self._send(403, b"Forbidden", "text/plain")
                    return self._send(200, app.page.encode("utf-8"), "text/html; charset=utf-8")
                if self.headers.get("X-Token") != app.token:
                    return self._send(403, {"error": "bad token"})
                if u.path == "/api/state":
                    app.last_seen = time.time()
                    st = app.manager.state()
                    st["update"] = app.updater.snapshot()
                    st["frozen"] = bool(getattr(sys, "frozen", False))
                    st["port"] = app.port
                    st["ext_port_ok"] = app.port == DEFAULT_PORT
                    st["ext_seen"] = app.ext_seen
                    st["ext_dir"] = app.ext_dir
                    return self._send(200, st)
                if u.path == "/api/ping":
                    return self._send(200, {"ok": True})
                self._send(404, {"error": "not found"})

            def do_POST(self):
                if not self._host_ok():
                    return self._send(403, {"error": "forbidden"})
                p = urllib.parse.urlparse(self.path).path
                try:
                    body = self._body()
                except ValueError:
                    return self._send(400, {"error": "bad json"})
                m = app.manager
                if p.startswith("/ext/"):
                    return self._ext(p, body)
                if self.headers.get("X-Token") != app.token:
                    return self._send(403, {"error": "forbidden"})
                try:
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
                        elif act == "start-now":
                            m.resume(id, force=True)
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
                        return self._send(200, m.update_settings(body))
                    if p == "/api/cancel-power":
                        m.cancel_power()
                        m.settings["when_done"] = "none"
                        m.save()
                        return self._send(200, {"ok": True})
                    if p == "/api/pick-folder":
                        chosen = pick_folder(m.settings["dir"])
                        if chosen:
                            m.settings["dir"] = os.path.normpath(chosen)
                            m.save()
                        return self._send(200, {"dir": m.settings["dir"], "picked": bool(chosen)})
                    if p == "/api/update/check":
                        app.updater.check(video=body.get("video", True), app=body.get("app", True))
                        m.settings["last_update_check"] = time.time()
                        m.save()
                        return self._send(200, {"ok": True})
                    if p == "/api/update/install":
                        app.updater.install_app()
                        return self._send(200, {"ok": True})
                    if p == "/api/open-extension-folder":
                        open_path(app.ext_dir)
                        return self._send(200, {"ok": True})
                    if p == "/api/restart":
                        threading.Thread(target=app.restart, daemon=True).start()
                        return self._send(200, {"ok": True})
                    self._send(404, {"error": "not found"})
                except KeyError:
                    self._send(404, {"error": "no such download"})
                except Exception as e:
                    if sys.stderr:
                        import traceback
                        traceback.print_exc()
                    self._send(400, {"error": str(e) or e.__class__.__name__})

            def _ext(self, p, body):
                """Browser extension API (authenticated by the pairing key)."""
                if not self._ext_ok():
                    return self._send(403, {"error": "pairing key needed"})
                app.ext_seen = time.time()
                m = app.manager
                try:
                    if p == "/ext/add":
                        url = body.get("url", "")
                        if body.get("kind") == "video" or (body.get("kind") != "file"
                                                           and fastgrab_video.is_video_url(url)):
                            # let the user pick the quality in the app window
                            m.event({"type": "video", "url": url})
                            return self._send(200, {"ok": True, "picker": True})
                        hdrs = {"Cookie": body.get("cookie"), "Referer": body.get("referrer"),
                                "User-Agent": body.get("userAgent")}
                        id = m.add(url, headers={k: v for k, v in hdrs.items() if v},
                                   kind="file", source="browser",
                                   meta={"filename": body.get("filename")})
                        m.event({"type": "added", "item": id})
                        return self._send(200, {"ok": True, "id": id})
                    self._send(404, {"error": "not found"})
                except Exception as e:
                    self._send(400, {"error": str(e) or e.__class__.__name__})

        ThreadingHTTPServer.daemon_threads = True
        ThreadingHTTPServer.allow_reuse_address = os.name != "nt"
        self.ext_dir = None
        self.server = self._bind(port, Handler)
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/?t={self.token}"

    @staticmethod
    def _bind(port, handler):
        """Bind the fixed port (retrying briefly, e.g. right after a restart);
        fall back to any free port if it's taken by something else."""
        for _ in range(40 if port else 1):
            try:
                return ThreadingHTTPServer(("127.0.0.1", port), handler)
            except OSError:
                time.sleep(0.25)
        return ThreadingHTTPServer(("127.0.0.1", 0), handler)

    def watchdog(self, grace=30):
        """Quit (after pausing downloads safely) once the window has been closed."""
        while True:
            time.sleep(1)
            if self.last_seen and time.time() - self.last_seen > grace:
                self.quit()

    def quit(self):
        self.manager.pause_all()
        self.manager.save()
        try:
            os.remove(os.path.join(data_dir(), "running.json"))
        except OSError:
            pass
        os._exit(0)

    def restart(self):
        """Start a fresh copy on the same port and token, so the open window just
        reconnects (used after an update)."""
        time.sleep(0.3)                     # let the HTTP reply go out
        self.manager.pause_all()
        self.manager.save()
        self.server.shutdown()
        self.server.server_close()
        if getattr(sys, "frozen", False):
            cmd = [sys.executable]
        else:
            exe = sys.executable
            cmd = [exe, os.path.join(app_dir(), "fastgrab_app.py")]
        cmd += ["--restart", str(self.port), self.token]
        if not self.use_watchdog:
            cmd.append("--no-window")
        subprocess.Popen(cmd, cwd=app_dir(), close_fds=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=NO_WINDOW)
        os._exit(0)

    def background_updates(self):
        s = self.manager.settings
        if time.time() - (s.get("last_update_check") or 0) < 24 * 3600:
            return
        if not (s.get("auto_update_video") or s.get("check_app_updates")):
            return
        if self.updater.check(video=s.get("auto_update_video") and fastgrab_video.AVAILABLE,
                              app=s.get("check_app_updates")):
            s["last_update_check"] = time.time()

    def serve(self, window=True, reopen=True):
        fastgrab_update.cleanup_old_exe()
        self.ext_dir = extension_dir()
        with open(os.path.join(data_dir(), "running.json"), "w") as f:
            json.dump({"port": self.port, "token": self.token, "pid": os.getpid()}, f)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.manager.resume_interrupted()
        self.manager.schedule()
        if window and reopen:
            open_window(self.url)
        if self.use_watchdog:
            threading.Thread(target=self.watchdog, daemon=True).start()
        if not os.environ.get("FASTGRAB_NO_AUTO_UPDATE"):
            threading.Timer(20, self.background_updates).start()
        if sys.stdout:                    # None in a windowed .exe
            print(f"{APP_NAME} is running at {self.url}", flush=True)
        try:
            while True:
                time.sleep(1)
                act = self.manager.tick()
                if act:
                    do_power(act, self.manager.power["log"])
                    if act in ("quit", "shutdown") and not os.environ.get("FASTGRAB_DRY_POWER"):
                        self.quit()
                if int(time.time()) % 2 == 0:
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
    args = sys.argv[1:]
    window = "--no-window" not in args
    port = DEFAULT_PORT
    if "--port" in args:
        port = int(args[args.index("--port") + 1])
    if "--restart" in args:                       # started by App.restart()
        i = args.index("--restart")
        App(int(args[i + 1]), token=args[i + 2], watchdog=window).serve(window, reopen=False)
        return
    existing = already_running() if window else None
    if existing:
        open_window(existing)
        return
    App(port, watchdog=window).serve(window=window)


if __name__ == "__main__":
    main()
