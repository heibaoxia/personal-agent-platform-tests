#!/usr/bin/env python3
"""Validate and write two fixed scaffold outcomes; never choose semantic scores."""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile


SCHEMA_VERSION = 1
SPECIFICATION_VERSION = "1"
REPORT_FILE = "scaffold-evaluation.md"
HISTORY_FILE = "scaffold-score-history.md"
DIMENSIONS = ("goal-coverage", "rule-consistency", "behavior-logic",
              "context-stability", "interaction-efficiency")
LABELS = ("目标覆盖", "规则自洽", "行为逻辑", "Context 稳定性", "交互效率")
STATE_LABELS = {"unverified": "未验证", "not-applicable": "不适用"}
HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
POINTS = {"unmet": 0, "partial": 1, "met": 2}
OUTCOMES = (REPORT_FILE, HISTORY_FILE)


class EvaluationError(RuntimeError):
    def __init__(self, code: str, message: str, path: str | Path | None = None):
        self.code = code
        self.path = str(path) if path is not None else None
        super().__init__(f"{code}: {message}" + (f" ({path})" if path else ""))


def _fail(message: str, code: str = "INPUT_INVALID") -> None:
    raise EvaluationError(code, message)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json(data: object) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2)


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(f"{field} must be a nonempty string")
    return value


def _strings(value: object, field: str, *, nonempty: bool = False) -> list[str]:
    if not isinstance(value, list) or (nonempty and not value):
        _fail(f"{field} must be a list" + (" with evidence" if nonempty else ""))
    for item in value:
        _text(item, field)
    return value


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        _fail(f"{field} must be a stable identifier")
    return value


def _hash(value: object, field: str) -> None:
    if value is not None and (not isinstance(value, str) or not HASH_RE.fullmatch(value)):
        _fail(f"{field} must be a lowercase SHA-256 or null")


