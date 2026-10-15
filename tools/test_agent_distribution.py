"""Formal delivery, protected maintenance and distribution regression checks."""

import functools
import http.server
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
import zipfile

try:
    from .test_agent_scaffold import ROOT, CLI, MINIMAL, digest, invoke, load_maintenance_module
except ImportError:
    from test_agent_scaffold import ROOT, CLI, MINIMAL, digest, invoke, load_maintenance_module


SKILL_CLI = ROOT / ".agents/skills/agent-scaffold-maintenance/tools/agent_scaffold.py"


def make_formal(workspace: Path, version="1.0.0", redacted=False, evaluation=True) -> dict:
    maintenance = load_maintenance_module()
    workspace.mkdir(parents=True, exist_ok=True)
    entry = (MINIMAL / "AGENTS.md").read_text(encoding="utf-8")
    entry = entry.replace("---\n\n# Personal", "  - id: archive\n    path: .agent/skills/archive.md\n  - id: agent-architect\n    path: .agent/skills/agent-architect.md\n---\n\n# Personal", 1)
    entry += "\nDesign baseline: scaffold-description.md\nPackage: .agent/scaffold-manifest.json\n"
    if evaluation:
        entry = entry.replace("---\n\n# Personal", "  - id: scaffold-evaluate\n    path: .agent/skills/scaffold-evaluate.md\n---\n\n# Personal", 1)
    cli_rel = ".agent/architect/tools/agent_scaffold.py"
    body = "# Design baseline\n\nGoal: inspect the workspace and report evidence.\nPositioning: local assistant.\nTradeoff: deterministic checks.\nNon-goal: a new runtime.\nAcceptance: a check result and archive boundary.\nKnown gaps: host compatibility needs evidence.\nSimplification: no persistent targets.\nRules: AGENTS.md and .agent/skills/quiet-check.md.\n"
    files = {"AGENTS.md": entry,
             "scaffold-description.md": maintenance.render_description(body),
             ".agent/skills/quiet-check.md": (MINIMAL / ".agent/skills/quiet-check.md").read_text(encoding="utf-8").replace("宿主按声明执行，本 change 只声明元数据，不提供实现。", "Run the bundled static validator and return its concrete result.")}
    for ident, purpose in (("archive", "Archive the complete ordered session transcript"), ("agent-architect", "Maintain this assistant with a confirmed complete file plan")):
        files[f".agent/skills/{ident}.md"] = f'''---
id: {ident}
purpose: "{purpose}"
triggers:
  - "explicit {ident} request"
excludes:
  - "ordinary conversation"
inputs:
  - "user request"
outputs:
  - "structured result"
sideEffects: writes-workspace
confirmation: required
implementation: "The host follows these concrete rules."
---
'''
    files[".agent/skills/archive.md"] += '\n# Archive\nAuthorization: candidate-confirm. No configured targets: return no-persistence-target and write nothing. Preserve roles and behavior results in the complete ordered transcript.\n\n```agent-archive\n{"authorization":"candidate-confirm","targets":[]}\n```\n'
    instructions = (ROOT / ".agents/skills/agent-scaffold-maintenance/SKILL.md").read_text(encoding="utf-8").split("---", 2)[2]
    files[".agent/skills/agent-architect.md"] += "\nCLI: " + cli_rel + "\n" + instructions.replace("{baseDir}/tools", ".agent/architect/tools")
    protected = [".agent/skills/agent-architect.md"]
    tool_names = ("agent_scaffold.py", "validate_scaffold.py", "agent_asset_migration.py")
    if evaluation:
        tool_names += ("agent_evaluation.py",)
    for name in tool_names:
        rel = ".agent/architect/tools/" + name
        files[rel] = (ROOT / "tools" / name).read_text(encoding="utf-8")
        protected.append(rel)
    if evaluation:
        try:
            from . import release_architect_bundle as release
        except ImportError:
            import release_architect_bundle as release
        files[".agent/skills/scaffold-evaluate.md"] = release.evaluation_skill_bytes().decode("utf-8")
        files[".agent/architect/tools/scaffold-evaluate.md"] = (ROOT / ".agents/skills/scaffold-evaluate/SKILL.md").read_text(encoding="utf-8")
        protected.extend((".agent/skills/scaffold-evaluate.md", ".agent/architect/tools/scaffold-evaluate.md"))
    for rel, content in files.items():
        target = workspace / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
    description = maintenance.validate_scaffold.description_info(str(workspace))
    manifest = {"schemaVersion": 1, "packageVersion": version, "contractVersion": "1", "source": "personal-agent",
                "files": {rel: digest((workspace / rel).read_bytes()) for rel in files}, "protectedFiles": protected,
                "migrationVersion": "1",
                "entrypoints": {"initialSkill": "quiet-check", "archiveSkill": "archive",
                                "architectSkill": "agent-architect", "architectCli": cli_rel,
                                "migrationTool": ".agent/architect/tools/agent_asset_migration.py"},
                "exclusions": [], "description": {"path": "scaffold-description.md", **{key: description[key] for key in ("baselineVersion", "origin", "contentSha256")}, "redacted": redacted}}
    if evaluation:
        manifest["evaluationVersion"] = "1"
        manifest["entrypoints"]["evaluationSkill"] = "scaffold-evaluate"
    (workspace / maintenance.MANIFEST_FILE).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
    return manifest


class DistributionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="agent-distribution-")
        self.tmp = Path(self.directory.name)
        self.maintenance = load_maintenance_module()
        self.maintenance.BACKUP_ROOT = self.tmp / "backups"

    def tearDown(self):
        self.directory.cleanup()

    def formal(self, name="formal", **kwargs):
        root = self.tmp / name
        make_formal(root, **kwargs)
        return root

    def plan(self, root, files, sync="unchanged"):
        value = {"schemaVersion": 1, "workspace": str(root), "baseline": {rel: digest((root / rel).read_bytes()) if (root / rel).exists() else None for rel in files}, "files": files, "descriptionSync": sync}
        path = self.tmp / "plan.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def reviewed_pack(self, root, output="package"):
        candidate = self.tmp / "redacted-description.md"
        info = self.maintenance.validate_scaffold.description_info(str(root))
        candidate.write_text(self.maintenance.render_description(info["body"].replace("local assistant", "portable assistant"), info["baselineVersion"], info["origin"]), encoding="utf-8", newline="\n")
        review = self.tmp / "redaction.json"
        review.write_text(json.dumps({"status": "approved", "unresolved": [], "removed": ["private background"], "generalized": ["positioning"], "sourceBaselineVersion": info["baselineVersion"], "sourceDescriptionSha256": digest((root / "scaffold-description.md").read_bytes()), "candidateSha256": digest(candidate.read_bytes())}), encoding="utf-8")
        result, code = self.maintenance.package_operation("pack", str(root), None, False, str(self.tmp / output), str(candidate), str(review))
        self.assertEqual(code, 0, result)
        return self.tmp / output, candidate, review

    def test_bundled_modules_commands_states_and_diagnostics_are_identical(self):
        for name in ("agent_scaffold.py", "validate_scaffold.py", "agent_evaluation.py", "agent_asset_migration.py"):
            self.assertEqual((CLI.parent / name).read_bytes(), (SKILL_CLI.parent / name).read_bytes())
        self.assertEqual((ROOT / ".agents/skills/scaffold-evaluate/SKILL.md").read_bytes(), (SKILL_CLI.parent / "scaffold-evaluate.md").read_bytes())
        helps = [subprocess.run([sys.executable, str(script), "--help"], capture_output=True, check=True).stdout for script in (CLI, SKILL_CLI)]
        self.assertEqual(helps[0], helps[1])
        for script in (CLI, SKILL_CLI):
            missing = self.tmp / ("missing-" + str(len(str(script))))
            self.assertEqual(invoke(script, "inspect", missing)[1]["scaffold"], "missing")
            purge = invoke(script, "purge", "--older-than-hours", "999999", "--workspace-filter", missing)[1]
            self.assertTrue({"deleted", "retained", "errors"}.issubset(purge["purge"]))
            self.assertEqual(purge["purge"]["deleted"], [])
            legacy = self.tmp / ("legacy-" + str(len(str(script))))
            shutil.copytree(MINIMAL, legacy)
            (legacy / "AGENTS.md").rename(legacy / "AGENT.md")
            self.assertEqual(invoke(script, "inspect", legacy)[1]["scaffold"], "legacy")
            external = self.tmp / ("external-" + str(len(str(script))))
            external.mkdir()
            (external / "CLAUDE.md").write_text("host entry", encoding="utf-8")
            self.assertTrue(any(item["code"] == "WORKSPACE_ENTRY_UNSUPPORTED" for item in invoke(script, "inspect", external)[1]["warnings"]))
            minimal = self.tmp / ("minimal-" + str(len(str(script))))
            shutil.copytree(MINIMAL, minimal)
            self.assertEqual(invoke(script, "inspect", minimal)[1]["scaffold"], "valid")
            self.assertEqual(invoke(script, "verify", minimal)[0], 0)
            (minimal / "AGENTS.md").write_text("invalid", encoding="utf-8")
            invalid = invoke(script, "inspect", minimal)[1]
            self.assertEqual(invalid["classification"], "existing-unmanaged")
            self.assertEqual(invalid["scaffold"], "missing")
            self.assertTrue(any(item["code"] == "WORKSPACE_UNMANAGED_ENTRY" for item in invalid["warnings"]))

    def test_formal_delivery_and_minimal_init_boundary(self):
        formal = self.formal()
        self.assertEqual(self.maintenance.formal_delivery(str(formal))["status"], "complete")
        self.assertEqual(invoke(CLI, "verify", formal, "--formal")[0], 0)
        self.assertFalse((formal / "scaffold-evaluation.md").exists())
        self.assertFalse((formal / "scaffold-score-history.md").exists())
        legacy = self.formal("legacy-formal", evaluation=False)
        self.assertEqual(self.maintenance.formal_delivery(str(legacy))["status"], "legacy")
        self.assertEqual(invoke(CLI, "verify", legacy)[0], 0)
        self.assertEqual(invoke(CLI, "verify", legacy, "--formal")[0], 1)
        minimal = self.tmp / "init"
        subprocess.run([sys.executable, str(ROOT / "tools/agent_init.py"), str(minimal), "--agent-id", "example", "--purpose", "local task"], check=True, capture_output=True)
        self.assertEqual(invoke(CLI, "verify", minimal)[0], 0)
        self.assertEqual(invoke(CLI, "verify", minimal, "--formal")[1]["delivery"]["status"], "intermediate")
        (formal / "scaffold-description.md").unlink()
        self.assertEqual(self.maintenance.formal_delivery(str(formal))["status"], "intermediate")

    def test_protected_files_scope_and_description_omission_are_blocked(self):
        root = self.formal()
        cases = ({".agent/skills/agent-architect.md": None},
                 {".agent/architect/tools/agent_scaffold.py": "bad"},
                 {".agent/skills/scaffold-evaluate.md": None},
                 {".agent/architect/tools/agent_evaluation.py": "bad"},
                 {".agent/architect/tools/agent_asset_migration.py": "bad"},
                 {"scaffold-evaluation.md": "report"}, {"scaffold-score-history.md": "history"},
                 {"AGENTS.md": (root / "AGENTS.md").read_text(encoding="utf-8") + "\nchanged\n"},
                 {"memory/private.md": "private"})
        for files in cases:
            result, code = self.maintenance.run("apply", str(root), str(self.plan(root, files)), True, None)
            self.assertEqual(code, 1, result)
            self.assertIn(result["errors"][0]["code"], ("ARCHITECT_PROTECTED", "DESCRIPTION_SYNC_MISSING", "PLAN_PATH_SCOPE"))

    def test_formal_creation_uses_null_baselines_and_archive_policy_is_real(self):
        source = self.formal()
        manifest = self.maintenance.load_package_manifest(str(source))
        target = self.tmp / "created"
        files = {rel: (source / rel).read_text(encoding="utf-8") for rel in (*manifest["files"], self.maintenance.MANIFEST_FILE)}
        plan = self.plan(target, files, "updated")
        self.assertTrue(all(value is None for value in json.loads(plan.read_text())["baseline"].values()))
        result, code = self.maintenance.run("apply", str(target), str(plan), False, None)
        self.assertEqual(result["status"], "needs-confirmation", result)
        self.assertFalse(target.exists())
        result, code = self.maintenance.run("apply", str(target), str(plan), True, None)
        self.assertEqual(code, 0, result)
        self.assertEqual(self.maintenance.formal_delivery(str(target))["status"], "complete")
        archive = target / ".agent/skills/archive.md"
        archive.write_text(archive.read_text(encoding="utf-8").replace("sideEffects: writes-workspace", "sideEffects: none"), encoding="utf-8", newline="\n")
        result = self.maintenance.formal_delivery(str(target))
        self.assertTrue(any(item["code"] == "DELIVERY_ARCHIVE_POLICY" for item in result["errors"]))

    def test_existing_unmanaged_adoption_uses_complete_plan_and_preserves_root_rules(self):
        source = self.formal("adoption-source")
        source_manifest = self.maintenance.load_package_manifest(str(source))
        target = self.tmp / "adoption-target"
        target.mkdir()
        original_rules = "# Existing cross-project rules\n\nKeep each project in its original location.\n"
        (target / "AGENTS.md").write_text(original_rules, encoding="utf-8")
        (target / "README.md").write_text("# Existing projects\n", encoding="utf-8")

        files = {rel: (source / rel).read_text(encoding="utf-8") for rel in source_manifest["files"]}
        files["AGENTS.md"] += "\n\n" + original_rules
        source_manifest["files"]["AGENTS.md"] = digest(files["AGENTS.md"].encode("utf-8"))
        files[self.maintenance.MANIFEST_FILE] = json.dumps(source_manifest, ensure_ascii=False, indent=2) + "\n"
        plan = self.plan(target, files, "updated")

        checked, code = self.maintenance.run("check", str(target), str(plan), False, None)
        self.assertEqual(code, 0, checked)
        self.assertEqual(checked["status"], "ok")
        applied, code = self.maintenance.run("apply", str(target), str(plan), True, None)
        self.assertEqual(code, 0, applied)
        self.assertIn(original_rules, (target / "AGENTS.md").read_text(encoding="utf-8"))
        self.assertEqual(self.maintenance.formal_delivery(str(target))["status"], "complete")
        self.assertEqual((target / "README.md").read_text(encoding="utf-8"), "# Existing projects\n")

    def test_unchanged_description_and_metadata_sync_share_the_plan(self):
        root = self.formal()
        description = root / "scaffold-description.md"
        before = description.read_bytes(), description.stat().st_mtime_ns
        entry = (root / "AGENTS.md").read_text(encoding="utf-8") + "\nA corrected rule pointer.\n"
        manifest = self.maintenance.load_package_manifest(str(root))
        manifest["files"]["AGENTS.md"] = digest(entry.encode("utf-8"))
        files = {"AGENTS.md": entry, "scaffold-description.md": description.read_text(encoding="utf-8"), self.maintenance.MANIFEST_FILE: json.dumps(manifest)}
        plan = self.plan(root, files)
        result, code = self.maintenance.run("apply", str(root), str(plan), False, None)
        self.assertEqual(result["status"], "needs-confirmation", result)
        result, code = self.maintenance.run("apply", str(root), str(plan), True, None)
        self.assertEqual(code, 0, result)
        self.assertEqual(before, (description.read_bytes(), description.stat().st_mtime_ns))
        stale = json.loads(plan.read_text())
        stale["baseline"]["scaffold-description.md"] = "0" * 64
        plan.write_text(json.dumps(stale), encoding="utf-8")
        self.assertEqual(self.maintenance.run("apply", str(root), str(plan), True, None)[1], 1)

    def test_description_version_hash_reconstructed_and_write_failure_recovery(self):
        root = self.formal()
        old = {rel: (root / rel).read_bytes() for rel in ("AGENTS.md", "scaffold-description.md", self.maintenance.MANIFEST_FILE)}
        info = self.maintenance.validate_scaffold.description_info(str(root))
        updated = self.maintenance.render_description(info["body"] + "\nGap repaired: rules now cover pointers.\n", 2)
        entry = old["AGENTS.md"].decode() + "\nUpdated rules.\n"
        manifest = self.maintenance.load_package_manifest(str(root))
        manifest["files"].update({"AGENTS.md": digest(entry.encode()), "scaffold-description.md": digest(updated.encode())})
        manifest["description"].update(baselineVersion=2, contentSha256=digest(self.maintenance.validate_scaffold.description_body(updated).encode()))
        files = {"AGENTS.md": entry, "scaffold-description.md": updated, self.maintenance.MANIFEST_FILE: json.dumps(manifest)}
        plan = self.plan(root, files, "updated")
        replace = self.maintenance.os.replace
        count = 0
        def fail_once(source, target):
            nonlocal count
            count += 1
            if count == 3:
                raise OSError("injected Description write failure")
            return replace(source, target)
        with mock.patch.object(self.maintenance.os, "replace", side_effect=fail_once):
            result, code = self.maintenance.run("apply", str(root), str(plan), True, None)
        self.assertEqual(code, 1, result)
        self.assertEqual(old, {rel: (root / rel).read_bytes() for rel in old})
        result, code = self.maintenance.run("apply", str(root), str(plan), True, None)
        self.assertEqual(code, 0, result)
        self.assertEqual(self.maintenance.validate_scaffold.description_info(str(root))["baselineVersion"], 2)
        legacy = self.tmp / "reconstructed"
        shutil.copytree(MINIMAL, legacy)
        description = self.maintenance.render_description("Rebuilt from current rules; original intent is unavailable.\n", origin="reconstructed")
        entry = (legacy / "AGENTS.md").read_text(encoding="utf-8") + "\nDesign: scaffold-description.md\n"
        plan = self.plan(legacy, {"AGENTS.md": entry, "scaffold-description.md": description}, "updated")
        self.assertEqual(self.maintenance.run("apply", str(legacy), str(plan), False, None)[0]["status"], "needs-confirmation")
        self.assertFalse((legacy / "scaffold-description.md").exists())
        self.assertEqual(self.maintenance.run("apply", str(legacy), str(plan), True, None)[1], 0)

    def test_pack_redacts_excludes_private_files_and_preserves_original(self):
        root = self.formal()
        for rel in ("memory/context.md", "books/domain.txt", "project.bin", ".agent/private-state.json", "scaffold-evaluation.md", "scaffold-score-history.md"):
            target = root / rel
            target.parent.mkdir(exist_ok=True)
            target.write_bytes(b"secret bytes")
        description = root / "scaffold-description.md"
        before = description.read_bytes(), description.stat().st_mtime_ns
        result, code = self.maintenance.package_operation("pack", str(root), None, False, str(self.tmp / "blocked"))
        self.assertEqual(result["errors"][0]["code"], "PACKAGE_REDACTION_REQUIRED")
        package, candidate, review = self.reviewed_pack(root)
        manifest = self.maintenance.load_package_manifest(str(package))
        self.assertTrue(manifest["description"]["redacted"])
        self.assertEqual(manifest["description"]["sourceBaselineVersion"], 1)
        self.assertEqual((package / "scaffold-description.md").read_bytes(), candidate.read_bytes())
        self.assertEqual(before, (description.read_bytes(), description.stat().st_mtime_ns))
        self.assertTrue(all(not (package / rel).exists() for rel in manifest["exclusions"]))
        self.assertTrue({"scaffold-evaluation.md", "scaffold-score-history.md"}.issubset(manifest["exclusions"]))
        review_data = json.loads(review.read_text())
        review_data["unresolved"] = ["private identity"]
        review.write_text(json.dumps(review_data), encoding="utf-8")
        result, code = self.maintenance.package_operation("pack", str(root), None, False, str(self.tmp / "unresolved"), str(candidate), str(review))
        self.assertEqual(result["errors"][0]["code"], "PACKAGE_SENSITIVE_UNRESOLVED")
        self.assertFalse((self.tmp / "unresolved").exists())

    def test_description_frontmatter_delimiter_in_value_and_review_hash_binding(self):
        root = self.formal()
        description = (root / "scaffold-description.md").read_text(encoding="utf-8").replace('generatorVersion: "2.0.0"', 'generatorVersion: "release---candidate"')
        (root / "scaffold-description.md").write_text(description, encoding="utf-8", newline="\n")
        manifest = self.maintenance.load_package_manifest(str(root))
        manifest["files"]["scaffold-description.md"] = digest(description.encode())
        (root / self.maintenance.MANIFEST_FILE).write_text(json.dumps(manifest), encoding="utf-8")
        info = self.maintenance.validate_scaffold.description_info(str(root))
        self.assertFalse(info["errors"])
        entry = (root / "AGENTS.md").read_text(encoding="utf-8") + "\nchange\n"
        manifest["files"]["AGENTS.md"] = digest(entry.encode())
        plan = self.plan(root, {"AGENTS.md": entry, "scaffold-description.md": description, self.maintenance.MANIFEST_FILE: json.dumps(manifest)})
        self.assertEqual(self.maintenance.run("check", str(root), str(plan), False, None)[0]["status"], "ok")
        package, candidate, review = self.reviewed_pack(root)
        candidate.write_text(candidate.read_text(encoding="utf-8") + "unreviewed private token\n", encoding="utf-8")
        result, code = self.maintenance.package_operation("pack", str(root), None, False, str(self.tmp / "tampered"), str(candidate), str(review))
        self.assertEqual(result["errors"][0]["code"], "PACKAGE_REDACTION_STALE")
        self.assertFalse((self.tmp / "tampered").exists())

    def test_install_upgrade_conflicts_deletion_backup_and_user_preservation(self):
        producer = self.formal()
        package, _, _ = self.reviewed_pack(producer)
        target = self.tmp / "installed"
        result, code = self.maintenance.package_operation("install", str(target), str(package), False)
        self.assertEqual(result["status"], "needs-confirmation", result)
        self.assertFalse(target.exists())
        result, code = self.maintenance.package_operation("install", str(target), str(package), True)
        self.assertEqual(code, 0, result)
        for name in ("agent_scaffold.py", "agent_evaluation.py", "agent_asset_migration.py"):
            cli = target / ".agent/architect/tools" / name
            isolated = subprocess.run([sys.executable, "-E", "-B", str(cli), "--help"], cwd=target, capture_output=True)
            self.assertEqual(isolated.returncode, 0, isolated.stderr)
        self.assertEqual(self.maintenance.package_operation("package-check", str(package), str(package), False)[1], 0)
        self.assertEqual(self.maintenance.formal_delivery(str(target))["entrypoints"]["evaluationSkill"], "scaffold-evaluate")
        for rel in ("user-project.py", "memory/context.md", "books/domain.txt"):
            path = target / rel
            path.parent.mkdir(exist_ok=True)
            path.write_text("private", encoding="utf-8")
        users = {rel: ((target / rel).read_bytes(), (target / rel).stat().st_mtime_ns) for rel in ("AGENTS.md", "scaffold-description.md", ".agent/skills/quiet-check.md", "user-project.py", "memory/context.md", "books/domain.txt")}
        incoming = self.formal("v2", version="2.0.0", redacted=True)
        manifest = self.maintenance.load_package_manifest(str(incoming))
        obsolete = ".agent/architect/tools/obsolete.py"
        old_manifest = self.maintenance.load_package_manifest(str(target))
        (target / obsolete).write_text("old\n", encoding="utf-8")
        old_manifest["protectedFiles"].append(obsolete)
        old_manifest["files"][obsolete] = digest((target / obsolete).read_bytes())
        (target / self.maintenance.MANIFEST_FILE).write_text(json.dumps(old_manifest), encoding="utf-8")
        cli = manifest["entrypoints"]["architectCli"]
        (incoming / cli).write_text((incoming / cli).read_text(encoding="utf-8") + "\n# Upgrade probe\n", encoding="utf-8", newline="\n")
        manifest["files"][cli] = digest((incoming / cli).read_bytes())
        (incoming / self.maintenance.MANIFEST_FILE).write_text(json.dumps(manifest), encoding="utf-8")
        result, code = self.maintenance.package_operation("upgrade", str(target), str(incoming), True)
        self.assertEqual(code, 0, result)
        self.assertFalse((target / obsolete).exists())
        self.assertEqual(users, {rel: ((target / rel).read_bytes(), (target / rel).stat().st_mtime_ns) for rel in users})
        restored, code = self.maintenance.run("restore", str(target), None, False, result["backup"]["id"])
        self.assertEqual(code, 0, restored)
        self.assertTrue((target / obsolete).exists())
        (target / cli).write_text("local edit\n", encoding="utf-8")
        before = (target / cli).read_bytes()
        result, code = self.maintenance.package_operation("upgrade", str(target), str(incoming), True)
        self.assertEqual(result["status"], "conflict", result)
        self.assertEqual((target / cli).read_bytes(), before)

    def test_internal_rollback_preserves_concurrent_unwritten_description(self):
        root = self.formal()
        description = root / "scaffold-description.md"
        original = (root / "AGENTS.md").read_bytes()
        entry = original.decode() + "\nchanged\n"
        info = self.maintenance.validate_scaffold.description_info(str(root))
        updated = self.maintenance.render_description(info["body"] + "Updated gap.\n", 2)
        manifest = self.maintenance.load_package_manifest(str(root))
        manifest["files"].update({"AGENTS.md": digest(entry.encode()), "scaffold-description.md": digest(updated.encode())})
        manifest["description"].update(baselineVersion=2, contentSha256=digest(self.maintenance.validate_scaffold.description_body(updated).encode()))
        plan = self.plan(root, {"AGENTS.md": entry, "scaffold-description.md": updated, self.maintenance.MANIFEST_FILE: json.dumps(manifest)}, "updated")
        replace = self.maintenance.os.replace
        def concurrent_failure(source, target):
            if Path(target) == description:
                description.write_bytes(b"concurrent update")
                raise OSError("Description write failed after external update")
            return replace(source, target)
        with mock.patch.object(self.maintenance.os, "replace", side_effect=concurrent_failure):
            result, code = self.maintenance.run("apply", str(root), str(plan), True, None)
        self.assertEqual(code, 1, result)
        self.assertEqual(description.read_bytes(), b"concurrent update")
        self.assertEqual((root / "AGENTS.md").read_bytes(), original)

    def test_git_url_install_and_path_hash_failure_leave_no_partial_target(self):
        package = self.formal(redacted=True)
        subprocess.run(["git", "init", str(package)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(package), "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(package), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "fixture"], check=True, capture_output=True)
        result, code = self.maintenance.package_operation("install", str(self.tmp / "git-target"), "git+" + str(package), True)
        self.assertEqual(code, 0, result)
        archive = self.tmp / "package.zip"
        with zipfile.ZipFile(archive, "w") as zipped:
            for rel in self.maintenance.load_package_manifest(str(package))["files"]:
                zipped.write(package / rel, rel)
            zipped.write(package / self.maintenance.MANIFEST_FILE, self.maintenance.MANIFEST_FILE)
        class QuietHandler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(QuietHandler, directory=str(self.tmp)))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result, code = self.maintenance.package_operation("install", str(self.tmp / "url-target"), f"http://127.0.0.1:{server.server_port}/package.zip", True)
            self.assertEqual(code, 0, result)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        manifest = self.maintenance.load_package_manifest(str(package))
        manifest["files"]["../escape.md"] = "0" * 64
        (package / self.maintenance.MANIFEST_FILE).write_text(json.dumps(manifest), encoding="utf-8")
        result, code = self.maintenance.package_operation("install", str(self.tmp / "invalid-target"), str(package), True)
        self.assertEqual(code, 1, result)
        self.assertFalse((self.tmp / "invalid-target").exists())


if __name__ == "__main__":
    unittest.main()
