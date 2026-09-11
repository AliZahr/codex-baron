#!/usr/bin/env python3
"""Portable command-line facade for Codex Baron prompt routing.

The hook remains the integration point for Codex.  This small facade is useful
for smoke checks and operators running it from any current working directory.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from router_hook import recommend_route  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--prompt")
    source.add_argument("--prompt-file", type=Path)
    parser.add_argument("--model", default=None)
    parser.add_argument("--no-benefit-gate", action="store_true")
    args = parser.parse_args(argv)
    try:
        prompt = args.prompt if args.prompt is not None else args.prompt_file.read_text(encoding="utf-8")
        route, reason = recommend_route(
            prompt,
            benefit_gate_enabled=not args.no_benefit_gate,
            primary_model=args.model,
        )
    except OSError as exc:
        print(json.dumps({"error": {"code": "input_error", "message": str(exc)}}), file=sys.stderr)
        return 2
    print(json.dumps({"route": route, "reason": reason}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
