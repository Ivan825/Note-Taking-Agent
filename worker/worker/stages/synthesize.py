"""Stage 2 - LLM synthesis: summary bullets + flagged key-frame timestamps with captions.

Long transcripts are split on segment boundaries into chunks (LLM_CHUNK_TOKENS); each chunk is
summarized independently (and checkpointed), then merged in time order so section order is kept.
"""

from __future__ import annotations

import uuid

from notes_common.config import get_settings

from .. import claude, jobstore, prompts
from ..timefmt import mmss

LINE_SECONDS = 15  # group caption snippets into ~15s lines to save tokens


def transcript_lines(segments: list[dict]) -> list[tuple[float, str]]:
    lines: list[tuple[float, str]] = []
    cur_start: float | None = None
    buf: list[str] = []
    for seg in segments:
        if cur_start is None:
            cur_start = seg["start"]
        buf.append(seg["text"])
        if seg["start"] + seg["duration"] - cur_start >= LINE_SECONDS:
            lines.append((cur_start, " ".join(buf)))
            cur_start, buf = None, []
    if buf and cur_start is not None:
        lines.append((cur_start, " ".join(buf)))
    return lines


def chunk_lines(lines: list[tuple[float, str]], max_tokens: int) -> list[list[tuple[float, str]]]:
    chunks: list[list[tuple[float, str]]] = []
    cur: list[tuple[float, str]] = []
    cur_tokens = 0
    for line in lines:
        t = claude.estimate_tokens(line[1]) + 4
        if cur and cur_tokens + t > max_tokens:
            chunks.append(cur)
            cur, cur_tokens = [], 0
        cur.append(line)
        cur_tokens += t
    if cur:
        chunks.append(cur)
    return chunks


def render(lines: list[tuple[float, str]]) -> str:
    return "\n".join(f"[{mmss(start)}] {text}" for start, text in lines)


def clean_key_frames(frames: list[dict], duration: float, max_frames: int, min_gap: float = 8.0) -> list[dict]:
    """Clamp to the video, drop empties/near-duplicates, cap the count (evenly spread), sort."""
    end = max(0.0, duration - 1.0) if duration else None
    cleaned: list[dict] = []
    for f in sorted(frames, key=lambda f: f.get("timestamp_sec", 0)):
        caption = (f.get("caption") or "").strip()
        reason = (f.get("reason") or "").strip()
        if not caption or not reason:
            continue
        ts = max(0.0, float(f.get("timestamp_sec") or 0))
        if end is not None:
            ts = min(ts, end)
        if cleaned and ts - cleaned[-1]["timestamp_sec"] < min_gap:
            continue
        cleaned.append({"timestamp_sec": round(ts, 1), "caption": caption, "reason": reason})
    if len(cleaned) > max_frames:
        step = len(cleaned) / max_frames
        cleaned = [cleaned[int(i * step)] for i in range(max_frames)]
    return cleaned


def ensure_section_frames(sections: list[dict], frames: list[dict], duration: float, offset: float = 5.0) -> list[dict]:
    """Every section gets at least one frame: where Claude flagged none, capture one shortly after the section
    starts. The real frame is re-captioned from its pixels later; this text is only a provisional caption."""
    end = max(0.0, duration - 1.0) if duration else float("inf")
    ordered = sorted(sections, key=lambda s: s["start_sec"])
    out = list(frames)
    for i, sec in enumerate(ordered):
        start = sec["start_sec"]
        stop = ordered[i + 1]["start_sec"] if i + 1 < len(ordered) else float("inf")
        if any(start <= f["timestamp_sec"] < stop for f in out):
            continue
        ts = min(start + offset, stop - 1.0, end)
        if ts < start:  # section shorter than a second
            ts = min(start, end)
        out.append({
            "timestamp_sec": round(max(0.0, ts), 1),
            "caption": f"Video frame from the section \"{sec['heading']}\"",
            "reason": f"Shows what is on screen while this section is explained: {sec['heading']}.",
        })
    return sorted(out, key=lambda f: f["timestamp_sec"])


def merge_chunks(parts: list[dict]) -> dict:
    sections: list[dict] = []
    key_frames: list[dict] = []
    for p in parts:  # parts are already in time order
        sections.extend(sorted(p.get("sections", []), key=lambda s: s.get("start_sec", 0)))
        key_frames.extend(p.get("key_frames", []))
    return {"sections": sections, "key_frames": key_frames}


def run(job_id: uuid.UUID) -> dict:
    s = get_settings()
    done = jobstore.get_artifact(job_id, "synthesis")
    if done is not None:
        return done

    meta = jobstore.get_artifact(job_id, "meta") or {}
    transcript = jobstore.get_artifact(job_id, "transcript") or {"segments": []}
    lines = transcript_lines(transcript["segments"])
    chunks = chunk_lines(lines, s.llm_chunk_tokens)
    duration = float(meta.get("duration_sec") or (lines[-1][0] if lines else 0))
    per_chunk_frames = max(2, -(-s.llm_max_frames // len(chunks))) if chunks else s.llm_max_frames

    parts: list[dict] = []
    for i, chunk in enumerate(chunks):
        kind = f"chunk:{i}"
        part = jobstore.get_artifact(job_id, kind)
        if part is None:
            label = "Writing notes with Claude"
            if len(chunks) > 1:
                label += f" (part {i + 1} of {len(chunks)})"
            jobstore.set_progress(job_id, 30 + int(15 * i / max(1, len(chunks))), label)
            start = chunk[0][0]
            end = chunks[i + 1][0][0] if i + 1 < len(chunks) else duration
            part = claude.structured_call(
                system=prompts.SYSTEM_NOTES,
                content=prompts.chunk_prompt(
                    title=meta.get("title", ""),
                    channel=meta.get("channel", ""),
                    duration=mmss(duration),
                    part=i + 1,
                    parts=len(chunks),
                    start=mmss(start),
                    end=mmss(end),
                    max_frames=per_chunk_frames,
                    transcript=render(chunk),
                ),
                schema=prompts.CHUNK_SCHEMA,
            )
            jobstore.put_artifact(job_id, kind, part)
        parts.append(part)

    merged = merge_chunks(parts)
    if len(parts) == 1:
        overview, takeaways = parts[0]["overview"], parts[0]["takeaways"]
    else:
        jobstore.set_progress(job_id, 46, "Combining notes from all parts")
        combined = claude.structured_call(
            system=prompts.SYSTEM_NOTES,
            content=prompts.merge_prompt(title=meta.get("title", ""), parts=parts),
            schema=prompts.MERGE_SCHEMA,
            max_tokens=4000,
        )
        overview, takeaways = combined["overview"], combined["takeaways"]

    sections = [
        {
            "heading": sec["heading"].strip(),
            "start_sec": max(0.0, min(float(sec.get("start_sec") or 0), duration or float("inf"))),
            "bullets": [b.strip() for b in sec.get("bullets", []) if b.strip()],
        }
        for sec in merged["sections"]
        if sec.get("heading")
    ]
    key_frames = ensure_section_frames(
        sections, clean_key_frames(merged["key_frames"], duration, s.llm_max_frames), duration
    )

    synthesis = {
        "overview": overview.strip(),
        "takeaways": [t.strip() for t in takeaways if t.strip()],
        "sections": sections,
        "key_frames": key_frames,
        "chunks": len(chunks),
    }
    jobstore.put_artifact(job_id, "synthesis", synthesis)
    return synthesis
