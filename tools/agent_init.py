#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Create the smallest valid Agent workspace entry."""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

AGENT_FILE = "AGENTS.md"
AGENT_DIR = ".agent"
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
LINE_BREAKS = "\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029"


class UsageError(Exception):
    """An invalid command argument or target path type."""


class InitError(Exception):
    """A valid request that cannot be completed safely."""


def _quote_scalar(value: str) -> str:
    """Quote a value for the validator's supported YAML subset."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_agent(agent_id: str, purpose: str) -> str:
    """Render a deterministic, minimal AGENTS.md with LF line endings."""
    return (
        "---\n"
        f"agentId: {_quote_scalar(agent_id)}\n"
        'contractVersion: "1"\n'
        f"purpose: {_quote_scalar(purpose)}\n"
        "---\n"
        "\n"
        "# Agent 工作空间\n"
        "\n"
        "这是一个最小的 Agent 工作空间声明。frontmatter 是机器可读契约，正文仅供阅读。\n"
        "本文件不执行 Skill、Workflow、Behavior、Context、Dream 或 Integration，\n"
        "也不代表宿主运行时已经可用。\n"
    )


def _lexists(path: Path) -> bool:
    """Include dangling symlinks in conflict checks."""
    return os.path.lexists(os.fspath(path))


def _missing_directories(path: Path) -> list[Path]:
    missing = []
    current = path
    while not _lexists(current):
        missing.append(current)
        current = current.parent
    return missing


def _inspect_target(target: Path) -> tuple[str, list[Path], list[Path]]:
    """Return (classification, conflicts, missing_dirs) without writing."""
    try:
        exists = _lexists(target)
    except (OSError, ValueError) as exc:
        raise UsageError(f"目标路径无效: {exc}") from exc

    if exists:
        if not target.is_dir():
            raise UsageError("目标路径已存在但不是目录")
        conflicts = [
            target / AGENT_FILE,
            target / AGENT_DIR,
        ]
        conflicts = [path for path in conflicts if _lexists(path)]
        if conflicts:
            return "conflict", conflicts, []
        try:
            empty = next(target.iterdir(), None) is None
        except OSError as exc:
            raise InitError(f"无法读取目标目录: {exc}") from exc
        return ("empty" if empty else "repository"), [], []

    missing = _missing_directories(target)
    existing_parent = target.parent
    while not _lexists(existing_parent):
        existing_parent = existing_parent.parent
    try:
        parent_is_dir = existing_parent.is_dir()
    except OSError as exc:
        raise InitError(f"无法检查目标父目录: {exc}") from exc
    if not parent_is_dir:
        raise UsageError("目标路径的父路径不是目录")
    return "new", [], missing


def _validate_inputs(agent_id: str, purpose: str) -> None:
    if not SLUG_RE.fullmatch(agent_id):
        raise UsageError("agent-id 必须是小写 slug，例如 personal-agent")
    if not purpose.strip() or any(
        char in LINE_BREAKS or ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F
        for char in purpose
    ):
        raise UsageError("purpose 必须是非空的单行文本且不能包含控制字符")


def _cleanup_directories(directories: list[Path]) -> None:
    for directory in reversed(directories):
        try:
            directory.rmdir()
        except OSError:
            pass


def _write_entry(target: Path, content: str, created_directories: list[Path]) -> None:
    entry = target / AGENT_FILE
    file_created = False
    try:
        with open(entry, "x", encoding="utf-8", newline="\n") as handle:
            file_created = True
            handle.write(content)
    except (OSError, UnicodeError) as exc:
        if file_created:
            try:
                entry.unlink()
            except OSError:
                pass
        _cleanup_directories(created_directories)
        raise InitError(f"写入 {entry} 失败: {exc}") from exc


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="创建最小 Agent 工作空间入口 AGENTS.md。"
    )
    parser.add_argument("target_dir", help="目标工作空间目录")
    parser.add_argument("--agent-id", required=True, help="小写 slug 形式的 Agent 标识")
    parser.add_argument("--purpose", required=True, help="非空的单行用途说明")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只显示计划或冲突，不创建或修改任何路径",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    args = _build_parser().parse_args(argv)
    try:
        _validate_inputs(args.agent_id, args.purpose)
        target = Path(args.target_dir)
        classification, conflicts, missing = _inspect_target(target)
    except UsageError as exc:
        print(f"用法错误: {exc}", file=sys.stderr)
        return 2
    except InitError as exc:
        print(f"初始化失败: {exc}", file=sys.stderr)
        return 1

    if conflicts:
        paths = ", ".join(os.fspath(path) for path in conflicts)
        print(f"目标冲突，拒绝覆盖或合并: {paths}", file=sys.stderr)
        return 1

    entry = target / AGENT_FILE
    if args.dry_run:
        if classification == "new":
            print(f"预览: 将创建目录和 {entry}")
        else:
            print(f"预览: 将在 {target} 中创建 {entry}")
        return 0

    created_directories: list[Path] = []
    try:
        if classification == "new":
            for directory in reversed(missing):
                directory.mkdir()
                created_directories.append(directory)
        _write_entry(target, render_agent(args.agent_id, args.purpose), created_directories)
    except (OSError, UnicodeError, InitError) as exc:
        if isinstance(exc, InitError):
            message = str(exc)
        else:
            _cleanup_directories(created_directories)
            message = f"创建目标目录失败: {exc}"
        print(f"初始化失败: {message}", file=sys.stderr)
        return 1

    print(f"已创建: {entry}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
