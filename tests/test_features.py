"""
Tests for the scheduler, the when-done power actions, settings/languages and the
browser-extension API. Uses a fake clock file (FASTGRAB_FAKE_CLOCK) and dry-run
power actions (FASTGRAB_DRY_POWER), so nothing really sleeps or shuts down.
"""
import json
import os
import sys
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_app import AppBase, OPENER, sha256  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fastgrab_app  # noqa: E402


class WindowMath(unittest.TestCase):
    def test_in_window(self):
        f = fastgrab_app.in_window
        self.assertTrue(f(2 * 60 + 30, "02:00", "08:00"))
        self.assertFalse(f(9 * 60, "02:00", "08:00"))
        self.assertFalse(f(8 * 60, "02:00", "08:00"))          # stop is exclusive
        self.assertTrue(f(23 * 60 + 30, "23:00", "07:00"))     # across midnight
        self.assertTrue(f(6 * 60, "23:00", "07:00"))
        self.assertFalse(f(12 * 60, "23:00", "07:00"))
        self.assertTrue(f(12 * 60, "05:00", "05:00"))          # all day

    def test_hhmm(self):
        self.assertEqual(fastgrab_app.hhmm("7:5", "x"), "07:05")
        self.assertEqual(fastgrab_app.hhmm("25:00", "02:00"), "02:00")
        self.assertEqual(fastgrab_app.hhmm("abc", "02:00"), "02:00")


