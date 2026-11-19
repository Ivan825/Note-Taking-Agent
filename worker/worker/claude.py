"""Claude client wrapper: global rate limiting, chaos hooks, structured JSON output and
mapping of SDK errors onto the pipeline's retry taxonomy."""

from __future__ import annotations

import base64
import json
import logging
from functools import lru_cache

import anthropic

from notes_common import chaos, ratelimit
from notes_common.config import get_settings
from notes_common.errors import PermanentError, RateLimited, TransientError

log = logging.getLogger(__name__)

# Rough per-image input cost used only for rate-limit accounting.
IMAGE_TOKENS = 1600


@lru_cache
def _client() -> anthropic.Anthropic:
    s = get_settings()
    if not s.anthropic_api_key:
        raise PermanentError("ANTHROPIC_API_KEY is not configured on the worker", "LLM_NOT_CONFIGURED")
    # max_retries=0: retries are owned by the queue (delay queues + DLQ), not hidden inside the SDK.
    return anthropic.Anthropic(api_key=s.anthropic_api_key, max_retries=0, timeout=180)


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def structured_call(
    *,
    system: str,
    content: list[dict] | str,
    schema: dict,
    max_tokens: int = 16000,
    est_input_tokens: int | None = None,
) -> dict:
    """One Messages API call constrained to `schema`. Returns the parsed JSON object."""
    s = get_settings()
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    if est_input_tokens is None:
        est_input_tokens = estimate_tokens(system) + sum(
            estimate_tokens(b.get("text", "")) if b.get("type") == "text" else IMAGE_TOKENS for b in content
        )

    chaos.maybe_429("llm")
    ratelimit.acquire("llm_requests", 1, api="llm")
    ratelimit.acquire("llm_input_tokens", est_input_tokens, api="llm", max_wait=45)

    try:
        response = _client().messages.create(
            model=s.llm_model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": content}],
            output_config={
                "effort": s.llm_effort,
                "format": {"type": "json_schema", "schema": schema},
            },
        )
    except anthropic.RateLimitError as exc:
        ra = exc.response.headers.get("retry-after") if exc.response is not None else None
        raise RateLimited("llm", retry_after=float(ra) if ra else None) from exc
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
        raise PermanentError("The Claude API key was rejected", "LLM_AUTH_FAILED") from exc
    except anthropic.NotFoundError as exc:
        raise PermanentError(f"Claude model '{s.llm_model}' was not found", "LLM_MODEL_NOT_FOUND") from exc
    except anthropic.BadRequestError as exc:
        raise PermanentError(f"Claude rejected the request: {exc.message}", "LLM_BAD_REQUEST") from exc
    except (anthropic.APIConnectionError, anthropic.APITimeoutError) as exc:
        raise TransientError("Could not reach the Claude API", "LLM_UNREACHABLE") from exc
    except anthropic.APIStatusError as exc:
        raise TransientError(f"Claude API error {exc.status_code}", "LLM_UNAVAILABLE") from exc

    if response.stop_reason == "refusal":
        raise PermanentError("Claude declined to summarize this video", "LLM_REFUSED")
    if response.stop_reason == "max_tokens":
        raise TransientError("Claude's answer was cut off", "LLM_TRUNCATED")
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise TransientError("Claude returned malformed JSON", "LLM_BAD_OUTPUT") from exc


def image_block(jpeg: bytes) -> dict:
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(jpeg).decode()},
    }
