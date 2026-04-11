"""Post-run evaluation for background tasks (heartbeat & cron).

After the agent executes a background task, this module makes a lightweight
LLM call to decide whether the result warrants notifying the user.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from loguru import logger
from nanobot.utils.token_tracker import (
    estimate_usage_if_missing,
    extract_usage,
    log_token_event,
    summarize_message_roles,
    summarize_tool_schema,
)

if TYPE_CHECKING:
    from nanobot.providers.base import LLMProvider

_EVALUATE_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "evaluate_notification",
            "description": "Decide whether the user should be notified about this background task result.",
            "parameters": {
                "type": "object",
                "properties": {
                    "should_notify": {
                        "type": "boolean",
                        "description": "true = result contains actionable/important info the user should see; false = routine or empty, safe to suppress",
                    },
                    "reason": {
                        "type": "string",
                        "description": "One-sentence reason for the decision",
                    },
                },
                "required": ["should_notify"],
            },
        },
    }
]

_SYSTEM_PROMPT = (
    "You are a notification gate for a background agent. "
    "You will be given the original task and the agent's response. "
    "Call the evaluate_notification tool to decide whether the user "
    "should be notified.\n\n"
    "Notify when the response contains actionable information, errors, "
    "completed deliverables, or anything the user explicitly asked to "
    "be reminded about.\n\n"
    "Suppress when the response is a routine status check with nothing "
    "new, a confirmation that everything is normal, or essentially empty."
)


async def evaluate_response(
    response: str,
    task_context: str,
    provider: LLMProvider,
    model: str,
) -> bool:
    """Decide whether a background-task result should be delivered to the user.

    Uses a lightweight tool-call LLM request (same pattern as heartbeat
    ``_decide()``).  Falls back to ``True`` (notify) on any failure so
    that important messages are never silently dropped.
    """
    try:
        started = time.perf_counter()
        eval_messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": (
                f"## Original task\n{task_context}\n\n"
                f"## Agent response\n{response}"
            )},
        ]
        llm_response = await provider.chat_with_retry(
            messages=eval_messages,
            tools=_EVALUATE_TOOL,
            model=model,
            max_tokens=256,
            temperature=0.0,
        )
        log_token_event(
            phase="heartbeat_evaluate",
            provider=provider.__class__.__name__,
            model=model,
            usage=extract_usage(llm_response),
            latency_s=time.perf_counter() - started,
            extra={
                "message_count": 2,
                "task_context_chars": len(task_context),
                "response_chars": len(response),
                "tool_count": len(_EVALUATE_TOOL),
                "step_outcome": "tool_call" if llm_response.has_tool_calls else "final_answer",
                "finish_reason": llm_response.finish_reason,
                "retry_happened": int(getattr(provider, "_last_retry_attempts", 0) or 0) > 0,
                "retry_count": int(getattr(provider, "_last_retry_attempts", 0) or 0),
                **summarize_message_roles(eval_messages),
                **summarize_tool_schema(_EVALUATE_TOOL),
                **estimate_usage_if_missing(
                    provider=provider,
                    model=model,
                    messages=eval_messages,
                    tools=_EVALUATE_TOOL,
                    completion_text=llm_response.content,
                    has_exact_usage=extract_usage(llm_response).get("total_tokens") is not None,
                ),
            },
        )

        if not llm_response.has_tool_calls:
            logger.warning("evaluate_response: no tool call returned, defaulting to notify")
            return True

        args = llm_response.tool_calls[0].arguments
        should_notify = args.get("should_notify", True)
        reason = args.get("reason", "")
        logger.info("evaluate_response: should_notify={}, reason={}", should_notify, reason)
        return bool(should_notify)

    except Exception:
        logger.exception("evaluate_response failed, defaulting to notify")
        return True
