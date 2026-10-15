# Personal Agent Platform Tests

This repository is an explicitly approved, public test snapshot for Agent
Architect 3.2.0. It is not the product release repository.

Canonical Skill source:
`heibaoxia/My-Personal-Agent@b54c25284dd3caa1dd0a1c255ca14a43e1a9940a`

The first CI run exposed directory-alias handling and Windows stdout encoding
issues. This revision includes regression checks for those failures, preserves
the original plan-location and user-symlink restrictions, and reruns the same
platform matrix.

The snapshot includes maintenance tools, their regression tests and synthetic
`samples/agent-scaffold` fixtures only. It excludes the development repository's
OpenSpec documents, personal notes, transcripts, real assistant workspaces,
credentials and generated local reports.

Run the selected regression suite:

```sh
python3 -X utf8 -B tools/platform_smoke.py --json --regression --output-file platform-local.json
```

GitHub Actions runs the same snapshot on standard macOS and Windows runners.
Reports identify actual platform commands, interpreter selection, tool checks,
regression results and skips. This snapshot excludes unrelated product-planning
and host-adapter suites; it must not be described as the entire development
repository's regression suite.

Passing these tests is maintenance-tool evidence, not evidence that an actual
Agent application has loaded and executed a generated assistant on macOS.
Desktop/Documents privacy permissions and Agent-host behavior remain separate
acceptance items. The optional local consumer-workspace test may skip because
real user workspaces are intentionally absent.

`snapshot-manifest.json` records the exported file hashes and source provenance.
No product Release is created by this repository.
