---
name: webnovel-dashboard
description: Start, inspect, or stop a project-scoped, read-only local webnovel Dashboard. Use when the user asks to view novel progress, characters, pacing, contracts, projections, files, Dashboard health, the local Dashboard URL, or service status. Bind only to loopback, never open a browser automatically, and never install dependencies or repair project data.
---

# Webnovel Dashboard

Control the bundled Dashboard only through the unified lifecycle runtime. The service may create lifecycle metadata and logs under `WEBNOVEL_HOME/runtime/dashboard`, but it must not change novel facts.

## Required reference

Load [Runtime invocation](../../references/codex/runtime-invocation.md) before running this workflow. Resolve the plugin root from this loaded `SKILL.md` and resolve the novel project independently.

## Choose one action

- Inspect without starting:

  `python -X utf8 "<PLUGIN_ROOT>/scripts/webnovel.py" --project-root "<ABSOLUTE_PROJECT_OR_WORKSPACE>" dashboard status --format json`

- Start on a dynamic loopback port:

  `python -X utf8 "<PLUGIN_ROOT>/scripts/webnovel.py" --project-root "<ABSOLUTE_PROJECT_OR_WORKSPACE>" dashboard start --host 127.0.0.1 --port 0 --no-browser --format json`

- Stop only the verified project instance:

  `python -X utf8 "<PLUGIN_ROOT>/scripts/webnovel.py" --project-root "<ABSOLUTE_PROJECT_OR_WORKSPACE>" dashboard stop --format json`

On POSIX, use the resolved `python3` interpreter. Pass arguments separately through the host process API. Do not invoke `dashboard.server` directly.

## Interpret the result

- `running` or `already_running`: report the returned loopback URL, PID, log path, and health endpoints.
- `not_running` or `stopped`: report the idempotent state; do not start again unless requested.
- `blocked`: report the dependency, asset, port, lifecycle-lock, or identity blocker and its manual repair guidance. Exit code `1` is a handled blocker.
- `failed`: report the safe error and log path when present. Exit code `2` is an invalid or failed lifecycle operation.

After `start`, run `dashboard status --format json` once and require the same PID, port, and project root before reporting success. After `stop`, run status once and require `not_running`.

## Safety boundaries

- Bind only to `127.0.0.1`. Never use `0.0.0.0`, a LAN address, a hostname from runtime state, or a URL supplied by project files.
- Never open a browser automatically. Return the local URL for the user to open deliberately.
- Do not install Python or Node dependencies, run package managers, rebuild the frontend, access the network, initialize Git, or change project facts.
- Treat lifecycle state as untrusted. Let the runtime validate project hash, instance token, PID, and live identity; never signal a PID yourself.
- Do not expose the private instance token or environment secrets. Do not claim a missing dependency was repaired.
