"""
End-to-end test of the app window (ui/index.html) in a real Chrome. Needs Playwright:

    CHROME=/path/to/chrome python tests/ui_e2e.py

Checks the downloads list through the sidebar filters and the search box: rows
must come back when you return to "All downloads" (they used to stay hidden after
a filter had faded them out), and no duplicate rows may pile up.

Then the update popup, against a fake GitHub offering v9.9.9, with FastGrab running
from a temporary copy: the popup appears with the release notes, "Later" hides it
(also after reloading the window), "Update now" installs, restarts by itself and
the window comes back on v9.9.9 with the downloads list intact; Arabic layout.
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_fastgrab import ROOT, Server  # noqa: E402
from test_update import FAKE_YTDLP, FakeServer, make_wheel  # noqa: E402

CHROME = os.environ.get("CHROME", "/usr/bin/google-chrome")
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""), flush=True)


def main():
    srv = Server(size_mb=2, rate_kb=4096)
    tmp = tempfile.mkdtemp()
    env = dict(os.environ, HOME=tmp, USERPROFILE=tmp, APPDATA=tmp,
               FASTGRAB_NO_AUTO_UPDATE="1", FASTGRAB_NO_BROWSER="1")
    app = subprocess.Popen([sys.executable, os.path.join(ROOT, "fastgrab_app.py"), "--no-window", "--port", "0"],
                           env=env, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    data = os.path.join(tmp, "FastGrab") if os.name == "nt" else os.path.join(tmp, ".config", "fastgrab")
    info = os.path.join(data, "running.json")
    try:
        for _ in range(300):
            if os.path.exists(info):
                break
            time.sleep(0.1)
        time.sleep(0.5)
        with open(info) as f:
            r = json.load(f)
        base = f"http://127.0.0.1:{r['port']}"

        def call(path, body=None):
            req = urllib.request.Request(base + path, headers={"X-Token": r["token"], "Content-Type": "application/json"},
                                         data=None if body is None else json.dumps(body).encode())
            with OPENER.open(req, timeout=10) as resp:
                return json.load(resp)

        names = ("movie.mp4", "song.mp3", "setup.exe", "report.pdf")
        for n in names:
            call("/api/add", {"url": srv.url("/" + n), "dir": tmp})
        end = time.time() + 60
        while time.time() < end and not all(i["status"] == "completed" for i in call("/api/state")["items"]):
            time.sleep(0.3)

        with sync_playwright() as p:
            b = p.chromium.launch(executable_path=CHROME)
            pg = b.new_page(viewport={"width": 1240, "height": 800})
            errors = []
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.goto(f"{base}/?t={r['token']}")
            pg.wait_for_timeout(1500)
            visible = lambda: pg.evaluate(
                "[...document.querySelectorAll('#rows .row')].filter(e=>!e.classList.contains('leaving') && e.offsetHeight>20).length")
            in_page = lambda: pg.evaluate("document.querySelectorAll('#rows .row').length")
            check("all 4 downloads listed at start", visible() == 4, visible())
            expect = {"active": 0, "completed": 4, "stopped": 0, "video": 1, "audio": 1,
                      "archive": 0, "doc": 1, "app": 1}
            for f, n in expect.items():
                pg.click(f'.nav[data-f="{f}"]')
                pg.wait_for_timeout(700)
                got = visible()
                pg.click('.nav[data-f="all"]')
                pg.wait_for_timeout(700)
                check(f"filter '{f}' shows {n}, back to All shows 4", got == n and visible() == 4,
                      f"{got} then {visible()}")
            for f in ["video", "all", "audio", "completed", "all", "app", "all"] * 3:
                pg.click(f'.nav[data-f="{f}"]')
                pg.wait_for_timeout(60)          # faster than the fade-out animation
            pg.wait_for_timeout(900)
            check("rapid filter clicks end with all 4, no duplicates", visible() == 4 and in_page() == 4,
                  f"{visible()} visible, {in_page()} in page")
            pg.fill("#q", "mp")
            pg.wait_for_timeout(700)
            found = visible()
            pg.fill("#q", "")
            pg.wait_for_timeout(900)
            check("search 'mp' finds 2, clearing shows 4", found == 2 and visible() == 4 and in_page() == 4,
                  f"{found} then {visible()}")
            check("no page errors", not errors, errors)
            b.close()
    finally:
        app.kill()
        srv.close()
    update_popup()
    restart_popup()
    print(f"\n{sum(results)}/{len(results)} checks passed")
    sys.exit(0 if all(results) else 1)


FILES = ("fastgrab.py", "fastgrab_app.py", "fastgrab_video.py", "fastgrab_update.py",
         "ui/index.html", "ui/fastgrab.ico")


def update_popup():
    import zipfile
    fake = FakeServer()
    appdir = tempfile.mkdtemp()                 # a copy: the update replaces these files
    for rel in FILES:
        os.makedirs(os.path.join(appdir, os.path.dirname(rel)), exist_ok=True)
        shutil.copy(os.path.join(ROOT, rel), os.path.join(appdir, rel))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for rel in FILES:
            with open(os.path.join(ROOT, rel), "rb") as f:
                data = f.read()
            if rel == "fastgrab.py":
                data = re.sub(rb'__version__ = "[^"]+"', b'__version__ = "9.9.9"', data)
            z.writestr(rel, data)
    fake.release("9.9.9", {"fastgrab-source.zip": buf.getvalue()})
    fake.routes["/gh/repos/mohamad-mhajna/fastgrab/releases/latest"]["body"] = \
        "## FastGrab 9.9.9\n\n**New**\n- Faster than ever\n- `Tray` icon"
    srv = Server(size_mb=1, rate_kb=4096)
    tmp = tempfile.mkdtemp()
    env = dict(os.environ, HOME=tmp, USERPROFILE=tmp, APPDATA=tmp, FASTGRAB_NO_AUTO_UPDATE="1",
               FASTGRAB_NO_BROWSER="1", FASTGRAB_GITHUB_API=fake.base + "/gh")
    log = open(os.path.join(tmp, "app.log"), "w")
    proc = subprocess.Popen([sys.executable, os.path.join(appdir, "fastgrab_app.py"), "--no-window", "--port", "0"],
                            env=env, cwd=appdir, stdout=log, stderr=subprocess.STDOUT)
    data = os.path.join(tmp, "FastGrab") if os.name == "nt" else os.path.join(tmp, ".config", "fastgrab")
    info = os.path.join(data, "running.json")
    try:
        for _ in range(300):
            if os.path.exists(info):
                break
            time.sleep(0.1)
        time.sleep(0.5)
        with open(info) as f:
            r = json.load(f)
        base = f"http://127.0.0.1:{r['port']}"

        def call(path, body=None):
            req = urllib.request.Request(base + path, headers={"X-Token": r["token"], "Content-Type": "application/json"},
                                         data=None if body is None else json.dumps(body).encode())
            with OPENER.open(req, timeout=10) as resp:
                return json.load(resp)
        call("/api/add", {"url": srv.url("/keep-me.bin"), "dir": tmp})

        with sync_playwright() as p:
            b = p.chromium.launch(executable_path=CHROME)
            pg = b.new_page(viewport={"width": 1240, "height": 800})
            errors = []
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.goto(f"{base}/?t={r['token']}")
            pg.wait_for_timeout(1200)
            shown = lambda: pg.evaluate("document.querySelector('#umodal').classList.contains('on')")
            check("no popup while up to date", not shown())
            call("/api/update/check", {"video": False, "app": True})
            pg.wait_for_timeout(2000)
            check("popup appears when an update is found", shown())
            check("popup title and version", pg.inner_text("#u-ptitle") == "Update available"
                  and "9.9.9" in pg.inner_text("#u-ptext"), pg.inner_text("#u-ptext"))
            notes = pg.inner_text("#u-pnotes")
            check("release notes shown cleanly", "• Faster than ever" in notes and "**" not in notes
                  and "##" not in notes and "`" not in notes, notes)
            check("buttons: Later / Update now", pg.inner_text("#u-plater") == "Later"
                  and pg.inner_text("#u-pgo") == "Update now")
            pg.screenshot(path=os.path.join(tempfile.gettempdir(), "update-popup-en.png"))
            pg.click("#u-plater")
            pg.wait_for_timeout(1200)
            check("Later closes it", not shown())
            pg.reload()
            pg.wait_for_timeout(1800)
            check("stays hidden after Later (also after reopening the window)", not shown())
            pg.click("#openSettings")
            pg.wait_for_timeout(400)
            pg.click('.stab[data-p="updates"]')
            pg.wait_for_timeout(500)
            check("Settings → Updates still offers it", "9.9.9" in pg.inner_text("#u-box"))
            pg.keyboard.press("Escape")
            # Arabic: right-to-left popup (a different key brings it back)
            call("/api/settings", {"lang": "ar", "update_later": "something-else"})
            pg.wait_for_timeout(1800)
            check("popup back when not snoozed, in Arabic", shown() and pg.inner_text("#u-pgo") == "التحديث الآن",
                  pg.inner_text("#u-pgo"))
            check("Arabic layout is right-to-left", pg.evaluate("document.documentElement.dir") == "rtl")
            pg.screenshot(path=os.path.join(tempfile.gettempdir(), "update-popup-ar.png"))
            call("/api/settings", {"lang": "en"})
            pg.wait_for_timeout(1200)
            pid_before = r["pid"]
            pg.click("#u-pgo")                    # Update now
            pg.wait_for_timeout(300)
            busy = pg.evaluate("document.querySelector('#umodal').classList.contains('busy')")
            check("shows progress while installing", busy)
            ok = False
            for _ in range(120):
                pg.wait_for_timeout(500)
                try:
                    if pg.inner_text("#ver") == "v9.9.9":
                        ok = True
                        break
                except Exception:
                    pass
            check("installs, restarts by itself, window comes back on v9.9.9", ok)
            with open(info) as f:
                check("really a new process", json.load(f)["pid"] != pid_before)
            pg.wait_for_timeout(1500)
            check("no popup after updating", not shown())
            check("downloads list kept", "keep-me.bin" in pg.inner_text("#rows"))
            check("no page errors", not errors, errors)
            b.close()
    finally:
        proc.kill()
        try:
            with open(info) as f:
                os.kill(json.load(f)["pid"], 9)
        except Exception:
            pass
        srv.close()
        log.close()


def restart_popup():
    """Video support updated itself (fake PyPI with a newer yt-dlp): the popup asks
    to restart, and "Restart now" restarts FastGrab."""
    fake = FakeServer()
    fake.pypi("yt-dlp-ejs", "9.9", make_wheel({"yt_dlp_ejs/__init__.py": "V = '9.9'\n"}))
    fake.pypi("yt-dlp", "2099.1.1", make_wheel(FAKE_YTDLP), requires=['yt-dlp-ejs==9.9; extra == "default"'])
    appdir = tempfile.mkdtemp()
    for rel in FILES:
        os.makedirs(os.path.join(appdir, os.path.dirname(rel)), exist_ok=True)
        shutil.copy(os.path.join(ROOT, rel), os.path.join(appdir, rel))
    tmp = tempfile.mkdtemp()
    env = dict(os.environ, HOME=tmp, USERPROFILE=tmp, APPDATA=tmp, FASTGRAB_NO_AUTO_UPDATE="1",
               FASTGRAB_NO_BROWSER="1", FASTGRAB_PYPI=fake.base + "/pypi")
    proc = subprocess.Popen([sys.executable, os.path.join(appdir, "fastgrab_app.py"), "--no-window", "--port", "0"],
                            env=env, cwd=appdir, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    data = os.path.join(tmp, "FastGrab") if os.name == "nt" else os.path.join(tmp, ".config", "fastgrab")
    info = os.path.join(data, "running.json")
    try:
        for _ in range(300):
            if os.path.exists(info):
                break
            time.sleep(0.1)
        time.sleep(0.5)
        with open(info) as f:
            r = json.load(f)
        base = f"http://127.0.0.1:{r['port']}"
        req = urllib.request.Request(base + "/api/update/check", data=json.dumps({"video": True, "app": False}).encode(),
                                     headers={"X-Token": r["token"], "Content-Type": "application/json"})
        OPENER.open(req, timeout=10).read()
        with sync_playwright() as p:
            b = p.chromium.launch(executable_path=CHROME)
            pg = b.new_page(viewport={"width": 1240, "height": 800})
            pg.goto(f"{base}/?t={r['token']}")
            shown = lambda: pg.evaluate("document.querySelector('#umodal').classList.contains('on')")
            for _ in range(40):
                pg.wait_for_timeout(500)
                if shown():
                    break
            check("restart popup after video support updated itself", shown()
                  and pg.inner_text("#u-ptitle") == "Restart to finish updating"
                  and "2099.1.1" in pg.inner_text("#u-ptext") and pg.inner_text("#u-pgo") == "Restart now",
                  pg.inner_text("#u-ptitle"))
            pg.screenshot(path=os.path.join(tempfile.gettempdir(), "update-popup-restart.png"))
            pg.click("#u-pgo")
            ok = False
            for _ in range(60):
                pg.wait_for_timeout(500)
                try:
                    with open(info) as f:
                        new_pid = json.load(f)["pid"]
                    if new_pid != r["pid"] and not pg.evaluate("document.querySelector('#restarting').classList.contains('on')"):
                        ok = True
                        break
                except (OSError, ValueError):
                    pass
            check("Restart now restarts FastGrab and the window reconnects", ok)
            pg.wait_for_timeout(1500)
            check("no popup after the restart", not shown())
            b.close()
    finally:
        proc.kill()
        try:
            with open(info) as f:
                os.kill(json.load(f)["pid"], 9)
        except Exception:
            pass


if __name__ == "__main__":
    main()
