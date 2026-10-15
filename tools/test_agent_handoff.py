"""Reload the protected target CLI before accepting architect handoff."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

try:
    from .test_agent_distribution import make_formal
    from .test_agent_scaffold import digest, load_maintenance_module
except ImportError:
    from test_agent_distribution import make_formal
    from test_agent_scaffold import digest, load_maintenance_module


class ArchitectHandoffTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="agent-handoff-")
        self.tmp = Path(self.directory.name)
        self.maintenance = load_maintenance_module()
        self.maintenance.BACKUP_ROOT = self.tmp / "backups"

    def tearDown(self):
        self.directory.cleanup()

    def formal(self, name, version):
        root = self.tmp / name
        manifest = make_formal(root, version=version, redacted=True)
        manifest["source"] = self.maintenance.CANONICAL_REPOSITORY
        (root / self.maintenance.MANIFEST_FILE).write_text(json.dumps(manifest), encoding="utf-8")
        return root

    def replace_cli(self, root, old, new):
        manifest_path = root / self.maintenance.MANIFEST_FILE
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        cli = root / manifest["entrypoints"]["architectCli"]
        content = cli.read_text(encoding="utf-8")
        self.assertIn(old, content)
        cli.write_text(content.replace(old, new, 1), encoding="utf-8", newline="\n")
        manifest["files"][manifest["entrypoints"]["architectCli"]] = digest(cli.read_bytes())
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def snapshot(self, root):
        return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}

    def test_update_executes_target_version_and_formal_verification(self):
        target = self.formal("target", "1.0.0")
        incoming = self.formal("incoming", "2.1.0")
        self.replace_cli(incoming, 'TOOL_VERSION = "%s"' % self.maintenance.TOOL_VERSION, 'TOOL_VERSION = "2.1.0"')
        with mock.patch.object(self.maintenance.subprocess, "run", wraps=subprocess.run) as run:
            result, code = self.maintenance.self_update_operation(str(target), str(incoming), True)
        self.assertEqual(code, 0, result)
        self.assertEqual(result["handoff"]["toolVersion"], "2.1.0")
        self.assertEqual(result["handoff"]["verification"]["delivery"]["status"], "complete")
        cli = str(target / ".agent/architect/tools/agent_scaffold.py")
        commands = [call.args[0] for call in run.call_args_list]
        self.assertTrue(any(command[-2:] == [cli, "--version"] for command in commands), commands)
        self.assertTrue(any(command[-5:] == [cli, "verify", str(target), "--formal", "--json"] for command in commands), commands)

    def test_failed_reload_restores_update_and_retains_backup(self):
        target = self.formal("target", "1.0.0")
        incoming = self.formal("incoming", "2.0.0")
        self.replace_cli(incoming, 'version=TOOL_VERSION)', 'version="broken")')
        before = self.snapshot(target)
        result, code = self.maintenance.self_update_operation(str(target), str(incoming), True)
        self.assertEqual(code, 1, result)
        self.assertEqual(result["errors"][0]["code"], "HANDOFF_FAILED")
        self.assertEqual(result["recovery"]["status"], "ok")
        self.assertEqual(result["workspaceState"], "restored-after-handoff-failure")
        self.assertTrue(result["backup"]["id"])
        self.assertEqual(self.snapshot(target), before)

    def test_up_to_date_still_executes_target_cli_without_writing(self):
        target = self.formal("target", "2.0.0")
        before = self.snapshot(target)
        with mock.patch.object(self.maintenance.subprocess, "run", wraps=subprocess.run) as run:
            result, code = self.maintenance.self_update_operation(str(target), str(target), False)
        self.assertEqual((code, result["status"]), (0, "up-to-date"), result)
        self.assertEqual(result["handoff"]["toolVersion"], self.maintenance.TOOL_VERSION)
        self.assertEqual(len(run.call_args_list), 2)
        self.assertEqual(self.snapshot(target), before)
        self.assertNotIn("backup", result)

    def test_failed_up_to_date_verification_blocks_handoff(self):
        target = self.formal("target", "2.0.0")
        self.replace_cli(target, 'if args.operation == "verify":', 'if args.operation == "verify":\n        raise SystemExit(3)')
        before = self.snapshot(target)
        result, code = self.maintenance.self_update_operation(str(target), str(target), False)
        self.assertEqual(code, 1, result)
        self.assertEqual(result["errors"][0]["code"], "HANDOFF_FAILED")
        self.assertNotIn("handoff", result)
        self.assertNotIn("backup", result)
        self.assertEqual(self.snapshot(target), before)

    def test_failed_recovery_keeps_independent_diagnostics(self):
        target = self.formal("target", "1.0.0")
        incoming = self.formal("incoming", "2.0.0")
        self.replace_cli(incoming, 'version=TOOL_VERSION)', 'version="broken")')
        with mock.patch.object(self.maintenance, "rollback_applied", side_effect=OSError("restore unavailable")):
            result, code = self.maintenance.self_update_operation(str(target), str(incoming), True)
        self.assertEqual(code, 1, result)
        self.assertEqual([item["code"] for item in result["errors"]], ["HANDOFF_FAILED", "HANDOFF_RESTORE_FAILED"])
        self.assertEqual(result["recovery"]["status"], "error")
        self.assertEqual(result["workspaceState"], "partial-apply")
        self.assertTrue(result["backup"]["id"])


if __name__ == "__main__":
    unittest.main()
