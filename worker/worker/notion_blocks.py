"""Builds the Notion page body: overview, takeaways, sections with bullets, and real frames
(each paired with its LLM caption + reason) interleaved under the section they belong to."""

from __future__ import annotations

from datetime import date

from .timefmt import mmss

MAX_BLOCKS = 100  # Notion's per-request children limit
MAX_TEXT = 1900   # Notion limit is 2000 chars per text object


def _text(
    content: str, link: str | None = None, bold: bool = False, italic: bool = False, color: str = "default"
) -> dict:
    t: dict = {"type": "text", "text": {"content": content[:MAX_TEXT]}}
    if link:
        t["text"]["link"] = {"url": link}
    if bold or italic or color != "default":
        t["annotations"] = {"bold": bold, "italic": italic, "color": color}
    return t


def heading(level: int, *parts: dict) -> dict:
    key = f"heading_{level}"
    return {"object": "block", "type": key, key: {"rich_text": list(parts)}}


def paragraph(*parts: dict) -> dict:
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": list(parts)}}


def bullet(text: str) -> dict:
    return {"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": [_text(text)]}}


def callout(emoji: str, *parts: dict, color: str = "gray_background") -> dict:
    return {"object": "block", "type": "callout",
            "callout": {"rich_text": list(parts), "icon": {"type": "emoji", "emoji": emoji}, "color": color}}


def divider() -> dict:
    return {"object": "block", "type": "divider", "divider": {}}


def image(upload_id: str, caption_parts: list[dict]) -> dict:
    return {"object": "block", "type": "image",
            "image": {"type": "file_upload", "file_upload": {"id": upload_id}, "caption": caption_parts}}


def frame_caption(ts: float, caption: str, reason: str, video_url: str) -> list[dict]:
    return [
        _text(f"[{mmss(ts)}] ", link=f"{video_url}&t={int(ts)}s", bold=True),
        _text(caption.rstrip(".") + ". "),
        _text("Why it matters: ", italic=True, color="gray"),
        _text(reason, italic=True, color="gray"),
    ]


def page_title(video_title: str, tag: str) -> str:
    return f"{video_title[:180]} · #{tag}"


def build_children(*, meta: dict, synthesis: dict, frames: list[dict], video_url: str,
                   frames_requested: int, transcript_source: str = "captions") -> list[dict]:
    """`frames` = [{timestamp_sec, caption, reason, upload_id}] for successfully captured frames."""
    duration = meta.get("duration_sec") or 0
    header = callout(
        "📺",
        _text("Source: ", bold=True),
        _text(meta.get("title", "YouTube video"), link=video_url),
        _text(f"  ·  {meta.get('channel', '')}  ·  {mmss(duration)}  ·  generated {date.today().isoformat()}"),
    )
    blocks: list[dict] = [header]
    if transcript_source == "speech_to_text":
        blocks.append(callout(
            "🎙️",
            _text("This video has no captions, so its transcript was produced by automatic speech recognition. "
                  "Names and technical terms may be slightly off."),
            color="blue_background",
        ))

    if synthesis.get("overview"):
        blocks += [heading(2, _text("Overview")), paragraph(_text(synthesis["overview"]))]
    takeaways = synthesis.get("takeaways") or []
    if takeaways:
        blocks.append(heading(2, _text("Key takeaways")))
        blocks += [bullet(t) for t in takeaways[:6]]

    sections = sorted(synthesis.get("sections") or [], key=lambda s: s.get("start_sec", 0))
    pending_frames = sorted(frames, key=lambda f: f["timestamp_sec"])

    # Budget: keep every frame (they are the point of the page) and trim bullets if needed.
    fixed = len(blocks) + 2 + len(pending_frames) + len(sections) + 3
    bullet_budget = max(len(sections), MAX_BLOCKS - fixed)
    total_bullets = sum(len(s.get("bullets", [])) for s in sections) or 1
    per_section_cap = max(1, int(bullet_budget * 1.0 / max(1, len(sections))))
    if total_bullets <= bullet_budget:
        per_section_cap = 10**6

    if sections:
        blocks += [divider(), heading(2, _text("Notes"))]
    for i, sec in enumerate(sections):
        next_start = sections[i + 1]["start_sec"] if i + 1 < len(sections) else float("inf")
        start = sec.get("start_sec", 0)
        stamp = _text(f"[{mmss(start)}] ", link=f"{video_url}&t={int(start)}s")
        blocks.append(heading(3, stamp, _text(sec["heading"])))
        blocks += [bullet(b) for b in sec.get("bullets", [])[:per_section_cap]]
        while pending_frames and pending_frames[0]["timestamp_sec"] < next_start:
            f = pending_frames.pop(0)
            caption = frame_caption(f["timestamp_sec"], f["caption"], f["reason"], video_url)
            blocks.append(image(f["upload_id"], caption))
    if pending_frames:  # frames with no section (e.g. no sections returned)
        blocks.append(heading(2, _text("Key frames")))
        for f in pending_frames:
            caption = frame_caption(f["timestamp_sec"], f["caption"], f["reason"], video_url)
            blocks.append(image(f["upload_id"], caption))

    missing = frames_requested - len(frames)
    if missing > 0:
        blocks.append(callout(
            "⚠️",
            _text(f"{missing} of {frames_requested} key frames could not be captured from the video and were omitted. "
                  "Only real frames from the video are ever shown."),
            color="yellow_background",
        ))
    return blocks[:MAX_BLOCKS]
