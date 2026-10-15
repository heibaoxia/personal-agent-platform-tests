"""Focused deterministic outcome checks; fixtures are not assistant quality evidence."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

try:
    from . import agent_evaluation as evaluation
except ImportError:
    import agent_evaluation as evaluation


def sample_report(workspace: Path, report_id: str = "score-one", *, insufficient: bool = False,
                  observed: bool = False, output_dir: str | None = None) -> dict:
    def layer(status: str) -> dict:
        return evaluation.checkpoint_layer([
            {"status": status, "reason": "Confirmed evidence" if status == "met" else "Evidence not available",
             "evidence": ["AGENTS.md:1"] if status == "met" else []} for _ in range(5)])
    return {
        "schemaVersion": 1, "specificationVersion": "1", "reportId": report_id,
        "evaluatedAt": "2026-10-13T12:00:00+08:00", "architectVersion": "unknown",
        "benchmark": {"status": "insufficient" if insufficient else "sufficient",
                      "targets": [] if insufficient else ["Review the requested note"],
                      "gaps": ["Description missing or insufficient"] if insufficient else []},
        "snapshot": evaluation.capture_snapshot(workspace, output_dir=output_dir),
        "evidence": {"scope": ["AGENTS.md:1"], "gaps": [] if observed else ["No real task record"],
                     "types": ["file-review", "observation"] if observed else ["file-review"],
                     "contextTimepoints": [], "driftObserved": False},
        "dimensions": {dimension: {"design": layer("unknown" if insufficient else "met"),
                                    "usage": layer("met" if observed and not insufficient else "unknown"),
                                    "explanation": "Fixture explanation", "impact": "Fixture impact"}
                       for dimension in evaluation.DIMENSIONS},
        "findings": [], "targetEntryId": None,
    }


class EvaluationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="agent-evaluation-test-")
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / "workspace"
        self.workspace.mkdir()
        (self.workspace / "AGENTS.md").write_text("# Review assistant\n", encoding="utf-8")
        (self.workspace / "scaffold-description.md").write_text("# Confirmed target\nReview the requested note.\n", encoding="utf-8")

    def write(self, report_id: str = "score-one", *, output_dir: str | None = None, **kwargs) -> dict:
        return evaluation.write_evaluation(
            self.workspace, sample_report(self.workspace, report_id, output_dir=output_dir, **kwargs),
            authorized=True, output_dir=output_dir)

    def optimize(self, identity: str, text: str, *, at: str = "2026-10-13T09:00:00+08:00") -> None:
        evaluation.plan_optimization(self.workspace, identity, ["Clarify rule"], ["Rule gap"],
                                     authorized=True, baseline=evaluation.capture_snapshot(self.workspace), recorded_at=at)
        (self.workspace / "AGENTS.md").write_text(text, encoding="utf-8")
        evaluation.finish_optimization(self.workspace, identity, "completed", ["Clarified rule"], ["Verified target scenario"],
                                       authorized=True, baseline=evaluation.capture_snapshot(self.workspace), recorded_at=at)

    def test_checkpoint_formula_unknown_not_applicable_and_input_score(self) -> None:
        points = [{"status": status, "reason": "Target-based judgment", "evidence": ["Description:1"] if status in evaluation.POINTS else []}
                  for status in ("met", "partial", "partial", "not-applicable", "not-applicable")]
        self.assertEqual(evaluation.checkpoint_layer(points)["score"], 7)
        points[0]["status"] = "unknown"
        self.assertIsNone(evaluation.checkpoint_layer(points)["score"])
        for point in points:
            point["status"] = "not-applicable"
        self.assertEqual(evaluation.checkpoint_layer(points)["status"], "not-applicable")
        report = sample_report(self.workspace)
        report["dimensions"][evaluation.DIMENSIONS[0]]["design"]["score"] = 9
        with self.assertRaises(evaluation.EvaluationError):
            evaluation.normalize_report(report)

    def test_first_write_overwrite_two_audiences_and_only_fixed_outcomes(self) -> None:
        self.assertEqual(self.write()["status"], "completed")
        first_id = evaluation.read_history(self.workspace)["activeEntryId"]
        report = sample_report(self.workspace, "score-two", observed=True)
        self.assertEqual(evaluation.write_evaluation(self.workspace, report, authorized=True)["entryId"], first_id)
        text = (self.workspace / evaluation.REPORT_FILE).read_text(encoding="utf-8")
        self.assertIn("用户报告", text)
        self.assertIn("架构师报告", text)
        self.assertIn("| 目标覆盖 | 10 | 10 |", text)
        self.assertIn("设计分：10；使用分：10", text)
        self.assertEqual(evaluation.read_report(self.workspace)["reportId"], "score-two")
        history = evaluation.read_history(self.workspace)
        self.assertEqual(len(history["entries"]), 1)
        self.assertEqual(len(history["entries"][0]["scores"]), 2)
        self.assertEqual(set(path.name for path in self.workspace.iterdir()),
                         {"AGENTS.md", "scaffold-description.md", *evaluation.OUTCOMES})
        self.assertEqual(text.count("```scaffold-evaluation"), 1)

    def test_cancel_writes_nothing(self) -> None:
        result = evaluation.write_evaluation(self.workspace, {}, authorized=False)
        self.assertEqual(result["status"], "cancelled")
        self.assertFalse((self.workspace / evaluation.REPORT_FILE).exists())
        self.assertEqual(evaluation.plan_optimization(self.workspace, "id", [], [], authorized=False)["status"], "cancelled")

    def test_custom_output_directory_is_bound_to_snapshot_and_has_no_root_duplicates(self) -> None:
        report = sample_report(self.workspace, output_dir="reviews/current")
        paths = report["snapshot"]["outputPaths"]
        self.assertEqual(paths, {"report": "reviews/current/" + evaluation.REPORT_FILE,
                                 "history": "reviews/current/" + evaluation.HISTORY_FILE})
        self.assertEqual(set(report["snapshot"]["artifacts"]), set(paths.values()))
        result = evaluation.write_evaluation(self.workspace, report, authorized=True, output_dir="reviews/current")
        self.assertEqual(result["status"], "completed")
        self.assertTrue((self.workspace / paths["report"]).is_file())
        self.assertTrue((self.workspace / paths["history"]).is_file())
        self.assertFalse((self.workspace / evaluation.REPORT_FILE).exists())
        self.assertFalse((self.workspace / evaluation.HISTORY_FILE).exists())
        self.assertEqual(evaluation.read_report(self.workspace, "reviews/current")["reportId"], "score-one")
        self.assertEqual(evaluation.inspect_report(self.workspace, "reviews/current")["status"], "current")

    def test_snapshot_directory_mismatch_and_unauthorized_write_create_nothing(self) -> None:
        report = sample_report(self.workspace, output_dir="reviews/approved")
        with self.assertRaises(evaluation.EvaluationError) as failure:
            evaluation.write_evaluation(self.workspace, report, authorized=True, output_dir="reviews/other")
        self.assertEqual(failure.exception.code, "OUTPUT_DIR_MISMATCH")
        self.assertFalse((self.workspace / "reviews").exists())

        cancelled = evaluation.write_evaluation(
            self.workspace, report, authorized=False, output_dir="reviews/not-created")
        self.assertEqual(cancelled["status"], "cancelled")
        planned = evaluation.plan_optimization(
            self.workspace, "pending-plan", ["Clarify"], [], authorized=False,
            output_dir="reviews/not-created")
        self.assertEqual(planned["status"], "cancelled")
        finished = evaluation.finish_optimization(
            self.workspace, "pending-plan", "failed", [], ["Cancelled"], authorized=False,
            output_dir="reviews/not-created")
        self.assertEqual(finished["status"], "cancelled")
        self.assertFalse((self.workspace / "reviews").exists())

    def test_plan_and_result_reject_output_directory_changes_from_snapshot(self) -> None:
        output_dir = "confirmed-results"
        snapshot = evaluation.capture_snapshot(self.workspace, output_dir)
        evaluation.plan_optimization(self.workspace, "path-bound-plan", ["Clarify"], [],
                                     authorized=True, baseline=snapshot, output_dir=output_dir)
        for operation in ("plan", "result"):
            with self.subTest(operation=operation):
                try:
                    if operation == "plan":
                        evaluation.plan_optimization(
                            self.workspace, "other-plan", ["Clarify"], [], authorized=True,
                            baseline=snapshot, output_dir="other-results")
                    else:
                        evaluation.finish_optimization(
                            self.workspace, "path-bound-plan", "failed", [], ["Failed"], authorized=True,
                            baseline=snapshot, output_dir="other-results")
                except evaluation.EvaluationError as failure:
                    self.assertEqual(failure.code, "OUTPUT_DIR_MISMATCH")
                else:
                    self.fail("operation accepted an output directory different from its snapshot")
        self.assertFalse((self.workspace / "other-results").exists())

    def test_output_directory_rejects_traversal_absolute_and_symlink_components(self) -> None:
        for value in ("../outside", "nested/../../outside", str(Path(self.temp.name) / "absolute")):
            with self.subTest(value=value):
                with self.assertRaises(evaluation.EvaluationError) as failure:
                    evaluation.capture_snapshot(self.workspace, output_dir=value)
                self.assertEqual(failure.exception.code, "PATH_INVALID")
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        link = self.workspace / "linked-results"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("directory symbolic links are unavailable")
        with self.assertRaises(evaluation.EvaluationError) as failure:
            evaluation.capture_snapshot(self.workspace, output_dir="linked-results")
        self.assertEqual(failure.exception.code, "PATH_INVALID")
        self.assertEqual(list(outside.iterdir()), [])

    def test_legacy_root_snapshot_remains_accepted(self) -> None:
        snapshot = evaluation.capture_snapshot(self.workspace)
        snapshot.pop("outputDir")
        snapshot.pop("outputPaths")
        self.assertEqual(set(snapshot["artifacts"]), set(evaluation.OUTCOMES))
        self.assertEqual(evaluation._snapshot(snapshot), snapshot)

    def test_custom_output_history_baselines_and_plan_result_use_same_directory(self) -> None:
        output_dir = "evaluation-results"
        baseline = evaluation.capture_snapshot(self.workspace, output_dir)
        result = evaluation.plan_optimization(
            self.workspace, "custom-plan", ["Clarify consent"], ["Missing boundary"],
            authorized=True, baseline=baseline, output_dir=output_dir)
        self.assertEqual(result["status"], "completed")
        history = evaluation.read_history(self.workspace, output_dir)
        self.assertEqual(history["entries"][0]["status"], "planned")
        self.assertFalse((self.workspace / evaluation.HISTORY_FILE).exists())
        (self.workspace / "AGENTS.md").write_text("# Updated rule\n", encoding="utf-8")
        current = evaluation.capture_snapshot(self.workspace, output_dir)
        result = evaluation.finish_optimization(
            self.workspace, "custom-plan", "completed", ["Clarified consent"], ["Verified behavior"],
            authorized=True, baseline=current, output_dir=output_dir)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(evaluation.read_history(self.workspace, output_dir)["entries"][0]["status"], "completed")

    def test_cli_accepts_one_output_directory_for_snapshot_inspect_write_plan_and_result(self) -> None:
        cli = Path(evaluation.__file__)
        output_dir = "cli-results"
        snapshot_proc = subprocess.run(
            [sys.executable, str(cli), "snapshot", str(self.workspace), "--output-dir", output_dir],
            cwd=self.workspace, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(snapshot_proc.returncode, 0, snapshot_proc.stderr)
        snapshot = json.loads(snapshot_proc.stdout)
        self.assertEqual(snapshot["outputDir"], output_dir)

        request = Path(self.temp.name) / "write-request.json"
        report = sample_report(self.workspace, output_dir=output_dir)
        request.write_text(json.dumps(report), encoding="utf-8")
        write_proc = subprocess.run(
            [sys.executable, str(cli), "write", str(self.workspace), "--input", str(request),
             "--output-dir", output_dir, "--authorized"],
            cwd=self.workspace, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(write_proc.returncode, 0, write_proc.stderr)
        self.assertEqual(json.loads(write_proc.stdout)["status"], "completed")

        inspect_proc = subprocess.run(
            [sys.executable, str(cli), "inspect", str(self.workspace), "--output-dir", output_dir],
            cwd=self.workspace, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(json.loads(inspect_proc.stdout)["status"], "current")

        plan_request = Path(self.temp.name) / "plan-request.json"
        plan_request.write_text(json.dumps({
            "entryId": "cli-plan", "plannedChanges": ["Clarify"], "problems": [],
            "baseline": evaluation.capture_snapshot(self.workspace, output_dir),
        }), encoding="utf-8")
        plan_proc = subprocess.run(
            [sys.executable, str(cli), "plan", str(self.workspace), "--input", str(plan_request),
             "--output-dir", output_dir, "--authorized"],
            cwd=self.workspace, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(plan_proc.returncode, 0, plan_proc.stderr)
        self.assertEqual(json.loads(plan_proc.stdout)["status"], "completed")

        (self.workspace / "AGENTS.md").write_text("# Applied CLI rule\n", encoding="utf-8")
        result_request = Path(self.temp.name) / "result-request.json"
        result_request.write_text(json.dumps({
            "entryId": "cli-plan", "status": "completed", "actualChanges": ["Clarified"],
            "validation": ["Verified"], "baseline": evaluation.capture_snapshot(self.workspace, output_dir),
        }), encoding="utf-8")
        result_proc = subprocess.run(
            [sys.executable, str(cli), "result", str(self.workspace), "--input", str(result_request),
             "--output-dir", output_dir, "--authorized"],
            cwd=self.workspace, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result_proc.returncode, 0, result_proc.stderr)
        self.assertEqual(json.loads(result_proc.stdout)["optimizationStatus"], "completed")

    def test_missing_description_and_empty_entry_save_diagnostic_without_hashes(self) -> None:
        (self.workspace / "scaffold-description.md").unlink()
        (self.workspace / "AGENTS.md").unlink()
        report = sample_report(self.workspace, insufficient=True)
        self.assertIsNone(report["snapshot"]["ruleFingerprint"])
        self.assertIsNone(report["snapshot"]["descriptionSha256"])
        self.assertEqual(evaluation.write_evaluation(self.workspace, report, authorized=True)["status"], "completed")
        entry = evaluation.read_history(self.workspace)["entries"][0]
        self.assertEqual(entry["kind"], "benchmark-insufficient")
        self.assertEqual(entry["actualChanges"], [])
        self.assertIsNone(entry["modifiedAt"])
        self.assertTrue(all(layer["score"] is None for dimension in entry["scores"][0]["dimensions"].values()
                            for layer in (dimension["design"], dimension["usage"])))

    def test_unreadable_description_diagnostic_and_insufficient_goal(self) -> None:
        (self.workspace / "scaffold-description.md").write_bytes(b"\xff\xfe")
        report = sample_report(self.workspace, insufficient=True)
        self.assertIsNone(report["snapshot"]["descriptionSha256"])
        self.assertEqual(evaluation.write_evaluation(self.workspace, report, authorized=True)["status"], "completed")
        report = sample_report(self.workspace, "score-two", insufficient=True)
        report["benchmark"]["targets"] = ["Invented target"]
        with self.assertRaises(evaluation.EvaluationError):
            evaluation.normalize_report(report)

    def test_corrupt_existing_report_or_history_is_not_erased(self) -> None:
        for name in evaluation.OUTCOMES:
            with self.subTest(name=name):
                target = self.workspace / name
                target.write_text("User content without data block", encoding="utf-8")
                report = sample_report(self.workspace)
                with self.assertRaises(evaluation.EvaluationError):
                    evaluation.write_evaluation(self.workspace, report, authorized=True)
                self.assertEqual(target.read_text(encoding="utf-8"), "User content without data block")
                target.unlink()

    def test_symlink_outcome_and_rule_escape_are_rejected(self) -> None:
        outside = Path(self.temp.name) / "outside.md"
        outside.write_text("External bytes", encoding="utf-8")
        for relative in (evaluation.REPORT_FILE, ".agent/skills/domain.md"):
            link = self.workspace / relative
            link.parent.mkdir(parents=True, exist_ok=True)
            try:
                link.symlink_to(outside)
            except OSError:
                self.skipTest("OS does not allow symbolic links for this account")
            with self.assertRaises(evaluation.EvaluationError) as failure:
                evaluation.capture_snapshot(self.workspace)
            self.assertEqual(failure.exception.code, "PATH_INVALID")
            self.assertEqual(outside.read_text(encoding="utf-8"), "External bytes")
            link.unlink()

    def test_stale_rules_and_outcome_baseline_stop_writes(self) -> None:
        self.write()
        report = sample_report(self.workspace, "score-two")
        original = (self.workspace / evaluation.REPORT_FILE).read_bytes()
        (self.workspace / "AGENTS.md").write_text("# Changed rule\n", encoding="utf-8")
        with self.assertRaises(evaluation.EvaluationError) as failure:
            evaluation.write_evaluation(self.workspace, report, authorized=True)
        self.assertEqual(failure.exception.code, "RULES_STALE")
        self.assertEqual((self.workspace / evaluation.REPORT_FILE).read_bytes(), original)
        report = sample_report(self.workspace, "score-two")
        history = self.workspace / evaluation.HISTORY_FILE
        history.write_bytes(history.read_bytes() + b"\nUser added note\n")
        with self.assertRaises(evaluation.EvaluationError) as failure:
            evaluation.write_evaluation(self.workspace, report, authorized=True)
        self.assertEqual(failure.exception.code, "BASELINE_CHANGED")

    def test_single_file_failure_preserves_old_bytes_and_cleans_temp(self) -> None:
        self.write()
        old = (self.workspace / evaluation.REPORT_FILE).read_bytes()
        report = sample_report(self.workspace, "score-two")
        with patch.object(evaluation.os, "replace", side_effect=OSError("injected replacement failure")):
            with self.assertRaises(evaluation.EvaluationError) as failure:
                evaluation.write_evaluation(self.workspace, report, authorized=True)
        self.assertEqual(failure.exception.code, "WRITE_FAILED")
        self.assertEqual((self.workspace / evaluation.REPORT_FILE).read_bytes(), old)
        self.assertEqual(list(self.workspace.glob("*.tmp")), [])

    def test_partial_history_failure_and_retry_are_explicit_and_idempotent(self) -> None:
        report = sample_report(self.workspace)
        real_replace = evaluation.os.replace
        def fail_history(source, target):
            if Path(target).name == evaluation.HISTORY_FILE:
                raise OSError("injected history failure")
            real_replace(source, target)
        with patch.object(evaluation.os, "replace", side_effect=fail_history):
            result = evaluation.write_evaluation(self.workspace, report, authorized=True)
        self.assertEqual(result["status"], "partial")
        self.assertTrue(result["reportWritten"])
        self.assertFalse(result["historyWritten"])
        self.assertEqual(evaluation.read_report(self.workspace)["reportId"], report["reportId"])
        before = (self.workspace / evaluation.REPORT_FILE).stat().st_mtime_ns
        self.assertEqual(evaluation.write_evaluation(self.workspace, report, authorized=True)["status"], "completed")
        self.assertEqual((self.workspace / evaluation.REPORT_FILE).stat().st_mtime_ns, before)
        self.assertTrue(evaluation.write_evaluation(self.workspace, report, authorized=True)["idempotent"])
        retry = copy.deepcopy(report)
        retry["snapshot"]["artifacts"] = evaluation.capture_snapshot(self.workspace)["artifacts"]
        # New request data for an existing ID is rejected rather than silently changed.
        with self.assertRaises(evaluation.EvaluationError) as failure:
            evaluation.write_evaluation(self.workspace, retry, authorized=True)
        self.assertEqual(failure.exception.code, "ID_CONFLICT")
        self.assertEqual(len(evaluation.read_history(self.workspace)["entries"][0]["scores"]), 1)

    def test_two_optimizations_without_scores_then_multiple_late_scores(self) -> None:
        self.optimize("version-one", "# Rule one\n")
        self.optimize("version-two", "# Rule two\n", at="2026-10-13T10:00:00+08:00")
        history = evaluation.read_history(self.workspace)
        self.assertEqual([entry["scores"] for entry in history["entries"]], [[], []])
        self.assertEqual(self.write()["entryId"], "version-two")
        self.assertEqual(self.write("score-two", observed=True)["entryId"], "version-two")
        history = evaluation.read_history(self.workspace)
        self.assertEqual(len(history["entries"]), 2)
        self.assertEqual([len(entry["scores"]) for entry in history["entries"]], [0, 2])
        self.assertEqual(history["entries"][1]["modifiedAt"], "2026-10-13T10:00:00+08:00")
        self.assertEqual(history["entries"][1]["scores"][0]["evaluatedAt"], "2026-10-13T12:00:00+08:00")

    def test_same_fingerprint_new_active_version_and_explicit_old_version(self) -> None:
        self.optimize("version-one", "# Rule one\n")
        self.write("old-score")
        self.optimize("version-two", "# Rule two\n")
        self.optimize("version-three", "# Rule one\n")
        self.assertEqual(self.write("new-score")["entryId"], "version-three")
        history = evaluation.read_history(self.workspace)
        self.assertEqual(history["entries"][0]["ruleFingerprint"], history["entries"][2]["ruleFingerprint"])
        self.assertEqual([len(entry["scores"]) for entry in history["entries"]], [1, 0, 1])
        report = sample_report(self.workspace, "late-score")
        report["targetEntryId"] = "version-one"
        self.assertEqual(evaluation.write_evaluation(self.workspace, report, authorized=True)["entryId"], "version-one")
        self.assertEqual(evaluation.read_history(self.workspace)["activeEntryId"], "version-three")

    def test_old_report_import_requires_current_source_snapshot(self) -> None:
        self.write("old-score")
        (self.workspace / evaluation.HISTORY_FILE).unlink()
        self.write("new-score")
        history = evaluation.read_history(self.workspace)
        self.assertEqual([score["reportId"] for score in history["entries"][0]["scores"]], ["old-score", "new-score"])
        (self.workspace / evaluation.HISTORY_FILE).unlink()
        (self.workspace / "AGENTS.md").write_text("# Unregistered new rule\n", encoding="utf-8")
        self.write("new-source-score")
        self.assertEqual([score["reportId"] for score in evaluation.read_history(self.workspace)["entries"][0]["scores"]], ["new-source-score"])

    def test_unregistered_change_retains_previous_history(self) -> None:
        self.write()
        (self.workspace / "AGENTS.md").write_text("# User changed rule\n", encoding="utf-8")
        self.write("score-two")
        history = evaluation.read_history(self.workspace)
        self.assertEqual(len(history["entries"]), 2)
        self.assertEqual(history["entries"][1]["kind"], "unregistered-change")
        self.assertEqual(history["entries"][1]["actualChanges"], [])

    def test_restore_and_no_change_do_not_create_successful_version(self) -> None:
        self.optimize("version-one", "# Effective rule\n")
        evaluation.plan_optimization(self.workspace, "failed-plan", ["Attempt change"], [], authorized=True)
        evaluation.finish_optimization(self.workspace, "failed-plan", "restored", [], ["Original bytes restored"], authorized=True)
        history = evaluation.read_history(self.workspace)
        self.assertEqual(history["activeEntryId"], "version-one")
        self.assertEqual(history["entries"][1]["status"], "restored")
        evaluation.plan_optimization(self.workspace, "same-plan", ["Attempt ineffective change"], [], authorized=True)
        with self.assertRaises(evaluation.EvaluationError) as failure:
            evaluation.finish_optimization(self.workspace, "same-plan", "completed", ["Claimed change"], ["Claimed verification"], authorized=True)
        self.assertEqual(failure.exception.code, "NO_CHANGE")
        evaluation.finish_optimization(self.workspace, "same-plan", "no-change", [], ["No substantive diff"], authorized=True)
        self.assertEqual(evaluation.read_history(self.workspace)["activeEntryId"], "version-one")

    def test_plan_write_failure_blocks_and_result_failure_is_retryable(self) -> None:
        with patch.object(evaluation.os, "replace", side_effect=OSError("injected failure")):
            with self.assertRaises(evaluation.EvaluationError):
                evaluation.plan_optimization(self.workspace, "plan-one", ["Clarify"], [], authorized=True)
        self.assertEqual(evaluation.read_history(self.workspace)["entries"], [])
        original_snapshot = evaluation.capture_snapshot(self.workspace)
        evaluation.plan_optimization(self.workspace, "plan-one", ["Clarify"], [], authorized=True, baseline=original_snapshot)
        self.assertTrue(evaluation.plan_optimization(self.workspace, "plan-one", ["Clarify"], [], authorized=True, baseline=original_snapshot)["idempotent"])
        (self.workspace / "AGENTS.md").write_text("# Applied rules remain applied\n", encoding="utf-8")
        snapshot = evaluation.capture_snapshot(self.workspace)
        with patch.object(evaluation.os, "replace", side_effect=OSError("injected result failure")):
            with self.assertRaises(evaluation.EvaluationError):
                evaluation.finish_optimization(self.workspace, "plan-one", "completed", ["Clarified"], ["Verified"], authorized=True, baseline=snapshot)
        self.assertEqual(evaluation.read_history(self.workspace)["entries"][0]["status"], "planned")
        self.assertIn("remain applied", (self.workspace / "AGENTS.md").read_text(encoding="utf-8"))
        evaluation.finish_optimization(self.workspace, "plan-one", "completed", ["Clarified"], ["Verified"], authorized=True, baseline=snapshot)
        self.assertTrue(evaluation.finish_optimization(self.workspace, "plan-one", "completed", ["Clarified"], ["Verified"], authorized=True)["idempotent"])
        self.assertEqual(len(evaluation.read_history(self.workspace)["entries"]), 1)

    def test_protected_tools_and_context_data_are_not_rule_fingerprints(self) -> None:
        tools = self.workspace / ".agent/architect/tools"
        tools.mkdir(parents=True)
        tool = tools / "tool.py"
        tool.write_text("old", encoding="utf-8")
        context = self.workspace / ".agent/context/current.md"
        context.parent.mkdir()
        context.write_text("old context", encoding="utf-8")
        snapshot = evaluation.capture_snapshot(self.workspace)
        tool.write_text("new", encoding="utf-8")
        context.write_text("new context", encoding="utf-8")
        self.assertEqual(evaluation.capture_snapshot(self.workspace)["ruleFingerprint"], snapshot["ruleFingerprint"])

    def test_inspection_and_current_context_cannot_claim_observed_drift(self) -> None:
        self.assertEqual(evaluation.inspect_report(self.workspace)["status"], "missing")
        self.write()
        self.assertEqual(evaluation.inspect_report(self.workspace)["status"], "current")
        (self.workspace / "AGENTS.md").write_text("# Changed\n", encoding="utf-8")
        self.assertEqual(evaluation.inspect_report(self.workspace)["status"], "stale")
        report = sample_report(self.workspace, "score-two", observed=True)
        report["evidence"].update(driftObserved=True, contextTimepoints=["2026-10-13T10:00:00+08:00"])
        with self.assertRaises(evaluation.EvaluationError):
            evaluation.normalize_report(report)
        (self.workspace / evaluation.REPORT_FILE).write_text("Damaged", encoding="utf-8")
        self.assertEqual(evaluation.inspect_report(self.workspace)["status"], "invalid")

    def test_cli_uses_stdin_and_writes_no_independent_json(self) -> None:
        command = [sys.executable, str(Path(evaluation.__file__)), "write", str(self.workspace), "--input", "-", "--authorized"]
        proc = subprocess.run(command, input=json.dumps(sample_report(self.workspace)).encode("utf-8"), capture_output=True)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        self.assertEqual(json.loads(proc.stdout)["status"], "completed")
        self.assertEqual(list(self.workspace.rglob("*.json")), [])

    def test_severe_findings_are_visible_to_both_audiences(self) -> None:
        report = sample_report(self.workspace)
        report["findings"] = [{"priority": "P1", "dimension": "rule-consistency", "target": "Confirmed review target",
                               "evidence": ["AGENTS.md:1"], "impact": "Required authorization is missing",
                               "cause": "Rule omission", "attribution": "target-scaffold", "confidence": "high",
                               "candidate": "Declare the authorization boundary", "verification": "Attempt an unauthorized write"}]
        text = evaluation.render_report(report)
        user, architect = text.split("## 架构师报告", 1)
        self.assertIn("严重问题与影响", user)
        self.assertIn("Required authorization is missing", user)
        self.assertIn("Required authorization is missing", architect)

    def test_rules_or_outcomes_changed_while_preparing_replacement_are_detected(self) -> None:
        report = sample_report(self.workspace)
        fsync = evaluation.os.fsync
        def change_rules(handle):
            fsync(handle)
            (self.workspace / "AGENTS.md").write_text("# Changed during write\n", encoding="utf-8")
        with patch.object(evaluation.os, "fsync", side_effect=change_rules):
            with self.assertRaises(evaluation.EvaluationError) as failure:
                evaluation.write_evaluation(self.workspace, report, authorized=True)
        self.assertEqual(failure.exception.code, "RULES_STALE")
        self.assertFalse((self.workspace / evaluation.REPORT_FILE).exists())
        self.assertEqual(list(self.workspace.glob("*.tmp")), [])
        report = sample_report(self.workspace)
        def change_report(handle):
            fsync(handle)
            (self.workspace / evaluation.REPORT_FILE).write_text("Concurrent user content", encoding="utf-8")
        with patch.object(evaluation.os, "fsync", side_effect=change_report):
            with self.assertRaises(evaluation.EvaluationError) as failure:
                evaluation.write_evaluation(self.workspace, report, authorized=True)
        self.assertEqual(failure.exception.code, "BASELINE_CHANGED")
        self.assertEqual((self.workspace / evaluation.REPORT_FILE).read_text(encoding="utf-8"), "Concurrent user content")

    def test_timezone_alias_is_not_two_context_timepoints(self) -> None:
        report = sample_report(self.workspace, observed=True)
        report["evidence"].update(driftObserved=True, contextTimepoints=["2026-10-13T12:00:00+08:00", "2026-10-13T04:00:00Z"])
        with self.assertRaises(evaluation.EvaluationError):
            evaluation.normalize_report(report)

    def test_protected_manifest_cannot_omit_description_or_entry(self) -> None:
        directory = self.workspace / ".agent"
        directory.mkdir()
        (directory / "scaffold-manifest.json").write_text(json.dumps({"protectedFiles": ["AGENTS.md", "scaffold-description.md"]}), encoding="utf-8")
        with self.assertRaises(evaluation.EvaluationError) as failure:
            evaluation.capture_snapshot(self.workspace)
        self.assertEqual(failure.exception.code, "SNAPSHOT_INVALID")
        self.assertTrue((self.workspace / "AGENTS.md").exists())
        self.assertTrue((self.workspace / "scaffold-description.md").exists())

    def test_arbitrary_protected_claim_cannot_hide_changed_user_rule(self) -> None:
        directory = self.workspace / ".agent"
        (directory / "skills").mkdir(parents=True)
        rule = directory / "skills/quiet-check.md"
        rule.write_text("# User rule\n", encoding="utf-8")
        report = sample_report(self.workspace)
        self.assertIn(".agent/skills/quiet-check.md", report["snapshot"]["files"])
        (directory / "scaffold-manifest.json").write_text(json.dumps({"protectedFiles": [".agent/skills/quiet-check.md"]}), encoding="utf-8")
        rule.write_text("# User rule changed\n", encoding="utf-8")
        with self.assertRaises(evaluation.EvaluationError) as failure:
            evaluation.write_evaluation(self.workspace, report, authorized=True)
        self.assertEqual(failure.exception.code, "SNAPSHOT_INVALID")
        self.assertFalse((self.workspace / evaluation.REPORT_FILE).exists())

    def test_old_specification_is_stale_and_preserved_when_importing_history(self) -> None:
        report = sample_report(self.workspace, "old-spec-score")
        report["specificationVersion"] = "previous"
        (self.workspace / evaluation.REPORT_FILE).write_text(evaluation.render_report(report), encoding="utf-8")
        self.assertEqual(evaluation.inspect_report(self.workspace)["status"], "stale")
        with self.assertRaises(evaluation.EvaluationError) as failure:
            evaluation.write_evaluation(self.workspace, report, authorized=True)
        self.assertEqual(failure.exception.code, "SPECIFICATION_STALE")
        self.write("current-spec-score")
        scores = evaluation.read_history(self.workspace)["entries"][0]["scores"]
        self.assertEqual([score["specificationVersion"] for score in scores], ["previous", "1"])

    def test_history_missing_field_is_diagnosed_and_not_erased(self) -> None:
        self.write()
        history = evaluation.read_history(self.workspace)
        del history["entries"][0]["modifiedAt"]
        target = self.workspace / evaluation.HISTORY_FILE
        text = "```scaffold-score-history\n" + json.dumps(history) + "\n```\n"
        target.write_text(text, encoding="utf-8")
        with self.assertRaises(evaluation.EvaluationError) as failure:
            evaluation.read_history(self.workspace)
        self.assertEqual(failure.exception.code, "FORMAT_INVALID")
        self.assertEqual(target.read_text(encoding="utf-8"), text)

    def test_cli_rejects_request_file_inside_target(self) -> None:
        request = self.workspace / "request.json"
        request.write_text(json.dumps(sample_report(self.workspace)), encoding="utf-8")
        command = [sys.executable, str(Path(evaluation.__file__)), "write", str(self.workspace), "--input", str(request), "--authorized"]
        proc = subprocess.run(command, capture_output=True)
        self.assertEqual(json.loads(proc.stdout)["error"]["code"], "PATH_INVALID")
        self.assertFalse((self.workspace / evaluation.REPORT_FILE).exists())

    def test_crlf_report_and_history_blocks_are_accepted(self) -> None:
        self.write()
        for name in evaluation.OUTCOMES:
            target = self.workspace / name
            target.write_bytes(target.read_bytes().replace(b"\n", b"\r\n"))
        self.assertEqual(evaluation.inspect_report(self.workspace)["status"], "current")
        self.assertEqual(len(evaluation.read_history(self.workspace)["entries"]), 1)
        self.write("score-two")
        self.assertEqual(len(evaluation.read_history(self.workspace)["entries"][0]["scores"]), 2)

    def test_history_missing_active_id_is_structured_error(self) -> None:
        target = self.workspace / evaluation.HISTORY_FILE
        text = '```scaffold-score-history\n{"schemaVersion":1,"entries":[]}\n```\n'
        target.write_text(text, encoding="utf-8")
        with self.assertRaises(evaluation.EvaluationError) as failure:
            self.write()
        self.assertEqual(failure.exception.code, "FORMAT_INVALID")
        self.assertEqual(target.read_text(encoding="utf-8"), text)
        self.assertFalse((self.workspace / evaluation.REPORT_FILE).exists())

    def test_unknown_fingerprints_cannot_claim_restoration_or_no_change(self) -> None:
        (self.workspace / "scaffold-description.md").unlink()
        evaluation.plan_optimization(self.workspace, "unknown-plan", ["Clarify"], [], authorized=True)
        (self.workspace / "AGENTS.md").write_text("# Actually changed\n", encoding="utf-8")
        for status in ("restored", "no-change"):
            with self.subTest(status=status):
                with self.assertRaises(evaluation.EvaluationError) as failure:
                    evaluation.finish_optimization(self.workspace, "unknown-plan", status, [], ["Cannot prove baseline"], authorized=True)
                self.assertEqual(failure.exception.code, "BASELINE_UNVERIFIED")
        evaluation.finish_optimization(self.workspace, "unknown-plan", "failed", ["Rule result unverified"], ["Description missing"], authorized=True)
        self.assertEqual(evaluation.read_history(self.workspace)["entries"][0]["status"], "failed")

    def test_unavailable_snapshot_hashes_must_be_explicit_null(self) -> None:
        (self.workspace / "scaffold-description.md").unlink()
        report = sample_report(self.workspace, insufficient=True)
        for field in ("ruleFingerprint", "descriptionSha256"):
            damaged = copy.deepcopy(report)
            del damaged["snapshot"][field]
            with self.subTest(field=field):
                with self.assertRaises(evaluation.EvaluationError) as failure:
                    evaluation.write_evaluation(self.workspace, damaged, authorized=True)
                self.assertEqual(failure.exception.code, "INPUT_INVALID")
                self.assertFalse((self.workspace / evaluation.REPORT_FILE).exists())


if __name__ == "__main__":
    unittest.main()
