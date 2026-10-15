#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Black-box integration regression checks for the scaffold maintenance CLI."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "tools" / "agent_scaffold.py"
VALIDATOR = ROOT / "tools" / "validate_scaffold.py"
MINIMAL = ROOT / "samples" / "agent-scaffold" / "minimal"
INVALID_ROOT = ROOT / "samples" / "agent-scaffold" / "invalid"
READING_ASSISTANT = Path(r"F:\Personal agents\读书助手")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def invoke(script: Path, *args: str, input_text: str | None = None) -> tuple[int, dict]:
    proc = subprocess.run(
        [sys.executable, str(script), *map(str, args)],
        input=input_text,
        text=False,
        capture_output=True,
        cwd=ROOT,
    )
    try:
        stdout = proc.stdout.decode("utf-8", "replace")
        stderr = proc.stderr.decode("utf-8", "replace")
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:  # pragma: no cover - makes failures actionable
        raise AssertionError(f"non-JSON CLI output (exit={proc.returncode}): {stdout!r}\n{stderr}") from exc
    return proc.returncode, payload


def load_maintenance_module():
    tools_dir = str(CLI.parent)
    added = tools_dir not in sys.path
    if added:
        sys.path.insert(0, tools_dir)
    spec = importlib.util.spec_from_file_location("agent_scaffold_under_test", CLI)
    if spec is None or spec.loader is None:
        raise AssertionError(f"unable to import {CLI}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
        return module
    finally:
        if added:
            sys.path.remove(tools_dir)


class ScaffoldIntegrationTests(unittest.TestCase):
    def test_evaluation_artifacts_stay_outside_the_rule_plan_whitelist(self):
        maintenance = load_maintenance_module()
        for rel in ("scaffold-evaluation.md", "scaffold-score-history.md"):
            with self.assertRaises(maintenance.PlanError) as rejected:
                maintenance.safe_rel(rel)
            self.assertEqual(rejected.exception.code, "PLAN_PATH_SCOPE")
        self.assertEqual(maintenance.safe_rel(".agent/skills/scaffold-evaluate.md"), ".agent/skills/scaffold-evaluate.md")

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="agent-scaffold-integration-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def workspace(self, name: str = "workspace") -> Path:
        path = self.tmp / name
        path.mkdir(parents=True)
        return path

    def copy_minimal(self, name: str = "workspace") -> Path:
        path = self.tmp / name
        shutil.copytree(MINIMAL, path)
        return path

    def test_workspace_parent_alias_matches_resolved_plan_without_relaxing_plan_location(self):
        actual = self.workspace("physical")
        workspace = actual / "workspace"
        shutil.copytree(MINIMAL, workspace)
        alias = self.tmp / "alias"
        try:
            alias.symlink_to(actual, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("directory symbolic links are unavailable")
        command_target = alias / "workspace"
        entry = (workspace / "AGENTS.md").read_text(encoding="utf-8")
        plan = self.make_plan(workspace, {"AGENTS.md": entry + "\nalias check\n"})
        code, checked = invoke(CLI, "check", command_target, "--plan-file", plan)
        self.assertEqual((code, checked["status"]), (0, "ok"), checked)
        different = json.loads(plan.read_text(encoding="utf-8"))
        different["workspace"] = str(actual / "different-workspace")
        wrong = self.tmp / "different-plan.json"
        wrong.write_text(json.dumps(different), encoding="utf-8")
        code, rejected = invoke(CLI, "check", command_target, "--plan-file", wrong)
        self.assertEqual(code, 1, rejected)
        self.assertEqual(rejected["errors"][0]["code"], "PLAN_WORKSPACE_MISMATCH")
        inside = workspace / "plan.json"
        shutil.copyfile(plan, inside)
        code, rejected = invoke(CLI, "check", command_target, "--plan-file", inside)
        self.assertEqual(code, 1, rejected)
        self.assertEqual(rejected["errors"][0]["code"], "PLAN_FILE_INSIDE_WORKSPACE")
        external_link = workspace / "external-plan.json"
        external_link.symlink_to(plan)
        code, rejected = invoke(CLI, "check", workspace, "--plan-file", external_link)
        self.assertEqual(code, 1, rejected)
        self.assertEqual(rejected["errors"][0]["code"], "PLAN_FILE_INSIDE_WORKSPACE")

    def test_init_output_remains_utf8_under_a_legacy_console_encoding(self):
        target = self.tmp / "init-encoding"
        result = subprocess.run(
            [sys.executable, str(ROOT / "tools/agent_init.py"), str(target),
             "--agent-id", "example", "--purpose", "local task"],
            capture_output=True, env={**os.environ, "PYTHONIOENCODING": "cp1252"}, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        self.assertIn("AGENTS.md", result.stdout.decode("utf-8"))

    def make_plan(self, workspace: Path, files: dict[str, str | None], name: str = "plan.json", description_sync: str | None = None) -> Path:
        baseline: dict[str, str | None] = {}
        for rel in files:
            target = workspace / Path(rel)
            baseline[rel] = digest(target.read_bytes()) if target.is_file() else None
        plan = {
            "schemaVersion": 1,
            "workspace": str(workspace.resolve()),
            "baseline": baseline,
            "files": files,
        }
        if description_sync is not None:
            plan["descriptionSync"] = description_sync
        path = self.tmp / name
        path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        return path

    def test_missing_workspace_can_be_manufactured_and_unrelated_file_survives(self) -> None:
        empty = self.workspace("empty")
        empty_code, empty_info = invoke(CLI, "inspect", empty)
        self.assertEqual(empty_code, 0)
        self.assertEqual(empty_info["classification"], "missing")
        self.assertTrue(empty_info["adoptable"])

        workspace = self.workspace()
        unrelated = workspace / "book.bin"
        unrelated.write_bytes(b"private book content")
        before = unrelated.stat()
        code, inspected = invoke(CLI, "inspect", workspace)
        self.assertEqual(code, 0)
        self.assertEqual(inspected["scaffold"], "missing")
        self.assertEqual(inspected["classification"], "existing-unmanaged")
        self.assertTrue(inspected["adoptable"])
        self.assertTrue(inspected["inventory"]["complete"])

        files = {
            "AGENTS.md": (MINIMAL / "AGENTS.md").read_text(encoding="utf-8"),
            ".agent/skills/quiet-check.md": (MINIMAL / ".agent/skills/quiet-check.md").read_text(encoding="utf-8"),
        }
        empty_target = self.workspace("created-workspace")
        plan = self.make_plan(empty_target, files)
        code, checked = invoke(CLI, "check", empty_target, "--plan-file", plan)
        self.assertEqual(code, 0)
        self.assertEqual(checked["status"], "ok")
        self.assertEqual({item["action"] for item in checked["changes"]}, {"create"})
        code, applied = invoke(CLI, "apply", empty_target, "--plan-file", plan, "--confirm")
        self.assertEqual(code, 0)
        self.assertEqual(applied["status"], "ok")
        self.assertEqual(unrelated.read_bytes(), b"private book content")
        self.assertEqual(unrelated.stat().st_mtime_ns, before.st_mtime_ns)
        code, verified = invoke(CLI, "verify", empty_target)
        self.assertEqual(code, 0)
        self.assertEqual(verified["status"], "ok")

    def test_modify_requires_confirmation_and_preserves_private_file_mtime(self) -> None:
        workspace = self.copy_minimal()
        private = workspace / "runtime" / "memory.db"
        private.parent.mkdir()
        private.write_bytes(b"opaque runtime bytes")
        before_bytes, before_mtime = private.read_bytes(), private.stat().st_mtime_ns
        entry = (workspace / "AGENTS.md").read_text(encoding="utf-8")
        plan = self.make_plan(workspace, {"AGENTS.md": entry.replace("# Personal Agent", "# Personal Agent\n\nConsumer note")})

        code, diff = invoke(CLI, "diff", workspace, "--plan-file", plan)
        self.assertEqual(code, 0)
        self.assertEqual(diff["status"], "ok")
        code, pending = invoke(CLI, "apply", workspace, "--plan-file", plan)
        self.assertEqual(code, 0)
        self.assertEqual(pending["status"], "needs-confirmation")
        self.assertEqual(private.read_bytes(), before_bytes)
        self.assertEqual(private.stat().st_mtime_ns, before_mtime)

        code, applied = invoke(CLI, "apply", workspace, "--plan-file", plan, "--confirm")
        self.assertEqual(code, 0)
        self.assertEqual(applied["status"], "ok")
        self.assertEqual(private.read_bytes(), before_bytes)
        self.assertEqual(private.stat().st_mtime_ns, before_mtime)
        self.assertIn("id", applied.get("backup", {}))

    def test_runtime_data_is_outside_maintenance_plan_scope(self) -> None:
        workspace = self.copy_minimal()
        plan = self.make_plan(workspace, {"runtime/context.md": "runtime data"}, "runtime-plan.json")
        code, checked = invoke(CLI, "check", workspace, "--plan-file", plan)
        self.assertEqual(code, 1)
        self.assertEqual(checked["status"], "error")
        self.assertTrue(any(error.get("code") == "PLAN_PATH_SCOPE" for error in checked["errors"]))
        self.assertFalse((workspace / "runtime" / "context.md").exists())

    def test_baseline_conflict_is_reported_without_writing(self) -> None:
        workspace = self.copy_minimal()
        original = (workspace / "AGENTS.md").read_bytes()
        entry = original.decode("utf-8")
        plan = self.make_plan(workspace, {"AGENTS.md": entry + "\nplanned\n"})
        (workspace / "AGENTS.md").write_bytes(original + b"\nconcurrent change\n")
        code, result = invoke(CLI, "apply", workspace, "--plan-file", plan, "--confirm")
        self.assertNotEqual(code, 0)
        self.assertEqual(result["status"], "conflict")
        self.assertTrue(any(e.get("code") == "PLAN_BASELINE_CONFLICT" and e.get("path") == "AGENTS.md" for e in result["errors"]))
        self.assertEqual((workspace / "AGENTS.md").read_bytes(), original + b"\nconcurrent change\n")

    def test_invalid_scaffold_repair_and_legacy_migration(self) -> None:
        invalid = self.workspace("invalid")
        shutil.copy(INVALID_ROOT / "dangling-reference" / "AGENTS.md", invalid / "AGENTS.md")
        code, inspected = invoke(CLI, "inspect", invalid)
        self.assertEqual(code, 1)
        self.assertEqual(inspected["scaffold"], "invalid")
        repair_files = {
            "AGENTS.md": (MINIMAL / "AGENTS.md").read_text(encoding="utf-8"),
            ".agent/skills/quiet-check.md": (MINIMAL / ".agent/skills/quiet-check.md").read_text(encoding="utf-8"),
            ".agent/workflows/review.md": None,
        }
        plan = self.make_plan(invalid, repair_files, "repair.json")
        code, repaired = invoke(CLI, "apply", invalid, "--plan-file", plan, "--confirm")
        self.assertEqual(code, 0)
        self.assertEqual(repaired["status"], "ok")
        self.assertEqual(invoke(CLI, "verify", invalid)[1]["status"], "ok")

        legacy = self.workspace("legacy")
        shutil.copytree(MINIMAL, legacy, dirs_exist_ok=True)
        (legacy / "AGENTS.md").rename(legacy / "AGENT.md")
        code, legacy_info = invoke(CLI, "inspect", legacy)
        self.assertEqual(code, 0)
        self.assertEqual(legacy_info["scaffold"], "legacy")
        migration = self.make_plan(
            legacy,
            {"AGENT.md": None, "AGENTS.md": (MINIMAL / "AGENTS.md").read_text(encoding="utf-8"),
             ".agent/skills/quiet-check.md": (MINIMAL / ".agent/skills/quiet-check.md").read_text(encoding="utf-8")},
            "migration.json",
        )
        code, preview = invoke(CLI, "diff", legacy, "--plan-file", migration)
        self.assertEqual(code, 0)
        self.assertEqual(preview["status"], "ok")
        self.assertEqual({item["action"] for item in preview["changes"]}, {"create", "delete", "unchanged"})

    def test_plain_markdown_entry_is_existing_unmanaged_and_inspect_is_stable_read_only(self) -> None:
        workspace = self.workspace()
        entry = workspace / "AGENTS.md"
        entry.write_text("# Existing project rules\n\nKeep every project in its current directory.\n", encoding="utf-8")
        (workspace / "notes").mkdir()
        (workspace / "notes" / "readme.md").write_text("# Notes\n", encoding="utf-8")
        before = {path.relative_to(workspace).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
                  for path in workspace.rglob("*") if path.is_file()}

        first_code, first = invoke(CLI, "inspect", workspace)
        second_code, second = invoke(CLI, "inspect", workspace)

        self.assertEqual(first_code, 0)
        self.assertEqual(second_code, 0)
        self.assertEqual(first, second)
        self.assertEqual(first["classification"], "existing-unmanaged")
        self.assertEqual(first["scaffold"], "missing")
        self.assertTrue(first["adoptable"])
        self.assertEqual(first["entry"]["path"], "AGENTS.md")
        self.assertTrue(first["entry"]["exists"])
        self.assertEqual(first["errors"], [])
        after = {path.relative_to(workspace).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
                 for path in workspace.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_inspect_classifies_formal_and_external_host_entries(self) -> None:
        valid = self.copy_minimal("valid")
        valid_code, valid_info = invoke(CLI, "inspect", valid)
        self.assertEqual(valid_code, 0)
        self.assertEqual(valid_info["classification"], "valid")
        self.assertEqual(valid_info["scaffold"], "valid")

        invalid = self.workspace("formal-invalid")
        shutil.copy(INVALID_ROOT / "dangling-reference" / "AGENTS.md", invalid / "AGENTS.md")
        invalid_code, invalid_info = invoke(CLI, "inspect", invalid)
        self.assertEqual(invalid_code, 1)
        self.assertEqual(invalid_info["classification"], "invalid")
        self.assertFalse(invalid_info["adoptable"])

        unmanaged = self.workspace("external-entry")
        (unmanaged / "AGENT.md").write_text("# Another host format\n", encoding="utf-8")
        (unmanaged / "CLAUDE.md").write_text("# Claude rules\n", encoding="utf-8")
        code, info = invoke(CLI, "inspect", unmanaged)
        self.assertEqual(code, 0)
        self.assertEqual(info["classification"], "existing-unmanaged")
        self.assertTrue(any(item["code"] == "WORKSPACE_ENTRY_UNSUPPORTED" and item["path"] == "AGENT.md"
                            for item in info["warnings"]))
        self.assertTrue(any(item["code"] == "WORKSPACE_ENTRY_UNSUPPORTED" and item["path"] == "CLAUDE.md"
                            for item in info["warnings"]))

    def test_inventory_prunes_common_directories_and_never_reads_dotenv(self) -> None:
        workspace = self.workspace()
        (workspace / "AGENTS.md").write_text("# User rules\n", encoding="utf-8")
        for directory in (".git", ".venv", "node_modules", "__pycache__", "dist"):
            nested = workspace / directory
            nested.mkdir()
            (nested / "large.bin").write_bytes(b"not inspected")
        dotenv = workspace / ".env"
        dotenv.write_text("API_TOKEN=must-not-be-read\n", encoding="utf-8")

        maintenance = load_maintenance_module()
        original_open = open

        def reject_dotenv(path, *args, **kwargs):
            if os.path.abspath(os.fspath(path)) == os.path.abspath(dotenv):
                raise AssertionError("inspect attempted to open dotenv content")
            return original_open(path, *args, **kwargs)

        with patch("builtins.open", side_effect=reject_dotenv):
            info = maintenance.inspect_workspace(str(workspace))
        self.assertTrue(info["inventory"]["complete"])
        self.assertIn(".env", info["files"])
        self.assertNotIn(".git/large.bin", info["files"])
        self.assertNotIn(".venv/large.bin", info["files"])
        self.assertTrue(any(item["path"] == ".env" for item in info["inventory"]["excluded"]))
        self.assertNotIn("must-not-be-read", json.dumps(info))

    def test_inventory_permission_and_reserved_name_failures_are_structured(self) -> None:
        maintenance = load_maintenance_module()
        workspace = self.workspace()
        (workspace / "AGENTS.md").write_text("# User rules\n", encoding="utf-8")
        blocked = workspace / "blocked"
        blocked.mkdir()
        original_scandir = maintenance.os.scandir

        def fail_blocked(path):
            if os.path.abspath(os.fspath(path)) == os.path.abspath(blocked):
                raise PermissionError("injected permission failure")
            return original_scandir(path)

        with patch.object(maintenance.os, "scandir", side_effect=fail_blocked):
            info = maintenance.inspect_workspace(str(workspace))
        self.assertFalse(info["inventory"]["complete"])
        self.assertFalse(info["adoptable"])
        self.assertTrue(any(item["code"] == "INVENTORY_PERMISSION_DENIED" and item["path"] == "blocked"
                            for item in info["warnings"]))

        self.assertTrue(maintenance.is_windows_reserved_path("notes/nul"))
        with patch.object(maintenance, "is_windows_reserved_path", return_value=True):
            reserved = maintenance.inventory_workspace(str(workspace))
        self.assertFalse(reserved["complete"])
        self.assertTrue(any(item["code"] == "INVENTORY_RESERVED_NAME" and item["path"]
                            for item in reserved["warnings"]))

    def test_unreadable_project_rule_blocks_adoption_without_exposing_content(self) -> None:
        maintenance = load_maintenance_module()
        workspace = self.workspace()
        (workspace / "AGENTS.md").write_text("# Root rules\n", encoding="utf-8")
        nested = workspace / "project-a/AGENTS.md"
        nested.parent.mkdir()
        nested.write_text("# Secret project instructions\n", encoding="utf-8")
        original_open = open

        def deny_project_rule(path, *args, **kwargs):
            if os.path.abspath(os.fspath(path)) == os.path.abspath(nested):
                raise PermissionError("injected rule read failure")
            return original_open(path, *args, **kwargs)

        with patch("builtins.open", side_effect=deny_project_rule):
            info = maintenance.inspect_workspace(str(workspace))
        self.assertEqual(info["classification"], "existing-unmanaged")
        self.assertFalse(info["adoptable"])
        self.assertFalse(info["inventory"]["complete"])
        self.assertIn("project-a/AGENTS.md", info["inventory"]["ruleFiles"])
        self.assertTrue(any(item["code"] == "INVENTORY_PERMISSION_DENIED"
                            and item["path"] == "project-a/AGENTS.md"
                            for item in info["inventory"]["warnings"]))
        self.assertNotIn("Secret project instructions", json.dumps(info))

    def test_incomplete_formal_inventory_does_not_run_unbounded_validator(self) -> None:
        maintenance = load_maintenance_module()
        workspace = self.workspace()
        (workspace / "AGENTS.md").write_text(
            "---\nagentId: marked\ncontractVersion: \"1\"\npurpose: Marked scaffold\n---\n# Entry\n",
            encoding="utf-8")
        large = workspace / ".agent/scaffold-manifest.json"
        large.parent.mkdir()
        large.write_bytes(b" " * (maintenance.INVENTORY_RULE_MAX_BYTES + 1))
        info = maintenance.inspect_workspace(str(workspace))
        self.assertEqual(info["classification"], "invalid")
        self.assertFalse(info["adoptable"])
        self.assertNotIn("delivery", info)
        self.assertTrue(any(item["code"] == "INSPECT_VALIDATION_INCOMPLETE" for item in info["errors"]))

    def test_inventory_path_conversion_failure_is_reported_without_traceback(self) -> None:
        maintenance = load_maintenance_module()
        workspace = self.workspace()
        (workspace / "ordinary.txt").write_text("content", encoding="utf-8")
        with patch.object(maintenance, "_display_inventory_path", side_effect=UnicodeError("injected path error")):
            info = maintenance.inventory_workspace(str(workspace))
        self.assertFalse(info["complete"])
        self.assertTrue(any(item["code"] == "INVENTORY_PATH_INVALID" for item in info["warnings"]))

    def test_inventory_depth_and_entry_limits_are_bounded(self) -> None:
        maintenance = load_maintenance_module()
        workspace = self.workspace()
        nested = workspace / "one" / "two" / "three"
        nested.mkdir(parents=True)
        (nested / "item.md").write_text("content", encoding="utf-8")
        with patch.object(maintenance, "INVENTORY_MAX_DEPTH", 2):
            depth_inventory = maintenance.inventory_workspace(str(workspace))
        self.assertFalse(depth_inventory["complete"])
        self.assertTrue(any(item["code"] == "INVENTORY_DEPTH_LIMIT" for item in depth_inventory["warnings"]))

        (workspace / "one").rename(workspace / "renamed")
        (workspace / "a.txt").write_text("a", encoding="utf-8")
        (workspace / "b.txt").write_text("b", encoding="utf-8")
        with patch.object(maintenance, "INVENTORY_MAX_ENTRIES", 2):
            limited = maintenance.inventory_workspace(str(workspace))
        self.assertFalse(limited["complete"])
        self.assertLessEqual(limited["scannedEntries"], 2)
        self.assertTrue(any(item["code"] == "INVENTORY_ENTRY_LIMIT" for item in limited["warnings"]))

    def test_inventory_does_not_follow_symbolic_links(self) -> None:
        maintenance = load_maintenance_module()
        workspace = self.workspace()
        outside = self.workspace("outside")
        (outside / "secret.md").write_text("outside", encoding="utf-8")
        link = workspace / "linked"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("directory symbolic links are unavailable")
        inventory = maintenance.inventory_workspace(str(workspace))
        self.assertFalse(inventory["complete"])
        self.assertNotIn("linked/secret.md", inventory["files"])
        self.assertTrue(any(item["path"] == "linked" and item["reason"] == "symbolic-link"
                            for item in inventory["excluded"]))

    def test_context_source_file_mappings_are_backward_compatible_and_metadata_only(self) -> None:
        maintenance = load_maintenance_module()
        workspace = self.copy_minimal("context-sources")
        (workspace / "PROFILE.md").write_text("private profile body", encoding="utf-8")
        (workspace / "ACTIVE.md").write_text("private active body", encoding="utf-8")
        (workspace / "PROJECTS.md").write_text("private project body", encoding="utf-8")
        (workspace / "README.md").write_text("private readme body", encoding="utf-8")
        context = workspace / ".agent/context.yaml"
        context.write_text(
            "writePolicy: confirm\ncategories: [workspace-profile]\n"
            "sources: [agent-md, user-statement]\nstates: [temporary, pending, confirmed]\n"
            "sourceFiles:\n"
            "  - id: profile\n    path: PROFILE.md\n    purpose: Confirmed profile\n"
            "    readPolicy: on-demand\n    writePolicy: read-only\n"
            "  - id: active\n    path: ACTIVE.md\n    purpose: Current work\n"
            "    readPolicy: on-demand\n    writePolicy: confirm\n"
            "  - id: projects\n    path: PROJECTS.md\n    purpose: Project map\n"
            "    readPolicy: on-demand\n    writePolicy: read-only\n"
            "  - id: readme\n    path: README.md\n    purpose: Project overview\n"
            "    readPolicy: on-demand\n    writePolicy: read-only\n",
            encoding="utf-8",
        )
        source_paths = {os.path.abspath(workspace / name) for name in
                        ("PROFILE.md", "ACTIVE.md", "PROJECTS.md", "README.md")}
        original_open = open

        def reject_source_reads(path, *args, **kwargs):
            if os.path.abspath(os.fspath(path)) in source_paths:
                raise AssertionError("validator read a mapped source file")
            return original_open(path, *args, **kwargs)

        with patch("builtins.open", side_effect=reject_source_reads):
            errors = maintenance.validate_scaffold.validate(str(workspace))
        self.assertEqual(errors, [])
        self.assertIn("sources: [agent-md, user-statement]", context.read_text(encoding="utf-8"))

        legacy = self.copy_minimal("legacy-context")
        (legacy / ".agent/context.yaml").write_text(
            "writePolicy: confirm\ncategories: [workspace-profile]\n"
            "sources: [agent-md, user-statement]\nstates: [temporary, pending, confirmed]\n",
            encoding="utf-8",
        )
        self.assertEqual(maintenance.validate_scaffold.validate(str(legacy)), [])

    def test_context_source_file_mappings_reject_missing_unsafe_and_symlink_paths(self) -> None:
        maintenance = load_maintenance_module()
        workspace = self.copy_minimal("bad-context-sources")
        outside = self.workspace("outside-context")
        target = outside / "source.md"
        target.write_text("external data", encoding="utf-8")
        symlink = workspace / "linked-source.md"
        link_created = False
        try:
            symlink.symlink_to(target)
            link_created = True
        except (OSError, NotImplementedError):
            pass

        paths = ["missing.md", "../outside.md", "C:/outside.md", "*.md"]
        if link_created:
            paths.append("linked-source.md")
        entries = "\n".join(
            "  - id: source-%d\n    path: %s\n    purpose: Test source\n"
            "    readPolicy: on-demand\n    writePolicy: read-only" % (index, json.dumps(path))
            for index, path in enumerate(paths)
        )
        (workspace / ".agent/context.yaml").write_text(
            "writePolicy: confirm\ncategories: [workspace-profile]\n"
            "sources: [agent-md]\nstates: [temporary, pending, confirmed]\n"
            "sourceFiles:\n" + entries + "\n",
            encoding="utf-8",
        )
        errors = maintenance.validate_scaffold.validate(str(workspace))
        self.assertTrue(any(item["code"].startswith("SCAFFOLD_CONTEXT_SOURCE_") for item in errors))
        messages = "\n".join(item["message"] for item in errors)
        for path in paths:
            self.assertIn(path, messages)

    def test_context_source_file_mappings_validate_fields_and_unique_ids(self) -> None:
        maintenance = load_maintenance_module()
        workspace = self.copy_minimal("context-source-fields")
        (workspace / "source.md").write_text("source body", encoding="utf-8")
        (workspace / ".agent/context.yaml").write_text(
            "writePolicy: confirm\ncategories: [workspace-profile]\n"
            "sources: [agent-md]\nstates: [temporary, pending, confirmed]\n"
            "sourceFiles:\n"
            "  - id: source\n    path: source.md\n    purpose: Source\n"
            "    readPolicy: always\n    writePolicy: overwrite\n"
            "  - id: source\n    path: source.md\n    purpose: Duplicate\n"
            "    readPolicy: on-demand\n    writePolicy: read-only\n",
            encoding="utf-8",
        )
        errors = maintenance.validate_scaffold.validate(str(workspace))
        codes = {item["code"] for item in errors}
        self.assertIn("SCAFFOLD_CONTEXT_SOURCE_READ_POLICY", codes)
        self.assertIn("SCAFFOLD_CONTEXT_SOURCE_WRITE_POLICY", codes)
        self.assertIn("SCAFFOLD_CONTEXT_SOURCE_DUPLICATE", codes)

    def test_unmanaged_adoption_and_description_only_plans_are_blocked_without_partial_writes(self) -> None:
        maintenance = load_maintenance_module()
        workspace = self.workspace("adoption-gate")
        entry = workspace / "AGENTS.md"
        entry.write_text("# Existing root rules\n\nKeep project boundaries intact.\n", encoding="utf-8")
        before = entry.read_bytes()

        incomplete = self.make_plan(workspace, {"AGENTS.md": before.decode("utf-8")},
                                    "adoption-entry-only.json")
        checked, code = maintenance.run("check", str(workspace), str(incomplete), False, None)
        self.assertEqual(code, 1)
        self.assertEqual(checked["errors"][0]["code"], "ADOPTION_FORMAL_INCOMPLETE")
        self.assertIn("scaffold-description.md", checked["errors"][0]["message"])
        self.assertIn("capabilities", checked["errors"][0]["message"])

        full_source = self.copy_minimal("adoption-complete-source")
        from tools.test_agent_distribution import make_formal
        make_formal(full_source)
        manifest = maintenance.load_package_manifest(str(full_source))
        files = {rel: (full_source / rel).read_text(encoding="utf-8") for rel in manifest["files"]}
        files[maintenance.MANIFEST_FILE] = (full_source / maintenance.MANIFEST_FILE).read_text(encoding="utf-8")
        files["AGENTS.md"] = files["AGENTS.md"].replace(
            "# Personal Agent", "# Personal Agent\n\nKeep every project in its current directory.", 1)
        plan = self.make_plan(workspace, files, "adoption-rule-deletion.json", "unchanged")
        rejected, code = maintenance.run("check", str(workspace), str(plan), False, None)
        self.assertEqual(code, 1)
        self.assertEqual(rejected["errors"][0]["code"], "ADOPTION_RULES_NOT_PRESERVED")
        self.assertEqual(entry.read_bytes(), before)

        description = maintenance.render_description(
            "Goal: confirmed test assistant.\nNon-goal: moving project files.\n", origin="confirmed")
        description_only = self.make_plan(workspace, {"scaffold-description.md": description},
                                           "adoption-description-only.json", "updated")
        rejected, code = maintenance.run("apply", str(workspace), str(description_only), True, None)
        self.assertEqual(code, 1)
        self.assertEqual(rejected["errors"][0]["code"], "ADOPTION_FORMAL_INCOMPLETE")
        self.assertEqual(entry.read_bytes(), before)
        self.assertFalse((workspace / "scaffold-description.md").exists())
        self.assertFalse((workspace / ".agent").exists())

    def test_skill_requires_read_only_adoption_and_existing_rule_authority(self) -> None:
        skill = " ".join((ROOT / ".agents/skills/agent-scaffold-maintenance/SKILL.md")
                        .read_text(encoding="utf-8").split()).lower()
        for contract in (
            "existing-unmanaged",
            "description must not be inferred from readme",
            "preserve the complete original rule body",
            "stop and ask the user",
            "Skill is an execution procedure subordinate to applicable AGENTS rules",
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract.lower(), skill)

    def test_backup_restore_and_explicit_purge(self) -> None:
        maintenance = load_maintenance_module()
        maintenance.BACKUP_ROOT = self.tmp / "backups"
        workspace = self.copy_minimal()
        original = (workspace / "AGENTS.md").read_bytes()
        plan = self.make_plan(workspace, {"AGENTS.md": original.decode("utf-8") + "\nupdated\n"}, "apply-plan.json")
        applied, code = maintenance.run("apply", str(workspace), str(plan), True, None)
        self.assertEqual(code, 0)
        backup = applied.get("backup", {})
        self.assertIn("id", backup)
        self.assertTrue((workspace / "AGENTS.md").read_bytes() != original)
        restored, code = maintenance.run("restore", str(workspace), None, False, backup["id"])
        self.assertEqual(code, 0)
        self.assertEqual(restored["status"], "ok")
        self.assertEqual((workspace / "AGENTS.md").read_bytes(), original)
        manifest = maintenance.BACKUP_ROOT / f"{backup['id']}.json"
        self.assertTrue(manifest.is_file())
        purged, code = maintenance.run("purge", str(workspace), None, False, None, 0, str(workspace))
        self.assertEqual(code, 0)
        self.assertIn(backup["id"], {item.get("id") for item in purged["purge"]["deleted"]})

    def test_overlay_copy_excludes_private_content_but_keeps_unknown_metadata(self) -> None:
        """The validator overlay must not copy unrelated runtime/private bytes."""
        workspace = self.copy_minimal()
        private = workspace / "library" / "books.sqlite"
        private.parent.mkdir()
        private.write_bytes(b"book content that must never enter the overlay")
        unknown = workspace / ".agent" / "private-state.json"
        unknown.write_bytes(b"secret state")
        orphan = workspace / ".agent" / "skills" / "orphan-private.md"
        orphan.write_bytes(b"private source " * 100000)
        maintenance = load_maintenance_module()
        overlay = Path(maintenance.copy_workspace(str(workspace)))
        try:
            self.assertFalse((overlay / "library" / "books.sqlite").exists())
            self.assertTrue((overlay / ".agent" / "private-state.json").exists())
            self.assertEqual((overlay / ".agent" / "private-state.json").stat().st_size, 0)
            self.assertTrue((overlay / ".agent" / "skills" / "orphan-private.md").exists())
            self.assertEqual((overlay / ".agent" / "skills" / "orphan-private.md").stat().st_size, 0)
        finally:
            shutil.rmtree(overlay, ignore_errors=True)

    def test_validator_diagnostics_preserve_path_line_and_code(self) -> None:
        fixture = INVALID_ROOT / "requires-invalid-fields"
        code, report = invoke(VALIDATOR, "--json", fixture)
        self.assertEqual(code, 1)
        self.assertTrue(report["errors"])
        for item in report["errors"]:
            self.assertIsInstance(item.get("code"), str)
            self.assertIsInstance(item.get("path"), str)
            self.assertIsInstance(item.get("line"), int)
            self.assertIsInstance(item.get("message"), str)

    def test_apply_write_failure_reports_restore_state_and_paths(self) -> None:
        maintenance = load_maintenance_module()
        maintenance.BACKUP_ROOT = self.tmp / "backups"
        workspace = self.copy_minimal()
        entry = (workspace / "AGENTS.md").read_text(encoding="utf-8")
        skill_path = workspace / ".agent" / "skills" / "quiet-check.md"
        skill = skill_path.read_text(encoding="utf-8")
        plan = self.make_plan(workspace, {"AGENTS.md": entry + "\nwrite\n", ".agent/skills/quiet-check.md": skill + "\nwrite\n"}, "write-failure.json")
        original_replace = maintenance.os.replace
        calls = {"count": 0}

        def fail_after_first(source, target):
            calls["count"] += 1
            if calls["count"] >= 2:
                raise OSError("injected persistent write failure")
            return original_replace(source, target)

        maintenance.os.replace = fail_after_first
        try:
            result, code = maintenance.run("apply", str(workspace), str(plan), True, None)
        finally:
            maintenance.os.replace = original_replace
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["workspaceState"], "partial-apply")
        self.assertTrue(any(item["code"] == "APPLY_WRITE_FAILED" and item.get("path") for item in result["errors"]))
        self.assertTrue(any(item["code"] == "APPLY_RESTORE_FAILED" and item.get("path") for item in result["errors"]))

    def test_git_package_source_failure_is_structured_without_workspace_write(self) -> None:
        maintenance = load_maintenance_module()
        package_root = self.tmp / "package-source"
        package_root.mkdir()
        before = sorted(package_root.rglob("*"))
        with patch.object(maintenance.subprocess, "run", side_effect=FileNotFoundError("git")):
            with self.assertRaises(maintenance.PlanError) as failed:
                maintenance.package_source("git+https://example.invalid/package.git", str(package_root))
        self.assertEqual(failed.exception.code, "PACKAGE_SOURCE_FAILED")
        self.assertEqual(sorted(package_root.rglob("*")), before)

    def test_post_apply_validation_failure_reports_restore_state(self) -> None:
        maintenance = load_maintenance_module()
        maintenance.BACKUP_ROOT = self.tmp / "backups"
        workspace = self.copy_minimal()
        entry = (workspace / "AGENTS.md").read_text(encoding="utf-8")
        plan = self.make_plan(workspace, {"AGENTS.md": entry + "\nvalidation\n"}, "validation-failure.json")
        validator = maintenance.validate_scaffold.validate
        calls = {"count": 0}
        def fail_after_apply(root):
            calls["count"] += 1
            return validator(root) if calls["count"] == 1 else [{"code": "INJECTED", "path": "AGENTS.md", "line": 1, "message": "injected validation failure"}]
        maintenance.validate_scaffold.validate = fail_after_apply
        try:
            result, code = maintenance.run("apply", str(workspace), str(plan), True, None)
        finally:
            maintenance.validate_scaffold.validate = validator
        self.assertEqual(code, 1)
        self.assertEqual(result["workspaceState"], "restored-after-validation-failure")
        self.assertTrue(any(item["code"] == "APPLY_VALIDATION_FAILED" for item in result["errors"]))
        self.assertTrue(any(item["code"] == "INJECTED" and item.get("line") == 1 for item in result["errors"]))
        self.assertEqual((workspace / "AGENTS.md").read_text(encoding="utf-8"), entry)

    def test_purge_cutoff_malformed_and_delete_failure_are_isolated(self) -> None:
        maintenance = load_maintenance_module()
        maintenance.BACKUP_ROOT = self.tmp / "backups"
        maintenance.BACKUP_ROOT.mkdir()
        workspace = self.workspace("purge-workspace")
        now = maintenance.utc_now()
        old_id, fresh_id, fail_id = ("11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222", "33333333-3333-4333-8333-333333333333")

        def manifest(identifier, created):
            return {"schemaVersion": 1, "id": identifier, "createdAt": maintenance.format_utc(created), "workspace": str(workspace), "planSchemaVersion": 1, "protectedFiles": ["AGENTS.md"], "files": {"AGENTS.md": None}}

        (maintenance.BACKUP_ROOT / f"{old_id}.json").write_text(json.dumps(manifest(old_id, now - maintenance.dt.timedelta(hours=2))), encoding="utf-8")
        (maintenance.BACKUP_ROOT / f"{fresh_id}.json").write_text(json.dumps(manifest(fresh_id, now)), encoding="utf-8")
        (maintenance.BACKUP_ROOT / "malformed.json").write_text("{broken", encoding="utf-8")
        result = maintenance.purge_backups(1, str(workspace))
        self.assertIn(old_id, {item["id"] for item in result["deleted"]})
        self.assertIn(fresh_id, {item["id"] for item in result["retained"]})
        self.assertTrue(any(item["id"] == "malformed" for item in result["errors"]))

        (maintenance.BACKUP_ROOT / f"{fail_id}.json").write_text(json.dumps(manifest(fail_id, now - maintenance.dt.timedelta(hours=2))), encoding="utf-8")
        original_unlink = Path.unlink

        def fail_unlink(path, *args, **kwargs):
            if path.name == f"{fail_id}.json":
                raise OSError("injected delete failure")
            return original_unlink(path, *args, **kwargs)

        Path.unlink = fail_unlink
        try:
            failed = maintenance.purge_backups(1, str(workspace))
        finally:
            Path.unlink = original_unlink
        self.assertTrue(any(item["id"] == fail_id and item["code"] == "BACKUP_DELETE_FAILED" for item in failed["errors"]))
        self.assertTrue((maintenance.BACKUP_ROOT / f"{fail_id}.json").exists())

    def test_new_workspace_apply_and_restore_removes_created_scaffold(self) -> None:
        maintenance = load_maintenance_module()
        maintenance.BACKUP_ROOT = self.tmp / "backups"
        workspace = self.tmp / "new-workspace"
        files = {"AGENTS.md": (MINIMAL / "AGENTS.md").read_text(encoding="utf-8"), ".agent/skills/quiet-check.md": (MINIMAL / ".agent" / "skills" / "quiet-check.md").read_text(encoding="utf-8")}
        plan = self.make_plan(workspace, files, "new-plan.json")
        result, code = maintenance.run("apply", str(workspace), str(plan), True, None)
        self.assertEqual(code, 0)
        backup_id = result["backup"]["id"]
        self.assertTrue(workspace.is_dir())
        restored, code = maintenance.run("restore", str(workspace), None, False, backup_id)
        self.assertEqual(code, 0)
        self.assertFalse(workspace.exists())

    def test_reading_assistant_copy_is_read_only_and_domain_agnostic(self) -> None:
        if not READING_ASSISTANT.is_dir():
            self.skipTest(f"consumer sample is unavailable: {READING_ASSISTANT}")
        consumer = self.tmp / "reading-assistant"
        shutil.copytree(READING_ASSISTANT, consumer)
        private_files = sorted(path for root in (consumer / "books", consumer / "memory")
                               if root.is_dir() for path in root.rglob("*") if path.is_file())
        before = {path.relative_to(consumer): (path.read_bytes(), path.stat().st_mtime_ns)
                  for path in private_files}
        code, inspected = invoke(CLI, "inspect", consumer)
        self.assertEqual(code, 0)
        self.assertEqual(inspected["scaffold"], "valid")
        self.assertFalse(inspected["errors"])

        skill = consumer / ".agent" / "skills" / "book-router.md"
        original = skill.read_text(encoding="utf-8")
        maintenance = load_maintenance_module()
        updated = original + "\nconsumer probe\n"
        manifest = maintenance.load_package_manifest(str(consumer))
        manifest["files"][".agent/skills/book-router.md"] = digest(updated.encode("utf-8"))
        files = {
            ".agent/skills/book-router.md": updated,
            maintenance.DESCRIPTION_FILE: (consumer / maintenance.DESCRIPTION_FILE).read_bytes().decode("utf-8"),
            maintenance.MANIFEST_FILE: json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        }
        plan = self.make_plan(consumer, files, "consumer-plan.json", description_sync="unchanged")
        code, checked = invoke(CLI, "check", consumer, "--plan-file", plan)
        if inspected["delivery"]["status"] == "legacy":
            self.assertEqual(code, 1, checked)
            self.assertTrue(any(item["code"] == "DELIVERY_EVALUATION_MISSING" for item in checked["errors"]), checked)
        else:
            self.assertEqual(code, 0, checked)
            self.assertEqual(checked["status"], "ok")
        after = {path.relative_to(consumer): (path.read_bytes(), path.stat().st_mtime_ns)
                 for path in private_files}
        self.assertEqual(before, after)
        self.assertEqual((READING_ASSISTANT / "AGENTS.md").read_bytes(), (consumer / "AGENTS.md").read_bytes())

    def test_all_invalid_fixtures_fail_validator(self) -> None:
        fixture_dirs = sorted(path for path in INVALID_ROOT.iterdir() if path.is_dir())
        self.assertGreater(len(fixture_dirs), 10)
        for fixture in fixture_dirs:
            code, result = invoke(VALIDATOR, "--json", fixture)
            self.assertEqual(code, 1, fixture.name)
            self.assertGreater(result["counts"]["errors"], 0, fixture.name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
