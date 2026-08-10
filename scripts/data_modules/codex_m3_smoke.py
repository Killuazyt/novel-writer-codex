#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fail-closed evidence readers for the manual M3 Codex smoke.

The module never scans Codex sessions and never invokes a model.  A caller must
name an explicit rollout JSONL under the configured Codex sessions root.  Hook
trust evidence is deliberately independent from Agent model evidence.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from data_modules.codex_agent_runtime import (
    VerifiedRuntimeEvidence,
    validate_protected_state_snapshots,
)


SMOKE_SCHEMA_VERSION = "webnovel-codex-m3-smoke/v1"
HOOK_TRUST_SCHEMA_VERSION = "webnovel-hook-trust-evidence/v1"
MAX_ROLLOUT_BYTES = 32 * 1024 * 1024
FIXED_AGENT_NAMES = {
    "webnovel_context_agent",
    "webnovel_writer",
    "webnovel_reviewer",
    "webnovel_data_agent",
}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_AGENT_TASK_PREFIX_RE = re.compile(r"^[a-z][a-z0-9]{1,7}$")
_AGENT_TASK_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,94}[a-z0-9]$")


class SmokeEvidenceError(ValueError):
    """An explicit live-evidence artifact is missing or inconsistent."""


@dataclass(frozen=True)
class VerifiedParentEvidence:
    """Parent task identity parsed from its explicit Codex rollout."""

    evidence_source: str
    thread_id: str
    model: str
    reasoning_effort: str | None
    raw_sha256: str


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def derive_agent_task_name(binding_marker: str, *, prefix: str) -> str:
    """Derive one opaque host-visible child task name from an immutable marker."""

    if not _nonempty(binding_marker):
        raise SmokeEvidenceError("Agent binding marker must be explicit")
    if not isinstance(prefix, str) or _AGENT_TASK_PREFIX_RE.fullmatch(prefix) is None:
        raise SmokeEvidenceError("Agent task-name prefix is invalid")
    digest = base64.b32encode(hashlib.sha256(binding_marker.encode("utf-8")).digest())
    token = digest.decode("ascii").rstrip("=").lower()
    task_name = f"{prefix}_{token}"
    if _AGENT_TASK_NAME_RE.fullmatch(task_name) is None:
        raise SmokeEvidenceError("derived Agent task name is invalid")
    return task_name


def validate_agent_task_binding(
    spawn: Mapping[str, Any],
    *,
    expected_task_name: str,
) -> None:
    """Bind one depth-1 child rollout to the marker-derived root task path."""

    if _AGENT_TASK_NAME_RE.fullmatch(str(expected_task_name or "")) is None:
        raise SmokeEvidenceError("expected Agent task name is invalid")
    depth = spawn.get("depth")
    if type(depth) is not int or depth != 1:
        raise SmokeEvidenceError("thread_spawn depth must equal 1")
    expected_path = f"/root/{expected_task_name}"
    if spawn.get("agent_path") != expected_path:
        raise SmokeEvidenceError("thread_spawn agent_path does not match the bound Agent task")


def coalesce_session_meta_payloads(
    events: Sequence[Mapping[str, Any]],
    *,
    expected_thread_id: str,
) -> tuple[int, Mapping[str, Any]]:
    """Return one canonical session payload after validating safe duplicates."""

    if not _nonempty(expected_thread_id):
        raise SmokeEvidenceError("expected_thread_id must be explicit")
    session_matches = [
        (index, event) for index, event in enumerate(events) if event.get("type") == "session_meta"
    ]
    if not session_matches:
        raise SmokeEvidenceError("rollout lacks session_meta")

    payloads: list[Mapping[str, Any]] = []
    for _, event in session_matches:
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            raise SmokeEvidenceError("Codex session_meta payloads must be objects")
        if payload.get("id") != expected_thread_id:
            raise SmokeEvidenceError("session_meta thread id mismatch")
        payloads.append(payload)

    first_payload = dict(payloads[0])
    first_payload.pop("memory_mode", None)
    first_identity = _canonical_json(first_payload)
    memory_mode_seen = False
    memory_mode_value: str | None = None
    for payload in payloads:
        normalized = dict(payload)
        has_memory_mode = "memory_mode" in normalized
        current_memory_mode = normalized.pop("memory_mode", None)
        if has_memory_mode:
            canonical_memory_mode = _canonical_json(current_memory_mode)
            if memory_mode_seen and canonical_memory_mode != memory_mode_value:
                raise SmokeEvidenceError("conflicting session_meta memory_mode values")
            memory_mode_seen = True
            memory_mode_value = canonical_memory_mode
        elif memory_mode_seen:
            raise SmokeEvidenceError("session_meta memory_mode regressed from present to missing")
        if _canonical_json(normalized) != first_identity:
            raise SmokeEvidenceError("conflicting session_meta payloads")
    return session_matches[0][0], payloads[0]


