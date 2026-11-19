// Background service worker: one-time Notion OAuth, job submission, status polling.
import { api, ApiError, TERMINAL } from "./lib/common.js";

const MAX_JOBS = 25;
const POLL_ALARM = "vnt-poll";

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
  const active = jobs.filter((j) => !TERMINAL.has(j.status)).length;
  await chrome.action.setBadgeBackgroundColor({ color: "#6366f1" });
  await chrome.action.setBadgeText({ text: active ? String(active) : "" });
}

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
  return n ? { workspace_name: n.workspace_name, workspace_icon: n.workspace_icon, connected_at: n.connected_at } : null;
}

// ------------------------------------------------------------------ jobs
async function submitJob(video) {
  let notion = await getNotion();
  if (!notion) await connectNotion(true);
  notion = await getNotion();

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
  const job = {
    job_id: res.job_id,
    video_id: res.video_id,
    video_title: video.title || res.video_id,
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

async function pollJobs() {
  const jobs = await getJobs();
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
