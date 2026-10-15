#!/usr/bin/env python3
"""Publish or check the byte-identical standard-library maintenance CLI."""

import argparse
import hashlib
import json
from pathlib import Path
import re

try:
    from .release_architect_bundle import source_commit
except ImportError:
    from release_architect_bundle import source_commit


ROOT = Path(__file__).resolve().parents[1]
DESTINATION = ROOT / ".agents/skills/agent-scaffold-maintenance/tools"
MODULES = ("agent_scaffold.py", "validate_scaffold.py", "agent_evaluation.py", "agent_asset_migration.py")
CANONICAL_REPOSITORY = "heibaoxia/My-Personal-Agent"
ASSET_PREFIX = "architect-bundle-"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    source = {name: (ROOT / "tools" / name).read_bytes() for name in MODULES}
    source["scaffold-evaluate.md"] = (ROOT / ".agents/skills/scaffold-evaluate/SKILL.md").read_bytes()
    version = re.search(rb'TOOL_VERSION = "([^"]+)"', source["agent_scaffold.py"]).group(1).decode("ascii")
    commit = source_commit()
    manifest = {"schemaVersion": 1, "toolVersion": version, "bundleVersion": version,
                "contractVersion": "1", "source": "tools/", "sourceRepository": CANONICAL_REPOSITORY,
                "evaluationVersion": "1",
                "migrationVersion": "1",
                "sourceCommit": commit, "assetName": ASSET_PREFIX + version + ".zip",
                "files": {name: hashlib.sha256(content).hexdigest() for name, content in source.items()}}
    release = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    expected = {**source, "release.json": release}
    if args.check:
        errors = [str(DESTINATION / name) for name, content in expected.items()
                  if not (DESTINATION / name).is_file() or (DESTINATION / name).read_bytes() != content]
        print(json.dumps({"status": "error" if errors else "ok", "toolVersion": version, "drift": errors}, indent=2))
        return int(bool(errors))
    DESTINATION.mkdir(parents=True, exist_ok=True)
    for name, content in expected.items():
        (DESTINATION / name).write_bytes(content)
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
