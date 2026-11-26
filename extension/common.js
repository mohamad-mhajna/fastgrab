// Shared by the background worker and the popup.
export const DEFAULTS = { key: "", port: 47821, capture: true };

// File types the extension hands to FastGrab automatically.
export const CAPTURE_EXT = new Set(("zip rar 7z tar gz bz2 xz tgz zst iso img exe msi msix dmg pkg deb rpm apk " +
  "appimage mp4 mkv avi mov webm m4v wmv flv mp3 flac wav m4a aac ogg opus pdf epub bin dat").split(" "));
const CAPTURE_MIME = /^(video|audio)\/|^application\/(zip|x-zip|x-7z|x-rar|vnd\.rar|x-tar|gzip|x-gzip|x-xz|x-bzip2|octet-stream|x-msdownload|x-msi|x-ms-installer|x-apple-diskimage|vnd\.android\.package-archive|x-iso9660-image|pdf|epub\+zip|x-debian-package|x-rpm)/i;

export const VIDEO_HOSTS = ["youtube.com", "youtu.be", "tiktok.com", "instagram.com", "facebook.com", "fb.watch",
  "x.com", "twitter.com", "reddit.com", "redd.it", "vimeo.com", "twitch.tv", "soundcloud.com", "dailymotion.com",
  "dai.ly", "pinterest.com", "pin.it", "threads.net", "threads.com", "linkedin.com", "bilibili.com",
  "streamable.com", "kick.com", "rumble.com", "snapchat.com", "bandcamp.com", "ted.com"];

export function isVideoPage(url) {
  try {
    const h = new URL(url).hostname.toLowerCase();
    return VIDEO_HOSTS.some(v => h === v || h.endsWith("." + v));
  } catch { return false; }
}

export function extOf(s) {
  try { s = new URL(s).pathname; } catch {}
  const m = /\.([a-z0-9]{1,9})$/i.exec(String(s || "").split(/[?#]/)[0]);
  return m ? m[1].toLowerCase() : "";
}

// Small everyday files the browser handles fine on its own.
const SKIP_EXT = new Set(("txt html htm xml json csv md log ics vcf jpg jpeg png gif webp svg ico bmp " +
  "css js torrent").split(" "));
const DYNAMIC_EXT = new Set("php asp aspx jsp cgi do".split(" "));   // tell us nothing about the file

export function shouldCapture(item) {
  const url = item.finalUrl || item.url || "";
  if (!/^https?:\/\//i.test(url)) return false;            // blob:, data:, file: stay in the browser
  const ext = [extOf(item.filename), extOf(url)].find(e => e && !DYNAMIC_EXT.has(e)) || "";
  if (CAPTURE_EXT.has(ext)) return true;
  if (ext && SKIP_EXT.has(ext)) return false;              // the file type wins over a vague server label
  return CAPTURE_MIME.test(item.mime || "");
}

export async function config() {
  return { ...DEFAULTS, ...(await chrome.storage.local.get(DEFAULTS)) };
}

export async function call(path, body, timeoutMs = 3000) {
  const c = await config();
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const r = await fetch(`http://127.0.0.1:${c.port}${path}`, {
      method: body === undefined ? "GET" : "POST",
      headers: { "X-FastGrab-Key": c.key, "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: ctl.signal,
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw Object.assign(new Error(j.error || r.statusText), { status: r.status });
    return j;
  } finally { clearTimeout(timer); }
}

// "connected" | "unpaired" | "offline"
export async function status() {
  const c = await config();
  if (!c.key) return "unpaired";
  try { await call("/ext/ping", undefined, 1500); return "connected"; }
  catch (e) { return e.status === 403 ? "unpaired" : "offline"; }
}

export function t(key, ...subs) { return chrome.i18n.getMessage(key, subs) || key; }
