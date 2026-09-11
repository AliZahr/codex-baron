from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from usage_meter import UsageSummary, build_comparison, parse_usage_file, render_meter  # noqa: E402


def write_jsonl(path: Path, *records: object) -> None:
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")


class UsageMeterTests(unittest.TestCase):
    def test_aggregates_multiple_turn_completed_events(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "run.jsonl"
            write_jsonl(
                path,
                {"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 30, "reasoning_output_tokens": 7}},
                {"type": "message.completed", "usage": {"input_tokens": 999}},
                {"type": "turn.completed", "usage": {"input_tokens": 40, "cached_input_tokens": 5, "output_tokens": 10, "reasoning_output_tokens": 3}},
            )
            usage = parse_usage_file(path)
            self.assertEqual(usage.turns, 2)
            self.assertEqual(usage.input_tokens, 140)
            self.assertEqual(usage.cached_input_tokens, 25)
            self.assertEqual(usage.output_tokens, 40)
            self.assertEqual(usage.reasoning_output_tokens, 10)
            self.assertEqual(usage.total_tokens, 180)

    def test_rejects_malformed_turn_usage_after_valid_usage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "run.jsonl"
            path.write_text(
                '{"type":"turn.completed","usage":{"input_tokens":5,"output_tokens":2}}\n'
                '{not json}\n'
                '{"type":"message.completed"}\n'
                '{"type":"turn.completed","usage":"bad"}\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "Malformed (usage|or truncated)"):
                parse_usage_file(path)

    def test_no_usage_raises_clear_value_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "empty.jsonl"
            write_jsonl(path, {"type": "message.completed"})
            with self.assertRaisesRegex(ValueError, "No usage"):
                parse_usage_file(path)

    def test_saved_case_reports_positive_savings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            baseline_path = Path(temporary) / "baseline.jsonl"
            baron_path = Path(temporary) / "baron.jsonl"
            write_jsonl(baseline_path, {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 50}})
            write_jsonl(baron_path, {"type": "turn.completed", "usage": {"input_tokens": 60, "output_tokens": 30}})
            comparison = build_comparison(parse_usage_file(baseline_path), parse_usage_file(baron_path))
            self.assertEqual(comparison["baseline_total_tokens"], 150)
            self.assertEqual(comparison["baron_total_tokens"], 90)
            self.assertEqual(comparison["difference_tokens"], 60)
            self.assertEqual(comparison["status"], "saved")
            self.assertIn("saved", render_meter(comparison).lower())

    def test_exceeded_case_reports_extra_usage(self) -> None:
        baseline = UsageSummary(input_tokens=60, output_tokens=40)
        baron = UsageSummary(input_tokens=75, output_tokens=50)
        comparison = build_comparison(baseline, baron)
        self.assertEqual(comparison["difference_tokens"], -25)
        self.assertEqual(comparison["status"], "exceeded")
        self.assertIn("exceeded", render_meter(comparison).lower())

    def test_zero_baseline_is_handled(self) -> None:
        baseline = UsageSummary()
        baron = UsageSummary(input_tokens=10)
        comparison = build_comparison(baseline, baron)
        self.assertIsNone(comparison["savings_percent"])
        self.assertEqual(comparison["status"], "exceeded")

    def test_reasoning_tokens_are_reported_not_double_counted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "run.jsonl"
            write_jsonl(path, {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5, "reasoning_output_tokens": 20}})
            usage = parse_usage_file(path)
            self.assertEqual(usage.reasoning_output_tokens, 20)
            self.assertEqual(usage.total_tokens, 15)

    def test_command_and_agent_lifecycle_counts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "run.jsonl"
            write_jsonl(
                path,
                {"type": "item.started", "item": {"type": "command_execution"}},
                {"type": "item.completed", "item": {"type": "command_execution", "status": "completed"}},
                {"type": "item.started", "item": {"type": "command_execution"}},
                {"type": "item.completed", "item": {"type": "command_execution", "status": "failed"}},
                {"type": "item.completed", "item": {"type": "agent_message"}},
                {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}},
            )
            usage = parse_usage_file(path)
            self.assertEqual((usage.commands_started, usage.commands_completed, usage.commands_failed), (2, 1, 1))
            self.assertEqual(usage.agent_messages, 1)

    def test_release_gate_rejects_duplicate_or_incomplete_collaboration_lifecycle(self) -> None:
        cases = (
            (
                {"type": "item.completed", "item": {"id": "c", "type": "collab_tool_call", "status": "completed"}},
                {"type": "item.completed", "item": {"id": "c", "type": "collab_tool_call", "status": "failed"}},
            ),
            (
                {"type": "item.started", "item": {"id": "c", "type": "collab_tool_call", "status": "in_progress"}},
            ),
        )
        for records in cases:
            with self.subTest(records=records), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "run.jsonl"
                write_jsonl(
                    path,
                    *records,
                    {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}},
                )
                with self.assertRaisesRegex(ValueError, "lifecycle identities"):
                    parse_usage_file(path, release_gate=True)

    def test_collaboration_tool_calls_are_included_in_command_accounting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "collaboration.jsonl"
            write_jsonl(
                path,
                {
                    "type": "item.started",
                    "item": {
                        "id": "spawn-1",
                        "type": "collab_tool_call",
                        "tool": "spawn_agent",
                        "task_name": "bulk_reader",
                        "agent_type": "default",
                    },
                },
                {
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
                },
                {"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 2}},
            )
            usage = parse_usage_file(path, release_gate=True)
            self.assertEqual(usage.commands_started, 1)
            self.assertEqual(usage.commands_completed, 1)
            self.assertEqual(usage.commands_failed, 0)

    def test_rejects_concatenation_after_an_incomplete_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "run.jsonl"
            path.write_text(
                '{"type":"item.completed","item":{"id":"item_1","type":"command_execution",'
                '"aggregated_output":"truncated'
                '{"type":"item.started","item":{"id":"item_2","type":"command_execution",'
                '"status":"in_progress"}}\n'
                '{"type":"item.completed","item":{"id":"item_2","type":"command_execution",'
                '"exit_code":0,"status":"completed"}}\n'
                '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":2}}\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "Malformed or truncated"):
                parse_usage_file(path)

    def test_parses_multiple_complete_objects_on_one_line(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "run.jsonl"
            path.write_text(
                '{"item":{"id":"cmd","type":"command_execution","status":"completed"},"type":"item.completed"}'
                '{"usage":{"input_tokens":5,"output_tokens":2},"type":"turn.completed"}\n',
                encoding="utf-8",
            )
            usage = parse_usage_file(path, release_gate=True)
            self.assertEqual(usage.total_tokens, 7)
            self.assertEqual(usage.commands_started, 1)

    def test_rejects_malformed_turn_usage_to_prevent_token_undercount(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "run.jsonl"
            path.write_text(
                '{"type":"turn.completed","usage":{"input_tokens":10'
                '{"type":"turn.completed","usage":{"input_tokens":5,"output_tokens":2}}\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "Malformed (usage event|or truncated)"):
                parse_usage_file(path)

    def test_release_gate_rejects_reordered_malformed_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "run.jsonl"
            path.write_text(
                '{"metadata":true,"type":"turn.completed","usage":{"input_tokens":999\n'
                '{"usage":{"input_tokens":5,"output_tokens":2},"type":"turn.completed"}\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "Malformed or truncated"):
                parse_usage_file(path, release_gate=True)

    def test_lifecycle_key_order_does_not_change_command_count(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "run.jsonl"
            write_jsonl(
                path,
                {"item": {"status": "completed", "type": "command_execution", "id": "cmd"}, "type": "item.completed"},
                {"usage": {"input_tokens": 5, "output_tokens": 2}, "type": "turn.completed"},
            )
            self.assertEqual(parse_usage_file(path, release_gate=True).commands_started, 1)

    def test_cli_release_gates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = root / "baseline.jsonl"
            baron = root / "baron.jsonl"
            write_jsonl(baseline, {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 20}})
            write_jsonl(baron, {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 20}})
            command = [sys.executable, str(ROOT / "scripts" / "usage_meter.py"), str(baseline), str(baron)]
            gated = subprocess.run(command + ["--require-savings"], capture_output=True, text=True)
            self.assertEqual(gated.returncode, 1)
            self.assertIn("Release gate failed", gated.stderr)
            allowed = subprocess.run(command + ["--max-command-increase", "0"], capture_output=True, text=True)
            self.assertEqual(allowed.returncode, 0)


if __name__ == "__main__":
    unittest.main()
