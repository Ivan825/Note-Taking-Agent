// Shared helpers for the background service worker, popup and options page.

export const DEFAULT_GATEWAY = "http://localhost:8000";

export const STAGES = [
  { status: "QUEUED", label: "Queued" },
  { status: "TRANSCRIBING", label: "Transcribing" },
  { status: "SUMMARIZING", label: "Summarizing" },
  { status: "EXTRACTING_FRAMES", label: "Extracting frames" },
  { status: "PUBLISHING", label: "Publishing" },
  { status: "DONE", label: "Done" },
];

export const TERMINAL = new Set(["COMPLETED", "COMPLETED_WITH_WARNINGS", "FAILED", "DEAD_LETTERED"]);

export function stageIndex(status) {
  if (TERMINAL.has(status)) return STAGES.length - 1;
  const i = STAGES.findIndex((s) => s.status === status);
  return i < 0 ? 0 : i;
}

export async function getGateway() {
  const { gatewayUrl } = await chrome.storage.sync.get("gatewayUrl");
  return (gatewayUrl || DEFAULT_GATEWAY).replace(/\/+$/, "");
}

export class ApiError extends Error {
  constructor(code, message, status = 0, body = null) {
    super(message);
    this.code = code;
    this.status = status;
    this.body = body;
  }
}

export async function api(path, { method = "GET", body, timeoutMs = 15000 } = {}) {
  const base = await getGateway();
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  let res;
  try {
    res = await fetch(base + path, {
      method,
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
      signal: ctrl.signal,
    });
  } catch (e) {
    throw new ApiError("GATEWAY_UNREACHABLE", `Can't reach the note-taking server at ${base}. Is it running?`);
  } finally {
    clearTimeout(timer);
  }
  let data = null;
  try {
    data = await res.json();
  } catch (_) {
    /* empty body */
  }
  if (!res.ok) {
    const err = (data && data.error) || {};
    throw new ApiError(err.code || `HTTP_${res.status}`, err.message || `Server error (${res.status})`, res.status, data);
  }
  return data;
}

export function fmtTime(sec) {
  sec = Math.max(0, Math.round(Number(sec) || 0));
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
}

// Human-readable explanations for machine error codes. Raw JSON is never shown to the user.
const ERROR_TEXT = {
  VIDEO_TOO_LONG: ["Video is too long", "Only videos up to 20 minutes are supported."],
  NO_TRANSCRIPT: ["No transcript available", "This video has no captions, so it can't be summarized. Try a video with subtitles."],
  NO_SPEECH: ["No speech found", "This video has no captions and no recognizable speech to take notes from."],
  ASR_FAILED: ["Speech recognition failed", "The audio couldn't be transcribed. Please try again."],
  AUDIO_DOWNLOAD_FAILED: ["Couldn't download audio", "The video's audio couldn't be downloaded for transcription."],
  YOUTUBE_BOT_CHECK: ["YouTube blocked the request", "YouTube asked for a sign-in check. Try again in a few minutes."],
  VIDEO_UNAVAILABLE: ["Video unavailable", "The video is private, removed or region-locked."],
  VIDEO_AGE_RESTRICTED: ["Age-restricted video", "Age-restricted videos can't be processed."],
  VIDEO_MEMBERS_ONLY: ["Members-only video", "Members-only videos can't be processed."],
  VIDEO_IS_LIVE: ["Live stream", "Live streams and premieres can't be processed. Try again once it has ended."],
  NOTION_UNAUTHORIZED: ["Notion access expired", "Reconnect Notion and try again."],
  NOTION_PARENT_NOT_ACCESSIBLE: ["Notion page not shared", "The destination page isn't shared with Video Note Taker. Reconnect Notion and select the page."],
  NOTION_NO_DESTINATION: ["No Notion destination", "Reconnect Notion and select at least one page to share."],
  INVALID_NOTION_PARENT: ["Invalid Notion page", "The configured Notion destination isn't a valid page."],
  LLM_AUTH_FAILED: ["AI service misconfigured", "The server's Claude API key was rejected. Contact the administrator."],
  LLM_NOT_CONFIGURED: ["AI service misconfigured", "The server has no Claude API key configured."],
  LLM_MODEL_NOT_FOUND: ["AI service misconfigured", "The configured Claude model doesn't exist."],
  LLM_REFUSED: ["Couldn't summarize", "The AI declined to summarize this video."],
  GATEWAY_UNREACHABLE: ["Server unreachable", null],
  QUEUE_UNAVAILABLE: ["Server busy", "The job queue is temporarily unavailable. Try again in a moment."],
  INVALID_YOUTUBE_URL: ["Not a YouTube video", "Open a YouTube video page and try again."],
  MISSING_NOTION_TOKEN: ["Notion not connected", "Connect Notion first."],
  OAUTH_NOT_CONFIGURED: ["Notion not configured", "The server is missing its Notion OAuth credentials."],
  OAUTH_EXCHANGE_FAILED: ["Notion sign-in failed", null],
  OAUTH_CANCELLED: ["Notion sign-in cancelled", "Click Take Notes again to connect Notion."],
  CANCELLED: ["Cancelled", "You cancelled this job before it started."],
};

export function friendlyError(error) {
  if (!error) return { title: "Something went wrong", detail: "" };
  const [title, detail] = ERROR_TEXT[error.code] || [null, null];
  return {
    title: title || "Something went wrong",
    detail: detail || error.message || "",
  };
}

export const thumbUrl = (videoId) => `https://i.ytimg.com/vi/${encodeURIComponent(videoId || "")}/mqdefault.jpg`;

// One line of status for a job card/row: {cls, icon, text}. Icons are names from lib/icons.js.
export function describeJob(job) {
  switch (job.status) {
    case "COMPLETED":
      return { cls: "ok", icon: "check", text: "Notes published" };
    case "COMPLETED_WITH_WARNINGS":
      return { cls: "warn", icon: "warning", text: "Published with warnings" };
    case "FAILED":
      return { cls: "err", icon: "error", text: friendlyError(job.error).title };
    case "DEAD_LETTERED":
      return { cls: "err", icon: "error", text: "Stopped after repeated failures" };
  }
  const stage = STAGES[stageIndex(job.status)].label;
  if (job.next_retry_at) {
    const secs = Math.max(0, Math.round((Date.parse(job.next_retry_at) - Date.now()) / 1000));
    return { cls: "waiting active", icon: "schedule", text: `${stage} · retrying ${secs > 0 ? `in ${secs}s` : "now"}` };
  }
  if (job.status === "QUEUED") return { cls: "active", icon: null, text: "Queued" };
  return { cls: "active", icon: null, text: `${stage} · ${job.progress || 0}%` };
}

// Deterministic avatar colour for a workspace name (YouTube-style letter avatars).
export function avatarColor(name) {
  const palette = ["#5c6bc0", "#00897b", "#7e57c2", "#ef6c00", "#c2185b", "#0288d1", "#689f38"];
  let h = 0;
  for (const ch of name || "") h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return palette[h % palette.length];
}

export function needsReconnect(error) {
  return !!error && ["NOTION_UNAUTHORIZED", "NOTION_PARENT_NOT_ACCESSIBLE", "NOTION_NO_DESTINATION", "MISSING_NOTION_TOKEN"].includes(error.code);
}
