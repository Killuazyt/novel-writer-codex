---
name: webnovel-review
description: Review one webnovel chapter or a serial range of at most five chapters with the managed read-only reviewer Agent. Use when the user asks for continuity, setting, timeline, character, or logic review, a structured issue report, or recovery of an interrupted review. Enforce live Codex model evidence, per-run artifacts, finite blocking decisions, and zero unauthorized chapter edits.
---

# Webnovel Review

Run review through the stable runtime. Never imitate the reviewer in the parent task and never edit chapter text directly.

## Resolve roots and mode

Load [Runtime invocation](../../references/codex/runtime-invocation.md), then resolve the novel project with `where --format json`. Keep the Codex workspace root separate from the novel project root.

Use `full` unless the user explicitly requests a faster factual pass. `full` checks all five dimensions. `fast` checks setting, timeline, and continuity while returning exact `skipped: fast mode` conclusions for character and logic.

Load [Factual review guide](references/common-mistakes.md) when an issue category or evidence threshold is ambiguous. Load [Pacing requests](references/pacing-control.md) only when the user explicitly asks for pacing critique; do not expand the five-dimension schema.

## Prepare one chapter

The runtime binds the run to the host-provided canonical UUID in `CODEX_THREAD_ID`. Never set, replace, or synthesize this variable. If it is missing, malformed, or changes before accept, a decision, or range advancement, stop fail-closed.

Invoke:

`python -X utf8 "<PLUGIN_ROOT>/scripts/webnovel.py" --project-root "<PROJECT_ROOT>" review prepare --chapter <N> --mode <full|fast> --workspace-root "<WORKSPACE_ROOT>" --parent-model "<CURRENT_PARENT_MODEL>" --parent-effort "<CURRENT_PARENT_EFFORT>" --format json`

Parse one `webnovel-review-workflow/v1` object. Stop if chapter resolution is missing, ambiguous, outside the project, or crosses a symlink/junction; stop if the managed reviewer is missing or stale. Do not repair these conditions.

The returned `request_file` is the only trusted request package for this run. Preserve the returned `binding_marker` exactly. Invoke `webnovel_reviewer` as a native child Agent inside the current task. Do not create a Codex top-level task, use `create_thread`, or substitute the parent model. Give the child both the request-file path and the exact binding marker verbatim, and require one bare JSON object as its final answer. The reviewer is read-only and must not write files.

## Accept the reviewer result

Do not copy, rewrite, summarize, or place the reviewer response in the accept request. The runtime reads the exact final assistant text from the host-owned child rollout and hashes those bytes. Permit at most one serialization retry in the same managed child Agent with the same request, route, and exact binding marker. A retry must stay in that child; the first raw invalid JSON remains part of the rollout evidence.

Create a temporary UTF-8-without-BOM JSON file outside the novel project through the host's native file-writing API. Never use PowerShell, shell redirection, or string interpolation to create it. Keep Chinese, quotes, newlines, ampersands, semicolons, pipes, dollar signs, parentheses, and backticks inside JSON strings. Use this exact shape:

```json
{
  "schema_version": "webnovel-review-accept-request/v2",
  "run_id": "rv-ch0001-example",
  "chapter": 1,
  "review_mode": "full",
  "runtime": {
    "rollout_path": "ABSOLUTE_EXPLICIT_CHILD_ROLLOUT_JSONL",
    "sessions_root": "ABSOLUTE_CODEX_SESSIONS_ROOT",
    "child_thread_id": "CHILD_THREAD_ID",
    "parent_thread_id": "PARENT_THREAD_ID"
  },
  "duration_ms": 0
}
```

`sessions_root` must be the actual host-owned Codex sessions root. It is not a caller-selectable evidence directory, and changing `CODEX_HOME` or pointing at a temporary fixture does not authorize another root. `rollout_path` must name the explicit child JSONL below that root; do not manufacture, copy, edit, or replay a rollout.

Invoke:

`python -X utf8 "<PLUGIN_ROOT>/scripts/webnovel.py" --project-root "<PROJECT_ROOT>" review accept --run-id "<RUN_ID>" --request-file "<ABSOLUTE_ACCEPT_REQUEST_JSON>" --format json`

