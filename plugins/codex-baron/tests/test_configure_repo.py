from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import configure_repo  # noqa: E402


class ConfigureRepoTests(unittest.TestCase):
    def test_install_is_idempotent_and_remove_is_reversible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary) / "repo with spaces"
            repo.mkdir()
            self.assertEqual(configure_repo.main([str(repo)]), 0)
            self.assertEqual(configure_repo.main([str(repo)]), 0)
            installed = sorted((repo / ".codex" / "agents").glob("*.toml"))
            self.assertEqual(len(installed), 4)
            self.assertTrue((repo / ".codex" / "codex-baron.json").is_file())
            managed_manifest = repo / ".codex" / "codex-baron-managed.json"
            self.assertTrue(managed_manifest.is_file())
            self.assertEqual(managed_manifest.stat().st_mode & 0o777, 0o600)
            self.assertEqual(configure_repo.main([str(repo), "--remove"]), 0)
            self.assertFalse(any(path.exists() for path in installed))
            self.assertFalse((repo / ".codex" / "codex-baron.json").exists())
            self.assertFalse(managed_manifest.exists())

    def test_modified_managed_file_requires_force_for_install_or_remove(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary) / "repo"
            repo.mkdir()
            self.assertEqual(configure_repo.main([str(repo)]), 0)
            target = repo / ".codex" / "agents" / "bulk_reader.toml"
            target.write_text("user-owned\n", encoding="utf-8")
            self.assertEqual(configure_repo.main([str(repo)]), 2)
            self.assertEqual(configure_repo.main([str(repo), "--remove"]), 2)
            self.assertEqual(target.read_text(encoding="utf-8"), "user-owned\n")
            self.assertEqual(configure_repo.main([str(repo), "--force"]), 0)
            self.assertNotEqual(target.read_text(encoding="utf-8"), "user-owned\n")

    def test_dry_run_has_no_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary) / "repo"
            repo.mkdir()
            self.assertEqual(configure_repo.main([str(repo), "--dry-run"]), 0)
            self.assertFalse((repo / ".codex").exists())

    def test_missing_profiles_fail_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary) / "repo"
            repo.mkdir()
            empty_plugin = Path(temporary) / "empty-plugin"
            empty_plugin.mkdir()
            with patch.object(configure_repo, "PLUGIN_ROOT", empty_plugin):
                self.assertEqual(configure_repo.main([str(repo)]), 2)
            self.assertFalse((repo / ".codex").exists())


if __name__ == "__main__":
    unittest.main()
