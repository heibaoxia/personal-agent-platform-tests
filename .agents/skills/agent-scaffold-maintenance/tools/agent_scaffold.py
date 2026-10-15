#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Deterministic inspect/check/diff/apply/verify/restore for Agent scaffolds."""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import difflib
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
import urllib.request
import urllib.parse
import urllib.error
import zipfile
from pathlib import Path

sys.dont_write_bytecode = True

import validate_scaffold


SCHEMA_VERSION = 1
TOOL_VERSION = "3.2.0"
MANIFEST_FILE = ".agent/scaffold-manifest.json"
DESCRIPTION_FILE = validate_scaffold.DESCRIPTION_FILE
ARCHITECT_ROOT = ".agent/architect/"
EVALUATION_VERSION = "1"
EVALUATION_SKILL = "scaffold-evaluate"
EVALUATION_FILE = ".agent/skills/scaffold-evaluate.md"
MIGRATION_VERSION = "1"
MIGRATION_TOOL = "agent_asset_migration.py"
CANONICAL_REPOSITORY = "heibaoxia/My-Personal-Agent"
CANONICAL_REPOSITORY_URL = "https://github.com/" + CANONICAL_REPOSITORY
CANONICAL_API_URL = "https://api.github.com/repos/%s/releases/latest" % CANONICAL_REPOSITORY
RELEASE_ASSET_PREFIX = "architect-bundle-"
RELEASE_ASSET_SUFFIX = ".zip"
RELEASE_TIMEOUT_SECONDS = 30
RELEASE_METADATA_LIMIT = 2 * 1024 * 1024
RELEASE_ARCHIVE_LIMIT = 64 * 1024 * 1024
BACKUP_ROOT = Path(tempfile.gettempdir()) / "personal-agent-scaffold-backups"
BACKUP_ID_RE = re.compile(r"^[0-9a-f-]{36}$")
LEGACY_ENTRY = "AGENT.md"
EXTERNAL_ENTRY_NAMES = ("CLAUDE.md", "GEMINI.md", "NANOBOT.md", "PI.md")
BACKUP_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
INVENTORY_MAX_DEPTH = 12
INVENTORY_MAX_ENTRIES = 10000
INVENTORY_RULE_MAX_BYTES = 256 * 1024
INVENTORY_RULE_TOTAL_BYTES = 4 * 1024 * 1024
INVENTORY_PRUNED_DIRECTORIES = frozenset({
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
    ".cache", "cache", "build", "dist", "target", "coverage", ".next",
    ".turbo", "vendor",
})


