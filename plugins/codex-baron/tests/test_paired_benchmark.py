from __future__ import annotations

from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import paired_benchmark  # noqa: E402
import router_hook  # noqa: E402
from usage_meter import UsageSummary  # noqa: E402


class PairedBenchmarkTests(unittest.TestCase):
    @staticmethod
    def _production_rollouts(
        root_id: str,
        child_id: str,
        *,
        worker_commands: int = 4,
        verification_commands: int = 1,
        wait_timed_out: bool = False,
        final_message: str = "bounded findings",
        unauthorized_pre_handoff: bool = False,
        wait_before_spawn: bool = False,
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        def command_event(identifier: str, command: str) -> dict[str, object]:
            return {"type": "event_msg", "payload": {
                "type": "item_completed", "item": {
                    "id": identifier, "type": "CommandExecution",
                    "command": ["/bin/zsh", "-lc", command], "status": "completed", "exit_code": 0,
                },
            }}

        root_records: list[dict[str, object]] = [
            {"type": "session_meta", "payload": {
                "id": root_id, "session_id": root_id, "parent_thread_id": None, "source": "exec",
            }},
            command_event(
                "skill-read",
                f"sed -n '1,240p' '{ROOT / 'skills' / 'codex-baron' / 'SKILL.md'}'",
            ),
            {"type": "response_item", "payload": {
                "type": "function_call", "name": "spawn_agent", "call_id": "spawn-1",
            }},
            {"type": "response_item", "payload": {
                "type": "function_call_output", "call_id": "spawn-1",
                "output": json.dumps({"task_name": "/root/bulk_reader"}),
            }},
            {"type": "response_item", "payload": {
                "type": "function_call", "name": "wait_agent", "call_id": "wait-1",
            }},
            {"type": "response_item", "payload": {
                "type": "function_call_output", "call_id": "wait-1",
                "output": json.dumps({"message": "Wait completed.", "timed_out": wait_timed_out}),
            }},
        ]
        if wait_before_spawn:
            root_records[3], root_records[5] = root_records[5], root_records[3]
        if unauthorized_pre_handoff:
            root_records.insert(2, command_event("early-scan", "rg -n secret ."))
        root_records.extend(command_event(f"verify-{index}", "rg -n verify .")
                            for index in range(verification_commands))
        worker_records: list[dict[str, object]] = [{"type": "session_meta", "payload": {
            "id": child_id, "session_id": root_id, "parent_thread_id": root_id,
            "source": {"subagent": {"thread_spawn": {
                "parent_thread_id": root_id, "agent_role": "bulk_reader",
            }}},
        }}]
        worker_records.extend(command_event(f"worker-{index}", f"rg -n worker-{index} .")
                              for index in range(worker_commands))
        worker_records.append({"type": "event_msg", "payload": {
            "type": "task_complete", "last_agent_message": final_message,
        }})
        return root_records, worker_records

    @staticmethod
    def _write_rollouts(codex_home: Path, root_id: str, child_id: str, **kwargs: object) -> None:
        directory = codex_home / "sessions" / "2026" / "09" / "11"
        directory.mkdir(parents=True)
        root_records, worker_records = PairedBenchmarkTests._production_rollouts(root_id, child_id, **kwargs)
        for session_id, records in ((root_id, root_records), (child_id, worker_records)):
            path = directory / f"rollout-2026-09-11T00-00-00-{session_id}.jsonl"
            path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")

    def test_unified_exec_evidence_counts_only_bound_production_shape(self) -> None:
        root = "root-session"
        child = "worker-session"
        root_records, worker_records = self._production_rollouts(root, child)
        evidence = router_hook.inspect_unified_exec_evidence(root_records, worker_records, root, child)
        self.assertEqual(evidence["bulk_identities"], 1)
        self.assertEqual(evidence["mailbox_waits"], 1)
        self.assertEqual(evidence["spawn_calls"], 1)
        self.assertEqual(evidence["skill_reads"], 1)
        self.assertEqual(evidence["unauthorized_pre_handoff"], 0)
        self.assertEqual(evidence["worker_commands"], 4)
        self.assertEqual(evidence["verification_commands"], 1)
        self.assertTrue(evidence["session_bound"])
        self.assertTrue(evidence["worker_final"])

    def test_unified_exec_evidence_binds_installed_skill_path_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            installed = root / "cache" / "codex-baron" / "1.0.0" / "skills" / "codex-baron" / "SKILL.md"
            installed.parent.mkdir(parents=True)
            installed.write_bytes((ROOT / "skills" / "codex-baron" / "SKILL.md").read_bytes())
            root_records, worker_records = self._production_rollouts("root", "child")
            root_records[1]["payload"]["item"]["command"][-1] = f"sed -n '1,240p' '{installed}'"

            evidence = router_hook.inspect_unified_exec_evidence(
                root_records, worker_records, "root", "child", installed,
            )
            self.assertEqual(evidence["skill_reads"], 1)
            self.assertEqual(evidence["unauthorized_pre_handoff"], 0)

            stale = installed.parents[3] / "0.9.0" / "skills" / "codex-baron" / "SKILL.md"
            rejected = router_hook.inspect_unified_exec_evidence(
                root_records, worker_records, "root", "child", stale,
            )
            self.assertEqual(rejected["skill_reads"], 0)
            self.assertEqual(rejected["unauthorized_pre_handoff"], 1)

    def test_unified_exec_evidence_preserves_overages_and_fails_closed(self) -> None:
        root_records, worker_records = self._production_rollouts(
            "root", "child", worker_commands=5, verification_commands=2,
            final_message="",
        )
        evidence = router_hook.inspect_unified_exec_evidence(root_records, worker_records, "root", "child")
        self.assertEqual(evidence["worker_commands"], 5)
        self.assertEqual(evidence["verification_commands"], 2)
        self.assertEqual(evidence["mailbox_waits"], 1)
        self.assertFalse(evidence["worker_final"])
        wrong = router_hook.inspect_unified_exec_evidence(root_records, worker_records, "root", "other-child")
        self.assertFalse(wrong["session_bound"])
        timed_root, timed_worker = self._production_rollouts("root", "child", wait_timed_out=True)
        timed = router_hook.inspect_unified_exec_evidence(timed_root, timed_worker, "root", "child")
        self.assertEqual(timed["mailbox_waits"], 0)

        early_root, early_worker = self._production_rollouts("root", "child", unauthorized_pre_handoff=True)
        early = router_hook.inspect_unified_exec_evidence(early_root, early_worker, "root", "child")
        self.assertEqual(early["unauthorized_pre_handoff"], 1)

        reordered_root, reordered_worker = self._production_rollouts("root", "child", wait_before_spawn=True)
        reordered = router_hook.inspect_unified_exec_evidence(reordered_root, reordered_worker, "root", "child")
        self.assertEqual(reordered["mailbox_waits"], 0)

        failed_root, failed_worker = self._production_rollouts("root", "child")
        failed_skill = failed_root[1]["payload"]["item"]
        failed_skill["status"] = "failed"
        failed_skill["exit_code"] = 1
        failed = router_hook.inspect_unified_exec_evidence(failed_root, failed_worker, "root", "child")
        self.assertEqual(failed["skill_reads"], 0)
        self.assertEqual(failed["unauthorized_pre_handoff"], 1)
    @staticmethod
    def _hook_config(*, disabled_event: str | None = None) -> str:
        lines: list[str] = []
        for event in paired_benchmark.REQUIRED_HOOK_EVENTS:
            key = f"{paired_benchmark.PLUGIN_ID}:router:{event}:0"
            lines.extend([
                f'[hooks.state."{key}"]',
                f"enabled = {'false' if event == disabled_event else 'true'}",
                f'trusted_hash = "sha256:{"a" * 64}"',
            ])
        return "\n".join(lines) + "\n"

    def test_plugin_attestation_uses_bounded_local_marketplace_query(self) -> None:
        completed = subprocess.CompletedProcess([], 0, stdout="listing", stderr="")
        with patch.object(paired_benchmark.subprocess, "run", return_value=completed) as run:
            self.assertEqual(paired_benchmark._codex_plugin_list(), "listing")
        self.assertEqual(
            run.call_args.args[0],
            ["codex", "plugin", "list", "--marketplace", "codex-baron"],
        )
        self.assertEqual(run.call_args.kwargs["timeout"], 5)

    def test_plugin_toggle_verifies_disabled_and_enabled_states(self) -> None:
        results = [
            subprocess.CompletedProcess([], 0, stdout=json.dumps({"installed": [{
                "pluginId": paired_benchmark.PLUGIN_ID, "enabled": enabled,
            }]}), stderr="")
            for enabled in (False, True)
        ]
        with patch.object(paired_benchmark.subprocess, "run", side_effect=results) as run:
            paired_benchmark.verify_plugin_toggle()
        self.assertEqual(run.call_count, 2)
        self.assertIn(
            f"{paired_benchmark.PLUGIN_OVERRIDE}=false", run.call_args_list[0].args[0],
        )
        self.assertIn(
            f"{paired_benchmark.PLUGIN_OVERRIDE}=true", run.call_args_list[1].args[0],
        )

    def test_plugin_toggle_fails_closed_when_override_is_ignored(self) -> None:
        result = subprocess.CompletedProcess([], 0, stdout=json.dumps({"installed": [{
            "pluginId": paired_benchmark.PLUGIN_ID, "enabled": True,
        }]}), stderr="")
        with patch.object(paired_benchmark.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(ValueError, "did not take effect"):
                paired_benchmark.verify_plugin_toggle()

    def test_plugin_toggle_rejects_duplicate_json_keys(self) -> None:
        invalid_payloads = (
            '{"installed":[{"pluginId":"codex-baron@codex-baron","enabled":true,"enabled":false}]}',
            '{"installed":[{"pluginId":"other","pluginId":"codex-baron@codex-baron","enabled":false}]}',
            '{"installed":[],"installed":[{"pluginId":"codex-baron@codex-baron","enabled":false}]}',
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                result = subprocess.CompletedProcess([], 0, stdout=payload, stderr="")
                with patch.object(paired_benchmark.subprocess, "run", return_value=result):
                    with self.assertRaisesRegex(ValueError, "invalid state"):
                        paired_benchmark.verify_plugin_toggle()

    def test_hook_preflight_accepts_enabled_and_rejects_disabled_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            codex_home = Path(temporary)
            config = codex_home / "config.toml"
            config.write_text(self._hook_config(), encoding="utf-8")
            with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
                attestation = paired_benchmark.verify_hooks_enabled()
            self.assertTrue(attestation["configured_enabled"])
            self.assertEqual(
                attestation["verified_events"],
                list(paired_benchmark.REQUIRED_HOOK_EVENTS),
            )

            config.write_text(
                self._hook_config(disabled_event="subagent_start"), encoding="utf-8",
            )
            with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
                with self.assertRaisesRegex(ValueError, "subagent_start hook is disabled"):
                    paired_benchmark.verify_hooks_enabled()

    def test_delegation_jsonl_requires_one_spawn_and_targeted_wait(self) -> None:
        spawn = {
            "type": "item.completed",
            "item": {
                "id": "spawn-1",
                "type": "collab_tool_call",
                "tool": "spawn_agent",
                "status": "completed",
                "task_name": "bulk_reader",
                "agent_type": "default",
                "receiver_thread_ids": ["worker-1"],
            },
        }
        targeted_wait = {
            "type": "item.completed",
            "item": {
                "id": "wait-1",
                "type": "collab_tool_call",
                "tool": "wait",
                "status": "completed",
                "receiver_thread_ids": ["worker-1"],
            },
        }
        empty_wait = {
            "type": "item.completed",
            "item": {
                "id": "wait-empty",
                "type": "collab_tool_call",
                "tool": "wait",
                "status": "completed",
                "receiver_thread_ids": [],
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            stream = Path(temporary) / "baron.jsonl"
            stream.write_text(
                "\n".join(json.dumps(record) for record in (spawn, targeted_wait)) + "\n",
                encoding="utf-8",
            )
            evidence = paired_benchmark.inspect_delegation_evidence(stream)
            self.assertTrue(evidence["verified"])
            self.assertEqual(evidence["valid_waits"], 1)

            for records in ((spawn, empty_wait), (empty_wait,)):
                with self.subTest(records=records):
                    stream.write_text(
                        "\n".join(json.dumps(record) for record in records) + "\n",
                        encoding="utf-8",
                    )
                    rejected = paired_benchmark.inspect_delegation_evidence(stream)
                    self.assertFalse(rejected["verified"])
                    self.assertIn(
                        "no completed wait was bound to the spawned worker",
                        rejected["failures"],
                    )

            wrong_role = json.loads(json.dumps(spawn))
            wrong_role["item"]["task_name"] = "code_writer"
            stream.write_text(
                "\n".join(json.dumps(record) for record in (wrong_role, targeted_wait)) + "\n",
                encoding="utf-8",
            )
            rejected = paired_benchmark.inspect_delegation_evidence(stream)
            self.assertFalse(rejected["verified"])
            self.assertEqual(rejected["bulk_reader_spawns"], 0)

    def test_session_bound_hook_receipt_verifies_cli_without_parent_spawn_records(self) -> None:
        thread_id = "01a-test-root-session"
        state = {
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
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stream = root / "baron.jsonl"
            stream.write_text(json.dumps({
                "type": "thread.started", "thread_id": thread_id,
            }) + "\n", encoding="utf-8")
            receipt = root / "baron-routing-state.json"
            receipt.write_text(json.dumps({
                "schema_version": 2,
                "root_session_sha256": hashlib.sha256(thread_id.encode()).hexdigest(),
                "state": state,
            }) + "\n", encoding="utf-8")

            evidence = paired_benchmark.inspect_delegation_evidence(stream, receipt)
            self.assertTrue(evidence["verified"])
            self.assertEqual(evidence["verification_mode"], "hook_receipt")
            self.assertTrue(evidence["hook_receipt"]["session_bound"])

            wait = {
                "type": "item.completed",
                "item": {
                    "type": "collab_tool_call", "tool": "wait", "status": "completed",
                    "receiver_thread_ids": [],
                },
            }
            stream.write_text("\n".join((
                json.dumps({"type": "thread.started", "thread_id": thread_id}),
                json.dumps({"type": "item.started", "item": {**wait["item"], "status": "in_progress"}}),
                json.dumps(wait),
            )) + "\n", encoding="utf-8")
            evidence = paired_benchmark.inspect_delegation_evidence(stream, receipt)
            self.assertTrue(evidence["verified"])
            self.assertEqual(evidence["verification_mode"], "hook_receipt")

            wrong = json.loads(receipt.read_text(encoding="utf-8"))
            wrong["root_session_sha256"] = "0" * 64
            receipt.write_text(json.dumps(wrong) + "\n", encoding="utf-8")
            rejected = paired_benchmark.inspect_delegation_evidence(stream, receipt)
            self.assertFalse(rejected["verified"])
            self.assertIn("different root session", " ".join(rejected["failures"]))

            contradictory = {
                "type": "item.completed",
                "item": {
                    "type": "collab_tool_call", "tool": "spawn_agent", "status": "failed",
                    "task_name": "code_writer", "receiver_thread_ids": ["wrong-worker"],
                },
            }
            wrong["root_session_sha256"] = hashlib.sha256(thread_id.encode()).hexdigest()
            receipt.write_text(json.dumps(wrong) + "\n", encoding="utf-8")
            stream.write_text("\n".join((
                json.dumps({"type": "thread.started", "thread_id": thread_id}),
                json.dumps(contradictory),
            )) + "\n", encoding="utf-8")
            rejected = paired_benchmark.inspect_delegation_evidence(stream, receipt)
            self.assertFalse(rejected["verified"])
            self.assertEqual(rejected["verification_mode"], "none")

    def test_capture_hook_state_creates_privacy_safe_bound_receipt(self) -> None:
        thread_id = "01a-capture-root-session"
        worker_id = "01a-capture-worker-session"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex_home = root / "codex-home"
            data_root = root / "plugin-data"
            stream = root / "baron.jsonl"
            stream.write_text(json.dumps({
                "type": "thread.started", "thread_id": thread_id,
            }) + "\n", encoding="utf-8")
            state_dir = data_root / "state"
            state_dir.mkdir(parents=True)
            source = state_dir / f"{hashlib.sha256(thread_id.encode()).hexdigest()[:16]}.json"
            source.write_text(json.dumps({
                "route": "bulk_reader", "phase": "worker_complete",
                "worker_starts": 1, "spawn_attempts": 1, "worker_commands": 1,
                "verification_commands": 0, "identity_confidence": "stable",
                "spawn_observed_via_lifecycle": True,
                "worker_id": worker_id,
            }) + "\n", encoding="utf-8")
            self._write_rollouts(codex_home, thread_id, worker_id, worker_commands=1, verification_commands=0)
            with patch.dict(os.environ, {"PLUGIN_DATA": str(data_root), "CODEX_HOME": str(codex_home)}, clear=False):
                receipt_path = paired_benchmark.capture_hook_state(stream, root)
            self.assertIsNotNone(receipt_path)
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertEqual(receipt["schema_version"], 2)
            self.assertEqual(
                receipt["root_session_sha256"], hashlib.sha256(thread_id.encode()).hexdigest(),
            )
            self.assertNotIn("worker_id", receipt)
            self.assertNotIn("worker_id", receipt["state"])
            self.assertTrue(receipt["state"]["worker_identity_observed"])

            victim = root / "victim.txt"
            victim.write_text("safe\n", encoding="utf-8")
            receipt_path.unlink()
            receipt_path.symlink_to(victim)
            with patch.dict(os.environ, {"PLUGIN_DATA": str(data_root), "CODEX_HOME": str(codex_home)}, clear=False):
                replaced = paired_benchmark.capture_hook_state(stream, root)
            self.assertEqual(victim.read_text(encoding="utf-8"), "safe\n")
            self.assertFalse(replaced.is_symlink())

            source.write_text(json.dumps({
                "route": "bulk_reader private-secret", "phase": "worker_complete",
            }) + "\n", encoding="utf-8")
            with patch.dict(os.environ, {"PLUGIN_DATA": str(data_root), "CODEX_HOME": str(codex_home)}, clear=False):
                self.assertIsNone(paired_benchmark.capture_hook_state(stream, root))

    def test_capture_hook_state_uses_codex_plugin_data_directory(self) -> None:
        thread_id = "01a-plugin-data-root"
        worker_id = "01a-plugin-data-worker"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex_home = root / "codex-home"
            data_root = codex_home / "plugins" / "data" / "codex-baron-codex-baron"
            state_dir = data_root / "state"
            state_dir.mkdir(parents=True)
            stream = root / "baron.jsonl"
            stream.write_text(json.dumps({
                "type": "thread.started", "thread_id": thread_id,
            }) + "\n", encoding="utf-8")
            source = state_dir / f"{hashlib.sha256(thread_id.encode()).hexdigest()[:16]}.json"
            source.write_text(json.dumps({
                "route": "bulk_reader", "phase": "worker_complete",
                "worker_starts": 1, "spawn_attempts": 1, "worker_commands": 1,
                "verification_commands": 0, "identity_confidence": "stable",
                "spawn_observed_via_lifecycle": True, "worker_id": worker_id,
            }) + "\n", encoding="utf-8")
            self._write_rollouts(codex_home, thread_id, worker_id, worker_commands=1, verification_commands=0)
            with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}, clear=False):
                receipt = paired_benchmark.capture_hook_state(stream, root)
            self.assertIsNotNone(receipt)
            self.assertTrue(json.loads(receipt.read_text())["state"]["worker_identity_observed"])

    def test_capture_hook_state_derives_unified_exec_counters(self) -> None:
        root_id, child_id = "root-unified", "child-unified"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex_home = root / "codex-home"
            data_root = root / "plugin-data"
            state_dir = data_root / "state"
            state_dir.mkdir(parents=True)
            stream = root / "baron.jsonl"
            stream.write_text(json.dumps({"type": "thread.started", "thread_id": root_id}) + "\n", encoding="utf-8")
            source = state_dir / f"{hashlib.sha256(root_id.encode()).hexdigest()[:16]}.json"
            source.write_text(json.dumps({"route": "bulk_reader", "phase": "worker_complete",
                "worker_starts": 1, "spawn_attempts": 1, "worker_commands": 0,
                "verification_commands": 0, "identity_confidence": "stable",
                "spawn_observed_via_lifecycle": True}) + "\n", encoding="utf-8")
            source_data = json.loads(source.read_text())
            source_data["worker_id"] = child_id
            source.write_text(json.dumps(source_data) + "\n", encoding="utf-8")
            self._write_rollouts(codex_home, root_id, child_id)
            with patch.dict(os.environ, {"PLUGIN_DATA": str(data_root), "CODEX_HOME": str(codex_home)}, clear=False):
                receipt_path = paired_benchmark.capture_hook_state(stream, root)
            state = json.loads(receipt_path.read_text(encoding="utf-8"))["state"]
            self.assertEqual(state["worker_commands"], 4)
            self.assertEqual(state["verification_commands"], 1)
            self.assertEqual(state["mailbox_waits"], 1)
            self.assertTrue(state["worker_final"])

    def test_verify_agent_profile_accepts_exact_user_profile_and_rejects_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repo"
            repo.mkdir()
            codex_home = root / "codex-home"
            target = codex_home / "agents" / "bulk_reader.toml"
            target.parent.mkdir(parents=True)
            target.write_bytes((ROOT / "agents" / "bulk_reader.toml").read_bytes())
            with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}, clear=False):
                result = paired_benchmark.verify_agent_profile(repo)
                self.assertTrue(result["verified"])
                self.assertEqual(result["source"], "user")
                target.write_text("name = \"changed\"\n", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "differs"):
                    paired_benchmark.verify_agent_profile(repo)

    def test_installed_info_resolves_cache_not_marketplace_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            codex_home = Path(temporary) / "codex-home"
            runtime = codex_home / "plugins" / "cache" / "market" / "codex-baron" / "1.2.3"
            runtime.mkdir(parents=True)
            listing = (
                "Marketplace `market`\n/source/marketplace.json\n\n"
                "PLUGIN STATUS VERSION SOURCE\n"
                "codex-baron@codex-baron  installed, enabled  1.2.3  /source/plugin\n"
            )
            with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}), \
                    patch.object(paired_benchmark, "_codex_plugin_list", return_value=listing):
                version, resolved = paired_benchmark.installed_plugin_info()
            self.assertEqual(version, "1.2.3")
            self.assertEqual(resolved, runtime.resolve())

    def test_parse_args_registers_each_option_once(self) -> None:
        args = paired_benchmark.parse_args([
            "/tmp/repo", "--prompt", "inspect", "--sandbox", "workspace-write",
        ])
        self.assertEqual(args.sandbox, "workspace-write")

    def test_build_codex_command_is_argument_based_and_toggles_plugin(self) -> None:
        baseline = paired_benchmark.build_codex_command(
            Path("/tmp/repo"), "inspect; do-not-run", "gpt-test", "read-only", False, True,
        )
        baron = paired_benchmark.build_codex_command(
            Path("/tmp/repo"), "inspect; do-not-run", "gpt-test", "workspace-write", True, False,
        )
        self.assertIn('plugins.codex-baron@codex-baron.enabled=false', baseline)
        self.assertIn('plugins.codex-baron@codex-baron.enabled=true', baron)
        self.assertIn("inspect; do-not-run", baseline)
        self.assertNotIn("&&", baseline)
        self.assertIn("--ephemeral", baseline)
        self.assertNotIn("--ephemeral", baron)

    def test_require_clean_repository_accepts_clean_and_rejects_dirty(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / "file.txt").write_text("initial\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "file.txt"], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "initial"],
                check=True,
            )
            head = paired_benchmark.require_clean_repository(repo)
            self.assertTrue(head)
            (repo / "file.txt").write_text("dirty\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "must be clean"):
                paired_benchmark.require_clean_repository(repo)

    def test_quiescent_output_can_be_checked_without_delay(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "run.jsonl"
            output.write_text('{"type":"turn.completed"}\n', encoding="utf-8")
            paired_benchmark.wait_for_quiescent_output(
                output, minimum_wait=0, settle_seconds=0, timeout=1,
            )

    def test_main_runs_mocked_pair_and_fails_no_savings_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary) / "artifacts"
            records = {
                "baseline": paired_benchmark.RunRecord("baseline", 0.1, "baseline.jsonl", "baseline.err"),
                "baron": paired_benchmark.RunRecord("baron", 0.1, "baron.jsonl", "baron.err"),
            }

            def fake_run(label: str, *args: object) -> paired_benchmark.RunRecord:
                return records[label]

            same_usage = UsageSummary(input_tokens=10, output_tokens=5)
            with patch.object(paired_benchmark, "require_clean_repository", return_value="head"), \
                    patch.object(paired_benchmark, "repository_status_sha256", return_value="status-digest"), \
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
                    patch.object(paired_benchmark, "inspect_delegation_evidence", return_value={"verified": False}), \
                    patch.object(paired_benchmark, "parse_usage_file", return_value=same_usage), \
                    patch.object(paired_benchmark, "capture_root_rollout", side_effect=lambda *a: output_dir / f"{a[2]}-root-rollout.jsonl"), \
                    patch.object(paired_benchmark, "parse_rollout_usage_file", return_value=same_usage), \
                    patch.object(paired_benchmark, "_thread_id", return_value="root"), \
                    patch.object(paired_benchmark, "_inspect_jsonl_delegation_evidence", return_value={"completed_spawns": 0}), \
                    patch.object(paired_benchmark, "root_descendant_ids", return_value=set()), \
                    patch.object(paired_benchmark, "artifact_descriptor", return_value={"sha256": "artifact"}):
                result = paired_benchmark.main([str(ROOT), "--prompt", "test", "--output-dir", str(output_dir)])
            self.assertEqual(result, 1)
            metadata = json.loads((output_dir / "comparison.json").read_text(encoding="utf-8"))
            self.assertFalse(metadata["efficiency_gate_passed"])

    def test_release_pass_records_inherited_tool_acknowledgement_without_claiming_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary) / "artifacts"
            records = {
                "baseline": paired_benchmark.RunRecord("baseline", 0.1, "baseline.jsonl", "baseline.err"),
                "baron": paired_benchmark.RunRecord("baron", 0.1, "baron.jsonl", "baron.err"),
            }
            attestation = {
                "installed_plugin_version": paired_benchmark.plugin_version(),
                "source_runtime_sha256": "digest",
                "installed_runtime_sha256": "digest",
                "codex_cli_version": "codex-test",
            }
            usages = [
                UsageSummary(input_tokens=20, output_tokens=5),
                UsageSummary(input_tokens=10, output_tokens=5),
            ]
            def fake_capture(*args: object) -> Path:
                output_dir.mkdir(parents=True, exist_ok=True)
                (output_dir / "baron-worker-1.jsonl").write_text("worker\n", encoding="utf-8")
                state = output_dir / "baron-routing-state.json"
                state.write_text(json.dumps({
                    "worker_session_sha256": hashlib.sha256(b"worker").hexdigest(),
                }) + "\n", encoding="utf-8")
                return state
            with patch.object(paired_benchmark, "require_clean_repository", return_value="head"), \
                    patch.object(paired_benchmark, "repository_status_sha256", return_value="status-digest"), \
                    patch.object(paired_benchmark, "verify_plugin_toggle"), \
                    patch.object(paired_benchmark, "verify_hooks_enabled", return_value={
                        "configured_enabled": True,
                        "verified_events": list(paired_benchmark.REQUIRED_HOOK_EVENTS),
                    }), \
                    patch.object(paired_benchmark, "runtime_attestation", return_value=attestation), \
                    patch.object(paired_benchmark, "attested_skill_path", return_value=(
                        ROOT / "skills" / "codex-baron" / "SKILL.md"
                    )), \
                    patch.object(paired_benchmark, "run_one", side_effect=lambda label, *args, **kwargs: records[label]), \
                    patch.object(paired_benchmark, "parse_usage_file", side_effect=usages), \
                    patch.object(paired_benchmark, "capture_root_rollout", side_effect=lambda *a: output_dir / f"{a[2]}-root-rollout.jsonl"), \
                    patch.object(paired_benchmark, "capture_hook_state", side_effect=fake_capture), \
                    patch.object(paired_benchmark, "parse_rollout_usage_file", side_effect=[usages[0], usages[1], UsageSummary()]), \
                    patch.object(paired_benchmark, "_thread_id", return_value="root"), \
                    patch.object(paired_benchmark, "_inspect_jsonl_delegation_evidence", return_value={"completed_spawns": 0}), \
                    patch.object(paired_benchmark, "root_descendant_ids", side_effect=[set(), {"worker"}]), \
                    patch.object(paired_benchmark, "inspect_delegation_evidence", return_value={"verified": True}), \
                    patch.object(paired_benchmark, "artifact_descriptor", return_value={"sha256": "artifact"}):
                result = paired_benchmark.main([
                    str(ROOT), "--prompt", "inspect the repository", "--output-dir", str(output_dir),
                    "--reasoning-effort", "medium", "--sandbox", "read-only", "--accept-inherited-tools",
                ])
            self.assertEqual(result, 0)
            metadata = json.loads((output_dir / "comparison.json").read_text(encoding="utf-8"))
            self.assertFalse(metadata["comparability_verified"])
            self.assertTrue(metadata["efficiency_gate_eligible"])
            self.assertTrue(metadata["efficiency_gate_passed"])
            self.assertFalse(metadata["inputs"]["tools"]["effective_set_verified"])
            self.assertTrue(metadata["inputs"]["tools"]["operator_accepted_unverified"])


if __name__ == "__main__":
    unittest.main()
