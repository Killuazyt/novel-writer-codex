#!/usr/bin/env python3
"""Strict out-of-project request-file contract for Review Agent acceptance."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any


REVIEW_ACCEPT_REQUEST_SCHEMA = "webnovel-review-accept-request/v2"
REVIEW_DECISION_REQUEST_SCHEMA = "webnovel-review-decision-request/v1"
MAX_REQUEST_BYTES = 1024 * 1024
_RUN_ID_RE = re.compile(r"^rv-[A-Za-z0-9](?:[A-Za-z0-9._-]{0,90}[A-Za-z0-9])?$")
_RANGE_ID_RE = re.compile(r"^rr-[A-Za-z0-9](?:[A-Za-z0-9._-]{0,90}[A-Za-z0-9])?$")
_CHOICE_REQUEST_ID_RE = re.compile(r"^choice-[0-9a-f]{20}$")
_WINDOWS_RESERVED_ID_PAYLOADS = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


class ReviewRequestError(ValueError):
    """Raised when an accept request does not match the frozen schema."""


def _strict_json_object(raw: bytes) -> dict[str, Any]:
    def build_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ValueError(f"invalid JSON constant: {value}")

    payload = json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=build_object,
        parse_constant=reject_constant,
    )
    if not isinstance(payload, dict):
        raise ReviewRequestError("request-file top level must be a JSON object")
    return payload


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _clean_id(value: object, *, label: str, limit: int = 128) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReviewRequestError(f"{label} must be a non-empty string")
    result = value.strip()
    if "\x00" in result or len(result) > limit:
        raise ReviewRequestError(f"{label} is too long or contains NUL")
    return result


def _review_id(value: object, *, label: str, pattern: re.Pattern[str]) -> str:
    result = _clean_id(value, label=label, limit=96)
    payload_stem = result[3:].split(".", 1)[0].upper()
    if (
        value != result
        or pattern.fullmatch(result) is None
        or payload_stem in _WINDOWS_RESERVED_ID_PAYLOADS
    ):
        raise ReviewRequestError(f"{label} is invalid")
    return result


def _absolute_path(value: object, *, label: str) -> str:
    text = _clean_id(value, label=label, limit=4096)
    path = Path(text)
    if not path.is_absolute():
        raise ReviewRequestError(f"{label} must be an absolute path")
    return str(path)


def _load_bounded_request(
    request_file: str | Path,
    *,
    project_root: str | Path,
) -> dict[str, Any]:
    raw_path = Path(request_file)
    if not raw_path.is_absolute():
        raise ReviewRequestError("request-file must be an absolute path")
    if raw_path.is_symlink():
        raise ReviewRequestError("request-file must be a regular non-symlink file")
    try:
        path = raw_path.resolve(strict=True)
        project = Path(project_root).resolve(strict=True)
    except OSError as exc:
        raise ReviewRequestError(f"request-file path is unavailable: {exc}") from exc
    if not path.is_file() or path.is_symlink():
        raise ReviewRequestError("request-file must be a regular non-symlink file")
    if _inside(path, project):
        raise ReviewRequestError("request-file must be outside the novel project")
    try:
        with path.open("rb") as handle:
            before = os.fstat(handle.fileno())
            if before.st_size <= 0 or before.st_size > MAX_REQUEST_BYTES:
                raise ReviewRequestError(f"request-file size must be 1..{MAX_REQUEST_BYTES} bytes")
            raw = handle.read(MAX_REQUEST_BYTES + 1)
            after = os.fstat(handle.fileno())
    except OSError as exc:
        raise ReviewRequestError(f"request-file cannot be read: {exc}") from exc
    if (
        len(raw) != before.st_size
        or len(raw) > MAX_REQUEST_BYTES
        or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    ):
        raise ReviewRequestError(f"request-file size must be 1..{MAX_REQUEST_BYTES} bytes")
    try:
        current = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise ReviewRequestError(f"request-file identity cannot be verified: {exc}") from exc
    if path.is_symlink() or (
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
        raise ReviewRequestError("request-file changed while it was read")
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ReviewRequestError("request-file must be UTF-8 without BOM")
    try:
        return _strict_json_object(raw)
    except UnicodeDecodeError as exc:
        raise ReviewRequestError("request-file must be valid UTF-8") from exc
    except (json.JSONDecodeError, ValueError) as exc:
        raise ReviewRequestError(f"request-file must contain one JSON object: {exc}") from exc


def load_review_accept_request(
    request_file: str | Path,
    *,
    project_root: str | Path,
) -> dict[str, Any]:
    """Load one bounded UTF-8 JSON request without shell interpolation."""

    payload = _load_bounded_request(request_file, project_root=project_root)

    allowed = {
        "schema_version",
        "run_id",
        "chapter",
        "review_mode",
        "runtime",
        "duration_ms",
    }
    required = allowed - {"duration_ms"}
    if set(payload) - allowed:
        raise ReviewRequestError(
            "request-file contains unknown fields: " + ", ".join(sorted(set(payload) - allowed))
        )
    if not required.issubset(payload):
        raise ReviewRequestError(
            "request-file is missing fields: " + ", ".join(sorted(required - set(payload)))
        )
    if payload.get("schema_version") != REVIEW_ACCEPT_REQUEST_SCHEMA:
        raise ReviewRequestError("request-file schema_version is invalid")
    run_id = _review_id(payload.get("run_id"), label="run_id", pattern=_RUN_ID_RE)
    chapter = payload.get("chapter")
    if type(chapter) is not int or chapter <= 0:
        raise ReviewRequestError("chapter must be a positive integer")
    review_mode = payload.get("review_mode")
    if review_mode not in {"full", "fast"}:
        raise ReviewRequestError("review_mode must be full or fast")
    runtime = payload.get("runtime")
    runtime_fields = {
        "rollout_path",
        "sessions_root",
        "child_thread_id",
        "parent_thread_id",
    }
    if not isinstance(runtime, dict) or set(runtime) != runtime_fields:
        raise ReviewRequestError("runtime must contain exactly the four evidence fields")
    duration_ms = payload.get("duration_ms", 0)
    if type(duration_ms) is not int or duration_ms < 0:
        raise ReviewRequestError("duration_ms must be a non-negative integer")
    return {
        "schema_version": REVIEW_ACCEPT_REQUEST_SCHEMA,
        "run_id": run_id,
        "chapter": chapter,
        "review_mode": review_mode,
        "runtime": {
            "rollout_path": _absolute_path(runtime.get("rollout_path"), label="rollout_path"),
            "sessions_root": _absolute_path(runtime.get("sessions_root"), label="sessions_root"),
            "child_thread_id": _clean_id(runtime.get("child_thread_id"), label="child_thread_id"),
            "parent_thread_id": _clean_id(runtime.get("parent_thread_id"), label="parent_thread_id"),
        },
        "duration_ms": duration_ms,
    }


def load_review_decision_request(
    request_file: str | Path,
    *,
    project_root: str | Path,
) -> dict[str, Any]:
    """Load a host-rollout receipt request; no caller-supplied choice exists."""

    payload = _load_bounded_request(request_file, project_root=project_root)
    required = {
        "schema_version",
        "kind",
        "run_id",
        "range_id",
        "request_id",
        "runtime",
    }
    if set(payload) != required:
        raise ReviewRequestError("decision request must contain exactly the six contract fields")
    if payload.get("schema_version") != REVIEW_DECISION_REQUEST_SCHEMA:
        raise ReviewRequestError("decision request schema_version is invalid")
    kind = payload.get("kind")
    if kind not in {"run", "range"}:
        raise ReviewRequestError("decision request kind must be run or range")
    run_id = payload.get("run_id")
    range_id = payload.get("range_id")
    if kind == "run":
        run_id = _review_id(run_id, label="run_id", pattern=_RUN_ID_RE)
        if range_id is not None:
            raise ReviewRequestError("run decision scope is invalid")
    else:
        range_id = _review_id(range_id, label="range_id", pattern=_RANGE_ID_RE)
        if run_id is not None:
            raise ReviewRequestError("range decision scope is invalid")
    request_id = _clean_id(payload.get("request_id"), label="request_id", limit=96)
    if not _CHOICE_REQUEST_ID_RE.fullmatch(request_id):
        raise ReviewRequestError("decision request_id is invalid")
    runtime = payload.get("runtime")
    runtime_fields = {"rollout_path", "sessions_root", "parent_thread_id"}
    if not isinstance(runtime, dict) or set(runtime) != runtime_fields:
        raise ReviewRequestError("decision runtime must contain exactly the three evidence fields")
    return {
        "schema_version": REVIEW_DECISION_REQUEST_SCHEMA,
        "kind": kind,
        "run_id": run_id,
        "range_id": range_id,
        "request_id": request_id,
        "runtime": {
            "rollout_path": _absolute_path(runtime.get("rollout_path"), label="rollout_path"),
            "sessions_root": _absolute_path(runtime.get("sessions_root"), label="sessions_root"),
            "parent_thread_id": _clean_id(
                runtime.get("parent_thread_id"),
                label="parent_thread_id",
            ),
        },
    }
