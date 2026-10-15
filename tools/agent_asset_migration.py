#!/usr/bin/env python3
"""Plan, apply, verify, and restore explicitly approved workspace asset moves."""

from __future__ import annotations

import argparse
import base64
import difflib
import hashlib
import json
import os
from pathlib import Path
import posixpath
import re
import stat
import sys
import tempfile
from urllib.parse import unquote
import uuid

SCHEMA_VERSION = 1
MAX_FILES = 20000
MAX_DEPTH = 32
MAX_BACKUP_BYTES = 128 * 1024 * 1024
MAX_JOURNAL_BYTES = 2 * MAX_BACKUP_BYTES + 16 * 1024 * 1024
MAX_LINK_SCAN_FILES = 5000
MAX_TEXT_BYTES = 2 * 1024 * 1024
JOURNAL_ROOT = Path(tempfile.gettempdir()).resolve() / "personal-agent-asset-migrations"
RESERVED = re.compile(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?")
PRUNED_LINK_DIRS = frozenset({".git", ".hg", ".svn", ".venv", "venv", "node_modules",
                              "__pycache__", ".cache", "cache", "build", "dist", "target"})
MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\((<[^>]+>|[^)\s]+)(?:\s+[^)]*)?\)")
REFERENCE_LINK = re.compile(r"(?m)^\s*\[[^\]]+\]:\s*(<[^>]+>|\S+)")
RST_LINK = re.compile(r"`[^`<>]*<([^>]+)>`_")
RST_TARGET = re.compile(r"(?m)^\s*\.\.\s+_[^:]+:\s*(\S+)")
HTML_LINK = re.compile(r"""(?i)\b(?:href|src)\s*=\s*["']([^"']+)["']""")


