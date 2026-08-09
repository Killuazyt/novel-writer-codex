---
name: webnovel-learn
description: Save an author-approved writing pattern into a webnovel project's durable project memory. Use when the user asks Codex to remember a successful hook, pacing, dialogue, payoff, emotion, formatting, or other reusable writing technique for later chapters. This is a narrow controlled write; do not use it to edit canon, chapters, outlines, or Story System contracts.
---

# Webnovel Learn

Store exactly one user-approved writing pattern through the runtime. Never edit `.webnovel/project_memory.json` directly.

## Resolve the project

Load [Runtime invocation](../../references/codex/runtime-invocation.md), then resolve the novel project with `where --format json`. Stop on an unresolved project or `legacy_read_only` compatibility mode.

## Prepare the request

Classify the approved pattern as one of `hook`, `pacing`, `dialogue`, `payoff`, `emotion`, `format`, or `other`. Use importance `high`, `medium`, or `low`; default to `medium`. Include `source_chapter` only when it is a known positive chapter number.

Create one temporary UTF-8-without-BOM JSON file outside the novel project through the host's native file-writing API. Never use PowerShell, shell redirection, or string interpolation to create it. Use this exact shape:

```json
{
  "schema_version": "webnovel-learn-request/v1",
  "pattern_type": "pacing",
  "description": "The author-approved pattern, preserved as one JSON string.",
  "category": "optional category",
  "importance": "medium",
  "source_chapter": 12
}
```

Keep Chinese text, quotes, newlines, ampersands, semicolons, pipes, dollar signs, parentheses, and backticks inside the JSON file. User text must never appear in a PowerShell command string.

## Persist through the runtime

Invoke only:

`python -X utf8 "<PLUGIN_ROOT>/scripts/webnovel.py" --project-root "<PROJECT_ROOT>" project-memory add-pattern --input-json "<ABSOLUTE_REQUEST_JSON>"`

Parse one `webnovel-learn-result/v1` object. Treat `status=success` as appended and `status=skipped, reason=duplicate` as an idempotent success. On `status=error`, report the blocker without repairing or overwriting memory. Delete the temporary request with the host file API after the command.

## Boundaries

- Require an explicit request to remember the pattern; do not infer durable memory from ordinary praise.
- Do not delete or rewrite old patterns, initialize Git, access the network, or touch chapters, outlines, settings, commits, state, indexes, summaries, or Story System contracts.
- Do not bypass a damaged `project_memory.json`. Preserve it unchanged and ask the user to inspect or repair it later.
- Report the learned object, duplicate status, source chapter, and destination path without exposing unrelated project data.
