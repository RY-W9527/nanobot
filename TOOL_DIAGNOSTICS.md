# Tool diagnostics (local environment)

## Scope

This note summarizes practical causes behind observed tool instability (weather unavailable, web fetch/search failures, inconsistent results) based on current runtime code paths.

## 1) Tool registration and exposure

- Tools are registered in `AgentLoop._register_default_tools()`:
  - file tools: `read_file`, `write_file`, `edit_file`, `list_dir`
  - optional exec tool (only when `tools.exec.enable` is true)
  - web tools: `web_search`, `web_fetch`
  - messaging/spawn/cron tools
- Registration is direct and unconditional for web tools (no feature flag), so if web tools are missing at runtime, startup/config mismatch is more likely than registration code failure.

## 2) Why weather appears unavailable

- There is **no built-in `weather` function tool** in `nanobot/agent/tools`.
- `weather` is provided as a **skill** (`nanobot/skills/weather/SKILL.md`) that teaches the model to use shell `curl` commands.
- Therefore weather capability depends on:
  1. skill availability in prompt context,
  2. `exec` tool being enabled/configured,
  3. runtime network access from shell commands.
- If `tools.exec.enable=false`, the model can “know” weather steps but cannot execute them.

## 3) Why web search / web fetch can fail in practice

### Web search path behavior

- `web_search` supports providers: `brave`, `tavily`, `duckduckgo`, `searxng`, `jina`.
- Default provider is `brave`.
- If provider credentials/base URL are missing, tool falls back (often to DuckDuckGo).
- DuckDuckGo implementation imports `ddgs`; if `ddgs` is missing in the environment, search fails.

### Web fetch path behavior

- `web_fetch` first tries Jina Reader (`https://r.jina.ai/...`), then fallback via `readability-lxml`.
- If `readability-lxml` is missing, fallback can fail.
- Some sites intentionally block bot traffic (`403`), which is expected behavior for `weather.com`-style anti-bot defenses.
- Redirect target correctness depends on site behavior (geo/IP routing), so wrong-location redirects can happen (e.g., AccuWeather localization).

### Additional security/network constraints

- URL resolution and redirects are SSRF-checked in `nanobot/security/network.py`.
- Internal/private targets are blocked by design.
- Proxy misconfiguration can also cause failures (`httpx.ProxyError` path in `web_fetch`).

## 4) Environment findings from local checks

- `curl` exists.
- Python modules `ddgs` and `readability` were not importable in this test runtime.
- Missing these packages explains likely search/fetch instability even when tools are registered.

## 5) Recommended operational checks

1. Confirm tool config:
   - `tools.exec.enable` should be `true` for weather skill usage.
   - `tools.web.search.provider` and related credentials/env vars should match chosen provider.
2. Verify dependencies in active venv:
   - `ddgs`, `readability-lxml` import successfully.
3. Use `/status` and logs (`nanobot agent --logs`) to inspect behavior and retries.
4. Prefer robust sources/APIs for weather (Open-Meteo/wttr) rather than scraping anti-bot sites.

## 6) Bottom line

- “Weather tool unavailable” is primarily a **capability-model mismatch**:
  - weather is a skill pattern, not a native registered tool.
- “Web tools unreliable” is usually **environment + target-site behavior**:
  - missing search/fetch deps in venv,
  - anti-bot 403s / geo redirects,
  - provider credential/config fallbacks.
