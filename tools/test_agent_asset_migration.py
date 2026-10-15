"""Regression tests for the isolated, recoverable workspace asset migration CLI."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import errno
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools/agent_asset_migration.py"


def load_tool():
    spec = importlib.util.spec_from_file_location("agent_asset_migration_test", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class AssetMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agent-asset-migration-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "workspace"
        self.root.mkdir()
        self.tool = load_tool()
        self.tool.JOURNAL_ROOT = self.root.parent / "external-journals"

    def plan(self, *, source="projects/old", target="projects/new", files=None, links=None):
        source_root = self.root / source
        source_root.mkdir(parents=True, exist_ok=True)
        if files is None:
            files = {"README.md": b"# Old project\n", "notes/topic.md": b"topic\n"}
        for relative, data in files.items():
            path = source_root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        file_entries = [
            {"source": f"{source}/{relative}", "target": f"{target}/{relative}", "action": "move",
             "sha256": digest(data)}
            for relative, data in sorted(files.items())
        ]
        link_entries = []
        if links:
            for relative, (before, after) in links.items():
                path = self.root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(before)
                link_entries.append({"path": relative, "baselineSha256": digest(before),
                                     "content": after.decode("utf-8")})
        return {"schemaVersion": 1, "workspace": str(self.root.resolve()),
                "migrationRoot": ".", "sourceRoot": source, "targetRoot": target,
                "targetPolicy": "fail", "files": file_entries, "linkUpdates": link_entries}

    def test_check_and_diff_are_read_only_and_confirmation_is_plan_bound(self):
        plan = self.plan(links={"README.md": (
            b"# Old project\n", b"# New project\n")})
        checked = self.tool.check_plan(str(self.root), plan)
        self.assertEqual(checked["status"], "ok")
        self.assertEqual(checked["summary"]["fileCount"], 2)
        self.assertEqual(checked["summary"]["linkUpdateCount"], 1)
        diff = self.tool.diff_plan(str(self.root), plan)
        self.assertEqual(diff["status"], "ok")
        self.assertRegex(diff["confirmation"], r"^[0-9a-f]{64}$")
        self.assertTrue((self.root / "projects/old/README.md").exists())
        self.assertFalse((self.root / "projects/new").exists())
        self.assertEqual((self.root / "README.md").read_bytes(), b"# Old project\n")
        self.assertIn("-# Old project", diff["linkDiffs"][0]["diff"])
        self.assertIn("+# New project", diff["linkDiffs"][0]["diff"])

    def test_check_rejects_omitted_asset_target_collision_and_scope_escape(self):
        plan = self.plan(files={"one.md": b"one", "nested/two.md": b"two"})
        del plan["files"][0]
        result = self.tool.check_plan(str(self.root), plan)
        self.assertEqual(result["status"], "conflict")
        self.assertTrue(any(item["code"] == "ASSET_OMITTED" for item in result["errors"]))

        plan = self.plan(source="projects/collision-old", target="projects/collision-new",
                         files={"one.md": b"one"})
        target = self.root / "projects/collision-new/other.md"
        target.parent.mkdir(parents=True)
        target.write_text("user data", encoding="utf-8")
        result = self.tool.check_plan(str(self.root), plan)
        self.assertTrue(any(item["code"] == "TARGET_CONFLICT" for item in result["errors"]))
        self.assertEqual(target.read_text(encoding="utf-8"), "user data")

        plan = self.plan(files={"one.md": b"one"})
        plan["files"][0]["target"] = "../outside.md"
        result = self.tool.check_plan(str(self.root), plan)
        self.assertEqual(result["status"], "error")
        self.assertTrue(any(item["code"] == "PATH_INVALID" for item in result["errors"]))

    def test_plan_requires_explicit_move_actions_and_nonoverlapping_paths(self):
        plan = self.plan(files={"one.md": b"one", "nested/two.md": b"two"})
        plan["files"][0].pop("action")
        result = self.tool.check_plan(self.root, plan)
        self.assertEqual(result["errors"][0]["code"], "PLAN_INVALID")

        plan = self.plan(files={"one.md": b"one", "nested/two.md": b"two"})
        plan["files"][1]["target"] = plan["files"][0]["target"] + "/child.md"
        result = self.tool.check_plan(self.root, plan)
        self.assertEqual(result["errors"][0]["code"], "PLAN_PATH_OVERLAP")

    def test_check_rejects_omitted_relative_link_and_accepts_explicit_rewrite(self):
        before = b"See [project](projects/old/README.md)\n"
        after = b"See [project](projects/new/README.md)\n"
        plan = self.plan(links={"INDEX.md": (before, after)})
        del plan["linkUpdates"][0]
        result = self.tool.check_plan(str(self.root), plan)
        self.assertEqual(result["status"], "conflict")
        self.assertTrue(any(item["code"] == "LINK_UPDATE_OMITTED" and item["path"] == "INDEX.md"
                            for item in result["errors"]))

        plan["linkUpdates"] = [{"path": "INDEX.md", "baselineSha256": digest(before),
                                "content": after.decode("utf-8")}]
        checked = self.tool.check_plan(str(self.root), plan)
        self.assertEqual(checked["status"], "ok", checked)
        result = self.tool.apply_plan(str(self.root), plan, checked["confirmation"])
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual((self.root / "INDEX.md").read_bytes(), after)
        restored = self.tool.restore_migration(str(self.root), result["migrationId"])
        self.assertEqual(restored["status"], "restored", restored)
        self.assertEqual((self.root / "INDEX.md").read_bytes(), before)

    def test_check_rejects_symlinks_and_dotenv_without_following_or_reading(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (outside / "secret.md").write_text("secret", encoding="utf-8")
        source_link = self.root / "projects/old/link"
        source_link.parent.mkdir(parents=True, exist_ok=True)
        try:
            source_link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("directory symbolic links are unavailable")
        plan = self.plan(files={"README.md": b"readme"})
        result = self.tool.check_plan(str(self.root), plan)
        self.assertTrue(any(item["code"] == "SOURCE_SYMLINK" for item in result["errors"]))
        source_link.unlink()

        (self.root / "projects/old/.env").write_text("TOKEN=secret", encoding="utf-8")
        plan = self.plan(files={"README.md": b"readme"})
        result = self.tool.check_plan(str(self.root), plan)
        self.assertTrue(any(item["code"] == "SECRET_FILE" for item in result["errors"]))

    def test_apply_requires_exact_confirmation_and_stages_before_source_removal(self):
        plan = self.plan()
        checked = self.tool.check_plan(str(self.root), plan)
        pending = self.tool.apply_plan(str(self.root), plan, confirmation=None)
        self.assertEqual(pending["status"], "needs-confirmation")
        self.assertTrue((self.root / "projects/old/README.md").exists())
        self.assertFalse((self.root / "projects/new").exists())

        result = self.tool.apply_plan(str(self.root), plan, confirmation="0" * 64)
        self.assertEqual(result["status"], "error")
        self.assertTrue((self.root / "projects/old/README.md").exists())
        self.assertFalse((self.root / "projects/new").exists())

        result = self.tool.apply_plan(str(self.root), plan, confirmation=checked["confirmation"])
        self.assertEqual(result["status"], "completed", result)
        self.assertFalse((self.root / "projects/old/README.md").exists())
        self.assertEqual((self.root / "projects/new/README.md").read_bytes(), b"# Old project\n")
        self.assertEqual((self.root / "projects/new/notes/topic.md").read_bytes(), b"topic\n")
        self.assertTrue(Path(result["journal"]).is_relative_to(self.tool.JOURNAL_ROOT))

    def test_apply_aborts_all_writes_if_any_source_baseline_changed(self):
        plan = self.plan()
        confirmation = self.tool.check_plan(str(self.root), plan)["confirmation"]
        (self.root / "projects/old/notes/topic.md").write_text("concurrent", encoding="utf-8")
        result = self.tool.apply_plan(str(self.root), plan, confirmation=confirmation)
        self.assertEqual(result["status"], "conflict")
        self.assertFalse((self.root / "projects/new").exists())
        self.assertEqual((self.root / "projects/old/notes/topic.md").read_text(encoding="utf-8"), "concurrent")

    def test_source_change_during_staging_blocks_removal_and_preserves_concurrent_bytes(self):
        plan = self.plan()
        confirmation = self.tool.check_plan(str(self.root), plan)["confirmation"]
        original = self.tool._write_bytes
        calls = {"count": 0}

        def change_source_after_copy(root, relative, content, **kwargs):
            result = original(root, relative, content, **kwargs)
            if relative.startswith("projects/new/"):
                calls["count"] += 1
                if calls["count"] == 1:
                    (self.root / "projects/old/notes/topic.md").write_bytes(b"concurrent source edit")
            return result

        with patch.object(self.tool, "_write_bytes", side_effect=change_source_after_copy):
            result = self.tool.apply_plan(str(self.root), plan, confirmation)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["recovery"], "partial")
        self.assertTrue(any(item["code"] == "SOURCE_BASELINE_CONFLICT" for item in result["errors"]))
        self.assertEqual((self.root / "projects/old/notes/topic.md").read_bytes(), b"concurrent source edit")
        self.assertTrue((self.root / "projects/new/README.md").exists())

    def test_interrupted_source_removal_is_recovered_from_external_journal(self):
        plan = self.plan()
        confirmation = self.tool.check_plan(str(self.root), plan)["confirmation"]
        original_unlink = Path.unlink
        failed = {"done": False}

        def fail_second_source(path, *args, **kwargs):
            if str(path).replace("\\", "/").startswith(str(self.root).replace("\\", "/") + "/projects/old/"):
                if path.name == "topic.md" and not failed["done"]:
                    failed["done"] = True
                    raise OSError("injected interrupted source removal")
            return original_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", fail_second_source):
            result = self.tool.apply_plan(str(self.root), plan, confirmation)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["recovery"], "rolled-back")
        self.assertEqual((self.root / "projects/old/README.md").read_bytes(), b"# Old project\n")
        self.assertEqual((self.root / "projects/old/notes/topic.md").read_bytes(), b"topic\n")
        self.assertFalse((self.root / "projects/new/README.md").exists())

    def test_default_journal_resolves_system_temporary_directory_aliases(self):
        physical = Path(self.temp.name).resolve()
        alias = physical / "temp-alias"
        try:
            alias.symlink_to(physical, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("directory symbolic links are unavailable")
        with patch.object(tempfile, "gettempdir", return_value=str(alias)):
            tool = load_tool()
        self.assertEqual(tool.JOURNAL_ROOT, physical / "personal-agent-asset-migrations")
        tool._check_journal_location(self.root)
        tool.JOURNAL_ROOT = alias / "user-configured-journal"
        with self.assertRaises(tool.MigrationError) as rejected:
            tool._check_journal_location(self.root)
        self.assertEqual(rejected.exception.code, "JOURNAL_INVALID")

    def test_link_update_is_applied_to_staged_target_and_restore_recovers_original_bytes(self):
        plan = self.plan(links={"projects/old/README.md": (
            b"# Old project\n", b"# New project\n")})
        confirmation = self.tool.check_plan(str(self.root), plan)["confirmation"]
        result = self.tool.apply_plan(str(self.root), plan, confirmation=confirmation)
        self.assertEqual(result["status"], "completed", result)
        target = self.root / "projects/new/README.md"
        self.assertEqual(target.read_bytes(), b"# New project\n")
        verified = self.tool.verify_migration(str(self.root), result["migrationId"])
        self.assertEqual(verified["status"], "verified")
        restored = self.tool.restore_migration(str(self.root), result["migrationId"])
        self.assertEqual(restored["status"], "restored", restored)
        self.assertEqual((self.root / "projects/old/README.md").read_bytes(), b"# Old project\n")
        self.assertFalse(target.exists())
        self.assertFalse((self.root / "projects/new").exists())

    def test_restore_preserves_concurrent_target_and_source_content(self):
        plan = self.plan()
        confirmation = self.tool.check_plan(str(self.root), plan)["confirmation"]
        result = self.tool.apply_plan(str(self.root), plan, confirmation=confirmation)
        target = self.root / "projects/new/README.md"
        target.write_bytes(b"user changed target")
        (self.root / "projects/old/notes").mkdir(parents=True, exist_ok=True)
        (self.root / "projects/old/notes/topic.md").write_bytes(b"user recreated source")
        restored = self.tool.restore_migration(str(self.root), result["migrationId"])
        self.assertEqual(restored["status"], "conflict")
        self.assertEqual(target.read_bytes(), b"user changed target")
        self.assertEqual((self.root / "projects/old/notes/topic.md").read_bytes(), b"user recreated source")
        self.assertTrue(restored["errors"])

    def test_partial_copy_failure_retains_source_and_rolls_back_created_targets(self):
        plan = self.plan()
        confirmation = self.tool.check_plan(str(self.root), plan)["confirmation"]
        original = self.tool._write_bytes
        count = {"value": 0}

        def fail_second(*args, **kwargs):
            count["value"] += 1
            if count["value"] == 2:
                raise OSError("injected copy failure")
            return original(*args, **kwargs)

        with patch.object(self.tool, "_write_bytes", side_effect=fail_second):
            result = self.tool.apply_plan(str(self.root), plan, confirmation=confirmation)
        self.assertEqual(result["status"], "error")
        self.assertTrue((self.root / "projects/old/README.md").exists())
        self.assertFalse((self.root / "projects/new/README.md").exists())

    def test_restore_journal_is_external_and_contains_verified_recovery_bytes(self):
        plan = self.plan()
        confirmation = self.tool.check_plan(str(self.root), plan)["confirmation"]
        result = self.tool.apply_plan(str(self.root), plan, confirmation=confirmation)
        journal = json.loads(Path(result["journal"]).read_text(encoding="utf-8"))
        self.assertFalse(Path(result["journal"]).is_relative_to(self.root))
        self.assertEqual(base64.b64decode(journal["sourceBackup"]["projects/old/README.md"]), b"# Old project\n")

    def test_missing_journal_is_reported_and_never_guesses_recovery(self):
        result = self.tool.restore_migration(str(self.root), "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["errors"][0]["code"], "JOURNAL_UNAVAILABLE")

    def test_concurrent_external_link_edit_is_preserved_during_apply_and_restore(self):
        before = b"See [project](projects/old/README.md)\n"
        after = b"See [project](projects/new/README.md)\n"
        plan = self.plan(links={"INDEX.md": (before, after)})
        confirmation = self.tool.check_plan(str(self.root), plan)["confirmation"]
        original = self.tool._write_bytes
        concurrent = {"done": False}

        def edit_link_before_replace(root, relative, content, **kwargs):
            if relative == "INDEX.md" and kwargs.get("replace") and not concurrent["done"]:
                (self.root / relative).write_bytes(b"user concurrent edit")
                concurrent["done"] = True
            return original(root, relative, content, **kwargs)

        with patch.object(self.tool, "_write_bytes", side_effect=edit_link_before_replace):
            result = self.tool.apply_plan(str(self.root), plan, confirmation)
        self.assertEqual(result["status"], "error")
        self.assertEqual((self.root / "INDEX.md").read_bytes(), b"user concurrent edit")
        self.assertEqual((self.root / "projects/old/README.md").read_bytes(), b"# Old project\n")
        self.assertTrue((self.root / "projects/new/README.md").exists())

    def test_restore_corrupt_or_missing_backup_data_does_not_delete_targets(self):
        plan = self.plan()
        result = self.tool.apply_plan(str(self.root), plan, self.tool.diff_plan(self.root, plan)["confirmation"])
        path = Path(result["journal"])
        original = json.loads(path.read_text(encoding="utf-8"))
        for damaged in ({**original, "sourceBackup": {}},
                        {**original, "sourceBackup": {**original["sourceBackup"],
                         "projects/old/README.md": base64.b64encode(b"corrupted").decode("ascii")}}):
            path.write_text(json.dumps(damaged), encoding="utf-8")
            restored = self.tool.restore_migration(self.root, result["migrationId"])
            self.assertEqual(restored["status"], "error")
            self.assertEqual(restored["errors"][0]["code"], "JOURNAL_INVALID")
            self.assertEqual((self.root / "projects/new/README.md").read_bytes(), b"# Old project\n")
            self.assertFalse((self.root / "projects/old/README.md").exists())
        path.write_text(json.dumps(original), encoding="utf-8")
        self.assertEqual(self.tool.restore_migration(self.root, result["migrationId"])["status"], "restored")

    def test_failed_restore_preserves_targets_and_is_retryable(self):
        plan = self.plan()
        result = self.tool.apply_plan(self.root, plan, self.tool.diff_plan(self.root, plan)["confirmation"])
        write = self.tool._write_bytes

        def fail_source(root, relative, data, **kwargs):
            if relative.startswith("projects/old/"):
                raise OSError("injected restore failure")
            return write(root, relative, data, **kwargs)

        with patch.object(self.tool, "_write_bytes", side_effect=fail_source):
            failed = self.tool.restore_migration(self.root, result["migrationId"])
        self.assertEqual(failed["status"], "conflict")
        self.assertTrue(all(item["code"] == "RESTORE_SOURCE_FAILED" for item in failed["errors"]))
        self.assertTrue((self.root / "projects/new/README.md").exists())
        restored = self.tool.restore_migration(self.root, result["migrationId"])
        self.assertEqual(restored["status"], "restored", restored)
        self.assertTrue(self.tool.restore_migration(self.root, result["migrationId"])["idempotent"])

    def test_cross_volume_move_never_renames_source_to_target(self):
        plan = self.plan()
        replace = self.tool.os.replace

        def simulate_volumes(source, target):
            if "projects/old/" in str(source).replace("\\", "/"):
                raise OSError(errno.EXDEV, "simulated cross-volume rename")
            return replace(source, target)

        with patch.object(self.tool.os, "replace", side_effect=simulate_volumes):
            result = self.tool.apply_plan(self.root, plan, self.tool.diff_plan(self.root, plan)["confirmation"])
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(self.tool.verify_migration(self.root, result["migrationId"])["status"], "verified")

    def test_target_file_count_conflict_is_detected_before_source_removal(self):
        plan = self.plan()
        write = self.tool._write_bytes

        def extra_target(root, relative, data, **kwargs):
            write(root, relative, data, **kwargs)
            if relative.startswith("projects/new/"):
                (self.root / "projects/new/user-new.txt").write_bytes(b"concurrent asset")

        with patch.object(self.tool, "_write_bytes", side_effect=extra_target):
            result = self.tool.apply_plan(self.root, plan, self.tool.diff_plan(self.root, plan)["confirmation"])
        self.assertEqual(result["status"], "error", result)
        self.assertTrue(any(item["code"] == "TARGET_INVENTORY_MISMATCH" for item in result["errors"]))
        self.assertTrue((self.root / "projects/old/README.md").exists())
        self.assertEqual((self.root / "projects/new/user-new.txt").read_bytes(), b"concurrent asset")

    def test_dotenv_text_in_link_scope_is_never_read(self):
        plan = self.plan()
        secret = self.root / ".env.secret.md"
        secret.write_bytes(b"secret")
        read = self.tool._read_bytes

        def reject(root, relative):
            if relative.startswith(".env"):
                self.fail("dotenv read by link scanner")
            return read(root, relative)

        with patch.object(self.tool, "_read_bytes", side_effect=reject):
            self.assertEqual(self.tool.check_plan(self.root, plan)["status"], "ok")

    def test_asset_cli_json_check_diff_and_confirmation(self):
        plan = self.plan()
        path = Path(self.temp.name) / "asset-plan.json"
        path.write_text(json.dumps(plan), encoding="utf-8")
        for operation in ("check", "diff", "apply"):
            process = subprocess.run([sys.executable, "-B", str(TOOL), operation,
                                      str(self.root), "--plan-file", str(path)], capture_output=True)
            self.assertEqual(process.returncode, 0, process.stderr)
            payload = json.loads(process.stdout)
            self.assertEqual(payload["status"], "needs-confirmation" if operation == "apply" else "ok")

    def test_scaffold_allowlist_is_unchanged(self):
        from tools.test_agent_scaffold import load_maintenance_module
        maintenance = load_maintenance_module()
        with self.assertRaises(maintenance.PlanError) as failure:
            maintenance.safe_rel("projects/old/README.md")
        self.assertEqual(failure.exception.code, "PLAN_PATH_SCOPE")


if __name__ == "__main__":
    unittest.main()
