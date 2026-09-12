"use strict";

const $ = (id) => document.getElementById(id);
const els = {
  text: $("text"), sendText: $("send-text"), clearInput: $("clear-input"),
  drop: $("drop"), file: $("file"), browse: $("browse"),
  progress: $("progress"), bar: $("bar"),
  items: $("items"), empty: $("empty"), count: $("count"),
  refresh: $("refresh"), clearAll: $("clear-all"), toast: $("toast"),
};

// Server-injected mount prefix ("" at the root, "/clip" when mounted there).
const BASE = window.CLIP_BASE || "";
const POLL_MS = 4000;
let lastRender = "";
let toastTimer = null;

/* ---------------------------------------------------------------- utils */

function toast(message, bad) {
  els.toast.textContent = message;
  els.toast.classList.toggle("bad", !!bad);
  els.toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { els.toast.hidden = true; }, 2600);
}

function humanSize(bytes) {
  if (bytes < 1024) return bytes + " B";
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes / 1024, i = 0;
  while (value >= 1024 && i < units.length - 1) { value /= 1024; i++; }
  return (value >= 10 ? value.toFixed(0) : value.toFixed(1)) + " " + units[i];
}

function humanAge(epochSeconds) {
  const seconds = Math.max(0, Date.now() / 1000 - epochSeconds);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return Math.floor(seconds / 60) + "m ago";
  if (seconds < 86400) return Math.floor(seconds / 3600) + "h ago";
  return Math.floor(seconds / 86400) + "d ago";
}

async function api(path, options) {
  const response = await fetch(path, options);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error || ("HTTP " + response.status));
  return body;
}

/* Works on plain http:// too, where navigator.clipboard is unavailable. */
function copyText(value) {
  if (navigator.clipboard && window.isSecureContext) {
    return navigator.clipboard.writeText(value);
  }
  return new Promise((resolve, reject) => {
    const scratch = document.createElement("textarea");
    scratch.value = value;
    scratch.setAttribute("readonly", "");
    scratch.style.cssText = "position:fixed;top:0;left:0;opacity:0";
    document.body.appendChild(scratch);
    scratch.select();
    scratch.setSelectionRange(0, scratch.value.length);
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (_) { ok = false; }
    document.body.removeChild(scratch);
    ok ? resolve() : reject(new Error("Copy was blocked by the browser"));
  });
}

/* ---------------------------------------------------------------- render */

function render(items) {
  const signature = JSON.stringify(items.map((i) => [i.id, i.size]));
  if (signature === lastRender) return;
  lastRender = signature;

  els.items.textContent = "";
  els.empty.hidden = items.length > 0;
  els.count.textContent = items.length ? "(" + items.length + ")" : "";
  els.clearAll.disabled = items.length === 0;

  for (const item of items) {
    const li = document.createElement("li");
    li.className = "item";

    const top = document.createElement("div");
    top.className = "item-top";

    const name = document.createElement("div");
    name.className = "name";
    const tag = document.createElement("span");
    tag.className = "tag";
    tag.textContent = item.kind;
    name.appendChild(tag);
    name.appendChild(document.createTextNode(
      item.kind === "text" ? "Text snippet" : (item.name || "file")));

    const meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = humanSize(item.size) + " · " + humanAge(item.created);

    top.append(name, meta);
    li.appendChild(top);

    if (item.kind === "text" && item.preview) {
      const pre = document.createElement("pre");
      pre.className = "preview";
      pre.textContent = item.preview + (item.truncated ? "…" : "");
      li.appendChild(pre);
    }

    const row = document.createElement("div");
    row.className = "row";

    if (item.kind === "text") {
      const copy = document.createElement("button");
      copy.className = "primary";
      copy.textContent = "Copy";
      copy.addEventListener("click", () => copyItem(item, copy));
      row.appendChild(copy);
    }

    const download = document.createElement("a");
    download.href = BASE + "/api/items/" + item.id + "/download";
    download.setAttribute("download", "");
    download.innerHTML = "<button>Download</button>";
    row.appendChild(download);

    const remove = document.createElement("button");
    remove.className = "danger";
    remove.textContent = "Delete";
    remove.addEventListener("click", async () => {
      remove.disabled = true;
      try {
        await api(BASE + "/api/items/" + item.id, { method: "DELETE" });
        toast("Deleted");
        await refresh();
      } catch (err) {
        remove.disabled = false;
        toast(err.message, true);
      }
    });
    row.appendChild(remove);

    li.appendChild(row);
    els.items.appendChild(li);
  }
}

