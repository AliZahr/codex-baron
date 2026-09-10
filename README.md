# Codex Baron

Codex Baron routes engineering subtasks across specialized Codex models while keeping a primary agent responsible for integration, safety, and verification.

## Quick start

```bash
codex plugin marketplace add AliZahr/codex-baron
codex plugin add codex-baron@codex-baron
python3 plugins/codex-baron/scripts/configure_repo.py /absolute/path/to/your/repository
```

When installing from a local clone, replace `AliZahr/codex-baron` with `"$PWD"`.

Start a new Codex task after installation. See [the plugin README](plugins/codex-baron/README.md) for configuration, privacy, verification, and rollout guidance.
