#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fail-closed routing and acceptance gates for Codex project agents.

The Setup generator owns agent names, models, efforts, and contract hashes.
This module consumes that source of truth. It never invokes a model and never
promotes or commits a provisional artifact.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from data_modules.codex_setup import agent_spec, inspect_managed_agent


ROUTE_SCHEMA_VERSION = "webnovel-agent-route/v1"
ENVELOPE_SCHEMA_VERSION = "webnovel-agent-run-envelope/v1"
RESULT_SCHEMA_VERSION = "webnovel-agent-acceptance/v1"
WORKFLOWS = {"plan", "write", "review", "init", "init_reference"}
WRITE_MODES = {"default", "fast", "minimal"}
RUNTIME_EVIDENCE_SOURCES = {
    "codex_task_event",
    "codex_trace",
    "codex_ui_export",
}
CANNED_EVIDENCE_SOURCE = "canned_fixture"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,94}[A-Za-z0-9])?$")
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
_MAX_AGENT_ARTIFACT_BYTES = 8 * 1024 * 1024
_MAX_WRITER_RESOLUTION_SUMMARY_CHARS = 1024
_WRITER_RESULT_V1 = "webnovel-writer-result/v1"
_WRITER_RESULT_V2 = "webnovel-writer-result/v2"
_WRITER_MANIFEST_V1 = "webnovel-writer-manifest/v1"
_WRITER_MANIFEST_V2 = "webnovel-writer-manifest/v2"


class AgentRuntimeError(ValueError):
    """Invalid route, run envelope, or Agent payload."""


@dataclass(frozen=True)
class VerifiedRuntimeEvidence:
    """Model identity parsed from an explicit Codex-owned trace/export."""

    evidence_source: str
    agent_name: str
    actual_model: str
    actual_reasoning_effort: str | None
    thread_id: str
    parent_thread_id: str
    raw_sha256: str


def _step(
    agent_name: str,
    *,
    parent_model: str,
    parent_reasoning_effort: str | None,
    plugin_root: str | Path | None,
) -> dict[str, Any]:
    spec = agent_spec(agent_name, plugin_root)
    fixed = spec.get("model") is not None
    return {
        "agent_name": spec["name"],
        "model_source": "fixed" if fixed else "parent",
        "requested_model": spec.get("model") or parent_model,
        "requested_reasoning_effort": (
            spec.get("model_reasoning_effort") if fixed else parent_reasoning_effort
        ),
        "sandbox_mode": spec["sandbox_mode"],
        "contract_hash": spec["contract_hash"],
        "managed_sha256": spec["managed_sha256"],
        "parent_model": parent_model,
        "parent_reasoning_effort": parent_reasoning_effort,
    }


def build_workflow_route(
    workflow: str,
    *,
    parent_model: str,
    parent_reasoning_effort: str | None = None,
    mode: str = "default",
    plugin_root: str | Path | None = None,
) -> dict[str, Any]:
    """Build the only allowed Agent route for one workflow."""

    workflow = str(workflow or "").strip()
    parent_model = str(parent_model or "").strip()
    if workflow not in WORKFLOWS:
        raise AgentRuntimeError(f"unsupported workflow: {workflow}")
    if not parent_model:
        raise AgentRuntimeError("parent_model is required")
    if workflow == "write" and mode not in WRITE_MODES:
        raise AgentRuntimeError(f"unsupported write mode: {mode}")

    if workflow == "plan":
        agent_names: list[str] = []
    elif workflow == "write":
        agent_names = ["context", "writer"]
        if mode != "minimal":
            agent_names.append("reviewer")
        agent_names.append("data")
    elif workflow == "review":
        agent_names = ["reviewer"]
    elif workflow == "init_reference":
        agent_names = ["deconstruction"]
    else:
        agent_names = []

    steps = [
        _step(
            name,
            parent_model=parent_model,
            parent_reasoning_effort=parent_reasoning_effort,
            plugin_root=plugin_root,
        )
        for name in agent_names
    ]
    return {
        "schema_version": ROUTE_SCHEMA_VERSION,
        "workflow": workflow,
        "mode": mode if workflow == "write" else None,
        "executor": "parent" if workflow in {"plan", "init"} else "agents",
        "parent_model": parent_model,
        "parent_reasoning_effort": parent_reasoning_effort,
        "planning_model": parent_model if workflow == "plan" else None,
        "steps": steps,
        "fallback_allowed": False,
    }


def validate_route_readiness(
    workspace_root: str | Path,
    route: Mapping[str, Any],
    *,
    plugin_root: str | Path | None = None,
) -> dict[str, Any]:
    """Fail closed when a required managed Agent is absent or stale."""

    if route.get("schema_version") != ROUTE_SCHEMA_VERSION:
        raise AgentRuntimeError("unsupported route schema")
    inspections: list[dict[str, Any]] = []
    problems: list[dict[str, str]] = []
    for step in route.get("steps") or []:
        name = str(step.get("agent_name") or "")
        inspection = inspect_managed_agent(
            workspace_root,
            name,
            plugin_root=plugin_root,
        )
        inspections.append(inspection)
        if not inspection.get("current"):
            problems.append(
                {
                    "code": "agent_unavailable",
                    "agent_name": name,
                    "detail": f"managed agent status is {inspection.get('status')}",
                }
            )
    return {
        "ready": not problems,
        "status": "ready" if not problems else "blocked",
        "problems": problems,
        "agents": inspections,
    }


