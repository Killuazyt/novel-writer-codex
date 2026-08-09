#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

import codex_m3_smoke as codex_m3_smoke_cli
from data_modules import codex_m3_smoke

from data_modules.codex_agent_runtime import (
    VerifiedRuntimeEvidence,
    build_canned_envelope,
    build_workflow_route,
    validate_agent_envelope,
)
from data_modules.codex_m3_smoke import (
    HOOK_TRUST_SCHEMA_VERSION,
    SmokeEvidenceError,
    VerifiedParentEvidence,
    build_codex_exec_argv,
    build_hook_trust_plan,
    parse_parent_rollout_identity,
    parse_rollout_runtime_evidence,
    probe_codex_cli,
    validate_hook_trust_evidence,
    validate_parent_model_matrix,
)


ROOT = Path(__file__).resolve().parents[2]


def _write_rollout(
    sessions_root: Path,
    *,
    thread_id: str = "child-001",
    parent_id: str = "parent-001",
    role: str = "webnovel_writer",
    model: str = "gpt-5.6-luna",
    effort: str = "medium",
    omit_session_model: bool = False,
) -> Path:
    path = sessions_root / "2026" / "08" / "07" / f"rollout-test-{thread_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    events = [
        {
            "type": "session_meta",
            "payload": {
                "id": thread_id,
                "parent_thread_id": parent_id,
                "model": None if omit_session_model else model,
                "source": {
                    "subagent": {
                        "thread_spawn": {
                            "parent_thread_id": parent_id,
                            "depth": 1,
                            "agent_path": "webnovel_writer",
                            "agent_nickname": "writer",
                            "agent_role": role,
                        }
                    }
                },
            },
        },
        {"type": "response_item", "payload": {"type": "developer_message"}},
        {
            "type": "turn_context",
            "payload": {"turn_id": "turn-001", "model": model, "effort": effort},
        },
        {"type": "response_item", "payload": {"type": "message"}},
    ]
    path.write_text(
        "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n",
        encoding="utf-8",
    )
    return path


def _parse(path: Path, sessions_root: Path, **overrides) -> VerifiedRuntimeEvidence:
    arguments = {
        "expected_thread_id": "child-001",
        "expected_parent_thread_id": "parent-001",
        "expected_agent_role": "webnovel_writer",
        "expected_model": "gpt-5.6-luna",
        "expected_reasoning_effort": "medium",
        "sessions_root": sessions_root,
    }
    arguments.update(overrides)
    return parse_rollout_runtime_evidence(path, **arguments)


def _rollout_events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _replace_rollout_events(path: Path, events: list[object]) -> None:
    path.write_text(
        "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n",
        encoding="utf-8",
    )


def test_explicit_codex_rollout_is_verified_and_can_validate_envelope(tmp_path: Path) -> None:
    sessions_root = tmp_path / ".codex" / "sessions"
    path = _write_rollout(sessions_root)
    evidence = _parse(path, sessions_root)
    step = build_workflow_route("write", parent_model="gpt-5.6-sol")["steps"][1]
    envelope = build_canned_envelope(step, evidence_source="codex_trace")

    result = validate_agent_envelope(step, envelope, verified_evidence=evidence)

    assert result["accepted"] is True
    assert evidence.agent_name == "webnovel_writer"
    assert evidence.actual_model == "gpt-5.6-luna"
    assert evidence.actual_reasoning_effort == "medium"


def test_rollout_accepts_missing_session_model_when_turn_context_is_authoritative(
    tmp_path: Path,
) -> None:
    sessions_root = tmp_path / ".codex" / "sessions"
    path = _write_rollout(sessions_root, omit_session_model=True)

    evidence = _parse(path, sessions_root)

    assert evidence.actual_model == "gpt-5.6-luna"


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"expected_thread_id": "different-child"}, "filename"),
        ({"expected_parent_thread_id": "different-parent"}, "parent"),
        ({"expected_agent_role": "webnovel_reviewer"}, "role"),
        ({"expected_model": "gpt-5.6-sol"}, "model"),
        ({"expected_reasoning_effort": "high"}, "effort"),
    ],
)
def test_rollout_identity_mismatch_fails_closed(
    tmp_path: Path,
    override: dict,
    message: str,
) -> None:
    sessions_root = tmp_path / ".codex" / "sessions"
    path = _write_rollout(sessions_root)

    with pytest.raises(SmokeEvidenceError, match=message):
        _parse(path, sessions_root, **override)


