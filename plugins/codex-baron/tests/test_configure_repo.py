from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import configure_repo  # noqa: E402


class ConfigureRepoTests(unittest.TestCase):
    def _repo(self, temporary: str) -> Path:
        repo = Path(temporary) / "repo"
        repo.mkdir()
        return repo

    def test_defaults_match_bundled_files_byte_for_byte(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = self._repo(temporary)
            self.assertEqual(configure_repo.main([str(repo)]), 0)
            for source in (configure_repo.PLUGIN_ROOT / "agents").glob("*.toml"):
                installed = repo / ".codex" / "agents" / source.name
                self.assertEqual(installed.read_bytes(), source.read_bytes())
            self.assertEqual(
                (repo / ".codex" / "codex-baron.json").read_bytes(),
                (configure_repo.PLUGIN_ROOT / "config" / "router.json").read_bytes(),
            )

    def test_role_overrides_fast_and_bulk_reader_sync(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = self._repo(temporary)
            args = [
                str(repo), "--model", "bulk_reader=custom.model",
                "--model", "test_writer=custom_writer",
                "--reasoning", "code_writer=ultra", "--fast",
            ]
            self.assertEqual(configure_repo.main(args), 0)
            bulk = (repo / ".codex" / "agents" / "bulk_reader.toml").read_text()
            writer = (repo / ".codex" / "agents" / "test_writer.toml").read_text()
            self.assertIn('model = "custom.model"', bulk)
            self.assertIn('model = "custom_writer"', writer)
            self.assertIn(
                'model_reasoning_effort = "ultra"',
                (repo / ".codex" / "agents" / "code_writer.toml").read_text(),
            )
            for profile in (repo / ".codex" / "agents").glob("*.toml"):
                self.assertIn('service_tier = "fast"', profile.read_text())
            config = json.loads((repo / ".codex" / "codex-baron.json").read_text())
            self.assertEqual(config["bulk_reader_model"], "custom.model")

            self.assertEqual(configure_repo.main([str(repo), "--no-fast"]), 0)
            for profile in (repo / ".codex" / "agents").glob("*.toml"):
                self.assertNotIn("service_tier =", profile.read_text())
            self.assertEqual(
                (repo / ".codex" / "agents" / "bulk_reader.toml").read_bytes(),
                (configure_repo.PLUGIN_ROOT / "agents" / "bulk_reader.toml").read_bytes(),
            )
            self.assertEqual(
                (repo / ".codex" / "agents" / "code_writer.toml").read_bytes(),
                (configure_repo.PLUGIN_ROOT / "agents" / "code_writer.toml").read_bytes(),
            )

    def test_invalid_assignments_fail_argparse_style(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = self._repo(temporary)
            for option, value in (
                ("--model", "bulk_reader"),
                ("--model", "unknown=x"),
                ("--model", "bulk_reader="),
                ("--model", "bulk_reader=bad\"value"),
                ("--reasoning", "bulk_reader=unsupported"),
            ):
                with self.assertRaises(SystemExit) as failure:
                    configure_repo.main([str(repo), option, value])
                self.assertEqual(failure.exception.code, 2)

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
