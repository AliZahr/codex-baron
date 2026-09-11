"""Black-box regression tests for production-facing Baron behavior.

These tests deliberately use temporary files/processes and only the Python
standard library.  The manifest test documents the paired runner contract that
an incompatible plugin manifest is rejected before a pair runs.
"""

from __future__ import annotations

import hashlib
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
import paired_benchmark  # noqa: E402
import router_hook  # noqa: E402
from usage_meter import UsageSummary, build_comparison, parse_usage_file  # noqa: E402


def usage(**values: int) -> dict[str, object]:
    result: dict[str, object] = {"input_tokens": 0, "output_tokens": 0}
    result.update(values)
    return {"type": "turn.completed", "usage": result}


def write_jsonl(path: Path, *records: object) -> None:
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")


def run_cli(script: Path, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
    )


class UsageMeterHardeningTests(unittest.TestCase):
    def test_partial_usage_record_fails_closed_even_after_valid_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "partial.jsonl"
            path.write_text(
                json.dumps(usage(input_tokens=8, output_tokens=2)) + "\n"
                + '{"type":"turn.completed","usage":{"input_tokens":9',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "Malformed (usage|or truncated)"):
                parse_usage_file(path)

    def test_unrelated_malformed_json_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "mixed.jsonl"
            path.write_text(
                json.dumps(usage(input_tokens=8, output_tokens=2)) + "\n"
                + "not-json\n"
                + '{"type":"future.event","payload":{"opaque":"value"}}\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "Malformed or truncated"):
                parse_usage_file(path)

    def test_duplicate_and_out_of_order_lifecycle_events_are_not_double_counted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "lifecycle.jsonl"
            records = [
                {"type": "item.completed", "item": {"id": "cmd-1", "type": "command_execution", "status": "completed"}},
                {"type": "item.started", "item": {"id": "cmd-1", "type": "command_execution", "status": "in_progress"}},
                {"type": "item.completed", "item": {"id": "cmd-1", "type": "command_execution", "status": "completed"}},
                {"type": "item.completed", "item": {"id": "cmd-2", "type": "command_execution", "status": "failed"}},
                usage(input_tokens=4, output_tokens=1),
            ]
            write_jsonl(path, *records)
            result = parse_usage_file(path)
            self.assertEqual(result.commands_started, 2)
            self.assertEqual(result.commands_completed, 1)
            self.assertEqual(result.commands_failed, 1)

    def test_missing_and_zero_usage_fields_are_zero_but_null_is_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            accepted = Path(temporary) / "accepted.jsonl"
            write_jsonl(
                accepted,
                {"type": "turn.completed", "usage": {"input_tokens": 3, "output_tokens": 0}},
                {"type": "turn.completed", "usage": {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0}},
            )
            result = parse_usage_file(accepted)
            self.assertEqual(result.turns, 2)
            self.assertEqual(result.total_tokens, 3)

            rejected = Path(temporary) / "null.jsonl"
            write_jsonl(rejected, {"type": "turn.completed", "usage": {"input_tokens": None, "output_tokens": 2}})
            with self.assertRaisesRegex(ValueError, "Malformed usage"):
                parse_usage_file(rejected)

            missing = Path(temporary) / "missing-usage.jsonl"
            write_jsonl(missing, {"type": "turn.completed"})
            with self.assertRaisesRegex(ValueError, "Malformed usage"):
                parse_usage_file(missing)

    def test_unknown_records_and_extra_fields_are_schema_forward_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "evolved.jsonl"
            write_jsonl(
                path,
                {"type": "session.started", "schema_version": 99, "new_field": {"x": True}},
                {"type": "turn.completed", "usage": {"input_tokens": 7, "output_tokens": 1, "future_tokens": 123}, "new_field": "ignored"},
                {"type": "item.unknown", "item": {"type": "new_item", "raw": "opaque"}},
            )
            result = parse_usage_file(path)
            self.assertEqual(result.total_tokens, 8)
            self.assertEqual(result.turns, 1)

    def test_oversized_line_and_file_are_rejected_with_actionable_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            oversized_line = root / "oversized-line.jsonl"
            oversized_line.write_text(
                json.dumps(usage(input_tokens=1, output_tokens=1)) + ("x" * 1024) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "(line|size|limit|large)"):
                parse_usage_file(oversized_line, max_line_bytes=128)

            oversized_file = root / "oversized-file.jsonl"
            oversized_file.write_text(json.dumps(usage(input_tokens=1, output_tokens=1)) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "(file|size|limit|large)"):
                parse_usage_file(oversized_file, max_bytes=8)

    def test_comparison_metadata_contains_no_raw_prompt_or_tool_output(self) -> None:
        prompt = "PRIVATE_PROMPT_7a1f: inspect /private/repo and print TOOL_OUTPUT_9b2e"
        comparison = build_comparison(
            UsageSummary(input_tokens=20, output_tokens=10),
            UsageSummary(input_tokens=10, output_tokens=5),
        )
        metadata = json.dumps(comparison, sort_keys=True)
        self.assertNotIn(prompt, metadata)
        self.assertNotIn("TOOL_OUTPUT_9b2e", metadata)
        self.assertNotIn("/private/repo", metadata)

    def test_runner_comparison_manifest_is_privacy_safe(self) -> None:
        prompt = "PRIVATE_PROMPT_2c4d inspect /private/repo"
        tool_output = "TOOL_OUTPUT_8e0f secret command output"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output_dir = root / "artifacts"
            baseline_jsonl = root / "baseline.jsonl"
            baron_jsonl = root / "baron.jsonl"
            baseline_stderr = root / "baseline.stderr"
            baron_stderr = root / "baron.stderr"
            for path in (baseline_jsonl, baron_jsonl):
                path.write_text(json.dumps(usage(input_tokens=20, output_tokens=10)) + "\n", encoding="utf-8")
            baseline_stderr.write_text(tool_output, encoding="utf-8")
            baron_stderr.write_text(tool_output, encoding="utf-8")
            records = {
                "baseline": paired_benchmark.RunRecord("baseline", 0.1, str(baseline_jsonl), str(baseline_stderr)),
                "baron": paired_benchmark.RunRecord("baron", 0.1, str(baron_jsonl), str(baron_stderr)),
            }

            def fake_run(label: str, *args: object, **kwargs: object) -> paired_benchmark.RunRecord:
                return records[label]

            with patch.object(paired_benchmark, "require_clean_repository", return_value="head"), \
                    patch.object(paired_benchmark, "repository_status_sha256", return_value="hash"), \
                    patch.object(paired_benchmark, "verify_plugin_toggle"), \
                    patch.object(paired_benchmark, "verify_hooks_enabled", return_value={
                        "configured_enabled": True,
                        "verified_events": list(paired_benchmark.REQUIRED_HOOK_EVENTS),
                    }), \
                    patch.object(paired_benchmark, "runtime_attestation", return_value={
                        "installed_plugin_version": paired_benchmark.plugin_version(),
                        "source_runtime_sha256": "digest",
                        "installed_runtime_sha256": "digest",
                        "codex_cli_version": "codex-test",
                    }), \
                    patch.object(paired_benchmark, "attested_skill_path", return_value=(
                        ROOT / "skills" / "codex-baron" / "SKILL.md"
                    )), \
                    patch.object(paired_benchmark, "run_one", side_effect=fake_run), \
                    patch.object(paired_benchmark, "parse_usage_file", return_value=UsageSummary(input_tokens=20, output_tokens=10)), \
                    patch.object(paired_benchmark, "capture_root_rollout", side_effect=lambda *a: output_dir / f"{a[2]}-root-rollout.jsonl"), \
                    patch.object(paired_benchmark, "parse_rollout_usage_file", return_value=UsageSummary(input_tokens=20, output_tokens=10)), \
                    patch.object(paired_benchmark, "_thread_id", return_value="root"), \
                    patch.object(paired_benchmark, "_inspect_jsonl_delegation_evidence", return_value={"completed_spawns": 0}), \
                    patch.object(paired_benchmark, "root_descendant_ids", return_value=set()), \
                    patch.object(paired_benchmark, "inspect_delegation_evidence", return_value={"verified": False}), \
                    patch.object(paired_benchmark, "artifact_descriptor", return_value={"sha256": "artifact"}):
                result = paired_benchmark.main([
                    str(ROOT), "--prompt", prompt, "--output-dir", str(output_dir), "--max-command-increase", "0",
                ])
            self.assertEqual(result, 1)
            metadata = (output_dir / "comparison.json").read_text(encoding="utf-8")
            self.assertNotIn(prompt, metadata)
            self.assertNotIn(tool_output, metadata)
            self.assertNotIn("/private/repo", metadata)
            self.assertIn(hashlib.sha256(prompt.encode()).hexdigest(), metadata)

    def test_artifact_hash_is_reproducible_and_not_raw_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "artifact.jsonl"
            secret = "same deterministic prompt and TOOL_OUTPUT"
            path.write_text(secret, encoding="utf-8")
            first = paired_benchmark.artifact_descriptor(path, "codex_jsonl")
            second = paired_benchmark.artifact_descriptor(path, "codex_jsonl")
            self.assertEqual(first["sha256"], second["sha256"])
            self.assertNotIn(secret, first["sha256"])

    def test_failed_command_then_retry_counts_failure_and_success_separately(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "retry.jsonl"
            write_jsonl(
                path,
                {"type": "item.started", "item": {"id": "try-1", "type": "command_execution"}},
                {"type": "item.completed", "item": {"id": "try-1", "type": "command_execution", "status": "failed"}},
                {"type": "item.started", "item": {"id": "try-2", "type": "command_execution"}},
                {"type": "item.completed", "item": {"id": "try-2", "type": "command_execution", "status": "completed"}},
                usage(input_tokens=2, output_tokens=1),
            )
            result = parse_usage_file(path)
            self.assertEqual(result.commands_started, 2)
            self.assertEqual(result.commands_failed, 1)
            self.assertEqual(result.commands_completed, 1)

    def test_usage_cli_exit_codes_and_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = root / "baseline.jsonl"
            baron = root / "baron.jsonl"
            write_jsonl(baseline, usage(input_tokens=10, output_tokens=4))
            write_jsonl(baron, usage(input_tokens=10, output_tokens=4))
            gated = run_cli(SCRIPTS / "usage_meter.py", str(baseline), str(baron), "--require-savings")
            self.assertEqual(gated.returncode, 1)
            self.assertIn("Release gate failed", gated.stderr)

            missing = run_cli(SCRIPTS / "usage_meter.py", str(root / "missing"), str(baron))
            self.assertEqual(missing.returncode, 2)
            self.assertRegex(missing.stderr, "(Cannot read|No usage|usage_meter)")

            malformed = root / "malformed.jsonl"
            malformed.write_text('{"type":"turn.completed","usage":{"input_tokens":1\n', encoding="utf-8")
            rejected = run_cli(SCRIPTS / "usage_meter.py", str(malformed), str(baron))
            self.assertEqual(rejected.returncode, 2)
            self.assertRegex(rejected.stderr, "Malformed (usage|or truncated)")


class HookHardeningTests(unittest.TestCase):
    def test_telemetry_is_off_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()
            data = Path(temporary) / "data"
            payload = {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "session",
                "cwd": str(root),
                "model": "gpt-5.6-sol",
                "prompt": "Trace the complete repository",
            }
            completed = subprocess.run(
                [sys.executable, str(SCRIPTS / "router_hook.py")],
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                env={"PLUGIN_ROOT": str(ROOT), "PLUGIN_DATA": str(data)},
            )
            self.assertEqual(completed.returncode, 0)
            self.assertFalse((data / "events.jsonl").exists())
            self.assertEqual(len(list((data / "state").glob("*.json"))), 1)

    def test_state_is_atomic_under_concurrent_mutations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            start = threading.Event()

            def increment(state: dict[str, object]) -> None:
                start.wait(timeout=3)
                state["count"] = int(state.get("count", 0)) + 1

            with patch.dict(os.environ, {"PLUGIN_DATA": str(root / "telemetry")}):
                threads = [threading.Thread(target=router_hook.mutate_route_state, args=(root, "session", increment)) for _ in range(8)]
                for thread in threads:
                    thread.start()
                start.set()
                for thread in threads:
                    thread.join(timeout=5)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            state_files = list((root / "telemetry" / "state").glob("*.json"))
            self.assertEqual(len(state_files), 1)
            self.assertEqual(json.loads(state_files[0].read_text(encoding="utf-8"))["count"], 8)

    def test_stale_lock_file_is_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_root = root / "telemetry" / "state"
            state_root.mkdir(parents=True)
            key = hashlib.sha256(b"session").hexdigest()[:16]
            stale_lock = state_root / f"{key}.lock"
            stale_lock.write_text("stale", encoding="utf-8")
            stale_time = time.time() - 120
            os.utime(stale_lock, (stale_time, stale_time))
            with patch.dict(os.environ, {"PLUGIN_DATA": str(root / "telemetry")}):
                def mark_recovered(state: dict[str, object]) -> str:
                    state["ok"] = True
                    return "recovered"

                self.assertEqual(router_hook.mutate_route_state(root, "session", mark_recovered), "recovered")
                state_path = state_root / f"{key}.json"
                self.assertTrue(state_path.exists())

    def test_active_kernel_lock_is_not_bypassed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_root = root / "telemetry" / "state"
            state_root.mkdir(parents=True)
            key = hashlib.sha256(b"session").hexdigest()[:16]
            legacy_lock = state_root / f"{key}.lock"
            descriptor = os.open(legacy_lock, os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            config = {"state_lock_timeout_seconds": 0.05, "state_lock_stale_seconds": 30}
            try:
                with patch.dict(os.environ, {"PLUGIN_DATA": str(root / "telemetry")}):
                    result = router_hook.mutate_route_state(root, "session", lambda state: state.update(ok=True), config)
                self.assertIsNone(result)
                self.assertTrue(legacy_lock.exists())
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

    def test_discovery_guard_fails_closed_when_state_lock_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_root = root / "telemetry" / "state"
            state_root.mkdir(parents=True)
            key = hashlib.sha256(b"session").hexdigest()[:16]
            legacy_lock = state_root / f"{key}.lock"
            descriptor = os.open(legacy_lock, os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            config = {
                "exclusive_discovery_guard_enabled": True,
                "state_lock_timeout_seconds": 0.05,
                "state_lock_stale_seconds": 30,
            }
            try:
                with patch.dict(os.environ, {"PLUGIN_DATA": str(root / "telemetry")}):
                    denial = router_hook.discovery_guard(root, "session", "gpt-5.6-sol", config)
                self.assertIn("denied", denial)
                self.assertTrue(legacy_lock.exists())
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)


    def test_hook_uses_payload_cwd_not_process_cwd_and_keeps_metadata_private(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo with spaces"
            root.mkdir()
            data = Path(temporary) / "telemetry"
            payload = {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "private-session",
                "cwd": str(root),
                "model": "gpt-5.6-sol",
                "prompt": "SECRET_PROMPT",
            }
            completed = subprocess.run(
                [sys.executable, str(SCRIPTS / "router_hook.py")],
                input=json.dumps(payload), text=True, capture_output=True,
                cwd=Path(temporary),
                env={
                    "PLUGIN_ROOT": str(ROOT),
                    "PLUGIN_DATA": str(data),
                    "ENGINEERING_ROUTER_TELEMETRY": "on",
                },
            )
            self.assertEqual(completed.returncode, 0)
            events = (data / "events.jsonl").read_text(encoding="utf-8")
            self.assertNotIn("SECRET_PROMPT", events)
            self.assertNotIn("private-session", events)
            self.assertNotIn(str(root), events)


class ManifestAndRunnerHardeningTests(unittest.TestCase):
    def test_manifest_mismatch_is_rejected_before_a_pair_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = Path(temporary) / "plugin.json"
            manifest.write_text(json.dumps({"name": "wrong-plugin", "version": "0.0.0"}), encoding="utf-8")
            with patch.object(paired_benchmark, "PLUGIN_MANIFEST", manifest):
                with self.assertRaisesRegex(ValueError, "(manifest|name|mismatch|plugin)"):
                    paired_benchmark.plugin_version()

    def test_runner_rejects_invalid_repository_with_exit_two(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = run_cli(
                SCRIPTS / "paired_benchmark.py",
                str(Path(temporary) / "not-a-repository"),
                "--prompt",
                "safe prompt",
            )
            self.assertEqual(result.returncode, 2)
            self.assertRegex(result.stderr, "(paired_benchmark:|invalid_inputs)")
            self.assertIn("usable Git repository", result.stderr)

    def test_installed_version_mismatch_is_rejected_before_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary) / "repo"
            repo.mkdir()
            with patch.object(paired_benchmark, "require_clean_repository", return_value="head"), \
                    patch.object(paired_benchmark, "repository_status_sha256", return_value="status"), \
                    patch.object(paired_benchmark, "runtime_attestation", side_effect=ValueError(
                        "Installed Codex Baron version does not match runner version"
                    )), \
                    patch.object(paired_benchmark, "run_one") as run_one:
                result = paired_benchmark.main([str(repo), "--prompt", "safe prompt"])
            self.assertEqual(result, 2)
            run_one.assert_not_called()


if __name__ == "__main__":
    unittest.main()
