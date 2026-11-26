"""
FastGrab auto-update.

1. Video support (yt-dlp): sites like YouTube change often and yt-dlp ships fixes
   within days. FastGrab downloads the newest yt-dlp wheel from PyPI, checks its
   SHA-256 against PyPI's published digest, and unpacks it into
   <data dir>/addons. A small import hook makes Python load yt-dlp from there,
   ahead of the copy bundled in the .exe or installed with pip. No pip needed.

2. FastGrab itself: compares the version with the latest GitHub release. The .exe
   replaces itself (Windows allows renaming a running .exe); a source install
   replaces its .py/.html files from the release's fastgrab-source.zip. Every
   download is verified against the release's SHA256SUMS.txt.
"""
import hashlib
import importlib.machinery
import io
import json
import os
import re
import shutil
import sys
import threading
import time
import urllib.parse
import urllib.request
import zipfile

REPO = "mohamadar8162-design/fastgrab"
PYPI = os.environ.get("FASTGRAB_PYPI", "https://pypi.org/pypi")
GITHUB_API = os.environ.get("FASTGRAB_GITHUB_API", "https://api.github.com")
SOURCE_FILES = ("fastgrab.py", "fastgrab_app.py", "fastgrab_video.py", "fastgrab_update.py",
                "ui/index.html")
ADDON_MODULES = {"yt-dlp": "yt_dlp", "yt-dlp-ejs": "yt_dlp_ejs"}


def data_dir():
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        d = os.path.join(base, "FastGrab")
    else:
        d = os.path.join(os.path.expanduser("~"), ".config", "fastgrab")
    os.makedirs(d, exist_ok=True)
    return d


def addons_dir():
    d = os.path.join(data_dir(), "addons")
    os.makedirs(d, exist_ok=True)
    return d


def vtuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", str(v or ""))[:4])


def _get(url, timeout=60):
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    handlers = [urllib.request.ProxyHandler({})] if host in ("127.0.0.1", "localhost") else []
    req = urllib.request.Request(url, headers={"User-Agent": "FastGrab-updater",
                                               "Accept": "application/json, */*"})
    with urllib.request.build_opener(*handlers).open(req, timeout=timeout) as r:
        return r.read()


def _get_json(url):
    return json.loads(_get(url, timeout=20))


def _safe_extract(zf, dest):
    root = os.path.realpath(dest)
    for m in zf.infolist():
        target = os.path.realpath(os.path.join(dest, m.filename))
        if not (target == root or target.startswith(root + os.sep)):
            raise RuntimeError(f"unsafe path in archive: {m.filename}")
    zf.extractall(dest)


# ------------------------------------------------------------ addon loading --

def _active_file():
    return os.path.join(addons_dir(), "active.json")


