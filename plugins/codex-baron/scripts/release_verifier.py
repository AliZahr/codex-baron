#!/usr/bin/env python3
"""Fail-closed verification of benchmark quality evidence.

Token efficiency is only one input to a production decision. This verifier
requires a separately authored quality attestation covering both answers,
corrections, evaluator identity, and the exact benchmark evidence.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import time

from usage_meter import build_comparison, combine_usage, parse_rollout_usage_file, parse_usage_file
from paired_benchmark import _thread_id, inspect_delegation_evidence, root_descendant_ids


MAX_JSON_BYTES = 8 * 1024 * 1024
SHA256 = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA = re.compile(r"^[0-9a-f]{7,128}$")
REQUIRED_QUALITY_HASHES = (
    "prompt_sha256", "repository_status_sha256", "baseline_jsonl_sha256",
    "baron_jsonl_sha256", "rubric_sha256",
)


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def read_json(path: str | Path) -> dict[str, object]:
    source = Path(path)
    try:
        data = source.read_bytes()
    except OSError as exc:
        raise ValueError(f"cannot read evidence file: {source.name}") from exc
    if len(data) > MAX_JSON_BYTES:
        raise ValueError(f"evidence file exceeds {MAX_JSON_BYTES} bytes: {source.name}")
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_strict_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"malformed evidence JSON: {source.name}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"evidence root must be an object: {source.name}")
    return value


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ValueError(f"cannot read artifact: {Path(path).name}") from exc
    return digest.hexdigest()


def _rollout_thread_id(path: Path) -> str:
    found: list[str] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                value = json.loads(line, object_pairs_hook=_strict_object)
                if isinstance(value, dict) and value.get("type") == "session_meta":
                    payload = value.get("payload")
                    identity = payload.get("id") if isinstance(payload, dict) else None
                    if isinstance(identity, str) and identity:
                        found.append(identity)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("cannot read rollout session identity") from exc
    if len(found) != 1:
        raise ValueError("rollout does not contain one stable session identity")
    return found[0]


def _require_hash(value: object, field: str) -> str:
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise ValueError(f"invalid {field}")
    return value


def _require_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"invalid {field}")
    return value


def _require_answer(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or value.get("passed") is not True:
        raise ValueError(f"{label} quality answer did not pass")
    corrections = value.get("corrections")
    if isinstance(corrections, bool) or not isinstance(corrections, int) or corrections < 0:
        raise ValueError(f"invalid {label} correction count")
    return value


def _require_nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"invalid {field}")
    return value


def _verify_execution_contract(comparison: dict[str, object]) -> dict[str, object]:
    plugin = comparison.get("plugin")
    runtime = comparison.get("runtime")
    repository = comparison.get("repository")
    inputs = comparison.get("inputs")
    routing = comparison.get("routing")
    if not all(isinstance(value, dict) for value in (plugin, runtime, repository, inputs, routing)):
        raise ValueError("production execution contract is incomplete")
    if plugin.get("id") != "codex-baron@codex-baron":
        raise ValueError("unexpected plugin identity")
    version = _require_string(plugin.get("version"), "plugin version")
    if runtime.get("installed_plugin_version") != version or comparison.get("plugin_version") != version:
        raise ValueError("installed plugin version does not match the benchmark build")
    source_digest = _require_hash(runtime.get("source_runtime_sha256"), "source runtime hash")
    installed_digest = _require_hash(runtime.get("installed_runtime_sha256"), "installed runtime hash")
    if source_digest != installed_digest:
        raise ValueError("installed runtime differs from the benchmark source")
    cli_version = _require_string(runtime.get("codex_cli_version"), "Codex CLI version")
    head = _require_string(repository.get("head"), "repository head")
    status_hash = _require_hash(repository.get("status_sha256"), "repository status hash")
    if repository.get("status") != "clean":
        raise ValueError("benchmark repository was not clean")
    if (
        comparison.get("repository_head") != head
        or comparison.get("repository_status") != "clean"
        or comparison.get("repository_status_sha256") != status_hash
    ):
        raise ValueError("repository aliases disagree with the execution contract")
    model = _require_string(inputs.get("model"), "benchmark model")
    reasoning = inputs.get("reasoning_effort")
    tools = inputs.get("tools")
    if inputs.get("sandbox") != "read-only":
        raise ValueError("production benchmark sandbox was not read-only")
    if not isinstance(reasoning, dict) or reasoning.get("source") != "explicit_cli" or reasoning.get("verified") is not True:
        raise ValueError("production benchmark reasoning effort was not explicit and verified")
    _require_string(reasoning.get("value"), "reasoning effort")
    if not isinstance(tools, dict) or not (
        tools.get("mode") == "same_inherited_codex_runtime"
        and tools.get("pairwise_held_constant") is True
        and tools.get("operator_accepted_unverified") is True
    ):
        raise ValueError("production benchmark tool policy was not held constant and accepted")
    if (
        comparison.get("model") != model
        or comparison.get("sandbox") != "read-only"
        or comparison.get("reasoning_effort") != reasoning
        or comparison.get("tools") != tools
    ):
        raise ValueError("top-level execution aliases disagree with benchmark inputs")
    if routing.get("expected_route") != "bulk_reader" or routing.get("delegation_required") is not True:
        raise ValueError("production benchmark did not require the bulk_reader route")
    return {
        "plugin": {"id": plugin["id"], "version": version, "runtime_sha256": installed_digest},
        "codex_cli_version": cli_version,
        "model": model,
        "reasoning_effort": reasoning["value"],
        "sandbox": "read-only",
    }


def _verify_efficiency(comparison: dict[str, object]) -> None:
    if comparison.get("efficiency_gate_eligible") is not True:
        raise ValueError("efficiency gate evidence is absent or ineligible")
    if comparison.get("efficiency_gate_passed") is not True:
        raise ValueError("efficiency gate did not pass")
    gate = comparison.get("efficiency_gate")
    measured = comparison.get("comparison")
    if not isinstance(gate, dict) or not isinstance(measured, dict):
        raise ValueError("efficiency gate details are missing")
    if gate.get("eligible") is not True or gate.get("passed") is not True:
        raise ValueError("efficiency gate detail disagrees with approval")
    baseline = measured.get("baseline")
    baron = measured.get("baron")
    if not isinstance(baseline, dict) or not isinstance(baron, dict):
        raise ValueError("efficiency measurements are missing")
    baseline_tokens = _require_nonnegative_int(baseline.get("total_tokens"), "baseline total tokens")
    baron_tokens = _require_nonnegative_int(baron.get("total_tokens"), "baron total tokens")
    difference = baseline_tokens - baron_tokens
    if difference <= 0 or measured.get("difference_tokens") != difference or measured.get("status") != "saved":
        raise ValueError("efficiency token result is inconsistent or did not save tokens")
    baseline_commands = _require_nonnegative_int(baseline.get("commands_started"), "baseline command count")
    baron_commands = _require_nonnegative_int(baron.get("commands_started"), "baron command count")
    command_increase = baron_commands - baseline_commands
    maximum = _require_nonnegative_int(gate.get("max_command_increase"), "maximum command increase")
    if gate.get("command_increase") != command_increase or command_increase > maximum:
        raise ValueError("efficiency command result is inconsistent or exceeded its limit")
    baseline_failures = _require_nonnegative_int(baseline.get("commands_failed"), "baseline command failures")
    baron_failures = _require_nonnegative_int(baron.get("commands_failed"), "baron command failures")
    failure_increase = baron_failures - baseline_failures
    failure_maximum = _require_nonnegative_int(
        gate.get("max_command_failure_increase"), "maximum command failure increase"
    )
    if failure_maximum != 0 or gate.get("command_failure_increase") != failure_increase or failure_increase > 0:
        raise ValueError("efficiency command-failure result is inconsistent or exceeded its limit")


def _verify_measured_artifacts(
    comparison_file: Path,
    declared: dict[str, object],
) -> None:
    runs = declared.get("runs")
    accounting = declared.get("accounting")
    if not isinstance(runs, dict) or not isinstance(accounting, dict):
        raise ValueError("portable run accounting evidence is missing")
    if accounting.get("mode") != "root_plus_bound_descendants":
        raise ValueError("unsupported run accounting mode")
    try:
        summaries: dict[str, object] = {}
        breakdown: dict[str, object] = {}
        for label in ("baseline", "baron"):
            run = runs.get(label)
            artifacts = run.get("artifacts") if isinstance(run, dict) else None
            if not isinstance(artifacts, dict):
                raise ValueError(f"{label} artifact inventory is missing")
            public_path = comparison_file.parent / f"{label}.jsonl"
            public = parse_usage_file(public_path, release_gate=True)
            thread_id = _thread_id(public_path)
            if thread_id is None:
                raise ValueError(f"{label} root thread identity is missing")
            root_descriptor = artifacts.get("root_rollout")
            expected_root_name = f"{label}-root-rollout.jsonl"
            if not isinstance(root_descriptor, dict) or root_descriptor.get("name") != expected_root_name:
                raise ValueError(f"{label} authoritative root rollout is missing")
            root_path = comparison_file.parent / expected_root_name
            if sha256_file(root_path) != _require_hash(
                root_descriptor.get("sha256"), f"{label} root rollout hash"
            ):
                raise ValueError(f"{label} authoritative root rollout changed after comparison")
            authoritative = parse_rollout_usage_file(
                root_path,
                expected_thread_id=thread_id,
                expected_root_session_id=thread_id,
                release_gate=True,
            )
            if any(getattr(public, field) != getattr(authoritative, field) for field in (
                "input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens"
            )):
                raise ValueError(f"{label} root usage disagrees across public and authoritative artifacts")
            observed_children = root_descendant_ids(root_path)
            descendants = artifacts.get("descendants", [])
            if not isinstance(descendants, list):
                raise ValueError(f"{label} descendant artifact inventory is malformed")
            descendant_summaries = []
            declared_child_ids: set[str] = set()
            seen_names: set[str] = set()
            for index, descriptor in enumerate(descendants, 1):
                expected_name = f"{label}-worker-{index}.jsonl"
                if not isinstance(descriptor, dict) or descriptor.get("name") != expected_name:
                    raise ValueError(f"{label} descendant artifact name is not canonical")
                if expected_name in seen_names:
                    raise ValueError(f"{label} descendant artifact is duplicated")
                seen_names.add(expected_name)
                descendant_path = comparison_file.parent / expected_name
                if sha256_file(descendant_path) != _require_hash(
                    descriptor.get("sha256"), f"{label} descendant rollout hash"
                ):
                    raise ValueError(f"{label} descendant rollout changed after comparison")
                descendant_id = _rollout_thread_id(descendant_path)
                declared_child_ids.add(descendant_id)
                if label == "baron" and index == 1:
                    receipt = read_json(comparison_file.parent / "baron-routing-state.json")
                    expected_worker_hash = _require_hash(
                        receipt.get("worker_session_sha256"), "worker session hash"
                    )
                    if hashlib.sha256(descendant_id.encode()).hexdigest() != expected_worker_hash:
                        raise ValueError("descendant rollout does not match the verified worker identity")
                    receipt_artifact = receipt.get("worker_artifact")
                    if not isinstance(receipt_artifact, dict) or (
                        receipt_artifact.get("name") != expected_name
                        or receipt_artifact.get("sha256") != descriptor.get("sha256")
                    ):
                        raise ValueError("worker receipt artifact binding is inconsistent")
                descendant_summaries.append(parse_rollout_usage_file(
                    descendant_path,
                    expected_thread_id=descendant_id,
                    expected_root_session_id=thread_id,
                    release_gate=True,
                ))
            if observed_children != declared_child_ids:
                raise ValueError(f"{label} root descendant tree does not match its artifact inventory")
            aggregate = combine_usage(public, *descendant_summaries, path=f"{label}+workers")
            summaries[label] = aggregate
            breakdown[label] = {
                "root": public.to_dict(),
                "descendants": [summary.to_dict() for summary in descendant_summaries],
                "aggregate": aggregate.to_dict(),
            }
        if accounting.get("baseline") != breakdown["baseline"] or accounting.get("baron") != breakdown["baron"]:
            raise ValueError("declared accounting breakdown does not match portable artifacts")
        recomputed = build_comparison(summaries["baseline"], summaries["baron"])
    except (OSError, ValueError) as exc:
        raise ValueError("benchmark JSONL artifacts cannot be independently verified") from exc
    if declared.get("comparison") != recomputed:
        raise ValueError("comparison metrics do not match the benchmark JSONL artifacts")


def verify_release(
    comparison_path: str | Path,
    quality_path: str | Path,
    rubric_path: str | Path,
    *,
    output_path: str | Path | None = None,
) -> dict[str, object]:
    """Verify and write a privacy-safe production approval attestation."""

    comparison_file = Path(comparison_path)
    quality_file = Path(quality_path)
    rubric_file = Path(rubric_path)
    comparison = read_json(comparison_file)
    quality = read_json(quality_file)
    if comparison.get("schema_version") != 3:
        raise ValueError("unsupported or missing comparison schema")
    if quality.get("schema_version") != 1:
        raise ValueError("unsupported or missing quality attestation schema")
    execution = _verify_execution_contract(comparison)
    _verify_efficiency(comparison)
    if comparison.get("plugin_toggle_verified") is not True:
        raise ValueError("plugin toggle evidence is absent or unverifiable")
    hooks = comparison.get("hooks")
    if not isinstance(hooks, dict) or hooks.get("configured_enabled") is not True:
        raise ValueError("Baron hooks were not verified enabled for the benchmark")
    if hooks.get("verified_events") != ["pre_tool_use", "user_prompt_submit", "subagent_start", "subagent_stop"]:
        raise ValueError("Baron hook attestation is incomplete")
    agents = comparison.get("agents")
    if not isinstance(agents, dict) or agents.get("required") is not True or agents.get("verified") is not True:
        raise ValueError("Baron bulk_reader agent profile was not verified for the benchmark")
    _require_hash(agents.get("sha256"), "Baron bulk_reader agent profile hash")
    if agents.get("source") not in {"project", "user"} or agents.get("name") != "bulk_reader":
        raise ValueError("Baron bulk_reader agent profile attestation is malformed")
    _verify_measured_artifacts(comparison_file, comparison)
    routing = comparison.get("routing")
    if not isinstance(routing, dict) or routing.get("delegation_required") is not True:
        raise ValueError("production evidence must exercise a required Baron delegation route")
    baron_run = comparison.get("runs", {}).get("baron") if isinstance(comparison.get("runs"), dict) else None
    baron_artifacts = baron_run.get("artifacts") if isinstance(baron_run, dict) else None
    state_descriptor = baron_artifacts.get("routing_state") if isinstance(baron_artifacts, dict) else None
    state_path: Path | None = None
    if isinstance(state_descriptor, dict):
        if state_descriptor.get("name") != "baron-routing-state.json":
            raise ValueError("Baron routing-state artifact name is not canonical")
        declared_state_hash = _require_hash(state_descriptor.get("sha256"), "Baron routing-state artifact hash")
        state_path = comparison_file.parent / "baron-routing-state.json"
        if sha256_file(state_path) != declared_state_hash:
            raise ValueError("Baron routing-state artifact changed after comparison")
    declared_delegation = routing.get("delegation")
    measured_delegation = inspect_delegation_evidence(
        comparison_file.parent / "baron.jsonl", state_path
    )
    if declared_delegation != measured_delegation or measured_delegation.get("verified") is not True:
        raise ValueError("Baron delegation evidence is absent, invalid, or inconsistent")

    prompt_hash = _require_hash(comparison.get("prompt_sha256"), "comparison prompt hash")
    repository_head = comparison.get("repository_head")
    if not isinstance(repository_head, str) or not GIT_SHA.fullmatch(repository_head):
        raise ValueError("invalid comparison repository head")
    status_hash = _require_hash(comparison.get("repository_status_sha256"), "comparison status hash")
    runs = comparison.get("runs")
    if not isinstance(runs, dict):
        raise ValueError("comparison runs are missing")
    artifacts: dict[str, str] = {}
    for label in ("baseline", "baron"):
        run = runs.get(label)
        if not isinstance(run, dict):
            raise ValueError(f"comparison {label} run is missing")
        run_artifacts = run.get("artifacts")
        if not isinstance(run_artifacts, dict) or not isinstance(run_artifacts.get("jsonl"), dict):
            raise ValueError(f"comparison {label} JSONL artifact is missing")
        descriptor = run_artifacts["jsonl"]
        name = descriptor.get("name")
        if name != f"{label}.jsonl":
            raise ValueError(f"comparison {label} artifact name is not canonical")
        artifacts[label] = _require_hash(descriptor.get("sha256"), f"comparison {label} artifact hash")

    quality_hashes = {field: _require_hash(quality.get(field), field) for field in REQUIRED_QUALITY_HASHES}
    quality_head = quality.get("repository_head")
    if quality_head != repository_head:
        raise ValueError("quality attestation repository head is stale or mismatched")
    if quality_hashes["prompt_sha256"] != prompt_hash or quality_hashes["repository_status_sha256"] != status_hash:
        raise ValueError("quality attestation is not bound to this comparison")
    if quality_hashes["baseline_jsonl_sha256"] != artifacts["baseline"] or quality_hashes["baron_jsonl_sha256"] != artifacts["baron"]:
        raise ValueError("quality attestation artifact hashes do not match comparison")
    actual_comparison_hash = sha256_file(comparison_file)
    if quality.get("comparison_sha256") != actual_comparison_hash:
        raise ValueError("quality attestation comparison hash is stale or mismatched")
    actual_rubric_hash = sha256_file(rubric_file)
    if quality_hashes["rubric_sha256"] != actual_rubric_hash:
        raise ValueError("quality attestation rubric hash is stale or mismatched")
    for label in ("baseline", "baron"):
        actual = sha256_file(comparison_file.parent / f"{label}.jsonl")
        if actual != artifacts[label]:
            raise ValueError(f"{label} JSONL artifact changed after comparison")

    evaluator = quality.get("evaluator")
    if not isinstance(evaluator, dict):
        raise ValueError("evaluator identity is missing")
    evaluator_id = _require_string(evaluator.get("id"), "evaluator id")
    evaluator_version = _require_string(evaluator.get("version"), "evaluator version")
    baseline_answer = _require_answer(quality.get("baseline"), "baseline")
    baron_answer = _require_answer(quality.get("baron"), "baron")
    result: dict[str, object] = {
        "schema_version": 1,
        "status": "production_approved",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "comparison_sha256": actual_comparison_hash,
        "quality_attestation_sha256": sha256_file(quality_file),
        "rubric_sha256": actual_rubric_hash,
        "prompt_sha256": prompt_hash,
        "repository_head": repository_head,
        "repository_status_sha256": status_hash,
        "baseline_jsonl_sha256": artifacts["baseline"],
        "baron_jsonl_sha256": artifacts["baron"],
        "execution": execution,
        "evaluator": {"id": evaluator_id, "version": evaluator_version},
        "answers": {
            "baseline": {"passed": True, "corrections": baseline_answer["corrections"]},
            "baron": {"passed": True, "corrections": baron_answer["corrections"]},
        },
    }
    destination = Path(output_path) if output_path is not None else comparison_file.parent / "production_release.json"
    temporary = destination.with_name(f".{destination.name}.{time.time_ns()}.tmp")
    temporary.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(destination)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("comparison", type=Path)
    parser.add_argument("quality", type=Path)
    parser.add_argument("rubric", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        verify_release(args.comparison, args.quality, args.rubric, output_path=args.output)
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": {"code": "release_evidence_invalid", "message": str(exc)}}, sort_keys=True), file=sys.stderr)
        return 2
    print("Production evidence verified; production_release.json written.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
