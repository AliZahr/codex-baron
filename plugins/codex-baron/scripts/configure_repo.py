#!/usr/bin/env python3
"""Install Codex Baron custom-agent profiles into a repository."""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import sys


PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", type=Path, help="Repository root to configure")
    parser.add_argument("--force", action="store_true", help="Replace existing router-owned agent files")
    parser.add_argument("--dry-run", action="store_true", help="Show actions without writing")
    args = parser.parse_args()
    repo = args.repo.expanduser().resolve()
    if not repo.is_dir():
        parser.error(f"repository directory does not exist: {repo}")
    destination = repo / ".codex" / "agents"
    sources = sorted((PLUGIN_ROOT / "agents").glob("*.toml"))
    conflicts = [destination / source.name for source in sources if (destination / source.name).exists() and not args.force]
    if conflicts:
        print("Refusing to overwrite existing files; pass --force after review:", file=sys.stderr)
        for conflict in conflicts:
            print(f"  {conflict}", file=sys.stderr)
        return 2
    for source in sources:
        target = destination / source.name
        print(f"install {source.name} -> {target}")
        if not args.dry_run:
            destination.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    override = repo / ".codex" / "codex-baron.json"
    if not override.exists():
        print(f"create optional config -> {override}")
        if not args.dry_run:
            override.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(PLUGIN_ROOT / "config" / "router.json", override)
    print("Configured. Review and commit .codex/ if these policies should apply to the whole team.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
