#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fail-closed single-chapter and bounded range Review workflow.

This module does not invoke Codex Agents.  ``prepare`` creates a hashed request
artifact for the managed reviewer; ``accept`` consumes the reviewer JSON plus
an explicitly named Codex rollout and applies the M3 runtime identity gate.
Only accepted runs may reach report/metrics/database persistence.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
import tempfile
import uuid
from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import filelock

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

try:
    from chapter_paths import extract_chapter_num_from_filename
except ImportError:
    from scripts.chapter_paths import extract_chapter_num_from_filename

try:
    from security_utils import _replace_with_retry, atomic_write_json
except ImportError:
    from scripts.security_utils import _replace_with_retry, atomic_write_json

from .codex_agent_runtime import (
    ENVELOPE_SCHEMA_VERSION,
    VerifiedRuntimeEvidence,
    build_workflow_route,
    snapshot_protected_state,
    validate_agent_envelope,
    validate_agent_payload,
    validate_protected_state_snapshots,
    validate_route_readiness,
)
from .codex_interaction import ChoiceProtocolError, build_choice_request, resolve_choice
from .codex_m3_smoke import (
    MAX_ROLLOUT_BYTES,
    SmokeEvidenceError,
    coalesce_session_meta_payloads,
    derive_agent_task_name,
    parse_parent_rollout_identity,
    parse_rollout_runtime_evidence,
)
from .config import DataModulesConfig
from .index_manager import IndexManager
from .memory_contract_adapter import MemoryContractAdapter
from .review_request import (
    ReviewRequestError,
    load_review_accept_request,
    load_review_decision_request,
)
from .review_schema import MAX_REVIEW_JSON_BYTES, ReviewResult, ReviewSchemaError, parse_review_output
from .sqlite_readonly import read_only_sqlite_uri
from .run_ledger import (
    RunLedgerError,
    file_signature,
    get_review_range,
    get_review_run,
    locked_ledger,
    valid_review_range_id,
    valid_review_run_id,
)


REVIEW_REQUEST_SCHEMA = "webnovel-review-run-request/v1"
REVIEW_ARTIFACT_SCHEMA = "webnovel-review-artifact/v1"
REVIEW_WORKFLOW_SCHEMA = "webnovel-review-workflow/v1"
REVIEW_DECISION_SCHEMA = "webnovel-review-decision/v1"
REVIEW_RANGE_SCHEMA = "webnovel-review-range/v1"
REVIEW_MODES = {"full", "fast"}
BLOCKING_CHOICES = ("targeted_fix", "report_only", "abandon")
RANGE_CHOICES = ("stop", "continue")
TERMINAL_RUN_STATUSES = {
    "persisted",
    "abandoned",
    "targeted_fix_pending",
    "targeted_fix_blocked",
    "failed_validation",
    "stale",
}
ACTIVE_RUN_STATUSES = {
    "prepared",
    "accepted",
    "awaiting_decision",
    "validated",
    "failed_persistence",
}
MAX_CONTEXT_BYTES = 2 * 1024 * 1024
MAX_REVIEW_INPUT_BYTES = 32 * 1024 * 1024
MAX_DECISION_ANSWER_BYTES = 4096
MAX_SQLITE_WAL_RECOVERY_BYTES = 256 * 1024 * 1024
REVIEW_BINDING_MARKER_SCHEMA = "WEBNOVEL_REVIEW_BINDING/v1"
REVIEW_DECISION_MARKER_SCHEMA = "WEBNOVEL_REVIEW_DECISION/v1"
# Resolve the host location once at process start.  In particular, an accept
# request and a later CODEX_HOME environment mutation cannot redirect Review
# to an attacker-controlled sessions tree.
# Keep the host path unresolved until the trust check.  Resolving here would
# erase the fact that ``~/.codex`` or ``sessions`` is a junction/symlink and
# turn its target into the trusted root.
TRUSTED_CODEX_SESSIONS_ROOT = Path.home() / ".codex" / "sessions"


@dataclass(frozen=True)
class VerifiedReviewerExecution:
    """One host-owned reviewer trace plus its bounded final assistant texts."""

    runtime: VerifiedRuntimeEvidence
    raw_outputs: tuple[str, ...]


class ReviewWorkflowError(ValueError):
    """A stable Review workflow error with a machine-readable code."""

    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.code = str(code)
        self.details = dict(details or {})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _current_codex_thread_id(expected: object = None) -> str:
    """Return the canonical host-provided current parent task UUID."""

    raw = str(os.environ.get("CODEX_THREAD_ID") or "").strip()
    try:
        parsed = uuid.UUID(raw)
    except (AttributeError, ValueError) as exc:
        raise ReviewWorkflowError(
            "parent_runtime_unavailable",
            "CODEX_THREAD_ID must be a non-empty UUID for the current parent task",
        ) from exc
    current = str(parsed)
    if not raw or raw.casefold() != current:
        raise ReviewWorkflowError(
            "parent_runtime_unavailable",
            "CODEX_THREAD_ID must use canonical UUID form",
        )
    expected_value = str(expected or "").strip()
    if expected_value and current != expected_value:
        raise ReviewWorkflowError(
            "parent_runtime_mismatch",
            "current CODEX_THREAD_ID is not the parent task bound at Review prepare time",
        )
    return current


def _canonical_json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _strict_json_object(raw: str) -> dict[str, Any]:
    def build_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ValueError(f"invalid JSON constant: {value}")

    payload = json.loads(raw, object_pairs_hook=build_object, parse_constant=reject_constant)
    if not isinstance(payload, dict):
        raise ValueError("reviewer output must be one JSON object")
    return payload


def _sha_payload(payload: object) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _path_hash(path: Path) -> str:
    return hashlib.sha256(str(path).casefold().encode("utf-8")).hexdigest()


def _is_reparse(path: Path) -> bool:
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


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=True))
        return True
    except (OSError, ValueError):
        return False


def _safe_project_path(path: Path, root: Path, *, require_file: bool = False) -> bool:
    if not _inside(path, root) or _is_reparse(root):
        return False
    try:
        relative = path.resolve(strict=False).relative_to(root.resolve(strict=True))
    except (OSError, ValueError):
        return False
    current = root.resolve(strict=True)
    for part in relative.parts:
        current = current / part
        if current.exists() or current.is_symlink():
            if _is_reparse(current):
                return False
    return not require_file or path.is_file()


def _reject_reparse_chain(path: Path, *, code: str, label: str) -> None:
    """Reject every existing symlink/junction component in an absolute path."""

    if not path.is_absolute():
        raise ReviewWorkflowError(code, f"{label} must be absolute")
    parts = path.parts
    if not parts:
        raise ReviewWorkflowError(code, f"{label} is empty")
    current = Path(parts[0])
    for part in parts[1:]:
        current = current / part
        if current.exists() or current.is_symlink():
            if _is_reparse(current):
                raise ReviewWorkflowError(code, f"{label} contains a symlink, junction, or reparse point")


def _validate_project_lock_path(path: Path, root: Path, *, code: str) -> None:
    if not _safe_project_path(path, root):
        raise ReviewWorkflowError(code, f"lock path is unsafe: {path}")
    if path.exists() or path.is_symlink():
        if _is_reparse(path) or not path.is_file():
            raise ReviewWorkflowError(code, f"lock path must be a regular non-reparse file: {path}")


class _VerifiedProjectFileLock(AbstractContextManager[filelock.FileLock]):
    """FileLock wrapper that checks the lock leaf before and after acquire."""

    def __init__(self, path: Path, root: Path, *, code: str) -> None:
        self.path = path
        self.root = root
        self.code = code
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _validate_project_lock_path(self.path, self.root, code=self.code)
        self._lock = filelock.FileLock(str(self.path), timeout=10)

    def __enter__(self) -> filelock.FileLock:
        _validate_project_lock_path(self.path, self.root, code=self.code)
        self._lock.acquire()
        try:
            _validate_project_lock_path(self.path, self.root, code=self.code)
        except Exception:
            self._lock.release()
            raise
        return self._lock

    def __exit__(self, exc_type, exc_value, traceback) -> bool | None:
        validation_error: Exception | None = None
        try:
            _validate_project_lock_path(self.path, self.root, code=self.code)
        except Exception as exc:  # pragma: no cover - adversarial lock swap
            validation_error = exc
        finally:
            self._lock.release()
        if validation_error is not None and exc_type is None:
            raise validation_error
        return None