def test_workspace_fixture_cannot_masquerade_as_codex_session(tmp_path: Path) -> None:
    sessions_root = tmp_path / ".codex" / "sessions"
    source = _write_rollout(sessions_root)
    outside = tmp_path / source.name
    outside.write_bytes(source.read_bytes())

    with pytest.raises(SmokeEvidenceError, match="sessions root"):
        _parse(outside, sessions_root)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("too_short", "lacks session_meta"),
        ("duplicate_session", "exactly one session_meta"),
        ("missing_turn", "lacks turn_context"),
        ("turn_payload", "turn_context payloads"),
        ("session_payload", "event payloads"),
        ("missing_turn_id", "turn_id"),
        ("conflicting_turn", "conflicting turn_context"),
        ("not_subagent", "thread_spawn"),
        ("child_thread_id", "child thread id"),
    ],
)
def test_rollout_jsonl_structure_errors_fail_closed(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    sessions_root = tmp_path / ".codex" / "sessions"
    path = _write_rollout(sessions_root)
    events = _rollout_events(path)
    if mutation == "too_short":
        events = [{}]
    elif mutation == "duplicate_session":
        events.insert(1, events[0].copy())
    elif mutation == "missing_turn":
        events = [event for event in events if event.get("type") != "turn_context"]
    elif mutation == "turn_payload":
        events[2]["payload"] = "not-an-object"
    elif mutation == "session_payload":
        events[0]["payload"] = "not-an-object"
    elif mutation == "missing_turn_id":
        events[2]["payload"]["turn_id"] = ""
    elif mutation == "conflicting_turn":
        events.insert(
            3,
            {
                "type": "turn_context",
                "payload": {
                    "turn_id": "turn-002",
                    "model": "gpt-5.6-luna",
                    "effort": "high",
                },
            },
        )
    elif mutation == "not_subagent":
        events[0]["payload"]["source"] = {}
    else:
        events[0]["payload"]["id"] = "other-child"
    _replace_rollout_events(path, events)

    with pytest.raises(SmokeEvidenceError, match=message):
        _parse(path, sessions_root)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"\xef\xbb\xbf{}\n", "without BOM"),
        (b"\xff\xfe", "UTF-8 JSONL"),
        (b"{not-json}\n", "UTF-8 JSONL"),
    ],
)
def test_rollout_encoding_errors_are_rejected(
    tmp_path: Path,
    raw: bytes,
    message: str,
) -> None:
    sessions_root = tmp_path / ".codex" / "sessions"
    path = _write_rollout(sessions_root)
    path.write_bytes(raw)

    with pytest.raises(SmokeEvidenceError, match=message):
        _parse(path, sessions_root)


def test_missing_and_unreadable_rollouts_are_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions_root = tmp_path / ".codex" / "sessions"
    missing = sessions_root / "rollout-test-child-001.jsonl"
    with pytest.raises(SmokeEvidenceError, match="missing"):
        _parse(missing, sessions_root)

    path = _write_rollout(sessions_root)
    real_read_bytes = Path.read_bytes

    def denied(candidate: Path) -> bytes:
        if candidate.resolve() == path.resolve():
            raise OSError("simulated read denial")
        return real_read_bytes(candidate)

    monkeypatch.setattr(Path, "read_bytes", denied)
    with pytest.raises(SmokeEvidenceError, match="unreadable"):
        _parse(path, sessions_root)


def test_rollout_expected_identity_must_name_a_supported_agent(tmp_path: Path) -> None:
    sessions_root = tmp_path / ".codex" / "sessions"
    path = _write_rollout(sessions_root)

    with pytest.raises(SmokeEvidenceError, match="identities must be explicit"):
        _parse(path, sessions_root, expected_agent_role="unsupported-agent")


def test_parent_rollout_binds_thread_model_effort_and_hash(tmp_path: Path) -> None:
    sessions_root = tmp_path / ".codex" / "sessions"
    path = _write_rollout(
        sessions_root,
        thread_id="parent-sol",
        parent_id="root",
        role="webnovel_writer",
        model="gpt-5.6-sol",
        effort="high",
    )

    evidence = parse_parent_rollout_identity(
        path,
        sessions_root=sessions_root,
        expected_thread_id="parent-sol",
        expected_model="gpt-5.6-sol",
        expected_reasoning_effort="high",
    )

    assert evidence.thread_id == "parent-sol"
    assert evidence.model == "gpt-5.6-sol"
    assert evidence.reasoning_effort == "high"
    assert len(evidence.raw_sha256) == 64


