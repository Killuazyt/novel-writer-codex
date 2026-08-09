# Finite-choice interaction contract

Use this contract whenever an author decision can materially change files, canon, workflow direction, recovery behavior, or review scope. Keep author decisions separate from Codex permission review.

## Ask

- Ask one to three short questions at a time.
- Give each question two or three mutually exclusive options.
- Put the recommended option first and state its concrete effect in one sentence.
- Preserve a free-form response path.
- Prefer the client's structured-choice control when it is available.
- Otherwise present the same choices as a numbered list and explicitly ask the user to reply with a number or a clear free-form choice.

## Wait and resume

- Persist or return a pending decision record before yielding. The record contains a stable decision ID, the allowed branch IDs, and no selected branch.
- Do not run a write, choose the recommended option, or advance the workflow while the decision is pending.
- Reject an ambiguous or out-of-range answer and ask the same question again without side effects.
- After a valid answer, record exactly one selected branch and execute only that branch.
- If the user chooses cancel, leave the protected state unchanged and report the stopped stage.

## Permission boundary

An author choice authorizes only the selected business branch. It does not authorize filesystem access outside the active sandbox, command escalation, network access, dependency installation, Git operations, or external publication. Use Codex's native permission flow separately for those actions.

Do not turn native permission prompts into story choices, and do not describe an author choice as a security approval.
