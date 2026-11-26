import { config, status, call, isVideoPage, t } from "./common.js";

const $ = s => document.querySelector(s);
const lang = (chrome.i18n.getUILanguage() || "en").toLowerCase();
document.documentElement.lang = lang;
document.documentElement.dir = /^(ar|he|iw|fa|ur)\b/.test(lang) ? "rtl" : "ltr";
document.querySelectorAll("[data-i18n]").forEach(el => el.textContent = t(el.dataset.i18n));

async function refresh() {
  const c = await config();
  $("#key").value = c.key;
  $("#port").value = c.port;
  $("#capture").checked = c.capture;
  const s = await status();
  const pill = $("#status");
  pill.className = "pill " + s;
  pill.textContent = t("status_" + s);
  $("#msg").textContent = s === "offline" ? t("notRunning") : s === "unpaired" && c.key ? t("wrongKey") : "";
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (tab && /^https?:/i.test(tab.url || "") && s === "connected") {
    $("#tabBox").hidden = false;
    $("#grabTab").textContent = isVideoPage(tab.url) ? t("grabVideo") : t("grabPage");
    $("#grabTab").onclick = async () => {
      try {
        await call("/ext/add", { url: tab.url, kind: "video", referrer: tab.url }, 8000);
        $("#msg").textContent = t("notifyPicker");
      } catch (e) { $("#msg").textContent = e.message; }
    };
  }
}

$("#save").onclick = async () => {
  await chrome.storage.local.set({ key: $("#key").value.trim() });
  await refresh();
};
$("#savePort").onclick = async () => {
  const p = parseInt($("#port").value, 10);
  if (p > 0 && p < 65536) await chrome.storage.local.set({ port: p });
  await refresh();
};
$("#capture").onchange = e => chrome.storage.local.set({ capture: e.target.checked });
$("#key").addEventListener("keydown", e => { if (e.key === "Enter") $("#save").click(); });
refresh();
