"""Regression checks for the cross-platform interpreter smoke helper."""

from __future__ import annotations

import subprocess
import os
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    from . import platform_smoke
except ImportError:
    import platform_smoke


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / ".agents/skills/agent-scaffold-maintenance/SKILL.md"


class InterpreterProbeTests(unittest.TestCase):
    def test_macos_prefers_python3(self):
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            if argv[0] == "python3":
                return subprocess.CompletedProcess(argv, 0, "3.11.9\n", "")
            return subprocess.CompletedProcess(argv, 1, "", "not found")

        result = platform_smoke.probe_interpreters("darwin", runner=runner)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["command"], ["python3"])
        self.assertEqual(calls, [["python3", "-c", platform_smoke.VERSION_CODE]])

    def test_windows_falls_back_to_py_three(self):
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            if argv[:2] == ["py", "-3"]:
                return subprocess.CompletedProcess(argv, 0, "3.11.9\n", "")
            raise FileNotFoundError(argv[0])

        result = platform_smoke.probe_interpreters("win32", runner=runner)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["command"], ["py", "-3"])
        self.assertEqual(calls[0], ["python", "-c", platform_smoke.VERSION_CODE])

    def test_low_versions_and_missing_commands_return_cli_unavailable(self):
        def runner(argv, **kwargs):
            if argv[0] == "python3":
                return subprocess.CompletedProcess(argv, 0, "3.9.18\n", "")
            raise FileNotFoundError(argv[0])

        result = platform_smoke.probe_interpreters("darwin", runner=runner)

        self.assertEqual(result["status"], "cli-unavailable")
        self.assertEqual([item["command"] for item in result["attempts"]],
                         [["python3"], ["python"]])
        self.assertTrue(all(item["reason"] for item in result["attempts"]))

    def test_macos_falls_back_from_old_python3_to_python(self):
        def runner(argv, **kwargs):
            version = "3.9.18" if argv[0] == "python3" else "3.10.0"
            return subprocess.CompletedProcess(argv, 0, version, "")

        result = platform_smoke.probe_interpreters("darwin", runner=runner)
        self.assertEqual(result["command"], ["python"])
        self.assertEqual(len(result["attempts"]), 1)

    def test_hanging_candidate_does_not_block_fallback(self):
        def runner(argv, **kwargs):
            if argv[0] == "python3":
                raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
            return subprocess.CompletedProcess(argv, 0, "3.11.9", "")

        result = platform_smoke.probe_interpreters("darwin", runner=runner)
        self.assertEqual(result["command"], ["python"])
        self.assertIn("timeout", result["attempts"][0]["reason"])

    def test_unreadable_cli_path_is_structured(self):
        result = platform_smoke.run_cli(
            ["python3"], ROOT / "missing-agent-scaffold.py", ["--help"],
            runner=lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError("missing")),
        )

        self.assertEqual(result["status"], "cli-unavailable")
        self.assertEqual(result["path"], "missing-agent-scaffold.py")
        self.assertEqual(result["attempts"][0]["command"], ["python3"])

    def test_permission_failure_preserves_command_path_and_error(self):
        cli = ROOT / "tools/agent_scaffold.py"
        with mock.patch.object(platform_smoke.subprocess, "run", side_effect=PermissionError("denied")) as runner:
            result = platform_smoke.run_cli(["python3"], cli, ["--help"], runner=runner)
        self.assertEqual(result["status"], "cli-unavailable")
        self.assertEqual(result["path"], "tools/agent_scaffold.py")
        self.assertIn("denied", result["attempts"][0]["reason"])
        self.assertEqual(result["argv"][:3], ["python3", "-E", "-B"])

    def test_failed_startup_retains_stderr(self):
        def runner(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 2, "", "can't open file: permission denied")

        result = platform_smoke.run_cli(
            ["python3"], ROOT / "tools/agent_scaffold.py", ["--help"], runner=runner)
        self.assertEqual(result["exitCode"], 2)
        self.assertEqual(result["stderr"], "can't open file: permission denied")
        self.assertNotEqual(result["status"], "ok")

    def test_smoke_cannot_succeed_without_discovered_python(self):
        unavailable = {"status": "cli-unavailable", "attempts": [{"command": ["python3"], "reason": "missing"}]}
        with mock.patch.object(platform_smoke, "probe_interpreters", return_value=unavailable):
            with mock.patch.object(platform_smoke, "run_cli") as run:
                result = platform_smoke.run_smoke(ROOT)
        self.assertEqual(result["status"], "cli-unavailable")
        run.assert_not_called()

    def test_smoke_reuses_discovered_command(self):
        selected = {"status": "ok", "command": ["validated-python"], "version": "3.11.9", "attempts": []}
        with mock.patch.object(platform_smoke, "probe_interpreters", return_value=selected):
            with mock.patch.object(platform_smoke, "run_cli", return_value={"status": "ok"}) as run:
                result = platform_smoke.run_smoke(ROOT)
        self.assertEqual(result["status"], "ok")
        self.assertGreaterEqual(run.call_count, 6)
        self.assertTrue(all(call.args[0] == ["validated-python"] for call in run.call_args_list))

    def test_missing_git_has_structured_cli_error_without_target_writes(self):
        import sys
        with tempfile.TemporaryDirectory(prefix="platform-git-") as temporary:
            target = Path(temporary) / "missing target"
            argv = [sys.executable, "-B", str(ROOT / "tools/agent_scaffold.py"), "install",
                    str(target), "--package-source", "git+https://example.invalid/package.git", "--confirm", "--json"]
            completed = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                                       env={**os.environ, "PATH": ""}, cwd=ROOT, timeout=20)
            self.assertEqual(completed.returncode, 1, completed.stderr)
            report = json.loads(completed.stdout)
            self.assertEqual(report["errors"][0]["code"], "PACKAGE_SOURCE_FAILED")
            self.assertEqual(report["errors"][0]["path"], "git+https://example.invalid/package.git")
            self.assertFalse(target.exists())


