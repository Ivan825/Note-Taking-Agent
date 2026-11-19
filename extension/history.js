// Every job in chrome.storage.local.jobs, newest first and grouped by day, with title search and status chips.
// Server-provided strings only ever go through textContent.
import { describeJob, fmtTime, friendlyError, TERMINAL, thumbUrl } from "./lib/common.js";
import { icon } from "./lib/icons.js";

const $ = (sel) => document.querySelector(sel);
const FILTERS = {
  all: () => true,
  done: (j) => j.status === "COMPLETED",
  warn: (j) => j.status === "COMPLETED_WITH_WARNINGS",
  failed: (j) => j.status === "FAILED" || j.status === "DEAD_LETTERED",
};
let jobs = [];
let filter = "all";

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function dayLabel(ts) {
  const d = new Date(ts);
  const today = new Date();
  const days = Math.round((new Date(today.toDateString()) - new Date(d.toDateString())) / 86400000);
  if (days === 0) return "Today";
  if (days === 1) return "Yesterday";
  return d.toLocaleDateString(undefined, { weekday: days < 7 ? "long" : undefined, month: "short", day: "numeric" });
}

function row(j) {
  const d = describeJob(j);
  const li = el("li", `row ${d.cls}`);

  const thumb = el("div", "thumb-wrap");
  const img = el("img");
  img.alt = "";
  img.src = thumbUrl(j.video_id);
  thumb.append(img);
  if (!d.cls.startsWith("err")) {
    thumb.classList.add("has-bar");
    const bar = el("div", "resume");
    const fill = el("div", "resume-fill");
    fill.style.transform = `scaleX(${Math.max(0.02, (j.progress || 0) / 100)})`;
    bar.append(fill);
    thumb.append(bar);
  }
  thumb.append(el("span", "badge", j.duration_sec ? fmtTime(j.duration_sec) : ""));

  const text = el("div", "row-text");
  text.append(el("h3", "row-title", j.video_title || j.video_id));
  const meta = el("p", "row-meta");
  if (d.icon) meta.append(icon(d.icon));
  const when = new Date(j.submitted_at || 0).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  meta.append(el("span", null, `${d.text} · ${when}`));
  text.append(meta);
  if (j.status === "FAILED" || j.status === "DEAD_LETTERED") {
    text.append(el("p", "detail", friendlyError(j.error).detail));
  }
  const url = j.result && j.result.notion_page_url;
  if (url && TERMINAL.has(j.status)) {
    const actions = el("div", "actions");
    const b = el("button", "pill primary small");
    b.type = "button";
    b.append(icon("open"), document.createTextNode("View in Notion"));
    b.addEventListener("click", () => chrome.tabs.create({ url }));
    actions.append(b);
    text.append(actions);
  }
  li.append(thumb, text);
  return li;
}

function render() {
  const q = $("#q").value.trim().toLowerCase();
  const shown = jobs
    .filter((j) => FILTERS[filter](j) && (j.video_title || j.video_id || "").toLowerCase().includes(q))
    .sort((a, b) => (b.submitted_at || 0) - (a.submitted_at || 0));

  const groups = [];
  for (const j of shown) {
    const label = dayLabel(j.submitted_at || 0);
    if (!groups.length || groups.at(-1).label !== label) groups.push({ label, items: [] });
    groups.at(-1).items.push(j);
  }
  $("#groups").replaceChildren(
    ...groups.flatMap((g) => {
      const ol = el("ol", "rows");
      ol.append(...g.items.map(row));
      return [el("h2", "group-title", g.label), ol];
    })
  );
  const empty = $("#empty");
  empty.hidden = shown.length > 0;
  empty.textContent = jobs.length ? "No notes match this search." : "Notes you take on YouTube videos will show up here.";
}

async function load() {
  const res = await chrome.storage.local.get("jobs");
  jobs = Array.isArray(res.jobs) ? res.jobs : [];
  render();
}

$("#search-icon").replaceWith(icon("search"));
$("#clear").prepend(icon("delete"));
$("#q").addEventListener("input", render);
document.querySelectorAll(".chip").forEach((chip) =>
  chip.addEventListener("click", () => {
    filter = chip.dataset.filter;
    document.querySelectorAll(".chip").forEach((c) => c.setAttribute("aria-pressed", String(c === chip)));
    render();
  })
);
$("#clear").addEventListener("click", async () => {
  await chrome.runtime.sendMessage({ type: "CLEAR_FINISHED" }); // removes terminal jobs only
  load();
});
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes.jobs) load();
});
load();
