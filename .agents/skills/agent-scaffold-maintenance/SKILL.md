---
name: agent-scaffold-maintenance
description: Maintain an Agent workspace with the deterministic scaffold CLI. Use when a user asks to inspect, initialize, extend, update, validate, apply, or restore an Agent scaffold.
allowed-tools:
  - Bash(python {baseDir}/tools/agent_scaffold.py:*)
  - Bash(python3 {baseDir}/tools/agent_scaffold.py:*)
  - Bash(py -3 {baseDir}/tools/agent_scaffold.py:*)
  - Bash(python {baseDir}/tools/agent_asset_migration.py:*)
  - Bash(python3 {baseDir}/tools/agent_asset_migration.py:*)
  - Bash(py -3 {baseDir}/tools/agent_asset_migration.py:*)
  - Bash(python {baseDir}/tools/agent_evaluation.py:*)
  - Bash(python3 {baseDir}/tools/agent_evaluation.py:*)
  - Bash(py -3 {baseDir}/tools/agent_evaluation.py:*)
  - Bash(python {baseDir}/tools/validate_scaffold.py:*)
  - Bash(python3 {baseDir}/tools/validate_scaffold.py:*)
  - Bash(py -3 {baseDir}/tools/validate_scaffold.py:*)
license: MIT
metadata:
  author: personal-agent
  version: "3.2.0"
  sourceRepository: heibaoxia/My-Personal-Agent
  sourcePath: .agents/skills/agent-scaffold-maintenance
  toolVersion: "3.2.0"
---

# Agent Scaffold Maintenance

Use this Skill to design, create, maintain and distribute Agent scaffolds. It connects a
calling Agent's requirements work to the deterministic CLI; it does not route
or execute the domain Skills stored in a produced workspace.

This directory is the complete installable architect Skill: `SKILL.md`,
`tools/agent_scaffold.py`, `tools/validate_scaffold.py`, `tools/agent_evaluation.py`,
`tools/agent_asset_migration.py`, `tools/scaffold-evaluate.md` (the canonical
evaluation Skill) and `tools/release.json`.
Python 3.10 or newer and its standard library are sufficient for local work;
Git is needed only for `git+` package sources. No development repository,
OpenSpec installation, separate Agent Init script or Python package install
is required. The CLI provides `inspect`, `check`, `diff`, `apply`, `verify`,
`restore`, `purge`, `pack`, `package-check`, `install`, `upgrade`, `preflight`
and `self-update`.

## Scaffold authoring contract

Read these rules before authoring a complete creation or maintenance plan.
Use the bundled validator to check the result; its implementation defines the
accepted YAML subset and reports unsupported fields.

- The root `AGENTS.md` has YAML frontmatter with `agentId` (lowercase slug),
  `contractVersion: "1"` and nonempty `purpose`. Optional `skills`, `workflows`
  and `integrations` indexes contain `id` and workspace-relative `path` entries.
  Each object uses `.agent/<collection>/<id>.md`. Object IDs are globally unique;
  indexes and files must agree, and references must resolve.
- Skill frontmatter requires `id`, `purpose`, nonempty string lists `triggers`,
  `excludes`, `inputs`, `outputs`, `sideEffects`, `confirmation` and nonempty
  `implementation`. `sideEffects` is `none`, `writes-workspace` or
  `calls-external`; writes and external calls require `confirmation: required`.
  External calls also require an indexed Integration. For read-only Skills,
  `confirmation` may be `not-required`.
- Optional nonempty `requires` uses a block list of mappings containing `id`,
  single-line `purpose`, `from` and `ifMissing`. `from: user` has no extra source
  field; `from: context` requires a declared `contextCategory`; `from: integration`
  requires an indexed `integration`. `ifMissing` is `ask`, `refuse` or `degrade`;
  only `degrade` includes a nonempty `note`. Optional nonempty `readBefore`
  lists existing safe workspace-relative files. Optional `workflow` or
  `workflows` references indexed Workflows.
- Keep the same entry, indexes, relative paths and core Python source on Windows
  and macOS. Declare platform-specific tools or permissions in `implementation`
  or the existing `requires`/Integration fields, with an explicit missing policy.
  Do not invent `platforms` fields, duplicate Skills, platform directories or
  launchers merely because Python has a different command name.
- Workflow frontmatter requires `id`, `purpose` and at least two `steps`.
  Optional `usesSkills` references indexed Skills; `inputs` and `outputs` are
  string lists. Integration frontmatter requires `id`, `kind` (`mcp`, `cli` or
  `http`), `purpose`, `requires`, `fallback` (`block` or `degrade`), `fallbackNote`
  and boolean `enabled`. Degradation needs a nonempty fallback note; do not
  store credentials in declarations.
- Optional `.agent/context.yaml` declares nonempty `categories` and `sources`,
  `states: [temporary, pending, confirmed]` and `writePolicy: confirm`.
  `.agent/behaviors.md` describes behavior timing and boundaries. Context data
  files are user assets and remain separate from these declarations. Create
  neither optional objects nor empty directories unless the assistant needs them.
