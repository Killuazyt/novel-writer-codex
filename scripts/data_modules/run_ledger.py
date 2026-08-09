#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import filelock

if __package__ in {None, ""}:  # pragma: no cover - direct script entry
    scripts_dir = Path(__file__).resolve().parents[1]
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))

try:
    from chapter_paths import find_chapter_file
except ImportError:
    from scripts.chapter_paths import find_chapter_file

if __package__ in {None, ""}:  # pragma: no cover - direct script entry
    from data_modules.artifact_validator import OK_PROJECTION_STATUSES, REQUIRED_PROJECTION_WRITERS
    from data_modules.project_phase import COMMIT_ARTIFACT_FILES, contract_files_for_chapter
    from data_modules.projection_log import latest_projection_run, projection_status_from_run
else:
    from .artifact_validator import OK_PROJECTION_STATUSES, REQUIRED_PROJECTION_WRITERS
    from .project_phase import COMMIT_ARTIFACT_FILES, contract_files_for_chapter
    from .projection_log import latest_projection_run, projection_status_from_run

try:
    from security_utils import atomic_write_json
except ImportError:
    from scripts.security_utils import atomic_write_json


SCHEMA_VERSION = "webnovel-run-ledger/v2"
LEGACY_SCHEMA_VERSION = "webnovel-run-ledger/v1"
# The persisted ledger needs v2 for Review namespaces, but the established
# write-resume response is a public v1 contract and remains unchanged.
WRITE_RESUME_SCHEMA_VERSION = LEGACY_SCHEMA_VERSION
LEDGER_REL = Path(".webnovel") / "run_ledger.json"
WRITE_STEPS = ("draft", "review", "data", "commit", "projection", "backup")
REVIEW_RUN_ID_RE = re.compile(r"^rv-[A-Za-z0-9](?:[A-Za-z0-9._-]{0,90}[A-Za-z0-9])?$")
REVIEW_RANGE_ID_RE = re.compile(r"^rr-[A-Za-z0-9](?:[A-Za-z0-9._-]{0,90}[A-Za-z0-9])?$")
_WINDOWS_RESERVED_ID_PAYLOADS = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_REVIEW_RUN_SCHEMA = "webnovel-review-workflow/v1"
_REVIEW_RANGE_SCHEMA = "webnovel-review-range/v1"
MAX_LEDGER_BYTES = 8 * 1024 * 1024
_REVIEW_RUN_STATUSES = {
    "prepared",
    "accepted",
    "awaiting_decision",
    "validated",
    "failed_persistence",
    "persisted",
    "abandoned",
    "targeted_fix_pending",
    "targeted_fix_blocked",
    "failed_validation",
    "stale",
}
_REVIEW_RANGE_STATUSES = {
    "preparing",
    "in_progress",
    "awaiting_decision",
    "completed",
    "partial",
    "stopped",
    "failed",
}
_ACTIVE_REVIEW_RUN_STATUSES = {
    "prepared",
    "accepted",
    "awaiting_decision",
    "validated",
    "failed_persistence",
}


def _valid_review_id(value: object, pattern: re.Pattern[str], prefix: str) -> bool:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        return False
    payload_stem = value[len(prefix) :].split(".", 1)[0].upper()
    return payload_stem not in _WINDOWS_RESERVED_ID_PAYLOADS


def valid_review_run_id(value: object) -> bool:
    return _valid_review_id(value, REVIEW_RUN_ID_RE, "rv-")


def valid_review_range_id(value: object) -> bool:
    return _valid_review_id(value, REVIEW_RANGE_ID_RE, "rr-")


def _reject_windows_id_collisions(values: object, *, label: str) -> None:
    if not isinstance(values, dict):
        return
    seen: dict[str, str] = {}
    for key in values:
        text = str(key)
        normalized = text.casefold()
        previous = seen.get(normalized)
        if previous is not None and previous != text:
            raise RunLedgerError(
                f"{label} ids collide under Windows normalization: {previous!r}, {text!r}"
            )
        seen[normalized] = text


class RunLedgerError(ValueError):
    """The review ledger is missing, corrupt, or internally inconsistent."""


def ledger_path(project_root: str | Path) -> Path:
    return Path(project_root) / LEDGER_REL


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


def _safe_ledger_path(project_root: str | Path) -> Path:
    """Resolve the ledger without accepting reparse storage or backup paths."""

    try:
        root = Path(project_root).resolve(strict=True)
    except OSError as exc:
        raise RunLedgerError(f"project root cannot be resolved safely: {exc}") from exc
    webnovel_dir = root / ".webnovel"
    if not webnovel_dir.is_dir() or _is_reparse(webnovel_dir):
        raise RunLedgerError("run ledger parent must be a real non-reparse .webnovel directory")
    path = webnovel_dir / "run_ledger.json"
    related = (
        path,
        path.with_suffix(path.suffix + ".lock"),
        path.with_suffix(path.suffix + ".bak"),
    )
    for candidate in related:
        if candidate.exists() or candidate.is_symlink():
            if _is_reparse(candidate) or not candidate.is_file():
                raise RunLedgerError(f"unsafe run ledger storage path: {candidate}")
    return path