def _time(value: object, field: str) -> str:
    text = _text(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        _fail(f"{field} must be an ISO date/time with timezone")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _fail(f"{field} must include a timezone")
    return text


def _now(value: str | None) -> str:
    return _time(value or datetime.now(timezone.utc).isoformat(), "recordedAt")


def _root(workspace: str | Path) -> Path:
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        _fail("workspace must be an existing directory", "PATH_INVALID")
    return root


def _relative(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        _fail("paths must be normalized workspace-relative paths", "PATH_INVALID")
    path = Path(value)
    if path.is_absolute() or any(part in (".", "..") for part in value.split("/")) or "//" in value:
        _fail("path must remain inside the workspace", "PATH_INVALID")
    return value


def _output_dir(value: object) -> str:
    if value is None or value == ".":
        return "."
    relative = _relative(value)
    parts = relative.split("/")
    if any(part.startswith(".") and part in (".", "..") for part in parts):
        _fail("output directory must be a normalized relative path", "PATH_INVALID")
    if any(re.search(r'[:<>"|?*\x00-\x1f]', part) or part.endswith((".", " "))
           or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part)
           for part in parts):
        _fail("output directory contains an unsafe path component", "PATH_INVALID")
    return relative


def _output_paths(root: Path, output_dir: object) -> dict[str, str]:
    directory = _output_dir(output_dir)
    names = {"report": REPORT_FILE, "history": HISTORY_FILE}
    paths = {role: name if directory == "." else f"{directory}/{name}" for role, name in names.items()}
    for relative in paths.values():
        _path(root, relative, outcome=True)
    return paths


def _path(root: Path, relative: str, *, outcome: bool = False) -> Path:
    candidate = root / _relative(relative)
    try:
        candidate.resolve().relative_to(root)
    except (ValueError, OSError) as exc:
        raise EvaluationError("PATH_INVALID", "path escapes workspace", relative) from exc
    if outcome:
        current = root
        for part in relative.split("/"):
            current = current / part
            try:
                mode = current.lstat().st_mode
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise EvaluationError("PATH_INVALID", "outcome path cannot be checked", relative) from exc
            if stat.S_ISLNK(mode):
                raise EvaluationError("PATH_INVALID", "outcome path may not traverse a symbolic link", relative)
    if candidate.exists() and not candidate.is_file():
        raise EvaluationError("PATH_INVALID", "path must be a file", relative)
    return candidate


def _bytes(root: Path, relative: str, *, outcome: bool = False) -> bytes | None:
    path = _path(root, relative, outcome=outcome)
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise EvaluationError("UNREADABLE", "file cannot be read", relative) from exc


def _rule_paths(root: Path) -> list[str]:
    paths = {"AGENTS.md", "scaffold-description.md"}
    protected = {".agent/skills/scaffold-evaluate.md"}
    manifest_path = root / ".agent/scaffold-manifest.json"
    if manifest_path.exists():
        _path(root, ".agent/scaffold-manifest.json")
        try:
            manifest = json.loads(manifest_path.read_bytes())
            listed = manifest["protectedFiles"]
            if not isinstance(listed, list):
                _fail("manifest protectedFiles must be a list", "SNAPSHOT_INVALID")
            listed = [_relative(item) for item in listed]
            points = manifest.get("entrypoints", {})
            roles = [points.get(name) for name in ("initialSkill", "archiveSkill", "architectSkill")] if isinstance(points, dict) else []
            architect = None
            if len(roles) == 3 and all(isinstance(role, str) and re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", role) for role in roles) and len(set(roles)) == 3:
                architect = f".agent/skills/{roles[2]}.md"
            # An arbitrary protectedFiles claim must not hide user rules from the evaluated snapshot.
            for item in listed:
                if not (item.startswith(".agent/architect/") or item in (architect, ".agent/skills/scaffold-evaluate.md")):
                    _fail("manifest protection must identify a shared architect/evaluation resource", "SNAPSHOT_INVALID")
                protected.add(item)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise EvaluationError("SNAPSHOT_INVALID", "cannot determine protected rule scope") from exc
    for relative in (".agent/behaviors.md", ".agent/context.yaml"):
        if (root / relative).exists():
            paths.add(relative)
    for collection in ("skills", "workflows", "integrations"):
        directory = root / ".agent" / collection
        if directory.exists():
            try:
                directory.resolve().relative_to(root)
            except ValueError as exc:
                raise EvaluationError("PATH_INVALID", "rule directory escapes workspace", directory) from exc
            paths.update(path.relative_to(root).as_posix() for path in directory.rglob("*.md"))
    return sorted((paths - protected) | {"AGENTS.md", "scaffold-description.md"})


def _fingerprint(files: dict[str, str | None]) -> str | None:
    if not files.get("AGENTS.md") or any(value is None for value in files.values()):
        return None
    return digest(_json(files).encode("utf-8"))


def capture_snapshot(workspace: str | Path, output_dir: str | None = None) -> dict:
    """Capture evaluated user rules and outcome baselines without reading Context data."""
    root = _root(workspace)
    directory = _output_dir(output_dir)
    output_paths = _output_paths(root, directory)
    files = {}
    gaps = {}
    for relative in _rule_paths(root):
        try:
            data = _bytes(root, relative)
            if data is None:
                gaps[relative] = "missing"
            else:
                data.decode("utf-8")
            files[relative] = digest(data) if data is not None else None
        except UnicodeError:
            files[relative] = None
            gaps[relative] = "not UTF-8"
        except EvaluationError as exc:
            if exc.code != "UNREADABLE":
                raise
            files[relative] = None
            gaps[relative] = "unreadable"
    artifacts = {}
    for relative in output_paths.values():
        data = _bytes(root, relative, outcome=True)
        artifacts[relative] = digest(data) if data is not None else None
    return {"files": files, "ruleFingerprint": _fingerprint(files),
            "descriptionSha256": files["scaffold-description.md"], "outputDir": directory,
            "outputPaths": output_paths, "artifacts": artifacts, "gaps": gaps}


def _snapshot(value: object) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("files"), dict):
        _fail("snapshot must contain files")
    if not {"files", "ruleFingerprint", "descriptionSha256", "artifacts"} <= value.keys():
        _fail("snapshot must explicitly contain both fingerprints and outcome baselines")
    files = value["files"]
    if not {"AGENTS.md", "scaffold-description.md"} <= files.keys():
        _fail("snapshot must include entry and Description, using null when unavailable")
    for relative, checksum in files.items():
        _relative(relative)
        if relative in OUTCOMES or relative.startswith(".agent/architect/") or relative == ".agent/skills/scaffold-evaluate.md":
            _fail("outcomes and protected tools may not enter the rule fingerprint")
        _hash(checksum, "snapshot.files")
    _hash(value.get("ruleFingerprint"), "snapshot.ruleFingerprint")
    if value.get("ruleFingerprint") != _fingerprint(files):
        _fail("rule fingerprint does not match snapshot files")
    if value.get("descriptionSha256") != files["scaffold-description.md"]:
        _fail("Description fingerprint does not match snapshot")
    artifacts = value.get("artifacts")
    if "outputDir" not in value and "outputPaths" not in value:
        paths = {"report": REPORT_FILE, "history": HISTORY_FILE}
        directory = "."
        expected_artifacts = set(OUTCOMES)
    else:
        directory = _output_dir(value.get("outputDir"))
        paths = value.get("outputPaths")
        expected_paths = {role: name if directory == "." else f"{directory}/{name}"
                          for role, name in (("report", REPORT_FILE), ("history", HISTORY_FILE))}
        if paths != expected_paths:
            _fail("snapshot output paths do not match its output directory", "PATH_INVALID")
        expected_artifacts = set(expected_paths.values())
    if not isinstance(artifacts, dict) or set(artifacts) != expected_artifacts:
        _fail("snapshot must include exactly both outcome baselines")
    for checksum in artifacts.values():
        _hash(checksum, "snapshot.artifacts")
    if "outputDir" in value and value["outputDir"] != directory:
        _fail("snapshot outputDir is not normalized", "PATH_INVALID")
    return value


def _same_rules(left: dict, right: dict) -> bool:
    left_dir = _output_dir(left.get("outputDir"))
    right_dir = _output_dir(right.get("outputDir"))
    left_paths = left.get("outputPaths") or {
        "report": REPORT_FILE, "history": HISTORY_FILE}
    right_paths = right.get("outputPaths") or {
        "report": REPORT_FILE, "history": HISTORY_FILE}
    return (all(left.get(key) == right.get(key) for key in
                ("files", "ruleFingerprint", "descriptionSha256"))
            and left_dir == right_dir and left_paths == right_paths)


def _bind_output_dir(snapshot: dict, requested: str | None) -> tuple[str, dict[str, str]]:
    directory = _output_dir(snapshot.get("outputDir"))
    if requested is not None and _output_dir(requested) != directory:
        raise EvaluationError("OUTPUT_DIR_MISMATCH",
                              "operation output directory differs from the snapshot", requested)
    paths = snapshot.get("outputPaths")
    if paths is None:
        paths = {"report": REPORT_FILE, "history": HISTORY_FILE}
    return directory, paths


def _check_baseline(root: Path, baseline: dict, *, rules: bool = True,
                    outcomes: tuple[str, ...] | None = None) -> dict:
    _snapshot(baseline)
    directory = baseline.get("outputDir", ".")
    current = capture_snapshot(root, directory)
    if rules and not _same_rules(baseline, current):
        _fail("evaluated rules changed; capture and evaluate again", "RULES_STALE")
    paths = baseline.get("outputPaths", {"report": REPORT_FILE, "history": HISTORY_FILE})
    names = outcomes or tuple(paths.values())
    for name in names:
        if baseline["artifacts"][name] != current["artifacts"][name]:
            raise EvaluationError("BASELINE_CHANGED", "outcome changed after capture", name)
    return current


def checkpoint_layer(checkpoints: list[dict]) -> dict:
    """Convert five evidenced judgments to one deterministic layer result."""
    if not isinstance(checkpoints, list) or len(checkpoints) != 5:
        _fail("each layer requires exactly five ordered checkpoints")
    known = applicable = unknown = 0
    for point in checkpoints:
        if not isinstance(point, dict):
            _fail("checkpoint must be an object")
        status = point.get("status")
        if status not in (*POINTS, "unknown", "not-applicable"):
            _fail("unsupported checkpoint status")
        _text(point.get("reason"), "checkpoint.reason")
        _strings(point.get("evidence"), "checkpoint.evidence", nonempty=status in POINTS)
        if status == "unknown":
            unknown += 1
        elif status in POINTS:
            applicable += 1
            known += POINTS[status]
    state = "unverified" if unknown else ("verified" if applicable else "not-applicable")
    score = (known * 10 + applicable) // (2 * applicable) if state == "verified" else None
    return {"status": state, "score": score, "checkpoints": copy.deepcopy(checkpoints)}


def normalize_report(report: object) -> dict:
    """Validate the single source for user and architect sections; choose no judgments."""
    if not isinstance(report, dict):
        _fail("report must be an object")
    data = copy.deepcopy(report)
    if type(data.get("schemaVersion")) is not int or data["schemaVersion"] != SCHEMA_VERSION:
        _fail("unsupported report schema version")
    _text(data.get("specificationVersion"), "specificationVersion")
    _identifier(data.get("reportId"), "reportId")
    _time(data.get("evaluatedAt"), "evaluatedAt")
    _text(data.get("architectVersion"), "architectVersion")
    benchmark = data.get("benchmark")
    if not isinstance(benchmark, dict) or benchmark.get("status") not in ("sufficient", "insufficient"):
        _fail("benchmark status must be sufficient or insufficient")
    _strings(benchmark.get("targets"), "benchmark.targets", nonempty=benchmark["status"] == "sufficient")
    _strings(benchmark.get("gaps"), "benchmark.gaps", nonempty=benchmark["status"] == "insufficient")
    if benchmark["status"] == "insufficient" and benchmark["targets"]:
        _fail("insufficient benchmark may not invent confirmed targets")
    _snapshot(data.get("snapshot"))
    if benchmark["status"] == "sufficient" and not data["snapshot"]["descriptionSha256"]:
        _fail("sufficient benchmark requires a readable Description")
    evidence = data.get("evidence")
    if not isinstance(evidence, dict):
        _fail("evidence must be an object")
    _strings(evidence.get("scope"), "evidence.scope")
    _strings(evidence.get("gaps"), "evidence.gaps")
    _strings(evidence.get("types"), "evidence.types")
    if set(evidence["types"]) - {"file-review", "scenario", "observation"}:
        _fail("unsupported evidence type")
    times = _strings(evidence.get("contextTimepoints"), "evidence.contextTimepoints")
    for time in times:
        _time(time, "Context timepoint")
    if type(evidence.get("driftObserved")) is not bool:
        _fail("driftObserved must be boolean")
    instants = {datetime.fromisoformat(time.replace("Z", "+00:00")).astimezone(timezone.utc) for time in times}
    if evidence["driftObserved"] and (len(instants) < 2 or "observation" not in evidence["types"] or not evidence["scope"]):
        _fail("observed drift requires multiple dated states and real change evidence")
    dimensions = data.get("dimensions")
    if not isinstance(dimensions, dict) or set(dimensions) != set(DIMENSIONS):
        _fail("report requires exactly the five fixed dimensions")
    for dimension in DIMENSIONS:
        result = dimensions[dimension]
        if not isinstance(result, dict):
            _fail("dimension result must be an object")
        _text(result.get("explanation"), "dimension.explanation")
        _text(result.get("impact"), "dimension.impact")
        for name in ("design", "usage"):
            layer = result.get(name)
            if not isinstance(layer, dict):
                _fail("dimension requires design and usage layers")
            calculated = checkpoint_layer(layer.get("checkpoints"))
            if ("score" in layer and (layer["score"] != calculated["score"] or
                                      (layer["score"] is not None and type(layer["score"]) is not int))) or ("status" in layer and layer["status"] != calculated["status"]):
                _fail("layer status/score disagrees with its checkpoints")
            if benchmark["status"] == "insufficient" and calculated["status"] != "unverified":
                _fail("insufficient benchmark requires unverified layers, without scores")
            if name == "usage" and calculated["status"] == "verified" and "observation" not in evidence["types"]:
                _fail("usage scores require actual observations")
            result[name] = calculated
    findings = data.get("findings")
    if not isinstance(findings, list):
        _fail("findings must be a list")
    for finding in findings:
        if not isinstance(finding, dict) or finding.get("priority") not in ("P0", "P1", "P2", "P3"):
            _fail("finding requires a valid priority")
        if finding.get("dimension") not in DIMENSIONS:
            _fail("finding requires a fixed dimension")
        if finding.get("attribution") not in ("target-scaffold", "user-change", "host-tool", "architect", "undetermined"):
            _fail("unsupported finding attribution")
        if finding.get("confidence") not in ("low", "medium", "high"):
            _fail("unsupported attribution confidence")
        for field in ("target", "impact", "cause", "candidate", "verification"):
            _text(finding.get(field), f"finding.{field}")
        _strings(finding.get("evidence"), "finding.evidence", nonempty=True)
    data.setdefault("targetEntryId", None)
    if data["targetEntryId"] is not None:
        _identifier(data["targetEntryId"], "targetEntryId")
    return data


def _block(text: str, marker: str) -> dict:
    blocks = re.findall(r"^```" + re.escape(marker) + r"[ \t]*\r?\n(.*?)^```[ \t]*\r?$", text, re.MULTILINE | re.DOTALL)
    if len(blocks) != 1:
        _fail("outcome requires exactly one explicit data block", "FORMAT_INVALID")
    try:
        result = json.loads(blocks[0])
    except ValueError as exc:
        raise EvaluationError("FORMAT_INVALID", "outcome data block is not valid JSON") from exc
    if not isinstance(result, dict):
        _fail("outcome data block must be an object", "FORMAT_INVALID")
    return result


def _read(root: Path, name: str, output_dir: str | None = None) -> dict | None:
    paths = _output_paths(root, output_dir)
    if name == REPORT_FILE:
        name = paths["report"]
    elif name == HISTORY_FILE:
        name = paths["history"]
    raw = _bytes(root, name, outcome=True)
    if raw is None:
        return None
    try:
        return _block(raw.decode("utf-8"), Path(name).name[:-3])
    except UnicodeError as exc:
        raise EvaluationError("FORMAT_INVALID", "outcome is not UTF-8", name) from exc


def read_report(workspace: str | Path, output_dir: str | None = None) -> dict | None:
    data = _read(_root(workspace), REPORT_FILE, output_dir)
    return normalize_report(data) if data is not None else None


def _history(data: object) -> dict:
    if not isinstance(data, dict) or type(data.get("schemaVersion")) is not int or data["schemaVersion"] != SCHEMA_VERSION:
        _fail("unsupported history schema", "FORMAT_INVALID")
    if not {"schemaVersion", "activeEntryId", "entries"} <= data.keys():
        _fail("history is missing required top-level fields", "FORMAT_INVALID")
    entries = data.get("entries")
    if not isinstance(entries, list):
        _fail("history entries must be a list", "FORMAT_INVALID")
    ids = set()
    all_report_ids = set()
    for entry in entries:
        if not isinstance(entry, dict):
            _fail("history entry must be an object", "FORMAT_INVALID")
        required = {"entryId", "kind", "status", "plannedAt", "modifiedAt", "baselineFingerprint", "ruleFingerprint",
                    "plannedChanges", "problems", "actualChanges", "validation", "scores"}
        if not required <= entry.keys():
            _fail("history entry is missing required fields", "FORMAT_INVALID")
        identity = _identifier(entry.get("entryId"), "entryId")
        if identity in ids:
            _fail("duplicate history entry ID", "FORMAT_INVALID")
        ids.add(identity)
        if entry.get("kind") not in ("optimization", "existing-baseline", "unregistered-change", "benchmark-insufficient"):
            _fail("unsupported history entry kind", "FORMAT_INVALID")
        if entry.get("status") not in ("planned", "completed", "failed", "restored", "no-change", "baseline"):
            _fail("unsupported history entry status", "FORMAT_INVALID")
        _time(entry.get("plannedAt"), "plannedAt")
        if entry.get("modifiedAt") is not None:
            _time(entry["modifiedAt"], "modifiedAt")
        for field in ("plannedChanges", "problems", "actualChanges", "validation"):
            _strings(entry.get(field), field)
        for field in ("baselineFingerprint", "ruleFingerprint"):
            _hash(entry.get(field), field)
        if entry["kind"] == "optimization":
            _strings(entry["plannedChanges"], "plannedChanges", nonempty=True)
            if entry["status"] == "baseline":
                _fail("optimization may not impersonate an existing baseline", "FORMAT_INVALID")
            if entry["status"] == "completed" and (entry["modifiedAt"] is None or
                    entry["ruleFingerprint"] is None or entry["ruleFingerprint"] == entry["baselineFingerprint"] or
                    not entry["actualChanges"] or not entry["validation"]):
                _fail("completed optimization requires actual changes and verification", "FORMAT_INVALID")
            if entry["status"] in ("restored", "no-change") and entry["ruleFingerprint"] != entry["baselineFingerprint"]:
                _fail("restored/no-change result differs from its baseline", "FORMAT_INVALID")
            if entry["status"] in ("restored", "no-change") and entry["ruleFingerprint"] is None:
                _fail("restored/no-change result must have a verifiable rule baseline", "FORMAT_INVALID")
        elif entry["status"] != "baseline" or entry["plannedChanges"] or entry["actualChanges"] or entry["modifiedAt"] is not None:
            _fail("baseline entry may not invent an optimization process", "FORMAT_INVALID")
        scores = entry.get("scores")
        if not isinstance(scores, list):
            _fail("history scores must be a list", "FORMAT_INVALID")
        for score in scores:
            normalized = normalize_report(score)
            if normalized != score or score["reportId"] in all_report_ids or score["targetEntryId"] != identity:
                _fail("invalid, duplicate or wrongly associated historical score", "FORMAT_INVALID")
            if score["snapshot"]["ruleFingerprint"] != entry["ruleFingerprint"]:
                _fail("historical score differs from its rule version", "FORMAT_INVALID")
            all_report_ids.add(score["reportId"])
    active = data.get("activeEntryId")
    if active is not None:
        entry = next((entry for entry in entries if entry["entryId"] == active), None)
        if entry is None or entry["status"] not in ("completed", "baseline"):
            _fail("active entry must identify an effective version", "FORMAT_INVALID")
    return data


def read_history(workspace: str | Path, output_dir: str | None = None) -> dict:
    data = _read(_root(workspace), HISTORY_FILE, output_dir)
    return _history(data) if data is not None else {"schemaVersion": SCHEMA_VERSION, "activeEntryId": None, "entries": []}


def inspect_report(workspace: str | Path, output_dir: str | None = None) -> dict:
    try:
        requested = _output_dir(output_dir)
        report = read_report(workspace, requested)
        if report is None:
            if requested != "." and (read_report(workspace) is not None
                                     or read_history(workspace)["entries"]):
                raise EvaluationError("OUTPUT_DIR_MISMATCH",
                                      "existing outcomes use the workspace root directory", requested)
            return {"status": "missing"}
        _bind_output_dir(report["snapshot"], requested)
        current = capture_snapshot(workspace, requested)
        return {"status": "current" if _same_rules(report["snapshot"], current) and report["specificationVersion"] == SPECIFICATION_VERSION else "stale",
                "reportId": report["reportId"], "report": report}
    except EvaluationError as exc:
        return {"status": "unreadable" if exc.code == "UNREADABLE" else "invalid",
                "error": {"code": exc.code, "message": str(exc)}}


def _cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def _score(layer: dict) -> str:
    return str(layer["score"]) if layer["score"] is not None else STATE_LABELS[layer["status"]]


def render_report(report: dict) -> str:
    data = normalize_report(report)
    lines = ["# 脚手架评分报告", "", "## 用户报告", "",
             f"评分日期：{data['evaluatedAt']}", "目标基准：" + ("可评分" if data["benchmark"]["status"] == "sufficient" else "基准不足"),
             "", "确认目标：" + _cell("; ".join(data["benchmark"]["targets"]) or "未验证：Description 不可取得或不足"),
             "", "证据范围：" + _cell("; ".join(data["evidence"]["scope"]) or "没有可用证据"),
             "", "证据缺口：" + _cell("; ".join(data["benchmark"]["gaps"] + data["evidence"]["gaps"]) or "未记录"),
             "", "| 维度 | 设计分 | 使用分 | 通俗说明 | 用户影响 |",
             "| --- | --- | --- | --- | --- |"]
    for dimension, label in zip(DIMENSIONS, LABELS):
        result = data["dimensions"][dimension]
        lines.append("| " + " | ".join(_cell(item) for item in (label, _score(result["design"]), _score(result["usage"]), result["explanation"], result["impact"])) + " |")
    severe = [finding for finding in data["findings"] if finding["priority"] in ("P0", "P1")]
    if severe:
        lines.extend(["", "### 严重问题与影响", ""])
        lines.extend(f"- **{finding['priority']}**：{_cell(finding['impact'])}（目标：{_cell(finding['target'])}）"
                     for finding in sorted(severe, key=lambda finding: finding["priority"]))
    lines.extend(["", "## 架构师报告", "", f"评分规范：{data['specificationVersion']}；架构师版本：{data['architectVersion']}",
                  f"报告 ID：{data['reportId']}；优化条目：{data['targetEntryId'] or '尚未关联'}",
                  f"Description 指纹：{data['snapshot']['descriptionSha256'] or '未验证'}",
                  f"规则指纹：{data['snapshot']['ruleFingerprint'] or '未验证'}", ""])
    for dimension, label in zip(DIMENSIONS, LABELS):
        result = data["dimensions"][dimension]
        lines.extend([f"### {label}", "", f"设计分：{_score(result['design'])}；使用分：{_score(result['usage'])}", ""])
        for name in ("design", "usage"):
            for index, point in enumerate(result[name]["checkpoints"], 1):
                layer_name = "设计" if name == "design" else "使用"
                lines.append(f"- {layer_name}检查点 {index}：{point['status']}；{_cell(point['reason'])}；证据：{_cell('; '.join(point['evidence']) or '未取得')}")
        lines.append("")
    for finding in sorted(data["findings"], key=lambda item: item["priority"]):
        lines.extend([f"### {finding['priority']}: {_cell(finding['impact'])}", "",
                      f"Description 目标：{_cell(finding['target'])}；维度：{finding['dimension']}",
                      "证据：" + _cell("; ".join(finding["evidence"])),
                      f"原因候选：{_cell(finding['cause'])}；归因：{finding['attribution']}（{finding['confidence']}）",
                      "最小改进候选：" + _cell(finding["candidate"]),
                      "验证场景：" + _cell(finding["verification"]), ""])
    if not data["findings"]:
        lines.extend(["没有记录有依据的改进发现。未验证检查仍属于证据缺口。", ""])
    lines.extend(["```scaffold-evaluation", _json(data), "```", ""])
    return "\n".join(lines)


def render_history(history: dict) -> str:
    data = _history(history)
    lines = ["# 脚手架优化与评分历史", "", "仅供人回看；历史分数不作为新评分依据或维护验收门槛。",
             "", f"活动条目：{data['activeEntryId'] or '无'}", ""]
    for number, entry in enumerate(data["entries"], 1):
        lines.extend([f"## 版本 {number}：{entry['entryId']}", "", f"类型：{entry['kind']}；状态：{entry['status']}",
                      f"计划/基线日期：{entry['plannedAt']}；规则结果日期：{entry['modifiedAt'] or '未记录'}",
                      f"修改前指纹：{entry['baselineFingerprint'] or '未验证'}；结果指纹：{entry['ruleFingerprint'] or '未验证'}",
                      "拟修改：" + _cell("; ".join(entry["plannedChanges"]) or "无：已有状态，不宣称优化"),
                      "拟解决问题：" + _cell("; ".join(entry["problems"]) or "未记录"),
                      "实际变化：" + _cell("; ".join(entry["actualChanges"]) or "未记录"),
                      "验证结果：" + _cell("; ".join(entry["validation"]) or "未记录"), ""])
        for score in entry["scores"]:
            summary = "; ".join(f"{LABELS[index]} {_score(score['dimensions'][dimension]['design'])}/{_score(score['dimensions'][dimension]['usage'])}" for index, dimension in enumerate(DIMENSIONS))
            lines.append(f"- 评分日期 {score['evaluatedAt']}；报告 {score['reportId']}；规范 {score['specificationVersion']}；证据 {', '.join(score['evidence']['types']) or '无'}；设计/使用分：{summary}")
        if not entry["scores"]:
            lines.append("尚未评分。")
        lines.append("")
    lines.extend(["```scaffold-score-history", _json(data), "```", ""])
    return "\n".join(lines)


def _atomic_write(root: Path, name: str, content: str, expected: str | None, *, rules: dict | None = None) -> None:
    path = _path(root, name, outcome=True)
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="wb", prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        current = _bytes(root, name, outcome=True)
        if (digest(current) if current is not None else None) != expected:
            raise EvaluationError("BASELINE_CHANGED", "outcome changed before replacement", name)
        if rules is not None and not _same_rules(rules, capture_snapshot(root, rules.get("outputDir"))):
            _fail("rules changed before outcome replacement", "RULES_STALE")
        # ponytail: writers are serialized by the host; add an OS lock if concurrent writers are needed.
        os.replace(temporary, path)
    except OSError as exc:
        raise EvaluationError("WRITE_FAILED", "atomic outcome replacement failed", name) from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _entry(entry_id: str, kind: str, snapshot: dict, at: str) -> dict:
    return {"entryId": entry_id, "kind": kind, "status": "baseline", "plannedAt": at,
            "modifiedAt": None, "baselineFingerprint": snapshot["ruleFingerprint"],
            "ruleFingerprint": snapshot["ruleFingerprint"], "plannedChanges": [],
            "problems": [], "actualChanges": [], "validation": [], "scores": []}


def _associate(history: dict, report: dict) -> dict:
    identity = report.get("targetEntryId") or history["activeEntryId"]
    entry = next((item for item in history["entries"] if item["entryId"] == identity), None)
    if report.get("targetEntryId") and (entry is None or entry["status"] not in ("completed", "baseline") or
                                       entry["ruleFingerprint"] is None or entry["ruleFingerprint"] != report["snapshot"]["ruleFingerprint"]):
        _fail("explicit score target does not match a verified version", "VERSION_MISMATCH")
    if entry is None or entry["status"] not in ("completed", "baseline") or entry["ruleFingerprint"] is None or entry["ruleFingerprint"] != report["snapshot"]["ruleFingerprint"]:
        kind = "benchmark-insufficient" if report["benchmark"]["status"] == "insufficient" else ("unregistered-change" if history["entries"] else "existing-baseline")
        identity = "baseline-" + digest(report["reportId"].encode("utf-8"))
        entry = next((item for item in history["entries"] if item["entryId"] == identity), None)
        if entry is None:
            entry = _entry(identity, kind, report["snapshot"], report["evaluatedAt"])
            history["entries"].append(entry)
        history["activeEntryId"] = identity
    report["targetEntryId"] = identity
    previous = next((item for item in entry["scores"] if item["reportId"] == report["reportId"]), None)
    if previous is not None:
        if previous != report:
            _fail("reportId was already used with different data", "ID_CONFLICT")
    else:
        if any(score["reportId"] == report["reportId"] for item in history["entries"] for score in item["scores"]):
            _fail("reportId already belongs to another version", "ID_CONFLICT")
        entry["scores"].append(copy.deepcopy(report))
    return entry


def write_evaluation(workspace: str | Path, report: dict, *, authorized: bool,
                     baseline: dict | None = None, output_dir: str | None = None) -> dict:
    if authorized is not True:
        return {"status": "cancelled", "reportWritten": False, "historyWritten": False}
    root = _root(workspace)
    data = normalize_report(report)
    if data["specificationVersion"] != SPECIFICATION_VERSION:
        _fail("use the current scoring specification for a new evaluation", "SPECIFICATION_STALE")
    captured = _snapshot(baseline or data["snapshot"])
    directory, paths = _bind_output_dir(captured, output_dir)
    old_report = read_report(root, directory)
    history = read_history(root, directory)
    current = capture_snapshot(root, directory)
    if not _same_rules(data["snapshot"], current):
        _fail("evaluated rules changed; do not save a stale report", "RULES_STALE")
    retry = old_report is not None and old_report["reportId"] == data["reportId"]
    if retry:
        comparable = copy.deepcopy(old_report)
        comparable["targetEntryId"] = data["targetEntryId"]
        if comparable != data:
            _fail("reportId already exists with different data", "ID_CONFLICT")
        saved = next((score for item in history["entries"] for score in item["scores"]
                      if score["reportId"] == data["reportId"]), None)
        if saved is not None:
            if saved != old_report:
                _fail("saved report and history disagree for the same ID", "ID_CONFLICT")
            return {"status": "completed", "reportWritten": True, "historyWritten": True,
                    "reportId": data["reportId"], "entryId": saved["targetEntryId"], "idempotent": True}
        # A previous report-only write can be supplemented without rewriting it.
        _check_baseline(root, captured, outcomes=(paths["history"],))
    else:
        _check_baseline(root, captured)
    if not history["entries"] and old_report is not None and not retry and _same_rules(old_report["snapshot"], current):
        imported = copy.deepcopy(old_report)
        imported["targetEntryId"] = None
        _associate(history, imported)
    if retry and data["targetEntryId"] is None:
        # The deterministic baseline ID also makes an initial partial write retryable.
        data["targetEntryId"] = old_report["targetEntryId"] if old_report["snapshot"]["ruleFingerprint"] is not None and any(item["entryId"] == old_report["targetEntryId"] for item in history["entries"]) else None
    entry = _associate(history, data)
    if not retry:
        _atomic_write(root, paths["report"], render_report(data),
                      current["artifacts"][paths["report"]], rules=data["snapshot"])
    result = {"status": "completed", "reportWritten": True, "historyWritten": False,
              "reportId": data["reportId"], "entryId": entry["entryId"]}
    try:
        _atomic_write(root, paths["history"], render_history(history),
                      current["artifacts"][paths["history"]], rules=data["snapshot"])
        result["historyWritten"] = True
    except EvaluationError as exc:
        result.update(status="partial", error={"code": exc.code, "message": str(exc)})
    return result


def plan_optimization(workspace: str | Path, entry_id: str, planned_changes: list[str],
                      problems: list[str], *, authorized: bool,
                      baseline: dict | None = None, recorded_at: str | None = None,
                      output_dir: str | None = None) -> dict:
    if authorized is not True:
        return {"status": "cancelled", "historyWritten": False}
    root = _root(workspace)
    _identifier(entry_id, "entryId")
    _strings(planned_changes, "plannedChanges", nonempty=True)
    _strings(problems, "problems")
    captured = _snapshot(baseline or capture_snapshot(root, output_dir))
    directory, paths = _bind_output_dir(captured, output_dir)
    history = read_history(root, directory)
    existing = next((item for item in history["entries"] if item["entryId"] == entry_id), None)
    if existing is not None:
        if existing["plannedChanges"] != planned_changes or existing["problems"] != problems or existing["baselineFingerprint"] != captured["ruleFingerprint"]:
            _fail("optimization ID already has a different plan", "ID_CONFLICT")
        return {"status": "completed", "historyWritten": True, "entryId": entry_id,
                "optimizationStatus": existing["status"], "idempotent": True}
    current = _check_baseline(root, captured)
    entry = _entry(entry_id, "optimization", captured, _now(recorded_at))
    entry.update(status="planned", plannedChanges=copy.deepcopy(planned_changes), problems=copy.deepcopy(problems), ruleFingerprint=None)
    history["entries"].append(entry)
    _atomic_write(root, paths["history"], render_history(history),
                  current["artifacts"][paths["history"]], rules=captured)
    return {"status": "completed", "historyWritten": True, "entryId": entry_id, "optimizationStatus": "planned"}


def finish_optimization(workspace: str | Path, entry_id: str, status: str,
                        actual_changes: list[str], validation: list[str], *, authorized: bool,
                        baseline: dict | None = None, recorded_at: str | None = None,
                        output_dir: str | None = None) -> dict:
    if authorized is not True:
        return {"status": "cancelled", "historyWritten": False}
    root = _root(workspace)
    _identifier(entry_id, "entryId")
    if status not in ("completed", "failed", "restored", "no-change"):
        _fail("result must be completed, failed, restored or no-change")
    _strings(actual_changes, "actualChanges", nonempty=status == "completed")
    _strings(validation, "validation", nonempty=True)
    captured = _snapshot(baseline or capture_snapshot(root, output_dir))
    directory, paths = _bind_output_dir(captured, output_dir)
    history = read_history(root, directory)
    entry = next((item for item in history["entries"] if item["entryId"] == entry_id), None)
    if entry is None or entry["kind"] != "optimization":
        _fail("save a confirmed optimization plan before applying rules", "PLAN_MISSING")
    result_fields = {"status": status, "actualChanges": actual_changes, "validation": validation,
                     "ruleFingerprint": captured["ruleFingerprint"]}
    if entry["status"] != "planned":
        if any(entry[field] != value for field, value in result_fields.items()):
            _fail("optimization result already recorded differently", "ID_CONFLICT")
        return {"status": "completed", "historyWritten": True, "entryId": entry_id,
                "optimizationStatus": status, "idempotent": True}
    current = _check_baseline(root, captured)
    same = captured["ruleFingerprint"] == entry["baselineFingerprint"]
    if status == "completed" and (same or captured["ruleFingerprint"] is None):
        _fail("a completed optimization requires a verified substantive rule change", "NO_CHANGE")
    if status in ("restored", "no-change"):
        if captured["ruleFingerprint"] is None or entry["baselineFingerprint"] is None:
            _fail("restored/no-change requires a verifiable original rule baseline", "BASELINE_UNVERIFIED")
        if not same:
            _fail("restored/no-change result must match the original rule baseline", "VERSION_MISMATCH")
    entry.update(copy.deepcopy(result_fields), modifiedAt=_now(recorded_at))
    if status == "completed":
        history["activeEntryId"] = entry_id
    _atomic_write(root, paths["history"], render_history(history),
                  current["artifacts"][paths["history"]], rules=captured)
    return {"status": "completed", "historyWritten": True, "entryId": entry_id, "optimizationStatus": status}


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdin, sys.stdout):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("snapshot", "inspect", "write", "plan", "result"))
    parser.add_argument("workspace")
    parser.add_argument("--input", help="ephemeral request JSON or - for stdin")
    parser.add_argument("--output-dir", help="workspace-relative directory for the report and history")
    parser.add_argument("--authorized", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "snapshot":
            result = capture_snapshot(args.workspace, args.output_dir)
        elif args.command == "inspect":
            result = inspect_report(args.workspace, args.output_dir)
        else:
            if not args.input:
                _fail("write/plan/result requires --input")
            if args.input != "-":
                source = Path(args.input).resolve()
                try:
                    source.relative_to(_root(args.workspace))
                except ValueError:
                    pass
                else:
                    _fail("request JSON must be ephemeral and outside the target workspace; use stdin", "PATH_INVALID")
            payload = json.loads(sys.stdin.read() if args.input == "-" else source.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                _fail("request JSON must be an object")
            if args.command == "write":
                result = write_evaluation(args.workspace, payload, authorized=args.authorized,
                                          output_dir=args.output_dir or ".")
            elif args.command == "plan":
                result = plan_optimization(args.workspace, payload.get("entryId"), payload.get("plannedChanges"), payload.get("problems"),
                                           authorized=args.authorized, baseline=payload.get("baseline"), recorded_at=payload.get("recordedAt"),
                                           output_dir=args.output_dir or ".")
            else:
                result = finish_optimization(args.workspace, payload.get("entryId"), payload.get("status"), payload.get("actualChanges"), payload.get("validation"),
                                             authorized=args.authorized, baseline=payload.get("baseline"), recorded_at=payload.get("recordedAt"),
                                             output_dir=args.output_dir or ".")
        print(_json(result))
        return 1 if result.get("status") in ("partial", "invalid", "unreadable") else 0
    except (EvaluationError, OSError, UnicodeError, ValueError) as exc:
        print(_json({"status": "error", "error": {"code": getattr(exc, "code", "INPUT_INVALID"), "message": str(exc)}}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
