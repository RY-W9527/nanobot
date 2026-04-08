"""Lightweight token usage tracker for runtime instrumentation."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from loguru import logger


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_mapping(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        try:
            dumped = value.model_dump()
            if isinstance(dumped, dict):
                return dumped
        except Exception:
            return None
    if hasattr(value, "__dict__"):
        try:
            return dict(vars(value))
        except Exception:
            return None
    return None


def extract_usage(response_or_usage: Any) -> dict[str, int | None]:
    """Extract token usage from dict-style or object-style payloads."""
    usage_obj = response_or_usage
    top_map = _as_mapping(response_or_usage)
    if top_map is not None and "usage" in top_map:
        usage_obj = top_map.get("usage")
    elif hasattr(response_or_usage, "usage"):
        usage_obj = getattr(response_or_usage, "usage")

    usage_map = _as_mapping(usage_obj)
    if usage_map is not None:
        prompt = _coerce_int(usage_map.get("prompt_tokens"))
        completion = _coerce_int(usage_map.get("completion_tokens"))
        total = _coerce_int(usage_map.get("total_tokens"))
    else:
        prompt = _coerce_int(getattr(usage_obj, "prompt_tokens", None))
        completion = _coerce_int(getattr(usage_obj, "completion_tokens", None))
        total = _coerce_int(getattr(usage_obj, "total_tokens", None))

    if total is None and prompt is not None and completion is not None:
        total = prompt + completion

    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
    }


def default_log_path() -> Path:
    """Resolve token ledger path from env or current working directory."""
    raw = os.environ.get("NANOBOT_TOKEN_LOG_PATH", "token_usage.jsonl").strip() or "token_usage.jsonl"
    return Path(raw).expanduser()


def log_token_event(
    *,
    phase: str,
    provider: str | None,
    model: str | None,
    usage: dict[str, int | None] | None = None,
    latency_s: float | None = None,
    session_id: str | None = None,
    task_id: str | None = None,
    step_id: int | str | None = None,
    extra: dict[str, Any] | None = None,
    log_path: Path | None = None,
) -> None:
    """Append one token usage event to JSONL."""
    usage = usage or {}
    event = {
        "timestamp": datetime.now(UTC).isoformat(),
        "task_id": task_id,
        "session_id": session_id,
        "step_id": step_id,
        "phase": phase,
        "provider": provider,
        "model": model,
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "latency_s": latency_s,
        "extra": extra or {},
    }

    path = log_path or default_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        logger.debug("Failed to write token usage event to {}", path)
