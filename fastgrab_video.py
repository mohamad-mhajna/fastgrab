"""
fastgrab video mode: YouTube, TikTok, Instagram, X, Facebook, Reddit, Vimeo,
SoundCloud, Twitch and ~1,000 other sites, powered by yt-dlp (optional).

    pip install "yt-dlp[default]"          # required for video sites
    pip install imageio-ffmpeg             # optional: FFmpeg for 1080p+ (merges video+audio)
    Node.js 20+ or Deno                    # needed by yt-dlp for full YouTube support

VideoDownload has the same interface as fastgrab.Download (start(), stop,
status, snapshot()), so the app treats both the same way.
"""
import glob
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.parse

try:
    import yt_dlp
    from yt_dlp.utils import DownloadCancelled
    AVAILABLE = True
except ImportError:                         # video mode is optional
    yt_dlp = None
    AVAILABLE = False

    class DownloadCancelled(Exception):
        pass

VIDEO_HOSTS = (
    "youtube.com", "youtu.be", "tiktok.com", "instagram.com", "facebook.com", "fb.watch",
    "x.com", "twitter.com", "reddit.com", "redd.it", "vimeo.com", "twitch.tv",
    "soundcloud.com", "dailymotion.com", "dai.ly", "pinterest.com", "pin.it",
    "threads.net", "threads.com", "linkedin.com", "bilibili.com", "streamable.com",
    "kick.com", "rumble.com", "snapchat.com", "bandcamp.com", "ted.com",
)
QUALITIES = ("best", "2160", "1440", "1080", "720", "480", "360", "audio")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0      # CREATE_NO_WINDOW


def is_video_url(url):
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in VIDEO_HOSTS)


def version():
    if not AVAILABLE:
        return None
    from yt_dlp.version import __version__
    return __version__


def _works(cmd):
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=15, creationflags=_NO_WINDOW)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False        # missing, or blocked (e.g. by Windows Smart App Control)


_tools = None
_tools_lock = threading.Lock()


def tools(refresh=False):
    """Find a *working* FFmpeg and JavaScript runtime (checked once, then cached)."""
    global _tools
    with _tools_lock:
        if _tools is not None and not refresh:
            return _tools
        ffmpeg = None
        cands = [shutil.which("ffmpeg")]
        try:
            import imageio_ffmpeg
            cands.append(imageio_ffmpeg.get_ffmpeg_exe())
        except Exception:
            pass
        for c in cands:
            if c and _works([c, "-version"]):
                ffmpeg = c
                break
        js = {}
        for name in ("deno", "node"):
            p = shutil.which(name)
            if p and _works([p, "--version"]):
                js[name] = {"path": p}
        _tools = {"ffmpeg": ffmpeg, "js": js}
        return _tools


def status():
    t = tools() if AVAILABLE else {"ffmpeg": None, "js": {}}
    return {"available": AVAILABLE, "version": version(),
            "ffmpeg": bool(t["ffmpeg"]), "js_runtime": next(iter(t["js"]), None)}


def _base_opts():
    t = tools()
    o = {"quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True,
         "socket_timeout": 20, "retries": 10, "fragment_retries": 10,
         "windowsfilenames": True}
    if t["js"]:
        o["js_runtimes"] = t["js"]
    if t["ffmpeg"]:
        o["ffmpeg_location"] = t["ffmpeg"]
    return o


def clean_error(e):
    msg = _ANSI.sub("", str(e)).replace("ERROR: ", "").strip()
    msg = re.sub(r"^\[[\w:]+\] [\w-]+: ", "", msg)
    if "Sign in to confirm" in msg or "login required" in msg.lower() or "log in" in msg.lower():
        return "This video needs you to be logged in, so it can't be downloaded here."
    if "Unsupported URL" in msg:
        return "This site or link isn't supported for video downloads."
    if "Private video" in msg or "This video is private" in msg:
        return "This video is private."
    return msg[:300] or e.__class__.__name__


def probe(url):
    """Video details for the quality picker (no download)."""
    if not AVAILABLE:
        raise RuntimeError("Video support isn't installed")
    with yt_dlp.YoutubeDL({**_base_opts(), "skip_download": True}) as y:
        try:
            info = y.extract_info(url, download=False)
        except Exception as e:
            raise RuntimeError(clean_error(e)) from None
    if info.get("_type") in ("playlist", "multi_video"):
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise RuntimeError("This is a channel or playlist page. Paste a link to a single video.")
        info = entries[0]
    fmts = info.get("formats") or [info]
    heights = sorted({f.get("height") for f in fmts
                      if f.get("height") and f.get("vcodec") not in (None, "none")}, reverse=True)
    audio_ext = ("mp3", "m4a", "opus", "ogg", "wav", "flac", "aac", "weba")
    has_video = bool(heights) or any(f.get("vcodec") != "none" and (f.get("ext") or "") not in audio_ext
                                     for f in fmts)
    t = tools()
    if not has_video:
        choices = ["audio"]
    else:
        top = heights[0] if heights else 0
        choices = ["best"] + [str(h) for h in (2160, 1440, 1080, 720, 480, 360) if h <= top] + ["audio"]
    return {"title": info.get("title") or "video", "uploader": info.get("uploader") or info.get("channel"),
            "duration": info.get("duration"), "thumbnail": info.get("thumbnail"),
            "site": info.get("extractor_key") or info.get("extractor"),
            "url": info.get("webpage_url") or url, "heights": heights, "choices": choices,
            "ffmpeg": bool(t["ffmpeg"]), "js_runtime": next(iter(t["js"]), None)}


