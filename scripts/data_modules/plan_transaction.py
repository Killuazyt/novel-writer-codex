#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Hash-bound validation and atomic promotion for staged volume plans."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import UUID

try:
    from filelock import FileLock, Timeout
except ImportError:  # fail closed at runtime
    FileLock = None  # type: ignore[assignment]

from .plan_validator import (
    REQUIRED_ARTIFACTS,
    file_sha256,
    sha256_bytes,
    validate_plan_manifest,
)
from .codex_interaction import ChoiceProtocolError, build_choice_request, resolve_choice
from .plan_request import PlanRequestError, plan_request_sha256, validate_plan_request
from .project_phase import contract_files_for_chapter
from .story_contract_schema import ChapterBrief, MasterSetting, ReviewContract, VolumeBrief
from .write_gates import run_write_gate

try:
    from update_master_outline import (
        MasterOutlineSyncError,
        _append_foreshadow_rows,
        _normalize_anchor,
        _structured_writeback_items,
        _update_volume_table,
        sync_master_outline,
    )
except ImportError:
    from scripts.update_master_outline import (
        MasterOutlineSyncError,
        _append_foreshadow_rows,
        _normalize_anchor,
        _structured_writeback_items,
        _update_volume_table,
        sync_master_outline,
    )


VALIDATION_RECEIPT_SCHEMA = "webnovel-plan-validation-receipt/v1"
APPLY_RECEIPT_SCHEMA = "webnovel-plan-apply-receipt/v2"
STAGE_RECEIPT_SCHEMA = "webnovel-plan-stage-receipt/v2"
PARENT_EVIDENCE_SCHEMA = "webnovel-plan-parent-evidence/v1"
PARENT_MARKER_SCHEMA = "webnovel-plan-parent-marker/v1"
BATCH_FRAGMENT_SCHEMA = "webnovel-plan-batch-fragment/v1"
BATCH_ACCEPTED_SCHEMA = "webnovel-plan-batch-accepted/v1"
BATCH_SET_SCHEMA = "webnovel-plan-batch-set/v1"
PLAN_DECISION_REQUEST_SCHEMA = "webnovel-plan-decision-request/v1"
PLAN_DECISION_RECEIPT_SCHEMA = "webnovel-plan-decision-receipt/v1"
PARENT_MARKER_PREFIX = "WEBNOVEL_PLAN_EVIDENCE "
PLAN_DECISION_MARKER_PREFIX = "WEBNOVEL_PLAN_DECISION/v1 "
DOWNSTREAM_STAGES = ("master_outline", "state", "contracts", "prewrite")
PLAN_DECISION_STAGES = ("apply", "master_outline", "state", "contracts")
PLAN_DECISION_CHOICES = ("keep", "replace", "cancel")
MAX_EVIDENCE_BYTES = 32 * 1024 * 1024
MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_BATCH_FRAGMENT_BYTES = 8 * 1024 * 1024
TRUSTED_CODEX_SESSIONS_ROOT = Path(os.path.abspath(Path.home() / ".codex" / "sessions"))
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,94}[A-Za-z0-9])?$")
_WINDOWS_RESERVED_RUN_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
_BATCH_FRAGMENT_NAME_RE = re.compile(r"^batch-([0-9]{6})-([0-9]{6})\.json$")
_VERIFIED_DOWNSTREAM_TOKEN = object()


class PlanTransactionError(RuntimeError):
    """A plan transaction could not safely advance."""


def _require_safe_run_id(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not _RUN_ID_RE.fullmatch(value)
        or value.split(".", 1)[0].upper() in _WINDOWS_RESERVED_RUN_NAMES
    ):
        raise PlanTransactionError("invalid plan run_id")
    return value


class PlanApplyChoiceRequired(PlanTransactionError):
    """Existing authored planning facts require an explicit overwrite choice."""

    def __init__(self, decision: Mapping[str, Any]):
        super().__init__("existing plan artifacts differ; trusted parent decision receipt is pending")
        self.decision = dict(decision)
        self.paths = list(decision.get("paths") or [])
        self.token = str(decision.get("scope_challenge") or "")


class PlanDownstreamChoiceRequired(PlanTransactionError):
    """A downstream authored fact conflicts with the verified plan."""

    def __init__(self, *, stage: str, decision: Mapping[str, Any]):
        super().__init__(f"{stage} contains different authored facts; trusted parent decision receipt is pending")
        self.stage = stage
        self.decision = dict(decision)
        self.paths = list(decision.get("paths") or [])
        self.token = str(decision.get("scope_challenge") or "")


class _PlanDecisionNoWrite(Exception):
    """A trusted keep/cancel choice completed without changing novel facts."""

    def __init__(self, result: Mapping[str, Any]):
        super().__init__(str(result.get("status") or "decision_selected"))
        self.result = dict(result)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _receipt_hash(payload: Mapping[str, Any]) -> str:
    return sha256_bytes(_canonical_bytes(dict(payload)))


