// Unit tests for the extension's decision rules:  node --test tests/
import { test } from "node:test";
import assert from "node:assert/strict";
import { shouldCapture, isVideoPage, extOf } from "../extension/common.js";

test("captures programs, archives, media and PDFs", () => {
  for (const u of ["https://x.org/setup.exe", "https://x.org/a.ZIP", "https://x.org/movie.mkv?sig=1",
                   "https://x.org/song.mp3", "https://x.org/book.pdf", "https://x.org/ubuntu.iso"])
    assert.equal(shouldCapture({ url: u }), true, u);
});
test("leaves small everyday files to the browser", () => {
  for (const u of ["https://x.org/notes.txt", "https://x.org/photo.jpg", "https://x.org/page.html"])
    assert.equal(shouldCapture({ url: u, mime: "application/octet-stream" }), false, u);
});
test("never touches blob:, data: or file: downloads", () => {
  for (const u of ["blob:https://x.org/123", "data:text/plain,hi", "file:///C:/a.zip"])
    assert.equal(shouldCapture({ url: u, mime: "application/zip" }), false, u);
});
test("uses the type label when the link has no file name", () => {
  assert.equal(shouldCapture({ url: "https://x.org/download?id=5", mime: "application/x-msdownload" }), true);
  assert.equal(shouldCapture({ url: "https://x.org/get.php?id=5", mime: "video/mp4" }), true);
  assert.equal(shouldCapture({ url: "https://x.org/get.php?id=5", mime: "text/html" }), false);
  assert.equal(shouldCapture({ url: "https://x.org/x", filename: "C:\\\\Users\\\\a\\\\Downloads\\\\tool.msi" }), true);
});
test("recognizes video sites", () => {
  assert.equal(isVideoPage("https://www.youtube.com/watch?v=1"), true);
  assert.equal(isVideoPage("https://vm.tiktok.com/abc"), true);
  assert.equal(isVideoPage("https://notyoutube.com/x"), false);
  assert.equal(isVideoPage("not a url"), false);
});
test("extOf", () => {
  assert.equal(extOf("https://a.b/c/d.tar.gz?x=1#y"), "gz");
  assert.equal(extOf("C:\\\\dl\\\\File.MP4"), "mp4");
  assert.equal(extOf("https://a.b/noext"), "");
});
