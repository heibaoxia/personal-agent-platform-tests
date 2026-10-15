#!/usr/bin/env python3
"""Publish only the standalone Architect Skill to its dedicated GitHub repository."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import zipfile

try:
    from . import release_architect_bundle as bundle
except ImportError:
    import release_architect_bundle as bundle


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "heibaoxia/My-Personal-Agent"
API_ROOT = "repos/" + REPOSITORY
SOURCES = {
    "SKILL.md": ".agents/skills/agent-scaffold-maintenance/SKILL.md",
    "tools/agent_scaffold.py": "tools/agent_scaffold.py",
    "tools/validate_scaffold.py": "tools/validate_scaffold.py",
    "tools/agent_evaluation.py": "tools/agent_evaluation.py",
    "tools/agent_asset_migration.py": "tools/agent_asset_migration.py",
    "tools/scaffold-evaluate.md": ".agents/skills/scaffold-evaluate/SKILL.md",
}
FILES = {*SOURCES, "tools/release.json"}


def gh(arguments: list[str], allow_missing: bool = False) -> str | None:
    result = subprocess.run(["gh", *arguments], capture_output=True, text=True,
                            encoding="utf-8", timeout=120)
    if result.returncode:
        if allow_missing and "(HTTP 404)" in result.stderr:
            return None
        raise ValueError(result.stderr.strip() or result.stdout.strip() or "GitHub command failed")
    return result.stdout


def api(endpoint: str, payload: dict | None = None, allow_missing: bool = False):
    arguments = ["api", API_ROOT + "/" + endpoint]
    if payload is None:
        result = gh(arguments, allow_missing)
    else:
        with tempfile.TemporaryDirectory(prefix="architect-publish-request-") as temporary:
            request = Path(temporary) / "request.json"
            request.write_text(json.dumps(payload), encoding="utf-8")
            result = gh([*arguments, "--method", "PATCH" if endpoint.startswith("git/refs/") else "POST",
                         "--input", str(request)], allow_missing)
    return json.loads(result) if result is not None else None


def version_tuple(value: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or not re.fullmatch(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", value):
        raise ValueError("version must be numeric major.minor.patch without leading zeros")
    return tuple(map(int, value.split(".")))


def blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest()


def snapshot() -> dict[str, bytes]:
    contents = {}
    for destination, relative in SOURCES.items():
        source = ROOT / relative
        if not source.is_file() or source.is_symlink():
            raise ValueError("source is missing or symbolic: " + str(source))
        contents[destination] = source.read_bytes()
    tool_version(contents)
    return contents


def tool_version(contents: dict[str, bytes]) -> str:
    matched = re.search(rb'^TOOL_VERSION = "([^"]+)"', contents["tools/agent_scaffold.py"], re.MULTILINE)
    if matched is None:
        raise ValueError("CLI does not declare TOOL_VERSION")
    value = matched.group(1).decode("ascii")
    version_tuple(value)
    return value


def tree_files(tree: dict) -> dict[str, str]:
    if tree.get("truncated"):
        raise ValueError("GitHub returned an incomplete file tree")
    found = {}
    for entry in tree["tree"]:
        path = entry["path"]
        if entry["type"] == "tree" and path == "tools":
            continue
        if entry["type"] != "blob" or entry["mode"] != "100644" or path not in FILES:
            raise ValueError("remote contains a file outside the standalone Skill: " + path)
        found[path] = entry["sha"]
    if not {"SKILL.md", "tools/agent_scaffold.py", "tools/validate_scaffold.py"}.issubset(found):
        raise ValueError("remote is missing standalone Skill source files")
    return found


def commit_files(commit: str) -> tuple[str, dict[str, str]]:
    item = api("git/commits/" + commit)
    tree = item["tree"]["sha"]
    return tree, tree_files(api("git/trees/" + tree + "?recursive=1"))


def remote_state() -> dict:
    head = api("git/ref/heads/main")["object"]["sha"]
    tree, files = commit_files(head)
    metadata = {}
    if "tools/release.json" in files:
        encoded = api("git/blobs/" + files["tools/release.json"])
        metadata = json.loads(base64.b64decode(encoded["content"]).decode("utf-8"))
        if not isinstance(metadata, dict):
            raise ValueError("remote release.json must be an object")
    return {"head": head, "tree": tree, "files": files, "metadata": metadata}


def verify_source(commit: str, contents: dict[str, bytes]) -> None:
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("remote release.json has no public sourceCommit")
    _, files = commit_files(commit)
    if any(files.get(path) != blob_sha(data) for path, data in contents.items()):
        raise ValueError("public sourceCommit does not match the standalone source files")


def release_bytes(contents: dict[str, bytes], version: str, source_commit: str) -> bytes:
    document = {
        "schemaVersion": 1, "toolVersion": tool_version(contents), "bundleVersion": version,
        "contractVersion": "1", "source": "tools/", "sourceRepository": REPOSITORY,
        "evaluationVersion": bundle.EVALUATION_VERSION,
        "migrationVersion": bundle.MIGRATION_VERSION,
        "sourceCommit": source_commit, "assetName": "architect-bundle-" + version + ".zip",
        "files": {Path(path).name: hashlib.sha256(data).hexdigest()
                  for path, data in contents.items() if path.startswith("tools/")},
    }
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")


def latest_release():
    return api("releases/latest", allow_missing=True)


def draft_release(tag: str) -> dict | None:
    releases = api("releases?per_page=100")
    if not isinstance(releases, list):
        raise ValueError("GitHub Release listing is invalid")
    matches = [item for item in releases
               if isinstance(item, dict) and item.get("tag_name") == tag and item.get("draft") is True]
    if len(matches) > 1:
        raise ValueError("multiple draft Releases use the same tag")
    return matches[0] if matches else None


def selected_version(contents: dict[str, bytes], requested: str | None, latest: dict | None) -> str:
    current = version_tuple(tool_version(contents))
    latest_version = version_tuple(latest["tag_name"].removeprefix("v")) if latest else None
    if requested is None:
        candidate = (latest_version[0], latest_version[1], latest_version[2] + 1) if latest_version else current
        requested = ".".join(map(str, max(current, candidate)))
    value = version_tuple(requested)
    if value < current or (latest_version is not None and value <= latest_version):
        raise ValueError("release version must be at least TOOL_VERSION and newer than the latest Release")
    return requested


def put_blob(data: bytes) -> str:
    actual = api("git/blobs", {"encoding": "base64", "content": base64.b64encode(data).decode("ascii")})["sha"]
    if actual != blob_sha(data):
        raise ValueError("GitHub blob hash does not match the local snapshot")
    return actual


def commit_changes(parent: str, tree: str, changes: dict[str, bytes], message: str) -> tuple[str, str]:
    entries = [{"path": path, "mode": "100644", "type": "blob", "sha": put_blob(data)}
               for path, data in sorted(changes.items())]
    tree = api("git/trees", {"base_tree": tree, "tree": entries})["sha"]
    commit = api("git/commits", {"message": message, "tree": tree, "parents": [parent]})["sha"]
    return commit, tree


def push(contents: dict[str, bytes], version: str | None = None, dry_run: bool = False) -> dict:
    remote = remote_state()
    if version is None:
        old_version = remote["metadata"].get("bundleVersion", tool_version(contents))
        version = ".".join(map(str, max(version_tuple(old_version), version_tuple(tool_version(contents)))))
    version_tuple(version)
    changed = {path: data for path, data in contents.items() if remote["files"].get(path) != blob_sha(data)}
    source = None if changed else remote["metadata"].get("sourceCommit")
    if not changed:
        verify_source(source, contents)
    metadata = release_bytes(contents, version, source) if source else None
    metadata_changed = metadata is None or remote["files"].get("tools/release.json") != blob_sha(metadata)
    paths = sorted([*changed, *(["tools/release.json"] if metadata_changed else [])])
    result = {"status": "dry-run" if dry_run else "ok", "repository": REPOSITORY,
              "remoteHead": remote["head"], "bundleVersion": version,
              "changedFiles": paths, "sourceCommit": source, "commit": remote["head"]}
    if dry_run or not paths:
        return result
    parent, tree = remote["head"], remote["tree"]
    if changed:
        source, tree = commit_changes(parent, tree, changed, "Update standalone Architect Skill sources")
        parent = source
        metadata = release_bytes(contents, version, source)
    parent, tree = commit_changes(parent, tree, {"tools/release.json": metadata},
                                  "Bind Architect Skill release metadata " + version)
    if api("git/ref/heads/main")["object"]["sha"] != remote["head"]:
        raise ValueError("remote main changed during publication; retry after reviewing the new state")
    api("git/refs/heads/main", {"sha": parent, "force": False})
    result.update(commit=parent, sourceCommit=source)
    return result


def write_new_or_same(path: Path, data: bytes) -> None:
    if path.is_symlink() or (path.exists() and (not path.is_file() or path.read_bytes() != data)):
        raise ValueError("existing staged file differs: " + str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(data)


def staged_assets(stage: Path, skill: Path, version: str, source: str) -> dict[str, str]:
    runtime = stage / ("architect-bundle-" + version + ".zip")
    if runtime.is_symlink():
        raise ValueError("staged runtime ZIP cannot be symbolic")
    if not runtime.exists():
        bundle.build(str(stage / ("bundle-" + str(time.time_ns()))), version, source,
                     str(runtime), skill_root=skill)
    checked = bundle.check(str(runtime))
    if checked["bundleVersion"] != version or checked["sourceCommit"] != source:
        raise ValueError("staged runtime ZIP has different version or sourceCommit")
    expected_runtime = {
        ".agent/architect/tools/agent_scaffold.py": (skill / "tools/agent_scaffold.py").read_bytes(),
        ".agent/architect/tools/validate_scaffold.py": (skill / "tools/validate_scaffold.py").read_bytes(),
        ".agent/architect/tools/agent_evaluation.py": (skill / "tools/agent_evaluation.py").read_bytes(),
        ".agent/architect/tools/agent_asset_migration.py": (skill / "tools/agent_asset_migration.py").read_bytes(),
        ".agent/architect/tools/scaffold-evaluate.md": (skill / "tools/scaffold-evaluate.md").read_bytes(),
        ".agent/architect/tools/release.json": (skill / "tools/release.json").read_bytes(),
        ".agent/skills/agent-architect.md": bundle.architect_skill_bytes(skill),
        ".agent/skills/scaffold-evaluate.md": bundle.evaluation_skill_bytes(skill),
    }
    if checked["files"] != {path: hashlib.sha256(data).hexdigest() for path, data in expected_runtime.items()}:
        raise ValueError("staged runtime ZIP does not match the source snapshot")
    with zipfile.ZipFile(runtime) as archive:
        write_new_or_same(stage / bundle.MANIFEST_NAME, archive.read(bundle.MANIFEST_NAME))
    installed = stage / ("agent-scaffold-maintenance-" + version + ".zip")
    expected = {path: (skill / path).read_bytes() for path in sorted(FILES)}
    if installed.is_symlink():
        raise ValueError("staged Skill ZIP cannot be symbolic")
    if not installed.exists():
        with zipfile.ZipFile(installed, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path, data in expected.items():
                archive.writestr(path, data)
    with zipfile.ZipFile(installed) as archive:
        if len(archive.namelist()) != len(FILES) or set(archive.namelist()) != FILES:
            raise ValueError("staged Skill ZIP must exactly match the standalone file whitelist")
        if any(archive.read(path) != data for path, data in expected.items()):
            raise ValueError("staged Skill ZIP does not match the source snapshot")
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (installed, runtime, stage / bundle.MANIFEST_NAME)}


def tag_target(tag: str) -> str | None:
    ref = api("git/ref/tags/" + tag, allow_missing=True)
    if ref is None:
        return None
    item = ref["object"]
    while item["type"] == "tag":
        item = api("git/tags/" + item["sha"])["object"]
    if item["type"] != "commit":
        raise ValueError("release tag does not identify a commit")
    return item["sha"]


def verify_assets(release: dict, assets: dict[str, str], complete: bool = False) -> None:
    found = {}
    for item in release.get("assets", []):
        name = item["name"]
        if name not in assets or name in found or item.get("digest") != "sha256:" + assets[name]:
            raise ValueError("GitHub asset is unexpected or has a different SHA-256: " + name)
        found[name] = item
    if complete and set(found) != set(assets):
        raise ValueError("GitHub Release is missing one or more of its three assets")


def release(contents: dict[str, bytes], requested: str | None, notes_file: str | None,
            dry_run: bool = False) -> dict:
    version = selected_version(contents, requested, latest_release())
    tag = "v" + version
    existing = api("releases/tags/" + tag, allow_missing=True)
    if existing is None:
        existing = draft_release(tag)
    if existing and (not existing["draft"] or existing.get("prerelease")):
        raise ValueError("an existing public or prerelease version cannot be overwritten")
    notes = Path(notes_file).read_text(encoding="utf-8") if notes_file else "Standalone Architect Skill " + tag
    stage = ROOT / ".osx-probe/releases" / version
    record = stage / "publication.json"
    if stage.is_symlink() or record.is_symlink():
        raise ValueError("release staging cannot be symbolic")
    publication = json.loads(record.read_text(encoding="utf-8")) if record.is_file() else None
    target = tag_target(tag)
    if target and not publication:
        raise ValueError("existing tag has no matching local publication record")
    if publication:
        if publication.get("repository") != REPOSITORY or publication.get("bundleVersion") != version:
            raise ValueError("staged publication belongs to a different repository or version")
        skill = stage / "skill"
        if any(not (skill / path).is_file() or (skill / path).is_symlink() or
               (skill / path).read_bytes() != data for path, data in contents.items()):
            raise ValueError("local sources changed since staging this version; use a new version")
        verify_source(publication["sourceCommit"], contents)
        metadata = release_bytes(contents, version, publication["sourceCommit"])
        _, target_files = commit_files(publication["commit"])
        expected = {**contents, "tools/release.json": metadata}
        if any(target_files.get(path) != blob_sha(data) for path, data in expected.items()):
            raise ValueError("staged publication commit does not match its standalone snapshot")
    else:
        if existing:
            raise ValueError("existing draft has no matching local publication record")
        publication = push(contents, version, dry_run=dry_run)
    if target and target != publication["commit"]:
        raise ValueError("existing tag points to a different commit")
    if existing and existing["target_commitish"] != publication["commit"]:
        raise ValueError("existing draft points to a different commit")
    if dry_run:
        return {**publication, "status": "dry-run", "tag": tag, "staging": str(stage),
                "assets": ["agent-scaffold-maintenance-" + version + ".zip",
                           "architect-bundle-" + version + ".zip", bundle.MANIFEST_NAME]}
    skill = stage / "skill"
    for path, data in {**contents, "tools/release.json": release_bytes(contents, version, publication["sourceCommit"])}.items():
        write_new_or_same(skill / path, data)
    write_new_or_same(record, (json.dumps(publication, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    assets = staged_assets(stage, skill, version, publication["sourceCommit"])
    if existing is None:
        existing = api("releases", {"tag_name": tag, "target_commitish": publication["commit"],
                                   "name": "Architect Skill " + tag, "body": notes,
                                   "draft": True, "prerelease": False})
    verify_assets(existing, assets)
    uploaded = {item["name"] for item in existing["assets"]}
    for name in assets:
        if name not in uploaded:
            gh(["release", "upload", tag, str(stage / name), "--repo", REPOSITORY])
    existing = api("releases/" + str(existing["id"]))
    if not existing["draft"] or existing["target_commitish"] != publication["commit"]:
        raise ValueError("draft changed during asset upload")
    verify_assets(existing, assets, complete=True)
    gh(["release", "edit", tag, "--repo", REPOSITORY, "--draft=false", "--latest"])
    published = latest_release()
    if published is None or published["tag_name"] != tag or published["draft"] or published["prerelease"]:
        raise ValueError("GitHub did not confirm the requested latest official Release")
    verify_assets(published, assets, complete=True)
    return {**publication, "status": "ok", "tag": tag, "url": published["html_url"],
            "staging": str(stage), "assets": assets}


def validate_before_publication() -> None:
    commands = ([sys.executable, "-B", str(ROOT / "tools/sync_scaffold_skill.py")],
                [sys.executable, "-B", "-m", "unittest", "tools.test_agent_distribution",
                 "tools.test_agent_preflight", "tools.test_agent_handoff",
                 "tools.test_release_architect_bundle", "tools.test_publish_architect_skill", "-q"])
    for command in commands:
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                                encoding="utf-8", timeout=180)
        if result.returncode:
            raise ValueError("local validation failed: " + (result.stderr or result.stdout).strip())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    pushed = commands.add_parser("push")
    pushed.add_argument("--dry-run", action="store_true")
    released = commands.add_parser("release")
    released.add_argument("--version")
    released.add_argument("--notes-file")
    released.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command != "status" and not args.dry_run:
            validate_before_publication()
        contents = snapshot()
        if args.command == "release":
            result = release(contents, args.version, args.notes_file, args.dry_run)
        else:
            result = push(contents, dry_run=args.command == "status" or args.dry_run)
            if args.command == "status":
                latest = latest_release()
                result.update(status="ok", latestRelease=latest["tag_name"] if latest else None,
                              toolVersion=tool_version(contents))
    except (OSError, ValueError, KeyError, subprocess.SubprocessError, zipfile.BadZipFile) as exc:
        print(json.dumps({"status": "error", "code": "PUBLISH_BLOCKED", "message": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