class PlanError(Exception):
    def __init__(self, code: str, message: str, path: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.path = path


def error(code: str, message: str, path: str | None = None, line: int | None = None) -> dict:
    return {"code": code, "path": path, "line": line, "message": message}


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def format_utc(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).strftime(BACKUP_TIME_FORMAT)


def parse_backup_time(value: object) -> dt.datetime:
    if not isinstance(value, str):
        raise ValueError("createdAt must be an ISO-8601 UTC timestamp")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("createdAt must be an ISO-8601 UTC timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("createdAt must include a UTC offset")
    return parsed.astimezone(dt.timezone.utc)


def abs_workspace(value: str) -> str:
    return os.path.abspath(os.path.expanduser(value))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _manifest_pairs(pairs):
    """Reject duplicate JSON keys instead of silently accepting the last one."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("manifest contains duplicate key: %s" % key)
        result[key] = value
    return result


def safe_rel(path: str) -> str:
    if not isinstance(path, str) or not path.strip():
        raise PlanError("PLAN_PATH_EMPTY", "plan path must be a non-empty string")
    normalized = path.replace("\\", "/")
    if normalized.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", normalized):
        raise PlanError("PLAN_PATH_ABSOLUTE", "plan path must be relative", path)
    parts = normalized.split("/")
    if any(part in ("", ".", "..") or re.search(r'[:<>"|?*\x00-\x1f]', part) or part.endswith((".", " "))
           or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part) for part in parts):
        raise PlanError("PLAN_PATH_ESCAPE", "plan path must not contain empty, '.' or '..' segments", path)
    normalized = "/".join(parts)
    allowed = normalized in ("AGENT.md", "AGENTS.md", DESCRIPTION_FILE, MANIFEST_FILE)
    allowed = allowed or normalized in (".agent/context.yaml", ".agent/behaviors.md")
    allowed = allowed or bool(re.fullmatch(r"\.agent/(skills|workflows|integrations)/[a-z0-9]+(?:-[a-z0-9]+)*\.md", normalized))
    allowed = allowed or (normalized.startswith(ARCHITECT_ROOT) and normalized.endswith((".md", ".py", ".json")))
    if not allowed:
        raise PlanError("PLAN_PATH_SCOPE", "plan path is outside the controlled scaffold files", path)
    return normalized


def path_bytes(workspace: str, rel: str) -> bytes | None:
    path = safe_target_path(workspace, rel)
    if not path.exists():
        return None
    if not path.is_file():
        raise PlanError("PLAN_TARGET_NOT_FILE", "planned path exists but is not a file", rel)
    try:
        return path.read_bytes()
    except OSError as exc:
        raise PlanError("PLAN_TARGET_UNREADABLE", str(exc), rel) from exc


def safe_target_path(workspace: str, rel: str) -> Path:
    """Reject symlinked planned paths before any read or write follows them."""
    root = os.path.realpath(workspace)
    path = Path(workspace) / Path(rel)
    if not validate_scaffold.is_within(os.path.abspath(path), os.path.abspath(workspace)):
        raise PlanError("PLAN_PATH_ESCAPE", "planned path resolves outside the workspace", rel)
    if not os.path.lexists(workspace):
        return path
    current = path
    while current != Path(workspace).parent and current != current.parent:
        if os.path.islink(current) or (os.path.lexists(current) and not validate_scaffold.is_within(os.path.realpath(current), root)):
            raise PlanError("PLAN_PATH_ESCAPE", "planned path must not follow a symbolic link", rel)
        current = current.parent
    return path


def read_json(source: str | None, raw_input: str | None = None) -> dict:
    try:
        raw = Path(source).read_text(encoding="utf-8") if source else (raw_input if raw_input is not None else sys.stdin.buffer.read().decode("utf-8"))
        value = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PlanError("PLAN_JSON_INVALID", "unable to parse plan JSON: %s" % exc) from exc
    if not isinstance(value, dict):
        raise PlanError("PLAN_TYPE", "plan must be a JSON object")
    return value


def parse_plan(workspace: str, source: str | None, raw_input: str | None = None) -> dict:
    if source:
        source_path = abs_workspace(source)
        for plan_path, target_path in (
                (source_path, workspace),
                (os.path.realpath(source_path), os.path.realpath(workspace))):
            try:
                if os.path.commonpath([plan_path, target_path]) == target_path:
                    raise PlanError("PLAN_FILE_INSIDE_WORKSPACE", "plan file must be outside the target workspace")
            except ValueError:
                pass
    plan = read_json(source, raw_input)
    if plan.get("schemaVersion") != SCHEMA_VERSION:
        raise PlanError("PLAN_SCHEMA_VERSION", "plan schemaVersion must be 1")
    declared = plan.get("workspace")
    if (not isinstance(declared, str)
            or os.path.normcase(os.path.realpath(abs_workspace(declared)))
            != os.path.normcase(os.path.realpath(workspace))):
        raise PlanError("PLAN_WORKSPACE_MISMATCH", "plan workspace does not match the command target")
    baseline = plan.get("baseline")
    files = plan.get("files")
    if not isinstance(baseline, dict) or not isinstance(files, dict) or not files:
        raise PlanError("PLAN_FIELDS_MISSING", "plan must contain non-empty baseline and files objects")
    parsed_files: dict[str, str | None] = {}
    parsed_baseline: dict[str, str | None] = {}
    for raw_path, content in files.items():
        rel = safe_rel(raw_path)
        if rel in parsed_files:
            raise PlanError("PLAN_DUPLICATE_PATH", "duplicate planned path", rel)
        if content is not None and not isinstance(content, str):
            raise PlanError("PLAN_CONTENT_TYPE", "planned file content must be a string or null", rel)
        parsed_files[rel] = content
    for raw_path, digest in baseline.items():
        rel = safe_rel(raw_path)
        if rel in parsed_baseline:
            raise PlanError("PLAN_DUPLICATE_PATH", "duplicate baseline path", rel)
        if digest is not None and (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise PlanError("PLAN_BASELINE_INVALID", "baseline value must be a lowercase SHA-256 or null", rel)
        parsed_baseline[rel] = digest
    if set(parsed_files) != set(parsed_baseline):
        raise PlanError("PLAN_BASELINE_INCOMPLETE", "baseline and files must contain exactly the same paths")
    parsed = {"schemaVersion": SCHEMA_VERSION, "workspace": workspace,
            "baseline": dict(sorted(parsed_baseline.items())),
            "files": dict(sorted(parsed_files.items())), "descriptionSync": plan.get("descriptionSync")}
    check_maintenance_boundaries(workspace, parsed)
    return parsed


def current_hash(workspace: str, rel: str) -> str | None:
    data = path_bytes(workspace, rel)
    return None if data is None else sha256_bytes(data)


def plan_changes(workspace: str, plan: dict) -> tuple[list[dict], list[dict]]:
    changes = []
    conflicts = []
    for rel in sorted(plan["files"]):
        actual = current_hash(workspace, rel)
        expected = plan["baseline"][rel]
        if actual != expected:
            conflicts.append(error("PLAN_BASELINE_CONFLICT",
                                   "current file does not match the plan baseline", rel))
        content = plan["files"][rel]
        after_hash = None if content is None else sha256_bytes(content.encode("utf-8"))
        if actual == after_hash:
            action = "unchanged"
        elif actual is None:
            action = "create"
        elif content is None:
            action = "delete"
        else:
            action = "modify"
        before_data = path_bytes(workspace, rel)
        before_text = "" if before_data is None else before_data.decode("utf-8", "replace")
        after_text = "" if content is None else content
        diff = "".join(difflib.unified_diff(
            before_text.splitlines(True), after_text.splitlines(True),
            fromfile=rel if before_data is not None else "/dev/null",
            tofile=rel if content is not None else "/dev/null"))
        changes.append({"path": rel, "action": action, "beforeHash": actual,
                        "afterHash": after_hash, "diff": diff})
    return changes, conflicts


def copy_workspace(workspace: str) -> str:
    temp = tempfile.mkdtemp(prefix="agent-scaffold-")
    rel = None
    try:
        if not os.path.isdir(workspace):
            return temp
        _, declarations = inspect_entry(workspace)
        if not declarations and not os.path.lexists(Path(workspace) / "AGENTS.md") and os.path.isfile(Path(workspace) / LEGACY_ENTRY):
            validator = validate_scaffold.Validator(workspace, validate_scaffold.Collector())
            block, _ = validator.parse_document(LEGACY_ENTRY, True)
            declarations = index_declarations(workspace, block.data) if block and isinstance(block.data, dict) else []
        indexed = {item["path"] for item in declarations}
        inputs = [("AGENTS.md", True), (DESCRIPTION_FILE, True)]
        manifest = load_package_manifest(workspace) if os.path.lexists(Path(workspace) / MANIFEST_FILE) else None
        if manifest:
            inputs.extend((rel, True) for rel in manifest["protectedFiles"])
        agent = Path(workspace) / ".agent"
        if agent.exists():
            if not validate_scaffold.is_within(os.path.realpath(agent), os.path.realpath(workspace)):
                raise PlanError("SCAFFOLD_PATH_ESCAPE", "symbolic link points outside the workspace", ".agent")
            if agent.is_dir():
                (Path(temp) / ".agent").mkdir()
                for name in sorted(os.listdir(agent)):
                    rel = ".agent/" + name
                    inputs.append((rel, name in ("context.yaml", "behaviors.md", "scaffold-manifest.json")))
                    if name in validate_scaffold.INDEX_COLLECTIONS and (agent / name).is_dir():
                        if not validate_scaffold.is_within(os.path.realpath(agent / name), os.path.realpath(workspace)):
                            raise PlanError("SCAFFOLD_PATH_ESCAPE", "symbolic link points outside the workspace", rel)
                        inputs.append((rel, False))
                        for child in sorted(os.listdir(agent / name)):
                            object_rel = rel + "/" + child
                            inputs.append((object_rel, object_rel in indexed))
                    elif (agent / name).is_dir():
                        # Keep unknown object paths discoverable without copying their bytes.
                        for child_root, child_dirs, child_names in os.walk(agent / name, followlinks=False):
                            child_rel = Path(child_root).relative_to(Path(workspace)).as_posix()
                            for child_dir in sorted(child_dirs):
                                inputs.append((child_rel + "/" + child_dir, False))
                            for child_name in sorted(child_names):
                                inputs.append((child_rel + "/" + child_name, False))
            else:
                (Path(temp) / ".agent").touch()
        for rel, copy_content in inputs:
            src, dst = Path(workspace) / rel, Path(temp) / rel
            if not os.path.lexists(src):
                continue
            if not validate_scaffold.is_within(os.path.realpath(src), os.path.realpath(workspace)):
                raise PlanError("SCAFFOLD_PATH_ESCAPE", "symbolic link points outside the workspace", rel)
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                dst.mkdir(exist_ok=True)
            elif copy_content and src.is_file():
                shutil.copy2(src, dst)
            elif not dst.exists():
                dst.touch()
        return temp
    except (OSError, PlanError) as exc:
        shutil.rmtree(temp, ignore_errors=True)
        if isinstance(exc, PlanError):
            raise
        raise PlanError("OVERLAY_COPY_FAILED", str(exc), rel) from exc


def read_before_placeholders(workspace: str, temp: str, plan: dict) -> list[dict]:
    overlay = validate_scaffold.Validator(temp, validate_scaffold.Collector())
    overlay.check_entry()
    collector = validate_scaffold.Collector()
    original = validate_scaffold.Validator(workspace, collector)
    for entry in overlay.by_path["skills"].values():
        rel = entry["normalized"]
        block, _ = overlay.parse_document(rel, require_frontmatter=True)
        value = block.data.get("readBefore") if block is not None and isinstance(block.data, dict) else None
        if not isinstance(value, list):
            continue
        for index, path in enumerate(value, start=1):
            if not isinstance(path, str) or not path.strip():
                continue
            line = overlay.line_of(block, "readBefore", None)
            safe = original.check_relative_path(rel, path, line, None, "readBefore entry %d" % index)
            normalized = validate_scaffold.normalize_path(path)
            target = Path(temp) / normalized
            source = Path(workspace) / normalized
            if not validate_scaffold.is_within(os.path.abspath(target), os.path.abspath(temp)):
                continue
            if normalized in plan["files"] or target.exists():
                continue
            # A stub retains existence checks while original path checks retain symlink errors.
            if (safe and source.is_file()) or not safe:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.touch()
    return collector.items


def write_plan_to(root: str, plan: dict) -> None:
    for rel, content in plan["files"].items():
        path = safe_target_path(root, rel)
        if content is None:
            if path.exists():
                path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")


def validate_overlay(workspace: str, plan: dict) -> list[dict]:
    temp = copy_workspace(workspace)
    try:
        write_plan_to(temp, plan)
        collector = validate_scaffold.Collector()
        for item in read_before_placeholders(workspace, temp, plan) + validate_scaffold.validate(temp):
            collector.add(item)
        if os.path.lexists(Path(temp) / MANIFEST_FILE):
            for item in formal_delivery(temp)["errors"]:
                collector.add(item)
        return validate_scaffold.sorted_errors(collector.items)
    except OSError as exc:
        raise PlanError("OVERLAY_WRITE_FAILED", str(exc)) from exc
    finally:
        shutil.rmtree(temp, ignore_errors=True)


def validate_errors(errors: list[dict]) -> list[dict]:
    return [error(item.get("code", "SCAFFOLD_ERROR"), item.get("message", ""), item.get("path"), item.get("line")) for item in errors]


def index_declarations(workspace: str, data: dict) -> list[dict]:
    declarations = []
    for collection in validate_scaffold.INDEX_COLLECTIONS:
        indexed = data.get(collection)
        if not isinstance(indexed, list):
            continue
        for item in indexed:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not isinstance(item.get("path"), str):
                continue
            rel = item["path"].replace("\\", "/")
            declarations.append({"type": collection[:-1], "id": item["id"], "path": rel,
                                 "exists": os.path.isfile(os.path.join(workspace, rel.replace("/", os.sep)))})
    return declarations


def inspect_entry(workspace: str) -> tuple[dict, list[dict]]:
    entry_path = os.path.join(workspace, "AGENTS.md")
    exists = os.path.isfile(entry_path)
    entry = {"path": "AGENTS.md", "exists": exists, "canCreate": not os.path.lexists(entry_path)}
    if not exists:
        return entry, []
    collector = validate_scaffold.Collector()
    validator = validate_scaffold.Validator(workspace, collector)
    block, _ = validator.parse_document("AGENTS.md", require_frontmatter=True)
    data = block.data if block is not None and isinstance(block.data, dict) else {}
    for key in ("agentId", "contractVersion", "purpose"):
        if key in data:
            entry[key] = data[key]
    declarations = index_declarations(workspace, data)
    entry["indexes"] = declarations
    return entry, declarations


def legacy_entry(workspace: str) -> tuple[dict, list[dict]]:
    """Inspect the pre-AGENTS.md entry without treating arbitrary AGENT.md as legacy."""
    path = os.path.join(workspace, LEGACY_ENTRY)
    entry = {"path": LEGACY_ENTRY, "exists": os.path.lexists(path), "recognized": False}
    if not entry["exists"] or not os.path.isfile(path):
        return entry, []
    collector = validate_scaffold.Collector()
    validator = validate_scaffold.Validator(workspace, collector)
    # The entry schema is unchanged; reuse it without writing or replacing AGENT.md.
    validator.texts[validate_scaffold.AGENT_FILE] = validator.load_text(LEGACY_ENTRY)
    validator.run()
    block = validator.entry_block
    if collector.items or block is None or not isinstance(block.data, dict):
        return entry, []
    data = block.data
    declarations = index_declarations(workspace, data)
    if any(not item["exists"] for item in declarations):
        return entry, []
    for key in ("agentId", "contractVersion", "purpose"):
        entry[key] = data[key]
    entry["indexes"] = declarations
    entry["recognized"] = True
    return entry, entry["indexes"]


def is_windows_reserved_path(relative: str) -> bool:
    return any(re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part)
               for part in relative.split("/"))


def _display_inventory_path(value: str) -> str:
    return value.encode("utf-8", "replace").decode("utf-8")


def inventory_workspace(workspace: str) -> dict:
    """List bounded path metadata without following links or opening user files."""
    files: list[str] = []
    objects: list[str] = []
    excluded: list[dict] = []
    skipped: list[str] = []
    warnings: list[dict] = []
    rule_files: list[str] = []
    complete = True
    scanned = 0
    rule_bytes = 0
    stack = [("", workspace, 0)]
    stopped = False

    def diagnose(code: str, message: str, relative: str | None = None) -> None:
        nonlocal complete
        complete = False
        try:
            path = _display_inventory_path(relative) if relative else None
        except (ValueError, UnicodeError):
            path = "<unrepresentable>"
        warnings.append(error(code, message, path))

    while stack and not stopped:
        parent_rel, parent_abs, depth = stack.pop()
        if depth >= INVENTORY_MAX_DEPTH:
            skipped.append(_display_inventory_path(parent_rel or "."))
            diagnose("INVENTORY_DEPTH_LIMIT", "directory depth limit reached", parent_rel or ".")
            continue
        children = []
        try:
            with os.scandir(parent_abs) as entries:
                for item in entries:
                    if scanned >= INVENTORY_MAX_ENTRIES:
                        diagnose("INVENTORY_ENTRY_LIMIT", "inventory entry limit reached", parent_rel or ".")
                        stopped = True
                        break
                    scanned += 1
                    children.append(item)
        except PermissionError:
            diagnose("INVENTORY_PERMISSION_DENIED", "directory cannot be enumerated", parent_rel or ".")
            continue
        except OSError as exc:
            diagnose("INVENTORY_ENUMERATION_FAILED", exc.strerror or "directory cannot be enumerated",
                     parent_rel or ".")
            continue

        children.sort(key=lambda item: (item.name.casefold(), item.name))
        directories = []
        for item in children:
            relative = parent_rel or "."
            try:
                relative = (parent_rel + "/" if parent_rel else "") + item.name
                relative = _display_inventory_path(relative.replace("\\", "/"))
                if is_windows_reserved_path(relative):
                    diagnose("INVENTORY_RESERVED_NAME", "path contains a Windows reserved name", relative)
                    excluded.append({"path": relative, "reason": "windows-reserved-name"})
                    continue
                if item.is_symlink():
                    diagnose("INVENTORY_SYMLINK_SKIPPED", "symbolic link was not followed", relative)
                    excluded.append({"path": relative, "reason": "symbolic-link"})
                    continue
                if item.is_dir(follow_symlinks=False):
                    if item.name.casefold() in INVENTORY_PRUNED_DIRECTORIES:
                        skipped.append(relative)
                    elif depth + 1 >= INVENTORY_MAX_DEPTH:
                        skipped.append(relative)
                        diagnose("INVENTORY_DEPTH_LIMIT", "directory depth limit reached", relative)
                    else:
                        directories.append((relative, item.path, depth + 1))
                    continue
                if not item.is_file(follow_symlinks=False):
                    excluded.append({"path": relative, "reason": "special-file"})
                    continue
                files.append(relative)
                if relative.startswith(".agent/"):
                    objects.append(relative)
                if item.name.casefold() == "agents.md":
                    rule_files.append(relative)
                    if rule_bytes + item.stat(follow_symlinks=False).st_size > INVENTORY_RULE_TOTAL_BYTES:
                        diagnose("INVENTORY_RULE_BUDGET", "required rules exceed the inventory read budget", relative)
                        continue
                    with open(item.path, "rb") as handle:
                        content = handle.read(INVENTORY_RULE_MAX_BYTES + 1)
                    if len(content) > INVENTORY_RULE_MAX_BYTES:
                        diagnose("INVENTORY_RULE_TOO_LARGE",
                                 "required rule exceeds the per-file read limit", relative)
                        continue
                    rule_bytes += len(content)
                if relative.casefold() == MANIFEST_FILE.casefold():
                    size = item.stat(follow_symlinks=False).st_size
                    if size > INVENTORY_RULE_MAX_BYTES:
                        diagnose("INVENTORY_MANIFEST_TOO_LARGE",
                                 "scaffold manifest exceeds the inventory metadata limit", relative)
                if item.name.casefold() == ".env" or item.name.casefold().startswith(".env."):
                    excluded.append({"path": relative, "reason": "dotenv"})
            except PermissionError:
                diagnose("INVENTORY_PERMISSION_DENIED", "path metadata cannot be read", relative)
            except OSError as exc:
                diagnose("INVENTORY_PATH_UNREADABLE", exc.strerror or "path metadata cannot be read", relative)
            except (ValueError, UnicodeError) as exc:
                diagnose("INVENTORY_PATH_INVALID", str(exc), relative)
        stack.extend(reversed(directories))

    return {
        "complete": complete,
        "scannedEntries": scanned,
        "maxDepth": INVENTORY_MAX_DEPTH,
        "maxEntries": INVENTORY_MAX_ENTRIES,
        "skippedDirectories": sorted(skipped),
        "excluded": sorted(excluded, key=lambda item: item["path"]),
        "ruleFiles": sorted(rule_files),
        "ruleBytesRead": rule_bytes,
        "warnings": sorted(warnings, key=lambda item: (item.get("path") or "", item["code"])),
        "files": sorted(files),
        "objects": sorted(objects),
    }


def has_scaffold_contract_marker(workspace: str) -> tuple[bool, dict | None]:
    """Recognize only a positive scaffold declaration, not arbitrary Markdown."""
    manifest = os.path.join(workspace, MANIFEST_FILE.replace("/", os.sep))
    if os.path.lexists(manifest):
        return True, None
    entry = os.path.join(workspace, "AGENTS.md")
    if not os.path.lexists(entry):
        return False, None
    if os.path.islink(entry) or not os.path.isfile(entry):
        return False, error("INSPECT_ENTRY_UNREADABLE", "AGENTS.md is not a readable regular file", "AGENTS.md")
    try:
        with open(entry, "rb") as handle:
            raw = handle.read(256 * 1024)
    except PermissionError:
        return False, error("INSPECT_ENTRY_UNREADABLE", "AGENTS.md cannot be read", "AGENTS.md")
    except OSError as exc:
        return False, error("INSPECT_ENTRY_UNREADABLE", exc.strerror or "AGENTS.md cannot be read", "AGENTS.md")

    marker = re.compile(rb"(?m)^contractVersion\s*:")
    if not raw.startswith(b"---"):
        return False, None
    closer = re.search(rb"(?m)^---[ \t]*\r?$", raw[3:])
    header = raw[3:closer.start() + 3] if closer else raw[3:]
    return bool(marker.search(header)), None


def inspect_workspace(workspace: str) -> dict:
    if not os.path.lexists(workspace):
        return {"classification": "missing", "scaffold": "missing", "adoptable": True,
                "entry": {"path": "AGENTS.md", "exists": False, "canCreate": True},
                "objects": [], "files": [], "errors": [],
                "inventory": {"complete": True, "scannedEntries": 0, "maxDepth": INVENTORY_MAX_DEPTH,
                              "maxEntries": INVENTORY_MAX_ENTRIES, "skippedDirectories": [],
                              "excluded": [], "ruleFiles": [], "ruleBytesRead": 0, "warnings": []},
                "warnings": [error("WORKSPACE_MISSING", "workspace does not exist; Agent entry can be created")]}
    if not os.path.isdir(workspace):
        return {"classification": "existing-unmanaged", "scaffold": "missing", "adoptable": False,
                "entry": {"path": "AGENTS.md", "exists": False, "canCreate": False},
                "objects": [], "files": [], "errors": [error("WORKSPACE_NOT_DIRECTORY", "workspace is not a directory")],
                "inventory": {"complete": False, "scannedEntries": 0, "maxDepth": INVENTORY_MAX_DEPTH,
                              "maxEntries": INVENTORY_MAX_ENTRIES, "skippedDirectories": [],
                              "excluded": [], "ruleFiles": [], "ruleBytesRead": 0, "warnings": []}, "warnings": []}
    inventory = inventory_workspace(workspace)
    files = inventory["files"]
    objects = inventory["objects"]
    entry_exists = os.path.lexists(os.path.join(workspace, "AGENTS.md"))
    entry = {"path": "AGENTS.md", "exists": entry_exists,
             "canCreate": not entry_exists and not os.path.lexists(os.path.join(workspace, "AGENTS.md"))}
    errors = []
    warnings = list(inventory["warnings"])
    marker, marker_error = has_scaffold_contract_marker(workspace)
    if marker_error:
        warnings.append(marker_error)
    declarations = []
    if marker:
        if inventory["complete"] and marker_error is None:
            entry, declarations = inspect_entry(workspace)
            objects = sorted(set(objects) | {item["path"] for item in declarations})
            try:
                errors = validate_errors(validate_scaffold.validate(workspace))
            except (OSError, UnicodeError, ValueError) as exc:
                errors.append(error("INSPECT_VALIDATION_FAILED", str(exc), "AGENTS.md"))
        else:
            errors.append(error("INSPECT_VALIDATION_INCOMPLETE",
                                "formal scaffold validation was not run because required inventory paths were incomplete"))
    legacy, legacy_declarations = (legacy_entry(workspace)
                                   if not entry_exists and not marker and inventory["complete"]
                                   else ({"path": LEGACY_ENTRY, "exists": os.path.lexists(os.path.join(workspace, LEGACY_ENTRY)),
                                          "recognized": False}, []))
    if not entry_exists and not marker:
        if legacy["recognized"]:
            warnings.append(error("WORKSPACE_LEGACY_ENTRY", "AGENT.md is a legacy entry; create an explicit migration plan to AGENTS.md", LEGACY_ENTRY))
            declarations = legacy_declarations
            objects = sorted(set(objects) | {item["path"] for item in declarations})
        else:
            warnings.append(error("WORKSPACE_ENTRY_MISSING", "AGENTS.md is absent; Agent entry can be created"))
    if (not entry_exists or not marker) and files:
        warnings.append(error("WORKSPACE_EXISTING_FILES", "existing project files will be preserved unless a plan lists them"))
    if entry_exists and not marker:
        warnings.append(error("WORKSPACE_UNMANAGED_ENTRY", "AGENTS.md is an existing user rule without a scaffold contract marker; preserve its contents", "AGENTS.md"))
    if legacy["exists"] and not legacy["recognized"]:
        warnings.append(error("WORKSPACE_ENTRY_UNSUPPORTED", "AGENT.md is not a recognized legacy entry; do not migrate it automatically", LEGACY_ENTRY))
    for name in EXTERNAL_ENTRY_NAMES:
        if os.path.lexists(os.path.join(workspace, name)):
            warnings.append(error("WORKSPACE_ENTRY_UNSUPPORTED", "%s is an external host entry; do not migrate it automatically" % name, name))
    if marker:
        classification = "valid" if not errors else "invalid"
    elif not entry_exists and legacy["recognized"]:
        classification = "legacy"
    elif files or entry_exists or marker_error:
        classification = "existing-unmanaged"
    else:
        classification = "missing"
    alias = "missing" if classification == "existing-unmanaged" else classification
    adoptable = inventory["complete"] and classification != "invalid" and marker_error is None
    result = {"classification": classification, "scaffold": alias, "adoptable": adoptable,
              "entry": entry, "objects": declarations, "files": files, "errors": errors,
              "warnings": warnings, "inventory": inventory}
    if marker and inventory["complete"] and marker_error is None:
        result["delivery"] = formal_delivery(workspace)
    if legacy["exists"]:
        result["legacyEntry"] = legacy
    return result


def base_result(operation: str, workspace: str, status: str = "ok") -> dict:
    return {"schemaVersion": SCHEMA_VERSION, "operation": operation,
            "workspace": workspace, "status": status, "errors": [], "warnings": []}


def plan_result(operation: str, workspace: str, source: str | None, with_diff: bool = True,
                raw_input: str | None = None) -> dict:
    result = base_result(operation, workspace)
    try:
        plan = parse_plan(workspace, source, raw_input)
        changes, conflicts = plan_changes(workspace, plan)
        result["plan"] = {"schemaVersion": plan["schemaVersion"], "workspace": workspace,
                           "paths": sorted(plan["files"]), "baseline": plan["baseline"]}
        if plan.get("descriptionSync"):
            result["plan"]["descriptionSync"] = plan["descriptionSync"]
        result["changes"] = changes if with_diff else []
        if conflicts:
            result["status"] = "conflict"
            result["errors"] = conflicts
            return result
        overlay_errors = validate_overlay(workspace, plan)
        if overlay_errors:
            result["status"] = "error"
            result["errors"] = validate_errors(overlay_errors)
        return result
    except PlanError as exc:
        result["status"] = "error"
        result["errors"] = [error(exc.code, exc.message, exc.path)]
        return result


def backup_workspace(workspace: str, plan: dict) -> str:
    if validate_scaffold.is_within(os.path.realpath(BACKUP_ROOT), os.path.realpath(workspace)):
        raise PlanError("BACKUP_LOCATION_INVALID", "backup root must be outside the workspace")
    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    ident = str(uuid.uuid4())
    entries = {}
    for rel in plan["files"]:
        data = path_bytes(workspace, rel)
        entries[rel] = None if data is None else base64.b64encode(data).decode("ascii")
    manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "id": ident,
        "createdAt": format_utc(utc_now()),
        "workspace": workspace,
        "planSchemaVersion": plan["schemaVersion"],
        "workspaceExisted": os.path.isdir(workspace),
        "protectedFiles": sorted(entries),
        "files": entries,
    }
    (BACKUP_ROOT / (ident + ".json")).write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return ident


def backup_summary(manifest: dict) -> dict:
    """Return stable, machine-readable backup metadata for apply and restore."""
    created = parse_backup_time(manifest["createdAt"]) if "createdAt" in manifest else None
    age = max(0.0, (utc_now() - created).total_seconds() / 3600.0) if created else None
    protected = manifest.get("protectedFiles", sorted(manifest.get("files", {})))
    return {
        "id": manifest.get("id"),
        "createdAt": manifest.get("createdAt"),
        "ageHours": round(age, 6) if age is not None else None,
        "workspace": manifest.get("workspace"),
        "planSchemaVersion": manifest.get("planSchemaVersion"),
        "protectedFiles": protected,
    }


def validate_backup_manifest(manifest: object, ident: str, require_metadata: bool = False) -> dict:
    try:
        if not isinstance(manifest, dict):
            raise ValueError("manifest must be an object")
        if type(manifest.get("schemaVersion")) is not int or manifest["schemaVersion"] != SCHEMA_VERSION:
            raise ValueError("unsupported backup schemaVersion")
        if str(uuid.UUID(ident)) != ident or manifest.get("id") != ident:
            raise ValueError("backup id must match its manifest filename")
        if not isinstance(manifest.get("workspace"), str) or not os.path.isabs(manifest["workspace"]):
            raise ValueError("manifest workspace must be an absolute path")
        if "workspaceExisted" in manifest and not isinstance(manifest["workspaceExisted"], bool):
            raise ValueError("workspaceExisted must be a boolean")
        files = manifest.get("files")
        if not isinstance(files, dict) or not files:
            raise ValueError("backup files must be a non-empty object")
        for rel, encoded in files.items():
            if safe_rel(rel) != rel:
                raise ValueError("backup paths must be normalized")
            if encoded is not None:
                if not isinstance(encoded, str):
                    raise ValueError("backup file content must be base64 or null")
                base64.b64decode(encoded, validate=True)
        if require_metadata or "createdAt" in manifest:
            parse_backup_time(manifest.get("createdAt"))
            if type(manifest.get("planSchemaVersion")) is not int or manifest["planSchemaVersion"] != SCHEMA_VERSION:
                raise ValueError("unsupported planSchemaVersion")
            if manifest.get("protectedFiles") != sorted(files):
                raise ValueError("protectedFiles must match the backup file paths")
        return manifest
    except (ValueError, TypeError, PlanError) as exc:
        raise PlanError("BACKUP_MANIFEST_INVALID", str(exc)) from exc


def load_backup(workspace: str, ident: str) -> dict:
    if not BACKUP_ID_RE.fullmatch(ident):
        raise PlanError("BACKUP_ID_INVALID", "backup id is invalid")
    path = BACKUP_ROOT / (ident + ".json")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_manifest_pairs)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PlanError("BACKUP_NOT_FOUND", "backup could not be read: %s" % exc) from exc
    manifest = validate_backup_manifest(manifest, ident)
    if manifest.get("workspace") != workspace:
        raise PlanError("BACKUP_WORKSPACE_MISMATCH", "backup belongs to another workspace")
    return manifest


def purge_backups(age_hours: float, workspace_filter: str | None = None) -> dict:
    """Delete only explicitly requested old backups and report every outcome."""
    if not math.isfinite(age_hours) or age_hours < 0:
        raise PlanError("PURGE_AGE_INVALID", "purge age threshold must be non-negative")
    try:
        cutoff = utc_now() - dt.timedelta(hours=age_hours)
    except OverflowError as exc:
        raise PlanError("PURGE_AGE_INVALID", "purge age threshold is out of range") from exc
    target = abs_workspace(workspace_filter) if workspace_filter else None
    result = {
        "cutoff": format_utc(cutoff),
        "ageHours": age_hours,
        "workspaceFilter": target,
        "deleted": [],
        "retained": [],
        "errors": [],
    }
    if not BACKUP_ROOT.exists():
        return result
    for path in sorted(BACKUP_ROOT.glob("*.json")):
        ident = path.stem
        record = {"id": ident, "path": str(path)}
        try:
            if path.is_symlink() or not path.is_file():
                raise ValueError("backup manifest must be a regular file")
            manifest = json.loads(path.read_text(encoding="utf-8"))
            manifest = validate_backup_manifest(manifest, ident, require_metadata=True)
            created = parse_backup_time(manifest.get("createdAt"))
            workspace = manifest.get("workspace")
            if not isinstance(workspace, str):
                raise ValueError("manifest workspace is missing")
            if target and abs_workspace(workspace) != target:
                record["reason"] = "workspace-filter"
                result["retained"].append(record)
                continue
            record["createdAt"] = manifest.get("createdAt")
            record["ageHours"] = round(max(0.0, (utc_now() - created).total_seconds() / 3600.0), 6)
            if created < cutoff:
                try:
                    path.unlink()
                except OSError as exc:
                    record["code"] = "BACKUP_DELETE_FAILED"
                    record["line"] = None
                    record["message"] = str(exc)
                    result["errors"].append(record)
                else:
                    result["deleted"].append(record)
            else:
                record["reason"] = "too-new"
                result["retained"].append(record)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, PlanError) as exc:
            record["code"] = "BACKUP_MANIFEST_INVALID"
            record["line"] = None
            record["message"] = str(exc)
            result["errors"].append(record)
    return result


def restore_manifest(workspace: str, manifest: dict) -> list[dict]:
    """Restore every protected file and return independent per-path failures.

    Recovery is deliberately best effort: one blocked path must not prevent the
    remaining protected files from being restored. Callers decide whether the
    returned list means ``RESTORE`` or ``APPLY_RESTORE`` failure.
    """
    failures: list[dict] = []
    files = manifest.get("files", {})
    if not isinstance(files, dict):
        return [error("RESTORE_MANIFEST_INVALID", "backup files must be an object")]
    for rel, encoded in sorted(files.items()):
        path = None
        temp = None
        try:
            path = safe_target_path(workspace, rel)
            if encoded is None:
                if path.exists():
                    path.unlink()
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, temp = tempfile.mkstemp(prefix=".agent-scaffold-", dir=str(path.parent))
            with os.fdopen(fd, "wb") as handle:
                handle.write(base64.b64decode(encoded, validate=True))
            os.replace(temp, path)
            temp = None
        except (OSError, UnicodeError, ValueError, TypeError, PlanError) as exc:
            failures.append(error("RESTORE_WRITE_FAILED", str(exc), rel))
        finally:
            if temp and os.path.exists(temp):
                try:
                    os.unlink(temp)
                except OSError as exc:
                    failures.append(error("RESTORE_TEMP_CLEANUP_FAILED", str(exc), rel))
    try:
        cleanup_empty_parents(workspace, files)
    except (OSError, ValueError) as exc:
        failures.append(error("RESTORE_CLEANUP_FAILED", str(exc)))
    if not manifest.get("workspaceExisted", True):
        try:
            Path(workspace).rmdir()
        except OSError as exc:
            failures.append(error("RESTORE_WORKSPACE_CLEANUP_FAILED", str(exc), "."))
    return failures


def cleanup_empty_parents(workspace: str, rel_paths) -> None:
    workspace_path = Path(workspace)
    candidates = set()
    for rel in rel_paths:
        path = workspace_path / Path(rel)
        while path.parent != workspace_path and path.parent != path.parent.parent:
            path = path.parent
            try:
                within = os.path.commonpath([os.path.abspath(path), os.path.abspath(workspace)]) == os.path.abspath(workspace)
            except ValueError:
                within = False
            if within and (path.name == ".agent" or ".agent" in path.relative_to(workspace_path).parts):
                candidates.add(path)
    for directory in sorted(candidates, key=lambda item: len(item.parts), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass


def apply_plan(workspace: str, plan: dict, changes: list[dict]) -> tuple[str, list[dict]]:
    try:
        backup = backup_workspace(workspace, plan)
    except (OSError, UnicodeError, PlanError) as exc:
        return "", [error("APPLY_BACKUP_FAILED", str(exc))]
    created_dirs: list[Path] = []
    applied: list[dict] = []
    by_path = {item["path"]: item for item in changes}
    current_rel = None
    try:
        for rel, content in plan["files"].items():
            current_rel = rel
            if current_hash(workspace, rel) != plan["baseline"][rel]:
                raise PlanError("PLAN_BASELINE_CONFLICT", "file changed immediately before write", rel)
            if path_bytes(workspace, rel) == (None if content is None else content.encode("utf-8")):
                continue
            path = safe_target_path(workspace, rel)
            if content is None:
                if path.exists():
                    path.unlink()
                    applied.append(by_path[rel])
                continue
            missing = []
            parent = path.parent
            while not parent.exists():
                missing.append(parent)
                parent = parent.parent
            path.parent.mkdir(parents=True, exist_ok=True)
            created_dirs.extend(reversed(missing))
            fd, temp = tempfile.mkstemp(prefix=".agent-scaffold-", dir=str(path.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(content)
                os.replace(temp, path)
                applied.append(by_path[rel])
            finally:
                if os.path.exists(temp):
                    os.unlink(temp)
        return backup, []
    except (OSError, UnicodeError, PlanError) as exc:
        failures = [error("APPLY_WRITE_FAILED", str(exc), current_rel)]
        try:
            restore_errors = rollback_applied(workspace, backup, applied)
            failures.extend(error("APPLY_RESTORE_FAILED", item["message"], item.get("path"), item.get("line"))
                             for item in restore_errors)
        except (OSError, PlanError) as restore_exc:
            failures.append(error("APPLY_RESTORE_FAILED", str(restore_exc), current_rel))
        for directory in reversed(created_dirs):
            try:
                directory.rmdir()
            except OSError:
                pass
        return backup, failures


def rollback_applied(workspace: str, backup: str, changes: list[dict]) -> list[dict]:
    manifest = load_backup(workspace, backup)
    owned = {}
    failures = []
    for item in changes:
        if item["action"] == "unchanged":
            continue
        rel = item["path"]
        try:
            if current_hash(workspace, rel) == item["afterHash"]:
                owned[rel] = manifest["files"][rel]
            else:
                failures.append(error("APPLY_RECOVERY_CONFLICT", "file changed after this operation; preserving concurrent content", rel))
        except PlanError as exc:
            failures.append(error("APPLY_RECOVERY_CONFLICT", exc.message, rel))
    if owned:
        failures.extend(restore_manifest(workspace, {**manifest, "files": owned}))
    return failures


def run(operation: str, workspace: str, source: str | None, confirm: bool,
        backup_id: str | None, age_hours: float | None = None,
        workspace_filter: str | None = None) -> tuple[dict, int]:
    if operation == "inspect":
        result = base_result(operation, workspace)
        details = inspect_workspace(workspace)
        result.update(details)
        if details["errors"]:
            result["status"] = "error"
        return result, 1 if result["status"] == "error" else 0
    if operation == "verify":
        result = base_result(operation, workspace)
        if not os.path.isdir(workspace):
            result["status"] = "error"
            result["errors"] = [error("WORKSPACE_NOT_DIRECTORY", "workspace is not a directory")]
        else:
            result["errors"] = validate_errors(validate_scaffold.validate(workspace))
            if result["errors"]:
                result["status"] = "error"
        return result, 1 if result["status"] == "error" else 0
    if operation == "purge":
        result = base_result(operation, workspace)
        try:
            if age_hours is None:
                raise PlanError("PURGE_AGE_MISSING", "purge requires --older-than-hours")
            report = purge_backups(age_hours, workspace_filter)
            result["purge"] = report
            if report["errors"]:
                result["status"] = "error"
                result["errors"] = [error(item.get("code", "BACKUP_PURGE_FAILED"),
                                           item.get("message", ""), item.get("path"), item.get("line"))
                                     for item in report["errors"]]
        except PlanError as exc:
            result["status"] = "error"
            result["errors"] = [error(exc.code, exc.message, exc.path)]
        return result, 1 if result["status"] == "error" else 0
    if operation == "restore":
        result = base_result(operation, workspace)
        try:
            if backup_id is None:
                raise PlanError("BACKUP_ID_MISSING", "restore requires --backup")
            manifest = load_backup(workspace, backup_id)
            restore_errors = restore_manifest(workspace, manifest)
            result["backup"] = backup_summary(manifest)
            result["backup"]["restored"] = sorted(manifest.get("files", {}))
            if restore_errors:
                result["status"] = "error"
                result["workspaceState"] = "partial-restore"
                result["errors"] = restore_errors
                return result, 1
            validation = validate_errors(validate_scaffold.validate(workspace))
            if validation and os.path.isfile(os.path.join(workspace, "AGENTS.md")):
                result["status"] = "error"
                result["workspaceState"] = "restored-but-invalid"
                result["errors"] = [error("RESTORE_VALIDATION_FAILED", "restored workspace failed static validation")]
                result["errors"].extend(validation)
            elif validation:
                result["warnings"] = [error("WORKSPACE_NOT_SCAFFOLD", "backup restored, but the original workspace had no valid AGENTS.md")]
        except (OSError, UnicodeError, ValueError, TypeError) as exc:
            result["status"] = "error"
            result["workspaceState"] = "partial-restore"
            result["errors"] = [error("RESTORE_WRITE_FAILED", str(exc), backup_id)]
        except PlanError as exc:
            result["status"] = "error"
            result["errors"] = [error(exc.code, exc.message, exc.path)]
        return result, 1 if result["status"] == "error" else 0
    raw_input = sys.stdin.read() if source is None else None
    result = plan_result(operation, workspace, source, with_diff=True, raw_input=raw_input)
    if operation != "apply" or result["status"] != "ok":
        if operation == "apply" and result["status"] == "ok" and not confirm:
            result["status"] = "needs-confirmation"
        return result, 1 if result["status"] in ("error", "conflict") else 0
    if not confirm:
        result["status"] = "needs-confirmation"
        return result, 0
    try:
        plan = parse_plan(workspace, source, raw_input)
        changes, conflicts = plan_changes(workspace, plan)
        if conflicts:
            result["status"] = "conflict"
            result["errors"] = conflicts
            return result, 1
        backup, apply_errors = apply_plan(workspace, plan, changes)
        if backup:
            manifest = load_backup(workspace, backup)
            result["backup"] = backup_summary(manifest)
        if apply_errors:
            result["status"] = "error"
            result["errors"] = apply_errors
            result["workspaceState"] = ("partial-apply" if any(item["code"] == "APPLY_RESTORE_FAILED" for item in apply_errors)
                                         else "restored-after-apply-failure")
            return result, 1
        post_errors = validate_scaffold.validate(workspace)
        if os.path.lexists(Path(workspace) / MANIFEST_FILE):
            delivery = formal_delivery(workspace)
            result["delivery"] = delivery
            post_errors = delivery["errors"]
        if post_errors:
            try:
                restore_errors = rollback_applied(workspace, backup, changes)
            except (OSError, UnicodeError, ValueError, TypeError, PlanError) as exc:
                restore_errors = [error("APPLY_RESTORE_FAILED", str(exc))]
            result["status"] = "error"
            result["errors"] = [error("APPLY_VALIDATION_FAILED", "applied workspace failed static validation")]
            result["errors"].extend(validate_errors(post_errors))
            result["workspaceState"] = "partial-apply" if restore_errors else "restored-after-validation-failure"
            result["errors"].extend(error("APPLY_RESTORE_FAILED", item["message"], item.get("path"), item.get("line"))
                                     for item in restore_errors)
            return result, 1
        result["changes"] = changes
        return result, 0
    except PlanError as exc:
        result["status"] = "error"
        result["errors"] = [error(exc.code, exc.message, exc.path)]
        return result, 1


def load_package_manifest(workspace: str, text: str | None = None) -> dict:
    try:
        raw = text if text is not None else (path_bytes(workspace, MANIFEST_FILE) or b"").decode("utf-8")
        manifest = json.loads(raw, object_pairs_hook=_manifest_pairs)
        if not isinstance(manifest, dict) or type(manifest.get("schemaVersion")) is not int or manifest["schemaVersion"] != 1:
            raise ValueError("manifest schemaVersion must be 1")
        if not isinstance(manifest.get("packageVersion"), str) or not re.fullmatch(r"\d+\.\d+\.\d+", manifest["packageVersion"]):
            raise ValueError("packageVersion must be a numeric major.minor.patch")
        if manifest.get("contractVersion") != "1" or not isinstance(manifest.get("source"), str) or not manifest["source"].strip():
            raise ValueError("manifest needs contractVersion 1 and a source")
        # packageVersion/source are retained for old propagated packages. New
        # release bundles carry the stronger canonical identity fields below.
        legacy = "bundleVersion" not in manifest
        if not legacy:
            if not isinstance(manifest.get("bundleVersion"), str) or not re.fullmatch(r"\d+\.\d+\.\d+", manifest["bundleVersion"]):
                raise ValueError("bundleVersion must be a numeric major.minor.patch")
            if manifest["bundleVersion"] != manifest["packageVersion"]:
                raise ValueError("bundleVersion must match packageVersion")
            repository = manifest.get("sourceRepository")
            if repository not in (CANONICAL_REPOSITORY, CANONICAL_REPOSITORY_URL):
                raise ValueError("sourceRepository must be the canonical GitHub repository")
            commit = manifest.get("sourceCommit")
            if not isinstance(commit, str) or not re.fullmatch(r"(?:[0-9a-f]{40}|unknown)", commit):
                raise ValueError("sourceCommit must be a 40-character SHA-1 or 'unknown'")
            asset_name = manifest.get("assetName")
            expected_asset = RELEASE_ASSET_PREFIX + manifest["bundleVersion"] + ".zip"
            if asset_name is not None and asset_name != expected_asset:
                raise ValueError("assetName must be %s" % expected_asset)
        manifest["manifestFormat"] = "legacy-package" if legacy else "architect-bundle"
        files, protected = manifest.get("files"), manifest.get("protectedFiles")
        if not isinstance(files, dict) or not files or not isinstance(protected, list) or not protected:
            raise ValueError("manifest needs files and protectedFiles")
        seen = set()
        for rel, digest in files.items():
            if safe_rel(rel) != rel or rel in (MANIFEST_FILE, LEGACY_ENTRY):
                raise ValueError("manifest paths must be normalized rule/tool paths")
            if rel.casefold() in seen:
                raise ValueError("case-insensitive duplicate manifest path")
            seen.add(rel.casefold())
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError("manifest file hashes must be SHA-256")
        if any(not isinstance(rel, str) or rel not in files or not (rel.startswith(ARCHITECT_ROOT) or rel.startswith(".agent/skills/")) for rel in protected):
            raise ValueError("protected files must be indexed architect Skills or architect tools")
        if len(protected) != len(set(protected)) or any(rel.startswith(ARCHITECT_ROOT) and rel not in protected for rel in files):
            raise ValueError("architect files must be protected, without duplicates")
        if not isinstance(manifest.get("exclusions"), list) or not all(isinstance(item, str) for item in manifest["exclusions"]):
            raise ValueError("manifest needs an exclusions list")
        points = manifest.get("entrypoints")
        roles = {"initialSkill", "archiveSkill", "architectSkill", "architectCli"}
        evaluation = "evaluationVersion" in manifest or (isinstance(points, dict) and "evaluationSkill" in points)
        migration = "migrationVersion" in manifest
        if evaluation:
            roles.add("evaluationSkill")
            if manifest.get("evaluationVersion") != EVALUATION_VERSION or not isinstance(points, dict) or points.get("evaluationSkill") != EVALUATION_SKILL:
                raise ValueError("evaluationVersion 1 and the fixed scaffold-evaluate entrypoint are required")
        if migration:
            roles.add("migrationTool")
        if not isinstance(points, dict) or set(points) != roles:
            raise ValueError("manifest needs initial/archive/architect entrypoints")
        if migration and manifest["migrationVersion"] != MIGRATION_VERSION:
            raise ValueError("unsupported migrationVersion")
        if any(not isinstance(points[key], str) or not validate_scaffold.SLUG_RE.fullmatch(points[key]) for key in ("initialSkill", "archiveSkill", "architectSkill")):
            raise ValueError("Skill entrypoints must be slugs")
        if len({points[key] for key in ("initialSkill", "archiveSkill", "architectSkill")}) != 3:
            raise ValueError("initial, archive and architect Skills must be distinct")
        if evaluation and EVALUATION_SKILL in (points[key] for key in ("initialSkill", "archiveSkill", "architectSkill")):
            raise ValueError("evaluation Skill must have a distinct role")
        if points["architectCli"] not in protected or not points["architectCli"].startswith(ARCHITECT_ROOT) or not points["architectCli"].endswith("/agent_scaffold.py"):
            raise ValueError("architect CLI must be a protected agent_scaffold.py")
        if ".agent/skills/" + points["architectSkill"] + ".md" not in protected:
            raise ValueError("architect Skill must be protected")
        protected_skills = {".agent/skills/" + points["architectSkill"] + ".md"}
        if evaluation:
            protected_skills.add(EVALUATION_FILE)
            helper = str(Path(points["architectCli"]).with_name("agent_evaluation.py")).replace("\\", "/")
            rules = str(Path(points["architectCli"]).with_name("scaffold-evaluate.md")).replace("\\", "/")
            if not {EVALUATION_FILE, helper, rules}.issubset(protected):
                raise ValueError("evaluation Skill, canonical rules and agent_evaluation.py must be protected")
        if migration:
            migration_tool = str(Path(points["architectCli"]).with_name(MIGRATION_TOOL)).replace("\\", "/")
            if points["migrationTool"] != migration_tool or migration_tool not in protected:
                raise ValueError("asset migration tool must be protected")
        if any(not rel.startswith(ARCHITECT_ROOT) and rel not in protected_skills for rel in protected):
            raise ValueError("protected Skills may only contain architect and evaluation entrypoints")
        if not {"AGENTS.md", DESCRIPTION_FILE}.issubset(files):
            raise ValueError("manifest must include entry and Description")
        description = manifest.get("description")
        if not isinstance(description, dict) or description.get("path") != DESCRIPTION_FILE or not isinstance(description.get("redacted"), bool):
            raise ValueError("manifest needs Description metadata with redacted state")
        return manifest
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
        raise PlanError("PACKAGE_MANIFEST_INVALID", str(exc), MANIFEST_FILE) from exc


def formal_delivery(workspace: str) -> dict:
    errors = validate_errors(validate_scaffold.validate(workspace))
    info = validate_scaffold.description_info(workspace)
    errors.extend(info["errors"])
    report = {"status": "intermediate", "errors": errors, "description": {key: value for key, value in info.items() if key not in ("body", "errors")}}
    try:
        manifest = load_package_manifest(workspace)
        report.update(packageVersion=manifest["packageVersion"],
                      bundleVersion=manifest.get("bundleVersion"),
                      sourceRepository=manifest.get("sourceRepository"),
                      sourceCommit=manifest.get("sourceCommit"),
                      manifestFormat=manifest.get("manifestFormat"),
                      evaluationVersion=manifest.get("evaluationVersion"),
                      evaluationStatus="available" if "evaluationVersion" in manifest else "legacy",
                      migrationVersion=manifest.get("migrationVersion"),
                      migrationStatus="available" if "migrationVersion" in manifest else "legacy",
                      manifest=MANIFEST_FILE, protectedFiles=manifest["protectedFiles"])
        report["files"] = manifest["files"]
        for rel, expected in manifest["files"].items():
            actual = current_hash(workspace, rel)
            if actual != expected:
                errors.append(error("PACKAGE_HASH_MISMATCH", "file does not match manifest hash", rel))
        for key in ("baselineVersion", "origin", "contentSha256"):
            if manifest["description"].get(key) != info.get(key):
                errors.append(error("DESCRIPTION_METADATA_MISMATCH", "manifest Description metadata differs: " + key, MANIFEST_FILE))
        entry, indexes = inspect_entry(workspace)
        skills = {item["id"]: item["path"] for item in indexes if item["type"] == "skill"}
        points = manifest["entrypoints"]
        roles = ["initialSkill", "archiveSkill", "architectSkill"]
        if "evaluationVersion" in manifest:
            roles.append("evaluationSkill")
        else:
            errors.append(error("DELIVERY_EVALUATION_MISSING", "legacy package lacks the default evaluation capability; use a controlled upgrade", EVALUATION_FILE))
        for role in roles:
            ident = points[role]
            rel = skills.get(ident)
            if rel is None or rel not in manifest["files"]:
                errors.append(error("DELIVERY_ENTRYPOINT_MISSING", "missing indexed " + role, "AGENTS.md"))
        for role in ("initialSkill", "archiveSkill"):
            rel = skills.get(points[role])
            if not rel:
                continue
            collector = validate_scaffold.Collector()
            block, _ = validate_scaffold.Validator(workspace, collector).parse_document(rel, True)
            implementation = block.data.get("implementation", "") if block and isinstance(block.data, dict) else ""
            text = (path_bytes(workspace, rel) or b"").decode("utf-8", "replace")
            if not isinstance(implementation, str) or not implementation.strip() or not validate_scaffold.description_body(text).strip() or any(marker in implementation for marker in ("只声明", "不提供实现", "placeholder")):
                errors.append(error("DELIVERY_CAPABILITY_PLACEHOLDER", role + " must declare implementation and nonempty rules", rel))
            if role == "archiveSkill" and block and (not isinstance(block.data, dict) or block.data.get("sideEffects") != "writes-workspace" or block.data.get("confirmation") != "required"):
                errors.append(error("DELIVERY_ARCHIVE_POLICY", "archive needs writes-workspace and required confirmation", rel))
        architect = skills.get(points["architectSkill"])
        cli = points["architectCli"]
        companion = str(Path(cli).with_name("validate_scaffold.py")).replace("\\", "/")
        if companion not in manifest["protectedFiles"]:
            errors.append(error("DELIVERY_CLI_DEPENDENCY_MISSING", "self-contained CLI needs protected validate_scaffold.py", companion))
        if architect and cli not in (path_bytes(workspace, architect) or b"").decode("utf-8", "replace"):
            errors.append(error("DELIVERY_CLI_NOT_DISCOVERABLE", "architect Skill must point to its workspace-relative CLI", architect))
        if "evaluationVersion" in manifest:
            evaluation = skills.get(EVALUATION_SKILL)
            helper = str(Path(cli).with_name("agent_evaluation.py")).replace("\\", "/")
            if evaluation != EVALUATION_FILE:
                errors.append(error("DELIVERY_EVALUATION_MISSING", "evaluation Skill must be indexed at its fixed path", EVALUATION_FILE))
            elif evaluation:
                block, _ = validate_scaffold.Validator(workspace, validate_scaffold.Collector()).parse_document(evaluation, True)
                data = block.data if block and isinstance(block.data, dict) else {}
                text = (path_bytes(workspace, evaluation) or b"").decode("utf-8", "replace")
                if data.get("sideEffects") != "writes-workspace" or data.get("confirmation") != "required":
                    errors.append(error("DELIVERY_EVALUATION_POLICY", "evaluation needs writes-workspace and required confirmation", evaluation))
                if not data.get("implementation") or not validate_scaffold.description_body(text).strip() or helper not in text:
                    errors.append(error("DELIVERY_EVALUATION_TOOL_MISSING", "evaluation Skill must contain rules and locate protected agent_evaluation.py", evaluation))
        if "migrationVersion" not in manifest:
            errors.append(error("DELIVERY_MIGRATION_MISSING",
                                "package lacks the protected asset migration tool", MIGRATION_TOOL))
        else:
            migration_tool = str(Path(cli).with_name(MIGRATION_TOOL)).replace("\\", "/")
            if (manifest["entrypoints"].get("migrationTool") != migration_tool
                    or migration_tool not in manifest["protectedFiles"]
                    or migration_tool not in manifest["files"]):
                errors.append(error("DELIVERY_MIGRATION_MISSING",
                                    "asset migration tool must be packaged and protected", migration_tool))
        archive = skills.get(points["archiveSkill"])
        if archive:
            archive_text = (path_bytes(workspace, archive) or b"").decode("utf-8", "replace")
            matches = re.findall(r"```agent-archive\s*\n(.*?)\n```", archive_text, re.DOTALL)
            try:
                contract = json.loads(matches[0]) if len(matches) == 1 else None
                if not isinstance(contract, dict) or contract.get("authorization") not in ("archive-action", "candidate-confirm") or not isinstance(contract.get("targets"), list):
                    raise ValueError("archive contract needs a supported authorization and target list")
            except (ValueError, json.JSONDecodeError) as exc:
                errors.append(error("DELIVERY_ARCHIVE_CONTRACT", str(exc), archive))
        entry_text = (path_bytes(workspace, "AGENTS.md") or b"").decode("utf-8", "replace")
        if MANIFEST_FILE not in validate_scaffold.description_body(entry_text):
            errors.append(error("DELIVERY_MANIFEST_NOT_DISCOVERABLE", "entry body must point to " + MANIFEST_FILE, "AGENTS.md"))
        report["entrypoints"] = points
    except PlanError as exc:
        errors.append(error(exc.code, exc.message, exc.path))
    collector = validate_scaffold.Collector()
    for item in errors:
        collector.add(item)
    report["errors"] = validate_scaffold.sorted_errors(collector.items)
    legacy_gaps = {"DELIVERY_EVALUATION_MISSING", "DELIVERY_MIGRATION_MISSING"}
    report["status"] = ("complete" if not report["errors"] else "legacy"
                        if (report.get("evaluationStatus") == "legacy" or report.get("migrationStatus") == "legacy")
                        and all(item["code"] in legacy_gaps for item in report["errors"])
                        else "intermediate")
    return report


def check_maintenance_boundaries(workspace: str, plan: dict) -> None:
    old = load_package_manifest(workspace) if os.path.lexists(Path(workspace) / MANIFEST_FILE) else None
    new_text = plan["files"].get(MANIFEST_FILE)
    new = load_package_manifest(workspace, new_text) if isinstance(new_text, str) else old
    check_adoption_completeness(workspace, plan, old, new)
    if old:
        for rel in old["protectedFiles"]:
            if rel in plan["files"] and current_hash(workspace, rel) != (None if plan["files"][rel] is None else sha256_bytes(plan["files"][rel].encode("utf-8"))):
                raise PlanError("ARCHITECT_PROTECTED", "architect files may only change through upgrade", rel)
        if MANIFEST_FILE in plan["files"]:
            if new_text is None:
                raise PlanError("ARCHITECT_PROTECTED", "cannot remove the architect manifest", MANIFEST_FILE)
            immutable_old = {key: value for key, value in old.items() if key not in ("files", "description")}
            immutable_new = {key: value for key, value in new.items() if key not in ("files", "description")}
            if immutable_old != immutable_new or {rel: old["files"][rel] for rel in old["protectedFiles"]} != {rel: new["files"].get(rel) for rel in old["protectedFiles"]}:
                raise PlanError("ARCHITECT_PROTECTED", "ordinary maintenance cannot change architect metadata", MANIFEST_FILE)
    for rel in plan["files"]:
        if rel.startswith(ARCHITECT_ROOT) and (not new or rel not in new["protectedFiles"]):
            raise PlanError("ARCHITECT_PROTECTED", "architect paths require a protected manifest declaration", rel)
    if old or new or DESCRIPTION_FILE in plan["files"]:
        if DESCRIPTION_FILE not in plan["files"] or plan.get("descriptionSync") not in ("updated", "unchanged"):
            raise PlanError("DESCRIPTION_SYNC_MISSING", "include complete Description, baseline and descriptionSync updated/unchanged", DESCRIPTION_FILE)
        target = plan["files"][DESCRIPTION_FILE]
        if target is None:
            raise PlanError("DESCRIPTION_REQUIRED", "cannot delete a formal Description", DESCRIPTION_FILE)
        before = path_bytes(workspace, DESCRIPTION_FILE)
        after = target.encode("utf-8")
        if (plan["descriptionSync"] == "unchanged") != (before == after):
            raise PlanError("DESCRIPTION_SYNC_INVALID", "descriptionSync must agree with byte content", DESCRIPTION_FILE)
        if before is not None and before != after:
            try:
                old_info = validate_scaffold.description_metadata(before.decode("utf-8"))
                new_info = validate_scaffold.description_metadata(target)
                body_changed = validate_scaffold.description_body(before.decode("utf-8")) != validate_scaffold.description_body(target)
                expected = old_info["baselineVersion"] + int(body_changed)
                if new_info["baselineVersion"] != expected:
                    raise PlanError("DESCRIPTION_VERSION_INVALID", "increment baselineVersion only when the body changes", DESCRIPTION_FILE)
                if not body_changed:
                    raise PlanError("DESCRIPTION_NO_CHANGE", "retain original bytes when Description body is unchanged", DESCRIPTION_FILE)
            except (UnicodeError, ValueError, IndexError, KeyError, TypeError, validate_scaffold.YamlFail) as exc:
                raise PlanError("DESCRIPTION_FIELD_INVALID", str(exc), DESCRIPTION_FILE) from exc


def check_adoption_completeness(workspace: str, plan: dict,
                                old_manifest: dict | None, new_manifest: dict | None) -> None:
    if old_manifest is not None:
        return
    classification, inventory, marker_error = workspace_adoption_state(workspace)
    formal_intent = (classification == "existing-unmanaged"
                     or MANIFEST_FILE in plan["files"]
                     or (classification == "missing" and DESCRIPTION_FILE in plan["files"]))
    if not formal_intent:
        return

    required = ("AGENTS.md", DESCRIPTION_FILE, MANIFEST_FILE)
    missing = [rel for rel in required
               if rel not in plan["files"] or plan["files"][rel] is None]
    if new_manifest is None:
        missing.append("indexed initial/archive/architect/evaluation capabilities and migration tool")
    else:
        if "evaluationVersion" not in new_manifest:
            missing.append("default scaffold-evaluate capability and tool")
        if new_manifest.get("migrationVersion") != MIGRATION_VERSION:
            missing.append("protected asset migration tool")
        absent = sorted(rel for rel in new_manifest["files"]
                        if rel not in plan["files"] or plan["files"][rel] is None)
        missing.extend(absent)

    if (classification in ("existing-unmanaged", "legacy", "formal")
            and (not inventory["complete"] or marker_error is not None)):
        raise PlanError("ADOPTION_INVENTORY_INCOMPLETE",
                        "read-only inventory is incomplete; resolve its path diagnostics before adoption")
    if DESCRIPTION_FILE in plan["files"] and isinstance(plan["files"][DESCRIPTION_FILE], str):
        try:
            metadata = validate_scaffold.description_metadata(plan["files"][DESCRIPTION_FILE])
            if metadata.get("origin") != "confirmed":
                missing.append("user-confirmed Description (origin: confirmed)")
        except (ValueError, IndexError, KeyError, TypeError, validate_scaffold.YamlFail):
            missing.append("valid user-confirmed Description")
    if missing:
        unique = list(dict.fromkeys(missing))
        raise PlanError(
            "ADOPTION_FORMAL_INCOMPLETE",
            "formal adoption requires a confirmed Description, AGENTS.md entry, actual indexed capabilities, "
            "evaluation capability, manifest, and baselines for every manifest file; missing: "
            + ", ".join(unique),
            MANIFEST_FILE,
        )
    if classification == "existing-unmanaged" and "AGENTS.md" in plan["files"]:
        original = path_bytes(workspace, "AGENTS.md")
        replacement = plan["files"]["AGENTS.md"]
        if original is not None and isinstance(replacement, str):
            try:
                original_rules = original.decode("utf-8-sig")
            except UnicodeError as exc:
                raise PlanError("ADOPTION_RULES_UNREADABLE",
                                "existing AGENTS.md is not valid UTF-8; preserve and resolve it before adoption",
                                "AGENTS.md") from exc
            original_rules = original_rules.replace("\r\n", "\n").replace("\r", "\n")
            replacement = replacement.replace("\r\n", "\n").replace("\r", "\n")
            if original_rules and original_rules not in replacement:
                raise PlanError("ADOPTION_RULES_NOT_PRESERVED",
                                "formal adoption must preserve the complete original AGENTS.md body verbatim",
                                "AGENTS.md")


def workspace_adoption_state(workspace: str) -> tuple[str, dict, dict | None]:
    if not os.path.lexists(workspace):
        return "missing", {"complete": True}, None
    if not os.path.isdir(workspace):
        return "existing-unmanaged", {"complete": False}, error(
            "WORKSPACE_NOT_DIRECTORY", "workspace is not a directory")
    inventory = inventory_workspace(workspace)
    marker, marker_error = has_scaffold_contract_marker(workspace)
    if marker:
        return "formal", inventory, marker_error
    if os.path.lexists(os.path.join(workspace, "AGENTS.md")):
        return "existing-unmanaged", inventory, marker_error
    legacy_path = os.path.join(workspace, LEGACY_ENTRY)
    if os.path.lexists(legacy_path):
        legacy, _ = legacy_entry(workspace)
        if legacy["recognized"]:
            return "legacy", inventory, marker_error
    return ("existing-unmanaged" if inventory["files"] else "missing"), inventory, marker_error


def render_description(body: str, baseline_version: int = 1, origin: str = "confirmed") -> str:
    body = body.replace("\r\n", "\n").replace("\r", "\n")
    return "---\nschemaVersion: 1\nbaselineVersion: %d\norigin: %s\ngeneratorVersion: \"%s\"\ncontentSha256: %s\n---\n%s" % (baseline_version, origin, TOOL_VERSION, sha256_bytes(body.encode("utf-8")), body)


def package_source(source: str, temp: str) -> str:
    if source.startswith("git+"):
        target = Path(temp) / "git-package"
        try:
            subprocess.run(["git", "-c", "core.hooksPath=" + temp, "-c", "core.autocrlf=false", "clone", "--depth", "1", "--", source[4:], str(target)],
                           check=True, capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            raise PlanError("PACKAGE_SOURCE_FAILED", str(exc), source) from exc
        return str(target)
    if source.startswith(("https://", "http://")):
        archive = Path(temp) / "package.zip"
        try:
            # ponytail: URL packages are ZIP files capped at 64 MiB; add streaming formats only when needed.
            with urllib.request.urlopen(source, timeout=30) as response:
                data = response.read(64 * 1024 * 1024 + 1)
            if len(data) > 64 * 1024 * 1024:
                raise ValueError("URL package exceeds 64 MiB")
            archive.write_bytes(data)
            target = Path(temp) / "url-package"
            target.mkdir()
            with zipfile.ZipFile(archive) as zipped:
                if sum(item.file_size for item in zipped.infolist()) > 64 * 1024 * 1024:
                    raise ValueError("expanded package exceeds 64 MiB")
                seen = set()
                for item in zipped.infolist():
                    if item.is_dir():
                        continue
                    rel = item.filename if item.filename == "architect-bundle-manifest.json" else safe_rel(item.filename)
                    if rel.casefold() in seen or (item.external_attr >> 16) & 0o170000 == 0o120000:
                        raise ValueError("ZIP package contains duplicate files or symbolic links")
                    seen.add(rel.casefold())
                    path = safe_target_path(str(target), rel)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(zipped.read(item))
            return str(target)
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            raise PlanError("PACKAGE_SOURCE_FAILED", str(exc), source) from exc
    target = abs_workspace(source)
    if not os.path.isdir(target):
        raise PlanError("PACKAGE_SOURCE_FAILED", "local package source must be a directory", source)
    return target


def source_commit(workspace: str) -> str:
    """Return the repository commit when available, otherwise an explicit unknown marker."""
    try:
        completed = subprocess.run(["git", "-C", workspace, "rev-parse", "HEAD"],
                                   check=True, capture_output=True, text=True, timeout=5)
        value = completed.stdout.strip().lower()
        return value if re.fullmatch(r"[0-9a-f]{40}", value) else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def add_bundle_metadata(manifest: dict, workspace: str) -> dict:
    """Add release identity while preserving packageVersion for old consumers."""
    manifest.pop("manifestFormat", None)
    version = manifest.get("bundleVersion", manifest.get("packageVersion"))
    manifest["bundleVersion"] = version
    manifest["packageVersion"] = version
    manifest["sourceRepository"] = manifest.get("sourceRepository", CANONICAL_REPOSITORY)
    manifest["sourceCommit"] = manifest.get("sourceCommit", source_commit(workspace))
    manifest["assetName"] = RELEASE_ASSET_PREFIX + version + ".zip"
    return manifest


def pack_workspace(workspace: str, output: str | None, candidate: str | None, review_path: str | None) -> dict:
    report = formal_delivery(workspace)
    if report["errors"]:
        raise PlanError("PACKAGE_SOURCE_INVALID", json.dumps(report["errors"], ensure_ascii=False))
    if not candidate or not review_path:
        raise PlanError("PACKAGE_REDACTION_REQUIRED", "provide --description-candidate and --redaction-review", DESCRIPTION_FILE)
    review = read_json(review_path)
    candidate_bytes = Path(candidate).read_bytes()
    original = path_bytes(workspace, DESCRIPTION_FILE)
    info = report["description"]
    if review.get("status") != "approved" or review.get("unresolved") != []:
        raise PlanError("PACKAGE_SENSITIVE_UNRESOLVED", "sensitive review must be approved with unresolved: []", DESCRIPTION_FILE)
    if review.get("sourceDescriptionSha256") != sha256_bytes(original) or review.get("sourceBaselineVersion") != info["baselineVersion"]:
        raise PlanError("PACKAGE_REDACTION_STALE", "redaction review does not match the local Description baseline", DESCRIPTION_FILE)
    if review.get("candidateSha256") != sha256_bytes(candidate_bytes):
        raise PlanError("PACKAGE_REDACTION_STALE", "candidate bytes differ from the reviewed candidate hash", DESCRIPTION_FILE)
    if not all(isinstance(review.get(key), list) and all(isinstance(item, str) for item in review[key]) for key in ("removed", "generalized")):
        raise PlanError("PACKAGE_REDACTION_INVALID", "review needs removed/generalized string lists", DESCRIPTION_FILE)
    if not output:
        raise PlanError("PACKAGE_OUTPUT_REQUIRED", "pack requires --output")
    destination = Path(abs_workspace(output))
    if os.path.lexists(destination) or validate_scaffold.is_within(os.path.realpath(destination), os.path.realpath(workspace)):
        raise PlanError("PACKAGE_OUTPUT_CONFLICT", "output must be a new directory outside the workspace", str(destination))
    manifest = add_bundle_metadata(load_package_manifest(workspace), workspace)
    with tempfile.TemporaryDirectory(prefix="agent-package-") as stage:
        for rel in manifest["files"]:
            path = Path(stage) / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            content = candidate_bytes if rel == DESCRIPTION_FILE else path_bytes(workspace, rel)
            path.write_bytes(content)
        candidate_info = validate_scaffold.description_info(stage)
        if candidate_info["errors"]:
            raise PlanError("PACKAGE_REDACTION_INVALID", json.dumps(candidate_info["errors"], ensure_ascii=False), DESCRIPTION_FILE)
        manifest["files"][DESCRIPTION_FILE] = current_hash(stage, DESCRIPTION_FILE)
        manifest["description"] = {"path": DESCRIPTION_FILE, **{key: candidate_info[key] for key in ("baselineVersion", "origin", "contentSha256")},
                                   "redacted": True, "sourceBaselineVersion": info["baselineVersion"]}
        excluded = sorted(rel for rel in inspect_workspace(workspace)["files"] if rel not in manifest["files"] and rel != MANIFEST_FILE)
        manifest["exclusions"] = excluded
        target_manifest = Path(stage) / MANIFEST_FILE
        target_manifest.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8", newline="\n")
        validation = formal_delivery(stage)
        if validation["errors"]:
            raise PlanError("PACKAGE_VALIDATION_FAILED", json.dumps(validation["errors"], ensure_ascii=False))
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".agent-package-", dir=destination.parent))
        try:
            shutil.copytree(stage, temporary, dirs_exist_ok=True)
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    return {"output": str(destination), "packageVersion": manifest["packageVersion"],
            "bundleVersion": manifest["bundleVersion"], "sourceRepository": manifest["sourceRepository"],
            "sourceCommit": manifest["sourceCommit"], "assetName": manifest["assetName"],
            "files": sorted(manifest["files"]),
            "excluded": excluded, "redaction": review, "description": manifest["description"]}


def package_operation(operation: str, workspace: str, source: str | None, confirm: bool,
                      output: str | None = None, candidate: str | None = None, review: str | None = None) -> tuple[dict, int]:
    result = base_result(operation, workspace)
    try:
        if operation == "pack":
            result["package"] = pack_workspace(workspace, output, candidate, review)
            return result, 0
        with tempfile.TemporaryDirectory(prefix="agent-package-source-") as temp:
            package = package_source(source or workspace, temp)
            if operation == "package-check" and (Path(package) / "architect-bundle-manifest.json").is_file():
                bundle_manifest = _read_architect_bundle_manifest(package)
                result.update(status="ok", bundleVersion=bundle_manifest["bundleVersion"],
                              sourceRepository=bundle_manifest["sourceRepository"],
                              sourceCommit=bundle_manifest["sourceCommit"],
                              assetName=bundle_manifest["assetName"],
                              manifest="architect-bundle-manifest.json",
                              evaluationVersion=bundle_manifest.get("evaluationVersion"),
                              evaluationStatus="available" if "evaluationVersion" in bundle_manifest else "legacy",
                              migrationVersion=bundle_manifest.get("migrationVersion"),
                              migrationStatus="available" if "migrationVersion" in bundle_manifest else "legacy",
                              files=bundle_manifest["files"],
                              protectedFiles=bundle_manifest["protectedFiles"])
                return result, 0
            delivery = formal_delivery(package)
            if delivery["errors"]:
                result.update(status="error", delivery=delivery, errors=delivery["errors"])
                return result, 1
            incoming = load_package_manifest(package)
            if operation != "upgrade" and not incoming["description"]["redacted"]:
                raise PlanError("PACKAGE_REDACTION_REQUIRED", "installation sources must carry a redacted Description", DESCRIPTION_FILE)
            result["packageVersion"] = incoming["packageVersion"]
            if operation == "package-check":
                result["delivery"] = delivery
                return result, 0
            if os.path.lexists(workspace) and not os.path.isdir(workspace):
                raise PlanError("WORKSPACE_NOT_DIRECTORY", "target is not a directory")
            if operation == "install":
                if os.path.lexists(Path(workspace) / "AGENTS.md") or os.path.lexists(Path(workspace) / ".agent"):
                    raise PlanError("PACKAGE_INSTALL_CONFLICT", "existing scaffold requires upgrade")
                paths = set(incoming["files"]) | {MANIFEST_FILE}
                files = {rel: path_bytes(package, rel).decode("utf-8") for rel in paths}
                for rel in paths:
                    if os.path.lexists(Path(workspace) / rel):
                        raise PlanError("PACKAGE_INSTALL_CONFLICT", "target file already exists", rel)
            else:
                installed = load_package_manifest(workspace)
                old_version = tuple(map(int, installed["packageVersion"].split(".")))
                new_version = tuple(map(int, incoming["packageVersion"].split(".")))
                if new_version <= old_version:
                    raise PlanError("PACKAGE_VERSION_NOT_NEWER", "upgrade packageVersion must be newer")
                if incoming["entrypoints"]["architectSkill"] != installed["entrypoints"]["architectSkill"]:
                    raise PlanError("PACKAGE_ENTRYPOINT_CONFLICT", "upgrade must retain the indexed architect Skill id")
                conflicts = []
                for rel in installed["protectedFiles"]:
                    actual = current_hash(workspace, rel)
                    if actual != installed["files"][rel]:
                        conflicts.append({**error("PACKAGE_BASELINE_CONFLICT", "protected file differs from installed baseline", rel),
                                          "actualHash": actual, "expectedHash": installed["files"][rel]})
                for rel in set(incoming["protectedFiles"]) - set(installed["protectedFiles"]):
                    if os.path.lexists(Path(workspace) / rel):
                        conflicts.append(error("PACKAGE_BASELINE_CONFLICT", "new protected path already exists", rel))
                if conflicts:
                    result.update(status="conflict", errors=conflicts)
                    return result, 1
                files = {rel: (path_bytes(package, rel).decode("utf-8") if rel in incoming["protectedFiles"] else None)
                         for rel in set(installed["protectedFiles"]) | set(incoming["protectedFiles"])}
                for rel in installed["protectedFiles"]:
                    installed["files"].pop(rel)
                installed["files"].update({rel: incoming["files"][rel] for rel in incoming["protectedFiles"]})
                installed["protectedFiles"] = incoming["protectedFiles"]
                installed["entrypoints"]["architectCli"] = incoming["entrypoints"]["architectCli"]
                if "evaluationVersion" in incoming:
                    installed["evaluationVersion"] = incoming["evaluationVersion"]
                    installed["entrypoints"]["evaluationSkill"] = EVALUATION_SKILL
                    entry = _entry_with_evaluation(workspace)
                    if entry != path_bytes(workspace, "AGENTS.md"):
                        files["AGENTS.md"] = entry.decode("utf-8")
                        installed["files"]["AGENTS.md"] = sha256_bytes(entry)
                elif "evaluationVersion" in installed:
                    raise PlanError("PACKAGE_EVALUATION_DOWNGRADE", "upgrade cannot remove the default evaluation capability")
                if "migrationVersion" in incoming:
                    installed["migrationVersion"] = incoming["migrationVersion"]
                    installed["entrypoints"]["migrationTool"] = incoming["entrypoints"]["migrationTool"]
                elif "migrationVersion" in installed:
                    raise PlanError("PACKAGE_MIGRATION_DOWNGRADE", "upgrade cannot remove the asset migration tool")
                installed["packageVersion"] = incoming["packageVersion"]
                installed["source"] = incoming["source"]
                if "bundleVersion" in incoming:
                    for key in ("bundleVersion", "contractVersion", "sourceRepository", "sourceCommit", "assetName"):
                        if key in incoming:
                            installed[key] = incoming[key]
                else:
                    # A legacy package upgrade remains legacy until a new
                    # bundle is explicitly installed; do not create a mixed
                    # manifest whose two version fields disagree.
                    for key in ("bundleVersion", "sourceRepository", "sourceCommit", "assetName"):
                        installed.pop(key, None)
                files[MANIFEST_FILE] = json.dumps(installed, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
            plan = {"schemaVersion": 1, "workspace": workspace, "baseline": {rel: current_hash(workspace, rel) for rel in files}, "files": files}
            changes, conflicts = plan_changes(workspace, plan)
            result["changes"] = changes
            overlay_errors = validate_overlay(workspace, plan)
            if conflicts or overlay_errors:
                result.update(status="conflict" if conflicts else "error", errors=conflicts or overlay_errors)
                return result, 1
            if not confirm:
                result["status"] = "needs-confirmation"
                return result, 0
            _, conflicts = plan_changes(workspace, plan)
            if conflicts:
                result.update(status="conflict", errors=conflicts)
                return result, 1
            backup, failures = apply_plan(workspace, plan, changes)
            if backup:
                result["backup"] = backup_summary(load_backup(workspace, backup))
            if failures:
                result["workspaceState"] = "partial-apply" if any(item["code"] == "APPLY_RESTORE_FAILED" for item in failures) else "restored-after-apply-failure"
            if not failures:
                result["delivery"] = formal_delivery(workspace)
                failures = result["delivery"]["errors"]
                if failures:
                    recovery = rollback_applied(workspace, backup, changes)
                    failures += recovery
                    result["workspaceState"] = "partial-apply" if recovery else "restored-after-validation-failure"
            if failures:
                result.update(status="error", errors=failures)
                return result, 1
            return result, 0
    except (PlanError, OSError, UnicodeError) as exc:
        result.update(status="error", errors=[error(exc.code if isinstance(exc, PlanError) else "PACKAGE_IO_FAILED", str(exc), getattr(exc, "path", None))])
        return result, 1


def _release_error(code: str, message: str, path: str | None = None) -> PlanError:
    return PlanError(code, message, path)


def _release_url(url: str, *, api: bool = False) -> urllib.parse.ParseResult:
    """Validate a Release URL before opening it.

    The preflight path accepts only GitHub's canonical repository.  Local
    directories are handled separately as deterministic test/package inputs.
    """
    try:
        parsed = urllib.parse.urlparse(url)
    except (TypeError, ValueError) as exc:
        raise _release_error("RELEASE_SOURCE_INVALID", "Release source URL is invalid", url) from exc
    if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.fragment
            or parsed.port not in (None, 443)):
        raise _release_error("RELEASE_SOURCE_INVALID", "Release source must use HTTPS without credentials or fragments", url)
    host = (parsed.hostname or "").lower()
    if api:
        expected = "/repos/%s/releases/latest" % CANONICAL_REPOSITORY
        if host != "api.github.com" or parsed.path.rstrip("/") != expected or parsed.query:
            raise _release_error("RELEASE_SOURCE_INVALID", "Release metadata URL must be the canonical latest Release endpoint", url)
    elif host != "github.com" or not parsed.path.startswith(
        "/%s/releases/download/" % CANONICAL_REPOSITORY
    ):
        raise _release_error("RELEASE_SOURCE_INVALID", "Release asset host is not the canonical GitHub host", url)
    return parsed


def _bounded_url_bytes(url: str, limit: int, *, api: bool = False) -> bytes:
    parsed = _release_url(url, api=api)
    request = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json" if api else "application/zip",
        "User-Agent": "personal-agent-scaffold/%s" % TOOL_VERSION,
    })
    try:
        opener = urllib.request.build_opener(_ReleaseRedirect())
        with opener.open(request, timeout=RELEASE_TIMEOUT_SECONDS) as response:
            length = response.headers.get("Content-Length") if getattr(response, "headers", None) else None
            if length is not None:
                try:
                    if int(length) > limit:
                        raise _release_error("RELEASE_RESPONSE_TOO_LARGE", "Release response exceeds the size limit", url)
                except ValueError:
                    pass
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = response.read(min(1024 * 1024, limit - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise _release_error("RELEASE_RESPONSE_TOO_LARGE", "Release response exceeds the size limit", url)
                chunks.append(chunk)
            return b"".join(chunks)
    except PlanError:
        raise
    except (OSError, urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        raise _release_error("RELEASE_OFFLINE", "Unable to retrieve canonical Release: %s" % exc, url) from exc


class _ReleaseRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        parsed = urllib.parse.urlparse(url)
        if (parsed.scheme != "https" or parsed.username or parsed.password
                or parsed.port not in (None, 443) or parsed.hostname not in (
                    "api.github.com", "github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"
                )):
            raise _release_error("RELEASE_SOURCE_INVALID", "Release redirect must remain on trusted GitHub HTTPS hosts", url)
        return super().redirect_request(request, response, code, message, headers, url)


def _release_version(value: object) -> tuple[int, int, int]:
    if not isinstance(value, str):
        raise ValueError("Release version must be a major.minor.patch string")
    text = value[1:] if value.startswith(("v", "V")) else value
    if not re.fullmatch(r"\d+\.\d+\.\d+", text):
        raise ValueError("Release version must be a major.minor.patch string")
    return tuple(int(part) for part in text.split("."))


def _extract_release_zip(data: bytes, destination: str) -> str:
    Path(destination).mkdir(parents=True, exist_ok=True)
    archive = Path(destination) / "release.zip"
    package = Path(destination) / "package"
    archive.write_bytes(data)
    package.mkdir()
    try:
        with zipfile.ZipFile(archive) as zipped:
            entries = zipped.infolist()
            seen: set[str] = set()
            expanded = 0
            for item in entries:
                raw_name = item.filename.replace("\\", "/")
                if item.flag_bits & 1:
                    raise _release_error("PACKAGE_INVALID", "Release ZIP must not contain encrypted files", raw_name)
                mode = (item.external_attr >> 16) & 0o170000
                if mode == 0o120000:
                    raise _release_error("PACKAGE_SOURCE_FAILED", "Release ZIP contains symbolic links", raw_name)
                if not raw_name or raw_name.endswith("/"):
                    continue
                rel = raw_name if raw_name == "architect-bundle-manifest.json" else safe_rel(raw_name)
                folded = rel.casefold()
                if folded in seen:
                    raise _release_error("PACKAGE_SOURCE_FAILED", "Release ZIP contains duplicate files", rel)
                seen.add(folded)
                expanded += item.file_size
                if expanded > RELEASE_ARCHIVE_LIMIT:
                    raise _release_error("RELEASE_RESPONSE_TOO_LARGE", "expanded Release package exceeds the size limit", rel)
                target = safe_target_path(str(package), rel)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(zipped.read(item))
    except zipfile.BadZipFile as exc:
        raise _release_error("PACKAGE_INVALID", "Release asset is not a valid ZIP archive") from exc
    except (OSError, KeyError, RuntimeError, NotImplementedError) as exc:
        raise _release_error("PACKAGE_INVALID", str(exc)) from exc
    return str(package)


def _read_architect_bundle_manifest(root: str) -> dict:
    """Validate the standalone Release bundle manifest and all listed bytes."""
    path = Path(root) / "architect-bundle-manifest.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_manifest_pairs)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise _release_error("PACKAGE_INVALID", "architect bundle manifest is unreadable", str(path)) from exc
    if not isinstance(manifest, dict) or type(manifest.get("schemaVersion")) is not int or manifest["schemaVersion"] != 1:
        raise _release_error("PACKAGE_INVALID", "architect bundle manifest schemaVersion must be 1", str(path))
    version = manifest.get("bundleVersion")
    try:
        _release_version(version)
    except ValueError as exc:
        raise _release_error("PACKAGE_INVALID", str(exc), "bundleVersion") from exc
    if manifest.get("contractVersion") != "1" or manifest.get("sourceRepository") != CANONICAL_REPOSITORY:
        raise _release_error("RELEASE_SOURCE_INVALID", "architect bundle source or contract is not canonical", str(path))
    commit = manifest.get("sourceCommit")
    if not isinstance(commit, str) or not re.fullmatch(r"(?:[0-9a-f]{40}|unknown)", commit):
        raise _release_error("PACKAGE_INVALID", "sourceCommit must be a 40-character SHA-1 or 'unknown'", "sourceCommit")
    expected_asset = RELEASE_ASSET_PREFIX + version + RELEASE_ASSET_SUFFIX
    if manifest.get("assetName") != expected_asset:
        raise _release_error("PACKAGE_INVALID", "assetName does not match bundleVersion", "assetName")
    files = manifest.get("files")
    protected = manifest.get("protectedFiles")
    points = manifest.get("entrypoints")
    if (not isinstance(files, dict) or not files or not isinstance(protected, list)
            or not all(isinstance(rel, str) for rel in protected)
            or sorted(protected) != sorted(files) or len(protected) != len(set(protected))):
        raise _release_error("PACKAGE_INVALID", "architect bundle files/protectedFiles are inconsistent", str(path))
    roles = {"skill", "cli", "validator"}
    if "evaluationVersion" in manifest:
        if manifest["evaluationVersion"] != EVALUATION_VERSION:
            raise _release_error("PACKAGE_INVALID", "unsupported evaluationVersion", str(path))
        roles.update(("evaluationSkill", "evaluationTool"))
    if "migrationVersion" in manifest:
        if manifest["migrationVersion"] != MIGRATION_VERSION:
            raise _release_error("PACKAGE_INVALID", "unsupported migrationVersion", str(path))
        roles.add("migrationTool")
    if (not isinstance(points, dict) or set(points) != roles
            or any(not isinstance(points[key], str) or points[key] not in files for key in points)):
        raise _release_error("PACKAGE_INVALID", "architect bundle entrypoints are incomplete", str(path))
    if "evaluationVersion" in manifest:
        helper = str(Path(points["cli"]).with_name("agent_evaluation.py")).replace("\\", "/")
        rules = str(Path(points["cli"]).with_name("scaffold-evaluate.md")).replace("\\", "/")
        if points["evaluationSkill"] != EVALUATION_FILE or points["evaluationTool"] != helper or rules not in files:
            raise _release_error("PACKAGE_INVALID", "evaluation bundle paths or canonical rules are incomplete", str(path))
    if "migrationVersion" in manifest:
        migration_tool = str(Path(points["cli"]).with_name(MIGRATION_TOOL)).replace("\\", "/")
        if points["migrationTool"] != migration_tool:
            raise _release_error("PACKAGE_INVALID", "asset migration bundle path is invalid", str(path))
    for rel, expected in files.items():
        try:
            safe_rel(rel)
        except PlanError as exc:
            raise _release_error("PACKAGE_INVALID", exc.message, rel) from exc
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise _release_error("PACKAGE_INVALID", "architect bundle hash is invalid", rel)
        target = safe_target_path(root, rel)
        if target.is_symlink() or not target.is_file() or sha256_bytes(target.read_bytes()) != expected:
            raise _release_error("PACKAGE_INVALID", "architect bundle file hash mismatch", rel)
    found = {path.relative_to(root).as_posix() for path in Path(root).rglob("*") if path.is_file() and path.name != "architect-bundle-manifest.json"}
    if found != set(files):
        raise _release_error("PACKAGE_INVALID", "architect bundle contains files outside its manifest", str(path))
    return manifest


def _candidate_manifest(root: str) -> tuple[dict, bool]:
    bundle_path = Path(root) / "architect-bundle-manifest.json"
    if bundle_path.is_file():
        return _read_architect_bundle_manifest(root), True
    return load_package_manifest(root), False


def _release_candidate(source: str | None, temp: str) -> tuple[str, dict]:
    """Resolve a canonical Release candidate without touching the target."""
    if source and not source.startswith(("https://", "http://")):
        candidate = Path(abs_workspace(source))
        if candidate.is_dir():
            manifest, bundle = _candidate_manifest(str(candidate))
            return str(candidate), {"source": "local", "manifest": manifest, "bundle": bundle, "verified": False}
        if candidate.is_file() and candidate.suffix.lower() == ".zip":
            package = _extract_release_zip(candidate.read_bytes(), temp)
            manifest, bundle = _candidate_manifest(package)
            return package, {"source": "local", "manifest": manifest, "bundle": bundle, "verified": False}
        raise _release_error("RELEASE_SOURCE_INVALID", "local Release source must be a package directory or ZIP", source)

    metadata_url = source or CANONICAL_API_URL
    if metadata_url.startswith("http://"):
        raise _release_error("RELEASE_SOURCE_INVALID", "Release source must use HTTPS", metadata_url)
    if metadata_url.endswith(".zip"):
        parsed = _release_url(metadata_url)
        asset_name = Path(parsed.path).name
        if not re.fullmatch(r"%s\d+\.\d+\.\d+%s" % (re.escape(RELEASE_ASSET_PREFIX), re.escape(RELEASE_ASSET_SUFFIX)), asset_name):
            raise _release_error("RELEASE_SOURCE_INVALID", "Release asset name is not canonical", asset_name)
        data = _bounded_url_bytes(metadata_url, RELEASE_ARCHIVE_LIMIT)
        package = _extract_release_zip(data, temp)
        manifest, bundle = _candidate_manifest(package)
        if manifest.get("assetName") != asset_name:
            raise _release_error("PACKAGE_INVALID", "Release manifest version differs from asset", asset_name)
        return package, {"source": metadata_url, "manifest": manifest, "bundle": bundle, "assetName": asset_name, "verified": True}

    _release_url(metadata_url, api=True)
    raw = _bounded_url_bytes(metadata_url, RELEASE_METADATA_LIMIT, api=True)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise _release_error("PACKAGE_INVALID", "Release metadata is not valid JSON", metadata_url) from exc
    if not isinstance(payload, dict):
        raise _release_error("PACKAGE_INVALID", "Release metadata must be an object", metadata_url)
    tag = payload.get("tag_name")
    try:
        version = _release_version(tag)
    except ValueError as exc:
        raise _release_error("PACKAGE_INVALID", str(exc), "tag_name") from exc
    release_id = payload.get("id")
    if type(release_id) is not int or release_id <= 0 or payload.get("draft") is not False or payload.get("prerelease") is not False:
        raise _release_error("PACKAGE_INVALID", "canonical Release must be a published stable version", metadata_url)
    if (payload.get("html_url") != "https://github.com/%s/releases/tag/%s" % (CANONICAL_REPOSITORY, tag)
            or payload.get("url") != "https://api.github.com/repos/%s/releases/%d" % (CANONICAL_REPOSITORY, release_id)):
        raise _release_error("RELEASE_SOURCE_INVALID", "Release metadata is not from the canonical repository", metadata_url)
    asset_name = "%s%d.%d.%d%s" % ((RELEASE_ASSET_PREFIX,) + version + (RELEASE_ASSET_SUFFIX,))
    assets = payload.get("assets")
    if not isinstance(assets, list):
        raise _release_error("PACKAGE_INVALID", "Release metadata has no assets", metadata_url)
    asset = next((item for item in assets if isinstance(item, dict) and item.get("name") == asset_name), None)
    if asset is None or not isinstance(asset.get("browser_download_url"), str):
        raise _release_error("PACKAGE_INVALID", "canonical Release asset is missing", asset_name)
    download_url = asset["browser_download_url"]
    _release_url(download_url)
    if download_url != "https://github.com/%s/releases/download/%s/%s" % (CANONICAL_REPOSITORY, tag, asset_name):
        raise _release_error("RELEASE_SOURCE_INVALID", "Release asset does not belong to the selected version", download_url)
    data = _bounded_url_bytes(download_url, RELEASE_ARCHIVE_LIMIT)
    expected_digest = asset.get("digest")
    if isinstance(expected_digest, str) and expected_digest.startswith("sha256:"):
        expected_digest = expected_digest[7:]
        if not re.fullmatch(r"[0-9a-f]{64}", expected_digest) or sha256_bytes(data) != expected_digest:
            raise _release_error("PACKAGE_INVALID", "Release asset SHA-256 does not match metadata", asset_name)
    package = _extract_release_zip(data, temp)
    manifest, bundle = _candidate_manifest(package)
    if _manifest_version(manifest) != version or manifest.get("assetName") != asset_name:
        raise _release_error("PACKAGE_INVALID", "Release manifest version differs from tag or asset", asset_name)
    return package, {"source": CANONICAL_REPOSITORY, "manifest": manifest, "assetName": asset_name,
                     "assetUrl": download_url, "tag": tag, "sourceCommit": payload.get("target_commitish"),
                     "bundle": bundle, "verified": True}


def _manifest_source(manifest: dict) -> str:
    return str(manifest.get("sourceRepository") or manifest.get("source") or "")


def _manifest_version(manifest: dict) -> tuple[int, int, int]:
    return _release_version(manifest.get("bundleVersion") or manifest.get("packageVersion"))


def _bundle_file(root: str, manifest: dict, role: str) -> bytes:
    rel = manifest["entrypoints"][role]
    try:
        return path_bytes(root, rel) or b""
    except PlanError as exc:
        raise _release_error("PACKAGE_INVALID", exc.message, rel) from exc


def _bundle_skill_for_target(data: bytes, target_id: str, cli_rel: str, fallback: bytes) -> bytes:
    """Keep the target Skill contract while moving the standalone Skill body."""
    text = data.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")
    if not text.startswith("---\n"):
        return fallback
    end = text.find("\n---\n", 4)
    if end < 0:
        return fallback
    front = text[4:end]
    body = text[end + 5:]
    front = re.sub(r"(?m)^(?:name|id):\s*[^\n]+$", "id: %s" % target_id, front)
    cli_dir = str(Path(cli_rel).parent).replace("\\", "/")
    body = body.replace("{baseDir}/tools", cli_dir).replace(".agent/architect/tools", cli_dir)
    front = front.replace("allowed-tools: Bash(python {baseDir}/tools/agent_scaffold.py:*)", "allowed-tools: Bash(python %s:*)" % cli_rel)
    if not re.search(r"(?m)^id:\s*", front):
        return fallback
    # The standalone Skill intentionally omits the produced-workspace fields.
    # Retain a valid existing frontmatter contract when it cannot be adapted.
    required = ("purpose:", "triggers:", "excludes:", "inputs:", "outputs:", "sideEffects:", "confirmation:", "implementation:")
    if any(marker not in front for marker in required):
        fallback_end = fallback.find(b"\n---\n", 4)
        if fallback.startswith(b"---\n") and fallback_end >= 0:
            old_front = fallback[4:fallback_end].decode("utf-8", "replace")
            old_body = fallback[fallback_end + 5:].decode("utf-8", "replace")
            return ("---\n%s\n---\n%s\n%s" % (old_front, body, old_body)).encode("utf-8")
        return fallback
    return ("---\n%s\n---\n%s" % (front, body)).encode("utf-8")


def _bundle_target_files(bundle: str, bundle_manifest: dict, workspace: str, installed: dict) -> dict[str, bytes]:
    points = installed["entrypoints"]
    target_skill_rel = ".agent/skills/" + points["architectSkill"] + ".md"
    source_skill = _bundle_file(bundle, bundle_manifest, "skill")
    target_cli_rel = points["architectCli"]
    target_validator_rel = str(Path(target_cli_rel).with_name("validate_scaffold.py")).replace("\\", "/")
    mapped = {
        target_skill_rel: _bundle_skill_for_target(source_skill, points["architectSkill"], target_cli_rel, path_bytes(workspace, target_skill_rel) or b""),
        target_cli_rel: _bundle_file(bundle, bundle_manifest, "cli"),
        target_validator_rel: _bundle_file(bundle, bundle_manifest, "validator"),
    }
    source_release = str(Path(bundle_manifest["entrypoints"]["cli"]).with_name("release.json")).replace("\\", "/")
    if source_release in bundle_manifest["files"]:
        target_release = str(Path(target_cli_rel).with_name("release.json")).replace("\\", "/")
        mapped[target_release] = path_bytes(bundle, source_release)
    if "evaluationVersion" in bundle_manifest:
        helper = str(Path(target_cli_rel).with_name("agent_evaluation.py")).replace("\\", "/")
        mapped[EVALUATION_FILE] = _bundle_file(bundle, bundle_manifest, "evaluationSkill").replace(
            b".agent/architect/tools", str(Path(target_cli_rel).parent).replace("\\", "/").encode("utf-8"))
        mapped[helper] = _bundle_file(bundle, bundle_manifest, "evaluationTool")
        source_rules = str(Path(bundle_manifest["entrypoints"]["cli"]).with_name("scaffold-evaluate.md")).replace("\\", "/")
        if source_rules not in bundle_manifest["files"]:
            raise PlanError("PACKAGE_INVALID", "evaluation bundle lacks its canonical rule source", source_rules)
        target_rules = str(Path(target_cli_rel).with_name("scaffold-evaluate.md")).replace("\\", "/")
        mapped[target_rules] = path_bytes(bundle, source_rules)
    if "migrationVersion" in bundle_manifest:
        migration_tool = str(Path(target_cli_rel).with_name(MIGRATION_TOOL)).replace("\\", "/")
        mapped[migration_tool] = _bundle_file(bundle, bundle_manifest, "migrationTool")
    return mapped


def _entry_with_evaluation(workspace: str) -> bytes:
    """Add the fixed index entry while preserving existing root rule text."""
    data = path_bytes(workspace, "AGENTS.md") or b""
    _, indexes = inspect_entry(workspace)
    for item in indexes:
        if item["id"] == EVALUATION_SKILL or item["path"] == EVALUATION_FILE:
            if item["type"] == "skill" and item["id"] == EVALUATION_SKILL and item["path"] == EVALUATION_FILE:
                return data
            raise PlanError("PACKAGE_ENTRYPOINT_CONFLICT", "fixed evaluation index conflicts with an existing object", item["path"])
    text = data.decode("utf-8")
    lines = text.splitlines(keepends=True)
    block, _ = validate_scaffold.Validator(workspace, validate_scaffold.Collector()).parse_document("AGENTS.md", True)
    if not block or "skills" not in block.lines:
        raise PlanError("PACKAGE_ENTRYPOINT_CONFLICT", "formal root entry has no Skills index", "AGENTS.md")
    start = block.lines["skills"]
    insertion = start + 1
    while insertion < len(lines) and (not lines[insertion].strip() or lines[insertion].startswith(" ") or lines[insertion].lstrip().startswith("#")):
        insertion += 1
    newline = "\r\n" if "\r\n" in text else "\n"
    indent = next((len(line) - len(line.lstrip()) for line in lines[start + 1:insertion] if line.lstrip().startswith("- ")), 2)
    item = " " * indent + "- id: " + EVALUATION_SKILL + newline
    item += " " * (indent + 2) + "path: " + EVALUATION_FILE + newline
    lines.insert(insertion, item)
    return "".join(lines).encode("utf-8")


def _materialize_bundle_for_target(bundle: str, bundle_manifest: dict, workspace: str, temp: str) -> str:
    """Convert a standalone Release bundle into the existing upgrade plan shape."""
    installed = load_package_manifest(workspace)
    mapped = _bundle_target_files(bundle, bundle_manifest, workspace, installed)
    stage = Path(temp) / "upgrade-package"
    stage.mkdir(parents=True, exist_ok=True)
    for rel in set(installed["files"]) - set(installed["protectedFiles"]):
        data = path_bytes(workspace, rel)
        if data is not None:
            target = stage / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    for rel, data in mapped.items():
        target = stage / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    synthetic = dict(installed)
    version = bundle_manifest["bundleVersion"]
    synthetic.update({"packageVersion": version, "bundleVersion": version,
                      "contractVersion": bundle_manifest["contractVersion"],
                      "source": bundle_manifest["sourceRepository"],
                      "sourceRepository": bundle_manifest["sourceRepository"],
                      "sourceCommit": bundle_manifest["sourceCommit"],
                      "assetName": bundle_manifest["assetName"],
                      "manifestFormat": "architect-bundle-adapter"})
    synthetic["files"] = {rel: value for rel, value in installed["files"].items() if rel not in installed["protectedFiles"]}
    for rel, data in mapped.items():
        synthetic["files"][rel] = sha256_bytes(data)
    synthetic["protectedFiles"] = sorted(mapped)
    if "evaluationVersion" in bundle_manifest:
        synthetic["evaluationVersion"] = bundle_manifest["evaluationVersion"]
        synthetic["entrypoints"] = {**installed["entrypoints"], "evaluationSkill": EVALUATION_SKILL}
        entry = _entry_with_evaluation(workspace)
        (stage / "AGENTS.md").write_bytes(entry)
        synthetic["files"]["AGENTS.md"] = sha256_bytes(entry)
    if "migrationVersion" in bundle_manifest:
        synthetic["migrationVersion"] = bundle_manifest["migrationVersion"]
        synthetic["entrypoints"] = {
            **synthetic["entrypoints"],
            "migrationTool": str(Path(synthetic["entrypoints"]["architectCli"])
                                 .with_name(MIGRATION_TOOL)).replace("\\", "/"),
        }
    (stage / MANIFEST_FILE).parent.mkdir(parents=True, exist_ok=True)
    (stage / MANIFEST_FILE).write_text(json.dumps(synthetic, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8", newline="\n")
    return str(stage)


def preflight_operation(workspace: str, source: str | None = None) -> tuple[dict, int]:
    result = base_result("preflight", workspace)
    result.update(currentVersion=None, canonicalVersion=None, source=None, differences=[], nextAction="block")
    try:
        manifest_path = Path(workspace) / MANIFEST_FILE
        if not manifest_path.is_file():
            result.update(status="unsupported", reason="manifest-missing", errors=[error("ARCHITECT_PACKAGE_UNSUPPORTED", "target has no architect package manifest", MANIFEST_FILE)])
            return result, 1
        try:
            installed = load_package_manifest(workspace)
        except PlanError as exc:
            result.update(status="unsupported", reason="manifest-invalid", errors=[error("ARCHITECT_PACKAGE_UNSUPPORTED", exc.message, exc.path)])
            return result, 1
        current_version = ".".join(str(part) for part in _manifest_version(installed))
        result["currentVersion"] = current_version
        result["source"] = _manifest_source(installed)
        for rel in installed["protectedFiles"]:
            actual = current_hash(workspace, rel)
            expected = installed["files"].get(rel)
            if actual != expected:
                result["differences"].append({"path": rel, "currentHash": actual, "expectedHash": expected})
        if result["differences"]:
            result.update(status="conflict", reason="protected-files-modified", nextAction="resolve-conflict",
                          errors=[error("PACKAGE_BASELINE_CONFLICT", "protected file differs from installed manifest", item["path"]) for item in result["differences"]])
            return result, 1
        if "evaluationVersion" not in installed and os.path.lexists(Path(workspace) / EVALUATION_FILE):
            result.update(status="conflict", reason="new-protected-path-conflict", nextAction="resolve-conflict",
                          errors=[error("PACKAGE_BASELINE_CONFLICT", "fixed evaluation path already exists outside the installed package", EVALUATION_FILE)])
            return result, 1
        installed_migration_tool = str(Path(installed["entrypoints"]["architectCli"]).with_name(MIGRATION_TOOL)).replace("\\", "/")
        if "migrationVersion" not in installed and os.path.lexists(Path(workspace) / installed_migration_tool):
            result.update(status="conflict", reason="new-protected-path-conflict", nextAction="resolve-conflict",
                          errors=[error("PACKAGE_BASELINE_CONFLICT", "asset migration tool path already exists outside the installed package", installed_migration_tool)])
            return result, 1
        delivery = formal_delivery(workspace)
        if delivery["errors"] and delivery["status"] != "legacy":
            result.update(status="unsupported", reason="target-invalid", errors=delivery["errors"])
            return result, 1
        with tempfile.TemporaryDirectory(prefix="agent-release-preflight-") as temp:
            package, release = _release_candidate(source, temp)
            incoming = release["manifest"]
            canonical_version = ".".join(str(part) for part in _manifest_version(incoming))
            result["canonicalVersion"] = canonical_version
            result["canonicalSource"] = _manifest_source(incoming) or release.get("source")
            result["asset"] = {key: release[key] for key in ("assetName", "assetUrl", "tag", "sourceCommit") if key in release}
            incoming_delivery = {"errors": []} if release.get("bundle") else formal_delivery(package)
            if incoming_delivery["errors"]:
                result.update(status="unsupported", reason="package-invalid", nextAction="block",
                              errors=[error("PACKAGE_INVALID", "canonical package failed formal delivery", item.get("path")) for item in incoming_delivery["errors"]])
                return result, 1
            try:
                old_tuple = _manifest_version(installed)
                new_tuple = _manifest_version(incoming)
            except ValueError as exc:
                result.update(status="unsupported", reason="version-invalid", errors=[error("PACKAGE_INVALID", str(exc))])
                return result, 1
            if new_tuple < old_tuple:
                result.update(status="unsupported", reason="version-not-newer", nextAction="block",
                              errors=[error("VERSION_NOT_NEWER", "canonical Release is older than the installed package")])
                return result, 1
            installed_source = _manifest_source(installed)
            incoming_source = _manifest_source(incoming)
            source_match = incoming_source == CANONICAL_REPOSITORY and installed_source in (
                CANONICAL_REPOSITORY, CANONICAL_REPOSITORY_URL, "personal-agent"
            )
            if not source_match:
                result.update(status="unsupported", reason="source-mismatch", nextAction="block",
                              errors=[error("RELEASE_SOURCE_MISMATCH", "installed package source is not a recognized canonical source")])
                return result, 1
            if release.get("bundle"):
                incoming_hashes = {rel: sha256_bytes(data) for rel, data in
                                   _bundle_target_files(package, incoming, workspace, installed).items()}
            else:
                incoming_hashes = {rel: incoming["files"][rel] for rel in incoming["protectedFiles"]}
            if "evaluationVersion" in installed and "evaluationVersion" not in incoming:
                raise PlanError("PACKAGE_INVALID", "canonical package would remove the default evaluation capability")
            if "migrationVersion" in installed and "migrationVersion" not in incoming:
                raise PlanError("PACKAGE_INVALID", "canonical package would remove the asset migration tool")
            new_conflicts = [error("PACKAGE_BASELINE_CONFLICT", "new protected path already exists", rel)
                             for rel in set(incoming_hashes) - set(installed["protectedFiles"])
                             if os.path.lexists(Path(workspace) / rel)]
            if new_conflicts:
                result.update(status="conflict", reason="new-protected-path-conflict", nextAction="resolve-conflict", errors=new_conflicts)
                return result, 1
            for rel in sorted(set(installed["protectedFiles"]) | set(incoming_hashes)):
                old_hash = installed["files"].get(rel)
                new_hash = incoming_hashes.get(rel)
                if old_hash != new_hash:
                    result["differences"].append({"path": rel, "currentHash": old_hash, "canonicalHash": new_hash})
            if new_tuple == old_tuple and source_match and not result["differences"]:
                result.update(status="up-to-date", nextAction="handoff")
            elif new_tuple == old_tuple and source_match:
                result.update(status="conflict", reason="same-version-content-mismatch", nextAction="resolve-conflict",
                              errors=[error("VERSION_REUSED", "canonical Release reuses the installed version with different protected bytes")])
            elif not source_match and new_tuple == old_tuple:
                result.update(status="conflict", reason="source-mismatch", nextAction="resolve-conflict",
                              errors=[error("RELEASE_SOURCE_MISMATCH", "installed package source differs from canonical Release")])
            else:
                result.update(status="outdated", nextAction="self-update")
            return result, 0 if result["status"] in ("up-to-date", "outdated") else 1
    except PlanError as exc:
        if exc.code == "RELEASE_OFFLINE":
            result.update(status="offline", reason="release-unavailable", nextAction="retry", errors=[error(exc.code, exc.message, exc.path)])
        elif exc.code in ("PACKAGE_INVALID", "PACKAGE_SOURCE_FAILED", "RELEASE_RESPONSE_TOO_LARGE"):
            result.update(status="unsupported", reason="package-invalid", nextAction="block", errors=[error(exc.code, exc.message, exc.path)])
        else:
            result.update(status="unsupported", reason="release-source-invalid", nextAction="block", errors=[error(exc.code, exc.message, exc.path)])
        return result, 1


def reload_architect(workspace: str, expected_version: str | None, old_version: str | None = None) -> dict:
    installed = load_package_manifest(workspace)
    version = ".".join(str(part) for part in _manifest_version(installed))
    if version != expected_version or _manifest_source(installed) not in (
        CANONICAL_REPOSITORY, CANONICAL_REPOSITORY_URL, "personal-agent"
    ):
        raise PlanError("HANDOFF_FAILED", "installed architect version or source differs from verified Release", MANIFEST_FILE)
    points = installed["entrypoints"]
    for rel in installed["protectedFiles"]:
        if current_hash(workspace, rel) != installed["files"][rel]:
            raise PlanError("HANDOFF_FAILED", "protected architect file changed before handoff", rel)
    cli = str(Path(workspace) / points["architectCli"])
    actual = subprocess.run([sys.executable, "-E", "-B", cli, "--version"], check=True, capture_output=True,
                            text=True, encoding="utf-8", timeout=30, cwd=workspace).stdout.strip()
    _release_version(actual)
    release_rel = str(Path(points["architectCli"]).with_name("release.json")).replace("\\", "/")
    release_bytes = path_bytes(workspace, release_rel)
    if release_bytes is not None:
        release = json.loads(release_bytes.decode("utf-8"))
        if not isinstance(release, dict) or actual != release.get("toolVersion"):
            raise PlanError("HANDOFF_FAILED", "reloaded CLI version differs from local release metadata", release_rel)
    checked = subprocess.run([sys.executable, "-E", "-B", cli, "verify", workspace, "--formal", "--json"],
                             check=True, capture_output=True, text=True, encoding="utf-8", timeout=30,
                             cwd=workspace)
    verification = json.loads(checked.stdout)
    if not isinstance(verification, dict) or verification.get("status") != "ok" or verification.get("errors"):
        raise PlanError("HANDOFF_FAILED", "reloaded CLI failed formal verification", points["architectCli"])
    delivery = verification.get("delivery", {})
    if (verification.get("operation") != "verify" or verification.get("workspace") != abs_workspace(workspace)
            or not isinstance(delivery, dict) or delivery.get("status") != "complete"
            or delivery.get("packageVersion") != installed["packageVersion"] or delivery.get("entrypoints") != points):
        raise PlanError("HANDOFF_FAILED", "reloaded CLI verified a different workspace or architect entrypoint", points["architectCli"])
    return {"oldVersion": old_version, "newVersion": version, "version": version, "toolVersion": actual,
            "architectSkill": points["architectSkill"], "architectCli": points["architectCli"],
            "path": cli, "reloaded": True, "source": _manifest_source(installed),
            "verification": verification}


def self_update_operation(workspace: str, source: str | None, confirm: bool) -> tuple[dict, int]:
    result, code = preflight_operation(workspace, source)
    result["operation"] = "self-update"
    if code != 0 or result.get("status") not in ("up-to-date", "outdated"):
        return result, code
    if result.get("status") == "outdated" and not confirm:
        result["status"] = "needs-confirmation"
        result["nextAction"] = "confirm-self-update"
        return result, 0
    applied = None
    try:
        if result.get("status") == "up-to-date":
            result["handoff"] = reload_architect(workspace, result.get("currentVersion"))
            return result, 0
        with tempfile.TemporaryDirectory(prefix="agent-release-update-") as temp:
            package, release = _release_candidate(source, temp)
            if release.get("bundle"):
                package = _materialize_bundle_for_target(package, release["manifest"], workspace, temp)
            old_version = result.get("currentVersion")
            applied, apply_code = package_operation("upgrade", workspace, package, True)
        if apply_code != 0:
            applied["operation"] = "self-update"
            applied["preflight"] = result
            return applied, apply_code
        handoff = reload_architect(workspace, result.get("canonicalVersion"), old_version)
        applied.update(operation="self-update", preflight=result, handoff=handoff)
        return applied, 0
    except (PlanError, OSError, UnicodeError, ValueError, subprocess.SubprocessError) as exc:
        failed = applied if applied is not None else result
        failed.update(operation="self-update", status="error", reason="handoff-failed",
                      errors=[error("HANDOFF_FAILED", str(exc), getattr(exc, "path", None))])
        if applied is not None:
            failed["preflight"] = result
            try:
                recovery = rollback_applied(workspace, applied["backup"]["id"], applied["changes"])
            except (PlanError, OSError, UnicodeError) as restore_exc:
                recovery = [error("HANDOFF_RESTORE_FAILED", str(restore_exc), getattr(restore_exc, "path", None))]
            failed["recovery"] = {"status": "error" if recovery else "ok", "errors": recovery}
            failed["workspaceState"] = "partial-apply" if recovery else "restored-after-handoff-failure"
            failed["errors"].extend(recovery)
        return failed, 1


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Maintain an Agent scaffold deterministically.")
    p.add_argument("operation", choices=("inspect", "check", "diff", "apply", "verify", "restore", "purge", "pack", "package-check", "install", "upgrade", "preflight", "self-update"))
    p.add_argument("workspace", nargs="?", help="workspace path; optional for purge")
    p.add_argument("--plan-file", help="plan JSON path; omit to read stdin")
    p.add_argument("--backup", help="backup id for restore")
    p.add_argument("--older-than-hours", type=float, help="purge backups older than this non-negative age")
    p.add_argument("--workspace-filter", help="purge only backups for this workspace")
    p.add_argument("--confirm", action="store_true", help="confirm writes for apply")
    p.add_argument("--json", action="store_true", help="emit JSON (default)")
    p.add_argument("--formal", action="store_true", help="require complete formal delivery for verify")
    p.add_argument("--package-source", help="local directory, git+URL or HTTP(S) ZIP package")
    p.add_argument("--release-source", help="canonical HTTPS GitHub Release API/asset URL or local fixture package")
    p.add_argument("--output", help="new output directory for pack")
    p.add_argument("--description-candidate", help="reviewed redacted Description Markdown for pack")
    p.add_argument("--redaction-review", help="redaction review JSON for pack")
    p.add_argument("--version", action="version", version=TOOL_VERSION)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    if not args.workspace and args.operation != "purge":
        parser().error("workspace path is required for %s" % args.operation)
    workspace = abs_workspace(args.workspace or ".")
    purge_filter = args.workspace_filter
    if args.operation == "purge" and args.workspace:
        purge_filter = purge_filter or workspace
    if args.operation == "preflight":
        result, code = preflight_operation(workspace, args.release_source or args.package_source)
    elif args.operation == "self-update":
        result, code = self_update_operation(workspace, args.release_source or args.package_source, args.confirm)
    elif args.operation in ("pack", "package-check", "install", "upgrade"):
        result, code = package_operation(args.operation, workspace, args.package_source, args.confirm,
                                         args.output, args.description_candidate, args.redaction_review)
    else:
        result, code = run(args.operation, workspace, args.plan_file, args.confirm, args.backup,
                           args.older_than_hours, purge_filter)
    if args.operation == "verify":
        result["delivery"] = formal_delivery(workspace)
        if args.formal and result["delivery"]["errors"]:
            result.update(status="error", errors=result["delivery"]["errors"])
            code = 1
    sys.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
