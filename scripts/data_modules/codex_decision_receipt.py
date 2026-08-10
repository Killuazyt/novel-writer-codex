#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Scope-bound author decisions proven by a trusted Codex parent rollout.

This module never treats a caller-supplied choice as authorization.  It builds
one finite-choice request and an exact assistant marker, then derives the
selected branch from the first durable user message after that unique marker.
Receipts contain hashes and the normalized option id, never the plaintext user
answer.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

from .codex_interaction import ChoiceProtocolError, build_choice_request, resolve_choice
from .codex_m3_smoke import (
    SmokeEvidenceError,
    coalesce_session_meta_payloads,
    coalesce_turn_context_payloads,
)


DECISION_REQUEST_SCHEMA = "webnovel-codex-decision-request/v1"
DECISION_RECEIPT_SCHEMA = "webnovel-codex-decision-receipt/v1"
DECISION_MARKER_SCHEMA = "WEBNOVEL_CODEX_DECISION/v1"
MAX_SCOPE_BYTES = 1024 * 1024
MAX_ROLLOUT_BYTES = 32 * 1024 * 1024
MAX_ANSWER_BYTES = 4096
_SHA256_LENGTH = 64


class DecisionReceiptError(ValueError):
    """A decision request or its host evidence is missing or inconsistent."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _canonical_bytes(payload: object, *, code: str = "invalid_decision_request") -> bytes:
    try:
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise DecisionReceiptError(code, "decision payload must be canonical JSON") from exc


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _payload_sha256(payload: object, *, code: str = "invalid_decision_request") -> str:
    return _sha256(_canonical_bytes(payload, code=code))


def _json_scope_snapshot(scope: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(scope, Mapping) or not scope:
        raise DecisionReceiptError("invalid_decision_scope", "decision scope must be a non-empty object")
    if any(not isinstance(key, str) or not key for key in scope):
        raise DecisionReceiptError("invalid_decision_scope", "decision scope keys must be non-empty strings")
    raw = _canonical_bytes(dict(scope), code="invalid_decision_scope")
    if len(raw) > MAX_SCOPE_BYTES:
        raise DecisionReceiptError("invalid_decision_scope", "decision scope exceeds the bounded size")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):  # defensive; the input is already a Mapping
        raise DecisionReceiptError("invalid_decision_scope", "decision scope must be an object")
    return value


def _clean_identity(value: object, *, label: str) -> str:
    text = str(value or "")
    if not text or text.strip() != text or len(text) > 160:
        raise DecisionReceiptError("invalid_parent_identity", f"{label} must be a non-empty canonical string")
    return text


def _canonical_thread_id(value: object, *, label: str) -> str:
    text = str(value or "")
    if not text or text.strip() != text:
        raise DecisionReceiptError(
            "invalid_parent_thread",
            f"{label} must be a canonical non-zero UUID",
        )
    try:
        parsed = UUID(text)
    except (ValueError, AttributeError) as exc:
        raise DecisionReceiptError(
            "invalid_parent_thread",
            f"{label} must be a canonical non-zero UUID",
        ) from exc
    canonical = str(parsed)
    if parsed.int == 0 or text != canonical:
        raise DecisionReceiptError(
            "invalid_parent_thread",
            f"{label} must be a canonical non-zero UUID",
        )
    return canonical


def _require_current_thread(expected: object) -> str:
    expected_id = _canonical_thread_id(expected, label="expected parent thread id")
    supplied_id = _canonical_thread_id(
        os.environ.get("CODEX_THREAD_ID"),
        label="CODEX_THREAD_ID",
    )
    if supplied_id != expected_id:
        raise DecisionReceiptError(
            "cross_thread_decision",
            "current CODEX_THREAD_ID does not equal the decision parent thread",
        )
    return expected_id


def _parent_binding(
    *,
    expected_parent_thread_id: object,
    expected_parent_model: object,
    expected_parent_reasoning_effort: object,
) -> dict[str, str]:
    return {
        "thread_id": _require_current_thread(expected_parent_thread_id),
        "model": _clean_identity(expected_parent_model, label="expected parent model"),
        "reasoning_effort": _clean_identity(
            expected_parent_reasoning_effort,
            label="expected parent reasoning effort",
        ),
    }


def _marker_payload(
    *,
    scope_sha256: str,
    question_id: str,
    request_id: str,
    parent_binding: Mapping[str, str],
) -> dict[str, str]:
    return {
        "schema_version": DECISION_MARKER_SCHEMA,
        "scope_sha256": scope_sha256,
        "question_id": question_id,
        "request_id": request_id,
        "parent_thread_id": parent_binding["thread_id"],
        "parent_model": parent_binding["model"],
        "parent_reasoning_effort": parent_binding["reasoning_effort"],
    }


def _binding_marker(payload: Mapping[str, Any]) -> str:
    return f"{DECISION_MARKER_SCHEMA} {_canonical_bytes(payload).decode('utf-8')}"


def _request_body(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key != "request_sha256"}


def _validate_request(request: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(request, Mapping):
        raise DecisionReceiptError("invalid_decision_request", "decision request must be an object")
    required = {
        "schema_version",
        "scope",
        "scope_sha256",
        "question_id",
        "choice_request",
        "parent_binding",
        "binding_marker",
        "binding_marker_sha256",
        "request_sha256",
    }
    if set(request) != required or request.get("schema_version") != DECISION_REQUEST_SCHEMA:
        raise DecisionReceiptError("invalid_decision_request", "unsupported or malformed decision request")

    scope = request.get("scope")
    if not isinstance(scope, Mapping):
        raise DecisionReceiptError("invalid_decision_scope", "decision scope must be an object")
    scope_snapshot = _json_scope_snapshot(scope)
    scope_sha256 = _payload_sha256(scope_snapshot, code="invalid_decision_scope")
    if request.get("scope_sha256") != scope_sha256:
        raise DecisionReceiptError("cross_scope_decision", "decision scope hash does not match its content")

    parent = request.get("parent_binding")
    if not isinstance(parent, Mapping) or set(parent) != {"thread_id", "model", "reasoning_effort"}:
        raise DecisionReceiptError("invalid_parent_identity", "decision parent binding is malformed")
    parent_snapshot = _parent_binding(
        expected_parent_thread_id=parent.get("thread_id"),
        expected_parent_model=parent.get("model"),
        expected_parent_reasoning_effort=parent.get("reasoning_effort"),
    )

    choice = request.get("choice_request")
    if not isinstance(choice, Mapping):
        raise DecisionReceiptError("invalid_decision_request", "finite-choice request is missing")
    try:
        questions = choice.get("questions")
        transport = choice.get("transport")
        if not isinstance(questions, list) or len(questions) != 1 or not isinstance(transport, str):
            raise ChoiceProtocolError("scope-bound decisions require exactly one question")
        rebuilt_choice = build_choice_request(questions, transport=transport)
        pending = resolve_choice(rebuilt_choice, None)
    except ChoiceProtocolError as exc:
        raise DecisionReceiptError("invalid_decision_request", str(exc)) from exc
    if dict(choice) != rebuilt_choice or pending.get("status") != "awaiting_user":
        raise DecisionReceiptError("invalid_decision_request", "finite-choice request is not canonical")
    question = rebuilt_choice["questions"][0]
    question_id = str(request.get("question_id") or "")
    if question.get("id") != question_id or not 2 <= len(question.get("options") or []) <= 3:
        raise DecisionReceiptError("invalid_decision_request", "question identity or option count is invalid")

    marker_payload = _marker_payload(
        scope_sha256=scope_sha256,
        question_id=question_id,
        request_id=rebuilt_choice["request_id"],
        parent_binding=parent_snapshot,
    )
    marker = _binding_marker(marker_payload)
    if request.get("binding_marker") != marker or request.get("binding_marker_sha256") != _sha256(
        marker.encode("utf-8")
    ):
        raise DecisionReceiptError("invalid_decision_marker", "decision marker binding is invalid")

    body = _request_body(request)
    if request.get("request_sha256") != _payload_sha256(body):
        raise DecisionReceiptError("invalid_decision_request", "decision request hash is invalid")
    return {
        **body,
        "scope": scope_snapshot,
        "choice_request": rebuilt_choice,
        "parent_binding": parent_snapshot,
        "request_sha256": request["request_sha256"],
    }


def build_scope_bound_decision_request(
    scope: Mapping[str, Any],
    *,
    question_id: str,
    prompt: str,
    options: Sequence[Mapping[str, Any]],
    expected_parent_thread_id: str,
    expected_parent_model: str,
    expected_parent_reasoning_effort: str,
    transport: str = "structured_choice",
) -> dict[str, Any]:
    """Build one deterministic, zero-write, scope-bound choice request."""

    scope_snapshot = _json_scope_snapshot(scope)
    parent = _parent_binding(
        expected_parent_thread_id=expected_parent_thread_id,
        expected_parent_model=expected_parent_model,
        expected_parent_reasoning_effort=expected_parent_reasoning_effort,
    )
    try:
        choice = build_choice_request(
            [{"id": question_id, "prompt": prompt, "options": list(options)}],
            transport=transport,
        )
    except ChoiceProtocolError as exc:
        raise DecisionReceiptError("invalid_decision_request", str(exc)) from exc
    scope_sha256 = _payload_sha256(scope_snapshot, code="invalid_decision_scope")
    marker_payload = _marker_payload(
        scope_sha256=scope_sha256,
        question_id=question_id,
        request_id=choice["request_id"],
        parent_binding=parent,
    )
    marker = _binding_marker(marker_payload)
    body: dict[str, Any] = {
        "schema_version": DECISION_REQUEST_SCHEMA,
        "scope": scope_snapshot,
        "scope_sha256": scope_sha256,
        "question_id": question_id,
        "choice_request": choice,
        "parent_binding": parent,
        "binding_marker": marker,
        "binding_marker_sha256": _sha256(marker.encode("utf-8")),
    }
    request = {**body, "request_sha256": _payload_sha256(body)}
    return _validate_request(request)


def _is_reparse(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        info = path.lstat()
    except OSError:
        return False
    attributes = int(getattr(info, "st_file_attributes", 0) or 0)
    flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(attributes & flag)


def _absolute_lexical(path: str | Path, *, label: str) -> Path:
    value = Path(path)
    if not value.is_absolute():
        raise DecisionReceiptError("invalid_rollout_path", f"{label} must be absolute")
    return Path(os.path.abspath(value))


def _path_chain(path: Path) -> list[Path]:
    parts = path.parts
    if not parts:
        return []
    current = Path(parts[0])
    chain = [current]
    for part in parts[1:]:
        current /= part
        chain.append(current)
    return chain


def _reject_reparse_chain(path: Path, *, label: str) -> None:
    for current in _path_chain(path):
        try:
            current.lstat()
        except OSError as exc:
            raise DecisionReceiptError("invalid_rollout_path", f"{label} path is missing or unreadable") from exc
        if _is_reparse(current):
            raise DecisionReceiptError("invalid_rollout_path", f"{label} path contains a reparse point")


def _trusted_paths(
    sessions_root: str | Path,
    rollout_path: str | Path,
    *,
    expected_thread_id: str,
) -> tuple[Path, Path]:
    root_lexical = _absolute_lexical(sessions_root, label="sessions_root")
    rollout_lexical = _absolute_lexical(rollout_path, label="rollout_path")
    _reject_reparse_chain(root_lexical, label="sessions_root")
    _reject_reparse_chain(rollout_lexical, label="rollout_path")
    try:
        root_stat = root_lexical.stat(follow_symlinks=False)
        rollout_stat = rollout_lexical.stat(follow_symlinks=False)
        root = root_lexical.resolve(strict=True)
        rollout = rollout_lexical.resolve(strict=True)
        rollout.relative_to(root)
    except (OSError, ValueError) as exc:
        raise DecisionReceiptError(
            "invalid_rollout_path",
            "rollout must be a file under the specified trusted sessions root",
        ) from exc
    if not stat.S_ISDIR(root_stat.st_mode) or not stat.S_ISREG(rollout_stat.st_mode):
        raise DecisionReceiptError("invalid_rollout_path", "sessions root or rollout has the wrong file type")
    if rollout.suffix.casefold() != ".jsonl" or expected_thread_id not in rollout.name:
        raise DecisionReceiptError(
            "invalid_rollout_path",
            "rollout filename must identify the expected parent thread",
        )
    return root, rollout


def _stable_rollout_bytes(root: Path, rollout: Path) -> bytes:
    _reject_reparse_chain(root, label="sessions_root")
    _reject_reparse_chain(rollout, label="rollout_path")
    try:
        with rollout.open("rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size <= 0 or before.st_size > MAX_ROLLOUT_BYTES:
                raise DecisionReceiptError(
                    "invalid_rollout",
                    "parent rollout size or file type is outside the trusted bound",
                )
            raw = handle.read(MAX_ROLLOUT_BYTES + 1)
            after = os.fstat(handle.fileno())
    except DecisionReceiptError:
        raise
    except OSError as exc:
        raise DecisionReceiptError("invalid_rollout", "parent rollout cannot be read safely") from exc
    identity = lambda item: (  # noqa: E731 - compact immutable identity tuple
        item.st_dev,
        item.st_ino,
        item.st_mode,
        item.st_size,
        item.st_mtime_ns,
    )
    if identity(before) != identity(after) or len(raw) != before.st_size or len(raw) > MAX_ROLLOUT_BYTES:
        raise DecisionReceiptError("invalid_rollout", "parent rollout changed while it was read")
    try:
        current = rollout.stat(follow_symlinks=False)
    except OSError as exc:
        raise DecisionReceiptError("invalid_rollout", "parent rollout identity cannot be verified") from exc
    if _is_reparse(rollout) or not stat.S_ISREG(current.st_mode) or identity(current) != identity(after):
        raise DecisionReceiptError("invalid_rollout", "parent rollout path changed while it was read")
    _reject_reparse_chain(root, label="sessions_root")
    _reject_reparse_chain(rollout, label="rollout_path")
    if raw.startswith(b"\xef\xbb\xbf"):
        raise DecisionReceiptError("invalid_rollout", "parent rollout must be UTF-8 without BOM")
    return raw


def _rollout_records(raw: bytes) -> list[tuple[int, int, Mapping[str, Any]]]:
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
                raise ValueError("rollout event must be an object")
            records.append((start, offset, event))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise DecisionReceiptError("invalid_rollout", "parent rollout is not bounded UTF-8 JSONL") from exc
    if not records:
        raise DecisionReceiptError("invalid_rollout", "parent rollout contains no durable records")
    return records


def _verify_parent_identity(
    records: Sequence[tuple[int, int, Mapping[str, Any]]],
    parent: Mapping[str, str],
) -> None:
    events = [event for _, _, event in records]
    try:
        session_index, session = coalesce_session_meta_payloads(
            events,
            expected_thread_id=parent["thread_id"],
        )
        turn_events = [
            event
            for event in events[session_index + 1 :]
            if event.get("type") == "turn_context"
        ]
        if not turn_events:
            raise SmokeEvidenceError("rollout lacks turn_context after session_meta")
        turns = coalesce_turn_context_payloads(turn_events)
    except SmokeEvidenceError as exc:
        code = (
            "cross_thread_decision"
            if str(exc) == "session_meta thread id mismatch"
            else "invalid_parent_identity"
        )
        raise DecisionReceiptError(code, str(exc)) from exc

    source = session.get("source")
    if bool(str(session.get("parent_thread_id") or "").strip()) or (
        isinstance(source, Mapping) and source.get("subagent") is not None
    ):
        raise DecisionReceiptError("child_rollout_rejected", "decision rollout belongs to a child Agent")
    session_model = session.get("model")
    if session_model is not None and session_model != parent["model"]:
        raise DecisionReceiptError("parent_model_mismatch", "parent session model does not match the request")

    for turn in turns:
        if turn.get("model") != parent["model"]:
            raise DecisionReceiptError("parent_model_mismatch", "parent turn model does not match the request")
        if turn.get("effort") != parent["reasoning_effort"]:
            raise DecisionReceiptError(
                "parent_effort_mismatch",
                "parent reasoning effort does not match the request",
            )


def _message_text(payload: Mapping[str, Any]) -> tuple[str, str] | None:
    message = payload.get("item") if isinstance(payload.get("item"), Mapping) else payload
    if not isinstance(message, Mapping) or message.get("type") != "message":
        return None
    role = str(message.get("role") or "")
    content = message.get("content")
    if isinstance(content, str):
        return role, content
    if not isinstance(content, list):
        return None
    texts: list[str] = []
    for item in content:
        if not isinstance(item, Mapping) or item.get("type") not in {"input_text", "output_text", "text"}:
            continue
        text = item.get("text")
        if isinstance(text, str):
            texts.append(text)
    return (role, "".join(texts)) if texts else None


def _answer_after_unique_marker(
    raw: bytes,
    records: Sequence[tuple[int, int, Mapping[str, Any]]],
    *,
    marker: str,
) -> tuple[str, int]:
    marker_positions: list[int] = []
    for index, (_, _, event) in enumerate(records):
        if event.get("type") != "response_item" or not isinstance(event.get("payload"), Mapping):
            continue
        parsed = _message_text(event["payload"])
        if parsed is None or parsed[0] != "assistant":
            continue
        occurrences = sum(1 for line in parsed[1].splitlines() if line.strip() == marker)
        marker_positions.extend([index] * occurrences)
    if len(marker_positions) != 1:
        raise DecisionReceiptError(
            "decision_marker_not_unique",
            "parent rollout must contain exactly one exact assistant decision marker",
        )

    for _, end, event in records[marker_positions[0] + 1 :]:
        if event.get("type") != "response_item" or not isinstance(event.get("payload"), Mapping):
            continue
        parsed = _message_text(event["payload"])
        if parsed is None or not parsed[1].strip():
            continue
        role, text = parsed
        if role != "user":
            raise DecisionReceiptError(
                "decision_answer_missing",
                "the next durable message after the decision marker is not a user answer",
            )
        answer = text.strip()
        if len(answer.encode("utf-8")) > MAX_ANSWER_BYTES:
            raise DecisionReceiptError("invalid_decision_answer", "decision answer exceeds the bounded size")
        if end <= 0 or end > len(raw):
            raise DecisionReceiptError("invalid_rollout", "decision answer prefix boundary is invalid")
        return answer, end
    raise DecisionReceiptError(
        "decision_answer_missing",
        "parent rollout has no durable user answer after the exact decision marker",
    )


def _receipt_body(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key != "receipt_sha256"}


def _selected_receipt(
    request: Mapping[str, Any],
    *,
    sessions_root: str | Path,
    rollout_path: str | Path,
) -> dict[str, Any]:
    checked = _validate_request(request)
    parent = checked["parent_binding"]
    root, rollout = _trusted_paths(
        sessions_root,
        rollout_path,
        expected_thread_id=parent["thread_id"],
    )
    raw = _stable_rollout_bytes(root, rollout)
    records = _rollout_records(raw)
    _verify_parent_identity(records, parent)
    answer, prefix_bytes = _answer_after_unique_marker(
        raw,
        records,
        marker=checked["binding_marker"],
    )
    try:
        resolution = resolve_choice(checked["choice_request"], answer)
    except ChoiceProtocolError as exc:
        raise DecisionReceiptError("invalid_decision_answer", str(exc)) from exc
    selected = resolution.get("selected_branches")
    question_id = checked["question_id"]
    if (
        resolution.get("status") != "selected"
        or resolution.get("write_allowed") is not True
        or not isinstance(selected, Mapping)
        or set(selected) != {question_id}
        or not isinstance(selected.get(question_id), str)
    ):
        raise DecisionReceiptError(
            "invalid_decision_answer",
            "user answer did not select exactly one offered decision branch",
        )
    body = {
        "schema_version": DECISION_RECEIPT_SCHEMA,
        "status": "selected",
        "selected": selected[question_id],
        "question_id": question_id,
        "scope_sha256": checked["scope_sha256"],
        "request_id": checked["choice_request"]["request_id"],
        "request_sha256": checked["request_sha256"],
        "binding_marker_sha256": checked["binding_marker_sha256"],
        "answer_sha256": _sha256(answer.encode("utf-8")),
        "authorization_prefix_sha256": _sha256(raw[:prefix_bytes]),
        "authorization_prefix_bytes": prefix_bytes,
        "parent_thread_id": parent["thread_id"],
        "parent_model": parent["model"],
        "parent_reasoning_effort": parent["reasoning_effort"],
        "sessions_root": str(root),
        "rollout_path": str(rollout),
        "evidence_source": "codex_trace",
    }
    return {**body, "receipt_sha256": _payload_sha256(body, code="invalid_decision_receipt")}


def select_scope_bound_decision(
    request: Mapping[str, Any],
    *,
    sessions_root: str | Path,
    rollout_path: str | Path,
) -> dict[str, Any]:
    """Create a selected receipt from the current trusted parent rollout."""

    return _selected_receipt(
        request,
        sessions_root=sessions_root,
        rollout_path=rollout_path,
    )


def verify_scope_bound_decision_receipt(
    request: Mapping[str, Any],
    receipt: Mapping[str, Any],
    *,
    sessions_root: str | Path,
    rollout_path: str | Path,
) -> dict[str, Any]:
    """Revalidate a selected receipt while permitting rollout-only appends."""

    if not isinstance(receipt, Mapping):
        raise DecisionReceiptError("invalid_decision_receipt", "decision receipt must be an object")
    required = {
        "schema_version",
        "status",
        "selected",
        "question_id",
        "scope_sha256",
        "request_id",
        "request_sha256",
        "binding_marker_sha256",
        "answer_sha256",
        "authorization_prefix_sha256",
        "authorization_prefix_bytes",
        "parent_thread_id",
        "parent_model",
        "parent_reasoning_effort",
        "sessions_root",
        "rollout_path",
        "evidence_source",
        "receipt_sha256",
    }
    if (
        set(receipt) != required
        or receipt.get("schema_version") != DECISION_RECEIPT_SCHEMA
        or receipt.get("status") != "selected"
        or receipt.get("evidence_source") != "codex_trace"
    ):
        raise DecisionReceiptError("invalid_decision_receipt", "unsupported or malformed decision receipt")
    body = _receipt_body(receipt)
    claimed = str(receipt.get("receipt_sha256") or "")
    if len(claimed) != _SHA256_LENGTH or claimed != _payload_sha256(
        body,
        code="invalid_decision_receipt",
    ):
        raise DecisionReceiptError("invalid_decision_receipt", "decision receipt hash is invalid")
    expected = _selected_receipt(
        request,
        sessions_root=sessions_root,
        rollout_path=rollout_path,
    )
    if dict(receipt) != expected:
        raise DecisionReceiptError(
            "invalid_decision_receipt",
            "stored decision receipt no longer matches its trusted rollout prefix and scope",
        )
    return expected