class _VerifiedLedgerLock:
    """Revalidate ledger, lock, and backup leaves inside the held lock."""

    def __init__(self, project_root: str | Path) -> None:
        self.project_root = project_root
        path = _safe_ledger_path(project_root)
        self._lock = filelock.FileLock(str(path.with_suffix(path.suffix + ".lock")), timeout=10)

    def __enter__(self):
        _safe_ledger_path(self.project_root)
        self._lock.acquire()
        try:
            _safe_ledger_path(self.project_root)
        except Exception:
            self._lock.release()
            raise
        return self._lock

    def __exit__(self, exc_type, exc_value, traceback):
        validation_error: Exception | None = None
        try:
            _safe_ledger_path(self.project_root)
        except Exception as exc:  # pragma: no cover - adversarial lock swap
            validation_error = exc
        finally:
            self._lock.release()
        if validation_error is not None and exc_type is None:
            raise validation_error
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_json(path: Path, *, strict: bool = False) -> dict[str, Any]:
    try:
        if not path.exists() and not path.is_symlink():
            return {}
        if _is_reparse(path):
            raise OSError("run ledger is a reparse path")
        with path.open("rb") as handle:
            before = os.fstat(handle.fileno())
            if before.st_size <= 0 or before.st_size > MAX_LEDGER_BYTES:
                raise OSError(f"run ledger size must be 1..{MAX_LEDGER_BYTES} bytes")
            raw = handle.read(MAX_LEDGER_BYTES + 1)
            after = os.fstat(handle.fileno())
        if (
            len(raw) != before.st_size
            or len(raw) > MAX_LEDGER_BYTES
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        ):
            raise OSError("run ledger changed while it was read")
        current = path.stat(follow_symlinks=False)
        if (
            _is_reparse(path)
            or not stat.S_ISREG(current.st_mode)
            or (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        ):
            raise OSError("run ledger path changed while it was read")
        if raw.startswith(b"\xef\xbb\xbf"):
            raise UnicodeError("run ledger must be UTF-8 without BOM")
        payload = json.loads(raw.decode("utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        if strict:
            raise RunLedgerError(f"run ledger cannot be read safely: {exc}") from exc
        return {}
    if not isinstance(payload, dict):
        if strict:
            raise RunLedgerError("run ledger top level must be a JSON object")
        return {}
    return payload


def _empty_ledger() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "write": {},
        "review": {"runs": {}, "ranges": {}},
    }


def _valid_signature(value: object) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("path"), str)
        and bool(value["path"])
        and isinstance(value.get("sha256"), str)
        and _SHA256_RE.fullmatch(value["sha256"]) is not None
    )


