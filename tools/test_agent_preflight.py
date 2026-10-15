"""Focused regression checks for canonical Release preflight and self-update."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

try:
    from .test_agent_distribution import make_formal
    from .test_agent_scaffold import load_maintenance_module
except ImportError:
    from test_agent_distribution import make_formal
    from test_agent_scaffold import load_maintenance_module


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="agent-preflight-")
        self.tmp = Path(self.directory.name)
        self.maintenance = load_maintenance_module()
        self.maintenance.BACKUP_ROOT = self.tmp / "backups"

    def tearDown(self):
        self.directory.cleanup()

    def formal(self, name: str, version: str, redacted: bool = True):
        root = self.tmp / name
        make_formal(root, version=version, redacted=redacted)
        return root

    def test_missing_manifest_is_unsupported_without_network_or_write(self):
        workspace = self.tmp / "empty"
        workspace.mkdir()
        before = sorted(workspace.rglob("*"))
        result, code = self.maintenance.preflight_operation(str(workspace), "https://example.invalid/release.zip")
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "unsupported")
        self.assertEqual(result["reason"], "manifest-missing")
        self.assertEqual(sorted(workspace.rglob("*")), before)

    def test_preflight_outdated_then_confirmed_update_handoff(self):
        target = self.formal("target", "1.0.0")
        incoming = self.formal("incoming", "2.0.0")
        manifest_path = incoming / self.maintenance.MANIFEST_FILE
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["source"] = self.maintenance.CANONICAL_REPOSITORY
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        user_file = target / "scaffold-description.md"
        user_bytes, user_mtime = user_file.read_bytes(), user_file.stat().st_mtime_ns
        result, code = self.maintenance.preflight_operation(str(target), str(incoming))
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "outdated")
        result, code = self.maintenance.self_update_operation(str(target), str(incoming), True)
        self.assertEqual(code, 0, result)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["handoff"]["oldVersion"], "1.0.0")
        self.assertEqual(result["handoff"]["newVersion"], "2.0.0")
        self.assertTrue(result["handoff"]["reloaded"])
        self.assertEqual((user_file.read_bytes(), user_file.stat().st_mtime_ns), (user_bytes, user_mtime))

    def test_current_candidate_upgrade_and_restore_preserves_user_assets(self):
        try:
            from . import release_architect_bundle as release
        except ImportError:
            import release_architect_bundle as release
        from unittest import mock
        target = self.formal("candidate target \u6d4b\u8bd5", "3.1.0")
        bundle = self.tmp / "candidate-3.2.0"
        release.build(str(bundle), "3.2.0", "unknown", None)
        assets = ("AGENTS.md", "scaffold-description.md", ".agent/skills/quiet-check.md",
                  "memory/context.md", "project.txt", "books/domain.txt",
                  "scaffold-evaluation.md", "scaffold-score-history.md")
        for relative in assets[3:]:
            path = target / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"user-owned bytes\r\n")
        original = {relative: ((target / relative).read_bytes(), (target / relative).stat().st_mtime_ns)
                    for relative in assets}
        original_tree = {path.relative_to(target).as_posix(): path.read_bytes()
                         for path in target.rglob("*") if path.is_file()}

        result, code = self.maintenance.preflight_operation(str(target), str(bundle))
        self.assertEqual((code, result["status"]), (0, "outdated"), result)
        preview, code = self.maintenance.self_update_operation(str(target), str(bundle), False)
        self.assertEqual((code, preview["status"]), (0, "needs-confirmation"), preview)
        self.assertEqual(original_tree, {path.relative_to(target).as_posix(): path.read_bytes()
                                       for path in target.rglob("*") if path.is_file()})
        with mock.patch.object(self.maintenance.subprocess, "run", wraps=subprocess.run) as commands:
            updated, code = self.maintenance.self_update_operation(str(target), str(bundle), True)
        self.assertEqual(code, 0, updated)
        self.assertEqual(updated["handoff"]["newVersion"], "3.2.0")
        self.assertEqual(updated["handoff"]["verification"]["delivery"]["status"], "complete")
        handoff_cli = str(target / ".agent/architect/tools/agent_scaffold.py")
        handoff = [call.args[0] for call in commands.call_args_list if handoff_cli in call.args[0]]
        self.assertTrue(handoff)
        self.assertTrue(all(command[0] == sys.executable for command in handoff))
        self.assertEqual(original, {relative: ((target / relative).read_bytes(), (target / relative).stat().st_mtime_ns)
                                    for relative in assets})

        restored, code = self.maintenance.run("restore", str(target), None, False, updated["backup"]["id"])
        self.assertEqual(code, 0, restored)
        self.assertEqual(original, {relative: ((target / relative).read_bytes(), (target / relative).stat().st_mtime_ns)
                                    for relative in assets})
        self.assertEqual(self.maintenance.load_package_manifest(str(target))["packageVersion"], "3.1.0")
        self.assertEqual(original_tree, {path.relative_to(target).as_posix(): path.read_bytes()
                                       for path in target.rglob("*") if path.is_file()})
        verified, code = self.maintenance.run("verify", str(target), None, False, None)
        self.assertEqual((code, verified["status"]), (0, "ok"), verified)

    def test_preflight_detects_protected_conflict_without_writing(self):
        target = self.formal("target", "1.0.0")
        incoming = self.formal("incoming", "2.0.0")
        cli = target / ".agent/architect/tools/agent_scaffold.py"
        original = cli.read_bytes()
        cli.write_bytes(original + b"\nlocal edit\n")
        result, code = self.maintenance.preflight_operation(str(target), str(incoming))
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "conflict")
        self.assertTrue(any(item["path"] == ".agent/architect/tools/agent_scaffold.py" for item in result["differences"]))
        self.assertEqual(cli.read_bytes(), original + b"\nlocal edit\n")

    def test_preflight_detects_migration_tool_modification_without_writing(self):
        target = self.formal("target", "1.0.0")
        incoming = self.formal("incoming", "2.0.0")
        migration = target / ".agent/architect/tools/agent_asset_migration.py"
        before = migration.read_bytes()
        migration.write_bytes(before + b"\nlocal edit\n")
        result, code = self.maintenance.preflight_operation(str(target), str(incoming))
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "conflict")
        self.assertTrue(any(item["path"].endswith("agent_asset_migration.py")
                            for item in result["differences"]))
        self.assertEqual(migration.read_bytes(), before + b"\nlocal edit\n")

    def test_https_and_zip_safety_guards(self):
        with self.assertRaises(self.maintenance.PlanError) as non_https:
            self.maintenance._release_url("http://github.com/heibaoxia/My-Personal-Agent/releases/latest", api=True)
        self.assertEqual(non_https.exception.code, "RELEASE_SOURCE_INVALID")
        archive = self.tmp / "escape.zip"
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr("../escape.txt", "bad")
        with self.assertRaises(self.maintenance.PlanError) as escaped:
            self.maintenance._extract_release_zip(archive.read_bytes(), str(self.tmp / "stage"))
        self.assertIn(escaped.exception.code, {"PLAN_PATH_ESCAPE", "PACKAGE_SOURCE_FAILED"})

    def test_standalone_release_bundle_updates_formal_target(self):
        target = self.formal("target", "1.0.0", redacted=False)
        description = target / "scaffold-description.md"
        original = description.read_bytes(), description.stat().st_mtime_ns
        bundle = self.tmp / "bundle"
        archive = self.tmp / "architect-bundle-2.0.0.zip"
        subprocess.run([
            sys.executable, "-B", "tools/release_architect_bundle.py", "build",
            "--output", str(bundle), "--version", "2.0.0", "--zip", str(archive),
        ], check=True, capture_output=True, cwd=Path(__file__).resolve().parents[1])
        result, code = self.maintenance.preflight_operation(str(target), str(archive))
        self.assertEqual(code, 0, result)
        self.assertEqual(result["status"], "outdated")
        result, code = self.maintenance.self_update_operation(str(target), str(archive), True)
        self.assertEqual(code, 0, result)
        self.assertEqual(result["handoff"]["newVersion"], "2.0.0")
        self.assertEqual(self.maintenance.formal_delivery(str(target))["status"], "complete")
        self.assertEqual((description.read_bytes(), description.stat().st_mtime_ns), original)
        manifest = self.maintenance.load_package_manifest(str(target))
        release_path = ".agent/architect/tools/release.json"
        self.assertIn(release_path, manifest["protectedFiles"])
        release = json.loads((target / release_path).read_text(encoding="utf-8"))
        for name, expected in release["files"].items():
            self.assertEqual(self.maintenance.current_hash(str(target), ".agent/architect/tools/" + name), expected)
        skill = (target / ".agent/skills/agent-architect.md").read_text(encoding="utf-8")
        self.assertNotIn("{baseDir}/tools", skill)
        result, code = self.maintenance.preflight_operation(str(target), str(bundle))
        self.assertEqual((code, result["status"]), (0, "up-to-date"), result)

    def test_legacy_upgrade_adds_evaluation_and_preserves_artifacts_on_restore(self):
        try:
            from . import release_architect_bundle as release
        except ImportError:
            import release_architect_bundle as release
        target = self.tmp / "legacy"
        make_formal(target, evaluation=False)
        artifacts = ("scaffold-evaluation.md", "scaffold-score-history.md")
        for name in artifacts:
            (target / name).write_bytes((name + " private result\r\n").encode())
        before = {name: ((target / name).read_bytes(), (target / name).stat().st_mtime_ns) for name in artifacts}
        original_entry = (target / "AGENTS.md").read_bytes()
        bundle = self.tmp / "new-bundle"
        release.build(str(bundle), "3.2.1", "unknown", None)
        self.assertEqual(self.maintenance.formal_delivery(str(target))["status"], "legacy")
        result, code = self.maintenance.preflight_operation(str(target), str(bundle))
        self.assertEqual((code, result["status"]), (0, "outdated"), result)
        result, code = self.maintenance.self_update_operation(str(target), str(bundle), True)
        self.assertEqual(code, 0, result)
        self.assertEqual(result["handoff"]["verification"]["delivery"]["status"], "complete")
        self.assertEqual(self.maintenance.load_package_manifest(str(target))["entrypoints"]["evaluationSkill"], "scaffold-evaluate")
        self.assertTrue((target / ".agent/skills/scaffold-evaluate.md").is_file())
        self.assertEqual(before, {name: ((target / name).read_bytes(), (target / name).stat().st_mtime_ns) for name in artifacts})
        restored, code = self.maintenance.run("restore", str(target), None, False, result["backup"]["id"])
        self.assertEqual(code, 0, restored)
        self.assertEqual((target / "AGENTS.md").read_bytes(), original_entry)
        self.assertEqual(before, {name: ((target / name).read_bytes(), (target / name).stat().st_mtime_ns) for name in artifacts})

    def test_evaluation_modification_and_unregistered_path_block_update(self):
        try:
            from . import release_architect_bundle as release
        except ImportError:
            import release_architect_bundle as release
        bundle = self.tmp / "new-bundle"
        release.build(str(bundle), "3.2.1", "unknown", None)
        for evaluation in (True, False):
            target = self.tmp / str(evaluation)
            make_formal(target, evaluation=evaluation)
            skill = target / ".agent/skills/scaffold-evaluate.md"
            skill.write_bytes(b"local evaluation file")
            before = skill.read_bytes()
            result, code = self.maintenance.preflight_operation(str(target), str(bundle))
            self.assertEqual((code, result["status"]), (1, "conflict"), result)
            self.assertEqual(skill.read_bytes(), before)
            result, code = self.maintenance.self_update_operation(str(target), str(bundle), True)
            self.assertEqual((code, result["status"]), (1, "conflict"), result)
            self.assertEqual(skill.read_bytes(), before)

    def test_legacy_package_migration_path_collision_blocks_upgrade(self):
        target = self.formal("legacy-target", "1.0.0")
        manifest_path = target / self.maintenance.MANIFEST_FILE
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        migration = ".agent/architect/tools/agent_asset_migration.py"
        manifest.pop("migrationVersion")
        manifest["files"].pop(migration)
        manifest["protectedFiles"].remove(migration)
        manifest["entrypoints"].pop("migrationTool")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        migration_path = target / migration
        migration_path.unlink()
        migration_path.write_text("# user-owned file\n", encoding="utf-8")
        incoming = self.formal("incoming", "2.0.0")

        result, code = self.maintenance.preflight_operation(str(target), str(incoming))

        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "conflict")
        self.assertTrue(any(item.get("path") == migration for item in result["errors"]))
        self.assertEqual(migration_path.read_text(encoding="utf-8"), "# user-owned file\n")


if __name__ == "__main__":
    unittest.main()
