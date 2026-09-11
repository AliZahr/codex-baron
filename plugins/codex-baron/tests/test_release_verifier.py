from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import release_verifier  # noqa: E402


class ReleaseVerifierTests(unittest.TestCase):
    def _evidence(self, root: Path) -> tuple[Path, Path, Path]:
        baseline = root / "baseline.jsonl"
        baron = root / "baron.jsonl"
        rubric = root / "rubric.txt"
        baseline_id, baron_id, worker_id = "baseline-root", "baron-root", "worker-1"
        baseline.write_text("\n".join(json.dumps(record) for record in (
            {"type": "thread.started", "thread_id": baseline_id},
            {"type": "item.completed", "item": {
                "id": "base-command", "type": "command_execution", "status": "completed",
            }},
            {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 0}},
        )) + "\n", encoding="utf-8")
        delegation_records = [
            {
                "type": "item.completed",
                "item": {
                    "id": "spawn-1", "type": "collab_tool_call", "tool": "spawn_agent",
                    "status": "completed", "task_name": "bulk_reader", "agent_type": "default",
                    "receiver_thread_ids": ["worker-1"],
                },
            },
            {
                "type": "item.completed",
                "item": {
                    "id": "wait-1", "type": "collab_tool_call", "tool": "wait",
                    "status": "completed", "receiver_thread_ids": ["worker-1"],
                },
            },
            {"type": "thread.started", "thread_id": baron_id},
            {"type": "turn.completed", "usage": {"input_tokens": 30, "output_tokens": 0}},
        ]
        baron.write_text(
            "\n".join(json.dumps(record) for record in delegation_records) + "\n",
            encoding="utf-8",
        )
        rubric.write_text("rubric\n", encoding="utf-8")
        digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
        def rollout(path: Path, thread_id: str, root_id: str, tokens: int, *, worker: bool = False) -> None:
            source = ({"subagent": {"thread_spawn": {
                "parent_thread_id": root_id, "agent_role": "bulk_reader",
            }}} if worker else "exec")
            records = [
                {"ordinal": 0, "type": "session_meta", "payload": {
                    "id": thread_id, "session_id": root_id, "source": source,
                }},
                {"ordinal": 1, "type": "event_msg", "payload": {
                    "type": "item_completed", "item": {
                        "id": f"command-{thread_id}", "type": "CommandExecution", "status": "completed",
                    },
                }},
                {"ordinal": 2, "type": "token_usage_record", "payload": {
                    "thread_id": thread_id, "session_id": root_id, "response_id": f"response-{thread_id}",
                    "usage": {"input_tokens": tokens, "cached_input_tokens": 0, "output_tokens": 0,
                              "reasoning_output_tokens": 0, "total_tokens": tokens},
                    "thread_token_usage": {"input_tokens": tokens, "cached_input_tokens": 0,
                                           "output_tokens": 0, "reasoning_output_tokens": 0,
                                           "total_tokens": tokens},
                }},
                {"ordinal": 3, "type": "event_msg", "payload": {"type": "token_count", "info": {
                    "total_token_usage": {"input_tokens": tokens, "cached_input_tokens": 0,
                                          "output_tokens": 0, "reasoning_output_tokens": 0,
                                          "total_tokens": tokens},
                }}},
                {"ordinal": 4, "type": "event_msg", "payload": {
                    "type": "task_complete", "last_agent_message": "done",
                }},
            ]
            path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
        baseline_root = root / "baseline-root-rollout.jsonl"
        baron_root = root / "baron-root-rollout.jsonl"
        worker = root / "baron-worker-1.jsonl"
        rollout(baseline_root, baseline_id, baseline_id, 100)
        rollout(baron_root, baron_id, baron_id, 30)
        rollout(worker, worker_id, baron_id, 20, worker=True)
        baron_root_records = [json.loads(line) for line in baron_root.read_text(encoding="utf-8").splitlines()]
        for record in baron_root_records[2:]:
            record["ordinal"] += 1
        baron_root_records.insert(2, {"ordinal": 2, "type": "event_msg", "payload": {
            "type": "item_completed", "item": {"type": "SubAgentActivity", "kind": "started",
                                                "agent_thread_id": worker_id},
        }})
        baron_root.write_text(
            "\n".join(json.dumps(record) for record in baron_root_records) + "\n", encoding="utf-8"
        )
        receipt = root / "baron-routing-state.json"
        receipt.write_text(json.dumps({
            "schema_version": 2,
            "root_session_sha256": hashlib.sha256(baron_id.encode()).hexdigest(),
            "worker_session_sha256": hashlib.sha256(worker_id.encode()).hexdigest(),
            "worker_artifact": {"name": worker.name, "sha256": digest(worker)},
            "state": {"route": "bulk_reader", "phase": "worker_complete", "worker_starts": 1,
                      "spawn_attempts": 1, "worker_identity_observed": True,
                      "identity_confidence": "stable", "spawn_observed_via_lifecycle": True,
                      "worker_commands": 1, "verification_commands": 0, "spawn_calls": 1,
                      "mailbox_waits": 1, "skill_reads": 1, "unauthorized_pre_handoff": 0,
                      "worker_final": True},
        }) + "\n", encoding="utf-8")
        baseline_usage = release_verifier.parse_usage_file(baseline, release_gate=True)
        baron_usage = release_verifier.parse_usage_file(baron, release_gate=True)
        worker_usage = release_verifier.parse_rollout_usage_file(
            worker, expected_root_session_id=baron_id, release_gate=True,
        )
        baron_aggregate = release_verifier.combine_usage(baron_usage, worker_usage, path="baron+workers")
        measured = release_verifier.build_comparison(
            baseline_usage, baron_aggregate,
        )
        comparison = {
            "schema_version": 3,
            "efficiency_gate_eligible": True,
            "efficiency_gate_passed": True,
            "efficiency_gate": {
                "eligible": True,
                "passed": True,
                "command_increase": 2,
                "max_command_increase": 2,
                "command_failure_increase": 0,
                "max_command_failure_increase": 0,
            },
            "comparison": measured,
            "accounting": {
                "mode": "root_plus_bound_descendants",
                "baseline": {"root": baseline_usage.to_dict(), "descendants": [],
                             "aggregate": baseline_usage.to_dict()},
                "baron": {"root": baron_usage.to_dict(), "descendants": [worker_usage.to_dict()],
                          "aggregate": baron_aggregate.to_dict()},
            },
            "plugin": {"id": "codex-baron@codex-baron", "version": "1.0.0-test"},
            "runtime": {"installed_plugin_version": "1.0.0-test", "codex_cli_version": "codex-cli test",
                        "installed_runtime_sha256": "e" * 64, "source_runtime_sha256": "e" * 64},
            "plugin_toggle_verified": True,
            "hooks": {
                "configured_enabled": True,
                "verified_events": [
                    "pre_tool_use", "user_prompt_submit", "subagent_start", "subagent_stop",
                ],
            },
            "agents": {
                "required": True,
                "name": "bulk_reader",
                "source": "project",
                "sha256": "d" * 64,
                "verified": True,
            },
            "routing": {
                "expected_route": "bulk_reader",
                "delegation_required": True,
                "delegation": release_verifier.inspect_delegation_evidence(baron, receipt),
            },
            "repository": {"head": "b" * 40, "status": "clean", "status_sha256": "c" * 64},
            "inputs": {"prompt_sha256": "a" * 64, "model": "gpt-test", "sandbox": "read-only",
                       "reasoning_effort": {"value": "medium", "source": "explicit_cli", "verified": True},
                       "tools": {"mode": "same_inherited_codex_runtime", "pairwise_held_constant": True,
                                 "effective_set_verified": False, "operator_accepted_unverified": True}},
            "prompt_sha256": "a" * 64,
            "repository_head": "b" * 40,
            "repository_status": "clean",
            "repository_status_sha256": "c" * 64,
            "plugin_version": "1.0.0-test", "model": "gpt-test", "sandbox": "read-only",
            "reasoning_effort": {"value": "medium", "source": "explicit_cli", "verified": True},
            "tools": {"mode": "same_inherited_codex_runtime", "pairwise_held_constant": True,
                      "effective_set_verified": False, "operator_accepted_unverified": True},
            "runs": {
                label: {"artifacts": {
                    "jsonl": {"name": f"{label}.jsonl", "sha256": digest(root / f"{label}.jsonl")},
                    "root_rollout": {"name": f"{label}-root-rollout.jsonl",
                                     "sha256": digest(root / f"{label}-root-rollout.jsonl")},
                    **({"descendants": [{"name": "baron-worker-1.jsonl", "sha256": digest(worker)}]}
                       if label == "baron" else {}),
                    **({"routing_state": {"name": receipt.name, "sha256": digest(receipt)}}
                       if label == "baron" else {}),
                }}
                for label in ("baseline", "baron")
            },
        }
        comparison_path = root / "comparison.json"
        comparison_path.write_text(json.dumps(comparison) + "\n", encoding="utf-8")
        quality = {
            "schema_version": 1,
            "comparison_sha256": digest(comparison_path),
            "prompt_sha256": "a" * 64,
            "repository_head": "b" * 40,
            "repository_status_sha256": "c" * 64,
            "baseline_jsonl_sha256": digest(baseline),
            "baron_jsonl_sha256": digest(baron),
            "rubric_sha256": digest(rubric),
            "evaluator": {"id": "eval", "version": "1"},
            "baseline": {"passed": True, "corrections": 0},
            "baron": {"passed": True, "corrections": 1},
        }
        quality_path = root / "quality.json"
        quality_path.write_text(json.dumps(quality) + "\n", encoding="utf-8")
        return comparison_path, quality_path, rubric

    @staticmethod
    def _rewrite_comparison(comparison: Path, quality: Path, mutate) -> None:
        payload = json.loads(comparison.read_text(encoding="utf-8"))
        mutate(payload)
        comparison.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        quality_payload = json.loads(quality.read_text(encoding="utf-8"))
        quality_payload["comparison_sha256"] = hashlib.sha256(comparison.read_bytes()).hexdigest()
        quality.write_text(json.dumps(quality_payload) + "\n", encoding="utf-8")

    def test_verifies_bound_quality_and_emits_private_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            comparison, quality, rubric = self._evidence(Path(temporary))
            result = release_verifier.verify_release(comparison, quality, rubric)
            self.assertEqual(result["status"], "production_approved")
            self.assertTrue((Path(temporary) / "production_release.json").exists())

    def test_rejects_legacy_receipt_only_accounting_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            comparison, quality, rubric = self._evidence(root)
            payload = json.loads(comparison.read_text(encoding="utf-8"))
            payload["schema_version"] = 2
            comparison.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unsupported"):
                release_verifier.verify_release(comparison, quality, rubric)
            return
            baron = root / "baron.jsonl"
            thread_id = "01a-release-root-session"
            baron.write_text("\n".join((
                json.dumps({"type": "thread.started", "thread_id": thread_id}),
                json.dumps({
                    "type": "turn.completed",
                    "usage": {"input_tokens": 10, "output_tokens": 0},
                }),
            )) + "\n", encoding="utf-8")
            receipt = root / "baron-routing-state.json"
            receipt.write_text(json.dumps({
                "schema_version": 1,
                "root_session_sha256": hashlib.sha256(thread_id.encode()).hexdigest(),
                "state": {
                    "route": "bulk_reader",
                    "phase": "worker_complete",
                    "worker_starts": 1,
                    "spawn_attempts": 1,
                    "worker_identity_observed": True,
                    "identity_confidence": "stable",
                    "spawn_observed_via_lifecycle": True,
                    "worker_commands": 2,
                    "verification_commands": 1,
                    "spawn_calls": 1,
                    "mailbox_waits": 1,
                    "skill_reads": 1,
                    "unauthorized_pre_handoff": 0,
                    "worker_final": True,
                },
            }) + "\n", encoding="utf-8")
            payload = json.loads(comparison.read_text(encoding="utf-8"))
            measured = release_verifier.build_comparison(
                release_verifier.parse_usage_file(root / "baseline.jsonl", release_gate=True),
                release_verifier.parse_usage_file(baron, release_gate=True),
            )
            payload["comparison"] = measured
            payload["agents"] = {
                "required": True,
                "name": "bulk_reader",
                "source": "user",
                "sha256": "a" * 64,
                "verified": True,
            }
            payload["efficiency_gate"].update({
                "command_increase": (
                    measured["baron"]["commands_started"]
                    - measured["baseline"]["commands_started"]
                ),
                "max_command_increase": 2,
                "command_failure_increase": 0,
                "max_command_failure_increase": 0,
            })
            payload["runs"]["baron"]["artifacts"]["jsonl"]["sha256"] = hashlib.sha256(
                baron.read_bytes()
            ).hexdigest()
            payload["runs"]["baron"]["artifacts"]["routing_state"] = {
                "name": receipt.name,
                "sha256": hashlib.sha256(receipt.read_bytes()).hexdigest(),
            }
            payload["routing"]["delegation"] = release_verifier.inspect_delegation_evidence(
                baron, receipt
            )
            comparison.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            quality_payload = json.loads(quality.read_text(encoding="utf-8"))
            quality_payload["baron_jsonl_sha256"] = hashlib.sha256(baron.read_bytes()).hexdigest()
            quality_payload["comparison_sha256"] = hashlib.sha256(
                comparison.read_bytes()
            ).hexdigest()
            quality.write_text(json.dumps(quality_payload) + "\n", encoding="utf-8")

            result = release_verifier.verify_release(comparison, quality, rubric)
            self.assertEqual(result["status"], "production_approved")

            receipt_payload = json.loads(receipt.read_text(encoding="utf-8"))
            receipt_payload["root_session_sha256"] = "0" * 64
            receipt.write_text(json.dumps(receipt_payload) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "routing-state artifact changed"):
                release_verifier.verify_release(comparison, quality, rubric)

    def test_rejects_duplicate_json_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            comparison, quality, rubric = self._evidence(root)
            quality.write_text('{"schema_version":1,"schema_version":1}\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "malformed|duplicate"):
                release_verifier.verify_release(comparison, quality, rubric)

    def test_rejects_missing_or_invalid_delegation_attestation(self) -> None:
        mutations = (
            lambda payload: payload.pop("routing"),
            lambda payload: payload["routing"].update({"delegation": {"verified": False}}),
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(case=index), tempfile.TemporaryDirectory() as temporary:
                comparison, quality, rubric = self._evidence(Path(temporary))
                self._rewrite_comparison(comparison, quality, mutate)
                with self.assertRaisesRegex(ValueError, "delegation|execution contract"):
                    release_verifier.verify_release(comparison, quality, rubric)

    def test_rejects_missing_or_invalid_hook_attestation(self) -> None:
        mutations = (
            lambda payload: payload.pop("hooks"),
            lambda payload: payload["hooks"].update({"configured_enabled": False}),
            lambda payload: payload["hooks"].update({"verified_events": ["pre_tool_use"]}),
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(case=index), tempfile.TemporaryDirectory() as temporary:
                comparison, quality, rubric = self._evidence(Path(temporary))
                self._rewrite_comparison(comparison, quality, mutate)
                with self.assertRaisesRegex(ValueError, "hook"):
                    release_verifier.verify_release(comparison, quality, rubric)

    def test_rejects_stale_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            comparison, quality, rubric = self._evidence(root)
            (root / "baron.jsonl").write_text("changed\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "artifact"):
                release_verifier.verify_release(comparison, quality, rubric)

    def test_rejects_failed_efficiency_gate_even_when_quality_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            comparison, quality, rubric = self._evidence(root)
            payload = json.loads(comparison.read_text(encoding="utf-8"))
            payload["efficiency_gate_passed"] = False
            comparison.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            quality_payload = json.loads(quality.read_text(encoding="utf-8"))
            quality_payload["comparison_sha256"] = hashlib.sha256(comparison.read_bytes()).hexdigest()
            quality.write_text(json.dumps(quality_payload) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "did not pass"):
                release_verifier.verify_release(comparison, quality, rubric)

    def test_rejects_failed_baron_quality_even_when_efficiency_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            comparison, quality, rubric = self._evidence(root)
            payload = json.loads(quality.read_text(encoding="utf-8"))
            payload["baron"]["passed"] = False
            quality.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "baron quality answer did not pass"):
                release_verifier.verify_release(comparison, quality, rubric)

    def test_recomputes_efficiency_instead_of_trusting_pass_boolean(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            comparison, quality, rubric = self._evidence(root)
            payload = json.loads(comparison.read_text(encoding="utf-8"))
            payload["comparison"]["baseline"]["total_tokens"] = 10
            payload["comparison"]["baron"]["total_tokens"] = 20
            payload["comparison"]["difference_tokens"] = -10
            payload["comparison"]["status"] = "exceeded"
            payload["efficiency_gate"]["passed"] = True
            payload["efficiency_gate_passed"] = True
            comparison.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            quality_payload = json.loads(quality.read_text(encoding="utf-8"))
            quality_payload["comparison_sha256"] = hashlib.sha256(comparison.read_bytes()).hexdigest()
            quality.write_text(json.dumps(quality_payload) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "did not save"):
                release_verifier.verify_release(comparison, quality, rubric)

    def test_rejects_self_consistent_additional_baron_failure_allowance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            comparison, quality, rubric = self._evidence(root)
            baron = root / "baron.jsonl"
            with baron.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({
                    "type": "item.completed",
                    "item": {"id": "failed-extra", "type": "command_execution", "status": "failed"},
                }) + "\n")
            payload = json.loads(comparison.read_text(encoding="utf-8"))
            measured = release_verifier.build_comparison(
                release_verifier.parse_usage_file(root / "baseline.jsonl", release_gate=True),
                release_verifier.parse_usage_file(baron, release_gate=True),
            )
            payload["comparison"] = measured
            payload["efficiency_gate"].update({
                "command_increase": measured["baron"]["commands_started"] - measured["baseline"]["commands_started"],
                "max_command_increase": 3,
                "command_failure_increase": 1,
                "max_command_failure_increase": 1,
            })
            digest = hashlib.sha256(baron.read_bytes()).hexdigest()
            payload["runs"]["baron"]["artifacts"]["jsonl"]["sha256"] = digest
            comparison.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            quality_payload = json.loads(quality.read_text(encoding="utf-8"))
            quality_payload["baron_jsonl_sha256"] = digest
            quality_payload["comparison_sha256"] = hashlib.sha256(comparison.read_bytes()).hexdigest()
            quality.write_text(json.dumps(quality_payload) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "command-failure"):
                release_verifier.verify_release(comparison, quality, rubric)

    def test_rejects_self_consistent_manifest_that_disagrees_with_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            comparison, quality, rubric = self._evidence(root)
            payload = json.loads(comparison.read_text(encoding="utf-8"))
            payload["comparison"]["baseline"]["total_tokens"] = 30
            payload["comparison"]["difference_tokens"] = 20
            comparison.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            quality_payload = json.loads(quality.read_text(encoding="utf-8"))
            quality_payload["comparison_sha256"] = hashlib.sha256(comparison.read_bytes()).hexdigest()
            quality.write_text(json.dumps(quality_payload) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "do not match|did not save"):
                release_verifier.verify_release(comparison, quality, rubric)


if __name__ == "__main__":
    unittest.main()
