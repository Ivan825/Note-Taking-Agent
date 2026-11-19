// Background service worker: one-time Notion OAuth, job submission, status polling.
import { api, ApiError, TERMINAL, friendlyError } from "./lib/common.js";

const MAX_JOBS = 100;
const POLL_ALARM = "vnt-poll";
const FLASH_MS = 10000;
let badgeFlash = null; // {text, color, until}: shown briefly after a job finishes

// ------------------------------------------------------------------ storage helpers
async function getNotion() {
  const { notion } = await chrome.storage.local.get("notion");
  return notion || null;
}

async function getJobs() {
  const { jobs } = await chrome.storage.local.get("jobs");
  return Array.isArray(jobs) ? jobs : [];
}

async function saveJobs(jobs) {
  await chrome.storage.local.set({ jobs: jobs.slice(0, MAX_JOBS) });
  await updateBadge(jobs);
}

async function updateBadge(jobs) {
  if (badgeFlash && Date.now() < badgeFlash.until) {
    await chrome.action.setBadgeBackgroundColor({ color: badgeFlash.color });
    await chrome.action.setBadgeText({ text: badgeFlash.text });
    return;
  }
  const active = jobs.filter((j) => !TERMINAL.has(j.status)).length;
  await chrome.action.setBadgeBackgroundColor({ color: "#2563eb" });
  await chrome.action.setBadgeText({ text: active ? String(active) : "" });
}

// Green ✓ (or red ! if any failed) for ~10s, then back to the active count.
// ponytail: setTimeout dies with the service worker; the next poll/save restores the badge anyway.
function flashBadge(finished) {
  const failed = finished.some((j) => j.status === "FAILED" || j.status === "DEAD_LETTERED");
  badgeFlash = failed ? { text: "!", color: "#dc2626" } : { text: "✓", color: "#16a34a" };
  badgeFlash.until = Date.now() + FLASH_MS;
  setTimeout(async () => updateBadge(await getJobs()), FLASH_MS + 50);
}

function notifyFinished(job) {
  const outcome = {
    COMPLETED: "Notes published to Notion.",
    COMPLETED_WITH_WARNINGS: "Notes published, but some frames were left out.",
    FAILED: `Failed: ${friendlyError(job.error).title}.`,
    DEAD_LETTERED: "Stopped after repeated failures.",
  }[job.status];
  chrome.notifications.create(`job:${job.job_id}`, {
    type: "basic",
    iconUrl: "icons/icon128.png",
    title: job.video_title || job.video_id || "Video Note Taker",
    message: outcome || job.status,
  });
}

chrome.notifications.onClicked.addListener(async (id) => {
  if (!id.startsWith("job:")) return;
  const job = (await getJobs()).find((j) => j.job_id === id.slice(4));
  const url = job && job.result && job.result.notion_page_url;
  if (url) chrome.tabs.create({ url });
  chrome.notifications.clear(id);
});

// ------------------------------------------------------------------ Notion OAuth (one time)
async function connectNotion(interactive = true) {
  const cfg = await api("/oauth/notion/config");
  const redirect = chrome.identity.getRedirectURL("notion");
  if (cfg.redirect_uri !== redirect) {
    throw new ApiError(
      "OAUTH_NOT_CONFIGURED",
      `The server expects redirect URI ${cfg.redirect_uri} but this extension uses ${redirect}. Update NOTION_REDIRECT_URI.`
    );
  }
  let responseUrl;
  try {
    responseUrl = await chrome.identity.launchWebAuthFlow({ url: cfg.authorize_url, interactive });
  } catch (e) {
    throw new ApiError("OAUTH_CANCELLED", "Notion sign-in was cancelled.");
  }
  const params = new URL(responseUrl).searchParams;
  if (params.get("error")) throw new ApiError("OAUTH_CANCELLED", `Notion sign-in failed: ${params.get("error")}`);
  if (params.get("state") !== cfg.state) throw new ApiError("OAUTH_EXCHANGE_FAILED", "Notion sign-in state mismatch.");
  const code = params.get("code");
  const token = await api("/oauth/notion/exchange", { method: "POST", body: { code, redirect_uri: redirect }, timeoutMs: 60000 });
  const notion = {
    access_token: token.access_token,
    refresh_token: token.refresh_token || null,
    workspace_name: token.workspace_name || "Notion",
    workspace_icon: token.workspace_icon || null,
    default_parent_id: token.default_parent_id || null,
    connected_at: Date.now(),
  };
  await chrome.storage.local.set({ notion });
  return publicNotion(notion);
}

function publicNotion(n) {
  if (!n) return null;
  return {
    workspace_name: n.workspace_name,
    workspace_icon: n.workspace_icon,
    connected_at: n.connected_at,
    // Page picked in Settings; null = the default "Video Notes" page.
    destination: n.default_parent_title ? { title: n.default_parent_title, icon: n.default_parent_icon || null } : null,
  };
}

async function ensureNotion() {
  return (await getNotion()) || (await connectNotion(true), await getNotion());
}

// ------------------------------------------------------------------ jobs
async function submitJob(video) {
  const notion = await ensureNotion();
  const res = await api("/jobs", {
    method: "POST",
    body: {
      youtube_url: video.url,
      notion_token: notion.access_token,
      notion_parent_id: notion.default_parent_id || undefined,
      duration_sec: Number.isFinite(video.duration_sec) ? video.duration_sec : undefined,
      video_title: video.title || undefined,
    },
  });
  return addJob(res, video.title);
}

