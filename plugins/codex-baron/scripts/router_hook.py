#!/usr/bin/env python3
"""Codex hook for deterministic routing and metadata-only telemetry."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import sys
from datetime import datetime, timezone
from typing import Any


PLUGIN_ROOT = Path(os.environ.get("PLUGIN_ROOT", Path(__file__).resolve().parents[1])).resolve()
DEFAULT_CONFIG = PLUGIN_ROOT / "config" / "router.json"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def load_config(cwd: Path) -> dict[str, Any]:
    config = _read_json(DEFAULT_CONFIG)
    override = _read_json(cwd / ".codex" / "codex-baron.json")
    for key in config:
        if key in override and isinstance(override[key], type(config[key])):
            config[key] = override[key]
    env_threshold = os.environ.get("ENGINEERING_ROUTER_LARGE_FILE_LINES")
    if env_threshold and env_threshold.isdigit():
        config["large_file_lines"] = max(50, int(env_threshold))
    if os.environ.get("ENGINEERING_ROUTER_TELEMETRY", "").lower() in {"0", "off", "false"}:
        config["telemetry_enabled"] = False
    return config


def _matches(text: str, words: tuple[str, ...]) -> bool:
    return any(re.search(rf"(?<![a-z0-9_]){re.escape(word)}(?![a-z0-9_])", text) for word in words)


def classify_prompt(prompt: str) -> tuple[str, str]:
    text = prompt.lower()
    senior = (
        "security", "secure", "vulnerability", "auth", "oauth", "permission", "crypto",
        "secret", "payment", "billing", "race condition", "deadlock", "concurrency",
        "transaction", "migration", "architecture", "root cause", "production incident",
    )
    debug = ("debug", "bug", "fix", "crash", "failure", "regression", "incorrect", "why")
    tests = ("test", "tests", "fixture", "fixtures", "mock", "mocks", "snapshot", "coverage")
    discovery = ("explore", "analyze", "analyse", "trace", "find", "locate", "map", "understand", "summarize")
    generation = ("scaffold", "boilerplate", "generate", "stub", "config", "documentation", "docs", "translate")
    if _matches(text, senior):
        return "senior_reviewer", "high-risk or deep-reasoning keywords"
    if _matches(text, debug):
        return "senior_reviewer", "debugging or root-cause analysis"
    if _matches(text, tests):
        return "test_writer", "test-focused request"
    if _matches(text, discovery):
        return "bulk_reader", "repository discovery or reading"
    if _matches(text, generation):
        return "code_writer", "predictable generation or documentation"
    return "primary", "general engineering task"


def _safe_repo_file(cwd: Path, token: str) -> Path | None:
    cleaned = token.strip("'\"")
    if not cleaned or cleaned == "-" or cleaned.startswith("-"):
        return None
    candidate = Path(cleaned)
    if not candidate.is_absolute():
        candidate = cwd / candidate
    try:
        resolved = candidate.resolve()
        resolved.relative_to(cwd.resolve())
        return resolved if resolved.is_file() else None
    except (OSError, ValueError):
        return None


def find_large_full_read(command: str, cwd: Path, threshold: int) -> tuple[Path, int] | None:
    # Only classify simple, unpiped `cat file...` commands. Targeted reads and pipelines stay allowed.
    if any(operator in command for operator in ("|", ">", "<", ";", "&&", "||")):
        return None
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    if not tokens:
        return None
    executable = Path(tokens[0]).name
    if executable not in {"cat", "bat"}:
        return None
    for token in tokens[1:]:
        path = _safe_repo_file(cwd, token)
        if path is None:
            continue
        try:
            with path.open("rb") as handle:
                lines = sum(1 for _ in handle)
        except OSError:
            continue
        if lines > threshold:
            return path, lines
    return None


def patch_paths(command: str) -> list[str]:
    return re.findall(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", command, flags=re.MULTILINE)


def sensitive_path(paths: list[str], patterns: list[str]) -> bool:
    joined = " ".join(paths).lower()
    return any(pattern.lower() in joined for pattern in patterns)


def _bucket(value: int) -> str:
    if value <= 500:
        return "0-500"
    if value <= 1000:
        return "501-1000"
    if value <= 2500:
        return "1001-2500"
    return "2500+"


def write_telemetry(event: dict[str, Any], config: dict[str, Any]) -> None:
    if not config.get("telemetry_enabled", True):
        return
    data_root = os.environ.get("PLUGIN_DATA")
    base = Path(data_root) if data_root else Path(event.get("cwd") or ".") / ".codex" / "router-telemetry"
    try:
        base.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event.get("event"),
            "route": event.get("route"),
            "reason": event.get("reason"),
            "model": event.get("model"),
            "tool": event.get("tool"),
            "agent_type": event.get("agent_type"),
            "session_hash": hashlib.sha256(str(event.get("session_id", "")).encode()).hexdigest()[:16],
        }
        if event.get("line_count") is not None:
            record["line_bucket"] = _bucket(int(event["line_count"]))
        if event.get("extension"):
            record["extension"] = event["extension"]
        with (base / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({k: v for k, v in record.items() if v is not None}, sort_keys=True) + "\n")
    except OSError:
        pass  # Telemetry must never break an engineering task.


def context_output(event_name: str, message: str) -> dict[str, Any]:
    return {"hookSpecificOutput": {"hookEventName": event_name, "additionalContext": message}}


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return 0
    if not isinstance(payload, dict):
        return 0
    event_name = str(payload.get("hook_event_name", ""))
    cwd = Path(payload.get("cwd") or ".").resolve()
    config = load_config(cwd)
    base_event = {
        "event": event_name,
        "session_id": payload.get("session_id"),
        "model": payload.get("model"),
        "cwd": str(cwd),
    }

    if event_name == "UserPromptSubmit" and config.get("prompt_routing_enabled", True):
        route, reason = classify_prompt(str(payload.get("prompt", "")))
        write_telemetry({**base_event, "route": route, "reason": reason}, config)
        if route == "primary":
            return 0
        message = (
            f"Codex Baron recommendation: use `{route}` for bounded {reason}. "
            "Keep the primary agent responsible for integration and verification. Apply the codex-baron skill's "
            "risk rules; skip delegation when the task is trivial or the agent is unavailable."
        )
        print(json.dumps(context_output(event_name, message)))
        return 0

    if event_name == "PreToolUse":
        tool_name = str(payload.get("tool_name", ""))
        tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
        command = str(tool_input.get("command") or tool_input.get("cmd") or "")
        if tool_name == "Bash" and config.get("large_read_guard_enabled", True):
            result = find_large_full_read(command, cwd, int(config.get("large_file_lines", 500)))
            if result:
                path, lines = result
                write_telemetry({
                    **base_event, "route": "bulk_reader", "reason": "large full-file read blocked",
                    "tool": tool_name, "line_count": lines, "extension": path.suffix.lower() or "[none]",
                }, config)
                reason = (
                    f"Codex Baron blocked a full read of a {lines}-line file. Use `bulk_reader` with a focused "
                    "question, use `rg`, or read a targeted line range. Do not retry the same full-file command."
                )
                print(json.dumps({"hookSpecificOutput": {
                    "hookEventName": "PreToolUse", "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }}))
                return 0
        if tool_name == "apply_patch" and config.get("sensitive_review_enabled", True):
            paths = patch_paths(command)
            if paths and sensitive_path(paths, list(config.get("sensitive_patterns", []))):
                write_telemetry({**base_event, "route": "senior_reviewer", "reason": "sensitive path changed", "tool": tool_name}, config)
                print(json.dumps(context_output("PreToolUse", (
                    "Codex Baron detected a sensitive-area change. After applying the patch, require a "
                    "`senior_reviewer` pass and focused tests before declaring completion."
                ))))
                return 0
        write_telemetry({**base_event, "tool": tool_name, "reason": "tool observed"}, config)
        return 0

    if event_name in {"SubagentStart", "SubagentStop"}:
        write_telemetry({**base_event, "agent_type": payload.get("agent_type"), "reason": "subagent lifecycle"}, config)
        print("{}")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
