"""Exercise the documented host handoff with real rule plans and artifact writes."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

try:
    from . import agent_evaluation as evaluation
    from .test_agent_distribution import make_formal
    from .test_agent_scaffold import digest, load_maintenance_module
except ImportError:
    import agent_evaluation as evaluation
    from test_agent_distribution import make_formal
    from test_agent_scaffold import digest, load_maintenance_module


class MaintenanceEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="agent-maintenance-evaluation-")
        self.tmp = Path(self.directory.name)
        self.workspace = self.tmp / "assistant"
        make_formal(self.workspace)
        self.maintenance = load_maintenance_module()
        self.maintenance.BACKUP_ROOT = self.tmp / "backups"

    def tearDown(self):
        self.directory.cleanup()

    def rule_plan(self, suffix):
        maintenance = self.maintenance
        entry = (self.workspace / "AGENTS.md").read_text(encoding="utf-8") + suffix
        description = (self.workspace / "scaffold-description.md").read_text(encoding="utf-8")
        manifest = maintenance.load_package_manifest(str(self.workspace))
        manifest.pop("manifestFormat", None)
        manifest["files"]["AGENTS.md"] = digest(entry.encode("utf-8"))
        files = {"AGENTS.md": entry, "scaffold-description.md": description,
                 maintenance.MANIFEST_FILE: json.dumps(manifest)}
        value = {"schemaVersion": 1, "workspace": str(self.workspace),
                 "baseline": {rel: digest((self.workspace / rel).read_bytes()) for rel in files},
                 "files": files, "descriptionSync": "unchanged"}
        path = self.tmp / "rule-plan.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        for operation in ("check", "diff"):
            result, code = maintenance.run(operation, str(self.workspace), str(path), False, None)
            self.assertEqual(code, 0, result)
        self.assertNotIn("scaffold-score-history.md", value["files"])
        self.assertNotIn("scaffold-evaluation.md", value["files"])
        return path

    def plan_record(self, entry_id):
        result = evaluation.plan_optimization(
            self.workspace, entry_id, ["Clarify the authorized entry rule"],
            ["The requested rule is incomplete"], authorized=True,
            baseline=evaluation.capture_snapshot(self.workspace))
        self.assertTrue(result["historyWritten"], result)
        return result

    def finish_record(self, entry_id, status="completed"):
        actual = (["Applied the reviewed entry change"] if status == "completed"
                  else ["Rule write failed; previous rules were retained or restored"])
        validation = (["Maintenance apply and verify succeeded"] if status == "completed"
                      else ["Maintenance apply failed; recovered rules passed verify"])
        result = evaluation.finish_optimization(
            self.workspace, entry_id, status, actual, validation, authorized=True,
            baseline=evaluation.capture_snapshot(self.workspace))
        self.assertTrue(result["historyWritten"], result)
        return result

    def apply_rules(self, plan):
        result, code = self.maintenance.run("apply", str(self.workspace), str(plan), True, None)
        self.assertEqual(code, 0, result)
        verified, code = self.maintenance.run("verify", str(self.workspace), None, False, None)
        self.assertEqual(code, 0, verified)
        return result

    def test_missing_report_allows_planned_apply_verify_and_completed_history(self):
        self.assertEqual(evaluation.inspect_report(self.workspace)["status"], "missing")
        plan = self.rule_plan("\nReturn the precise changed rule and its validation result.\n")
        self.plan_record("first-optimization")
        original_apply = self.maintenance.apply_plan

        def apply_after_plan(*args):
            history = evaluation.read_history(self.workspace)
            self.assertEqual(history["entries"][-1]["status"], "planned")
            self.assertEqual(history["entries"][-1]["entryId"], "first-optimization")
            return original_apply(*args)

        with mock.patch.object(self.maintenance, "apply_plan", side_effect=apply_after_plan):
            self.apply_rules(plan)
        self.finish_record("first-optimization")
        history = evaluation.read_history(self.workspace)
        self.assertEqual(history["activeEntryId"], "first-optimization")
        self.assertEqual(history["entries"][-1]["status"], "completed")
        self.assertFalse((self.workspace / "scaffold-evaluation.md").exists())

    def test_cancelled_or_unchanged_rule_plan_creates_no_success_version(self):
        plan = self.rule_plan("\nA change awaiting confirmation.\n")
        pending, code = self.maintenance.run("apply", str(self.workspace), str(plan), False, None)
        self.assertEqual((code, pending["status"]), (0, "needs-confirmation"))
        cancelled = evaluation.plan_optimization(
            self.workspace, "cancelled", ["Unconfirmed rule"], ["Pending scope"],
            authorized=False, baseline=evaluation.capture_snapshot(self.workspace))
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertFalse((self.workspace / "scaffold-score-history.md").exists())
        # The host skips the optimization record when the substantive rule diff is empty.
        entry = (self.workspace / "AGENTS.md").read_text(encoding="utf-8")
        description = (self.workspace / "scaffold-description.md").read_text(encoding="utf-8")
        value = {"schemaVersion": 1, "workspace": str(self.workspace),
                 "files": {"AGENTS.md": entry, "scaffold-description.md": description},
                 "baseline": {rel: digest((self.workspace / rel).read_bytes())
                              for rel in ("AGENTS.md", "scaffold-description.md")},
                 "descriptionSync": "unchanged"}
        plan.write_text(json.dumps(value), encoding="utf-8")
        diff, code = self.maintenance.run("diff", str(self.workspace), str(plan), False, None)
        self.assertEqual(code, 0, diff)
        self.assertTrue(all(item["action"] == "unchanged" for item in diff["changes"]))
        self.assertFalse((self.workspace / "scaffold-score-history.md").exists())

    def test_plan_record_failure_keeps_rules_and_existing_history(self):
        plan = self.rule_plan("\nA change blocked by a failed history record.\n")
        before = (self.workspace / "AGENTS.md").read_bytes()
        with mock.patch.object(evaluation.os, "replace", side_effect=OSError("plan storage unavailable")):
            with self.assertRaises(evaluation.EvaluationError) as raised:
                self.plan_record("blocked-optimization")
        self.assertEqual(raised.exception.code, "WRITE_FAILED")
        self.assertEqual((self.workspace / "AGENTS.md").read_bytes(), before)
        self.assertFalse((self.workspace / "scaffold-score-history.md").exists())
        self.assertTrue(plan.is_file())

    def test_apply_failure_records_restored_without_replacing_active_version(self):
        first_plan = self.rule_plan("\nA first verified improvement.\n")
        self.plan_record("first-optimization")
        self.apply_rules(first_plan)
        self.finish_record("first-optimization")
        before = (self.workspace / "AGENTS.md").read_bytes()
        second_plan = self.rule_plan("\nA second improvement whose write fails.\n")
        self.plan_record("failed-optimization")
        replace = self.maintenance.os.replace
        failed = False

        def fail_entry_once(source, target):
            nonlocal failed
            if not failed and Path(target).name == "AGENTS.md":
                failed = True
                raise OSError("injected rule write failure")
            return replace(source, target)

        with mock.patch.object(self.maintenance.os, "replace", side_effect=fail_entry_once):
            result, code = self.maintenance.run("apply", str(self.workspace), str(second_plan), True, None)
        self.assertEqual(code, 1, result)
        self.assertEqual(result["workspaceState"], "restored-after-apply-failure")
        self.assertEqual((self.workspace / "AGENTS.md").read_bytes(), before)
        restored, code = self.maintenance.run("verify", str(self.workspace), None, False, None)
        self.assertEqual(code, 0, restored)
        self.finish_record("failed-optimization", "restored")
        history = evaluation.read_history(self.workspace)
        self.assertEqual(history["activeEntryId"], "first-optimization")
        self.assertEqual(history["entries"][-1]["status"], "restored")

    def test_result_record_failure_preserves_successful_rules_and_supports_retry(self):
        plan = self.rule_plan("\nA successful rule change with pending result recording.\n")
        self.plan_record("pending-result")
        self.apply_rules(plan)
        after = (self.workspace / "AGENTS.md").read_bytes()
        history_path = self.workspace / "scaffold-score-history.md"
        before_history = history_path.read_bytes()
        with mock.patch.object(evaluation.os, "replace", side_effect=OSError("result storage unavailable")):
            with self.assertRaises(evaluation.EvaluationError) as raised:
                self.finish_record("pending-result")
        self.assertEqual(raised.exception.code, "WRITE_FAILED")
        self.assertEqual((self.workspace / "AGENTS.md").read_bytes(), after)
        self.assertEqual(history_path.read_bytes(), before_history)
        self.assertEqual(evaluation.read_history(self.workspace)["entries"][-1]["status"], "planned")
        self.finish_record("pending-result")
        self.finish_record("pending-result")
        history = evaluation.read_history(self.workspace)
        self.assertEqual(history["activeEntryId"], "pending-result")
        self.assertEqual(len(history["entries"]), 1)
        self.assertEqual(history["entries"][0]["status"], "completed")

    def test_fixed_artifacts_stay_outside_rule_plan_whitelist(self):
        for name in ("scaffold-evaluation.md", "scaffold-score-history.md"):
            with self.assertRaises(self.maintenance.PlanError) as raised:
                self.maintenance.safe_rel(name)
            self.assertEqual(raised.exception.code, "PLAN_PATH_SCOPE")

    def test_report_inspection_diagnoses_current_stale_and_damaged_without_writes(self):
        layer = evaluation.checkpoint_layer([
            {"status": "unknown", "reason": "No semantic assessment in this protocol fixture", "evidence": []}
            for _ in range(5)])
        report = {"schemaVersion": 1, "specificationVersion": "1", "reportId": "inspection-fixture",
                  "evaluatedAt": "2026-10-13T12:00:00+08:00", "architectVersion": "unknown",
                  "benchmark": {"status": "sufficient", "targets": ["Return a check result"], "gaps": []},
                  "snapshot": evaluation.capture_snapshot(self.workspace),
                  "evidence": {"scope": ["scaffold-description.md"], "gaps": ["No observed tasks"],
                               "types": ["file-review"], "contextTimepoints": [], "driftObserved": False},
                  "dimensions": {dimension: {"design": layer, "usage": layer,
                                  "explanation": "Protocol fixture has no quality score",
                                  "impact": "Semantic evidence remains unverified"}
                                 for dimension in evaluation.DIMENSIONS},
                  "findings": [], "targetEntryId": None}
        saved = evaluation.write_evaluation(self.workspace, report, authorized=True)
        self.assertTrue(saved["reportWritten"], saved)
        path = self.workspace / "scaffold-evaluation.md"
        before = path.read_bytes(), path.stat().st_mtime_ns
        self.assertEqual(evaluation.inspect_report(self.workspace)["status"], "current")
        plan = self.rule_plan("\nA later authorized rule change invalidates old feedback.\n")
        self.plan_record("new-rules")
        self.apply_rules(plan)
        self.finish_record("new-rules")
        self.assertEqual(evaluation.inspect_report(self.workspace)["status"], "stale")
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        path.write_text("Damaged user report without its structured data block.\n", encoding="utf-8")
        damaged = path.read_bytes(), path.stat().st_mtime_ns
        self.assertEqual(evaluation.inspect_report(self.workspace)["status"], "invalid")
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), damaged)


if __name__ == "__main__":
    unittest.main()