def _valid_decision_receipt(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    required = {
        "evidence_source",
        "rollout_path",
        "sessions_root",
        "parent_thread_id",
        "parent_model",
        "parent_reasoning_effort",
        "evidence_sha256",
        "authorization_prefix_sha256",
        "binding_marker_sha256",
        "answer_sha256",
        "request_id",
    }
    if set(value) != required or value.get("evidence_source") != "codex_trace":
        return False
    if any(
        not isinstance(value.get(field), str) or not value[field]
        for field in (
            "rollout_path",
            "sessions_root",
            "parent_thread_id",
            "parent_model",
            "parent_reasoning_effort",
        )
    ):
        return False
    if any(
        not isinstance(value.get(field), str) or _SHA256_RE.fullmatch(value[field]) is None
        for field in (
            "evidence_sha256",
            "authorization_prefix_sha256",
            "binding_marker_sha256",
            "answer_sha256",
        )
    ):
        return False
    request_id = value.get("request_id")
    return isinstance(request_id, str) and re.fullmatch(r"choice-[0-9a-f]{20}", request_id) is not None


def _validate_selected_decision(
    decision: object,
    *,
    allowed: set[str],
    label: str,
) -> None:
    if (
        not isinstance(decision, dict)
        or decision.get("schema_version") != "webnovel-review-decision/v1"
        or decision.get("status") != "selected"
        or decision.get("selected") not in allowed
        or not _valid_decision_receipt(decision.get("runtime_receipt"))
    ):
        raise RunLedgerError(f"{label} has an invalid trusted decision receipt")
    receipt = decision["runtime_receipt"]
    if (
        receipt.get("request_id") != decision.get("request_id")
        or receipt.get("binding_marker_sha256")
        != hashlib.sha256(str(decision.get("binding_marker") or "").encode("utf-8")).hexdigest()
    ):
        raise RunLedgerError(f"{label} decision receipt is not bound to its marker")


def _validate_review_run(key: str, run: object) -> None:
    if not valid_review_run_id(key):
        raise RunLedgerError(f"invalid review run key: {key!r}")
    if not isinstance(run, dict) or run.get("run_id") != key:
        raise RunLedgerError(f"review run entry is corrupt: {key}")
    required = {
        "schema_version",
        "run_id",
        "range_id",
        "chapter",
        "review_mode",
        "status",
        "project_root_hash",
        "workspace_root",
        "parent_thread_id",
        "parent_model",
        "parent_reasoning_effort",
        "agent_name",
        "requested_model",
        "requested_reasoning_effort",
        "contract_hash",
        "route_sha256",
        "request_sha256",
        "binding_marker_sha256",
        "inputs",
        "protected_before",
        "attempts",
        "decision",
        "artifacts",
        "stages",
        "problems",
    }
    missing = required - set(run)
    if missing:
        raise RunLedgerError(
            f"review run {key} is missing fields: {', '.join(sorted(missing))}"
        )
    if run.get("schema_version") != _REVIEW_RUN_SCHEMA:
        raise RunLedgerError(f"review run {key} has an unsupported schema")
    if type(run.get("chapter")) is not int or run["chapter"] <= 0:
        raise RunLedgerError(f"review run {key} has an invalid chapter")
    if run.get("review_mode") not in {"full", "fast"}:
        raise RunLedgerError(f"review run {key} has an invalid mode")
    if run.get("status") not in _REVIEW_RUN_STATUSES:
        raise RunLedgerError(f"review run {key} has an invalid status")
    range_id = run.get("range_id")
    if range_id is not None and not valid_review_range_id(range_id):
        raise RunLedgerError(f"review run {key} has an invalid range id")
    for field in (
        "project_root_hash",
        "contract_hash",
        "route_sha256",
        "request_sha256",
        "binding_marker_sha256",
    ):
        value = run.get(field)
        if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
            raise RunLedgerError(f"review run {key} has an invalid {field}")
    if run.get("agent_name") != "webnovel_reviewer":
        raise RunLedgerError(f"review run {key} has an invalid agent")
    if run.get("requested_model") != "gpt-5.6-luna" or run.get(
        "requested_reasoning_effort"
    ) != "medium":
        raise RunLedgerError(f"review run {key} has invalid requested runtime identity")
    if not isinstance(run.get("workspace_root"), str) or not run["workspace_root"]:
        raise RunLedgerError(f"review run {key} has an invalid workspace root")
    if (
        not isinstance(run.get("parent_thread_id"), str)
        or _UUID_RE.fullmatch(run["parent_thread_id"]) is None
    ):
        raise RunLedgerError(f"review run {key} has an invalid parent thread id")
    if not isinstance(run.get("parent_model"), str) or not run["parent_model"]:
        raise RunLedgerError(f"review run {key} has an invalid parent model")
    parent_effort = run.get("parent_reasoning_effort")
    if parent_effort is not None and (
        not isinstance(parent_effort, str) or not parent_effort.strip()
    ):
        raise RunLedgerError(f"review run {key} has an invalid parent reasoning effort")
    inputs = run.get("inputs")
    artifacts = run.get("artifacts")
    if not isinstance(inputs, dict) or set(inputs) != {"chapter", "context"}:
        raise RunLedgerError(f"review run {key} has invalid inputs")
    if not all(_valid_signature(inputs.get(name)) for name in ("chapter", "context")):
        raise RunLedgerError(f"review run {key} has invalid input signatures")
    if not isinstance(artifacts, dict):
        raise RunLedgerError(f"review run {key} has invalid artifacts")
    for name in ("request", "context"):
        if not _valid_signature(artifacts.get(name)):
            raise RunLedgerError(f"review run {key} has an invalid {name} artifact")
    if (
        not isinstance(run.get("attempts"), list)
        or not isinstance(run.get("stages"), dict)
        or not isinstance(run.get("protected_before"), dict)
        or not isinstance(run.get("problems"), list)
    ):
        raise RunLedgerError(f"review run {key} has invalid attempts, stages, or safety state")
    evidence = run.get("runtime_evidence")
    if evidence is not None:
        if not isinstance(evidence, dict):
            raise RunLedgerError(f"review run {key} has invalid runtime evidence")
        evidence_hash = evidence.get("evidence_sha256")
        if not isinstance(evidence_hash, str) or _SHA256_RE.fullmatch(evidence_hash) is None:
            raise RunLedgerError(f"review run {key} has invalid runtime evidence hash")
        if (
            not isinstance(evidence.get("rollout_path"), str)
            or not evidence["rollout_path"]
            or not isinstance(evidence.get("child_thread_id"), str)
            or not evidence["child_thread_id"]
            or not isinstance(evidence.get("parent_thread_id"), str)
            or not evidence["parent_thread_id"]
            or not isinstance(evidence.get("binding_marker_sha256"), str)
            or _SHA256_RE.fullmatch(evidence["binding_marker_sha256"]) is None
            or not isinstance(evidence.get("output_sha256s"), list)
            or not 1 <= len(evidence["output_sha256s"]) <= 2
            or any(
                not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None
                for value in evidence["output_sha256s"]
            )
        ):
            raise RunLedgerError(f"review run {key} has malformed runtime evidence")
        if evidence.get("parent_thread_id") != run.get("parent_thread_id"):
            raise RunLedgerError(
                f"review run {key} runtime evidence is not from its prepare-time parent task"
            )
    accepted_statuses = {
        "accepted",
        "awaiting_decision",
        "validated",
        "failed_persistence",
        "persisted",
        "abandoned",
        "targeted_fix_pending",
        "targeted_fix_blocked",
        "stale",
    }
    if run.get("status") in accepted_statuses:
        if (
            run.get("actual_model") != "gpt-5.6-luna"
            or run.get("actual_reasoning_effort") != "medium"
            or not isinstance(run.get("reviewer_output_sha256"), str)
            or _SHA256_RE.fullmatch(run["reviewer_output_sha256"]) is None
            or type(run.get("has_blocking")) is not bool
            or type(run.get("blocking_count")) is not int
            or run["blocking_count"] < 0
            or evidence is None
        ):
            raise RunLedgerError(f"review run {key} has incomplete accepted evidence")
        for name in ("raw", "result"):
            if not _valid_signature(artifacts.get(name)):
                raise RunLedgerError(f"review run {key} has an invalid {name} artifact")
    if run.get("status") == "persisted":
        for name in ("metrics", "report"):
            if not _valid_signature(artifacts.get(name)):
                raise RunLedgerError(f"review run {key} has an invalid {name} artifact")
    if run.get("has_blocking") is True and run.get("status") in {
        "validated",
        "failed_persistence",
        "persisted",
        "abandoned",
        "targeted_fix_pending",
        "targeted_fix_blocked",
    }:
        _validate_selected_decision(
            run.get("decision"),
            allowed={"targeted_fix", "report_only", "abandon"},
            label=f"review run {key}",
        )
        if run["decision"]["runtime_receipt"].get("parent_thread_id") != evidence.get(
            "parent_thread_id"
        ):
            raise RunLedgerError(
                f"review run {key} decision receipt is not from its reviewer parent task"
            )


def _validate_review_range(key: str, entry: object) -> None:
    if not valid_review_range_id(key):
        raise RunLedgerError(f"invalid review range key: {key!r}")
    if not isinstance(entry, dict) or entry.get("range_id") != key:
        raise RunLedgerError(f"review range entry is corrupt: {key}")
    required = {
        "schema_version",
        "range_id",
        "status",
        "project_root_hash",
        "chapters",
        "run_ids",
        "current_index",
        "review_mode",
        "workspace_root",
        "parent_thread_id",
        "parent_model",
        "parent_reasoning_effort",
        "decision",
        "decision_history",
        "overrides",
        "skipped",
    }
    missing = required - set(entry)
    if missing:
        raise RunLedgerError(
            f"review range {key} is missing fields: {', '.join(sorted(missing))}"
        )
    if entry.get("schema_version") != _REVIEW_RANGE_SCHEMA:
        raise RunLedgerError(f"review range {key} has an unsupported schema")
    if entry.get("status") not in _REVIEW_RANGE_STATUSES:
        raise RunLedgerError(f"review range {key} has an invalid status")
    root_hash = entry.get("project_root_hash")
    if not isinstance(root_hash, str) or _SHA256_RE.fullmatch(root_hash) is None:
        raise RunLedgerError(f"review range {key} has an invalid project hash")
    chapters = entry.get("chapters")
    run_ids = entry.get("run_ids")
    if (
        not isinstance(chapters, list)
        or not 1 <= len(chapters) <= 5
        or any(type(chapter) is not int or chapter <= 0 for chapter in chapters)
        or chapters != list(range(chapters[0], chapters[-1] + 1))
        or not isinstance(run_ids, list)
        or len(run_ids) != len(chapters)
    ):
        raise RunLedgerError(f"review range {key} has invalid chapters or run ids")
    if any(
        run_id is not None and not valid_review_run_id(run_id)
        for run_id in run_ids
    ):
        raise RunLedgerError(f"review range {key} has an invalid run id")
    current_index = entry.get("current_index")
    if type(current_index) is not int or not 0 <= current_index < len(chapters):
        raise RunLedgerError(f"review range {key} has an invalid current index")
    if entry.get("review_mode") not in {"full", "fast"}:
        raise RunLedgerError(f"review range {key} has an invalid mode")
    if not isinstance(entry.get("workspace_root"), str) or not entry["workspace_root"]:
        raise RunLedgerError(f"review range {key} has an invalid workspace root")
    if (
        not isinstance(entry.get("parent_thread_id"), str)
        or _UUID_RE.fullmatch(entry["parent_thread_id"]) is None
    ):
        raise RunLedgerError(f"review range {key} has an invalid parent thread id")
    if not isinstance(entry.get("parent_model"), str) or not entry["parent_model"]:
        raise RunLedgerError(f"review range {key} has an invalid parent model")
    parent_effort = entry.get("parent_reasoning_effort")
    if parent_effort is not None and (
        not isinstance(parent_effort, str) or not parent_effort.strip()
    ):
        raise RunLedgerError(f"review range {key} has an invalid parent reasoning effort")
    if (
        not isinstance(entry.get("decision_history"), dict)
        or not isinstance(entry.get("overrides"), dict)
        or not isinstance(entry.get("skipped"), list)
    ):
        raise RunLedgerError(f"review range {key} has invalid recovery state")
    history = entry["decision_history"]
    for raw_index, decision in history.items():
        if (
            not isinstance(raw_index, str)
            or not raw_index.isdecimal()
            or raw_index != str(int(raw_index))
            or not 0 <= int(raw_index) < len(chapters)
        ):
            raise RunLedgerError(f"review range {key} has an invalid decision history index")
        _validate_selected_decision(
            decision,
            allowed={"stop", "continue"},
            label=f"review range {key}[{raw_index}]",
        )
    for raw_index, override in entry["overrides"].items():
        decision = history.get(str(raw_index))
        if override != "continue" or not isinstance(decision, dict) or decision.get("selected") != "continue":
            raise RunLedgerError(f"review range {key} has an unreceipted continuation override")
    if entry.get("status") == "stopped":
        current_decision = history.get(str(current_index))
        if not isinstance(current_decision, dict) or current_decision.get("selected") != "stop":
            raise RunLedgerError(f"stopped review range {key} lacks a trusted stop receipt")


def _validate_review_links(runs: dict[str, Any], ranges: dict[str, Any]) -> None:
    """Validate attached range slots without rejecting one recoverable orphan."""

    attached: dict[str, tuple[str, int]] = {}
    for range_id, entry in ranges.items():
        chapters = entry["chapters"]
        run_ids = entry["run_ids"]
        if entry.get("status") == "completed" and any(run_id is None for run_id in run_ids):
            raise RunLedgerError(f"completed review range {range_id} has an empty run slot")
        for index, run_id in enumerate(run_ids):
            if run_id is None:
                continue
            if run_id in attached:
                previous_range, previous_index = attached[run_id]
                raise RunLedgerError(
                    f"review run {run_id} is attached to both "
                    f"{previous_range}[{previous_index}] and {range_id}[{index}]"
                )
            run = runs.get(run_id)
            if not isinstance(run, dict):
                raise RunLedgerError(f"review range {range_id} references missing run {run_id}")
            expected = {
                "range_id": range_id,
                "chapter": chapters[index],
                "project_root_hash": entry.get("project_root_hash"),
                "review_mode": entry.get("review_mode"),
                "workspace_root": entry.get("workspace_root"),
                "parent_thread_id": entry.get("parent_thread_id"),
                "parent_model": entry.get("parent_model"),
                "parent_reasoning_effort": entry.get("parent_reasoning_effort"),
            }
            if any(run.get(field) != value for field, value in expected.items()):
                raise RunLedgerError(
                    f"review range {range_id} run slot {index} has mismatched provenance"
                )
            historical_decision = entry.get("decision_history", {}).get(str(index))
            if isinstance(historical_decision, dict):
                runtime_evidence = run.get("runtime_evidence")
                expected_parent = (
                    runtime_evidence.get("parent_thread_id")
                    if isinstance(runtime_evidence, dict)
                    else None
                )
                if (
                    not expected_parent
                    or historical_decision["runtime_receipt"].get("parent_thread_id")
                    != expected_parent
                ):
                    raise RunLedgerError(
                        f"review range {range_id}[{index}] decision receipt is not from "
                        "its reviewer parent task"
                    )
            attached[run_id] = (range_id, index)

    for run_id, run in runs.items():
        range_id = run.get("range_id")
        if range_id is None:
            continue
        if range_id not in ranges:
            raise RunLedgerError(f"review run {run_id} references missing range {range_id}")
        if run_id not in attached and run.get("status") not in _ACTIVE_REVIEW_RUN_STATUSES:
            raise RunLedgerError(
                f"terminal review run {run_id} is orphaned from range {range_id}"
            )


def _normalize_ledger(payload: dict[str, Any], *, strict: bool) -> dict[str, Any]:
    version = payload.get("schema_version")
    if not payload:
        return _empty_ledger()
    if version not in {SCHEMA_VERSION, LEGACY_SCHEMA_VERSION}:
        if strict:
            raise RunLedgerError(f"unsupported run ledger schema: {version!r}")
        return _empty_ledger()

    write = payload.get("write")
    if not isinstance(write, dict):
        if strict:
            raise RunLedgerError("run ledger write section must be an object")
        write = {}
    review = payload.get("review", {})
    if not isinstance(review, dict):
        if strict:
            raise RunLedgerError("run ledger review section must be an object")
        review = {}
    runs = review.get("runs", {})
    ranges = review.get("ranges", {})
    if not isinstance(runs, dict) or not isinstance(ranges, dict):
        if strict:
            raise RunLedgerError("run ledger review runs/ranges must be objects")
        runs, ranges = {}, {}
    if strict:
        _reject_windows_id_collisions(runs, label="review run")
        _reject_windows_id_collisions(ranges, label="review range")
        for key, run in runs.items():
            _validate_review_run(str(key), run)
        for key, entry in ranges.items():
            _validate_review_range(str(key), entry)
        _validate_review_links(runs, ranges)
    return {
        **payload,
        "schema_version": SCHEMA_VERSION,
        "write": write,
        "review": {**review, "runs": runs, "ranges": ranges},
    }


def load_ledger(project_root: str | Path, *, strict: bool = False) -> dict[str, Any]:
    path = _safe_ledger_path(project_root)
    return _normalize_ledger(
        _read_json(path, strict=strict),
        strict=strict,
    )


def _save_ledger_unlocked(project_root: str | Path, ledger: dict[str, Any]) -> Path:
    path = _safe_ledger_path(project_root)
    normalized = _normalize_ledger(dict(ledger), strict=True)
    atomic_write_json(path, normalized, use_lock=False, backup=True)
    _safe_ledger_path(project_root)
    return path


def save_ledger(project_root: str | Path, ledger: dict[str, Any]) -> Path:
    with _VerifiedLedgerLock(project_root):
        return _save_ledger_unlocked(project_root, ledger)


@contextmanager
def locked_ledger(
    project_root: str | Path,
    *,
    strict: bool = True,
) -> Iterator[dict[str, Any]]:
    """Hold the ledger lock across one read-modify-atomic-write transaction."""

    with _VerifiedLedgerLock(project_root):
        _safe_ledger_path(project_root)
        ledger = load_ledger(project_root, strict=strict)
        yield ledger
        _safe_ledger_path(project_root)
        _save_ledger_unlocked(project_root, ledger)


def get_review_run(project_root: str | Path, run_id: str) -> dict[str, Any] | None:
    if not valid_review_run_id(run_id):
        raise RunLedgerError("invalid review run id")
    ledger = load_ledger(project_root, strict=True)
    run = ledger["review"]["runs"].get(run_id)
    if run is None:
        return None
    if not isinstance(run, dict) or run.get("run_id") != run_id:
        raise RunLedgerError("review run entry is corrupt")
    return dict(run)


def get_review_range(project_root: str | Path, range_id: str) -> dict[str, Any] | None:
    if not valid_review_range_id(range_id):
        raise RunLedgerError("invalid review range id")
    ledger = load_ledger(project_root, strict=True)
    entry = ledger["review"]["ranges"].get(range_id)
    if entry is None:
        return None
    if not isinstance(entry, dict) or entry.get("range_id") != range_id:
        raise RunLedgerError("review range entry is corrupt")
    return dict(entry)


def file_signature(path: str | Path) -> dict[str, Any]:
    target = Path(path)
    if not target.is_file():
        return {"path": str(target), "exists": False}
    stat = target.stat()
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    return {
        "path": str(target),
        "exists": True,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": digest,
    }


def _chapter_key(chapter: int) -> str:
    return f"chapter_{int(chapter):03d}"


def _write_run(ledger: dict[str, Any], chapter: int, mode: str) -> dict[str, Any]:
    write = ledger.setdefault("write", {})
    key = _chapter_key(chapter)
    run = write.setdefault(key, {})
    run.setdefault("chapter", int(chapter))
    run.setdefault("mode", mode or "default")
    run.setdefault("steps", {})
    run["updated_at"] = _now_iso()
    return run


def record_write_step(
    project_root: str | Path,
    *,
    chapter: int,
    step: str,
    status: str,
    mode: str = "default",
    inputs: dict[str, str | Path] | None = None,
    outputs: dict[str, str | Path] | None = None,
    problems: list[str] | None = None,
    auto_handled: list[str] | None = None,
    duration_ms: int = 0,
) -> dict[str, Any]:
    if step not in WRITE_STEPS:
        raise ValueError(f"unknown write step: {step}")
    root = Path(project_root)
    input_signatures = {
        str(name): file_signature(path)
        for name, path in (inputs or {}).items()
    }
    output_signatures = {
        str(name): file_signature(path)
        for name, path in (outputs or {}).items()
    }
    entry = {
        "step": step,
        "status": status,
        "recorded_at": _now_iso(),
        "duration_ms": int(duration_ms or 0),
        "inputs": input_signatures,
        "outputs": output_signatures,
        "problems": list(problems or []),
        "auto_handled": list(auto_handled or []),
    }
    # Keep the v1 write API while making its read-modify-write cycle atomic.
    with locked_ledger(root, strict=False) as ledger:
        run = _write_run(ledger, chapter, mode)
        run["steps"][step] = entry
    return entry


def _same_signature(expected: dict[str, Any] | None, current: dict[str, Any]) -> bool:
    if not isinstance(expected, dict):
        return False
    return bool(expected.get("exists")) and expected.get("sha256") == current.get("sha256")


def _step_completed(run: dict[str, Any], step: str) -> dict[str, Any] | None:
    steps = run.get("steps") if isinstance(run.get("steps"), dict) else {}
    entry = steps.get(step)
    if not isinstance(entry, dict):
        return None
    return entry if entry.get("status") == "completed" else None


def _trusted_output(entry: dict[str, Any] | None, name: str) -> bool:
    if not entry:
        return False
    outputs = entry.get("outputs") if isinstance(entry.get("outputs"), dict) else {}
    expected = outputs.get(name)
    if not isinstance(expected, dict):
        return False
    return _same_signature(expected, file_signature(expected.get("path") or ""))


def _trusted_input(entry: dict[str, Any] | None, name: str, path: Path | None) -> bool:
    if not entry or path is None:
        return False
    inputs = entry.get("inputs") if isinstance(entry.get("inputs"), dict) else {}
    expected = inputs.get(name)
    if not isinstance(expected, dict):
        return False
    return _same_signature(expected, file_signature(path))


def _commit_path(project_root: Path, chapter: int) -> Path:
    return project_root / ".story-system" / "commits" / f"chapter_{chapter:03d}.commit.json"


def _commit_status(project_root: Path, chapter: int) -> str:
    payload = _read_json(_commit_path(project_root, chapter))
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    return str(meta.get("status") or "")


def _projection_done(project_root: Path, chapter: int) -> bool:
    run = latest_projection_run(project_root, chapter=chapter)
    statuses = projection_status_from_run(run) if run else {}
    if not statuses:
        payload = _read_json(_commit_path(project_root, chapter))
        raw = payload.get("projection_status") if isinstance(payload.get("projection_status"), dict) else {}
        statuses = {str(key): str(value) for key, value in raw.items()}
    if not statuses:
        return False
    return all(str(statuses.get(writer) or "") in OK_PROJECTION_STATUSES for writer in REQUIRED_PROJECTION_WRITERS)


def _backup_exists(project_root: Path, chapter: int) -> bool:
    backup_dir = project_root / ".webnovel" / "backups"
    if not backup_dir.is_dir():
        return False
    return any(backup_dir.glob(f"ch{chapter:04d}*"))


def _latest_contract_mtime(project_root: Path, chapter: int) -> int:
    mtimes: list[int] = []
    for path in contract_files_for_chapter(project_root, chapter).values():
        if path.is_file():
            mtimes.append(path.stat().st_mtime_ns)
    return max(mtimes or [0])


def build_write_resume_plan(
    project_root: str | Path,
    *,
    chapter: int,
    mode: str = "default",
) -> dict[str, Any]:
    root = Path(project_root)
    ledger = load_ledger(root)
    run = ((ledger.get("write") or {}).get(_chapter_key(chapter)) or {})
    if not isinstance(run, dict):
        run = {}

    chapter_file = find_chapter_file(root, chapter)
    draft_entry = _step_completed(run, "draft")
    review_entry = _step_completed(run, "review")
    data_entry = _step_completed(run, "data")
    commit_status = _commit_status(root, chapter)
    accepted_done = commit_status == "accepted"
    rejected_done = commit_status == "rejected"

    steps: list[dict[str, str]] = []
    confirmations: list[dict[str, str]] = []

    draft_trusted = bool(accepted_done or (chapter_file and _trusted_output(draft_entry, "chapter_file")))
    if draft_entry and chapter_file and not draft_trusted:
        confirmations.append(
            {
                "code": "chapter_file_changed",
                "message": "正文文件与上次记录不一致，需要确认沿用手改正文还是重新起草。",
            }
        )
    if draft_trusted and chapter_file and _latest_contract_mtime(root, chapter) > chapter_file.stat().st_mtime_ns:
        draft_trusted = False
        confirmations.append(
            {
                "code": "outline_newer_than_draft",
                "message": "章纲或合同晚于正文，需要确认沿用旧正文还是重新起草。",
            }
        )
    steps.append({"step": "draft", "action": "skip" if draft_trusted else "run", "reason": "正文可信" if draft_trusted else "正文缺失或已过期"})

    review_path = root / COMMIT_ARTIFACT_FILES[0]
    review_trusted = bool(accepted_done or (draft_trusted and review_path.is_file() and _trusted_input(review_entry, "chapter_file", chapter_file)))
    steps.append({"step": "review", "action": "skip" if review_trusted else "run", "reason": "审查结果匹配当前正文" if review_trusted else "正文变更后需要重审"})

    data_paths = [root / rel for rel in COMMIT_ARTIFACT_FILES[1:]]
    data_trusted = bool(accepted_done or (review_trusted and all(path.is_file() for path in data_paths) and _trusted_input(data_entry, "chapter_file", chapter_file)))
    steps.append({"step": "data", "action": "skip" if data_trusted else "run", "reason": "故事事实提取可信" if data_trusted else "data artifacts 缺失或过期"})

    if accepted_done:
        confirmations.append(
            {
                "code": "chapter_already_accepted",
                "message": "本章已 accepted；重跑前需要确认是重写正文，还是只查看状态/补跑后续步骤。",
            }
        )
    if rejected_done:
        confirmations.append(
            {
                "code": "chapter_commit_rejected",
                "message": "本章事实提交未通过，需要先处理审查/大纲/消歧阻断项，再重新提交。",
            }
        )
    commit_reason = (
        f"commit status={commit_status}"
        if accepted_done
        else "commit rejected，需要修复后重新提交"
        if rejected_done
        else "尚未生成 commit"
    )
    steps.append({"step": "commit", "action": "skip" if accepted_done else "run", "reason": commit_reason})

    projection_done = bool(commit_status == "accepted" and _projection_done(root, chapter))
    projection_action = "skip" if projection_done else ("retry" if accepted_done else "run")
    projection_reason = (
        "资料更新已完成"
        if projection_done
        else "commit accepted 后再更新资料"
        if not accepted_done
        else "需要补跑资料更新"
    )
    steps.append({"step": "projection", "action": projection_action, "reason": projection_reason})

    backup_done = _backup_exists(root, chapter)
    backup_action = "skip" if backup_done else ("retry" if commit_status == "accepted" else "run")
    steps.append({"step": "backup", "action": backup_action, "reason": "备份已确认" if backup_done else "备份未确认"})

    resume_from = "done"
    for item in steps:
        if item["action"] != "skip":
            resume_from = item["step"]
            break

    return {
        "schema_version": WRITE_RESUME_SCHEMA_VERSION,
        "stage": "write",
        "chapter": int(chapter),
        "mode": mode or "default",
        "resume_from": resume_from,
        "steps": steps,
        "needs_user_confirmation": confirmations,
    }


def format_resume_plan(plan: dict[str, Any], output_format: str = "json") -> str:
    if output_format == "json":
        return json.dumps(plan, ensure_ascii=False, indent=2)
    lines = [
        f"resume_from: {plan.get('resume_from')}",
        f"chapter: {plan.get('chapter')}",
    ]
    for item in plan.get("steps") or []:
        lines.append(f"- {item.get('step')}: {item.get('action')} ({item.get('reason')})")
    confirmations = plan.get("needs_user_confirmation") or []
    if confirmations:
        lines.append("needs_user_confirmation:")
        lines.extend(f"- {item.get('code')}: {item.get('message')}" for item in confirmations)
    return "\n".join(lines)


def _parse_path_map(raw: str) -> dict[str, str]:
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"不是合法 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("必须是 JSON object")
    return {str(key): str(value) for key, value in payload.items()}


def _parse_string_list(raw: str) -> list[str]:
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"不是合法 JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise ValueError("必须是 JSON list")
    return [str(item) for item in payload]


def main() -> None:
    parser = argparse.ArgumentParser(description="Record and inspect webnovel write run ledger")
    parser.add_argument("--project-root", required=True, help="书项目根目录")
    sub = parser.add_subparsers(dest="action", required=True)
    record = sub.add_parser("record-write-step", help="记录写章步骤状态")
    record.add_argument("--chapter", type=int, required=True)
    record.add_argument("--step", choices=WRITE_STEPS, required=True)
    record.add_argument("--status", required=True)
    record.add_argument("--mode", default="default")
    record.add_argument("--inputs-json", default="{}")
    record.add_argument("--outputs-json", default="{}")
    record.add_argument("--problems-json", default="[]")
    record.add_argument("--auto-handled-json", default="[]")
    record.add_argument("--duration-ms", type=int, default=0)
    record.add_argument("--format", choices=["json", "text"], default="json")
    resume = sub.add_parser("write-resume", help="输出写章断点续跑建议")
    resume.add_argument("--chapter", type=int, required=True)
    resume.add_argument("--mode", default="default")
    resume.add_argument("--format", choices=["json", "text"], default="json")
    args = parser.parse_args()

    if args.action == "record-write-step":
        try:
            entry = record_write_step(
                args.project_root,
                chapter=args.chapter,
                step=args.step,
                status=args.status,
                mode=args.mode,
                inputs=_parse_path_map(args.inputs_json),
                outputs=_parse_path_map(args.outputs_json),
                problems=_parse_string_list(args.problems_json),
                auto_handled=_parse_string_list(args.auto_handled_json),
                duration_ms=args.duration_ms,
            )
        except ValueError as exc:
            raise SystemExit(str(exc))
        if args.format == "json":
            print(json.dumps(entry, ensure_ascii=False, indent=2))
        else:
            print(f"{entry['step']}: {entry['status']}")
        return

    if args.action == "write-resume":
        plan = build_write_resume_plan(args.project_root, chapter=args.chapter, mode=args.mode)
        print(format_resume_plan(plan, args.format))


if __name__ == "__main__":
    main()
