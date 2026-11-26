"""
End-to-end and unit tests for fastgrab.   Run:  python -m unittest -v
Each test spins up tests/server.py on a free port and checks the downloaded file's SHA-256.
"""
import hashlib
import os
import random
import signal
import socket
import re
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import fastgrab  # noqa: E402

FASTGRAB = os.path.join(ROOT, "fastgrab.py")
SERVER = os.path.join(ROOT, "tests", "server.py")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


class Server:
    def __init__(self, size_mb=8, rate_kb=2048, drop=0.0):
        self.port = free_port()
        self.proc = subprocess.Popen(
            [sys.executable, SERVER, "--port", str(self.port), "--size-mb", str(size_mb),
             "--rate-kb", str(rate_kb), "--drop", str(drop)],
            stdout=subprocess.PIPE, text=True)
        self.sha = self.proc.stdout.readline().split()[1]
        for _ in range(100):                       # wait until it accepts connections
            try:
                socket.create_connection(("127.0.0.1", self.port), 0.5).close()
                break
            except OSError:
                time.sleep(0.1)
        else:
            self.close()
            raise RuntimeError(f"test server on port {self.port} never became reachable")

    def url(self, path="/file.bin"):
        return f"http://127.0.0.1:{self.port}{path}"

    def close(self):
        self.proc.kill()
        self.proc.wait()
        self.proc.stdout.close()


def run_fastgrab(url, out, *extra):
    return subprocess.run([sys.executable, FASTGRAB, url, "-o", out, *extra],
                          capture_output=True, text=True, timeout=120)


def took(result, fallback):
    """Seconds from fastgrab's own "Time : 0:05 (5.4 s)" line."""
    m = re.search(r"Time : .*\(([\d.]+) s\)", result.stdout)
    return float(m.group(1)) if m else fallback


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_multi_connection_matches_and_is_faster(self):
        srv = Server(size_mb=8, rate_kb=1024)        # 1 MB/s per connection
        try:
            t = time.time()
            r1 = run_fastgrab(srv.url(), os.path.join(self.tmp, "a"), "-c", "1")
            single = time.time() - t
            t = time.time()
            r8 = run_fastgrab(srv.url(), os.path.join(self.tmp, "b"), "-c", "8")
            multi = time.time() - t
            self.assertEqual(r1.returncode, 0, r1.stdout)
            self.assertEqual(r8.returncode, 0, r8.stdout)
            self.assertEqual(sha256(os.path.join(self.tmp, "a", "file.bin")), srv.sha)
            self.assertEqual(sha256(os.path.join(self.tmp, "b", "file.bin")), srv.sha)
            # use the download time fastgrab reports, not the wall clock: starting
            # Python takes over a second on slow CI machines and isn't download time
            single, multi = took(r1, single), took(r8, multi)
            self.assertLess(multi, single / 3, f"8 conns {multi:.1f}s vs 1 conn {single:.1f}s")
        finally:
            srv.close()

    def test_resume_after_hard_crash(self):
        srv = Server(size_mb=16, rate_kb=256)   # ~8 s total, so we kill it mid-way
        out = os.path.join(self.tmp, "c")
        try:
            p = subprocess.Popen([sys.executable, FASTGRAB, srv.url(), "-o", out],
                                 stdout=subprocess.DEVNULL)
            time.sleep(2)
            p.send_signal(signal.SIGKILL if hasattr(signal, "SIGKILL") else signal.SIGTERM)
            p.wait()
            state = os.path.join(out, "file.bin.fgpart.json")
            self.assertTrue(os.path.exists(state), "no resume state after crash")
            r = run_fastgrab(srv.url(), out)
            self.assertIn("Resuming", r.stdout)
            self.assertEqual(sha256(os.path.join(out, "file.bin")), srv.sha)
            self.assertFalse(os.path.exists(state), "state file left behind")
        finally:
            srv.close()

    def test_server_without_range_support(self):
        srv = Server(size_mb=4, rate_kb=8192)
        try:
            r = run_fastgrab(srv.url("/norange/file.bin"), self.tmp)
            self.assertIn("single connection", r.stdout)
            self.assertEqual(sha256(os.path.join(self.tmp, "file.bin")), srv.sha)
        finally:
            srv.close()

    def test_survives_dropped_connections(self):
        srv = Server(size_mb=8, rate_kb=8192, drop=0.02)
        try:
            r = run_fastgrab(srv.url(), self.tmp)
            self.assertEqual(r.returncode, 0, r.stdout)
            self.assertEqual(sha256(os.path.join(self.tmp, "file.bin")), srv.sha)
        finally:
            srv.close()


class SegmentTableTest(unittest.TestCase):
    def test_splits_never_overlap_or_leave_gaps(self):
        size = 50 * 1024 * 1024
        first = fastgrab.Segment(0, size)
        first.owner = 0
        table = fastgrab.SegmentTable([first])
        rng = random.Random(1)
        owned = {0: first}
        for step in range(2000):                  # random mix of progress + stealing
            wid = rng.randrange(8)
            seg = owned.get(wid)
            if seg is None or seg.done():
                seg = table.acquire(wid)
                if seg is None:
                    owned.pop(wid, None)
                    continue
                owned[wid] = seg
            off, n = table.claim(seg, rng.randrange(1, 300_000))
            table.commit(seg, n)
        segs = sorted(table.segs, key=lambda s: s.start)
        cursor = 0
        for s in segs:                            # must tile [0, size) exactly
            self.assertEqual(s.start, cursor)
            self.assertTrue(s.start <= s.pos <= s.end)
            cursor = s.end
        self.assertEqual(cursor, size)
        self.assertGreater(len(segs), 8, "work stealing never split anything")


if __name__ == "__main__":
    unittest.main(verbosity=2)