def format_for(choice, ffmpeg):
    if choice == "audio":
        return "ba[ext=m4a]/ba/b"
    h = "" if choice in (None, "", "best") else f"[height<={int(choice)}]"
    if ffmpeg:          # best separate video + audio streams, merged by FFmpeg
        return f"bv*{h}+ba/b{h}/bv*+ba/b"
    # no FFmpeg: only formats that already contain video and audio together
    return f"b{h}[vcodec!=none][acodec!=none]/b[vcodec!=none][acodec!=none]/b{h}/b"


class VideoDownload:
    def __init__(self, url, out_dir=".", choice="best", connections=8, quiet=True):
        self.url = url
        self.out_dir = out_dir
        self.choice = choice or "best"
        self.connections = connections
        self.quiet = quiet
        self.stop = threading.Event()
        self.status = "connecting"
        self.message = "Getting video info…"
        self.title = None
        self.result_path = None
        self.parts = set()            # temp files, removed on cancel
        self.streams = {}             # filename -> [downloaded, total]
        self.speed = 0.0
        self.eta = None
        self.downloaded = 0
        self.info = {"resumable": True}

    # -- yt-dlp callbacks ---------------------------------------------------------
    def _hook(self, d):
        if self.stop.is_set():
            raise DownloadCancelled()
        fn = d.get("filename")
        if d.get("tmpfilename"):
            self.parts.add(d["tmpfilename"])
        if fn:
            self.parts.add(fn + ".part")
            self.parts.add(fn + ".ytdl")
        if d["status"] == "downloading":
            self.status = "downloading"
            self.message = ""
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            self.streams[fn] = [d.get("downloaded_bytes") or 0, total]
            self.speed = d.get("speed") or 0.0
            self.eta = d.get("eta")
        elif d["status"] == "finished":
            done = d.get("total_bytes") or d.get("downloaded_bytes") or 0
            self.streams[fn] = [done, done]
        self.downloaded = sum(v[0] for v in self.streams.values())

    def _pp_hook(self, d):
        if d.get("status") == "started" and d.get("postprocessor") in ("Merger", "FFmpegMerger"):
            self.message = "Merging video and audio…"

    # -- run ------------------------------------------------------------------------
    def start(self):
        if not AVAILABLE:
            self.status, self.message = "failed", "Video support isn't installed"
            return False
        t = tools()
        opts = {**_base_opts(),
                "format": format_for(self.choice, t["ffmpeg"]),
                "outtmpl": os.path.join(self.out_dir, "%(title).120B [%(id)s].%(ext)s"),
                "progress_hooks": [self._hook], "postprocessor_hooks": [self._pp_hook],
                "continuedl": True, "overwrites": False, "part": True,
                "concurrent_fragment_downloads": max(1, min(self.connections, 16))}
        if t["ffmpeg"] and self.choice != "audio":
            opts["merge_output_format"] = "mp4"
        try:
            with yt_dlp.YoutubeDL(opts) as y:
                info = y.extract_info(self.url, download=True)
                if info.get("_type") in ("playlist", "multi_video") and info.get("entries"):
                    info = next(e for e in info["entries"] if e)
                self.title = info.get("title")
                rd = info.get("requested_downloads") or []
                path = (rd[0].get("filepath") if rd else None) or y.prepare_filename(info)
            self.result_path = path
            if path and os.path.exists(path):
                size = os.path.getsize(path)
                self.streams = {path: [size, size]}
                self.downloaded = size
            self.status = "completed"
            self.message = ""
            return True
        except DownloadCancelled:
            self.status, self.message = "paused", ""
            return False
        except Exception as e:
            if self.stop.is_set():
                self.status, self.message = "paused", ""
            else:
                self.status, self.message = "failed", clean_error(e)
            return False

    def cleanup(self):
        """Delete this download's temporary files (used by Cancel)."""
        for p in list(self.parts):
            for f in glob.glob(glob.escape(p) + "*") if p.endswith(".part") else [p]:
                if os.path.isfile(f) and (f.endswith((".part", ".ytdl")) or ".part-Frag" in f):
                    try:
                        os.remove(f)
                    except OSError:
                        pass

    def snapshot(self):
        total = sum(v[1] for v in self.streams.values()) or None
        segs, pos = [], 0
        for i, (done, tot) in enumerate(self.streams.values()):
            tot = tot or done
            segs.append([pos, pos + min(done, tot), pos + tot, i])
            pos += tot
        name = self.title
        if self.result_path:
            name = os.path.basename(self.result_path)
        return {"status": self.status, "message": self.message, "filename": name,
                "size": total, "downloaded": self.downloaded,
                "speed": self.speed if self.status == "downloading" else 0.0,
                "eta": self.eta, "active": len(self.streams) and (1 if self.status == "downloading" else 0),
                "connections": self.connections, "resumable": True,
                "path": self.result_path, "segments": segs}
