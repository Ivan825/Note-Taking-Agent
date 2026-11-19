import { STAGES, TERMINAL, api, fmtTime, friendlyError, getGateway, needsReconnect, stageIndex } from "./lib/common.js";

const $ = (sel) => document.querySelector(sel);
const send = (msg) => chrome.runtime.sendMessage(msg);

let state = { jobs: [], notion: null };
let currentVideo = null;
let pollTimer = null;

// ------------------------------------------------------------------ rendering
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function jobClass(job) {
  if (job.status === "COMPLETED") return "ok";
  if (job.status === "COMPLETED_WITH_WARNINGS") return "warn";
  if (job.status === "FAILED" || job.status === "DEAD_LETTERED") return "err";
  if (job.next_retry_at) return "waiting active";
  return "active";
}

function statusText(job) {
  switch (job.status) {
    case "COMPLETED": return "Done — notes published";
    case "COMPLETED_WITH_WARNINGS": return "Done with warnings";
    case "FAILED": return "Failed";
    case "DEAD_LETTERED": return "Stopped after repeated failures";
    default: {
      const stage = STAGES[stageIndex(job.status)];
      return job.next_retry_at ? `${stage.label} — waiting to retry` : stage.label;
    }
  }
}

function retryCountdown(job) {
  if (!job.next_retry_at) return null;
  const secs = Math.max(0, Math.round((Date.parse(job.next_retry_at) - Date.now()) / 1000));
  return secs;
}

function detailNode(job) {
  const p = el("p", "detail");
  if (job.status === "FAILED" || job.status === "DEAD_LETTERED") {
    const fe = friendlyError(job.error);
    p.appendChild(el("b", null, fe.title + ". "));
    p.appendChild(document.createTextNode(fe.detail));
    if (job.status === "DEAD_LETTERED") {
      p.appendChild(document.createTextNode(` (gave up after ${job.error?.attempts || job.max_attempts || "several"} attempts; sent to the dead-letter queue for inspection)`));
    }
    return p;
  }
  if (job.status === "COMPLETED") {
    const r = job.result || {};
    p.textContent = r.frames_requested
      ? `Summary and ${r.frames_published} captioned frame${r.frames_published === 1 ? "" : "s"} published to Notion.`
      : "Summary published to Notion.";
    return p;
  }
  if (job.status === "COMPLETED_WITH_WARNINGS") {
    const r = job.result || {};
    p.textContent = `Published with ${r.frames_published ?? 0} of ${r.frames_requested ?? 0} frames.`;
    return p;
  }
  const secs = retryCountdown(job);
  if (secs !== null) {
    const reason = (job.status_detail || "Temporary problem").split(" - retrying")[0];
    p.textContent = `${reason}. Retrying ${secs > 0 ? `in ${secs}s` : "now"} (attempt ${job.attempt} of ${job.max_attempts}).`;
  } else {
    let text = job.status_detail || "Working…";
    if (job.recoveries) text += ` · recovered after a worker failure`;
    p.textContent = text;
  }
  if (job.offline) p.textContent += " (server unreachable — showing last known state)";
  return p;
}

function stepsNode(job) {
  const ol = el("ol", "steps");
  const idx = stageIndex(job.status);
  const failed = job.status === "FAILED" || job.status === "DEAD_LETTERED";
  // For failed jobs, highlight the stage that was running when it failed.
  const failedAt = failed ? Math.max(1, STAGES.findIndex((s) => s.status === (job.error && job.error.stage))) : -1;
  STAGES.forEach((s, i) => {
    const li = el("li", "step");
    const mark = el("span", "mark");
    let label = s.label;
    if (failed) {
      if (i < failedAt) { li.classList.add("done"); mark.textContent = "✓"; }
      else if (i === failedAt) { li.classList.add("failed", "current"); mark.textContent = "!"; }
    } else if (TERMINAL.has(job.status)) {
      li.classList.add("done");
      mark.textContent = "✓";
      if (i === STAGES.length - 1) {
        li.classList.add("final");
        if (job.status === "COMPLETED_WITH_WARNINGS") mark.textContent = "!";
      }
    } else if (i < idx) {
      li.classList.add("done");
      mark.textContent = "✓";
    } else if (i === idx) {
      li.classList.add("current");
    }
    li.appendChild(mark);
    li.appendChild(el("span", null, label));
    ol.appendChild(li);
  });
  return ol;
}

function warningsNode(job) {
  const ul = el("ul", "warnings");
  for (const w of job.warnings || []) {
    let text = w.message;
    if (w.code === "FRAME_EXTRACTION_FAILED") {
      text = `Frame at ${fmtTime(w.timestamp_sec)} couldn't be captured, so it was left out.`;
    }
    ul.appendChild(el("li", null, "⚠ " + (text || w.code)));
  }
  return ul;
}

function actionsNode(job) {
  const div = el("div", "actions");
  const url = job.result && job.result.notion_page_url;
  if (url && (job.status === "COMPLETED" || job.status === "COMPLETED_WITH_WARNINGS")) {
    const a = el("button", "btn primary", "View in Notion");
    a.type = "button";
    a.addEventListener("click", () => chrome.tabs.create({ url }));
    div.appendChild(a);
  }
  if (needsReconnect(job.error)) {
    const b = el("button", "btn secondary", "Reconnect Notion");
    b.type = "button";
    b.addEventListener("click", connectNotion);
    div.appendChild(b);
  }
  return div;
}

