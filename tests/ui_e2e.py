"""
End-to-end test of the app window (ui/index.html) in a real Chrome. Needs Playwright:

    CHROME=/path/to/chrome python tests/ui_e2e.py

Checks the downloads list through the sidebar filters and the search box: rows
must come back when you return to "All downloads" (they used to stay hidden after
a filter had faded them out), and no duplicate rows may pile up.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_fastgrab import ROOT, Server  # noqa: E402

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
    print(f"\n{sum(results)}/{len(results)} checks passed")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
