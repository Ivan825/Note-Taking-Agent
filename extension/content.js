// Content script: injects a "Take Notes" button into the YouTube player toolbar and keeps it
// there across YouTube's single-page navigation between videos.
(() => {
  const BTN_ID = "vnt-take-notes";
  const TOAST_ID = "vnt-toast";
  const MAX_SECONDS = 20 * 60;
  const SVG_NS = "http://www.w3.org/2000/svg";

  const currentVideoId = () =>
    location.pathname === "/watch" ? new URLSearchParams(location.search).get("v") : null;

  // YouTube enforces Trusted Types, so build the icon with DOM APIs (no innerHTML).
  function icon() {
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("width", "18");
    svg.setAttribute("height", "18");
    svg.setAttribute("aria-hidden", "true");
    const paths = [
      "M6 2h9l5 5v13a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2z",
      "M14 2v6h6",
      "M8 13h8M8 17h5",
    ];
    for (const d of paths) {
      const p = document.createElementNS(SVG_NS, "path");
      p.setAttribute("d", d);
      p.setAttribute("fill", "none");
      p.setAttribute("stroke", "currentColor");
      p.setAttribute("stroke-width", "2");
      p.setAttribute("stroke-linecap", "round");
      p.setAttribute("stroke-linejoin", "round");
      svg.appendChild(p);
    }
    return svg;
  }

  function videoInfo() {
    const id = currentVideoId();
    const video = document.querySelector("#movie_player video");
    const duration = video && Number.isFinite(video.duration) && video.duration > 0 ? video.duration : undefined;
    const titleEl = document.querySelector("ytd-watch-metadata h1 yt-formatted-string, h1.ytd-watch-metadata");
    const title = (titleEl && titleEl.textContent.trim()) || document.title.replace(/ - YouTube$/, "");
    return { video_id: id, url: `https://www.youtube.com/watch?v=${id}`, duration_sec: duration, title };
  }

  function toast(kind, title, detail) {
    const player = document.querySelector("#movie_player") || document.body;
    let el = document.getElementById(TOAST_ID);
    if (!el) {
      el = document.createElement("div");
      el.id = TOAST_ID;
      el.setAttribute("role", "status");
    }
    if (el.parentElement !== player) player.appendChild(el);
    el.className = `vnt-toast vnt-${kind}`;
    el.replaceChildren();
    const t = document.createElement("strong");
    t.textContent = title;
    el.appendChild(t);
    if (detail) {
      const d = document.createElement("span");
      d.textContent = detail;
      el.appendChild(d);
    }
    el.classList.add("vnt-show");
    clearTimeout(el._hide);
    el._hide = setTimeout(() => el.classList.remove("vnt-show"), kind === "error" ? 7000 : 4500);
  }

  function setBusy(btn, busy) {
    btn.disabled = busy;
    btn.classList.toggle("vnt-busy", busy);
    btn.querySelector(".vnt-label").textContent = busy ? "Sending…" : "Take Notes";
  }

  async function onClick(e) {
    e.preventDefault();
    e.stopPropagation();
    const btn = e.currentTarget;
    const info = videoInfo();
    if (!info.video_id) return;
    if (info.duration_sec && info.duration_sec > MAX_SECONDS) {
      toast("error", "Video is too long", "Only videos up to 20 minutes are supported.");
      return;
    }
    setBusy(btn, true);
    try {
      const res = await chrome.runtime.sendMessage({ type: "SUBMIT_JOB", video: info });
      if (res && res.ok) {
        toast("ok", "Notes queued", "Open Video Note Taker in the toolbar to follow progress.");
      } else {
        const err = (res && res.error) || {};
        toast("error", err.code === "VIDEO_TOO_LONG" ? "Video is too long" : "Couldn't start notes", err.message || "Please try again.");
      }
    } catch (err) {
      const msg = String((err && err.message) || err);
      toast("error", "Extension was updated", msg.includes("context invalidated") ? "Reload this page to keep using Take Notes." : msg);
    } finally {
      setBusy(btn, false);
    }
  }

  function makeButton() {
    const btn = document.createElement("button");
    btn.id = BTN_ID;
    btn.className = "ytp-button vnt-btn";
    btn.type = "button";
    btn.title = "Take notes on this video (Video Note Taker)";
    btn.setAttribute("aria-label", "Take notes on this video");
    btn.appendChild(icon());
    const label = document.createElement("span");
    label.className = "vnt-label";
    label.textContent = "Take Notes";
    btn.appendChild(label);
    btn.addEventListener("click", onClick);
    return btn;
  }

  function ensureButton() {
    const existing = document.getElementById(BTN_ID);
    if (!currentVideoId()) {
      if (existing) existing.remove();
      return;
    }
    const controls = document.querySelector("#movie_player .ytp-right-controls");
    if (!controls) return;
    if (existing && existing.parentElement === controls) return;
    if (existing) existing.remove();
    controls.prepend(makeButton());
  }

  // YouTube is a SPA: re-check on its navigation events and whenever the player re-renders.
  let scheduled = false;
  const schedule = () => {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => {
      scheduled = false;
      ensureButton();
    });
  };
  document.addEventListener("yt-navigate-finish", schedule);
  document.addEventListener("yt-page-data-updated", schedule);
  window.addEventListener("popstate", schedule);
  new MutationObserver(schedule).observe(document.documentElement, { childList: true, subtree: true });
  schedule();
})();
