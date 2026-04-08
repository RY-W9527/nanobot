#!/usr/bin/env python3
"""Summarize nanobot token usage JSONL ledger."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def summarize(path: Path) -> None:
    if not path.exists():
        print(f"Token ledger not found: {path}")
        return

    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    total_events = len(events)
    prompt_total = sum(_to_int(e.get("prompt_tokens")) for e in events)
    completion_total = sum(_to_int(e.get("completion_tokens")) for e in events)
    token_total = sum(_to_int(e.get("total_tokens")) for e in events)
    avg_total = (token_total / total_events) if total_events else 0.0

    by_phase: dict[str, dict[str, int]] = defaultdict(lambda: {"events": 0, "prompt": 0, "completion": 0, "total": 0})
    by_session: dict[str, int] = defaultdict(int)
    by_task: dict[str, int] = defaultdict(int)

    for event in events:
        phase = str(event.get("phase") or "unknown")
        by_phase[phase]["events"] += 1
        by_phase[phase]["prompt"] += _to_int(event.get("prompt_tokens"))
        by_phase[phase]["completion"] += _to_int(event.get("completion_tokens"))
        by_phase[phase]["total"] += _to_int(event.get("total_tokens"))

        session_id = event.get("session_id")
        if session_id:
            by_session[str(session_id)] += _to_int(event.get("total_tokens"))
        task_id = event.get("task_id")
        if task_id:
            by_task[str(task_id)] += _to_int(event.get("total_tokens"))

    print(f"ledger: {path}")
    print(f"total recorded events: {total_events}")
    print(f"total prompt tokens: {prompt_total}")
    print(f"total completion tokens: {completion_total}")
    print(f"total tokens: {token_total}")
    print(f"average total tokens/event: {avg_total:.2f}")

    print("\ntotals by phase:")
    for phase, totals in sorted(by_phase.items(), key=lambda x: x[0]):
        print(
            f"- {phase}: events={totals['events']} prompt={totals['prompt']} "
            f"completion={totals['completion']} total={totals['total']}"
        )

    if by_session:
        print("\ntop sessions by total tokens:")
        for sid, total in sorted(by_session.items(), key=lambda x: x[1], reverse=True)[:10]:
            print(f"- {sid}: {total}")

    if by_task:
        print("\ntop tasks by total tokens:")
        for tid, total in sorted(by_task.items(), key=lambda x: x[1], reverse=True)[:10]:
            print(f"- {tid}: {total}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize token_usage.jsonl from nanobot.")
    parser.add_argument(
        "path",
        nargs="?",
        default="token_usage.jsonl",
        help="Path to token usage JSONL file (default: token_usage.jsonl)",
    )
    args = parser.parse_args()
    summarize(Path(args.path).expanduser())


if __name__ == "__main__":
    main()
