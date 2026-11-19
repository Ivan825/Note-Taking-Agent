"""Prompts and JSON schemas for LLM synthesis."""

from __future__ import annotations

SYSTEM_NOTES = (
    "You turn video transcripts into concise, accurate study notes for a student. "
    "Ground every statement in the transcript and never invent facts, numbers or names. "
    "Write bullets as information-dense notes (definitions, formulas, steps, examples), not as a narration "
    "of what the speaker does."
)

CHUNK_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "overview": {"type": "string"},
        "takeaways": {"type": "array", "items": {"type": "string"}},
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "heading": {"type": "string"},
                    "start_sec": {"type": "number"},
                    "bullets": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["heading", "start_sec", "bullets"],
                "additionalProperties": False,
            },
        },
        "key_frames": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "timestamp_sec": {"type": "number"},
                    "caption": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["timestamp_sec", "caption", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["overview", "takeaways", "sections", "key_frames"],
    "additionalProperties": False,
}

MERGE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "overview": {"type": "string"},
        "takeaways": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["overview", "takeaways"],
    "additionalProperties": False,
}

FRAME_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "shows_expected_visual": {"type": "boolean"},
        "caption": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["shows_expected_visual", "caption", "reason"],
    "additionalProperties": False,
}


def chunk_prompt(*, title: str, channel: str, duration: str, part: int, parts: int, start: str, end: str,
                 max_frames: int, transcript: str) -> str:
    scope = f"This is part {part} of {parts} of the transcript, covering {start}-{end}." if parts > 1 else \
        "This is the complete transcript."
    return f"""Video: "{title}" by {channel} (length {duration}).
{scope}

Produce:
1. sections - the topics covered, in order. Each has a short descriptive heading, start_sec (when the topic
   begins, in seconds), and 2-6 bullets with the key ideas, definitions, formulas, steps or examples.
2. key_frames - between {min(3, max_frames)} and {max_frames} moments where the screen most likely shows
   something visually important that is worth capturing as an image: a diagram, slide, chart, equation,
   code, worked example or demonstration. Use cues in the narration ("as you can see", "this diagram",
   "here", "look at", drawing or writing on screen). If the narration has few explicit cues (talks,
   commentary, news), still pick the moments most likely to show informative on-screen content - text or
   headlines, charts, screenshots, product shots or footage illustrating the point - typically a few
   seconds into a new topic. Each frame is captured from the real video and re-captioned from its actual
   pixels, so only real content is ever shown. For each:
   - timestamp_sec: when the visual is fully on screen - usually 2-6 seconds after the cue - and inside
     {start}-{end}.
   - caption: one sentence describing what the frame shows.
   - reason: one sentence explaining why this visual matters for understanding the material.
   Spread them across the video and avoid moments where only the speaker is likely to be on screen.
3. overview - a 2-3 sentence summary.
4. takeaways - the 3-5 most important things to remember.

Transcript (each line starts with its [m:ss] timestamp):
{transcript}"""


def merge_prompt(*, title: str, parts: list[dict]) -> str:
    body = "\n\n".join(
        f"Part {i + 1} overview: {p['overview']}\nPart {i + 1} takeaways:\n- " + "\n- ".join(p["takeaways"])
        for i, p in enumerate(parts)
    )
    return (
        f'These are partial notes for consecutive parts of the video "{title}". Write one overview '
        "(3-4 sentences) for the whole video and the 4-6 most important takeaways overall, keeping the "
        f"video's order.\n\n{body}"
    )


def frame_prompt(*, timestamp: str, caption: str, reason: str, context: str) -> str:
    return f"""This image is a real frame captured from the video at {timestamp}.
It was flagged from the transcript with this proposed caption:
  caption: {caption}
  reason: {reason}

Narration around this moment:
{context}

Write the final caption that will be shown under this image in the study notes:
- caption: one sentence describing exactly what is visible in THIS frame (text, diagram, chart, code...).
  Describe only what you can actually see.
- reason: one sentence on why this visual matters, grounded in the narration.
- shows_expected_visual: false if the frame does not show meaningful visual content (e.g. only the
  speaker, a blank or transition screen), true otherwise."""