@pytest.mark.parametrize(
    ("mutation", "overrides", "message"),
    [
        ("none", {"expected_model": ""}, "identities must be explicit"),
        ("thread", {}, "thread id mismatch"),
        ("none", {"expected_model": "gpt-5.6-terra"}, "model mismatch"),
        ("none", {"expected_reasoning_effort": "medium"}, "effort mismatch"),
    ],
)
def test_parent_rollout_identity_errors_fail_closed(
    tmp_path: Path,
    mutation: str,
    overrides: dict[str, str],
    message: str,
) -> None:
    sessions_root = tmp_path / ".codex" / "sessions"
    path = _write_rollout(
        sessions_root,
        thread_id="parent-sol",
        model="gpt-5.6-sol",
        effort="high",
    )
    if mutation == "thread":
        events = _rollout_events(path)
        events[0]["payload"]["id"] = "different-parent"
        _replace_rollout_events(path, events)
    arguments = {
        "sessions_root": sessions_root,
        "expected_thread_id": "parent-sol",
        "expected_model": "gpt-5.6-sol",
        "expected_reasoning_effort": "high",
    }
    arguments.update(overrides)

    with pytest.raises(SmokeEvidenceError, match=message):
        parse_parent_rollout_identity(path, **arguments)


def test_parent_model_matrix_requires_two_complete_fixed_role_sets() -> None:
    observations = []
    for parent in ("gpt-5.6-sol", "gpt-5.6-terra"):
        parent_evidence = VerifiedParentEvidence(
            evidence_source="codex_trace",
            thread_id=f"parent-{parent}",
            model=parent,
            reasoning_effort="high",
            raw_sha256="b" * 64,
        )
        for role in (
            "webnovel_context_agent",
            "webnovel_writer",
            "webnovel_reviewer",
            "webnovel_data_agent",
        ):
            observations.append(
                {
                    "parent_evidence": parent_evidence,
                    "workflow": "review" if role == "webnovel_reviewer" else "write",
                    "evidence": VerifiedRuntimeEvidence(
                        evidence_source="codex_trace",
                        agent_name=role,
                        actual_model="gpt-5.6-luna",
                        actual_reasoning_effort="medium",
                        thread_id=f"{parent}-{role}",
                        parent_thread_id=parent_evidence.thread_id,
                        raw_sha256="a" * 64,
                    ),
                }
            )

    assert validate_parent_model_matrix(observations)["accepted"] is True
    assert validate_parent_model_matrix(observations[:-1])["code"] == "incomplete_parent_model_matrix"
    assert validate_parent_model_matrix([])["code"] == "live_model_evidence_required"


def test_parent_model_matrix_rejects_malformed_observations() -> None:
    assert validate_parent_model_matrix([{}])["code"] == "model_evidence_mismatch"


def _hook_evidence() -> dict:
    digest = hashlib.sha256((ROOT / "hooks" / "hooks.json").read_bytes()).hexdigest()
    return {
        "schema_version": HOOK_TRUST_SCHEMA_VERSION,
        "evidence_source": "codex_ui_export",
        "bypass_used": False,
        "untrusted": {
            "task_id": "task-untrusted",
            "fresh_task": True,
            "trust_state": "untrusted",
            "hook_config_sha256": digest,
            "hook_observed": False,
            "hook_result": "skipped_untrusted",
            "runtime_gate": "blocked",
            "protected_before": {".webnovel/state.json": "b" * 64},
            "protected_after": {".webnovel/state.json": "b" * 64},
        },
        "trusted": {
            "task_id": "task-trusted",
            "fresh_task": True,
            "trust_state": "trusted",
            "trust_method": "persisted_hooks_review",
            "hook_config_sha256": digest,
            "reviewed_sha256": digest,
            "hook_observed": True,
            "hook_result": "deny",
            "runtime_gate": "blocked",
            "protected_before": {".webnovel/state.json": "b" * 64},
            "protected_after": {".webnovel/state.json": "b" * 64},
        },
    }


def test_hook_trust_evidence_requires_two_fresh_safe_tasks_and_exact_hash() -> None:
    evidence = _hook_evidence()

    result = validate_hook_trust_evidence(evidence, hooks_config=ROOT / "hooks" / "hooks.json")

    assert result["accepted"] is True
    assert result["model_evidence"] is False


def test_hook_trust_bypass_or_protected_mutation_never_passes() -> None:
    bypass = _hook_evidence()
    bypass["bypass_used"] = True
    changed = _hook_evidence()
    changed["trusted"]["protected_after"] = {".webnovel/state.json": "c" * 64}

    config = ROOT / "hooks" / "hooks.json"
    assert validate_hook_trust_evidence(bypass, hooks_config=config)["code"] == "untrusted_hook_evidence"
    assert validate_hook_trust_evidence(changed, hooks_config=config)["code"] == "protected_state_changed"
    assert validate_hook_trust_evidence(None, hooks_config=config)["code"] == "live_hook_evidence_required"


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("missing_phase", "incomplete_hook_evidence"),
        ("extra_field", "hook_trust_mismatch"),
        ("same_task", "hook_trust_mismatch"),
    ],
)
def test_hook_trust_evidence_rejects_incomplete_or_mismatched_phases(
    mutation: str,
    expected: str,
) -> None:
    evidence = _hook_evidence()
    if mutation == "missing_phase":
        evidence["trusted"] = None
    elif mutation == "extra_field":
        evidence["trusted"]["unexpected"] = True
    else:
        evidence["trusted"]["task_id"] = evidence["untrusted"]["task_id"]

    result = validate_hook_trust_evidence(
        evidence,
        hooks_config=ROOT / "hooks" / "hooks.json",
    )

    assert result["code"] == expected