- Author real Markdown implementation rules, one initial assistant capability
  based on the user's goal, and the archive contract described below. An empty
  entry or placeholder capability is an intermediate result, not formal delivery.
- `scaffold-description.md` has frontmatter `schemaVersion: 1`, integer
  `baselineVersion` starting at 1, `origin: confirmed` (or `reconstructed` for a
  confirmed legacy reconstruction), `generatorVersion: "3.2.0"` and
  `contentSha256` of the UTF-8 LF Markdown body after the closing frontmatter.
  Describe the assistant's goals, non-goals, capabilities, acceptance criteria,
  known gaps and rule-file pointers. Increment the baseline only for body changes.
- `.agent/scaffold-manifest.json` has `schemaVersion: 1`, matching numeric
  `packageVersion` and `bundleVersion`, `contractVersion: "1"`, nonempty `source`,
  `sourceRepository: "heibaoxia/My-Personal-Agent"`, `sourceCommit`,
  `assetName: "architect-bundle-<bundleVersion>.zip"`, `files` (relative path to
  raw-byte SHA-256), `protectedFiles` and `exclusions` arrays. `entrypoints` has
  exactly `initialSkill`, `archiveSkill`, `architectSkill`, `evaluationSkill`
  (four distinct indexed Skill IDs) and `architectCli` (the protected
  workspace-relative Python path). Set `evaluationVersion: "1"` and
  `evaluationSkill: "scaffold-evaluate"`; its fixed path is
  `.agent/skills/scaffold-evaluate.md`.
  `description` contains `path: "scaffold-description.md"`, its `baselineVersion`,
  `origin`, body `contentSha256` and boolean `redacted`. Include the entry,
  Description and declared rules in `files`; never list the manifest itself or
  user Context data, `scaffold-evaluation.md` or `scaffold-score-history.md`.
  Only the architect/evaluation Skills and `.agent/architect/` runtime
  files belong to `protectedFiles`. The entry body discovers both the manifest
  and Description. Use the matching Release source commit when available;
  an explicit `unknown` marks an unverified local source.

For new formal scaffolds, embed this Skill's body in an indexed architect
Skill with the valid Skill frontmatter above. Copy the four Python modules,
`scaffold-evaluate.md` and
`release.json` bytes into `.agent/architect/tools/` through the confirmed plan,
and declare them and both shared Skills in `protectedFiles`. Render
`tools/scaffold-evaluate.md` using valid produced-Skill frontmatter with
`id: scaffold-evaluate`, `sideEffects: writes-workspace`, `confirmation: required`,
nonempty purpose/triggers/excludes/inputs/outputs/implementation and the unchanged
canonical body. Replace `{baseDir}/tools` in that body with
`.agent/architect/tools`, index its fixed path and explicitly locate
`.agent/architect/tools/agent_evaluation.py`. Do not create either artifact before
an actual evaluation or optimization. The embedded
Skill must resolve its CLI at that workspace-relative path, verify the local
release hashes, and execute the maintenance sequence. Replace each
`{baseDir}/tools` command prefix in the embedded body with
`.agent/architect/tools`; embedded discovery starts at the target workspace
root and does not expect a `SKILL.md` beside `agent-architect.md`.
Read any file bytes from
this loaded Skill directory, not another installed copy. Generate the entry,
Description, domain/archive Skills and scaffold manifest from the user's
confirmed requirements; use null baselines for creation and `verify --formal`
after the complete plan is applied.

## Responsibility boundary

- This Skill and `{baseDir}/tools/agent_scaffold.py` maintain files in a target workspace:
  inspect its state, check a complete plan, preview changes, apply an explicitly
  confirmed plan, verify the result, and restore a CLI backup.
- The calling Agent understands the user's domain request, asks for missing
  information, chooses the workspace files, writes complete target contents in
  a plan, and presents the diff for confirmation.
- A produced workspace starts at its root `AGENTS.md`. Its `.agent/skills/`
  files are domain capabilities used during ordinary conversations. They are
  not this maintenance Skill, and ordinary domain work must not require the
  maintenance CLI.
- `inspect.classification` classifies a target as `missing`,
  `existing-unmanaged`, `legacy`, `valid`, or `invalid`:
  `existing-unmanaged` means user/project assets exist without a positive
  scaffold contract marker. An ordinary Markdown `AGENTS.md` remains a
  user-owned rule and is not an invalid generated entry.
  `legacy` means a recognized pre-`AGENTS.md` `AGENT.md` entry with its
  indexed `.agent/` objects present. An unrecognized `AGENT.md` or another
  host entry is reported as `WORKSPACE_ENTRY_UNSUPPORTED`; it is never
  migrated automatically. `scaffold` is a compatibility alias that maps
  `existing-unmanaged` to `missing`; branch on `classification`.
  `adoptable` means the bounded read-only inventory can proceed to a
  user-confirmed adoption discussion, not that writes are authorized.
  `inventory.complete` must be true before preparing a complete adoption plan.
