---
name: webnovel-query
description: Query a webnovel project's canonical Story System facts and derived read models without modifying files. Use for entity state, relationships, world rules, open loops or foreshadowing, chapter summaries, and combined chapter context. Preserve source paths, line applicability, and legacy fallback labels; do not use for edits or project repair.
---

# Webnovel Query

Answer project-fact questions from the narrowest trustworthy source. Keep canonical Story System data distinct from derived `.webnovel` read models.

## Required reference

Load [Runtime invocation](../../references/codex/runtime-invocation.md) before every query. It defines stable root discovery, argument-vector construction, UTF-8 handling, JSON parsing, and exit codes.

Load at most the relevant private reference:

- [System data flow](references/system-data-flow.md) for source priority and fallback semantics.
- [Foreshadowing](references/advanced/foreshadowing.md) for open-loop urgency interpretation.
- [Tag specification](references/tag-specification.md) only when the user explicitly asks about optional manual tags.

## Resolve the project

Resolve the plugin root from this loaded `SKILL.md`. Resolve the novel project separately with:

`python -X utf8 "<PLUGIN_ROOT>/scripts/webnovel.py" --project-root "<ABSOLUTE_WORKSPACE_OR_PROJECT>" where --format json`

Parse exactly one object. Preserve `resolved_from` and `compatibility_mode`; never treat a legacy pointer as native Story System evidence.

## Route to the narrowest query

Never interpolate user prose into a shell command. For an entity/name or optional domain supplied by the user, create a temporary UTF-8-without-BOM JSON request through the host's native file-writing API, outside the novel project. Do not create the request with PowerShell, shell redirection, or string interpolation. Delete it with the host file API after the query.

| Query type | Stable runtime invocation after the common script and project-root arguments |
|---|---|
| Entity state at chapter N | `knowledge query-entity-state --request-file <ABSOLUTE_QUERY_REQUEST_JSON>` |
| Relationships at chapter N | `knowledge query-relationships --request-file <ABSOLUTE_QUERY_REQUEST_JSON>` |
| World rules | `memory-contract --read-only --with-provenance --request-file <ABSOLUTE_QUERY_REQUEST_JSON> query-rules` |
| Open loops | `memory-contract --read-only --with-provenance get-open-loops [--status active]` |
| Combined chapter context | `memory-contract --read-only --with-provenance load-context --chapter <N>` |
| One chapter summary | `memory-contract --read-only --with-provenance read-summary --chapter <N>` |

Use schema `webnovel-query-request/v1`. Set `query_type` to `entity_state` or `relationships` with `entity` and `at_chapter`; set it to `world_rules` with optional `domain`. The runtime rejects unknown fields, wrong types, BOM, oversized files, symlinks, relative paths, and request files inside the novel project. World rules use `query-rules` with optional `domain`; that command does not accept `--chapter`. Chapter-specific context uses `load-context` or `read-summary`.

For a static setting question that is best answered from `设定集` or `大纲`, use the host's read-only file search and reader rather than a shell pipeline. Restrict the search to the resolved project, return the exact relative path and the smallest relevant line range, and do not inspect plugin or unrelated workspace files.

## Source priority and fallback

Prefer evidence in this order:

1. `.story-system/MASTER_SETTING.json`, volume and chapter contracts.
2. The latest accepted `.story-system/commits/chapter_NNN.commit.json` at or before the requested chapter.
3. `.webnovel/memory_scratchpad.json`, `summaries/chNNNN.md`, and `index.db` as derived read models.
4. Static `设定集` and `大纲` text when the question is explicitly about those authored files.

Never call a derived read model canonical. When the runtime returns `legacy_projection_fallback`, missing contracts, or a missing accepted commit, state the downgrade prominently. SQLite rows have no source line number: report `line: not applicable` rather than inventing one. Markdown or text excerpts must include real line numbers.

## Output contract

Return:

1. A short answer to the user's actual question.
2. Query type and effective chapter or domain.
3. Source entries with role (`authoritative`, `derived`, `authored_context`, `reference`, or `non_authoritative`), path, line range or `not applicable`, and fallback label.
4. Ambiguities, missing data, or conflicts between canonical and derived data.

Parse the single `webnovel-query-result/v1` envelope for every query type. If an entity name has multiple candidates, show the finite candidate IDs and wait for the user to choose; never silently select one. A Knowledge query whose required `index.db` or table is missing returns exit code `1`; report that blocker and suggest `$webnovel-doctor`. Optional summary or scratchpad data may instead return exit code `0` with an empty result or `sources[].exists=false`; report the absence without claiming a crash. Never create or repair a database.

## Safety boundaries

- Query is strictly read-only. Do not initialize or migrate databases, update projections, repair files, install dependencies, start services, change Git state, or access the network.
- Keep Chinese names, single and double quotes, newlines, and PowerShell metacharacters such as ampersands, semicolons, pipes, dollar signs, parentheses, and backticks inside the JSON request file. User text must never appear in the PowerShell command string.
- Do not expose environment secrets or paste large project files into the answer.
