from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from router_hook import (  # noqa: E402
    classify_prompt,
    find_large_full_read,
    patch_paths,
    recommend_route,
    sensitive_path,
)


def run_hook(payload: dict, data_root: Path) -> subprocess.CompletedProcess[str]:
    env = {
        "PLUGIN_ROOT": str(ROOT),
        "PLUGIN_DATA": str(data_root),
        "ENGINEERING_ROUTER_TELEMETRY": "on",
    }
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "router_hook.py")],
        input=json.dumps(payload), text=True, capture_output=True, check=True, env=env,
    )


class RouterTests(unittest.TestCase):
    def test_prompt_routes(self) -> None:
        self.assertEqual(classify_prompt("Trace all callers of this handler")[0], "bulk_reader")
        self.assertEqual(classify_prompt("Generate boilerplate config")[0], "code_writer")
        self.assertEqual(classify_prompt("Add unit tests and mocks")[0], "test_writer")
        self.assertEqual(classify_prompt("Fix the OAuth race condition")[0], "senior_reviewer")
        self.assertEqual(classify_prompt("Rename x to count")[0], "primary")

    def test_recommend_route_handles_scope_and_explicit_delegation(self) -> None:
        self.assertEqual(
            recommend_route("Trace the complete authentication flow without modifying code")[0],
            "bulk_reader",
        )
        self.assertEqual(recommend_route("Fix the OAuth authorization bug")[0], "senior_reviewer")
        self.assertEqual(recommend_route("Find the Foo declaration")[0], "primary")
        self.assertEqual(
            recommend_route("Delegate finding the Foo declaration to a bulk reader")[0],
            "bulk_reader",
        )
        self.assertEqual(recommend_route("Delegate this to the code_writer")[0], "code_writer")
        self.assertEqual(recommend_route("Trace all callers of this handler")[0], "bulk_reader")
        self.assertEqual(
            recommend_route("Find the Foo declaration", benefit_gate_enabled=False)[0],
            "bulk_reader",
        )

    def test_same_model_worker_route_stays_primary_unless_explicit(self) -> None:
        self.assertEqual(
            recommend_route("Trace all callers", primary_model="gpt-5.6-terra")[0],
            "primary",
        )
        self.assertEqual(
            recommend_route(
                "Delegate tracing all callers to a bulk reader",
                primary_model="gpt-5.6-terra",
            )[0],
            "bulk_reader",
        )

    def test_exclusive_discovery_state_spans_hook_processes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            telemetry = root / "telemetry"

            def hook(event: str, model: str, **extra: object) -> dict:
                payload = {
                    "hook_event_name": event,
                    "session_id": "shared-session",
                    "cwd": str(root),
                    "model": model,
                    **extra,
                }
                output = run_hook(payload, telemetry).stdout
                return json.loads(output) if output.strip() else {}

            hook("UserPromptSubmit", "gpt-5.6-sol", prompt="Trace all callers")
            first = hook("PreToolUse", "gpt-5.6-sol", tool_name="Bash", tool_input={"command": "rg Foo ."})
            self.assertEqual(first["hookSpecificOutput"]["permissionDecision"], "deny")
            second = hook("PreToolUse", "gpt-5.6-sol", tool_name="Bash", tool_input={"command": "rg Foo ."})
            self.assertEqual(second["hookSpecificOutput"]["permissionDecision"], "deny")
            self.assertEqual(
                hook(
                    "PreToolUse",
                    "gpt-5.6-sol",
                    tool_name="functions.collaboration.spawn_agent",
                    tool_input={"agent_type": "bulk_reader", "prompt": "Trace all callers"},
                ),
                {},
            )
            started = hook("SubagentStart", "gpt-5.6-terra", agent_type="bulk_reader", agent_id="bulk-1")
            self.assertIn("four focused commands", started["hookSpecificOutput"]["additionalContext"])

            for _ in range(4):
                self.assertEqual(
                    hook("PreToolUse", "gpt-5.6-terra", agent_type="bulk_reader", agent_id="bulk-1", tool_name="Bash", tool_input={"command": "rg Foo ."}),
                    {},
                )
            capped = hook("PreToolUse", "gpt-5.6-terra", agent_type="bulk_reader", agent_id="bulk-1", tool_name="Bash", tool_input={"command": "rg Foo ."})
            self.assertEqual(capped["hookSpecificOutput"]["permissionDecision"], "deny")
            hook("SubagentStop", "gpt-5.6-terra", agent_type="bulk_reader", agent_id="bulk-1")
            for _ in range(1):
                self.assertEqual(
                    hook("PreToolUse", "gpt-5.6-sol", tool_name="Bash", tool_input={"command": "rg Foo ."}),
                    {},
                )
            verification_capped = hook("PreToolUse", "gpt-5.6-sol", tool_name="Bash", tool_input={"command": "rg Foo ."})
            self.assertEqual(verification_capped["hookSpecificOutput"]["permissionDecision"], "deny")
            duplicate = hook("SubagentStart", "gpt-5.6-terra", agent_type="bulk_reader")
            self.assertIn("late or duplicate", duplicate["hookSpecificOutput"]["additionalContext"])

    def test_large_read_only_blocks_simple_full_reads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            large = root / "large.py"
            large.write_text("line\n" * 501, encoding="utf-8")
            self.assertIsNotNone(find_large_full_read("cat large.py", root, 500))
            self.assertIsNone(find_large_full_read("cat large.py | rg symbol", root, 500))
            self.assertIsNone(find_large_full_read("sed -n '10,30p' large.py", root, 500))

    def test_read_guard_does_not_escape_repo(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            self.assertIsNone(find_large_full_read("cat /etc/hosts", Path(temporary), 1))

    def test_sensitive_patch_detection(self) -> None:
        patch = "*** Begin Patch\n*** Update File: src/auth/session.ts\n*** End Patch"
        paths = patch_paths(patch)
        self.assertEqual(paths, ["src/auth/session.ts"])
        self.assertTrue(sensitive_path(paths, ["auth", "payment"]))

    def test_prompt_hook_emits_no_prompt_or_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = {
                "hook_event_name": "UserPromptSubmit",
                "prompt": "Delegate adding unit tests for /company/private/project.py to a test_writer",
                "session_id": "private-session",
                "cwd": str(root),
                "model": "gpt-5.6-sol",
            }
            completed = run_hook(payload, root / "telemetry")
            output = json.loads(completed.stdout)
            self.assertIn("test_writer", output["hookSpecificOutput"]["additionalContext"])
            telemetry = (root / "telemetry" / "events.jsonl").read_text(encoding="utf-8")
            self.assertNotIn("/company/private", telemetry)
            self.assertNotIn("private-session", telemetry)
            self.assertIn("test_writer", telemetry)

    def test_pretool_hook_denies_large_full_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "large.ts").write_text("line\n" * 501, encoding="utf-8")
            payload = {
                "hook_event_name": "PreToolUse", "session_id": "s", "cwd": str(root),
                "model": "gpt-5.6-sol", "tool_name": "Bash",
                "tool_input": {"command": "cat large.ts"},
            }
            completed = run_hook(payload, root / "telemetry")
            output = json.loads(completed.stdout)
            specific = output["hookSpecificOutput"]
            self.assertEqual(specific["permissionDecision"], "deny")
            self.assertIn("bulk_reader", specific["permissionDecisionReason"])

    def test_pretool_hook_flags_sensitive_patch_without_exposing_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = {
                "hook_event_name": "PreToolUse", "session_id": "s", "cwd": str(root),
                "model": "gpt-5.6-sol", "tool_name": "apply_patch",
                "tool_input": {"command": "*** Begin Patch\n*** Update File: src/payment/private.ts\n*** End Patch"},
            }
            completed = run_hook(payload, root / "telemetry")
            output = json.loads(completed.stdout)
            self.assertIn("senior_reviewer", output["hookSpecificOutput"]["additionalContext"])
            telemetry = (root / "telemetry" / "events.jsonl").read_text(encoding="utf-8")
            self.assertNotIn("src/payment/private.ts", telemetry)


if __name__ == "__main__":
    unittest.main()
