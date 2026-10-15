"""Focused checks for standalone Skill publication boundaries."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import zipfile

try:
    from . import publish_architect_skill as publish
except ImportError:
    import publish_architect_skill as publish


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.contents = {"SKILL.md": b"---\nname: standalone-skill\n---\nstandalone skill\n", "tools/agent_scaffold.py": b'TOOL_VERSION = "2.0.0"\n',
                         "tools/validate_scaffold.py": b"validator\n", "tools/agent_evaluation.py": b"evaluation tool\n",
                         "tools/agent_asset_migration.py": b"migration tool\n",
                         "tools/scaffold-evaluate.md": b"---\nname: scaffold-evaluate\n---\nevaluation rules\n"}
        self.source = "a" * 40
        self.head = "b" * 40
        self.metadata = publish.release_bytes(self.contents, "2.0.0", self.source)
        self.remote = {"head": self.head, "tree": "c" * 40,
                       "files": {path: publish.blob_sha(data) for path, data in self.contents.items()},
                       "metadata": json.loads(self.metadata)}
        self.remote["files"]["tools/release.json"] = publish.blob_sha(self.metadata)

    def test_source_snapshot_contains_evaluation_and_excludes_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(publish, "ROOT", Path(temporary)):
            for path, relative in publish.SOURCES.items():
                source = Path(temporary) / relative
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_bytes(self.contents[path])
            (Path(temporary) / "private.txt").write_text("do not publish", encoding="utf-8")
            for name in ("scaffold-evaluation.md", "scaffold-score-history.md"):
                (Path(temporary) / name).write_text("private artifact", encoding="utf-8")
            self.assertEqual(publish.snapshot(), self.contents)

    def test_legacy_remote_sources_can_upgrade_to_evaluation(self):
        paths = ("SKILL.md", "tools/agent_scaffold.py", "tools/validate_scaffold.py", "tools/release.json")
        entries = [{"path": path, "type": "blob", "mode": "100644", "sha": self.remote["files"][path]} for path in paths]
        self.assertEqual(set(publish.tree_files({"tree": entries})), set(paths))

    def test_remote_scope_rejects_extras_and_submodules(self):
        entries = [{"path": path, "type": "blob", "mode": "100644", "sha": sha}
                   for path, sha in self.remote["files"].items()]
        self.assertEqual(set(publish.tree_files({"tree": entries})), publish.FILES)
        for entry in ({"path": "private.txt", "type": "blob", "mode": "100644", "sha": self.head},
                      {"path": "tools/agent_scaffold.py", "type": "commit", "mode": "160000", "sha": self.head}):
            with self.assertRaisesRegex(ValueError, "outside"):
                publish.tree_files({"tree": [*entries, entry]})

    def test_unchanged_push_is_read_only_and_reuses_verified_source(self):
        with mock.patch.object(publish, "remote_state", return_value=self.remote), \
             mock.patch.object(publish, "verify_source") as verified, \
             mock.patch.object(publish, "api") as api:
            result = publish.push(self.contents)
        verified.assert_called_once_with(self.source, self.contents)
        api.assert_not_called()
        self.assertEqual(result["changedFiles"], [])
        self.assertEqual(result["sourceCommit"], self.source)
        self.assertEqual(result["commit"], self.head)

    def test_source_commit_must_match_public_source_bytes(self):
        with mock.patch.object(publish, "commit_files", return_value=("tree", self.remote["files"])):
            publish.verify_source(self.source, self.contents)
            with self.assertRaisesRegex(ValueError, "does not match"):
                publish.verify_source(self.source, {**self.contents, "SKILL.md": b"changed"})
            with self.assertRaisesRegex(ValueError, "public sourceCommit"):
                publish.verify_source("unknown", self.contents)

    def test_source_and_metadata_commit_use_one_nonforce_ref_update(self):
        changed = {**self.contents, "SKILL.md": b"updated skill\n"}
        source, final = "d" * 40, "e" * 40
        with mock.patch.object(publish, "remote_state", return_value=self.remote), \
             mock.patch.object(publish, "commit_changes", side_effect=[(source, "source-tree"), (final, "final-tree")]) as commits, \
             mock.patch.object(publish, "api", return_value={"object": {"sha": self.head}}) as api:
            result = publish.push(changed)
        self.assertEqual(commits.call_args_list[0].args[2], {"SKILL.md": changed["SKILL.md"]})
        metadata_call = commits.call_args_list[1].args
        self.assertEqual(metadata_call[0], source)
        self.assertEqual(json.loads(metadata_call[2]["tools/release.json"])["sourceCommit"], source)
        api.assert_has_calls([mock.call("git/ref/heads/main"),
                              mock.call("git/refs/heads/main", {"sha": final, "force": False})])
        self.assertEqual(result["commit"], final)

    def test_concurrent_ref_change_blocks_update_and_dry_run_writes_nothing(self):
        changed = {**self.contents, "SKILL.md": b"updated skill\n"}
        with mock.patch.object(publish, "remote_state", return_value=self.remote), \
             mock.patch.object(publish, "commit_changes", return_value=("d" * 40, "tree")), \
             mock.patch.object(publish, "api", return_value={"object": {"sha": "f" * 40}}) as api:
            with self.assertRaisesRegex(ValueError, "main changed"):
                publish.push(changed)
        self.assertEqual(api.call_count, 1)
        with mock.patch.object(publish, "remote_state", return_value=self.remote), \
             mock.patch.object(publish, "commit_changes") as commits, mock.patch.object(publish, "api") as api:
            result = publish.push(changed, dry_run=True)
        commits.assert_not_called()
        api.assert_not_called()
        self.assertEqual(result["changedFiles"], ["SKILL.md", "tools/release.json"])

    def test_release_version_and_public_asset_are_immutable(self):
        latest = {"tag_name": "v2.0.4"}
        self.assertEqual(publish.selected_version(self.contents, None, latest), "2.0.5")
        for version in ("2.0.4", "1.9.9", "02.0.5"):
            with self.assertRaises(ValueError):
                publish.selected_version(self.contents, version, latest)
        with mock.patch.object(publish, "latest_release", return_value=latest), \
             mock.patch.object(publish, "api", return_value={"draft": False}), \
             mock.patch.object(publish, "push") as pushed:
            with self.assertRaisesRegex(ValueError, "cannot be overwritten"):
                publish.release(self.contents, "2.0.5", None)
        pushed.assert_not_called()
        with self.assertRaisesRegex(ValueError, "different SHA-256"):
            publish.verify_assets({"assets": [{"name": "asset.zip", "digest": "sha256:wrong"}]},
                                  {"asset.zip": "a" * 64})

    def test_draft_lookup_uses_release_list_when_tag_endpoint_hides_drafts(self):
        draft = {"id": 123, "tag_name": "v3.1.0", "draft": True}
        with mock.patch.object(publish, "api", return_value=[{"tag_name": "v3.0.0", "draft": False}, draft]) as api:
            self.assertEqual(publish.draft_release("v3.1.0"), draft)
        api.assert_called_once_with("releases?per_page=100")
        with mock.patch.object(publish, "api", return_value=[]) as api:
            self.assertIsNone(publish.draft_release("v3.1.0"))
        with mock.patch.object(publish, "api", return_value=[draft, {**draft, "id": 456}]):
            with self.assertRaisesRegex(ValueError, "multiple draft"):
                publish.draft_release("v3.1.0")

    def test_status_and_dry_run_do_not_sync_or_run_validation(self):
        for arguments in (["status"], ["push", "--dry-run"], ["release", "--dry-run"]):
            with mock.patch.object(publish, "snapshot", return_value=self.contents), \
                 mock.patch.object(publish, "push", return_value={}), \
                 mock.patch.object(publish, "release", return_value={}), \
                 mock.patch.object(publish, "latest_release", return_value=None), \
                 mock.patch.object(publish, "validate_before_publication") as validate, \
                 mock.patch("builtins.print"):
                self.assertEqual(publish.main(arguments), 0)
            validate.assert_not_called()

    def test_draft_retry_verifies_snapshot_and_uploads_only_missing_assets(self):
        for missing_skill in (True, False):
            with self.subTest(missing_skill=missing_skill), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                stage = root / ".osx-probe/releases/2.0.1"
                skill = stage / "skill"
                metadata = publish.release_bytes(self.contents, "2.0.1", self.source)
                expected = {**self.contents, "tools/release.json": metadata}
                publication = {"repository": publish.REPOSITORY, "bundleVersion": "2.0.1",
                               "sourceCommit": self.source, "commit": "e" * 40, "status": "ok"}
                for path, data in expected.items():
                    publish.write_new_or_same(skill / path, data)
                publish.write_new_or_same(stage / "publication.json",
                                          (json.dumps(publication, indent=2, sort_keys=True) + "\n").encode("utf-8"))
                assets = publish.staged_assets(stage, skill, "2.0.1", self.source)
                skill_asset = "agent-scaffold-maintenance-2.0.1.zip"
                uploaded = [{"name": name, "digest": "sha256:" + digest}
                            for name, digest in reversed(list(assets.items()))]
                draft = {"id": 789, "tag_name": "v2.0.1", "draft": True, "prerelease": False, "target_commitish": publication["commit"],
                         "assets": [item for item in uploaded if not missing_skill or item["name"] != skill_asset]}
                complete = {**draft, "assets": uploaded}
                published = {**complete, "draft": False, "tag_name": "v2.0.1", "html_url": "https://example.com/release"}
                target_files = {path: publish.blob_sha(data) for path, data in expected.items()}
                with mock.patch.object(publish, "ROOT", root), \
                     mock.patch.object(publish, "latest_release", side_effect=[{"tag_name": "v2.0.0"}, published]), \
                     mock.patch.object(publish, "api", side_effect=[None, [draft], complete]) as api, \
                     mock.patch.object(publish, "tag_target", return_value=None), \
                     mock.patch.object(publish, "commit_files", return_value=("tree", target_files)) as commits, \
                     mock.patch.object(publish, "push") as pushed, \
                     mock.patch.object(publish, "gh", return_value="") as gh:
                    result = publish.release(self.contents, "2.0.1", None)
                pushed.assert_not_called()
                api.assert_has_calls([
                    mock.call("releases/tags/v2.0.1", allow_missing=True),
                    mock.call("releases?per_page=100"),
                    mock.call("releases/789"),
                ])
                commits.assert_has_calls([mock.call(self.source), mock.call(publication["commit"])])
                commands = [call.args[0] for call in gh.call_args_list]
                self.assertEqual(len(commands), 2 if missing_skill else 1)
                if missing_skill:
                    self.assertEqual(commands[0], ["release", "upload", "v2.0.1", str(stage / skill_asset),
                                                   "--repo", publish.REPOSITORY])
                self.assertEqual(commands[-1], ["release", "edit", "v2.0.1", "--repo", publish.REPOSITORY,
                                                "--draft=false", "--latest"])
                self.assertEqual(result["assets"], assets)
                with zipfile.ZipFile(stage / skill_asset) as archive:
                    self.assertEqual(set(archive.namelist()), publish.FILES)
                    self.assertEqual(json.loads(archive.read("tools/release.json"))["sourceCommit"], self.source)

    def test_incomplete_or_unexpected_asset_blocks_finalization(self):
        assets = {"skill.zip": "a" * 64, "runtime.zip": "b" * 64, "manifest.json": "c" * 64}
        partial = {"assets": [{"name": "skill.zip", "digest": "sha256:" + assets["skill.zip"]}]}
        publish.verify_assets(partial, assets)
        with self.assertRaisesRegex(ValueError, "missing"):
            publish.verify_assets(partial, assets, complete=True)
        with self.assertRaisesRegex(ValueError, "unexpected"):
            publish.verify_assets({"assets": [{"name": "private.txt", "digest": "sha256:" + "d" * 64}]}, assets)


if __name__ == "__main__":
    unittest.main()
