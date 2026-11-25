# fastgrab ⚡

[![tests](https://github.com/mohamadar8162-design/fastgrab/actions/workflows/tests.yml/badge.svg)](https://github.com/mohamadar8162-design/fastgrab/actions/workflows/tests.yml)
![python](https://img.shields.io/badge/python-3.10%2B-blue)
![deps](https://img.shields.io/badge/dependencies-none-brightgreen)
![license](https://img.shields.io/badge/license-MIT-green)

**Up to 10× faster downloads.** A multi-connection download manager with a desktop app and a command-line tool, built with **only the Python standard library**.

![FastGrab desktop app](assets/screenshot.png)

It speeds up downloads the same way IDM does: it splits a file into byte ranges, downloads them over several connections in parallel, and keeps every connection busy until the last byte by **stealing work** from the slowest one. If the download is interrupted (Ctrl+C, a dropped connection, even a crash or power cut), it resumes where it left off.

The **desktop app** shows every connection live in the *connections map*: each color is one connection filling its own part of the file. When one finishes, it takes over half of the slowest one's remaining bytes.

The **command-line tool** does the same in a terminal:

```
$ python fastgrab.py https://example.com/big.iso -c 8
File : big.iso
Size : 30.0 MB
Resumable: yes
[##############################] 100.0%  30.0 MB/30.0 MB  14.7 MB/s  ETA 0:00  conns 8
Done -> ./big.iso
```

## Benchmark

60 MB file from a test server that limits each connection to 2 MB/s (this kind of per-connection limit is common on real servers). Each setting was run twice, and both runs gave the same time:

| Connections | Time | Speed | vs. 1 connection |
|---|---|---|---|
| 1 (like a normal browser download) | 30.5 s | 2.0 MB/s | 1× |
| 8 (default) | 4.0 s | 15.1 MB/s | **7.6×** |
| 16 (`-c 16`) | 2.4 s | 25.2 MB/s | **12.7×** |

Every downloaded file had the same SHA-256 as the original.

> How much faster you get depends on the server and your connection. If a single connection already uses all of your bandwidth, extra connections can't add more speed.

## Features

**Desktop app**
- Dark glass interface with a downloads list, sidebar filters (status and file type) and search
- Live **connections map** (per-connection segment bars) and a **speed graph**
- Pause, resume, retry, cancel, open file, show in folder
- Up to 3 downloads at once, with the rest queued
- Paste a link anywhere with Ctrl+V, or drag a link onto the window
- Downloads that were running when you closed the window resume on the next start

**Engine**

- **Parallel segmented downloads** over HTTP `Range` requests (default 8 connections)
- **Dynamic work stealing**: idle connections split the largest remaining segment, so there's no slow tail at the end
- **Crash-safe resume**: progress is checkpointed every second and survives `kill -9` and power loss
- **Change detection**: if the file changed on the server (ETag / Last-Modified / size), it restarts instead of producing a corrupted file
- **Automatic retries** with exponential backoff when connections drop
- **Fallback** to a single connection when the server doesn't support ranges
- **No merge step**: every connection writes straight to its final offset in one file
- Custom headers (`Cookie`, `Referer`, …) for downloads that need you to be logged in
- Zero dependencies, so it runs anywhere Python 3.10+ runs (Windows, macOS, Linux)

## Download (no Python needed)

From the [Releases page](https://github.com/mohamadar8162-design/fastgrab/releases/latest):

- **`FastGrab.exe`** is the desktop app. Double-click it to open the window.
- **`fastgrab-cli.exe`** is the command-line tool: `.\fastgrab-cli.exe https://example.com/big.iso -c 16`

Both are single files with nothing to install. They're built and tested automatically by GitHub Actions from the source in this repo.

To run the app from source instead: `python fastgrab_app.py`.

## Install from source (Windows)

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1
```

This copies fastgrab to `%LOCALAPPDATA%\Programs\fastgrab` and adds that folder to your **user** PATH. Your other PATH entries aren't touched, and a backup is saved first. No admin rights are needed. Then open a new terminal and run `fastgrab URL` from anywhere. To remove it, run `uninstall.ps1`, which removes only what the installer added.

On macOS/Linux, just run `python3 fastgrab.py URL`.

## Usage

```bash
python fastgrab.py URL                          # 8 connections, save to current folder
python fastgrab.py URL -c 16 -o ~/Downloads     # 16 connections, custom folder
python fastgrab.py URL -H "Cookie: session=abc" -H "Referer: https://site.com"
```

Press **Ctrl+C** to pause. Run the same command again to resume.

## How it works

### Desktop app architecture

The app uses the same architecture as [Prism](https://github.com/FortoGraphy/prism-download-manager), with zero dependencies instead of Tauri + React:

```mermaid
flowchart LR
    UI["ui/index.html<br/>(app window: Edge/Chrome --app)"] -- "JSON over 127.0.0.1<br/>+ per-run token" --> API["fastgrab_app.py<br/>local API + download manager"]
    API --> ENG["fastgrab.py engine<br/>(one Download per item)"]
    API --> DB[("downloads.json<br/>list + settings")]
```

- The window is a single HTML page shown by Edge (built into Windows) in app mode, so it has no tabs or address bar.
- The page polls `/api/state` twice per second. The engine's `snapshot()` returns status, speed, ETA and every segment's `[start, pos, end, connection]`, which the page draws on a `<canvas>`.
- **Security:** the API listens only on `127.0.0.1`, requires a random 192-bit token generated at each start, and checks the `Host` header. That stops websites you visit from controlling your downloads through cross-site requests or DNS rebinding.
- When the window closes, the app pauses all downloads (saving their resume state) and exits.


### 1. Probe without a wasted request

The first request is a normal `GET` with `Range: bytes=0-`. If the server answers `206 Partial Content` with a `Content-Range` header, ranges are supported and we learn the total size. That same response then keeps streaming as connection #1, so no separate `HEAD` request is needed. A `200` means no range support, so fastgrab falls back to one connection.

### 2. Work stealing

Each segment is a byte range `[pos, end)`. When a connection is idle, it takes the segment with the most bytes left and splits it at the midpoint (4 KiB aligned, never below 512 KiB):

```mermaid
flowchart LR
    A["conn 1: [0 ─────────────── 30 MB)"] -->|conn 2 steals| B["conn 1: [0 ── 15 MB)   conn 2: [15 ── 30 MB)"]
    B -->|conn 3 steals the bigger half| C["conn 1: [0 ─ 15)   conn 2: [15 ─ 22.5)   conn 3: [22.5 ─ 30)"]
```

The victim's `end` simply shrinks. It notices on its next read, writes up to the new end, and closes its connection. The same thing happens whenever a connection finishes early, so fast connections keep helping slow ones until the very end.

To make stealing safe across threads, each segment tracks two cursors:
- `claim` is how far the owner has *reserved* (used for splitting, so a split can never land on bytes that are about to be written)
- `pos` is how far is actually *written* (used for saving state, so saved progress is never ahead of the disk)

### 3. Positioned writes

The output file `name.fgpart` is pre-allocated to its full size, and every connection writes its bytes directly at their offset (`seek` + `write`). When everything is done, the file is fsynced and atomically renamed. There are no temporary part files and no slow merge step.

### 4. Crash-safe resume

Every second, fastgrab saves the remaining segments to `name.fgpart.json`, in this order:

1. **snapshot** the segment table
2. **fsync** the data file
3. **atomically replace** the JSON (write to `.tmp`, fsync, `os.replace`)

Because the snapshot comes before the fsync, the saved state can never claim bytes that aren't really on disk. On restart, fastgrab probes the URL again, compares size + ETag + Last-Modified with the saved state, and only downloads what's missing.

## Tests

```bash
python -m unittest discover -s tests -v
```

The suite starts a local HTTP server (`tests/server.py`) that can throttle each connection, ignore `Range`, or randomly cut connections. Each test then checks the SHA-256 of the downloaded file:

| Test | What it proves |
|---|---|
| `test_multi_connection_matches_and_is_faster` | 8 connections give an identical file and finish over 3× faster than 1 |
| `test_resume_after_hard_crash` | the process is `SIGKILL`ed mid-download and resumes to an identical file |
| `test_server_without_range_support` | single-connection fallback works |
| `test_survives_dropped_connections` | a server that randomly drops connections still yields a correct file |
| `test_splits_never_overlap_or_leave_gaps` | 2000 random claim/steal steps always tile the file exactly |
| `test_app` (5 tests) | app API: live segments, pause/resume, unique names for same-name downloads, cancel deletes only its own partial files, rejects non-http links, 403 without token or with a foreign `Host` |

The release pipeline runs `test_app` against the built **`FastGrab.exe`** itself before publishing it.

Tests run on Linux, Windows and macOS through GitHub Actions.

## Roadmap

- [ ] Browser extension (Chrome MV3 + native messaging) to catch downloads automatically
- [ ] Download queue and speed limiter (token bucket)
- [ ] Optional checksum verification (`--sha256`)
- [x] Desktop app
- [ ] System tray icon and notifications

## Inspiration

The engine design (probe-as-first-stream, midpoint work stealing, `snapshot → fsync → persist` checkpoints) is inspired by the architecture of [Prism Download Manager](https://github.com/FortoGraphy/prism-download-manager), a full-featured download manager written in Rust. fastgrab is an independent, from-scratch Python implementation of those ideas.

## License

[MIT](LICENSE) © 2026 Mohamad Mahajna
