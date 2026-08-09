#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Host-neutral finite-choice gates for author decisions.

This module deliberately does not request Codex permissions and does not write
project files.  It turns a business decision into either a structured-choice
payload or a numbered text fallback, then authorizes only explicitly selected
branches.  Callers remain responsible for invoking the selected runtime action.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

try:
    from security_utils import atomic_write_json
except ImportError:  # pragma: no cover - package-style import compatibility
    from scripts.security_utils import atomic_write_json


SCHEMA_VERSION = "webnovel-codex-choice/v1"
TRANSPORTS = {"structured_choice", "numbered_fallback"}
_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


class ChoiceProtocolError(ValueError):
    """Raised when a choice request or answer violates the protocol."""


def _canonical_json(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _selector_token(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value or "").strip()).casefold()


def _request_id(questions: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256(_canonical_json(questions).encode("utf-8")).hexdigest()
    return f"choice-{digest[:20]}"


def _clean_text(value: object, *, label: str, limit: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise ChoiceProtocolError(f"{label} must not be empty")
    if len(text) > limit:
        raise ChoiceProtocolError(f"{label} exceeds {limit} characters")
    return text


def _normalize_questions(questions: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(questions, Sequence) or isinstance(questions, (str, bytes)):
        raise ChoiceProtocolError("questions must be a sequence")
    if not 1 <= len(questions) <= 3:
        raise ChoiceProtocolError("a decision must contain 1 to 3 questions")

    normalized: list[dict[str, Any]] = []
    question_ids: set[str] = set()
    for raw_question in questions:
        if not isinstance(raw_question, Mapping):
            raise ChoiceProtocolError("each question must be an object")
        question_id = str(raw_question.get("id") or "").strip()
        if not _ID_RE.fullmatch(question_id):
            raise ChoiceProtocolError(f"invalid question id: {question_id!r}")
        if question_id in question_ids:
            raise ChoiceProtocolError(f"duplicate question id: {question_id}")
        question_ids.add(question_id)

        prompt = _clean_text(raw_question.get("prompt"), label="question prompt", limit=240)
        raw_options = raw_question.get("options")
        if not isinstance(raw_options, Sequence) or isinstance(raw_options, (str, bytes)):
            raise ChoiceProtocolError(f"{question_id}: options must be a sequence")
        if not 2 <= len(raw_options) <= 3:
            raise ChoiceProtocolError(f"{question_id}: expected 2 or 3 options")

        options: list[dict[str, Any]] = []
        option_ids: set[str] = set()
        for index, raw_option in enumerate(raw_options):
            if not isinstance(raw_option, Mapping):
                raise ChoiceProtocolError(f"{question_id}: each option must be an object")
            option_id = str(raw_option.get("id") or "").strip()
            if not _ID_RE.fullmatch(option_id):
                raise ChoiceProtocolError(f"{question_id}: invalid option id {option_id!r}")
            if option_id in option_ids:
                raise ChoiceProtocolError(f"{question_id}: duplicate option id {option_id}")
            option_ids.add(option_id)
            recommended = bool(raw_option.get("recommended", False))
            if recommended != (index == 0):
                raise ChoiceProtocolError(
                    f"{question_id}: only the first option must be recommended"
                )
            options.append(
                {
                    "id": option_id,
                    "label": _clean_text(
                        raw_option.get("label"),
                        label=f"{question_id}.{option_id} label",
                        limit=80,
                    ),
                    "description": _clean_text(
                        raw_option.get("description"),
                        label=f"{question_id}.{option_id} description",
                        limit=240,
                    ),
                    "recommended": recommended,
                }
            )

        selector_owners: dict[str, set[str]] = {}
        for option_index, option in enumerate(options, start=1):
            option_id = str(option["id"])
            selectors = {
                str(option_index),
                _selector_token(option_id),
                _selector_token(option["label"]),
            }
            for selector in selectors:
                selector_owners.setdefault(selector, set()).add(option_id)
        if any(len(owners) != 1 for owners in selector_owners.values()):
            raise ChoiceProtocolError(
                f"{question_id}: option selectors are ambiguous after NFKC/casefold normalization"
            )

        normalized.append(
            {
                "id": question_id,
                "prompt": prompt,
                "options": options,
                "allow_freeform": True,
            }
        )
    return normalized


def _numbered_prompt(questions: list[dict[str, Any]]) -> str:
    lines = ["请先选择；收到明确回答前不会执行写入。"]
    for question_index, question in enumerate(questions, start=1):
        if len(questions) > 1:
            lines.append(f"问题 {question_index}（{question['id']}）：{question['prompt']}")
        else:
            lines.append(str(question["prompt"]))
        for option_index, option in enumerate(question["options"], start=1):
            recommended = "（推荐）" if option["recommended"] else ""
            lines.append(
                f"{option_index}. {option['label']}{recommended} — {option['description']}"
            )
    if len(questions) > 1:
        lines.append("请按问题顺序回复编号，例如：1, 2。也可以自由说明新的选择。")
    else:
        lines.append("请回复选项编号；也可以自由说明新的选择。")
    return "\n".join(lines)


def build_choice_request(
    questions: Sequence[Mapping[str, Any]],
    *,
    transport: str = "structured_choice",
) -> dict[str, Any]:
    """Build a zero-write author-decision request.

    ``structured_choice`` maps to a native client choice control when one is
    available.  ``numbered_fallback`` carries identical choices in plain text.
    Neither representation grants system permissions or authorizes a branch.
    """

    if transport not in TRANSPORTS:
        raise ChoiceProtocolError(f"unsupported choice transport: {transport}")
    normalized = _normalize_questions(questions)
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "request_id": _request_id(normalized),
        "kind": "business_decision",
        "transport": transport,
        "status": "awaiting_user",
        "questions": normalized,
        "authorization": {
            "write_allowed": False,
            "selected_branches": {},
            "native_permission_requested": False,
        },
    }
    if transport == "numbered_fallback":
        payload["prompt"] = _numbered_prompt(normalized)
    return payload


def _answer_mapping(request: Mapping[str, Any], answer: object) -> dict[str, object] | None:
    questions = list(request.get("questions") or [])
    if answer is None:
        return None
    if isinstance(answer, Mapping):
        raw_answers = answer.get("answers", answer)
        if not isinstance(raw_answers, Mapping):
            raise ChoiceProtocolError("structured answers must be an object")
        return {str(key): value for key, value in raw_answers.items()}

    text = str(answer).strip()
    if not text:
        return None
    if len(questions) == 1:
        return {str(questions[0]["id"]): text}

    tokens = [token.strip() for token in re.split(r"[,，;；\n]+", text) if token.strip()]
    if len(tokens) == len(questions) and all("=" not in token and ":" not in token for token in tokens):
        return {
            str(question["id"]): token
            for question, token in zip(questions, tokens, strict=True)
        }

    mapped: dict[str, object] = {}
    for token in tokens:
        match = re.fullmatch(r"([a-z][a-z0-9_-]{0,63})\s*[:=]\s*(.+)", token)
        if not match:
            return {"__freeform__": text}
        mapped[match.group(1)] = match.group(2).strip()
    return mapped


def _select_option(question: Mapping[str, Any], raw_answer: object) -> str | None:
    token = _selector_token(raw_answer)
    if not token:
        return None
    options = list(question.get("options") or [])
    matches: set[str] = set()
    if token.isdecimal():
        number = int(token)
        if 1 <= number <= len(options):
            matches.add(str(options[number - 1]["id"]))
    for option in options:
        option_id = str(option.get("id") or "")
        normalized_id = _selector_token(option_id)
        normalized_label = _selector_token(option.get("label"))
        if token in {normalized_id, normalized_label}:
            matches.add(option_id)
    return next(iter(matches)) if len(matches) == 1 else None


def resolve_choice(request: Mapping[str, Any], answer: object = None) -> dict[str, Any]:
    """Resolve an answer without silently treating the recommendation as consent."""

    if request.get("schema_version") != SCHEMA_VERSION:
        raise ChoiceProtocolError("unsupported or missing choice schema")
    questions = request.get("questions")
    if not isinstance(questions, list):
        raise ChoiceProtocolError("choice request has no questions")
    expected_id = _request_id(_normalize_questions(questions))
    if request.get("request_id") != expected_id:
        raise ChoiceProtocolError("choice request id does not match its questions")

    mapped = _answer_mapping(request, answer)
    base = {
        "schema_version": SCHEMA_VERSION,
        "request_id": expected_id,
        "kind": "business_decision",
        "native_permission_requested": False,
    }
    if mapped is None:
        return {
            **base,
            "status": "awaiting_user",
            "write_allowed": False,
            "selected_branches": {},
            "unresolved_questions": [str(question["id"]) for question in questions],
        }
    if "__freeform__" in mapped:
        return {
            **base,
            "status": "needs_clarification",
            "write_allowed": False,
            "selected_branches": {},
            "freeform_answer": str(mapped["__freeform__"]),
            "unresolved_questions": [str(question["id"]) for question in questions],
        }

    selected: dict[str, str] = {}
    unresolved: list[str] = []
    freeform: dict[str, str] = {}
    known_ids = {str(question["id"]) for question in questions}
    if any(question_id not in known_ids for question_id in mapped):
        raise ChoiceProtocolError("answer contains an unknown question id")

    for question in questions:
        question_id = str(question["id"])
        if question_id not in mapped or not str(mapped[question_id] or "").strip():
            unresolved.append(question_id)
            continue
        option_id = _select_option(question, mapped[question_id])
        if option_id is None:
            unresolved.append(question_id)
            freeform[question_id] = str(mapped[question_id]).strip()
            continue
        selected[question_id] = option_id

    if unresolved:
        return {
            **base,
            "status": "needs_clarification" if freeform else "awaiting_user",
            "write_allowed": False,
            "selected_branches": {},
            "candidate_selections": selected,
            "freeform_answers": freeform,
            "unresolved_questions": unresolved,
        }
    return {
        **base,
        "status": "selected",
        "write_allowed": True,
        "selected_branches": selected,
        "unresolved_questions": [],
    }


def execute_selected_branches(
    request: Mapping[str, Any],
    resolution: Mapping[str, Any],
    branches: Mapping[str, Mapping[str, Callable[[], Any]]],
) -> dict[str, Any]:
    """Execute exactly one selected callback per question.

    This is intentionally strict: pending, free-form, stale, or partially
    answered decisions raise before any callback can run.
    """

    if request.get("request_id") != resolution.get("request_id"):
        raise ChoiceProtocolError("stale choice resolution")
    if resolution.get("status") != "selected" or resolution.get("write_allowed") is not True:
        raise ChoiceProtocolError("user choice has not authorized a branch")
    selected = resolution.get("selected_branches")
    if not isinstance(selected, Mapping):
        raise ChoiceProtocolError("selected branches are missing")

    questions = list(request.get("questions") or [])
    prepared: list[tuple[str, str, Callable[[], Any]]] = []
    for question in questions:
        question_id = str(question.get("id") or "")
        option_id = str(selected.get(question_id) or "")
        callback = branches.get(question_id, {}).get(option_id)
        if callback is None:
            raise ChoiceProtocolError(
                f"no callback registered for selected branch {question_id}.{option_id}"
            )
        prepared.append((question_id, option_id, callback))

    results: dict[str, Any] = {}
    for question_id, option_id, callback in prepared:
        results[question_id] = {
            "option_id": option_id,
            "result": callback(),
        }
    return results


def pending_choice_path(workspace_root: str | Path, request_id: str) -> Path:
    """Return the fixed management path for a pending decision.

    Pending metadata is deliberately kept out of the novel project facts and
    Story System.  A symlinked management directory that escapes the workspace
    is rejected instead of following it.
    """

    if not re.fullmatch(r"choice-[0-9a-f]{20}", str(request_id or "")):
        raise ChoiceProtocolError("invalid choice request id")
    root = Path(workspace_root).resolve()
    if not root.is_dir():
        raise ChoiceProtocolError("workspace root must be an existing directory")
    path = (
        root
        / ".codex"
        / "novel-writer-codex"
        / "pending-decisions"
        / f"{request_id}.json"
    )
    resolved_parent = path.parent.resolve(strict=False)
    try:
        resolved_parent.relative_to(root)
    except ValueError as exc:
        raise ChoiceProtocolError("pending decision path escapes the workspace") from exc
    return path


def persist_pending_choice(
    workspace_root: str | Path,
    request: Mapping[str, Any],
) -> Path:
    """Atomically persist only an unanswered decision envelope.

    This management record is not branch authorization.  It always stores
    ``write_allowed=false`` and cannot contain a selected branch.
    """

    if request.get("schema_version") != SCHEMA_VERSION:
        raise ChoiceProtocolError("unsupported or missing choice schema")
    if request.get("status") != "awaiting_user":
        raise ChoiceProtocolError("only an awaiting decision can be persisted")
    authorization = request.get("authorization")
    if not isinstance(authorization, Mapping) or authorization.get("write_allowed") is not False:
        raise ChoiceProtocolError("pending decision must not authorize writes")
    if authorization.get("selected_branches"):
        raise ChoiceProtocolError("pending decision must not select a branch")

    # Re-validate all content and its deterministic id before touching disk.
    questions = request.get("questions")
    if not isinstance(questions, list):
        raise ChoiceProtocolError("choice request has no questions")
    normalized = _normalize_questions(questions)
    expected_id = _request_id(normalized)
    if request.get("request_id") != expected_id:
        raise ChoiceProtocolError("choice request id does not match its questions")

    path = pending_choice_path(workspace_root, expected_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "request_id": expected_id,
        "kind": "business_decision",
        "status": "awaiting_user",
        "transport": request.get("transport"),
        "questions": normalized,
        "authorization": {
            "write_allowed": False,
            "selected_branches": {},
            "native_permission_requested": False,
        },
    }
    if request.get("transport") == "numbered_fallback":
        payload["prompt"] = _numbered_prompt(normalized)
    atomic_write_json(path, payload, use_lock=True, backup=False)
    return path


def load_pending_choice(
    workspace_root: str | Path,
    request_id: str,
) -> dict[str, Any]:
    """Load and revalidate a persisted pending decision."""

    path = pending_choice_path(workspace_root, request_id)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ChoiceProtocolError("pending decision does not exist") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ChoiceProtocolError(f"pending decision cannot be read: {exc}") from exc
    if not isinstance(payload, dict):
        raise ChoiceProtocolError("pending decision must be a JSON object")
    # resolve_choice performs schema, shape, and deterministic-id validation.
    resolution = resolve_choice(payload, None)
    if resolution.get("status") != "awaiting_user":
        raise ChoiceProtocolError("stored decision is not pending")
    authorization = payload.get("authorization")
    if not isinstance(authorization, dict) or authorization.get("write_allowed") is not False:
        raise ChoiceProtocolError("stored pending decision unexpectedly authorizes writes")
    return payload
