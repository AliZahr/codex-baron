#!/usr/bin/env python3
"""Compare tokens from paired ``codex exec --json`` runs."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import sys
from typing import Any


TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)

# A Codex event stream is local input, but it is still untrusted input.  Keep
# parsing bounded so a truncated or accidentally enormous stream cannot make
# the meter consume unbounded memory or CPU.
MAX_JSONL_BYTES = 64 * 1024 * 1024
MAX_JSONL_LINE_BYTES = 2 * 1024 * 1024
MAX_JSONL_RECORDS = 1_000_000
MAX_TOKEN_VALUE = 10**15


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value

@dataclass(frozen=True)
class UsageSummary:
    """Aggregated, privacy-preserving token counters for one Codex JSONL run."""

    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    turns: int = 0
    commands_started: int = 0
    commands_completed: int = 0
    commands_failed: int = 0
    agent_messages: int = 0
    malformed_records: int = 0
    # True when lifecycle records have no stable identity or contradictory
    # statuses; release-gate callers must reject such command budgets.
    commands_untrusted: bool = False
    path: str = ""

    @property
    def uncached_input_tokens(self) -> int:
        return max(0, self.input_tokens - self.cached_input_tokens)

    @property
    def total_tokens(self) -> int:
        # Codex reports reasoning_output_tokens as a detail of output_tokens.
        return self.input_tokens + self.output_tokens

    def to_dict(self, *, include_path: bool = False) -> dict[str, int | str]:
        result = asdict(self)
        # Absolute paths are not useful for comparison and can disclose local
        # usernames or repository layout.  Callers that explicitly need the
        # source path can opt in; manifests use the privacy-safe default.
        if not include_path:
            result.pop("path", None)
        result["uncached_input_tokens"] = self.uncached_input_tokens
        result["total_tokens"] = self.total_tokens
        return result


def _token_value(usage: dict[str, Any], field: str) -> int:
    value = usage.get(field, 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > MAX_TOKEN_VALUE:
        raise ValueError(f"invalid {field}")
    return value


def _record_identity(record: dict[str, Any]) -> str | None:
    """Return a stable turn identity when the producer supplied one."""

    for field in ("turn_id", "turnId", "event_id", "eventId", "id"):
        value = record.get(field)
        if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value):
            return str(value)
    return None


def _turn_identity(record: dict[str, Any]) -> str | None:
    for field in ("turn_id", "turnId"):
        value = record.get(field)
        if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value):
            return str(value)
    return None


def _sequence_value(record: dict[str, Any]) -> int | None:
    for field in ("sequence_number", "sequence", "event_index"):
        value = record.get(field)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"invalid {field}")
        return value
    return None


def parse_usage_file(
    path: str | Path,
    *,
    max_bytes: int = MAX_JSONL_BYTES,
    max_line_bytes: int = MAX_JSONL_LINE_BYTES,
    release_gate: bool = False,
) -> UsageSummary:
    """Aggregate usage from a bounded, complete and ordered JSONL stream.

    Usage events are deliberately fail-closed: a malformed completion, a
    duplicate turn identity, an out-of-order sequence, or an unfinished turn
    raises instead of silently undercounting tokens.
    """

    source = Path(path)
    totals = {field: 0 for field in TOKEN_FIELDS}
    turns = 0
    commands_started = 0
    commands_completed = 0
    commands_failed = 0
    agent_messages = 0
    malformed = 0
    malformed_usage_records = 0
    if max_bytes <= 0 or max_line_bytes <= 0:
        raise ValueError("JSONL size limits must be positive")
    try:
        size = source.stat().st_size
        if size > max_bytes:
            raise ValueError(f"JSONL input exceeds {max_bytes} bytes: {source}")
        handle = source.open("rb")
    except OSError as exc:
        raise ValueError(f"Cannot read {source}: {exc}") from exc

    open_turns: set[str] = set()
    anonymous_starts = 0
    completed_ids: set[str] = set()
    seen_sequences: set[int] = set()
    last_sequence: int | None = None
    records_seen = 0
    bytes_seen = 0
    command_states: dict[str, str | None] = {}
    command_started: set[str] = set()
    command_completed: set[str] = set()
    message_completed: set[str] = set()
    anonymous_command_events = False

    def lifecycle_record(record: dict[str, Any]) -> None:
        nonlocal commands_started, commands_completed, commands_failed
        nonlocal agent_messages, anonymous_command_events
        event_type = record.get("type")
        if event_type not in {"item.started", "item.completed"}:
            return
        item = record.get("item")
        if not isinstance(item, dict):
            return
        item_type = item.get("type")
        if item_type not in {"command_execution", "collab_tool_call", "agent_message"}:
            return
        is_command = item_type in {"command_execution", "collab_tool_call"}
        identity = item.get("id")
        identity = str(identity) if isinstance(identity, (str, int)) and not isinstance(identity, bool) and str(identity) else None
        if event_type == "item.started":
            if is_command and identity is None:
                anonymous_command_events = True
            if is_command and identity is not None:
                if identity in command_started or identity in command_states:
                    anonymous_command_events = True
                else:
                    command_started.add(identity)
                    command_states[identity] = None
                    commands_started += 1
            return
        if item_type == "agent_message":
            if identity is None:
                agent_messages += 1
            elif identity not in message_completed:
                message_completed.add(identity)
                agent_messages += 1
            return
        if identity is None:
            anonymous_command_events = True
            commands_started += 1
        elif identity not in command_states:
            command_states[identity] = None
            command_started.add(identity)
            commands_started += 1
        status = item.get("status")
        normalized = str(status) if isinstance(status, str) else "completed"
        if normalized not in {"completed", "failed"}:
            anonymous_command_events = True
        previous = command_states.get(identity) if identity is not None else None
        if identity is not None and identity in command_completed:
            anonymous_command_events = True
            if previous is not None and previous != normalized:
                anonymous_command_events = True
            return
        if identity is not None:
            command_completed.add(identity)
        if previous is not None and previous != normalized:
            anonymous_command_events = True
        if identity is not None:
            command_states[identity] = normalized
        if normalized == "failed":
            commands_failed += 1
        else:
            commands_completed += 1

    decoder = json.JSONDecoder()
    try:
        while True:
            raw_line = handle.readline(max_line_bytes + 1)
            if not raw_line:
                break
            bytes_seen += len(raw_line)
            if bytes_seen > max_bytes:
                raise ValueError(f"JSONL input exceeds {max_bytes} bytes while reading: {source}")
            if len(raw_line) > max_line_bytes:
                raise ValueError(f"JSONL line exceeds {max_line_bytes} bytes: {source}")
            try:
                line = raw_line.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError(f"Invalid UTF-8 in {source}") from exc
            if not line.strip():
                continue
            offset = 0
            decoded_any = False
            records_seen += 1
            if records_seen > MAX_JSONL_RECORDS:
                raise ValueError(f"JSONL input exceeds {MAX_JSONL_RECORDS} records: {source}")
            while offset < len(line):
                while offset < len(line) and line[offset].isspace():
                    offset += 1
                if offset >= len(line):
                    break
                try:
                    record, end = decoder.raw_decode(line, offset)
                except (json.JSONDecodeError, TypeError):
                    malformed += 1
                    raise ValueError(f"Malformed or truncated JSON record in {source}")
                first_record = not decoded_any
                decoded_any = True
                if not first_record:
                    records_seen += 1
                    if records_seen > MAX_JSONL_RECORDS:
                        raise ValueError(f"JSONL input exceeds {MAX_JSONL_RECORDS} records: {source}")
                offset = end
                if not isinstance(record, dict):
                    malformed += 1
                    raise ValueError(f"Malformed JSON record in {source}")
                lifecycle_record(record)
                sequence = _sequence_value(record)
                if sequence is not None:
                    if sequence in seen_sequences or (last_sequence is not None and sequence <= last_sequence):
                        raise ValueError(f"Out-of-order or duplicate event sequence in {source}")
                    seen_sequences.add(sequence)
                    last_sequence = sequence
                event_type = record.get("type")
                if event_type == "turn.started":
                    identity = _turn_identity(record)
                    if identity is None:
                        anonymous_starts += 1
                    elif identity in open_turns:
                        raise ValueError(f"Duplicate turn.started event in {source}: {identity}")
                    else:
                        open_turns.add(identity)
                    continue
                if event_type != "turn.completed":
                    continue
                try:
                    usage = record.get("usage")
                    if not isinstance(usage, dict):
                        malformed += 1
                        malformed_usage_records += 1
                        raise ValueError("turn.completed event has no usage object")
                    values = {field: _token_value(usage, field) for field in TOKEN_FIELDS}
                    if values["cached_input_tokens"] > values["input_tokens"]:
                        raise ValueError("cached input exceeds input")
                    turn_identity = _turn_identity(record)
                    identity = _record_identity(record)
                    if identity is not None:
                        if identity in completed_ids:
                            raise ValueError(f"duplicate turn.completed event: {identity}")
                        completed_ids.add(identity)
                    if turn_identity is not None:
                        open_turns.discard(turn_identity)
                    elif anonymous_starts:
                        anonymous_starts -= 1
                    for field, value in values.items():
                        totals[field] += value
                    turns += 1
                except ValueError as exc:
                    malformed += 1
                    malformed_usage_records += 1
                    raise ValueError(f"Malformed usage event in {source}: {exc}") from exc
            if not decoded_any and line.strip():
                raise ValueError(f"Malformed or truncated JSON record in {source}")
    finally:
        handle.close()

    if malformed_usage_records:
        raise ValueError(f"Malformed usage event found in {source}; refusing to undercount tokens")
    incomplete_commands = any(status is None for status in command_states.values())
    if release_gate and (anonymous_command_events or incomplete_commands):
        raise ValueError(f"Command lifecycle identities are incomplete or ambiguous in {source}")
    if open_turns or anonymous_starts:
        raise ValueError(f"Incomplete turn in {source}; refusing to undercount tokens")
    if not turns:
        raise ValueError(f"No usage events found in {source}")
    return UsageSummary(
        **totals,
        turns=turns,
        commands_started=commands_started,
        commands_completed=commands_completed,
        commands_failed=commands_failed,
        agent_messages=agent_messages,
        malformed_records=malformed,
        commands_untrusted=anonymous_command_events or incomplete_commands,
        path=source.name,
    )


def parse_rollout_usage_file(
    path: str | Path,
    *,
    expected_thread_id: str | None = None,
    expected_root_session_id: str | None = None,
    max_bytes: int = MAX_JSONL_BYTES,
    max_line_bytes: int = MAX_JSONL_LINE_BYTES,
    release_gate: bool = False,
) -> UsageSummary:
    """Measure one authoritative desktop rollout without trusting snapshots blindly.

    Desktop subagents emit cumulative ``thread_token_usage`` snapshots rather
    than the top-level ``turn.completed`` records produced by ``codex exec``.
    This parser sums the per-response usage records and requires every
    cumulative snapshot to agree, preventing both omission and double-counting.
    """

    source = Path(path)
    if max_bytes <= 0 or max_line_bytes <= 0:
        raise ValueError("JSONL size limits must be positive")
    try:
        if source.stat().st_size > max_bytes:
            raise ValueError(f"JSONL input exceeds {max_bytes} bytes: {source}")
        handle = source.open("rb")
    except OSError as exc:
        raise ValueError(f"Cannot read {source}: {exc}") from exc

    totals = {field: 0 for field in TOKEN_FIELDS}
    responses: set[str] = set()
    command_ids: set[str] = set()
    commands_failed = 0
    agent_messages = 0
    records_seen = 0
    last_ordinal: int | None = None
    session_verified = expected_thread_id is None and expected_root_session_id is None
    session_count = 0
    actual_thread_id: str | None = None
    task_completed = False
    final_message_present = False
    final_token_snapshot: dict[str, int] | None = None

    try:
        for raw_line in handle:
            records_seen += 1
            if records_seen > MAX_JSONL_RECORDS:
                raise ValueError(f"JSONL input exceeds {MAX_JSONL_RECORDS} records: {source}")
            if len(raw_line) > max_line_bytes:
                raise ValueError(f"JSONL line exceeds {max_line_bytes} bytes: {source}")
            if not raw_line.strip():
                continue
            try:
                record = json.loads(
                    raw_line.decode("utf-8"), object_pairs_hook=_strict_json_object
                )
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                raise ValueError(f"Malformed or truncated JSON record in {source}") from exc
            if not isinstance(record, dict):
                raise ValueError(f"Malformed JSON record in {source}")
            ordinal = record.get("ordinal")
            if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0:
                raise ValueError(f"Invalid rollout ordinal in {source}")
            if last_ordinal is not None and ordinal <= last_ordinal:
                raise ValueError(f"Out-of-order or duplicate rollout ordinal in {source}")
            last_ordinal = ordinal
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            if record.get("type") == "session_meta":
                session_count += 1
                if session_count != 1:
                    raise ValueError("rollout contains duplicate session metadata")
                thread_id = payload.get("id")
                root_id = payload.get("session_id")
                if not isinstance(thread_id, str) or not thread_id:
                    raise ValueError("rollout session identity is missing")
                actual_thread_id = thread_id
                if expected_thread_id is not None and thread_id != expected_thread_id:
                    raise ValueError("worker rollout thread identity does not match the verified handoff")
                if expected_root_session_id is not None and root_id != expected_root_session_id:
                    raise ValueError("worker rollout is not bound to the benchmark root session")
                if expected_root_session_id is not None:
                    is_root = thread_id == expected_root_session_id
                    source_info = payload.get("source")
                    if is_root and source_info != "exec":
                        raise ValueError("root rollout does not have the expected exec source")
                    if not is_root:
                        spawn = (
                            source_info.get("subagent", {}).get("thread_spawn", {})
                            if isinstance(source_info, dict) else {}
                        )
                        if (
                            spawn.get("parent_thread_id") != expected_root_session_id
                            or spawn.get("agent_role") != "bulk_reader"
                        ):
                            raise ValueError("worker rollout parent or role is not bound to the handoff")
                session_verified = True
                continue
            if record.get("type") == "token_usage_record":
                if actual_thread_id is None or payload.get("thread_id") != actual_thread_id:
                    raise ValueError("worker usage belongs to a different thread")
                if expected_root_session_id is not None and payload.get("session_id") != expected_root_session_id:
                    raise ValueError("worker usage belongs to a different root session")
                response_id = payload.get("response_id")
                if not isinstance(response_id, str) or not response_id or response_id in responses:
                    raise ValueError("worker usage has a missing or duplicate response identity")
                responses.add(response_id)
                usage = payload.get("usage")
                cumulative = payload.get("thread_token_usage")
                if not isinstance(usage, dict) or not isinstance(cumulative, dict):
                    raise ValueError("worker usage record is incomplete")
                values = {field: _token_value(usage, field) for field in TOKEN_FIELDS}
                if values["cached_input_tokens"] > values["input_tokens"]:
                    raise ValueError("worker cached input exceeds input")
                if usage.get("total_tokens") != values["input_tokens"] + values["output_tokens"]:
                    raise ValueError("worker usage total is inconsistent")
                for field, value in values.items():
                    totals[field] += value
                cumulative_values = {field: _token_value(cumulative, field) for field in TOKEN_FIELDS}
                if cumulative_values != totals:
                    raise ValueError("worker cumulative usage does not match response usage")
                if cumulative.get("total_tokens") != totals["input_tokens"] + totals["output_tokens"]:
                    raise ValueError("worker cumulative total is inconsistent")
                continue
            if record.get("type") == "event_msg" and payload.get("type") == "token_count":
                info = payload.get("info")
                snapshot = info.get("total_token_usage") if isinstance(info, dict) else None
                if not isinstance(snapshot, dict):
                    raise ValueError("rollout token snapshot is malformed")
                final_token_snapshot = {field: _token_value(snapshot, field) for field in TOKEN_FIELDS}
                if snapshot.get("total_tokens") != (
                    final_token_snapshot["input_tokens"] + final_token_snapshot["output_tokens"]
                ):
                    raise ValueError("rollout token snapshot total is inconsistent")
                continue
            if record.get("type") == "event_msg" and payload.get("type") == "task_complete":
                if task_completed:
                    raise ValueError("rollout contains duplicate task completion")
                task_completed = True
                final = payload.get("last_agent_message")
                final_message_present = isinstance(final, str) and bool(final.strip())
                continue
            if record.get("type") != "event_msg" or payload.get("type") != "item_completed":
                continue
            item = payload.get("item")
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type", "")).lower()
            if item_type == "agentmessage":
                agent_messages += 1
            if item_type != "commandexecution":
                continue
            identity = item.get("id")
            if not isinstance(identity, str) or not identity or identity in command_ids:
                raise ValueError("worker command lifecycle identity is missing or duplicated")
            command_ids.add(identity)
            status = item.get("status", "completed")
            if status not in {"completed", "failed"}:
                raise ValueError("worker command lifecycle status is invalid")
            commands_failed += int(status == "failed")
    finally:
        handle.close()

    if not session_verified:
        raise ValueError("worker rollout has no verified session metadata")
    if session_count != 1 or actual_thread_id is None:
        raise ValueError("rollout does not contain exactly one session identity")
    if not responses:
        raise ValueError(f"No usage events found in {source}")
    if final_token_snapshot is not None and final_token_snapshot != totals:
        raise ValueError("final rollout token snapshot disagrees with response usage")
    if release_gate and final_token_snapshot is None:
        raise ValueError("rollout has no final cumulative token snapshot")
    if release_gate and (not task_completed or not final_message_present):
        raise ValueError("rollout has no unique nonempty task completion")
    if release_gate and actual_thread_id != expected_root_session_id and not command_ids:
        raise ValueError("worker rollout has no attributable command lifecycle")
    return UsageSummary(
        **totals,
        turns=len(responses),
        commands_started=len(command_ids),
        commands_completed=len(command_ids) - commands_failed,
        commands_failed=commands_failed,
        agent_messages=agent_messages,
        path=source.name,
    )


def combine_usage(*summaries: UsageSummary, path: str = "") -> UsageSummary:
    """Add disjoint root/worker usage summaries into one measured run."""

    return UsageSummary(
        **{field: sum(getattr(summary, field) for summary in summaries) for field in TOKEN_FIELDS},
        turns=sum(summary.turns for summary in summaries),
        commands_started=sum(summary.commands_started for summary in summaries),
        commands_completed=sum(summary.commands_completed for summary in summaries),
        commands_failed=sum(summary.commands_failed for summary in summaries),
        agent_messages=sum(summary.agent_messages for summary in summaries),
        malformed_records=sum(summary.malformed_records for summary in summaries),
        commands_untrusted=any(summary.commands_untrusted for summary in summaries),
        path=path,
    )


def _metric(baseline: int, baron: int) -> dict[str, int | float | None]:
    saved = baseline - baron
    return {
        "baseline": baseline,
        "baron": baron,
        "difference": saved,
        "savings_percent": (saved / baseline * 100) if baseline else None,
    }


def build_comparison(baseline: UsageSummary, baron: UsageSummary) -> dict[str, Any]:
    """Build a serializable comparison; positive differences mean tokens saved."""

    difference = baseline.total_tokens - baron.total_tokens
    status = "saved" if difference > 0 else "exceeded" if difference < 0 else "unchanged"
    fields = (
        "input_tokens",
        "cached_input_tokens",
        "uncached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "total_tokens",
        "commands_started",
        "commands_failed",
    )
    return {
        "status": status,
        "baseline_total_tokens": baseline.total_tokens,
        "baron_total_tokens": baron.total_tokens,
        "difference_tokens": difference,
        "savings_percent": (difference / baseline.total_tokens * 100) if baseline.total_tokens else None,
        "baseline": baseline.to_dict(),
        "baron": baron.to_dict(),
        "metrics": {
            field: _metric(int(getattr(baseline, field)), int(getattr(baron, field)))
            for field in fields
        },
    }


def _bar(value: int, maximum: int, width: int) -> str:
    filled = round(width * value / maximum) if maximum else 0
    return f"[{'#' * filled}{'.' * (width - filled)}]"


def render_meter(comparison: dict[str, Any], width: int = 30) -> str:
    """Render the paired result as a compact terminal meter."""

    baseline = comparison["baseline"]
    baron = comparison["baron"]
    baseline_total = int(comparison["baseline_total_tokens"])
    baron_total = int(comparison["baron_total_tokens"])
    maximum = max(baseline_total, baron_total)
    percent = comparison["savings_percent"]
    percent_text = "n/a" if percent is None else f"{abs(percent):.1f}%"
    difference = abs(int(comparison["difference_tokens"]))
    status = str(comparison["status"])
    result = "UNCHANGED" if status == "unchanged" else f"{status.upper()} {difference:,} tokens ({percent_text})"

    return "\n".join((
        "Codex Baron token meter",
        f"  Without Baron {_bar(baseline_total, maximum, width)} {baseline_total:,}",
        f"  With Baron    {_bar(baron_total, maximum, width)} {baron_total:,}",
        "",
        f"  Input             {int(baseline['input_tokens']):>12,} -> {int(baron['input_tokens']):>12,}",
        f"  Cached input      {int(baseline['cached_input_tokens']):>12,} -> {int(baron['cached_input_tokens']):>12,}",
        f"  Uncached input    {int(baseline['uncached_input_tokens']):>12,} -> {int(baron['uncached_input_tokens']):>12,}",
        f"  Output            {int(baseline['output_tokens']):>12,} -> {int(baron['output_tokens']):>12,}",
        f"  Reasoning output* {int(baseline['reasoning_output_tokens']):>12,} -> {int(baron['reasoning_output_tokens']):>12,}",
        f"  Commands started  {int(baseline['commands_started']):>12,} -> {int(baron['commands_started']):>12,}",
        f"  Command failures  {int(baseline['commands_failed']):>12,} -> {int(baron['commands_failed']):>12,}",
        "",
        f"  Result: {result}",
        "  * Reasoning output is already included in output and is not counted twice.",
    ))


# Descriptive aliases retained for callers using the initial API names.
aggregate_usage = parse_usage_file
compare_usage = build_comparison


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Show tokens saved or exceeded by a paired Baron run.",
    )
    parser.add_argument("baseline", metavar="BASELINE", help="Codex JSONL produced without Baron")
    parser.add_argument("baron", metavar="BARON", help="Codex JSONL produced with Baron")
    parser.add_argument("--json", action="store_true", dest="as_json", help="emit machine-readable JSON")
    parser.add_argument(
        "--require-savings",
        action="store_true",
        help="exit nonzero unless Baron uses fewer total tokens",
    )
    parser.add_argument(
        "--max-command-increase",
        type=int,
        metavar="N",
        help="exit nonzero when Baron starts more than N additional commands",
    )
    args = parser.parse_args(argv)
    release_gate = args.require_savings or args.max_command_increase is not None
    try:
        comparison = build_comparison(
            parse_usage_file(args.baseline, release_gate=release_gate),
            parse_usage_file(args.baron, release_gate=release_gate),
        )
    except ValueError as exc:
        parser.error(str(exc))
    if args.as_json:
        print(json.dumps(comparison, indent=2, sort_keys=True))
    else:
        print(render_meter(comparison))
    failures: list[str] = []
    if args.require_savings and comparison["difference_tokens"] <= 0:
        failures.append("Baron did not save total tokens")
    if args.max_command_increase is not None:
        command_increase = (
            comparison["baron"]["commands_started"]
            - comparison["baseline"]["commands_started"]
        )
        if command_increase > args.max_command_increase:
            failures.append(
                f"Baron started {command_increase} additional commands "
                f"(limit {args.max_command_increase})"
            )
    if failures:
        print("Release gate failed: " + "; ".join(failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