async function copyItem(item, button) {
  try {
    // Inlined text copies synchronously, which keeps Safari's clipboard
    // permission attached to the click; larger snippets need a fetch first.
    const value = typeof item.text === "string"
      ? item.text
      : await (await fetch(BASE + "/api/items/" + item.id + "/raw")).text();
    await copyText(value);
    const original = button.textContent;
    button.textContent = "Copied";
    setTimeout(() => { button.textContent = original; }, 1400);
  } catch (err) {
    toast(err.message || "Copy failed", true);
  }
}

async function refresh() {
  try {
    const data = await api(BASE + "/api/items");
    render(data.items || []);
  } catch (err) {
    toast(err.message, true);
  }
}

/* ---------------------------------------------------------------- actions */

async function sendText() {
  const value = els.text.value;
  if (!value.trim()) { toast("Nothing to send", true); return; }
  els.sendText.disabled = true;
  try {
    await api(BASE + "/api/text", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: value }),
    });
    els.text.value = "";
    toast("Text sent");
    await refresh();
  } catch (err) {
    toast(err.message, true);
  } finally {
    els.sendText.disabled = false;
  }
}

function uploadOne(file) {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("POST", BASE + "/api/files");
    request.setRequestHeader("Content-Type", file.type || "application/octet-stream");
    // Header values must be latin-1; percent-encode so unicode names survive.
    request.setRequestHeader("X-Filename", encodeURIComponent(file.name || "upload.bin"));
    request.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable) {
        els.bar.style.width = Math.round((event.loaded / event.total) * 100) + "%";
      }
    });
    request.addEventListener("load", () => {
      if (request.status >= 200 && request.status < 300) return resolve();
      let message = "Upload failed (HTTP " + request.status + ")";
      try { message = JSON.parse(request.responseText).error || message; } catch (_) {}
      reject(new Error(message));
    });
    request.addEventListener("error", () => reject(new Error("Network error during upload")));
    request.send(file);
  });
}

async function upload(files) {
  const list = Array.from(files);
  if (!list.length) return;
  els.progress.hidden = false;
  try {
    for (const file of list) {
      els.bar.style.width = "0%";
      await uploadOne(file);
    }
    toast(list.length === 1 ? "Uploaded" : list.length + " files uploaded");
    await refresh();
  } catch (err) {
    toast(err.message, true);
  } finally {
    els.progress.hidden = true;
    els.bar.style.width = "0%";
    els.file.value = "";
  }
}

/* ---------------------------------------------------------------- wiring */

els.sendText.addEventListener("click", sendText);
els.clearInput.addEventListener("click", () => { els.text.value = ""; els.text.focus(); });
els.text.addEventListener("keydown", (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key === "Enter") sendText();
});

els.browse.addEventListener("click", () => els.file.click());
els.file.addEventListener("change", () => upload(els.file.files));

["dragenter", "dragover"].forEach((name) =>
  els.drop.addEventListener(name, (event) => {
    event.preventDefault();
    els.drop.classList.add("over");
  }));
["dragleave", "drop"].forEach((name) =>
  els.drop.addEventListener(name, (event) => {
    event.preventDefault();
    els.drop.classList.remove("over");
  }));
els.drop.addEventListener("drop", (event) => {
  if (event.dataTransfer && event.dataTransfer.files.length) upload(event.dataTransfer.files);
});

// Pasting a screenshot anywhere outside the textarea uploads it as a file.
document.addEventListener("paste", (event) => {
  if (event.target === els.text || !event.clipboardData) return;
  const files = Array.from(event.clipboardData.files || []);
  if (files.length) { event.preventDefault(); upload(files); }
});

els.refresh.addEventListener("click", refresh);
els.clearAll.addEventListener("click", async () => {
  if (!confirm("Delete everything on the server?")) return;
  els.clearAll.disabled = true;
  try {
    const data = await api(BASE + "/api/items", { method: "DELETE" });
    toast("Cleared " + data.deleted + " item" + (data.deleted === 1 ? "" : "s"));
    await refresh();
  } catch (err) {
    toast(err.message, true);
  } finally {
    // render() owns this flag normally; keep it truthful if the call failed.
    els.clearAll.disabled = els.items.children.length === 0;
  }
});

setInterval(() => { if (!document.hidden) refresh(); }, POLL_MS);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
refresh();
