#!/usr/bin/env python3
"""Run deterministic classifier and large-read-guard benchmark scenarios."""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from router_hook import classify_prompt, find_large_full_read  # noqa: E402


SCENARIOS = [
    ("Find every caller of the checkout service", "bulk_reader"),
    ("Generate a typed configuration stub", "code_writer"),
    ("Add unit tests and fixtures for retries", "test_writer"),
    ("Debug a race condition in payment authorization", "senior_reviewer"),
    ("Rename this local variable", "primary"),
]


def main() -> int:
    correct = 0
    print("prompt-routing benchmark")
    for prompt, expected in SCENARIOS:
        actual, reason = classify_prompt(prompt)
        ok = actual == expected
        correct += int(ok)
        print(f"  {'PASS' if ok else 'FAIL'} {expected:16s} <- {prompt} ({reason})")
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        large = root / "large.py"
        large.write_text("x\n" * 501, encoding="utf-8")
        blocked = find_large_full_read("cat large.py", root, 500) is not None
        targeted = find_large_full_read("cat large.py | rg needle", root, 500) is None
    print(f"\nlarge-read guard: {'PASS' if blocked and targeted else 'FAIL'}")
    print(f"classifier accuracy: {correct}/{len(SCENARIOS)}")
    return 0 if correct == len(SCENARIOS) and blocked and targeted else 1


if __name__ == "__main__":
    raise SystemExit(main())
