#!/usr/bin/env python3
"""Build and verify the small, reproducible Agent Architect release bundle.

The bundle contains the maintenance and evaluation rules and their
standard-library tools. It is separate from a user scaffold and never copies
Description, Context, domain Skills, tests, or repository metadata.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]
CANONICAL_REPOSITORY = "heibaoxia/My-Personal-Agent"
CONTRACT_VERSION = "1"
EVALUATION_VERSION = "1"
MIGRATION_VERSION = "1"
MANIFEST_NAME = "architect-bundle-manifest.json"
ASSET_PREFIX = "architect-bundle-"
BUNDLE_LIMIT = 64 * 1024 * 1024
FILE_MAP = {
    ".agent/architect/tools/agent_scaffold.py": ROOT / "tools/agent_scaffold.py",
    ".agent/architect/tools/validate_scaffold.py": ROOT / "tools/validate_scaffold.py",
    ".agent/architect/tools/agent_evaluation.py": ROOT / "tools/agent_evaluation.py",
    ".agent/architect/tools/agent_asset_migration.py": ROOT / "tools/agent_asset_migration.py",
    ".agent/architect/tools/scaffold-evaluate.md": ROOT / ".agents/skills/scaffold-evaluate/SKILL.md",
}


def architect_skill_bytes(skill_root: Path | None = None) -> bytes:
    """Render the portable maintenance Skill as the scaffold's architect entry."""
    skill_file = skill_root / "SKILL.md" if skill_root else ROOT / ".agents/skills/agent-scaffold-maintenance/SKILL.md"
    source = skill_file.read_text(encoding="utf-8")
    parts = source.split("---", 2)
    if len(parts) != 3:
        raise ValueError("maintenance Skill is missing frontmatter")
    frontmatter = """---
id: agent-architect
purpose: "Maintain this assistant with a confirmed complete file plan"
triggers:
  - "explicit agent-architect request"
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
"""
    body = parts[2].lstrip("\r\n").replace("{baseDir}/tools", ".agent/architect/tools")
    return (frontmatter + body +
            "\nCLI: .agent/architect/tools/agent_scaffold.py\n").encode("utf-8")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def evaluation_skill_bytes(skill_root: Path | None = None) -> bytes:
    """Use the canonical evaluation body with the produced-workspace contract."""
    path = skill_root / "tools/scaffold-evaluate.md" if skill_root else ROOT / ".agents/skills/scaffold-evaluate/SKILL.md"
    source = path.read_text(encoding="utf-8")
    parts = source.split("---", 2)
    if len(parts) != 3:
        raise ValueError("evaluation Skill is missing frontmatter")
    frontmatter = """---
id: scaffold-evaluate
purpose: "Evaluate this assistant against its Description with traceable evidence"
triggers:
  - "explicit scaffold evaluation request"
excludes:
  - "ordinary domain conversation or Context archive"
inputs:
  - "user evaluation request and target workspace"
outputs:
  - "scaffold-evaluation.md and scaffold-score-history.md"
sideEffects: writes-workspace
confirmation: required
implementation: "The host follows the canonical evaluation rules and uses the bundled artifact tool."
---
"""
    body = parts[2].lstrip("\r\n").replace("{baseDir}/tools", ".agent/architect/tools")
    return (frontmatter + body + "\nArtifact tool: .agent/architect/tools/agent_evaluation.py\n").encode("utf-8")


