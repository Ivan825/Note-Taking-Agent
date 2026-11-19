import {
  STAGES, TERMINAL, api, avatarColor, describeJob, fmtTime, friendlyError, getGateway, needsReconnect,
  stageIndex, thumbUrl,
} from "./lib/common.js";
import { icon } from "./lib/icons.js";

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

function pill(label, cls, iconName, onClick) {
  const b = el("button", `pill ${cls}`);
  b.type = "button";
  if (iconName) b.appendChild(icon(iconName));
  b.appendChild(document.createTextNode(label));
  b.addEventListener("click", onClick);
  return b;
}

function detailNode(job) {
  const p = el("p", "detail");
  if (job.status === "FAILED" || job.status === "DEAD_LETTERED") {
    p.textContent = friendlyError(job.error).detail;
    if (job.status === "DEAD_LETTERED") {
      const n = job.error?.attempts || job.max_attempts || "several";
      p.textContent = `${friendlyError(job.error).title}. Gave up after ${n} attempts; the job is in the dead-letter queue.`;
    }
  } else if (job.status === "COMPLETED") {
    const r = job.result || {};
    p.textContent = r.frames_requested
      ? `Summary and ${r.frames_published} captioned frame${r.frames_published === 1 ? "" : "s"} in Notion.`
      : "Summary published to Notion.";
  } else if (job.status === "COMPLETED_WITH_WARNINGS") {
    const r = job.result || {};
    p.textContent = `${r.frames_published ?? 0} of ${r.frames_requested ?? 0} frames made it into Notion.`;
  } else if (job.next_retry_at) {
    const reason = (job.status_detail || "Temporary problem").split(" - retrying")[0];
    p.textContent = `${reason}. Attempt ${job.attempt} of ${job.max_attempts}.`;
  } else {
    p.textContent = (job.status_detail || "Working…") + (job.recoveries ? " · recovered after a worker failure" : "");
  }
  if (job.offline) p.textContent += " Server unreachable, showing the last known state.";
  return p;
}

function chaptersNode(job) {
  const ol = el("ol", "chapters");
  ol.setAttribute("aria-label", "Progress");
  const idx = stageIndex(job.status);
  const failed = job.status === "FAILED" || job.status === "DEAD_LETTERED";
  const failedAt = failed ? Math.max(1, STAGES.findIndex((s) => s.status === (job.error && job.error.stage))) : -1;
  STAGES.forEach((s, i) => {
    const li = el("li", "chapter");
    if (failed) {
      if (i < failedAt) li.classList.add("done");
      else if (i === failedAt) li.classList.add("failed");
    } else if (TERMINAL.has(job.status) || i < idx) {
      li.classList.add("done");
    } else if (i === idx) {
      li.classList.add("current");
      li.setAttribute("aria-current", "step");
    }
    li.append(el("span", "seg"), el("span", "label", s.label));
    ol.appendChild(li);
  });
  return ol;
}

function warningsNode(job) {
  const ul = el("ul", "warnings");
  for (const w of job.warnings || []) {
    const text = w.code === "FRAME_EXTRACTION_FAILED"
      ? `Frame at ${fmtTime(w.timestamp_sec)} couldn't be captured, so it was left out.`
      : w.message || w.code;
    const li = el("li");
    li.append(icon("warning"), el("span", null, text));
    ul.appendChild(li);
  }
  return ul;
}

function actionsNode(job) {
  const div = el("div", "actions");
  const url = job.result && job.result.notion_page_url;
  if (url && (job.status === "COMPLETED" || job.status === "COMPLETED_WITH_WARNINGS")) {
    div.appendChild(pill("View in Notion", "primary small", "open", () => chrome.tabs.create({ url })));
  }
  if (job.status === "FAILED" || job.status === "DEAD_LETTERED") {
    div.appendChild(actionButton("Retry", "retry", "RETRY_JOB", job));
  }
  if (job.status === "QUEUED") {
    div.appendChild(actionButton("Cancel", "close", "CANCEL_JOB", job));
  }
  if (needsReconnect(job.error)) {
    div.appendChild(pill("Reconnect Notion", "tonal small", "account", connectNotion));
  }
  return div;
}

