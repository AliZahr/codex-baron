from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import configure_repo  # noqa: E402


def make_plugin(root: Path, version: str, marker: str) -> Path:
    (root / "agents").mkdir(parents=True)
    (root / "config").mkdir()
    (root / ".codex-plugin").mkdir()
    (root / "agents" / "bulk_reader.toml").write_text(
        f'name = "bulk_reader"\nmarker = "{marker}"\n',
        encoding="utf-8",
    )
    (root / "config" / "router.json").write_text(
        json.dumps({"version": 1, "marker": marker}) + "\n",
        encoding="utf-8",
    )
    (root / ".codex-plugin" / "plugin.json").write_text(
        json.dumps({"name": "codex-baron", "version": version}) + "\n",
        encoding="utf-8",
    )
    return root


def quiet_main(arguments: list[str]) -> int:
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        return configure_repo.main(arguments)


class InstallerAdversarialTests(unittest.TestCase):
    def test_symlinked_agents_directory_cannot_escape_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repo"
            outside = root / "outside"
            repo.mkdir()
            outside.mkdir()
            (repo / ".codex").mkdir()
            (repo / ".codex" / "agents").symlink_to(outside, target_is_directory=True)

            self.assertEqual(quiet_main([str(repo)]), 2)
            self.assertEqual(list(outside.iterdir()), [])
            self.assertFalse((repo / ".codex" / "codex-baron.json").exists())

    def test_partial_install_failure_rolls_back_every_managed_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary) / "repo"
            repo.mkdir()
            original = configure_repo._atomic_write
            failed = False

            def fail_once(repo_path, relative, data, mode):
                nonlocal failed
                if relative.name == "senior_reviewer.toml" and not failed:
                    failed = True
                    raise configure_repo.InstallError("injected write failure")
                return original(repo_path, relative, data, mode)

            with patch.object(configure_repo, "_atomic_write", side_effect=fail_once):
                self.assertEqual(quiet_main([str(repo)]), 2)

            self.assertFalse(any((repo / ".codex" / "agents").glob("*.toml")))
            self.assertFalse((repo / ".codex" / "codex-baron.json").exists())
            self.assertFalse((repo / ".codex" / "codex-baron-managed.json").exists())

    def test_v1_to_v2_upgrade_replaces_only_prior_unchanged_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repo with spaces"
            repo.mkdir()
            v1 = make_plugin(root / "plugin-v1", "1.0.0", "v1")
            v2 = make_plugin(root / "plugin-v2", "2.0.0", "v2")

            with patch.object(configure_repo, "PLUGIN_ROOT", v1):
                self.assertEqual(quiet_main([str(repo)]), 0)
            with patch.object(configure_repo, "PLUGIN_ROOT", v2):
                self.assertEqual(quiet_main([str(repo)]), 0)

            self.assertIn("v2", (repo / ".codex" / "agents" / "bulk_reader.toml").read_text())
            self.assertEqual(
                json.loads((repo / ".codex" / "codex-baron.json").read_text())["marker"],
                "v2",
            )
            manifest_text = (repo / ".codex" / "codex-baron-managed.json").read_text()
            manifest = json.loads(manifest_text)
            self.assertEqual(manifest["plugin"]["version"], "2.0.0")
            self.assertNotIn(str(repo), manifest_text)

    def test_customization_is_preserved_and_blocks_entire_upgrade(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repo"
            repo.mkdir()
            v1 = make_plugin(root / "plugin-v1", "1.0.0", "v1")
            v2 = make_plugin(root / "plugin-v2", "2.0.0", "v2")

            with patch.object(configure_repo, "PLUGIN_ROOT", v1):
                self.assertEqual(quiet_main([str(repo)]), 0)
            profile = repo / ".codex" / "agents" / "bulk_reader.toml"
            profile.write_text("user customization\n", encoding="utf-8")

            with patch.object(configure_repo, "PLUGIN_ROOT", v2):
                self.assertEqual(quiet_main([str(repo)]), 2)

            self.assertEqual(profile.read_text(encoding="utf-8"), "user customization\n")
            self.assertEqual(
                json.loads((repo / ".codex" / "codex-baron.json").read_text())["marker"],
                "v1",
            )
            manifest = json.loads((repo / ".codex" / "codex-baron-managed.json").read_text())
            self.assertEqual(manifest["plugin"]["version"], "1.0.0")

    def test_remove_uses_prior_manifest_digests_after_plugin_upgrade(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repo"
            repo.mkdir()
            v1 = make_plugin(root / "plugin-v1", "1.0.0", "v1")
            v2 = make_plugin(root / "plugin-v2", "2.0.0", "v2")

            with patch.object(configure_repo, "PLUGIN_ROOT", v1):
                self.assertEqual(quiet_main([str(repo)]), 0)
            with patch.object(configure_repo, "PLUGIN_ROOT", v2):
                self.assertEqual(quiet_main([str(repo), "--remove"]), 0)

            self.assertFalse((repo / ".codex" / "agents" / "bulk_reader.toml").exists())
            self.assertFalse((repo / ".codex" / "codex-baron.json").exists())
            self.assertFalse((repo / ".codex" / "codex-baron-managed.json").exists())

    def test_partial_remove_failure_restores_files_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary) / "repo"
            repo.mkdir()
            self.assertEqual(quiet_main([str(repo)]), 0)
            before = {
                path.relative_to(repo): path.read_bytes()
                for path in (repo / ".codex").rglob("*")
                if path.is_file()
            }
            original = configure_repo._safe_unlink
            failed = False

            def fail_once(repo_path, relative):
                nonlocal failed
                if relative.name == "code_writer.toml" and not failed:
                    failed = True
                    raise configure_repo.InstallError("injected unlink failure")
                return original(repo_path, relative)

            with patch.object(configure_repo, "_safe_unlink", side_effect=fail_once):
                self.assertEqual(quiet_main([str(repo), "--remove"]), 2)

            after = {
                path.relative_to(repo): path.read_bytes()
                for path in (repo / ".codex").rglob("*")
                if path.is_file()
            }
            self.assertEqual(after, before)

    def test_manifest_path_traversal_is_rejected_without_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repo"
            repo.mkdir()
            victim = repo / "victim.txt"
            victim.write_text("keep\n", encoding="utf-8")
            (repo / ".codex").mkdir()
            (repo / ".codex" / "codex-baron-managed.json").write_text(
                json.dumps({
                    "schema_version": 1,
                    "plugin": {"name": "codex-baron", "version": "1.0.0"},
                    "managed_files": {"../victim.txt": {"sha256": "0" * 64}},
                }),
                encoding="utf-8",
            )

            self.assertEqual(quiet_main([str(repo), "--remove", "--force"]), 2)
            self.assertEqual(victim.read_text(encoding="utf-8"), "keep\n")


if __name__ == "__main__":
    unittest.main()