def _read_active():
    try:
        with open(_active_file(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


class _AddonFinder:
    """Load yt_dlp / yt_dlp_ejs (and their submodules) from the addons folder,
    even inside a PyInstaller .exe, whose own importer would otherwise win."""

    def __init__(self, dirs):
        self.dirs = dirs                     # module name -> folder that contains it

    def find_spec(self, name, path=None, target=None):
        top = name.split(".")[0]
        if top not in self.dirs:
            return None
        search = [self.dirs[top]] if "." not in name else path
        return importlib.machinery.PathFinder.find_spec(name, search)


_finder = None


def activate():
    """Call before importing yt_dlp. Returns {package: version} of active addons."""
    global _finder
    active = _read_active()
    dirs = {}
    for pkg, info in active.items():
        mod = ADDON_MODULES.get(pkg)
        if mod and os.path.isdir(os.path.join(info.get("dir", ""), mod)):
            dirs[mod] = info["dir"]
    if dirs and _finder is None:
        _finder = _AddonFinder(dirs)
        sys.meta_path.insert(0, _finder)
    return {p: i["version"] for p, i in active.items() if ADDON_MODULES.get(p) in dirs}


def deactivate():
    """Stop loading addons (used when an updated yt-dlp fails to import)."""
    global _finder
    if _finder in sys.meta_path:
        sys.meta_path.remove(_finder)
    _finder = None
    for name in list(sys.modules):
        if name.split(".")[0] in ADDON_MODULES.values():
            del sys.modules[name]


# ------------------------------------------------------------- video update --

def installed_version(pkg):
    v = _read_active().get(pkg, {}).get("version")
    try:
        from importlib.metadata import version
        mv = version(pkg)
    except Exception:
        mv = None
    if pkg == "yt-dlp" and not mv:
        try:
            from yt_dlp.version import __version__ as mv
        except Exception:
            mv = None
    cands = [x for x in (v, mv) if x]
    return max(cands, key=vtuple) if cands else None


def _pypi_wheel(pkg, version=None):
    j = _get_json(f"{PYPI}/{pkg}/json" if not version else f"{PYPI}/{pkg}/{version}/json")
    ver = j["info"]["version"]
    wheel = next(u for u in j["urls"] if u.get("packagetype") == "bdist_wheel"
                 and u["filename"].endswith("-none-any.whl"))
    return ver, wheel["url"], wheel["digests"]["sha256"], j["info"].get("requires_dist") or []


def _install_wheel(pkg, ver, url, sha):
    data = _get(url, timeout=300)
    if hashlib.sha256(data).hexdigest() != sha:
        raise RuntimeError(f"{pkg} {ver}: download didn't match PyPI's checksum")
    dest = os.path.join(addons_dir(), f"{pkg}-{ver}")
    tmp = dest + ".tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        _safe_extract(zf, tmp)
    shutil.rmtree(dest, ignore_errors=True)
    os.replace(tmp, dest)
    active = _read_active()
    old = active.get(pkg, {}).get("dir")
    active[pkg] = {"version": ver, "dir": dest}
    tmpf = _active_file() + ".tmp"
    with open(tmpf, "w", encoding="utf-8") as f:
        json.dump(active, f, indent=1)
    os.replace(tmpf, _active_file())
    for name in os.listdir(addons_dir()):      # remove older copies we created
        p = os.path.join(addons_dir(), name)
        if name.startswith(pkg + "-") and p not in (dest, old) and os.path.isdir(p) \
                and re.fullmatch(re.escape(pkg) + r"-[\d.]+(\.post\d+)?(\.tmp)?", name):
            shutil.rmtree(p, ignore_errors=True)


def update_video_support():
    """Install the newest yt-dlp (+ the yt-dlp-ejs version it needs) if newer.
    Returns {"updated": bool, "version": str}."""
    cur = installed_version("yt-dlp")
    ver, url, sha, requires = _pypi_wheel("yt-dlp")
    if cur and vtuple(ver) <= vtuple(cur):
        return {"updated": False, "version": cur}
    ejs = next((re.search(r"==\s*([\w.]+)", r).group(1) for r in requires
                if r.replace("_", "-").startswith("yt-dlp-ejs") and "==" in r), None)
    if ejs and vtuple(installed_version("yt-dlp-ejs")) != vtuple(ejs):
        e_ver, e_url, e_sha, _ = _pypi_wheel("yt-dlp-ejs", ejs)
        _install_wheel("yt-dlp-ejs", e_ver, e_url, e_sha)
    _install_wheel("yt-dlp", ver, url, sha)
    return {"updated": True, "version": ver}


# --------------------------------------------------------------- app update --

def latest_release():
    j = _get_json(f"{GITHUB_API}/repos/{REPO}/releases/latest")
    return {"version": j["tag_name"].lstrip("v"), "tag": j["tag_name"], "url": j["html_url"],
            "notes": (j.get("body") or "")[:2000],
            "assets": {a["name"]: a["browser_download_url"] for a in j.get("assets", [])}}


def _sums(release):
    url = release["assets"].get("SHA256SUMS.txt")
    if not url:
        raise RuntimeError("release has no SHA256SUMS.txt")
    out = {}
    for line in _get(url).decode().splitlines():
        parts = line.split()
        if len(parts) == 2:
            out[parts[1].lstrip("*")] = parts[0].lower()
    return out


def _verified(release, name):
    url = release["assets"].get(name)
    if not url:
        raise RuntimeError(f"release has no {name}")
    data = _get(url, timeout=600)
    if hashlib.sha256(data).hexdigest() != _sums(release).get(name):
        raise RuntimeError(f"{name}: download didn't match the release checksum")
    return data


def install_app_update(release, app_dir):
    """Replace FastGrab with the release's files. Takes effect after a restart."""
    if getattr(sys, "frozen", False):
        exe = sys.executable
        name = "FastGrab.exe" if "cli" not in os.path.basename(exe).lower() else "fastgrab-cli.exe"
        data = _verified(release, name)
        new, old = exe + ".new", exe + ".old"
        with open(new, "wb") as f:
            f.write(data)
        if os.path.exists(old):
            os.remove(old)
        os.replace(exe, old)               # a running .exe can be renamed, not overwritten
        os.replace(new, exe)
        return
    data = _verified(release, "fastgrab-source.zip")
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = set(zf.namelist())
        missing = [f for f in SOURCE_FILES if f not in names]
        if missing:
            raise RuntimeError(f"update is missing {', '.join(missing)}")
        backup = os.path.join(app_dir, ".previous")
        shutil.rmtree(backup, ignore_errors=True)
        for rel in sorted(names):
            if rel.endswith("/") or not (rel in SOURCE_FILES or rel.startswith(("ui/", "extension/"))):
                continue
            target = os.path.join(app_dir, *rel.split("/"))
            if os.path.exists(target):
                b = os.path.join(backup, *rel.split("/"))
                os.makedirs(os.path.dirname(b), exist_ok=True)
                shutil.copy2(target, b)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            tmp = target + ".new"
            with open(tmp, "wb") as f:
                f.write(zf.read(rel))
            os.replace(tmp, target)


def cleanup_old_exe():
    if getattr(sys, "frozen", False):
        try:
            os.remove(sys.executable + ".old")
        except OSError:
            pass


# ---------------------------------------------------------------- the state --

class Updater:
    """Runs checks in the background and keeps a status the UI can show."""

    def __init__(self, current_version, app_dir):
        self.current = current_version
        self.app_dir = app_dir
        self.lock = threading.Lock()
        self.state = {"busy": None, "error": None, "last_check": None,
                      "app": {"current": current_version, "latest": None, "url": None,
                              "notes": "", "available": False, "installed": False},
                      "video": {"version": None, "updated_to": None},
                      "restart_needed": False}
        self._release = None

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.state))

    def _run(self, busy, fn):
        with self.lock:
            if self.state["busy"]:
                return False
            self.state.update(busy=busy, error=None)

        def go():
            try:
                fn()
            except Exception as e:
                with self.lock:
                    self.state["error"] = str(e)[:300] or e.__class__.__name__
            finally:
                with self.lock:
                    self.state["busy"] = None
        threading.Thread(target=go, daemon=True).start()
        return True

    def check(self, video=True, app=True):
        def work():
            if video:
                r = update_video_support()
                with self.lock:
                    self.state["video"]["version"] = r["version"]
                    if r["updated"]:
                        self.state["video"]["updated_to"] = r["version"]
                        self.state["restart_needed"] = True
            if app:
                rel = latest_release()
                self._release = rel
                with self.lock:
                    self.state["app"].update(latest=rel["version"], url=rel["url"], notes=rel["notes"],
                                             available=vtuple(rel["version"]) > vtuple(self.current))
            with self.lock:
                self.state["last_check"] = time.time()
        return self._run("checking", work)

    def install_app(self):
        def work():
            rel = self._release or latest_release()
            if vtuple(rel["version"]) <= vtuple(self.current):
                raise RuntimeError("FastGrab is already up to date")
            install_app_update(rel, self.app_dir)
            with self.lock:
                self.state["app"]["installed"] = True
                self.state["restart_needed"] = True
        return self._run("installing", work)
