# Codex Baron

Codex Baron keeps the standard Codex chat, CLI, or IDE experience while routing bounded engineering work to specialized models.

It provides:

- deterministic prompt recommendations before each turn;
- `bulk_reader` on GPT-5.6 Terra for broad read-only exploration;
- `code_writer` and `test_writer` on GPT-5.6 Luna for predictable work;
- `senior_reviewer` on GPT-5.6 Sol for high-risk reasoning;
- a hard guard against simple full reads of oversized files;
- mandatory senior-review context for sensitive paths;
- local metadata-only telemetry and a reporting command.

## Install from GitHub

```bash
codex plugin marketplace add AliZahr/codex-baron
codex plugin add codex-baron@codex-baron
```

For local development from the repository root:

```bash
codex plugin marketplace add "$PWD"
codex plugin add codex-baron@codex-baron
```

Start a new Codex task after installation so the plugin's skills and hooks are loaded. Review and trust the plugin hooks when Codex prompts you; the hooks execute the bundled Python script locally.

Each engineer signs into Codex using their own account:

```bash
codex login
codex login status
```

Do not share `auth.json`, session tokens, passwords, or API keys.

## Configure a repository's named agents

Plugin skills and hooks load from the plugin. Current custom-agent profiles are repository configuration, so install the included profiles once per repository:

```bash
python3 plugins/codex-baron/scripts/configure_repo.py /absolute/path/to/repository
```

Review the generated `.codex/agents/*.toml` and `.codex/codex-baron.json`, then commit them if they should apply to the team. Existing files are never overwritten unless `--force` is explicitly supplied.

## Use

Prompt normally. Examples:

```text
Implement rate limiting for password reset and add regression tests.
Find why checkout sometimes charges twice.
Map every caller of PaymentService without changing code.
Generate fixtures matching the adjacent test suite.
```

The prompt classifier adds a routing recommendation. The primary agent remains responsible for decomposition, integration, diff review, and validation. A task can therefore use several models rather than assigning the entire prompt to one worker.

## Configuration

Repository overrides live in `.codex/codex-baron.json`:

```json
{
  "version": 1,
  "large_file_lines": 500,
  "telemetry_enabled": true,
  "prompt_routing_enabled": true,
  "large_read_guard_enabled": true,
  "sensitive_review_enabled": true,
  "sensitive_patterns": ["auth", "security", "payment", "migration"]
}
```

Environment overrides:

- `ENGINEERING_ROUTER_LARGE_FILE_LINES=750`
- `ENGINEERING_ROUTER_TELEMETRY=off`

The large-read guard intentionally blocks only simple, unpiped `cat`/`bat` commands. Targeted tools such as `rg`, `sed -n`, and piped searches remain available. Hooks are a useful guardrail, not a complete security boundary.

## Telemetry and privacy

Telemetry is written to the plugin data directory as `events.jsonl`. It contains timestamps, route names, reason categories, model names, tool names, agent types, hashed session identifiers, extension names, and coarse line-count buckets.

It never intentionally records prompts, commands, source code, raw paths, account identifiers, or credentials. Disable it with the repository setting or environment variable above.

Generate a local summary:

```bash
python3 plugins/codex-baron/scripts/router_report.py /path/to/events.jsonl
```

## Verify

The MVP uses only the Python standard library:

```bash
python3 -m unittest discover -s plugins/codex-baron/tests -p 'test_*.py'
python3 plugins/codex-baron/scripts/benchmark.py
```

See [docs/rollout.md](docs/rollout.md) for a production rollout and benchmark plan.
