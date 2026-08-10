#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Immutable receipt chain for the transactional chapter workflow.

The module orchestrates no model calls.  Agent stages advance only after the
M3 runtime identity gate and role payload validator both accept the result.
Canned evidence is restricted to explicitly test-only transactions and can
never be mistaken for a production completion.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from uuid import UUID, uuid4

try:
    from filelock import FileLock, Timeout
except ImportError:  # fail closed
    FileLock = None  # type: ignore[assignment]

try:
    from chapter_paths import find_chapter_file
except ImportError:
    from scripts.chapter_paths import find_chapter_file

from .codex_agent_runtime import (
    VerifiedRuntimeEvidence,
    build_canned_envelope,
    build_workflow_route,
    validate_agent_envelope,
    validate_agent_payload,
    validate_route_readiness,
)
from .codex_decision_receipt import (
    DecisionReceiptError,
    build_scope_bound_decision_request,
    select_scope_bound_decision,
    verify_scope_bound_decision_receipt,
)
from .codex_m3_smoke import (
    SmokeEvidenceError,
    coalesce_session_meta_payloads,
    coalesce_turn_context_payloads,
    derive_agent_task_name,
    validate_agent_task_binding,
)
from .project_phase import contract_files_for_chapter
from .projection_log import (
    commit_hash,
    latest_projection_run,
    projection_status_from_run,
    read_projection_runs,
)
from .review_schema import ReviewSchemaError, parse_review_output
from .write_gates import run_write_gate


TRANSACTION_SCHEMA_VERSION = "webnovel-write-transaction/v1"
RECEIPT_SCHEMA_VERSION = "webnovel-write-stage-receipt/v1"
NO_REVIEW_SCHEMA_VERSION = "webnovel-no-review/v1"
AGENT_ACCEPT_REQUEST_SCHEMA = "webnovel-write-agent-accept-request/v2"
AGENT_LAUNCH_REQUEST_SCHEMA = "webnovel-write-agent-launch-request/v1"
AGENT_LAUNCH_INPUT_SCHEMA = "webnovel-write-agent-launch-input/v1"
AGENT_PROMPT_MARKER_SCHEMA = "webnovel-write-agent-prompt-marker/v1"
AGENT_PROMPT_MARKER_PREFIX = "WEBNOVEL_WRITE_AGENT_REQUEST "
AGENT_TASK_NAME_PREFIX = "wnw"
STAGE_REQUEST_SCHEMA = "webnovel-write-stage-request/v1"
TARGETED_FIX_RESOLUTION_SCHEMA = "webnovel-write-targeted-fix-resolution/v1"
TARGETED_FIX_DECISION_KIND = "write_targeted_fix"
RECOVERY_DECISION_KIND = "write_recovery"
TERMINAL_RECOVERY_CHOICES = frozenset({"keep_current", "cancel", "status_only"})
_RECOVERY_RECEIPT_NAME_RE = re.compile(
    r"recovery-(?P<request_sha256>[0-9a-f]{64})-receipt\.json\Z"
)
_RECOVERY_CONFLICT_MESSAGES = {
    "chapter_file_changed": "正文在本轮开始后被修改",
    "contracts_changed_after_begin": "章纲或合同在本轮开始后发生变化",
    "outline_newer_than_draft": "章纲或合同晚于现有正文",
    "chapter_file_created_concurrently": "正文在本轮开始后由其他流程创建",
    "chapter_already_accepted": "本章已有 accepted commit",
}
MAX_REQUEST_BYTES = 1024 * 1024
MAX_ROLLOUT_BYTES = 32 * 1024 * 1024
MAX_CONTROL_BYTES = 8 * 1024 * 1024
MAX_WRITER_RESOLUTION_SUMMARY_CHARS = 1024
TRUSTED_CODEX_SESSIONS_ROOT = Path(os.path.abspath(Path.home() / ".codex" / "sessions"))
WRITE_MODES = {"default", "fast", "minimal"}
WRITE_STAGES = (
    "preflight",
    "prewrite",
    "context_agent",
    "writer_draft",
    "reviewer",
    "review_pipeline",
    "writer_final",
    "promotion",
    "data_agent",
    "precommit",
    "commit",
    "projections",
    "postcommit",
    "backup",
    "complete",
)
AGENT_STAGES = {
    "context_agent": "webnovel_context_agent",
    "writer_draft": "webnovel_writer",
    "reviewer": "webnovel_reviewer",
    "writer_final": "webnovel_writer",
    "data_agent": "webnovel_data_agent",
}
PROJECTION_WRITERS = {"state", "index", "summary", "memory", "vector"}
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,94}[A-Za-z0-9])?$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_VERIFIED_STAGE_TOKEN = object()
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
_ACTIVE_TEST_RUNS: set[tuple[str, str, str]] = set()


class WriteTransactionError(RuntimeError):
    """A write stage cannot safely advance."""


class WriteRecoveryChoiceRequired(WriteTransactionError):
    """The author must choose how to handle existing or changed canon."""

    def __init__(
        self,
        code: str,
        message: str,
        choices: Sequence[str] = ("keep_current", "replace_with_verified", "cancel"),
    ):
        super().__init__(message)
        self.code = code
        self.choices = tuple(choices)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _valid_run_id(value: object) -> bool:
    if not isinstance(value, str) or _RUN_ID_RE.fullmatch(value) is None:
        return False
    return value.split(".", 1)[0].upper() not in _WINDOWS_RESERVED_NAMES


def _test_run_key(root: Path, run_id: str, transaction_sha256: object) -> tuple[str, str, str]:
    return (
        os.path.normcase(str(root.resolve())),
        run_id,
        str(transaction_sha256 or ""),
    )


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _payload_sha256(payload: object) -> str:
    if isinstance(payload, str):
        return _sha256_bytes(payload.encode("utf-8"))
    return _sha256_bytes(_canonical_bytes(payload))


