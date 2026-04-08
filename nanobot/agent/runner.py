"""Shared execution loop for tool-using agents."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

from nanobot.agent.hook import AgentHook, AgentHookContext
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.providers.base import LLMProvider, ToolCallRequest
from nanobot.utils.token_tracker import (
    estimate_usage_if_missing,
    extract_usage,
    log_token_event,
    summarize_message_roles,
    summarize_tool_schema,
)
from nanobot.utils.helpers import build_assistant_message

_DEFAULT_MAX_ITERATIONS_MESSAGE = (
    "I reached the maximum number of tool call iterations ({max_iterations}) "
    "without completing the task. You can try breaking the task into smaller steps."
)
_DEFAULT_ERROR_MESSAGE = "Sorry, I encountered an error calling the AI model."


@dataclass(slots=True)
class AgentRunSpec:
    """Configuration for a single agent execution."""

    initial_messages: list[dict[str, Any]]
    tools: ToolRegistry
    model: str
    max_iterations: int
    temperature: float | None = None
    max_tokens: int | None = None
    reasoning_effort: str | None = None
    hook: AgentHook | None = None
    error_message: str | None = _DEFAULT_ERROR_MESSAGE
    max_iterations_message: str | None = None
    concurrent_tools: bool = False
    fail_on_tool_error: bool = False
    telemetry_phase: str = "agent_loop"
    telemetry_session_id: str | None = None
    telemetry_task_id: str | None = None
    telemetry_chat_id: str | None = None
    telemetry_run_id: str | None = None
    telemetry_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AgentRunResult:
    """Outcome of a shared agent execution."""

    final_content: str | None
    messages: list[dict[str, Any]]
    tools_used: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    stop_reason: str = "completed"
    error: str | None = None
    tool_events: list[dict[str, str]] = field(default_factory=list)


class AgentRunner:
    """Run a tool-capable LLM loop without product-layer concerns."""

    def __init__(self, provider: LLMProvider):
        self.provider = provider

    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        hook = spec.hook or AgentHook()
        messages = list(spec.initial_messages)
        final_content: str | None = None
        tools_used: list[str] = []
        usage = {"prompt_tokens": 0, "completion_tokens": 0}
        error: str | None = None
        stop_reason = "completed"
        tool_events: list[dict[str, str]] = []

        for iteration in range(spec.max_iterations):
            context = AgentHookContext(iteration=iteration, messages=messages)
            await hook.before_iteration(context)
            kwargs: dict[str, Any] = {
                "messages": messages,
                "tools": spec.tools.get_definitions(),
                "model": spec.model,
            }
            if spec.temperature is not None:
                kwargs["temperature"] = spec.temperature
            if spec.max_tokens is not None:
                kwargs["max_tokens"] = spec.max_tokens
            if spec.reasoning_effort is not None:
                kwargs["reasoning_effort"] = spec.reasoning_effort

            if hook.wants_streaming():
                async def _stream(delta: str) -> None:
                    await hook.on_stream(context, delta)

                call_started = time.perf_counter()
                response = await self.provider.chat_stream_with_retry(
                    **kwargs,
                    on_content_delta=_stream,
                )
            else:
                call_started = time.perf_counter()
                response = await self.provider.chat_with_retry(**kwargs)
            latency_s = time.perf_counter() - call_started

            raw_usage = extract_usage(response)
            usage = {
                "prompt_tokens": int(raw_usage.get("prompt_tokens") or 0),
                "completion_tokens": int(raw_usage.get("completion_tokens") or 0),
            }
            messages_text = json.dumps(messages, ensure_ascii=False, default=str)
            context.response = response
            context.usage = usage
            context.tool_calls = list(response.tool_calls)
            tools_def = kwargs.get("tools") or []
            attempted_tool_call = bool(response.has_tool_calls)
            request_mode = "tool_augmented" if tools_def else "direct_answer_only"
            provider_retry_count = int(getattr(self.provider, "_last_retry_attempts", 0) or 0)
            task_id = spec.telemetry_task_id or (
                spec.telemetry_session_id if (spec.telemetry_session_id or "").startswith("cli:") else None
            )

            if response.has_tool_calls:
                if hook.wants_streaming():
                    await hook.on_stream_end(context, resuming=True)

                messages.append(build_assistant_message(
                    response.content or "",
                    tool_calls=[tc.to_openai_tool_call() for tc in response.tool_calls],
                    reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                ))
                tools_used.extend(tc.name for tc in response.tool_calls)

                await hook.before_execute_tools(context)

                results, new_events, fatal_error = await self._execute_tools(spec, response.tool_calls)
                tool_events.extend(new_events)
                context.tool_results = list(results)
                context.tool_events = list(new_events)
                tool_obs_chars = sum(len(str(r)) for r in results)
                tool_errors = [e for e in new_events if e.get("status") == "error"]
                error_detail = tool_errors[0]["detail"] if tool_errors else None
                error_type = None
                if error_detail and ":" in error_detail:
                    error_type = error_detail.split(":", 1)[0].strip()
                if fatal_error is not None:
                    error = f"Error: {type(fatal_error).__name__}: {fatal_error}"
                    stop_reason = "tool_error"
                    context.error = error
                    context.stop_reason = stop_reason
                    await hook.after_iteration(context)
                    step_outcome = "tool_error"
                    finish_reason = response.finish_reason
                    extra = {
                        "loop_iteration": iteration,
                        "step_outcome": step_outcome,
                        "finish_reason": finish_reason,
                        "retry_happened": provider_retry_count > 0,
                        "retry_count": provider_retry_count,
                        "streaming": hook.wants_streaming(),
                        "request_mode": request_mode,
                        "message_count": len(messages),
                        "serialized_message_chars": len(messages_text),
                        "tool_count": len(tools_def),
                        "model_attempted_tool_call": attempted_tool_call,
                        "tool_call_count": len(response.tool_calls),
                        "tool_names_called": [tc.name for tc in response.tool_calls],
                        "tool_observation_count": len(results),
                        "tool_observation_chars": tool_obs_chars,
                        "tool_events": new_events,
                        "tool_error_type": error_type,
                        "tool_error_message": error_detail[:200] if error_detail else None,
                        "session_id": spec.telemetry_session_id,
                        "chat_id": spec.telemetry_chat_id,
                        "run_id": spec.telemetry_run_id or spec.telemetry_session_id,
                        **summarize_message_roles(messages),
                        **summarize_tool_schema(tools_def),
                        **estimate_usage_if_missing(
                            provider=self.provider,
                            model=spec.model,
                            messages=messages,
                            tools=tools_def,
                            completion_text=response.content,
                            has_exact_usage=raw_usage.get("total_tokens") is not None,
                        ),
                        **spec.telemetry_metadata,
                    }
                    log_token_event(
                        phase=spec.telemetry_phase,
                        provider=self.provider.__class__.__name__,
                        model=spec.model,
                        usage=raw_usage,
                        latency_s=latency_s,
                        session_id=spec.telemetry_session_id,
                        task_id=task_id,
                        step_id=iteration,
                        extra=extra,
                    )
                    break
                for tool_call, result in zip(response.tool_calls, results):
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": tool_call.name,
                        "content": result,
                    })
                step_outcome = "tool_call"
                finish_reason = response.finish_reason
                extra = {
                    "loop_iteration": iteration,
                    "step_outcome": step_outcome,
                    "finish_reason": finish_reason,
                    "retry_happened": provider_retry_count > 0,
                    "retry_count": provider_retry_count,
                    "streaming": hook.wants_streaming(),
                    "request_mode": request_mode,
                    "message_count": len(messages),
                    "serialized_message_chars": len(messages_text),
                    "tool_count": len(tools_def),
                    "model_attempted_tool_call": attempted_tool_call,
                    "tool_call_count": len(response.tool_calls),
                    "tool_names_called": [tc.name for tc in response.tool_calls],
                    "tool_observation_count": len(results),
                    "tool_observation_chars": tool_obs_chars,
                    "tool_events": new_events,
                    "tool_error_type": error_type,
                    "tool_error_message": error_detail[:200] if error_detail else None,
                    "session_id": spec.telemetry_session_id,
                    "chat_id": spec.telemetry_chat_id,
                    "run_id": spec.telemetry_run_id or spec.telemetry_session_id,
                    **summarize_message_roles(messages),
                    **summarize_tool_schema(tools_def),
                    **estimate_usage_if_missing(
                        provider=self.provider,
                        model=spec.model,
                        messages=messages,
                        tools=tools_def,
                        completion_text=response.content,
                        has_exact_usage=raw_usage.get("total_tokens") is not None,
                    ),
                    **spec.telemetry_metadata,
                }
                log_token_event(
                    phase=spec.telemetry_phase,
                    provider=self.provider.__class__.__name__,
                    model=spec.model,
                    usage=raw_usage,
                    latency_s=latency_s,
                    session_id=spec.telemetry_session_id,
                    task_id=task_id,
                    step_id=iteration,
                    extra=extra,
                )
                await hook.after_iteration(context)
                continue

            if hook.wants_streaming():
                await hook.on_stream_end(context, resuming=False)

            clean = hook.finalize_content(context, response.content)
            if response.finish_reason == "error":
                final_content = clean or spec.error_message or _DEFAULT_ERROR_MESSAGE
                stop_reason = "error"
                error = final_content
                context.final_content = final_content
                context.error = error
                context.stop_reason = stop_reason
                await hook.after_iteration(context)
                step_outcome = "error"
                extra = {
                    "loop_iteration": iteration,
                    "step_outcome": step_outcome,
                    "finish_reason": response.finish_reason,
                    "retry_happened": provider_retry_count > 0,
                    "retry_count": provider_retry_count,
                    "streaming": hook.wants_streaming(),
                    "request_mode": request_mode,
                    "message_count": len(messages),
                    "serialized_message_chars": len(messages_text),
                    "tool_count": len(tools_def),
                    "model_attempted_tool_call": attempted_tool_call,
                    "tool_call_count": 0,
                    "tool_names_called": [],
                    "tool_observation_count": 0,
                    "tool_observation_chars": 0,
                    "tool_events": [],
                    "session_id": spec.telemetry_session_id,
                    "chat_id": spec.telemetry_chat_id,
                    "run_id": spec.telemetry_run_id or spec.telemetry_session_id,
                    **summarize_message_roles(messages),
                    **summarize_tool_schema(tools_def),
                    **estimate_usage_if_missing(
                        provider=self.provider,
                        model=spec.model,
                        messages=messages,
                        tools=tools_def,
                        completion_text=response.content,
                        has_exact_usage=raw_usage.get("total_tokens") is not None,
                    ),
                    **spec.telemetry_metadata,
                }
                log_token_event(
                    phase=spec.telemetry_phase,
                    provider=self.provider.__class__.__name__,
                    model=spec.model,
                    usage=raw_usage,
                    latency_s=latency_s,
                    session_id=spec.telemetry_session_id,
                    task_id=task_id,
                    step_id=iteration,
                    extra=extra,
                )
                break

            messages.append(build_assistant_message(
                clean,
                reasoning_content=response.reasoning_content,
                thinking_blocks=response.thinking_blocks,
            ))
            final_content = clean
            context.final_content = final_content
            context.stop_reason = stop_reason
            await hook.after_iteration(context)
            step_outcome = "final_answer"
            extra = {
                "loop_iteration": iteration,
                "step_outcome": step_outcome,
                "finish_reason": response.finish_reason,
                "retry_happened": provider_retry_count > 0,
                "retry_count": provider_retry_count,
                "streaming": hook.wants_streaming(),
                "request_mode": request_mode,
                "message_count": len(messages),
                "serialized_message_chars": len(messages_text),
                "tool_count": len(tools_def),
                "model_attempted_tool_call": attempted_tool_call,
                "tool_call_count": 0,
                "tool_names_called": [],
                "tool_observation_count": 0,
                "tool_observation_chars": 0,
                "tool_events": [],
                "session_id": spec.telemetry_session_id,
                "chat_id": spec.telemetry_chat_id,
                "run_id": spec.telemetry_run_id or spec.telemetry_session_id,
                **summarize_message_roles(messages),
                **summarize_tool_schema(tools_def),
                **estimate_usage_if_missing(
                    provider=self.provider,
                    model=spec.model,
                    messages=messages,
                    tools=tools_def,
                    completion_text=response.content,
                    has_exact_usage=raw_usage.get("total_tokens") is not None,
                ),
                **spec.telemetry_metadata,
            }
            log_token_event(
                phase=spec.telemetry_phase,
                provider=self.provider.__class__.__name__,
                model=spec.model,
                usage=raw_usage,
                latency_s=latency_s,
                session_id=spec.telemetry_session_id,
                task_id=task_id,
                step_id=iteration,
                extra=extra,
            )
            break
        else:
            stop_reason = "max_iterations"
            template = spec.max_iterations_message or _DEFAULT_MAX_ITERATIONS_MESSAGE
            final_content = template.format(max_iterations=spec.max_iterations)

        return AgentRunResult(
            final_content=final_content,
            messages=messages,
            tools_used=tools_used,
            usage=usage,
            stop_reason=stop_reason,
            error=error,
            tool_events=tool_events,
        )

    async def _execute_tools(
        self,
        spec: AgentRunSpec,
        tool_calls: list[ToolCallRequest],
    ) -> tuple[list[Any], list[dict[str, str]], BaseException | None]:
        if spec.concurrent_tools:
            tool_results = await asyncio.gather(*(
                self._run_tool(spec, tool_call)
                for tool_call in tool_calls
            ))
        else:
            tool_results = [
                await self._run_tool(spec, tool_call)
                for tool_call in tool_calls
            ]

        results: list[Any] = []
        events: list[dict[str, str]] = []
        fatal_error: BaseException | None = None
        for result, event, error in tool_results:
            results.append(result)
            events.append(event)
            if error is not None and fatal_error is None:
                fatal_error = error
        return results, events, fatal_error

    async def _run_tool(
        self,
        spec: AgentRunSpec,
        tool_call: ToolCallRequest,
    ) -> tuple[Any, dict[str, str], BaseException | None]:
        try:
            result = await spec.tools.execute(tool_call.name, tool_call.arguments)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            event = {
                "name": tool_call.name,
                "status": "error",
                "detail": str(exc),
            }
            if spec.fail_on_tool_error:
                return f"Error: {type(exc).__name__}: {exc}", event, exc
            return f"Error: {type(exc).__name__}: {exc}", event, None

        detail = "" if result is None else str(result)
        detail = detail.replace("\n", " ").strip()
        if not detail:
            detail = "(empty)"
        elif len(detail) > 120:
            detail = detail[:120] + "..."
        return result, {
            "name": tool_call.name,
            "status": "error" if isinstance(result, str) and result.startswith("Error") else "ok",
            "detail": detail,
        }, None