def source_commit() -> str:
    sources = ["tools/agent_scaffold.py", "tools/validate_scaffold.py",
               "tools/agent_evaluation.py", "tools/agent_asset_migration.py",
               ".agents/skills/scaffold-evaluate/SKILL.md",
               ".agents/skills/agent-scaffold-maintenance/SKILL.md"]
    try:
        dirty = subprocess.run(
            ["git", "-C", str(ROOT), "status", "--porcelain", "--", *sources],
            check=True, capture_output=True, text=True, timeout=5)
        if dirty.stdout.strip():
            return "unknown"
        result = subprocess.run(
            ["git", "-C", str(ROOT), "log", "-1", "--format=%H", "--", *sources],
            check=True, capture_output=True, text=True, timeout=5
        )
        value = result.stdout.strip().lower()
        return value if re.fullmatch(r"[0-9a-f]{40}", value) else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def safe_rel(value: object) -> str:
    if not isinstance(value, str) or not value or value.replace("\\", "/") != value:
        raise ValueError("bundle path must use normalized relative POSIX syntax")
    path = value.split("/")
    if any(part in ("", ".", "..") for part in path) or value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        raise ValueError("bundle path escapes its root")
    if any(re.search(r'[:<>"|?*\x00-\x1f]', part) or part.endswith((".", " "))
           or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part) for part in path):
        raise ValueError("bundle path is unsafe on supported filesystems")
    return value


def pairs_without_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("manifest contains duplicate key: %s" % key)
        result[key] = value
    return result


def manifest_for(version: str, commit: str, files: dict[str, str]) -> dict:
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("bundle version must be major.minor.patch")
    return {
        "schemaVersion": 1,
        "bundleVersion": version,
        "contractVersion": CONTRACT_VERSION,
        "evaluationVersion": EVALUATION_VERSION,
        "migrationVersion": MIGRATION_VERSION,
        "sourceRepository": CANONICAL_REPOSITORY,
        "sourceCommit": commit,
        "assetName": ASSET_PREFIX + version + ".zip",
        "files": dict(sorted(files.items())),
        "protectedFiles": sorted(files),
        "entrypoints": {
            "skill": ".agent/skills/agent-architect.md",
            "cli": ".agent/architect/tools/agent_scaffold.py",
            "validator": ".agent/architect/tools/validate_scaffold.py",
            "evaluationSkill": ".agent/skills/scaffold-evaluate.md",
            "evaluationTool": ".agent/architect/tools/agent_evaluation.py",
            "migrationTool": ".agent/architect/tools/agent_asset_migration.py",
        },
    }


def _read_bundle_files(root: Path) -> tuple[dict, dict[str, bytes]]:
    manifest_path = root / MANIFEST_NAME
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ValueError("bundle is missing %s" % MANIFEST_NAME)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"), object_pairs_hook=pairs_without_duplicates)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("invalid bundle manifest: %s" % exc) from exc
    if not isinstance(manifest, dict) or manifest.get("schemaVersion") != 1:
        raise ValueError("bundle manifest schemaVersion must be 1")
    version = manifest.get("bundleVersion")
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("bundleVersion must be major.minor.patch")
    if manifest.get("contractVersion") != CONTRACT_VERSION:
        raise ValueError("unsupported contractVersion")
    if manifest.get("sourceRepository") != CANONICAL_REPOSITORY:
        raise ValueError("sourceRepository is not canonical")
    commit = manifest.get("sourceCommit")
    if not isinstance(commit, str) or not re.fullmatch(r"(?:[0-9a-f]{40}|unknown)", commit):
        raise ValueError("sourceCommit must be a 40-character SHA-1 or 'unknown'")
    if manifest.get("assetName") != ASSET_PREFIX + version + ".zip":
        raise ValueError("assetName does not match bundleVersion")
    files = manifest.get("files")
    protected = manifest.get("protectedFiles")
    if not isinstance(files, dict) or not files or not isinstance(protected, list):
        raise ValueError("manifest needs files and protectedFiles")
    if len({rel.casefold() for rel in files}) != len(files):
        raise ValueError("manifest contains duplicate paths")
    if not all(isinstance(rel, str) for rel in protected) or sorted(protected) != sorted(files) or len(protected) != len(set(protected)):
        raise ValueError("protectedFiles must exactly list files without duplicates")
    found: dict[str, bytes] = {}
    for rel, expected in files.items():
        safe_rel(rel)
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError("invalid SHA-256 for %s" % rel)
        path = root / rel
        if not path.is_file() or path.is_symlink():
            raise ValueError("bundle file is missing or symbolic: %s" % rel)
        data = path.read_bytes()
        if digest(data) != expected:
            raise ValueError("bundle file hash mismatch: %s" % rel)
        found[rel] = data
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.name != MANIFEST_NAME
    }
    if actual != set(files):
        raise ValueError("bundle contains files outside its manifest")
    points = manifest.get("entrypoints")
    roles = {"skill", "cli", "validator"}
    if "evaluationVersion" in manifest:
        if manifest["evaluationVersion"] != EVALUATION_VERSION:
            raise ValueError("unsupported evaluationVersion")
        roles.update(("evaluationSkill", "evaluationTool"))
    if "migrationVersion" in manifest:
        if manifest["migrationVersion"] != MIGRATION_VERSION:
            raise ValueError("unsupported migrationVersion")
        roles.add("migrationTool")
    if not isinstance(points, dict) or set(points) != roles:
        raise ValueError("manifest entrypoints are incomplete")
    if any(not isinstance(points[key], str) or points[key] not in files for key in points):
        raise ValueError("manifest entrypoint is not listed in files")
    if "evaluationVersion" in manifest:
        helper = str(Path(points["cli"]).with_name("agent_evaluation.py")).replace("\\", "/")
        rules = str(Path(points["cli"]).with_name("scaffold-evaluate.md")).replace("\\", "/")
        if points["evaluationSkill"] != ".agent/skills/scaffold-evaluate.md" or points["evaluationTool"] != helper or rules not in files:
            raise ValueError("evaluation bundle paths or canonical rules are incomplete")
    if "migrationVersion" in manifest:
        migration_tool = str(Path(points["cli"]).with_name("agent_asset_migration.py")).replace("\\", "/")
        if points["migrationTool"] != migration_tool:
            raise ValueError("asset migration bundle path is invalid")
    return manifest, found


