#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Host-neutral request contract for parent-only volume planning.

This module never calls a model and never writes novel facts.  It turns an
already-authorized parent task into a bounded, deterministic batch request
that the planning Skill can execute in the current conversation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

try:
    from filelock import FileLock, Timeout
except ImportError:
    FileLock = None  # type: ignore[assignment]


SCHEMA_VERSION = "webnovel-plan-request/v1"
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,94}[A-Za-z0-9])?$")
_WINDOWS_RESERVED_RUN_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
_MAX_REQUEST_BYTES = 2 * 1024 * 1024


class PlanRequestError(ValueError):
    """The planning request is unsafe or internally inconsistent."""


def _safe_run_id(value: Any) -> bool:
    if not isinstance(value, str) or not _RUN_ID_RE.fullmatch(value):
        return False
    return value.split(".", 1)[0].upper() not in _WINDOWS_RESERVED_RUN_NAMES


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def plan_request_sha256(payload: Mapping[str, Any]) -> str:
    """Return the stable digest used by parent-rollout evidence."""

    return hashlib.sha256(_canonical_bytes(dict(payload))).hexdigest()


def _resolved_root(project_root: str | Path) -> Path:
    root = _absolute_lexical(project_root)
    for component in _path_chain(root):
        if (component.exists() or component.is_symlink()) and _is_reparse_point(component):
            raise PlanRequestError(f"reparse-point project_root is forbidden: {component}")
    if not root.is_dir():
        raise PlanRequestError(f"project_root is not a directory: {root}")
    return root.resolve(strict=True)


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


def _is_reparse_point(path: Path) -> bool:
    try:
        attrs = int(getattr(path.lstat(), "st_file_attributes", 0))
    except OSError:
        return False
    return path.is_symlink() or bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _prepare_request_target(root: Path, path: Path) -> None:
    root = root.resolve()
    path = _absolute_lexical(path)
    try:
        relative_parent = path.parent.relative_to(root)
    except ValueError as exc:
        raise PlanRequestError(f"plan request path escapes project: {path}") from exc
    current = root
    for part in relative_parent.parts:
        current = current / part
        if current.exists() or current.is_symlink():
            if _is_reparse_point(current) or not current.is_dir():
                raise PlanRequestError(f"unsafe plan request parent: {current}")
        else:
            current.mkdir()
            if _is_reparse_point(current) or not current.is_dir():
                raise PlanRequestError(f"unsafe plan request parent: {current}")
    for candidate in (path, path.with_suffix(path.suffix + ".lock")):
        if candidate.exists() or candidate.is_symlink():
            if _is_reparse_point(candidate) or not candidate.is_file():
                raise PlanRequestError(f"unsafe plan request control path: {candidate}")


def _read_request_bytes(root: Path, path: Path) -> bytes:
    _prepare_request_target(root, path)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise PlanRequestError(f"existing plan request is unreadable: {path}: {exc}") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_REQUEST_BYTES:
            raise PlanRequestError(f"existing plan request is not regular or too large: {path}")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(_MAX_REQUEST_BYTES + 1)
        after = os.fstat(fd)
        path_after = path.stat()
        before_id = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        after_id = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        path_id = (path_after.st_dev, path_after.st_ino, path_after.st_size, path_after.st_mtime_ns)
        _prepare_request_target(root, path)
        if len(raw) > _MAX_REQUEST_BYTES or len(raw) != before.st_size or before_id != after_id or before_id != path_id:
            raise PlanRequestError(f"existing plan request changed during read: {path}")
        return raw
    except OSError as exc:
        raise PlanRequestError(f"existing plan request is unreadable: {path}: {exc}") from exc
    finally:
        os.close(fd)


def _batches(start_chapter: int, end_chapter: int, batch_size: int) -> list[dict[str, int]]:
    return [
        {"start_chapter": start, "end_chapter": min(start + batch_size - 1, end_chapter)}
        for start in range(start_chapter, end_chapter + 1, batch_size)
    ]


