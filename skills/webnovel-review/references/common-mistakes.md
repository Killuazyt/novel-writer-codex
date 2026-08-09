# Factual review guide

Use this guide only to classify evidence-backed issues returned by the managed reviewer. Do not turn subjective prose preferences into blocking issues.

| Dimension | Report only when | Typical evidence |
|---|---|---|
| `setting` | A stated ability, realm, place, item, currency, rule, limit, or cost conflicts with trusted context | The shortest chapter excerpt plus the conflicting contract, accepted fact, or derived context entry |
| `timeline` | Time order, countdown, travel time, or simultaneous location is impossible or contradicts trusted context | Both time statements and their source locations |
| `continuity` | A prior hook, scene transition, physical state, or immediate action changes without an established bridge | Adjacent chapter/scene excerpts that demonstrate the break |
| `character` | Dialogue or action contradicts established motivation, personality, or knowledge boundary | The current excerpt plus a specific established character fact |
| `logic` | Cause, decision, power comparison, or outcome does not follow from established facts | The premise and consequence that cannot both hold |

Require non-empty `location`, `description`, `evidence`, and `fix_hint`. Keep the fix hint local and planning-preserving. Use `critical` only for a definite fact contradiction and always set `blocking=true` for it.

Do not report generic claims such as “not exciting,” “too slow,” “needs a twist,” “AI-like,” or “should add a cool point.” Do not read `state.json`, databases, summaries, or Story System files directly when the request package provides a runtime context artifact.