def coalesce_turn_context_payloads(
    turn_events: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Coalesce exact duplicate turns while preserving cross-turn model gates."""

    payloads: list[Mapping[str, Any]] = []
    identities_by_turn_id: dict[str, str] = {}
    first_identity: tuple[object, object] | None = None
    for event in turn_events:
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            raise SmokeEvidenceError("Codex turn_context payloads must be objects")
        turn_id = payload.get("turn_id")
        if not _nonempty(turn_id):
            raise SmokeEvidenceError("turn_context turn_id is missing")
        canonical_payload = _canonical_json(payload)
        previous_payload = identities_by_turn_id.get(turn_id)
        if previous_payload is not None:
            if canonical_payload != previous_payload:
                raise SmokeEvidenceError("conflicting turn_context payload for duplicate turn_id")
            continue
        turn_identity = (payload.get("model"), payload.get("effort"))
        if first_identity is None:
            first_identity = turn_identity
        elif turn_identity != first_identity:
            raise SmokeEvidenceError("conflicting turn_context model or effort")
        identities_by_turn_id[turn_id] = canonical_payload
        payloads.append(payload)
    return payloads


def _load_explicit_rollout(
    rollout_path: str | Path,
    *,
    expected_thread_id: str,
    sessions_root: str | Path | None,
) -> tuple[bytes, Mapping[str, Any], Mapping[str, Any]]:
    path = Path(rollout_path).resolve()
    root = (
        Path(sessions_root).resolve()
        if sessions_root is not None
        else (Path.home() / ".codex" / "sessions").resolve()
    )
    if not _inside(path, root):
        raise SmokeEvidenceError("rollout_path must be under the explicit Codex sessions root")
    if expected_thread_id not in path.name or path.suffix.lower() != ".jsonl":
        raise SmokeEvidenceError("rollout filename must identify the expected thread")
    try:
        if not path.is_file() or path.stat().st_size > MAX_ROLLOUT_BYTES:
            raise SmokeEvidenceError("rollout is missing or exceeds the bounded evidence size")
        raw = path.read_bytes()
    except OSError as exc:
        raise SmokeEvidenceError(f"rollout is unreadable: {exc}") from exc
    if raw.startswith(b"\xef\xbb\xbf"):
        raise SmokeEvidenceError("rollout must be UTF-8 without BOM")
    try:
        lines = [line for line in raw.decode("utf-8").splitlines() if line.strip()]
        events = [json.loads(line) for line in lines]
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SmokeEvidenceError("rollout is not UTF-8 JSONL") from exc
    if len(events) < 2 or not all(isinstance(event, Mapping) for event in events):
        raise SmokeEvidenceError("rollout lacks session_meta and turn_context events")
    session_index, session = coalesce_session_meta_payloads(
        events,
        expected_thread_id=expected_thread_id,
    )
    turn_events = [
        event for event in events[session_index + 1 :] if event.get("type") == "turn_context"
    ]
    if not turn_events:
        raise SmokeEvidenceError("rollout lacks turn_context after session_meta")
    turn_payloads = coalesce_turn_context_payloads(turn_events)
    turn = turn_payloads[0]
    return raw, session, turn


def parse_rollout_runtime_evidence(
    rollout_path: str | Path,
    *,
    expected_thread_id: str,
    expected_parent_thread_id: str,
    expected_agent_role: str,
    expected_model: str,
    expected_reasoning_effort: str,
    expected_task_name: str | None = None,
    sessions_root: str | Path | None = None,
) -> VerifiedRuntimeEvidence:
    """Parse one explicitly named Codex child rollout without directory scans."""

    raw, session, turn = _load_explicit_rollout(
        rollout_path,
        expected_thread_id=expected_thread_id,
        sessions_root=sessions_root,
    )
    source = session.get("source")
    subagent = source.get("subagent") if isinstance(source, Mapping) else None
    spawn = subagent.get("thread_spawn") if isinstance(subagent, Mapping) else None
    if not isinstance(spawn, Mapping):
        raise SmokeEvidenceError("session_meta is not a thread_spawn subagent session")
    if expected_task_name is not None:
        validate_agent_task_binding(spawn, expected_task_name=expected_task_name)

    session_thread_id = session.get("id")
    session_parent_id = session.get("parent_thread_id")
    spawn_parent_id = spawn.get("parent_thread_id")
    role = spawn.get("agent_role")
    session_model = session.get("model")
    turn_model = turn.get("model")
    effort = turn.get("effort")
    if (
        not all(
            _nonempty(value)
            for value in (
                expected_thread_id,
                expected_parent_thread_id,
                expected_agent_role,
                expected_model,
                expected_reasoning_effort,
            )
        )
        or expected_agent_role not in FIXED_AGENT_NAMES | {"webnovel_deconstruction_agent"}
    ):
        raise SmokeEvidenceError("all expected rollout identities must be explicit")
    if session_thread_id != expected_thread_id:
        raise SmokeEvidenceError("session_meta child thread id mismatch")
    if session_parent_id != expected_parent_thread_id or spawn_parent_id != expected_parent_thread_id:
        raise SmokeEvidenceError("session_meta parent thread id mismatch")
    if role != expected_agent_role:
        raise SmokeEvidenceError("session_meta agent role mismatch")
    if (session_model is not None and session_model != expected_model) or turn_model != expected_model:
        raise SmokeEvidenceError("session_meta/turn_context model mismatch")
    if effort != expected_reasoning_effort:
        raise SmokeEvidenceError("turn_context reasoning effort mismatch")

    return VerifiedRuntimeEvidence(
        evidence_source="codex_trace",
        agent_name=expected_agent_role,
        actual_model=expected_model,
        actual_reasoning_effort=expected_reasoning_effort,
        thread_id=expected_thread_id,
        parent_thread_id=expected_parent_thread_id,
        raw_sha256=hashlib.sha256(raw).hexdigest(),
    )


def parse_parent_rollout_identity(
    rollout_path: str | Path,
    *,
    expected_thread_id: str,
    expected_model: str,
    expected_reasoning_effort: str,
    sessions_root: str | Path | None = None,
) -> VerifiedParentEvidence:
    """Parse one explicitly named parent task rollout and its selected model."""

    raw, session, turn = _load_explicit_rollout(
        rollout_path,
        expected_thread_id=expected_thread_id,
        sessions_root=sessions_root,
    )
    if not all(
        _nonempty(value)
        for value in (expected_thread_id, expected_model, expected_reasoning_effort)
    ):
        raise SmokeEvidenceError("all expected parent rollout identities must be explicit")
    if session.get("id") != expected_thread_id:
        raise SmokeEvidenceError("parent session_meta thread id mismatch")
    source = session.get("source")
    if session.get("parent_thread_id") not in (None, "") or (
        isinstance(source, Mapping) and source.get("subagent") is not None
    ):
        raise SmokeEvidenceError("parent rollout must be a top-level Codex task")
    session_model = session.get("model")
    if (session_model is not None and session_model != expected_model) or turn.get("model") != expected_model:
        raise SmokeEvidenceError("parent session_meta/turn_context model mismatch")
    if turn.get("effort") != expected_reasoning_effort:
        raise SmokeEvidenceError("parent turn_context reasoning effort mismatch")
    return VerifiedParentEvidence(
        evidence_source="codex_trace",
        thread_id=expected_thread_id,
        model=expected_model,
        reasoning_effort=expected_reasoning_effort,
        raw_sha256=hashlib.sha256(raw).hexdigest(),
    )


def build_codex_exec_argv(
    codex_executable: str | Path,
    *,
    workspace_root: str | Path,
    parent_model: str,
    prompt: str,
) -> list[str]:
    """Build a fresh, ephemeral JSON-event smoke command without trust bypasses."""

    executable = str(codex_executable)
    workspace = Path(workspace_root).resolve()
    if not executable.strip() or not workspace.is_dir() or not _nonempty(parent_model) or not _nonempty(prompt):
        raise SmokeEvidenceError("executable, workspace, parent model, and prompt are required")
    return [
        executable,
        "exec",
        "--ephemeral",
        "--json",
        "--sandbox",
        "read-only",
        "-C",
        str(workspace),
        "--model",
        parent_model,
        prompt,
    ]


def probe_codex_cli(codex_executable: str | Path | None = None) -> dict[str, Any]:
    """Check whether the local CLI can start; this is not live model evidence."""

    executable = str(codex_executable) if codex_executable else shutil.which("codex")
    if not executable:
        return {
            "schema_version": SMOKE_SCHEMA_VERSION,
            "status": "blocked",
            "code": "codex_cli_not_found",
            "path": None,
            "live_model_evidence": False,
        }
    try:
        completed = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
            check=False,
        )
    except PermissionError as exc:
        return {
            "schema_version": SMOKE_SCHEMA_VERSION,
            "status": "blocked",
            "code": "codex_cli_access_denied",
            "path": executable,
            "detail": str(exc),
            "live_model_evidence": False,
        }
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "schema_version": SMOKE_SCHEMA_VERSION,
            "status": "blocked",
            "code": "codex_cli_unusable",
            "path": executable,
            "detail": str(exc),
            "live_model_evidence": False,
        }
    if completed.returncode != 0:
        return {
            "schema_version": SMOKE_SCHEMA_VERSION,
            "status": "blocked",
            "code": "codex_cli_unusable",
            "path": executable,
            "returncode": completed.returncode,
            "detail": (completed.stderr or completed.stdout).strip()[:1000],
            "live_model_evidence": False,
        }
    return {
        "schema_version": SMOKE_SCHEMA_VERSION,
        "status": "available",
        "code": "ok",
        "path": executable,
        "version": completed.stdout.strip(),
        "live_model_evidence": False,
    }


def build_hook_trust_plan(
    hooks_config: str | Path,
    *,
    workspace_root: str | Path,
) -> dict[str, Any]:
    """Describe the two fresh-task checks; it does not claim they ran."""

    config = Path(hooks_config).resolve()
    workspace = Path(workspace_root).resolve()
    if not config.is_file() or not workspace.is_dir():
        raise SmokeEvidenceError("hooks config and workspace must exist")
    raw = config.read_bytes()
    return {
        "schema_version": HOOK_TRUST_SCHEMA_VERSION,
        "status": "blocked",
        "code": "live_hook_evidence_required",
        "workspace_root": str(workspace),
        "hooks_config": str(config),
        "hook_config_sha256": hashlib.sha256(raw).hexdigest(),
        "steps": [
            "Start a fresh task before reviewing project hooks; confirm the hook is skipped.",
            "Verify the runtime gate still blocks a protected mutation and compare protected snapshots.",
            "Use the persisted /hooks review flow for this exact hook hash.",
            "Start a second fresh task; confirm the trusted hook denies the mutation.",
            "Verify the runtime gate also blocks and protected snapshots remain identical.",
        ],
        "invalid_shortcut": "--dangerously-bypass-hook-trust never counts as trusted evidence",
    }


def validate_hook_trust_evidence(
    evidence: object,
    *,
    hooks_config: str | Path,
) -> dict[str, Any]:
    """Validate captured untrusted/trusted phases without using model fields."""

    config = Path(hooks_config).resolve()
    try:
        config_sha256 = hashlib.sha256(config.read_bytes()).hexdigest()
    except OSError:
        return {"status": "blocked", "accepted": False, "code": "hook_config_unverified"}
    if not isinstance(evidence, Mapping):
        return {"status": "blocked", "accepted": False, "code": "live_hook_evidence_required"}
    if (
        set(evidence) != {
            "schema_version", "evidence_source", "bypass_used", "untrusted", "trusted",
        }
        or
        evidence.get("schema_version") != HOOK_TRUST_SCHEMA_VERSION
        or evidence.get("evidence_source") not in {"codex_trace", "codex_ui_export"}
        or evidence.get("bypass_used") is not False
    ):
        return {"status": "blocked", "accepted": False, "code": "untrusted_hook_evidence"}
    untrusted = evidence.get("untrusted")
    trusted = evidence.get("trusted")
    if not isinstance(untrusted, Mapping) or not isinstance(trusted, Mapping):
        return {"status": "blocked", "accepted": False, "code": "incomplete_hook_evidence"}
    untrusted_fields = {
        "task_id", "fresh_task", "trust_state", "hook_config_sha256",
        "hook_observed", "hook_result", "runtime_gate", "protected_before",
        "protected_after",
    }
    trusted_fields = untrusted_fields | {"trust_method", "reviewed_sha256"}
    if set(untrusted) != untrusted_fields or set(trusted) != trusted_fields:
        return {"status": "blocked", "accepted": False, "code": "hook_trust_mismatch"}
    hashes = (
        untrusted.get("hook_config_sha256"),
        trusted.get("hook_config_sha256"),
        trusted.get("reviewed_sha256"),
    )
    if (
        any(not isinstance(value, str) or not _SHA256_RE.fullmatch(value) for value in hashes)
        or len(set(hashes)) != 1
        or hashes[0] != config_sha256
        or not all(_nonempty(phase.get("task_id")) for phase in (untrusted, trusted))
        or untrusted.get("task_id") == trusted.get("task_id")
        or untrusted.get("fresh_task") is not True
        or trusted.get("fresh_task") is not True
        or untrusted.get("trust_state") != "untrusted"
        or trusted.get("trust_state") != "trusted"
        or untrusted.get("hook_observed") is not False
        or trusted.get("hook_observed") is not True
        or untrusted.get("hook_result") != "skipped_untrusted"
        or trusted.get("hook_result") != "deny"
        or trusted.get("trust_method") != "persisted_hooks_review"
        or untrusted.get("runtime_gate") != "blocked"
        or trusted.get("runtime_gate") != "blocked"
    ):
        return {"status": "blocked", "accepted": False, "code": "hook_trust_mismatch"}
    for phase in (untrusted, trusted):
        snapshots = validate_protected_state_snapshots(
            phase.get("protected_before"),
            phase.get("protected_after"),
        )
        if not snapshots["accepted"]:
            return {
                "status": "blocked",
                "accepted": False,
                "code": snapshots["code"],
                "phase": phase.get("trust_state"),
            }
    return {
        "status": "accepted",
        "accepted": True,
        "code": "ok",
        "hook_config_sha256": config_sha256,
        "model_evidence": False,
    }


def validate_parent_model_matrix(observations: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Require two parents and fixed Luna/medium evidence for every fixed role."""

    if not observations:
        return {"status": "blocked", "accepted": False, "code": "live_model_evidence_required"}
    parent_models: set[str] = set()
    parent_threads: set[str] = set()
    roles_by_parent: dict[str, set[str]] = {}
    for observation in observations:
        evidence = observation.get("evidence")
        parent = observation.get("parent_evidence")
        workflow = observation.get("workflow")
        if (
            not isinstance(evidence, VerifiedRuntimeEvidence)
            or not isinstance(parent, VerifiedParentEvidence)
            or workflow not in {"write", "review"}
            or evidence.agent_name not in FIXED_AGENT_NAMES
            or evidence.actual_model != "gpt-5.6-luna"
            or evidence.actual_reasoning_effort != "medium"
            or evidence.parent_thread_id != parent.thread_id
            or evidence.evidence_source != "codex_trace"
            or parent.evidence_source != "codex_trace"
            or not _SHA256_RE.fullmatch(evidence.raw_sha256)
            or not _SHA256_RE.fullmatch(parent.raw_sha256)
        ):
            return {"status": "blocked", "accepted": False, "code": "model_evidence_mismatch"}
        parent_models.add(parent.model)
        parent_threads.add(parent.thread_id)
        roles_by_parent.setdefault(parent.thread_id, set()).add(evidence.agent_name)
    required = FIXED_AGENT_NAMES
    if (
        len(parent_models) < 2
        or len(parent_threads) < 2
        or any(not required.issubset(roles_by_parent[parent]) for parent in parent_threads)
    ):
        return {
            "status": "blocked",
            "accepted": False,
            "code": "incomplete_parent_model_matrix",
            "parent_models": sorted(parent_models),
            "parent_threads": sorted(parent_threads),
        }
    return {
        "status": "accepted",
        "accepted": True,
        "code": "ok",
        "parent_models": sorted(parent_models),
        "parent_threads": sorted(parent_threads),
    }