class MigrationError(Exception):
    def __init__(self, code: str, message: str, path: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.path = path


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _result(status: str, errors: list[dict] | None = None, **fields) -> dict:
    return {"schemaVersion": SCHEMA_VERSION, "status": status,
            "errors": errors or [], **fields}


def _diagnostic(code: str, message: str, path: str | None = None) -> dict:
    return {"code": code, "path": path, "message": message}


def _safe_rel(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise MigrationError("PATH_INVALID", "path must use normalized relative POSIX syntax",
                            value if isinstance(value, str) else None)
    if value.startswith("/") or any(part in ("", ".", "..") for part in value.split("/")):
        raise MigrationError("PATH_INVALID", "path must not be absolute or escape its root", value)
    for part in value.split("/"):
        if (RESERVED.fullmatch(part) or part.endswith((".", " "))
                or re.search(r'[:<>"|?*\x00-\x1f]', part)):
            raise MigrationError("PATH_INVALID", "path contains an unsafe filesystem component", value)
    return value


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _root(workspace: str | Path) -> Path:
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        raise MigrationError("WORKSPACE_INVALID", "workspace must be an existing directory", str(root))
    return root


def _path(root: Path, relative: str, *, allow_missing: bool = True) -> Path:
    relative = _safe_rel(relative)
    candidate = root.joinpath(*relative.split("/"))
    if not _within(candidate, root):
        raise MigrationError("PATH_INVALID", "path escapes the workspace", relative)
    current = root
    for part in relative.split("/"):
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            if not allow_missing:
                raise MigrationError("PATH_MISSING", "path does not exist", relative)
            continue
        except OSError as exc:
            raise MigrationError("PATH_UNREADABLE", exc.strerror or "path cannot be checked", relative) from exc
        if stat.S_ISLNK(mode):
            raise MigrationError("SYMLINK_FORBIDDEN", "migration paths must not traverse symbolic links", relative)
    try:
        candidate.resolve(strict=False).relative_to(root)
    except (OSError, ValueError) as exc:
        raise MigrationError("PATH_INVALID", "path resolves outside the workspace", relative) from exc
    return candidate


def _read_bytes(root: Path, relative: str) -> bytes:
    path = _path(root, relative, allow_missing=False)
    try:
        mode = path.lstat().st_mode
        if not stat.S_ISREG(mode):
            raise MigrationError("PATH_NOT_FILE", "path must be a regular file", relative)
        return path.read_bytes()
    except MigrationError:
        raise
    except OSError as exc:
        raise MigrationError("PATH_UNREADABLE", exc.strerror or "file cannot be read", relative) from exc


def _write_bytes(root: Path, relative: str, content: bytes, *, replace: bool = False,
                 expected_before: str | None = None) -> None:
    path = _path(root, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not replace and os.path.lexists(path):
        raise MigrationError("TARGET_CONFLICT", "target already exists", relative)
    fd, temporary = tempfile.mkstemp(prefix=".agent-asset-migration-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if expected_before is not None and digest(_read_bytes(root, relative)) != expected_before:
            raise MigrationError("BASELINE_CONFLICT", "file changed before atomic replacement", relative)
        _path(root, relative)
        if replace:
            os.replace(temporary, path)
        else:
            try:
                os.link(temporary, path)
            except FileExistsError as exc:
                raise MigrationError("TARGET_CONFLICT", "target appeared during write", relative) from exc
            os.unlink(temporary)
        temporary = ""
    except OSError as exc:
        raise MigrationError("WRITE_FAILED", exc.strerror or str(exc), relative) from exc
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _plan(root: Path, value: object) -> dict:
    if not isinstance(value, dict) or type(value.get("schemaVersion")) is not int or value["schemaVersion"] != SCHEMA_VERSION:
        raise MigrationError("PLAN_INVALID", "plan schemaVersion must be 1")
    workspace = value.get("workspace")
    if not isinstance(workspace, str) or Path(workspace).expanduser().resolve() != root:
        raise MigrationError("WORKSPACE_MISMATCH", "plan workspace does not match command target")
    migration_root = "." if value.get("migrationRoot") == "." else _safe_rel(value.get("migrationRoot"))
    source_root = _safe_rel(value.get("sourceRoot"))
    target_root = _safe_rel(value.get("targetRoot"))
    if value.get("targetPolicy") != "fail":
        raise MigrationError("PLAN_INVALID", "targetPolicy must be 'fail'")
    migration_abs = root.joinpath(*migration_root.split("/"))
    source_abs = root.joinpath(*source_root.split("/"))
    target_abs = root.joinpath(*target_root.split("/"))
    recovery_directory = JOURNAL_ROOT.expanduser().resolve()
    if _within(recovery_directory, root):
        raise MigrationError("JOURNAL_SCOPE", "external recovery journal must be outside the workspace",
                            str(recovery_directory))
    if not _within(source_abs, migration_abs) or not _within(target_abs, migration_abs):
        raise MigrationError("PLAN_SCOPE", "sourceRoot and targetRoot must be inside migrationRoot")
    if _within(source_abs, target_abs) or _within(target_abs, source_abs):
        raise MigrationError("PLAN_SCOPE", "sourceRoot and targetRoot must not overlap")
    files_value = value.get("files")
    links_value = value.get("linkUpdates", [])
    if not isinstance(files_value, list) or not files_value or not isinstance(links_value, list):
        raise MigrationError("PLAN_INVALID", "plan requires a nonempty files list and optional linkUpdates list")
    if len(files_value) > MAX_FILES or len(links_value) > MAX_LINK_SCAN_FILES:
        raise MigrationError("PLAN_LIMIT", "plan exceeds file or link update limits")
    files = []
    seen_sources = set()
    seen_targets = set()
    for entry in files_value:
        if not isinstance(entry, dict):
            raise MigrationError("PLAN_INVALID", "each files entry must be an object")
        if entry.get("action") != "move":
            raise MigrationError("PLAN_INVALID", "each files entry action must be 'move'")
        source = _safe_rel(entry.get("source"))
        target = _safe_rel(entry.get("target"))
        if Path(target).name.casefold() == ".env" or Path(target).name.casefold().startswith(".env."):
            raise MigrationError("SECRET_FILE", "dotenv files are not migration targets", target)
        checksum = entry.get("sha256")
        if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
            raise MigrationError("PLAN_INVALID", "files.sha256 must be lowercase SHA-256", source)
        if not _within(root / source, source_abs) or not _within(root / target, target_abs):
            raise MigrationError("PLAN_SCOPE", "file source/target is outside its declared root", source)
        source_folded = source.casefold()
        target_folded = target.casefold()
        if source_folded in seen_sources or target_folded in seen_targets:
            raise MigrationError("PLAN_DUPLICATE", "source or target path is duplicated", source)
        for previous in seen_sources:
            if source_folded.startswith(previous + "/") or previous.startswith(source_folded + "/"):
                raise MigrationError("PLAN_PATH_OVERLAP", "planned source paths must not overlap", source)
        for previous in seen_targets:
            if target_folded.startswith(previous + "/") or previous.startswith(target_folded + "/"):
                raise MigrationError("PLAN_PATH_OVERLAP", "planned target paths must not overlap", target)
        seen_sources.add(source_folded)
        seen_targets.add(target_folded)
        files.append({"source": source, "target": target, "action": "move", "sha256": checksum})
    files.sort(key=lambda entry: entry["source"])
    links = []
    seen_links = set()
    source_names = {entry["source"].casefold(): entry["source"] for entry in files}
    for entry in links_value:
        if not isinstance(entry, dict):
            raise MigrationError("PLAN_INVALID", "each linkUpdates entry must be an object")
        path = _safe_rel(entry.get("path"))
        path = source_names.get(path.casefold(), path)
        if not _within(root / path, migration_abs):
            raise MigrationError("PLAN_SCOPE", "link update path is outside migrationRoot", path)
        if _within(root / path, target_abs):
            raise MigrationError("PLAN_SCOPE", "link updates must refer to existing source or external files", path)
        if Path(path).name.casefold() == ".env" or Path(path).name.casefold().startswith(".env."):
            raise MigrationError("SECRET_FILE", "dotenv files are not eligible for migration", path)
        checksum = entry.get("baselineSha256")
        content = entry.get("content")
        if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
            raise MigrationError("PLAN_INVALID", "link baselineSha256 must be lowercase SHA-256", path)
        if not isinstance(content, str):
            raise MigrationError("PLAN_INVALID", "link update content must be complete UTF-8 text", path)
        if path.casefold() in seen_links:
            raise MigrationError("PLAN_DUPLICATE", "link update path is duplicated", path)
        seen_links.add(path.casefold())
        links.append({"path": path, "baselineSha256": checksum, "content": content})
    links.sort(key=lambda entry: entry["path"])
    canonical = {"schemaVersion": SCHEMA_VERSION, "workspace": str(root),
                 "migrationRoot": migration_root, "sourceRoot": source_root,
                 "targetRoot": target_root, "targetPolicy": "fail",
                 "recoveryDirectory": str(recovery_directory),
                 "files": files, "linkUpdates": links}
    return canonical


def _inventory(root: Path, source_root: str) -> tuple[set[str], list[dict]]:
    start = _path(root, source_root, allow_missing=False)
    if not start.is_dir():
        raise MigrationError("SOURCE_ROOT_INVALID", "sourceRoot must be a directory", source_root)
    found = set()
    errors = []
    stack = [(start, 0)]
    visited = 0
    while stack:
        directory, depth = stack.pop()
        relative_dir = directory.relative_to(root).as_posix()
        if depth > MAX_DEPTH:
            errors.append(_diagnostic("SOURCE_DEPTH_LIMIT", "source depth limit reached", relative_dir))
            continue
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda item: (item.name.casefold(), item.name), reverse=True)
        except OSError as exc:
            errors.append(_diagnostic("SOURCE_ENUMERATION_FAILED", exc.strerror or str(exc), relative_dir))
            continue
        for item in entries:
            visited += 1
            relative = (relative_dir + "/" + item.name).replace("\\", "/")
            if visited > MAX_FILES:
                errors.append(_diagnostic("SOURCE_ENTRY_LIMIT", "source entry limit reached", relative))
                return found, errors
            try:
                mode = item.stat(follow_symlinks=False).st_mode
                _safe_rel(relative)
                if stat.S_ISLNK(mode):
                    errors.append(_diagnostic("SOURCE_SYMLINK", "source contains a symbolic link", relative))
                elif stat.S_ISDIR(mode):
                    stack.append((Path(item.path), depth + 1))
                elif stat.S_ISREG(mode):
                    if item.name.casefold() == ".env" or item.name.casefold().startswith(".env."):
                        errors.append(_diagnostic("SECRET_FILE", "dotenv files are not eligible for migration", relative))
                    else:
                        found.add(relative)
                else:
                    errors.append(_diagnostic("SOURCE_SPECIAL_FILE", "source contains a non-regular file", relative))
            except MigrationError as exc:
                errors.append(_diagnostic(exc.code, exc.message, relative))
            except OSError as exc:
                errors.append(_diagnostic("SOURCE_METADATA_FAILED", exc.strerror or str(exc), relative))
    return found, errors


def _confirmation(plan: dict) -> str:
    payload = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return digest(payload)


def _markdown_targets(text: str):
    matches = [*MARKDOWN_LINK.finditer(text), *REFERENCE_LINK.finditer(text),
               *RST_LINK.finditer(text), *RST_TARGET.finditer(text), *HTML_LINK.finditer(text)]
    for match in matches:
        target = match.group(1).strip("<>")
        if (not target or target.startswith(("#", "/", "\\", "~"))
                or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", target)):
            continue
        target = unquote(target.split("#", 1)[0].split("?", 1)[0]).replace("\\", "/")
        if target:
            yield target


def _link_scan_files(root: Path, migration_root: str) -> tuple[list[str], list[dict]]:
    start = root if migration_root == "." else root.joinpath(*migration_root.split("/"))
    found = []
    errors = []
    stack = [(start, 0)]
    entries_seen = 0
    while stack:
        directory, depth = stack.pop()
        relative_dir = directory.relative_to(root).as_posix() if directory != root else "."
        if depth > MAX_DEPTH:
            errors.append(_diagnostic("LINK_SCAN_DEPTH_LIMIT", "link scan depth limit reached", relative_dir))
            continue
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda item: (item.name.casefold(), item.name), reverse=True)
        except OSError as exc:
            errors.append(_diagnostic("LINK_SCAN_FAILED", exc.strerror or str(exc), relative_dir))
            continue
        for item in entries:
            entries_seen += 1
            if entries_seen > MAX_FILES:
                errors.append(_diagnostic("LINK_SCAN_ENTRY_LIMIT", "link scan entry limit reached", relative_dir))
                return found, errors
            relative = (relative_dir + "/" if relative_dir != "." else "") + item.name
            try:
                mode = item.stat(follow_symlinks=False).st_mode
                if stat.S_ISLNK(mode):
                    errors.append(_diagnostic("LINK_SCAN_SYMLINK",
                                              "symbolic link is not traversed; review its link scope", relative))
                    continue
                if stat.S_ISDIR(mode):
                    if item.name.casefold() not in PRUNED_LINK_DIRS:
                        stack.append((Path(item.path), depth + 1))
                elif item.name.casefold() == ".env" or item.name.casefold().startswith(".env."):
                    continue
                elif stat.S_ISREG(mode) and Path(item.name).suffix.casefold() in (
                        ".md", ".markdown", ".mdx", ".rst", ".txt"):
                    found.append(relative)
                    if len(found) > MAX_LINK_SCAN_FILES:
                        errors.append(_diagnostic("LINK_SCAN_FILE_LIMIT", "link scan file limit reached", relative))
                        return found[:MAX_LINK_SCAN_FILES], errors
            except OSError as exc:
                errors.append(_diagnostic("LINK_SCAN_METADATA_FAILED", exc.strerror or str(exc), relative))
    return sorted(found), errors


def _check_link_coverage(root: Path, plan: dict) -> list[dict]:
    paths, errors = _link_scan_files(root, plan["migrationRoot"])
    if errors:
        return errors
    source_to_target = {item["source"]: item["target"] for item in plan["files"]}
    updates = {item["path"]: item for item in plan["linkUpdates"]}
    source_root = plan["sourceRoot"]
    diagnostics = []
    for relative in paths:
        try:
            raw = _read_bytes(root, relative)
            if len(raw) > MAX_TEXT_BYTES:
                diagnostics.append(_diagnostic("LINK_SCAN_FILE_TOO_LARGE",
                                               "text file exceeds link scan limit", relative))
                continue
            text = raw.decode("utf-8")
        except (MigrationError, UnicodeError) as exc:
            diagnostics.append(_diagnostic("LINK_SCAN_UNREADABLE", str(exc), relative))
            continue
        new_document = source_to_target.get(relative, relative)
        needs_update = False
        for target in _markdown_targets(text):
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(relative), target))
            if resolved == ".." or resolved.startswith("../"):
                continue
            mapped = source_to_target.get(resolved, resolved)
            if resolved == source_root:
                mapped = plan["targetRoot"]
            if resolved.startswith(source_root + "/") and resolved not in source_to_target:
                diagnostics.append(_diagnostic("LINK_TARGET_NOT_PLANNED",
                                               "relative link points to an asset absent from the file map", relative))
                continue
            after_resolved = posixpath.normpath(posixpath.join(posixpath.dirname(new_document), target))
            if after_resolved != mapped:
                needs_update = True
        if needs_update and relative not in updates:
            diagnostics.append(_diagnostic("LINK_UPDATE_OMITTED",
                                           "relative link becomes invalid after migration; list a complete link update",
                                           relative))
        update = updates.get(relative)
        if update:
            for target in _markdown_targets(update["content"]):
                resolved = posixpath.normpath(posixpath.join(posixpath.dirname(new_document), target))
                if resolved == source_root or resolved.startswith(source_root + "/"):
                    diagnostics.append(_diagnostic("LINK_UPDATE_STILL_POINTS_TO_SOURCE",
                                                   "planned link content still points into sourceRoot",
                                                   relative))
                    break
    return diagnostics


def _verify_targets(root: Path, journal: dict) -> list[dict]:
    found, errors = _inventory(root, journal["plan"]["targetRoot"])
    expected = journal["targetHashes"]
    if found != set(expected):
        errors.append(_diagnostic("TARGET_INVENTORY_MISMATCH",
                                  "target file count or inventory differs from the plan",
                                  journal["plan"]["targetRoot"]))
    for relative, checksum in expected.items():
        try:
            if digest(_read_bytes(root, relative)) != checksum:
                errors.append(_diagnostic("TARGET_HASH_MISMATCH", "target hash differs from the plan", relative))
        except MigrationError as exc:
            errors.append(_diagnostic(exc.code, exc.message, relative))
    return errors


def _output_hashes(root: Path, plan: dict) -> tuple[dict[str, str], dict[str, str]]:
    source_hashes = {}
    target_hashes = {}
    link_sources = {entry["path"]: entry for entry in plan["linkUpdates"]}
    by_source = {entry["source"]: entry for entry in plan["files"]}
    for entry in plan["files"]:
        content = _read_bytes(root, entry["source"])
        actual = digest(content)
        if actual != entry["sha256"]:
            raise MigrationError("SOURCE_BASELINE_CONFLICT", "source file differs from plan baseline", entry["source"])
        source_hashes[entry["source"]] = actual
        link = link_sources.get(entry["source"])
        output = link["content"].encode("utf-8") if link else content
        target_hashes[entry["target"]] = digest(output)
    for link in plan["linkUpdates"]:
        path = link["path"]
        if path in by_source:
            actual = source_hashes[path]
        else:
            data = _read_bytes(root, path)
            actual = digest(data)
            if actual != link["baselineSha256"]:
                raise MigrationError("LINK_BASELINE_CONFLICT", "link file differs from plan baseline", path)
        if actual != link["baselineSha256"]:
            raise MigrationError("LINK_BASELINE_CONFLICT", "link file differs from plan baseline", path)
    return source_hashes, target_hashes


def check_plan(workspace: str | Path, value: object) -> dict:
    try:
        root = _root(workspace)
        plan = _plan(root, value)
        inventory, inventory_errors = _inventory(root, plan["sourceRoot"])
        listed = {entry["source"] for entry in plan["files"]}
        omitted = sorted(inventory - listed)
        extra = sorted(listed - inventory)
        diagnostics = list(inventory_errors)
        diagnostics.extend(_diagnostic("ASSET_OMITTED", "source file is not listed in the migration plan", path)
                           for path in omitted)
        diagnostics.extend(_diagnostic("SOURCE_NOT_FOUND", "planned source file is not in sourceRoot", path)
                           for path in extra)
        if diagnostics:
            return _result("conflict", diagnostics, confirmation=_confirmation(plan), plan=plan)
        link_errors = _check_link_coverage(root, plan)
        if link_errors:
            return _result("conflict", link_errors, confirmation=_confirmation(plan), plan=plan)
        _output_hashes(root, plan)
        target_root = _path(root, plan["targetRoot"])
        if os.path.lexists(target_root):
            if target_root.is_symlink() or not target_root.is_dir():
                raise MigrationError("TARGET_CONFLICT", "targetRoot already exists and is not a directory",
                                     plan["targetRoot"])
            def target_scan_error(exc):
                raise MigrationError("TARGET_ENUMERATION_FAILED", exc.strerror or str(exc),
                                     getattr(exc, "filename", plan["targetRoot"]))

            for current, dirs, names in os.walk(target_root, followlinks=False, onerror=target_scan_error):
                dirs[:] = sorted(dirs)
                for name in dirs:
                    directory = Path(current) / name
                    if directory.is_symlink():
                        raise MigrationError("TARGET_CONFLICT",
                                             "target contains a symbolic directory", directory.relative_to(root).as_posix())
                    raise MigrationError("TARGET_CONFLICT", "target contains an unplanned directory",
                                         directory.relative_to(root).as_posix())
                for name in names:
                    path = Path(current) / name
                    relative = path.relative_to(root).as_posix()
                    if path.is_symlink() or not path.is_file():
                        raise MigrationError("TARGET_CONFLICT", "target contains a symbolic or special file", relative)
                    raise MigrationError("TARGET_CONFLICT", "target contains an unplanned file", relative)
        for entry in plan["files"]:
            target = _path(root, entry["target"])
            if os.path.lexists(target):
                raise MigrationError("TARGET_CONFLICT", "target file already exists", entry["target"])
        link_sources = {entry["source"] for entry in plan["files"]}
        for link in plan["linkUpdates"]:
            if link["path"] not in link_sources:
                _read_bytes(root, link["path"])
        summary = {"fileCount": len(plan["files"]), "linkUpdateCount": len(plan["linkUpdates"]),
                   "sourceRoot": plan["sourceRoot"], "targetRoot": plan["targetRoot"],
                   "targetRootExisted": os.path.lexists(_path(root, plan["targetRoot"])),
                   "recoveryDirectory": plan["recoveryDirectory"],
                   "files": [{"source": item["source"], "target": item["target"],
                              "action": item["action"], "sha256": item["sha256"]}
                             for item in plan["files"]],
                   "linkUpdates": [{"path": item["path"], "beforeSha256": item["baselineSha256"],
                                    "afterSha256": digest(item["content"].encode("utf-8"))}
                                   for item in plan["linkUpdates"]]}
        return _result("ok", confirmation=_confirmation(plan), plan=plan, summary=summary)
    except MigrationError as exc:
        status = "conflict" if exc.code.endswith("CONFLICT") or exc.code in ("PATH_MISSING",) else "error"
        return _result(status, [_diagnostic(exc.code, exc.message, exc.path)])
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        return _result("error", [_diagnostic("INSPECT_FAILED", str(exc))])


def diff_plan(workspace: str | Path, value: object) -> dict:
    checked = check_plan(workspace, value)
    if checked["status"] != "ok":
        return checked
    root = _root(workspace)
    link_diffs = []
    for update in checked["plan"]["linkUpdates"]:
        try:
            before = _read_bytes(root, update["path"]).decode("utf-8")
        except (MigrationError, UnicodeError) as exc:
            return _result("error", [_diagnostic("LINK_UPDATE_UNREADABLE", str(exc), update["path"])])
        after = update["content"]
        diff = "".join(difflib.unified_diff(
            before.splitlines(keepends=True), after.splitlines(keepends=True),
            fromfile=update["path"] + " (before)", tofile=update["path"] + " (after)"))
        link_diffs.append({"path": update["path"], "beforeSha256": update["baselineSha256"],
                           "afterSha256": digest(after.encode("utf-8")), "diff": diff})
    moves = [{"source": item["source"], "target": item["target"], "action": item["action"], "sha256": item["sha256"]}
             for item in checked["plan"]["files"]]
    return {**checked, "operation": "diff", "moves": moves, "linkDiffs": link_diffs}


def _journal_path(identifier: str) -> Path:
    try:
        if str(uuid.UUID(identifier)) != identifier:
            raise ValueError
    except (ValueError, TypeError, AttributeError) as exc:
        raise MigrationError("MIGRATION_ID_INVALID", "migration id must be a UUID", identifier) from exc
    return JOURNAL_ROOT / (identifier + ".json")


def _check_journal_location(workspace: Path | None = None) -> None:
    location = JOURNAL_ROOT.expanduser().absolute()
    if workspace is not None and _within(location.resolve(), workspace):
        raise MigrationError("JOURNAL_SCOPE", "recovery journal must be outside the workspace", str(location))
    for current in (location, *location.parents):
        if current.is_symlink():
            raise MigrationError("JOURNAL_INVALID", "recovery directory must not traverse a symbolic link", str(current))


def _json_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key: " + key)
        result[key] = value
    return result


def _save_journal(journal: dict) -> Path:
    _check_journal_location(Path(journal["workspace"]))
    JOURNAL_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = _journal_path(journal["id"])
    if path.is_symlink():
        raise MigrationError("JOURNAL_INVALID", "recovery journal must not be symbolic", str(path))
    expected_current = journal.get("journalSha256")
    if path.exists():
        try:
            current = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_json_pairs)
        except (OSError, UnicodeError, ValueError) as exc:
            raise MigrationError("JOURNAL_CONFLICT", "existing recovery journal changed or is unreadable",
                                 str(path)) from exc
        if not isinstance(current, dict) or current.get("journalSha256") != expected_current:
            raise MigrationError("JOURNAL_CONFLICT", "recovery journal changed concurrently", str(path))
    elif expected_current is not None:
        raise MigrationError("JOURNAL_CONFLICT", "recovery journal was removed concurrently", str(path))
    journal["journalSha256"] = _journal_digest(journal)
    payload = json.dumps(journal, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=".asset-migration-", suffix=".tmp", dir=str(JOURNAL_ROOT))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path


