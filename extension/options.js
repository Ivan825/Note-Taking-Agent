import { DEFAULT_GATEWAY, api, getGateway } from "./lib/common.js";

const input = document.getElementById("gateway");
const status = document.getElementById("status");

async function load() {
  input.value = await getGateway();
  const res = await chrome.runtime.sendMessage({ type: "GET_STATE" });
  document.getElementById("notion").textContent =
    res && res.notion ? `Connected to “${res.notion.workspace_name}”.` : "Not connected — click Take Notes on any video to connect.";
}

document.getElementById("save").addEventListener("click", async () => {
  const value = (input.value || DEFAULT_GATEWAY).trim().replace(/\/+$/, "");
  try {
    new URL(value);
  } catch (_) {
    status.textContent = "Enter a valid URL.";
    return;
  }
  await chrome.storage.sync.set({ gatewayUrl: value });
  status.textContent = "Saved.";
});

document.getElementById("test").addEventListener("click", async () => {
  status.textContent = "Testing…";
  try {
    const h = await api("/healthz", { timeoutMs: 5000 });
    status.textContent = h.status === "ok" ? "✓ Server healthy" : "Server degraded";
  } catch (e) {
    status.textContent = e.message;
  }
});

load();
