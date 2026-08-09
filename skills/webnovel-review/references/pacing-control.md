# Pacing requests under the strict Review contract

The frozen upstream skill included score-based pacing and word-density rules. Those rules are not part of the Codex factual reviewer contract and must not be copied into issue severity, blocking decisions, metrics, or reports.

When a user asks for “pacing review,” keep this Skill within its five supported factual dimensions:

- Report a `timeline` issue when elapsed time, countdown, travel duration, or event order contradicts trusted facts.
- Report a `continuity` issue when a scene or action jumps without the bridge required by the immediately preceding text.
- Do not report word-count targets, cool-point quotas, information-density scores, genre formulas, or general prose taste as factual issues.
- Explain that subjective pacing critique needs a separate authorial/editorial workflow if no evidence-backed timeline or continuity defect exists.

Never add a `pacing` category to reviewer JSON. Never manufacture an issue so that a requested pacing review appears non-empty.
