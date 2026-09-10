from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from router_hook import classify_prompt, find_large_full_read, patch_paths, sensitive_path  # noqa: E402


def run_hook(payload: dict, data_root: Path) -> subprocess.CompletedProcess[str]:
    env = {"PLUGIN_ROOT": str(ROOT), "PLUGIN_DATA": str(data_root)}
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
                "prompt": "Add unit tests for /company/private/project.py",
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
