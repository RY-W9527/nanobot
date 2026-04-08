# Run nanobot locally from source

## Prerequisites

- macOS or Linux
- Python **3.11+**
- Git
- An API key for one configured provider (for example: OpenRouter, OpenAI, Anthropic, etc.)

## Install from source (virtualenv)

```bash
git clone https://github.com/HKUDS/nanobot.git
cd nanobot
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e .
```

## Config path

Main config file:

- `~/.nanobot/config.json`

This is the default returned by `nanobot.config.loader.get_config_path()`.

## Minimum provider/model settings

Use `nanobot onboard` once to create initial config, then ensure you have at least:

```json
{
  "providers": {
    "openrouter": {
      "apiKey": "sk-or-v1-xxxxx"
    }
  },
  "agents": {
    "defaults": {
      "provider": "openrouter",
      "model": "anthropic/claude-opus-4-5"
    }
  }
}
```

You can replace provider/model with any supported pair in your environment.

## First run

```bash
nanobot onboard
nanobot agent
```

## Minimal smoke test command

```bash
nanobot agent -m "Reply with exactly: local_smoke_ok"
```

---

## Token instrumentation smoke test

By default, token events are logged to `token_usage.jsonl` in your current working directory.

1. Run 2–3 short tasks:

```bash
nanobot agent -s cli:token-smoke -m "Say hello in one short sentence."
nanobot agent -s cli:token-smoke -m "What is 2+2? Reply with one token."
nanobot agent -s cli:token-smoke -m "Give me three bullet points about testing."
```

2. Verify ledger is populated:

```bash
wc -l token_usage.jsonl
tail -n 5 token_usage.jsonl
```

3. Summarize usage:

```bash
python scripts/summarize_token_usage.py token_usage.jsonl
```

Optional: set a custom ledger path with:

```bash
export NANOBOT_TOKEN_LOG_PATH=/path/to/token_usage.jsonl
```
