# Token instrumentation notes

## Token-relevant runtime paths identified

### 1) Main agent execution loop (primary task path)

- `nanobot/agent/runner.py`
  - `AgentRunner.run(...)`
  - Calls provider LLM APIs via:
    - `provider.chat_with_retry(...)`
    - `provider.chat_stream_with_retry(...)`
  - This is used by both:
    - main user-facing agent loop (`nanobot/agent/loop.py`)
    - subagent background loop (`nanobot/agent/subagent.py`)

- `nanobot/agent/loop.py`
  - `_run_agent_loop(...)` builds and executes `AgentRunSpec`
  - Now passes telemetry session metadata (channel/chat/message id)

- `nanobot/agent/subagent.py`
  - `_run_subagent(...)` executes `AgentRunSpec`
  - Now passes subagent task id and origin session metadata

### 2) Memory consolidation / maintenance path

- `nanobot/agent/memory.py`
  - `MemoryStore.consolidate(...)`
  - Performs LLM call(s) for memory tool-call output:
    - forced `tool_choice=save_memory`
    - fallback retry with `tool_choice=auto` if unsupported
  - Instrumented as `phase="memory_consolidation"`

### 3) Other runtime LLM calls related to task execution flow

- `nanobot/heartbeat/service.py`
  - `HeartbeatService._decide(...)`
  - LLM decides whether background tasks should run
  - Instrumented as `phase="heartbeat_decide"`

- `nanobot/utils/evaluator.py`
  - `evaluate_response(...)`
  - LLM gate decides whether heartbeat/cron result should notify user
  - Instrumented as `phase="heartbeat_evaluate"`

## Instrumentation utility

- `nanobot/utils/token_tracker.py`
  - `extract_usage(...)`: robust usage parsing from dict-style or object-style payloads
  - `log_token_event(...)`: append structured event JSON per line
  - default log path:
    - env override: `NANOBOT_TOKEN_LOG_PATH`
    - else: `token_usage.jsonl` in current working directory

## Event schema (per JSONL line)

Each event includes:

- `timestamp`
- `task_id`
- `session_id`
- `step_id`
- `phase`
- `provider`
- `model`
- `prompt_tokens`
- `completion_tokens`
- `total_tokens`
- `latency_s`
- `extra` (metadata dict)

If usage is unavailable, token fields remain `null` and `extra` carries approximate context stats
(message count, serialized character length, tool count).

## Summary utility

- `scripts/summarize_token_usage.py`
  - reads `token_usage.jsonl`
  - prints:
    - total recorded events
    - total prompt/completion/total tokens
    - totals by phase
    - average total tokens per event
    - top sessions/tasks by total tokens
