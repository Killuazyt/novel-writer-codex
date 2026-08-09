# Query data flow

Use this reference to distinguish canonical facts from derived read models.

## Source roles

| Role | Path | Meaning |
|---|---|---|
| Authoritative prewrite contract | `.story-system/MASTER_SETTING.json` | Global setting and hard constraints |
| Authoritative prewrite contract | `.story-system/volumes/volume_NNN.json` | Volume goals and pacing contract |
| Authoritative prewrite contract | `.story-system/chapters/chapter_NNN.json` | Chapter-specific contract |
| Authoritative postwrite fact | `.story-system/commits/chapter_NNN.commit.json` with accepted status | Published chapter fact record |
| Derived read model | `.webnovel/index.db` | Entity, alias, relationship, state-change, chapter, and scene projections |
| Derived read model | `.webnovel/memory_scratchpad.json` | Projected rules, open loops, and timeline memory |
| Derived read model | `.webnovel/summaries/chNNNN.md` | Projected chapter summary |
| Lightweight projection | `.webnovel/state.json` | Progress and selected current-state views |

The `.webnovel` layer is convenient for queries but does not override a conflicting Story System contract or accepted commit. If required contracts or an accepted commit are absent, label derived results `legacy_projection_fallback`.

## Read-only command map

Use the stable runtime and separate arguments:

- Entity history: `knowledge query-entity-state --request-file <ABSOLUTE_QUERY_REQUEST_JSON>`
- Relationships: `knowledge query-relationships --request-file <ABSOLUTE_QUERY_REQUEST_JSON>`
- Rules: `memory-contract --read-only --with-provenance --request-file <ABSOLUTE_QUERY_REQUEST_JSON> query-rules`
- Open loops: `memory-contract --read-only --with-provenance get-open-loops --status active`
- Combined context: `memory-contract --read-only --with-provenance load-context --chapter <N>`
- Summary: `memory-contract --read-only --with-provenance read-summary --chapter <N>`

Put user-supplied entity names and domains only in the strict `webnovel-query-request/v1` file described by the main Skill. These query paths must never initialize, migrate, repair, or update the project.

## Line-number rules

- Text and Markdown excerpts cite the actual file and smallest relevant line range.
- JSON objects may cite a path and a line range only when the reader returned exact lines.
- SQLite results always use `line: not applicable`.
- Missing files remain explicit source entries with `exists=false`; do not hide them.
