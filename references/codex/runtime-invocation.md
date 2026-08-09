# Codex runtime invocation contract

Use this contract from every `webnovel-*` Skill and project Agent.

## Locate the runtime

1. Start from the absolute path of the loaded Skill or installed Agent contract.
2. Resolve the plugin root that contains `.codex-plugin/plugin.json` and `scripts/webnovel.py`.
3. Invoke that absolute script path. Do not depend on the current directory, a host-provided plugin-root variable, or a legacy host directory.
4. Pass the workspace or novel project as an explicit absolute argument whenever the command supports it.

## Construct commands

- Resolve a Python 3 interpreter through the host process launcher before building the command. Use `python` on Windows when it resolves to Python 3, and prefer `python3` on POSIX hosts; do not assume that the POSIX `python` alias exists.
- Add `-X utf8` and pass each argument separately through the available process API.
- Keep user prose out of command strings. When a command accepts a request file, put structured or multiline user input in that UTF-8 JSON request; otherwise pass a scalar only through a verified non-shell argument-vector API.
- Quote paths in displayed examples and preserve Chinese characters, spaces, parentheses, ampersands, and other Unicode characters.
- Do not build pipelines, command substitutions, shell iteration, or host-specific redirection into a distributed prompt.
- Do not change directories merely to make a relative path work.

## Bind the active Codex task

- A production command that consumes a parent-rollout identity or a user-decision receipt must read the non-empty canonical UUID in the host-injected `CODEX_THREAD_ID` environment value and require every claimed parent thread to equal it.
- Do not accept a thread ID from CLI arguments, request JSON, a copied rollout, or Agent self-report as a substitute for the host value. Do not override or print the environment value.
- Treat this as host-bound live evidence, not as a native permission or cryptographic credential. If the host does not expose it, or if it is malformed or mismatched, return a structured live-gate blocker and perform no protected write.
- A workflow intentionally resumed in a different top-level task needs a new scope-bound recovery/decision receipt for that task. An old task's unanswered marker never authorizes the new task.

Stable entry shape:

- Windows: `python -X utf8 "<PLUGIN_ROOT>/scripts/webnovel.py" <command> <arguments>`
- POSIX: `python3 -X utf8 "<PLUGIN_ROOT>/scripts/webnovel.py" <command> <arguments>`

## Handle results

- When JSON is available, request it and parse exactly one top-level object.
- Treat exit code `0` as success or an already-satisfied state, `1` as a reported blocker or actionable conflict, and `2` as invalid input or execution failure.
- A documented exit code `1` is not a crashed Skill. Surface its blocker and repair guidance without a traceback.
- Never print secret environment values. Report only the source name, field name, redacted status, and safe paths.
- Verify every write through a read-only status or validation command before reporting completion.

## Preserve runtime ownership

Skills and Agents orchestrate the Python runtime; they do not reproduce commit, projection, memory, review, or path-resolution logic. Protected novel facts may change only through the stable runtime gates.
