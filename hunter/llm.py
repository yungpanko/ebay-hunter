"""Shared Claude client helpers."""

from __future__ import annotations

import json
import os
from typing import Any

import anthropic

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class ClaudeError(RuntimeError):
    pass


def client() -> anthropic.Anthropic:
    # A key pasted from the clipboard often carries stray whitespace or quotes, which reads as an invalid key.
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip().strip('"').strip("'")
    return anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()


def create(c: anthropic.Anthropic, **kwargs: Any):
    """messages.create with server-side refusal fallback turned on.

    If a safety classifier declines a request, the API retries it on a fallback model
    in the same call instead of returning nothing.
    """
    return c.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **kwargs)


def structured(c: anthropic.Anthropic, *, model: str, system: Any, content: list[dict], schema: dict,
               effort: str = "medium", max_tokens: int = 16000) -> dict[str, Any]:
    """One call that must come back as JSON matching `schema`."""
    resp = create(
        c,
        model=model,
        max_tokens=max_tokens,
        system=system,
        thinking={"type": "adaptive"},
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        cache_control={"type": "ephemeral"},
        messages=[{"role": "user", "content": content}],
    )
    if resp.stop_reason == "refusal":
        raise ClaudeError("Claude declined this request.")
    if resp.stop_reason == "max_tokens":
        raise ClaudeError("Claude ran out of room before finishing its answer.")
    text = next((b.text for b in resp.content if b.type == "text"), None)
    if text is None:
        raise ClaudeError("Claude returned no text.")
    return json.loads(text)


def obj(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    """JSON-schema object helper; every property required, no extras (structured-output rules)."""
    return {
        "type": "object",
        "properties": properties,
        "required": required if required is not None else list(properties),
        "additionalProperties": False,
    }


STR = {"type": "string"}
NUM = {"type": "number"}
BOOL = {"type": "boolean"}


def arr(item: dict[str, Any]) -> dict[str, Any]:
    return {"type": "array", "items": item}


def nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [schema, {"type": "null"}]}
