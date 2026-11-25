#!/usr/bin/env python3
"""
fastgrab - a tiny multi-connection download manager (Prism/IDM-style engine).

Pure Python standard library, no pip installs needed.

    python fastgrab.py URL                 # 8 connections, saves to current folder
    python fastgrab.py URL -c 16 -o D:/Downloads
    python fastgrab.py URL -H "Cookie: a=b" -H "Referer: https://site.com"

Press Ctrl+C to pause. Run the same command again to resume.

How it works (same ideas as Prism's engine):
  1. Probe:   GET with "Range: bytes=0-". A 206 proves range support and gives the
              size, and that same response keeps streaming as connection #1.
  2. Steal:   every other connection takes the segment with the most bytes left
              and splits it in the middle. The victim notices its end moved and
              stops there. So all connections stay busy until the very end.
  3. Write:   each connection writes straight into "<name>.fgpart" at the right
              offset (no merge step), then the file is renamed when complete.
  4. Resume:  progress is saved to "<name>.fgpart.json" every second, in the
              order snapshot -> fsync data -> save json, so the saved state never
              claims bytes that aren't on disk, even after a crash or power cut.
"""
import argparse
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from email.message import Message

__version__ = "1.0.0"

CHUNK = 64 * 1024            # bytes read per socket read
MIN_SPLIT = 512 * 1024       # never create segments smaller than this
ALIGN = 4096                 # split points are 4 KiB aligned
MAX_RETRIES = 5              # consecutive failures (without progress) per connection
TIMEOUT = 20                 # socket timeout, seconds
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) fastgrab/1.0"  # keep in sync with __version__


# ----------------------------------------------------------------- segments --

class Segment:
    """Byte range [pos, end). `claim` is how far the owner has reserved for
    writing; `pos` is how far is actually written. Splits use `claim`, saved
    state uses `pos`, so neither can ever be wrong."""
    __slots__ = ("start", "pos", "claim", "end", "owner")

    def __init__(self, pos, end):
        self.start = self.pos = self.claim = pos
        self.end = end
        self.owner = None

    def done(self):
        return self.pos >= self.end