def build_canned_envelope(
    step: Mapping[str, Any],
    *,
    status: str = "completed",
    actual_model: str | None = None,
    actual_reasoning_effort: str | None = None,
    evidence_source: str = "canned_fixture",
    artifacts: Sequence[Mapping[str, Any]] | None = None,
    fallback_used: bool = False,
) -> dict[str, Any]:
    """Create deterministic model-free evidence for behavior tests."""

    return {
        "schema_version": ENVELOPE_SCHEMA_VERSION,
        "agent_name": step.get("agent_name"),
        "status": status,
        "requested_model": step.get("requested_model"),
        "actual_model": step.get("requested_model") if actual_model is None else actual_model,
        "requested_reasoning_effort": step.get("requested_reasoning_effort"),
        "actual_reasoning_effort": (
            step.get("requested_reasoning_effort")
            if actual_reasoning_effort is None
            else actual_reasoning_effort
        ),
        "parent_model": step.get("parent_model"),
        "parent_reasoning_effort": step.get("parent_reasoning_effort"),
        "contract_hash": step.get("contract_hash"),
        "evidence_source": evidence_source,
        "fallback_used": fallback_used,
        "artifacts": [dict(item) for item in artifacts or []],
    }


def _blocked(code: str, detail: str, envelope: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "status": "blocked",
        "accepted": False,
        "code": code,
        "detail": detail,
        "agent_name": str(envelope.get("agent_name") or ""),
        "accepted_artifacts": [],
    }


def validate_agent_envelope(
    expected_step: Mapping[str, Any],
    envelope: Mapping[str, Any],
    *,
    allow_canned: bool = False,
    verified_evidence: VerifiedRuntimeEvidence | None = None,
) -> dict[str, Any]:
    """Validate requested and actual runtime identity before artifact use."""

    if envelope.get("schema_version") != ENVELOPE_SCHEMA_VERSION:
        return _blocked("invalid_envelope", "unsupported envelope schema", envelope)
    expected_name = str(expected_step.get("agent_name") or "")
    if envelope.get("agent_name") != expected_name:
        return _blocked(
            "agent_mismatch",
            f"expected {expected_name}, got {envelope.get('agent_name')}",
            envelope,
        )

    status = str(envelope.get("status") or "")
    if status == "agent_unavailable":
        return _blocked("agent_unavailable", "required Agent is unavailable", envelope)
    if status == "model_unavailable":
        return _blocked("model_unavailable", "required model is unavailable", envelope)
    if status != "completed":
        return _blocked("agent_failed", f"Agent status is {status or 'missing'}", envelope)

    evidence_source = str(envelope.get("evidence_source") or "")
    canned_allowed = allow_canned and evidence_source == CANNED_EVIDENCE_SOURCE
    if not canned_allowed and evidence_source not in RUNTIME_EVIDENCE_SOURCES:
        return _blocked(
            "untrusted_model_evidence",
            "model identity must come from Codex runtime evidence, not Agent self-report",
            envelope,
        )
    if not canned_allowed:
        if not isinstance(verified_evidence, VerifiedRuntimeEvidence):
            return _blocked(
                "unverified_model_evidence",
                "a source label or Agent-supplied fields are not verified Codex evidence",
                envelope,
            )
        if (
            verified_evidence.evidence_source != evidence_source
            or verified_evidence.agent_name != envelope.get("agent_name")
            or verified_evidence.actual_model != envelope.get("actual_model")
            or verified_evidence.actual_reasoning_effort
            != envelope.get("actual_reasoning_effort")
            or not verified_evidence.thread_id
            or not verified_evidence.parent_thread_id
            or not _valid_sha(verified_evidence.raw_sha256)
        ):
            return _blocked(
                "runtime_evidence_mismatch",
                "verified Codex evidence does not match the run envelope",
                envelope,
            )
    if envelope.get("contract_hash") != expected_step.get("contract_hash"):
        return _blocked("contract_hash_mismatch", "managed contract hash drifted", envelope)

    requested_model = str(envelope.get("requested_model") or "")
    expected_model = str(expected_step.get("requested_model") or "")
    if requested_model != expected_model:
        return _blocked(
            "requested_model_mismatch",
            f"expected requested model {expected_model}, got {requested_model}",
            envelope,
        )

    actual_model = str(envelope.get("actual_model") or "")
    parent_model = str(expected_step.get("parent_model") or "")
    fixed_route = expected_step.get("model_source") == "fixed"
    if bool(envelope.get("fallback_used")) or (
        fixed_route and actual_model == parent_model and parent_model != expected_model
    ):
        return _blocked(
            "parent_model_fallback_forbidden",
            "fixed Agent route fell back to the parent model",
            envelope,
        )
    if actual_model != expected_model:
        return _blocked(
            "actual_model_mismatch",
            f"expected actual model {expected_model}, got {actual_model or 'missing'}",
            envelope,
        )

    requested_effort = envelope.get("requested_reasoning_effort")
    expected_effort = expected_step.get("requested_reasoning_effort")
    if requested_effort != expected_effort:
        return _blocked(
            "requested_reasoning_effort_mismatch",
            f"expected requested reasoning effort {expected_effort}, got {requested_effort}",
            envelope,
        )
    actual_effort = envelope.get("actual_reasoning_effort")
    if actual_effort != expected_effort:
        return _blocked(
            "actual_reasoning_effort_mismatch",
            f"expected actual reasoning effort {expected_effort}, got {actual_effort}",
            envelope,
        )

    artifacts = envelope.get("artifacts")
    if not isinstance(artifacts, list):
        return _blocked("invalid_envelope", "artifacts must be a list", envelope)
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "status": "accepted",
        "accepted": True,
        "code": "ok",
        "detail": "runtime Agent/model/effort matches the managed route",
        "agent_name": expected_name,
        # Identity evidence alone never validates paths, bytes, hashes, or the
        # role schema.  Artifact promotion happens only in run_canned_workflow
        # (tests) or a future live workflow after validate_agent_payload.
        "accepted_artifacts": [],
        "provisional_artifacts": artifacts,
    }


