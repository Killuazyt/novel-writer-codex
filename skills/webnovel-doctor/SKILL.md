---
name: webnovel-doctor
description: Run a strictly read-only, phase-aware health check for a webnovel project. Use when the user asks to diagnose, inspect, check, troubleshoot, or verify project initialization, planning readiness, writing readiness, dependencies, SQLite projections, RAG configuration, or Dashboard assets. Do not repair files, install dependencies, or start services.
---

# Webnovel Doctor

Diagnose the current novel project without changing it. A reported blocker is a valid diagnostic result, not a crashed Skill.

## Required reference

Load [Runtime invocation](../../references/codex/runtime-invocation.md) before running this workflow. Use its path-resolution, argument-vector, UTF-8, JSON, exit-code, and secret-redaction rules.

## Workflow

1. Resolve the plugin root from this loaded `SKILL.md`. Resolve the current workspace or explicitly named novel project independently; never infer the plugin root from project state.
2. Run the short status first with an absolute candidate path:

   `python -X utf8 "<PLUGIN_ROOT>/scripts/webnovel.py" --project-root "<ABSOLUTE_PROJECT_OR_WORKSPACE>" project-status --format json`

   On POSIX, use the resolved `python3` executable. Parse exactly one JSON object. Preserve `phase`, `target_chapter`, `blocking`, `warnings`, and `next_action` for the final report.
3. Run the phase-aware doctor in JSON mode:

   `python -X utf8 "<PLUGIN_ROOT>/scripts/webnovel.py" --project-root "<ABSOLUTE_PROJECT_OR_WORKSPACE>" doctor --format json`

   If the user requested a chapter, add `--chapter <POSITIVE_INTEGER>`. If the user explicitly requested a deep check, add `--deep`. Pass every argument separately; never splice user text into a command string.
4. Interpret exit codes using the runtime contract:

   - `0`: no blocking diagnostic was found. Warnings can still require attention.
   - `1`: the doctor found one or more blockers. Parse and report them normally; do not describe this as a Skill failure.
   - `2`: the invocation or input is invalid. Report the safe error and stop.
5. Report the resolved project root, phase, target chapter, doctor mode, blocker and warning counts, and each non-OK check. For every issue include its source path when present, impact, and recommended action. Never print API-key values; only report configured or missing.

## Stage expectations

- No project: explain that no valid `.webnovel/state.json` project root was resolved and point to `$webnovel-init` or explicit project selection.
- Initialized: do not require chapter contracts before planning has begun.
- Planning: identify incomplete master, volume, chapter, or review contracts for the requested chapter.
- Writing: validate the target chapter contracts, draft/artifact state, projection status, and read models appropriate to the detected phase.
- Deep mode: additionally inspect Dashboard package assets. It still must not import or start the Dashboard service.

## Safety boundaries

- This Skill is read-only. Do not repair files, create missing directories, update projections, install dependencies, start Dashboard, open a browser, change Git state, or access the network.
- Do not read protected fact paths through an ad-hoc shell command. Use the stable runtime, whose Doctor SQLite checks open databases in read-only mode.
- Do not ask for secrets and do not expose values loaded from environment files.
- Never turn a diagnostic recommendation into an automatic action. Return control to the user after the report.