def _absolute_lexical(path: str | Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _path_chain(path: Path) -> list[Path]:
    absolute = _absolute_lexical(path)
    current = Path(absolute.anchor)
    result = [current]
    for part in absolute.parts[1:]:
        current = current / part
        result.append(current)
    return result


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False


def _is_reparse_point(path: Path) -> bool:
    try:
        attrs = int(getattr(path.lstat(), "st_file_attributes", 0))
    except OSError:
        return False
    return path.is_symlink() or bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _safe_project_root(project_root: str | Path) -> Path:
    lexical = _absolute_lexical(project_root)
    for component in _path_chain(lexical):
        if (component.exists() or component.is_symlink()) and _is_reparse_point(component):
            raise PlanTransactionError(f"reparse-point project root is forbidden: {component}")
    if not lexical.is_dir():
        raise PlanTransactionError(f"project_root is not a directory: {lexical}")
    return lexical.resolve(strict=True)


def _require_trusted_file(trusted_root: Path, path: Path) -> Path:
    """Reject lexical linklike ancestors before resolving a host evidence file."""

    root = _absolute_lexical(trusted_root)
    lexical = _absolute_lexical(path)
    try:
        lexical.relative_to(root)
    except ValueError as exc:
        raise PlanTransactionError("parent rollout must be under the trusted Codex sessions root") from exc
    for component in _path_chain(lexical):
        if (component.exists() or component.is_symlink()) and _is_reparse_point(component):
            raise PlanTransactionError(f"reparse-point parent evidence path is forbidden: {component}")
    if not root.is_dir() or not lexical.is_file():
        raise PlanTransactionError("parent rollout is missing from the trusted Codex sessions root")
    try:
        lexical.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise PlanTransactionError("parent rollout must be under the trusted Codex sessions root") from exc
    return lexical


def _require_current_parent_rollout(thread_id: str, rollout_path: Path) -> str:
    supplied = str(os.environ.get("CODEX_THREAD_ID") or "").strip().lower()
    try:
        current_thread = str(UUID(supplied))
        claimed_thread = str(UUID(str(thread_id).strip().lower()))
    except (ValueError, AttributeError) as exc:
        raise PlanTransactionError("CODEX_THREAD_ID and parent thread_id must be non-empty UUIDs") from exc
    if claimed_thread != current_thread:
        raise PlanTransactionError("parent evidence does not belong to the current Codex task")
    root = _absolute_lexical(TRUSTED_CODEX_SESSIONS_ROOT)
    rollout_path = _require_trusted_file(root, rollout_path)
    matches: list[Path] = []
    for current_raw, dirs, files in os.walk(root, followlinks=False):
        current = Path(current_raw)
        for name in dirs:
            if _is_reparse_point(current / name):
                raise PlanTransactionError("trusted Codex sessions root contains a reparse directory")
        for name in files:
            if current_thread in name and name.lower().endswith(".jsonl"):
                matches.append(current / name)
    if len(matches) != 1 or matches[0].resolve(strict=True) != rollout_path.resolve(strict=True):
        raise PlanTransactionError("CODEX_THREAD_ID must uniquely identify the supplied parent rollout")
    return current_thread


def _require_fixed_path(
    root: Path,
    path: Path,
    *,
    expected: Path,
    must_exist: bool = True,
) -> Path:
    root_resolved = root.resolve()
    lexical = Path(os.path.abspath(path))
    expected_lexical = Path(os.path.abspath(expected))
    if lexical != expected_lexical:
        raise PlanTransactionError(f"path is not the fixed run artifact: {path}")
    try:
        relative = expected_lexical.relative_to(root_resolved)
    except ValueError as exc:
        raise PlanTransactionError(f"path is outside the project: {path}") from exc
    current = root_resolved
    for part in relative.parts:
        current = current / part
        if (current.exists() or current.is_symlink()) and _is_reparse_point(current):
            raise PlanTransactionError(f"reparse-point path is forbidden: {current}")
    resolved = lexical.resolve()
    if resolved != expected_lexical.resolve() or not _inside(resolved, root_resolved):
        raise PlanTransactionError(f"path is not the fixed run artifact: {path}")
    if must_exist and not resolved.is_file():
        raise PlanTransactionError(f"required file is missing: {resolved}")
    if not must_exist and (lexical.exists() or lexical.is_symlink()) and not lexical.is_file():
        raise PlanTransactionError(f"fixed file path is not a regular file: {lexical}")
    return resolved


def _read_bounded_bytes(path: Path, *, max_bytes: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise PlanTransactionError(f"cannot open bounded file: {path}: {exc}") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
            raise PlanTransactionError(f"file is not regular or exceeds size limit: {path}")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(max_bytes + 1)
        after = os.fstat(fd)
        try:
            path_after = path.stat()
        except OSError as exc:
            raise PlanTransactionError(f"bounded file disappeared during read: {path}") from exc
        identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        identity_path = (path_after.st_dev, path_after.st_ino, path_after.st_size, path_after.st_mtime_ns)
        if (
            len(raw) > max_bytes
            or len(raw) != before.st_size
            or identity_before != identity_after
            or identity_before != identity_path
            or _is_reparse_point(path)
        ):
            raise PlanTransactionError(f"bounded file changed during read: {path}")
        return raw
    finally:
        os.close(fd)


def _read_bounded_json(path: Path, *, max_bytes: int) -> tuple[dict[str, Any], bytes]:
    raw = _read_bounded_bytes(path, max_bytes=max_bytes)
    if raw.startswith(b"\xef\xbb\xbf"):
        raise PlanTransactionError(f"UTF-8 BOM is forbidden: {path}")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlanTransactionError(f"invalid UTF-8 JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PlanTransactionError(f"JSON file must contain an object: {path}")
    return value, raw


def _prepare_atomic_json_target(root: Path, path: Path) -> None:
    root_resolved = root.resolve()
    try:
        relative_parent = Path(os.path.abspath(path.parent)).relative_to(root_resolved)
    except ValueError as exc:
        raise PlanTransactionError(f"JSON parent escapes project: {path.parent}") from exc
    current = root_resolved
    for part in relative_parent.parts:
        current = current / part
        if current.exists() or current.is_symlink():
            if _is_reparse_point(current) or not current.is_dir():
                raise PlanTransactionError(f"unsafe JSON parent component: {current}")
        else:
            current.mkdir()
            if _is_reparse_point(current) or not current.is_dir():
                raise PlanTransactionError(f"unsafe JSON parent component: {current}")
    _require_fixed_path(root, path, expected=path, must_exist=False)
    for sibling in (
        path.with_suffix(path.suffix + ".lock"),
        path.with_suffix(path.suffix + ".bak"),
    ):
        _require_fixed_path(root, sibling, expected=sibling, must_exist=False)


def _safe_json_write_locked(
    root: Path,
    path: Path,
    payload: Mapping[str, Any],
    *,
    backup: bool,
    expected_before_sha256: str | None | object = ...,
) -> None:
    """Write JSON only after lock-held revalidation of target/lock/backup."""

    _prepare_atomic_json_target(root, path)
    if expected_before_sha256 is not ...:
        current = file_sha256(path) if path.is_file() else None
        if current != expected_before_sha256:
            raise PlanTransactionError(f"JSON target changed while waiting for its lock: {path}")
    raw = json.dumps(dict(payload), ensure_ascii=False, indent=2).encode("utf-8")
    if backup and path.is_file():
        backup_path = path.with_suffix(path.suffix + ".bak")
        _atomic_write_bytes(backup_path, _read_bounded_bytes(path, max_bytes=8 * 1024 * 1024))
    _atomic_write_bytes(path, raw)
    _prepare_atomic_json_target(root, path)
    if _read_json(path) != dict(payload):
        raise PlanTransactionError(f"JSON target failed exact readback: {path}")


def _safe_json_write(
    root: Path,
    path: Path,
    payload: Mapping[str, Any],
    *,
    backup: bool,
    expected_before_sha256: str | None | object = ...,
) -> None:
    if FileLock is None:
        raise PlanTransactionError("filelock is required for safe JSON writes")
    _prepare_atomic_json_target(root, path)
    lock_path = path.with_suffix(path.suffix + ".lock")
    try:
        with FileLock(str(lock_path), timeout=10):
            _safe_json_write_locked(
                root,
                path,
                payload,
                backup=backup,
                expected_before_sha256=expected_before_sha256,
            )
    except Timeout as exc:
        raise PlanTransactionError(f"JSON target lock is busy: {path}") from exc


def _load_saved_plan_request(
    root: Path,
    request_file: str | Path,
) -> tuple[dict[str, Any], Path, str]:
    request_path = Path(request_file)
    if not request_path.is_absolute() or request_path.name != "plan-request.json":
        raise PlanTransactionError("plan request path must be the absolute fixed run request")
    run_id = _require_safe_run_id(request_path.parent.name)
    expected = root / ".webnovel" / "tmp" / "plan-runs" / run_id / "plan-request.json"
    request_path = _require_fixed_path(root, request_path, expected=expected)
    request, _ = _read_bounded_json(request_path, max_bytes=MAX_REQUEST_BYTES)
    try:
        validate_plan_request(request, project_root=root)
    except PlanRequestError as exc:
        raise PlanTransactionError(f"plan request rejected: {exc}") from exc
    if request.get("run_id") != run_id:
        raise PlanTransactionError("plan request file does not bind its run directory")
    return request, request_path, plan_request_sha256(request)


def _batch_fragment_path(root: Path, run_id: str, start: int, end: int) -> Path:
    run_id = _require_safe_run_id(run_id)
    return (
        root
        / ".webnovel"
        / "tmp"
        / "plan-runs"
        / run_id
        / "batches"
        / f"batch-{start:06d}-{end:06d}.json"
    )


def _batch_receipt_path(root: Path, run_id: str, start: int, end: int) -> Path:
    return (
        _runtime_dir(root, run_id)
        / "batches"
        / f"batch-{start:06d}-{end:06d}.accepted.json"
    )


def _validate_batch_fragment(
    fragment: Mapping[str, Any],
    request: Mapping[str, Any],
    *,
    start: int,
    end: int,
) -> list[dict[str, Any]]:
    if set(fragment) != {
        "schema_version",
        "run_id",
        "volume",
        "start_chapter",
        "end_chapter",
        "chapters",
    }:
        raise PlanTransactionError("plan batch fragment has an invalid shape")
    expected_batch = {"start_chapter": start, "end_chapter": end}
    if (
        fragment.get("schema_version") != BATCH_FRAGMENT_SCHEMA
        or fragment.get("run_id") != request.get("run_id")
        or fragment.get("volume") != request.get("volume")
        or fragment.get("start_chapter") != start
        or fragment.get("end_chapter") != end
        or expected_batch not in (request.get("batches") or [])
    ):
        raise PlanTransactionError("plan batch fragment does not bind one requested batch")
    chapters = fragment.get("chapters")
    expected_chapters = list(range(start, end + 1))
    if not isinstance(chapters, list) or [
        item.get("chapter") if isinstance(item, Mapping) else None for item in chapters
    ] != expected_chapters:
        raise PlanTransactionError("plan batch fragment must cover its exact chapter range once")
    return [dict(item) for item in chapters]


def _verify_accepted_batch_receipt(
    root: Path,
    request: Mapping[str, Any],
    request_path: Path,
    *,
    start: int,
    end: int,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    receipt_path = _batch_receipt_path(root, str(request["run_id"]), start, end)
    receipt_path = _require_fixed_path(root, receipt_path, expected=receipt_path)
    receipt = _read_json(receipt_path)
    expected_keys = {
        "schema_version",
        "status",
        "created_at",
        "project_root",
        "run_id",
        "volume",
        "start_chapter",
        "end_chapter",
        "request_path",
        "request_sha256",
        "fragment_path",
        "fragment_sha256",
        "receipt_sha256",
    }
    if set(receipt) != expected_keys:
        raise PlanTransactionError("accepted plan batch receipt has an invalid shape")
    unsigned = dict(receipt)
    claimed = str(unsigned.pop("receipt_sha256", ""))
    if not _SHA256_RE.fullmatch(claimed) or claimed != _receipt_hash(unsigned):
        raise PlanTransactionError("accepted plan batch receipt hash mismatch")
    fragment_path = _batch_fragment_path(root, str(request["run_id"]), start, end)
    if (
        receipt.get("schema_version") != BATCH_ACCEPTED_SCHEMA
        or receipt.get("status") != "accepted"
        or not isinstance(receipt.get("created_at"), str)
        or not str(receipt.get("created_at") or "").strip()
        or receipt.get("project_root") != str(root)
        or receipt.get("run_id") != request.get("run_id")
        or receipt.get("volume") != request.get("volume")
        or receipt.get("start_chapter") != start
        or receipt.get("end_chapter") != end
        or receipt.get("request_path") != str(request_path)
        or receipt.get("request_sha256") != plan_request_sha256(request)
        or receipt.get("fragment_path") != str(fragment_path)
    ):
        raise PlanTransactionError("accepted plan batch receipt does not bind the current request")
    fragment_path = _require_fixed_path(root, fragment_path, expected=fragment_path)
    fragment, fragment_raw = _read_bounded_json(
        fragment_path,
        max_bytes=MAX_BATCH_FRAGMENT_BYTES,
    )
    fragment_sha = sha256_bytes(fragment_raw)
    if receipt.get("fragment_sha256") != fragment_sha:
        raise PlanTransactionError("accepted plan batch fragment changed after acceptance")
    chapters = _validate_batch_fragment(fragment, request, start=start, end=end)
    signature = {
        "start_chapter": start,
        "end_chapter": end,
        "fragment_path": str(fragment_path),
        "fragment_sha256": fragment_sha,
        "receipt_path": str(receipt_path),
        "receipt_sha256": claimed,
    }
    return receipt, chapters, signature


def accept_plan_batch(
    project_root: str | Path,
    request_file: str | Path,
    fragment_file: str | Path,
) -> dict[str, Any]:
    """Accept one immutable run-scoped planning fragment."""

    root = _safe_project_root(project_root)
    request, request_path, request_sha = _load_saved_plan_request(root, request_file)
    fragment_path = Path(fragment_file)
    if not fragment_path.is_absolute():
        raise PlanTransactionError("plan batch fragment path must be absolute")
    match = _BATCH_FRAGMENT_NAME_RE.fullmatch(fragment_path.name)
    if match is None:
        raise PlanTransactionError("plan batch fragment filename is invalid")
    start, end = (int(match.group(1)), int(match.group(2)))
    expected_fragment = _batch_fragment_path(root, str(request["run_id"]), start, end)
    fragment_path = _require_fixed_path(root, fragment_path, expected=expected_fragment)
    fragment, fragment_raw = _read_bounded_json(
        fragment_path,
        max_bytes=MAX_BATCH_FRAGMENT_BYTES,
    )
    _validate_batch_fragment(fragment, request, start=start, end=end)
    receipt_path = _batch_receipt_path(root, str(request["run_id"]), start, end)
    if receipt_path.is_file():
        existing, _, _ = _verify_accepted_batch_receipt(
            root,
            request,
            request_path,
            start=start,
            end=end,
        )
        return existing
    receipt = {
        "schema_version": BATCH_ACCEPTED_SCHEMA,
        "status": "accepted",
        "created_at": _now_iso(),
        "project_root": str(root),
        "run_id": request["run_id"],
        "volume": request["volume"],
        "start_chapter": start,
        "end_chapter": end,
        "request_path": str(request_path),
        "request_sha256": request_sha,
        "fragment_path": str(fragment_path),
        "fragment_sha256": sha256_bytes(fragment_raw),
    }
    receipt["receipt_sha256"] = _receipt_hash(receipt)
    _write_receipt_once(receipt_path, receipt, project_root=root)
    stored, _, _ = _verify_accepted_batch_receipt(
        root,
        request,
        request_path,
        start=start,
        end=end,
    )
    return stored


def _verify_complete_batch_set(
    root: Path,
    request: Mapping[str, Any],
    request_path: Path,
    report: Mapping[str, Any],
) -> dict[str, Any]:
    manifest = _read_json(Path(str(report.get("manifest_path") or "")))
    chapters = manifest.get("chapters")
    if not isinstance(chapters, list):
        raise PlanTransactionError("plan manifest chapters are missing for batch verification")
    chapters_by_number = {
        item.get("chapter"): dict(item)
        for item in chapters
        if isinstance(item, Mapping) and isinstance(item.get("chapter"), int)
    }
    signatures: list[dict[str, Any]] = []
    covered: list[int] = []
    for batch in request.get("batches") or []:
        if not isinstance(batch, Mapping):
            raise PlanTransactionError("plan request batches are invalid")
        start = int(batch.get("start_chapter") or 0)
        end = int(batch.get("end_chapter") or 0)
        _, fragment_chapters, signature = _verify_accepted_batch_receipt(
            root,
            request,
            request_path,
            start=start,
            end=end,
        )
        expected_fragment = [chapters_by_number.get(chapter) for chapter in range(start, end + 1)]
        if any(item is None for item in expected_fragment) or fragment_chapters != expected_fragment:
            raise PlanTransactionError(
                f"accepted batch {start}-{end} does not match the final plan manifest"
            )
        covered.extend(range(start, end + 1))
        signatures.append(signature)
    expected_coverage = list(
        range(int(request["start_chapter"]), int(request["end_chapter"]) + 1)
    )
    if covered != expected_coverage or len(set(covered)) != len(covered):
        raise PlanTransactionError("accepted batches must provide non-overlapping complete coverage")
    payload = {
        "schema_version": BATCH_SET_SCHEMA,
        "run_id": request["run_id"],
        "manifest_sha256": report.get("manifest_sha256"),
        "batches": signatures,
    }
    payload["batch_set_sha256"] = _receipt_hash(payload)
    return payload


def _parent_marker_payload(
    root: Path,
    request: Mapping[str, Any],
    report: Mapping[str, Any],
    batch_set: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": PARENT_MARKER_SCHEMA,
        "project_root": str(root),
        "run_id": report.get("run_id"),
        "request_sha256": plan_request_sha256(request),
        "manifest_sha256": report.get("manifest_sha256"),
        "content_sha256": report.get("content_sha256"),
        "artifact_hashes": report.get("artifact_hashes"),
        "batch_set_sha256": batch_set.get("batch_set_sha256"),
    }


def _load_bound_plan_request(
    root: Path,
    request_file: str | Path,
    report: Mapping[str, Any],
) -> tuple[dict[str, Any], Path, str]:
    request, request_path, request_sha = _load_saved_plan_request(root, request_file)
    run_id = _require_safe_run_id(request.get("run_id"))
    expected_manifest = (
        root
        / ".webnovel"
        / "tmp"
        / "plan-runs"
        / run_id
        / "plan-manifest.json"
    )
    request_manifest = _require_fixed_path(
        root,
        Path(str(request.get("manifest_path") or "")),
        expected=expected_manifest,
    )
    report_manifest = _require_fixed_path(
        root,
        Path(str(report.get("manifest_path") or "")),
        expected=request_manifest,
    )
    manifest = _read_json(report_manifest)
    chapter_range = manifest.get("chapter_range")
    if (
        request.get("run_id") != report.get("run_id")
        or request.get("volume") != report.get("volume")
        or not isinstance(chapter_range, list)
        or request.get("start_chapter") != chapter_range[0]
        or request.get("end_chapter") != chapter_range[1]
        or request.get("parent_model") != manifest.get("parent_model")
        or request.get("executor") != manifest.get("executor")
        or request.get("invoked_agents") != manifest.get("invoked_agents")
    ):
        raise PlanTransactionError("plan request does not bind this manifest")
    return request, request_path, request_sha


def build_parent_evidence_marker(
    project_root: str | Path,
    manifest_path: str | Path,
    request_file: str | Path,
) -> str:
    """Build the exact marker the current parent task must emit."""

    root = _safe_project_root(project_root)
    report = validate_plan_manifest(root, manifest_path)
    if not report.get("ok"):
        raise PlanTransactionError("plan does not validate; parent marker cannot be built")
    request, request_path, _ = _load_bound_plan_request(root, request_file, report)
    batch_set = _verify_complete_batch_set(root, request, request_path, report)
    return PARENT_MARKER_PREFIX + _canonical_bytes(
        _parent_marker_payload(root, request, report, batch_set)
    ).decode("utf-8")


def _assistant_parent_markers(raw: bytes, *, run_id: str) -> list[dict[str, Any]]:
    try:
        events = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlanTransactionError("parent rollout is not UTF-8 JSONL") from exc
    markers: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, Mapping) or event.get("type") != "response_item":
            continue
        payload = event.get("payload")
        if not isinstance(payload, Mapping) or payload.get("type") != "message" or payload.get("role") != "assistant":
            continue
        content = payload.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, Mapping) or item.get("type") not in {"output_text", "text"}:
                continue
            for line in str(item.get("text") or "").splitlines():
                if not line.startswith(PARENT_MARKER_PREFIX):
                    continue
                try:
                    marker = json.loads(line[len(PARENT_MARKER_PREFIX) :])
                except json.JSONDecodeError as exc:
                    raise PlanTransactionError("parent evidence marker is invalid JSON") from exc
                if isinstance(marker, dict) and marker.get("run_id") == run_id:
                    markers.append(marker)
    return markers


def _parse_parent_identity(
    raw: bytes,
    *,
    rollout_path: Path,
    thread_id: str,
    expected_model: str,
    expected_effort: str,
) -> dict[str, str]:
    if thread_id not in rollout_path.name or rollout_path.suffix.lower() != ".jsonl":
        raise PlanTransactionError("parent rollout filename must identify the expected thread")
    if not all(value.strip() for value in (thread_id, expected_model, expected_effort)):
        raise PlanTransactionError("parent thread, model, and reasoning effort must be explicit")
    try:
        events = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlanTransactionError("parent rollout is not UTF-8 JSONL") from exc
    sessions = [
        (index, event)
        for index, event in enumerate(events)
        if isinstance(event, Mapping) and event.get("type") == "session_meta"
    ]
    if len(sessions) != 1:
        raise PlanTransactionError("parent rollout must contain exactly one session_meta")
    session_index, session_event = sessions[0]
    session = session_event.get("payload")
    turns = [
        event.get("payload")
        for event in events[session_index + 1 :]
        if isinstance(event, Mapping) and event.get("type") == "turn_context"
    ]
    if not isinstance(session, Mapping) or not turns or any(not isinstance(turn, Mapping) for turn in turns):
        raise PlanTransactionError("parent rollout lacks valid session/turn identity")
    source = session.get("source")
    if session.get("parent_thread_id") not in {None, ""} or (
        isinstance(source, Mapping) and source.get("subagent") is not None
    ):
        raise PlanTransactionError("parent rollout must be a top-level Codex task, not a subagent")
    seen_turns: set[str] = set()
    for turn in turns:
        turn_id = str(turn.get("turn_id") or "").strip()
        if not turn_id or turn_id in seen_turns:
            raise PlanTransactionError("parent rollout turn ids are missing or duplicated")
        seen_turns.add(turn_id)
        if turn.get("model") != expected_model or turn.get("effort") != expected_effort:
            raise PlanTransactionError("parent rollout contains conflicting model or effort")
    if (
        session.get("id") != thread_id
        or (session.get("model") is not None and session.get("model") != expected_model)
    ):
        raise PlanTransactionError("parent rollout session identity mismatch")
    return {"thread_id": thread_id, "model": expected_model, "effort": expected_effort}


def _verify_parent_evidence(
    root: Path,
    report: Mapping[str, Any],
    request_file: str | Path,
    evidence_file: str | Path,
) -> dict[str, Any]:
    request, request_path, request_sha = _load_bound_plan_request(root, request_file, report)
    batch_set = _verify_complete_batch_set(root, request, request_path, report)
    evidence_path = Path(evidence_file)
    if not evidence_path.is_absolute():
        raise PlanTransactionError("parent evidence path must be absolute")
    expected_evidence = request_path.with_name("parent-evidence.json")
    evidence_path = _require_fixed_path(root, evidence_path, expected=expected_evidence)
    evidence, evidence_raw = _read_bounded_json(evidence_path, max_bytes=MAX_REQUEST_BYTES)
    if set(evidence) != {
        "schema_version",
        "run_id",
        "request_path",
        "request_sha256",
        "rollout_path",
        "thread_id",
    } or evidence.get("schema_version") != PARENT_EVIDENCE_SCHEMA:
        raise PlanTransactionError("parent evidence request has an invalid shape")
    if (
        evidence.get("run_id") != report.get("run_id")
        or Path(str(evidence.get("request_path") or "")).resolve() != request_path
        or evidence.get("request_sha256") != request_sha
        or not isinstance(evidence.get("thread_id"), str)
        or not evidence.get("thread_id", "").strip()
    ):
        raise PlanTransactionError("parent evidence request does not bind this plan request")
    rollout_path = Path(str(evidence.get("rollout_path") or ""))
    if not rollout_path.is_absolute():
        raise PlanTransactionError("parent rollout must be under the trusted Codex sessions root")
    rollout_path = _require_trusted_file(TRUSTED_CODEX_SESSIONS_ROOT, rollout_path)
    current_thread_id = _require_current_parent_rollout(str(evidence["thread_id"]), rollout_path)
    rollout_raw = _read_bounded_bytes(rollout_path, max_bytes=MAX_EVIDENCE_BYTES)
    _require_trusted_file(TRUSTED_CODEX_SESSIONS_ROOT, rollout_path)
    identity = _parse_parent_identity(
        rollout_raw,
        rollout_path=rollout_path,
        thread_id=current_thread_id,
        expected_model=str(request.get("parent_model") or ""),
        expected_effort=str(request.get("parent_reasoning_effort") or ""),
    )
    expected_marker = _parent_marker_payload(root, request, report, batch_set)
    markers = _assistant_parent_markers(rollout_raw, run_id=str(report.get("run_id") or ""))
    if len(markers) != 1 or markers[0] != expected_marker:
        raise PlanTransactionError("parent rollout lacks one exact request/manifest/artifact hash marker")
    rollout_binding = {
        "rollout_path": str(rollout_path.resolve(strict=True)),
        "thread_id": identity["thread_id"],
        "model": identity["model"],
        "effort": identity["effort"],
        "marker": expected_marker,
    }
    return {
        "request_path": str(request_path),
        "request_sha256": request_sha,
        "evidence_path": str(evidence_path),
        "evidence_sha256": sha256_bytes(evidence_raw),
        "parent_thread_id": identity["thread_id"],
        "parent_model": identity["model"],
        "parent_reasoning_effort": identity["effort"],
        "parent_rollout_path": str(rollout_path.resolve(strict=True)),
        # Rollouts are append-only while the task remains active. Re-parse the
        # current file on every use, but bind the immutable identity + marker
        # rather than the evolving whole-file hash.
        "parent_rollout_binding_sha256": sha256_bytes(_canonical_bytes(rollout_binding)),
        "parent_marker_sha256": sha256_bytes(_canonical_bytes(expected_marker)),
        "batch_set": batch_set,
    }


def _runtime_dir(root: Path, run_id: str) -> Path:
    run_id = _require_safe_run_id(run_id)
    return root / ".webnovel" / "plan-runs" / run_id


def _volume_lifecycle_lock_path(root: Path, volume: Any) -> Path:
    if not isinstance(volume, int) or isinstance(volume, bool) or volume <= 0:
        raise PlanTransactionError("invalid plan volume for lifecycle lock")
    return root / ".webnovel" / "plan-runs" / f".volume-{volume:06d}.lifecycle.lock"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = _read_bounded_bytes(path, max_bytes=8 * 1024 * 1024)
        if raw.startswith(b"\xef\xbb\xbf"):
            raise ValueError("UTF-8 BOM is forbidden")
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, PlanTransactionError) as exc:
        raise PlanTransactionError(f"invalid JSON receipt: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise PlanTransactionError(f"receipt must be an object: {path}")
    return payload


def _write_receipt_once(
    path: Path,
    payload: dict[str, Any],
    *,
    project_root: Path | None = None,
) -> Path:
    root = _safe_project_root(project_root) if project_root is not None else _safe_project_root(path.parent)
    if FileLock is None:
        raise PlanTransactionError("filelock is required for immutable receipts")
    _prepare_atomic_json_target(root, path)
    lock_path = path.with_suffix(path.suffix + ".lock")
    try:
        with FileLock(str(lock_path), timeout=10):
            _prepare_atomic_json_target(root, path)
            if path.exists():
                existing = _read_json(path)
                if existing != payload:
                    raise PlanTransactionError(f"immutable receipt already exists with different content: {path}")
                return path
            _safe_json_write_locked(
                root,
                path,
                payload,
                backup=False,
                expected_before_sha256=None,
            )
            return path
    except Timeout as exc:
        raise PlanTransactionError(f"receipt lock is busy: {path}") from exc


def _write_plan_decision_file_under_lifecycle_lock(
    root: Path,
    path: Path,
    payload: Mapping[str, Any],
) -> Path:
    """Write an immutable decision artifact while the volume lock is held.

    Decision requests and receipts are always created beneath the enclosing
    volume lifecycle lock.  Taking a second per-file ``FileLock`` here would
    create an unnecessary nested lock (and a different lock ordering from the
    apply/stage paths), so perform the same immutable compare-or-create while
    retaining the caller's lifecycle serialization.
    """

    _prepare_atomic_json_target(root, path)
    expected = dict(payload)
    if path.exists():
        existing = _read_json(path)
        if existing != expected:
            raise PlanTransactionError(
                f"immutable plan decision artifact already exists with different content: {path}"
            )
        return path
    _safe_json_write_locked(
        root,
        path,
        expected,
        backup=False,
        expected_before_sha256=None,
    )
    if _read_json(path) != expected:
        raise PlanTransactionError(f"plan decision artifact failed exact readback: {path}")
    return path


def create_validation_receipt(
    project_root: str | Path,
    manifest_path: str | Path,
    *,
    request_file: str | Path | None = None,
    parent_evidence_file: str | Path | None = None,
) -> dict[str, Any]:
    """Validate first; write a runtime-only receipt only after a clean pass."""

    root = _safe_project_root(project_root)
    report = validate_plan_manifest(root, manifest_path)
    if not report.get("ok"):
        # Deliberately return without creating .webnovel/plan-runs or touching
        # authored facts.  Callers can render the deterministic problem list.
        return report
    if request_file is None or parent_evidence_file is None:
        return {
            **report,
            "ok": False,
            "status": "blocked",
            "problems": [
                {
                    "code": "parent_evidence_required",
                    "detail": "fixed request and trusted parent rollout evidence are required",
                }
            ],
        }
    try:
        parent_evidence = _verify_parent_evidence(
            root,
            report,
            request_file,
            parent_evidence_file,
        )
    except PlanTransactionError as exc:
        return {
            **report,
            "ok": False,
            "status": "blocked",
            "problems": [{"code": "parent_evidence_rejected", "detail": str(exc)}],
        }
    receipt = {
        "schema_version": VALIDATION_RECEIPT_SCHEMA,
        "status": "validated",
        "created_at": _now_iso(),
        "project_root": str(root),
        "run_id": report["run_id"],
        "volume": report["volume"],
        "manifest_path": report["manifest_path"],
        "manifest_sha256": report["manifest_sha256"],
        "content_sha256": report["content_sha256"],
        "artifact_hashes": report["artifact_hashes"],
        **parent_evidence,
    }
    receipt["receipt_sha256"] = _receipt_hash(receipt)
    path = _runtime_dir(root, str(report["run_id"])) / "validation.json"
    if path.is_file():
        existing = _read_json(path)
        _verify_validation_receipt(root, existing, manifest_path)
        return existing
    _write_receipt_once(path, receipt, project_root=root)
    return receipt


def build_overwrite_token(validation_receipt: Mapping[str, Any]) -> str:
    """Bind an explicit user decision to one immutable validation receipt."""

    digest = str(validation_receipt.get("receipt_sha256") or "")
    return "webnovel-plan-overwrite:" + hashlib.sha256(
        ("webnovel-plan-overwrite/v1\0" + digest).encode("utf-8")
    ).hexdigest()


def _verify_validation_receipt(
    root: Path,
    receipt: Mapping[str, Any],
    manifest_path: str | Path,
) -> dict[str, Any]:
    expected_keys = {
        "schema_version",
        "status",
        "created_at",
        "project_root",
        "run_id",
        "volume",
        "manifest_path",
        "manifest_sha256",
        "content_sha256",
        "artifact_hashes",
        "request_path",
        "request_sha256",
        "evidence_path",
        "evidence_sha256",
        "parent_thread_id",
        "parent_model",
        "parent_reasoning_effort",
        "parent_rollout_path",
        "parent_rollout_binding_sha256",
        "parent_marker_sha256",
        "batch_set",
        "receipt_sha256",
    }
    if set(receipt) != expected_keys:
        raise PlanTransactionError("validation receipt has an invalid shape")
    if receipt.get("schema_version") != VALIDATION_RECEIPT_SCHEMA:
        raise PlanTransactionError("unsupported validation receipt schema")
    receipt_without_hash = dict(receipt)
    claimed_hash = str(receipt_without_hash.pop("receipt_sha256", ""))
    if not _SHA256_RE.fullmatch(claimed_hash) or claimed_hash != _receipt_hash(receipt_without_hash):
        raise PlanTransactionError("validation receipt hash mismatch")
    report = validate_plan_manifest(root, manifest_path)
    if not report.get("ok"):
        raise PlanTransactionError("plan no longer validates")
    if (
        receipt.get("status") != "validated"
        or not isinstance(receipt.get("created_at"), str)
        or not str(receipt.get("created_at") or "").strip()
        or receipt.get("project_root") != str(root)
        or receipt.get("manifest_path") != report.get("manifest_path")
    ):
        raise PlanTransactionError("validation receipt does not bind the current project manifest")
    for key in ("run_id", "volume", "manifest_sha256", "content_sha256", "artifact_hashes"):
        if receipt.get(key) != report.get(key):
            raise PlanTransactionError(f"validation receipt is stale: {key}")
    parent_evidence = _verify_parent_evidence(
        root,
        report,
        str(receipt.get("request_path") or ""),
        str(receipt.get("evidence_path") or ""),
    )
    for key, value in parent_evidence.items():
        if receipt.get(key) != value:
            raise PlanTransactionError(f"validation receipt parent evidence is stale: {key}")
    return report


def _atomic_write_bytes(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.stem + "_", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def _manifest_artifacts(manifest_path: Path, root: Path) -> dict[str, tuple[Path, Path, str]]:
    manifest = _read_json(manifest_path)
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise PlanTransactionError("manifest artifacts are missing")
    result: dict[str, tuple[Path, Path, str]] = {}
    for name in REQUIRED_ARTIFACTS:
        spec = artifacts.get(name)
        if not isinstance(spec, Mapping):
            raise PlanTransactionError(f"manifest artifact is missing: {name}")
        source_lexical = root / str(spec.get("path") or "")
        target_lexical = root / str(spec.get("target") or "")
        source = _require_fixed_path(root, source_lexical, expected=source_lexical)
        target = _require_fixed_path(
            root,
            target_lexical,
            expected=target_lexical,
            must_exist=False,
        )
        result[name] = (source, target, str(spec.get("sha256") or ""))
    return result


def _stable_artifact_bytes(
    root: Path,
    path: Path,
    *,
    must_exist: bool,
    max_bytes: int = 64 * 1024 * 1024,
) -> bytes | None:
    """Read one project artifact without trusting a pre-lock path check."""

    checked = _require_fixed_path(root, path, expected=path, must_exist=must_exist)
    if not must_exist and not checked.is_file():
        # Recheck the lexical path after observing absence so a concurrently
        # introduced reparse point cannot be treated as an absent target.
        _require_fixed_path(root, path, expected=path, must_exist=False)
        return None
    raw = _read_bounded_bytes(checked, max_bytes=max_bytes)
    _require_fixed_path(root, path, expected=path)
    return raw


def _verify_apply_receipt(
    root: Path,
    receipt: Mapping[str, Any],
    validation: Mapping[str, Any],
    report: Mapping[str, Any],
    artifacts: Mapping[str, tuple[Path, Path, str]],
) -> dict[str, Any]:
    """Validate the immutable apply receipt and all four promoted facts."""

    expected_keys = {
        "schema_version",
        "status",
        "complete",
        "created_at",
        "project_root",
        "run_id",
        "volume",
        "validation_receipt_sha256",
        "manifest_sha256",
        "content_sha256",
        "targets",
        "downstream_required",
        "overwrite_authorized",
        "decision_receipt_path",
        "decision_receipt_sha256",
        "receipt_sha256",
    }
    if set(receipt) != expected_keys:
        raise PlanTransactionError("apply receipt has an invalid shape")
    unsigned = dict(receipt)
    claimed_hash = str(unsigned.pop("receipt_sha256", ""))
    if not _SHA256_RE.fullmatch(claimed_hash) or claimed_hash != _receipt_hash(unsigned):
        raise PlanTransactionError("apply receipt hash mismatch")
    if (
        receipt.get("schema_version") != APPLY_RECEIPT_SCHEMA
        or receipt.get("status") != "applied"
        or receipt.get("complete") is not False
        or not isinstance(receipt.get("created_at"), str)
        or not str(receipt.get("created_at") or "").strip()
        or receipt.get("project_root") != str(root)
        or receipt.get("run_id") != report.get("run_id")
        or receipt.get("volume") != report.get("volume")
        or receipt.get("validation_receipt_sha256") != validation.get("receipt_sha256")
        or receipt.get("manifest_sha256") != report.get("manifest_sha256")
        or receipt.get("content_sha256") != report.get("content_sha256")
        or receipt.get("downstream_required") != list(DOWNSTREAM_STAGES)
        or not isinstance(receipt.get("overwrite_authorized"), bool)
    ):
        raise PlanTransactionError("apply receipt does not bind the current validated run")
    if receipt.get("overwrite_authorized") is True:
        if not _decision_reference_current(
            root,
            run_id=str(report.get("run_id") or ""),
            stage="apply",
            receipt_path=receipt.get("decision_receipt_path"),
            receipt_sha256=receipt.get("decision_receipt_sha256"),
        ):
            raise PlanTransactionError("apply receipt lacks a current trusted overwrite decision")
    elif receipt.get("decision_receipt_path") is not None or receipt.get(
        "decision_receipt_sha256"
    ) is not None:
        raise PlanTransactionError("non-overwrite apply receipt must not bind a decision receipt")
    targets = receipt.get("targets")
    if not isinstance(targets, Mapping) or set(targets) != set(REQUIRED_ARTIFACTS):
        raise PlanTransactionError("apply receipt must bind all promoted plan artifacts")
    for name in REQUIRED_ARTIFACTS:
        item = targets.get(name)
        if not isinstance(item, Mapping) or set(item) != {"path", "sha256"}:
            raise PlanTransactionError(f"apply receipt target is invalid: {name}")
        _, expected_target, expected_sha = artifacts[name]
        if item.get("path") != str(expected_target) or item.get("sha256") != expected_sha:
            raise PlanTransactionError(f"apply receipt target binding mismatch: {name}")
        try:
            raw = _stable_artifact_bytes(root, expected_target, must_exist=True)
        except PlanTransactionError as exc:
            raise PlanTransactionError(
                f"existing apply receipt no longer matches promoted facts: "
                f"promoted plan artifact is stale: {name}"
            ) from exc
        if raw is None or sha256_bytes(raw) != expected_sha:
            raise PlanTransactionError(
                f"existing apply receipt no longer matches promoted facts: "
                f"promoted plan artifact is stale: {name}"
            )
    return dict(receipt)


def apply_validated_plan(
    project_root: str | Path,
    manifest_path: str | Path,
    validation_receipt: Mapping[str, Any] | str | Path,
    *,
    overwrite_token: str | None = None,
    decision_receipt: str | Path | None = None,
) -> dict[str, Any]:
    """Atomically promote only the validated planning artifacts.

    Existing different files are never overwritten without a trusted parent
    decision receipt bound to their exact before/after hashes. A legacy bare
    token remains a public scope challenge and never authorizes replacement.
    Any mid-apply failure restores every old target.
    State/Story System updates are subsequent, separately receipted stages.
    """

    root = _safe_project_root(project_root)
    manifest_file = Path(manifest_path)
    if not manifest_file.is_absolute():
        manifest_file = root / manifest_file
    manifest_file = manifest_file.resolve()
    if isinstance(validation_receipt, (str, Path)):
        receipt_path = Path(validation_receipt)
        if not receipt_path.is_absolute():
            receipt_path = root / receipt_path
        receipt = _read_json(receipt_path.resolve())
    else:
        receipt = dict(validation_receipt)
    report = _verify_validation_receipt(root, receipt, manifest_file)
    run_id = str(report["run_id"])
    runtime_dir = _runtime_dir(root, run_id)
    apply_path = runtime_dir / "apply.json"
    if FileLock is None:
        raise PlanTransactionError("filelock is required for plan apply")

    # Every run targeting the same volume shares this lock. A per-run lock lets
    # two freshly validated runs both observe absent targets and overwrite each
    # other without ever surfacing the authored-fact conflict.
    lock_path = _volume_lifecycle_lock_path(root, report["volume"])
    _require_fixed_path(root, lock_path, expected=lock_path, must_exist=False)
    try:
        with FileLock(str(lock_path), timeout=10):
            _require_fixed_path(root, lock_path, expected=lock_path)
            # Revalidate all caller-controlled and mutable inputs after waiting
            # for the shared volume lock.
            report = _verify_validation_receipt(root, receipt, manifest_file)
            artifacts = _manifest_artifacts(manifest_file, root)
            if apply_path.is_file():
                apply_path = _require_fixed_path(root, apply_path, expected=apply_path)
                existing = _read_json(apply_path)
                return _verify_apply_receipt(root, existing, receipt, report, artifacts)

            source_payloads: dict[Path, bytes] = {}
            snapshots: dict[Path, bytes | None] = {}
            conflicts: dict[str, dict[str, str]] = {}
            for source, target, digest in artifacts.values():
                source_raw = _stable_artifact_bytes(root, source, must_exist=True)
                if source_raw is None or sha256_bytes(source_raw) != digest:
                    raise PlanTransactionError(f"source hash changed before apply: {source}")
                source_payloads[source] = source_raw
                target_raw = _stable_artifact_bytes(root, target, must_exist=False)
                snapshots[target] = target_raw
                if target_raw is not None and sha256_bytes(target_raw) != digest:
                    conflicts[str(target)] = {
                        "before_sha256": sha256_bytes(target_raw),
                        "after_sha256": digest,
                    }

            overwrite_decision: dict[str, Any] | None = None
            if conflicts:
                try:
                    overwrite_decision = _authorize_plan_conflicts(
                        root,
                        receipt,
                        _read_json(manifest_file),
                        stage="apply",
                        conflicts=conflicts,
                        decision_receipt=decision_receipt,
                    )
                except _PlanDecisionNoWrite as selected:
                    return selected.result

            promoted: list[Path] = []
            applied: dict[str, Any] | None = None
            try:
                for source, target, digest in artifacts.values():
                    raw = _stable_artifact_bytes(root, source, must_exist=True)
                    if raw is None or raw != source_payloads[source] or sha256_bytes(raw) != digest:
                        raise PlanTransactionError(f"source hash changed during apply: {source}")
                    current = _stable_artifact_bytes(root, target, must_exist=False)
                    if current != snapshots[target]:
                        if current is not None and sha256_bytes(current) == digest:
                            continue
                        raise PlanTransactionError(f"target changed during apply: {target}")
                    if current is not None and sha256_bytes(current) == digest:
                        continue
                    _atomic_write_bytes(target, raw)
                    promoted.append(target)
                    persisted = _stable_artifact_bytes(root, target, must_exist=True)
                    if persisted is None or sha256_bytes(persisted) != digest:
                        raise PlanTransactionError(f"promoted hash mismatch: {target}")

                targets = {
                    name: {"path": str(target), "sha256": digest}
                    for name, (_, target, digest) in artifacts.items()
                }
                applied = {
                    "schema_version": APPLY_RECEIPT_SCHEMA,
                    "status": "applied",
                    "complete": False,
                    "created_at": _now_iso(),
                    "project_root": str(root),
                    "run_id": run_id,
                    "volume": report["volume"],
                    "validation_receipt_sha256": receipt["receipt_sha256"],
                    "manifest_sha256": report["manifest_sha256"],
                    "content_sha256": report["content_sha256"],
                    "targets": targets,
                    "downstream_required": list(DOWNSTREAM_STAGES),
                    "overwrite_authorized": overwrite_decision is not None,
                    "decision_receipt_path": _decision_reference(overwrite_decision)[0],
                    "decision_receipt_sha256": _decision_reference(overwrite_decision)[1],
                }
                applied["receipt_sha256"] = _receipt_hash(applied)
                _write_receipt_once(apply_path, applied, project_root=root)
                stored = _read_json(_require_fixed_path(root, apply_path, expected=apply_path))
                return _verify_apply_receipt(root, stored, receipt, report, artifacts)
            except Exception as apply_exc:
                rollback_errors: list[str] = []
                for target in reversed(promoted):
                    try:
                        current = _stable_artifact_bytes(root, target, must_exist=False)
                        expected_digest = next(
                            digest
                            for _, candidate, digest in artifacts.values()
                            if candidate == target
                        )
                        old = snapshots[target]
                        if current is None and old is None:
                            continue
                        if current is None or sha256_bytes(current) != expected_digest:
                            raise PlanTransactionError(f"target changed before rollback: {target}")
                        if old is None:
                            target.unlink()
                            _require_fixed_path(root, target, expected=target, must_exist=False)
                            if target.exists() or target.is_symlink():
                                raise PlanTransactionError(f"new target survived rollback: {target}")
                        else:
                            _atomic_write_bytes(target, old)
                            restored = _stable_artifact_bytes(root, target, must_exist=True)
                            if restored != old:
                                raise PlanTransactionError(f"target failed rollback readback: {target}")
                    except Exception as rollback_exc:
                        rollback_errors.append(str(rollback_exc))
                if applied is not None and apply_path.is_file():
                    try:
                        apply_path = _require_fixed_path(root, apply_path, expected=apply_path)
                        if _read_json(apply_path) != applied:
                            raise PlanTransactionError(
                                "apply receipt changed before failed-apply cleanup"
                            )
                        apply_path.unlink()
                        _require_fixed_path(root, apply_path, expected=apply_path, must_exist=False)
                    except Exception as cleanup_exc:
                        rollback_errors.append(str(cleanup_exc))
                if rollback_errors:
                    raise PlanTransactionError(
                        "plan apply failed and rollback was incomplete: " + "; ".join(rollback_errors)
                    ) from apply_exc
                raise
    except Timeout as exc:
        raise PlanTransactionError("plan apply lock is busy") from exc


def _load_bound_plan(root: Path, run_id: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    runtime_dir = _runtime_dir(root, run_id)
    try:
        validation_path = _require_fixed_path(
            root,
            runtime_dir / "validation.json",
            expected=runtime_dir / "validation.json",
        )
        apply_path = _require_fixed_path(
            root,
            runtime_dir / "apply.json",
            expected=runtime_dir / "apply.json",
        )
    except PlanTransactionError as exc:
        raise PlanTransactionError(f"invalid JSON receipt for plan run {run_id}: {exc}") from exc
    validation = _read_json(validation_path)
    apply_receipt = _read_json(apply_path)
    if apply_receipt.get("run_id") != run_id or validation.get("run_id") != run_id:
        raise PlanTransactionError("plan runtime receipts do not bind the requested run")
    report = _verify_validation_receipt(root, validation, str(validation.get("manifest_path") or ""))
    manifest = _read_json(Path(str(report["manifest_path"])))
    artifacts = _manifest_artifacts(Path(str(report["manifest_path"])), root)
    apply_receipt = _verify_apply_receipt(root, apply_receipt, validation, report, artifacts)
    return validation, apply_receipt, manifest


def build_downstream_overwrite_token(
    validation_receipt: Mapping[str, Any],
    *,
    stage: str,
    conflicts: Mapping[str, str],
) -> str:
    payload = {
        "schema_version": "webnovel-plan-downstream-overwrite/v1",
        "validation_receipt_sha256": validation_receipt.get("receipt_sha256"),
        "stage": stage,
        "conflicts": dict(sorted(conflicts.items())),
    }
    return "webnovel-plan-downstream-overwrite:" + sha256_bytes(_canonical_bytes(payload))


def _normalize_decision_conflicts(
    root: Path,
    conflicts: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, str]]:
    normalized: dict[str, dict[str, str]] = {}
    for raw_path, raw_binding in sorted(conflicts.items()):
        path = Path(str(raw_path))
        if not path.is_absolute():
            raise PlanTransactionError("plan decision conflict path must be absolute")
        path = _require_fixed_path(root, path, expected=path)
        if not isinstance(raw_binding, Mapping) or set(raw_binding) != {
            "before_sha256",
            "after_sha256",
        }:
            raise PlanTransactionError("plan decision conflict binding is invalid")
        before = str(raw_binding.get("before_sha256") or "")
        after = str(raw_binding.get("after_sha256") or "")
        if not _SHA256_RE.fullmatch(before) or not _SHA256_RE.fullmatch(after) or before == after:
            raise PlanTransactionError("plan decision conflict hashes are invalid")
        normalized[str(path)] = {"before_sha256": before, "after_sha256": after}
    if not normalized:
        raise PlanTransactionError("plan decision requires at least one authored conflict")
    return normalized


def _decision_scope_payload(
    root: Path,
    validation: Mapping[str, Any],
    manifest: Mapping[str, Any],
    *,
    stage: str,
    conflicts: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    if stage not in PLAN_DECISION_STAGES:
        raise PlanTransactionError(f"invalid plan decision stage: {stage}")
    normalized = _normalize_decision_conflicts(root, conflicts)
    return {
        "schema_version": "webnovel-plan-decision-scope/v1",
        "project_root": str(root),
        "run_id": _require_safe_run_id(manifest.get("run_id")),
        "volume": manifest.get("volume"),
        "stage": stage,
        "validation_receipt_sha256": validation.get("receipt_sha256"),
        "manifest_sha256": validation.get("manifest_sha256"),
        "parent_thread_id": validation.get("parent_thread_id"),
        "parent_model": validation.get("parent_model"),
        "parent_reasoning_effort": validation.get("parent_reasoning_effort"),
        "conflicts": normalized,
    }


def _decision_request_path(root: Path, run_id: str, stage: str, scope_sha256: str) -> Path:
    run_id = _require_safe_run_id(run_id)
    if stage not in PLAN_DECISION_STAGES or not _SHA256_RE.fullmatch(scope_sha256):
        raise PlanTransactionError("invalid plan decision request identity")
    return _runtime_dir(root, run_id) / "decisions" / f"{stage}-{scope_sha256}.request.json"


def _decision_receipt_path(root: Path, run_id: str, stage: str, scope_sha256: str) -> Path:
    return _decision_request_path(root, run_id, stage, scope_sha256).with_suffix(".receipt.json")


def _build_plan_decision_request(
    root: Path,
    validation: Mapping[str, Any],
    manifest: Mapping[str, Any],
    *,
    stage: str,
    conflicts: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    scope = _decision_scope_payload(
        root,
        validation,
        manifest,
        stage=stage,
        conflicts=conflicts,
    )
    scope_sha = _receipt_hash(scope)
    stage_label = "规划文件提升" if stage == "apply" else f"规划下游步骤 {stage}"
    choice_request = build_choice_request(
        [
            {
                "id": "plan_action",
                "prompt": f"{stage_label}发现作者已有内容，请选择唯一处理方式。",
                "options": [
                    {
                        "id": "keep",
                        "label": "保留现有",
                        "description": "保留当前作者内容，本次步骤不写入小说事实。",
                        "recommended": True,
                    },
                    {
                        "id": "replace",
                        "label": "替换为已验证规划",
                        "description": "仅替换本次 hash 绑定范围内的冲突内容。",
                        "recommended": False,
                    },
                    {
                        "id": "cancel",
                        "label": "取消本次规划",
                        "description": "取消本次步骤，不写入小说事实。",
                        "recommended": False,
                    },
                ],
            }
        ]
    )
    marker_payload = {
        "schema_version": "webnovel-plan-decision-marker/v1",
        "project_root": str(root),
        "run_id": scope["run_id"],
        "volume": scope["volume"],
        "stage": stage,
        "scope_sha256": scope_sha,
        "validation_receipt_sha256": scope["validation_receipt_sha256"],
        "parent_thread_id": scope["parent_thread_id"],
        "choice_request_id": choice_request["request_id"],
        "choice_request_sha256": _receipt_hash(choice_request),
    }
    return {
        "schema_version": PLAN_DECISION_REQUEST_SCHEMA,
        "scope": scope,
        "scope_sha256": scope_sha,
        "choice_request": choice_request,
        "binding_marker": PLAN_DECISION_MARKER_PREFIX
        + _canonical_bytes(marker_payload).decode("utf-8"),
    }


def _store_plan_decision_request(root: Path, request: Mapping[str, Any]) -> Path:
    scope = request.get("scope")
    if not isinstance(scope, Mapping):
        raise PlanTransactionError("plan decision request scope is missing")
    path = _decision_request_path(
        root,
        str(scope.get("run_id") or ""),
        str(scope.get("stage") or ""),
        str(request.get("scope_sha256") or ""),
    )
    _write_plan_decision_file_under_lifecycle_lock(root, path, request)
    stored, _ = _read_bounded_json(path, max_bytes=MAX_REQUEST_BYTES)
    if stored != dict(request):
        raise PlanTransactionError("plan decision request failed exact readback")
    return path


def _load_plan_decision_request(root: Path, request_file: str | Path) -> tuple[dict[str, Any], Path, bytes]:
    path = Path(request_file)
    if not path.is_absolute():
        raise PlanTransactionError("plan decision request path must be absolute")
    request, raw = _read_bounded_json(path, max_bytes=MAX_REQUEST_BYTES)
    if set(request) != {
        "schema_version",
        "scope",
        "scope_sha256",
        "choice_request",
        "binding_marker",
    } or request.get("schema_version") != PLAN_DECISION_REQUEST_SCHEMA:
        raise PlanTransactionError("plan decision request has an invalid shape")
    scope = request.get("scope")
    if not isinstance(scope, Mapping) or set(scope) != {
        "schema_version",
        "project_root",
        "run_id",
        "volume",
        "stage",
        "validation_receipt_sha256",
        "manifest_sha256",
        "parent_thread_id",
        "parent_model",
        "parent_reasoning_effort",
        "conflicts",
    }:
        raise PlanTransactionError("plan decision request scope has an invalid shape")
    stage = str(scope.get("stage") or "")
    scope_sha = str(request.get("scope_sha256") or "")
    expected_path = _decision_request_path(root, str(scope.get("run_id") or ""), stage, scope_sha)
    path = _require_fixed_path(root, path, expected=expected_path)
    conflicts = scope.get("conflicts")
    if not isinstance(conflicts, Mapping):
        raise PlanTransactionError("plan decision conflicts must be an object")
    normalized_scope = dict(scope)
    normalized_scope["conflicts"] = _normalize_decision_conflicts(root, conflicts)
    if (
        normalized_scope.get("schema_version") != "webnovel-plan-decision-scope/v1"
        or normalized_scope.get("project_root") != str(root)
        or not _SHA256_RE.fullmatch(scope_sha)
        or scope_sha != _receipt_hash(normalized_scope)
        or normalized_scope != dict(scope)
        or not isinstance(request.get("binding_marker"), str)
        or not str(request.get("binding_marker") or "").startswith(PLAN_DECISION_MARKER_PREFIX)
    ):
        raise PlanTransactionError("plan decision request scope hash mismatch")
    choice_request = request.get("choice_request")
    try:
        unresolved = resolve_choice(choice_request if isinstance(choice_request, Mapping) else {}, None)
    except ChoiceProtocolError as exc:
        raise PlanTransactionError(f"plan decision finite-choice request is invalid: {exc}") from exc
    if unresolved.get("status") != "awaiting_user":
        raise PlanTransactionError("plan decision request must remain unanswered")
    return request, path, raw


def _plan_rollout_message(payload: Mapping[str, Any]) -> tuple[str, str] | None:
    message = payload.get("item") if isinstance(payload.get("item"), Mapping) else payload
    if not isinstance(message, Mapping) or message.get("type") != "message":
        return None
    role = str(message.get("role") or "")
    content = message.get("content")
    if isinstance(content, str):
        return (role, content) if content else None
    if not isinstance(content, list):
        return None
    texts = [
        str(item.get("text"))
        for item in content
        if isinstance(item, Mapping)
        and item.get("type") in {"input_text", "output_text", "text"}
        and isinstance(item.get("text"), str)
    ]
    return (role, "".join(texts)) if texts else None


def _resolve_plan_parent_choice(
    validation: Mapping[str, Any],
    request: Mapping[str, Any],
) -> tuple[str, dict[str, str]]:
    rollout_path = Path(str(validation.get("parent_rollout_path") or ""))
    rollout_path = _require_trusted_file(TRUSTED_CODEX_SESSIONS_ROOT, rollout_path)
    thread_id = _require_current_parent_rollout(
        str(validation.get("parent_thread_id") or ""),
        rollout_path,
    )
    raw = _read_bounded_bytes(rollout_path, max_bytes=MAX_EVIDENCE_BYTES)
    _require_trusted_file(TRUSTED_CODEX_SESSIONS_ROOT, rollout_path)
    identity = _parse_parent_identity(
        raw,
        rollout_path=rollout_path,
        thread_id=thread_id,
        expected_model=str(validation.get("parent_model") or ""),
        expected_effort=str(validation.get("parent_reasoning_effort") or ""),
    )
    marker = str(request.get("binding_marker") or "")
    records: list[tuple[int, Mapping[str, Any]]] = []
    offset = 0
    try:
        for line in raw.splitlines(keepends=True):
            offset += len(line)
            if not line.strip():
                continue
            event = json.loads(line.decode("utf-8"))
            if not isinstance(event, Mapping):
                raise ValueError("rollout event must be an object")
            records.append((offset, event))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise PlanTransactionError("parent decision rollout is not UTF-8 JSONL") from exc
    marker_indexes: list[int] = []
    for index, (_, event) in enumerate(records):
        if event.get("type") != "response_item" or not isinstance(event.get("payload"), Mapping):
            continue
        parsed = _plan_rollout_message(event["payload"])
        if parsed is not None and parsed[0] == "assistant" and marker in [
            line.strip() for line in parsed[1].splitlines()
        ]:
            marker_indexes.append(index)
    if len(marker_indexes) != 1:
        raise PlanTransactionError(
            "parent rollout must contain exactly one exact plan decision marker"
        )
    answer = ""
    answer_end = 0
    for end, event in records[marker_indexes[0] + 1 :]:
        if event.get("type") != "response_item" or not isinstance(event.get("payload"), Mapping):
            continue
        parsed = _plan_rollout_message(event["payload"])
        if parsed is None or not parsed[1].strip():
            continue
        if parsed[0] != "user":
            raise PlanTransactionError(
                "the next durable message after the plan decision marker is not a user answer"
            )
        answer = parsed[1].strip()
        answer_end = end
        break
    if not answer or answer_end <= 0 or len(answer.encode("utf-8")) > 4096:
        raise PlanTransactionError("parent rollout has no bounded user answer after the plan decision marker")
    choice_request = request.get("choice_request")
    try:
        resolution = resolve_choice(
            choice_request if isinstance(choice_request, Mapping) else {},
            answer,
        )
    except ChoiceProtocolError as exc:
        raise PlanTransactionError(f"plan decision answer is invalid: {exc}") from exc
    selected = (resolution.get("selected_branches") or {}).get("plan_action")
    if (
        resolution.get("status") != "selected"
        or resolution.get("write_allowed") is not True
        or selected not in PLAN_DECISION_CHOICES
    ):
        raise PlanTransactionError("user answer did not select one offered plan decision")
    return str(selected), {
        "parent_thread_id": identity["thread_id"],
        "parent_model": identity["model"],
        "parent_reasoning_effort": identity["effort"],
        "parent_rollout_path": str(rollout_path.resolve(strict=True)),
        "authorization_prefix_sha256": sha256_bytes(raw[:answer_end]),
        "binding_marker_sha256": sha256_bytes(marker.encode("utf-8")),
        "answer_sha256": sha256_bytes(answer.encode("utf-8")),
        "choice_request_id": str(resolution.get("request_id") or ""),
    }


def _load_validation_for_decision(
    root: Path,
    run_id: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    validation = _read_json(_runtime_dir(root, run_id) / "validation.json")
    if validation.get("run_id") != run_id:
        raise PlanTransactionError("plan decision validation belongs to another run")
    report = _verify_validation_receipt(
        root,
        validation,
        str(validation.get("manifest_path") or ""),
    )
    manifest = _read_json(Path(str(report["manifest_path"])))
    return validation, report, manifest


def _verify_plan_decision_receipt(
    root: Path,
    receipt_file: str | Path,
    *,
    expected_request: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    path = Path(receipt_file)
    if not path.is_absolute():
        raise PlanTransactionError("plan decision receipt path must be absolute")
    receipt, _ = _read_bounded_json(path, max_bytes=MAX_REQUEST_BYTES)
    expected_keys = {
        "schema_version",
        "status",
        "created_at",
        "project_root",
        "run_id",
        "volume",
        "stage",
        "validation_receipt_sha256",
        "manifest_sha256",
        "scope_sha256",
        "request_path",
        "request_sha256",
        "selected",
        "parent_thread_id",
        "parent_model",
        "parent_reasoning_effort",
        "parent_rollout_path",
        "authorization_prefix_sha256",
        "binding_marker_sha256",
        "answer_sha256",
        "choice_request_id",
        "receipt_path",
        "receipt_sha256",
    }
    if set(receipt) != expected_keys:
        raise PlanTransactionError("plan decision receipt has an invalid shape")
    unsigned = dict(receipt)
    claimed = str(unsigned.pop("receipt_sha256", ""))
    if not _SHA256_RE.fullmatch(claimed) or claimed != _receipt_hash(unsigned):
        raise PlanTransactionError("plan decision receipt hash mismatch")
    run_id = _require_safe_run_id(receipt.get("run_id"))
    stage = str(receipt.get("stage") or "")
    scope_sha = str(receipt.get("scope_sha256") or "")
    expected_path = _decision_receipt_path(root, run_id, stage, scope_sha)
    path = _require_fixed_path(root, path, expected=expected_path)
    if receipt.get("receipt_path") != str(path):
        raise PlanTransactionError("plan decision receipt path binding mismatch")
    request, request_path, request_raw = _load_plan_decision_request(
        root,
        str(receipt.get("request_path") or ""),
    )
    if expected_request is not None and request != dict(expected_request):
        raise PlanTransactionError("plan decision receipt belongs to a stale conflict scope")
    validation, _, manifest = _load_validation_for_decision(root, run_id)
    scope = request["scope"]
    if (
        receipt.get("schema_version") != PLAN_DECISION_RECEIPT_SCHEMA
        or receipt.get("status") != "selected"
        or not isinstance(receipt.get("created_at"), str)
        or not str(receipt.get("created_at") or "").strip()
        or receipt.get("project_root") != str(root)
        or receipt.get("volume") != manifest.get("volume")
        or receipt.get("stage") != scope.get("stage")
        or receipt.get("validation_receipt_sha256") != validation.get("receipt_sha256")
        or receipt.get("manifest_sha256") != validation.get("manifest_sha256")
        or receipt.get("scope_sha256") != request.get("scope_sha256")
        or receipt.get("request_path") != str(request_path)
        or receipt.get("request_sha256") != sha256_bytes(request_raw)
        or receipt.get("selected") not in PLAN_DECISION_CHOICES
    ):
        raise PlanTransactionError("plan decision receipt does not bind the current validated run")
    selected, proof = _resolve_plan_parent_choice(validation, request)
    if selected != receipt.get("selected") or any(receipt.get(key) != value for key, value in proof.items()):
        raise PlanTransactionError("plan decision receipt no longer matches its trusted parent rollout prefix")
    return receipt


def _current_plan_decision_request(
    root: Path,
    validation: Mapping[str, Any],
    manifest: Mapping[str, Any],
    *,
    stage: str,
) -> dict[str, Any]:
    conflicts = _current_plan_conflicts(root, validation, manifest, stage=stage)
    if not conflicts:
        raise PlanTransactionError("plan decision conflict scope is no longer current")
    return _build_plan_decision_request(
        root,
        validation,
        manifest,
        stage=stage,
        conflicts=conflicts,
    )


def create_plan_decision_receipt(
    project_root: str | Path,
    request_file: str | Path,
) -> dict[str, Any]:
    """Resolve one finite choice from the current trusted parent rollout."""

    root = _safe_project_root(project_root)
    initial, _, _ = _load_plan_decision_request(root, request_file)
    scope = initial["scope"]
    run_id = _require_safe_run_id(scope.get("run_id"))
    stage = str(scope.get("stage") or "")
    volume = scope.get("volume")
    if FileLock is None:
        raise PlanTransactionError("filelock is required for plan decisions")
    lock_path = _volume_lifecycle_lock_path(root, volume)
    try:
        with FileLock(str(lock_path), timeout=10):
            request, request_path, request_raw = _load_plan_decision_request(root, request_file)
            validation, _, manifest = _load_validation_for_decision(root, run_id)
            if stage != "apply":
                _load_bound_plan(root, run_id)
                _check_stage_order(root, run_id, stage)
            expected = _current_plan_decision_request(
                root,
                validation,
                manifest,
                stage=stage,
            )
            if request != expected:
                raise PlanTransactionError("plan decision request belongs to a stale conflict scope")
            selected, proof = _resolve_plan_parent_choice(validation, request)
            receipt_path = _decision_receipt_path(
                root,
                run_id,
                stage,
                str(request["scope_sha256"]),
            )
            receipt = {
                "schema_version": PLAN_DECISION_RECEIPT_SCHEMA,
                "status": "selected",
                "created_at": _now_iso(),
                "project_root": str(root),
                "run_id": run_id,
                "volume": manifest["volume"],
                "stage": stage,
                "validation_receipt_sha256": validation["receipt_sha256"],
                "manifest_sha256": validation["manifest_sha256"],
                "scope_sha256": request["scope_sha256"],
                "request_path": str(request_path),
                "request_sha256": sha256_bytes(request_raw),
                "selected": selected,
                **proof,
                "receipt_path": str(receipt_path),
            }
            receipt["receipt_sha256"] = _receipt_hash(receipt)
            if receipt_path.is_file():
                return _verify_plan_decision_receipt(
                    root,
                    receipt_path,
                    expected_request=request,
                )
            _write_plan_decision_file_under_lifecycle_lock(root, receipt_path, receipt)
            return _verify_plan_decision_receipt(
                root,
                receipt_path,
                expected_request=request,
            )
    except Timeout as exc:
        raise PlanTransactionError("plan lifecycle lock is busy") from exc


def _prepare_plan_decision(
    root: Path,
    validation: Mapping[str, Any],
    manifest: Mapping[str, Any],
    *,
    stage: str,
    conflicts: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    request = _build_plan_decision_request(
        root,
        validation,
        manifest,
        stage=stage,
        conflicts=conflicts,
    )
    request_path = _store_plan_decision_request(root, request)
    normalized = request["scope"]["conflicts"]
    return {
        "decision_request_file": str(request_path),
        "scope_challenge": "webnovel-plan-decision:" + str(request["scope_sha256"]),
        "paths": list(normalized),
        "choice_request": request["choice_request"],
        "binding_marker": request["binding_marker"],
    }


def _authorize_plan_conflicts(
    root: Path,
    validation: Mapping[str, Any],
    manifest: Mapping[str, Any],
    *,
    stage: str,
    conflicts: Mapping[str, Mapping[str, Any]],
    decision_receipt: str | Path | None,
) -> dict[str, Any]:
    decision = _prepare_plan_decision(
        root,
        validation,
        manifest,
        stage=stage,
        conflicts=conflicts,
    )
    if decision_receipt is None or not str(decision_receipt).strip():
        if stage == "apply":
            raise PlanApplyChoiceRequired(decision)
        raise PlanDownstreamChoiceRequired(stage=stage, decision=decision)
    expected_request, _, _ = _load_plan_decision_request(
        root,
        decision["decision_request_file"],
    )
    receipt = _verify_plan_decision_receipt(
        root,
        decision_receipt,
        expected_request=expected_request,
    )
    if receipt["selected"] != "replace":
        status = "kept_existing" if receipt["selected"] == "keep" else "cancelled"
        raise _PlanDecisionNoWrite(
            {
                "schema_version": "webnovel-plan-decision-result/v1",
                "status": status,
                "run_id": manifest["run_id"],
                "volume": manifest["volume"],
                "stage": stage,
                "facts_changed": False,
                "decision_receipt_path": receipt["receipt_path"],
                "decision_receipt_sha256": receipt["receipt_sha256"],
            }
        )
    return receipt


def _decision_reference(receipt: Mapping[str, Any] | None) -> tuple[str | None, str | None]:
    if receipt is None:
        return None, None
    return str(receipt.get("receipt_path") or ""), str(receipt.get("receipt_sha256") or "")


def _decision_reference_current(
    root: Path,
    *,
    run_id: str,
    stage: str,
    receipt_path: object,
    receipt_sha256: object,
) -> bool:
    if receipt_path is None and receipt_sha256 is None:
        return True
    if not isinstance(receipt_path, str) or not isinstance(receipt_sha256, str):
        return False
    try:
        receipt = _verify_plan_decision_receipt(root, receipt_path)
    except PlanTransactionError:
        return False
    return bool(
        receipt.get("run_id") == run_id
        and receipt.get("stage") == stage
        and receipt.get("selected") == "replace"
        and receipt.get("receipt_sha256") == receipt_sha256
    )


def _check_stage_order(root: Path, run_id: str, stage: str) -> None:
    runtime_dir = _runtime_dir(root, run_id)
    apply_receipt_sha256 = ""
    if DOWNSTREAM_STAGES.index(stage) > 0:
        apply_receipt = _read_json(runtime_dir / "apply.json")
        apply_receipt_sha256 = str(apply_receipt.get("receipt_sha256") or "")
    for earlier in DOWNSTREAM_STAGES[: DOWNSTREAM_STAGES.index(stage)]:
        attempts = sorted(runtime_dir.glob(f"stage-{earlier}-*.json"))
        if not attempts:
            raise PlanTransactionError(f"downstream stage out of order: {stage} before {earlier}")
        receipt = _read_json(attempts[-1])
        if not _stage_receipt_current(
            root,
            receipt,
            run_id=run_id,
            stage=earlier,
            apply_receipt_sha256=apply_receipt_sha256,
        ):
            raise PlanTransactionError(f"downstream stage out of order: {stage} before {earlier}")


def _stage_outputs_current(receipt: Mapping[str, Any], *, root: Path | None = None) -> bool:
    unsigned = dict(receipt)
    claimed = str(unsigned.pop("receipt_sha256", ""))
    if claimed != _receipt_hash(unsigned):
        return False
    outputs = receipt.get("outputs")
    if not isinstance(outputs, Mapping):
        return False
    for item in outputs.values():
        if not isinstance(item, Mapping) or set(item) != {"path", "sha256"}:
            return False
        path = Path(str(item.get("path") or ""))
        try:
            if root is not None:
                path = _require_fixed_path(root, path, expected=path)
                raw = _stable_artifact_bytes(root, path, must_exist=True)
                current_sha = sha256_bytes(raw) if raw is not None else ""
            else:
                current_sha = file_sha256(path) if path.is_file() else ""
            if current_sha != item.get("sha256"):
                return False
        except (OSError, PlanTransactionError):
            return False
    return True


def _stage_output_paths_match(
    receipt: Mapping[str, Any],
    expected: Mapping[str, Path],
) -> bool:
    outputs = receipt.get("outputs")
    if not isinstance(outputs, Mapping) or set(outputs) != set(expected):
        return False
    return all(
        isinstance(outputs.get(name), Mapping)
        and outputs[name].get("path") == str(path)
        for name, path in expected.items()
    )


def _stage_truth_current(
    root: Path,
    receipt: Mapping[str, Any],
    *,
    run_id: str,
    stage: str,
) -> bool:
    """Rebuild one stage's fixed truth instead of trusting receipt outputs."""

    try:
        validation, _, manifest = _load_bound_plan(root, run_id)
        volume = int(manifest["volume"])
        verification = receipt.get("verification")
        if not isinstance(verification, Mapping):
            return False

        if stage == "master_outline":
            master = root / "大纲" / "总纲.md"
            writeback = root / "大纲" / f"第{volume}卷-总纲写回.json"
            backup = _runtime_dir(root, run_id) / "master-outline.before"
            expected_paths = {
                "master_outline": master,
                "writeback": writeback,
                "master_outline_backup": backup,
            }
            if not _stage_output_paths_match(receipt, expected_paths):
                return False
            master_raw = _stable_artifact_bytes(
                root, master, must_exist=True, max_bytes=8 * 1024 * 1024
            )
            backup_raw = _stable_artifact_bytes(
                root, backup, must_exist=True, max_bytes=8 * 1024 * 1024
            )
            if master_raw is None or backup_raw is None:
                return False
            before = backup_raw.decode("utf-8")
            payload = _read_json(_require_fixed_path(root, writeback, expected=writeback))
            anchor = _normalize_anchor(payload, volume + 1)
            items = _structured_writeback_items(payload)
            candidate, _ = _update_volume_table(before, anchor)
            candidate, _ = _append_foreshadow_rows(candidate, items)
            candidate_raw = candidate.encode("utf-8")
            expected_verification = {
                "next_volume": volume + 1,
                "anchor": anchor,
                "writeback_sha256": file_sha256(writeback),
                "before_sha256": sha256_bytes(backup_raw),
                "after_sha256": sha256_bytes(candidate_raw),
            }
            return master_raw == candidate_raw and dict(verification) == expected_verification

        if stage == "state":
            state_path, state = _read_state(root)
            start, end = [int(value) for value in manifest["chapter_range"]]
            exact = {
                "volume": volume,
                "chapters_range": f"{start}-{end}",
                "plan_run_id": str(manifest["run_id"]),
                "plan_content_sha256": str(manifest["content_sha256"]),
                "plan_manifest_sha256": str(validation["manifest_sha256"]),
            }
            entries = [
                item
                for item in state["progress"]["volumes_planned"]
                if item.get("volume") == volume
            ]
            return bool(
                _stage_output_paths_match(receipt, {"state": state_path})
                and len(entries) == 1
                and all(entries[0].get(key) == value for key, value in exact.items())
                and dict(verification) == {"planned_entry": exact}
            )

        if stage == "contracts":
            expected_contracts = _expected_contracts(root, validation, manifest)
            expected_paths = {
                f"contract_{index:04d}": path
                for index, path in enumerate(sorted(expected_contracts), 1)
            }
            expected_paths["master_setting"] = root / ".story-system" / "MASTER_SETTING.json"
            if not _stage_output_paths_match(receipt, expected_paths):
                return False
            for path, payload in expected_contracts.items():
                if _read_json(_require_fixed_path(root, path, expected=path)) != payload:
                    return False
            expected_verification = {
                "chapter_range": manifest["chapter_range"],
                "contract_count": len(expected_contracts) + 1,
                "binding": _contract_binding(validation, manifest),
            }
            return dict(verification) == expected_verification

        if stage == "prewrite":
            chapter = int(manifest["chapter_range"][0])
            expected_paths = contract_files_for_chapter(root, chapter)
            expected_paths["state"] = root / ".webnovel" / "state.json"
            if not _stage_output_paths_match(receipt, expected_paths):
                return False
            report = run_write_gate(root, chapter=chapter, stage="prewrite")
            if (
                report.get("ok") is not True
                or report.get("chapter") != chapter
                or report.get("stage") != "prewrite"
            ):
                return False
            expected_verification = {
                "chapter": chapter,
                "gate_sha256": sha256_bytes(_canonical_bytes(report)),
                "gate_report": report,
            }
            return dict(verification) == expected_verification
    except Exception:
        return False
    return False


def _stage_receipt_current(
    root: Path,
    receipt: Mapping[str, Any],
    *,
    run_id: str,
    stage: str,
    apply_receipt_sha256: str,
) -> bool:
    expected_keys = {
        "schema_version",
        "run_id",
        "stage",
        "status",
        "created_at",
        "apply_receipt_sha256",
        "outputs",
        "verification",
        "decision_receipt_path",
        "decision_receipt_sha256",
        "detail",
        "receipt_sha256",
    }
    return bool(
        set(receipt) == expected_keys
        and receipt.get("schema_version") == STAGE_RECEIPT_SCHEMA
        and receipt.get("run_id") == run_id
        and receipt.get("stage") == stage
        and receipt.get("status") == "completed"
        and isinstance(receipt.get("created_at"), str)
        and bool(str(receipt.get("created_at") or "").strip())
        and receipt.get("apply_receipt_sha256") == apply_receipt_sha256
        and isinstance(receipt.get("outputs"), Mapping)
        and bool(receipt.get("outputs"))
        and isinstance(receipt.get("verification"), Mapping)
        and _decision_reference_current(
            root,
            run_id=run_id,
            stage=stage,
            receipt_path=receipt.get("decision_receipt_path"),
            receipt_sha256=receipt.get("decision_receipt_sha256"),
        )
        and receipt.get("detail") == ""
        and _stage_outputs_current(receipt, root=root)
        and _stage_truth_current(root, receipt, run_id=run_id, stage=stage)
    )


def record_downstream_stage(
    project_root: str | Path,
    run_id: str,
    *,
    stage: str,
    status: str,
    outputs: Mapping[str, str | Path] | None = None,
    detail: str = "",
    verification: Mapping[str, Any] | None = None,
    decision_receipt: Mapping[str, Any] | None = None,
    _verified_token: object | None = None,
) -> dict[str, Any]:
    """Internal receipt writer; production callers must use ``run_downstream_stage``."""

    if _verified_token is not _VERIFIED_DOWNSTREAM_TOKEN:
        raise PlanTransactionError("direct downstream receipts are forbidden; use the truth-source stage runner")
    root = _safe_project_root(project_root)
    if stage not in DOWNSTREAM_STAGES:
        raise PlanTransactionError(f"unknown downstream stage: {stage}")
    if status not in {"completed", "failed"}:
        raise PlanTransactionError("downstream status must be completed or failed")
    runtime_dir = _runtime_dir(root, run_id)
    apply_receipt = _read_json(runtime_dir / "apply.json")
    _check_stage_order(root, run_id, stage)
    signatures: dict[str, dict[str, Any]] = {}
    for name, value in (outputs or {}).items():
        path = _require_fixed_path(root, Path(value), expected=Path(value))
        signatures[str(name)] = {"path": str(path), "sha256": file_sha256(path)}
    receipt = {
        "schema_version": STAGE_RECEIPT_SCHEMA,
        "run_id": run_id,
        "stage": stage,
        "status": status,
        "created_at": _now_iso(),
        "apply_receipt_sha256": apply_receipt.get("receipt_sha256"),
        "outputs": signatures,
        "verification": dict(verification or {}),
        "decision_receipt_path": _decision_reference(decision_receipt)[0],
        "decision_receipt_sha256": _decision_reference(decision_receipt)[1],
        "detail": str(detail or ""),
    }
    receipt["receipt_sha256"] = _receipt_hash(receipt)
    if status == "completed" and not _stage_receipt_current(
        root,
        receipt,
        run_id=run_id,
        stage=stage,
        apply_receipt_sha256=str(apply_receipt.get("receipt_sha256") or ""),
    ):
        raise PlanTransactionError(f"downstream truth-source receipt is invalid: {stage}")
    attempts = sorted(runtime_dir.glob(f"stage-{stage}-*.json"))
    if attempts:
        latest = _read_json(attempts[-1])
        if latest.get("status") == "completed":
            comparable = (
                "outputs",
                "verification",
                "decision_receipt_path",
                "decision_receipt_sha256",
                "detail",
            )
            if all(latest.get(key) == receipt.get(key) for key in comparable) and _stage_receipt_current(
                root,
                latest,
                run_id=run_id,
                stage=stage,
                apply_receipt_sha256=str(apply_receipt.get("receipt_sha256") or ""),
            ):
                return latest
            raise PlanTransactionError(f"completed downstream stage is immutable: {stage}")
    attempt = len(attempts) + 1
    _write_receipt_once(
        runtime_dir / f"stage-{stage}-{attempt:03d}.json",
        receipt,
        project_root=root,
    )
    return receipt


def _read_state(root: Path) -> tuple[Path, dict[str, Any]]:
    path = _require_fixed_path(
        root,
        root / ".webnovel" / "state.json",
        expected=root / ".webnovel" / "state.json",
    )
    payload, _ = _read_bounded_json(path, max_bytes=8 * 1024 * 1024)
    progress = payload.get("progress")
    if not isinstance(progress, dict):
        raise PlanTransactionError("state progress must be an object")
    planned = progress.get("volumes_planned", [])
    if not isinstance(planned, list) or not all(isinstance(item, dict) for item in planned):
        raise PlanTransactionError("state progress.volumes_planned must be a list of objects")
    return path, payload


def _master_outline_candidate(root: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    volume = int(manifest["volume"])
    master = _require_fixed_path(
        root,
        root / "大纲" / "总纲.md",
        expected=root / "大纲" / "总纲.md",
    )
    writeback = _require_fixed_path(
        root,
        root / "大纲" / f"第{volume}卷-总纲写回.json",
        expected=root / "大纲" / f"第{volume}卷-总纲写回.json",
    )
    payload = _read_json(writeback)
    try:
        anchor = _normalize_anchor(payload, volume + 1)
        items = _structured_writeback_items(payload)
    except MasterOutlineSyncError as exc:
        raise PlanTransactionError(str(exc)) from exc
    before_raw = _stable_artifact_bytes(
        root,
        master,
        must_exist=True,
        max_bytes=8 * 1024 * 1024,
    )
    if before_raw is None:
        raise PlanTransactionError("master outline disappeared before writeback")
    try:
        before = before_raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PlanTransactionError("master outline is not valid UTF-8") from exc
    candidate, _ = _update_volume_table(before, anchor)
    candidate, _ = _append_foreshadow_rows(candidate, items)
    candidate_raw = candidate.encode("utf-8")
    existing_row = any(
        line.strip().startswith("|")
        and line.strip().strip("|").split("|", 1)[0].strip() == str(volume + 1)
        for line in before.splitlines()
    )
    conflicts = {}
    if existing_row and candidate_raw != before_raw:
        conflicts[str(master)] = {
            "before_sha256": sha256_bytes(before_raw),
            "after_sha256": sha256_bytes(candidate_raw),
        }
    return {
        "volume": volume,
        "master": master,
        "writeback": writeback,
        "anchor": anchor,
        "items": items,
        "before_raw": before_raw,
        "candidate": candidate,
        "candidate_raw": candidate_raw,
        "conflicts": conflicts,
    }


def _master_outline_stage(
    root: Path,
    validation: Mapping[str, Any],
    manifest: Mapping[str, Any],
    *,
    overwrite_token: str | None,
    decision_receipt: str | Path | None = None,
) -> tuple[dict[str, Path], dict[str, Any], dict[str, Any] | None]:
    inputs = _master_outline_candidate(root, manifest)
    volume = int(inputs["volume"])
    master = Path(inputs["master"])
    writeback = Path(inputs["writeback"])
    anchor = inputs["anchor"]
    before_raw = bytes(inputs["before_raw"])
    candidate = str(inputs["candidate"])
    candidate_raw = bytes(inputs["candidate_raw"])
    conflicts = inputs["conflicts"]
    overwrite_decision = None
    if conflicts:
        overwrite_decision = _authorize_plan_conflicts(
            root,
            validation,
            manifest,
            stage="master_outline",
            conflicts=conflicts,
            decision_receipt=decision_receipt,
        )

    backup_path = _runtime_dir(root, str(manifest["run_id"])) / "master-outline.before"
    _require_fixed_path(root, backup_path, expected=backup_path, must_exist=False)
    _atomic_write_bytes(backup_path, before_raw)
    backup_raw = _stable_artifact_bytes(
        root,
        backup_path,
        must_exist=True,
        max_bytes=8 * 1024 * 1024,
    )
    if backup_raw != before_raw:
        raise PlanTransactionError("master outline backup failed exact readback")
    try:
        sync_master_outline(root, volume, writeback_file=writeback)
        after_raw = _stable_artifact_bytes(root, master, must_exist=True, max_bytes=8 * 1024 * 1024)
        if after_raw is None:
            raise PlanTransactionError(
                "master outline writeback did not persist the exact structured result"
            )
        after_text = after_raw.decode("utf-8")
        if after_text.replace("\r\n", "\n") != candidate.replace("\r\n", "\n"):
            raise PlanTransactionError(
                "master outline writeback did not persist the exact structured result"
            )
        if after_raw != candidate_raw:
            _atomic_write_bytes(master, candidate_raw)
            after_raw = _stable_artifact_bytes(
                root,
                master,
                must_exist=True,
                max_bytes=8 * 1024 * 1024,
            )
        if after_raw != candidate_raw:
            raise PlanTransactionError("master outline failed atomic exact readback")
    except Exception as exc:
        rollback_errors: list[str] = []
        try:
            current_raw = _stable_artifact_bytes(
                root,
                master,
                must_exist=False,
                max_bytes=8 * 1024 * 1024,
            )
            if current_raw != before_raw:
                _atomic_write_bytes(master, before_raw)
            restored = _stable_artifact_bytes(
                root,
                master,
                must_exist=True,
                max_bytes=8 * 1024 * 1024,
            )
            if restored != before_raw:
                raise PlanTransactionError("master outline failed rollback readback")
        except Exception as rollback_exc:
            rollback_errors.append(str(rollback_exc))
        if rollback_errors:
            raise PlanTransactionError(
                "master outline stage failed and rollback was incomplete: "
                + "; ".join(rollback_errors)
            ) from exc
        if isinstance(exc, PlanTransactionError):
            raise
        if isinstance(exc, MasterOutlineSyncError):
            raise PlanTransactionError(str(exc)) from exc
        raise PlanTransactionError(f"master outline stage failed: {exc}") from exc
    return {
        "master_outline": master,
        "writeback": writeback,
        "master_outline_backup": backup_path,
    }, {
        "next_volume": volume + 1,
        "anchor": anchor,
        "writeback_sha256": file_sha256(writeback),
        "before_sha256": sha256_bytes(before_raw),
        "after_sha256": sha256_bytes(candidate_raw),
    }, overwrite_decision


def _state_candidate(
    root: Path,
    validation: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    state_path, _ = _read_state(root)
    state_before_raw = _stable_artifact_bytes(
        root,
        state_path,
        must_exist=True,
        max_bytes=8 * 1024 * 1024,
    )
    if state_before_raw is None:
        raise PlanTransactionError("state disappeared before planning update")
    try:
        state = json.loads(state_before_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlanTransactionError("state is not valid UTF-8 JSON") from exc
    if not isinstance(state, dict) or not isinstance(state.get("progress"), dict):
        raise PlanTransactionError("state progress must be an object")
    progress = state["progress"]
    planned = progress.get("volumes_planned", [])
    if not isinstance(planned, list) or not all(isinstance(item, dict) for item in planned):
        raise PlanTransactionError("state progress.volumes_planned must be a list of objects")
    volume = int(manifest["volume"])
    start, end = [int(value) for value in manifest["chapter_range"]]
    exact = {
        "volume": volume,
        "chapters_range": f"{start}-{end}",
        "plan_run_id": str(manifest["run_id"]),
        "plan_content_sha256": str(manifest["content_sha256"]),
        "plan_manifest_sha256": str(validation["manifest_sha256"]),
    }
    matching = [item for item in planned if item.get("volume") == volume]
    if len(matching) > 1:
        raise PlanTransactionError("state contains duplicate volumes_planned entries")
    conflict = bool(matching and any(matching[0].get(key) != value for key, value in exact.items()))
    changed = not matching or conflict
    if conflict:
        entry = matching[0]
        entry.update(exact)
        entry.setdefault("planned_at", datetime.now(timezone.utc).date().isoformat())
        entry["updated_at"] = datetime.now(timezone.utc).date().isoformat()
    elif not matching:
        planned.append({**exact, "planned_at": datetime.now(timezone.utc).date().isoformat()})
    if changed:
        progress["volumes_planned"] = planned
    candidate_raw = json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8")
    conflicts = {}
    if conflict:
        conflicts[str(state_path)] = {
            "before_sha256": sha256_bytes(state_before_raw),
            "after_sha256": sha256_bytes(candidate_raw),
        }
    return {
        "state_path": state_path,
        "state": state,
        "before_raw": state_before_raw,
        "candidate_raw": candidate_raw,
        "exact": exact,
        "changed": changed,
        "conflicts": conflicts,
    }


def _state_stage(
    root: Path,
    validation: Mapping[str, Any],
    manifest: Mapping[str, Any],
    *,
    overwrite_token: str | None,
    decision_receipt: str | Path | None = None,
) -> tuple[dict[str, Path], dict[str, Any], dict[str, Any] | None]:
    state_path, _ = _read_state(root)
    lock_path = state_path.with_suffix(state_path.suffix + ".lock")
    _prepare_atomic_json_target(root, state_path)
    if FileLock is None:
        raise PlanTransactionError("filelock is required for state planning update")
    try:
        with FileLock(str(lock_path), timeout=10):
            _prepare_atomic_json_target(root, state_path)
            candidate = _state_candidate(root, validation, manifest)
            state_path = Path(candidate["state_path"])
            state = candidate["state"]
            state_before_raw = bytes(candidate["before_raw"])
            expected_raw = bytes(candidate["candidate_raw"])
            state_before_sha = sha256_bytes(state_before_raw)
            exact = candidate["exact"]
            volume = int(manifest["volume"])
            conflicts = candidate["conflicts"]
            overwrite_decision = None
            if conflicts:
                overwrite_decision = _authorize_plan_conflicts(
                    root,
                    validation,
                    manifest,
                    stage="state",
                    conflicts=conflicts,
                    decision_receipt=decision_receipt,
                )
            changed = bool(candidate["changed"])
            if changed:
                try:
                    _safe_json_write_locked(
                        root,
                        state_path,
                        state,
                        backup=True,
                        expected_before_sha256=state_before_sha,
                    )
                    _, persisted = _read_state(root)
                    persisted_entries = [
                        item
                        for item in persisted["progress"]["volumes_planned"]
                        if item.get("volume") == volume
                    ]
                    if len(persisted_entries) != 1 or any(
                        persisted_entries[0].get(key) != value for key, value in exact.items()
                    ):
                        raise PlanTransactionError(
                            "state planning entry failed exact readback verification"
                        )
                except Exception as exc:
                    try:
                        current_raw = _stable_artifact_bytes(
                            root,
                            state_path,
                            must_exist=False,
                            max_bytes=8 * 1024 * 1024,
                        )
                        if current_raw not in {None, state_before_raw, expected_raw}:
                            raise PlanTransactionError(
                                "state changed before planning rollback"
                            )
                        if current_raw != state_before_raw:
                            _atomic_write_bytes(state_path, state_before_raw)
                        restored = _stable_artifact_bytes(
                            root,
                            state_path,
                            must_exist=True,
                            max_bytes=8 * 1024 * 1024,
                        )
                        if restored != state_before_raw:
                            raise PlanTransactionError(
                                "state planning update failed rollback readback"
                            )
                    except Exception as rollback_exc:
                        raise PlanTransactionError(
                            f"state planning update failed and rollback was incomplete: {rollback_exc}"
                        ) from exc
                    raise
            else:
                _, persisted = _read_state(root)
                persisted_entries = [
                    item
                    for item in persisted["progress"]["volumes_planned"]
                    if item.get("volume") == volume
                ]
                if len(persisted_entries) != 1 or any(
                    persisted_entries[0].get(key) != value for key, value in exact.items()
                ):
                    raise PlanTransactionError(
                        "state planning entry failed exact readback verification"
                    )
    except Timeout as exc:
        raise PlanTransactionError("state planning lock is busy") from exc
    return {"state": state_path}, {"planned_entry": exact}, overwrite_decision


def _contract_binding(validation: Mapping[str, Any], manifest: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "source": "webnovel-plan",
        "run_id": manifest["run_id"],
        "manifest_sha256": validation["manifest_sha256"],
        "content_sha256": manifest["content_sha256"],
    }


def _expected_contracts(
    root: Path,
    validation: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> dict[Path, dict[str, Any]]:
    story_root = root / ".story-system"
    master_path = _require_fixed_path(root, story_root / "MASTER_SETTING.json", expected=story_root / "MASTER_SETTING.json")
    master_raw = _read_json(master_path)
    try:
        master = MasterSetting.model_validate(master_raw)
    except Exception as exc:
        raise PlanTransactionError(f"MASTER_SETTING contract is invalid: {exc}") from exc
    anti_path = story_root / "anti_patterns.json"
    anti_patterns: list[str] = []
    if anti_path.is_file():
        try:
            raw_anti = json.loads(anti_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PlanTransactionError(f"anti-pattern contract is invalid: {exc}") from exc
        if not isinstance(raw_anti, list):
            raise PlanTransactionError("anti-pattern contract must be a list")
        anti_patterns = [str(item.get("text") or "") for item in raw_anti if isinstance(item, Mapping) and item.get("text")]
    binding = _contract_binding(validation, manifest)
    volume = int(manifest["volume"])
    start, end = [int(value) for value in manifest["chapter_range"]]
    goals = [str(item.get("goal") or "") for item in manifest["chapters"]]
    genre = str(master.route.get("primary_genre") or "")
    tone = str(master.master_constraints.get("core_tone") or "")
    pacing = str(master.master_constraints.get("pacing_strategy") or "")
    volume_contract = VolumeBrief.model_validate(
        {
            "meta": {"contract_type": "VOLUME_BRIEF", "source_trace": [binding]},
            "volume_goal": {
                "volume": volume,
                "chapter_range": [start, end],
                "summary": "；".join(goals),
                "final_open_question": manifest["beat"]["final_open_question"],
            },
            "selected_tropes": [genre] if genre else [],
            "selected_pacing": {"wave": pacing} if pacing else {},
            "selected_scenes": goals,
            "anti_patterns": anti_patterns,
            "system_constraints": [tone] if tone else [],
        }
    ).model_dump()
    expected: dict[Path, dict[str, Any]] = {
        story_root / "volumes" / f"volume_{volume:03d}.json": volume_contract,
    }
    for chapter_item in manifest["chapters"]:
        chapter = int(chapter_item["chapter"])
        directive = {
            key: chapter_item[key]
            for key in (
                "goal",
                "time_offset_minutes",
                "span_minutes",
                "transition",
                "time_mode",
                "countdowns",
                "cbn",
                "cpns",
                "cen",
                "must_cover_nodes",
                "forbidden_zones",
                "chapter_end_open_question",
            )
        }
        directive["key_entities"] = list(dict.fromkeys(
            str(node.get("subject") or "")
            for node in [chapter_item["cbn"], *chapter_item["cpns"], chapter_item["cen"]]
            if str(node.get("subject") or "")
        ))
        chapter_contract = ChapterBrief.model_validate(
            {
                "meta": {"contract_type": "CHAPTER_BRIEF", "chapter": chapter, "source_trace": [binding]},
                "override_allowed": {"chapter_focus": chapter_item["goal"]},
                "chapter_directive": directive,
                "source_trace": [binding],
            }
        ).model_dump()
        review_contract = ReviewContract.model_validate(
            {
                "meta": {"contract_type": "REVIEW_CONTRACT", "source_trace": [binding]},
                "must_check": chapter_item["must_cover_nodes"],
                "blocking_rules": chapter_item["forbidden_zones"],
                "genre_specific_risks": [genre] if genre else [],
                "anti_patterns": anti_patterns,
                "system_constraints": [tone] if tone else [],
                "review_thresholds": {"blocking_count": 0, "missed_nodes": 0},
            }
        ).model_dump()
        expected[story_root / "chapters" / f"chapter_{chapter:03d}.json"] = chapter_contract
        expected[story_root / "reviews" / f"chapter_{chapter:03d}.review.json"] = review_contract
        resolved = contract_files_for_chapter(root, chapter)
        if (
            resolved["master"] != master_path
            or resolved["volume"] != story_root / "volumes" / f"volume_{volume:03d}.json"
            or resolved["chapter"] != story_root / "chapters" / f"chapter_{chapter:03d}.json"
            or resolved["review"] != story_root / "reviews" / f"chapter_{chapter:03d}.review.json"
        ):
            raise PlanTransactionError(f"state volume mapping does not bind chapter {chapter} to volume {volume}")
    return expected


def _contracts_candidate(
    root: Path,
    validation: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> tuple[dict[Path, dict[str, Any]], dict[Path, bytes | None], dict[str, dict[str, str]]]:
    expected = _expected_contracts(root, validation, manifest)
    conflicts: dict[str, dict[str, str]] = {}
    snapshots: dict[Path, bytes | None] = {}
    for path, payload in expected.items():
        _prepare_atomic_json_target(root, path)
        current_raw = _stable_artifact_bytes(
            root,
            path,
            must_exist=False,
            max_bytes=8 * 1024 * 1024,
        )
        snapshots[path] = current_raw
        if current_raw is None:
            continue
        try:
            current = json.loads(current_raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PlanTransactionError(f"existing contract is unreadable: {path}: {exc}") from exc
        if current != payload:
            candidate_raw = json.dumps(dict(payload), ensure_ascii=False, indent=2).encode("utf-8")
            conflicts[str(path)] = {
                "before_sha256": sha256_bytes(current_raw),
                "after_sha256": sha256_bytes(candidate_raw),
            }
    return expected, snapshots, conflicts


def _contracts_stage(
    root: Path,
    validation: Mapping[str, Any],
    manifest: Mapping[str, Any],
    *,
    overwrite_token: str | None,
    decision_receipt: str | Path | None = None,
) -> tuple[dict[str, Path], dict[str, Any], dict[str, Any] | None]:
    expected, snapshots, conflicts = _contracts_candidate(root, validation, manifest)
    overwrite_decision = None
    if conflicts:
        overwrite_decision = _authorize_plan_conflicts(
            root,
            validation,
            manifest,
            stage="contracts",
            conflicts=conflicts,
            decision_receipt=decision_receipt,
        )
    attempted: list[Path] = []
    try:
        for path, payload in expected.items():
            before = sha256_bytes(snapshots[path]) if snapshots[path] is not None else None
            # Register rollback intent before calling the writer. The writer can
            # atomically replace the target and then fail during exact readback;
            # appending only after it returns would strand that new contract.
            attempted.append(path)
            _safe_json_write(
                root,
                path,
                payload,
                backup=True,
                expected_before_sha256=before,
            )
        for path, payload in expected.items():
            persisted = _read_json(path)
            if persisted != payload:
                raise PlanTransactionError(f"contract failed exact schema/hash readback: {path}")
    except Exception as exc:
        rollback_errors: list[str] = []
        for path in reversed(attempted):
            lock_path = path.with_suffix(path.suffix + ".lock")
            try:
                with FileLock(str(lock_path), timeout=10):
                    _prepare_atomic_json_target(root, path)
                    expected_raw = json.dumps(dict(expected[path]), ensure_ascii=False, indent=2).encode("utf-8")
                    current_raw = (
                        _read_bounded_bytes(path, max_bytes=8 * 1024 * 1024)
                        if path.is_file()
                        else None
                    )
                    old = snapshots[path]
                    if current_raw == old:
                        continue
                    if current_raw is not None and current_raw != expected_raw:
                        raise PlanTransactionError(f"contract changed before rollback: {path}")
                    if old is None:
                        if path.is_file():
                            path.unlink()
                    else:
                        _atomic_write_bytes(path, old)
                    restored = (
                        _read_bounded_bytes(path, max_bytes=8 * 1024 * 1024)
                        if path.is_file()
                        else None
                    )
                    if restored != old:
                        raise PlanTransactionError(f"contract failed rollback readback: {path}")
            except Exception as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        if rollback_errors:
            raise PlanTransactionError(
                "contract stage failed and rollback was incomplete: " + "; ".join(rollback_errors)
            ) from exc
        raise
    outputs = {f"contract_{index:04d}": path for index, path in enumerate(sorted(expected), 1)}
    outputs["master_setting"] = root / ".story-system" / "MASTER_SETTING.json"
    return outputs, {
        "chapter_range": manifest["chapter_range"],
        "contract_count": len(expected) + 1,
        "binding": _contract_binding(validation, manifest),
    }, overwrite_decision


def _current_plan_conflicts(
    root: Path,
    validation: Mapping[str, Any],
    manifest: Mapping[str, Any],
    *,
    stage: str,
) -> dict[str, dict[str, str]]:
    """Rebuild the exact authored before/validated after scope without writes."""

    if stage == "apply":
        artifacts = _manifest_artifacts(Path(str(validation.get("manifest_path") or "")), root)
        conflicts: dict[str, dict[str, str]] = {}
        for _, target, digest in artifacts.values():
            current = _stable_artifact_bytes(root, target, must_exist=False)
            if current is not None and sha256_bytes(current) != digest:
                conflicts[str(target)] = {
                    "before_sha256": sha256_bytes(current),
                    "after_sha256": digest,
                }
        return conflicts
    if stage == "master_outline":
        return dict(_master_outline_candidate(root, manifest)["conflicts"])
    if stage == "state":
        return dict(_state_candidate(root, validation, manifest)["conflicts"])
    if stage == "contracts":
        return _contracts_candidate(root, validation, manifest)[2]
    raise PlanTransactionError(f"invalid plan decision stage: {stage}")


def _prewrite_stage(
    root: Path,
    manifest: Mapping[str, Any],
) -> tuple[dict[str, Path], dict[str, Any]]:
    chapter = int(manifest["chapter_range"][0])
    report = run_write_gate(root, chapter=chapter, stage="prewrite")
    if report.get("ok") is not True or report.get("chapter") != chapter or report.get("stage") != "prewrite":
        raise PlanTransactionError("target first-chapter prewrite gate is blocking")
    outputs = contract_files_for_chapter(root, chapter)
    outputs["state"] = root / ".webnovel" / "state.json"
    return outputs, {
        "chapter": chapter,
        "gate_sha256": sha256_bytes(_canonical_bytes(report)),
        "gate_report": report,
    }


def run_downstream_stage(
    project_root: str | Path,
    run_id: str,
    *,
    stage: str,
    overwrite_token: str | None = None,
    decision_receipt: str | Path | None = None,
) -> dict[str, Any]:
    """Execute and read back one fixed downstream truth-source stage."""

    root = _safe_project_root(project_root)
    if stage not in DOWNSTREAM_STAGES:
        raise PlanTransactionError(f"unknown downstream stage: {stage}")
    if FileLock is None:
        raise PlanTransactionError("filelock is required for plan downstream stages")
    _, _, initial_manifest = _load_bound_plan(root, run_id)
    lock_path = _volume_lifecycle_lock_path(root, initial_manifest.get("volume"))
    _require_fixed_path(root, lock_path, expected=lock_path, must_exist=False)
    try:
        with FileLock(str(lock_path), timeout=10):
            _require_fixed_path(root, lock_path, expected=lock_path)
            validation, _, manifest = _load_bound_plan(root, run_id)
            if manifest.get("volume") != initial_manifest.get("volume"):
                raise PlanTransactionError("plan volume changed while waiting for lifecycle lock")
            _check_stage_order(root, run_id, stage)
            attempts = sorted(_runtime_dir(root, run_id).glob(f"stage-{stage}-*.json"))
            if attempts:
                latest = _read_json(attempts[-1])
                apply_receipt = _read_json(_runtime_dir(root, run_id) / "apply.json")
                if _stage_receipt_current(
                    root,
                    latest,
                    run_id=run_id,
                    stage=stage,
                    apply_receipt_sha256=str(apply_receipt.get("receipt_sha256") or ""),
                ):
                    return latest
            try:
                if stage == "master_outline":
                    outputs, verification, overwrite_decision = _master_outline_stage(
                        root,
                        validation,
                        manifest,
                        overwrite_token=overwrite_token,
                        decision_receipt=decision_receipt,
                    )
                elif stage == "state":
                    outputs, verification, overwrite_decision = _state_stage(
                        root,
                        validation,
                        manifest,
                        overwrite_token=overwrite_token,
                        decision_receipt=decision_receipt,
                    )
                elif stage == "contracts":
                    outputs, verification, overwrite_decision = _contracts_stage(
                        root,
                        validation,
                        manifest,
                        overwrite_token=overwrite_token,
                        decision_receipt=decision_receipt,
                    )
                else:
                    outputs, verification = _prewrite_stage(root, manifest)
                    overwrite_decision = None
            except _PlanDecisionNoWrite as selected:
                return selected.result
            except PlanDownstreamChoiceRequired:
                raise
            except Exception as exc:
                record_downstream_stage(
                    root,
                    run_id,
                    stage=stage,
                    status="failed",
                    detail=str(exc),
                    _verified_token=_VERIFIED_DOWNSTREAM_TOKEN,
                )
                if isinstance(exc, PlanTransactionError):
                    raise
                raise PlanTransactionError(f"{stage} stage failed: {exc}") from exc
            return record_downstream_stage(
                root,
                run_id,
                stage=stage,
                status="completed",
                outputs=outputs,
                verification=verification,
                decision_receipt=overwrite_decision,
                _verified_token=_VERIFIED_DOWNSTREAM_TOKEN,
            )
    except Timeout as exc:
        raise PlanTransactionError("plan lifecycle lock is busy") from exc


def plan_transaction_status(project_root: str | Path, run_id: str) -> dict[str, Any]:
    root = _safe_project_root(project_root)
    runtime_dir = _runtime_dir(root, run_id)
    validation_path = runtime_dir / "validation.json"
    apply_path = runtime_dir / "apply.json"
    integrity_errors: list[str] = []

    def load_runtime_receipt(path: Path, label: str) -> dict[str, Any] | None:
        if not (path.exists() or path.is_symlink()):
            return None
        try:
            checked = _require_fixed_path(root, path, expected=path)
            return _read_json(checked)
        except PlanTransactionError as exc:
            integrity_errors.append(f"{label}: {exc}")
            return {"status": "stale", "stale": True, "error": str(exc)}

    validation = load_runtime_receipt(validation_path, "validation")
    applied = load_runtime_receipt(apply_path, "apply")
    validation_current = False
    apply_current = False
    report: dict[str, Any] | None = None
    artifacts: dict[str, tuple[Path, Path, str]] | None = None
    if validation is not None and not validation.get("stale"):
        try:
            if validation.get("run_id") != run_id:
                raise PlanTransactionError("validation receipt does not bind the requested run")
            report = _verify_validation_receipt(
                root,
                validation,
                str(validation.get("manifest_path") or ""),
            )
            artifacts = _manifest_artifacts(Path(str(report["manifest_path"])), root)
            validation_current = True
        except PlanTransactionError as exc:
            integrity_errors.append(f"validation: {exc}")
            validation = {**validation, "status": "stale", "stale": True, "error": str(exc)}
    if applied is not None and not applied.get("stale"):
        try:
            if not validation_current or report is None or artifacts is None or validation is None:
                raise PlanTransactionError("apply receipt lacks a current validation receipt")
            applied = _verify_apply_receipt(root, applied, validation, report, artifacts)
            apply_current = True
        except PlanTransactionError as exc:
            integrity_errors.append(f"apply: {exc}")
            applied = {**applied, "status": "stale", "stale": True, "error": str(exc)}

    stages: dict[str, Any] = {}
    for stage in DOWNSTREAM_STAGES:
        attempts = sorted(runtime_dir.glob(f"stage-{stage}-*.json"))
        if attempts:
            try:
                latest_path = _require_fixed_path(
                    root,
                    attempts[-1],
                    expected=attempts[-1],
                )
                latest = _read_json(latest_path)
            except PlanTransactionError as exc:
                integrity_errors.append(f"{stage}: {exc}")
                latest = {"status": "stale", "stale": True, "error": str(exc)}
            if latest.get("status") == "completed" and (
                not apply_current
                or applied is None
                or not _stage_receipt_current(
                    root,
                    latest,
                    run_id=run_id,
                    stage=stage,
                    apply_receipt_sha256=str(applied.get("receipt_sha256") or ""),
                )
            ):
                detail = "downstream receipt or truth-source outputs are stale"
                integrity_errors.append(f"{stage}: {detail}")
                latest = {**latest, "status": "stale", "stale": True, "error": detail}
            stages[stage] = latest
    complete = bool(
        validation_current
        and apply_current
        and all((stages.get(stage) or {}).get("status") == "completed" for stage in DOWNSTREAM_STAGES)
    )
    stale = bool(integrity_errors)
    if not validation_current:
        next_stage = "validate" if validation is not None else None
    elif not apply_current:
        next_stage = "apply"
    else:
        next_stage = next(
            (stage for stage in DOWNSTREAM_STAGES if (stages.get(stage) or {}).get("status") != "completed"),
            None,
        )
    return {
        "run_id": run_id,
        "status": "complete" if complete else "stale" if stale else "in_progress" if validation else "missing",
        "validation": validation,
        "apply": applied,
        "stages": stages,
        "next_stage": next_stage,
        "complete": complete,
        "integrity_errors": integrity_errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate and apply a staged webnovel plan")
    parser.add_argument("--project-root", required=True)
    sub = parser.add_subparsers(dest="action", required=True)
    batch_cmd = sub.add_parser("accept-batch")
    batch_cmd.add_argument("--request-file", required=True)
    batch_cmd.add_argument("--fragment-file", required=True)
    validate_cmd = sub.add_parser("validate")
    validate_cmd.add_argument("--manifest", required=True)
    validate_cmd.add_argument("--request-file", required=True)
    validate_cmd.add_argument("--parent-evidence-file", required=True)
    marker_cmd = sub.add_parser("marker")
    marker_cmd.add_argument("--manifest", required=True)
    marker_cmd.add_argument("--request-file", required=True)
    apply_cmd = sub.add_parser("apply")
    apply_cmd.add_argument("--manifest", required=True)
    apply_cmd.add_argument("--receipt", required=True)
    apply_cmd.add_argument("--overwrite-token", default=None)
    apply_cmd.add_argument("--decision-receipt", default=None)
    decision_cmd = sub.add_parser("decision")
    decision_cmd.add_argument("--request-file", required=True)
    status_cmd = sub.add_parser("status")
    status_cmd.add_argument("--run-id", required=True)
    stage_cmd = sub.add_parser("stage")
    stage_cmd.add_argument("--run-id", required=True)
    stage_cmd.add_argument("--stage", choices=DOWNSTREAM_STAGES, required=True)
    stage_cmd.add_argument("--overwrite-token", default=None)
    stage_cmd.add_argument("--decision-receipt", default=None)
    args = parser.parse_args()
    try:
        if args.action == "accept-batch":
            result = accept_plan_batch(
                args.project_root,
                args.request_file,
                args.fragment_file,
            )
            exit_code = 0
        elif args.action == "validate":
            result = create_validation_receipt(
                args.project_root,
                args.manifest,
                request_file=args.request_file,
                parent_evidence_file=args.parent_evidence_file,
            )
            exit_code = 0 if result.get("status") == "validated" else 2
        elif args.action == "marker":
            result = {
                "status": "ready",
                "marker": build_parent_evidence_marker(
                    args.project_root,
                    args.manifest,
                    args.request_file,
                ),
            }
            exit_code = 0
        elif args.action == "apply":
            result = apply_validated_plan(
                args.project_root,
                args.manifest,
                args.receipt,
                overwrite_token=args.overwrite_token,
                decision_receipt=args.decision_receipt,
            )
            exit_code = 0
        elif args.action == "decision":
            result = create_plan_decision_receipt(
                args.project_root,
                args.request_file,
            )
            exit_code = 0
        elif args.action == "stage":
            result = run_downstream_stage(
                args.project_root,
                args.run_id,
                stage=args.stage,
                overwrite_token=args.overwrite_token,
                decision_receipt=args.decision_receipt,
            )
            exit_code = 0
        else:
            result = plan_transaction_status(args.project_root, args.run_id)
            exit_code = 0 if result.get("status") != "missing" else 2
    except PlanApplyChoiceRequired as exc:
        result = {
            "status": "choice_required",
            "code": "plan_overwrite_requires_user_choice",
            **exc.decision,
            "authorization_gate": "trusted_parent_decision_required",
        }
        exit_code = 1
    except PlanDownstreamChoiceRequired as exc:
        result = {
            "status": "choice_required",
            "code": "plan_downstream_overwrite_requires_user_choice",
            "stage": exc.stage,
            **exc.decision,
            "authorization_gate": "trusted_parent_decision_required",
        }
        exit_code = 1
    except PlanTransactionError as exc:
        result = {"status": "failed", "error": str(exc)}
        exit_code = 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
