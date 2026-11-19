import { DEFAULT_GATEWAY, api, avatarColor, friendlyError, getGateway } from "./lib/common.js";
import { icon } from "./lib/icons.js";

const $ = (id) => document.getElementById(id);
const input = $("gateway");
const send = (msg) => chrome.runtime.sendMessage(msg);

function setStatus(node, text, kind = "") {
  node.textContent = text;
  node.className = `status ${kind}`;
}

// Notion page icons are an emoji or an image URL; we only show emoji and fall back to a page glyph.
function pageIcon(node, value) {
  node.replaceChildren(value && !value.startsWith("http") ? document.createTextNode(value) : icon("page"));
}

function renderNotion(notion) {
  const name = notion && (notion.workspace_name || "Notion");
  $("avatar").textContent = name ? name.trim().charAt(0).toUpperCase() : "";
  $("avatar").style.setProperty("--avatar", name ? avatarColor(name) : "var(--tonal)");
  if (!name) $("avatar").replaceChildren(icon("account"));
  $("notion-name").textContent = name || "Not connected";
  $("notion-sub").textContent = name ? "Connected" : "Click Take Notes on any video to connect.";
  $("disconnect").hidden = !notion;
  $("dest-row").hidden = !notion;

  const d = notion && notion.destination;
  pageIcon($("dest-icon"), d ? d.icon : "🎬");
  $("dest-title").textContent = d ? d.title : "Video Notes";
  $("dest-tag").hidden = !!d;
  $("dest-reset").hidden = !d;
}

async function load() {
  input.value = await getGateway();
  const res = await send({ type: "GET_STATE" });
  renderNotion(res && res.notion);
}

function showError(res) {
  const fe = friendlyError(res && res.error);
  setStatus($("dest-status"), `${fe.title}. ${fe.detail}`, "err");
}

async function setDestination(page) {
  const res = await send({ type: "SET_NOTION_DESTINATION", page });
  if (!res || !res.ok) return showError(res);
  renderNotion(res.notion);
  $("dest-results").replaceChildren();
  setStatus($("dest-status"), "Saved. New notes will be published here.", "ok");
}

async function search(e) {
  e.preventDefault();
  setStatus($("dest-status"), "Searching…");
  const res = await send({ type: "SEARCH_NOTION_PAGES", query: $("dest-q").value.trim() });
  if (!res || !res.ok) return showError(res);
  setStatus($("dest-status"), res.pages.length ? "" : "No pages found. Share a page with the connection in Notion first.");
  $("dest-results").replaceChildren(
    ...res.pages.map((p) => {
      const li = document.createElement("li");
      const b = document.createElement("button");
      b.type = "button";
      const ico = document.createElement("span");
      ico.className = "page-ico";
      pageIcon(ico, p.icon);
      const title = document.createElement("span");
      title.textContent = p.title;
      b.append(ico, title);
      b.addEventListener("click", () => setDestination(p));
      li.append(b);
      return li;
    })
  );
}

$("disconnect").addEventListener("click", async () => {
  await send({ type: "DISCONNECT_NOTION" });
  renderNotion(null);
});
$("dest-form").addEventListener("submit", search);
$("dest-reset").addEventListener("click", () => setDestination(null));

$("save").addEventListener("click", async () => {
  const value = (input.value || DEFAULT_GATEWAY).trim().replace(/\/+$/, "");
  try {
    new URL(value);
  } catch (_) {
    setStatus($("status"), "Enter a valid URL, like http://localhost:8000.", "err");
    return;
  }
  await chrome.storage.sync.set({ gatewayUrl: value });
  setStatus($("status"), "Saved.", "ok");
});

$("test").addEventListener("click", async () => {
  setStatus($("status"), "Testing…");
  try {
    const h = await api("/healthz", { timeoutMs: 5000 });
    if (h.status === "ok") setStatus($("status"), "Server healthy.", "ok");
    else setStatus($("status"), "Server is up but degraded. Check make health.", "err");
  } catch (e) {
    setStatus($("status"), e.message, "err");
  }
});

load();
