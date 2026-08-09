#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only command line helpers for the manual M3 Codex smoke."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from data_modules.codex_m3_smoke import (
    SmokeEvidenceError,
    build_hook_trust_plan,
    parse_parent_rollout_identity,
    parse_rollout_runtime_evidence,
    probe_codex_cli,
    validate_hook_trust_evidence,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    probe = commands.add_parser("probe", help="Check whether the local Codex CLI can start")
    probe.add_argument("--codex")

    rollout = commands.add_parser("rollout", help="Verify one explicit Codex child rollout")
    rollout.add_argument("--path", required=True)
    rollout.add_argument("--sessions-root")
    rollout.add_argument("--thread-id", required=True)
    rollout.add_argument("--parent-thread-id", required=True)
    rollout.add_argument("--agent-role", required=True)
    rollout.add_argument("--model", required=True)
    rollout.add_argument("--effort", required=True)

    parent_rollout = commands.add_parser(
        "parent-rollout", help="Verify one explicit Codex parent task rollout"
    )
    parent_rollout.add_argument("--path", required=True)
    parent_rollout.add_argument("--sessions-root")
    parent_rollout.add_argument("--thread-id", required=True)
    parent_rollout.add_argument("--model", required=True)
    parent_rollout.add_argument("--effort", required=True)

    hook_plan = commands.add_parser("hook-plan", help="Print the fail-closed trust checklist")
    hook_plan.add_argument("--hooks-config", required=True)
    hook_plan.add_argument("--workspace-root", required=True)

    verify_hook = commands.add_parser("verify-hook", help="Validate captured two-phase hook evidence")
    verify_hook.add_argument("--evidence", required=True)
    verify_hook.add_argument("--hooks-config", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "probe":
            result = probe_codex_cli(args.codex)
        elif args.command == "rollout":
            result = {
                "status": "accepted",
                "accepted": True,
                "code": "ok",
                "evidence": asdict(
                    parse_rollout_runtime_evidence(
                        args.path,
                        sessions_root=args.sessions_root,
                        expected_thread_id=args.thread_id,
                        expected_parent_thread_id=args.parent_thread_id,
                        expected_agent_role=args.agent_role,
                        expected_model=args.model,
                        expected_reasoning_effort=args.effort,
                    )
                ),
            }
        elif args.command == "parent-rollout":
            result = {
                "status": "accepted",
                "accepted": True,
                "code": "ok",
                "evidence": asdict(
                    parse_parent_rollout_identity(
                        args.path,
                        sessions_root=args.sessions_root,
                        expected_thread_id=args.thread_id,
                        expected_model=args.model,
                        expected_reasoning_effort=args.effort,
                    )
                ),
            }
        elif args.command == "hook-plan":
            result = build_hook_trust_plan(
                args.hooks_config,
                workspace_root=args.workspace_root,
            )
        else:
            evidence = json.loads(Path(args.evidence).read_text(encoding="utf-8"))
            result = validate_hook_trust_evidence(
                evidence,
                hooks_config=args.hooks_config,
            )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, SmokeEvidenceError) as exc:
        result = {
            "status": "blocked",
            "accepted": False,
            "code": "invalid_smoke_evidence",
            "detail": str(exc),
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("status") in {"accepted", "available"} else 2


if __name__ == "__main__":
    sys.exit(main())
