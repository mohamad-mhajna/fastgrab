#!/usr/bin/env python3
"""
Local test server for fastgrab.

    python tests/server.py --size-mb 40 --rate-kb 2048

  /file.bin          supports Range, each connection throttled to --rate-kb KB/s
  /norange/file.bin  ignores Range (always 200) to test single-connection mode
"""
import argparse
import hashlib
import os
import re
import random
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, default=8765)
ap.add_argument("--size-mb", type=int, default=40)
ap.add_argument("--rate-kb", type=int, default=2048, help="per-connection limit")
ap.add_argument("--drop", type=float, default=0.0, help="chance to cut a connection per 64 KB chunk")
args = ap.parse_args()

DATA = os.urandom(args.size_mb * 1024 * 1024)
ETAG = '"' + hashlib.sha256(DATA).hexdigest()[:16] + '"'
print("sha256:", hashlib.sha256(DATA).hexdigest(), flush=True)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        norange = self.path.startswith("/norange")
        total = len(DATA)
        start, end = 0, total - 1
        rng = self.headers.get("Range")
        m = re.match(r"bytes=(\d+)-(\d*)", rng or "")
        if m and not norange:
            start = int(m.group(1))
            end = min(int(m.group(2)) if m.group(2) else total - 1, total - 1)
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        else:
            self.send_response(200)
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("ETag", ETAG)
        self.send_header("Accept-Ranges", "none" if norange else "bytes")
        self.end_headers()
        step = 64 * 1024
        per_step = step / (args.rate_kb * 1024)
        pos = start
        try:
            while pos <= end:
                n = min(step, end - pos + 1)
                if args.drop and random.random() < args.drop:
                    self.close_connection = True
                    return                      # simulate a dropped connection
                self.wfile.write(DATA[pos:pos + n])
                pos += n
                time.sleep(per_step)
        except ConnectionError:      # client closed on purpose (Linux: BrokenPipe/Reset, Windows: Aborted)
            pass


ThreadingHTTPServer.daemon_threads = True
ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