def build(output: str, version: str, commit: str | None, zip_output: str | None,
          skill_root: str | None = None) -> dict:
    commit = commit or source_commit()
    if not re.fullmatch(r"(?:[0-9a-f]{40}|unknown)", commit):
        raise ValueError("source commit must be a 40-character SHA-1 or 'unknown'")
    destination = Path(output).expanduser().resolve()
    if destination.exists() or destination.is_symlink():
        raise ValueError("output already exists: %s" % destination)
    contents = {}
    snapshot = Path(skill_root).resolve() if skill_root else None
    sources = {rel: snapshot / "tools" / Path(rel).name for rel in FILE_MAP} if snapshot else FILE_MAP
    for rel, source in sources.items():
        if not source.is_file() or source.is_symlink():
            raise ValueError("source file is missing or symbolic: %s" % source)
        contents[rel] = source.read_bytes()
    contents[".agent/skills/agent-architect.md"] = architect_skill_bytes(snapshot)
    contents[".agent/skills/scaffold-evaluate.md"] = evaluation_skill_bytes(snapshot)
    tool_version = re.search(rb'TOOL_VERSION = "([^"]+)"', contents[".agent/architect/tools/agent_scaffold.py"])
    if tool_version is None:
        raise ValueError("CLI is missing TOOL_VERSION")
    release = {
        "schemaVersion": 1, "toolVersion": tool_version.group(1).decode("ascii"),
        "bundleVersion": version, "contractVersion": CONTRACT_VERSION,
        "evaluationVersion": EVALUATION_VERSION,
        "migrationVersion": MIGRATION_VERSION,
        "source": "tools/", "sourceRepository": CANONICAL_REPOSITORY,
        "sourceCommit": commit, "assetName": ASSET_PREFIX + version + ".zip",
        "files": {Path(rel).name: digest(data) for rel, data in contents.items() if rel.startswith(".agent/architect/tools/")},
    }
    contents[".agent/architect/tools/release.json"] = (json.dumps(release, indent=2, sort_keys=True) + "\n").encode("utf-8")
    manifest = manifest_for(version, commit, {rel: digest(data) for rel, data in contents.items()})
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="architect-bundle-", dir=str(destination.parent)))
    try:
        for rel, data in contents.items():
            path = stage / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        (stage / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        checked, _ = _read_bundle_files(stage)
        os.replace(stage, destination)
        stage = None
    finally:
        if stage is not None and stage.exists():
            shutil.rmtree(stage)
    archive = None
    if zip_output:
        archive = Path(zip_output).expanduser().resolve()
        if archive.exists() or archive.is_symlink():
            raise ValueError("zip output already exists: %s" % archive)
        archive.parent.mkdir(parents=True, exist_ok=True)
        temporary = archive.with_name("." + archive.name + ".tmp")
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
            for path in sorted(destination.rglob("*")):
                if path.is_file() and not path.is_symlink():
                    zipped.write(path, path.relative_to(destination).as_posix())
        os.replace(temporary, archive)
    return {"status": "ok", "bundle": str(destination), "zip": str(archive) if archive else None,
            "bundleVersion": checked["bundleVersion"], "sourceRepository": checked["sourceRepository"],
            "sourceCommit": checked["sourceCommit"], "files": checked["files"],
            "assetName": checked["assetName"]}


def check(bundle: str) -> dict:
    path = Path(bundle).expanduser().resolve()
    with tempfile.TemporaryDirectory(prefix="architect-bundle-check-") as temp:
        root = Path(temp)
        if path.is_dir():
            if any(item.is_symlink() for item in path.rglob("*")):
                raise ValueError("bundle directory contains symbolic links")
            shutil.copytree(path, root / "bundle")
            root = root / "bundle"
        elif path.is_file() and path.suffix.lower() == ".zip":
            if path.stat().st_size > BUNDLE_LIMIT:
                raise ValueError("bundle ZIP exceeds 64 MiB")
            with zipfile.ZipFile(path) as archive:
                seen = set()
                expanded = 0
                for info in archive.infolist():
                    rel = safe_rel(info.filename.rstrip("/"))
                    if rel.casefold() in seen or (info.external_attr >> 16) & 0o170000 == 0o120000:
                        raise ValueError("ZIP contains duplicate files or symbolic links")
                    seen.add(rel.casefold())
                    if info.flag_bits & 1:
                        raise ValueError("bundle ZIP must not contain encrypted files")
                    if info.is_dir():
                        continue
                    expanded += info.file_size
                    if expanded > BUNDLE_LIMIT:
                        raise ValueError("expanded bundle exceeds 64 MiB")
                    target = root / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(archive.read(info))
        else:
            raise ValueError("bundle must be a directory or .zip")
        manifest, files = _read_bundle_files(root)
        return {"status": "ok", "bundle": str(path), "bundleVersion": manifest["bundleVersion"],
                "evaluationVersion": manifest.get("evaluationVersion"),
                "evaluationStatus": "available" if "evaluationVersion" in manifest else "legacy",
                "migrationVersion": manifest.get("migrationVersion"),
                "migrationStatus": "available" if "migrationVersion" in manifest else "legacy",
                "sourceRepository": manifest["sourceRepository"], "sourceCommit": manifest["sourceCommit"],
                "assetName": manifest["assetName"], "files": manifest["files"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    build_parser = sub.add_parser("build", help="build a directory and optional ZIP release asset")
    build_parser.add_argument("--output", required=True)
    build_parser.add_argument("--version", required=True)
    build_parser.add_argument("--source-commit")
    build_parser.add_argument("--zip")
    build_parser.add_argument("--skill-root", help="build from a standalone Skill directory snapshot")
    check_parser = sub.add_parser("check", help="verify a bundle directory or ZIP")
    check_parser.add_argument("bundle")
    args = parser.parse_args(argv)
    try:
        result = build(args.output, args.version, args.source_commit, args.zip, args.skill_root) if args.command == "build" else check(args.bundle)
    except (OSError, ValueError, RuntimeError, NotImplementedError, zipfile.BadZipFile) as exc:
        print(json.dumps({"status": "error", "code": "BUNDLE_INVALID", "message": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