def build_plan_request(
    project_root: str | Path,
    *,
    volume: int,
    start_chapter: int,
    end_chapter: int,
    parent_model: str,
    parent_reasoning_effort: str | None = None,
    batch_size: int = 10,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Build the only supported planning route: the current parent task."""

    root = _resolved_root(project_root)
    run_id = f"plan-v{volume}-{uuid4().hex[:12]}" if run_id is None else run_id
    if not _safe_run_id(run_id):
        raise PlanRequestError(
            "run_id must be canonical, start/end with ASCII alphanumerics, "
            "and not use a Windows reserved name"
        )
    payload = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "project_root": str(root),
        "volume": volume,
        "start_chapter": start_chapter,
        "end_chapter": end_chapter,
        "batch_size": batch_size,
        "batches": _batches(start_chapter, end_chapter, batch_size)
        if isinstance(start_chapter, int)
        and isinstance(end_chapter, int)
        and isinstance(batch_size, int)
        and start_chapter > 0
        and end_chapter >= start_chapter
        and batch_size > 0
        else [],
        "executor": "parent",
        "parent_model": str(parent_model or "").strip(),
        "parent_reasoning_effort": parent_reasoning_effort,
        "planning_model": str(parent_model or "").strip(),
        "invoked_agents": [],
        "fallback_allowed": False,
        "facts_write_allowed": False,
        "manifest_path": str(
            root
            / ".webnovel"
            / "tmp"
            / "plan-runs"
            / run_id
            / "plan-manifest.json"
        ),
        "request_path": str(
            root
            / ".webnovel"
            / "tmp"
            / "plan-runs"
            / run_id
            / "plan-request.json"
        ),
    }
    validate_plan_request(payload, project_root=root)
    return payload


def validate_plan_request(
    payload: Mapping[str, Any],
    *,
    project_root: str | Path | None = None,
) -> dict[str, Any]:
    """Fail closed if planning is delegated or the requested range is unsafe."""

    problems: list[dict[str, str]] = []

    def problem(code: str, detail: str) -> None:
        problems.append({"code": code, "detail": detail})

    if payload.get("schema_version") != SCHEMA_VERSION:
        problem("invalid_schema", "unsupported plan request schema")
    run_id = payload.get("run_id")
    if not _safe_run_id(run_id):
        problem(
            "invalid_run_id",
            "run_id must be canonical, start/end with ASCII alphanumerics, and avoid Windows reserved names",
        )

    for field in ("volume", "start_chapter", "end_chapter", "batch_size"):
        value = payload.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            problem("invalid_range", f"{field} must be a positive integer")
    start = payload.get("start_chapter")
    end = payload.get("end_chapter")
    batch_size = payload.get("batch_size")
    if isinstance(start, int) and isinstance(end, int) and start > end:
        problem("invalid_range", "start_chapter must not exceed end_chapter")
    if isinstance(batch_size, int) and batch_size > 12:
        problem("invalid_batch_size", "planning batches may not exceed 12 chapters")

    if payload.get("executor") != "parent":
        problem("parent_only", "volume planning must execute in the current parent task")
    if payload.get("invoked_agents") != []:
        problem("planning_subagent_forbidden", "planning may not invoke context/writer/reviewer/data agents")
    parent_model = payload.get("parent_model")
    if not isinstance(parent_model, str) or not parent_model.strip():
        problem("parent_model_missing", "parent_model is required")
    if payload.get("planning_model") != parent_model:
        problem("planning_model_mismatch", "planning_model must inherit the current parent model")
    parent_effort = payload.get("parent_reasoning_effort")
    if not isinstance(parent_effort, str) or not parent_effort.strip():
        problem("parent_effort_missing", "parent_reasoning_effort is required for host evidence")
    if payload.get("fallback_allowed") is not False:
        problem("fallback_forbidden", "planning fallback is not permitted")
    if payload.get("facts_write_allowed") is not False:
        problem("premature_fact_write", "a request cannot authorize fact writes before validation")

    try:
        root = _resolved_root(project_root or str(payload.get("project_root") or ""))
        request_root = _absolute_lexical(str(payload.get("project_root") or ""))
        if request_root != root:
            problem("project_root_mismatch", "request project_root does not match the selected project")
        if _safe_run_id(run_id):
            expected_manifest = _absolute_lexical(
                root / ".webnovel" / "tmp" / "plan-runs" / run_id / "plan-manifest.json"
            )
            manifest = _absolute_lexical(str(payload.get("manifest_path") or ""))
            if manifest != expected_manifest:
                problem("manifest_path_out_of_bounds", "manifest_path must use the current run staging directory")
            expected_request = expected_manifest.with_name("plan-request.json")
            request_path = _absolute_lexical(str(payload.get("request_path") or ""))
            if request_path != expected_request:
                problem("request_path_out_of_bounds", "request_path must use the current run staging directory")
    except (OSError, PlanRequestError, ValueError) as exc:
        problem("invalid_project_root", str(exc))

    expected_batches = []
    if (
        isinstance(start, int)
        and isinstance(end, int)
        and isinstance(batch_size, int)
        and start > 0
        and end >= start
        and 0 < batch_size <= 12
    ):
        expected_batches = _batches(start, end, batch_size)
    if payload.get("batches") != expected_batches:
        problem("batch_manifest_mismatch", "batches must exactly cover the requested range once")

    report = {
        "schema_version": SCHEMA_VERSION,
        "ok": not problems,
        "status": "accepted" if not problems else "blocked",
        "problems": problems,
    }
    if problems:
        raise PlanRequestError(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return report


def save_plan_request(payload: Mapping[str, Any]) -> Path:
    """Persist one immutable request at its fixed per-run staging path."""

    validate_plan_request(payload)
    if FileLock is None:
        raise PlanRequestError("filelock is required to persist a plan request")
    root = _resolved_root(str(payload["project_root"]))
    path = _absolute_lexical(str(payload["request_path"]))
    expected = _absolute_lexical(
        root / ".webnovel" / "tmp" / "plan-runs" / str(payload["run_id"]) / "plan-request.json"
    )
    if path != expected:
        raise PlanRequestError("plan request path is not the fixed per-run path")
    raw = json.dumps(dict(payload), ensure_ascii=False, indent=2).encode("utf-8")
    _prepare_request_target(root, path)
    lock_path = path.with_suffix(path.suffix + ".lock")
    try:
        with FileLock(str(lock_path), timeout=10):
            _prepare_request_target(root, path)
            if path.is_file():
                try:
                    existing_raw = _read_request_bytes(root, path)
                    if existing_raw.startswith(b"\xef\xbb\xbf"):
                        raise PlanRequestError(f"existing plan request has a UTF-8 BOM: {path}")
                    existing = json.loads(existing_raw.decode("utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError, PlanRequestError) as exc:
                    raise PlanRequestError(f"existing plan request is unreadable: {path}: {exc}") from exc
                if existing != dict(payload):
                    raise PlanRequestError(f"immutable plan request already exists with different content: {path}")
                return path
            fd, temp_name = tempfile.mkstemp(prefix="plan-request_", suffix=".tmp", dir=path.parent)
            temp_path = Path(temp_name)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                _prepare_request_target(root, path)
                os.replace(temp_path, path)
                _prepare_request_target(root, path)
                if _read_request_bytes(root, path) != raw:
                    raise PlanRequestError("saved plan request failed exact readback")
            finally:
                if temp_path.exists():
                    temp_path.unlink()
    except Timeout as exc:
        raise PlanRequestError("plan request lock is busy") from exc
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a parent-only webnovel planning request")
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--volume", type=int, required=True)
    parser.add_argument("--start-chapter", type=int, required=True)
    parser.add_argument("--end-chapter", type=int, required=True)
    parser.add_argument("--parent-model", required=True)
    parser.add_argument("--parent-reasoning-effort", required=True)
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--save", action="store_true", help="write the immutable fixed run request")
    args = parser.parse_args()
    try:
        result = build_plan_request(
            args.project_root,
            volume=args.volume,
            start_chapter=args.start_chapter,
            end_chapter=args.end_chapter,
            parent_model=args.parent_model,
            parent_reasoning_effort=args.parent_reasoning_effort,
            batch_size=args.batch_size,
            run_id=args.run_id,
        )
    except PlanRequestError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(2) from exc
    if args.save:
        save_plan_request(result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
