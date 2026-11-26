// FastGrab browser extension: hands downloads and video pages to the FastGrab app.
import { call, config, shouldCapture, isVideoPage, t } from "./common.js";

async function cookieHeader(url) {
  try {
    const list = await chrome.cookies.getAll({ url });
    return list.map(c => `${c.name}=${c.value}`).join("; ");
  } catch { return ""; }
}

function notify(title, message) {
  chrome.notifications.create({ type: "basic", iconUrl: "icons/icon128.png", title, message: message || "" });
}

const baseName = p => String(p || "").split(/[\\/]/).pop();

// Send a link to FastGrab. Returns true if FastGrab took it.
export async function sendToFastGrab({ url, referrer = "", filename = "", kind }) {
  const body = { url, referrer, filename, userAgent: navigator.userAgent, kind };
  if (kind !== "video") body.cookie = await cookieHeader(url);
  const r = await call("/ext/add", body, 8000);
  notify(r.picker ? t("notifyPicker") : t("notifySent"), r.picker ? url : (filename || baseName(new URL(url).pathname)));
  return true;
}

// --- catch downloads automatically ---------------------------------------------
const handled = new Set();
async function onDownload(item) {
  const c = await config();
  if (!c.capture || !c.key || handled.has(item.id) || !shouldCapture(item)) return;
  handled.add(item.id);
  try { await chrome.downloads.pause(item.id); } catch {}
  try {
    await sendToFastGrab({ url: item.finalUrl || item.url, referrer: item.referrer || "",
                           filename: baseName(item.filename), kind: "file" });
    try { await chrome.downloads.cancel(item.id); } catch {}
    try { await chrome.downloads.erase({ id: item.id }); } catch {}
  } catch {
    // FastGrab isn't running (or refused): let the browser download it as usual
    try { await chrome.downloads.resume(item.id); } catch {}
  }
}
chrome.downloads.onCreated.addListener(onDownload);

// --- right-click menu ----------------------------------------------------------
chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({ id: "fg-link", title: t("menuLink"), contexts: ["link"] });
    chrome.contextMenus.create({ id: "fg-video", title: t("menuVideo"), contexts: ["page", "video", "audio"] });
  });
});

async function onMenu(info, tab) {
  try {
    if (info.menuItemId === "fg-link") {
      await sendToFastGrab({ url: info.linkUrl, referrer: info.pageUrl || "",
                             kind: isVideoPage(info.linkUrl) ? "video" : undefined });
    } else if (info.menuItemId === "fg-video") {
      const src = info.srcUrl && /^https?:/i.test(info.srcUrl) && !isVideoPage(info.pageUrl) ? info.srcUrl : info.pageUrl;
      await sendToFastGrab({ url: src, referrer: info.pageUrl || "", kind: "video" });
    }
  } catch (e) {
    notify(t("notifyFailed"), e.status === 403 ? t("needPairing") : t("notRunning"));
  }
}
chrome.contextMenus.onClicked.addListener(onMenu);

// for the automated tests
self.__fastgrab = { onDownload, onMenu, sendToFastGrab };