- Physical project asset moves use the separate
  `{baseDir}/tools/agent_asset_migration.py`. Never add project paths to the
  ordinary scaffold plan or call the migration tool as part of rule maintenance.

The CLI is deterministic and does not call an LLM, interpret Skill prose,
route Skills, run Workflows, read Context state, call Integrations, or ask the
user questions. A successful static verification proves file and declaration
consistency only.

## Runtime lifecycle handoff

This Skill maintains the declarations that a host runtime consumes; it is not
the host runtime. A normal conversation in a produced workspace follows the
lifecycle described here:

- A new or explicitly reset session reads `AGENTS.md` once, selects one initial
  behavior, records that route in session state, and does not reroute on every
  subsequent message. No match, multiple equal matches, or an unreadable
  entry/behavior must stop, ask, or use an explicitly declared degradation.
- A later behavior call, including archive, must reference an indexed behavior
  or Skill. The host reads that behavior's own `requires`, `readBefore`, side
  effect, and confirmation declarations before calling it. `requires` and
  `readBefore` are runtime duties; validator success does not perform them.
- Context is an on-demand dependency. Load only categories and files declared
  by the current behavior and never create `context.yaml`, memory categories,
  archive files, or a fixed directory layout merely to satisfy this lifecycle.
- Archive is a user-triggered behavior. It receives the complete ordered
  transcript up to the archive action, preserves message roles and behavior
  results, updates only declared Context targets, and returns a result for each
  target. Incomplete transcript or unavailable prerequisites must block or be
  reported as an explicit degradation.
- Each archive-enabled workspace chooses exactly one authorization meaning:
  the archive action authorizes that write, or the action only creates a
  candidate and a separate confirmation authorizes it. Do not infer this
  meaning from `writePolicy`, `confirmation`, or conversational tone, and do
  not silently broaden the authorized target set.
- Target updates follow their declared replace/merge/delete/pending and conflict
  rules. Repeating the same archive boundary is idempotent; it must not append
  the full transcript without bound. Failed or conflicting targets retain their
  prior content or return an explicit partial result.

Every formal workspace has an archive entrypoint; its persistence targets are
optional. Without targets the host returns `no-persistence-target` and creates
no placeholders. Runtime archive writes belong to the host behavior and its declared targets.
They must not be smuggled into a maintenance plan. The maintenance CLI remains
limited to controlled rules, Description and manifest user-asset metadata.

## CLI discovery

Determine whether this is the external installed Skill or the indexed embedded
architect in the target scaffold before resolving the executable. For the
embedded `.agent/skills/<architect-id>.md`, resolve `architectCli` from the
target workspace's verified `.agent/scaffold-manifest.json`, then check its
adjacent `release.json` version and hashes. A verified external handoff is the
authorization to use this local runtime for the same request. The embedded
architect proceeds to Required sequence and never recursively calls preflight
or self-update. The external installed Skill uses the discovery rules below
and completes Architect Release preflight before the Required sequence on an
existing formal target. New workspace creation uses the verified external Skill
directly; a missing or unrecognized target is not auto-upgraded.

Resolve `{baseDir}` to the directory containing the `SKILL.md` that was
actually selected for this conversation, then invoke only
`{baseDir}/tools/agent_scaffold.py`. Before the first CLI call, try the
platform-appropriate Python 3.10+ command candidates: Windows uses `python`
then `py -3`; macOS uses `python3` then `python`. Probe each candidate with
`-c "import sys; print(sys.version); print(sys.executable); sys.exit(0 if sys.version_info >= (3, 10) else 1)"`.
A missing command, nonzero exit or 10-second timeout tries the next candidate.
Record the selected interpreter, its version and `sys.executable`, and reuse the
same interpreter for every command in this request, including evaluation,
migration, validator and the verified embedded handoff. Prefer its resolved
executable path after discovery so PATH changes do not select another Python.
The command examples below use `python` as this logical placeholder; replace
it with the selected command, preserving its argument order. At the start of
every maintenance task, record the resolved Skill root and CLI path, run
`<selected-python> {baseDir}/tools/agent_scaffold.py --version`, and require
tool version `3.2.0` or newer. When `tools/release.json` exists,
its version and SHA-256 entries must match all bundled tools and evaluation
rule source before continuing.
Pass executable, script and parameters as separate argv items when the host
supports that API; use no shell expansion. With a shell, quote every path and
keep `py -3` as two arguments. PowerShell needs `&` before a quoted executable:
`& 'C:/Python/python.exe' 'C:/My Skill/tools/agent_scaffold.py' --help`.
In macOS Zsh/Bash use
`python3 '/Users/name/My Skill/tools/agent_scaffold.py' --help`.
Never paste PowerShell syntax into Zsh or change shell/profile settings.
If multiple same-named installations are visible, use the selected verified
Skill; conflicting versions or hashes block maintenance. Use the external CLI
until a successful Release preflight hands off to the target architect.
An arbitrary `tools/agent_scaffold.py` inside a target workspace is not this
Skill's CLI. The verified `.agent/architect/tools/agent_scaffold.py` becomes
the maintenance CLI only after the explicit handoff described below.