async function addJob(res, title) {
  const job = {
    job_id: res.job_id,
    video_id: res.video_id,
    video_title: title || res.video_id,
    status: res.status,
    progress: 0,
    status_detail: "Waiting for a free worker",
    submitted_at: Date.now(),
  };
  const jobs = await getJobs();
  await saveJobs([job, ...jobs.filter((j) => j.job_id !== job.job_id)]);
  ensurePolling();
  return job;
}

// New job for the same video; the failed card stays as it was.
async function retryJob(jobId) {
  const notion = await ensureNotion();
  const old = (await getJobs()).find((j) => j.job_id === jobId);
  const res = await api(`/jobs/${jobId}/retry`, {
    method: "POST",
    body: { notion_token: notion.access_token, notion_parent_id: notion.default_parent_id || undefined },
  });
  return addJob(res, old && old.video_title);
}

async function cancelJob(jobId) {
  const s = await api(`/jobs/${jobId}`, { method: "DELETE" });
  const jobs = await getJobs();
  await saveJobs(jobs.map((j) => (j.job_id === jobId ? { ...j, ...s, video_title: s.video_title || j.video_title } : j)));
  return s;
}

async function searchNotionPages(query) {
  const notion = await getNotion();
  if (!notion) throw new ApiError("MISSING_NOTION_TOKEN", "Connect Notion first.");
  return api("/notion/pages/search", { method: "POST", body: { notion_token: notion.access_token, query } });
}

async function setNotionDestination(page) {
  const notion = await getNotion();
  if (!notion) throw new ApiError("MISSING_NOTION_TOKEN", "Connect Notion first.");
  // null = reset: the worker then finds/creates the default "Video Notes" page itself.
  notion.default_parent_id = page ? page.id : null;
  notion.default_parent_title = page ? page.title : null;
  notion.default_parent_icon = page ? page.icon || null : null;
  await chrome.storage.local.set({ notion });
  return publicNotion(notion);
}

async function pollJobs() {
  const jobs = await getJobs();
  const wasActive = new Set(jobs.filter((j) => !TERMINAL.has(j.status)).map((j) => j.job_id));
  let changed = false;
  await Promise.all(
    jobs.map(async (j, i) => {
      if (TERMINAL.has(j.status)) return;
      try {
        const s = await api(`/jobs/${j.job_id}`, { timeoutMs: 8000 });
        jobs[i] = { ...j, ...s, video_title: s.video_title || j.video_title, polled_at: Date.now() };
        changed = true;
      } catch (e) {
        if (e.status === 404) {
          jobs[i] = { ...j, status: "FAILED", error: { code: "JOB_LOST", message: "The server no longer knows this job." } };
          changed = true;
        } else {
          jobs[i] = { ...j, offline: true };
          changed = true;
        }
      }
    })
  );
  const finished = jobs.filter((j) => wasActive.has(j.job_id) && TERMINAL.has(j.status));
  finished.forEach(notifyFinished);
  if (finished.length) flashBadge(finished);
  if (changed) await saveJobs(jobs);
  if (!jobs.some((j) => !TERMINAL.has(j.status))) chrome.alarms.clear(POLL_ALARM);
  return jobs;
}

function ensurePolling() {
  chrome.alarms.create(POLL_ALARM, { periodInMinutes: 0.5 });
}

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === POLL_ALARM) pollJobs();
});

chrome.runtime.onStartup.addListener(async () => {
  const jobs = await getJobs();
  await updateBadge(jobs);
  if (jobs.some((j) => !TERMINAL.has(j.status))) ensurePolling();
});

// ------------------------------------------------------------------ messaging
async function handle(msg) {
  switch (msg && msg.type) {
    case "SUBMIT_JOB":
      return { ok: true, job: await submitJob(msg.video) };
    case "CONNECT_NOTION":
      return { ok: true, notion: await connectNotion(true) };
    case "DISCONNECT_NOTION":
      await chrome.storage.local.remove("notion");
      return { ok: true };
    case "RETRY_JOB":
      return { ok: true, job: await retryJob(msg.job_id) };
    case "CANCEL_JOB":
      return { ok: true, job: await cancelJob(msg.job_id) };
    case "SEARCH_NOTION_PAGES":
      return { ok: true, pages: await searchNotionPages(msg.query || "") };
    case "SET_NOTION_DESTINATION":
      return { ok: true, notion: await setNotionDestination(msg.page || null) };
    case "GET_STATE": {
      const jobs = msg.refresh ? await pollJobs() : await getJobs();
      return { ok: true, jobs, notion: publicNotion(await getNotion()) };
    }
    case "CLEAR_FINISHED": {
      const jobs = (await getJobs()).filter((j) => !TERMINAL.has(j.status));
      await saveJobs(jobs);
      return { ok: true, jobs };
    }
    default:
      return { ok: false, error: { code: "UNKNOWN_MESSAGE", message: "Unknown request" } };
  }
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  handle(msg)
    .then(sendResponse)
    .catch((e) =>
      sendResponse({ ok: false, error: { code: e.code || "ERROR", message: e.message || String(e) } })
    );
  return true; // async response
});