class SkillContractTests(unittest.TestCase):
    def test_skill_documents_platform_interpreters_and_reuses_selection(self):
        text = " ".join(SKILL.read_text(encoding="utf-8").split())
        for fragment in (
            "Bash(python3 {baseDir}/tools/agent_scaffold.py:*)",
            "Bash(py -3 {baseDir}/tools/agent_scaffold.py:*)",
            "selected interpreter",
            "cli-unavailable",
            "same interpreter",
            "sys.version_info",
            "PowerShell",
            "Zsh",
        ):
            with self.subTest(fragment=fragment):
                self.assertTrue(fragment in text, fragment + " missing from canonical Skill")

    def test_evaluation_skill_is_also_usable_with_python3(self):
        text = (ROOT / ".agents/skills/scaffold-evaluate/SKILL.md").read_text(encoding="utf-8")
        self.assertTrue("python3" in text, "python3 missing from evaluation Skill")
        self.assertTrue("py -3" in text, "py -3 missing from evaluation Skill")

    def test_smoke_helper_uses_current_interpreter_without_platform_specific_files(self):
        result = platform_smoke.run_smoke(ROOT)

        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["cli"]["status"], "ok", result)
        self.assertEqual(result["validator"]["status"], "ok", result)
        self.assertEqual(result["fixtures"]["minimal"]["status"], "ok", result)
        self.assertEqual(result["fixtures"]["extended"]["status"], "ok", result)
        self.assertFalse((ROOT / "tools/agent_scaffold.bat").exists())
        self.assertFalse((ROOT / "tools/agent_scaffold.command").exists())


@unittest.skipIf(os.name == "nt", "requires POSIX filesystem permissions")
class PosixFilesystemTests(unittest.TestCase):
    def test_denied_directory_is_diagnosed_without_touching_user_files(self):
        try:
            from .test_agent_scaffold import invoke, CLI, MINIMAL
        except ImportError:
            from test_agent_scaffold import invoke, CLI, MINIMAL
        with tempfile.TemporaryDirectory(prefix="platform-permissions-") as temporary:
            workspace = Path(temporary) / "workspace with spaces"
            shutil.copytree(MINIMAL, workspace)
            private = workspace / "private"
            private.mkdir()
            secret = private / "note.txt"
            secret.write_bytes(b"preserve these user bytes")
            private.chmod(0)
            try:
                if os.access(private, os.R_OK | os.X_OK):
                    self.skipTest("account bypasses POSIX directory permissions")
                _, report = invoke(CLI, "inspect", workspace, "--json")
                self.assertFalse(report["inventory"]["complete"])
                self.assertTrue(any(item["code"] == "INVENTORY_PERMISSION_DENIED"
                                    for item in report["inventory"]["warnings"]))
            finally:
                private.chmod(0o700)
            self.assertEqual(secret.read_bytes(), b"preserve these user bytes")


if __name__ == "__main__":
    unittest.main()