function renderJob(job) {
  const tpl = $("#job-tpl").content.firstElementChild.cloneNode(true);
  tpl.className = `job ${jobClass(job)}`;
  tpl.dataset.jobId = job.job_id;
  tpl.querySelector(".thumb").src = `https://i.ytimg.com/vi/${encodeURIComponent(job.video_id)}/mqdefault.jpg`;
  tpl.querySelector(".job-title").textContent = job.video_title || job.video_id;
  tpl.querySelector(".job-title").title = job.video_title || "";
  tpl.querySelector(".status-text").textContent = statusText(job);
  tpl.querySelector(".steps").replaceWith(stepsNode(job));
  tpl.querySelector(".bar-fill").style.width = `${Math.max(3, job.progress || 0)}%`;
  tpl.querySelector(".detail").replaceWith(detailNode(job));
  tpl.querySelector(".warnings").replaceWith(warningsNode(job));
  tpl.querySelector(".actions").replaceWith(actionsNode(job));
  return tpl;
}

function render() {
  const list = $("#jobs");
  list.replaceChildren(...state.jobs.map(renderJob));
  $("#empty").hidden = state.jobs.length > 0;
  $("#clear").hidden = !state.jobs.some((j) => TERMINAL.has(j.status));

  const chip = $("#notion-chip");
  if (state.notion) {
    chip.textContent = state.notion.workspace_name || "Notion";
    chip.className = "chip connected";
    chip.title = "Connected to Notion — click to reconnect";
  } else {
    chip.textContent = "Connect Notion";
    chip.className = "chip disconnected";
    chip.title = "Connect your Notion workspace";
  }
}

function banner(kind, text) {
  const b = $("#banner");
  if (!text) { b.hidden = true; return; }
  b.className = `banner ${kind}`;
  b.textContent = text;
  b.hidden = false;
}

// ------------------------------------------------------------------ data
async function refresh(poll = true) {
  const res = await send({ type: "GET_STATE", refresh: poll });
  if (res && res.ok) {
    state = { jobs: res.jobs, notion: res.notion };
    render();
  }
  const active = state.jobs.some((j) => !TERMINAL.has(j.status));
  clearTimeout(pollTimer);
  pollTimer = setTimeout(() => refresh(true), active ? 1500 : 5000);
}

async function connectNotion() {
  banner("info", "Opening Notion… choose the pages Video Note Taker may write to.");
  const res = await send({ type: "CONNECT_NOTION" });
  if (res && res.ok) {
    banner(null);
    state.notion = res.notion;
    render();
  } else {
    const fe = friendlyError(res && res.error);
    banner("error", `${fe.title}. ${fe.detail}`);
  }
}

async function takeNotesOnCurrent() {
  if (!currentVideo) return;
  const btn = $("#take-notes");
  btn.disabled = true;
  btn.textContent = "Sending…";
  try {
    if (currentVideo.duration_sec && currentVideo.duration_sec > 20 * 60) {
      banner("error", "Video is too long. Only videos up to 20 minutes are supported.");
      return;
    }
    const res = await send({ type: "SUBMIT_JOB", video: currentVideo });
    if (res && res.ok) {
      banner(null);
      await refresh(false);
    } else {
      const fe = friendlyError(res && res.error);
      banner("error", `${fe.title}. ${fe.detail}`);
    }
  } finally {
    btn.disabled = false;
    btn.textContent = "Take notes";
  }
}

async function detectCurrentTab() {
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab || !tab.url) return;
    const u = new URL(tab.url);
    const id = u.hostname.endsWith("youtube.com") && u.pathname === "/watch" ? u.searchParams.get("v") : null;
    if (!id) return;
    let duration;
    try {
      const [{ result }] = await chrome.scripting?.executeScript?.({
        target: { tabId: tab.id },
        func: () => document.querySelector("#movie_player video")?.duration,
      }) || [{}];
      duration = Number.isFinite(result) ? result : undefined;
    } catch (_) { /* scripting permission not granted - the gateway/worker still enforce the limit */ }
    currentVideo = {
      video_id: id,
      url: `https://www.youtube.com/watch?v=${id}`,
      title: (tab.title || "").replace(/^\(\d+\)\s*/, "").replace(/ - YouTube$/, ""),
      duration_sec: duration,
    };
    $("#current-title").textContent = currentVideo.title || id;
    $("#current").hidden = false;
  } catch (_) { /* ignore */ }
}

async function checkServer() {
  const base = await getGateway();
  const s = $("#server");
  s.textContent = base.replace(/^https?:\/\//, "");
  try {
    await api("/healthz", { timeoutMs: 4000 });
    s.className = "server up";
    s.title = "Server healthy";
  } catch (e) {
    s.className = "server down";
    s.title = e.message;
    if (e.code === "GATEWAY_UNREACHABLE") banner("error", e.message);
  }
}

// ------------------------------------------------------------------ wiring
$("#notion-chip").addEventListener("click", connectNotion);
$("#take-notes").addEventListener("click", takeNotesOnCurrent);
$("#clear").addEventListener("click", async () => {
  const res = await send({ type: "CLEAR_FINISHED" });
  if (res && res.ok) { state.jobs = res.jobs; render(); }
});

detectCurrentTab();
checkServer();
refresh(true);