First run `<selected-python> {baseDir}/tools/agent_scaffold.py --help`. If no
candidate Python interpreter, the CLI file, or its required standard-library
modules can be found, or the command fails, report `cli-unavailable` with all
attempted executables, the CLI path and the actual errors. If the version or
release check fails, report `skill-version-conflict`
with the selected Skill root, CLI path, detected version, and expected
version/hash. Stop maintenance without editing the target manually; do not
fall back to a target-workspace CLI. `cli-unavailable` is reported by the
calling Agent even when Python cannot start; it is not a JSON promise from a
process that never ran. Include the minimum Python version and the actual OS,
shell, attempted argv, exit codes/stderr or launch errors. A verified Python
with an unreadable CLI must not trigger fallback to another copy of the tools.
Python, optional Git, host capabilities and directory permissions remain user
prerequisites. On macOS, denied Desktop/Documents access may require the user
to grant the host access in system settings; never grant it automatically.

## Required sequence

1. **Inspect before designing.** Run `inspect` against the target workspace.
   Read the JSON and retain its `workspace`, authoritative `classification`,
   non-authorizing `adoptable`, `inventory.complete`, `entry`, `objects`,
   `files`, `errors`, and `warnings` fields. Do not write during inspection.
   Branch on `classification`, never the compatibility `scaffold` alias.
   `missing` may be manufactured; `legacy` requires an explicit migration;
   `valid` supports incremental maintenance; `invalid` supports contract
   repair. For `existing-unmanaged`, begin with a read-only adoption inventory
   and do not prepare an applicable plan until the user has confirmed the
   assistant goal and boundaries. If the inventory is incomplete or a needed
   rule cannot be read safely, report the exact paths and stop planning.
   A legacy migration must explicitly create `AGENTS.md` and delete
   `AGENT.md` in the same plan; the CLI never renames or deletes it on its own.
2. **Review content before planning.** For manufacture or modification, read
   the existing `scaffold-description.md` first as the confirmed goal baseline,
   then the controlled entry, Context, Behavior, Skill, Workflow, and Integration
   files. Conditionally inspect the latest evaluation as described below.
   Check that the user's goal is covered, indexes and references agree,
    confirmation and side-effect boundaries are consistent, and existing rules
    outside the request remain protected. For archive behavior, require an
    explicit single authorization meaning (action-authorizes-write or
    candidate-then-confirm), declared Context targets, update/idempotence and
    conflict rules, and a complete-transcript boundary. Do not add fixed
    Context categories or archive placeholders. This semantic review belongs to
    the calling Agent; the CLI only checks structure, paths, baselines, and
    static references.
   During `existing-unmanaged` adoption, inventory existing root and
   project-local `AGENTS.md` rules, project roots, relevant indexes, and
   proposed Context source paths. A Context `sourceFiles` mapping is a
   reference and access policy, not a data repository: do not copy its source
   contents into new memory files. Description MUST NOT be inferred from
   README, existing rules, filenames, evaluation history, or a missing
   Description.
3. **Resolve authority and missing information.** Applicable user and platform
   instructions take priority. Root `AGENTS.md` rules govern cross-project
   boundaries; project-local rules may refine them only within the root rules.
   A Skill is an execution procedure subordinate to applicable AGENTS rules
   and cannot override them. Preserve the complete original rule body of an
   unmanaged `AGENTS.md`; if an entry change is needed, show the exact diff
   while retaining every original rule. When root rules, project rules, or a
   Skill conflict and the hierarchy cannot resolve it, identify both sources
   and stop and ask the user. Also ask about missing purpose, identifiers,
   desired Skills, side effects, confirmation policy, or prerequisites.
