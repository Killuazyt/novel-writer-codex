---
name: webnovel-setup
description: Check and provision the five project-scoped Codex agents required by Novel Writer Codex. Use when the user asks to set up, install, repair, update, or verify webnovel project agents, when another webnovel skill reports agent_unavailable or an agent contract hash mismatch, or before the first agent-backed webnovel workflow in a workspace. Do not use it to initialize a novel, install dependencies, or change Git state.
---

# Webnovel Setup

Provision only the managed project agents. Treat checking and applying as separate phases, and never turn a check into a write without the user's explicit choice.

## Required references

Load both files before running the workflow:

- [Runtime invocation](../../references/codex/runtime-invocation.md) for stable path resolution, command construction, output handling, and exit codes.
- [Interaction contract](../../references/codex/interaction-contract.md) for the finite-choice confirmation and permission boundary.

## Workflow

1. Resolve the plugin root from this loaded `SKILL.md` path. Resolve the current Codex workspace root independently. Do not infer either location from a novel project pointer or legacy host configuration.
2. Resolve the host Python 3 interpreter as required by the runtime contract (`python` on Windows when it is Python 3; normally `python3` on POSIX). Run the stable runtime with an absolute workspace path:

   `python -X utf8 "<PLUGIN_ROOT>/scripts/webnovel.py" codex-setup --workspace-root "<WORKSPACE_ROOT>" --check --format json`

3. Parse exactly one JSON object and branch on `status`:

   - `current`: report that all five agents match the managed contracts. Make no changes.
   - `changes_required`: show the exact `created`, `updated`, and `unchanged` lists. Ask one finite-choice question with “Apply the managed agent changes” first and “Leave the workspace unchanged” second. Wait for the answer.
   - `conflict`: report every conflicting path and stop. Never overwrite an unmanaged same-name agent and never invent a force option.
   - `failed`: report the supplied error and repair guidance, then stop.

4. Only after the user selects the apply branch, run:

   `python -X utf8 "<PLUGIN_ROOT>/scripts/webnovel.py" codex-setup --workspace-root "<WORKSPACE_ROOT>" --apply --format json`

   On POSIX, use the resolved `python3` executable for the same argument vector.

5. Accept success only when the result is `applied`, contains no conflicts, and sets `restart_required` to `true`. Run the check command once more; it must return `current`.
6. Report created, updated, unchanged, and backup paths. Tell the user to open a new Codex task before invoking an agent-backed webnovel skill. Do not try the newly installed agents in the current task.

## Safety boundaries

- Setup may write only `.codex/agents/*.toml` and `.codex/novel-writer-codex/**` under the resolved workspace.
- It must not create a novel project or modify `.story-system`, `.webnovel`, `正文`, `设定集`, `大纲`, `.claude`, dependency state, or Git state.
- A user's workflow choice is not a Codex filesystem, command, network, or privilege approval. Request native permission separately when the host requires it.
- Agent absence, hash drift, an unavailable required model, or an actual-model mismatch is a blocker. Do not simulate a missing agent or fall back to the parent model.