def _absolute_lexical(path: str | Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _is_reparse_point(path: Path) -> bool:
    try:
        attrs = int(getattr(path.lstat(), "st_file_attributes", 0))
    except OSError:
        return True
    return path.is_symlink() or bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _path_chain(path: Path) -> list[Path]:
    absolute = _absolute_lexical(path)
    current = Path(absolute.anchor)
    result = [current]
    for part in absolute.parts[1:]:
        current = current / part
        result.append(current)
    return result


def _safe_project_root(project_root: str | Path) -> Path:
    lexical = _absolute_lexical(project_root)
    for component in _path_chain(lexical):
        if (component.exists() or component.is_symlink()) and _is_reparse_point(component):
            raise WriteTransactionError(f"reparse-point project root is forbidden: {component}")
    if not lexical.is_dir():
        raise WriteTransactionError(f"project_root is not a directory: {lexical}")
    return lexical.resolve(strict=True)


def _require_safe_path(
    root: Path,
    path: Path,
    *,
    allowed_root: Path | None = None,
    must_exist: bool,
    regular_file: bool = False,
) -> Path:
    root_lexical = _absolute_lexical(root)
    lexical = _absolute_lexical(path)
    allowed = _absolute_lexical(allowed_root or root)
    try:
        allowed.relative_to(root_lexical)
        lexical.relative_to(allowed)
    except ValueError as exc:
        raise WriteTransactionError(f"path escapes trusted root: {lexical}") from exc
    for component in _path_chain(lexical):
        if (component.exists() or component.is_symlink()) and _is_reparse_point(component):
            raise WriteTransactionError(f"reparse-point path is forbidden: {component}")
    if must_exist and not lexical.exists():
        raise WriteTransactionError(f"required path is missing: {lexical}")
    if regular_file and must_exist and not lexical.is_file():
        raise WriteTransactionError(f"required file is missing: {lexical}")
    if regular_file and (lexical.exists() or lexical.is_symlink()) and not lexical.is_file():
        raise WriteTransactionError(f"path is not a regular file: {lexical}")
    try:
        lexical.resolve(strict=must_exist).relative_to(allowed.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise WriteTransactionError(f"path escapes trusted root: {lexical}") from exc
    return lexical


def _safe_mkdir_chain(root: Path, target: Path) -> None:
    root = _absolute_lexical(root)
    target = _absolute_lexical(target)
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise WriteTransactionError(f"directory escapes project: {target}") from exc
    current = root
    for part in relative.parts:
        current = current / part
        if current.exists() or current.is_symlink():
            if _is_reparse_point(current) or not current.is_dir():
                raise WriteTransactionError(f"unsafe transaction directory: {current}")
        else:
            current.mkdir()
            if _is_reparse_point(current) or not current.is_dir():
                raise WriteTransactionError(f"unsafe transaction directory: {current}")


def _prepare_control_target(root: Path, path: Path) -> None:
    _safe_mkdir_chain(root, path.parent)
    for candidate in (
        path,
        path.with_suffix(path.suffix + ".lock"),
        path.with_suffix(path.suffix + ".bak"),
    ):
        _require_safe_path(root, candidate, must_exist=False, regular_file=True)


def _stable_read_snapshot(
    path: Path,
    *,
    trusted_root: Path,
    max_bytes: int,
) -> tuple[bytes, os.stat_result]:
    lexical = _require_safe_path(
        trusted_root,
        path,
        allowed_root=trusted_root,
        must_exist=True,
        regular_file=True,
    )
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(lexical, flags)
    except OSError as exc:
        raise WriteTransactionError(f"file is unreadable: {lexical}: {exc}") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
            raise WriteTransactionError(f"file is not regular or exceeds size limit: {lexical}")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(max_bytes + 1)
        after = os.fstat(fd)
        path_after = lexical.stat()
        identity = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
        _require_safe_path(
            trusted_root,
            lexical,
            allowed_root=trusted_root,
            must_exist=True,
            regular_file=True,
        )
        if (
            len(raw) > max_bytes
            or len(raw) != before.st_size
            or identity(before) != identity(after)
            or identity(before) != identity(path_after)
        ):
            raise WriteTransactionError(f"file changed during bounded read: {lexical}")
        return raw, before
    except OSError as exc:
        raise WriteTransactionError(f"file is unreadable: {lexical}: {exc}") from exc
    finally:
        os.close(fd)


def _stable_read_bytes(
    path: Path,
    *,
    trusted_root: Path,
    max_bytes: int,
) -> bytes:
    raw, _ = _stable_read_snapshot(path, trusted_root=trusted_root, max_bytes=max_bytes)
    return raw


def _file_signature(
    path: str | Path | None,
    *,
    trusted_root: Path | None = None,
) -> dict[str, Any]:
    if not path:
        return {"path": "", "exists": False}
    target = _absolute_lexical(path)
    root = trusted_root or target.parent
    _require_safe_path(root, target, allowed_root=root, must_exist=False, regular_file=True)
    if not target.is_file():
        return {"path": str(target), "exists": False}
    raw, stat_result = _stable_read_snapshot(
        target,
        trusted_root=root,
        max_bytes=MAX_CONTROL_BYTES,
    )
    return {
        "path": str(target),
        "exists": True,
        "sha256": _sha256_bytes(raw),
        "bytes": len(raw),
        "mtime_ns": stat_result.st_mtime_ns,
    }


def _receipt_hash(payload: Mapping[str, Any]) -> str:
    return _sha256_bytes(_canonical_bytes(dict(payload)))


def _run_dir(root: Path, run_id: str) -> Path:
    return root / ".webnovel" / "write-runs" / run_id


def _staging_dir(root: Path, run_id: str) -> Path:
    return root / ".webnovel" / "tmp" / "write-runs" / run_id


def _json_object_from_bytes(raw: bytes, path: Path) -> dict[str, Any]:
    def build_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ValueError(f"invalid JSON constant: {value}")

    try:
        if raw.startswith(b"\xef\xbb\xbf"):
            raise ValueError("UTF-8 BOM is forbidden")
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=build_object,
            parse_constant=reject_constant,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise WriteTransactionError(f"invalid transaction JSON: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise WriteTransactionError(f"transaction JSON must be an object: {path}")
    return payload


def _read_json(path: Path, *, trusted_root: Path | None = None) -> dict[str, Any]:
    raw = _stable_read_bytes(
        path,
        trusted_root=trusted_root or path.parent,
        max_bytes=MAX_CONTROL_BYTES,
    )
    return _json_object_from_bytes(raw, path)


def _write_json_once(root: Path, path: Path, payload: dict[str, Any]) -> None:
    if FileLock is None:
        raise WriteTransactionError("filelock is required for transaction control writes")
    _prepare_control_target(root, path)
    lock_path = path.with_suffix(path.suffix + ".lock")
    raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    try:
        with FileLock(str(lock_path), timeout=10):
            _prepare_control_target(root, path)
            if path.is_file():
                if _read_json(path, trusted_root=root) != payload:
                    raise WriteTransactionError(f"immutable transaction file differs: {path}")
                return
            _atomic_write_bytes(path, raw, root=root)
            _prepare_control_target(root, path)
            if _stable_read_bytes(path, trusted_root=root, max_bytes=MAX_CONTROL_BYTES) != raw:
                raise WriteTransactionError(f"transaction control write failed exact readback: {path}")
    except Timeout as exc:
        raise WriteTransactionError(f"transaction control lock is busy: {path}") from exc


def _write_bytes_once(
    root: Path,
    path: Path,
    raw: bytes,
    *,
    replace_before_stage: tuple[str, str] | None = None,
) -> None:
    """Persist one immutable, byte-exact run evidence artifact."""

    if FileLock is None:
        raise WriteTransactionError("filelock is required for transaction evidence writes")
    _prepare_control_target(root, path)
    lock_path = path.with_suffix(path.suffix + ".lock")
    try:
        with FileLock(str(lock_path), timeout=10):
            _prepare_control_target(root, path)
            if path.is_file():
                current = _stable_read_bytes(
                    path,
                    trusted_root=root,
                    max_bytes=MAX_CONTROL_BYTES,
                )
                if current != raw:
                    if replace_before_stage is None:
                        raise WriteTransactionError(
                            f"immutable transaction evidence differs: {path}"
                        )
                    run_id, stage = replace_before_stage
                    transaction = _load_transaction(root, run_id)
                    receipts = _validated_receipts(
                        _run_dir(root, run_id),
                        transaction=transaction,
                    )
                    progress = _derive_progress(transaction, receipts)
                    if stage in progress.get("completed", {}):
                        raise WriteTransactionError(
                            f"accepted transaction evidence is immutable: {path}"
                        )
                else:
                    return
            _atomic_write_bytes(path, raw, root=root)
            _prepare_control_target(root, path)
            if (
                _stable_read_bytes(path, trusted_root=root, max_bytes=MAX_CONTROL_BYTES)
                != raw
            ):
                raise WriteTransactionError(
                    f"transaction evidence write failed exact readback: {path}"
                )
    except Timeout as exc:
        raise WriteTransactionError(f"transaction evidence lock is busy: {path}") from exc


def _safe_relative_path(root: Path, path: Path, allowed_root: Path) -> bool:
    try:
        _require_safe_path(
            root,
            _absolute_lexical(path),
            allowed_root=_absolute_lexical(allowed_root),
            must_exist=False,
        )
    except WriteTransactionError:
        return False
    return True


def _read_bounded_utf8(
    path: Path,
    *,
    trusted_root: Path | None = None,
) -> tuple[bytes, str]:
    raw = _stable_read_bytes(
        path,
        trusted_root=trusted_root or path.parent,
        max_bytes=MAX_REQUEST_BYTES,
    )
    if raw.startswith(b"\xef\xbb\xbf"):
        raise WriteTransactionError(f"request artifact must be UTF-8 without BOM: {path}")
    try:
        return raw, raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WriteTransactionError(f"request artifact is not UTF-8: {path}") from exc


def _read_bounded_rollout(path: Path) -> bytes:
    return _stable_read_bytes(
        path,
        trusted_root=TRUSTED_CODEX_SESSIONS_ROOT,
        max_bytes=MAX_ROLLOUT_BYTES,
    )


def _current_parent_host_evidence() -> dict[str, Any]:
    """Bind production work to the Desktop task identified by CODEX_THREAD_ID."""

    supplied = os.environ.get("CODEX_THREAD_ID")
    if not isinstance(supplied, str) or not supplied or supplied.strip() != supplied:
        raise WriteTransactionError(
            "CODEX_THREAD_ID must be a canonical non-zero UUID for production write"
        )
    try:
        parsed_thread_id = UUID(supplied)
    except (ValueError, AttributeError) as exc:
        raise WriteTransactionError(
            "CODEX_THREAD_ID must be a canonical non-zero UUID for production write"
        ) from exc
    thread_id = str(parsed_thread_id)
    if parsed_thread_id.int == 0 or supplied != thread_id:
        raise WriteTransactionError(
            "CODEX_THREAD_ID must be a canonical non-zero UUID for production write"
        )
    sessions_root = _absolute_lexical(TRUSTED_CODEX_SESSIONS_ROOT)
    _require_safe_path(sessions_root, sessions_root, must_exist=True)
    candidates: list[Path] = []
    for current_raw, dirs, files in os.walk(sessions_root, followlinks=False):
        current = Path(current_raw)
        _require_safe_path(sessions_root, current, must_exist=True)
        safe_dirs: list[str] = []
        for name in dirs:
            child = current / name
            if _is_reparse_point(child):
                raise WriteTransactionError("trusted Codex sessions root contains a reparse directory")
            safe_dirs.append(name)
        dirs[:] = safe_dirs
        for name in files:
            if thread_id in name and name.lower().endswith(".jsonl"):
                candidates.append(current / name)
    if len(candidates) != 1:
        raise WriteTransactionError("CODEX_THREAD_ID must match exactly one trusted parent rollout")
    rollout = _require_safe_path(
        sessions_root,
        candidates[0],
        must_exist=True,
        regular_file=True,
    )
    raw = _read_bounded_rollout(rollout)
    try:
        events = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WriteTransactionError("current parent rollout is not UTF-8 JSONL") from exc
    if not all(isinstance(event, Mapping) for event in events):
        raise WriteTransactionError("current parent rollout events must be JSON objects")
    try:
        _, session = coalesce_session_meta_payloads(
            events,
            expected_thread_id=thread_id,
        )
    except SmokeEvidenceError as exc:
        raise WriteTransactionError(
            "current parent rollout session identity does not match CODEX_THREAD_ID: "
            f"{exc}"
        ) from exc
    source = session.get("source")
    if (
        bool(str(session.get("parent_thread_id") or "").strip())
        or (isinstance(source, Mapping) and isinstance(source.get("subagent"), Mapping))
    ):
        raise WriteTransactionError(
            "current write parent rollout belongs to a child Agent rather than the top-level task"
        )
    return {
        "thread_id": thread_id,
        "rollout_path": str(rollout.resolve(strict=True)),
        "rollout_sha256": _sha256_bytes(raw),
        "rollout_bytes": len(raw),
        "_raw": raw,
    }


def _assert_current_parent_binding(transaction: Mapping[str, Any]) -> None:
    evidence = _current_parent_host_evidence()
    if (
        transaction.get("parent_thread_id") != evidence["thread_id"]
        or transaction.get("parent_rollout_path") != evidence["rollout_path"]
    ):
        raise WriteTransactionError("current parent task evidence changed after write begin")
    expected_bytes = int(transaction.get("parent_rollout_bytes") or 0)
    current_raw = evidence.get("_raw")
    if isinstance(current_raw, bytes) and expected_bytes > 0:
        if (
            len(current_raw) < expected_bytes
            or _sha256_bytes(current_raw[:expected_bytes]) != transaction.get("parent_rollout_sha256")
        ):
            raise WriteTransactionError("current parent rollout changed before its bound append point")
    elif transaction.get("parent_rollout_sha256") != evidence.get("rollout_sha256"):
        raise WriteTransactionError("current parent task evidence changed after write begin")


def _load_run_request(root: Path, run_id: str, request_file: str | Path) -> dict[str, Any]:
    request_path = Path(request_file)
    if not request_path.is_absolute():
        raise WriteTransactionError("request-file must be an absolute path")
    request_path = _absolute_lexical(request_path)
    requests_root = _absolute_lexical(_staging_dir(root, run_id) / "requests")
    if not _safe_relative_path(root, request_path, requests_root):
        raise WriteTransactionError("request-file must stay inside this run requests directory")
    raw, text = _read_bounded_utf8(request_path, trusted_root=requests_root)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise WriteTransactionError(f"invalid request JSON: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("run_id") != run_id:
        raise WriteTransactionError("request-file must be an object bound to this run")
    payload["_request_path"] = str(request_path)
    payload["_request_sha256"] = _sha256_bytes(raw)
    payload["_request_bytes"] = len(raw)
    return payload


def _request_artifact(
    root: Path,
    run_id: str,
    spec: object,
    *,
    allowed_root: Path,
) -> tuple[Path, dict[str, Any], bytes]:
    if not isinstance(spec, Mapping) or set(spec) != {"path", "sha256"}:
        raise WriteTransactionError("request artifact must contain exactly path and sha256")
    path = Path(str(spec.get("path") or ""))
    digest = str(spec.get("sha256") or "")
    if not path.is_absolute() or not _SHA256_RE.fullmatch(digest):
        raise WriteTransactionError("request artifact path/hash is invalid")
    path = _absolute_lexical(path)
    allowed_root = _absolute_lexical(allowed_root)
    if not _safe_relative_path(root, path, allowed_root) or not path.is_file():
        raise WriteTransactionError("request artifact is outside its allowed root")
    raw, stat_result = _stable_read_snapshot(
        path,
        trusted_root=allowed_root,
        max_bytes=MAX_CONTROL_BYTES,
    )
    signature = {
        "path": str(path),
        "exists": True,
        "sha256": _sha256_bytes(raw),
        "bytes": len(raw),
        "mtime_ns": stat_result.st_mtime_ns,
    }
    if signature.get("sha256") != digest:
        raise WriteTransactionError("request artifact hash mismatch")
    return path, signature, raw


def _load_agent_launch_request(
    root: Path,
    run_id: str,
    stage: str,
    transaction: Mapping[str, Any],
    spec: object,
) -> tuple[dict[str, Any], Path, dict[str, Any]]:
    requests_root = _absolute_lexical(_staging_dir(root, run_id) / "requests")
    path, signature, raw = _request_artifact(
        root,
        run_id,
        spec,
        allowed_root=requests_root,
    )
    expected = _absolute_lexical(requests_root / f"{stage}-launch.json")
    if path != expected:
        raise WriteTransactionError("agent launch request must use the fixed stage filename")
    launch = _json_object_from_bytes(raw, path)
    if set(launch) != {
        "schema_version",
        "run_id",
        "stage",
        "transaction_sha256",
        "input_artifacts",
    } or launch.get("schema_version") != AGENT_LAUNCH_REQUEST_SCHEMA:
        raise WriteTransactionError("agent launch request has an invalid shape")
    if (
        launch.get("run_id") != run_id
        or launch.get("stage") != stage
        or launch.get("transaction_sha256") != transaction.get("transaction_sha256")
    ):
        raise WriteTransactionError("agent launch request does not bind this transaction stage")
    inputs = launch.get("input_artifacts")
    if not isinstance(inputs, list) or not 1 <= len(inputs) <= 32:
        raise WriteTransactionError("agent launch request needs 1-32 explicit input artifacts")
    seen: set[tuple[str, str]] = set()
    verified_inputs: list[dict[str, Any]] = []
    for item in inputs:
        input_path, input_signature, _ = _request_artifact(
            root,
            run_id,
            item,
            allowed_root=root,
        )
        pair = (str(input_path), str(input_signature.get("sha256") or ""))
        if pair in seen:
            raise WriteTransactionError("agent launch request contains duplicate inputs")
        seen.add(pair)
        verified_inputs.append({"path": pair[0], "sha256": pair[1]})
    launch["input_artifacts"] = verified_inputs
    return launch, path, signature


def _agent_prompt_marker_payload(
    launch: Mapping[str, Any],
    launch_signature: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": AGENT_PROMPT_MARKER_SCHEMA,
        "run_id": launch["run_id"],
        "stage": launch["stage"],
        "transaction_sha256": launch["transaction_sha256"],
        "launch_request_sha256": launch_signature["sha256"],
        "input_artifacts": launch["input_artifacts"],
    }


def build_agent_prompt_marker(
    project_root: str | Path,
    run_id: str,
    *,
    stage: str,
    launch_request: object,
) -> str:
    """Return the exact marker that must be placed in the child Agent prompt."""

    root = _safe_project_root(project_root)
    transaction = _load_transaction(root, run_id)
    if stage not in AGENT_STAGES:
        raise WriteTransactionError("prompt marker stage must be an Agent stage")
    if not transaction.get("test_only"):
        _assert_current_parent_binding(transaction)
    launch, _, signature = _load_agent_launch_request(
        root,
        run_id,
        stage,
        transaction,
        launch_request,
    )
    _, progress = _replayed_progress(root, transaction)
    if progress.get("next_stage") != stage:
        raise WriteTransactionError(f"Agent prompt marker is out of order: expected {progress.get('next_stage')}")
    _validate_stage_launch_lineage(
        root,
        run_id,
        stage,
        transaction,
        progress,
        launch.get("input_artifacts") or [],
    )
    return AGENT_PROMPT_MARKER_PREFIX + _canonical_bytes(
        _agent_prompt_marker_payload(launch, signature)
    ).decode("utf-8")


def _task_name_from_prompt_marker(marker: str) -> str:
    """Derive the host task name without accepting a caller-supplied alias."""

    try:
        return derive_agent_task_name(marker, prefix=AGENT_TASK_NAME_PREFIX)
    except SmokeEvidenceError as exc:
        raise WriteTransactionError(f"Agent prompt marker cannot bind a task name: {exc}") from exc


def prepare_agent_launch_request(
    project_root: str | Path,
    run_id: str,
    input_request_file: str | Path,
) -> dict[str, Any]:
    """Create one immutable launch request from a bounded input-artifact file."""

    root = _safe_project_root(project_root)
    transaction = _load_transaction(root, run_id)
    if transaction.get("test_only"):
        raise WriteTransactionError("Agent launch requests are production-only")
    _assert_current_parent_binding(transaction)
    request = _load_run_request(root, run_id, input_request_file)
    fields = {key for key in request if not key.startswith("_request_")}
    if fields != {"schema_version", "run_id", "stage", "input_artifacts"} or request.get(
        "schema_version"
    ) != AGENT_LAUNCH_INPUT_SCHEMA:
        raise WriteTransactionError("Agent launch input request has an invalid shape")
    stage = str(request.get("stage") or "")
    if stage not in AGENT_STAGES:
        raise WriteTransactionError("Agent launch input names a non-Agent stage")
    _, progress = _replayed_progress(root, transaction)
    if progress.get("next_stage") != stage:
        raise WriteTransactionError(f"Agent launch is out of order: expected {progress.get('next_stage')}")
    raw_inputs = request.get("input_artifacts")
    if not isinstance(raw_inputs, list) or not 1 <= len(raw_inputs) <= 32:
        raise WriteTransactionError("Agent launch needs 1-32 input artifacts")
    inputs: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for spec in raw_inputs:
        path, signature, _ = _request_artifact(root, run_id, spec, allowed_root=root)
        pair = (str(path), str(signature.get("sha256") or ""))
        if pair in seen:
            raise WriteTransactionError("Agent launch input contains duplicate artifacts")
        seen.add(pair)
        inputs.append({"path": pair[0], "sha256": pair[1]})
    _validate_stage_launch_lineage(
        root,
        run_id,
        stage,
        transaction,
        progress,
        inputs,
    )
    launch = {
        "schema_version": AGENT_LAUNCH_REQUEST_SCHEMA,
        "run_id": run_id,
        "stage": stage,
        "transaction_sha256": transaction["transaction_sha256"],
        "input_artifacts": inputs,
    }
    launch_path = _staging_dir(root, run_id) / "requests" / f"{stage}-launch.json"
    _write_json_once(root, launch_path, launch)
    signature = _file_signature(launch_path)
    marker = build_agent_prompt_marker(
        root,
        run_id,
        stage=stage,
        launch_request={"path": signature["path"], "sha256": signature["sha256"]},
    )
    agent_task_name = _task_name_from_prompt_marker(marker)
    return {
        "status": "ready",
        "stage": stage,
        "launch_request": {"path": signature["path"], "sha256": signature["sha256"]},
        "prompt_marker": marker,
        "agent_task_name": agent_task_name,
    }


def _message_text(payload: Mapping[str, Any]) -> str | None:
    if payload.get("type") != "message":
        return None
    content = payload.get("content")
    if not isinstance(content, list):
        return None
    pieces = [
        str(item.get("text") or "")
        for item in content
        if isinstance(item, Mapping) and item.get("type") in {"output_text", "input_text", "text"}
    ]
    return "".join(pieces) if pieces else None


def _parse_bound_agent_rollout(
    raw: bytes,
    *,
    rollout_path: Path,
    thread_id: str,
    parent_thread_id: str,
    expected_agent: str,
    expected_model: str,
    expected_effort: str,
    expected_marker: str,
    expected_task_name: str | None,
) -> tuple[VerifiedRuntimeEvidence, str]:
    if thread_id not in rollout_path.name or rollout_path.suffix.lower() != ".jsonl":
        raise WriteTransactionError("rollout filename must identify the expected child thread")
    try:
        events = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WriteTransactionError("rollout is not UTF-8 JSONL") from exc
    if not all(isinstance(event, Mapping) for event in events):
        raise WriteTransactionError("rollout events must be JSON objects")
    try:
        session_index, session = coalesce_session_meta_payloads(
            events,
            expected_thread_id=thread_id,
        )
    except SmokeEvidenceError as exc:
        raise WriteTransactionError(f"rollout session identity is invalid: {exc}") from exc
    source = session.get("source")
    subagent = source.get("subagent") if isinstance(source, Mapping) else None
    spawn = subagent.get("thread_spawn") if isinstance(subagent, Mapping) else None
    if not isinstance(spawn, Mapping):
        raise WriteTransactionError("rollout is not a Codex child Agent session")
    if expected_task_name is not None:
        try:
            validate_agent_task_binding(spawn, expected_task_name=expected_task_name)
        except SmokeEvidenceError as exc:
            raise WriteTransactionError(f"rollout Agent task binding is invalid: {exc}") from exc
    if (
        session.get("id") != thread_id
        or session.get("parent_thread_id") != parent_thread_id
        or spawn.get("parent_thread_id") != parent_thread_id
        or spawn.get("agent_role") != expected_agent
        or (session.get("model") is not None and session.get("model") != expected_model)
    ):
        raise WriteTransactionError("rollout child/parent/role/model identity mismatch")
    turn_events = [
        event
        for event in events[session_index + 1 :]
        if event.get("type") == "turn_context"
    ]
    if not turn_events:
        raise WriteTransactionError("rollout lacks turn_context")
    try:
        turns = coalesce_turn_context_payloads(turn_events)
    except SmokeEvidenceError as exc:
        raise WriteTransactionError(f"rollout turn identity is invalid: {exc}") from exc
    for turn in turns:
        if turn.get("model") != expected_model or turn.get("effort") != expected_effort:
            raise WriteTransactionError("rollout model or effort changed within the child task")

    marker_indexes: list[int] = []
    prompt_text = spawn.get("prompt")
    if isinstance(prompt_text, str) and expected_marker in prompt_text.splitlines():
        marker_indexes.append(session_index)
    response_messages: list[tuple[int, Mapping[str, Any], str]] = []
    for index, event in enumerate(events[session_index + 1 :], start=session_index + 1):
        if event.get("type") != "response_item":
            continue
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            continue
        text = _message_text(payload)
        if text is None:
            continue
        if payload.get("role") == "user" and expected_marker in text.splitlines():
            marker_indexes.append(index)
        response_messages.append((index, payload, text))
    if len(marker_indexes) > 1:
        raise WriteTransactionError("rollout contains duplicate Agent prompt markers")

    assistant_outputs: list[str] = []
    if marker_indexes:
        # Explicit-marker compatibility for older Desktop rollouts.  A real
        # legacy record may omit phase, while newer hosts label durable output
        # as final/final_answer.  Explicit commentary is never payload evidence.
        marker_index = marker_indexes[0]
        assistant_outputs = [
            text
            for index, payload, text in response_messages
            if index > marker_index
            and payload.get("role") == "assistant"
            and payload.get("phase") in {None, "final", "final_answer"}
            and text
        ]
    elif expected_task_name is not None:
        # Current Desktop child rollouts can omit the plaintext prompt.  The
        # marker-derived /root/<task_name> is then the invocation binding, and
        # only the durable final assistant record is payload evidence.
        assistant_outputs = [
            text
            for _, payload, text in response_messages
            if payload.get("role") == "assistant"
            and payload.get("phase") in {"final", "final_answer"}
            and text
        ]

    if len(assistant_outputs) != 1 or not assistant_outputs[0]:
        if len(assistant_outputs) > 1:
            raise WriteTransactionError("rollout contains multiple assistant outputs after the bound marker")
        raise WriteTransactionError("rollout lacks the bound prompt marker or final assistant output")
    evidence = VerifiedRuntimeEvidence(
        evidence_source="codex_trace",
        agent_name=expected_agent,
        actual_model=expected_model,
        actual_reasoning_effort=expected_effort,
        thread_id=thread_id,
        parent_thread_id=parent_thread_id,
        raw_sha256=_sha256_bytes(raw),
    )
    return evidence, assistant_outputs[0]


def _atomic_write_bytes(path: Path, raw: bytes, *, root: Path | None = None) -> None:
    if root is not None:
        _safe_mkdir_chain(root, path.parent)
        _require_safe_path(root, path, must_exist=False, regular_file=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.stem + "_", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        if root is not None:
            _require_safe_path(root, path.parent, must_exist=True)
            _require_safe_path(root, path, must_exist=False, regular_file=True)
        os.replace(temp_path, path)
        if root is not None:
            _require_safe_path(root, path, must_exist=True, regular_file=True)
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def _latest_contract_mtime(root: Path, chapter: int) -> int:
    mtimes = [
        path.stat().st_mtime_ns
        for path in contract_files_for_chapter(root, chapter).values()
        if path.is_file()
    ]
    return max(mtimes or [0])


def _contract_signatures(root: Path, chapter: int) -> dict[str, dict[str, Any]]:
    """Snapshot every chapter contract so mid-run edits cannot go unnoticed."""

    return {
        name: _file_signature(path, trusted_root=root)
        for name, path in sorted(contract_files_for_chapter(root, chapter).items())
    }


def _validate_contract_signatures(
    root: Path,
    chapter: int,
    value: object,
) -> dict[str, dict[str, Any]]:
    expected = {
        name: _absolute_lexical(path)
        for name, path in sorted(contract_files_for_chapter(root, chapter).items())
    }
    if not isinstance(value, Mapping) or set(value) != set(expected):
        raise WriteTransactionError("transaction contract signatures have an invalid file set")
    normalized: dict[str, dict[str, Any]] = {}
    for name, expected_path in expected.items():
        signature = value.get(name)
        if not isinstance(signature, Mapping):
            raise WriteTransactionError("transaction contract signature is not an object")
        exists = signature.get("exists")
        required = (
            {"path", "exists", "sha256", "bytes", "mtime_ns"}
            if exists is True
            else {"path", "exists"}
        )
        if (
            set(signature) != required
            or type(exists) is not bool
            or _absolute_lexical(str(signature.get("path") or "")) != expected_path
            or (
                exists is True
                and (
                    not _SHA256_RE.fullmatch(str(signature.get("sha256") or ""))
                    or type(signature.get("bytes")) is not int
                    or int(signature.get("bytes") or 0) < 0
                    or type(signature.get("mtime_ns")) is not int
                    or int(signature.get("mtime_ns") or 0) <= 0
                )
            )
        ):
            raise WriteTransactionError("transaction contract signature shape is invalid")
        normalized[name] = dict(signature)
    return normalized


def _accepted_commit(root: Path, chapter: int) -> dict[str, Any] | None:
    path = root / ".story-system" / "commits" / f"chapter_{chapter:03d}.commit.json"
    if not path.is_file():
        return None
    payload = _read_json(path)
    meta = payload.get("meta") if isinstance(payload.get("meta"), Mapping) else {}
    return payload if meta.get("status") == "accepted" else None


def _accepted_commit_stable_snapshot(
    root: Path,
    chapter: int,
) -> dict[str, Any] | None:
    """Read one accepted commit once and derive every binding from those bytes."""

    path = (
        root / ".story-system" / "commits" / f"chapter_{chapter:03d}.commit.json"
    ).resolve()
    commits_root = root / ".story-system" / "commits"
    if not path.is_file():
        return None
    raw, stat_result = _stable_read_snapshot(
        path,
        trusted_root=commits_root,
        max_bytes=MAX_CONTROL_BYTES,
    )
    payload = _json_object_from_bytes(raw, path)
    meta = payload.get("meta") if isinstance(payload.get("meta"), Mapping) else {}
    if meta.get("status") != "accepted":
        return None
    return {
        "payload": payload,
        "signature": {
            "path": str(path),
            "exists": True,
            "sha256": _sha256_bytes(raw),
            "bytes": len(raw),
            "mtime_ns": stat_result.st_mtime_ns,
        },
        "commit_hash": commit_hash(payload),
    }


def begin_write_transaction(
    project_root: str | Path,
    *,
    chapter: int,
    mode: str,
    parent_model: str,
    parent_reasoning_effort: str | None = None,
    workspace_root: str | Path | None = None,
    run_id: str | None = None,
    test_only: bool = False,
) -> dict[str, Any]:
    """Create one immutable run descriptor without touching novel facts."""

    root = _safe_project_root(project_root)
    if not root.is_dir():
        raise WriteTransactionError(f"project_root is not a directory: {root}")
    if not isinstance(chapter, int) or isinstance(chapter, bool) or chapter <= 0:
        raise WriteTransactionError("chapter must be a positive integer")
    if mode not in WRITE_MODES:
        raise WriteTransactionError(f"unsupported write mode: {mode}")
    if not str(parent_model or "").strip():
        raise WriteTransactionError("parent_model is required")
    run_id = run_id or f"write-ch{chapter:04d}-{uuid4().hex[:12]}"
    if not _valid_run_id(run_id):
        raise WriteTransactionError("invalid run_id")
    route = build_workflow_route(
        "write",
        parent_model=str(parent_model).strip(),
        parent_reasoning_effort=parent_reasoning_effort,
        mode=mode,
    )
    workspace = Path(workspace_root).resolve() if workspace_root is not None else root
    readiness: dict[str, Any] | None = None
    parent_evidence: dict[str, str] | None = None
    if not test_only:
        if workspace_root is None or not workspace.is_dir():
            raise WriteTransactionError("production write begin requires an explicit workspace_root")
        try:
            readiness = validate_route_readiness(workspace, route)
        except Exception as exc:
            raise WriteTransactionError(f"managed agent readiness check failed: {exc}") from exc
        if readiness.get("ready") is not True:
            raise WriteTransactionError("all managed write agents must be current before begin")
        parent_evidence = _current_parent_host_evidence()
    body = find_chapter_file(root, chapter)
    transaction = {
        "schema_version": TRANSACTION_SCHEMA_VERSION,
        "run_id": run_id,
        "created_at": _now_iso(),
        "project_root": str(root),
        "workspace_root": str(workspace),
        "chapter": chapter,
        "mode": mode,
        "parent_model": str(parent_model).strip(),
        "parent_reasoning_effort": parent_reasoning_effort,
        "route": route,
        "route_readiness_sha256": (
            _sha256_bytes(_canonical_bytes(readiness)) if readiness is not None else "test-only"
        ),
        "stages": list(WRITE_STAGES),
        "body_before": _file_signature(body),
        "latest_contract_mtime_ns": _latest_contract_mtime(root, chapter),
        "contract_signatures_before": _contract_signatures(root, chapter),
        "accepted_commit_before": bool(_accepted_commit(root, chapter)),
        "parent_task_binding_status": "test_only" if test_only else "verified_current_parent",
        "parent_thread_id": parent_evidence["thread_id"] if parent_evidence else None,
        "parent_rollout_path": parent_evidence["rollout_path"] if parent_evidence else None,
        "parent_rollout_sha256": parent_evidence["rollout_sha256"] if parent_evidence else None,
        "parent_rollout_bytes": parent_evidence.get("rollout_bytes") if parent_evidence else None,
        "test_only": bool(test_only),
        "production_evidence_required": not test_only,
    }
    transaction["transaction_sha256"] = _receipt_hash(transaction)
    run_dir = _run_dir(root, run_id)
    _write_json_once(root, run_dir / "transaction.json", transaction)
    _safe_mkdir_chain(root, _staging_dir(root, run_id))
    if test_only:
        _ACTIVE_TEST_RUNS.add(_test_run_key(root, run_id, transaction["transaction_sha256"]))
    return transaction


def _load_transaction(root: Path, run_id: str) -> dict[str, Any]:
    if not _valid_run_id(run_id):
        raise WriteTransactionError("invalid run_id")
    transaction_path = _run_dir(root, run_id) / "transaction.json"
    _require_safe_path(root, transaction_path, must_exist=True, regular_file=True)
    transaction = _read_json(transaction_path, trusted_root=root)
    if transaction.get("schema_version") != TRANSACTION_SCHEMA_VERSION:
        raise WriteTransactionError("unsupported transaction schema")
    expected_fields = {
        "schema_version",
        "run_id",
        "created_at",
        "project_root",
        "workspace_root",
        "chapter",
        "mode",
        "parent_model",
        "parent_reasoning_effort",
        "route",
        "route_readiness_sha256",
        "stages",
        "body_before",
        "latest_contract_mtime_ns",
        "contract_signatures_before",
        "accepted_commit_before",
        "parent_task_binding_status",
        "parent_thread_id",
        "parent_rollout_path",
        "parent_rollout_sha256",
        "parent_rollout_bytes",
        "test_only",
        "production_evidence_required",
        "transaction_sha256",
    }
    if set(transaction) != expected_fields:
        raise WriteTransactionError("transaction descriptor fields are invalid")
    check = dict(transaction)
    claimed = str(check.pop("transaction_sha256", ""))
    if claimed != _receipt_hash(check):
        raise WriteTransactionError("transaction descriptor hash mismatch")
    if Path(str(transaction.get("project_root") or "")).resolve() != root:
        raise WriteTransactionError("transaction project_root mismatch")
    if transaction.get("run_id") != run_id:
        raise WriteTransactionError("transaction run_id mismatch")
    if (
        type(transaction.get("chapter")) is not int
        or int(transaction["chapter"]) <= 0
        or transaction.get("mode") not in WRITE_MODES
        or not isinstance(transaction.get("parent_model"), str)
        or not str(transaction.get("parent_model") or "").strip()
        or transaction.get("stages") != list(WRITE_STAGES)
        or type(transaction.get("test_only")) is not bool
        or type(transaction.get("production_evidence_required")) is not bool
        or transaction.get("production_evidence_required") is transaction.get("test_only")
        or type(transaction.get("accepted_commit_before")) is not bool
        or type(transaction.get("latest_contract_mtime_ns")) is not int
        or not isinstance(transaction.get("body_before"), Mapping)
    ):
        raise WriteTransactionError("transaction descriptor semantics are invalid")
    _validate_contract_signatures(
        root,
        int(transaction["chapter"]),
        transaction.get("contract_signatures_before"),
    )
    expected_route = build_workflow_route(
        "write",
        parent_model=transaction["parent_model"],
        parent_reasoning_effort=transaction.get("parent_reasoning_effort"),
        mode=transaction["mode"],
    )
    if transaction.get("route") != expected_route:
        raise WriteTransactionError("transaction route no longer matches the managed Agent contract")
    if transaction.get("test_only") is True:
        if (
            transaction.get("parent_task_binding_status") != "test_only"
            or transaction.get("route_readiness_sha256") != "test-only"
            or any(
                transaction.get(field) is not None
                for field in (
                    "parent_thread_id",
                    "parent_rollout_path",
                    "parent_rollout_sha256",
                    "parent_rollout_bytes",
                )
            )
        ):
            raise WriteTransactionError("test-only transaction descriptor is invalid")
    elif (
        transaction.get("parent_task_binding_status") != "verified_current_parent"
        or not _SHA256_RE.fullmatch(str(transaction.get("route_readiness_sha256") or ""))
        or not str(transaction.get("parent_thread_id") or "")
        or not Path(str(transaction.get("parent_rollout_path") or "")).is_absolute()
        or not _SHA256_RE.fullmatch(str(transaction.get("parent_rollout_sha256") or ""))
        or type(transaction.get("parent_rollout_bytes")) is not int
        or int(transaction.get("parent_rollout_bytes") or 0) <= 0
    ):
        raise WriteTransactionError("production transaction evidence binding is invalid")
    if transaction.get("test_only") is True and _test_run_key(root, run_id, claimed) not in _ACTIVE_TEST_RUNS:
        raise WriteTransactionError(
            "test-only transaction is not active in this process and cannot be resumed"
        )
    return transaction


def _receipt_files(run_dir: Path) -> list[Path]:
    try:
        root = run_dir.parents[2]
    except IndexError as exc:
        raise WriteTransactionError("invalid write run directory") from exc
    receipts_dir = run_dir / "receipts"
    _require_safe_path(root, run_dir, must_exist=True)
    _require_safe_path(root, receipts_dir, must_exist=False)
    if not receipts_dir.exists():
        return []
    if not receipts_dir.is_dir():
        raise WriteTransactionError("write receipt path is not a directory")
    paths = sorted(receipts_dir.glob("*.json"))
    for path in paths:
        _require_safe_path(root, path, must_exist=True, regular_file=True)
    return paths


def _validated_receipts(
    run_dir: Path,
    *,
    transaction: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    root = run_dir.parents[2]
    if transaction is None:
        transaction = _load_transaction(root, run_dir.name)
    receipts: list[dict[str, Any]] = []
    previous_hash = ""
    for expected_sequence, path in enumerate(_receipt_files(run_dir), start=1):
        receipt = _read_json(path, trusted_root=root)
        if receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION:
            raise WriteTransactionError(f"unsupported receipt schema: {path}")
        if set(receipt) != {
            "schema_version",
            "run_id",
            "transaction_sha256",
            "sequence",
            "stage",
            "status",
            "created_at",
            "previous_receipt_sha256",
            "details",
            "test_only",
            "receipt_sha256",
        }:
            raise WriteTransactionError(f"receipt fields are invalid: {path}")
        check = dict(receipt)
        claimed = str(check.pop("receipt_sha256", ""))
        if claimed != _receipt_hash(check):
            raise WriteTransactionError(f"receipt hash mismatch: {path}")
        if receipt.get("sequence") != expected_sequence:
            raise WriteTransactionError(f"receipt sequence gap: {path}")
        if path.name != f"{expected_sequence:03d}-{receipt.get('stage')}.json":
            raise WriteTransactionError(f"receipt path is not the fixed stage path: {path}")
        if receipt.get("previous_receipt_sha256") != previous_hash:
            raise WriteTransactionError(f"receipt chain mismatch: {path}")
        if (
            receipt.get("run_id") != transaction.get("run_id")
            or receipt.get("transaction_sha256") != transaction.get("transaction_sha256")
            or receipt.get("test_only") is not transaction.get("test_only")
            or receipt.get("stage") not in WRITE_STAGES
            or receipt.get("status") not in {"completed", "skipped", "failed"}
            or not isinstance(receipt.get("created_at"), str)
            or not isinstance(receipt.get("details"), Mapping)
        ):
            raise WriteTransactionError(f"receipt transaction binding is invalid: {path}")
        previous_hash = claimed
        receipts.append(receipt)
    return receipts


def _derive_progress(transaction: Mapping[str, Any], receipts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    index = 0
    stages = list(transaction.get("stages") or [])
    completed: dict[str, Mapping[str, Any]] = {}
    last_failure: Mapping[str, Any] | None = None
    for receipt in receipts:
        if index >= len(stages):
            raise WriteTransactionError("receipt exists after complete")
        stage = str(receipt.get("stage") or "")
        if stage != stages[index]:
            raise WriteTransactionError(f"receipt stage out of order: expected {stages[index]}, got {stage}")
        status = receipt.get("status")
        if status == "failed":
            last_failure = receipt
            continue
        if status not in {"completed", "skipped"}:
            raise WriteTransactionError(f"invalid stage status: {status}")
        completed[stage] = receipt
        last_failure = None
        index += 1
    return {
        "next_index": index,
        "next_stage": stages[index] if index < len(stages) else None,
        "completed": completed,
        "last_failure": last_failure,
        "last_receipt_sha256": receipts[-1].get("receipt_sha256") if receipts else "",
    }


def _validate_stage_details(
    transaction: Mapping[str, Any],
    progress: Mapping[str, Any],
    stage: str,
    status: str,
    details: Mapping[str, Any],
) -> None:
    mode = transaction.get("mode")
    if status == "skipped":
        if stage in {"reviewer", "review_pipeline"} and mode == "minimal":
            if details.get("code") != "minimal_mode":
                raise WriteTransactionError("minimal review skip requires a fresh minimal_mode receipt")
            return
        if stage == "backup" and details.get("code") in {"skipped_non_git", "no_allowlisted_changes"}:
            return
        raise WriteTransactionError(f"stage may not be skipped: {stage}")
    if status == "failed":
        if not str(details.get("code") or "").strip():
            raise WriteTransactionError("failed stage requires a stable error code")
        return
    if stage in {"preflight", "prewrite", "precommit", "postcommit"}:
        if details.get("gate_ok") is not True:
            raise WriteTransactionError(f"{stage} completion requires gate_ok=true")
    elif stage == "review_pipeline":
        if mode == "minimal":
            raise WriteTransactionError("minimal mode must skip review_pipeline")
        if not _SHA256_RE.fullmatch(str(details.get("review_sha256") or "")):
            raise WriteTransactionError("review_pipeline requires a normalized review hash")
    elif stage == "reviewer" and mode == "minimal":
        raise WriteTransactionError("minimal mode must use the fresh run-bound no-review artifact")
    elif stage == "commit":
        signature = details.get("commit")
        if not isinstance(signature, Mapping) or signature.get("exists") is not True:
            raise WriteTransactionError("commit completion requires a commit file signature")
        if details.get("commit_status") != "accepted":
            raise WriteTransactionError("only an accepted commit may advance")
    elif stage == "projections":
        statuses = details.get("projection_status")
        if not isinstance(statuses, Mapping) or set(statuses) != PROJECTION_WRITERS:
            raise WriteTransactionError("projection receipt must contain exactly five writers")
        if any(value not in {"done", "skipped"} for value in statuses.values()):
            raise WriteTransactionError("all projections must be done or skipped")
    elif stage == "backup":
        if details.get("ok") is not True or details.get("status") not in {"completed", "skipped"}:
            raise WriteTransactionError("backup completion requires a structured success receipt")
    elif stage == "complete":
        if progress.get("next_stage") != "complete":
            raise WriteTransactionError("transaction cannot complete before every prior receipt")


def record_write_stage(
    project_root: str | Path,
    run_id: str,
    *,
    stage: str,
    status: str,
    details: Mapping[str, Any] | None = None,
    _agent_acceptance: bool = False,
    test_only_agent_override: bool = False,
    _verified_stage_token: object | None = None,
) -> dict[str, Any]:
    """Append one immutable receipt after enforcing exact stage order."""

    root = _safe_project_root(project_root)
    if FileLock is None:
        raise WriteTransactionError("filelock is required for write transactions")
    run_dir = _run_dir(root, run_id)
    transaction = _load_transaction(root, run_id)
    if status not in {"completed", "skipped", "failed"}:
        raise WriteTransactionError("invalid stage status")
    if (
        not transaction.get("test_only")
        and stage not in AGENT_STAGES
        and _verified_stage_token is not _VERIFIED_STAGE_TOKEN
    ):
        raise WriteTransactionError(
            "production runtime stages require a verified request and truth-source readback"
        )
    if stage in AGENT_STAGES and not _agent_acceptance:
        if not (transaction.get("test_only") and test_only_agent_override):
            raise WriteTransactionError("agent stages require M3 runtime and payload acceptance")
    details_dict = dict(details or {})
    lock_path = run_dir / "transaction.lock"
    _require_safe_path(root, lock_path, must_exist=False, regular_file=True)
    try:
        with FileLock(str(lock_path), timeout=10):
            _require_safe_path(root, lock_path, must_exist=True, regular_file=True)
            transaction = _load_transaction(root, run_id)
            if not transaction.get("test_only"):
                if transaction.get("parent_task_binding_status") != "verified_current_parent":
                    raise WriteTransactionError(
                        "production stage is blocked until current-parent rollout evidence is verified"
                    )
                _assert_current_parent_binding(transaction)
            receipts, progress = _replayed_progress(root, transaction)
            if progress["next_stage"] != stage:
                completed = progress["completed"].get(stage)
                if completed and completed.get("status") == status and completed.get("details") == details_dict:
                    return dict(completed)
                raise WriteTransactionError(
                    f"stage out of order: expected {progress['next_stage']}, got {stage}"
                )
            if stage == "complete" and status == "completed":
                audit = _audit_current_truth(root, transaction, progress)
                if audit["ok"] is not True:
                    raise WriteTransactionError(
                        "transaction truth is stale or incomplete: " + "; ".join(audit["problems"])
                    )
                details_dict = {
                    **details_dict,
                    "verified": True,
                    "truth_audit_sha256": _sha256_bytes(_canonical_bytes(audit)),
                }
            _validate_stage_details(transaction, progress, stage, status, details_dict)
            sequence = len(receipts) + 1
            receipt = {
                "schema_version": RECEIPT_SCHEMA_VERSION,
                "run_id": run_id,
                "transaction_sha256": transaction["transaction_sha256"],
                "sequence": sequence,
                "stage": stage,
                "status": status,
                "created_at": _now_iso(),
                "previous_receipt_sha256": progress["last_receipt_sha256"],
                "details": details_dict,
                "test_only": bool(transaction.get("test_only")),
            }
            receipt["receipt_sha256"] = _receipt_hash(receipt)
            if not transaction.get("test_only"):
                _replay_completed_receipts(
                    root,
                    transaction,
                    [*receipts, receipt],
                    candidate_receipt=receipt,
                )
            path = run_dir / "receipts" / f"{sequence:03d}-{stage}.json"
            _write_json_once(root, path, receipt)
            return receipt
    except Timeout as exc:
        raise WriteTransactionError("write transaction lock is busy") from exc


def _expected_route_step(transaction: Mapping[str, Any], agent_name: str) -> Mapping[str, Any]:
    steps = (transaction.get("route") or {}).get("steps") if isinstance(transaction.get("route"), Mapping) else []
    for step in steps or []:
        if isinstance(step, Mapping) and step.get("agent_name") == agent_name:
            return step
    raise WriteTransactionError(f"required agent is not present in the route: {agent_name}")


def _artifact_pairs(items: Sequence[Mapping[str, Any]]) -> set[tuple[str, str]]:
    return {
        (str(item.get("path") or ""), str(item.get("sha256") or ""))
        for item in items
    }


def _receipt_details(
    progress: Mapping[str, Any],
    stage: str,
) -> Mapping[str, Any]:
    completed = progress.get("completed")
    receipt = completed.get(stage) if isinstance(completed, Mapping) else None
    details = receipt.get("details") if isinstance(receipt, Mapping) else None
    if not isinstance(details, Mapping):
        raise WriteTransactionError(f"{stage} receipt is missing current-run lineage details")
    return details


def _decision_options(kind: str, *, replace_allowed: bool = True) -> list[dict[str, Any]]:
    if kind == TARGETED_FIX_DECISION_KIND:
        return [
            {
                "id": "targeted_fix",
                "label": "定点修复",
                "description": "由受管 writer 逐项修复全部 blocking issue，再继续事务。",
                "recommended": True,
            },
            {
                "id": "report_only",
                "label": "仅保留审查",
                "description": "保留本轮审查证据并停止，不生成可提交正文。",
                "recommended": False,
            },
            {
                "id": "abandon",
                "label": "放弃本轮",
                "description": "停止当前写章事务，正文与事实数据保持不变。",
                "recommended": False,
            },
        ]
    options = [
        {
            "id": "keep_current",
            "label": "保留当前正文",
            "description": "不提升本轮 staging 终稿，保留作者当前文件。",
            "recommended": True,
        }
    ]
    if replace_allowed:
        options.append(
            {
                "id": "replace_with_verified",
                "label": "替换为已验证终稿",
                "description": "仅覆盖此冲突快照，并继续受管提交事务。",
                "recommended": False,
            }
        )
    else:
        options.append(
            {
                "id": "status_only",
                "label": "仅查看状态",
                "description": "本章已有 accepted commit，只报告状态而不改写。",
                "recommended": False,
            }
        )
    options.append(
        {
            "id": "cancel",
            "label": "取消",
            "description": "停止本轮恢复，不修改正文或提交事实。",
            "recommended": False,
        }
    )
    return options


def _blocking_issue_occurrences(
    root: Path,
    transaction: Mapping[str, Any],
    progress: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], Mapping[str, Any], Mapping[str, Any]]:
    review = _receipt_details(progress, "review_pipeline")
    review_spec = review.get("review_artifact")
    if not isinstance(review_spec, Mapping):
        raise WriteTransactionError("blocking decision requires the run-bound review artifact")
    review_path = Path(str(review_spec.get("path") or ""))
    if not _signature_is_current(review_spec, trusted_root=_staging_dir(root, str(transaction["run_id"]))):
        raise WriteTransactionError("run-bound review changed before blocking decision")
    payload = _read_json(review_path, trusted_root=_staging_dir(root, str(transaction["run_id"])))
    issues = payload.get("issues")
    if not isinstance(issues, list):
        raise WriteTransactionError("run-bound review issues are missing")
    occurrences = [
        {"issue_index": index, "issue_sha256": _sha256_bytes(_canonical_bytes(issue))}
        for index, issue in enumerate(issues)
        if isinstance(issue, Mapping) and issue.get("blocking") is True
    ]
    if not occurrences or review.get("blocking_issue_hashes") != [
        item["issue_sha256"] for item in occurrences
    ]:
        raise WriteTransactionError("blocking review occurrence hashes are stale")
    draft = _receipt_details(progress, "writer_draft").get("accepted_artifacts")
    if not isinstance(draft, list) or len(draft) != 1 or not isinstance(draft[0], Mapping):
        raise WriteTransactionError("blocking decision requires one current-run draft")
    return occurrences, dict(review_spec), dict(draft[0])


def _targeted_fix_decision_request(
    root: Path,
    transaction: Mapping[str, Any],
    progress: Mapping[str, Any],
) -> dict[str, Any]:
    occurrences, review, draft = _blocking_issue_occurrences(root, transaction, progress)
    scope = {
        "kind": TARGETED_FIX_DECISION_KIND,
        "project_root": str(root),
        "run_id": transaction["run_id"],
        "transaction_sha256": transaction["transaction_sha256"],
        "chapter": transaction["chapter"],
        "parent_thread_id": transaction["parent_thread_id"],
        "draft": {"path": draft.get("path"), "sha256": draft.get("sha256")},
        "review": {"path": review.get("path"), "sha256": review.get("sha256")},
        "blocking_issues": occurrences,
    }
    try:
        return build_scope_bound_decision_request(
            scope,
            question_id="write_action",
            prompt="本章存在 blocking issue，请选择本次写章事务的唯一处理方式。",
            options=_decision_options(TARGETED_FIX_DECISION_KIND),
            expected_parent_thread_id=str(transaction.get("parent_thread_id") or ""),
            expected_parent_model=str(transaction.get("parent_model") or ""),
            expected_parent_reasoning_effort=str(
                transaction.get("parent_reasoning_effort") or ""
            ),
        )
    except DecisionReceiptError as exc:
        raise WriteTransactionError(f"targeted-fix decision request rejected: {exc}") from exc


def _targeted_fix_request_path(root: Path, run_id: str) -> Path:
    return _staging_dir(root, run_id) / "decisions" / "targeted-fix-request.json"


def _targeted_fix_receipt_path(root: Path, run_id: str) -> Path:
    return _run_dir(root, run_id) / "decisions" / "targeted-fix-receipt.json"


def prepare_targeted_fix_decision(
    project_root: str | Path,
    run_id: str,
) -> dict[str, Any]:
    """Persist the exact parent marker/choice scope for one blocking write review."""

    root = _safe_project_root(project_root)
    transaction = _load_transaction(root, run_id)
    if transaction.get("test_only"):
        raise WriteTransactionError("public targeted-fix decisions require a production transaction")
    _assert_current_parent_binding(transaction)
    _, progress = _replayed_progress(root, transaction)
    if progress.get("next_stage") != "writer_final":
        raise WriteTransactionError("targeted-fix decision is available only before writer_final")
    request = _targeted_fix_decision_request(root, transaction, progress)
    path = _targeted_fix_request_path(root, run_id)
    _write_json_once(root, path, request)
    signature = _file_signature(path, trusted_root=_staging_dir(root, run_id))
    return {
        "status": "choice_required",
        "kind": TARGETED_FIX_DECISION_KIND,
        "decision_request": signature,
        "choice_request": request["choice_request"],
        "binding_marker": request["binding_marker"],
        "blocking_issues": request["scope"]["blocking_issues"],
    }


def record_targeted_fix_decision(
    project_root: str | Path,
    run_id: str,
    request_file: str | Path,
) -> dict[str, Any]:
    """Derive one immutable targeted-fix choice from the trusted parent rollout."""

    root = _safe_project_root(project_root)
    transaction = _load_transaction(root, run_id)
    if transaction.get("test_only"):
        raise WriteTransactionError("public targeted-fix decisions require a production transaction")
    _assert_current_parent_binding(transaction)
    if not Path(request_file).is_absolute():
        raise WriteTransactionError("targeted-fix request-file must be absolute")
    request_path = _absolute_lexical(request_file)
    expected_path = _targeted_fix_request_path(root, run_id)
    if request_path != _absolute_lexical(expected_path):
        raise WriteTransactionError("targeted-fix request-file is not the fixed current-run path")
    request = _read_json(request_path, trusted_root=_staging_dir(root, run_id))
    _, progress = _replayed_progress(root, transaction)
    if request != _targeted_fix_decision_request(root, transaction, progress):
        raise WriteTransactionError("targeted-fix decision scope changed before the user answer")
    try:
        receipt = select_scope_bound_decision(
            request,
            sessions_root=TRUSTED_CODEX_SESSIONS_ROOT,
            rollout_path=str(transaction.get("parent_rollout_path") or ""),
        )
    except DecisionReceiptError as exc:
        raise WriteTransactionError(f"targeted-fix user decision rejected: {exc}") from exc
    receipt_path = _targeted_fix_receipt_path(root, run_id)
    _write_json_once(root, receipt_path, receipt)
    return {
        "status": "selected",
        "kind": TARGETED_FIX_DECISION_KIND,
        "selected": receipt["selected"],
        "decision_receipt": _file_signature(receipt_path, trusted_root=_run_dir(root, run_id)),
    }


def _verified_targeted_fix_decision(
    root: Path,
    transaction: Mapping[str, Any],
    progress: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    request_path = _targeted_fix_request_path(root, str(transaction["run_id"]))
    receipt_path = _targeted_fix_receipt_path(root, str(transaction["run_id"]))
    request = _read_json(request_path, trusted_root=_staging_dir(root, str(transaction["run_id"])))
    receipt = _read_json(receipt_path, trusted_root=_run_dir(root, str(transaction["run_id"])))
    current = _targeted_fix_decision_request(root, transaction, progress)
    if request != current:
        raise WriteTransactionError("targeted-fix decision request is stale")
    try:
        verified = verify_scope_bound_decision_receipt(
            request,
            receipt,
            sessions_root=TRUSTED_CODEX_SESSIONS_ROOT,
            rollout_path=str(transaction.get("parent_rollout_path") or ""),
        )
    except DecisionReceiptError as exc:
        raise WriteTransactionError(f"targeted-fix decision receipt rejected: {exc}") from exc
    if verified.get("selected") != "targeted_fix":
        raise WriteTransactionError("the selected write branch does not authorize targeted_fix")
    return request, verified, _file_signature(receipt_path, trusted_root=_run_dir(root, str(transaction["run_id"])))


def _lineage_pair(spec: object, *, label: str) -> tuple[str, str]:
    if not isinstance(spec, Mapping):
        raise WriteTransactionError(f"{label} lineage artifact is missing")
    path = str(spec.get("path") or "")
    digest = str(spec.get("sha256") or "")
    if not Path(path).is_absolute() or not _SHA256_RE.fullmatch(digest):
        raise WriteTransactionError(f"{label} lineage artifact identity is invalid")
    return path, digest


def _commit_review_spec(
    transaction: Mapping[str, Any],
    progress: Mapping[str, Any],
) -> Mapping[str, Any]:
    review = _receipt_details(progress, "review_pipeline")
    if transaction.get("mode") == "minimal":
        spec = review.get("no_review")
    else:
        final = progress.get("completed", {}).get("writer_final")
        final_details = final.get("details") if isinstance(final, Mapping) else None
        targeted = final_details.get("targeted_fix") if isinstance(final_details, Mapping) else None
        spec = targeted.get("resolved_review") if isinstance(targeted, Mapping) else review.get(
            "review_artifact"
        )
    if not isinstance(spec, Mapping):
        raise WriteTransactionError("run-bound commit review snapshot is missing")
    return spec


def _required_stage_lineage_pairs(
    root: Path,
    run_id: str,
    stage: str,
    transaction: Mapping[str, Any],
    progress: Mapping[str, Any],
) -> set[tuple[str, str]]:
    if stage == "context_agent":
        transaction_signature = _file_signature(
            _run_dir(root, run_id) / "transaction.json",
            trusted_root=root,
        )
        return {_lineage_pair(transaction_signature, label="transaction")}
    if stage == "writer_draft":
        context = _receipt_details(progress, "context_agent")
        bindings = context.get("source_bindings")
        payload = bindings.get("payload") if isinstance(bindings, Mapping) else None
        return {_lineage_pair(payload, label="context")}
    if stage == "reviewer":
        if transaction.get("mode") == "minimal":
            raise WriteTransactionError(
                "minimal mode must use the fresh run-bound no-review artifact"
            )
        draft = _receipt_details(progress, "writer_draft").get("accepted_artifacts")
        if not isinstance(draft, list) or len(draft) != 1:
            raise WriteTransactionError("writer_draft receipt must bind exactly one current-run artifact")
        return {_lineage_pair(draft[0], label="writer draft")}
    if stage == "writer_final":
        draft = _receipt_details(progress, "writer_draft").get("accepted_artifacts")
        if not isinstance(draft, list) or len(draft) != 1:
            raise WriteTransactionError("writer_draft receipt must bind exactly one current-run artifact")
        review = _receipt_details(progress, "review_pipeline")
        review_spec = review.get("no_review") if transaction.get("mode") == "minimal" else review.get(
            "review_artifact"
        )
        required = {
            _lineage_pair(draft[0], label="writer draft"),
            _lineage_pair(review_spec, label="review"),
        }
        if int(review.get("blocking_count") or 0) > 0:
            _, _, decision_signature = _verified_targeted_fix_decision(
                root,
                transaction,
                progress,
            )
            required.add(_lineage_pair(decision_signature, label="targeted-fix decision"))
        return required
    if stage == "data_agent":
        final_artifacts = _receipt_details(progress, "writer_final").get("accepted_artifacts")
        if not isinstance(final_artifacts, list) or len(final_artifacts) != 1:
            raise WriteTransactionError("writer_final receipt must bind exactly one current-run artifact")
        promotion = _receipt_details(progress, "promotion")
        review_spec = _commit_review_spec(transaction, progress)
        return {
            _lineage_pair(final_artifacts[0], label="writer final"),
            _lineage_pair(promotion.get("target"), label="promoted chapter"),
            _lineage_pair(review_spec, label="review"),
        }
    raise WriteTransactionError(f"unsupported Agent lineage stage: {stage}")


def _validate_stage_launch_lineage(
    root: Path,
    run_id: str,
    stage: str,
    transaction: Mapping[str, Any],
    progress: Mapping[str, Any],
    inputs: Sequence[Mapping[str, Any]],
) -> None:
    actual = _artifact_pairs(inputs)
    required = _required_stage_lineage_pairs(root, run_id, stage, transaction, progress)
    missing = sorted(required - actual)
    if missing:
        raise WriteTransactionError(
            f"{stage} launch is missing current-run lineage artifacts: {missing}"
        )
    if stage != "context_agent":
        unexpected = sorted(actual - required)
        if unexpected:
            raise WriteTransactionError(
                f"{stage} launch contains unrelated artifacts outside its exact lineage: {unexpected}"
            )

    input_paths = [_absolute_lexical(path) for path, _ in actual]
    current_control = _absolute_lexical(_run_dir(root, run_id))
    current_staging = _absolute_lexical(_staging_dir(root, run_id))
    run_roots = (
        (_absolute_lexical(root / ".webnovel" / "write-runs"), current_control),
        (_absolute_lexical(root / ".webnovel" / "tmp" / "write-runs"), current_staging),
    )
    for path in input_paths:
        for all_runs, current_run in run_roots:
            try:
                path.relative_to(all_runs)
            except ValueError:
                continue
            try:
                path.relative_to(current_run)
            except ValueError as exc:
                raise WriteTransactionError(
                    f"{stage} launch may not consume another write run artifact: {path}"
                ) from exc


def _verified_writer_manifest_binding(
    root: Path,
    run_id: str,
    stage: str,
    payload: object,
    launch: Mapping[str, Any],
) -> dict[str, Any] | None:
    if stage not in {"writer_draft", "writer_final"}:
        return None
    if not isinstance(payload, Mapping):
        raise WriteTransactionError("writer payload is not an object")
    staging = _absolute_lexical(_staging_dir(root, run_id))
    manifest_path = _absolute_lexical(str(payload.get("manifest_path") or ""))
    expected_path = staging / "manifest.json"
    if manifest_path != expected_path:
        raise WriteTransactionError("writer manifest is not the current-run manifest.json")
    raw, stat_result = _stable_read_snapshot(
        manifest_path,
        trusted_root=staging,
        max_bytes=MAX_CONTROL_BYTES,
    )
    digest = _sha256_bytes(raw)
    if digest != payload.get("manifest_sha256"):
        raise WriteTransactionError("writer manifest changed before lineage binding")
    manifest = _json_object_from_bytes(raw, manifest_path)
    manifest_inputs = manifest.get("inputs")
    launch_inputs = launch.get("input_artifacts")
    if (
        not isinstance(manifest_inputs, list)
        or not isinstance(launch_inputs, list)
        or len(manifest_inputs) != len(launch_inputs)
        or _artifact_pairs(
            [item for item in manifest_inputs if isinstance(item, Mapping)]
        )
        != _artifact_pairs([item for item in launch_inputs if isinstance(item, Mapping)])
    ):
        raise WriteTransactionError("writer manifest inputs do not match the bound launch request")
    return {
        "path": str(manifest_path),
        "exists": True,
        "sha256": digest,
        "bytes": len(raw),
        "mtime_ns": stat_result.st_mtime_ns,
    }


def _snapshot_writer_manifest_binding(
    root: Path,
    run_id: str,
    stage: str,
    payload: object,
    launch: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Copy a mutable writer manifest into immutable, stage-scoped evidence."""

    current = _verified_writer_manifest_binding(root, run_id, stage, payload, launch)
    if current is None:
        return None
    manifest_path = Path(str(current["path"]))
    raw, _ = _stable_read_snapshot(
        manifest_path,
        trusted_root=_staging_dir(root, run_id),
        max_bytes=MAX_CONTROL_BYTES,
    )
    if _sha256_bytes(raw) != current.get("sha256"):
        raise WriteTransactionError("writer manifest changed before immutable evidence binding")
    evidence_path = _staging_dir(root, run_id) / "evidence" / f"{stage}-manifest.json"
    _write_bytes_once(
        root,
        evidence_path,
        raw,
        replace_before_stage=(run_id, stage),
    )
    evidence = _file_signature(
        evidence_path,
        trusted_root=_staging_dir(root, run_id),
    )
    if (
        evidence.get("exists") is not True
        or evidence.get("sha256") != current.get("sha256")
        or evidence.get("bytes") != current.get("bytes")
    ):
        raise WriteTransactionError("writer manifest evidence failed exact readback")
    return evidence


def _targeted_fix_evidence(
    root: Path,
    transaction: Mapping[str, Any],
    progress: Mapping[str, Any],
    *,
    payload: Mapping[str, Any],
    accepted_artifacts: Sequence[Mapping[str, Any]],
    source_bindings: Mapping[str, Any],
    persist: bool,
) -> dict[str, Any]:
    """Bind a trusted choice and exact blocker occurrences to one writer final."""

    request, decision, decision_signature = _verified_targeted_fix_decision(
        root,
        transaction,
        progress,
    )
    expected_occurrences = request.get("scope", {}).get("blocking_issues")
    resolutions = payload.get("resolutions")
    if not isinstance(expected_occurrences, list) or not isinstance(resolutions, list):
        raise WriteTransactionError("targeted_fix lacks exact blocker resolution evidence")
    actual_occurrences = [
        {
            "issue_index": item.get("issue_index"),
            "issue_sha256": item.get("issue_sha256"),
        }
        for item in resolutions
        if isinstance(item, Mapping)
    ]
    if (
        len(actual_occurrences) != len(resolutions)
        or actual_occurrences != expected_occurrences
        or any(
            item.get("status") != "resolved"
            or not str(item.get("resolution_summary") or "").strip()
            for item in resolutions
            if isinstance(item, Mapping)
        )
    ):
        raise WriteTransactionError("targeted_fix resolutions do not cover every blocking issue occurrence")
    if len(accepted_artifacts) != 1 or not isinstance(accepted_artifacts[0], Mapping):
        raise WriteTransactionError("targeted_fix must bind one final writer artifact")
    final = _file_signature(
        Path(str(accepted_artifacts[0].get("path") or "")),
        trusted_root=_staging_dir(root, str(transaction["run_id"])),
    )
    draft_spec = _receipt_details(progress, "writer_draft").get("accepted_artifacts")
    if not isinstance(draft_spec, list) or len(draft_spec) != 1 or not isinstance(draft_spec[0], Mapping):
        raise WriteTransactionError("targeted_fix draft lineage is missing")
    draft = _file_signature(
        Path(str(draft_spec[0].get("path") or "")),
        trusted_root=_staging_dir(root, str(transaction["run_id"])),
    )
    if final.get("exists") is not True or final.get("sha256") == draft.get("sha256"):
        raise WriteTransactionError("targeted_fix final artifact must differ from the reviewed draft")
    review_spec = _receipt_details(progress, "review_pipeline").get("review_artifact")
    if not isinstance(review_spec, Mapping) or not _signature_is_current(
        review_spec,
        trusted_root=_staging_dir(root, str(transaction["run_id"])),
    ):
        raise WriteTransactionError("targeted_fix original review is stale")
    request_path = _targeted_fix_request_path(root, str(transaction["run_id"]))
    request_signature = _file_signature(
        request_path,
        trusted_root=_staging_dir(root, str(transaction["run_id"])),
    )
    payload_binding = source_bindings.get("payload")
    manifest_binding = source_bindings.get("writer_manifest")
    rollout_binding = source_bindings.get("rollout")
    if not all(isinstance(item, Mapping) for item in (payload_binding, manifest_binding, rollout_binding)):
        raise WriteTransactionError("targeted_fix source evidence is incomplete")
    resolution_body = {
        "schema_version": TARGETED_FIX_RESOLUTION_SCHEMA,
        "run_id": transaction["run_id"],
        "transaction_sha256": transaction["transaction_sha256"],
        "chapter": transaction["chapter"],
        "decision_request": request_signature,
        "decision_receipt": decision_signature,
        "decision_receipt_sha256": decision["receipt_sha256"],
        "draft": draft,
        "original_review": dict(review_spec),
        "writer_payload": dict(payload_binding),
        "writer_manifest": dict(manifest_binding),
        "writer_rollout": dict(rollout_binding),
        "final_artifact": final,
        "resolutions": [dict(item) for item in resolutions if isinstance(item, Mapping)],
    }
    resolution = {**resolution_body, "receipt_sha256": _receipt_hash(resolution_body)}
    evidence_root = _staging_dir(root, str(transaction["run_id"])) / "evidence"
    resolution_path = evidence_root / "targeted-fix-resolution.json"
    if persist:
        _write_bytes_once(
            root,
            resolution_path,
            json.dumps(resolution, ensure_ascii=False, indent=2).encode("utf-8"),
            replace_before_stage=(str(transaction["run_id"]), "writer_final"),
        )
    elif _read_json(resolution_path, trusted_root=_staging_dir(root, str(transaction["run_id"]))) != resolution:
        raise WriteTransactionError("targeted_fix resolution receipt changed")
    resolution_signature = _file_signature(
        resolution_path,
        trusted_root=_staging_dir(root, str(transaction["run_id"])),
    )

    original_path = Path(str(review_spec.get("path") or ""))
    original = _read_json(original_path, trusted_root=_staging_dir(root, str(transaction["run_id"])))
    resolved = json.loads(json.dumps(original, ensure_ascii=False))
    issues = resolved.get("issues")
    if not isinstance(issues, list):
        raise WriteTransactionError("targeted_fix original review issues are missing")
    resolved_indexes: set[int] = set()
    for occurrence in expected_occurrences:
        index = int(occurrence["issue_index"])
        if index < 0 or index >= len(issues) or not isinstance(issues[index], dict):
            raise WriteTransactionError("targeted_fix issue occurrence index is stale")
        if _sha256_bytes(_canonical_bytes(issues[index])) != occurrence["issue_sha256"]:
            raise WriteTransactionError("targeted_fix issue occurrence hash is stale")
        resolved_indexes.add(index)
    # The strict review schema requires critical issues to remain blocking.  A
    # commit review therefore contains only still-open issues; the immutable
    # original review and resolution receipt preserve every resolved blocker.
    resolved["issues"] = [
        issue for index, issue in enumerate(issues) if index not in resolved_indexes
    ]
    resolved["issues_count"] = len(resolved["issues"])
    issues = resolved["issues"]
    remaining_by_dimension: dict[str, int] = {}
    for issue in issues:
        if isinstance(issue, Mapping):
            category = str(issue.get("category") or "")
            remaining_by_dimension[category] = remaining_by_dimension.get(category, 0) + 1
    dimensions = resolved.get("dimension_results")
    if not isinstance(dimensions, list):
        raise WriteTransactionError("targeted_fix review dimensions are missing")
    for dimension in dimensions:
        if not isinstance(dimension, dict):
            raise WriteTransactionError("targeted_fix review dimension is invalid")
        name = str(dimension.get("dimension") or "")
        if transaction.get("mode") == "fast" and name in {"character", "logic"}:
            dimension["conclusion"] = "skipped: fast mode"
        elif remaining_by_dimension.get(name, 0) == 0:
            dimension["conclusion"] = "pass"
    remaining = sum(
        1 for issue in issues if isinstance(issue, Mapping) and issue.get("blocking") is True
    )
    resolved["blocking_count"] = remaining
    resolved["has_blocking"] = bool(remaining)
    try:
        parsed = parse_review_output(
            int(transaction["chapter"]),
            resolved,
            review_mode="fast" if transaction.get("mode") == "fast" else "full",
            strict=True,
        ).to_dict()
    except ReviewSchemaError as exc:
        raise WriteTransactionError(f"targeted_fix resolved review is invalid: {exc}") from exc
    if parsed != resolved or remaining != 0:
        raise WriteTransactionError("targeted_fix did not resolve every blocking review issue")
    resolved_path = _staging_dir(root, str(transaction["run_id"])) / "review_results.resolved.json"
    if persist:
        _write_bytes_once(
            root,
            resolved_path,
            json.dumps(resolved, ensure_ascii=False, indent=2).encode("utf-8"),
            replace_before_stage=(str(transaction["run_id"]), "writer_final"),
        )
    elif _read_json(resolved_path, trusted_root=_staging_dir(root, str(transaction["run_id"]))) != resolved:
        raise WriteTransactionError("targeted_fix resolved review changed")
    resolved_signature = _file_signature(
        resolved_path,
        trusted_root=_staging_dir(root, str(transaction["run_id"])),
    )
    return {
        "decision_request": request_signature,
        "decision_receipt": decision_signature,
        "resolution_receipt": resolution_signature,
        "resolved_review": resolved_signature,
        "blocking_issues": expected_occurrences,
    }


def accept_verified_agent_stage(
    project_root: str | Path,
    run_id: str,
    *,
    stage: str,
    envelope: Mapping[str, Any],
    payload: object,
    verified_evidence: VerifiedRuntimeEvidence | None,
    allow_canned: bool = False,
    _source_bindings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Advance an Agent stage only after identity and role output both pass."""

    root = _safe_project_root(project_root)
    transaction = _load_transaction(root, run_id)
    if not transaction.get("test_only") and _source_bindings is None:
        raise WriteTransactionError("production agent acceptance requires the request-file launcher")
    expected_agent = AGENT_STAGES.get(stage)
    if expected_agent is None:
        raise WriteTransactionError(f"not an agent stage: {stage}")
    if allow_canned and not transaction.get("test_only"):
        raise WriteTransactionError("canned evidence is forbidden in production transactions")
    expected_step = _expected_route_step(transaction, expected_agent)
    identity = validate_agent_envelope(
        expected_step,
        envelope,
        allow_canned=allow_canned,
        verified_evidence=verified_evidence,
    )
    if not identity.get("accepted"):
        record_write_stage(
            root,
            run_id,
            stage=stage,
            status="failed",
            details={"code": str(identity.get("code") or "agent_identity_rejected")},
            _agent_acceptance=True,
        )
        raise WriteTransactionError(f"agent identity rejected: {identity.get('code')}")
    payload_result = validate_agent_payload(
        expected_agent,
        payload,
        project_root=root,
        run_id=run_id,
    )
    if not payload_result.get("accepted"):
        record_write_stage(
            root,
            run_id,
            stage=stage,
            status="failed",
            details={"code": str(payload_result.get("code") or "agent_payload_rejected")},
            _agent_acceptance=True,
        )
        raise WriteTransactionError(f"agent payload rejected: {payload_result.get('code')}")

    accepted_artifacts = [
        dict(item) for item in payload_result.get("accepted_artifacts") or [] if isinstance(item, Mapping)
    ]
    envelope_artifacts = [
        dict(item) for item in envelope.get("artifacts") or [] if isinstance(item, Mapping)
    ]
    if accepted_artifacts and _artifact_pairs(accepted_artifacts) != _artifact_pairs(envelope_artifacts):
        record_write_stage(
            root,
            run_id,
            stage=stage,
            status="failed",
            details={"code": "envelope_artifact_mismatch"},
            _agent_acceptance=True,
        )
        raise WriteTransactionError("envelope artifacts do not match role-validated artifacts")

    targeted_fix_details: dict[str, Any] | None = None
    if stage == "writer_draft" and isinstance(payload, Mapping) and payload.get("operation") != "draft":
        raise WriteTransactionError("writer_draft requires operation=draft")
    if stage == "writer_final" and isinstance(payload, Mapping):
        operation = payload.get("operation")
        if operation not in {"targeted_fix", "polish"}:
            raise WriteTransactionError("writer_final requires targeted_fix or polish")
        _, progress = _replayed_progress(root, transaction)
        review_receipt = progress["completed"].get("review_pipeline")
        review_details = review_receipt.get("details") if isinstance(review_receipt, Mapping) else None
        blocking_count = int(review_details.get("blocking_count") or 0) if isinstance(review_details, Mapping) else 0
        if blocking_count == 0 and operation != "polish":
            raise WriteTransactionError("clean review permits only writer_final operation=polish")
        if blocking_count > 0:
            if operation != "targeted_fix":
                raise WriteTransactionError("blocking review permits only targeted_fix")
            if transaction.get("test_only"):
                raise WriteTransactionError(
                    "blocking review decision/resolution receipt requires production parent evidence"
                )
            if not isinstance(_source_bindings, Mapping):
                raise WriteTransactionError("targeted_fix source bindings are missing")
            targeted_fix_details = _targeted_fix_evidence(
                root,
                transaction,
                progress,
                payload=payload,
                accepted_artifacts=accepted_artifacts,
                source_bindings=_source_bindings,
                persist=True,
            )

    evidence_payload = asdict(verified_evidence) if isinstance(verified_evidence, VerifiedRuntimeEvidence) else None
    bound_artifacts: list[dict[str, Any]] = []
    if stage == "data_agent":
        allowed_names = {
            "fulfillment_result.json",
            "disambiguation_result.json",
            "extraction_result.json",
        }
        for artifact in accepted_artifacts:
            source = Path(str(artifact.get("path") or "")).resolve()
            if source.name not in allowed_names or not source.is_file():
                raise WriteTransactionError("data agent artifact set cannot be bound to this run")
            target = _staging_dir(root, run_id) / "commit-inputs" / source.name
            _atomic_write_bytes(
                target,
                _stable_read_bytes(source, trusted_root=root, max_bytes=MAX_CONTROL_BYTES),
                root=root,
            )
            signature = _file_signature(target)
            if signature.get("sha256") != artifact.get("sha256"):
                raise WriteTransactionError("data agent artifact changed while binding")
            bound_artifacts.append(signature)
    details = {
        "agent_name": expected_agent,
        "requested_model": envelope.get("requested_model"),
        "actual_model": envelope.get("actual_model"),
        "requested_reasoning_effort": envelope.get("requested_reasoning_effort"),
        "actual_reasoning_effort": envelope.get("actual_reasoning_effort"),
        "contract_hash": envelope.get("contract_hash"),
        "evidence_source": envelope.get("evidence_source"),
        "evidence_trust": "canned_test_only" if allow_canned else "verified_runtime",
        "verified_evidence": evidence_payload,
        "payload_sha256": _payload_sha256(payload),
        "accepted_artifacts": accepted_artifacts,
        "bound_artifacts": bound_artifacts,
        "operation": payload.get("operation") if isinstance(payload, Mapping) else None,
        "targeted_fix": targeted_fix_details,
        "source_bindings": dict(_source_bindings or {}),
    }
    return record_write_stage(
        root,
        run_id,
        stage=stage,
        status="completed",
        details=details,
        _agent_acceptance=True,
    )


def accept_agent_request(
    project_root: str | Path,
    run_id: str,
    request_file: str | Path,
) -> dict[str, Any]:
    """Accept one agent result from bounded files and an explicit Codex rollout.

    No正文 or user-authored payload is carried in command-line arguments.  The
    rollout parser independently derives model/effort/thread identity before
    the existing envelope and role payload gates run.
    """

    root = _safe_project_root(project_root)
    transaction = _load_transaction(root, run_id)
    if transaction.get("test_only"):
        raise WriteTransactionError("accept-agent is for production runtime evidence only")
    workspace = Path(str(transaction.get("workspace_root") or "")).resolve()
    try:
        readiness = validate_route_readiness(workspace, transaction.get("route") or {})
    except Exception as exc:
        raise WriteTransactionError(f"managed agent readiness recheck failed: {exc}") from exc
    if (
        readiness.get("ready") is not True
        or _sha256_bytes(_canonical_bytes(readiness)) != transaction.get("route_readiness_sha256")
    ):
        raise WriteTransactionError("managed agent route changed or is no longer current")
    _assert_current_parent_binding(transaction)
    request = _load_run_request(root, run_id, request_file)
    request_fields = {key for key in request if not key.startswith("_request_")}
    if request_fields != {
        "schema_version",
        "run_id",
        "stage",
        "rollout",
        "launch_request",
        "payload",
    } or request.get("schema_version") != AGENT_ACCEPT_REQUEST_SCHEMA:
        raise WriteTransactionError("unsupported or malformed agent accept request")
    stage = str(request.get("stage") or "")
    expected_agent = AGENT_STAGES.get(stage)
    if expected_agent is None:
        raise WriteTransactionError("accept-agent request names a non-agent stage")
    expected_step = _expected_route_step(transaction, expected_agent)

    launch, _, launch_signature = _load_agent_launch_request(
        root,
        run_id,
        stage,
        transaction,
        request.get("launch_request"),
    )
    expected_marker = AGENT_PROMPT_MARKER_PREFIX + _canonical_bytes(
        _agent_prompt_marker_payload(launch, launch_signature)
    ).decode("utf-8")
    expected_task_name = _task_name_from_prompt_marker(expected_marker)
    expected_agent_path = f"/root/{expected_task_name}"

    rollout = request.get("rollout")
    if not isinstance(rollout, Mapping) or set(rollout) != {
        "path",
        "thread_id",
        "parent_thread_id",
    }:
        raise WriteTransactionError("rollout request must contain exact path and thread identities")
    rollout_path = Path(str(rollout.get("path") or ""))
    if (
        not rollout_path.is_absolute()
        or not _safe_relative_path(
            TRUSTED_CODEX_SESSIONS_ROOT,
            rollout_path,
            TRUSTED_CODEX_SESSIONS_ROOT,
        )
    ):
        raise WriteTransactionError("rollout must stay under the trusted Codex sessions root")
    rollout_raw = _read_bounded_rollout(rollout_path)
    evidence, final_assistant = _parse_bound_agent_rollout(
        rollout_raw,
        rollout_path=rollout_path,
        thread_id=str(rollout.get("thread_id") or ""),
        parent_thread_id=str(rollout.get("parent_thread_id") or ""),
        expected_agent=expected_agent,
        expected_model=str(expected_step.get("requested_model") or ""),
        expected_effort=str(expected_step.get("requested_reasoning_effort") or ""),
        expected_marker=expected_marker,
        expected_task_name=expected_task_name,
    )
    if evidence.parent_thread_id != transaction.get("parent_thread_id"):
        raise WriteTransactionError("child Agent parent does not match the current write task")
    if _rollout_used_by_other_receipt(
        root,
        run_id,
        rollout_path=str(rollout_path.resolve()),
        thread_id=evidence.thread_id,
        receipt_sequence=-1,
    ):
        raise WriteTransactionError("an Agent rollout may be consumed only once across write runs")

    _, progress = _replayed_progress(root, transaction)
    if progress.get("next_stage") != stage:
        raise WriteTransactionError(f"Agent acceptance is out of order: expected {progress.get('next_stage')}")
    _validate_stage_launch_lineage(
        root,
        run_id,
        stage,
        transaction,
        progress,
        launch.get("input_artifacts") or [],
    )

    staging_root = _staging_dir(root, run_id).resolve()
    payload_path, payload_signature, payload_raw = _request_artifact(
        root,
        run_id,
        request.get("payload"),
        allowed_root=staging_root,
    )
    if payload_raw.startswith(b"\xef\xbb\xbf"):
        raise WriteTransactionError(f"request artifact must be UTF-8 without BOM: {payload_path}")
    try:
        payload_text = payload_raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WriteTransactionError(f"request artifact is not UTF-8: {payload_path}") from exc
    if stage == "context_agent":
        payload: object = payload_text
        if final_assistant != payload_text:
            raise WriteTransactionError("context payload bytes do not match the final rollout assistant text")
    else:
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError as exc:
            raise WriteTransactionError(f"agent payload JSON is invalid: {exc}") from exc
        canonical_payload = _canonical_bytes(payload)
        try:
            rollout_payload = json.loads(final_assistant)
        except json.JSONDecodeError as exc:
            raise WriteTransactionError("final rollout assistant output is not canonical JSON") from exc
        if payload_raw != canonical_payload or _canonical_bytes(rollout_payload) != canonical_payload:
            raise WriteTransactionError(
                "JSON payload bytes must be canonical and exactly match the final rollout assistant output"
            )
    payload_result = validate_agent_payload(
        expected_agent,
        payload,
        project_root=root,
        run_id=run_id,
    )
    if not payload_result.get("accepted"):
        raise WriteTransactionError(f"agent payload rejected: {payload_result.get('code')}")
    manifest_binding = _snapshot_writer_manifest_binding(
        root,
        run_id,
        stage,
        payload,
        launch,
    )
    envelope = build_canned_envelope(
        expected_step,
        evidence_source="codex_trace",
        actual_model=evidence.actual_model,
        actual_reasoning_effort=evidence.actual_reasoning_effort,
        artifacts=[
            dict(item)
            for item in payload_result.get("accepted_artifacts") or []
            if isinstance(item, Mapping)
        ],
    )

    return accept_verified_agent_stage(
        root,
        run_id,
        stage=stage,
        envelope=envelope,
        payload=payload,
        verified_evidence=evidence,
        _source_bindings={
            "request": {
                "path": request["_request_path"],
                "sha256": request["_request_sha256"],
                "bytes": request["_request_bytes"],
            },
            "launch_request": {
                "path": launch_signature["path"],
                "sha256": launch_signature["sha256"],
            },
            "payload": {
                "path": payload_signature["path"],
                "sha256": payload_signature["sha256"],
            },
            "writer_manifest": manifest_binding,
            "rollout": {
                "path": str(rollout_path.resolve()),
                "sha256": evidence.raw_sha256,
                "bytes": len(rollout_raw),
                "thread_id": evidence.thread_id,
                "parent_thread_id": evidence.parent_thread_id,
                "prompt_marker_sha256": _sha256_bytes(expected_marker.encode("utf-8")),
                "agent_task_name": expected_task_name,
                "agent_path": expected_agent_path,
            },
        },
    )


def record_minimal_no_review(
    project_root: str | Path,
    run_id: str,
) -> dict[str, Any]:
    """Create a fresh, run-bound minimal no-review artifact and two skip receipts."""

    root = _safe_project_root(project_root)
    transaction = _load_transaction(root, run_id)
    if transaction.get("mode") != "minimal":
        raise WriteTransactionError("no-review artifact is only valid in minimal mode")
    if not transaction.get("test_only"):
        _assert_current_parent_binding(transaction)
    receipts, progress = _replayed_progress(root, transaction)
    if progress.get("next_stage") not in {"reviewer", "review_pipeline", "writer_final"}:
        raise WriteTransactionError("minimal no-review artifact is out of order")
    draft_receipt = progress["completed"].get("writer_draft")
    artifacts = (draft_receipt or {}).get("details", {}).get("accepted_artifacts", [])
    if not artifacts:
        raise WriteTransactionError("minimal no-review requires an accepted draft artifact")
    draft_sha = str(artifacts[0].get("sha256") or "")
    artifact = {
        "schema_version": NO_REVIEW_SCHEMA_VERSION,
        "run_id": run_id,
        "chapter": transaction["chapter"],
        "review_mode": "minimal",
        "review_skipped": True,
        "source_sha256": draft_sha,
        "issues": [],
        "issues_count": 0,
        "blocking_count": 0,
        "has_blocking": False,
        "summary": "minimal mode: reviewer skipped by explicit mode selection",
    }
    path = _staging_dir(root, run_id) / "no-review.json"
    if path.is_file():
        existing = _read_json(path)
        if existing != artifact:
            raise WriteTransactionError("stale no-review artifact exists for this run")
    else:
        _write_json_once(root, path, artifact)
    signature = _file_signature(path)
    no_review_bytes = _stable_read_bytes(path, trusted_root=root, max_bytes=MAX_CONTROL_BYTES)
    global_review = root / ".webnovel" / "tmp" / "review_results.json"
    current_global = _file_signature(global_review, trusted_root=root)
    if current_global.get("sha256") != signature.get("sha256"):
        _atomic_write_bytes(global_review, no_review_bytes, root=root)
    global_signature = _file_signature(global_review)
    if global_signature.get("sha256") != signature.get("sha256"):
        raise WriteTransactionError("minimal runtime review artifact write did not persist")

    reviewer_receipt = progress["completed"].get("reviewer")
    if reviewer_receipt is None:
        if progress.get("next_stage") != "reviewer":
            raise WriteTransactionError("minimal reviewer receipt is missing from a later stage")
        reviewer_receipt = record_write_stage(
            root,
            run_id,
            stage="reviewer",
            status="skipped",
            details={
                "code": "minimal_mode",
                "no_review": signature,
                "runtime_review": global_signature,
            },
            _agent_acceptance=True,
        )
        receipts, progress = _replayed_progress(root, transaction)
    else:
        reviewer_details = reviewer_receipt.get("details")
        if not isinstance(reviewer_details, Mapping) or _lineage_pair(
            reviewer_details.get("no_review"),
            label="minimal reviewer",
        ) != _lineage_pair(signature, label="minimal artifact"):
            raise WriteTransactionError("minimal reviewer receipt does not bind this no-review artifact")

    pipeline_receipt = progress["completed"].get("review_pipeline")
    if pipeline_receipt is None:
        if progress.get("next_stage") != "review_pipeline":
            raise WriteTransactionError("minimal review pipeline receipt is missing from a later stage")
        pipeline_receipt = record_write_stage(
            root,
            run_id,
            stage="review_pipeline",
            status="skipped",
            details={
                "code": "minimal_mode",
                "no_review": signature,
                "runtime_review": global_signature,
            },
            _verified_stage_token=_VERIFIED_STAGE_TOKEN,
        )
    else:
        pipeline_details = pipeline_receipt.get("details")
        if not isinstance(pipeline_details, Mapping) or _lineage_pair(
            pipeline_details.get("no_review"),
            label="minimal review pipeline",
        ) != _lineage_pair(signature, label="minimal artifact"):
            raise WriteTransactionError(
                "minimal review pipeline receipt does not bind this no-review artifact"
            )
    return {
        "artifact": signature,
        "reviewer_receipt": reviewer_receipt,
        "review_pipeline_receipt": pipeline_receipt,
    }


def _verified_backup_details(
    root: Path,
    transaction: Mapping[str, Any],
    request: Mapping[str, Any],
) -> tuple[str, dict[str, Any]]:
    from backup_manager import (
        BACKUP_RECEIPT_SCHEMA,
        GitBackupManager,
        verify_git_backup_authorization_state,
        verify_git_backup_decision_receipt,
    )

    manager = GitBackupManager(str(root))
    if manager.repository_status == "not_repo":
        if request.get("artifact") is not None:
            raise WriteTransactionError("non-Git backup skip must not trust a supplied receipt")
        return "skipped", {
            "ok": True,
            "status": "skipped",
            "code": "skipped_non_git",
            "project_root": str(root),
            "chapter": transaction["chapter"],
        }
    if manager.repository_status != "exact":
        raise WriteTransactionError(
            f"Git repository probe failed: {manager.repository_error or manager.repository_status}"
        )

    receipt_path, signature, receipt_raw = _request_artifact(
        root,
        str(transaction["run_id"]),
        request.get("artifact"),
        allowed_root=_staging_dir(root, str(transaction["run_id"])),
    )
    receipt = _json_object_from_bytes(receipt_raw, receipt_path)
    if (
        receipt.get("schema_version") != BACKUP_RECEIPT_SCHEMA
        or Path(str(receipt.get("project_root") or "")).resolve() != root
        or receipt.get("chapter") != transaction.get("chapter")
        or receipt.get("ok") is not True
        or receipt.get("status") not in {"completed", "skipped"}
    ):
        raise WriteTransactionError("backup receipt identity/status is invalid")

    allowlist = receipt.get("allowlist")
    if not isinstance(allowlist, list) or not allowlist or not all(isinstance(item, str) for item in allowlist):
        raise WriteTransactionError("backup receipt allowlist is invalid")
    try:
        decision = verify_git_backup_decision_receipt(
            root,
            int(transaction["chapter"]),
            allowlist,
            receipt.get("decision_receipt"),
        )
    except Exception as exc:
        raise WriteTransactionError(f"backup user-decision receipt is invalid: {exc}") from exc
    decision_sha = str(decision.get("receipt_sha256") or "")
    try:
        authorization_state = verify_git_backup_authorization_state(root, decision)
    except Exception as exc:
        raise WriteTransactionError(f"backup authorization registry is invalid: {exc}") from exc
    if (
        receipt.get("decision_receipt_sha256") != decision_sha
        or authorization_state.get("status") != "completed"
        or authorization_state.get("binding", {}).get("receipt_sha256") != decision_sha
        or authorization_state.get("result") != receipt
    ):
        raise WriteTransactionError("backup authorization registry does not complete this exact receipt")
    if receipt["status"] == "completed":
        expected_tag = f"ch{int(transaction['chapter']):04d}"
        if receipt.get("tag") != expected_tag:
            raise WriteTransactionError("backup tag identity is invalid")
        if not _SHA256_RE.fullmatch(str(receipt.get("authorization_token_sha256") or "")):
            raise WriteTransactionError("backup receipt lacks authorization binding")
    elif receipt.get("code") == "no_allowlisted_changes":
        pass
    else:
        raise WriteTransactionError("unsupported Git backup skip receipt")
    return str(receipt["status"]), {**receipt, "receipt_artifact": signature}


def _verified_commit_input_specs(
    root: Path,
    run_id: str,
    transaction: Mapping[str, Any],
    *,
    progress: Mapping[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    if progress is None:
        receipts = _validated_receipts(_run_dir(root, run_id), transaction=transaction)
        progress = _derive_progress(transaction, receipts)
    bound_review = _commit_review_spec(transaction, progress)
    review_path = Path(str(bound_review.get("path") or ""))
    if _file_signature(review_path).get("sha256") != bound_review.get("sha256"):
        raise WriteTransactionError("run-bound review snapshot changed")
    data_receipt = progress["completed"].get("data_agent")
    data_details = (data_receipt or {}).get("details")
    bound_data = data_details.get("bound_artifacts") if isinstance(data_details, Mapping) else None
    if not isinstance(bound_data, list) or len(bound_data) != 3:
        raise WriteTransactionError("run-bound data artifacts are incomplete")
    specs = {"review_results.json": dict(bound_review)}
    expected_names = {
        "fulfillment_result.json",
        "disambiguation_result.json",
        "extraction_result.json",
    }
    seen: set[str] = set()
    for signature in bound_data:
        if not isinstance(signature, Mapping):
            raise WriteTransactionError("run-bound data signature is invalid")
        bound_path = Path(str(signature.get("path") or ""))
        name = bound_path.name
        digest = str(signature.get("sha256") or "")
        if name not in expected_names or _file_signature(bound_path).get("sha256") != digest:
            raise WriteTransactionError("run-bound data artifact changed")
        seen.add(name)
        specs[name] = dict(signature)
    if seen != expected_names:
        raise WriteTransactionError("run-bound data artifact set is invalid")
    return specs


def _verified_commit_input_hashes(
    root: Path,
    run_id: str,
    transaction: Mapping[str, Any],
    *,
    progress: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    return {
        name: str(signature.get("sha256") or "")
        for name, signature in _verified_commit_input_specs(
            root,
            run_id,
            transaction,
            progress=progress,
        ).items()
    }


def _expected_commit_payload(
    root: Path,
    transaction: Mapping[str, Any],
    progress: Mapping[str, Any],
    *,
    projection_status: Mapping[str, str],
) -> tuple[dict[str, Any], dict[str, str]]:
    """Rebuild the accepted commit from the four immutable run-bound inputs."""

    from .chapter_commit_schema import (
        DisambiguationResult,
        ExtractionResult,
        FulfillmentResult,
        ReviewResult,
        normalize_accepted_events,
    )

    run_id = str(transaction["run_id"])
    chapter = int(transaction["chapter"])
    promotion = _receipt_details(progress, "promotion")
    contracts = _replay_recovery_decision(root, transaction, promotion)
    if _contract_signatures(root, chapter) != contracts:
        raise WriteTransactionError("commit contracts changed after their bound snapshot")
    specs = _verified_commit_input_specs(
        root,
        run_id,
        transaction,
        progress=progress,
    )
    payloads: dict[str, dict[str, Any]] = {}
    for name, signature in specs.items():
        path = Path(str(signature.get("path") or ""))
        raw = _stable_read_bytes(
            path,
            trusted_root=_staging_dir(root, run_id),
            max_bytes=MAX_CONTROL_BYTES,
        )
        if _sha256_bytes(raw) != signature.get("sha256"):
            raise WriteTransactionError(f"run-bound commit input changed: {name}")
        payloads[name] = _json_object_from_bytes(raw, path)

    try:
        review = ReviewResult.model_validate(payloads["review_results.json"]).model_dump()
        fulfillment = FulfillmentResult.model_validate(
            payloads["fulfillment_result.json"]
        ).model_dump()
        disambiguation = DisambiguationResult.model_validate(
            payloads["disambiguation_result.json"]
        ).model_dump()
        extraction = ExtractionResult.model_validate(
            payloads["extraction_result.json"]
        ).model_dump()
        extraction["accepted_events"] = normalize_accepted_events(
            chapter,
            extraction.get("accepted_events"),
        )
    except Exception as exc:
        raise WriteTransactionError(f"run-bound commit inputs are invalid: {exc}") from exc
    if (
        int(review.get("blocking_count") or 0) != 0
        or fulfillment.get("missed_nodes")
        or disambiguation.get("pending")
    ):
        raise WriteTransactionError("run-bound inputs cannot produce an accepted commit")

    try:
        contract_refs = {
            "master": Path(str(contracts["master"]["path"])).name,
            "volume": Path(str(contracts["volume"]["path"])).name,
            "chapter": Path(str(contracts["chapter"]["path"])).name,
            "review": Path(str(contracts["review"]["path"])).name,
        }
    except (KeyError, TypeError) as exc:
        raise WriteTransactionError("transaction contract references are invalid") from exc
    expected = {
        "meta": {
            "schema_version": "story-system/v1",
            "chapter": chapter,
            "status": "accepted",
        },
        "contract_refs": contract_refs,
        "provenance": {
            "write_fact_role": "chapter_commit",
            "projection_role": "derived_read_models",
            "legacy_state_role": "projection_only",
        },
        "outline_snapshot": {
            "planned_nodes": fulfillment["planned_nodes"],
            "covered_nodes": fulfillment["covered_nodes"],
            "missed_nodes": fulfillment["missed_nodes"],
            "extra_nodes": fulfillment["extra_nodes"],
        },
        "review_result": review,
        "fulfillment_result": fulfillment,
        "disambiguation_result": disambiguation,
        "extraction_result": extraction,
        "projection_status": dict(projection_status),
    }
    return expected, {
        name: str(signature.get("sha256") or "")
        for name, signature in specs.items()
    }


_COMMIT_DETAIL_FIELDS = {
    "commit_status",
    "commit",
    "commit_hash",
    "promotion_body",
    "commit_input_hashes",
    "projection_status",
    "commit_projection_status",
    "projection_run_id",
    "projection_commit_path",
    "projection_commit_hash",
}


def _projection_writer_bindings_are_compatible(
    writers: Mapping[str, Any],
    commit_statuses: Mapping[str, str],
) -> bool:
    if set(writers) != PROJECTION_WRITERS or set(commit_statuses) != PROJECTION_WRITERS:
        return False
    for name in PROJECTION_WRITERS:
        result = writers.get(name)
        if not isinstance(result, Mapping):
            return False
        writer = str(result.get("status") or "")
        committed = str(commit_statuses.get(name) or "")
        if writer in {"done", "skipped"}:
            if committed != writer:
                return False
        elif writer == "failed":
            error = result.get("error")
            if not isinstance(error, str) or not error or committed != f"failed:{error}":
                return False
        elif writer.startswith("failed:"):
            if committed != writer:
                return False
        else:
            return False
    return True


def _projection_run_status_from_writers(writers: Mapping[str, Any]) -> str:
    statuses = {
        str(result.get("status") or "")
        for result in writers.values()
        if isinstance(result, Mapping)
    }
    if any(status == "failed" or status.startswith("failed:") for status in statuses):
        return "failed"
    if statuses and statuses <= {"skipped"}:
        return "skipped"
    return "done"


def _verified_materialized_commit_truth(
    root: Path,
    transaction: Mapping[str, Any],
    progress: Mapping[str, Any],
    *,
    receipt_details: Mapping[str, Any] | None = None,
    allow_projection_advance: bool = False,
) -> dict[str, Any] | None:
    """Bind a materialized commit to this run; mere file existence is never enough."""

    chapter = int(transaction["chapter"])
    accepted_snapshot = _accepted_commit_stable_snapshot(root, chapter)
    if accepted_snapshot is None:
        return None
    accepted = accepted_snapshot["payload"]
    promotion = _receipt_details(progress, "promotion")
    promotion_body = promotion.get("target")
    if not _signature_is_current(promotion_body, trusted_root=root / "正文"):
        raise WriteTransactionError("accepted commit does not bind the current promoted body")
    precommit = _receipt_details(progress, "precommit")
    commit_inputs = _verified_commit_input_hashes(
        root,
        str(transaction["run_id"]),
        transaction,
        progress=progress,
    )
    if precommit.get("commit_input_hashes") != commit_inputs:
        raise WriteTransactionError("accepted commit input hashes do not match precommit")

    commit_path = (
        root / ".story-system" / "commits" / f"chapter_{chapter:03d}.commit.json"
    ).resolve()
    commit_signature = accepted_snapshot["signature"]
    current_hash = accepted_snapshot["commit_hash"]
    latest = latest_projection_run(root, chapter=chapter)
    statuses = projection_status_from_run(latest)
    committed_statuses = (
        {
            str(name): str(status)
            for name, status in latest.get("projection_status", {}).items()
        }
        if isinstance(latest, Mapping) and isinstance(latest.get("projection_status"), Mapping)
        else {}
    )
    required_projection_fields = {
        "schema_version",
        "run_id",
        "created_at",
        "chapter",
        "commit_path",
        "commit_hash",
        "commit_status",
        "status",
        "writers",
        "projection_status",
    }
    if (
        not isinstance(latest, Mapping)
        or not required_projection_fields.issubset(latest)
        or latest.get("schema_version") != "webnovel-projection-log/v1"
        or not str(latest.get("run_id") or "").strip()
        or latest.get("chapter") != chapter
        or not isinstance(latest.get("writers"), Mapping)
        or not _projection_writer_bindings_are_compatible(
            latest["writers"],
            committed_statuses,
        )
        or latest.get("status") != _projection_run_status_from_writers(latest["writers"])
        or Path(str(latest.get("commit_path") or "")).resolve() != commit_path
        or latest.get("commit_hash") != current_hash
        or latest.get("commit_status") != "accepted"
    ):
        raise WriteTransactionError(
            "accepted commit lacks an exact latest projection binding"
        )
    expected, expected_hashes = _expected_commit_payload(
        root,
        transaction,
        progress,
        projection_status=committed_statuses,
    )
    if expected_hashes != commit_inputs or accepted != expected:
        raise WriteTransactionError("accepted commit payload is not the exact run-bound commit")
    if not _signature_is_current(
        commit_signature,
        trusted_root=root / ".story-system" / "commits",
    ):
        raise WriteTransactionError("accepted commit changed during truth verification")

    current = {
        "commit_status": "accepted",
        "commit": commit_signature,
        "commit_hash": current_hash,
        "promotion_body": dict(promotion_body),
        "commit_input_hashes": commit_inputs,
        "projection_status": statuses,
        "commit_projection_status": committed_statuses,
        "projection_run_id": latest.get("run_id"),
        "projection_commit_path": str(commit_path),
        "projection_commit_hash": latest.get("commit_hash"),
    }
    if receipt_details is None:
        return current
    if set(receipt_details) != _COMMIT_DETAIL_FIELDS:
        raise WriteTransactionError("commit receipt details have an invalid exact schema")
    if (
        receipt_details.get("commit_status") != "accepted"
        or receipt_details.get("promotion_body") != current["promotion_body"]
        or receipt_details.get("commit_input_hashes") != current["commit_input_hashes"]
        or Path(str(receipt_details.get("projection_commit_path") or "")).resolve()
        != commit_path
    ):
        raise WriteTransactionError("commit receipt no longer binds this exact write run")

    historical = [
        run
        for run in read_projection_runs(root, chapter=chapter)
        if run.get("run_id") == receipt_details.get("projection_run_id")
    ]
    historical_status = projection_status_from_run(historical[0]) if len(historical) == 1 else {}
    historical_commit_status = (
        {
            str(name): str(status)
            for name, status in historical[0].get("projection_status", {}).items()
        }
        if len(historical) == 1 and isinstance(historical[0].get("projection_status"), Mapping)
        else {}
    )
    if (
        len(historical) != 1
        or not required_projection_fields.issubset(historical[0])
        or historical[0].get("schema_version") != "webnovel-projection-log/v1"
        or historical[0].get("chapter") != chapter
        or historical[0].get("commit_hash") != receipt_details.get("projection_commit_hash")
        or Path(str(historical[0].get("commit_path") or "")).resolve() != commit_path
        or historical[0].get("commit_status") != "accepted"
        or historical_status != receipt_details.get("projection_status")
        or historical_commit_status != receipt_details.get("commit_projection_status")
        or not isinstance(historical[0].get("writers"), Mapping)
        or not _projection_writer_bindings_are_compatible(
            historical[0]["writers"],
            historical_commit_status,
        )
        or historical[0].get("status")
        != _projection_run_status_from_writers(historical[0]["writers"])
        or receipt_details.get("commit_hash") != receipt_details.get("projection_commit_hash")
    ):
        raise WriteTransactionError("commit receipt historical projection binding changed")
    if not allow_projection_advance:
        for field in _COMMIT_DETAIL_FIELDS:
            if receipt_details.get(field) != current.get(field):
                raise WriteTransactionError("commit receipt current truth changed before projections")
    return current


def _sync_commit_review_truth(
    root: Path,
    transaction: Mapping[str, Any],
    progress: Mapping[str, Any],
) -> dict[str, Any]:
    """Publish the run-bound commit review to the legacy runtime handoff path."""

    spec = _commit_review_spec(transaction, progress)
    source = Path(str(spec.get("path") or ""))
    raw = _stable_read_bytes(
        source,
        trusted_root=_staging_dir(root, str(transaction["run_id"])),
        max_bytes=MAX_CONTROL_BYTES,
    )
    if _sha256_bytes(raw) != spec.get("sha256"):
        raise WriteTransactionError("run-bound commit review changed before precommit")
    target = root / ".webnovel" / "tmp" / "review_results.json"
    _atomic_write_bytes(target, raw, root=root)
    current = _file_signature(target, trusted_root=root / ".webnovel" / "tmp")
    if current.get("sha256") != spec.get("sha256"):
        raise WriteTransactionError("runtime commit review handoff failed readback")
    return current


_AGENT_DETAIL_FIELDS = {
    "agent_name",
    "requested_model",
    "actual_model",
    "requested_reasoning_effort",
    "actual_reasoning_effort",
    "contract_hash",
    "evidence_source",
    "evidence_trust",
    "verified_evidence",
    "payload_sha256",
    "accepted_artifacts",
    "bound_artifacts",
    "operation",
    "targeted_fix",
    "source_bindings",
}


def _require_detail_fields(
    stage: str,
    details: Mapping[str, Any],
    expected: set[str],
) -> None:
    if set(details) != expected:
        raise WriteTransactionError(f"{stage} receipt details have an invalid exact schema")


def _signature_binding_matches(
    recorded: object,
    current: Mapping[str, Any],
) -> bool:
    return bool(
        isinstance(recorded, Mapping)
        and recorded.get("path") == current.get("path")
        and recorded.get("exists") is current.get("exists")
        and recorded.get("sha256") == current.get("sha256")
        and recorded.get("bytes") == current.get("bytes")
    )


def _bound_data_payload(
    root: Path,
    run_id: str,
    payload: object,
    details: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(payload, Mapping) or set(payload) != {
        "schema_version",
        "status",
        "run_id",
        "artifacts",
        "pending_count",
        "missed_nodes_count",
        "problems",
        "warnings",
    }:
        raise WriteTransactionError("data Agent payload schema is invalid during replay")
    artifacts = payload.get("artifacts")
    if (
        payload.get("schema_version") != "webnovel-data-result/v1"
        or payload.get("status") not in {"completed", "partial"}
        or payload.get("run_id") != run_id
        or type(payload.get("pending_count")) is not int
        or int(payload.get("pending_count")) < 0
        or type(payload.get("missed_nodes_count")) is not int
        or int(payload.get("missed_nodes_count")) < 0
        or not isinstance(payload.get("problems"), list)
        or not all(isinstance(item, str) for item in payload.get("problems") or [])
        or not isinstance(payload.get("warnings"), list)
        or not all(isinstance(item, str) for item in payload.get("warnings") or [])
        or not isinstance(artifacts, list)
        or len(artifacts) != 3
    ):
        raise WriteTransactionError("data Agent payload semantics are invalid during replay")
    expected_names = {
        "fulfillment_result": "fulfillment_result.json",
        "disambiguation_result": "disambiguation_result.json",
        "extraction_result": "extraction_result.json",
    }
    declared: dict[str, dict[str, Any]] = {}
    for item in artifacts:
        if (
            not isinstance(item, Mapping)
            or set(item) != {"name", "path", "sha256", "bytes"}
            or item.get("name") not in expected_names
            or item.get("name") in declared
            or not _SHA256_RE.fullmatch(str(item.get("sha256") or ""))
            or type(item.get("bytes")) is not int
        ):
            raise WriteTransactionError("data Agent artifact declaration is invalid during replay")
        expected_global = _absolute_lexical(root / ".webnovel" / "tmp" / expected_names[str(item["name"])])
        if _absolute_lexical(str(item.get("path") or "")) != expected_global:
            raise WriteTransactionError("data Agent artifact path changed during replay")
        declared[str(item["name"])] = dict(item)
    bound = details.get("bound_artifacts")
    if not isinstance(bound, list) or len(bound) != 3:
        raise WriteTransactionError("data Agent run-bound artifact set is missing")
    current_bound: list[dict[str, Any]] = []
    documents: dict[str, dict[str, Any]] = {}
    for signature in bound:
        if not isinstance(signature, Mapping):
            raise WriteTransactionError("data Agent run-bound signature is invalid")
        path = _absolute_lexical(str(signature.get("path") or ""))
        expected_root = _absolute_lexical(_staging_dir(root, run_id) / "commit-inputs")
        if path.parent != expected_root or path.name not in expected_names.values():
            raise WriteTransactionError("data Agent run-bound path is invalid")
        raw, stat_result = _stable_read_snapshot(
            path,
            trusted_root=expected_root,
            max_bytes=MAX_CONTROL_BYTES,
        )
        current = {
            "path": str(path),
            "exists": True,
            "sha256": _sha256_bytes(raw),
            "bytes": len(raw),
            "mtime_ns": stat_result.st_mtime_ns,
        }
        if not _signature_binding_matches(signature, current):
            raise WriteTransactionError("data Agent run-bound artifact changed")
        name = next(key for key, filename in expected_names.items() if filename == path.name)
        declared_item = declared[name]
        if (
            declared_item.get("sha256") != current["sha256"]
            or declared_item.get("bytes") != current["bytes"]
        ):
            raise WriteTransactionError("data Agent payload no longer binds the run-bound copy")
        documents[name] = _json_object_from_bytes(raw, path)
        current_bound.append(dict(signature))
    fulfillment = documents.get("fulfillment_result")
    disambiguation = documents.get("disambiguation_result")
    extraction = documents.get("extraction_result")
    fulfillment_fields = {"planned_nodes", "covered_nodes", "missed_nodes", "extra_nodes"}
    extraction_required = {"accepted_events", "state_deltas", "entity_deltas"}
    extraction_allowed = extraction_required | {
        "entities_appeared",
        "scenes",
        "summary_text",
        "chapter_meta",
        "dominant_strand",
    }
    if (
        not isinstance(fulfillment, Mapping)
        or set(fulfillment) != fulfillment_fields
        or any(not isinstance(fulfillment.get(field), list) for field in fulfillment_fields)
        or not isinstance(disambiguation, Mapping)
        or set(disambiguation) != {"pending"}
        or not isinstance(disambiguation.get("pending"), list)
        or not isinstance(extraction, Mapping)
        or not extraction_required.issubset(extraction)
        or not set(extraction).issubset(extraction_allowed)
        or any(not isinstance(extraction.get(field), list) for field in extraction_required)
        or payload.get("pending_count") != len(disambiguation["pending"])
        or payload.get("missed_nodes_count") != len(fulfillment["missed_nodes"])
    ):
        raise WriteTransactionError("data Agent run-bound artifact schema is invalid")
    return [dict(item) for item in artifacts], current_bound


def _rollout_used_by_other_receipt(
    root: Path,
    run_id: str,
    *,
    rollout_path: str,
    thread_id: str,
    receipt_sequence: int,
) -> bool:
    runs_root = root / ".webnovel" / "write-runs"
    if not runs_root.is_dir():
        return False
    for candidate in sorted(runs_root.glob("*")):
        if not candidate.is_dir() or not _valid_run_id(candidate.name):
            continue
        for path in _receipt_files(candidate):
            try:
                other = _read_json(path, trusted_root=root)
            except WriteTransactionError as exc:
                raise WriteTransactionError(
                    f"cannot prove global Agent rollout uniqueness because a receipt is unreadable: {path}"
                ) from exc
            if candidate.name == run_id and other.get("sequence") == receipt_sequence:
                continue
            details = other.get("details") if isinstance(other.get("details"), Mapping) else {}
            bindings = details.get("source_bindings") if isinstance(details, Mapping) else {}
            rollout = bindings.get("rollout") if isinstance(bindings, Mapping) else {}
            if isinstance(rollout, Mapping) and (
                rollout.get("path") == rollout_path or rollout.get("thread_id") == thread_id
            ):
                return True
    return False


def _writer_word_count(text: str) -> int:
    body = text
    if body.startswith("---\n"):
        end = body.find("\n---\n", 4)
        if end >= 0:
            body = body[end + len("\n---\n") :]
    return len(re.sub(r"\s+", "", body))


def _valid_writer_resolutions(value: object, *, operation: object) -> bool:
    """Replay the public Writer v2 resolution contract exactly."""

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
        issue_sha256 = str(item.get("issue_sha256") or "")
        summary = item.get("resolution_summary")
        if (
            type(issue_index) is not int
            or issue_index < 0
            or not _SHA256_RE.fullmatch(issue_sha256)
            or item.get("status") != "resolved"
            or not isinstance(summary, str)
            or not summary.strip()
            or "\x00" in summary
            or len(summary) > MAX_WRITER_RESOLUTION_SUMMARY_CHARS
        ):
            return False
        pair = (issue_index, issue_sha256)
        if issue_index in seen_indexes or pair in seen_pairs:
            return False
        seen_indexes.add(issue_index)
        seen_pairs.add(pair)
    return True


def _replay_writer_payload(
    root: Path,
    transaction: Mapping[str, Any],
    stage: str,
    payload: object,
    launch: Mapping[str, Any],
    recorded_manifest: object,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Replay a writer result against immutable stage manifest evidence."""

    run_id = str(transaction["run_id"])
    if not isinstance(payload, Mapping):
        raise WriteTransactionError(f"{stage} writer payload schema is invalid")
    result_schema = payload.get("schema_version")
    result_v2 = result_schema == "webnovel-writer-result/v2"
    expected_fields = {
        "schema_version",
        "status",
        "run_id",
        "operation",
        "artifacts",
        "manifest_path",
        "manifest_sha256",
        "problems",
        "warnings",
    }
    if result_v2:
        expected_fields.add("resolutions")
    if set(payload) != expected_fields:
        raise WriteTransactionError(f"{stage} writer payload schema is invalid")
    operation = payload.get("operation")
    if (
        result_schema not in {"webnovel-writer-result/v1", "webnovel-writer-result/v2"}
        or payload.get("status") != "completed"
        or payload.get("run_id") != run_id
        or (stage == "writer_draft" and operation != "draft")
        or (stage == "writer_final" and operation not in {"targeted_fix", "polish"})
        or (result_schema == "webnovel-writer-result/v1" and operation == "targeted_fix")
        or (result_v2 and not _valid_writer_resolutions(payload.get("resolutions"), operation=operation))
        or not isinstance(payload.get("problems"), list)
        or not all(isinstance(item, str) for item in payload.get("problems") or [])
        or not isinstance(payload.get("warnings"), list)
        or not all(isinstance(item, str) for item in payload.get("warnings") or [])
    ):
        raise WriteTransactionError(f"{stage} writer payload semantics are invalid")
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) != 1:
        raise WriteTransactionError(f"{stage} writer artifact set is invalid")
    artifact = artifacts[0]
    expected_name = "draft.md" if operation == "draft" else "polished.md"
    expected_kind = "draft" if operation == "draft" else "polished"
    expected_path = _absolute_lexical(_staging_dir(root, run_id) / expected_name)
    if (
        not isinstance(artifact, Mapping)
        or set(artifact) != {"kind", "path", "sha256", "bytes", "word_count"}
        or artifact.get("kind") != expected_kind
        or _absolute_lexical(str(artifact.get("path") or "")) != expected_path
        or not _SHA256_RE.fullmatch(str(artifact.get("sha256") or ""))
        or type(artifact.get("bytes")) is not int
        or int(artifact.get("bytes")) < 0
        or type(artifact.get("word_count")) is not int
        or int(artifact.get("word_count")) < 0
    ):
        raise WriteTransactionError(f"{stage} writer artifact declaration is invalid")
    raw, _ = _stable_read_snapshot(
        expected_path,
        trusted_root=_staging_dir(root, run_id),
        max_bytes=MAX_CONTROL_BYTES,
    )
    try:
        if raw.startswith(b"\xef\xbb\xbf"):
            raise UnicodeDecodeError("utf-8", raw, 0, 3, "BOM is forbidden")
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WriteTransactionError(f"{stage} writer artifact is not UTF-8/no-BOM") from exc
    if (
        artifact.get("sha256") != _sha256_bytes(raw)
        or artifact.get("bytes") != len(raw)
        or artifact.get("word_count") != _writer_word_count(text)
    ):
        raise WriteTransactionError(f"{stage} writer artifact changed after acceptance")

    staging = _absolute_lexical(_staging_dir(root, run_id))
    expected_manifest_path = staging / "manifest.json"
    expected_evidence_path = staging / "evidence" / f"{stage}-manifest.json"
    if (
        payload.get("manifest_path") != str(expected_manifest_path)
        or not _SHA256_RE.fullmatch(str(payload.get("manifest_sha256") or ""))
        or not isinstance(recorded_manifest, Mapping)
        or set(recorded_manifest) != {"path", "exists", "sha256", "bytes", "mtime_ns"}
        or _absolute_lexical(str(recorded_manifest.get("path") or ""))
        != expected_evidence_path
    ):
        raise WriteTransactionError(f"{stage} writer manifest binding is invalid")
    manifest_raw, stat_result = _stable_read_snapshot(
        expected_evidence_path,
        trusted_root=staging,
        max_bytes=MAX_CONTROL_BYTES,
    )
    current_manifest = {
        "path": str(expected_evidence_path),
        "exists": True,
        "sha256": _sha256_bytes(manifest_raw),
        "bytes": len(manifest_raw),
        "mtime_ns": stat_result.st_mtime_ns,
    }
    if (
        not _signature_binding_matches(recorded_manifest, current_manifest)
        or payload.get("manifest_sha256") != current_manifest["sha256"]
    ):
        raise WriteTransactionError(f"{stage} immutable writer manifest changed")
    manifest = _json_object_from_bytes(manifest_raw, expected_evidence_path)
    manifest_inputs = manifest.get("inputs")
    launch_inputs = launch.get("input_artifacts")
    manifest_fields = {
        "schema_version",
        "run_id",
        "agent_name",
        "operation",
        "status",
        "inputs",
        "outputs",
        "problems",
        "warnings",
    }
    if result_v2:
        manifest_fields.add("resolutions")
    if (
        set(manifest) != manifest_fields
        or manifest.get("schema_version")
        != ("webnovel-writer-manifest/v2" if result_v2 else "webnovel-writer-manifest/v1")
        or manifest.get("run_id") != run_id
        or manifest.get("agent_name") != "webnovel_writer"
        or manifest.get("operation") != operation
        or manifest.get("status") != "completed"
        or manifest.get("outputs") != artifacts
        or manifest.get("problems") != payload.get("problems")
        or manifest.get("warnings") != payload.get("warnings")
        or (result_v2 and manifest.get("resolutions") != payload.get("resolutions"))
        or not isinstance(manifest_inputs, list)
        or not isinstance(launch_inputs, list)
        or len(manifest_inputs) != len(launch_inputs)
        or any(
            not isinstance(item, Mapping) or set(item) != {"path", "sha256"}
            for item in manifest_inputs
        )
        or _artifact_pairs(
            [dict(item) for item in manifest_inputs if isinstance(item, Mapping)]
        )
        != _artifact_pairs(
            [dict(item) for item in launch_inputs if isinstance(item, Mapping)]
        )
    ):
        raise WriteTransactionError(f"{stage} immutable writer manifest semantics are invalid")
    return [dict(artifact)], dict(recorded_manifest)


def _replay_agent_receipt(
    root: Path,
    transaction: Mapping[str, Any],
    receipt: Mapping[str, Any],
    progress_before: Mapping[str, Any],
) -> None:
    stage = str(receipt["stage"])
    run_id = str(transaction["run_id"])
    details = receipt.get("details")
    if not isinstance(details, Mapping):
        raise WriteTransactionError(f"{stage} receipt details are missing")
    _require_detail_fields(stage, details, _AGENT_DETAIL_FIELDS)
    expected_agent = AGENT_STAGES[stage]
    expected_step = _expected_route_step(transaction, expected_agent)
    bindings = details.get("source_bindings")
    if not isinstance(bindings, Mapping) or set(bindings) != {
        "request",
        "launch_request",
        "payload",
        "writer_manifest",
        "rollout",
    }:
        raise WriteTransactionError(f"{stage} source bindings are invalid")
    request_binding = bindings.get("request")
    if not isinstance(request_binding, Mapping) or set(request_binding) != {"path", "sha256", "bytes"}:
        raise WriteTransactionError(f"{stage} request binding is invalid")
    request = _load_run_request(root, run_id, str(request_binding.get("path") or ""))
    if (
        request.get("_request_sha256") != request_binding.get("sha256")
        or request.get("_request_bytes") != request_binding.get("bytes")
        or request.get("stage") != stage
    ):
        raise WriteTransactionError(f"{stage} accept request changed after acceptance")
    launch, _, launch_signature = _load_agent_launch_request(
        root,
        run_id,
        stage,
        transaction,
        request.get("launch_request"),
    )
    launch_binding = bindings.get("launch_request")
    if (
        not isinstance(launch_binding, Mapping)
        or set(launch_binding) != {"path", "sha256"}
        or any(
            launch_binding.get(key) != launch_signature.get(key)
            for key in ("path", "sha256")
        )
    ):
        raise WriteTransactionError(f"{stage} launch request changed after acceptance")
    _validate_stage_launch_lineage(
        root,
        run_id,
        stage,
        transaction,
        progress_before,
        launch.get("input_artifacts") or [],
    )
    marker = AGENT_PROMPT_MARKER_PREFIX + _canonical_bytes(
        _agent_prompt_marker_payload(launch, launch_signature)
    ).decode("utf-8")
    expected_task_name = _task_name_from_prompt_marker(marker)
    expected_agent_path = f"/root/{expected_task_name}"
    rollout_request = request.get("rollout")
    rollout_binding = bindings.get("rollout")
    required_rollout_fields = {
        "path",
        "sha256",
        "bytes",
        "thread_id",
        "parent_thread_id",
        "prompt_marker_sha256",
        "agent_task_name",
        "agent_path",
    }
    if not isinstance(rollout_request, Mapping) or not isinstance(rollout_binding, Mapping):
        raise WriteTransactionError(f"{stage} rollout binding is invalid")
    if set(rollout_binding) != required_rollout_fields:
        raise WriteTransactionError(f"{stage} rollout binding is invalid")
    if (
        rollout_binding.get("thread_id") != rollout_request.get("thread_id")
        or rollout_binding.get("parent_thread_id") != rollout_request.get("parent_thread_id")
        or rollout_binding.get("prompt_marker_sha256") != _sha256_bytes(marker.encode("utf-8"))
    ):
        raise WriteTransactionError(f"{stage} rollout binding is invalid")
    if (
        rollout_binding.get("agent_task_name") != expected_task_name
        or rollout_binding.get("agent_path") != expected_agent_path
    ):
        raise WriteTransactionError(f"{stage} rollout task binding is invalid")
    rollout_path = _absolute_lexical(str(rollout_binding.get("path") or ""))
    if rollout_path != _absolute_lexical(str(rollout_request.get("path") or "")):
        raise WriteTransactionError(f"{stage} rollout request path changed")
    current_rollout = _read_bounded_rollout(rollout_path)
    prefix_bytes = rollout_binding.get("bytes")
    if (
        type(prefix_bytes) is not int
        or prefix_bytes <= 0
        or len(current_rollout) < prefix_bytes
        or _sha256_bytes(current_rollout[:prefix_bytes]) != rollout_binding.get("sha256")
    ):
        raise WriteTransactionError(f"{stage} trusted rollout prefix changed")
    evidence, final_assistant = _parse_bound_agent_rollout(
        current_rollout[:prefix_bytes],
        rollout_path=rollout_path,
        thread_id=str(rollout_binding.get("thread_id") or ""),
        parent_thread_id=str(rollout_binding.get("parent_thread_id") or ""),
        expected_agent=expected_agent,
        expected_model=str(expected_step.get("requested_model") or ""),
        expected_effort=str(expected_step.get("requested_reasoning_effort") or ""),
        expected_marker=marker,
        expected_task_name=expected_task_name,
    )
    if evidence.parent_thread_id != transaction.get("parent_thread_id"):
        raise WriteTransactionError(f"{stage} child Agent no longer binds the current parent")
    if _rollout_used_by_other_receipt(
        root,
        run_id,
        rollout_path=str(rollout_path.resolve()),
        thread_id=evidence.thread_id,
        receipt_sequence=int(receipt["sequence"]),
    ):
        raise WriteTransactionError(f"{stage} child rollout is reused by another receipt")
    payload_path, payload_signature, payload_raw = _request_artifact(
        root,
        run_id,
        request.get("payload"),
        allowed_root=_staging_dir(root, run_id),
    )
    payload_binding = bindings.get("payload")
    if (
        not isinstance(payload_binding, Mapping)
        or set(payload_binding) != {"path", "sha256"}
        or any(
            payload_binding.get(key) != payload_signature.get(key)
            for key in ("path", "sha256")
        )
    ):
        raise WriteTransactionError(f"{stage} payload changed after acceptance")
    try:
        payload_text = payload_raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WriteTransactionError(f"{stage} payload is no longer UTF-8") from exc
    if stage == "context_agent":
        payload: object = payload_text
        if final_assistant != payload_text:
            raise WriteTransactionError("context payload no longer matches its Agent rollout")
    else:
        payload = _json_object_from_bytes(payload_raw, payload_path)
        if payload_raw != _canonical_bytes(payload):
            raise WriteTransactionError(f"{stage} payload is no longer canonical JSON")
        try:
            rollout_payload = json.loads(final_assistant)
        except json.JSONDecodeError as exc:
            raise WriteTransactionError(f"{stage} rollout output is no longer JSON") from exc
        if _canonical_bytes(rollout_payload) != payload_raw:
            raise WriteTransactionError(f"{stage} payload no longer matches its Agent rollout")
    if stage == "data_agent":
        accepted_artifacts, bound_artifacts = _bound_data_payload(root, run_id, payload, details)
        manifest_binding = None
    elif stage in {"writer_draft", "writer_final"}:
        accepted_artifacts, manifest_binding = _replay_writer_payload(
            root,
            transaction,
            stage,
            payload,
            launch,
            bindings.get("writer_manifest"),
        )
        bound_artifacts = []
    else:
        payload_result = validate_agent_payload(
            expected_agent,
            payload,
            project_root=root,
            run_id=run_id,
        )
        if payload_result.get("accepted") is not True:
            raise WriteTransactionError(
                f"{stage} payload replay failed: {payload_result.get('code')}"
            )
        accepted_artifacts = [
            dict(item)
            for item in payload_result.get("accepted_artifacts") or []
            if isinstance(item, Mapping)
        ]
        bound_artifacts = []
        manifest_binding = None
    envelope = build_canned_envelope(
        expected_step,
        evidence_source="codex_trace",
        actual_model=evidence.actual_model,
        actual_reasoning_effort=evidence.actual_reasoning_effort,
        artifacts=accepted_artifacts,
    )
    identity = validate_agent_envelope(
        expected_step,
        envelope,
        verified_evidence=evidence,
    )
    if identity.get("accepted") is not True:
        raise WriteTransactionError(f"{stage} runtime identity replay failed")
    expected_source_bindings = {
        "request": dict(request_binding),
        "launch_request": dict(launch_binding),
        "payload": dict(payload_binding),
        "writer_manifest": manifest_binding,
        "rollout": dict(rollout_binding),
    }
    replayed_targeted_fix: dict[str, Any] | None = None
    if (
        stage == "writer_final"
        and isinstance(payload, Mapping)
        and payload.get("operation") == "targeted_fix"
    ):
        replayed_targeted_fix = _targeted_fix_evidence(
            root,
            transaction,
            progress_before,
            payload=payload,
            accepted_artifacts=accepted_artifacts,
            source_bindings=expected_source_bindings,
            persist=False,
        )
    expected_details = {
        "agent_name": expected_agent,
        "requested_model": envelope.get("requested_model"),
        "actual_model": envelope.get("actual_model"),
        "requested_reasoning_effort": envelope.get("requested_reasoning_effort"),
        "actual_reasoning_effort": envelope.get("actual_reasoning_effort"),
        "contract_hash": envelope.get("contract_hash"),
        "evidence_source": envelope.get("evidence_source"),
        "evidence_trust": "verified_runtime",
        "verified_evidence": asdict(evidence),
        "payload_sha256": _payload_sha256(payload),
        "accepted_artifacts": accepted_artifacts,
        "bound_artifacts": bound_artifacts,
        "operation": payload.get("operation") if isinstance(payload, Mapping) else None,
        "targeted_fix": replayed_targeted_fix,
        "source_bindings": expected_source_bindings,
    }
    if dict(details) != expected_details:
        raise WriteTransactionError(f"{stage} receipt does not match replayed evidence")


def _replay_minimal_receipt(
    root: Path,
    transaction: Mapping[str, Any],
    progress_before: Mapping[str, Any],
    stage: str,
    details: Mapping[str, Any],
) -> None:
    _require_detail_fields(stage, details, {"code", "no_review", "runtime_review"})
    if details.get("code") != "minimal_mode":
        raise WriteTransactionError("minimal review receipt code is invalid")
    run_id = str(transaction["run_id"])
    path = _staging_dir(root, run_id) / "no-review.json"
    current = _file_signature(path, trusted_root=_staging_dir(root, run_id))
    if not _signature_binding_matches(details.get("no_review"), current):
        raise WriteTransactionError("minimal no-review artifact changed")
    artifact = _read_json(path, trusted_root=_staging_dir(root, run_id))
    draft = _receipt_details(progress_before, "writer_draft").get("accepted_artifacts")
    if not isinstance(draft, list) or len(draft) != 1:
        raise WriteTransactionError("minimal no-review draft lineage is missing")
    expected = {
        "schema_version": NO_REVIEW_SCHEMA_VERSION,
        "run_id": run_id,
        "chapter": transaction["chapter"],
        "review_mode": "minimal",
        "review_skipped": True,
        "source_sha256": draft[0].get("sha256"),
        "issues": [],
        "issues_count": 0,
        "blocking_count": 0,
        "has_blocking": False,
        "summary": "minimal mode: reviewer skipped by explicit mode selection",
    }
    if artifact != expected:
        raise WriteTransactionError("minimal no-review artifact semantics changed")


def _replay_review_pipeline(
    root: Path,
    transaction: Mapping[str, Any],
    progress_before: Mapping[str, Any],
    details: Mapping[str, Any],
) -> None:
    _require_detail_fields(
        "review_pipeline",
        details,
        {
            "review_sha256",
            "review_artifact",
            "blocking_count",
            "blocking_issue_hashes",
            "resolution_status",
        },
    )
    reviewer = _receipt_details(progress_before, "reviewer")
    bindings = reviewer.get("source_bindings")
    payload_spec = bindings.get("payload") if isinstance(bindings, Mapping) else None
    raw_path, _, raw = _request_artifact(
        root,
        str(transaction["run_id"]),
        payload_spec,
        allowed_root=_staging_dir(root, str(transaction["run_id"])),
    )
    reviewer_payload = _json_object_from_bytes(raw, raw_path)
    try:
        expected = parse_review_output(
            int(transaction["chapter"]),
            reviewer_payload,
            review_mode="fast" if transaction.get("mode") == "fast" else "full",
            strict=True,
        ).to_dict()
    except ReviewSchemaError as exc:
        raise WriteTransactionError(f"review pipeline source replay failed: {exc}") from exc
    bound = _staging_dir(root, str(transaction["run_id"])) / "review_results.json"
    current = _file_signature(bound, trusted_root=_staging_dir(root, str(transaction["run_id"])))
    if not _signature_binding_matches(details.get("review_artifact"), current):
        raise WriteTransactionError("run-bound review artifact changed")
    if _read_json(bound, trusted_root=_staging_dir(root, str(transaction["run_id"]))) != expected:
        raise WriteTransactionError("run-bound review artifact no longer normalizes the reviewer payload")
    blocking_hashes = [
        _sha256_bytes(_canonical_bytes(issue))
        for issue in expected.get("issues") or []
        if isinstance(issue, Mapping) and issue.get("blocking") is True
    ]
    if (
        details.get("review_sha256") != current.get("sha256")
        or details.get("blocking_count") != len(blocking_hashes)
        or details.get("blocking_issue_hashes") != blocking_hashes
        or details.get("resolution_status")
        != ("not_required" if not blocking_hashes else "decision_pending")
    ):
        raise WriteTransactionError("review pipeline receipt semantics changed")


def _replay_recovery_decision(
    root: Path,
    transaction: Mapping[str, Any],
    details: Mapping[str, Any],
) -> Mapping[str, Any]:
    recovery = details.get("recovery_decision")
    if recovery is None:
        expected = transaction.get("contract_signatures_before")
        if not isinstance(expected, Mapping):
            raise WriteTransactionError("transaction contract signatures are missing")
        if _contract_signatures(root, int(transaction["chapter"])) != expected:
            raise WriteTransactionError("contracts changed after promotion")
        return expected
    if not isinstance(recovery, Mapping) or set(recovery) != {
        "selected",
        "scope_sha256",
        "conflict_code",
        "target_before",
        "request",
        "receipt",
    }:
        raise WriteTransactionError("production promotion recovery binding is invalid")
    request_spec = recovery.get("request")
    receipt_spec = recovery.get("receipt")
    if not isinstance(request_spec, Mapping) or not isinstance(receipt_spec, Mapping):
        raise WriteTransactionError("promotion recovery artifacts are missing")
    request_path = Path(str(request_spec.get("path") or ""))
    receipt_path = Path(str(receipt_spec.get("path") or ""))
    current_request = _file_signature(
        request_path,
        trusted_root=_staging_dir(root, str(transaction["run_id"])),
    )
    current_receipt = _file_signature(
        receipt_path,
        trusted_root=_run_dir(root, str(transaction["run_id"])),
    )
    if not _signature_binding_matches(request_spec, current_request) or not _signature_binding_matches(
        receipt_spec,
        current_receipt,
    ):
        raise WriteTransactionError("promotion recovery request or receipt changed")
    request = _read_json(
        request_path,
        trusted_root=_staging_dir(root, str(transaction["run_id"])),
    )
    receipt = _read_json(
        receipt_path,
        trusted_root=_run_dir(root, str(transaction["run_id"])),
    )
    if request_path != _recovery_request_path(
        root,
        str(transaction["run_id"]),
        str(request.get("request_sha256") or ""),
    ) or receipt_path != _recovery_receipt_path(
        root,
        str(transaction["run_id"]),
        str(request.get("request_sha256") or ""),
    ):
        raise WriteTransactionError("promotion recovery artifacts use an invalid path")
    try:
        verified = verify_scope_bound_decision_receipt(
            request,
            receipt,
            sessions_root=TRUSTED_CODEX_SESSIONS_ROOT,
            rollout_path=str(transaction.get("parent_rollout_path") or ""),
        )
    except DecisionReceiptError as exc:
        raise WriteTransactionError(f"promotion recovery receipt replay failed: {exc}") from exc
    scope = request.get("scope")
    expected_scope_fields = {
        "kind",
        "project_root",
        "run_id",
        "transaction_sha256",
        "chapter",
        "parent_thread_id",
        "conflict_code",
        "conflict_message",
        "target",
        "final_artifact",
        "contracts",
        "accepted_commit",
    }
    source = details.get("source")
    target = details.get("target")
    accepted = scope.get("accepted_commit") if isinstance(scope, Mapping) else None
    if (
        not isinstance(scope, Mapping)
        or set(scope) != expected_scope_fields
        or scope.get("kind") != RECOVERY_DECISION_KIND
        or scope.get("project_root") != str(root)
        or scope.get("run_id") != transaction.get("run_id")
        or scope.get("transaction_sha256") != transaction.get("transaction_sha256")
        or scope.get("chapter") != transaction.get("chapter")
        or scope.get("parent_thread_id") != transaction.get("parent_thread_id")
        or verified.get("selected") != "replace_with_verified"
        or recovery.get("selected") != verified.get("selected")
        or recovery.get("scope_sha256") != request.get("scope_sha256")
        or recovery.get("conflict_code") != scope.get("conflict_code")
        or recovery.get("target_before") != scope.get("target")
        or not isinstance(source, Mapping)
        or not isinstance(target, Mapping)
        or not isinstance(scope.get("target"), Mapping)
        or not isinstance(scope.get("final_artifact"), Mapping)
        or scope["final_artifact"].get("path") != source.get("path")
        or scope["final_artifact"].get("sha256") != source.get("sha256")
        or scope.get("target", {}).get("path") != target.get("path")
        or not isinstance(scope.get("contracts"), Mapping)
        or _contract_signatures(root, int(transaction["chapter"]))
        != scope.get("contracts")
        or not isinstance(accepted, Mapping)
        or accepted.get("accepted") is not False
    ):
        raise WriteTransactionError("promotion recovery decision scope is invalid")
    return scope["contracts"]


def _replay_completed_receipts(
    root: Path,
    transaction: Mapping[str, Any],
    receipts: Sequence[Mapping[str, Any]],
    *,
    candidate_receipt: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Re-derive every production stage from its bound evidence and current truth."""

    progress = _derive_progress(transaction, receipts)
    if transaction.get("test_only"):
        return progress
    workspace = Path(str(transaction.get("workspace_root") or ""))
    if not workspace.is_dir():
        raise WriteTransactionError("write workspace is no longer available")
    readiness = validate_route_readiness(workspace, transaction.get("route") or {})
    if (
        readiness.get("ready") is not True
        or _sha256_bytes(_canonical_bytes(readiness)) != transaction.get("route_readiness_sha256")
    ):
        raise WriteTransactionError("managed Agent route changed during receipt replay")
    materialized_commit: dict[str, Any] | None = None
    completed_lineage = progress.get("completed", {})
    has_commit_lineage = all(
        isinstance(completed_lineage, Mapping) and completed_lineage.get(stage)
        for stage in ("promotion", "data_agent", "precommit")
    )
    if has_commit_lineage:
        materialized_commit = _verified_materialized_commit_truth(
            root,
            transaction,
            progress,
        )
    # An accepted file that predates this run, or appears concurrently before
    # this run has commit lineage, is not authorization for any later stage.
    # It can, however, legitimately change the phase reported by the prewrite
    # gate; replay the receipt's invariant identity while leaving every state,
    # body, contract, promotion, and commit binding strict.
    unbound_accepted_commit = (
        not has_commit_lineage
        and _accepted_commit_stable_snapshot(root, int(transaction["chapter"])) is not None
    )
    completed_so_far: list[Mapping[str, Any]] = []
    for receipt in receipts:
        stage = str(receipt.get("stage") or "")
        status = str(receipt.get("status") or "")
        details = receipt.get("details")
        if not isinstance(details, Mapping):
            raise WriteTransactionError(f"{stage} receipt details are invalid")
        progress_before = _derive_progress(transaction, completed_so_far)
        _validate_stage_details(transaction, progress_before, stage, status, details)
        if status == "failed":
            if set(details) not in ({"code"}, {"code", "request_sha256"}):
                raise WriteTransactionError(f"{stage} failure receipt schema is invalid")
            completed_so_far.append(receipt)
            continue
        if stage in AGENT_STAGES:
            _replay_agent_receipt(root, transaction, receipt, progress_before)
        elif stage == "preflight":
            _require_detail_fields(stage, details, {"gate_ok", "state"})
            current_state = _file_signature(root / ".webnovel" / "state.json", trusted_root=root)
            if not _signature_binding_matches(details.get("state"), current_state):
                if (
                    materialized_commit is None
                    or materialized_commit.get("projection_status", {}).get("state") != "done"
                ):
                    raise WriteTransactionError("preflight state changed without an exact commit projection")
        elif stage in {"prewrite", "precommit", "postcommit"}:
            _require_detail_fields(
                stage,
                details,
                {
                    "gate_ok",
                    "gate_schema",
                    "gate_phase",
                    "gate_report_sha256",
                    "commit_input_hashes",
                },
            )
            if not _SHA256_RE.fullmatch(str(details.get("gate_report_sha256") or "")):
                raise WriteTransactionError(f"{stage} gate receipt hash is invalid")
            exact_report_required = True
            relaxed_prewrite = False
            if stage in {"prewrite", "precommit"} and materialized_commit is not None:
                exact_report_required = False
            elif stage == "prewrite" and unbound_accepted_commit:
                exact_report_required = False
            elif stage == "prewrite" and progress.get("completed", {}).get("context_agent"):
                exact_report_required = False
                relaxed_prewrite = True
            elif stage == "postcommit" and progress.get("completed", {}).get("backup"):
                exact_report_required = False
            if exact_report_required or relaxed_prewrite:
                report = run_write_gate(root, chapter=int(transaction["chapter"]), stage=stage)
                if (
                    report.get("ok") is not True
                    or details.get("gate_schema") != report.get("schema_version")
                ):
                    raise WriteTransactionError(f"{stage} gate truth changed")
                if relaxed_prewrite and (
                    report.get("stage") != stage
                    or Path(str(report.get("project_root") or "")).resolve() != root
                    or report.get("chapter") != int(transaction["chapter"])
                ):
                    raise WriteTransactionError("prewrite gate identity changed")
                if exact_report_required and (
                    details.get("gate_phase") != report.get("phase")
                    or details.get("gate_report_sha256")
                    != _sha256_bytes(_canonical_bytes(report))
                ):
                    raise WriteTransactionError(f"{stage} gate truth changed")
            if stage == "precommit":
                expected_inputs = _verified_commit_input_hashes(
                    root,
                    str(transaction["run_id"]),
                    transaction,
                    progress=progress_before,
                )
                if details.get("commit_input_hashes") != expected_inputs:
                    raise WriteTransactionError("precommit receipt input hashes changed")
            elif details.get("commit_input_hashes") != {}:
                raise WriteTransactionError(f"{stage} must not claim commit input hashes")
        elif stage == "reviewer" and status == "skipped":
            _replay_minimal_receipt(root, transaction, progress_before, stage, details)
        elif stage == "review_pipeline" and status == "skipped":
            _replay_minimal_receipt(root, transaction, progress_before, stage, details)
        elif stage == "review_pipeline":
            _replay_review_pipeline(root, transaction, progress_before, details)
        elif stage == "promotion":
            _require_detail_fields(
                stage,
                details,
                {"source", "target", "changed", "owned_recovery", "recovery_decision", "lifecycle_lock"},
            )
            if not _signature_is_current(details.get("source"), trusted_root=_staging_dir(root, str(transaction["run_id"]))):
                raise WriteTransactionError("promotion source is stale")
            if not _signature_is_current(details.get("target"), trusted_root=root / "正文"):
                raise WriteTransactionError("promotion target is stale")
            if details.get("source", {}).get("sha256") != details.get("target", {}).get("sha256"):
                raise WriteTransactionError("promotion source/target hashes differ")
            _replay_recovery_decision(root, transaction, details)
        elif stage == "commit":
            _verified_materialized_commit_truth(
                root,
                transaction,
                progress_before,
                receipt_details=details,
                # Only an already-persisted immutable commit receipt may
                # survive a later projection retry.  A candidate receipt must
                # bind the exact current commit/run at its append point.
                allow_projection_advance=receipt is not candidate_receipt,
            )
        elif stage == "projections":
            _require_detail_fields(
                stage,
                details,
                {"projection_status", "projection_run_id", "projection_commit_hash"},
            )
        elif stage == "backup":
            if details.get("code") == "skipped_non_git":
                _require_detail_fields(stage, details, {"ok", "status", "code", "project_root", "chapter"})
            elif "receipt_artifact" not in details:
                raise WriteTransactionError("backup receipt artifact binding is missing")
        elif stage == "complete":
            _require_detail_fields(stage, details, {"verified", "truth_audit_sha256"})
            audit = _audit_current_truth(root, transaction, progress_before)
            if (
                details.get("verified") is not True
                or details.get("truth_audit_sha256")
                != _sha256_bytes(_canonical_bytes(audit))
            ):
                raise WriteTransactionError("complete receipt truth audit binding is invalid")
        completed_so_far.append(receipt)
    late_progress = _derive_progress(transaction, receipts)
    promotion_index = list(transaction.get("stages") or []).index("promotion")
    if (
        late_progress.get("completed", {}).get("context_agent")
        and not late_progress.get("completed", {}).get("promotion")
        and int(late_progress.get("next_index") or 0) < promotion_index
    ):
        if _contract_signatures(root, int(transaction["chapter"])) != transaction.get(
            "contract_signatures_before"
        ):
            raise WriteTransactionError("write contracts changed before promotion recovery")
        body_before = transaction.get("body_before")
        current_body = _file_signature(find_chapter_file(root, int(transaction["chapter"])))
        if not _signature_binding_matches(body_before, current_body):
            raise WriteTransactionError("chapter body changed before promotion recovery")
    if any(stage in late_progress.get("completed", {}) for stage in ("promotion", "commit", "projections", "postcommit", "backup", "complete")):
        audit = _audit_current_truth(root, transaction, late_progress)
        if audit.get("ok") is not True:
            raise WriteTransactionError(
                "receipt current-truth replay failed: " + "; ".join(audit.get("problems") or [])
            )
    return late_progress


def _replayed_progress(
    root: Path,
    transaction: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    receipts = _validated_receipts(
        _run_dir(root, str(transaction["run_id"])),
        transaction=transaction,
    )
    return receipts, _replay_completed_receipts(root, transaction, receipts)


def record_verified_stage_request(
    project_root: str | Path,
    run_id: str,
    request_file: str | Path,
) -> dict[str, Any]:
    """Reread runtime truth for a non-agent stage before appending a receipt."""

    root = _safe_project_root(project_root)
    transaction = _load_transaction(root, run_id)
    if transaction.get("test_only"):
        raise WriteTransactionError("verified stage requests are production-only")
    _assert_current_parent_binding(transaction)
    request = _load_run_request(root, run_id, request_file)
    fields = {key for key in request if not key.startswith("_request_")}
    if fields != {
        "schema_version",
        "run_id",
        "stage",
        "status",
        "error_code",
        "artifact",
    } or request.get("schema_version") != STAGE_REQUEST_SCHEMA:
        raise WriteTransactionError("unsupported or malformed stage request")
    stage = str(request.get("stage") or "")
    status = str(request.get("status") or "")
    if stage in AGENT_STAGES or stage in {"promotion", "reviewer"}:
        raise WriteTransactionError("this stage must use its dedicated acceptance path")
    if status == "failed":
        code = str(request.get("error_code") or "").strip()
        if not code:
            raise WriteTransactionError("failed stage request requires error_code")
        return record_write_stage(
            root,
            run_id,
            stage=stage,
            status="failed",
            details={"code": code, "request_sha256": request["_request_sha256"]},
            _verified_stage_token=_VERIFIED_STAGE_TOKEN,
        )
    if status != "completed":
        raise WriteTransactionError("successful stage requests must use status=completed")

    chapter = int(transaction["chapter"])
    if stage == "preflight":
        from project_locator import resolve_project_root

        try:
            resolved = resolve_project_root(str(root), cwd=root)
        except FileNotFoundError as exc:
            raise WriteTransactionError(f"preflight project root rejected: {exc}") from exc
        if resolved.resolve() != root:
            raise WriteTransactionError("preflight resolved a different project root")
        details = {"gate_ok": True, "state": _file_signature(root / ".webnovel" / "state.json")}
    elif stage in {"prewrite", "precommit", "postcommit"}:
        if stage == "precommit":
            _, commit_progress = _replayed_progress(root, transaction)
            _sync_commit_review_truth(root, transaction, commit_progress)
            commit_inputs = _verified_commit_input_hashes(
                root,
                run_id,
                transaction,
                progress=commit_progress,
            )
        else:
            commit_inputs = None
        report = run_write_gate(root, chapter=chapter, stage=stage)
        if report.get("ok") is not True:
            raise WriteTransactionError(f"{stage} truth-source gate is blocked")
        if stage == "precommit" and _verified_commit_input_hashes(root, run_id, transaction) != commit_inputs:
            raise WriteTransactionError("commit inputs changed while precommit gate was running")
        details = {
            "gate_ok": True,
            "gate_schema": report.get("schema_version"),
            "gate_phase": report.get("phase"),
            "gate_report_sha256": _sha256_bytes(_canonical_bytes(report)),
            "commit_input_hashes": commit_inputs or {},
        }
    elif stage == "review_pipeline":
        receipts, progress = _replayed_progress(root, transaction)
        reviewer_receipt = progress["completed"].get("reviewer")
        reviewer_details = (reviewer_receipt or {}).get("details")
        bindings = reviewer_details.get("source_bindings") if isinstance(reviewer_details, Mapping) else None
        raw_spec = bindings.get("payload") if isinstance(bindings, Mapping) else None
        raw_path, _, raw_review_bytes = _request_artifact(
            root,
            run_id,
            raw_spec,
            allowed_root=_staging_dir(root, run_id),
        )
        raw_review = _json_object_from_bytes(raw_review_bytes, raw_path)
        artifact_path, supplied, artifact_raw = _request_artifact(
            root,
            run_id,
            request.get("artifact"),
            allowed_root=root / ".webnovel" / "tmp",
        )
        expected = (root / ".webnovel" / "tmp" / "review_results.json").resolve()
        if artifact_path != expected:
            raise WriteTransactionError("review pipeline must use the runtime review_results.json")
        try:
            expected_review = parse_review_output(
                chapter,
                raw_review,
                review_mode="fast" if transaction.get("mode") == "fast" else "full",
                strict=True,
            ).to_dict()
        except ReviewSchemaError as exc:
            raise WriteTransactionError(f"bound reviewer payload is invalid: {exc}") from exc
        artifact_review = _json_object_from_bytes(artifact_raw, artifact_path)
        if artifact_review != expected_review:
            raise WriteTransactionError("review pipeline artifact does not normalize this run reviewer payload")
        bound = _staging_dir(root, run_id) / "review_results.json"
        _atomic_write_bytes(
            bound,
            artifact_raw,
            root=root,
        )
        blocking_issue_hashes = [
            _sha256_bytes(_canonical_bytes(issue))
            for issue in expected_review.get("issues") or []
            if isinstance(issue, Mapping) and issue.get("blocking") is True
        ]
        details = {
            "review_sha256": supplied["sha256"],
            "review_artifact": _file_signature(bound),
            "blocking_count": int(expected_review.get("blocking_count") or 0),
            "blocking_issue_hashes": blocking_issue_hashes,
            "resolution_status": "not_required" if not blocking_issue_hashes else "decision_pending",
        }
    elif stage == "commit":
        _, commit_progress = _replayed_progress(root, transaction)
        details = _verified_materialized_commit_truth(
            root,
            transaction,
            commit_progress,
        )
        if details is None:
            raise WriteTransactionError("exact run-bound chapter commit is missing or not accepted")
    elif stage == "projections":
        commit = _accepted_commit(root, chapter)
        run = latest_projection_run(root, chapter=chapter)
        statuses = projection_status_from_run(run)
        commit_path = root / ".story-system" / "commits" / f"chapter_{chapter:03d}.commit.json"
        if (
            commit is None
            or not isinstance(run, Mapping)
            or set(statuses) != PROJECTION_WRITERS
            or any(value not in {"done", "skipped"} for value in statuses.values())
            or run.get("commit_hash") != commit_hash(commit)
            or Path(str(run.get("commit_path") or "")).resolve() != commit_path.resolve()
            or run.get("commit_status") != "accepted"
        ):
            raise WriteTransactionError("latest projection truth does not match the accepted commit")
        details = {
            "projection_status": statuses,
            "projection_run_id": run.get("run_id"),
            "projection_commit_hash": run.get("commit_hash"),
        }
    elif stage == "backup":
        status, details = _verified_backup_details(root, transaction, request)
        return record_write_stage(
            root,
            run_id,
            stage=stage,
            status=status,
            details=details,
            _verified_stage_token=_VERIFIED_STAGE_TOKEN,
        )
    elif stage == "complete":
        details = {"verified": True}
    else:
        raise WriteTransactionError(f"stage has no truth-source verifier: {stage}")
    return record_write_stage(
        root,
        run_id,
        stage=stage,
        status="completed",
        details=details,
        _verified_stage_token=_VERIFIED_STAGE_TOKEN,
    )


def _recovery_conflict(transaction: Mapping[str, Any], root: Path, target: Path) -> tuple[str, str] | None:
    body_before = transaction.get("body_before") if isinstance(transaction.get("body_before"), Mapping) else {}
    current = _file_signature(target, trusted_root=root)
    if body_before.get("exists"):
        if current.get("sha256") != body_before.get("sha256"):
            return "chapter_file_changed", _RECOVERY_CONFLICT_MESSAGES["chapter_file_changed"]
        if _contract_signatures(root, int(transaction["chapter"])) != transaction.get(
            "contract_signatures_before"
        ):
            return (
                "contracts_changed_after_begin",
                _RECOVERY_CONFLICT_MESSAGES["contracts_changed_after_begin"],
            )
        if int(transaction.get("latest_contract_mtime_ns") or 0) > int(body_before.get("mtime_ns") or 0):
            return "outline_newer_than_draft", _RECOVERY_CONFLICT_MESSAGES["outline_newer_than_draft"]
    elif current.get("exists"):
        return (
            "chapter_file_created_concurrently",
            _RECOVERY_CONFLICT_MESSAGES["chapter_file_created_concurrently"],
        )
    elif _contract_signatures(root, int(transaction["chapter"])) != transaction.get(
        "contract_signatures_before"
    ):
        return (
            "contracts_changed_after_begin",
            _RECOVERY_CONFLICT_MESSAGES["contracts_changed_after_begin"],
        )
    if transaction.get("accepted_commit_before") or _accepted_commit(root, int(transaction["chapter"])):
        return "chapter_already_accepted", _RECOVERY_CONFLICT_MESSAGES["chapter_already_accepted"]
    return None


def _select_final_writer_artifact(
    root: Path,
    run_id: str,
    final_receipt: Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    details = (final_receipt or {}).get("details")
    if not isinstance(details, Mapping) or details.get("operation") not in {"targeted_fix", "polish"}:
        raise WriteTransactionError("final writer receipt must bind a targeted_fix or polish manifest")
    artifacts = details.get("accepted_artifacts")
    if not isinstance(artifacts, list) or len(artifacts) != 1 or not isinstance(artifacts[0], Mapping):
        raise WriteTransactionError("final writer receipt must bind exactly one artifact")
    artifact = artifacts[0]
    expected = _absolute_lexical(_staging_dir(root, run_id) / "polished.md")
    artifact_path = _absolute_lexical(str(artifact.get("path") or ""))
    if artifact.get("kind") != "polished" or artifact_path != expected:
        raise WriteTransactionError("final writer artifact must be the role-validated polished.md")
    _require_safe_path(root, artifact_path, allowed_root=_staging_dir(root, run_id), must_exist=True, regular_file=True)
    return artifact


def _promotion_target(
    root: Path,
    transaction: Mapping[str, Any],
    target_path: str | Path,
    *,
    prepare_directory: bool,
) -> Path:
    target = Path(target_path)
    if not target.is_absolute():
        target = root / target
    target = _absolute_lexical(target)
    manuscript_root = _absolute_lexical(root / "正文")
    if prepare_directory:
        _safe_mkdir_chain(root, manuscript_root)
    if target.parent != manuscript_root or (
        manuscript_root.is_dir()
        and not _safe_relative_path(root, target, manuscript_root)
    ):
        raise WriteTransactionError("promotion target must be a direct Markdown child of 正文/")
    _require_safe_path(
        root,
        target,
        allowed_root=manuscript_root if manuscript_root.is_dir() else root,
        must_exist=False,
        regular_file=True,
    )
    if target.suffix.lower() != ".md":
        raise WriteTransactionError("promotion target must be Markdown")
    chapter = int(transaction["chapter"])
    if not re.search(
        rf"(?:第0*{chapter}章|chapter[_ -]?0*{chapter}\b)",
        target.stem,
        re.IGNORECASE,
    ):
        raise WriteTransactionError("promotion filename does not match the transaction chapter")
    return target


def _accepted_commit_snapshot(root: Path, chapter: int) -> dict[str, Any]:
    path = root / ".story-system" / "commits" / f"chapter_{chapter:03d}.commit.json"
    return {
        "accepted": _accepted_commit(root, chapter) is not None,
        "artifact": _file_signature(
            path,
            trusted_root=root / ".story-system" / "commits"
            if (root / ".story-system" / "commits").is_dir()
            else root,
        ),
    }


def _recovery_decision_request(
    root: Path,
    transaction: Mapping[str, Any],
    progress: Mapping[str, Any],
    target: Path,
) -> dict[str, Any]:
    if progress.get("next_stage") != "promotion":
        raise WriteTransactionError("recovery decision is available only before promotion")
    final_receipt = progress.get("completed", {}).get("writer_final")
    final = _select_final_writer_artifact(root, str(transaction["run_id"]), final_receipt)
    final_signature = _file_signature(
        str(final.get("path") or ""),
        trusted_root=_staging_dir(root, str(transaction["run_id"])),
    )
    conflict = _recovery_conflict(transaction, root, target)
    if conflict is None:
        raise WriteTransactionError("current promotion facts do not require a recovery decision")
    chapter = int(transaction["chapter"])
    accepted = _accepted_commit_snapshot(root, chapter)
    target_signature = _file_signature(
        target,
        trusted_root=root / "正文" if (root / "正文").is_dir() else root,
    )
    scope = {
        "kind": RECOVERY_DECISION_KIND,
        "project_root": str(root),
        "run_id": transaction["run_id"],
        "transaction_sha256": transaction["transaction_sha256"],
        "chapter": chapter,
        "parent_thread_id": transaction["parent_thread_id"],
        "conflict_code": conflict[0],
        "conflict_message": conflict[1],
        "target": target_signature,
        "final_artifact": final_signature,
        "contracts": _contract_signatures(root, chapter),
        "accepted_commit": accepted,
    }
    try:
        return build_scope_bound_decision_request(
            scope,
            question_id="write_recovery_action",
            prompt=f"检测到正文恢复冲突（{conflict[0]}），请选择本次事务的唯一处理方式。",
            options=_decision_options(
                RECOVERY_DECISION_KIND,
                replace_allowed=accepted.get("accepted") is not True,
            ),
            expected_parent_thread_id=str(transaction.get("parent_thread_id") or ""),
            expected_parent_model=str(transaction.get("parent_model") or ""),
            expected_parent_reasoning_effort=str(
                transaction.get("parent_reasoning_effort") or ""
            ),
        )
    except DecisionReceiptError as exc:
        raise WriteTransactionError(f"write recovery decision request rejected: {exc}") from exc


def _recovery_request_path(root: Path, run_id: str, request_sha256: str) -> Path:
    if not _SHA256_RE.fullmatch(request_sha256):
        raise WriteTransactionError("recovery decision request hash is invalid")
    return (
        _staging_dir(root, run_id)
        / "decisions"
        / f"recovery-{request_sha256}-request.json"
    )


def _recovery_receipt_path(root: Path, run_id: str, request_sha256: str) -> Path:
    if not _SHA256_RE.fullmatch(request_sha256):
        raise WriteTransactionError("recovery decision request hash is invalid")
    return (
        _run_dir(root, run_id)
        / "decisions"
        / f"recovery-{request_sha256}-receipt.json"
    )


def prepare_write_recovery_decision(
    project_root: str | Path,
    run_id: str,
    *,
    target_path: str | Path,
) -> dict[str, Any]:
    """Persist an exact, no-manuscript-write recovery choice request."""

    root = _safe_project_root(project_root)
    transaction = _load_transaction(root, run_id)
    if transaction.get("test_only"):
        raise WriteTransactionError("public recovery decisions require a production transaction")
    _assert_current_parent_binding(transaction)
    target = _promotion_target(root, transaction, target_path, prepare_directory=False)
    _, progress = _replayed_progress(root, transaction)
    terminal = _verified_terminal_recovery_decision(root, transaction, progress)
    if terminal is not None:
        raise WriteTransactionError(
            "write recovery already ended this transaction; start a new transaction to change choice"
        )
    request = _recovery_decision_request(root, transaction, progress, target)
    path = _recovery_request_path(root, run_id, str(request["request_sha256"]))
    _write_json_once(root, path, request)
    return {
        "status": "choice_required",
        "kind": RECOVERY_DECISION_KIND,
        "decision_request": _file_signature(path, trusted_root=_staging_dir(root, run_id)),
        "choice_request": request["choice_request"],
        "binding_marker": request["binding_marker"],
        "conflict": {
            "code": request["scope"]["conflict_code"],
            "message": request["scope"]["conflict_message"],
        },
    }


def record_write_recovery_decision(
    project_root: str | Path,
    run_id: str,
    *,
    target_path: str | Path,
    request_file: str | Path,
) -> dict[str, Any]:
    """Derive an immutable recovery receipt from the current trusted parent rollout."""

    root = _safe_project_root(project_root)
    transaction = _load_transaction(root, run_id)
    if transaction.get("test_only"):
        raise WriteTransactionError("public recovery decisions require a production transaction")
    _assert_current_parent_binding(transaction)
    if not Path(request_file).is_absolute():
        raise WriteTransactionError("recovery request-file must be absolute")
    target = _promotion_target(root, transaction, target_path, prepare_directory=False)
    request_path = _absolute_lexical(request_file)
    request = _read_json(request_path, trusted_root=_staging_dir(root, run_id))
    expected_path = _recovery_request_path(
        root,
        run_id,
        str(request.get("request_sha256") or ""),
    )
    if request_path != _absolute_lexical(expected_path):
        raise WriteTransactionError("recovery request-file is not a current-run request path")
    _, progress = _replayed_progress(root, transaction)
    terminal = _verified_terminal_recovery_decision(root, transaction, progress)
    if terminal is not None:
        if (
            str(terminal.get("request_sha256") or "")
            == str(request.get("request_sha256") or "")
            and str(terminal.get("target") or "") == str(target)
        ):
            return {
                "status": "selected",
                "kind": RECOVERY_DECISION_KIND,
                "selected": terminal["selected"],
                "decision_receipt": terminal["receipt"],
            }
        raise WriteTransactionError(
            "write recovery already ended this transaction; start a new transaction to change choice"
        )
    if request != _recovery_decision_request(root, transaction, progress, target):
        raise WriteTransactionError("write recovery scope changed before the user answer")
    try:
        receipt = select_scope_bound_decision(
            request,
            sessions_root=TRUSTED_CODEX_SESSIONS_ROOT,
            rollout_path=str(transaction.get("parent_rollout_path") or ""),
        )
    except DecisionReceiptError as exc:
        raise WriteTransactionError(f"write recovery user decision rejected: {exc}") from exc
    receipt_path = _recovery_receipt_path(root, run_id, str(request["request_sha256"]))
    _write_json_once(root, receipt_path, receipt)
    return {
        "status": "selected",
        "kind": RECOVERY_DECISION_KIND,
        "selected": receipt["selected"],
        "decision_receipt": _file_signature(receipt_path, trusted_root=_run_dir(root, run_id)),
    }


def _verified_recovery_decision(
    root: Path,
    transaction: Mapping[str, Any],
    progress: Mapping[str, Any],
    target: Path,
    decision_receipt: str | Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    if not Path(decision_receipt).is_absolute():
        raise WriteTransactionError("recovery decision-receipt must be absolute")
    receipt_path = _absolute_lexical(decision_receipt)
    receipt = _read_json(receipt_path, trusted_root=_run_dir(root, str(transaction["run_id"])))
    request_sha256 = str(receipt.get("request_sha256") or "")
    expected_receipt = _recovery_receipt_path(
        root,
        str(transaction["run_id"]),
        request_sha256,
    )
    if receipt_path != _absolute_lexical(expected_receipt):
        raise WriteTransactionError("recovery decision-receipt is not a current-run receipt")
    request_path = _recovery_request_path(
        root,
        str(transaction["run_id"]),
        request_sha256,
    )
    request = _read_json(
        request_path,
        trusted_root=_staging_dir(root, str(transaction["run_id"])),
    )
    current = _recovery_decision_request(root, transaction, progress, target)
    if request != current:
        raise WriteTransactionError("write recovery decision is stale under the chapter lock")
    try:
        verified = verify_scope_bound_decision_receipt(
            request,
            receipt,
            sessions_root=TRUSTED_CODEX_SESSIONS_ROOT,
            rollout_path=str(transaction.get("parent_rollout_path") or ""),
        )
    except DecisionReceiptError as exc:
        raise WriteTransactionError(f"write recovery decision receipt rejected: {exc}") from exc
    return (
        request,
        verified,
        _file_signature(request_path, trusted_root=_staging_dir(root, str(transaction["run_id"]))),
        _file_signature(receipt_path, trusted_root=_run_dir(root, str(transaction["run_id"]))),
    )


def _validated_historical_signature(
    value: object,
    *,
    expected_path: Path,
    label: str,
    require_exists: bool | None = None,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise WriteTransactionError(f"{label} signature is not an object")
    exists = value.get("exists")
    required = (
        {"path", "exists", "sha256", "bytes", "mtime_ns"}
        if exists is True
        else {"path", "exists"}
    )
    if (
        set(value) != required
        or type(exists) is not bool
        or (require_exists is not None and exists is not require_exists)
        or not Path(str(value.get("path") or "")).is_absolute()
        or _absolute_lexical(str(value.get("path") or "")) != _absolute_lexical(expected_path)
        or (
            exists is True
            and (
                not _SHA256_RE.fullmatch(str(value.get("sha256") or ""))
                or type(value.get("bytes")) is not int
                or int(value.get("bytes") or 0) < 0
                or type(value.get("mtime_ns")) is not int
                or int(value.get("mtime_ns") or 0) <= 0
            )
        )
    ):
        raise WriteTransactionError(f"{label} signature shape is invalid")
    return dict(value)


def _validated_recovery_request_for_transaction(
    root: Path,
    transaction: Mapping[str, Any],
    progress: Mapping[str, Any],
    request: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate immutable recovery lineage without requiring old mutable facts to remain current."""

    scope = request.get("scope")
    if not isinstance(scope, Mapping):
        raise WriteTransactionError("write recovery request scope is missing")
    chapter = int(transaction["chapter"])
    immutable = {
        "kind": RECOVERY_DECISION_KIND,
        "project_root": str(root),
        "run_id": transaction["run_id"],
        "transaction_sha256": transaction["transaction_sha256"],
        "chapter": chapter,
        "parent_thread_id": transaction["parent_thread_id"],
    }
    if any(scope.get(key) != expected for key, expected in immutable.items()):
        raise WriteTransactionError("write recovery request belongs to another transaction scope")

    conflict_code = str(scope.get("conflict_code") or "")
    if scope.get("conflict_message") != _RECOVERY_CONFLICT_MESSAGES.get(conflict_code):
        raise WriteTransactionError("write recovery request conflict scope is invalid")

    target_value = scope.get("target")
    if not isinstance(target_value, Mapping):
        raise WriteTransactionError("write recovery target scope is missing")
    target_text = str(target_value.get("path") or "")
    if not Path(target_text).is_absolute():
        raise WriteTransactionError("write recovery target scope must be absolute")
    target = _promotion_target(root, transaction, target_text, prepare_directory=False)
    target_signature = _validated_historical_signature(
        target_value,
        expected_path=target,
        label="write recovery target",
    )

    final_receipt = (
        progress.get("completed", {}).get("writer_final")
        if isinstance(progress.get("completed"), Mapping)
        else None
    )
    final_details = final_receipt.get("details") if isinstance(final_receipt, Mapping) else None
    final_artifacts = (
        final_details.get("accepted_artifacts") if isinstance(final_details, Mapping) else None
    )
    if (
        not isinstance(final_details, Mapping)
        or final_details.get("operation") not in {"targeted_fix", "polish"}
        or not isinstance(final_artifacts, list)
        or len(final_artifacts) != 1
        or not isinstance(final_artifacts[0], Mapping)
    ):
        raise WriteTransactionError("write recovery request has no writer-final lineage")
    final_artifact = final_artifacts[0]
    expected_final = _absolute_lexical(_staging_dir(root, str(transaction["run_id"])) / "polished.md")
    if (
        final_artifact.get("kind") != "polished"
        or _absolute_lexical(str(final_artifact.get("path") or "")) != expected_final
        or not _SHA256_RE.fullmatch(str(final_artifact.get("sha256") or ""))
    ):
        raise WriteTransactionError("write recovery writer-final lineage is invalid")
    final_signature = _validated_historical_signature(
        scope.get("final_artifact"),
        expected_path=expected_final,
        label="write recovery final artifact",
        require_exists=True,
    )
    if final_signature["sha256"] != final_artifact["sha256"]:
        raise WriteTransactionError("write recovery final artifact is cross-scope")

    _validate_contract_signatures(root, chapter, scope.get("contracts"))
    accepted = scope.get("accepted_commit")
    if not isinstance(accepted, Mapping) or set(accepted) != {"accepted", "artifact"}:
        raise WriteTransactionError("write recovery accepted-commit scope is invalid")
    if type(accepted.get("accepted")) is not bool:
        raise WriteTransactionError("write recovery accepted-commit state is invalid")
    _validated_historical_signature(
        accepted.get("artifact"),
        expected_path=root / ".story-system" / "commits" / f"chapter_{chapter:03d}.commit.json",
        label="write recovery accepted commit",
    )

    try:
        expected_request = build_scope_bound_decision_request(
            scope,
            question_id="write_recovery_action",
            prompt=f"检测到正文恢复冲突（{conflict_code}），请选择本次事务的唯一处理方式。",
            options=_decision_options(
                RECOVERY_DECISION_KIND,
                replace_allowed=accepted.get("accepted") is not True,
            ),
            expected_parent_thread_id=str(transaction.get("parent_thread_id") or ""),
            expected_parent_model=str(transaction.get("parent_model") or ""),
            expected_parent_reasoning_effort=str(
                transaction.get("parent_reasoning_effort") or ""
            ),
        )
    except DecisionReceiptError as exc:
        raise WriteTransactionError(f"write recovery request rejected: {exc}") from exc
    if dict(request) != expected_request:
        raise WriteTransactionError("write recovery request no longer matches its exact scope")
    return {
        "scope": dict(scope),
        "target": target_signature,
    }


def _verified_terminal_recovery_decision(
    root: Path,
    transaction: Mapping[str, Any],
    progress: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Derive a no-write terminal state from one still-verifiable recovery receipt."""

    if transaction.get("test_only"):
        return None
    run_id = str(transaction["run_id"])
    decisions_dir = _run_dir(root, run_id) / "decisions"
    if not decisions_dir.exists():
        return None
    _require_safe_path(root, decisions_dir, must_exist=True)
    if not decisions_dir.is_dir():
        raise WriteTransactionError("write recovery decisions path is not a directory")
    candidates = sorted(
        (
            entry
            for entry in decisions_dir.iterdir()
            if _RECOVERY_RECEIPT_NAME_RE.fullmatch(entry.name)
        ),
        key=lambda entry: entry.name,
    )
    verified_receipts: list[dict[str, Any]] = []
    for receipt_path in candidates:
        match = _RECOVERY_RECEIPT_NAME_RE.fullmatch(receipt_path.name)
        if match is None:  # kept explicit for type narrowing and fail-closed reviewability
            continue
        request_sha256 = match.group("request_sha256")
        expected_receipt = _recovery_receipt_path(root, run_id, request_sha256)
        if _absolute_lexical(receipt_path) != _absolute_lexical(expected_receipt):
            raise WriteTransactionError("write recovery receipt path is non-canonical")
        receipt = _read_json(receipt_path, trusted_root=_run_dir(root, run_id))
        if receipt.get("request_sha256") != request_sha256:
            raise WriteTransactionError("write recovery receipt filename does not match its request")
        request_path = _recovery_request_path(root, run_id, request_sha256)
        request = _read_json(request_path, trusted_root=_staging_dir(root, run_id))
        if request.get("request_sha256") != request_sha256:
            raise WriteTransactionError("write recovery request filename does not match its content")
        lineage = _validated_recovery_request_for_transaction(
            root,
            transaction,
            progress,
            request,
        )
        try:
            verified = verify_scope_bound_decision_receipt(
                request,
                receipt,
                sessions_root=TRUSTED_CODEX_SESSIONS_ROOT,
                rollout_path=str(transaction.get("parent_rollout_path") or ""),
            )
        except DecisionReceiptError as exc:
            raise WriteTransactionError(
                f"write recovery terminal receipt rejected: {exc}"
            ) from exc
        verified_receipts.append(
            {
                "selected": verified["selected"],
                "scope_sha256": verified["scope_sha256"],
                "request_sha256": verified["request_sha256"],
                "receipt_sha256": verified["receipt_sha256"],
                "target": lineage["target"]["path"],
                "request": _file_signature(
                    request_path,
                    trusted_root=_staging_dir(root, run_id),
                ),
                "receipt": _file_signature(
                    receipt_path,
                    trusted_root=_run_dir(root, run_id),
                ),
            }
        )
    if len(verified_receipts) > 1:
        raise WriteTransactionError(
            "multiple recovery decisions exist; start a new transaction instead of changing choice"
        )
    if not verified_receipts:
        return None
    decision = verified_receipts[0]
    selected = str(decision["selected"])
    if selected not in TERMINAL_RECOVERY_CHOICES:
        return None
    if progress.get("next_stage") != "promotion":
        raise WriteTransactionError("terminal recovery receipt conflicts with an advanced transaction")
    return {
        **decision,
        "status": "cancelled" if selected == "cancel" else "stopped",
        "promotion_completed": False,
        "new_transaction_required_to_change_choice": True,
    }


def promote_verified_writer_artifact(
    project_root: str | Path,
    run_id: str,
    *,
    target_path: str | Path,
    recovery_decision: str | None = None,
    decision_receipt: str | Path | None = None,
) -> dict[str, Any]:
    """Atomically promote the accepted final writer artifact into 正文/."""

    root = _safe_project_root(project_root)
    transaction = _load_transaction(root, run_id)
    target = _promotion_target(root, transaction, target_path, prepare_directory=False)
    manuscript_root = _absolute_lexical(root / "正文")
    chapter = int(transaction["chapter"])
    if decision_receipt is not None and recovery_decision is not None:
        raise WriteTransactionError("use a scoped decision receipt, not two recovery authorities")
    if recovery_decision not in {None, "replace_with_verified"}:
        raise WriteRecoveryChoiceRequired(
            "promotion_not_authorized",
            "选择保留当前正文或只查看状态时不得提升 staging artifact",
        )
    if FileLock is None:
        raise WriteTransactionError("filelock is required for chapter promotion")
    lifecycle_dir = root / ".webnovel" / "write-locks"
    _safe_mkdir_chain(root, lifecycle_dir)
    lifecycle_lock = lifecycle_dir / f"chapter_{chapter:04d}.lock"
    _require_safe_path(root, lifecycle_lock, must_exist=False, regular_file=True)
    try:
        with FileLock(str(lifecycle_lock), timeout=10):
            _require_safe_path(root, lifecycle_lock, must_exist=True, regular_file=True)
            _require_safe_path(
                root,
                target,
                allowed_root=manuscript_root if manuscript_root.is_dir() else root,
                must_exist=False,
                regular_file=True,
            )
            transaction = _load_transaction(root, run_id)
            if not transaction.get("test_only"):
                if transaction.get("parent_task_binding_status") != "verified_current_parent":
                    raise WriteTransactionError(
                        "production promotion is blocked until current-parent rollout evidence is verified"
                    )
                _assert_current_parent_binding(transaction)
            receipts, progress = _replayed_progress(root, transaction)
            if progress.get("next_stage") != "promotion":
                raise WriteTransactionError("promotion is out of order")
            terminal = _verified_terminal_recovery_decision(root, transaction, progress)
            if terminal is not None:
                raise WriteTransactionError(
                    "write recovery ended this transaction without promotion; "
                    "start a new transaction to change choice"
                )
            final_receipt = progress["completed"].get("writer_final")
            artifact = _select_final_writer_artifact(root, run_id, final_receipt)
            source = _absolute_lexical(str(artifact.get("path") or ""))
            expected_source = _absolute_lexical(_staging_dir(root, run_id))
            if not _safe_relative_path(root, source, expected_source) or not source.is_file():
                raise WriteTransactionError("final writer artifact is outside run staging")
            raw, source_stat = _stable_read_snapshot(
                source,
                trusted_root=expected_source,
                max_bytes=MAX_CONTROL_BYTES,
            )
            signature = {
                "path": str(source),
                "exists": True,
                "sha256": _sha256_bytes(raw),
                "bytes": len(raw),
                "mtime_ns": source_stat.st_mtime_ns,
            }
            if signature.get("sha256") != artifact.get("sha256"):
                raise WriteTransactionError("final writer artifact hash changed")

            if transaction.get("accepted_commit_before") or _accepted_commit(root, chapter):
                raise WriteTransactionError(
                    "accepted chapter rewrite is unsupported; use a future amend transaction instead"
                )
            conflict = _recovery_conflict(transaction, root, target)
            target_before = _file_signature(
                target,
                trusted_root=manuscript_root if manuscript_root.is_dir() else root,
            )
            owned_recovery = bool(
                conflict
                and conflict[0] in {"chapter_file_changed", "chapter_file_created_concurrently"}
                and target_before.get("sha256") == signature.get("sha256")
            )
            recovery_binding: dict[str, Any] | str | None = None
            if conflict and not owned_recovery:
                if transaction.get("test_only") and recovery_decision == "replace_with_verified":
                    recovery_binding = recovery_decision
                elif not transaction.get("test_only") and decision_receipt is not None:
                    request, decision, request_signature, receipt_signature = (
                        _verified_recovery_decision(
                            root,
                            transaction,
                            progress,
                            target,
                            decision_receipt,
                        )
                    )
                    if decision.get("selected") != "replace_with_verified":
                        raise WriteRecoveryChoiceRequired(
                            "promotion_not_authorized",
                            "当前用户裁决不允许替换正文",
                            choices=tuple(
                                str(item.get("id"))
                                for item in request.get("choice_request", {})
                                .get("questions", [{}])[0]
                                .get("options", [])
                                if isinstance(item, Mapping)
                            ),
                        )
                    recovery_binding = {
                        "selected": decision["selected"],
                        "scope_sha256": request["scope_sha256"],
                        "conflict_code": request["scope"]["conflict_code"],
                        "target_before": target_before,
                        "request": request_signature,
                        "receipt": receipt_signature,
                    }
                else:
                    # A caller-supplied string never authorizes a production overwrite.
                    raise WriteRecoveryChoiceRequired(*conflict)
            elif decision_receipt is not None:
                raise WriteTransactionError("recovery decision receipt is stale because no conflict remains")

            if _sha256_bytes(raw) != signature.get("sha256"):
                raise WriteTransactionError("final writer artifact changed under the chapter lock")
            before = _file_signature(
                target,
                trusted_root=manuscript_root if manuscript_root.is_dir() else root,
            )
            # Re-read all mutable recovery facts immediately before replace.
            if (
                any(
                    before.get(key) != target_before.get(key)
                    for key in ("exists", "sha256", "bytes", "mtime_ns")
                )
                or (owned_recovery and before.get("sha256") != signature.get("sha256"))
                or _accepted_commit(root, chapter)
                or _recovery_conflict(transaction, root, target) != conflict
            ):
                raise WriteTransactionError("chapter recovery facts changed under the lifecycle lock")
            if before.get("sha256") != signature.get("sha256"):
                _atomic_write_bytes(target, raw, root=root)
            after = _file_signature(target, trusted_root=manuscript_root)
            if after.get("sha256") != signature.get("sha256"):
                raise WriteTransactionError("promoted body hash mismatch")
            body_before = (
                transaction.get("body_before")
                if isinstance(transaction.get("body_before"), Mapping)
                else {}
            )
            changed_by_transaction = bool(
                body_before.get("exists") != after.get("exists")
                or body_before.get("sha256") != after.get("sha256")
            )
            return record_write_stage(
                root,
                run_id,
                stage="promotion",
                status="completed",
                details={
                    "source": signature,
                    "target": after,
                    "changed": changed_by_transaction,
                    "owned_recovery": owned_recovery,
                    "recovery_decision": recovery_binding,
                    "lifecycle_lock": str(lifecycle_lock),
                },
                _verified_stage_token=_VERIFIED_STAGE_TOKEN,
            )
    except Timeout as exc:
        raise WriteTransactionError("chapter lifecycle lock is busy") from exc


def _signature_is_current(
    signature: object,
    *,
    trusted_root: Path,
) -> bool:
    if not isinstance(signature, Mapping) or signature.get("exists") is not True:
        return False
    path = Path(str(signature.get("path") or ""))
    try:
        current = _file_signature(path, trusted_root=trusted_root)
    except WriteTransactionError:
        return False
    return (
        current.get("exists") is True
        and current.get("sha256") == signature.get("sha256")
        and current.get("bytes") == signature.get("bytes", current.get("bytes"))
    )


def _audit_current_truth(
    root: Path,
    transaction: Mapping[str, Any],
    progress: Mapping[str, Any],
) -> dict[str, Any]:
    """Re-read mutable truth; immutable receipts alone never prove completion."""

    completed = progress.get("completed") if isinstance(progress.get("completed"), Mapping) else {}
    problems: list[str] = []
    run_id = str(transaction["run_id"])
    chapter = int(transaction["chapter"])
    staging = _staging_dir(root, run_id)

    final_receipt = completed.get("writer_final")
    if final_receipt:
        details = final_receipt.get("details") if isinstance(final_receipt, Mapping) else None
        artifacts = details.get("accepted_artifacts") if isinstance(details, Mapping) else None
        if not isinstance(artifacts, list) or len(artifacts) != 1 or not isinstance(artifacts[0], Mapping):
            problems.append("final_writer_artifact_missing")
        else:
            artifact = artifacts[0]
            try:
                current = _file_signature(artifact.get("path"), trusted_root=staging)
            except WriteTransactionError:
                current = {"exists": False}
            if current.get("sha256") != artifact.get("sha256"):
                problems.append("final_writer_artifact_stale")

    promotion_receipt = completed.get("promotion")
    if promotion_receipt:
        details = promotion_receipt.get("details") if isinstance(promotion_receipt, Mapping) else None
        source = details.get("source") if isinstance(details, Mapping) else None
        target = details.get("target") if isinstance(details, Mapping) else None
        if not _signature_is_current(source, trusted_root=staging):
            problems.append("promotion_source_stale")
        if not _signature_is_current(target, trusted_root=root / "正文"):
            problems.append("promotion_target_stale")
        if isinstance(source, Mapping) and isinstance(target, Mapping) and source.get("sha256") != target.get("sha256"):
            problems.append("promotion_hash_mismatch")

    commit_receipt = completed.get("commit")
    current_commit: dict[str, Any] | None = None
    if commit_receipt:
        details = commit_receipt.get("details") if isinstance(commit_receipt, Mapping) else None
        current_commit = _accepted_commit(root, chapter)
        if (
            not transaction.get("test_only")
            and isinstance(details, Mapping)
            and set(details) == _COMMIT_DETAIL_FIELDS
        ):
            try:
                _verified_materialized_commit_truth(
                    root,
                    transaction,
                    progress,
                    receipt_details=details,
                    allow_projection_advance=True,
                )
            except WriteTransactionError as exc:
                problems.append(f"accepted_commit_stale:{exc}")
        else:
            signature = details.get("commit") if isinstance(details, Mapping) else None
            if current_commit is None or not _signature_is_current(
                signature,
                trusted_root=root / ".story-system" / "commits",
            ):
                problems.append("accepted_commit_stale")

    if not transaction.get("test_only"):
        if completed.get("promotion") or progress.get("next_stage") is None:
            if transaction.get("parent_task_binding_status") != "verified_current_parent":
                problems.append("current_parent_task_binding_pending")
            else:
                try:
                    _assert_current_parent_binding(transaction)
                except WriteTransactionError as exc:
                    problems.append(f"current_parent_task_binding_stale:{exc}")
        if completed.get("precommit"):
            try:
                _verified_commit_input_hashes(root, run_id, transaction)
            except WriteTransactionError as exc:
                problems.append(f"commit_inputs_stale:{exc}")
        projection_receipt = completed.get("projections")
        if projection_receipt:
            details = projection_receipt.get("details") if isinstance(projection_receipt, Mapping) else None
            run = latest_projection_run(root, chapter=chapter)
            statuses = projection_status_from_run(run)
            if (
                current_commit is None
                or not isinstance(details, Mapping)
                or not isinstance(run, Mapping)
                or set(statuses) != PROJECTION_WRITERS
                or any(value not in {"done", "skipped"} for value in statuses.values())
                or run.get("run_id") != details.get("projection_run_id")
                or run.get("commit_hash") != details.get("projection_commit_hash")
                or run.get("commit_hash") != commit_hash(current_commit)
            ):
                problems.append("projection_truth_stale")
        postcommit_receipt = completed.get("postcommit")
        if postcommit_receipt:
            details = postcommit_receipt.get("details") if isinstance(postcommit_receipt, Mapping) else None
            report = run_write_gate(root, chapter=chapter, stage="postcommit")
            if (
                report.get("ok") is not True
                or not isinstance(details, Mapping)
                or details.get("gate_report_sha256") != _sha256_bytes(_canonical_bytes(report))
            ):
                problems.append("postcommit_gate_stale")
        backup_receipt = completed.get("backup")
        if backup_receipt:
            details = backup_receipt.get("details") if isinstance(backup_receipt, Mapping) else None
            if not isinstance(details, Mapping):
                problems.append("backup_receipt_missing")
            else:
                try:
                    from backup_manager import (
                        GitBackupManager,
                        verify_git_backup_authorization_state,
                        verify_git_backup_decision_receipt,
                    )

                    manager = GitBackupManager(str(root))
                    if details.get("code") == "skipped_non_git":
                        if manager.repository_status != "not_repo":
                            raise WriteTransactionError("non-Git skip is no longer true")
                    else:
                        if manager.repository_status != "exact":
                            raise WriteTransactionError(
                                manager.repository_error or "Git repository probe failed"
                            )
                        allowlist = details.get("allowlist")
                        if not isinstance(allowlist, list) or not allowlist:
                            raise WriteTransactionError("backup allowlist is missing")
                        decision = verify_git_backup_decision_receipt(
                            root,
                            chapter,
                            allowlist,
                            details.get("decision_receipt"),
                        )
                        state = verify_git_backup_authorization_state(root, decision)
                        receipted_result = dict(details)
                        receipted_result.pop("receipt_artifact", None)
                        if state.get("status") != "completed" or state.get("result") != receipted_result:
                            raise WriteTransactionError("backup registry result is stale")
                except Exception as exc:
                    problems.append(f"backup_truth_stale:{exc}")
    return {
        "schema_version": "webnovel-write-current-truth-audit/v1",
        "run_id": run_id,
        "ok": not problems,
        "problems": problems,
    }


def _write_transaction_status_locked(
    root: Path,
    run_id: str,
    transaction: Mapping[str, Any],
) -> dict[str, Any]:
    if not transaction.get("test_only"):
        _assert_current_parent_binding(transaction)
    _, progress = _replayed_progress(root, transaction)
    next_stage = progress["next_stage"]
    commit_done = "commit" in progress["completed"]
    audit = _audit_current_truth(root, transaction, progress)
    stale = audit["ok"] is not True
    terminal_recovery = _verified_terminal_recovery_decision(root, transaction, progress)
    terminal_status = (
        str(terminal_recovery["status"])
        if terminal_recovery is not None
        else None
    )
    production_complete = (
        next_stage is None
        and not transaction.get("test_only")
        and not stale
        and terminal_recovery is None
    )
    return {
        "schema_version": TRANSACTION_SCHEMA_VERSION,
        "run_id": run_id,
        "chapter": transaction["chapter"],
        "mode": transaction["mode"],
        "status": (
            terminal_status
            if terminal_status is not None
            else "stale"
            if stale
            else "complete"
            if production_complete
            else "test_only_complete"
            if next_stage is None
            else "failed"
            if progress["last_failure"]
            else "in_progress"
        ),
        "next_stage": (
            None
            if terminal_recovery is not None
            else "truth_recovery"
            if stale and next_stage is None
            else next_stage
        ),
        "completed_stages": list(progress["completed"]),
        "last_failure": progress["last_failure"],
        "commit_done": commit_done,
        "rerun_agents_allowed": terminal_recovery is None and not commit_done,
        "test_only": bool(transaction.get("test_only")),
        "production_complete": production_complete,
        "truth_audit": audit,
        "terminal_recovery": terminal_recovery,
        "new_transaction_required_to_change_choice": terminal_recovery is not None,
        "live_gates": (
            []
            if transaction.get("test_only")
            or transaction.get("parent_task_binding_status") == "verified_current_parent"
            else ["current_parent_task_binding_pending"]
        ),
    }


def write_transaction_status(project_root: str | Path, run_id: str) -> dict[str, Any]:
    root = _safe_project_root(project_root)
    transaction = _load_transaction(root, run_id)
    if FileLock is None:
        raise WriteTransactionError("filelock is required for write transaction status")
    lock_path = _run_dir(root, run_id) / "transaction.lock"
    _require_safe_path(root, lock_path, must_exist=False, regular_file=True)
    try:
        with FileLock(str(lock_path), timeout=10):
            _require_safe_path(root, lock_path, must_exist=True, regular_file=True)
            transaction = _load_transaction(root, run_id)
            return _write_transaction_status_locked(root, run_id, transaction)
    except Timeout as exc:
        raise WriteTransactionError("write transaction lock is busy") from exc


def build_write_resume_plan(project_root: str | Path, run_id: str) -> dict[str, Any]:
    status = write_transaction_status(project_root, run_id)
    stage = status.get("next_stage")
    terminal = status.get("terminal_recovery")
    action = str(status["status"]) if isinstance(terminal, Mapping) else "done"
    if terminal is not None:
        pass
    elif stage == "projections":
        action = "retry_projection_only"
    elif stage == "postcommit":
        action = "run_postcommit_only"
    elif stage == "backup":
        action = "retry_backup_only"
    elif stage:
        action = f"resume_{stage}"
    return {
        **status,
        "action": action,
        "must_not_rerun_agents": bool(terminal) or bool(status.get("commit_done")),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Transactional write receipt state machine")
    parser.add_argument("--project-root", required=True)
    sub = parser.add_subparsers(dest="action", required=True)
    begin = sub.add_parser("begin")
    begin.add_argument("--chapter", type=int, required=True)
    begin.add_argument("--mode", choices=sorted(WRITE_MODES), default="default")
    begin.add_argument("--parent-model", required=True)
    begin.add_argument("--parent-reasoning-effort", default=None)
    begin.add_argument("--workspace-root", required=True)
    begin.add_argument("--run-id", default=None)
    stage = sub.add_parser("stage")
    stage.add_argument("--run-id", required=True)
    stage.add_argument("--request-file", required=True)
    prepare_agent = sub.add_parser("prepare-agent")
    prepare_agent.add_argument("--run-id", required=True)
    prepare_agent.add_argument("--request-file", required=True)
    accept_agent = sub.add_parser("accept-agent")
    accept_agent.add_argument("--run-id", required=True)
    accept_agent.add_argument("--request-file", required=True)
    targeted_request = sub.add_parser("targeted-fix-request")
    targeted_request.add_argument("--run-id", required=True)
    targeted_decide = sub.add_parser("targeted-fix-decide")
    targeted_decide.add_argument("--run-id", required=True)
    targeted_decide.add_argument("--request-file", required=True)
    minimal = sub.add_parser("minimal-no-review")
    minimal.add_argument("--run-id", required=True)
    recovery_request = sub.add_parser("recovery-request")
    recovery_request.add_argument("--run-id", required=True)
    recovery_request.add_argument("--target", required=True)
    recovery_decide = sub.add_parser("recovery-decide")
    recovery_decide.add_argument("--run-id", required=True)
    recovery_decide.add_argument("--target", required=True)
    recovery_decide.add_argument("--request-file", required=True)
    promote = sub.add_parser("promote")
    promote.add_argument("--run-id", required=True)
    promote.add_argument("--target", required=True)
    promote.add_argument("--recovery-decision", default=None)
    promote.add_argument("--decision-receipt", default=None)
    status_cmd = sub.add_parser("status")
    status_cmd.add_argument("--run-id", required=True)
    resume = sub.add_parser("resume")
    resume.add_argument("--run-id", required=True)
    args = parser.parse_args()
    try:
        if args.action != "begin":
            cli_root = _safe_project_root(args.project_root)
            cli_transaction = _load_transaction(cli_root, args.run_id)
            if cli_transaction.get("test_only") is True:
                raise WriteTransactionError(
                    "public CLI cannot create, resume, or mutate test-only transactions"
                )
        if args.action == "begin":
            result = begin_write_transaction(
                args.project_root,
                chapter=args.chapter,
                mode=args.mode,
                parent_model=args.parent_model,
                parent_reasoning_effort=args.parent_reasoning_effort,
                workspace_root=args.workspace_root,
                run_id=args.run_id,
            )
        elif args.action == "stage":
            result = record_verified_stage_request(
                args.project_root,
                args.run_id,
                args.request_file,
            )
        elif args.action == "prepare-agent":
            result = prepare_agent_launch_request(
                args.project_root,
                args.run_id,
                args.request_file,
            )
        elif args.action == "accept-agent":
            result = accept_agent_request(args.project_root, args.run_id, args.request_file)
        elif args.action == "targeted-fix-request":
            result = prepare_targeted_fix_decision(args.project_root, args.run_id)
        elif args.action == "targeted-fix-decide":
            result = record_targeted_fix_decision(
                args.project_root,
                args.run_id,
                args.request_file,
            )
        elif args.action == "minimal-no-review":
            result = record_minimal_no_review(args.project_root, args.run_id)
        elif args.action == "recovery-request":
            result = prepare_write_recovery_decision(
                args.project_root,
                args.run_id,
                target_path=args.target,
            )
        elif args.action == "recovery-decide":
            result = record_write_recovery_decision(
                args.project_root,
                args.run_id,
                target_path=args.target,
                request_file=args.request_file,
            )
        elif args.action == "promote":
            result = promote_verified_writer_artifact(
                args.project_root,
                args.run_id,
                target_path=args.target,
                recovery_decision=args.recovery_decision,
                decision_receipt=args.decision_receipt,
            )
        elif args.action == "status":
            result = write_transaction_status(args.project_root, args.run_id)
        else:
            result = build_write_resume_plan(args.project_root, args.run_id)
        exit_code = 1 if result.get("status") == "choice_required" else 0
    except WriteRecoveryChoiceRequired as exc:
        result = {
            "status": "choice_required",
            "code": exc.code,
            "message": str(exc),
            "choices": list(exc.choices),
        }
        exit_code = 1
    except (WriteTransactionError, json.JSONDecodeError) as exc:
        result = {"status": "failed", "error": str(exc)}
        exit_code = 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