4. **Generate a complete plan.** Include the target workspace, the current
   baseline for every planned read or write, and complete target contents for
   each file to add or modify. Include explicit deletes only when requested.
   Keep the plan file outside the target workspace, or pass it through stdin.
   The plan uses `schemaVersion: 1`, the resolved absolute `workspace`, and
   matching `baseline` and `files` maps. Each map key is a workspace-relative
   `AGENT.md`, `AGENTS.md`, `scaffold-description.md`, or controlled `.agent/` path; a baseline is a lowercase
   SHA-256 string for an existing file or `null`, and a file content is UTF-8
   text or `null` for deletion.
   For a formal assistant, read `scaffold-description.md` and current rules
   before planning. Preserve confirmed goals and non-goals unless the user
   changes them; repair implementation gaps against that intent. Include its
   complete text and raw-byte baseline plus `descriptionSync: updated` or
   `unchanged`. Unchanged bytes are checked but never rewritten. Increment
   `baselineVersion` only when the UTF-8 LF body changes; `contentSha256` hashes
   the body after closing frontmatter. Sync affected manifest user-file hashes
   and Description metadata in this same plan. One confirmation covers both
   rules and Description; do not ask for repeated requirements or a separate
   Description confirmation. For a newly confirmed goal, create a Description
   with a null baseline and `origin: confirmed`. Only for a previously managed
   scaffold whose old Description is missing may an explicitly reviewed
   reconstruction use `origin: reconstructed`; do not treat that as a
   user-confirmed new goal. An `existing-unmanaged` formal adoption must use
   `origin: confirmed`. Ordinary conversations and archive cannot write
   Description.
    For an `existing-unmanaged` adoption that changes assistant behavior, and
    for any plan claiming formal delivery, the user must explicitly confirm
    the Description's goals, non-goals, capabilities, and acceptance scope.
    The same plan must include `AGENTS.md`, the confirmed Description, actual
    indexed capabilities, `.agent/scaffold-manifest.json`, and current
    baselines/content for every file declared by that manifest, including the
    evaluation capability. A Description-only or partial bootstrap plan is not
    a formal adoption; CLI `check` and `apply` reject it before any target write.
5. **Check the plan.** Run `check` with the plan. A valid plan must pass path
   safety, baseline, and scaffold checks. If the target changed, regenerate the
   baseline instead of overriding it.
6. **Preview the diff.** Run `diff` with the same plan and review `changes`.
   The preview is read-only. Ensure files outside the plan are absent from the
   change list and existing project files remain untouched.
7. **Get explicit confirmation.** Show the user the files and actions in the
   diff and, for an actual optimization, the planned summary and result updates
   to `scaffold-score-history.md`. Reuse this confirmation for those bounded
   history writes; no separate confirmation or evaluation is required. Do not
   infer confirmation from a request to inspect, check, or preview.
8. **Apply once confirmed.** Run `apply --confirm` with the same plan. The CLI
   validates a temporary copy before writing, checks the baseline again, keeps
   a recoverable backup, and returns the backup identifier when successful.
   For an optimization, save its `planned` history entry before calling apply;
   follow the artifact sequence below. A failed plan record blocks rule writes.
9. **Verify and recover.** Run `verify` after an apply. If apply or verification
   reports a failure, do not retry blindly; report the JSON error and use
   `restore --backup <id>` when the backup is available, then verify again.
   Record the actual completed, failed or restored result in the same history
   entry. History record failure does not undo or redefine the rule result.

## Separate asset migration

Only enter this workflow after the user explicitly selects each file, the
migration root, source and target layout, and every relative link change.
Review the exact external recovery-journal directory and recovery procedure
shown by `diff` with the user before applying. Ordinary rule maintenance
authorization does not authorize physical asset movement.

The separate JSON plan declares `schemaVersion: 1`, the resolved workspace,
workspace-relative `migrationRoot`, `sourceRoot`, `targetRoot`,
`targetPolicy: fail`, and a complete `files` list. Each file record includes
its source path, target path, and source SHA-256. `linkUpdates` records each
existing link file's path and baseline SHA-256 plus its complete replacement
content. Do not guess omitted assets or links. The CLI checks that the source
tree is fully covered, target paths are clear, paths remain within the declared
root, and the exact plan has a stable confirmation digest.

```text
python {baseDir}/tools/agent_asset_migration.py check <workspace> --plan-file <outside-workspace>/asset-plan.json
python {baseDir}/tools/agent_asset_migration.py diff <workspace> --plan-file <outside-workspace>/asset-plan.json
python {baseDir}/tools/agent_asset_migration.py apply <workspace> --plan-file <outside-workspace>/asset-plan.json --confirm <diff-confirmation-digest>
python {baseDir}/tools/agent_asset_migration.py verify <workspace> --migration <migration-id>
python {baseDir}/tools/agent_asset_migration.py restore <workspace> --migration <migration-id>
```

The user must confirm the exact digest printed by `diff`. Apply copies files
and link updates first, verifies destination count and SHA-256, and only then
removes sources. The external journal stores original bytes and recovery
baselines. Restore removes only unchanged migration outputs, restores missing
sources, and preserves concurrent user edits with path-specific conflicts.
Missing backup data or a failed restore is reported; never guess or overwrite.
The ordinary `agent_scaffold.py` `safe_rel` and `apply` scope remain unchanged.

## Evaluation feedback and optimization history

After the required Release preflight and any verified handoff, use the
`agent_evaluation.py` adjacent to the selected verified maintenance CLI.
It writes only the two fixed user artifacts, independently of rule plans.
On each new maintenance task, inspect `scaffold-evaluation.md` conditionally:

```text
python {baseDir}/tools/agent_evaluation.py inspect <workspace>
```