class SaveRace(unittest.TestCase):
    def test_parallel_saves_never_fail(self):
        """Downloads finishing on several threads save at the same time; this used
        to crash the app with FileNotFoundError (two saves sharing one temp file)."""
        import tempfile
        import threading
        m = fastgrab_app.Manager(os.path.join(tempfile.mkdtemp(), "downloads.json"))
        errors = []

        def go():
            for _ in range(100):
                try:
                    m.save()
                except Exception as e:      # noqa: BLE001
                    errors.append(repr(e))
        ts = [threading.Thread(target=go) for _ in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(errors, [])


class FeatureTest(AppBase):
    RATE_KB = 400
    EXTRA_ENV = {"FASTGRAB_POWER_COUNTDOWN": "3"}

    @classmethod
    def setUpClass(cls):
        cls.clock = os.path.join(__import__("tempfile").mkdtemp(), "clock")
        cls.set_clock("12:00")
        cls.EXTRA_ENV = {**cls.EXTRA_ENV, "FASTGRAB_FAKE_CLOCK": cls.clock}
        super().setUpClass()

    @classmethod
    def set_clock(cls, t):
        with open(cls.clock, "w") as f:
            f.write(t)

    def state(self):
        return self.call("/api/state")

    def test_1_settings_and_language(self):
        s = self.call("/api/settings", {"lang": "ar", "max_active": 2, "connections": 12})
        self.assertEqual((s["lang"], s["max_active"], s["connections"]), ("ar", 2, 12))
        s = self.call("/api/settings", {"lang": "xx", "max_active": 99})
        self.assertEqual((s["lang"], s["max_active"]), ("ar", 10))    # invalid ignored / clamped
        self.call("/api/settings", {"lang": "en", "max_active": 3, "connections": 8})
        self.assertNotIn("last_update_check", self.state()["settings"])

    def test_2_schedule_holds_then_starts_then_pauses(self):
        self.call("/api/settings", {"schedule": {"enabled": True, "start": "02:00", "stop": "03:00"}})
        self.assertFalse(self.state()["schedule_open"])
        id = self.call("/api/add", {"url": self.srv.url("/sched.bin"), "dir": self.downloads})["ids"][0]
        time.sleep(2.5)
        self.assertEqual(self.item(id)["status"], "queued", "must wait for the schedule")
        self.set_clock("02:30")                                  # window opens
        self.wait_status(id, "downloading", timeout=10)
        time.sleep(1.5)
        self.set_clock("03:10")                                  # window closes
        it = self.wait_status(id, "queued", timeout=15)
        self.assertGreater(it["downloaded"], 0, "progress is kept for next time")
        self.call("/api/action", {"id": id, "action": "start-now"})   # override the schedule
        it = self.wait_status(id, "completed", timeout=90)
        self.assertEqual(sha256(it["path"]), self.srv.sha)
        self.call("/api/settings", {"schedule": {"enabled": False}})
        self.set_clock("12:00")

    def test_3_when_done_countdown_can_be_cancelled(self):
        self.call("/api/settings", {"when_done": "shutdown"})
        id = self.call("/api/add", {"url": self.srv.url("/wd1.bin"), "dir": self.downloads,
                                    "connections": 16})["ids"][0]
        self.wait_status(id, "completed", timeout=90)
        end = time.time() + 6
        while time.time() < end and not self.state()["power"]["action"]:
            time.sleep(0.2)
        st = self.state()
        self.assertEqual(st["power"]["action"], "shutdown")
        self.call("/api/cancel-power", {})
        time.sleep(4)
        st = self.state()
        self.assertIsNone(st["power"]["action"])
        self.assertEqual(st["settings"]["when_done"], "none")
        self.assertEqual(st["power"]["log"], [], "cancelled: nothing may run")

    def test_4_when_done_runs_once(self):
        self.call("/api/settings", {"when_done": "sleep"})
        id = self.call("/api/add", {"url": self.srv.url("/wd2.bin"), "dir": self.downloads,
                                    "connections": 16})["ids"][0]
        self.wait_status(id, "completed", timeout=90)
        end = time.time() + 12
        while time.time() < end and not self.state()["power"]["log"]:
            time.sleep(0.3)
        st = self.state()
        self.assertEqual([e["action"] for e in st["power"]["log"]], ["sleep"])
        self.assertEqual(st["settings"]["when_done"], "none", "one time only")

    # -- browser extension API ----------------------------------------------------
    def ext(self, path, body=None, key=True):
        h = {"X-FastGrab-Key": self.state()["settings"]["ext_key"] if key is True else (key or "")}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, headers=h)
        with OPENER.open(req, timeout=15) as r:
            return json.load(r)

    def test_5_extension_needs_the_pairing_key(self):
        self.assertTrue(self.ext("/ext/ping")["ok"])
        for k in (False, "wrong-key"):
            with self.assertRaises(urllib.error.HTTPError) as e:
                self.ext("/ext/ping", key=k)
            self.assertEqual(e.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError):      # the UI token isn't enough either
            self.ext("/ext/add", {"url": self.srv.url()}, key=self.token)

    def test_6_extension_passes_cookies(self):
        r = self.ext("/ext/add", {"url": self.srv.url("/needs-cookie/members.bin"),
                                  "cookie": "session=ok", "referrer": "https://example.com/"})
        it = self.wait_status(r["id"], "completed", timeout=90)
        self.assertEqual(it["source"], "browser")
        self.assertEqual(sha256(it["path"]), self.srv.sha)
        # the same link without the cookie is refused by the "site"
        r = self.ext("/ext/add", {"url": self.srv.url("/needs-cookie/again.bin")})
        self.wait_status(r["id"], "failed", timeout=30)

    def test_7_extension_video_link_opens_picker(self):
        r = self.ext("/ext/add", {"url": "https://www.youtube.com/watch?v=abc"})
        self.assertTrue(r["picker"])
        ev = self.state()["events"][-1]
        self.assertEqual((ev["type"], ev["url"]), ("video", "https://www.youtube.com/watch?v=abc"))

    def test_8_new_pairing_key(self):
        old = self.state()["settings"]["ext_key"]
        self.call("/api/settings", {"new_ext_key": True})
        with self.assertRaises(urllib.error.HTTPError):
            self.ext("/ext/ping", key=old)


if __name__ == "__main__":
    unittest.main(verbosity=2)
