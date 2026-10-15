"""Canonical Release metadata, preflight trust and redirect regressions."""

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import urllib.request

try:
    from .test_agent_distribution import make_formal
    from .test_agent_scaffold import load_maintenance_module
    from . import release_architect_bundle
except ImportError:
    from test_agent_distribution import make_formal
    from test_agent_scaffold import load_maintenance_module
    import release_architect_bundle


class ReleaseSecurityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.cli = load_maintenance_module()
        self.target = self.root / "target"
        make_formal(self.target, version="1.0.0", redacted=True)
        self.bundle = self.root / "bundle"
        self.archive = self.root / "architect-bundle-2.0.0.zip"
        release_architect_bundle.build(str(self.bundle), "2.0.0", "a" * 40, str(self.archive))
        self.metadata = {
            "id": 411226017, "tag_name": "v2.0.0", "draft": False, "prerelease": False,
            "html_url": "https://github.com/heibaoxia/My-Personal-Agent/releases/tag/v2.0.0",
            "url": "https://api.github.com/repos/heibaoxia/My-Personal-Agent/releases/411226017",
            "assets": [{"name": self.archive.name,
                        "browser_download_url": "https://github.com/heibaoxia/My-Personal-Agent/releases/download/v2.0.0/" + self.archive.name,
                        "digest": "sha256:" + self.cli.sha256_bytes(self.archive.read_bytes())}],
        }

    def tearDown(self):
        self.directory.cleanup()

    def candidate(self, metadata):
        with tempfile.TemporaryDirectory() as stage, mock.patch.object(
            self.cli, "_bounded_url_bytes", side_effect=[json.dumps(metadata).encode(), self.archive.read_bytes()]
        ):
            return self.cli._release_candidate(None, stage)[1]

    def test_real_github_release_metadata_shape_is_accepted(self):
        result = self.candidate(self.metadata)
        self.assertEqual(result["manifest"]["bundleVersion"], "2.0.0")
        self.assertTrue(result["verified"])

    def test_forged_repository_and_wrong_asset_version_are_rejected(self):
        for patch in (
            {"url": "https://api.github.com/repos/forged/repository/releases/411226017"},
            {"html_url": "https://github.com/heibaoxia/My-Personal-Agent-fake/releases/tag/v2.0.0"},
            {"draft": True}, {"prerelease": True},
            {"assets": [{**self.metadata["assets"][0], "browser_download_url":
                        "https://github.com/heibaoxia/My-Personal-Agent/releases/download/v9.0.0/" + self.archive.name}]},
        ):
            with self.subTest(patch=patch), self.assertRaises(self.cli.PlanError):
                self.candidate({**self.metadata, **patch})
        metadata = {**self.metadata, "tag_name": "v3.0.0",
                    "html_url": "https://github.com/heibaoxia/My-Personal-Agent/releases/tag/v3.0.0",
                    "assets": [{"name": "architect-bundle-3.0.0.zip", "browser_download_url":
                                "https://github.com/heibaoxia/My-Personal-Agent/releases/download/v3.0.0/architect-bundle-3.0.0.zip"}]}
        with self.assertRaisesRegex(self.cli.PlanError, "manifest version"):
            self.candidate(metadata)

    def test_unknown_installed_source_cannot_be_upgraded(self):
        path = self.target / self.cli.MANIFEST_FILE
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["source"] = "https://untrusted.example/repo"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        before = {p.relative_to(self.target): p.read_bytes() for p in self.target.rglob("*") if p.is_file()}
        result, code = self.cli.self_update_operation(str(self.target), str(self.bundle), True)
        self.assertEqual((code, result["status"]), (1, "unsupported"), result)
        self.assertEqual(before, {p.relative_to(self.target): p.read_bytes() for p in self.target.rglob("*") if p.is_file()})

    def test_missing_architect_index_blocks_before_release_query(self):
        path = self.target / "AGENTS.md"
        path.write_text(path.read_text(encoding="utf-8").replace(
            "  - id: agent-architect\n    path: .agent/skills/agent-architect.md\n", ""
        ), encoding="utf-8")
        with mock.patch.object(self.cli, "_release_candidate") as queried:
            result, code = self.cli.preflight_operation(str(self.target))
        self.assertEqual((code, result["status"]), (1, "unsupported"))
        queried.assert_not_called()

    def test_network_failure_and_large_response_leave_target_unchanged(self):
        before = {p.relative_to(self.target): p.read_bytes() for p in self.target.rglob("*") if p.is_file()}
        for failure in (self.cli.PlanError("RELEASE_OFFLINE", "network unavailable"),
                        self.cli.PlanError("RELEASE_RESPONSE_TOO_LARGE", "too large")):
            with mock.patch.object(self.cli, "_bounded_url_bytes", side_effect=failure):
                result, code = self.cli.self_update_operation(str(self.target), None, True)
            self.assertEqual(code, 1, result)
            self.assertIn(result["status"], ("offline", "unsupported"))
            self.assertEqual(before, {p.relative_to(self.target): p.read_bytes() for p in self.target.rglob("*") if p.is_file()})
        with mock.patch.object(self.cli.urllib.request, "build_opener") as opener:
            response = io.BytesIO(b"123456")
            response.headers = {}
            opener.return_value.open.return_value = response
            with self.assertRaisesRegex(self.cli.PlanError, "size limit"):
                self.cli._bounded_url_bytes(self.cli.CANONICAL_API_URL, 5, api=True)

    def test_redirect_cannot_downgrade_https_or_leave_github(self):
        guard = self.cli._ReleaseRedirect()
        request = urllib.request.Request(self.cli.CANONICAL_API_URL)
        for url in ("http://github.com/file", "https://evil.example/file", "https://github.com:8443/file"):
            with self.subTest(url=url), self.assertRaises(self.cli.PlanError):
                guard.redirect_request(request, None, 302, "Found", {}, url)
        redirected = guard.redirect_request(request, None, 302, "Found", {},
                                             "https://release-assets.githubusercontent.com/file?signature=public")
        self.assertEqual(redirected.type, "https")

    def test_malformed_bundle_fields_return_structured_errors(self):
        path = self.bundle / "architect-bundle-manifest.json"
        original = json.loads(path.read_text(encoding="utf-8"))
        for patch in ({"entrypoints": {**original["entrypoints"], "skill": []}},
                      {"protectedFiles": [None]}, {"schemaVersion": True}):
            path.write_text(json.dumps({**original, **patch}), encoding="utf-8")
            result, code = self.cli.preflight_operation(str(self.target), str(self.bundle))
            self.assertEqual((code, result["status"]), (1, "unsupported"), result)
            self.assertEqual(result["errors"][0]["code"], "PACKAGE_INVALID")

    def test_unreadable_zip_returns_package_invalid(self):
        with tempfile.TemporaryDirectory() as stage, mock.patch.object(
            self.cli.zipfile.ZipFile, "read", side_effect=RuntimeError("password required")
        ):
            with self.assertRaises(self.cli.PlanError) as failed:
                self.cli._extract_release_zip(self.archive.read_bytes(), stage)
        self.assertEqual(failed.exception.code, "PACKAGE_INVALID")


if __name__ == "__main__":
    unittest.main()