class SegmentTable:
    def __init__(self, segments, splittable=True):
        self.lock = threading.Lock()
        self.segs = segments
        self.splittable = splittable

    def acquire(self, worker_id):
        """Find work for an idle connection. Returns a Segment or None."""
        with self.lock:
            # 1. an unowned unfinished segment (from resume, or a failed worker)
            free = [s for s in self.segs if s.owner is None and not s.done()]
            if free:
                seg = max(free, key=lambda s: s.end - s.claim)
                seg.owner = worker_id
                seg.claim = seg.pos
                return seg
            if not self.splittable:
                return None
            # 2. steal: split the owned segment with the largest remainder
            owned = [s for s in self.segs
                     if s.owner is not None and s.end - s.claim >= 2 * MIN_SPLIT]
            if not owned:
                return None
            victim = max(owned, key=lambda s: s.end - s.claim)
            mid = (victim.claim + (victim.end - victim.claim) // 2) // ALIGN * ALIGN
            if not victim.claim < mid < victim.end:
                return None
            piece = Segment(mid, victim.end)
            piece.owner = worker_id
            victim.end = mid                     # victim will stop here
            self.segs.append(piece)
            return piece

    def claim(self, seg, n):
        """Reserve up to n bytes at the segment's cursor. Returns (offset, allowed)."""
        with self.lock:
            allowed = max(0, min(n, seg.end - seg.claim))
            offset = seg.claim
            seg.claim += allowed
            return offset, allowed

    def commit(self, seg, n):
        with self.lock:
            seg.pos += n

    def room(self, seg):
        with self.lock:
            return seg.end - seg.claim

    def release(self, seg):
        with self.lock:
            seg.owner = None
            seg.claim = seg.pos

    def snapshot(self):
        with self.lock:
            return [[s.pos, s.end] for s in self.segs if not s.done()]

    def all_done(self):
        with self.lock:
            return all(s.done() for s in self.segs)


# --------------------------------------------------------------------- http --

class FatalError(Exception):
    pass


# Local addresses never go through a proxy (a system proxy can't reach your own machine).
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


def open_url(url, headers, start=None, end=None):
    h = {"User-Agent": UA, "Accept-Encoding": "identity", **headers}
    if start is not None:
        h["Range"] = f"bytes={start}-" + ("" if end is None else str(end))
    req = urllib.request.Request(url, headers=h)
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    opener = _DIRECT if host in _LOCAL_HOSTS or host.startswith("127.") else None
    try:
        if opener:
            return opener.open(req, timeout=TIMEOUT)
        return urllib.request.urlopen(req, timeout=TIMEOUT)
    except urllib.error.HTTPError as e:
        if e.code in (401, 404, 410):
            raise FatalError(f"HTTP {e.code} {e.reason}") from None
        raise


def parse_probe(resp, url):
    """Read size, range support, validators and filename from the first response."""
    info = {"final_url": resp.geturl(), "etag": resp.headers.get("ETag"),
            "last_modified": resp.headers.get("Last-Modified"),
            "size": None, "resumable": False}
    cr = resp.headers.get("Content-Range", "")
    m = re.match(r"bytes\s+0-\d+/(\d+)", cr)
    if resp.status == 206 and m:
        info["size"] = int(m.group(1))
        info["resumable"] = True
    elif resp.headers.get("Content-Length"):
        info["size"] = int(resp.headers["Content-Length"])
    info["filename"] = pick_filename(resp, url)
    return info


def pick_filename(resp, url):
    name = None
    cd = resp.headers.get("Content-Disposition")
    if cd:
        msg = Message()
        msg["content-disposition"] = cd
        name = msg.get_filename()
    if not name:
        path = urllib.parse.urlparse(resp.geturl()).path
        name = urllib.parse.unquote(os.path.basename(path))
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "").strip(" .")
    return name or "download.bin"


# ------------------------------------------------------------------ download --