def _journal_digest(journal: dict) -> str:
    content = {key: value for key, value in journal.items() if key != "journalSha256"}
    return digest(json.dumps(content, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8"))


def _load_journal(workspace: Path, identifier: str) -> dict:
    path = _journal_path(identifier)
    try:
        _check_journal_location(workspace)
        if path.is_symlink():
            raise MigrationError("JOURNAL_INVALID", "recovery journal must not be symbolic", str(path))
        if path.stat().st_size > MAX_JOURNAL_BYTES:
            raise MigrationError("JOURNAL_INVALID", "recovery journal exceeds the size limit", str(path))
        journal = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_json_pairs)
    except (OSError, UnicodeError, ValueError) as exc:
        raise MigrationError("JOURNAL_UNAVAILABLE", "external recovery journal is unavailable", str(path)) from exc
    if not isinstance(journal, dict) or type(journal.get("schemaVersion")) is not int or journal["schemaVersion"] != SCHEMA_VERSION:
        raise MigrationError("JOURNAL_INVALID", "recovery journal schema is invalid", str(path))
    if journal.get("id") != identifier or journal.get("workspace") != str(workspace):
        raise MigrationError("JOURNAL_WORKSPACE_MISMATCH", "recovery journal belongs to another workspace", str(path))
    if journal.get("journalSha256") != _journal_digest(journal):
        raise MigrationError("JOURNAL_INVALID", "recovery journal integrity check failed", str(path))
    try:
        plan = _plan(workspace, journal["plan"])
        if journal.get("confirmation") != _confirmation(plan):
            raise ValueError("journal plan digest does not match confirmation")
        if journal.get("phase") not in ("prepared", "verified", "completed", "rolled-back",
                                       "partial", "partial-restored", "restored"):
            raise ValueError("unsupported journal phase")
        expected_sources = {item["source"]: item["sha256"] for item in plan["files"]}
        if journal["sourceHashes"] != expected_sources or set(journal["sourceBackup"]) != set(expected_sources):
            raise ValueError("source backup inventory differs from the plan")
        backup_size = 0
        for source, encoded in journal["sourceBackup"].items():
            data = base64.b64decode(encoded, validate=True)
            backup_size += len(data)
            if backup_size > MAX_BACKUP_BYTES:
                raise ValueError("source backup exceeds the supported size")
            if digest(data) != expected_sources[source]:
                raise ValueError("backup SHA-256 mismatch: " + source)
        updates = {item["path"]: item for item in plan["linkUpdates"]}
        expected_targets = {
            item["target"]: digest(updates[item["source"]]["content"].encode("utf-8"))
            if item["source"] in updates else item["sha256"] for item in plan["files"]}
        if journal["targetHashes"] != expected_targets:
            raise ValueError("target hashes differ from the plan")
        written = journal["writtenTargets"]
        if (not isinstance(written, list) or not all(isinstance(item, str) for item in written)
                or len(written) != len(set(written)) or set(written) != set(expected_targets)):
            raise ValueError("writtenTargets is invalid")
        links = journal["linkBackups"]
        if not isinstance(links, list) or len(links) != len(updates):
            raise ValueError("link backup inventory differs from the plan")
        seen = set()
        for link in links:
            update = updates[link["path"]]
            if link["path"] in seen:
                raise ValueError("duplicate link backup")
            seen.add(link["path"])
            before = base64.b64decode(link["beforeBase64"], validate=True)
            backup_size += len(before)
            if backup_size > MAX_BACKUP_BYTES:
                raise ValueError("recovery backup exceeds the supported size")
            if (link["beforeSha256"] != update["baselineSha256"]
                    or digest(before) != update["baselineSha256"]
                    or link["afterSha256"] != digest(update["content"].encode("utf-8"))
                    or link["sourceFile"] != (link["path"] in expected_sources)):
                raise ValueError("link backup hash or metadata mismatch")
        directories = journal["createdDirectories"]
        if not isinstance(directories, list):
            raise ValueError("createdDirectories is invalid")
        if type(journal.get("targetRootExisted")) is not bool:
            raise ValueError("targetRootExisted must be boolean")
        target_root = plan["targetRoot"]
        if (not journal["targetRootExisted"] and target_root not in directories
                or journal["targetRootExisted"] and target_root in directories):
            raise ValueError("targetRootExisted disagrees with createdDirectories")
        if len(directories) != len(set(directories)):
            raise ValueError("duplicate created directory")
        for relative in directories:
            _safe_rel(relative)
            if relative != target_root and not relative.startswith(target_root + "/"):
                raise ValueError("created directory is outside targetRoot")
            if not any(target.startswith(relative + "/") for target in expected_targets):
                raise ValueError("unrelated directory in recovery journal")
    except (MigrationError, KeyError, TypeError, AttributeError, ValueError) as exc:
        raise MigrationError("JOURNAL_INVALID", "invalid recovery data: " + str(exc), str(path)) from exc
    return journal


def _cleanup_targets(root: Path, targets: dict[str, str]) -> list[dict]:
    errors = []
    for relative, expected in sorted(targets.items()):
        try:
            path = _path(root, relative)
            if not os.path.lexists(path):
                continue
            if not path.is_file() or digest(_read_bytes(root, relative)) != expected:
                errors.append(_diagnostic("RECOVERY_TARGET_CONFLICT",
                                          "target changed after migration; preserving it", relative))
                continue
            path.unlink()
        except MigrationError as exc:
            errors.append(_diagnostic("RECOVERY_TARGET_CONFLICT", exc.message, relative))
        except OSError as exc:
            errors.append(_diagnostic("RECOVERY_TARGET_REMOVE_FAILED", str(exc), relative))
    return errors


def _remove_created_directories(root: Path, directories: list[str]) -> list[dict]:
    errors = []
    for relative in sorted(directories, key=lambda item: (item.count("/"), item), reverse=True):
        try:
            path = _path(root, relative)
            path.rmdir()
        except (MigrationError, OSError) as exc:
            if os.path.lexists(root / relative):
                errors.append(_diagnostic("RESTORE_DIRECTORY_CONFLICT",
                                          "created directory is not empty or cannot be removed; preserving it",
                                          relative))
    return errors


def _restore_link_updates(root: Path, journal: dict) -> list[dict]:
    errors = []
    for entry in journal["linkBackups"]:
        if entry["sourceFile"]:
            continue
        path = entry["path"]
        try:
            current = _read_bytes(root, path)
            if digest(current) == entry["beforeSha256"]:
                continue
            if digest(current) != entry["afterSha256"]:
                errors.append(_diagnostic("RESTORE_LINK_CONFLICT",
                                          "link file changed after migration; preserving it", path))
                continue
            _write_bytes(root, path, base64.b64decode(entry["beforeBase64"], validate=True),
                         replace=True, expected_before=entry["afterSha256"])
        except MigrationError as exc:
            errors.append(_diagnostic("RESTORE_LINK_FAILED", exc.message, path))
        except (OSError, ValueError) as exc:
            errors.append(_diagnostic("RESTORE_LINK_FAILED", str(exc), path))
    return errors


def _restore_sources(root: Path, journal: dict) -> list[dict]:
    errors = []
    for source, encoded in sorted(journal["sourceBackup"].items()):
        try:
            path = _path(root, source)
            if os.path.lexists(path):
                if digest(_read_bytes(root, source)) != journal["sourceHashes"][source]:
                    errors.append(_diagnostic("RESTORE_SOURCE_CONFLICT",
                                              "source was recreated or changed; preserving it", source))
                continue
            _write_bytes(root, source, base64.b64decode(encoded, validate=True))
        except (MigrationError, OSError, ValueError) as exc:
            errors.append(_diagnostic("RESTORE_SOURCE_FAILED", str(exc), source))
    return errors


def _rollback_apply(root: Path, journal: dict) -> list[dict]:
    errors = _restore_sources(root, journal)
    errors.extend(_restore_link_updates(root, journal))
    if not errors:
        owned = {relative: journal["targetHashes"][relative] for relative in journal["writtenTargets"]}
        errors.extend(_cleanup_targets(root, owned))
        errors.extend(_remove_created_directories(root, journal["createdDirectories"]))
    return errors


def apply_plan(workspace: str | Path, value: object, confirmation: str | None) -> dict:
    try:
        root = _root(workspace)
        checked = check_plan(root, value)
        if checked["status"] != "ok":
            return checked
        plan = checked["plan"]
        expected_confirmation = checked["confirmation"]
        if confirmation is None:
            return _result("needs-confirmation", confirmation=expected_confirmation, summary=checked["summary"])
        if confirmation != expected_confirmation:
            return _result("error", [_diagnostic("CONFIRMATION_MISMATCH",
                                                 "confirmation must match the displayed plan digest")])
        source_hashes, target_hashes = _output_hashes(root, plan)
        backup = {}
        backup_size = 0
        for source in source_hashes:
            content = _read_bytes(root, source)
            if digest(content) != source_hashes[source]:
                raise MigrationError("SOURCE_BASELINE_CONFLICT", "source changed before backup", source)
            backup_size += len(content)
            backup[source] = base64.b64encode(content).decode("ascii")
        if backup_size > MAX_BACKUP_BYTES:
            raise MigrationError("BACKUP_LIMIT", "external recovery backup size limit exceeded")
        link_backups = []
        source_paths = set(source_hashes)
        for link in plan["linkUpdates"]:
            before = _read_bytes(root, link["path"])
            if digest(before) != link["baselineSha256"]:
                raise MigrationError("LINK_BASELINE_CONFLICT", "link file differs from plan baseline", link["path"])
            link_backups.append({"path": link["path"], "beforeBase64": base64.b64encode(before).decode("ascii"),
                                 "beforeSha256": digest(before),
                                 "afterSha256": digest(link["content"].encode("utf-8")),
                                 "sourceFile": link["path"] in source_paths})
            backup_size += len(before)
        if backup_size > MAX_BACKUP_BYTES:
            raise MigrationError("BACKUP_LIMIT", "external recovery backup size limit exceeded")
        created_directories = set()
        target_root_existed = os.path.lexists(_path(root, plan["targetRoot"]))
        for entry in plan["files"]:
            parent = _path(root, entry["target"]).parent
            while parent != root and not os.path.lexists(parent):
                created_directories.add(parent.relative_to(root).as_posix())
                parent = parent.parent
        journal = {"schemaVersion": SCHEMA_VERSION, "id": str(uuid.uuid4()), "workspace": str(root),
                   "confirmation": expected_confirmation, "plan": plan, "phase": "prepared",
                   "sourceBackup": backup, "sourceHashes": source_hashes,
                   "targetHashes": target_hashes, "linkBackups": link_backups,
                   "writtenTargets": sorted(target_hashes),
                   "targetRootExisted": target_root_existed,
                   "createdDirectories": sorted(created_directories)}
        journal_path = _save_journal(journal)
        _load_journal(root, journal["id"])
        try:
            by_source = {item["source"]: item for item in plan["files"]}
            updated_sources = {item["path"]: item for item in plan["linkUpdates"]
                               if item["path"] in by_source}
            for entry in plan["files"]:
                original = base64.b64decode(backup[entry["source"]], validate=True)
                update = updated_sources.get(entry["source"])
                content = update["content"].encode("utf-8") if update else original
                if digest(original) != entry["sha256"]:
                    raise MigrationError("SOURCE_BASELINE_CONFLICT",
                                         "source changed during staging", entry["source"])
                _write_bytes(root, entry["target"], content)
            for link in plan["linkUpdates"]:
                if link["path"] in source_paths:
                    continue
                if digest(_read_bytes(root, link["path"])) != link["baselineSha256"]:
                    raise MigrationError("LINK_BASELINE_CONFLICT",
                                         "link file changed before update", link["path"])
                _write_bytes(root, link["path"], link["content"].encode("utf-8"), replace=True,
                             expected_before=link["baselineSha256"])
            verification_errors = _verify_targets(root, journal)
            if verification_errors:
                problem = verification_errors[0]
                raise MigrationError(problem["code"], problem["message"], problem["path"])
            for link in plan["linkUpdates"]:
                if link["path"] not in source_paths:
                    if digest(_read_bytes(root, link["path"])) != digest(link["content"].encode("utf-8")):
                        raise MigrationError("LINK_VERIFY_FAILED", "link update did not verify", link["path"])
            journal["phase"] = "verified"
            _save_journal(journal)
            _load_journal(root, journal["id"])
            for entry in plan["files"]:
                if digest(_read_bytes(root, entry["source"])) != source_hashes[entry["source"]]:
                    raise MigrationError("SOURCE_BASELINE_CONFLICT",
                                         "source changed before removal", entry["source"])
            for entry in plan["files"]:
                if digest(_read_bytes(root, entry["source"])) != source_hashes[entry["source"]]:
                    raise MigrationError("SOURCE_BASELINE_CONFLICT",
                                         "source changed immediately before removal", entry["source"])
                _path(root, entry["source"], allow_missing=False).unlink()
            journal["phase"] = "completed"
            _save_journal(journal)
        except (MigrationError, OSError, UnicodeError, ValueError) as exc:
            rollback_errors = _rollback_apply(root, journal)
            journal["phase"] = "rolled-back" if not rollback_errors else "partial"
            journal["failure"] = {"code": getattr(exc, "code", "APPLY_FAILED"),
                                  "message": str(exc), "path": getattr(exc, "path", None)}
            journal["recoveryErrors"] = rollback_errors
            code = getattr(exc, "code", "APPLY_FAILED")
            diagnostics = [_diagnostic(code, str(exc), getattr(exc, "path", None))]
            diagnostics.extend(rollback_errors)
            try:
                _save_journal(journal)
            except (MigrationError, OSError) as journal_exc:
                diagnostics.append(_diagnostic("RECOVERY_JOURNAL_WRITE_FAILED", str(journal_exc), str(journal_path)))
            return _result("error", diagnostics, migrationId=journal["id"], journal=str(journal_path),
                           recovery="rolled-back" if not rollback_errors else "partial")
        return _result("completed", migrationId=journal["id"], journal=str(journal_path),
                       confirmation=expected_confirmation, summary=checked["summary"])
    except MigrationError as exc:
        return _result("conflict" if exc.code.endswith("CONFLICT") else "error",
                       [_diagnostic(exc.code, exc.message, exc.path)])
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        return _result("error", [_diagnostic("APPLY_FAILED", str(exc))])


def verify_migration(workspace: str | Path, identifier: str) -> dict:
    try:
        root = _root(workspace)
        journal = _load_journal(root, identifier)
        if journal["phase"] != "completed":
            return _result("conflict", [_diagnostic("MIGRATION_NOT_COMPLETE",
                                                    "migration is not in completed state")],
                           migrationId=identifier, phase=journal["phase"])
        errors = _verify_targets(root, journal)
        for source in journal["sourceHashes"]:
            if os.path.lexists(_path(root, source)):
                errors.append(_diagnostic("SOURCE_REMAINS", "source remains after completed migration", source))
        for link in journal["linkBackups"]:
            if link["sourceFile"]:
                continue
            try:
                if digest(_read_bytes(root, link["path"])) != link["afterSha256"]:
                    errors.append(_diagnostic("LINK_HASH_MISMATCH", "link update differs from journal", link["path"]))
            except MigrationError as exc:
                errors.append(_diagnostic(exc.code, exc.message, exc.path))
        return _result("verified" if not errors else "conflict", errors, migrationId=identifier,
                       phase=journal["phase"])
    except MigrationError as exc:
        return _result("error", [_diagnostic(exc.code, exc.message, exc.path)])
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
        return _result("error", [_diagnostic("VERIFY_FAILED", str(exc))])


def restore_migration(workspace: str | Path, identifier: str) -> dict:
    try:
        root = _root(workspace)
        journal = _load_journal(root, identifier)
        if journal["phase"] == "restored":
            return _result("restored", migrationId=identifier, phase="restored", idempotent=True)
        if journal["phase"] not in ("completed", "partial", "verified", "prepared", "partial-restored"):
            raise MigrationError("MIGRATION_NOT_RESTORABLE",
                                 "migration has no completed or partial state to restore")
        errors = _restore_sources(root, journal)
        errors.extend(_restore_link_updates(root, journal))
        if not errors:
            owned = {relative: journal["targetHashes"][relative] for relative in journal["writtenTargets"]}
            errors.extend(_cleanup_targets(root, owned))
            errors.extend(_remove_created_directories(root, journal["createdDirectories"]))
        journal["phase"] = "partial-restored" if errors else "restored"
        journal["recoveryErrors"] = errors
        try:
            _save_journal(journal)
        except (MigrationError, OSError) as exc:
            errors.append(_diagnostic("RECOVERY_JOURNAL_WRITE_FAILED", str(exc), str(_journal_path(identifier))))
        return _result("conflict" if errors else "restored", errors, migrationId=identifier,
                       journal=str(_journal_path(identifier)), phase=journal["phase"])
    except MigrationError as exc:
        return _result("error", [_diagnostic(exc.code, exc.message, exc.path)])
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
        return _result("error", [_diagnostic("RESTORE_FAILED", str(exc))])


def read_plan(source: str | None) -> dict:
    try:
        raw = Path(source).read_text(encoding="utf-8") if source else sys.stdin.read()
        value = json.loads(raw, object_pairs_hook=_json_pairs)
    except (OSError, UnicodeError, ValueError) as exc:
        raise MigrationError("PLAN_INVALID", "unable to read migration plan: %s" % exc) from exc
    if not isinstance(value, dict):
        raise MigrationError("PLAN_INVALID", "migration plan must be a JSON object")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("check", "diff", "apply", "verify", "restore"))
    parser.add_argument("workspace")
    parser.add_argument("--plan-file")
    parser.add_argument("--confirm", help="confirmation digest printed by diff")
    parser.add_argument("--migration", help="migration UUID for verify or restore")
    args = parser.parse_args(argv)
    try:
        if args.operation in ("check", "diff", "apply"):
            value = read_plan(args.plan_file)
            result = (check_plan(args.workspace, value) if args.operation == "check" else
                      diff_plan(args.workspace, value) if args.operation == "diff" else
                      apply_plan(args.workspace, value, args.confirm))
        elif args.operation == "verify":
            if not args.migration:
                raise MigrationError("MIGRATION_ID_MISSING", "verify requires --migration")
            result = verify_migration(args.workspace, args.migration)
        else:
            if not args.migration:
                raise MigrationError("MIGRATION_ID_MISSING", "restore requires --migration")
            result = restore_migration(args.workspace, args.migration)
    except MigrationError as exc:
        result = _result("error", [_diagnostic(exc.code, exc.message, exc.path)])
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 1 if result["status"] not in ("ok", "verified", "completed", "restored", "needs-confirmation") else 0


if __name__ == "__main__":
    raise SystemExit(main())