function actionButton(label, iconName, type, job) {
  const b = pill(label, "tonal small", iconName, async () => {
    b.disabled = true;
    const res = await send({ type, job_id: job.job_id });
    if (res && res.ok) {
      banner(null);
      await refresh(false);
    } else {
      b.disabled = false;
      const fe = friendlyError(res && res.error);
      banner("error", `${fe.title}. ${fe.detail}`);
    }
  });
  return b;
}

function renderJob(job) {
  const d = describeJob(job);
  const li = $("#job-tpl").content.firstElementChild.cloneNode(true);
  li.className = `job ${d.cls}`;
  li.dataset.jobId = job.job_id;

  li.querySelector(".thumb").src = thumbUrl(job.video_id);
  li.querySelector(".badge").textContent = job.duration_sec ? fmtTime(job.duration_sec) : "";
  // YouTube's red "watched" bar doubles as overall progress; failed jobs don't get one.
  if (!d.cls.startsWith("err")) {
    li.querySelector(".thumb-wrap").classList.add("has-bar");
    li.querySelector(".resume").hidden = false;
    li.querySelector(".resume-fill").style.transform = `scaleX(${Math.max(0.02, (job.progress || 0) / 100)})`;
  }

  const title = li.querySelector(".job-title");
  title.textContent = job.video_title || job.video_id;
  title.title = job.video_title || "";
  const status = li.querySelector(".job-status");
  if (d.icon) status.appendChild(icon(d.icon));
  status.appendChild(el("span", null, d.text));

  li.querySelector(".detail").replaceWith(detailNode(job));
  li.querySelector(".chapters").replaceWith(chaptersNode(job));
  li.querySelector(".warnings").replaceWith(warningsNode(job));
  li.querySelector(".actions").replaceWith(actionsNode(job));
  return li;
}

function renderAccount() {
  const old = $("#notion-chip");
  let btn;
  if (state.notion) {
    const name = state.notion.workspace_name || "Notion";
    btn = el("button", "avatar", name.trim().charAt(0).toUpperCase());
    btn.style.setProperty("--avatar", avatarColor(name));
    btn.title = `Notion: ${name}. Click to reconnect.`;
    btn.setAttribute("aria-label", `Connected to ${name} in Notion. Reconnect`);
  } else {
    btn = pill("Connect Notion", "outline", "account", () => {});
  }
  btn.id = "notion-chip";
  btn.type = "button";
  btn.addEventListener("click", connectNotion);
  old.replaceWith(btn);
}

function render() {
  $("#jobs").replaceChildren(...state.jobs.map(renderJob));
  $("#empty").hidden = state.jobs.length > 0;
  $("#clear").hidden = !state.jobs.some((j) => TERMINAL.has(j.status));
  renderAccount();
}

function banner(kind, text) {
  const b = $("#banner");
  if (!text) { b.hidden = true; return; }
  b.className = `banner ${kind}`;
  b.replaceChildren(icon(kind === "error" ? "error" : "schedule"), el("span", null, text));
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
  banner("info", "Opening Notion. Choose the pages Video Note Taker may write to.");
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
    $("#current-thumb").src = thumbUrl(id);
    $("#current-badge").textContent = duration ? fmtTime(duration) : "";
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
$("#empty-art").appendChild(icon("note"));
$("#take-notes").addEventListener("click", takeNotesOnCurrent);
$("#clear").addEventListener("click", async () => {
  const res = await send({ type: "CLEAR_FINISHED" });
  if (res && res.ok) { state.jobs = res.jobs; render(); }
});

detectCurrentTab();
checkServer();
refresh(true);