`missing` allows normal maintenance and creates no placeholder. Read a
`current` report's goal baseline, rule fingerprint, evidence scope and findings.
Explicitly report `stale`, `invalid` or `unreadable`; an old score is not a
current quality conclusion. An insufficient goal baseline remains a diagnosed
gap, not a fabricated goal or fingerprint. Review suggestions against the
Description and this request, keep only authorized candidates, and recheck
their evidence. Low scores do not authorize extra work, force a new evaluation,
or replace the Description. Do not add the optional report to `readBefore`.
History is for human review and version association, not a score or planning
gate. This artifact inspection cannot bypass a blocked preflight or handoff.

For a confirmed optimization of existing assistant rules:

1. Run `check` and `diff` first. Pure inspection, an unconfirmed plan, new
   assistant creation, or a diff with no substantive rule changes does not
   create a completed optimization version. Tool-package self-update preserves
   the two artifacts and is not an assistant-quality optimization.
2. Capture the current snapshot with the artifact tool. Choose a stable
   `entryId` for this maintenance request and include the authorized problem
   and change summaries in a plan-record input. Supply the snapshot as its
   `baseline`; it checks rule and history bytes. Save the record with `plan
   --input <outside-workspace>/optimization-plan.json --authorized`.
   Do not begin apply unless the result confirms `historyWritten: true`.
3. Apply the unchanged rule plan with the maintenance CLI, then verify. Follow
   its recovery protocol on failure. These rule plans never include
   `scaffold-evaluation.md` or `scaffold-score-history.md`; artifact authorization
   does not expand the rule-path whitelist.
4. Capture the resulting snapshot and pass it as the result-record baseline.
   Use the same `entryId`, actual change and validation summaries, and the
   truthful status `completed`, `failed`, `restored` or `no-change` with `result
   --input <outside-workspace>/optimization-result.json --authorized`.
   A restored attempt retains the prior active version; only a verified
   substantive optimization becomes the active completed version. No score is
   required, and multiple unscored versions are valid.
5. If result recording fails, report the successful or failed rule result
   separately from the pending history write. Preserve existing history,
   retain the returned entry identifier, re-read current artifacts and rules,
   and retry only that result record with a fresh checked snapshot. Never rerun
   a successful rule apply merely to repair history or claim rollback occurred.

```text
python {baseDir}/tools/agent_evaluation.py snapshot <workspace>
python {baseDir}/tools/agent_evaluation.py plan <workspace> --input <outside-workspace>/optimization-plan.json --authorized
python {baseDir}/tools/agent_scaffold.py apply <workspace> --plan-file <outside-workspace>/plan.json --confirm --json
python {baseDir}/tools/agent_scaffold.py verify <workspace> --json
python {baseDir}/tools/agent_evaluation.py snapshot <workspace>
python {baseDir}/tools/agent_evaluation.py result <workspace> --input <outside-workspace>/optimization-result.json --authorized
```

The plan-record JSON contains `entryId`, `plannedChanges`, `problems` and
`baseline`; the result contains `entryId`, `status`, `actualChanges`,
`validation` and `baseline`. All summary fields are string lists. Optional
`recordedAt` has an explicit timezone; the tool supplies a timestamp if absent.
Inputs live outside the target or are passed transiently through stdin. The
tool emits structured JSON and never determines scores or performs rule
changes. Ordinary domain conversations and Context archive never write these
artifacts or create optimization entries.

## Architect Release preflight

Only the external installed maintenance Skill performs this sequence when
delegating a maintenance request to an existing embedded architect. An embedded
architect receiving a verified handoff skips this sequence and uses its local
CLI; it does not re-check the remote Release. Ordinary domain conversations, Context archive actions and other
Skills do not perform a network check or write the workspace.

1. Run `preflight` before designing the maintenance plan:

   ```text
   python {baseDir}/tools/agent_scaffold.py preflight <workspace> --release-source <canonical-release-or-fixture> --json
   ```

   The result is one of `up-to-date`, `outdated`, `conflict`, `unsupported` or
   `offline`. It includes current and canonical versions, source, hash
   differences and the next action. A failed preflight blocks architect work.
2. After an `outdated` result and the same maintenance authorization, run:

   ```text
   python {baseDir}/tools/agent_scaffold.py self-update <workspace> --release-source <canonical-release-or-fixture> --confirm --json
   ```

   The operation replaces only manifest-declared architect files, returns the
   backup and handoff metadata, then re-reads the target Skill and CLI path.
   Continue the original request only through that reloaded path. On conflict,
   unsupported, offline, package or restore failure, report the structured
   error and do not begin the maintenance plan.

## CLI protocol

The stable entry point is:

```text
python {baseDir}/tools/agent_scaffold.py <operation> <workspace> [options]
```

Every operation emits JSON when `--json` is supplied. The JSON object always
contains:

```json
{
  "schemaVersion": 1,
  "operation": "inspect",
  "workspace": "<resolved target path>",
  "status": "ok",
  "errors": [],
  "warnings": [],
  "classification": "existing-unmanaged",
  "scaffold": "missing",
  "adoptable": true,
  "inventory": {
    "complete": true,
    "scannedEntries": 4,
    "maxDepth": 12,
    "maxEntries": 10000,
    "skippedDirectories": [],
    "excluded": [],
    "warnings": [],
    "files": ["AGENTS.md", "README.md"],
    "objects": []
  }
}
```

`status` is one of `ok`, `error`, `conflict`, or `needs-confirmation`. Treat
`errors` and `warnings` as arrays of structured records; preserve their code,
path, and message when asking the user for a correction. Do not parse human
text as a substitute for these fields.

Operation-specific fields:

- `inspect` also returns authoritative `classification`, non-authorizing
  `adoptable`, bounded `inventory`, compatibility `scaffold`, `entry`,
  `objects`, and `files`; when an `AGENT.md` exists it may include
  `legacyEntry`. The `inventory` skips common dependency/cache/build folders,
  does not follow links, has fixed depth and entry limits, and never opens
  dotenv files. `inventory.complete == false` blocks adoption planning.
  `scaffold` maps `existing-unmanaged` to `missing`; branch on
  `classification`. The operation-level `status` still reports whether the
  CLI call itself succeeded.
- `check` and `diff` also return `plan` and `changes`.
- `apply` requires `--confirm`; it accepts `--plan-file PATH` or a plan on
  stdin and returns the applied result and backup information. Each `changes`
  item contains `path`, `action` (`create`, `modify`, `delete`, or `unchanged`),
  `beforeHash`, `afterHash`, and a unified `diff`; a successful apply returns
  `backup.id` (a UUID) for recovery.
- `verify` checks the target workspace after an apply and returns the normal
  verification status. `--formal` additionally requires a complete delivery.
  Static `valid` with `delivery.status: intermediate` is not a complete assistant.
- `restore` requires `--backup ID` and returns the restored result plus backup
  age and protected-file metadata. `purge` is explicit and accepts
  `--older-than-hours N`; an optional positional workspace or
  `--workspace-filter PATH` limits cleanup to one target. Purge never runs as
  part of apply or verify and reports deleted, retained, malformed, and delete
  failures separately.

Use the same plan input for `check`, `diff`, and `apply`:

```text
python {baseDir}/tools/agent_scaffold.py inspect <workspace> --json
python {baseDir}/tools/agent_scaffold.py check <workspace> --plan-file <outside-workspace>/plan.json --json
python {baseDir}/tools/agent_scaffold.py diff <workspace> --plan-file <outside-workspace>/plan.json --json
python {baseDir}/tools/agent_scaffold.py apply <workspace> --plan-file <outside-workspace>/plan.json --confirm --json
python {baseDir}/tools/agent_scaffold.py verify <workspace> --json
python {baseDir}/tools/agent_scaffold.py restore <workspace> --backup <backup-id> --json
python {baseDir}/tools/agent_scaffold.py purge --older-than-hours 168 --json
python {baseDir}/tools/agent_scaffold.py purge <workspace> --older-than-hours 168 --json
```

Minimal plan shape:

```json
{
  "schemaVersion": 1,
  "workspace": "F:/path/to/workspace",
  "baseline": {"AGENTS.md": "<64 lowercase hex SHA-256>"},
  "files": {"AGENTS.md": "---\n...\n"}
}
```

For a transient plan, omit `--plan-file` and pipe the exact JSON document to
stdin. The CLI does not write a plan file into the target workspace.

## Handling results

- `inspect.status == "error"`: show the concrete errors and ask for the
  missing or conflicting information; do not create files manually.
- `inspect.classification == "existing-unmanaged"`: inventory the existing
  rules, projects, indexes, Context sources, and conflicts without writing.
  Resolve unconfirmed goals and boundaries with the user before preparing a
  complete formal plan.
- `inspect.classification == "legacy"`: prepare an explicit migration plan with an
  `AGENTS.md` creation and `AGENT.md` deletion, then send it through `check`,
  `diff`, confirmation, `apply`, and `verify` like any other plan.
- `inspect.classification == "missing"`: manufacture the smallest complete workspace
  that covers the user's request and preserve existing project files.
- `inspect.classification == "valid"`: review the controlled content and generate a
  minimal incremental plan; `invalid` uses the same protocol for repair.
- `WORKSPACE_ENTRY_UNSUPPORTED` warnings identify an external entry. Preserve
  it and ask the calling Agent to decide whether a separately designed plan is
  appropriate; do not treat the warning as permission to migrate it.
- `check.status == "conflict"`: the baseline is stale. Re-inspect, regenerate
  the complete plan, and run `check` again. Path safety or plan-shape errors
  return `error`; correct the named plan entry before rechecking.
- `diff.status == "ok"`: review every item in `changes`; this is still a
  read-only result and does not authorize writing.
- `apply.status == "needs-confirmation"`: stop and obtain explicit user
  confirmation, then rerun with `--confirm`.
- Any apply or verify error: preserve the error record, restore from the
  returned backup when appropriate, and verify the restored workspace.