def run_canned_workflow(
    route: Mapping[str, Any],
    envelopes: Sequence[Mapping[str, Any]],
    *,
    payloads: Sequence[object] | None = None,
    project_root: str | Path | None = None,
    run_id: str = "",
    reliable_source_text: bool = True,
    protected_before: Mapping[str, str] | None = None,
    protected_after: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Evaluate deterministic Agent runs and discard all artifacts on failure."""

    if route.get("schema_version") != ROUTE_SCHEMA_VERSION:
        raise AgentRuntimeError("unsupported route schema")
    expected_steps = list(route.get("steps") or [])
    invoked = [str(envelope.get("agent_name") or "") for envelope in envelopes]
    if route.get("workflow") == "plan":
        forbidden = {
            "webnovel_writer",
            "webnovel_reviewer",
            "webnovel_context_agent",
            "webnovel_data_agent",
        }.intersection(invoked)
        if forbidden:
            code = "planning_subagent_forbidden"
        elif envelopes:
            code = "unexpected_planning_agent"
        else:
            return {
                "status": "accepted",
                "code": "parent_planning_only",
                "invoked_agents": [],
                "planning_model": route.get("parent_model"),
                "accepted_artifacts": [],
                "results": [],
            }
        return {
            "status": "blocked",
            "code": code,
            "invoked_agents": invoked,
            "planning_model": route.get("parent_model"),
            "accepted_artifacts": [],
            "results": [],
        }

    if len(envelopes) != len(expected_steps):
        return {
            "status": "blocked",
            "code": "agent_unavailable",
            "invoked_agents": invoked,
            "accepted_artifacts": [],
            "results": [],
        }

    results = [
        validate_agent_envelope(step, envelope, allow_canned=True)
        for step, envelope in zip(expected_steps, envelopes, strict=True)
    ]
    failed = next((result for result in results if not result["accepted"]), None)
    if failed:
        return {
            "status": "blocked",
            "code": failed["code"],
            "invoked_agents": invoked,
            "accepted_artifacts": [],
            "results": results,
        }
    if (
        payloads is None
        or project_root is None
        or len(payloads) != len(expected_steps)
    ):
        return {
            "status": "blocked",
            "code": "payload_unvalidated",
            "invoked_agents": invoked,
            "accepted_artifacts": [],
            "results": results,
        }
    payload_results = [
        validate_agent_payload(
            step["agent_name"],
            payload,
            project_root=project_root,
            run_id=run_id,
            reliable_source_text=reliable_source_text,
        )
        for step, payload in zip(expected_steps, payloads, strict=True)
    ]
    payload_failure = next(
        (result for result in payload_results if not result["accepted"]),
        None,
    )
    if payload_failure:
        return {
            "status": "blocked",
            "code": payload_failure["code"],
            "invoked_agents": invoked,
            "accepted_artifacts": [],
            "results": results,
            "payload_results": payload_results,
        }
    protected_result = validate_protected_state_snapshots(
        protected_before,
        protected_after,
    )
    if not protected_result["accepted"]:
        return {
            "status": "blocked",
            "code": protected_result["code"],
            "invoked_agents": invoked,
            "accepted_artifacts": [],
            "results": results,
            "payload_results": payload_results,
            "protected_state": protected_result,
        }
    accepted_artifacts = [
        artifact
        for result in payload_results
        for artifact in result.get("accepted_artifacts") or []
    ]
    return {
        "status": "accepted",
        "code": "ok",
        "invoked_agents": invoked,
        "accepted_artifacts": accepted_artifacts,
        "results": results,
        "payload_results": payload_results,
        "protected_state": protected_result,
    }


def _inside(path: str | Path, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _is_reparse_point(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if callable(is_junction) and is_junction():
            return True
        attributes = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    except OSError:
        return True


def _safe_managed_path(path: Path, root: Path) -> bool:
    """Reject paths that escape or cross a symlink/junction/reparse point."""

    if not _inside(path, root):
        return False
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    current = root
    for part in relative.parts:
        current = current / part
        if current.exists() or current.is_symlink():
            if _is_reparse_point(current):
                return False
    return True


_PROTECTED_FILES = (
    ".webnovel/state.json",
    ".webnovel/index.db",
    ".webnovel/index.db-wal",
    ".webnovel/index.db-shm",
    ".webnovel/vectors.db",
    ".webnovel/memory_scratchpad.json",
    ".webnovel/projection_log.jsonl",
)
_PROTECTED_DIRECTORIES = (
    ".story-system",
    ".webnovel/summaries",
    "正文",
    "设定集",
    "大纲",
)


def snapshot_protected_state(project_root: str | Path) -> dict[str, str]:
    """Hash protected canon/read-model files without following reparse points."""

    root = Path(project_root).resolve()
    if not root.is_dir():
        raise AgentRuntimeError("project_root must be an existing directory")
    snapshot: dict[str, str] = {}

    def record(path: Path) -> None:
        relative = path.relative_to(root).as_posix()
        if _is_reparse_point(path):
            try:
                target = os.readlink(path)
            except OSError:
                target = "<unreadable>"
            snapshot[relative] = f"reparse:{target}"
            return
        signature = _file_signature(path)
        snapshot[relative] = (
            signature["sha256"] if signature is not None else "unreadable"
        )

    for relative in _PROTECTED_FILES:
        path = root / relative
        if path.exists() or path.is_symlink():
            record(path)
    for relative in _PROTECTED_DIRECTORIES:
        directory = root / relative
        if not directory.exists() and not directory.is_symlink():
            continue
        if _is_reparse_point(directory):
            record(directory)
            continue
        for current, directory_names, file_names in os.walk(directory, followlinks=False):
            current_path = Path(current)
            for name in list(directory_names):
                child = current_path / name
                if _is_reparse_point(child):
                    record(child)
                    directory_names.remove(name)
            for name in file_names:
                record(current_path / name)
    return dict(sorted(snapshot.items()))


def validate_protected_state_snapshots(
    before: Mapping[str, str] | None,
    after: Mapping[str, str] | None,
) -> dict[str, Any]:
    """Require real before/after snapshots and fail closed on any mutation."""

    if not isinstance(before, Mapping) or not isinstance(after, Mapping):
        return {
            "accepted": False,
            "code": "protected_state_unverified",
            "changed_paths": [],
        }
    before_copy = {str(key): str(value) for key, value in before.items()}
    after_copy = {str(key): str(value) for key, value in after.items()}
    changed = sorted(
        key
        for key in set(before_copy) | set(after_copy)
        if before_copy.get(key) != after_copy.get(key)
    )
    return {
        "accepted": not changed,
        "code": "ok" if not changed else "protected_state_changed",
        "changed_paths": changed,
    }


def _valid_sha(value: object) -> bool:
    return bool(_SHA256_RE.fullmatch(str(value or "")))


def _valid_run_id(value: object) -> bool:
    if not isinstance(value, str) or _RUN_ID_RE.fullmatch(value) is None:
        return False
    return value.split(".", 1)[0].upper() not in _WINDOWS_RESERVED_NAMES


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _stable_file_snapshot(
    path: Path,
    *,
    root: Path | None = None,
    max_bytes: int | None = None,
) -> tuple[bytes, dict[str, Any]] | None:
    """Read one regular, non-reparse file and bind bytes to its path identity."""

    if root is not None and not _safe_managed_path(path, root):
        return None
    if _is_reparse_point(path):
        return None
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            return None
        if max_bytes is not None and before.st_size > max_bytes:
            return None
        read_limit = before.st_size + 1
        if max_bytes is not None:
            read_limit = min(read_limit, max_bytes + 1)
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(read_limit)
        after = os.fstat(fd)
        path_after = path.stat(follow_symlinks=False)
        if root is not None and not _safe_managed_path(path, root):
            return None
        if _is_reparse_point(path) or not stat.S_ISREG(path_after.st_mode):
            return None
        if (
            len(raw) != before.st_size
            or _stat_identity(before) != _stat_identity(after)
            or _stat_identity(before) != _stat_identity(path_after)
        ):
            return None
    except OSError:
        return None
    finally:
        os.close(fd)
    return raw, {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
    }


def _file_signature(path: Path) -> dict[str, Any] | None:
    snapshot = _stable_file_snapshot(path)
    return snapshot[1] if snapshot is not None else None


def _matching_artifact_snapshot(
    artifact: Mapping[str, Any],
    path: Path,
    *,
    root: Path,
) -> tuple[bytes, dict[str, Any]] | None:
    snapshot = _stable_file_snapshot(
        path,
        root=root,
        max_bytes=_MAX_AGENT_ARTIFACT_BYTES,
    )
    if snapshot is None:
        return None
    raw, signature = snapshot
    if (
        artifact.get("path") != str(path)
        or artifact.get("sha256") != signature["sha256"]
        or artifact.get("bytes") != signature["bytes"]
    ):
        return None
    return raw, signature


def _json_object_from_bytes(raw: bytes) -> dict[str, Any] | None:
    """Parse one UTF-8/no-BOM JSON object from already-bound bytes."""

    try:
        if raw.startswith(b"\xef\xbb\xbf"):
            return None
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return dict(parsed) if isinstance(parsed, Mapping) else None


def _writer_word_count(text: str) -> int:
    """Count non-whitespace Unicode characters after Markdown frontmatter."""

    body = text
    if body.startswith("---\n"):
        end = body.find("\n---\n", 4)
        if end >= 0:
            body = body[end + len("\n---\n") :]
    return len(re.sub(r"\s+", "", body))


def _nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _list_of_strings(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _valid_writer_resolutions(value: object, *, operation: object) -> bool:
    """Validate Writer v2 issue-resolution declarations without reading prose."""

    if not isinstance(value, list):
        return False
    if operation in {"draft", "polish"}:
        return value == []
    if operation != "targeted_fix" or not value:
        return False

    seen_indexes: set[int] = set()
    seen_pairs: set[tuple[int, str]] = set()
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {
            "issue_index",
            "issue_sha256",
            "status",
            "resolution_summary",
        }:
            return False
        issue_index = item.get("issue_index")
        issue_sha256 = item.get("issue_sha256")
        summary = item.get("resolution_summary")
        if (
            type(issue_index) is not int
            or issue_index < 0
            or not _valid_sha(issue_sha256)
            or item.get("status") != "resolved"
            or not isinstance(summary, str)
            or not summary.strip()
            or "\x00" in summary
            or len(summary) > _MAX_WRITER_RESOLUTION_SUMMARY_CHARS
        ):
            return False
        pair = (issue_index, str(issue_sha256))
        if issue_index in seen_indexes or pair in seen_pairs:
            return False
        seen_indexes.add(issue_index)
        seen_pairs.add(pair)
    return True


def _json_object(payload: object) -> dict[str, Any] | None:
    if isinstance(payload, Mapping):
        return dict(payload)
    if not isinstance(payload, str):
        return None
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return None
    return dict(parsed) if isinstance(parsed, Mapping) else None


def _contains_key(payload: object, forbidden: set[str]) -> bool:
    if isinstance(payload, Mapping):
        return any(
            str(key) in forbidden or _contains_key(value, forbidden)
            for key, value in payload.items()
        )
    if isinstance(payload, list):
        return any(_contains_key(item, forbidden) for item in payload)
    return False


def validate_agent_payload(
    agent_name: str,
    payload: object,
    *,
    project_root: str | Path,
    run_id: str,
    reliable_source_text: bool = True,
) -> dict[str, Any]:
    """Validate role-specific canned output without interpreting input prose."""

    canonical = agent_spec(agent_name)["name"]
    root = Path(project_root).resolve()
    if not root.is_dir() or not _valid_run_id(run_id):
        return {"accepted": False, "code": "invalid_request"}

    if canonical == "webnovel_context_agent" and isinstance(payload, str):
        required = ("开篇委托", "这章的故事", "这章的人物", "怎么写更顺", "收在哪里")
        matches = list(re.finditer(r"(?m)^##[ \t]+([^\r\n]+?)[ \t]*$", payload))
        headings = tuple(match.group(1).strip() for match in matches)
        sections = []
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(payload)
            sections.append(payload[match.end() : end].strip())
        ok = bool(matches) and (
            headings == required
            and payload[: matches[0].start()].strip() == ""
            and all(sections)
        )
        return {
            "accepted": ok,
            "code": "ok" if ok else "incomplete_task_brief",
            "accepted_artifacts": [],
        }

    obj = _json_object(payload)
    if obj is None:
        return {"accepted": False, "code": "invalid_json", "accepted_artifacts": []}
    if _contains_key(
        obj,
        {"tool_calls", "commands", "system_prompt", "developer_instructions", "actual_model"},
    ):
        return {
            "accepted": False,
            "code": "embedded_instruction_rejected",
            "accepted_artifacts": [],
        }

    if canonical == "webnovel_context_agent":
        allowed_codes = {
            "invalid_request", "path_out_of_bounds", "input_hash_mismatch",
            "insufficient_context", "fact_conflict", "prompt_injection_detected",
            "agent_unavailable", "model_unavailable",
        }
        expected_fields = {
            "schema_version", "status", "code", "chapter", "missing_facts",
            "conflicts", "safe_message", "problems",
        }
        chapter = obj.get("chapter")
        ok = (
            set(obj) == expected_fields
            and obj.get("schema_version") == "webnovel-context-blocker/v1"
            and obj.get("status") == "blocked"
            and obj.get("code") in allowed_codes
            and isinstance(chapter, int)
            and not isinstance(chapter, bool)
            and chapter > 0
            and isinstance(obj.get("missing_facts"), list)
            and isinstance(obj.get("conflicts"), list)
            and _nonempty_string(obj.get("safe_message"))
            and isinstance(obj.get("problems"), list)
        )
        return {
            "accepted": ok,
            "code": "ok" if ok else "invalid_context_result",
            "accepted_artifacts": [],
        }

    if canonical == "webnovel_writer":
        result_schema = obj.get("schema_version")
        result_v2 = result_schema == _WRITER_RESULT_V2
        expected_fields = {
            "schema_version", "status", "run_id", "operation", "artifacts",
            "manifest_path", "manifest_sha256", "problems", "warnings",
        }
        if result_v2:
            expected_fields.add("resolutions")
        operation = obj.get("operation")
        status = obj.get("status")
        if (
            set(obj) != expected_fields
            or result_schema not in {_WRITER_RESULT_V1, _WRITER_RESULT_V2}
            or obj.get("run_id") != run_id
            or status not in {"completed", "blocked", "failed"}
            or operation not in {"draft", "targeted_fix", "polish"}
            or not _list_of_strings(obj.get("problems"))
            or not _list_of_strings(obj.get("warnings"))
        ):
            return {
                "accepted": False,
                "code": "invalid_writer_result",
                "accepted_artifacts": [],
            }
        if result_schema == _WRITER_RESULT_V1 and operation == "targeted_fix":
            return {
                "accepted": False,
                "code": "writer_resolution_contract_required",
                "accepted_artifacts": [],
            }
        if result_v2:
            resolutions_ok = (
                _valid_writer_resolutions(obj.get("resolutions"), operation=operation)
                if status == "completed"
                else obj.get("resolutions") == []
            )
            if not resolutions_ok:
                return {
                    "accepted": False,
                    "code": "invalid_writer_resolutions",
                    "accepted_artifacts": [],
                }
        artifacts = obj.get("artifacts")
        if status != "completed":
            ok = (
                artifacts == []
                and obj.get("manifest_path") in {"", None}
                and obj.get("manifest_sha256") in {"", None}
                and bool(obj.get("problems"))
            )
            return {
                "accepted": False,
                "code": "writer_blocked" if ok else "invalid_writer_result",
                "accepted_artifacts": [],
            }
        staging = root / ".webnovel" / "tmp" / "write-runs" / run_id
        expected_name = "draft.md" if operation == "draft" else "polished.md"
        if not isinstance(artifacts, list) or len(artifacts) != 1:
            return {
                "accepted": False,
                "code": "artifact_missing",
                "accepted_artifacts": [],
            }
        for artifact in artifacts:
            if (
                not isinstance(artifact, Mapping)
                or set(artifact) != {"kind", "path", "sha256", "bytes", "word_count"}
                or not isinstance(artifact.get("bytes"), int)
                or isinstance(artifact.get("bytes"), bool)
                or not isinstance(artifact.get("word_count"), int)
                or isinstance(artifact.get("word_count"), bool)
            ):
                return {
                    "accepted": False,
                    "code": "invalid_writer_result",
                    "accepted_artifacts": [],
                }
            path = staging / expected_name
            expected_kind = "draft" if expected_name == "draft.md" else "polished"
            artifact_snapshot = _matching_artifact_snapshot(artifact, path, root=root)
            if (
                artifact.get("kind") != expected_kind
                or not _safe_managed_path(path, root)
                or artifact_snapshot is None
                or not _valid_sha(artifact.get("sha256"))
            ):
                return {
                    "accepted": False,
                    "code": "artifact_out_of_bounds",
                    "accepted_artifacts": [],
                }
            raw = artifact_snapshot[0]
            try:
                if raw.startswith(b"\xef\xbb\xbf"):
                    raise UnicodeDecodeError("utf-8", raw, 0, 3, "BOM is forbidden")
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                return {
                    "accepted": False,
                    "code": "artifact_encoding_invalid",
                    "accepted_artifacts": [],
                }
            if artifact.get("word_count") != _writer_word_count(text):
                return {
                    "accepted": False,
                    "code": "artifact_word_count_mismatch",
                    "accepted_artifacts": [],
                }
        manifest = staging / "manifest.json"
        manifest_snapshot = _stable_file_snapshot(
            manifest,
            root=root,
            max_bytes=_MAX_AGENT_ARTIFACT_BYTES,
        )
        manifest_raw, signature = manifest_snapshot if manifest_snapshot is not None else (b"", None)
        manifest_obj = _json_object_from_bytes(manifest_raw) if signature is not None else None
        if (
            obj.get("manifest_path") != str(manifest)
            or not _safe_managed_path(manifest, root)
            or signature is None
            or obj.get("manifest_sha256") != signature["sha256"]
            or not _valid_sha(obj.get("manifest_sha256"))
            or manifest_obj is None
        ):
            return {
                "accepted": False,
                "code": "manifest_out_of_bounds",
                "accepted_artifacts": [],
            }
        manifest_fields = {
            "schema_version", "run_id", "agent_name", "operation", "status",
            "inputs", "outputs", "problems", "warnings",
        }
        expected_manifest_schema = _WRITER_MANIFEST_V2 if result_v2 else _WRITER_MANIFEST_V1
        if result_v2:
            manifest_fields.add("resolutions")
        inputs = manifest_obj.get("inputs")
        if (
            set(manifest_obj) != manifest_fields
            or manifest_obj.get("schema_version") != expected_manifest_schema
            or manifest_obj.get("run_id") != run_id
            or manifest_obj.get("agent_name") != "webnovel_writer"
            or manifest_obj.get("operation") != operation
            or manifest_obj.get("status") != "completed"
            or manifest_obj.get("outputs") != artifacts
            or manifest_obj.get("problems") != obj.get("problems")
            or manifest_obj.get("warnings") != obj.get("warnings")
            or (result_v2 and manifest_obj.get("resolutions") != obj.get("resolutions"))
            or not isinstance(inputs, list)
        ):
            return {
                "accepted": False,
                "code": "invalid_writer_manifest",
                "accepted_artifacts": [],
            }
        for input_item in inputs:
            if not isinstance(input_item, Mapping) or set(input_item) != {"path", "sha256"}:
                return {"accepted": False, "code": "invalid_writer_manifest", "accepted_artifacts": []}
            input_path = Path(str(input_item.get("path") or ""))
            input_signature = _file_signature(input_path)
            if (
                not input_path.is_absolute()
                or not _safe_managed_path(input_path, root)
                or input_signature is None
                or not _valid_sha(input_item.get("sha256"))
                or input_item.get("sha256") != input_signature["sha256"]
            ):
                return {"accepted": False, "code": "writer_input_hash_mismatch", "accepted_artifacts": []}
        return {"accepted": True, "code": "ok", "accepted_artifacts": artifacts}

    if canonical == "webnovel_reviewer":
        required_top = {
            "chapter", "issues", "issues_count", "blocking_count", "has_blocking",
            "dimension_results", "summary",
        }
        if set(obj) != required_top or _contains_key(
            obj, {"score", "overall_score", "dimension_scores"}
        ):
            return {
                "accepted": False,
                "code": "invalid_reviewer_json",
                "accepted_artifacts": [],
            }
        dimension_results = obj.get("dimension_results")
        if not isinstance(dimension_results, list):
            return {"accepted": False, "code": "invalid_reviewer_json", "accepted_artifacts": []}
        dimensions = [
            str(item.get("dimension") or "")
            for item in dimension_results
            if isinstance(item, Mapping)
        ]
        issues = obj.get("issues")
        if (
            dimensions != ["setting", "timeline", "continuity", "character", "logic"]
            or any(
                not isinstance(item, Mapping)
                or set(item) != {"dimension", "conclusion"}
                or not _nonempty_string(item.get("conclusion"))
                for item in dimension_results
            )
        ):
            return {"accepted": False, "code": "invalid_reviewer_json", "accepted_artifacts": []}
        if not isinstance(issues, list):
            return {"accepted": False, "code": "invalid_reviewer_json", "accepted_artifacts": []}
        required_issue_fields = {
            "severity", "category", "location", "description", "evidence",
            "fix_hint", "blocking",
        }
        if any(
            not isinstance(issue, Mapping)
            or set(issue) != required_issue_fields
            or issue.get("severity") not in {"critical", "high", "medium", "low"}
            or issue.get("category") not in {
                "setting", "timeline", "continuity", "character", "logic"
            }
            or not all(
                _nonempty_string(issue.get(field))
                for field in ("location", "description", "evidence", "fix_hint")
            )
            or not isinstance(issue.get("blocking"), bool)
            or (issue.get("severity") == "critical" and issue.get("blocking") is not True)
            for issue in issues
        ):
            return {"accepted": False, "code": "invalid_reviewer_json", "accepted_artifacts": []}
        blocking = sum(
            1 for issue in issues
            if isinstance(issue, Mapping) and issue.get("blocking") is True
        )
        ok = (
            isinstance(obj.get("chapter"), int)
            and not isinstance(obj.get("chapter"), bool)
            and obj.get("chapter") > 0
            and obj.get("issues_count") == len(issues)
            and obj.get("blocking_count") == blocking
            and obj.get("has_blocking") == bool(blocking)
            and _nonempty_string(obj.get("summary"))
        )
        return {
            "accepted": ok,
            "code": "ok" if ok else "invalid_reviewer_json",
            "accepted_artifacts": [],
        }

    if canonical == "webnovel_data_agent":
        expected_fields = {
            "schema_version", "status", "run_id", "artifacts", "pending_count",
            "missed_nodes_count", "problems", "warnings",
        }
        if (
            set(obj) != expected_fields
            or obj.get("schema_version") != "webnovel-data-result/v1"
            or obj.get("run_id") != run_id
            or not isinstance(obj.get("pending_count"), int)
            or isinstance(obj.get("pending_count"), bool)
            or obj.get("pending_count") < 0
            or not isinstance(obj.get("missed_nodes_count"), int)
            or isinstance(obj.get("missed_nodes_count"), bool)
            or obj.get("missed_nodes_count") < 0
            or not _list_of_strings(obj.get("problems"))
            or not _list_of_strings(obj.get("warnings"))
        ):
            return {"accepted": False, "code": "invalid_data_result", "accepted_artifacts": []}
        artifacts = obj.get("artifacts")
        if obj.get("status") not in {"completed", "partial"} or not isinstance(artifacts, list):
            return {"accepted": False, "code": "invalid_data_result", "accepted_artifacts": []}
        expected = {
            "fulfillment_result": "fulfillment_result.json",
            "disambiguation_result": "disambiguation_result.json",
            "extraction_result": "extraction_result.json",
        }
        if len(artifacts) != 3:
            return {"accepted": False, "code": "artifact_set_mismatch", "accepted_artifacts": []}
        artifact_root = root / ".webnovel" / "tmp"
        seen: set[str] = set()
        artifact_objects: dict[str, dict[str, Any]] = {}
        for item in artifacts:
            if (
                not isinstance(item, Mapping)
                or set(item) != {"name", "path", "sha256", "bytes"}
                or not isinstance(item.get("bytes"), int)
                or isinstance(item.get("bytes"), bool)
            ):
                return {"accepted": False, "code": "invalid_data_result", "accepted_artifacts": []}
            name = str(item.get("name") or "")
            if name not in expected or name in seen:
                return {"accepted": False, "code": "artifact_set_mismatch", "accepted_artifacts": []}
            seen.add(name)
            path = artifact_root / expected[name]
            artifact_snapshot = _matching_artifact_snapshot(item, path, root=root)
            if (
                not _safe_managed_path(path, root)
                or not _valid_sha(item.get("sha256"))
                or artifact_snapshot is None
            ):
                return {"accepted": False, "code": "artifact_hash_invalid", "accepted_artifacts": []}
            artifact_obj = _json_object_from_bytes(artifact_snapshot[0])
            if artifact_obj is None:
                return {"accepted": False, "code": "artifact_schema_invalid", "accepted_artifacts": []}
            artifact_objects[name] = artifact_obj
        if seen != set(expected):
            return {"accepted": False, "code": "artifact_set_mismatch", "accepted_artifacts": []}
        fulfillment = artifact_objects["fulfillment_result"]
        fulfillment_fields = {
            "planned_nodes", "covered_nodes", "missed_nodes", "extra_nodes",
        }
        if set(fulfillment) != fulfillment_fields or any(
            not isinstance(fulfillment.get(field), list) for field in fulfillment_fields
        ):
            return {"accepted": False, "code": "artifact_schema_invalid", "accepted_artifacts": []}
        disambiguation = artifact_objects["disambiguation_result"]
        if set(disambiguation) != {"pending"} or not isinstance(disambiguation.get("pending"), list):
            return {"accepted": False, "code": "artifact_schema_invalid", "accepted_artifacts": []}
        extraction = artifact_objects["extraction_result"]
        extraction_required = {"accepted_events", "state_deltas", "entity_deltas"}
        extraction_allowed = extraction_required | {
            "entities_appeared", "scenes", "summary_text", "chapter_meta", "dominant_strand",
        }
        if (
            not extraction_required.issubset(extraction)
            or not set(extraction).issubset(extraction_allowed)
            or any(not isinstance(extraction.get(field), list) for field in extraction_required)
        ):
            return {"accepted": False, "code": "artifact_schema_invalid", "accepted_artifacts": []}
        if (
            obj.get("pending_count") != len(disambiguation["pending"])
            or obj.get("missed_nodes_count") != len(fulfillment["missed_nodes"])
        ):
            return {"accepted": False, "code": "artifact_count_mismatch", "accepted_artifacts": []}
        return {"accepted": True, "code": "ok", "accepted_artifacts": artifacts}

    if canonical == "webnovel_deconstruction_agent":
        quality = obj.get("quality")
        expected_fields = {
            "source", "analysis_mode", "reader_promise", "opening_hook_patterns",
            "cool_point_loops", "protagonist_patterns", "antagonist_pressure_patterns",
            "pacing_notes", "borrowable_structures", "do_not_copy",
            "differentiation_requirements", "init_candidates", "quality", "resume_state",
            "orphan_plot_fallback", "canon_contamination_warnings",
        }
        if set(obj) != expected_fields:
            return {
                "accepted": False,
                "code": "invalid_deconstruction_result",
                "accepted_artifacts": [],
            }
        source = obj.get("source")
        reader_promise = obj.get("reader_promise")
        pacing_notes = obj.get("pacing_notes")
        resume_state = obj.get("resume_state")
        list_fields = {
            "opening_hook_patterns", "cool_point_loops", "protagonist_patterns",
            "antagonist_pressure_patterns", "borrowable_structures", "do_not_copy",
            "differentiation_requirements", "init_candidates", "orphan_plot_fallback",
            "canon_contamination_warnings",
        }
        if (
            not isinstance(source, Mapping)
            or set(source) != {"title", "platform", "input_type", "text_path"}
            or not all(isinstance(source.get(field), str) for field in source)
            or obj.get("analysis_mode") not in {"quick", "deep"}
            or not isinstance(reader_promise, Mapping)
            or set(reader_promise) != {"core_desire", "promise_delivery", "risk"}
            or not all(isinstance(reader_promise.get(field), str) for field in reader_promise)
            or not isinstance(pacing_notes, Mapping)
            or set(pacing_notes) != {
                "golden_three", "arc_cycle", "information_density", "chapter_end_strategy",
            }
            or not all(isinstance(pacing_notes.get(field), str) for field in pacing_notes)
            or any(not isinstance(obj.get(field), list) for field in list_fields)
            or not isinstance(quality, Mapping)
            or set(quality) != {"confidence", "coverage", "overlap", "passed", "warnings"}
            or any(
                not isinstance(quality.get(field), (int, float))
                or isinstance(quality.get(field), bool)
                or not 0.0 <= float(quality.get(field)) <= 1.0
                for field in ("confidence", "coverage", "overlap")
            )
            or not isinstance(quality.get("passed"), bool)
            or not _list_of_strings(quality.get("warnings"))
            or not isinstance(resume_state, Mapping)
            or set(resume_state) != {
                "current_stage", "processed_chapters", "next_action",
                "character_merges", "quality_checks",
            }
            or not isinstance(resume_state.get("current_stage"), str)
            or not isinstance(resume_state.get("next_action"), str)
            or any(
                not isinstance(resume_state.get(field), list)
                for field in ("processed_chapters", "character_merges", "quality_checks")
            )
        ):
            return {
                "accepted": False,
                "code": "invalid_deconstruction_result",
                "accepted_artifacts": [],
            }
        array_item_fields = {
            "opening_hook_patterns": {"pattern", "why_it_works", "transfer_rule", "avoid_copying"},
            "cool_point_loops": {
                "setup", "release", "reaction_layers", "transition", "pacing_ratio", "transfer_rule",
            },
            "protagonist_patterns": {
                "desire_model", "flaw_pressure", "competence_reveal", "differentiation_hint",
            },
            "antagonist_pressure_patterns": {
                "tier", "pressure_type", "mirror_function", "escalation_rule",
            },
            "borrowable_structures": {"structure", "use_case", "required_transformation"},
            "init_candidates": {
                "one_liner", "anti_trope", "hard_constraints", "protagonist_flaw",
                "antagonist_mirror", "opening_hook", "source_patterns_used", "transformation_notes",
            },
        }
        if any(
            not isinstance(item, Mapping) or set(item) != item_fields
            for field, item_fields in array_item_fields.items()
            for item in obj[field]
        ):
            return {
                "accepted": False,
                "code": "invalid_deconstruction_result",
                "accepted_artifacts": [],
            }
        if not reliable_source_text:
            analysis_lists = (
                "opening_hook_patterns", "cool_point_loops", "protagonist_patterns",
                "antagonist_pressure_patterns", "borrowable_structures", "do_not_copy",
                "differentiation_requirements", "init_candidates", "orphan_plot_fallback",
                "canon_contamination_warnings",
            )
            ok = (
                obj.get("analysis_mode") == "quick"
                and source.get("input_type") == "title"
                and _nonempty_string(source.get("title"))
                and source.get("text_path") == ""
                and all(value == "" for value in reader_promise.values())
                and all(value == "" for value in pacing_notes.values())
                and all(obj[field] == [] for field in analysis_lists)
                and quality.get("passed") is False
                and all(float(quality.get(field)) == 0.0 for field in ("confidence", "coverage", "overlap"))
                and bool(quality.get("warnings"))
                and any(
                    any(marker in warning.lower() for marker in ("文本", "正文", "text", "source"))
                    for warning in quality.get("warnings")
                )
                and resume_state.get("current_stage") == ""
                and resume_state.get("processed_chapters") == []
                and _nonempty_string(resume_state.get("next_action"))
                and any(
                    marker in resume_state.get("next_action", "").lower()
                    for marker in ("文本", "正文", "text", "source")
                )
                and resume_state.get("character_merges") == []
                and resume_state.get("quality_checks") == []
            )
            return {
                "accepted": ok,
                "code": "ok" if ok else "source_fabrication",
                "accepted_artifacts": [],
            }
        return {"accepted": True, "code": "ok", "accepted_artifacts": []}

    return {"accepted": False, "code": "unknown_agent", "accepted_artifacts": []}


def validate_reviewer_attempts(
    responses: Sequence[object],
    *,
    project_root: str | Path,
    run_id: str,
) -> dict[str, Any]:
    """Consume at most the initial response plus one serialization retry.

    The caller is responsible for invoking the same managed reviewer route for
    both responses.  A third response is never inspected or accepted.
    """

    attempts: list[dict[str, Any]] = []
    for response in responses[:2]:
        result = validate_agent_payload(
            "webnovel_reviewer",
            response,
            project_root=project_root,
            run_id=run_id,
        )
        attempts.append(result)
        if result.get("accepted"):
            return {
                "status": "accepted",
                "accepted": True,
                "code": "ok",
                "attempts_used": len(attempts),
                "retry_count": len(attempts) - 1,
                "retry_permitted": False,
                "accepted_artifacts": [],
                "attempts": attempts,
            }

    return {
        "status": "blocked",
        "accepted": False,
        "code": "invalid_reviewer_json",
        "attempts_used": len(attempts),
        "retry_count": max(0, len(attempts) - 1),
        "retry_permitted": len(attempts) == 1,
        "accepted_artifacts": [],
        "attempts": attempts,
    }


def validate_prompt_injection_fixture(
    agent_name: str,
    payload: object,
    *,
    untrusted_inputs: Sequence[str],
    project_root: str | Path,
    run_id: str,
    reliable_source_text: bool = True,
) -> dict[str, Any]:
    """Run the role gate and reject verbatim reflection of injected input.

    This model-free fixture adapter makes the untrusted input an explicit part
    of the behavior test.  It also scans every otherwise accepted artifact,
    so a canned writer/data response cannot hide the attack in a file.
    """

    markers = [str(value) for value in untrusted_inputs if str(value)]
    if not markers or len(markers) != len(untrusted_inputs):
        return {"accepted": False, "code": "invalid_injection_fixture", "accepted_artifacts": []}
    result = validate_agent_payload(
        agent_name,
        payload,
        project_root=project_root,
        run_id=run_id,
        reliable_source_text=reliable_source_text,
    )
    if not result.get("accepted"):
        return result
    if isinstance(payload, str):
        output_texts = [payload]
    else:
        output_texts = [json.dumps(payload, ensure_ascii=False, sort_keys=True)]
    for artifact in result.get("accepted_artifacts") or []:
        path = artifact.get("path") if isinstance(artifact, Mapping) else None
        try:
            if path:
                output_texts.append(Path(str(path)).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            return {"accepted": False, "code": "artifact_encoding_invalid", "accepted_artifacts": []}
    if any(marker in text for marker in markers for text in output_texts):
        return {"accepted": False, "code": "prompt_injection_reflected", "accepted_artifacts": []}
    return result
