from __future__ import annotations

import hashlib
import fcntl
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import router_hook  # noqa: E402


class RouterAdversarialTests(unittest.TestCase):
    def test_dual_lock_contention_stays_below_hook_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            state_root = data / "state"
            state_root.mkdir(parents=True)
            key = hashlib.sha256(b"session").hexdigest()[:16]
            state_lock = state_root / f"{key}.lock"
            telemetry_lock = data / "events.lock"
            descriptors = []
            for lock in (state_lock, telemetry_lock):
                descriptor = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                descriptors.append(descriptor)
            payload = {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "session",
                "cwd": str(root),
                "model": "primary",
                "prompt": "Trace the complete repository",
            }
            env = os.environ.copy()
            env.update({
                "PLUGIN_ROOT": str(ROOT),
                "PLUGIN_DATA": str(data),
                "ENGINEERING_ROUTER_TELEMETRY": "on",
                "ENGINEERING_ROUTER_LOCK_TIMEOUT": "2",
            })
            import time
            started = time.monotonic()
            try:
                completed = subprocess.run(
                    [sys.executable, str(ROOT / "scripts" / "router_hook.py")],
                    input=json.dumps(payload), text=True, capture_output=True, timeout=2, env=env,
                )
            finally:
                for descriptor in descriptors:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                    os.close(descriptor)
            self.assertEqual(completed.returncode, 0)
            self.assertLess(time.monotonic() - started, 1.25)

    def test_lock_release_does_not_delete_replacement_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            lock = Path(temporary) / "state.lock"
            with router_hook._directory_lock(lock, 0.1, 30):
                lock.unlink()
                lock.write_text("replacement", encoding="utf-8")
            self.assertEqual(lock.read_text(encoding="utf-8"), "replacement")

    def test_unrelated_subagent_stop_does_not_release_bulk_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            with patch.dict(os.environ, {"PLUGIN_DATA": str(data)}):
                self.assertTrue(router_hook.reset_route_state(root, "session", "bulk_reader", "primary"))
            env = os.environ.copy()
            env.update({"PLUGIN_ROOT": str(ROOT), "PLUGIN_DATA": str(data)})

            def hook(event: str, agent_type: str, agent_id: str) -> None:
                payload = {
                    "hook_event_name": event,
                    "session_id": "session",
                    "cwd": str(root),
                    "model": "worker",
                    "agent_type": agent_type,
                    "agent_id": agent_id,
                }
                subprocess.run(
                    [sys.executable, str(ROOT / "scripts" / "router_hook.py")],
                    input=json.dumps(payload), text=True, capture_output=True, check=True, env=env,
                )

            hook("SubagentStart", "bulk_reader", "bulk-1")
            hook("SubagentStop", "code_writer", "other-1")
            key = hashlib.sha256(b"session").hexdigest()[:16]
            state = json.loads((data / "state" / f"{key}.json").read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "worker_active")
            self.assertEqual(state["worker_id"], "bulk-1")

    def test_sensitive_named_route_cannot_downgrade_to_lower_tier(self) -> None:
        self.assertEqual(
            router_hook.recommend_route("Delegate implement auth token rotation to code_writer")[0],
            "senior_reviewer",
        )
        self.assertEqual(
            router_hook.recommend_route("Delegate the security review to bulk_reader")[0],
            "senior_reviewer",
        )
        self.assertEqual(
            router_hook.recommend_route("Delegate the payment auth migration to code_writer")[0],
            "senior_reviewer",
        )

    def test_sensitive_route_persists_review_requirement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            with patch.dict(os.environ, {"PLUGIN_DATA": str(data)}):
                self.assertTrue(router_hook.reset_route_state(root, "review", "senior_reviewer", "gpt-5.6-sol"))
            key = hashlib.sha256(b"review").hexdigest()[:16]
            state = json.loads((data / "state" / f"{key}.json").read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "awaiting_senior_review")
            self.assertTrue(state["senior_review_required"])

    def test_only_repository_reads_consume_discovery_budget(self) -> None:
        self.assertTrue(router_hook.is_repository_discovery_command("rg symbol ."))
        self.assertTrue(router_hook.is_repository_discovery_command("git status"))
        self.assertFalse(router_hook.is_repository_discovery_command("python build.py"))
        self.assertFalse(router_hook.is_repository_discovery_command("git reset --hard HEAD"))

    def test_plugin_skill_reads_are_exempt_but_mixed_repository_reads_are_not(self) -> None:
        skill = router_hook.PLUGIN_ROOT / "skills" / "codex-baron" / "SKILL.md"
        self.assertTrue(router_hook.is_baron_instruction_read(f"sed -n '1,260p' \"{skill}\""))
        self.assertTrue(router_hook.is_baron_instruction_read(
            f"wc -l \"{skill}\" && sed -n '1,260p' \"{skill}\""
        ))
        self.assertTrue(router_hook.is_baron_instruction_read(
            f"find \"{router_hook.PLUGIN_ROOT.parent.parent}\" -name SKILL.md -print"
        ))
        self.assertTrue(router_hook.is_baron_instruction_read(
            f"find \"{router_hook.PLUGIN_ROOT.parent.parent}\" -name SKILL.md "
            "-path '*/skills/codex-baron/*' -print"
        ))
        self.assertFalse(router_hook.is_baron_instruction_read(
            f"find \"{router_hook.PLUGIN_ROOT.parent.parent}\" -name SKILL.md "
            "-path '*/skills/other/*' -print"
        ))
        self.assertFalse(router_hook.is_baron_instruction_read(f"sed -n '1,20p' \"{skill}\" && rg --files ."))
        self.assertFalse(router_hook.is_baron_instruction_read("sed -n '1,20p' ./Sources/App.swift"))
        self.assertFalse(router_hook.is_baron_instruction_read(f"sed -n '1,20p' \"{skill}\" ./Sources/App.swift"))
        self.assertFalse(router_hook.is_baron_instruction_read(
            f"find \"{router_hook.PLUGIN_ROOT.parent.parent}\" -name SKILL.md -exec cat ./Sources/App.swift ;"
        ))
        self.assertFalse(router_hook.is_baron_instruction_read(f"sed -n '1,20p' \"{skill}\" > /tmp/out"))
        self.assertFalse(router_hook.is_baron_instruction_read(f"sed -n '1,20p' \"{skill}\" | head"))
        self.assertFalse(router_hook.is_baron_instruction_read(f"sed -n '1,20p' \"{skill}\"\nrg --files ."))
        self.assertFalse(router_hook.is_baron_instruction_read(f"./sed -n '1,20p' \"{skill}\""))
        self.assertFalse(router_hook.is_baron_instruction_read(
            f"/tmp/find \"{router_hook.PLUGIN_ROOT.parent.parent}\" -name SKILL.md -print"
        ))
        other = router_hook.PLUGIN_ROOT.parent.parent / "other" / "1.0" / "skills" / "other" / "SKILL.md"
        self.assertFalse(router_hook.is_baron_instruction_read(f"sed -n '1,20p' \"{other}\""))

    def test_installed_plugin_skill_read_uses_exact_cache_layout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            codex_home = Path(temporary) / ".codex"
            installed = (
                codex_home / "plugins" / "cache" / "codex-baron" / "codex-baron"
                / "1.0.0+codex.20260910210727" / "skills" / "codex-baron" / "SKILL.md"
            )
            sibling = (
                codex_home / "plugins" / "cache" / "other" / "codex-baron"
                / "1.0.0" / "skills" / "codex-baron" / "SKILL.md"
            )
            nested = installed.parent.parent / "nested" / "skills" / "codex-baron" / "SKILL.md"
            installed.parent.mkdir(parents=True)
            installed.write_text("line one\nline two\n", encoding="utf-8")
            self.assertTrue(router_hook.is_baron_instruction_read(
                f"sed -n '1,240p' \"{installed}\"",
                trusted_skill_path=installed,
                require_complete=True,
            ))
            self.assertFalse(router_hook.is_baron_instruction_read(
                f"sed -n '1,240p' \"{sibling}\"", trusted_skill_path=installed,
            ))
            self.assertFalse(router_hook.is_baron_instruction_read(
                f"sed -n '1,240p' \"{nested}\"", trusted_skill_path=installed,
            ))
            self.assertFalse(router_hook.is_baron_instruction_read(
                f"sed -n '1,1p' \"{installed}\"",
                trusted_skill_path=installed,
                require_complete=True,
            ))
            self.assertFalse(router_hook.is_baron_instruction_read(
                f"wc -l \"{installed}\"",
                trusted_skill_path=installed,
                require_complete=True,
            ))

    def test_non_bulk_agent_cannot_acquire_discovery_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = {"state_lock_timeout_seconds": 0.1}
            with patch.dict(os.environ, {"PLUGIN_DATA": str(root / "data")}):
                self.assertTrue(router_hook.reset_route_state(root, "ownership", "bulk_reader", "primary"))
                denial = router_hook.discovery_guard(
                    root, "ownership", "worker-model", config,
                    command="rg symbol .", agent_type="code_writer", agent_id="wrong",
                )
            self.assertIn("bulk_reader", denial or "")

    def test_missing_agent_identity_does_not_claim_discovery_from_bare_tool_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = {"state_lock_timeout_seconds": 0.1}
            with patch.dict(os.environ, {"PLUGIN_DATA": str(root / "data")}):
                self.assertTrue(router_hook.reset_route_state(root, "identity", "bulk_reader", "primary"))
                denial = router_hook.discovery_guard(
                    root, "identity", "worker-model", config, command="rg symbol .",
                )
            self.assertIn("SubagentStart", denial or "")

    def test_wait_is_denied_until_worker_is_started(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = {"state_lock_timeout_seconds": 0.1}
            with patch.dict(os.environ, {"PLUGIN_DATA": str(root / "data")}):
                self.assertTrue(router_hook.reset_route_state(root, "wait", "bulk_reader", "primary"))
                denial = router_hook.wait_guard(root, "wait", config)
            self.assertIn("nonempty", denial or "")

    def test_wait_must_target_exact_active_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = {"state_lock_timeout_seconds": 0.1, "telemetry_enabled": False}
            with patch.dict(os.environ, {"PLUGIN_DATA": str(root / "data")}):
                router_hook.reset_route_state(root, "targeted", "bulk_reader", "primary", config)
                def activate(state: dict[str, object]) -> None:
                    state.update(phase="worker_active", worker_id="bulk-1", worker_agent_type="bulk_reader")
                router_hook.mutate_route_state(root, "targeted", activate, config)
                self.assertIn("target exactly", router_hook.wait_guard(root, "targeted", config, {}) or "")
                self.assertIsNone(router_hook.wait_guard(
                    root, "targeted", config, {"targets": [{"agent_id": "bulk-1"}]},
                ))
                self.assertIsNone(router_hook.wait_guard(
                    root, "targeted", config, {"timeout_ms": 60_000}, "functions.collaboration.wait_agent",
                ))
                self.assertIn("target exactly", router_hook.wait_guard(
                    root, "targeted", config, {"receiver_thread_ids": ["wrong"]}, "wait",
                ) or "")
                def fallback(state: dict[str, object]) -> None:
                    state["phase"] = "local_fallback"
                router_hook.mutate_route_state(root, "targeted", fallback, config)
                self.assertIn("only while", router_hook.wait_guard(
                    root, "targeted", config, {"timeout_ms": 30_000}, "functions.collaboration.wait_agent",
                ) or "")
                def complete(state: dict[str, object]) -> None:
                    state["phase"] = "worker_complete"
                router_hook.mutate_route_state(root, "targeted", complete, config)
                self.assertIn("only while", router_hook.wait_guard(
                    root, "targeted", config, {"timeout_ms": 30_000}, "functions.collaboration.wait_agent",
                ) or "")

    def test_worker_command_cannot_self_claim_or_omit_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = {"state_lock_timeout_seconds": 0.1}
            with patch.dict(os.environ, {"PLUGIN_DATA": str(root / "data")}):
                router_hook.reset_route_state(root, "claim", "bulk_reader", "primary", config)
                router_hook.mark_spawn_attempted(root, "claim", config)
                denial = router_hook.discovery_guard(
                    root, "claim", "gpt-5.6-terra", config, "rg x .", "bulk_reader", None,
                )
                self.assertIn("SubagentStart", denial or "")
                def activate(state: dict[str, object]) -> None:
                    state.update(
                        phase="worker_active", worker_id="bulk-1", worker_agent_type="bulk_reader",
                        worker_model="gpt-5.6-terra",
                    )
                router_hook.mutate_route_state(root, "claim", activate, config)
                denial = router_hook.discovery_guard(
                    root, "claim", "gpt-5.6-terra", config, "rg x .", "bulk_reader", None,
                )
                self.assertIn("another bulk_reader", denial or "")

    def test_hook_manifest_registers_collaboration_guards(self) -> None:
        manifest = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        matcher = manifest["hooks"]["PreToolUse"][0]["matcher"]
        for tool in ("Agent", "spawn_agent", "wait", "wait_agent", "functions.collaboration.spawn_agent"):
            self.assertIn(tool, matcher)

    def test_agent_alias_is_recognized_as_spawn_tool(self) -> None:
        self.assertTrue(router_hook._is_spawn_agent_tool("Agent"))

    def test_real_spawn_and_lifecycle_payload_claims_bulk_reader_by_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            config = {"state_lock_timeout_seconds": 0.1, "telemetry_enabled": False}
            self.assertTrue(router_hook._spawn_targets_bulk_reader({
                "task_name": "bulk_reader",
                "message": "Map the public API with verified lines.",
            }))
            with patch.dict(os.environ, {"PLUGIN_DATA": str(data)}):
                router_hook.reset_route_state(root, "real-payload", "bulk_reader", "gpt-5.6-sol", config)
                pretool_payload = {
                    "hook_event_name": "PreToolUse",
                    "session_id": "real-payload",
                    "cwd": str(root),
                    "model": "gpt-5.6-sol",
                    "tool_name": "functions.collaboration.spawn_agent",
                    "tool_input": {
                        "task_name": "bulk_reader",
                        "message": "Map the public API with verified lines.",
                    },
                }
                with patch("sys.stdin", io.StringIO(json.dumps(pretool_payload))), patch(
                    "sys.stdout", new_callable=io.StringIO,
                ):
                    router_hook.main()
                payload = {
                    "hook_event_name": "SubagentStart",
                    "session_id": "real-payload",
                    "cwd": str(root),
                    "model": "gpt-5.6-terra",
                    "agent_type": "default",
                    "agent_id": "worker-real-1",
                }
                with patch("sys.stdin", io.StringIO(json.dumps(payload))), patch(
                    "sys.stdout", new_callable=io.StringIO,
                ):
                    router_hook.main()
                self.assertIsNone(router_hook.discovery_guard(
                    root, "real-payload", "gpt-5.6-terra", config,
                    command="rg symbol .", agent_type="default", agent_id="worker-real-1",
                ))
            key = hashlib.sha256(b"real-payload").hexdigest()[:16]
            state = json.loads((data / "state" / f"{key}.json").read_text(encoding="utf-8"))
            self.assertEqual(state["worker_agent_type"], "bulk_reader")
            self.assertEqual(state["observed_agent_type"], "default")
            self.assertEqual(state["worker_id"], "worker-real-1")

    def test_worker_start_without_stable_id_never_claims_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            config = {"state_lock_timeout_seconds": 0.1, "telemetry_enabled": False}
            with patch.dict(os.environ, {"PLUGIN_DATA": str(data)}):
                router_hook.reset_route_state(root, "missing-id", "bulk_reader", "primary", config)
                router_hook.mark_spawn_attempted(root, "missing-id", config)
                payload = {
                    "hook_event_name": "SubagentStart",
                    "session_id": "missing-id",
                    "cwd": str(root),
                    "model": "gpt-5.6-terra",
                    "agent_type": "bulk_reader",
                }
                with patch("sys.stdin", io.StringIO(json.dumps(payload))), patch("sys.stdout", new_callable=io.StringIO) as output:
                    router_hook.main()
                self.assertIn("worker ID is missing", output.getvalue())
            key = hashlib.sha256(b"missing-id").hexdigest()[:16]
            state = json.loads((data / "state" / f"{key}.json").read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "spawn_attempted")

    def test_unrelated_default_model_cannot_be_inferred_as_bulk_reader(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            config = {
                "state_lock_timeout_seconds": 0.1,
                "telemetry_enabled": False,
                "bulk_reader_model": "gpt-5.6-terra",
            }
            with patch.dict(os.environ, {"PLUGIN_DATA": str(data)}):
                router_hook.reset_route_state(root, "wrong-default", "bulk_reader", "gpt-5.6-sol", config)
                payload = {
                    "hook_event_name": "SubagentStart",
                    "session_id": "wrong-default",
                    "cwd": str(root),
                    "model": "gpt-5.6-luna",
                    "agent_type": "default",
                    "agent_id": "unrelated-worker",
                }
                with patch("sys.stdin", io.StringIO(json.dumps(payload))), patch(
                    "sys.stdout", new_callable=io.StringIO,
                ) as output:
                    router_hook.main()
                self.assertIn("only bulk_reader", output.getvalue())
            key = hashlib.sha256(b"wrong-default").hexdigest()[:16]
            state = json.loads((data / "state" / f"{key}.json").read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "awaiting_worker")
            self.assertEqual(state["worker_starts"], 0)

    def test_same_model_default_without_task_marker_cannot_claim_bulk_reader(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            config = {"state_lock_timeout_seconds": 0.1, "bulk_reader_model": "gpt-5.6-terra"}
            with patch.dict(os.environ, {"PLUGIN_DATA": str(data)}):
                router_hook.reset_route_state(root, "unmarked-default", "bulk_reader", "gpt-5.6-sol", config)
                payload = {
                    "hook_event_name": "SubagentStart", "session_id": "unmarked-default",
                    "cwd": str(root), "model": "gpt-5.6-terra", "agent_type": "default",
                    "agent_id": "unrelated-terra",
                }
                with patch("sys.stdin", io.StringIO(json.dumps(payload))), patch(
                    "sys.stdout", new_callable=io.StringIO,
                ) as output:
                    router_hook.main()
                self.assertIn("only bulk_reader", output.getvalue())
            key = hashlib.sha256(b"unmarked-default").hexdigest()[:16]
            state = json.loads((data / "state" / f"{key}.json").read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "awaiting_worker")

    def test_default_data_root_is_platform_specific_without_environment_override(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fake_home = Path(temporary)
            with patch.dict(os.environ, {}, clear=True), patch.object(
                router_hook.Path, "home", return_value=fake_home,
            ), patch.object(router_hook.sys, "platform", "darwin"):
                self.assertEqual(
                    router_hook._data_root(fake_home),
                    (fake_home / "Library" / "Application Support" / "codex-baron").resolve(),
                )
            with patch.dict(os.environ, {}, clear=True), patch.object(
                router_hook.Path, "home", return_value=fake_home,
            ), patch.object(router_hook.sys, "platform", "linux"):
                self.assertEqual(
                    router_hook._data_root(fake_home),
                    (fake_home / ".local" / "state" / "codex-baron").resolve(),
                )

    def test_local_fallback_requires_a_recorded_spawn_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            config = {"state_lock_timeout_seconds": 0.1}
            with patch.dict(os.environ, {"PLUGIN_DATA": str(data)}):
                router_hook.reset_route_state(root, "fallback", "bulk_reader", "primary")
                self.assertIn("reserved", router_hook.discovery_guard(root, "fallback", "primary", config, "rg x .") or "")
                self.assertIn("reserved", router_hook.discovery_guard(root, "fallback", "primary", config, "rg x .") or "")
                self.assertTrue(router_hook.mark_spawn_attempted(root, "fallback", config))
                self.assertIn("No worker handoff", router_hook.discovery_guard(root, "fallback", "primary", config, "rg x .") or "")
            key = hashlib.sha256(b"fallback").hexdigest()[:16]
            state = json.loads((data / "state" / f"{key}.json").read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "local_fallback")

    def test_reentrant_state_mutation_is_rejected_without_losing_outer_update(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.dict(os.environ, {"PLUGIN_DATA": str(root / "data")}):
                def outer(state: dict[str, object]) -> str:
                    state["outer"] = True
                    state["nested"] = router_hook.mutate_route_state(
                        root, "same", lambda nested: nested.update(inner=True), error_result="rejected"
                    )
                    return "outer-ok"

                self.assertEqual(router_hook.mutate_route_state(root, "same", outer), "outer-ok")
                key = hashlib.sha256(b"same").hexdigest()[:16]
                state = json.loads((root / "data" / "state" / f"{key}.json").read_text(encoding="utf-8"))
                self.assertTrue(state["outer"])
                self.assertEqual(state["nested"], "rejected")
                self.assertNotIn("inner", state)

    def test_state_lock_timeout_returns_safe_error_without_waiting_unboundedly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            entered = threading.Event()
            release = threading.Event()
            config = {"state_lock_timeout_seconds": 0.05}

            def hold(state: dict[str, object]) -> None:
                entered.set()
                release.wait(timeout=2)

            with patch.dict(os.environ, {"PLUGIN_DATA": str(root / "data")}):
                worker = threading.Thread(target=router_hook.mutate_route_state, args=(root, "locked", hold, config))
                worker.start()
                self.assertTrue(entered.wait(timeout=1))
                self.assertEqual(
                    router_hook.mutate_route_state(
                        root, "locked", lambda state: True, config, error_result="timed-out"
                    ),
                    "timed-out",
                )
                release.set()
                worker.join(timeout=2)
            self.assertFalse(worker.is_alive())

    def test_state_and_lock_permissions_and_retention_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            config = {"state_max_files": 2, "state_ttl_seconds": 3600, "state_lock_timeout_seconds": 0.1}
            with patch.dict(os.environ, {"PLUGIN_DATA": str(data)}):
                for session in ("one", "two", "three"):
                    self.assertIsNotNone(router_hook.mutate_route_state(root, session, lambda state: True, config))
            state_root = data / "state"
            state_files = list(state_root.glob("*.json"))
            self.assertLessEqual(len(state_files), 2)
            self.assertEqual(data.stat().st_mode & 0o777, 0o700)
            self.assertEqual(state_root.stat().st_mode & 0o777, 0o700)
            self.assertTrue(all(path.stat().st_mode & 0o777 == 0o600 for path in state_files))

    def test_default_state_is_not_written_inside_target_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary) / "repo"
            fake_home = Path(temporary) / "home"
            repo.mkdir()
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("PLUGIN_DATA", None)
                os.environ.pop("XDG_STATE_HOME", None)
                with patch.object(router_hook.Path, "home", return_value=fake_home):
                    self.assertTrue(router_hook.mutate_route_state(repo, "private", lambda state: True))
            self.assertFalse((repo / ".codex").exists())
            self.assertTrue((fake_home / "Library" / "Application Support" / "codex-baron").exists() or
                            (fake_home / ".local" / "state" / "codex-baron").exists())


if __name__ == "__main__":
    unittest.main()