The runtime independently parses the explicit Codex rollout and requires `webnovel_reviewer`, `gpt-5.6-luna`, and `medium`. It requires the exact run/request binding prompt before the final assistant output, rejects reuse of a child or rollout across runs, and binds the accepted raw output hash to the immutable `request.json`. It then verifies contract hash, chapter/context hashes, protected-state snapshots, exact fields, five dimensions, counts, and types. Agent self-report, a hand-written model label, caller-supplied response JSON, canned fixture, copied rollout, missing rollout, or parent-model fallback is not evidence. If the host cannot expose the explicit trusted rollout path and task identities, stop with the live gate pending; do not claim the review passed. Delete the temporary accept request with the host file API after use.

## Handle blocking issues

When `status=awaiting_user`, present exactly the returned three options and wait:

1. `targeted_fix` — request a managed writer transaction; never let the parent edit正文.
2. `report_only` — persist the report and metrics while leaving正文 unchanged.
3. `abandon` — keep only internal run evidence; create no report or database row.

The returned decision includes a `webnovel-codex-choice/v1` request and an exact `WEBNOVEL_REVIEW_DECISION/v1 ...` binding marker. Put that marker unchanged on its own line in the parent assistant message that presents the options, then pause. Only the next real user message in that same trusted parent rollout is an answer; never turn an Agent message, request-file field, summary, or inferred intent into a choice.

After the user answers, create a temporary UTF-8-without-BOM JSON file outside the novel project with the host file API. Do not include `choice`, `answer`, or any self-reported selection. Use this exact shape:

```json
{
  "schema_version": "webnovel-review-decision-request/v1",
  "kind": "run",
  "run_id": "rv-ch0001-example",
  "range_id": null,
  "request_id": "choice-0123456789abcdef0123",
  "runtime": {
    "rollout_path": "ABSOLUTE_EXPLICIT_PARENT_ROLLOUT_JSONL",
    "sessions_root": "ABSOLUTE_CODEX_SESSIONS_ROOT",
    "parent_thread_id": "PARENT_THREAD_ID"
  }
}
```

Invoke:

`review decide --run-id "<RUN_ID>" --request-file "<ABSOLUTE_DECISION_REQUEST_JSON>" --format json`

The runtime requires this rollout's thread ID to equal the parent task ID recorded in the bound reviewer child's runtime evidence. It also verifies the parent model and effort recorded at prepare time, the exact assistant marker, and the first durable user answer after it, then resolves only one offered option. Never infer consent. An unanswered, free-form, wrong-parent, stale, cross-run, replayed, copied, or edited receipt authorizes no branch. Delete the temporary request with the host file API after use. `targeted_fix` remains pending until the managed writer workflow validates a staging artifact; an already accepted chapter requires the M6 full transaction and must stay blocked here.

## Review a range

For an inclusive range of one to five chapters, invoke `review range-prepare` with `--start`, `--end`, the same mode/workspace/parent fields, and `--format json`. Review only the returned current run. After that run is persisted or explicitly handled, invoke:

`review range-resume --range-id "<RANGE_ID>" --format json`

The runtime prepares the next chapter only after the current one finishes. On a blocker or failure, stop by default. Present the returned `stop`/`continue` decision with its exact binding marker on a line by itself and pause for the real user answer. Then create the same strict decision request outside the project, changing only `kind` to `range`, setting `run_id` to `null`, setting the exact `range_id`, and naming the trusted parent rollout; it still contains no choice. Invoke `review range-decide --range-id "<RANGE_ID>" --request-file "<ABSOLUTE_DECISION_REQUEST_JSON>" --format json`. Never run range reviewers in parallel and never accept more than five chapters.

## Recover safely

Use `review resume --run-id "<RUN_ID>" --format json` after interruption. Recovery reuses validated reviewer artifacts and resumes report or database persistence; it must not invoke the reviewer again. A successful database write is detected by readback, so retry does not duplicate it. If input or artifact hashes changed, stop as stale and prepare a new review rather than rewriting ledger evidence.

## Report the outcome

Report run/range IDs, chapter, mode, actual verified reviewer model and effort, issue/blocking counts, artifact paths and hashes, database status, and any pending decision. Do not say completed while runtime evidence, report, metrics, database readback, or the current range chapter remains pending.