def _stable_project_bytes(path: Path, root: Path, *, max_bytes: int) -> bytes:
    """Read one in-project regular file through a stable bounded handle."""

    if not _safe_project_path(path, root, require_file=True) or _is_reparse(path):
        raise OSError("project file path is unsafe")
    with path.open("rb") as handle:
        before = os.fstat(handle.fileno())
        if before.st_size < 0 or before.st_size > max_bytes:
            raise OSError(f"project file exceeds {max_bytes} bytes")
        raw = handle.read(max_bytes + 1)
        after = os.fstat(handle.fileno())
    if (
        len(raw) != before.st_size
        or len(raw) > max_bytes
        or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    ):
        raise OSError("project file changed while it was read")
    current = path.stat(follow_symlinks=False)
    if (
        _is_reparse(path)
        or not stat.S_ISREG(current.st_mode)
        or (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    ):
        raise OSError("project file path changed while it was read")
    return raw


def _project_root(project_root: str | Path) -> Path:
    raw = Path(project_root)
    try:
        root = raw.resolve(strict=True)
    except OSError as exc:
        raise ReviewWorkflowError("invalid_project_root", f"project_root is unavailable: {exc}") from exc
    if not root.is_dir() or _is_reparse(root):
        raise ReviewWorkflowError("invalid_project_root", "project_root must be a real non-reparse directory")
    state = root / ".webnovel" / "state.json"
    if not _safe_project_path(state, root, require_file=True):
        raise ReviewWorkflowError("invalid_project_root", "project_root must contain a safe .webnovel/state.json")
    return root


def _workspace_root(workspace_root: str | Path) -> Path:
    try:
        root = Path(workspace_root).resolve(strict=True)
    except OSError as exc:
        raise ReviewWorkflowError("invalid_workspace_root", f"workspace_root is unavailable: {exc}") from exc
    if not root.is_dir() or _is_reparse(root):
        raise ReviewWorkflowError("invalid_workspace_root", "workspace_root must be a real non-reparse directory")
    return root


def _trusted_codex_sessions_root() -> Path:
    """Resolve the host-owned Codex sessions directory, never a request override."""

    raw = TRUSTED_CODEX_SESSIONS_ROOT
    _reject_reparse_chain(
        raw,
        code="trusted_sessions_unavailable",
        label="trusted Codex sessions root",
    )
    try:
        root = raw.resolve(strict=True)
    except OSError as exc:
        raise ReviewWorkflowError(
            "trusted_sessions_unavailable",
            f"trusted Codex sessions root is unavailable: {exc}",
        ) from exc
    if not root.is_dir() or _is_reparse(raw) or _is_reparse(root):
        raise ReviewWorkflowError(
            "trusted_sessions_unavailable",
            "trusted Codex sessions root must be a real non-reparse directory",
        )
    return root


def _plugin_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _binding_marker(run: Mapping[str, Any]) -> str:
    artifacts = run.get("artifacts") if isinstance(run.get("artifacts"), Mapping) else {}
    signature = artifacts.get("request") if isinstance(artifacts, Mapping) else None
    if not isinstance(signature, Mapping):
        raise ReviewWorkflowError("request_artifact_missing", "review request signature is missing")
    path = Path(str(signature.get("path") or ""))
    sha256 = str(signature.get("sha256") or "")
    if not path.is_absolute() or not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ReviewWorkflowError("request_artifact_invalid", "review request signature is invalid")
    payload = {
        "run_id": run.get("run_id"),
        "parent_thread_id": run.get("parent_thread_id"),
        "request_sha256": sha256,
        "request_path": str(path),
    }
    return f"{REVIEW_BINDING_MARKER_SCHEMA} {_canonical_json(payload)}"


def _reviewer_agent_binding(run: Mapping[str, Any]) -> tuple[str, str, str]:
    """Recompute the reviewer task binding from the immutable request marker."""

    binding_marker = _binding_marker(run)
    try:
        task_name = derive_agent_task_name(binding_marker, prefix="wnr")
    except SmokeEvidenceError as exc:
        raise ReviewWorkflowError("request_binding_mismatch", str(exc)) from exc
    agent_path = f"/root/{task_name}"
    stored_task_name = run.get("agent_task_name")
    stored_agent_path = run.get("agent_path")
    if stored_task_name is not None and stored_task_name != task_name:
        raise ReviewWorkflowError(
            "request_binding_mismatch",
            "stored reviewer task name does not match the immutable request marker",
        )
    if stored_agent_path is not None and stored_agent_path != agent_path:
        raise ReviewWorkflowError(
            "request_binding_mismatch",
            "stored reviewer Agent path does not match the immutable request marker",
        )
    return binding_marker, task_name, agent_path


def _message_text(payload: Mapping[str, Any]) -> tuple[str, str, str | None] | None:
    message = payload.get("item") if isinstance(payload.get("item"), Mapping) else payload
    if not isinstance(message, Mapping) or message.get("type") != "message":
        return None
    role = str(message.get("role") or "")
    content = message.get("content")
    if isinstance(content, str):
        return role, content, str(message.get("phase")) if message.get("phase") is not None else None
    if not isinstance(content, list):
        return None
    texts: list[str] = []
    for item in content:
        if not isinstance(item, Mapping):
            continue
        if item.get("type") not in {"input_text", "output_text", "text"}:
            continue
        value = item.get("text")
        if isinstance(value, str):
            texts.append(value)
    return (
        role,
        "".join(texts),
        str(message.get("phase")) if message.get("phase") is not None else None,
    ) if texts else None


def _extract_bound_reviewer_outputs(
    rollout_path: Path,
    *,
    evidence: VerifiedRuntimeEvidence,
    binding_marker: str,
    verified_agent_path: str | None = None,
) -> tuple[str, ...]:
    """Extract one or two final texts after a legacy marker or a verified Agent path."""

    try:
        if not rollout_path.is_file() or _is_reparse(rollout_path):
            raise ReviewWorkflowError("invalid_runtime_evidence", "rollout must be a regular non-reparse file")
        with rollout_path.open("rb") as handle:
            before = os.fstat(handle.fileno())
            if before.st_size <= 0 or before.st_size > MAX_ROLLOUT_BYTES:
                raise ReviewWorkflowError("invalid_runtime_evidence", "rollout size is outside the trusted bound")
            raw = handle.read(MAX_ROLLOUT_BYTES + 1)
            after = os.fstat(handle.fileno())
    except OSError as exc:
        raise ReviewWorkflowError("invalid_runtime_evidence", f"rollout cannot be read safely: {exc}") from exc
    stable_identity = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) == (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    if not stable_identity or len(raw) != before.st_size or len(raw) > MAX_ROLLOUT_BYTES:
        raise ReviewWorkflowError("invalid_runtime_evidence", "rollout changed while it was read")
    try:
        current = rollout_path.stat(follow_symlinks=False)
    except OSError as exc:
        raise ReviewWorkflowError("invalid_runtime_evidence", f"rollout identity cannot be verified: {exc}") from exc
    if _is_reparse(rollout_path) or not stat.S_ISREG(current.st_mode) or (
        current.st_dev,
        current.st_ino,
        current.st_size,
        current.st_mtime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise ReviewWorkflowError("invalid_runtime_evidence", "rollout path changed while it was read")
    if hashlib.sha256(raw).hexdigest() != evidence.raw_sha256:
        raise ReviewWorkflowError("invalid_runtime_evidence", "rollout changed while evidence was parsed")
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ReviewWorkflowError("invalid_runtime_evidence", "rollout must be UTF-8 without BOM")
    try:
        events = [
            json.loads(line)
            for line in raw.decode("utf-8").splitlines()
            if line.strip()
        ]
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReviewWorkflowError("invalid_runtime_evidence", "rollout is not UTF-8 JSONL") from exc
    if not all(isinstance(event, Mapping) for event in events):
        raise ReviewWorkflowError("invalid_runtime_evidence", "rollout events must be JSON objects")

    has_legacy_marker = False
    for event in events:
        if event.get("type") != "response_item" or not isinstance(event.get("payload"), Mapping):
            continue
        parsed = _message_text(event["payload"])
        if parsed is not None and parsed[0] == "user" and binding_marker in parsed[1]:
            has_legacy_marker = True
            break

    legacy_binding_seen = False
    response_outputs: list[str] = []
    legacy_event_outputs: list[str] = []
    for event in events:
        event_type = event.get("type")
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            continue
        if event_type == "response_item":
            parsed = _message_text(payload)
            if parsed is None:
                continue
            role, text, phase = parsed
            if role == "user" and binding_marker in text:
                legacy_binding_seen = True
                continue
            if (
                role == "assistant"
                and phase in {"final", "final_answer"}
                and text.strip()
                and (
                    legacy_binding_seen
                    if has_legacy_marker
                    else verified_agent_path is not None
                )
            ):
                response_outputs.append(text)
            continue
        if (
            event_type == "event_msg"
            and legacy_binding_seen
            and payload.get("type") == "agent_message"
        ):
            text = payload.get("message")
            if not isinstance(text, str):
                text = payload.get("text")
            if isinstance(text, str) and text.strip() and text not in legacy_event_outputs:
                legacy_event_outputs.append(text)
    if not has_legacy_marker and verified_agent_path is None:
        raise ReviewWorkflowError(
            "runtime_request_unbound",
            "reviewer rollout has neither an exact legacy marker nor a verified Agent path binding",
        )
    # response_item is the durable current-host record.  Only the explicit
    # marker branch may fall back to legacy event_msg traces, whose streaming
    # messages cannot otherwise be separated from commentary.
    outputs = response_outputs if response_outputs else legacy_event_outputs
    if not 1 <= len(outputs) <= 2:
        raise ReviewWorkflowError(
            "invalid_reviewer_attempt_count",
            "bound reviewer rollout must contain one response and at most one retry",
        )
    if any(len(text.encode("utf-8")) > MAX_REVIEW_JSON_BYTES for text in outputs):
        raise ReviewWorkflowError("invalid_reviewer_json", "reviewer output exceeds the bounded JSON size")
    return tuple(outputs)


def _stable_parent_rollout_records(
    rollout_path: Path,
    *,
    expected_sha256: str,
) -> tuple[bytes, list[tuple[int, int, Mapping[str, Any]]]]:
    """Re-read one parsed parent rollout through a stable bounded handle."""

    try:
        if not rollout_path.is_file() or _is_reparse(rollout_path):
            raise ReviewWorkflowError(
                "invalid_decision_receipt",
                "parent rollout must be a regular non-reparse file",
            )
        with rollout_path.open("rb") as handle:
            before = os.fstat(handle.fileno())
            if before.st_size <= 0 or before.st_size > MAX_ROLLOUT_BYTES:
                raise ReviewWorkflowError(
                    "invalid_decision_receipt",
                    "parent rollout size is outside the trusted bound",
                )
            raw = handle.read(MAX_ROLLOUT_BYTES + 1)
            after = os.fstat(handle.fileno())
    except OSError as exc:
        raise ReviewWorkflowError(
            "invalid_decision_receipt",
            f"parent rollout cannot be read safely: {exc}",
        ) from exc
    if (
        len(raw) != before.st_size
        or len(raw) > MAX_ROLLOUT_BYTES
        or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    ):
        raise ReviewWorkflowError(
            "invalid_decision_receipt",
            "parent rollout changed while it was read",
        )
    try:
        current = rollout_path.stat(follow_symlinks=False)
    except OSError as exc:
        raise ReviewWorkflowError(
            "invalid_decision_receipt",
            f"parent rollout identity cannot be verified: {exc}",
        ) from exc
    if _is_reparse(rollout_path) or not stat.S_ISREG(current.st_mode) or (
        current.st_dev,
        current.st_ino,
        current.st_size,
        current.st_mtime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise ReviewWorkflowError(
            "invalid_decision_receipt",
            "parent rollout path changed while it was read",
        )
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ReviewWorkflowError(
            "invalid_decision_receipt",
            "parent rollout changed while identity evidence was parsed",
        )
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ReviewWorkflowError(
            "invalid_decision_receipt",
            "parent rollout must be UTF-8 without BOM",
        )

    records: list[tuple[int, int, Mapping[str, Any]]] = []
    offset = 0
    try:
        for line in raw.splitlines(keepends=True):
            start = offset
            offset += len(line)
            if not line.strip():
                continue
            event = json.loads(line.decode("utf-8"))
            if not isinstance(event, Mapping):
                raise ValueError("rollout event must be a JSON object")
            records.append((start, offset, event))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ReviewWorkflowError(
            "invalid_decision_receipt",
            "parent rollout is not UTF-8 JSONL",
        ) from exc
    return raw, records


def _verified_user_choice(
    runtime: Mapping[str, Any],
    decision: Mapping[str, Any],
    *,
    expected_parent_thread_id: object,
    expected_parent_model: object,
    expected_parent_effort: object,
    question_id: str,
) -> tuple[str, dict[str, Any]]:
    """Resolve a finite choice only from a trusted parent rollout user reply."""

    parent_thread_id = str(expected_parent_thread_id or "").strip()
    parent_model = str(expected_parent_model or "").strip()
    parent_effort = str(expected_parent_effort or "").strip()
    if not parent_thread_id or not parent_model or not parent_effort:
        raise ReviewWorkflowError(
            "parent_runtime_unavailable",
            "review decision requires the reviewer child evidence's explicit parent task identity",
        )
    marker = decision.get("binding_marker")
    choice_request = decision.get("choice_request")
    if not isinstance(marker, str) or not marker or not isinstance(choice_request, Mapping):
        raise ReviewWorkflowError(
            "invalid_decision_receipt",
            "pending decision lacks its finite-choice marker or request",
        )

    trusted_root = _trusted_codex_sessions_root()
    try:
        supplied_root_raw = Path(str(runtime["sessions_root"]))
        _reject_reparse_chain(
            supplied_root_raw,
            code="untrusted_sessions_root",
            label="decision sessions_root",
        )
        supplied_root = supplied_root_raw.resolve(strict=True)
        rollout_raw = Path(str(runtime["rollout_path"]))
        _reject_reparse_chain(
            rollout_raw,
            code="invalid_decision_receipt",
            label="decision parent rollout",
        )
        rollout_path = rollout_raw.resolve(strict=True)
        supplied_parent_thread_id = str(runtime["parent_thread_id"])
    except ReviewWorkflowError:
        raise
    except (KeyError, OSError) as exc:
        raise ReviewWorkflowError("invalid_decision_receipt", str(exc)) from exc
    if supplied_root != trusted_root or _is_reparse(supplied_root_raw):
        raise ReviewWorkflowError(
            "untrusted_sessions_root",
            "decision sessions_root does not equal the host-owned Codex sessions root",
        )
    if supplied_parent_thread_id != parent_thread_id:
        raise ReviewWorkflowError(
            "invalid_decision_receipt",
            "decision rollout is not the parent task that spawned the bound reviewer child",
        )
    if (
        not _inside(rollout_path, trusted_root)
        or _is_reparse(rollout_raw)
        or rollout_path.suffix.casefold() != ".jsonl"
    ):
        raise ReviewWorkflowError(
            "invalid_decision_receipt",
            "parent rollout must be a JSONL file under the trusted Codex sessions root",
        )
    try:
        evidence = parse_parent_rollout_identity(
            rollout_path,
            expected_thread_id=parent_thread_id,
            expected_model=parent_model,
            expected_reasoning_effort=parent_effort,
            sessions_root=trusted_root,
        )
    except SmokeEvidenceError as exc:
        raise ReviewWorkflowError("invalid_decision_receipt", str(exc)) from exc
    raw, records = _stable_parent_rollout_records(
        rollout_path,
        expected_sha256=evidence.raw_sha256,
    )
    try:
        _, parent_session = coalesce_session_meta_payloads(
            [event for _, _, event in records],
            expected_thread_id=parent_thread_id,
        )
    except SmokeEvidenceError as exc:
        raise ReviewWorkflowError(
            "invalid_decision_receipt",
            f"decision receipt parent session identity is invalid: {exc}",
        ) from exc
    source = parent_session.get("source")
    if (
        (isinstance(source, Mapping) and isinstance(source.get("subagent"), Mapping))
        or bool(str(parent_session.get("parent_thread_id") or "").strip())
    ):
        raise ReviewWorkflowError(
            "invalid_decision_receipt",
            "decision receipt rollout belongs to a child Agent, not the parent task",
        )

    marker_records: list[int] = []
    for index, (_, _, event) in enumerate(records):
        if event.get("type") != "response_item":
            continue
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            continue
        parsed = _message_text(payload)
        if parsed is None:
            continue
        role, text, _ = parsed
        if role == "assistant" and marker in [line.strip() for line in text.splitlines()]:
            marker_records.append(index)
    if len(marker_records) != 1:
        raise ReviewWorkflowError(
            "invalid_decision_receipt",
            "parent rollout must contain exactly one exact assistant decision marker",
        )

    answer_text = ""
    answer_end = 0
    for _, end, event in records[marker_records[0] + 1 :]:
        if event.get("type") != "response_item":
            continue
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            continue
        parsed = _message_text(payload)
        if parsed is None or not parsed[1].strip():
            continue
        role, text, _ = parsed
        if role != "user":
            raise ReviewWorkflowError(
                "decision_answer_missing",
                "the next durable message after the decision marker is not a user answer",
            )
        answer_text = text.strip()
        answer_end = end
        break
    if not answer_text or answer_end <= 0:
        raise ReviewWorkflowError(
            "decision_answer_missing",
            "parent rollout has no user answer after the exact decision marker",
        )
    if len(answer_text.encode("utf-8")) > MAX_DECISION_ANSWER_BYTES:
        raise ReviewWorkflowError(
            "invalid_decision_answer",
            "decision answer exceeds the bounded size",
        )
    try:
        resolution = resolve_choice(choice_request, answer_text)
    except ChoiceProtocolError as exc:
        raise ReviewWorkflowError("invalid_decision_answer", str(exc)) from exc
    choice = (resolution.get("selected_branches") or {}).get(question_id)
    if (
        resolution.get("status") != "selected"
        or resolution.get("write_allowed") is not True
        or not isinstance(choice, str)
    ):
        raise ReviewWorkflowError(
            "invalid_decision_answer",
            "user answer did not select exactly one offered review choice",
        )
    return choice, {
        "evidence_source": evidence.evidence_source,
        "rollout_path": str(rollout_path),
        "sessions_root": str(trusted_root),
        "parent_thread_id": evidence.thread_id,
        "parent_model": evidence.model,
        "parent_reasoning_effort": evidence.reasoning_effort,
        "evidence_sha256": evidence.raw_sha256,
        "authorization_prefix_sha256": hashlib.sha256(raw[:answer_end]).hexdigest(),
        "binding_marker_sha256": hashlib.sha256(marker.encode("utf-8")).hexdigest(),
        "answer_sha256": hashlib.sha256(answer_text.encode("utf-8")).hexdigest(),
        "request_id": resolution.get("request_id"),
    }


def _reverify_selected_decision(
    decision: Mapping[str, Any],
    *,
    expected_parent_thread_id: object,
    expected_parent_model: object,
    expected_parent_effort: object,
    question_id: str,
) -> str:
    """Revalidate a persisted receipt while permitting rollout-only appends."""

    receipt = decision.get("runtime_receipt")
    if not isinstance(receipt, Mapping):
        raise ReviewWorkflowError(
            "invalid_decision_receipt",
            "selected review decision has no trusted runtime receipt",
        )
    runtime = {
        "rollout_path": receipt.get("rollout_path"),
        "sessions_root": receipt.get("sessions_root"),
        "parent_thread_id": receipt.get("parent_thread_id"),
    }
    choice, current = _verified_user_choice(
        runtime,
        decision,
        expected_parent_thread_id=expected_parent_thread_id,
        expected_parent_model=expected_parent_model,
        expected_parent_effort=expected_parent_effort,
        question_id=question_id,
    )
    stable_fields = {
        "evidence_source",
        "rollout_path",
        "sessions_root",
        "parent_thread_id",
        "parent_model",
        "parent_reasoning_effort",
        "authorization_prefix_sha256",
        "binding_marker_sha256",
        "answer_sha256",
        "request_id",
    }
    if any(receipt.get(field) != current.get(field) for field in stable_fields):
        raise ReviewWorkflowError(
            "invalid_decision_receipt",
            "stored review decision receipt no longer matches its trusted rollout prefix",
        )
    if choice != decision.get("selected"):
        raise ReviewWorkflowError(
            "invalid_decision_receipt",
            "stored review decision differs from the trusted user answer",
        )
    return choice


def _validate_selected_decision_scope(
    decision: Mapping[str, Any],
    expected_pending: Mapping[str, Any],
) -> None:
    """Ensure a stored selection is the same scoped request originally shown."""

    immutable_fields = set(expected_pending) - {"status", "write_allowed"}
    if any(decision.get(field) != expected_pending.get(field) for field in immutable_fields):
        raise ReviewWorkflowError(
            "stale_decision",
            "stored review decision is not bound to the current run or range scope",
        )


def _decision_parent_thread_id(run: Mapping[str, Any]) -> str:
    """Return the trusted parent task recorded by the bound reviewer child."""

    expected_parent_thread_id = str(run.get("parent_thread_id") or "").strip()
    evidence = run.get("runtime_evidence")
    evidence_parent_thread_id = (
        str(evidence.get("parent_thread_id") or "").strip()
        if isinstance(evidence, Mapping)
        else ""
    )
    if (
        not expected_parent_thread_id
        or not evidence_parent_thread_id
        or evidence_parent_thread_id != expected_parent_thread_id
    ):
        raise ReviewWorkflowError(
            "parent_runtime_unavailable",
            "review decision parent does not match prepare-time and reviewer child evidence",
        )
    return expected_parent_thread_id


def _verify_run_decision_receipt(run: Mapping[str, Any]) -> str:
    decision = run.get("decision")
    if not isinstance(decision, Mapping):
        raise ReviewWorkflowError(
            "invalid_decision_receipt",
            "blocking review has no selected decision receipt",
        )
    _validate_selected_decision_scope(decision, _decision_payload(run))
    return _reverify_selected_decision(
        decision,
        expected_parent_thread_id=_decision_parent_thread_id(run),
        expected_parent_model=run.get("parent_model"),
        expected_parent_effort=run.get("parent_reasoning_effort"),
        question_id="review_action",
    )


def _chapter_file(project_root: Path, chapter: int) -> Path:
    if type(chapter) is not int or chapter <= 0:
        raise ReviewWorkflowError("invalid_chapter", "chapter must be a positive integer")
    chapters = project_root / "正文"
    if not chapters.is_dir() or _is_reparse(chapters):
        raise ReviewWorkflowError("chapter_missing", "正文 directory is missing or unsafe")

    candidates: list[Path] = []
    unsafe_matches: list[str] = []
    for current, dir_names, file_names in os.walk(chapters, followlinks=False):
        current_path = Path(current)
        kept_dirs: list[str] = []
        for name in dir_names:
            child = current_path / name
            if _is_reparse(child):
                unsafe_matches.append(str(child))
            else:
                kept_dirs.append(name)
        dir_names[:] = kept_dirs
        for name in file_names:
            if Path(name).suffix.casefold() != ".md":
                continue
            if extract_chapter_num_from_filename(name) != chapter:
                continue
            candidate = current_path / name
            if _is_reparse(candidate) or not _safe_project_path(candidate, project_root, require_file=True):
                unsafe_matches.append(str(candidate))
            else:
                candidates.append(candidate.resolve(strict=True))
    candidates = sorted(set(candidates), key=lambda value: str(value).casefold())
    if unsafe_matches:
        raise ReviewWorkflowError(
            "unsafe_chapter_path",
            "chapter resolution encountered a symlink, junction, or reparse path",
            details={"paths": sorted(set(unsafe_matches))},
        )
    if not candidates:
        raise ReviewWorkflowError("chapter_missing", f"chapter {chapter} does not exist")
    if len(candidates) != 1:
        raise ReviewWorkflowError(
            "ambiguous_chapter",
            f"chapter {chapter} resolves to more than one file",
            details={"paths": [str(path) for path in candidates]},
        )
    try:
        raw = candidates[0].read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            raise UnicodeDecodeError("utf-8", raw, 0, 3, "BOM is forbidden")
        raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ReviewWorkflowError("chapter_encoding_invalid", "chapter must be readable UTF-8 without BOM") from exc
    return candidates[0]


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_with_retry(temp_path, path)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _write_json_once(path: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file():
            raise ReviewWorkflowError("artifact_collision", f"unsafe existing artifact: {path}")
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ReviewWorkflowError("artifact_collision", f"existing artifact is unreadable: {path}") from exc
        if existing != payload:
            raise ReviewWorkflowError("artifact_collision", f"existing artifact content differs: {path}")
    else:
        atomic_write_json(path, dict(payload), use_lock=False, backup=False)
    return file_signature(path)


def _write_text_once(path: Path, text: str) -> dict[str, Any]:
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file():
            raise ReviewWorkflowError("artifact_collision", f"unsafe existing report: {path}")
        try:
            existing = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ReviewWorkflowError("artifact_collision", f"existing report is unreadable: {path}") from exc
        if existing != text:
            raise ReviewWorkflowError("artifact_collision", f"existing report content differs: {path}")
    else:
        _atomic_write_text(path, text)
    return file_signature(path)


def _new_run_id(chapter: int) -> str:
    return f"rv-ch{chapter:04d}-{uuid.uuid4().hex[:16]}"


def _new_range_id(start: int, end: int) -> str:
    return f"rr-ch{start:04d}-{end:04d}-{uuid.uuid4().hex[:12]}"


def _run_dir(root: Path, run_id: str) -> Path:
    if not valid_review_run_id(run_id):
        raise ReviewWorkflowError("invalid_run_id", "review run id is invalid")
    path = root / ".webnovel" / "tmp" / "review-runs" / run_id
    if not _safe_project_path(path, root):
        raise ReviewWorkflowError("unsafe_run_path", "review run path escapes the project")
    return path


def _range_lock(root: Path, range_id: str) -> _VerifiedProjectFileLock:
    if not valid_review_range_id(range_id):
        raise ReviewWorkflowError("invalid_range_id", "review range id is invalid")
    lock_path = root / ".webnovel" / "tmp" / "review-ranges" / f"{range_id}.lock"
    return _VerifiedProjectFileLock(lock_path, root, code="unsafe_range_path")


def _run_lock(root: Path, run_id: str) -> _VerifiedProjectFileLock:
    path = _run_dir(root, run_id) / ".workflow.lock"
    return _VerifiedProjectFileLock(path, root, code="unsafe_run_path")


def _route(
    workspace_root: Path,
    *,
    parent_model: str,
    parent_reasoning_effort: str | None,
) -> dict[str, Any]:
    route = build_workflow_route(
        "review",
        parent_model=parent_model,
        parent_reasoning_effort=parent_reasoning_effort,
        plugin_root=_plugin_root(),
    )
    readiness = validate_route_readiness(workspace_root, route, plugin_root=_plugin_root())
    if not readiness.get("ready"):
        raise ReviewWorkflowError(
            "agent_unavailable",
            "managed reviewer is missing or stale",
            details={"problems": readiness.get("problems") or []},
        )
    steps = route.get("steps") or []
    if len(steps) != 1 or steps[0].get("agent_name") != "webnovel_reviewer":
        raise ReviewWorkflowError("invalid_route", "review route must contain only webnovel_reviewer")
    return route


def _build_context(root: Path, chapter: int) -> dict[str, Any]:
    config = DataModulesConfig.from_project_root(root)
    adapter = MemoryContractAdapter(config, read_only=True)
    pack = adapter.load_context(chapter)
    payload = pack.to_dict()
    if not isinstance(payload, dict):
        raise ReviewWorkflowError("context_unavailable", "memory-contract returned an invalid context object")
    encoded = _canonical_json(payload).encode("utf-8")
    if len(encoded) > MAX_CONTEXT_BYTES:
        raise ReviewWorkflowError("context_too_large", "review context exceeds the bounded artifact size")
    return payload


def _active_review_conflict(ledger: Mapping[str, Any], chapter: int) -> str | None:
    runs = (((ledger.get("review") or {}).get("runs")) or {})
    for run_id, run in runs.items():
        if not isinstance(run, Mapping):
            continue
        if run.get("chapter") == chapter and run.get("status") in ACTIVE_RUN_STATUSES:
            return str(run_id)
    return None


def _update_run(root: Path, run_id: str, updater) -> dict[str, Any]:
    with locked_ledger(root, strict=True) as ledger:
        runs = ledger["review"]["runs"]
        run = runs.get(run_id)
        if not isinstance(run, dict) or run.get("run_id") != run_id:
            raise ReviewWorkflowError("run_not_found", f"review run does not exist: {run_id}")
        updater(run)
        run["updated_at"] = _now_iso()
        return json.loads(json.dumps(run, ensure_ascii=False))


def _update_range(root: Path, range_id: str, updater) -> dict[str, Any]:
    with locked_ledger(root, strict=True) as ledger:
        ranges = ledger["review"]["ranges"]
        entry = ranges.get(range_id)
        if not isinstance(entry, dict) or entry.get("range_id") != range_id:
            raise ReviewWorkflowError("range_not_found", f"review range does not exist: {range_id}")
        updater(entry)
        entry["updated_at"] = _now_iso()
        return json.loads(json.dumps(entry, ensure_ascii=False))


def prepare_review(
    project_root: str | Path,
    *,
    chapter: int,
    review_mode: str,
    workspace_root: str | Path,
    parent_model: str,
    parent_reasoning_effort: str | None = None,
    range_id: str | None = None,
    _range_index: int | None = None,
) -> dict[str, Any]:
    """Prepare one immutable request artifact without invoking an Agent."""

    root = _project_root(project_root)
    parent_thread_id = _current_codex_thread_id()
    workspace = _workspace_root(workspace_root)
    if review_mode not in REVIEW_MODES:
        raise ReviewWorkflowError("invalid_review_mode", "review mode must be full or fast")
    if not isinstance(parent_model, str) or not parent_model.strip():
        raise ReviewWorkflowError("invalid_parent_model", "parent model must be explicit")
    if range_id is not None and not valid_review_range_id(range_id):
        raise ReviewWorkflowError("invalid_range_id", "range id is invalid")
    if (range_id is None) != (_range_index is None):
        raise ReviewWorkflowError("invalid_range_id", "range id and index must be provided together")
    chapter_path = _chapter_file(root, chapter)
    route = _route(
        workspace,
        parent_model=parent_model.strip(),
        parent_reasoning_effort=parent_reasoning_effort,
    )
    _recover_sqlite_wal_if_needed(root)
    _validate_sqlite_bundle(root)
    protected_before = snapshot_protected_state(root)
    context_payload = _build_context(root, chapter)
    _validate_sqlite_bundle(root)
    protected_after = snapshot_protected_state(root)
    protected_check = validate_protected_state_snapshots(protected_before, protected_after)
    if not protected_check.get("accepted"):
        raise ReviewWorkflowError(
            "readonly_context_mutated_project",
            "read-only context preparation changed protected project state",
            details={"changed_paths": protected_check.get("changed_paths") or []},
        )

    run_id = _new_run_id(chapter)
    run_dir = _run_dir(root, run_id)
    run_dir.mkdir(parents=True, exist_ok=False)
    context_path = run_dir / "context.json"
    context_signature = _write_json_once(context_path, context_payload)
    chapter_signature = file_signature(chapter_path)
    route_sha256 = _sha_payload(route)
    request_payload = {
        "schema_version": REVIEW_REQUEST_SCHEMA,
        "run_id": run_id,
        "range_id": range_id,
        "chapter": chapter,
        "review_mode": review_mode,
        "project_root": str(root),
        "workspace_root": str(workspace),
        "parent_thread_id": parent_thread_id,
        "agent_name": "webnovel_reviewer",
        "inputs": [
            {"kind": "chapter", **chapter_signature},
            {"kind": "context", **context_signature},
        ],
        "route": route,
        "route_sha256": route_sha256,
        "instructions": {
            "return": "one strict JSON object only",
            "write_allowed": False,
            "shell_interpolation_allowed": False,
        },
    }
    request_path = run_dir / "request.json"
    request_signature = _write_json_once(request_path, request_payload)
    created_at = _now_iso()
    run = {
        "schema_version": REVIEW_WORKFLOW_SCHEMA,
        "run_id": run_id,
        "range_id": range_id,
        "chapter": chapter,
        "review_mode": review_mode,
        "status": "prepared",
        "project_root_hash": _path_hash(root),
        "workspace_root": str(workspace),
        "parent_thread_id": parent_thread_id,
        "parent_model": parent_model.strip(),
        "parent_reasoning_effort": parent_reasoning_effort,
        "agent_name": "webnovel_reviewer",
        "requested_model": route["steps"][0]["requested_model"],
        "requested_reasoning_effort": route["steps"][0]["requested_reasoning_effort"],
        "contract_hash": route["steps"][0]["contract_hash"],
        "route_sha256": route_sha256,
        "request_sha256": request_signature["sha256"],
        "inputs": {"chapter": chapter_signature, "context": context_signature},
        "protected_before": protected_after,
        "attempts": [],
        "decision": None,
        "artifacts": {"request": request_signature, "context": context_signature},
        "stages": {
            "prepared": {"status": "completed", "at": created_at},
            "reviewer": {"status": "pending"},
            "decision": {"status": "pending"},
            "artifacts": {"status": "pending"},
            "metrics_db": {"status": "pending"},
        },
        "problems": [],
        "created_at": created_at,
        "updated_at": created_at,
    }
    binding_marker = _binding_marker(run)
    try:
        agent_task_name = derive_agent_task_name(binding_marker, prefix="wnr")
    except SmokeEvidenceError as exc:
        raise ReviewWorkflowError("request_binding_mismatch", str(exc)) from exc
    agent_path = f"/root/{agent_task_name}"
    run["binding_marker_sha256"] = hashlib.sha256(binding_marker.encode("utf-8")).hexdigest()
    run["agent_task_name"] = agent_task_name
    run["agent_path"] = agent_path
    try:
        with locked_ledger(root, strict=True) as ledger:
            conflict = _active_review_conflict(ledger, chapter)
            if conflict:
                raise ReviewWorkflowError(
                    "review_conflict",
                    f"chapter {chapter} already has an active review run",
                    details={"run_id": conflict},
                )
            ledger["review"]["runs"][run_id] = run
            if range_id is not None:
                ranges = ledger["review"]["ranges"]
                entry = ranges.get(range_id)
                if not isinstance(entry, dict) or entry.get("range_id") != range_id:
                    raise ReviewWorkflowError("range_not_found", f"review range does not exist: {range_id}")
                index = int(_range_index)
                chapters = entry.get("chapters") or []
                run_ids = entry.get("run_ids") or []
                if (
                    index < 0
                    or index >= len(chapters)
                    or index >= len(run_ids)
                    or chapters[index] != chapter
                    or run_ids[index] is not None
                ):
                    raise ReviewWorkflowError(
                        "range_state_changed",
                        "review range no longer accepts this chapter run",
                    )
                run_ids[index] = run_id
                entry["run_ids"] = run_ids
                entry["current_index"] = index
                entry["status"] = "in_progress"
                entry["decision"] = None
                entry["updated_at"] = _now_iso()
    except Exception:
        # An unreferenced, run-id-specific directory contains no canon/read
        # model data and is deliberately left for forensic recovery.
        raise
    return {
        "schema_version": REVIEW_WORKFLOW_SCHEMA,
        "status": "prepared",
        "run_id": run_id,
        "range_id": range_id,
        "chapter": chapter,
        "review_mode": review_mode,
        "parent_thread_id": parent_thread_id,
        "request_file": str(request_path),
        "request_sha256": request_signature["sha256"],
        "binding_marker": binding_marker,
        "agent_task_name": agent_task_name,
        "agent_path": agent_path,
        "agent_name": "webnovel_reviewer",
        "requested_model": route["steps"][0]["requested_model"],
        "requested_reasoning_effort": route["steps"][0]["requested_reasoning_effort"],
        "next_action": "invoke the managed reviewer in a native child Agent, then use review accept",
        "live_gate": "pending_explicit_codex_runtime_evidence",
    }


def _run_input_error(root: Path, run: Mapping[str, Any]) -> str | None:
    inputs = run.get("inputs") if isinstance(run.get("inputs"), Mapping) else {}
    for name in ("chapter", "context"):
        expected = inputs.get(name)
        if not isinstance(expected, Mapping):
            return f"missing {name} input signature"
        path = Path(str(expected.get("path") or ""))
        if not path.is_absolute() or not _safe_project_path(path, root, require_file=True):
            return f"unsafe {name} input path"
        try:
            raw_input = _stable_project_bytes(path, root, max_bytes=MAX_REVIEW_INPUT_BYTES)
        except OSError:
            return f"{name} input cannot be read safely"
        if hashlib.sha256(raw_input).hexdigest() != expected.get("sha256"):
            return f"{name} input hash changed"
    artifacts = run.get("artifacts") if isinstance(run.get("artifacts"), Mapping) else {}
    request_signature = artifacts.get("request") if isinstance(artifacts, Mapping) else None
    if not isinstance(request_signature, Mapping):
        return "missing request artifact signature"
    request_path = Path(str(request_signature.get("path") or ""))
    expected_request_path = _run_dir(root, str(run.get("run_id") or "")) / "request.json"
    if (
        not request_path.is_absolute()
        or request_path != expected_request_path
        or not _safe_project_path(request_path, root, require_file=True)
    ):
        return "unsafe request artifact path"
    try:
        request_raw = _stable_project_bytes(
            request_path,
            root,
            max_bytes=MAX_REVIEW_INPUT_BYTES,
        )
    except OSError:
        return "request artifact cannot be read safely"
    current_request_sha256 = hashlib.sha256(request_raw).hexdigest()
    if (
        current_request_sha256 != request_signature.get("sha256")
        or current_request_sha256 != run.get("request_sha256")
    ):
        return "request artifact hash changed"
    try:
        request_payload = json.loads(request_raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return "request artifact is not valid UTF-8 JSON"
    if not isinstance(request_payload, Mapping):
        return "request artifact is not a JSON object"
    if (
        request_payload.get("schema_version") != REVIEW_REQUEST_SCHEMA
        or request_payload.get("run_id") != run.get("run_id")
        or request_payload.get("range_id") != run.get("range_id")
        or request_payload.get("chapter") != run.get("chapter")
        or request_payload.get("review_mode") != run.get("review_mode")
        or request_payload.get("parent_thread_id") != run.get("parent_thread_id")
        or request_payload.get("route_sha256") != run.get("route_sha256")
        or _sha_payload(request_payload.get("route")) != run.get("route_sha256")
    ):
        return "request artifact provenance changed"
    request_inputs = request_payload.get("inputs")
    if not isinstance(request_inputs, list) or len(request_inputs) != 2:
        return "request artifact inputs changed"
    for item, name in zip(request_inputs, ("chapter", "context"), strict=True):
        expected = inputs.get(name)
        if (
            not isinstance(item, Mapping)
            or item.get("kind") != name
            or item.get("path") != expected.get("path")
            or item.get("sha256") != expected.get("sha256")
        ):
            return "request artifact inputs changed"
    return None


def _current_route(root: Path, run: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    workspace = _workspace_root(str(run.get("workspace_root") or ""))
    route = _route(
        workspace,
        parent_model=str(run.get("parent_model") or ""),
        parent_reasoning_effort=run.get("parent_reasoning_effort"),
    )
    if _sha_payload(route) != run.get("route_sha256"):
        raise ReviewWorkflowError("contract_hash_mismatch", "managed reviewer route changed after prepare")
    return route, route["steps"][0]


def _verified_runtime_evidence(
    runtime: Mapping[str, Any],
    expected_step: Mapping[str, Any],
    *,
    expected_parent_thread_id: str,
    binding_marker: str,
) -> VerifiedReviewerExecution:
    trusted_root = _trusted_codex_sessions_root()
    try:
        expected_task_name = derive_agent_task_name(binding_marker, prefix="wnr")
    except SmokeEvidenceError as exc:
        raise ReviewWorkflowError("invalid_runtime_evidence", str(exc)) from exc
    expected_agent_path = f"/root/{expected_task_name}"
    try:
        supplied_root_raw = Path(str(runtime["sessions_root"]))
        _reject_reparse_chain(
            supplied_root_raw,
            code="untrusted_sessions_root",
            label="request sessions_root",
        )
        supplied_root = supplied_root_raw.resolve(strict=True)
        rollout_raw = Path(str(runtime["rollout_path"]))
        rollout_path = rollout_raw.resolve(strict=True)
    except ReviewWorkflowError:
        raise
    except (KeyError, OSError) as exc:
        raise ReviewWorkflowError("invalid_runtime_evidence", str(exc)) from exc
    if supplied_root != trusted_root or _is_reparse(supplied_root_raw):
        raise ReviewWorkflowError(
            "untrusted_sessions_root",
            "request sessions_root does not equal the host-owned Codex sessions root",
        )
    if str(runtime.get("parent_thread_id") or "") != expected_parent_thread_id:
        raise ReviewWorkflowError(
            "invalid_runtime_evidence",
            "reviewer rollout parent does not equal the prepare-time CODEX_THREAD_ID",
        )
    if not _inside(rollout_path, trusted_root) or _is_reparse(rollout_raw):
        raise ReviewWorkflowError(
            "invalid_runtime_evidence",
            "rollout must be a regular file under the trusted Codex sessions root",
        )
    try:
        evidence = parse_rollout_runtime_evidence(
            rollout_path,
            expected_thread_id=runtime["child_thread_id"],
            expected_parent_thread_id=expected_parent_thread_id,
            expected_agent_role=str(expected_step["agent_name"]),
            expected_model=str(expected_step["requested_model"]),
            expected_reasoning_effort=str(expected_step["requested_reasoning_effort"]),
            expected_task_name=expected_task_name,
            sessions_root=trusted_root,
        )
    except (KeyError, SmokeEvidenceError) as exc:
        raise ReviewWorkflowError("invalid_runtime_evidence", str(exc)) from exc
    outputs = _extract_bound_reviewer_outputs(
        rollout_path,
        evidence=evidence,
        binding_marker=binding_marker,
        verified_agent_path=expected_agent_path,
    )
    return VerifiedReviewerExecution(runtime=evidence, raw_outputs=outputs)


def _reject_reused_runtime_reference(
    root: Path,
    run_id: str,
    runtime: Mapping[str, Any],
) -> None:
    """Preserve the pre-existing cross-run reuse gate before task-path parsing."""

    child_thread_id = str(runtime.get("child_thread_id") or "")
    try:
        rollout_path = str(Path(str(runtime.get("rollout_path") or "")).resolve(strict=True))
    except OSError:
        return
    with locked_ledger(root, strict=True) as ledger:
        for other_id, other in ledger["review"]["runs"].items():
            if other_id == run_id or not isinstance(other, Mapping):
                continue
            other_evidence = other.get("runtime_evidence")
            if not isinstance(other_evidence, Mapping):
                continue
            if (
                other_evidence.get("child_thread_id") == child_thread_id
                or other_evidence.get("rollout_path") == rollout_path
            ):
                raise ReviewWorkflowError(
                    "runtime_evidence_reused",
                    f"reviewer rollout or child thread is already bound to run {other_id}",
                )


def _claim_runtime_evidence(
    root: Path,
    run_id: str,
    *,
    execution: VerifiedReviewerExecution,
    runtime: Mapping[str, Any],
    binding_marker: str,
) -> dict[str, Any]:
    evidence = execution.runtime
    rollout_path = str(Path(str(runtime["rollout_path"])).resolve(strict=True))
    output_hashes = [hashlib.sha256(text.encode("utf-8")).hexdigest() for text in execution.raw_outputs]
    try:
        agent_task_name = derive_agent_task_name(binding_marker, prefix="wnr")
    except SmokeEvidenceError as exc:
        raise ReviewWorkflowError("invalid_runtime_evidence", str(exc)) from exc
    claim = {
        "evidence_source": evidence.evidence_source,
        "evidence_sha256": evidence.raw_sha256,
        "rollout_path": rollout_path,
        "child_thread_id": evidence.thread_id,
        "parent_thread_id": evidence.parent_thread_id,
        "agent_task_name": agent_task_name,
        "agent_path": f"/root/{agent_task_name}",
        "binding_marker_sha256": hashlib.sha256(binding_marker.encode("utf-8")).hexdigest(),
        "output_sha256s": output_hashes,
    }
    with locked_ledger(root, strict=True) as ledger:
        runs = ledger["review"]["runs"]
        run = runs.get(run_id)
        if not isinstance(run, dict) or run.get("run_id") != run_id:
            raise ReviewWorkflowError("run_not_found", f"review run does not exist: {run_id}")
        for other_id, other in runs.items():
            if other_id == run_id or not isinstance(other, Mapping):
                continue
            other_evidence = other.get("runtime_evidence")
            if not isinstance(other_evidence, Mapping):
                continue
            if (
                other_evidence.get("evidence_sha256") == evidence.raw_sha256
                or other_evidence.get("child_thread_id") == evidence.thread_id
                or other_evidence.get("rollout_path") == rollout_path
            ):
                raise ReviewWorkflowError(
                    "runtime_evidence_reused",
                    f"reviewer rollout or child thread is already bound to run {other_id}",
                )
        existing = run.get("runtime_evidence")
        if existing is not None and existing != claim:
            raise ReviewWorkflowError(
                "runtime_evidence_changed",
                "review run already claimed different runtime evidence",
            )
        run["runtime_evidence"] = claim
        run["updated_at"] = _now_iso()
    return claim


def _reverify_runtime_receipt(root: Path, run: Mapping[str, Any]) -> None:
    """Revalidate a persisted reviewer receipt, including its task-name path."""

    claim = run.get("runtime_evidence")
    if not isinstance(claim, Mapping):
        return
    binding_marker, agent_task_name, agent_path = _reviewer_agent_binding(run)
    marker_sha256 = hashlib.sha256(binding_marker.encode("utf-8")).hexdigest()
    if run.get("binding_marker_sha256") != marker_sha256:
        raise ReviewWorkflowError("request_binding_mismatch", "review request binding marker changed")
    expected_step = {
        "agent_name": run.get("agent_name"),
        "requested_model": run.get("requested_model"),
        "requested_reasoning_effort": run.get("requested_reasoning_effort"),
    }
    execution = _verified_runtime_evidence(
        {
            "rollout_path": claim.get("rollout_path"),
            "sessions_root": str(_trusted_codex_sessions_root()),
            "child_thread_id": claim.get("child_thread_id"),
            "parent_thread_id": claim.get("parent_thread_id"),
        },
        expected_step,
        expected_parent_thread_id=str(run.get("parent_thread_id") or ""),
        binding_marker=binding_marker,
    )
    current = {
        "evidence_source": execution.runtime.evidence_source,
        "evidence_sha256": execution.runtime.raw_sha256,
        "rollout_path": str(Path(str(claim.get("rollout_path") or "")).resolve(strict=True)),
        "child_thread_id": execution.runtime.thread_id,
        "parent_thread_id": execution.runtime.parent_thread_id,
        "agent_task_name": agent_task_name,
        "agent_path": agent_path,
        "binding_marker_sha256": marker_sha256,
        "output_sha256s": [
            hashlib.sha256(text.encode("utf-8")).hexdigest()
            for text in execution.raw_outputs
        ],
    }
    if any(claim.get(field) != value for field, value in current.items()):
        raise ReviewWorkflowError(
            "runtime_evidence_changed",
            "stored reviewer runtime receipt no longer matches its bound rollout",
        )


def _runtime_envelope(
    step: Mapping[str, Any],
    evidence: VerifiedRuntimeEvidence,
    *,
    raw_outputs: Sequence[str],
) -> dict[str, Any]:
    return {
        "schema_version": ENVELOPE_SCHEMA_VERSION,
        "agent_name": step.get("agent_name"),
        "status": "completed",
        "requested_model": step.get("requested_model"),
        "actual_model": evidence.actual_model,
        "requested_reasoning_effort": step.get("requested_reasoning_effort"),
        "actual_reasoning_effort": evidence.actual_reasoning_effort,
        "parent_model": step.get("parent_model"),
        "parent_reasoning_effort": step.get("parent_reasoning_effort"),
        "contract_hash": step.get("contract_hash"),
        "evidence_source": evidence.evidence_source,
        "fallback_used": False,
        "artifacts": [
            {
                "kind": "reviewer_output",
                "attempt": index,
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "bytes": len(text.encode("utf-8")),
            }
            for index, text in enumerate(raw_outputs, start=1)
        ],
    }


def _normalized_artifact(
    run: Mapping[str, Any],
    result: ReviewResult,
    *,
    reviewer_output_sha256: str,
) -> dict[str, Any]:
    payload = {
        "schema_version": REVIEW_ARTIFACT_SCHEMA,
        "run_id": run["run_id"],
        "range_id": run.get("range_id"),
        "review_mode": run["review_mode"],
        "chapter_sha256": run["inputs"]["chapter"]["sha256"],
        "context_sha256": run["inputs"]["context"]["sha256"],
        "reviewer_output_sha256": reviewer_output_sha256,
    }
    payload.update(result.to_dict())
    return payload


def _decision_payload(run: Mapping[str, Any]) -> dict[str, Any]:
    parent_thread_id = _decision_parent_thread_id(run)
    scope = {
        "workflow": "review",
        "project_root_hash": run.get("project_root_hash"),
        "run_id": run.get("run_id"),
        "range_id": run.get("range_id"),
        "chapter": run.get("chapter"),
        "reviewer_output_sha256": run.get("reviewer_output_sha256"),
        "parent_thread_id": parent_thread_id,
        "kind": "blocking_review",
    }
    scope_hash = _sha_payload(scope)
    choice_request = build_choice_request(
        [
            {
                "id": "review_action",
                "prompt": "本章存在 blocking issue，请选择唯一处理方式。",
                "options": [
                    {
                        "id": "targeted_fix",
                        "label": "定点修复",
                        "description": "进入受管 writer 事务，当前 Review 不修改正文。",
                        "recommended": True,
                    },
                    {
                        "id": "report_only",
                        "label": "仅保存报告",
                        "description": "保存报告和指标，正文保持不变。",
                        "recommended": False,
                    },
                    {
                        "id": "abandon",
                        "label": "放弃",
                        "description": "保留内部证据，不生成报告或数据库记录。",
                        "recommended": False,
                    },
                ],
            }
        ]
    )
    marker_payload = {
        "kind": "run",
        "project_root_hash": run.get("project_root_hash"),
        "run_id": run.get("run_id"),
        "range_id": run.get("range_id"),
        "reviewer_output_sha256": run.get("reviewer_output_sha256"),
        "parent_thread_id": parent_thread_id,
        "scope_hash": scope_hash,
        "request_id": choice_request["request_id"],
    }
    return {
        "schema_version": REVIEW_DECISION_SCHEMA,
        "request_id": choice_request["request_id"],
        "scope_hash": scope_hash,
        "status": "awaiting_user",
        "question": "本章存在 blocking issue，请选择唯一处理方式。",
        "options": [
            {"id": "targeted_fix", "label": "定点修复", "recommended": True},
            {"id": "report_only", "label": "仅保存报告", "recommended": False},
            {"id": "abandon", "label": "放弃", "recommended": False},
        ],
        "choice_request": choice_request,
        "binding_marker": f"{REVIEW_DECISION_MARKER_SCHEMA} {_canonical_json(marker_payload)}",
        "write_allowed": False,
    }


def _mark_run_failure(root: Path, run_id: str, code: str, message: str, *, stage: str) -> dict[str, Any]:
    def apply(run: dict[str, Any]) -> None:
        run["status"] = "failed_validation" if stage == "reviewer" else "failed_persistence"
        run.setdefault("problems", []).append(
            {"code": code, "message": message, "stage": stage, "at": _now_iso()}
        )
        run.setdefault("stages", {}).setdefault(stage, {})
        run["stages"][stage] = {"status": "failed", "code": code, "at": _now_iso()}

    return _update_run(root, run_id, apply)


def accept_review(
    project_root: str | Path,
    *,
    run_id: str,
    request_file: str | Path,
) -> dict[str, Any]:
    """Validate live runtime identity and at most two reviewer responses."""

    root = _project_root(project_root)
    request = load_review_accept_request(request_file, project_root=root)
    if request["run_id"] != run_id:
        raise ReviewWorkflowError("request_run_mismatch", "request-file run_id does not match --run-id")
    with _run_lock(root, run_id):
        run = get_review_run(root, run_id)
        if run is None:
            raise ReviewWorkflowError("run_not_found", f"review run does not exist: {run_id}")
        if request["chapter"] != run.get("chapter") or request["review_mode"] != run.get("review_mode"):
            raise ReviewWorkflowError("request_scope_mismatch", "request-file chapter or mode is stale")
        parent_thread_id = _current_codex_thread_id(run.get("parent_thread_id"))
        if run.get("status") != "prepared":
            return resume_review(root, run_id=run_id, _lock_held=True)
        input_error = _run_input_error(root, run)
        if input_error:
            _mark_run_failure(root, run_id, "input_hash_mismatch", input_error, stage="reviewer")
            raise ReviewWorkflowError("input_hash_mismatch", input_error)
        protected = validate_protected_state_snapshots(
            run.get("protected_before"),
            snapshot_protected_state(root),
        )
        if not protected.get("accepted"):
            _mark_run_failure(
                root,
                run_id,
                "protected_state_changed",
                "protected project state changed during reviewer execution",
                stage="reviewer",
            )
            raise ReviewWorkflowError(
                "protected_state_changed",
                "protected project state changed during reviewer execution",
                details={"changed_paths": protected.get("changed_paths") or []},
            )

        binding_marker, _agent_task_name, _agent_path = _reviewer_agent_binding(run)
        if hashlib.sha256(binding_marker.encode("utf-8")).hexdigest() != run.get(
            "binding_marker_sha256"
        ):
            raise ReviewWorkflowError("request_binding_mismatch", "review request binding marker changed")
        _route_payload, step = _current_route(root, run)
        try:
            _reject_reused_runtime_reference(root, run_id, request["runtime"])
            execution = _verified_runtime_evidence(
                request["runtime"],
                step,
                expected_parent_thread_id=parent_thread_id,
                binding_marker=binding_marker,
            )
        except ReviewWorkflowError as exc:
            _mark_run_failure(root, run_id, exc.code, str(exc), stage="reviewer")
            raise
        evidence = execution.runtime
        envelope = _runtime_envelope(step, evidence, raw_outputs=execution.raw_outputs)
        envelope_result = validate_agent_envelope(step, envelope, verified_evidence=evidence)
        if not envelope_result.get("accepted"):
            code = str(envelope_result.get("code") or "runtime_identity_mismatch")
            detail = str(envelope_result.get("detail") or code)
            _mark_run_failure(root, run_id, code, detail, stage="reviewer")
            raise ReviewWorkflowError(code, detail)
        runtime_claim = _claim_runtime_evidence(
            root,
            run_id,
            execution=execution,
            runtime=request["runtime"],
            binding_marker=binding_marker,
        )

        attempts: list[dict[str, Any]] = []
        accepted_response: dict[str, Any] | None = None
        accepted_result: ReviewResult | None = None
        accepted_raw_text: str | None = None
        for index, raw_text in enumerate(execution.raw_outputs, start=1):
            output_sha256 = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
            try:
                response = _strict_json_object(raw_text)
            except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
                attempts.append(
                    {
                        "attempt": index,
                        "status": "invalid",
                        "output_sha256": output_sha256,
                        "schema_status": f"invalid_json: {exc}",
                    }
                )
                continue
            runtime_payload = validate_agent_payload(
                "webnovel_reviewer",
                response,
                project_root=root,
                run_id=run_id,
            )
            schema_status = str(runtime_payload.get("code") or "invalid_reviewer_json")
            if runtime_payload.get("accepted"):
                try:
                    result = parse_review_output(
                        int(run["chapter"]),
                        response,
                        review_mode=str(run["review_mode"]),
                        strict=True,
                    )
                except ReviewSchemaError as exc:
                    schema_status = str(exc)
                else:
                    accepted_response = response
                    accepted_result = result
                    accepted_raw_text = raw_text
            attempts.append(
                {
                    "attempt": index,
                    "status": "accepted" if accepted_result is not None else "invalid",
                    "output_sha256": output_sha256,
                    "schema_status": "ok" if accepted_result is not None else schema_status,
                }
            )
            if accepted_result is not None:
                break
        if accepted_result is not None and len(execution.raw_outputs) > len(attempts):
            # A second response is permitted only to repair a rejected first
            # serialization.  Silently accepting the first of two valid
            # reports would leave an unbound, conflicting reviewer result in
            # the same trusted rollout.
            def fail_unnecessary_retry(run_payload: dict[str, Any]) -> None:
                run_payload["attempts"] = attempts

            _update_run(root, run_id, fail_unnecessary_retry)
            _mark_run_failure(
                root,
                run_id,
                "unexpected_reviewer_retry",
                "a reviewer retry is allowed only after an invalid first response",
                stage="reviewer",
            )
            raise ReviewWorkflowError(
                "unexpected_reviewer_retry",
                "a reviewer retry is allowed only after an invalid first response",
            )
        if accepted_response is None or accepted_result is None or accepted_raw_text is None:
            def fail(run_payload: dict[str, Any]) -> None:
                run_payload["attempts"] = attempts
            _update_run(root, run_id, fail)
            _mark_run_failure(
                root,
                run_id,
                "invalid_reviewer_json",
                "reviewer output failed the strict schema after the permitted retry",
                stage="reviewer",
            )
            raise ReviewWorkflowError(
                "invalid_reviewer_json",
                "reviewer output failed the strict schema after the permitted retry",
            )

        reviewer_output_sha256 = hashlib.sha256(accepted_raw_text.encode("utf-8")).hexdigest()
        run_dir = _run_dir(root, run_id)
        raw_signature = _write_text_once(run_dir / "reviewer_raw.txt", accepted_raw_text)
        if raw_signature.get("sha256") != reviewer_output_sha256:
            raise ReviewWorkflowError(
                "reviewer_output_hash_mismatch",
                "persisted reviewer raw output does not match the trusted rollout bytes",
            )
        normalized = _normalized_artifact(
            run,
            accepted_result,
            reviewer_output_sha256=reviewer_output_sha256,
        )
        result_signature = _write_json_once(run_dir / "review_results.json", normalized)
        reviewed_at = _now_iso()

        def accept(run_payload: dict[str, Any]) -> None:
            run_payload["status"] = "awaiting_decision" if accepted_result.has_blocking else "validated"
            run_payload["attempts"] = attempts
            run_payload["reviewer_output_sha256"] = reviewer_output_sha256
            run_payload["actual_model"] = evidence.actual_model
            run_payload["actual_reasoning_effort"] = evidence.actual_reasoning_effort
            run_payload["runtime_evidence"] = runtime_claim
            run_payload["has_blocking"] = accepted_result.has_blocking
            run_payload["blocking_count"] = accepted_result.blocking_count
            run_payload["reviewed_at"] = reviewed_at
            run_payload.setdefault("artifacts", {})["raw"] = raw_signature
            run_payload["artifacts"]["result"] = result_signature
            run_payload["stages"]["reviewer"] = {"status": "completed", "at": reviewed_at}
            if accepted_result.has_blocking:
                decision = _decision_payload({**run_payload, "reviewer_output_sha256": reviewer_output_sha256})
                run_payload["decision"] = decision
                run_payload["stages"]["decision"] = {"status": "awaiting_user", "at": reviewed_at}
            else:
                run_payload["decision"] = {"status": "not_required"}
                run_payload["stages"]["decision"] = {"status": "not_required", "at": reviewed_at}

        accepted_run = _update_run(root, run_id, accept)
        if accepted_result.has_blocking:
            return {
                "schema_version": REVIEW_WORKFLOW_SCHEMA,
                "status": "awaiting_user",
                "run_id": run_id,
                "chapter": run["chapter"],
                "blocking_count": accepted_result.blocking_count,
                "decision": accepted_run["decision"],
                "actual_model": accepted_run.get("actual_model"),
                "actual_reasoning_effort": accepted_run.get("actual_reasoning_effort"),
                "body_changed": False,
                "report_written": False,
                "metrics_saved": False,
            }
        return persist_review_run(root, run_id=run_id, _lock_held=True)


def _load_result_artifact(root: Path, run: Mapping[str, Any]) -> tuple[dict[str, Any], ReviewResult]:
    signature = ((run.get("artifacts") or {}).get("result"))
    if not isinstance(signature, Mapping):
        raise ReviewWorkflowError("artifact_missing", "accepted review artifact is missing from ledger")
    path = Path(str(signature.get("path") or ""))
    expected_path = _run_dir(root, str(run.get("run_id") or "")) / "review_results.json"
    if path != expected_path or not _safe_project_path(path, root, require_file=True):
        raise ReviewWorkflowError("artifact_out_of_bounds", "accepted review artifact path is unsafe")
    try:
        raw = _stable_project_bytes(path, root, max_bytes=MAX_REVIEW_INPUT_BYTES)
    except OSError as exc:
        raise ReviewWorkflowError("artifact_invalid", "accepted review artifact cannot be read safely") from exc
    if hashlib.sha256(raw).hexdigest() != signature.get("sha256"):
        raise ReviewWorkflowError("artifact_hash_mismatch", "accepted review artifact hash changed")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReviewWorkflowError("artifact_invalid", "accepted review artifact is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != REVIEW_ARTIFACT_SCHEMA:
        raise ReviewWorkflowError("artifact_invalid", "accepted review artifact provenance is invalid")
    for field, expected in (
        ("run_id", run.get("run_id")),
        ("range_id", run.get("range_id")),
        ("review_mode", run.get("review_mode")),
        ("chapter_sha256", run["inputs"]["chapter"]["sha256"]),
        ("context_sha256", run["inputs"]["context"]["sha256"]),
        ("reviewer_output_sha256", run.get("reviewer_output_sha256")),
    ):
        if payload.get(field) != expected:
            raise ReviewWorkflowError("artifact_provenance_mismatch", f"accepted review {field} changed")
    business = {
        key: payload[key]
        for key in (
            "chapter",
            "issues",
            "issues_count",
            "blocking_count",
            "has_blocking",
            "dimension_results",
            "summary",
        )
    }
    result = parse_review_output(
        int(run["chapter"]),
        business,
        review_mode=str(run["review_mode"]),
        strict=True,
    )
    return payload, result


def _report_relative(run: Mapping[str, Any]) -> str:
    return f"审查报告/第{int(run['chapter']):04d}章-{run['run_id']}.md"


def _metrics_payload(run: Mapping[str, Any], result: ReviewResult) -> dict[str, Any]:
    provenance = {
        "schema_version": REVIEW_ARTIFACT_SCHEMA,
        "run_id": run["run_id"],
        "range_id": run.get("range_id"),
        "review_mode": run["review_mode"],
        "review_sha256": run["reviewer_output_sha256"],
        "chapter_sha256": run["inputs"]["chapter"]["sha256"],
        "context_sha256": run["inputs"]["context"]["sha256"],
        "actual_model": run["actual_model"],
        "actual_reasoning_effort": run["actual_reasoning_effort"],
    }
    metrics = result.to_metrics_dict(
        report_file=_report_relative(run),
        provenance=provenance,
    )
    metrics["timestamp"] = str(run.get("reviewed_at") or run.get("updated_at") or _now_iso())
    return metrics


def _signed_artifact_bytes(
    root: Path,
    signature: object,
    *,
    expected_path: Path,
    label: str,
) -> bytes:
    if not isinstance(signature, Mapping):
        raise ReviewWorkflowError("artifact_missing", f"{label} signature is missing")
    path = Path(str(signature.get("path") or ""))
    if path != expected_path or not _safe_project_path(path, root, require_file=True):
        raise ReviewWorkflowError("artifact_out_of_bounds", f"{label} path is unsafe")
    try:
        raw = _stable_project_bytes(path, root, max_bytes=MAX_REVIEW_INPUT_BYTES)
    except OSError as exc:
        raise ReviewWorkflowError("artifact_missing", f"{label} cannot be read safely") from exc
    if hashlib.sha256(raw).hexdigest() != signature.get("sha256"):
        raise ReviewWorkflowError("artifact_hash_mismatch", f"{label} hash changed")
    return raw


def _validate_core_review_artifacts(
    root: Path,
    run: Mapping[str, Any],
) -> tuple[dict[str, Any], ReviewResult]:
    artifacts = run.get("artifacts") if isinstance(run.get("artifacts"), Mapping) else {}
    run_dir = _run_dir(root, str(run.get("run_id") or ""))
    raw = _signed_artifact_bytes(
        root,
        artifacts.get("raw"),
        expected_path=run_dir / "reviewer_raw.txt",
        label="reviewer raw artifact",
    )
    if hashlib.sha256(raw).hexdigest() != run.get("reviewer_output_sha256"):
        raise ReviewWorkflowError(
            "reviewer_output_hash_mismatch",
            "reviewer raw artifact no longer matches the accepted output hash",
        )
    return _load_result_artifact(root, run)


def _validate_persistence_receipt(
    root: Path,
    run: Mapping[str, Any],
) -> tuple[str | None, str | None]:
    """Return ``(stage, problem)`` for a stale persisted receipt, else nulls."""

    input_error = _run_input_error(root, run)
    if input_error:
        return "input", input_error
    try:
        normalized, result = _validate_core_review_artifacts(root, run)
    except ReviewWorkflowError as exc:
        return "core", f"{exc.code}: {exc}"
    metrics = _metrics_payload(run, result)
    artifacts = run.get("artifacts") if isinstance(run.get("artifacts"), Mapping) else {}
    run_dir = _run_dir(root, str(run.get("run_id") or ""))
    try:
        metrics_raw = _signed_artifact_bytes(
            root,
            artifacts.get("metrics"),
            expected_path=run_dir / "review_metrics.json",
            label="review metrics artifact",
        )
        stored_metrics = _strict_json_object(metrics_raw.decode("utf-8"))
        if stored_metrics != metrics:
            raise ReviewWorkflowError(
                "metrics_artifact_mismatch",
                "review metrics artifact differs from the accepted review",
            )
        try:
            from review_pipeline import render_review_report
        except ImportError:
            from scripts.review_pipeline import render_review_report

        report_raw = _signed_artifact_bytes(
            root,
            artifacts.get("report"),
            expected_path=root / _report_relative(run),
            label="review report artifact",
        )
        if report_raw.startswith(b"\xef\xbb\xbf"):
            raise ReviewWorkflowError("artifact_invalid", "review report contains a UTF-8 BOM")
        expected_report = render_review_report(
            {"chapter": run["chapter"], "review_result": normalized, "metrics": metrics}
        )
        if report_raw.decode("utf-8") != expected_report:
            raise ReviewWorkflowError(
                "report_artifact_mismatch",
                "review report differs from the accepted review",
            )
    except (UnicodeError, ValueError, ReviewWorkflowError) as exc:
        code = exc.code if isinstance(exc, ReviewWorkflowError) else "artifact_invalid"
        return "artifacts", f"{code}: {exc}"
    if not _db_record_matches(root, metrics):
        return "metrics_db", "review_metrics readback differs from persisted artifacts"
    return None, None


def _validate_sqlite_bundle(
    root: Path,
    *,
    allow_wal_recovery: bool = False,
) -> Path:
    """Reject database or sidecar paths that can escape or form a torn WAL bundle."""

    db_path = root / ".webnovel" / "index.db"
    paths = {
        "database": db_path,
        "wal": Path(f"{db_path}-wal"),
        "shm": Path(f"{db_path}-shm"),
    }
    present: dict[str, bool] = {}
    for name, path in paths.items():
        exists = path.exists() or path.is_symlink()
        present[name] = exists
        if exists and (
            _is_reparse(path)
            or not path.is_file()
            or not _safe_project_path(path, root, require_file=True)
        ):
            raise ReviewWorkflowError(
                "unsafe_sqlite_path",
                f"review metrics {name} path is unsafe: {path}",
            )
        if not exists and not _safe_project_path(path, root):
            raise ReviewWorkflowError(
                "unsafe_sqlite_path",
                f"review metrics {name} path escapes project_root",
            )
    if (present["wal"] or present["shm"]) and not present["database"]:
        raise ReviewWorkflowError(
            "sqlite_bundle_inconsistent",
            "review metrics WAL/SHM sidecars exist without index.db",
        )
    if present["wal"] and not present["shm"] and allow_wal_recovery:
        return db_path
    if present["wal"] != present["shm"]:
        raise ReviewWorkflowError(
            "sqlite_wal_recovery_required" if present["wal"] else "sqlite_bundle_inconsistent",
            "review metrics WAL requires controlled SQLite recovery"
            if present["wal"]
            else "review metrics SHM sidecar exists without WAL",
        )
    return db_path


def _wal_checksum(
    data: bytes,
    state: tuple[int, int],
    *,
    byteorder: str,
) -> tuple[int, int]:
    if len(data) % 8:
        raise ReviewWorkflowError(
            "sqlite_wal_recovery_failed",
            "review metrics WAL checksum input is misaligned",
        )
    first, second = state
    for offset in range(0, len(data), 8):
        word0 = int.from_bytes(data[offset : offset + 4], byteorder)
        word1 = int.from_bytes(data[offset + 4 : offset + 8], byteorder)
        first = (first + word0 + second) & 0xFFFFFFFF
        second = (second + word1 + first) & 0xFFFFFFFF
    return first, second


def _validate_wal_for_recovery(wal_path: Path) -> None:
    """Validate a complete SQLite WAL chain before allowing SQLite to replay it."""

    try:
        with wal_path.open("rb") as handle:
            before = os.fstat(handle.fileno())
            if not 32 <= before.st_size <= MAX_SQLITE_WAL_RECOVERY_BYTES:
                raise ReviewWorkflowError(
                    "sqlite_wal_recovery_failed",
                    "review metrics WAL size is outside the controlled recovery bound",
                )
            header = handle.read(32)
            if len(header) != 32:
                raise ReviewWorkflowError(
                    "sqlite_wal_recovery_failed",
                    "review metrics WAL header is truncated",
                )
            magic = int.from_bytes(header[0:4], "big")
            if magic not in {0x377F0682, 0x377F0683}:
                raise ReviewWorkflowError(
                    "sqlite_wal_recovery_failed",
                    "review metrics WAL magic is invalid",
                )
            page_size_field = int.from_bytes(header[8:12], "big")
            page_size = 65536 if page_size_field == 1 else page_size_field
            if (
                page_size < 512
                or page_size > 65536
                or page_size & (page_size - 1)
            ):
                raise ReviewWorkflowError(
                    "sqlite_wal_recovery_failed",
                    "review metrics WAL page size is invalid",
                )
            frame_size = 24 + page_size
            if (before.st_size - 32) % frame_size:
                raise ReviewWorkflowError(
                    "sqlite_wal_recovery_failed",
                    "review metrics WAL has a truncated frame",
                )
            checksum_order = "little" if magic == 0x377F0682 else "big"
            checksum = _wal_checksum(header[:24], (0, 0), byteorder=checksum_order)
            stored = (
                int.from_bytes(header[24:28], "big"),
                int.from_bytes(header[28:32], "big"),
            )
            if checksum != stored:
                raise ReviewWorkflowError(
                    "sqlite_wal_recovery_failed",
                    "review metrics WAL header checksum is invalid",
                )
            salt = header[16:24]
            frame_count = (before.st_size - 32) // frame_size
            for _ in range(frame_count):
                frame = handle.read(frame_size)
                if len(frame) != frame_size or frame[8:16] != salt:
                    raise ReviewWorkflowError(
                        "sqlite_wal_recovery_failed",
                        "review metrics WAL frame is truncated or has mismatched salt",
                    )
                if int.from_bytes(frame[0:4], "big") <= 0:
                    raise ReviewWorkflowError(
                        "sqlite_wal_recovery_failed",
                        "review metrics WAL frame page number is invalid",
                    )
                checksum = _wal_checksum(
                    frame[:8] + frame[24:],
                    checksum,
                    byteorder=checksum_order,
                )
                stored = (
                    int.from_bytes(frame[16:20], "big"),
                    int.from_bytes(frame[20:24], "big"),
                )
                if checksum != stored:
                    raise ReviewWorkflowError(
                        "sqlite_wal_recovery_failed",
                        "review metrics WAL frame checksum is invalid",
                    )
            after = os.fstat(handle.fileno())
    except ReviewWorkflowError:
        raise
    except OSError as exc:
        raise ReviewWorkflowError(
            "sqlite_wal_recovery_failed",
            f"review metrics WAL cannot be read safely: {exc}",
        ) from exc
    try:
        current = wal_path.stat(follow_symlinks=False)
    except OSError as exc:
        raise ReviewWorkflowError(
            "sqlite_wal_recovery_failed",
            f"review metrics WAL identity cannot be verified: {exc}",
        ) from exc
    if (
        _is_reparse(wal_path)
        or not stat.S_ISREG(current.st_mode)
        or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        or (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    ):
        raise ReviewWorkflowError(
            "sqlite_wal_recovery_failed",
            "review metrics WAL changed while it was validated",
        )


def _recover_sqlite_wal_if_needed(root: Path) -> None:
    """Let SQLite recover a safe DB+WAL bundle and recreate transient SHM."""

    db_path = _validate_sqlite_bundle(root, allow_wal_recovery=True)
    wal_path = Path(f"{db_path}-wal")
    shm_path = Path(f"{db_path}-shm")
    if not wal_path.is_file() or shm_path.is_file():
        _validate_sqlite_bundle(root)
        return
    _validate_wal_for_recovery(wal_path)
    try:
        # A writable SQLite connection is intentional here: SQLite owns WAL
        # recovery and the transient -shm file.  No caller-provided SQL runs.
        with sqlite3.connect(str(db_path), timeout=10) as conn:
            conn.execute("PRAGMA schema_version").fetchone()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            integrity = conn.execute("PRAGMA integrity_check(1)").fetchone()
            if not integrity or str(integrity[0]).casefold() != "ok":
                raise sqlite3.DatabaseError("integrity_check failed after WAL recovery")
    except sqlite3.Error as exc:
        raise ReviewWorkflowError(
            "sqlite_wal_recovery_failed",
            f"review metrics WAL recovery failed: {exc}",
        ) from exc
    _validate_sqlite_bundle(root)


def _db_record_matches(root: Path, metrics: Mapping[str, Any]) -> bool:
    try:
        db_path = _validate_sqlite_bundle(root)
    except ReviewWorkflowError as exc:
        if exc.code == "sqlite_wal_recovery_required":
            return False
        raise
    if not db_path.is_file():
        return False
    try:
        with sqlite3.connect(read_only_sqlite_uri(db_path), uri=True) as conn:
            row = conn.execute(
                """
                SELECT overall_score, dimension_scores, severity_counts,
                       critical_issues, report_file, notes
                FROM review_metrics
                WHERE start_chapter = ? AND end_chapter = ?
                """,
                (metrics["start_chapter"], metrics["end_chapter"]),
            ).fetchone()
    except sqlite3.Error:
        return False
    if row is None:
        return False
    try:
        return (
            abs(float(row[0]) - float(metrics["overall_score"])) < 1e-9
            and json.loads(row[1] or "{}") == metrics["dimension_scores"]
            and json.loads(row[2] or "{}") == metrics["severity_counts"]
            and json.loads(row[3] or "[]") == metrics["critical_issues"]
            and str(row[4] or "") == str(metrics["report_file"])
            and str(row[5] or "") == str(metrics["notes"])
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        return False


def _save_metrics(root: Path, metrics: Mapping[str, Any]) -> None:
    try:
        from review_pipeline import _build_review_metrics_record
    except ImportError:
        from scripts.review_pipeline import _build_review_metrics_record

    _recover_sqlite_wal_if_needed(root)
    _validate_sqlite_bundle(root)
    manager = IndexManager(DataModulesConfig.from_project_root(root))
    manager.save_review_metrics(_build_review_metrics_record(dict(metrics)))
    _validate_sqlite_bundle(root)


def persist_review_run(
    project_root: str | Path,
    *,
    run_id: str,
    _lock_held: bool = False,
) -> dict[str, Any]:
    """Persist only missing stages; never invoke or re-run the reviewer."""

    root = _project_root(project_root)
    if not _lock_held:
        with _run_lock(root, run_id):
            return persist_review_run(root, run_id=run_id, _lock_held=True)
    run = get_review_run(root, run_id)
    if run is None:
        raise ReviewWorkflowError("run_not_found", f"review run does not exist: {run_id}")
    if isinstance(run.get("runtime_evidence"), Mapping):
        _reverify_runtime_receipt(root, run)
    if run.get("status") == "persisted":
        if run.get("has_blocking"):
            if (run.get("decision") or {}).get("selected") != "report_only":
                raise ReviewWorkflowError(
                    "blocking_decision_required",
                    "persisted blocking review lacks an explicit report_only decision",
                )
            if _verify_run_decision_receipt(run) != "report_only":
                raise ReviewWorkflowError(
                    "invalid_decision_receipt",
                    "trusted decision receipt did not authorize report_only",
                )
        receipt_stage, receipt_problem = _validate_persistence_receipt(root, run)
        if receipt_stage is None:
            return {
                "schema_version": REVIEW_WORKFLOW_SCHEMA,
                "status": "persisted",
                "run_id": run_id,
                "chapter": run["chapter"],
                "artifacts": run.get("artifacts") or {},
                "actual_model": run.get("actual_model"),
                "actual_reasoning_effort": run.get("actual_reasoning_effort"),
                "reviewer_rerun": False,
                "receipt_verified": True,
            }
        if receipt_stage == "input":
            def stale(run_payload: dict[str, Any]) -> None:
                run_payload["status"] = "stale"
                run_payload.setdefault("problems", []).append(
                    {
                        "code": "input_hash_mismatch",
                        "message": str(receipt_problem),
                        "stage": "resume",
                        "at": _now_iso(),
                    }
                )

            _update_run(root, run_id, stale)
            return {
                "schema_version": REVIEW_WORKFLOW_SCHEMA,
                "status": "stale",
                "code": "input_hash_mismatch",
                "message": receipt_problem,
                "run_id": run_id,
                "chapter": run["chapter"],
                "reviewer_rerun": False,
            }
        if receipt_stage == "core":
            _mark_run_failure(
                root,
                run_id,
                "accepted_artifact_invalid",
                str(receipt_problem),
                stage="reviewer",
            )
            return {
                "schema_version": REVIEW_WORKFLOW_SCHEMA,
                "status": "failed_validation",
                "code": "accepted_artifact_invalid",
                "message": receipt_problem,
                "run_id": run_id,
                "chapter": run["chapter"],
                "reviewer_rerun": False,
            }
        _mark_run_failure(
            root,
            run_id,
            "persisted_receipt_invalid",
            str(receipt_problem),
            stage=str(receipt_stage),
        )
        run = get_review_run(root, run_id)
        if run is None:  # pragma: no cover - ledger mutation is lock-protected
            raise ReviewWorkflowError("run_not_found", f"review run does not exist: {run_id}")
    if run.get("status") not in {"validated", "failed_persistence"}:
        raise ReviewWorkflowError("persistence_not_authorized", "review run is not authorized for persistence")
    if run.get("has_blocking"):
        decision = run.get("decision")
        if not isinstance(decision, Mapping) or decision.get("selected") != "report_only":
            raise ReviewWorkflowError(
                "blocking_decision_required",
                "blocking review requires an explicit report_only decision",
            )
        if _verify_run_decision_receipt(run) != "report_only":
            raise ReviewWorkflowError(
                "invalid_decision_receipt",
                "trusted decision receipt did not authorize report_only",
            )
    input_error = _run_input_error(root, run)
    if input_error:
        def stale_before_persistence(run_payload: dict[str, Any]) -> None:
            run_payload["status"] = "stale"
            run_payload.setdefault("problems", []).append(
                {
                    "code": "input_hash_mismatch",
                    "message": input_error,
                    "stage": "artifacts",
                    "at": _now_iso(),
                }
            )

        _update_run(root, run_id, stale_before_persistence)
        return {
            "schema_version": REVIEW_WORKFLOW_SCHEMA,
            "status": "stale",
            "code": "input_hash_mismatch",
            "message": input_error,
            "run_id": run_id,
            "chapter": run["chapter"],
            "reviewer_rerun": False,
        }

    try:
        normalized, result = _validate_core_review_artifacts(root, run)
        metrics = _metrics_payload(run, result)
        try:
            from review_pipeline import render_review_report
        except ImportError:
            from scripts.review_pipeline import render_review_report

        report_payload = {
            "chapter": run["chapter"],
            "review_result": normalized,
            "metrics": metrics,
        }
        run_dir = _run_dir(root, run_id)
        metrics_signature = _write_json_once(run_dir / "review_metrics.json", metrics)
        report_path = root / _report_relative(run)
        if not _safe_project_path(report_path, root):
            raise ReviewWorkflowError("report_out_of_bounds", "review report path escapes project_root")
        report_signature = _write_text_once(report_path, render_review_report(report_payload))
    except Exception as exc:
        error = exc if isinstance(exc, ReviewWorkflowError) else ReviewWorkflowError("artifact_write_failed", str(exc))
        _mark_run_failure(root, run_id, error.code, str(error), stage="artifacts")
        return {
            "schema_version": REVIEW_WORKFLOW_SCHEMA,
            "status": "recoverable",
            "run_id": run_id,
            "resume_from": "artifacts",
            "code": error.code,
            "message": str(error),
            "reviewer_rerun": False,
        }

    def artifacts_done(run_payload: dict[str, Any]) -> None:
        run_payload.setdefault("artifacts", {})["metrics"] = metrics_signature
        run_payload["artifacts"]["report"] = report_signature
        run_payload["stages"]["artifacts"] = {"status": "completed", "at": _now_iso()}
        run_payload["status"] = "validated"

    _update_run(root, run_id, artifacts_done)
    try:
        if not _db_record_matches(root, metrics):
            _save_metrics(root, metrics)
        if not _db_record_matches(root, metrics):
            raise ReviewWorkflowError("metrics_readback_mismatch", "review_metrics readback differs from artifacts")
    except Exception as exc:
        error = exc if isinstance(exc, ReviewWorkflowError) else ReviewWorkflowError("metrics_db_failed", str(exc))
        _mark_run_failure(root, run_id, error.code, str(error), stage="metrics_db")
        return {
            "schema_version": REVIEW_WORKFLOW_SCHEMA,
            "status": "recoverable",
            "run_id": run_id,
            "resume_from": "metrics_db",
            "code": error.code,
            "message": str(error),
            "reviewer_rerun": False,
            "artifacts": {"metrics": metrics_signature, "report": report_signature},
        }

    def complete(run_payload: dict[str, Any]) -> None:
        run_payload["stages"]["metrics_db"] = {"status": "completed", "at": _now_iso()}
        run_payload["status"] = "persisted"

    completed = _update_run(root, run_id, complete)
    return {
        "schema_version": REVIEW_WORKFLOW_SCHEMA,
        "status": "persisted",
        "run_id": run_id,
        "range_id": completed.get("range_id"),
        "chapter": completed["chapter"],
        "blocking_count": completed.get("blocking_count", 0),
        "artifacts": completed.get("artifacts") or {},
        "actual_model": completed.get("actual_model"),
        "actual_reasoning_effort": completed.get("actual_reasoning_effort"),
        "metrics_saved": True,
        "body_changed": False,
        "reviewer_rerun": False,
    }


def decide_review(
    project_root: str | Path,
    *,
    run_id: str,
    request_file: str | Path,
) -> dict[str, Any]:
    root = _project_root(project_root)
    request = load_review_decision_request(request_file, project_root=root)
    if request.get("kind") != "run" or request.get("run_id") != run_id:
        raise ReviewWorkflowError(
            "cross_run_decision",
            "decision request does not name this review run",
        )
    with _run_lock(root, run_id):
        run = get_review_run(root, run_id)
        if run is None:
            raise ReviewWorkflowError("run_not_found", f"review run does not exist: {run_id}")
        _current_codex_thread_id(run.get("parent_thread_id"))
        if isinstance(run.get("runtime_evidence"), Mapping):
            _reverify_runtime_receipt(root, run)
        decision = run.get("decision")
        if run.get("status") != "awaiting_decision" or not isinstance(decision, Mapping):
            raise ReviewWorkflowError("decision_not_pending", "review run has no pending blocking decision")
        if request.get("request_id") != decision.get("request_id") or decision != _decision_payload(run):
            raise ReviewWorkflowError("stale_decision", "decision request does not match this run and artifact")
        choice, runtime_receipt = _verified_user_choice(
            request["runtime"],
            decision,
            expected_parent_thread_id=_decision_parent_thread_id(run),
            expected_parent_model=run.get("parent_model"),
            expected_parent_effort=run.get("parent_reasoning_effort"),
            question_id="review_action",
        )
        if choice not in BLOCKING_CHOICES:
            raise ReviewWorkflowError(
                "invalid_decision_answer",
                "verified user answer selected an unsupported review choice",
            )

        selected_at = _now_iso()
        if choice == "targeted_fix":
            commit_path = root / ".story-system" / "commits" / f"chapter_{int(run['chapter']):03d}.commit.json"
            accepted = False
            if commit_path.is_file() and _safe_project_path(commit_path, root, require_file=True):
                try:
                    payload = json.loads(commit_path.read_text(encoding="utf-8"))
                    accepted = ((payload.get("meta") or {}).get("status") == "accepted")
                except (OSError, UnicodeError, json.JSONDecodeError):
                    accepted = True

            def targeted(run_payload: dict[str, Any]) -> None:
                run_payload["decision"] = {
                    **decision,
                    "status": "selected",
                    "selected": choice,
                    "selected_at": selected_at,
                    "runtime_receipt": runtime_receipt,
                    "write_allowed": False,
                }
                run_payload["status"] = "targeted_fix_blocked" if accepted else "targeted_fix_pending"
                run_payload["stages"]["decision"] = {"status": "selected", "choice": choice, "at": selected_at}

            _update_run(root, run_id, targeted)
            return {
                "schema_version": REVIEW_WORKFLOW_SCHEMA,
                "status": "blocked" if accepted else "targeted_fix_pending",
                "code": "accepted_chapter_transaction_required" if accepted else "writer_agent_required",
                "run_id": run_id,
                "chapter": run["chapter"],
                "body_changed": False,
                "next_action": "M6 transactional writer workflow must perform and validate the targeted fix",
            }

        def select(run_payload: dict[str, Any]) -> None:
            run_payload["decision"] = {
                **decision,
                "status": "selected",
                "selected": choice,
                "selected_at": selected_at,
                "runtime_receipt": runtime_receipt,
                "write_allowed": choice == "report_only",
            }
            run_payload["stages"]["decision"] = {"status": "selected", "choice": choice, "at": selected_at}
            run_payload["status"] = "validated" if choice == "report_only" else "abandoned"

        _update_run(root, run_id, select)
        if choice == "abandon":
            return {
                "schema_version": REVIEW_WORKFLOW_SCHEMA,
                "status": "abandoned",
                "run_id": run_id,
                "chapter": run["chapter"],
                "body_changed": False,
                "report_written": False,
                "metrics_saved": False,
            }
        return persist_review_run(root, run_id=run_id, _lock_held=True)


def resume_review(
    project_root: str | Path,
    *,
    run_id: str,
    _lock_held: bool = False,
) -> dict[str, Any]:
    root = _project_root(project_root)
    if not _lock_held:
        with _run_lock(root, run_id):
            return resume_review(root, run_id=run_id, _lock_held=True)
    run = get_review_run(root, run_id)
    if run is None:
        raise ReviewWorkflowError("run_not_found", f"review run does not exist: {run_id}")
    status = str(run.get("status") or "")
    if status != "prepared" and isinstance(run.get("runtime_evidence"), Mapping):
        _reverify_runtime_receipt(root, run)
    if status in {"validated", "failed_persistence", "persisted"}:
        return persist_review_run(root, run_id=run_id, _lock_held=True)
    if status == "awaiting_decision":
        return {
            "schema_version": REVIEW_WORKFLOW_SCHEMA,
            "status": "awaiting_user",
            "run_id": run_id,
            "chapter": run["chapter"],
            "decision": run.get("decision"),
            "actual_model": run.get("actual_model"),
            "actual_reasoning_effort": run.get("actual_reasoning_effort"),
            "reviewer_rerun": False,
        }
    if status == "prepared":
        return {
            "schema_version": REVIEW_WORKFLOW_SCHEMA,
            "status": "prepared",
            "run_id": run_id,
            "range_id": run.get("range_id"),
            "chapter": run["chapter"],
            "review_mode": run.get("review_mode"),
            "request_file": str(_run_dir(root, run_id) / "request.json"),
            "request_sha256": run.get("request_sha256"),
            "binding_marker": _reviewer_agent_binding(run)[0],
            "agent_task_name": _reviewer_agent_binding(run)[1],
            "agent_path": _reviewer_agent_binding(run)[2],
            "agent_name": run.get("agent_name"),
            "requested_model": run.get("requested_model"),
            "requested_reasoning_effort": run.get("requested_reasoning_effort"),
            "next_action": "invoke the managed reviewer, then use review accept",
            "reviewer_rerun": False,
        }
    if run.get("has_blocking") and status in {
        "abandoned",
        "targeted_fix_pending",
        "targeted_fix_blocked",
        "stale",
    }:
        _verify_run_decision_receipt(run)
    return {
        "schema_version": REVIEW_WORKFLOW_SCHEMA,
        "status": status,
        "run_id": run_id,
        "chapter": run["chapter"],
        "artifacts": run.get("artifacts") or {},
        "actual_model": run.get("actual_model"),
        "actual_reasoning_effort": run.get("actual_reasoning_effort"),
        "reviewer_rerun": False,
    }


def prepare_review_range(
    project_root: str | Path,
    *,
    start: int,
    end: int,
    review_mode: str,
    workspace_root: str | Path,
    parent_model: str,
    parent_reasoning_effort: str | None = None,
) -> dict[str, Any]:
    root = _project_root(project_root)
    parent_thread_id = _current_codex_thread_id()
    if type(start) is not int or type(end) is not int or start <= 0 or end < start:
        raise ReviewWorkflowError("invalid_range", "range must be positive and ascending")
    chapters = list(range(start, end + 1))
    if len(chapters) > 5:
        raise ReviewWorkflowError("range_too_large", "a review range may contain at most five chapters")
    if review_mode not in REVIEW_MODES:
        raise ReviewWorkflowError("invalid_review_mode", "review mode must be full or fast")
    if not isinstance(parent_model, str) or not parent_model.strip():
        raise ReviewWorkflowError("invalid_parent_model", "parent model must be explicit")
    workspace = _workspace_root(workspace_root)
    range_id = _new_range_id(start, end)
    created_at = _now_iso()
    entry = {
        "schema_version": REVIEW_RANGE_SCHEMA,
        "range_id": range_id,
        "status": "preparing",
        "project_root_hash": _path_hash(root),
        "chapters": chapters,
        "run_ids": [None for _ in chapters],
        "superseded_run_ids": [],
        "current_index": 0,
        "review_mode": review_mode,
        "workspace_root": str(workspace),
        "parent_thread_id": parent_thread_id,
        "parent_model": parent_model.strip(),
        "parent_reasoning_effort": parent_reasoning_effort,
        "decision": None,
        "decision_history": {},
        "overrides": {},
        "skipped": [],
        "created_at": created_at,
        "updated_at": created_at,
    }
    with locked_ledger(root, strict=True) as ledger:
        ledger["review"]["ranges"][range_id] = entry
    try:
        prepared = prepare_review(
            root,
            chapter=start,
            review_mode=review_mode,
            workspace_root=workspace_root,
            parent_model=parent_model,
            parent_reasoning_effort=parent_reasoning_effort,
            range_id=range_id,
            _range_index=0,
        )
    except Exception as exc:
        _update_range(root, range_id, lambda value: value.update({"status": "failed", "problem": str(exc)}))
        raise

    return {
        "schema_version": REVIEW_RANGE_SCHEMA,
        "status": "in_progress",
        "range_id": range_id,
        "chapters": chapters,
        "current_chapter": start,
        "current_run": prepared,
        "serial": True,
    }


def _range_decision_payload(entry: Mapping[str, Any], run: Mapping[str, Any]) -> dict[str, Any]:
    parent_thread_id = _decision_parent_thread_id(run)
    scope = {
        "workflow": "review_range",
        "range_id": entry.get("range_id"),
        "current_index": entry.get("current_index"),
        "run_id": run.get("run_id"),
        "run_status": run.get("status"),
        "reviewer_output_sha256": run.get("reviewer_output_sha256"),
        "parent_thread_id": parent_thread_id,
    }
    scope_hash = _sha_payload(scope)
    choice_request = build_choice_request(
        [
            {
                "id": "range_action",
                "prompt": "当前章节存在 blocker 或失败。是否停止范围审查？",
                "options": [
                    {
                        "id": "stop",
                        "label": "停止",
                        "description": "停在当前章节，不继续范围中的后续章节。",
                        "recommended": True,
                    },
                    {
                        "id": "continue",
                        "label": "继续下一章",
                        "description": "明确跳过当前 blocker 或失败并继续串行审查。",
                        "recommended": False,
                    },
                ],
            }
        ]
    )
    marker_payload = {
        "kind": "range",
        "project_root_hash": entry.get("project_root_hash"),
        "range_id": entry.get("range_id"),
        "current_index": entry.get("current_index"),
        "run_id": run.get("run_id"),
        "reviewer_output_sha256": run.get("reviewer_output_sha256"),
        "parent_thread_id": parent_thread_id,
        "scope_hash": scope_hash,
        "request_id": choice_request["request_id"],
    }
    return {
        "schema_version": REVIEW_DECISION_SCHEMA,
        "request_id": choice_request["request_id"],
        "scope_hash": scope_hash,
        "status": "awaiting_user",
        "question": "当前章节存在 blocker 或失败。是否停止范围审查？",
        "options": [
            {"id": "stop", "label": "停止", "recommended": True},
            {"id": "continue", "label": "继续下一章", "recommended": False},
        ],
        "choice_request": choice_request,
        "binding_marker": f"{REVIEW_DECISION_MARKER_SCHEMA} {_canonical_json(marker_payload)}",
        "write_allowed": False,
    }


def _attach_next_range_run(root: Path, entry: Mapping[str, Any], index: int) -> dict[str, Any]:
    _current_codex_thread_id(entry.get("parent_thread_id"))
    recovered = _recover_orphaned_range_run(root, entry, index)
    if recovered is not None:
        return {
            **resume_review(root, run_id=recovered),
            "orphan_recovered": True,
        }
    chapter = int(entry["chapters"][index])
    prepared = prepare_review(
        root,
        chapter=chapter,
        review_mode=str(entry["review_mode"]),
        workspace_root=str(entry["workspace_root"]),
        parent_model=str(entry["parent_model"]),
        parent_reasoning_effort=entry.get("parent_reasoning_effort"),
        range_id=str(entry["range_id"]),
        _range_index=index,
    )
    return prepared


def _recover_orphaned_range_run(
    root: Path,
    entry: Mapping[str, Any],
    index: int,
) -> str | None:
    """Atomically attach one legacy active run left by the old two-step flow."""

    range_id = str(entry.get("range_id") or "")
    chapters = entry.get("chapters") or []
    if not valid_review_range_id(range_id) or not 0 <= index < len(chapters):
        raise ReviewWorkflowError("range_state_corrupt", "review range recovery scope is invalid")
    chapter = int(chapters[index])
    with locked_ledger(root, strict=True) as ledger:
        current = ledger["review"]["ranges"].get(range_id)
        if not isinstance(current, dict):
            raise ReviewWorkflowError("range_not_found", f"review range does not exist: {range_id}")
        run_ids = current.get("run_ids") or []
        if not 0 <= index < len(run_ids):
            raise ReviewWorkflowError("range_state_corrupt", "review range run slots are invalid")
        attached = run_ids[index]
        if attached is not None:
            return str(attached)
        candidates = [
            str(run_id)
            for run_id, run in ledger["review"]["runs"].items()
            if isinstance(run, Mapping)
            and run.get("range_id") == range_id
            and run.get("chapter") == chapter
            and run.get("status") in ACTIVE_RUN_STATUSES
        ]
        if len(candidates) > 1:
            raise ReviewWorkflowError(
                "range_state_corrupt",
                "multiple orphaned active review runs match the same range chapter",
            )
        if not candidates:
            return None
        recovered = candidates[0]
        if recovered in run_ids:
            raise ReviewWorkflowError(
                "range_state_corrupt",
                "orphaned review run is already attached to another range slot",
            )
        run = ledger["review"]["runs"][recovered]
        if (
            run.get("project_root_hash") != current.get("project_root_hash")
            or run.get("review_mode") != current.get("review_mode")
            or run.get("workspace_root") != current.get("workspace_root")
            or run.get("parent_thread_id") != current.get("parent_thread_id")
            or run.get("parent_model") != current.get("parent_model")
            or run.get("parent_reasoning_effort") != current.get("parent_reasoning_effort")
        ):
            raise ReviewWorkflowError(
                "range_state_corrupt",
                "orphaned review run provenance does not match its range",
            )
        run_ids[index] = recovered
        current["run_ids"] = run_ids
        current["current_index"] = index
        current["status"] = "in_progress"
        current["decision"] = None
        current["updated_at"] = _now_iso()
        return recovered


def _verify_range_decision_receipts(
    root: Path,
    entry: Mapping[str, Any],
) -> dict[str, Any] | None:
    history = entry.get("decision_history") or {}
    if not isinstance(history, Mapping):
        return {
            "status": "recoverable",
            "code": "range_decision_receipt_invalid",
            "message": "range decision history is malformed",
        }
    for raw_index, decision in history.items():
        if not isinstance(decision, Mapping):
            return {
                "status": "recoverable",
                "code": "range_decision_receipt_invalid",
                "message": f"range decision receipt {raw_index} is malformed",
            }
        try:
            index = int(raw_index)
            run_ids = entry.get("run_ids") or []
            if not 0 <= index < len(run_ids) or not run_ids[index]:
                raise ReviewWorkflowError(
                    "range_decision_receipt_invalid",
                    "range decision history does not name an attached run",
                )
            run = get_review_run(root, str(run_ids[index]))
            if run is None:
                raise ReviewWorkflowError(
                    "range_decision_receipt_invalid",
                    "range decision history names a missing run",
                )
            historical_entry = dict(entry)
            historical_entry["current_index"] = index
            _validate_selected_decision_scope(
                decision,
                _range_decision_payload(historical_entry, run),
            )
            _reverify_selected_decision(
                decision,
                expected_parent_thread_id=_decision_parent_thread_id(run),
                expected_parent_model=entry.get("parent_model"),
                expected_parent_effort=entry.get("parent_reasoning_effort"),
                question_id="range_action",
            )
        except ReviewWorkflowError as exc:
            return {
                "status": "recoverable",
                "code": "range_decision_receipt_invalid",
                "message": f"range decision receipt {raw_index} is invalid: {exc}",
            }
    return None


def _verify_terminal_range_receipts(root: Path, entry: Mapping[str, Any]) -> dict[str, Any] | None:
    """Verify every persisted receipt before returning a terminal range state."""

    skipped_ids = {
        str(item.get("run_id"))
        for item in (entry.get("skipped") or [])
        if isinstance(item, Mapping) and item.get("run_id")
    }
    run_ids = entry.get("run_ids") or []
    for index, run_id in enumerate(run_ids):
        if run_id is None:
            if entry.get("status") == "completed":
                return {
                    "status": "recoverable",
                    "code": "range_receipt_missing",
                    "message": f"completed range has no run for chapter {entry['chapters'][index]}",
                }
            continue
        run = get_review_run(root, str(run_id))
        if run is None:
            return {
                "status": "recoverable",
                "code": "range_run_missing",
                "message": f"range references missing run {run_id}",
            }
        if run.get("status") == "persisted":
            receipt = resume_review(root, run_id=str(run_id))
            if receipt.get("status") != "persisted":
                return {
                    "status": "recoverable",
                    "code": "range_receipt_invalid",
                    "message": f"persisted receipt for run {run_id} is no longer valid",
                    "current_run": receipt,
                }
        elif entry.get("status") in {"completed", "partial"} and str(run_id) not in skipped_ids:
            return {
                "status": "recoverable",
                "code": "range_receipt_incomplete",
                "message": f"terminal range contains unfinished run {run_id}",
            }
    return None


def resume_review_range(project_root: str | Path, *, range_id: str) -> dict[str, Any]:
    root = _project_root(project_root)
    with _range_lock(root, range_id):
        entry = get_review_range(root, range_id)
        if entry is None:
            raise ReviewWorkflowError("range_not_found", f"review range does not exist: {range_id}")
        _current_codex_thread_id(entry.get("parent_thread_id"))
        decision_problem = _verify_range_decision_receipts(root, entry)
        if decision_problem is not None:
            return {
                "schema_version": REVIEW_RANGE_SCHEMA,
                "range_id": range_id,
                **decision_problem,
            }
        if entry.get("status") in {"completed", "partial", "stopped"}:
            receipt_problem = _verify_terminal_range_receipts(root, entry)
            if receipt_problem is not None:
                return {
                    "schema_version": REVIEW_RANGE_SCHEMA,
                    "range_id": range_id,
                    **receipt_problem,
                }
            return {
                "schema_version": REVIEW_RANGE_SCHEMA,
                "status": entry["status"],
                "range_id": range_id,
                "chapters": entry["chapters"],
                "run_ids": entry["run_ids"],
                "skipped": entry.get("skipped") or [],
            }
        index = int(entry.get("current_index") or 0)
        run_ids = entry.get("run_ids") or []
        run_id = run_ids[index] if 0 <= index < len(run_ids) else None
        if not run_id:
            prepared = _attach_next_range_run(root, entry, index)
            return {
                "schema_version": REVIEW_RANGE_SCHEMA,
                "status": "in_progress",
                "range_id": range_id,
                "current_chapter": entry["chapters"][index],
                "current_run": prepared,
            }
        run = get_review_run(root, str(run_id))
        if run is None:
            raise ReviewWorkflowError("range_run_missing", "range references a missing review run")
        current_result: dict[str, Any] | None = None
        if run.get("status") in {"validated", "failed_persistence", "persisted"}:
            current_result = resume_review(root, run_id=str(run_id))
            run = get_review_run(root, str(run_id)) or run
        if run.get("status") in {"prepared", "awaiting_decision", "validated"}:
            return {
                "schema_version": REVIEW_RANGE_SCHEMA,
                "status": "paused",
                "range_id": range_id,
                "current_chapter": run["chapter"],
                "current_run": current_result or resume_review(root, run_id=str(run_id)),
            }

        needs_range_choice = (
            bool(run.get("has_blocking"))
            or run.get("status") in {
                "abandoned",
                "targeted_fix_pending",
                "targeted_fix_blocked",
                "failed_validation",
                "failed_persistence",
                "stale",
            }
        )
        if needs_range_choice and str(index) not in (entry.get("overrides") or {}):
            decision = _range_decision_payload(entry, run)
            def wait(value: dict[str, Any]) -> None:
                value["status"] = "awaiting_decision"
                value["decision"] = decision
            _update_range(root, range_id, wait)
            return {
                "schema_version": REVIEW_RANGE_SCHEMA,
                "status": "awaiting_user",
                "range_id": range_id,
                "current_chapter": run["chapter"],
                "decision": decision,
            }

        next_index = index + 1
        if next_index >= len(entry["chapters"]):
            final_status = "partial" if (entry.get("skipped") or needs_range_choice) else "completed"
            _update_range(root, range_id, lambda value: value.update({"status": final_status, "decision": None}))
            return {
                "schema_version": REVIEW_RANGE_SCHEMA,
                "status": final_status,
                "range_id": range_id,
                "chapters": entry["chapters"],
                "run_ids": entry["run_ids"],
                "skipped": entry.get("skipped") or [],
            }
        prepared = _attach_next_range_run(root, entry, next_index)
        return {
            "schema_version": REVIEW_RANGE_SCHEMA,
            "status": "in_progress",
            "range_id": range_id,
            "current_chapter": entry["chapters"][next_index],
            "current_run": prepared,
        }


def decide_review_range(
    project_root: str | Path,
    *,
    range_id: str,
    request_file: str | Path,
) -> dict[str, Any]:
    root = _project_root(project_root)
    request = load_review_decision_request(request_file, project_root=root)
    if request.get("kind") != "range" or request.get("range_id") != range_id:
        raise ReviewWorkflowError(
            "cross_range_decision",
            "decision request does not name this review range",
        )
    with _range_lock(root, range_id):
        entry = get_review_range(root, range_id)
        if entry is None:
            raise ReviewWorkflowError("range_not_found", f"review range does not exist: {range_id}")
        _current_codex_thread_id(entry.get("parent_thread_id"))
        decision = entry.get("decision")
        index = int(entry.get("current_index") or 0)
        run_id = (entry.get("run_ids") or [])[index]
        run = get_review_run(root, str(run_id))
        if run is None or entry.get("status") != "awaiting_decision" or not isinstance(decision, Mapping):
            raise ReviewWorkflowError("decision_not_pending", "review range has no pending decision")
        if request.get("request_id") != decision.get("request_id") or decision != _range_decision_payload(entry, run):
            raise ReviewWorkflowError("stale_decision", "range decision does not match current run state")
        choice, runtime_receipt = _verified_user_choice(
            request["runtime"],
            decision,
            expected_parent_thread_id=_decision_parent_thread_id(run),
            expected_parent_model=entry.get("parent_model"),
            expected_parent_effort=entry.get("parent_reasoning_effort"),
            question_id="range_action",
        )
        if choice not in RANGE_CHOICES:
            raise ReviewWorkflowError(
                "invalid_decision_answer",
                "verified user answer selected an unsupported range choice",
            )
        selected_at = _now_iso()
        if choice == "stop":
            def stop(value: dict[str, Any]) -> None:
                selected_decision = {
                    **decision,
                    "status": "selected",
                    "selected": choice,
                    "selected_at": selected_at,
                    "runtime_receipt": runtime_receipt,
                }
                value["status"] = "stopped"
                value["decision"] = selected_decision
                value.setdefault("decision_history", {})[str(index)] = selected_decision
            _update_range(root, range_id, stop)
            return {"schema_version": REVIEW_RANGE_SCHEMA, "status": "stopped", "range_id": range_id}

        def cont(value: dict[str, Any]) -> None:
            selected_decision = {
                **decision,
                "status": "selected",
                "selected": choice,
                "selected_at": selected_at,
                "runtime_receipt": runtime_receipt,
            }
            value.setdefault("overrides", {})[str(index)] = "continue"
            value.setdefault("skipped", []).append(
                {"chapter": run["chapter"], "run_id": run_id, "status": run.get("status")}
            )
            value["status"] = "in_progress"
            value["decision"] = selected_decision
            value.setdefault("decision_history", {})[str(index)] = selected_decision
        _update_range(root, range_id, cont)
    return resume_review_range(root, range_id=range_id)


def format_review_result(payload: Mapping[str, Any], output_format: str = "json") -> str:
    if output_format == "json":
        return json.dumps(payload, ensure_ascii=False, indent=2)
    status = str(payload.get("status") or "unknown")
    lines = [f"status: {status}"]
    for name in ("run_id", "range_id", "chapter", "current_chapter", "next_action"):
        value = payload.get(name)
        if value not in {None, ""}:
            lines.append(f"{name}: {value}")
    decision = payload.get("decision")
    if isinstance(decision, Mapping):
        lines.append(f"decision_request_id: {decision.get('request_id')}")
        for index, option in enumerate(decision.get("options") or [], start=1):
            lines.append(f"{index}. {option.get('id')} - {option.get('label')}")
        marker = decision.get("binding_marker")
        if isinstance(marker, str) and marker:
            lines.append("decision_binding_marker:")
            lines.append(marker)
    return "\n".join(lines)


def error_payload(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, ReviewWorkflowError):
        code, details = exc.code, exc.details
    elif isinstance(exc, (ReviewRequestError, RunLedgerError)):
        code, details = "invalid_request", {}
    else:
        code, details = "review_workflow_failed", {}
    return {
        "schema_version": REVIEW_WORKFLOW_SCHEMA,
        "status": "blocked",
        "code": code,
        "message": str(exc),
        "details": details,
        "body_changed": False,
        "report_written": False,
        "metrics_saved": False,
    }
