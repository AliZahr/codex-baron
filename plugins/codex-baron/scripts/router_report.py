#!/usr/bin/env python3
"""Summarize Codex Baron metadata-only JSONL telemetry."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("events", type=Path, help="Path to events.jsonl")
    args = parser.parse_args()
    routes: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    agents: Counter[str] = Counter()
    total = 0
    for line in args.events.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        total += 1
        if event.get("route"):
            routes[str(event["route"])] += 1
        if event.get("reason"):
            reasons[str(event["reason"])] += 1
        if event.get("agent_type"):
            agents[str(event["agent_type"])] += 1
    print(f"events: {total}")
    for title, values in (("routes", routes), ("agents", agents), ("reasons", reasons)):
        print(f"\n{title}:")
        for name, count in values.most_common():
            print(f"  {count:6d}  {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
