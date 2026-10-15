"""Regression checks for the standalone Architect Release bundle."""

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

try:
    from . import release_architect_bundle as release
except ImportError:
    import release_architect_bundle as release


class ReleaseBundleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="architect-release-test-")
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_build_check_directory_and_zip(self):
        bundle = self.root / "bundle"
        archive = self.root / "architect-bundle-3.2.1.zip"
        result = release.build(str(bundle), "3.2.1", "unknown", str(archive))
        self.assertEqual(result["sourceRepository"], release.CANONICAL_REPOSITORY)
        self.assertEqual(result["assetName"], "architect-bundle-3.2.1.zip")
        self.assertEqual(release.check(str(bundle))["files"], result["files"])
        self.assertEqual(release.check(str(archive))["bundleVersion"], "3.2.1")
        runtime = json.loads((bundle / ".agent/architect/tools/release.json").read_text(encoding="utf-8"))
        for name, expected in runtime["files"].items():
            self.assertEqual(release.digest((bundle / ".agent/architect/tools" / name).read_bytes()), expected)

    def test_check_rejects_hash_and_duplicate_manifest_keys(self):
        bundle = self.root / "bundle"
        release.build(str(bundle), "3.2.1", "unknown", None)
        path = bundle / ".agent/architect/tools/agent_scaffold.py"
        path.write_bytes(path.read_bytes() + b"\nchanged\n")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            release.check(str(bundle))
        release.build(str(self.root / "clean"), "3.2.1", "unknown", None)
        bundle = self.root / "clean"
        manifest = json.loads((bundle / release.MANIFEST_NAME).read_text(encoding="utf-8"))
        manifest["files"]["../escape.py"] = "0" * 64
        (bundle / release.MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(ValueError):
            release.check(str(bundle))

    def test_zip_rejects_path_escape(self):
        archive = self.root / "bad.zip"
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr("../escape.py", b"bad")
        with self.assertRaisesRegex(ValueError, "bundle path escapes"):
            release.check(str(archive))
        directory_archive = self.root / "bad-directory.zip"
        with zipfile.ZipFile(directory_archive, "w") as zipped:
            zipped.writestr("../escape.py/", b"")
        with self.assertRaisesRegex(ValueError, "bundle path escapes"):
            release.check(str(directory_archive))

    def test_same_sources_have_identical_portable_payloads(self):
        first = release.build(str(self.root / "windows workspace"), "3.2.0", "unknown", None)
        second = release.build(str(self.root / "macos workspace"), "3.2.0", "unknown", None)
        self.assertEqual(first["files"], second["files"])
        paths = set(first["files"])
        self.assertFalse(any(path.endswith((".bat", ".cmd", ".ps1", ".command")) for path in paths))
        architect = release.architect_skill_bytes().decode("utf-8")
        self.assertNotIn("{baseDir}/tools", architect)
        self.assertIn("python3", architect)
        self.assertIn("py -3", architect)

    def test_unlisted_platform_launcher_is_rejected(self):
        bundle = self.root / "bundle"
        release.build(str(bundle), "3.2.0", "unknown", None)
        for name in ("agent_scaffold.bat", "agent_scaffold.command"):
            with self.subTest(name=name):
                extra = bundle / ".agent/architect/tools" / name
                extra.write_bytes(b"unlisted launcher")
                try:
                    with self.assertRaisesRegex(ValueError, "outside its manifest"):
                        release.check(str(bundle))
                finally:
                    extra.unlink()

    def test_dirty_source_is_not_labeled_with_an_old_commit(self):
        calls = []

        def git(argv, **kwargs):
            calls.append(argv)
            output = " M tools/agent_scaffold.py\n" if "status" in argv else "a" * 40
            return subprocess.CompletedProcess(argv, 0, output, "")

        with mock.patch.object(release.subprocess, "run", side_effect=git):
            self.assertEqual(release.source_commit(), "unknown")
        self.assertIn("tools/agent_asset_migration.py", calls[0])

    def test_clean_source_retains_a_real_commit(self):
        def git(argv, **kwargs):
            output = "" if "status" in argv else "b" * 40
            return subprocess.CompletedProcess(argv, 0, output, "")

        with mock.patch.object(release.subprocess, "run", side_effect=git):
            self.assertEqual(release.source_commit(), "b" * 40)

    def test_standalone_skill_runs_without_the_development_repository(self):
        skill = self.root / "installed-skill"
        canonical = release.ROOT / ".agents/skills/agent-scaffold-maintenance"
        for rel in ("SKILL.md", "tools/agent_scaffold.py", "tools/validate_scaffold.py",
                    "tools/agent_evaluation.py", "tools/agent_asset_migration.py",
                    "tools/scaffold-evaluate.md", "tools/release.json"):
            target = skill / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(canonical / rel, target)
        cli = skill / "tools/agent_scaffold.py"
        result = subprocess.run([sys.executable, "-E", "-B", str(cli), "--help"],
                                cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        for command in ("inspect", "check", "diff", "apply", "verify", "restore", "purge",
                        "pack", "package-check", "install", "upgrade", "preflight", "self-update"):
            self.assertIn(command, result.stdout)
        document = (skill / "SKILL.md").read_text(encoding="utf-8")
        self.assertNotIn("../../../docs/", document)
        self.assertNotIn("python tools/sync_scaffold_skill.py", document)
        self.assertNotIn("python tools/release_architect_bundle.py", document)
        result = subprocess.run([sys.executable, "-E", "-B", str(skill / "tools/agent_evaluation.py"), "--help"], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((skill / "tools/scaffold-evaluate.md").read_bytes(), (release.ROOT / ".agents/skills/scaffold-evaluate/SKILL.md").read_bytes())

    def test_build_uses_the_supplied_skill_snapshot(self):
        snapshot = self.root / "snapshot"
        canonical = release.ROOT / ".agents/skills/agent-scaffold-maintenance"
        for rel in ("SKILL.md", "tools/agent_scaffold.py", "tools/validate_scaffold.py",
                    "tools/agent_evaluation.py", "tools/agent_asset_migration.py",
                    "tools/scaffold-evaluate.md"):
            target = snapshot / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(canonical / rel, target)
        cli = snapshot / "tools/agent_scaffold.py"
        cli.write_bytes(cli.read_bytes() + b"\n# isolated publication snapshot\n")
        bundle = self.root / "bundle"
        result = release.build(str(bundle), "3.2.1", "a" * 40, None, str(snapshot))
        self.assertEqual((bundle / ".agent/architect/tools/agent_scaffold.py").read_bytes(), cli.read_bytes())
        self.assertEqual(result["sourceCommit"], "a" * 40)
        self.assertEqual(release.check(str(bundle))["files"], result["files"])
        manifest = json.loads((bundle / release.MANIFEST_NAME).read_text(encoding="utf-8"))
        self.assertEqual(manifest["evaluationVersion"], "1")
        self.assertIn("evaluationSkill", manifest["entrypoints"])
        self.assertIn("evaluationTool", manifest["entrypoints"])
        self.assertEqual(manifest["migrationVersion"], "1")
        self.assertEqual(manifest["entrypoints"]["migrationTool"], ".agent/architect/tools/agent_asset_migration.py")
        self.assertIn(".agent/architect/tools/agent_evaluation.py", manifest["protectedFiles"])
        self.assertIn(".agent/architect/tools/agent_asset_migration.py", manifest["protectedFiles"])
        self.assertNotIn("scaffold-evaluation.md", manifest["files"])
        self.assertNotIn("scaffold-score-history.md", manifest["files"])


if __name__ == "__main__":
    unittest.main()