class Download:
    def __init__(self, url, out_dir=".", connections=8, headers=None):
        self.url = url
        self.out_dir = out_dir
        self.connections = max(1, connections)
        self.headers = headers or {}
        self.stop = threading.Event()
        self.fatal = None
        self.downloaded = 0          # bytes on disk (including from earlier sessions)
        self.count_lock = threading.Lock()
        self.active = 0
        self.t_start = time.time()
        self.session_start_bytes = 0   # bytes already on disk when this session began

    # -- setup -----------------------------------------------------------------
    def start(self):
        probe = open_url(self.url, self.headers, start=0)
        info = parse_probe(probe, self.url)
        self.info = info
        self.size = info["size"]
        self.final_path = os.path.join(self.out_dir, info["filename"])
        self.part_path = self.final_path + ".fgpart"
        self.state_path = self.part_path + ".json"

        print(f"File : {info['filename']}")
        print(f"Size : {fmt_size(self.size) if self.size else 'unknown'}")
        print(f"Resumable: {'yes' if info['resumable'] else 'no (single connection)'}")

        if not info["resumable"] or not self.size:
            return self.run_single(probe)

        segments = self.load_state()
        if segments is not None:
            probe.close()                 # resuming: every worker requests its own range
            first = None
        else:
            with open(self.part_path, "wb") as f:
                f.truncate(self.size)     # pre-allocate the full file
            seg = Segment(0, self.size)
            seg.owner = 0                 # reserved for connection #1 (the probe)
            segments = [seg]
            first = (seg, probe)          # probe body becomes connection #1's stream
        self.table = SegmentTable(segments)
        self.downloaded = self.size - sum(s.end - s.pos for s in segments)
        self.session_start_bytes = self.downloaded
        self.t_start = time.time()
        return self.run_segmented(first)

    def load_state(self):
        if not (os.path.exists(self.state_path) and os.path.exists(self.part_path)):
            return None
        try:
            with open(self.state_path) as f:
                st = json.load(f)
        except (OSError, ValueError):
            return None
        same = (st.get("size") == self.size and
                st.get("etag") == self.info["etag"] and
                st.get("last_modified") == self.info["last_modified"])
        if not same:
            print("File changed on the server since last time, starting over.")
            return None
        print("Resuming previous download.")
        return [Segment(p, e) for p, e in st["segments"]]

    def save_state(self, data_fd):
        segs = self.table.snapshot()      # 1. snapshot (never ahead of real writes)
        os.fsync(data_fd)                 # 2. make sure those bytes are on disk
        st = {"url": self.url, "size": self.size, "etag": self.info["etag"],
              "last_modified": self.info["last_modified"], "segments": segs}
        tmp = self.state_path + ".tmp"    # 3. write state atomically
        with open(tmp, "w") as f:
            json.dump(st, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.state_path)

    # -- segmented mode ----------------------------------------------------------
    def run_segmented(self, first):
        threads = []
        for i in range(self.connections):
            t = threading.Thread(target=self.worker, args=(i, first if i == 0 else None))
            threads.append(t)
            t.start()
            time.sleep(0.05)              # stagger connections a little

        data_fd = os.open(self.part_path, os.O_RDWR | getattr(os, "O_BINARY", 0))
        self.save_state(data_fd)          # save immediately, so even an early crash can resume
        try:
            last_save = time.time()
            while any(t.is_alive() for t in threads):
                self.show_progress()
                time.sleep(0.25)
                if time.time() - last_save >= 1:
                    self.save_state(data_fd)
                    last_save = time.time()
        except KeyboardInterrupt:
            self.stop.set()
            print("\nPausing...")
            for t in threads:
                t.join()
        self.show_progress(final=True)

        if self.table.all_done():
            os.fsync(data_fd)
            os.close(data_fd)
            self.finish()
            return True
        self.save_state(data_fd)
        os.close(data_fd)
        if self.fatal:
            print(f"Failed: {self.fatal}")
        elif self.stop.is_set():
            print("Paused. Run the same command again to resume.")
        else:
            print("Failed: no connection could make progress. Run again to retry.")
        return False

    def worker(self, wid, first):
        fails = 0
        f = open(self.part_path, "r+b", buffering=0)
        try:
            while not self.stop.is_set():
                if first:
                    seg, resp = first
                    seg.owner = wid
                    first = None
                else:
                    seg = self.table.acquire(wid)
                    if seg is None:
                        return                       # nothing left to steal
                    try:
                        resp = open_url(self.info["final_url"], self.headers,
                                        seg.claim, seg.end - 1)
                        if resp.status != 206:
                            raise IOError("server ignored Range")
                    except FatalError as e:
                        self.fatal = str(e)
                        self.stop.set()
                        return
                    except Exception:
                        self.table.release(seg)
                        fails += 1
                        if fails > MAX_RETRIES:
                            return
                        time.sleep(min(30, 0.5 * 2 ** fails))
                        continue
                self.bump_active(+1)
                try:
                    while not self.stop.is_set():
                        room = self.table.room(seg)
                        if room <= 0:
                            break                    # done, or someone stole our tail
                        data = resp.read(min(CHUNK, room))
                        if not data:
                            raise IOError("connection closed early")
                        offset, allowed = self.table.claim(seg, len(data))
                        if allowed:
                            f.seek(offset)
                            f.write(data[:allowed])  # positioned write
                            self.table.commit(seg, allowed)
                            self.add_bytes(allowed)
                            fails = 0
                    resp.close()                     # drop bytes we no longer want
                    if not seg.done():
                        self.table.release(seg)
                except Exception:
                    resp.close()
                    self.table.release(seg)
                    fails += 1
                    if fails > MAX_RETRIES:
                        return
                    time.sleep(min(30, 0.5 * 2 ** fails))
                finally:
                    self.bump_active(-1)
        finally:
            f.close()

    # -- single-connection mode (server has no Range support) --------------------
    def run_single(self, resp):
        try:
            with open(self.part_path, "wb") as f:
                self.bump_active(+1)
                while True:
                    data = resp.read(CHUNK)
                    if not data:
                        break
                    f.write(data)
                    self.add_bytes(len(data))
                    self.show_progress()
                f.flush()
                os.fsync(f.fileno())
        except KeyboardInterrupt:
            print("\nStopped. This server doesn't support resume, so it will restart from 0.")
            return False
        finally:
            resp.close()
        if self.size and self.downloaded != self.size:
            print("\nFailed: download was cut short.")
            return False
        self.show_progress(final=True)
        self.finish()
        return True

    # -- helpers -------------------------------------------------------------------
    def finish(self):
        target = self.final_path
        base, ext = os.path.splitext(target)
        n = 1
        while os.path.exists(target):
            target = f"{base} ({n}){ext}"
            n += 1
        os.replace(self.part_path, target)
        if os.path.exists(self.state_path):
            os.remove(self.state_path)
        took = max(time.time() - self.t_start, 1e-6)
        got = self.downloaded - self.session_start_bytes
        print(f"Done -> {target}")
        print(f"Time : {fmt_time(took)} ({took:.1f} s), average {fmt_size(got / took)}/s")

    def add_bytes(self, n):
        with self.count_lock:
            self.downloaded += n

    def bump_active(self, d):
        with self.count_lock:
            self.active += d

    _hist = None

    def show_progress(self, final=False):
        now = time.time()
        if self._hist is None:
            self._hist = []
        self._hist.append((now, self.downloaded))
        self._hist = [(t, b) for t, b in self._hist if now - t <= 3]
        t0, b0 = self._hist[0]
        speed = (self.downloaded - b0) / (now - t0) if now > t0 else 0
        if self.size:
            pct = self.downloaded / self.size
            bar = "#" * int(pct * 30) + "-" * (30 - int(pct * 30))
            left = self.size - self.downloaded
            eta = fmt_time(left / speed) if speed > 0 else "--:--"
            line = (f"[{bar}] {pct * 100:5.1f}%  {fmt_size(self.downloaded)}/"
                    f"{fmt_size(self.size)}  {fmt_size(speed)}/s  ETA {eta}  "
                    f"conns {self.active}")
        else:
            line = f"{fmt_size(self.downloaded)}  {fmt_size(speed)}/s"
        sys.stdout.write("\r" + line.ljust(100))
        if final:
            sys.stdout.write("\n")
        sys.stdout.flush()


def fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024


def fmt_time(s):
    s = int(s)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def main():
    ap = argparse.ArgumentParser(description="Tiny multi-connection download manager")
    ap.add_argument("--version", action="version", version=f"fastgrab {__version__}")
    ap.add_argument("url")
    ap.add_argument("-c", "--connections", type=int, default=8)
    ap.add_argument("-o", "--out", default=".", help="folder to save into")
    ap.add_argument("-H", "--header", action="append", default=[],
                    help='extra header, e.g. -H "Cookie: a=b" (repeatable)')
    a = ap.parse_args()
    headers = {}
    for h in a.header:
        k, _, v = h.partition(":")
        headers[k.strip()] = v.strip()
    os.makedirs(a.out, exist_ok=True)
    try:
        ok = Download(a.url, a.out, a.connections, headers).start()
    except FatalError as e:
        print(f"Failed: {e}")
        ok = False
    except KeyboardInterrupt:
        print("\nCancelled.")
        ok = False
    except (urllib.error.URLError, OSError) as e:
        print(f"Failed: {e}")
        ok = False
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
