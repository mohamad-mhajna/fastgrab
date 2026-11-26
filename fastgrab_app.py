#!/usr/bin/env python3
"""
FastGrab desktop app: a window on top of the fastgrab engine.

    python fastgrab_app.py

How it works (zero dependencies):
  * the UI is a single HTML page (ui/index.html) shown in its own app window
    (Edge or Chrome in "--app" mode, which every Windows PC already has)
  * this script runs a small local HTTP API on 127.0.0.1 that the page talks to
  * the API is protected by a random per-run token and a Host check, so websites
    you visit can't control your downloads
  * the browser extension talks to /ext/* with its own pairing key
  * closing the window keeps FastGrab running in the system tray (Windows), so
    downloads and the schedule carry on; downloads that were running when you
    quit resume on next start
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
WINDOW_GRACE = float(os.environ.get("FASTGRAB_WINDOW_GRACE", 30))  # window gone this long: closed for good
WINDOW_AWAY = min(5.0, WINDOW_GRACE)    # no poll for this long: nobody is looking (notify instead)


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
    "close_to_tray": True,      # closing the window keeps downloads going (tray icon on Windows)
    "keep_awake": True,         # stop Windows sleeping while something is downloading
    "tray_hint_shown": False,
    "update_later": None,       # {"key": what the update popup showed, "until": time} after "Later"
}


class Manager:
    def __init__(self, store_path):
        self.store_path = store_path
        self.lock = threading.RLock()
        self.save_lock = threading.Lock()   # one save at a time (downloads finish on other threads)
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
        with self.save_lock:
            with self.lock:
                for it in self.items.values():
                    it.sync_from_dl()
                data = {"settings": self.settings,
                        "items": [it.to_saved() for it in self.items.values()]}
            tmp = f"{self.store_path}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=1)
            for attempt in range(5):       # Windows: antivirus/indexer may hold the file briefly
                try:
                    os.replace(tmp, self.store_path)
                    return
                except PermissionError:
                    time.sleep(0.1 * (attempt + 1))
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
        for k in ("auto_update_video", "check_app_updates", "close_to_tray", "keep_awake"):
            if k in body:
                s[k] = bool(body[k])
        if body.get("update_later"):          # update popup: "Later" = ask again in a day
            s["update_later"] = {"key": str(body["update_later"])[:100], "until": time.time() + 24 * 3600}
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

    def pause_everything(self):
        """Pause every running or waiting download (tray menu "Pause all")."""
        for it in list(self.items.values()):
            if it.dl is not None or it.status == "queued":
                try:
                    self.pause(it.id, wait=False)
                except KeyError:              # removed meanwhile
                    pass

    def resume_everything(self):
        for it in list(self.items.values()):
            if it.status == "paused":
                try:
                    self.resume(it.id)
                except KeyError:
                    pass

    def counts(self):
        """(downloading, waiting, paused, total speed in bytes/s) for the tray."""
        down = wait = paused = 0
        speed = 0.0
        with self.lock:
            items = list(self.items.values())
        for it in items:
            dl = it.dl
            if dl is not None:
                down += 1
                try:
                    speed += dl.snapshot().get("speed") or 0
                except Exception:
                    pass
            elif it.status == "queued":
                wait += 1
            elif it.status == "paused":
                paused += 1
        return down, wait, paused, speed

    def busy(self):
        """Anything still to do (so a window-less app without a tray icon stays)."""
        with self.lock:
            return bool(self.power["action"]) or any(
                i.dl is not None or i.status in ("downloading", "connecting", "queued")
                for i in self.items.values())

    def stop_for_exit(self):
        """Quitting or restarting: pause what runs, and remember to continue it
        when FastGrab starts again."""
        for it in list(self.items.values()):
            if it.dl is not None and not it.dl.stop.is_set():
                it.resume_on_start = True
        self.pause_all()
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
    if os.environ.get("FASTGRAB_NO_BROWSER"):        # tests: record it instead
        with open(os.path.join(data_dir(), "opened-windows.log"), "a") as f:
            f.write(url + "\n")
        return
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


# ----------------------------------------------------------- tray icon (Win) --
# Standard library only: talks to Windows directly through ctypes, like any native
# tray app. On other systems there is no icon; the app keeps running in the
# background until its queue is done.
# menu command ids
CMD_OPEN, CMD_PAUSE_ALL, CMD_RESUME_ALL, CMD_QUIT = 1001, 1002, 1003, 1004

if os.name == "nt":
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                                 wintypes.WPARAM, wintypes.LPARAM)

    WM_DESTROY, WM_CLOSE, WM_COMMAND, WM_NULL = 0x0002, 0x0010, 0x0111, 0x0000
    WM_USER, WM_APP = 0x0400, 0x8000
    WM_LBUTTONUP, WM_LBUTTONDBLCLK, WM_RBUTTONUP, WM_CONTEXTMENU = 0x0202, 0x0203, 0x0205, 0x007B
    WM_TRAY = WM_APP + 1               # our icon's mouse messages
    WM_TRAY_WAKE = WM_APP + 2          # "do the queued work" (sent from other threads)
    NIN_BALLOONUSERCLICK = WM_USER + 5
    NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
    NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x1, 0x2, 0x4, 0x10
    NIIF_INFO, NIIF_NOSOUND = 0x1, 0x10
    MF_STRING, MF_SEPARATOR, MF_GRAYED = 0x0, 0x800, 0x1
    TPM_RIGHTBUTTON, TPM_RETURNCMD, TPM_NONOTIFY, TPM_LAYOUTRTL = 0x2, 0x100, 0x80, 0x8000
    IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE = 1, 0x10, 0x40
    IDI_APPLICATION = 32512
    SM_CXSMICON, SM_CYSMICON = 49, 50

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
                    ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
                    ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
                    ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD),
                    ("dwStateMask", wintypes.DWORD), ("szInfo", wintypes.WCHAR * 256),
                    ("uVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64),
                    ("dwInfoFlags", wintypes.DWORD), ("guidItem", GUID),
                    ("hBalloonIcon", wintypes.HICON)]

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                    ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                    ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                    ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                    ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

    class POINT(ctypes.Structure):
        _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

    class MSG(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("message", wintypes.UINT),
                    ("wParam", wintypes.WPARAM), ("lParam", wintypes.LPARAM),
                    ("time", wintypes.DWORD), ("pt", POINT), ("lPrivate", wintypes.DWORD)]

    def _sig(fn, res, *args):
        fn.restype, fn.argtypes = res, list(args)
        return fn

    _sig(user32.DefWindowProcW, LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    _sig(user32.RegisterClassW, wintypes.ATOM, ctypes.POINTER(WNDCLASSW))
    _sig(user32.UnregisterClassW, wintypes.BOOL, wintypes.LPCWSTR, wintypes.HINSTANCE)
    _sig(user32.CreateWindowExW, wintypes.HWND, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
         wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
         wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID)
    _sig(user32.DestroyWindow, wintypes.BOOL, wintypes.HWND)
    _sig(user32.GetMessageW, wintypes.BOOL, ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT)
    _sig(user32.TranslateMessage, wintypes.BOOL, ctypes.POINTER(MSG))
    _sig(user32.DispatchMessageW, LRESULT, ctypes.POINTER(MSG))
    _sig(user32.PostMessageW, wintypes.BOOL, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    _sig(user32.PostQuitMessage, None, ctypes.c_int)
    _sig(user32.RegisterWindowMessageW, wintypes.UINT, wintypes.LPCWSTR)
    _sig(user32.CreatePopupMenu, wintypes.HMENU)
    _sig(user32.AppendMenuW, wintypes.BOOL, wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR)
    _sig(user32.SetMenuDefaultItem, wintypes.BOOL, wintypes.HMENU, wintypes.UINT, wintypes.UINT)
    _sig(user32.TrackPopupMenu, wintypes.BOOL, wintypes.HMENU, wintypes.UINT, ctypes.c_int,
         ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.LPVOID)
    _sig(user32.DestroyMenu, wintypes.BOOL, wintypes.HMENU)
    _sig(user32.GetCursorPos, wintypes.BOOL, ctypes.POINTER(POINT))
    _sig(user32.SetForegroundWindow, wintypes.BOOL, wintypes.HWND)
    _sig(user32.LoadImageW, wintypes.HANDLE, wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
         ctypes.c_int, ctypes.c_int, wintypes.UINT)
    _sig(user32.LoadIconW, wintypes.HICON, wintypes.HINSTANCE, wintypes.LPVOID)
    _sig(user32.DestroyIcon, wintypes.BOOL, wintypes.HICON)
    _sig(user32.GetSystemMetrics, ctypes.c_int, ctypes.c_int)
    _sig(user32.FindWindowW, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR)
    _sig(shell32.Shell_NotifyIconW, wintypes.BOOL, wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW))
    _sig(kernel32.GetModuleHandleW, wintypes.HMODULE, wintypes.LPCWSTR)


class Tray:
    # the icon in the Windows tray (next to the clock), via Shell_NotifyIconW
    """The icon lives on its own thread with a Windows message loop. Every other
    method may be called from any thread."""

    def __init__(self, icon_path, on_open, on_pause_all, on_resume_all, on_quit, texts):
        self.icon_path = icon_path
        self.handlers = {CMD_OPEN: on_open, CMD_PAUSE_ALL: on_pause_all,
                         CMD_RESUME_ALL: on_resume_all, CMD_QUIT: on_quit}
        self.texts = texts            # () -> dict with labels, rtl, can_pause, can_resume
        self.hwnd = None
        self.shown = False            # the icon is in the tray right now
        self.tip = "FastGrab"
        self.thread = None
        self.lock = threading.Lock()
        self.ready = threading.Event()
        self.last_error = None
        self.menu_open = False

    # -- public, any thread ---------------------------------------------------------
    def start(self, timeout=10):
        if not os.name == "nt":
            return False
        self.thread = threading.Thread(target=self._loop, name="tray", daemon=True)
        self.thread.start()
        self.ready.wait(timeout)
        return self.shown

    def update(self, tip):
        tip = (tip or "FastGrab")[:127]
        if tip == self.tip:
            return
        self.tip = tip
        if self.shown:
            with self.lock:
                nid = self._nid(NIF_TIP)
                nid.szTip = tip
                shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    def notify(self, title, message):
        """A Windows notification from the tray icon. Clicking it opens FastGrab."""
        if not self.shown:
            return False
        with self.lock:
            nid = self._nid(NIF_INFO)
            nid.szInfoTitle = (title or "")[:63]
            nid.szInfo = (message or " ")[:255]
            nid.dwInfoFlags = NIIF_INFO
            return bool(shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid)))

    def stop(self):
        """Remove the icon and end the message loop. Safe to call twice."""
        if not os.name == "nt" or not self.hwnd:
            return
        self._remove_icon()
        user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(5)

    # -- message loop thread ----------------------------------------------------------
    def _nid(self, flags):
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = flags
        return nid

    def _add_icon(self):
        with self.lock:
            nid = self._nid(NIF_MESSAGE | NIF_ICON | NIF_TIP)
            nid.uCallbackMessage = WM_TRAY
            nid.hIcon = self.hicon
            nid.szTip = self.tip
            self.shown = bool(shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)))
            if not self.shown:
                self.last_error = ctypes.get_last_error()
        return self.shown

    def _remove_icon(self):
        with self.lock:
            if self.shown:
                nid = self._nid(0)
                shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
                self.shown = False

    def _loop(self):
        try:
            hinst = kernel32.GetModuleHandleW(None)
            self.hicon = None
            if self.icon_path and os.path.exists(self.icon_path):
                # the small-icon size for this screen's scaling (16 px at 100%, 24 at 150%...)
                cx, cy = user32.GetSystemMetrics(SM_CXSMICON), user32.GetSystemMetrics(SM_CYSMICON)
                self.hicon = user32.LoadImageW(None, self.icon_path, IMAGE_ICON, cx, cy,
                                               LR_LOADFROMFILE)
            self.own_icon = bool(self.hicon)
            if not self.hicon:
                self.hicon = user32.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION))
            # Explorer sends this when it restarts (e.g. after a crash): add the icon again
            self.taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")
            self._proc = WNDPROC(self._wndproc)      # keep a reference, or it gets freed
            self.cls_name = f"FastGrabTray{os.getpid()}"
            wc = WNDCLASSW()
            wc.lpfnWndProc = self._proc
            wc.hInstance = hinst
            wc.lpszClassName = self.cls_name
            if not user32.RegisterClassW(ctypes.byref(wc)):
                raise OSError(ctypes.get_last_error(), "RegisterClassW failed")
            # a hidden top-level window (not message-only, so it hears TaskbarCreated)
            self.hwnd = user32.CreateWindowExW(0, self.cls_name, "FastGrab", 0, 0, 0, 0, 0,
                                               None, None, hinst, None)
            if not self.hwnd:
                raise OSError(ctypes.get_last_error(), "CreateWindowExW failed")
            self._add_icon()
        except Exception as e:
            self.last_error = repr(e)
            self.ready.set()
            return
        self.ready.set()
        msg = MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        if self.own_icon:
            user32.DestroyIcon(self.hicon)
        user32.UnregisterClassW(self.cls_name, hinst)
        self.hwnd = None

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TRAY:
                event = lparam & 0xFFFF
                if event in (WM_LBUTTONDBLCLK, WM_LBUTTONUP, NIN_BALLOONUSERCLICK):
                    self._run(CMD_OPEN)
                elif event in (WM_RBUTTONUP, WM_CONTEXTMENU):
                    self._menu(hwnd)
                return 0
            if msg == WM_COMMAND:
                self._run(wparam & 0xFFFF)
                return 0
            if msg == self.taskbar_created and self.taskbar_created:
                # Explorer (re)started: put the icon back. Remove any leftover first,
                # since adding an icon Windows still has would fail.
                with self.lock:
                    shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid(0)))
                    self.shown = False
                self._add_icon()
                return 0
            if msg == WM_CLOSE:
                self._remove_icon()
                user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
        except Exception as e:           # never let an exception cross into Windows
            self.last_error = repr(e)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _run(self, cmd):
        fn = self.handlers.get(cmd)
        if fn:
            # off the message loop, so a slow action (Pause all) never freezes the icon
            threading.Thread(target=fn, daemon=True).start()

    def menu_items(self):
        """(id, label, enabled) rows of the right-click menu; None is a separator."""
        t = self.texts()
        return [(CMD_OPEN, t["open"], True), None,
                (CMD_PAUSE_ALL, t["pause_all"], t.get("can_pause", True)),
                (CMD_RESUME_ALL, t["resume_all"], t.get("can_resume", True)), None,
                (CMD_QUIT, t["quit"], True)]

    def _menu(self, hwnd):
        if self.menu_open:
            return
        self.menu_open = True
        menu = user32.CreatePopupMenu()
        try:
            for row in self.menu_items():
                if row is None:
                    user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
                else:
                    cid, label, enabled = row
                    user32.AppendMenuW(menu, MF_STRING | (0 if enabled else MF_GRAYED), cid, label)
            user32.SetMenuDefaultItem(menu, CMD_OPEN, 0)
            pt = POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            # without this the menu doesn't close when you click somewhere else
            user32.SetForegroundWindow(hwnd)
            flags = TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY
            if self.texts().get("rtl"):
                flags |= TPM_LAYOUTRTL
            cmd = user32.TrackPopupMenu(menu, flags, pt.x, pt.y, 0, hwnd, None)
            user32.PostMessageW(hwnd, WM_NULL, 0, 0)
            if cmd:
                self._run(cmd)
        finally:
            user32.DestroyMenu(menu)
            self.menu_open = False


# ---------------------------------------------------------------------- tray --

TRAY_TEXT = {
    "en": {"open": "Open FastGrab", "pause_all": "Pause all", "resume_all": "Resume all",
           "quit": "Quit FastGrab", "downloading": "{n} downloading · {speed}/s",
           "waiting": "{n} waiting", "paused": "{n} paused",
           "hint_t": "FastGrab is still running",
           "hint": "Downloads continue in the background. Click this icon to open FastGrab, right-click it to quit.",
           "done": "Download complete", "failed": "Download failed"},
    "ar": {"open": "فتح FastGrab", "pause_all": "إيقاف الكل مؤقتًا", "resume_all": "استئناف الكل",
           "quit": "إنهاء FastGrab", "downloading": "{n} قيد التنزيل · {speed}/ث",
           "waiting": "{n} بالانتظار", "paused": "{n} متوقف مؤقتًا",
           "hint_t": "FastGrab ما زال يعمل",
           "hint": "تستمر التنزيلات في الخلفية. انقر على هذه الأيقونة لفتح FastGrab، وانقر بزر الفأرة الأيمن للإنهاء.",
           "done": "اكتمل التنزيل", "failed": "فشل التنزيل"},
    "he": {"open": "פתח את FastGrab", "pause_all": "השהה הכל", "resume_all": "המשך הכל",
           "quit": "צא מ-FastGrab", "downloading": "{n} בהורדה · {speed}/ש'",
           "waiting": "{n} ממתינות", "paused": "{n} מושהות",
           "hint_t": "FastGrab עדיין פועל",
           "hint": "ההורדות ממשיכות ברקע. לחץ על הסמל כדי לפתוח את FastGrab, לחיצה ימנית כדי לצאת.",
           "done": "ההורדה הושלמה", "failed": "ההורדה נכשלה"},
}


def fmt_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def tray_tip(lang, counts):
    """Tooltip of the tray icon, e.g. "FastGrab - 2 downloading · 5.1 MB/s"."""
    t = TRAY_TEXT.get(lang, TRAY_TEXT["en"])
    down, wait, paused, speed = counts
    parts = []
    if down:
        parts.append(t["downloading"].format(n=down, speed=fmt_size(speed)))
    if wait:
        parts.append(t["waiting"].format(n=wait))
    if paused and not down:
        parts.append(t["paused"].format(n=paused))
    return (APP_NAME + " - " + " · ".join(parts)) if parts else APP_NAME


def keep_awake(on):
    """Ask Windows not to sleep while downloads run. The request belongs to the
    calling thread, so always call this from the same long-lived thread."""
    if os.name != "nt":
        return False
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
    f = ctypes.windll.kernel32.SetThreadExecutionState
    f.restype, f.argtypes = ctypes.c_uint32, [ctypes.c_uint32]
    return bool(f(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0)))


def no_power_throttling():
    """Opt out of Windows "power throttling" (EcoQoS), which can slow down
    background programs that have no window on screen."""
    if os.name != "nt":
        return False
    try:
        from ctypes import wintypes

        class STATE(ctypes.Structure):
            _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG),
                        ("StateMask", wintypes.ULONG)]
        k = ctypes.windll.kernel32
        k.GetCurrentProcess.restype = wintypes.HANDLE
        k.SetProcessInformation.restype = wintypes.BOOL
        k.SetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        st = STATE(1, 0x1, 0)        # control EXECUTION_SPEED, state off = never throttle
        return bool(k.SetProcessInformation(k.GetCurrentProcess(), 4,   # ProcessPowerThrottling
                                            ctypes.byref(st), ctypes.sizeof(st)))
    except Exception:
        return False


# ---------------------------------------------------------------------- http --

class App:
    def __init__(self, port=DEFAULT_PORT, token=None, watchdog=False, tray=False):
        self.token = token or secrets.token_urlsafe(24)
        self.manager = Manager(os.path.join(data_dir(), "downloads.json"))
        self.updater = fastgrab_update.Updater(fastgrab.__version__, app_dir())
        self.last_seen = None
        self.ext_seen = None
        self.use_watchdog = watchdog
        self.want_tray = tray and os.name == "nt"
        self.tray = None
        self.clients = {}           # window id -> last poll (one per open FastGrab window)
        self.replace = None         # windows to close once a newly opened one shows up
        self.replaced = set()
        self.clients_lock = threading.Lock()
        self.last_show = 0
        self.awake = False
        self.hint_due = False
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
                    close = app.seen_client(self.headers.get("X-Client"))
                    st = app.manager.state()
                    st["close_window"] = close
                    st["tray"] = bool(app.tray and app.tray.shown)
                    st["windows"] = os.name == "nt"
                    st["awake"] = app.awake
                    st["window_count"] = app.window_count()
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
                    if p == "/api/show-window":         # FastGrab was started again
                        app.show_window()
                        return self._send(200, {"ok": True})
                    if p == "/api/tray":                # the tray menu's actions (and tests)
                        act = body.get("action")
                        if act == "pause-all":
                            m.pause_everything()
                        elif act == "resume-all":
                            m.resume_everything()
                        else:
                            return self._send(400, {"error": "unknown action"})
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

    # -- windows ---------------------------------------------------------------
    def seen_client(self, cid):
        """A window polled. Returns True if that window should close itself
        because a newer one was opened (so you never end up with two)."""
        now = time.time()
        with self.clients_lock:
            for k in [k for k, v in self.clients.items() if now - v > 120]:
                del self.clients[k]
                self.replaced.discard(k)
            if not cid:
                return False
            cid = str(cid)[:64]
            if cid not in self.clients and self.replace is not None and cid not in self.replace:
                self.replaced |= self.replace          # the new window is up: close the old ones
                self.replace = None
            self.clients[cid] = now
            return cid in self.replaced

    def window_open(self):
        now = time.time()
        with self.clients_lock:
            if any(now - v < WINDOW_AWAY for k, v in self.clients.items() if k not in self.replaced):
                return True
        return bool(self.last_seen) and now - self.last_seen < WINDOW_AWAY and not self.clients

    def window_count(self):
        now = time.time()
        with self.clients_lock:
            return sum(1 for k, v in self.clients.items() if now - v < WINDOW_AWAY and k not in self.replaced)

    def show_window(self):
        """Open the window (tray click, or FastGrab started again). An already open
        window closes itself once the new one is up."""
        now = time.time()
        if now - self.last_show < 2:                 # a double click is one request
            return
        self.last_show = now
        with self.clients_lock:
            self.replace = {k for k, v in self.clients.items() if now - v < WINDOW_AWAY}
        open_window(self.url)

    # -- tray ------------------------------------------------------------------
    def tray_texts(self):
        lang = self.manager.settings.get("lang", "en")
        down, wait, paused, _ = self.manager.counts()
        return {**TRAY_TEXT.get(lang, TRAY_TEXT["en"]), "rtl": lang in ("ar", "he"),
                "can_pause": bool(down or wait), "can_resume": bool(paused)}

    def start_tray(self):
        if not self.want_tray:
            return False
        self.tray = Tray(
            next((p for p in (os.path.join(resource_dir(), "ui", "fastgrab.ico"),
                              os.path.join(resource_dir(), "fastgrab.ico"),
                              os.path.join(resource_dir(), "assets", "fastgrab.ico"))
                  if os.path.exists(p)), None),
            on_open=self.show_window,
            on_pause_all=self.manager.pause_everything,
            on_resume_all=self.manager.resume_everything,
            on_quit=self.quit,
            texts=self.tray_texts)
        ok = self.tray.start()
        if not ok and sys.stderr:
            print(f"tray icon unavailable: {self.tray.last_error}", file=sys.stderr, flush=True)
        return ok

    def housekeeping(self):
        """Every second, on one long-lived thread: tray tooltip, notifications,
        keep-awake, and what to do once the window is closed."""
        last = {}
        while True:
            time.sleep(1)
            try:
                m = self.manager
                s = m.settings
                counts = m.counts()
                want_awake = bool(s.get("keep_awake", True) and counts[0])
                if want_awake != self.awake:
                    keep_awake(want_awake)
                    self.awake = want_awake
                looking = self.window_open()
                tray = self.tray if (self.tray and self.tray.shown) else None
                if tray:
                    tray.update(tray_tip(s.get("lang", "en"), counts))
                    t = TRAY_TEXT.get(s.get("lang"), TRAY_TEXT["en"])
                    with m.lock:
                        now = {i.id: (i.status if i.dl is None else "running", i)
                               for i in m.items.values()}
                    for id, (st, it) in now.items():
                        before = last.get(id)
                        if not looking and before and before != st and st in ("completed", "failed"):
                            tray.notify(t["done"] if st == "completed" else t["failed"],
                                        it.filename or guess_name(it.url))
                    last = {k: v[0] for k, v in now.items()}
                gone = self.last_seen and time.time() - self.last_seen > WINDOW_GRACE
                if self.use_watchdog and self.last_seen and not looking:
                    if not s.get("close_to_tray", True):
                        if gone:
                            self.quit()             # the old behaviour: closing the window quits
                    elif tray:
                        if not s.get("tray_hint_shown"):
                            s["tray_hint_shown"] = True
                            m.save()
                            tray.notify(t["hint_t"], t["hint"])
                    elif gone and not m.busy():
                        self.quit()                 # no tray icon: stay only until the work is done
            except Exception:
                if sys.stderr:
                    import traceback
                    traceback.print_exc()

    def quit(self):
        if self.tray:
            self.tray.stop()                # or a dead icon stays next to the clock
        keep_awake(False)
        self.manager.stop_for_exit()
        try:
            os.remove(os.path.join(data_dir(), "running.json"))
        except OSError:
            pass
        os._exit(0)

    def restart(self):
        """Start a fresh copy on the same port and token, so the open window just
        reconnects (used after an update)."""
        time.sleep(0.3)                     # let the HTTP reply go out
        self.manager.stop_for_exit()
        self.server.shutdown()
        self.server.server_close()
        if self.tray:
            self.tray.stop()
        if getattr(sys, "frozen", False):
            cmd = [sys.executable]
        else:
            exe = sys.executable
            cmd = [exe, os.path.join(app_dir(), "fastgrab_app.py")]
        cmd += ["--restart", str(self.port), self.token]
        if not self.use_watchdog:
            cmd.append("--no-window")
        elif self.last_seen and not self.window_open():
            cmd.append("--hidden")          # updated while in the tray: stay there
        log = open(os.path.join(data_dir(), "restart.log"), "w")   # errors of the new copy, if any
        subprocess.Popen(cmd, cwd=app_dir(), close_fds=True, stdout=log, stderr=log,
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
        no_power_throttling()
        self.start_tray()
        if window and reopen:
            open_window(self.url)
        threading.Thread(target=self.housekeeping, name="housekeeping", daemon=True).start()
        if not os.environ.get("FASTGRAB_NO_AUTO_UPDATE"):
            threading.Timer(20, self.background_updates).start()
        if sys.stdout:                    # None in a windowed .exe
            print(f"{APP_NAME} is running at {self.url}", flush=True)
        try:
            while True:
                time.sleep(1)
                try:                        # one failed tick or save must never stop the app
                    act = self.manager.tick()
                    if act:
                        do_power(act, self.manager.power["log"])
                        if act in ("quit", "shutdown") and not os.environ.get("FASTGRAB_DRY_POWER"):
                            self.quit()
                    if int(time.time()) % 2 == 0:
                        self.manager.save()
                except Exception:
                    if sys.stderr:
                        import traceback
                        traceback.print_exc()
        except KeyboardInterrupt:
            if self.tray:
                self.tray.stop()
            self.manager.stop_for_exit()


def show_existing():
    """If FastGrab is already running (maybe only in the tray), ask it to show its
    window and return True, instead of starting a second copy."""
    try:
        with open(os.path.join(data_dir(), "running.json")) as f:
            r = json.load(f)
        req = urllib.request.Request(f"http://127.0.0.1:{r['port']}/api/show-window", data=b"{}",
                                     headers={"X-Token": r["token"], "Content-Type": "application/json"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        opener.open(req, timeout=2).read()
        return True
    except Exception:
        url = already_running()             # an older FastGrab without /api/show-window
        if url:
            open_window(url)
        return bool(url)


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
    # --tray / --watchdog: tray icon and close-the-window handling without opening
    # a window (used by the tests); --hidden: start in the tray only
    watchdog = window or "--watchdog" in args
    tray = window or "--tray" in args
    hidden = "--hidden" in args
    port = DEFAULT_PORT
    if "--port" in args:
        port = int(args[args.index("--port") + 1])
    if "--restart" in args:                       # started by App.restart()
        i = args.index("--restart")
        app = App(int(args[i + 1]), token=args[i + 2], watchdog=watchdog, tray=tray)
        if hidden:
            app.last_seen = time.time() - WINDOW_GRACE - 1   # the window was already closed
        app.serve(window, reopen=False)
        return
    if window and (already_running() if hidden else show_existing()):
        return                                    # FastGrab is already running
    App(port, watchdog=watchdog, tray=tray).serve(window=window and not hidden)


if __name__ == "__main__":
    main()