Never claim that a domain Skill or conversation archive ran because `verify`
passed. `inspect`/`check`/`diff`/`verify` prove static files, declarations,
paths, references, baselines, and plan consistency. `apply` proves that the
confirmed maintenance plan was written and backed up. None of these commands
proves session routing, behavior execution, on-demand Context reads, complete
transcript capture, archive authorization, target updates, idempotence,
conflict handling, or Integration availability. Those require host runtime
smoke tests and archive tests. Start a normal conversation in an initialized
workspace by reading `AGENTS.md`, following its rules, and then loading the
indexed `.agent/skills/<id>.md` files needed for the user's request. The host
evaluates `triggers`/`excludes`, checks `requires` and follows each `ifMissing`
policy, and reads every workspace-relative file in `readBefore` before
executing the selected domain Skill. Use this maintenance Skill only when the
workspace itself needs to be inspected or changed.

## Formal delivery and sharing

Minimal Init creates only `AGENTS.md`. Formal delivery requires a valid
Description, initial capability, archive Skill, protected architect Skill and
self-contained CLI. The entry body points to `scaffold-description.md` and
`.agent/scaffold-manifest.json`; its Skill index discovers all four roles,
including the protected fixed `scaffold-evaluate` entry. Old four-entrypoint
manifests without evaluation metadata are explicitly `legacy` delivery and
require a controlled newer package upgrade before current formal acceptance.
The manifest declares `schemaVersion: 1`, `packageVersion` (major.minor.patch),
`bundleVersion` (the same value for new bundles), `contractVersion: "1"`,
source, `sourceRepository`, `sourceCommit`, file SHA-256 hashes,
protectedFiles, exclusions, entrypoints and Description metadata. New bundles
use the canonical repository `heibaoxia/My-Personal-Agent` and the asset name
`architect-bundle-<bundleVersion>.zip`; a manifest with only old
`packageVersion` fields is explicitly reported as a legacy package. The
architect Skill lives in the normal Skill index; its tools live under
`.agent/architect/`. Bootstrap a formal assistant using the same complete
confirmed creation plan, with null baselines for new files. Domain execution
needs separate host evidence.

The indexed archive Skill declares `sideEffects: writes-workspace`,
`confirmation: required`, a concrete implementation and nonempty rules. Include
exactly one fenced `agent-archive` JSON block containing `authorization`
(`archive-action` or `candidate-confirm`) and `targets` (an array; empty is
valid). With no targets use `{"authorization":"candidate-confirm","targets":[]}`.
The host parses this contract; formal static delivery does not execute it.

Ordinary maintenance cannot modify protected architect files, package version,
protected scope or architect baseline hashes. It may sync only Description and
domain-file metadata. Use `upgrade` for newer architect packages; it preserves
the local Description, domain rules, Context and project files and stops on
baseline conflicts. Review the diff and reuse existing authorization for the
concrete operation when available.

```text
python {baseDir}/tools/agent_scaffold.py verify <workspace> --formal --json
python {baseDir}/tools/agent_scaffold.py package-check <package-dir> --json
python {baseDir}/tools/agent_scaffold.py install <target> --package-source <source> --confirm --json
python {baseDir}/tools/agent_scaffold.py upgrade <target> --package-source <source> --confirm --json
python {baseDir}/tools/agent_scaffold.py pack <workspace> --output <new-package-dir> --description-candidate <redacted.md> --redaction-review <review.json> --json
```

Sources are local directories, `git+<URL>` repositories, or HTTP(S) ZIP URLs.
The installer checks safe relative manifest paths, hashes, static declarations
and formal delivery before writing, and returns an external recoverable backup.

Before sharing, generate a separate redacted Description candidate. Preserve
intent, generalized useful preferences, acceptance, gaps and rule pointers;
remove identity, private background, secret values and domain-source text.
Do not claim text filters detect arbitrary privacy. Supply review JSON with
`status: approved`, `unresolved: []`, string lists `removed` and `generalized`,
`sourceBaselineVersion`, raw original-byte `sourceDescriptionSha256` and exact
reviewed `candidateSha256`. Unresolved items, missing candidates or stale hashes
block packing. Pack uses only that candidate, marks source baseline and redacted
metadata, excludes all unlisted files, and keeps the local Description unchanged.

Publishers distribute this complete Skill directory. The separate versioned
Release asset contains the scaffold's protected architect Skill, CLI, validator
and manifest. Development packaging tools are not runtime dependencies.
Ordinary domain conversations and archive actions do not perform a Release
lookup or write through this CLI; source, path, manifest and hash failures stop
maintenance with a structured diagnostic.

Windows and macOS consume the same versioned payload and raw-byte hashes.
Do not rewrite protected files or line endings on the receiver. Only package
actually needed files; any future launcher must be covered by the manifest,
protected scope, upgrade and restore. Static validation, tool execution on one
OS and actual host execution on another are distinct evidence levels.
