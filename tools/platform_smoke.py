#!/usr/bin/env python3
"""Run small cross-platform checks without adding a platform-specific runtime."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import platform
from pathlib import Path
import re
import subprocess
import sys
from typing import Callable, Sequence


MIN_PYTHON = (3, 10)
VERSION_CODE = "import sys; print('.'.join(map(str, sys.version_info[:3])))"
Runner = Callable[..., subprocess.CompletedProcess]


def interpreter_candidates(platform_name: str | None = None) -> tuple[list[str], ...]:
    platform_name = platform_name or sys.platform
    if platform_name == "win32":
        return (["python"], ["py", "-3"])
    return (["python3"], ["python"])


def _version(value: str) -> tuple[int, int, int] | None:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", value.strip())
    return tuple(int(part) for part in match.groups()) if match else None


def probe_interpreters(platform_name: str | None = None, *, runner: Runner = subprocess.run) -> dict:
    attempts = []
    for command in interpreter_candidates(platform_name):
        try:
            result = runner(
                [*command, "-c", VERSION_CODE],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            reason = "timeout" if isinstance(exc, subprocess.TimeoutExpired) else type(exc).__name__
            attempts.append({"command": command, "reason": reason + ": " + str(exc)})
            continue
        version_text = result.stdout.strip()
        parsed = _version(version_text)
        if result.returncode != 0:
            attempts.append({"command": command, "reason": result.stderr.strip() or "command-failed"})
            continue
        if parsed is None or parsed[:2] < MIN_PYTHON:
            attempts.append({"command": command, "reason": "python-version-too-old: " + version_text})
            continue
        return {"status": "ok", "command": command, "version": version_text, "attempts": attempts}
    return {"status": "cli-unavailable", "attempts": attempts}


def _execute(argv: list[str], cwd: Path, *, runner: Runner = subprocess.run, timeout: int = 30) -> dict:
    record = {"argv": argv, "cwd": str(cwd)}
    try:
        result = runner(argv, capture_output=True, text=True, encoding="utf-8",
                        errors="replace", cwd=str(cwd), timeout=timeout, shell=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return {**record, "status": "cli-unavailable", "reason": str(exc)}
    return {**record, "status": "ok" if result.returncode == 0 else "cli-error",
            "exitCode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}


def _display_path(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def run_cli(
    interpreter: Sequence[str],
    cli: Path,
    args: Sequence[str],
    *,
    runner: Runner = subprocess.run,
    cwd: Path | None = None,
) -> dict:
    command = list(interpreter)
    display = _display_path(Path(__file__).resolve().parents[1], cli)
    argv = [*command, "-E", "-B", str(cli), *args]
    record = {"path": display, "command": command, "argv": argv}
    try:
        readable = cli.is_file() and not cli.is_symlink()
    except OSError as exc:
        readable = False
        record["reason"] = str(exc)
    if not readable:
        return {
            **record,
            "status": "cli-unavailable",
            "attempts": [{"command": command, "reason": record.get("reason", "cli-path-unreadable")}],
        }
    result = {**record, **_execute(argv, cwd or cli.parent, runner=runner)}
    if result["status"] == "cli-unavailable":
        result["attempts"] = [{"command": command, "reason": result["reason"]}]
    return result


def run_smoke(root: Path, *, regression: bool = False) -> dict:
    root = root.resolve()
    probe = probe_interpreters()
    report = {
        "schemaVersion": 1,
        "platform": sys.platform,
        "machine": platform.machine(),
        "startedAtUtc": datetime.now(timezone.utc).isoformat(),
        "python": sys.version.split()[0],
        "interpreterProbe": probe,
        "hostRuntime": {"status": "unverified", "reason": "tool checks do not execute an Agent host"},
    }
    if probe["status"] != "ok":
        return {**report, "status": "cli-unavailable"}
    cli = root / "tools" / "agent_scaffold.py"
    validator = root / "tools" / "validate_scaffold.py"
    interpreter = probe["command"]
    cli_result = run_cli(interpreter, cli, ["--version"], cwd=root)
    help_result = run_cli(interpreter, cli, ["--help"], cwd=root)
    validator_result = run_cli(interpreter, validator, ["--self-test"], cwd=root)
    fixtures = {
        name: run_cli(interpreter, validator, ["--json", str(root / "samples" / "agent-scaffold" / name)], cwd=root)
        for name in ("minimal", "extended")
    }
    operations = {
        name: run_cli(interpreter, cli, [name, str(root / "samples/agent-scaffold/minimal"), "--json"], cwd=root)
        for name in ("inspect", "verify")
    }
    checks = [cli_result, help_result, validator_result, *fixtures.values(), *operations.values()]
    report.update({
        "cli": cli_result,
        "help": help_result,
        "validator": validator_result,
        "fixtures": fixtures,
        "operations": operations,
    })
    if regression and all(item["status"] == "ok" for item in checks):
        report["regression"] = _execute(
            [*interpreter, "-X", "utf8", "-B", "-m", "unittest", "discover", "-s", "tools", "-p", "test_*.py", "-v"],
            root, timeout=600)
        checks.append(report["regression"])
    report["status"] = "ok" if all(item["status"] == "ok" for item in checks) else "error"
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--json", action="store_true", help="emit JSON output")
    parser.add_argument("--regression", action="store_true", help="also run and record the full regression suite")
    parser.add_argument("--output-file", type=Path, help="save a new evidence JSON file; never overwrite")
    args = parser.parse_args(argv)
    report = run_smoke(args.root, regression=args.regression)
    output = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output_file:
        with args.output_file.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(output + "\n")
    print(output)
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
