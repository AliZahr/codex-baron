from pathlib import Path
import json
import shutil
import stat
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT.parents[1] / ".github/workflows/plugin-autorelease.yml"
sys.path.insert(0, str(ROOT / "scripts"))
import stamp_plugin  # noqa: E402


class StampPluginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name)
        shutil.copytree(ROOT, self.repo / "plugins/codex-baron")
        marketplace = self.repo / ".agents/plugins"
        marketplace.mkdir(parents=True)
        marketplace.joinpath("marketplace.json").write_text('{"name":"market"}\n')

    def tearDown(self):
        self.temp.cleanup()

    def test_content_fingerprint_and_idempotence(self):
        manifest = self.repo / "plugins/codex-baron/.codex-plugin/plugin.json"
        mode = manifest.stat().st_mode & 0o777
        self.assertTrue(stamp_plugin.stamp(self.repo))
        first = json.loads(manifest.read_text())["version"]
        self.assertFalse(stamp_plugin.stamp(self.repo))
        self.assertEqual(first, json.loads(manifest.read_text())["version"])
        self.assertEqual(mode, manifest.stat().st_mode & 0o777)
        before = stamp_plugin.fingerprint(self.repo)
        manifest.with_name("description.txt").write_text("content")
        self.assertNotEqual(before, stamp_plugin.fingerprint(self.repo))

    def test_marketplace_and_unrelated_changes(self):
        before = stamp_plugin.fingerprint(self.repo)
        (self.repo / ".agents/plugins/marketplace.json").write_text('{"name":"changed"}\n')
        self.assertNotEqual(before, stamp_plugin.fingerprint(self.repo))
        before = stamp_plugin.fingerprint(self.repo)
        (self.repo / "unrelated.txt").write_text("ignored")
        self.assertEqual(before, stamp_plugin.fingerprint(self.repo))

    def test_version_only_change_converges(self):
        manifest = self.repo / "plugins/codex-baron/.codex-plugin/plugin.json"
        original = json.loads(manifest.read_text())
        original["version"] = "1.0.0+codex.other"
        manifest.write_text(json.dumps(original))
        first = stamp_plugin.fingerprint(self.repo)
        original["version"] = "1.0.0+codex.different"
        manifest.write_text(json.dumps(original))
        self.assertEqual(first, stamp_plugin.fingerprint(self.repo))

    def test_manifest_metadata_and_missing_marketplace_fail_cleanly(self):
        manifest = self.repo / "plugins/codex-baron/.codex-plugin/plugin.json"
        original = json.loads(manifest.read_text())
        before = stamp_plugin.fingerprint(self.repo)
        original["description"] = "changed"
        manifest.write_text(json.dumps(original))
        self.assertNotEqual(before, stamp_plugin.fingerprint(self.repo))
        (self.repo / ".agents/plugins/marketplace.json").unlink()
        with self.assertRaises(ValueError):
            stamp_plugin.fingerprint(self.repo)

    def test_mode_change_and_symlink_fail_safely(self):
        script = self.repo / "plugins/codex-baron/scripts/usage_meter.py"
        before = stamp_plugin.fingerprint(self.repo)
        script.chmod(script.stat().st_mode ^ stat.S_IXUSR)
        self.assertNotEqual(before, stamp_plugin.fingerprint(self.repo))
        link = self.repo / "plugins/codex-baron/unsafe-link"
        link.symlink_to("README.md")
        with self.assertRaises(ValueError):
            stamp_plugin.fingerprint(self.repo)

    def test_workflow_keeps_repository_code_out_of_write_scoped_runner(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        prepare = workflow.split("  prepare:\n", 1)[1].split("  publish:\n", 1)[0]
        publish = workflow.split("  publish:\n", 1)[1]
        self.assertIn("permissions:\n      contents: read", prepare)
        self.assertIn("stamp_plugin.py", prepare)
        self.assertNotIn("github.token", prepare)
        self.assertIn("permissions:\n      contents: write", publish)
        self.assertNotIn("stamp_plugin.py", publish)
        self.assertEqual(1, publish.count("github.token"))
        self.assertIn("core.hooksPath=/dev/null", publish)
        self.assertIn("jq -cS 'del(.version)'", publish)
        self.assertIn('test "$prepared_base" = "$base_version"', publish)


if __name__ == "__main__":
    unittest.main()
