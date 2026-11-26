"""
End-to-end test of the browser extension in a real Chrome, against a running
FastGrab and the local test server. Needs Playwright and a Chrome that can load
unpacked extensions (Chrome for Testing / Chromium):

    CHROME=/path/to/chrome python tests/extension_e2e.py
"""
import hashlib
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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXT = os.path.join(ROOT, "extension")
CHROME = os.environ.get("CHROME", "/usr/bin/google-chrome")
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""), flush=True)


home = tempfile.mkdtemp()
env = dict(os.environ, HOME=home, USERPROFILE=home, APPDATA=home, FASTGRAB_NO_AUTO_UPDATE="1")
srv = subprocess.Popen([sys.executable, os.path.join(ROOT, "tests", "server.py"), "--port", "8831",
                        "--size-mb", "8", "--rate-kb", "4000"], stdout=subprocess.PIPE, text=True)
SHA = srv.stdout.readline().split()[1]
app = subprocess.Popen([sys.executable, os.path.join(ROOT, "fastgrab_app.py"), "--no-window", "--port", "0"],
                       env=env, stdout=subprocess.PIPE, text=True)
m = re.search(r"http://127\.0\.0\.1:(\d+)/\?t=(\S+)", app.stdout.readline())
PORT, TOKEN = int(m.group(1)), m.group(2)


def api(path, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", headers={"X-Token": TOKEN, "Content-Type": "application/json"},
                                 data=None if body is None else json.dumps(body).encode())
    with OPENER.open(req, timeout=10) as r:
        return json.load(r)


def wait_item(pred, timeout=40):
    end = time.time() + timeout
    while time.time() < end:
        for it in api("/api/state")["items"]:
            if pred(it):
                return it
        time.sleep(0.3)
    return None


KEY = api("/api/state")["settings"]["ext_key"]
# a page with links, like a normal download site
page_dir = tempfile.mkdtemp()
profile = tempfile.mkdtemp()
try:
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(profile, executable_path=CHROME, headless=True,
                                                   accept_downloads=True,
                                                   args=["--no-sandbox", f"--disable-extensions-except={EXT}",
                                                         f"--load-extension={EXT}"])
        sw = ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event("serviceworker", timeout=15000)
        ext_id = sw.url.split("/")[2]

        # 1. popup before pairing
        pop = ctx.new_page()
        pop.goto(f"chrome-extension://{ext_id}/popup.html")
        pop.wait_for_timeout(1500)
        check("popup shows 'Not paired' before pairing", "Not paired" in pop.inner_text("#status"), pop.inner_text("#status"))

        # 2. pair through the popup like a user would
        sw.evaluate(f"chrome.storage.local.set({{port: {PORT}}})")
        pop.fill("#key", KEY)
        pop.click("#save")
        pop.wait_for_timeout(1500)
        check("popup shows 'Connected' after pasting the key", "Connected" in pop.inner_text("#status"), pop.inner_text("#status"))
        pop.close()

        # 3. clicking a download link on a website goes to FastGrab
        page = ctx.new_page()
        page.set_content(f'<a id="dl" href="http://127.0.0.1:8831/setup-tool.zip">Download</a>')
        page.click("#dl")
        it = wait_item(lambda i: (i["filename"] or "").startswith("setup-tool") and i["status"] == "completed")
        check("auto-captured download completed in FastGrab", it, "no item")
        if it:
            check("captured file is byte-identical", hashlib.sha256(open(it["path"], "rb").read()).hexdigest() == SHA)
            check("item is marked as coming from the browser", it["source"] == "browser")
        time.sleep(1)
        left = sw.evaluate("chrome.downloads.search({}).then(l => l.filter(d => d.url.includes('setup-tool')).length)")
        check("the browser's own download was removed", left == 0, f"{left} left")

        # 4. sites that need you logged in: the cookie travels with the download
        ctx.add_cookies([{"name": "session", "value": "ok", "domain": "127.0.0.1", "path": "/"}])
        page.set_content('<a id="dl2" href="http://127.0.0.1:8831/needs-cookie/members-file.zip">Members file</a>')
        page.click("#dl2")
        it = wait_item(lambda i: (i["filename"] or "").startswith("members-file") and i["status"] in ("completed", "failed"))
        check("login-only download works (cookies passed)", it and it["status"] == "completed", it and it["message"])

        # 5. pages and small files that aren't "downloads" stay in the browser
        n_before = len(api("/api/state")["items"])
        page.set_content('<a id="txt" href="http://127.0.0.1:8831/readme.txt" download>notes</a>')
        page.click("#txt")
        time.sleep(2)
        check("a .txt download is left to the browser", len(api("/api/state")["items"]) == n_before)

        # 6. right-click 'Download link with FastGrab'
        sw.evaluate("self.__fastgrab.onMenu({menuItemId:'fg-link', linkUrl:'http://127.0.0.1:8831/menu-file.iso', pageUrl:'http://127.0.0.1:8831/'})")
        it = wait_item(lambda i: (i["filename"] or "").startswith("menu-file") and i["status"] == "completed")
        check("right-click menu sends the link", it)

        # 7. right-click 'Download video' on a YouTube page opens the quality picker in the app
        sw.evaluate("self.__fastgrab.onMenu({menuItemId:'fg-video', pageUrl:'https://www.youtube.com/watch?v=dQw4w9WgXcQ'})")
        time.sleep(1)
        ev = api("/api/state")["events"]
        check("video page opens the picker in FastGrab", ev and ev[-1]["type"] == "video" and "youtube" in ev[-1]["url"])

        # 8. FastGrab closed: the browser downloads normally (nothing is lost)
        sw.evaluate("chrome.storage.local.set({port: 9})")      # nothing listens there
        page.set_content('<a id="dl3" href="http://127.0.0.1:8831/offline-test.zip">Download</a>')
        with page.expect_download(timeout=30000) as dl:
            page.click("#dl3")
        d = dl.value
        path = d.path()
        check("with FastGrab closed the browser downloads it itself",
              path and hashlib.sha256(open(path, "rb").read()).hexdigest() == SHA)

        # 9. Arabic / Hebrew popup is right-to-left
        ctx.close()
    for loc, word in (("ar", "متصل"), ("he", "מחובר")):
        prof = tempfile.mkdtemp()
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(prof, executable_path=CHROME, headless=True, locale=loc,
                                                       args=["--no-sandbox", f"--lang={loc}", f"--disable-extensions-except={EXT}",
                                                             f"--load-extension={EXT}"])
            sw = ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event("serviceworker", timeout=15000)
            sw.evaluate(f"chrome.storage.local.set({{port: {PORT}, key: '{KEY}'}})")
            pop = ctx.new_page()
            pop.goto(f"chrome-extension://{sw.url.split('/')[2]}/popup.html")
            pop.wait_for_timeout(1500)
            check(f"popup in {loc}: right-to-left and translated",
                  pop.evaluate("document.documentElement.dir") == "rtl" and word in pop.inner_text("#status"),
                  pop.inner_text("#status"))
            pop.screenshot(path=os.path.join(tempfile.gettempdir(), f"popup-{loc}.png"))
            ctx.close()
finally:
    app.terminate()
    srv.terminate()

ok = all(r for _, r in results)
print(f"\n{sum(r for _, r in results)}/{len(results)} extension checks passed")
sys.exit(0 if ok else 1)