def test_unreadable_hook_config_is_never_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = (ROOT / "hooks" / "hooks.json").resolve()
    evidence = _hook_evidence()
    real_read_bytes = Path.read_bytes

    def denied(path: Path) -> bytes:
        if path.resolve() == config:
            raise OSError("simulated hook read denial")
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", denied)
    assert validate_hook_trust_evidence(evidence, hooks_config=config)["code"] == (
        "hook_config_unverified"
    )


def test_hook_plan_is_explicitly_blocked_until_live_evidence() -> None:
    result = build_hook_trust_plan(ROOT / "hooks" / "hooks.json", workspace_root=ROOT)

    assert result["status"] == "blocked"
    assert result["code"] == "live_hook_evidence_required"
    assert "bypass" in result["invalid_shortcut"]


def test_hook_plan_requires_existing_config_and_workspace(tmp_path: Path) -> None:
    with pytest.raises(SmokeEvidenceError, match="must exist"):
        build_hook_trust_plan(tmp_path / "missing.json", workspace_root=tmp_path)


def test_exec_argv_is_ephemeral_json_and_never_bypasses_hook_trust(tmp_path: Path) -> None:
    argv = build_codex_exec_argv(
        "codex",
        workspace_root=tmp_path,
        parent_model="gpt-5.6-sol",
        prompt="Run the read-only M3 smoke.",
    )

    assert argv[:4] == ["codex", "exec", "--ephemeral", "--json"]
    assert "--dangerously-bypass-hook-trust" not in argv
    assert "--yolo" not in argv


def test_exec_argv_rejects_incomplete_or_missing_inputs(tmp_path: Path) -> None:
    with pytest.raises(SmokeEvidenceError, match="are required"):
        build_codex_exec_argv(
            "codex",
            workspace_root=tmp_path / "missing",
            parent_model="gpt-5.6-sol",
            prompt="Run smoke.",
        )


def test_cli_access_denied_is_blocked_not_live_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("data_modules.codex_m3_smoke.shutil.which", lambda _: "codex")

    def deny(*args, **kwargs):
        raise PermissionError("Access is denied")

    monkeypatch.setattr("data_modules.codex_m3_smoke.subprocess.run", deny)

    result = probe_codex_cli()

    assert result["status"] == "blocked"
    assert result["code"] == "codex_cli_access_denied"
    assert result["live_model_evidence"] is False


def test_cli_probe_missing_exception_and_nonzero_exit_are_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(codex_m3_smoke.shutil, "which", lambda _: None)
    assert probe_codex_cli()["code"] == "codex_cli_not_found"

    def unusable(*args, **kwargs):
        raise OSError("simulated launch failure")

    monkeypatch.setattr(codex_m3_smoke.subprocess, "run", unusable)
    assert probe_codex_cli("codex")["code"] == "codex_cli_unusable"

    monkeypatch.setattr(
        codex_m3_smoke.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=["codex", "--version"],
            returncode=9,
            stdout="",
            stderr="cannot start",
        ),
    )
    result = probe_codex_cli("codex")
    assert result["code"] == "codex_cli_unusable"
    assert result["returncode"] == 9
    assert result["detail"] == "cannot start"


def test_cli_probe_success_reports_version_without_model_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        codex_m3_smoke.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=["codex", "--version"],
            returncode=0,
            stdout="codex-cli 1.2.3\n",
            stderr="",
        ),
    )

    result = probe_codex_cli("codex")

    assert result["status"] == "available"
    assert result["version"] == "codex-cli 1.2.3"
    assert result["live_model_evidence"] is False


def test_smoke_cli_main_emits_stable_probe_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        codex_m3_smoke_cli,
        "probe_codex_cli",
        lambda _: {
            "status": "blocked",
            "accepted": False,
            "code": "codex_cli_not_found",
        },
    )

    code = codex_m3_smoke_cli.main(["probe"])

    assert code == 2
    assert json.loads(capsys.readouterr().out)["code"] == "codex_cli_not_found"


def test_smoke_cli_main_converts_invalid_json_to_blocked_result(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    evidence = tmp_path / "hook-evidence.json"
    evidence.write_text("{not-json", encoding="utf-8")

    code = codex_m3_smoke_cli.main(
        [
            "verify-hook",
            "--evidence",
            str(evidence),
            "--hooks-config",
            str(ROOT / "hooks" / "hooks.json"),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload["status"] == "blocked"
    assert payload["code"] == "invalid_smoke_evidence"
