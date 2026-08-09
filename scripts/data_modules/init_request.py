#!/usr/bin/env python3
"""Strict request-file contract for confirmed webnovel initialization."""

from __future__ import annotations

import json
import hashlib
import os
import re
import stat
import unicodedata
from pathlib import Path
from typing import Any, Iterable

from host_paths import resolve_plugin_root, resolve_webnovel_home
from .codex_interaction import ChoiceProtocolError, build_choice_request, resolve_choice


INIT_REQUEST_SCHEMA = "webnovel-init-request/v1"
REFERENCE_CONFIRMATION_SCHEMA = "webnovel-init-reference-confirmation/v1"
REFERENCE_BINDING_SCHEMA = "WEBNOVEL_INIT_REFERENCE_BINDING/v1"
REFERENCE_CHOICE_MARKER_SCHEMA = "WEBNOVEL_INIT_REFERENCE_CHOICE/v1"
MAX_REQUEST_BYTES = 256 * 1024
MAX_TEXT_CHARS = 4096
MAX_SHORT_TEXT_CHARS = 256
MAX_LIST_ITEMS = 64

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_SLUG_ILLEGAL_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


class InitRequestError(ValueError):
    """Raised when an Init request does not match the frozen contract."""


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left)) == os.path.normcase(str(right))


def _is_linklike(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if is_junction and is_junction():
            return True
        attributes = getattr(os.lstat(path), "st_file_attributes", 0)
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    except FileNotFoundError:
        return False
    except OSError:
        return True


def _lexists(path: Path) -> bool:
    """Return true for normal entries and dangling links/reparse points."""

    return os.path.lexists(str(path))


def _canonical_sha256(payload: object) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def build_reference_adoption_confirmation(
    *,
    project_root: str,
    selected_idea: dict[str, Any],
    reference_candidate: dict[str, Any],
) -> dict[str, Any]:
    """Build the deterministic, project-scoped ``Adopt`` confirmation.

    This is a scope binding, not proof by itself that a user answered.  The
    caller must still obtain the explicit author choice.  The runtime combines
    it with verified Codex rollout identity and output-byte binding.
    """

    runtime = reference_candidate.get("runtime")
    if not isinstance(runtime, dict):
        runtime = {}
    choice_scope = {
        "project_root": str(project_root),
        "selected_idea": dict(selected_idea),
        "candidate_id": reference_candidate.get("candidate_id"),
        "source_path": reference_candidate.get("source_path"),
        "source_sha256": reference_candidate.get("source_sha256"),
        "output_sha256": reference_candidate.get("output_sha256"),
        "route_sha256": reference_candidate.get("route_sha256"),
        "contract_hash": reference_candidate.get("contract_hash"),
        "child_thread_id": runtime.get("child_thread_id"),
        "parent_thread_id": runtime.get("parent_thread_id"),
        "parent_identity_sha256": runtime.get("parent_identity_sha256"),
        "rollout_sha256": runtime.get("rollout_sha256"),
        "binding_marker_sha256": hashlib.sha256(
            str(reference_candidate.get("binding_marker") or "").encode("utf-8")
        ).hexdigest(),
    }
    choice_scope_sha256 = _canonical_sha256(choice_scope)
    choice_request = build_choice_request(
        [
            {
                "id": "reference_action",
                "prompt": (
                    "是否采用已验证的参考拆解候选 "
                    f"{reference_candidate.get('candidate_id') or 'unknown'} "
                    f"（范围 {choice_scope_sha256[:12]}）？"
                ),
                "options": [
                    {
                        "id": "adopt",
                        "label": "Adopt",
                        "description": "采用该候选并继续初始化。",
                        "recommended": True,
                    },
                    {
                        "id": "discard",
                        "label": "Discard",
                        "description": "丢弃该候选并改用原创输入。",
                        "recommended": False,
                    },
                    {
                        "id": "cancel",
                        "label": "Cancel",
                        "description": "停止本次初始化，不写入项目。",
                        "recommended": False,
                    },
                ],
            }
        ]
    )
    choice_marker = REFERENCE_CHOICE_MARKER_SCHEMA + " " + json.dumps(
        {
            "choice_request_sha256": _canonical_sha256(choice_request),
            "choice_scope_sha256": choice_scope_sha256,
            "child_rollout_sha256": runtime.get("rollout_sha256"),
            "parent_identity_sha256": runtime.get("parent_identity_sha256"),
            "request_id": choice_request["request_id"],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    scope = {
        **choice_scope,
        "choice_scope_sha256": choice_scope_sha256,
        "choice_request_sha256": _canonical_sha256(choice_request),
        "choice_marker_sha256": hashlib.sha256(choice_marker.encode("utf-8")).hexdigest(),
        "parent_rollout_path": runtime.get("parent_rollout_path"),
        "parent_rollout_sha256": runtime.get("parent_rollout_sha256"),
    }
    scope_sha256 = _canonical_sha256(scope)
    return {
        "schema_version": REFERENCE_CONFIRMATION_SCHEMA,
        "request_id": choice_request["request_id"],
        "scope_sha256": scope_sha256,
        "choice_scope_sha256": choice_scope_sha256,
        "choice_request": choice_request,
        "choice_marker": choice_marker,
    }


def build_reference_binding_marker(reference_candidate: dict[str, Any]) -> str:
    """Build the exact marker that must occur in the trusted Agent prompt."""

    runtime = reference_candidate.get("runtime")
    if not isinstance(runtime, dict):
        runtime = {}
    payload = {
        "source_path": reference_candidate.get("source_path"),
        "source_sha256": reference_candidate.get("source_sha256"),
        "route_sha256": reference_candidate.get("route_sha256"),
        "contract_hash": reference_candidate.get("contract_hash"),
        "parent_thread_id": runtime.get("parent_thread_id"),
        "parent_identity_sha256": runtime.get("parent_identity_sha256"),
    }
    return (
        REFERENCE_BINDING_SCHEMA
        + " "
        + json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )


def _object(
    parent: dict[str, Any],
    name: str,
    *,
    allowed: Iterable[str],
    required: bool = True,
) -> dict[str, Any]:
    value = parent.get(name)
    if value is None and not required:
        return {}
    if not isinstance(value, dict):
        raise InitRequestError(f"{name} must be a JSON object")
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise InitRequestError(f"{name} contains unknown fields: " + ", ".join(unknown))
    return value


def _text(
    parent: dict[str, Any],
    name: str,
    *,
    required: bool = False,
    max_chars: int = MAX_TEXT_CHARS,
) -> str:
    value = parent.get(name, "")
    if not isinstance(value, str):
        raise InitRequestError(f"{name} must be a string")
    value = value.strip()
    if required and not value:
        raise InitRequestError(f"{name} must be a non-empty string")
    if "\x00" in value or len(value) > max_chars:
        raise InitRequestError(f"{name} must not contain NUL or exceed {max_chars} characters")
    return value


def _positive_or_zero(parent: dict[str, Any], name: str) -> int:
    value = parent.get(name, 0)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2_147_483_647:
        raise InitRequestError(f"{name} must be a non-negative integer")
    return value


def _string_list(
    parent: dict[str, Any],
    name: str,
    *,
    max_items: int = MAX_LIST_ITEMS,
    item_chars: int = MAX_SHORT_TEXT_CHARS,
) -> list[str]:
    value = parent.get(name, [])
    if not isinstance(value, list) or len(value) > max_items:
        raise InitRequestError(f"{name} must be a JSON array with at most {max_items} items")
    normalized: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise InitRequestError(f"{name}[{index}] must be a string")
        item = item.strip()
        if not item or "\x00" in item or len(item) > item_chars:
            raise InitRequestError(
                f"{name}[{index}] must be non-empty, contain no NUL, and not exceed {item_chars} characters"
            )
        if item not in normalized:
            normalized.append(item)
    return normalized


def _string_map(parent: dict[str, Any], name: str) -> dict[str, str]:
    value = parent.get(name, {})
    if not isinstance(value, dict) or len(value) > MAX_LIST_ITEMS:
        raise InitRequestError(f"{name} must be a JSON object with string values")
    normalized: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise InitRequestError(f"{name} must contain only string keys and values")
        key = key.strip()
        item = item.strip()
        if (
            not key
            or "\x00" in key
            or "\x00" in item
            or len(key) > MAX_SHORT_TEXT_CHARS
            or len(item) > MAX_SHORT_TEXT_CHARS
        ):
            raise InitRequestError(f"{name} contains an invalid key or value")
        normalized[key] = item
    return normalized


def _validate_slug(value: Any) -> str:
    if not isinstance(value, str):
        raise InitRequestError("project_slug must be a string")
    slug = value.strip()
    if not slug or slug in {".", ".."}:
        raise InitRequestError("project_slug must not be empty or a dot directory")
    if slug != value or unicodedata.normalize("NFKC", slug) != slug:
        raise InitRequestError("project_slug must already be trimmed and NFKC-normalized")
    if slug.startswith(".") or slug.endswith((".", " ")) or _SLUG_ILLEGAL_RE.search(slug):
        raise InitRequestError("project_slug contains a forbidden path or filename form")
    if Path(slug).name != slug or len(Path(slug).parts) != 1:
        raise InitRequestError("project_slug must be exactly one path component")
    if slug.split(".", 1)[0].upper() in _WINDOWS_RESERVED:
        raise InitRequestError("project_slug is a reserved Windows filename")
    if slug.casefold() in {".codex", ".claude", ".codex-plugin"}:
        raise InitRequestError("project_slug must not name a host or plugin directory")
    if len(slug) > 120:
        raise InitRequestError("project_slug must not exceed 120 characters")
    return slug


def _validate_workspace(value: Any, slug: str) -> tuple[str, str]:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise InitRequestError("workspace_root must be a non-empty absolute path string")
    raw = Path(value)
    if not raw.is_absolute():
        raise InitRequestError("workspace_root must be an absolute path")
    if _is_linklike(raw):
        raise InitRequestError("workspace_root must not be a symlink or junction")
    try:
        workspace = raw.resolve(strict=True)
    except OSError as exc:
        raise InitRequestError(f"workspace_root is unavailable: {exc}") from exc
    if not workspace.is_dir():
        raise InitRequestError("workspace_root must be an existing directory")
    lexical = Path(os.path.abspath(str(raw)))
    if not _same_path(lexical, workspace):
        raise InitRequestError("workspace_root must not traverse a symlink, junction, or '..'")
    if any(part.casefold() in {".codex", ".claude"} for part in workspace.parts):
        raise InitRequestError("workspace_root must not be inside a Codex or Claude host directory")

    target = workspace / slug
    if _is_linklike(target):
        raise InitRequestError("resolved project root must not be a symlink or junction")
    if _lexists(target) and not target.exists():
        raise InitRequestError("resolved project root must not be a dangling filesystem entry")
    resolved_target = target.resolve(strict=False)
    if resolved_target.parent != workspace:
        raise InitRequestError("resolved project root escapes workspace_root")

    try:
        plugin_root = resolve_plugin_root(__file__).resolve()
    except FileNotFoundError as exc:
        raise InitRequestError(f"plugin root cannot be resolved: {exc}") from exc
    test_session_raw = os.environ.get("WEBNOVEL_TEST_SESSION_ROOT", "")
    test_session = Path(test_session_raw).resolve() if test_session_raw else None
    isolated_test_workspace = bool(
        os.environ.get("WEBNOVEL_TEST_ISOLATION") == "1"
        and test_session is not None
        and _inside(workspace, test_session)
    )
    if (workspace == plugin_root or _inside(workspace, plugin_root)) and not isolated_test_workspace:
        raise InitRequestError("workspace_root must not be the plugin directory or its descendant")
    if (
        resolved_target == plugin_root or _inside(resolved_target, plugin_root)
    ) and not isolated_test_workspace:
        raise InitRequestError("resolved project root must not be inside the plugin directory")
    return str(workspace), str(resolved_target)


def _normalize_reference(payload: dict[str, Any]) -> dict[str, Any] | None:
    if not payload:
        return None
    allowed = {
        "status",
        "candidate_id",
        "source_title",
        "source_path",
        "source_sha256",
        "output_sha256",
        "confidence",
        "transformation_notes",
        "do_not_copy",
        "canon_contamination_warnings",
        "route_sha256",
        "contract_hash",
        "binding_marker",
        "deconstruction_output",
        "runtime",
        "user_confirmation",
    }
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise InitRequestError("reference_candidate contains unknown fields: " + ", ".join(unknown))
    status = payload.get("status")
    if status not in {"proposed", "discarded", "adopted"}:
        raise InitRequestError("reference_candidate.status must be proposed, discarded, or adopted")
    confidence = payload.get("confidence", 0.0)
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise InitRequestError("reference_candidate.confidence must be a number")
    confidence = float(confidence)
    if not 0.0 <= confidence <= 1.0:
        raise InitRequestError("reference_candidate.confidence must be between 0 and 1")
    normalized = {
        "status": status,
        "candidate_id": _text(payload, "candidate_id", max_chars=MAX_SHORT_TEXT_CHARS),
        "source_title": _text(payload, "source_title", max_chars=MAX_SHORT_TEXT_CHARS),
        "source_path": _text(payload, "source_path"),
        "source_sha256": _text(payload, "source_sha256", max_chars=64),
        "output_sha256": _text(payload, "output_sha256", max_chars=64),
        "confidence": confidence,
        "transformation_notes": _text(payload, "transformation_notes"),
        "do_not_copy": _string_list(payload, "do_not_copy"),
        "canon_contamination_warnings": _string_list(
            payload, "canon_contamination_warnings"
        ),
        "route_sha256": _text(payload, "route_sha256", max_chars=64),
        "contract_hash": _text(payload, "contract_hash", max_chars=64),
        "binding_marker": _text(payload, "binding_marker", max_chars=2048),
    }
    for field in ("source_sha256", "output_sha256", "route_sha256", "contract_hash"):
        value = normalized[field]
        if value and not _SHA256_RE.fullmatch(value):
            raise InitRequestError(f"reference_candidate.{field} must be a lowercase SHA-256")

    output = payload.get("deconstruction_output")
    if output is not None and not isinstance(output, dict):
        raise InitRequestError("reference_candidate.deconstruction_output must be a JSON object")
    normalized["deconstruction_output"] = dict(output) if isinstance(output, dict) else None

    runtime = payload.get("runtime")
    if runtime is not None and not isinstance(runtime, dict):
        raise InitRequestError("reference_candidate.runtime must be a JSON object")
    normalized_runtime: dict[str, str] | None = None
    if isinstance(runtime, dict):
        allowed_runtime = {
            "rollout_path",
            "sessions_root",
            "child_thread_id",
            "parent_thread_id",
            "parent_model",
            "parent_reasoning_effort",
            "parent_identity_sha256",
            "parent_rollout_path",
            "parent_rollout_sha256",
            "rollout_sha256",
        }
        unknown_runtime = sorted(set(runtime) - allowed_runtime)
        if unknown_runtime:
            raise InitRequestError(
                "reference_candidate.runtime contains unknown fields: "
                + ", ".join(unknown_runtime)
            )
        normalized_runtime = {
            field: _text(runtime, field, required=True)
            for field in allowed_runtime
        }
        for field in ("rollout_path", "parent_rollout_path", "sessions_root"):
            if not Path(normalized_runtime[field]).is_absolute():
                raise InitRequestError(f"reference_candidate.runtime.{field} must be absolute")
        for field in (
            "rollout_sha256",
            "parent_identity_sha256",
            "parent_rollout_sha256",
        ):
            if not _SHA256_RE.fullmatch(normalized_runtime[field]):
                raise InitRequestError(
                    f"reference_candidate.runtime.{field} must be a lowercase SHA-256"
                )
    normalized["runtime"] = normalized_runtime

    confirmation = payload.get("user_confirmation")
    if confirmation is not None and not isinstance(confirmation, dict):
        raise InitRequestError("reference_candidate.user_confirmation must be a JSON object")
    normalized_confirmation: dict[str, Any] | None = None
    if isinstance(confirmation, dict):
        expected_fields = {
            "schema_version",
            "request_id",
            "scope_sha256",
            "choice_scope_sha256",
            "choice_request",
            "choice_marker",
        }
        if set(confirmation) != expected_fields:
            raise InitRequestError(
                "reference_candidate.user_confirmation must contain exactly "
                "schema_version, request_id, scope_sha256, choice_scope_sha256, "
                "choice_request, and choice_marker"
            )
        normalized_confirmation = {
            field: _text(confirmation, field, required=True, max_chars=4096)
            for field in expected_fields - {"choice_request"}
        }
        choice_request = confirmation.get("choice_request")
        if not isinstance(choice_request, dict):
            raise InitRequestError(
                "reference_candidate.user_confirmation.choice_request must be an object"
            )
        try:
            pending = resolve_choice(choice_request, None)
        except ChoiceProtocolError as exc:
            raise InitRequestError(
                f"reference_candidate.user_confirmation choice request is invalid: {exc}"
            ) from exc
        if (
            pending.get("status") != "awaiting_user"
            or pending.get("write_allowed") is not False
            or [question.get("id") for question in choice_request.get("questions", [])]
            != ["reference_action"]
        ):
            raise InitRequestError(
                "reference_candidate.user_confirmation choice request is not pending reference_action"
            )
        normalized_confirmation["choice_request"] = json.loads(
            json.dumps(choice_request, ensure_ascii=False)
        )
        if normalized_confirmation["schema_version"] != REFERENCE_CONFIRMATION_SCHEMA:
            raise InitRequestError("reference_candidate.user_confirmation schema is invalid")
        if normalized_confirmation["request_id"] != choice_request.get("request_id"):
            raise InitRequestError("reference_candidate.user_confirmation request_id is invalid")
        for field in ("scope_sha256", "choice_scope_sha256"):
            if not _SHA256_RE.fullmatch(normalized_confirmation[field]):
                raise InitRequestError(
                    f"reference_candidate.user_confirmation {field} is invalid"
                )
        if not normalized_confirmation["choice_marker"].startswith(
            REFERENCE_CHOICE_MARKER_SCHEMA + " "
        ):
            raise InitRequestError(
                "reference_candidate.user_confirmation choice_marker is invalid"
            )
    normalized["user_confirmation"] = normalized_confirmation

    if status == "adopted":
        required_text = (
            "candidate_id",
            "source_title",
            "source_path",
            "source_sha256",
            "output_sha256",
            "transformation_notes",
            "route_sha256",
            "contract_hash",
            "binding_marker",
        )
        if any(not normalized[field] for field in required_text):
            raise InitRequestError("an adopted reference candidate is missing provenance or transformation data")
        if normalized["confidence"] < 0.85:
            raise InitRequestError(
                "an adopted reference candidate requires confidence >= 0.85"
            )
        if normalized["deconstruction_output"] is None or normalized_runtime is None:
            raise InitRequestError(
                "an adopted reference candidate requires explicit deconstruction output and Codex rollout evidence"
            )
        if normalized_confirmation is None:
            raise InitRequestError(
                "an adopted reference candidate requires a project-scoped user confirmation"
            )
    return normalized


def _normalize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    allowed_top = {
        "schema_version",
        "workspace_root",
        "project_slug",
        "project",
        "protagonist",
        "relationship",
        "golden_finger",
        "world",
        "constraints",
        "reference_candidate",
    }
    unknown = sorted(set(payload) - allowed_top)
    if unknown:
        raise InitRequestError("input-json contains unknown fields: " + ", ".join(unknown))
    if payload.get("schema_version") != INIT_REQUEST_SCHEMA:
        raise InitRequestError("input-json schema_version is invalid")

    slug = _validate_slug(payload.get("project_slug"))
    workspace_root, project_root = _validate_workspace(payload.get("workspace_root"), slug)

    project = _object(
        payload,
        "project",
        allowed={
            "title",
            "genre",
            "target_words",
            "target_chapters",
            "one_liner",
            "core_conflict",
            "target_reader",
            "platform",
        },
    )
    target_words = _positive_or_zero(project, "target_words")
    target_chapters = _positive_or_zero(project, "target_chapters")
    if target_words == 0 and target_chapters == 0:
        raise InitRequestError("at least one of target_words or target_chapters must be positive")
    if target_words == 0:
        target_words = target_chapters * 3000
    if target_chapters == 0:
        target_chapters = max(1, (target_words + 2999) // 3000)
    normalized_project = {
        "title": _text(project, "title", required=True, max_chars=MAX_SHORT_TEXT_CHARS),
        "genre": _text(project, "genre", required=True, max_chars=MAX_SHORT_TEXT_CHARS),
        "target_words": target_words,
        "target_chapters": target_chapters,
        "one_liner": _text(project, "one_liner", required=True),
        "core_conflict": _text(project, "core_conflict", required=True),
        "target_reader": _text(project, "target_reader", max_chars=MAX_SHORT_TEXT_CHARS),
        "platform": _text(project, "platform", max_chars=MAX_SHORT_TEXT_CHARS),
    }

    protagonist = _object(
        payload,
        "protagonist",
        allowed={"name", "desire", "flaw", "archetype", "structure"},
    )
    normalized_protagonist = {
        "name": _text(protagonist, "name", required=True, max_chars=MAX_SHORT_TEXT_CHARS),
        "desire": _text(protagonist, "desire", required=True),
        "flaw": _text(protagonist, "flaw", required=True),
        "archetype": _text(protagonist, "archetype", max_chars=MAX_SHORT_TEXT_CHARS),
        "structure": _text(protagonist, "structure", max_chars=MAX_SHORT_TEXT_CHARS)
        or "单主角",
    }

    relationship = _object(
        payload,
        "relationship",
        allowed={
            "heroine_config",
            "heroine_names",
            "heroine_role",
            "co_protagonists",
            "co_protagonist_roles",
            "antagonist_tiers",
            "antagonist_level",
            "antagonist_mirror",
        },
    )
    normalized_relationship = {
        "heroine_config": _text(relationship, "heroine_config", max_chars=MAX_SHORT_TEXT_CHARS),
        "heroine_names": _string_list(relationship, "heroine_names"),
        "heroine_role": _text(relationship, "heroine_role", max_chars=MAX_SHORT_TEXT_CHARS),
        "co_protagonists": _string_list(relationship, "co_protagonists"),
        "co_protagonist_roles": _string_list(relationship, "co_protagonist_roles"),
        "antagonist_tiers": _string_map(relationship, "antagonist_tiers"),
        "antagonist_level": _text(relationship, "antagonist_level", max_chars=MAX_SHORT_TEXT_CHARS),
        "antagonist_mirror": _text(relationship, "antagonist_mirror"),
    }

    golden_finger = _object(
        payload,
        "golden_finger",
        allowed={"type", "name", "style", "visibility", "irreversible_cost", "growth_rhythm"},
    )
    normalized_golden_finger = {
        "type": _text(golden_finger, "type", required=True, max_chars=MAX_SHORT_TEXT_CHARS),
        "name": _text(golden_finger, "name", max_chars=MAX_SHORT_TEXT_CHARS),
        "style": _text(golden_finger, "style", max_chars=MAX_SHORT_TEXT_CHARS),
        "visibility": _text(golden_finger, "visibility", max_chars=MAX_SHORT_TEXT_CHARS),
        "irreversible_cost": _text(golden_finger, "irreversible_cost", required=True),
        "growth_rhythm": _text(golden_finger, "growth_rhythm", max_chars=MAX_SHORT_TEXT_CHARS),
    }

    world = _object(
        payload,
        "world",
        allowed={
            "scale",
            "factions",
            "power_system_type",
            "social_class",
            "resource_distribution",
            "currency_system",
            "currency_exchange",
            "sect_hierarchy",
            "cultivation_chain",
            "cultivation_subtiers",
        },
    )
    normalized_world = {
        "scale": _text(world, "scale", required=True, max_chars=MAX_SHORT_TEXT_CHARS),
        "factions": _text(world, "factions", required=True),
        "power_system_type": _text(
            world, "power_system_type", required=True, max_chars=MAX_SHORT_TEXT_CHARS
        ),
        "social_class": _text(world, "social_class", required=True),
        "resource_distribution": _text(world, "resource_distribution", required=True),
        "currency_system": _text(world, "currency_system", max_chars=MAX_SHORT_TEXT_CHARS),
        "currency_exchange": _text(world, "currency_exchange", max_chars=MAX_SHORT_TEXT_CHARS),
        "sect_hierarchy": _text(world, "sect_hierarchy"),
        "cultivation_chain": _text(world, "cultivation_chain"),
        "cultivation_subtiers": _text(world, "cultivation_subtiers"),
    }

    constraints = _object(
        payload,
        "constraints",
        allowed={"selected_idea", "core_selling_points", "creativity_refusal_reason"},
    )
    selected = _object(
        constraints,
        "selected_idea",
        allowed={
            "title",
            "one_liner",
            "anti_trope",
            "hard_constraints",
            "protagonist_flaw",
            "antagonist_mirror",
            "opening_hook",
            "origin",
        },
    )
    hard_constraints = _string_list(selected, "hard_constraints")
    anti_trope = _text(selected, "anti_trope")
    refusal = _text(constraints, "creativity_refusal_reason")
    if (not anti_trope or len(hard_constraints) < 2) and not refusal:
        raise InitRequestError(
            "selected_idea requires one anti_trope and at least two hard_constraints, or an explicit creativity_refusal_reason"
        )
    origin = _text(selected, "origin", max_chars=MAX_SHORT_TEXT_CHARS) or "original"
    if origin not in {"original", "reference_adopted", "mixed"}:
        raise InitRequestError("selected_idea.origin must be original, reference_adopted, or mixed")
    normalized_selected = {
        "title": _text(selected, "title", required=True, max_chars=MAX_SHORT_TEXT_CHARS),
        "one_liner": _text(selected, "one_liner", required=True),
        "anti_trope": anti_trope,
        "hard_constraints": hard_constraints,
        "protagonist_flaw": _text(selected, "protagonist_flaw", required=True),
        "antagonist_mirror": _text(selected, "antagonist_mirror"),
        "opening_hook": _text(selected, "opening_hook"),
        "origin": origin,
    }
    if normalized_selected["one_liner"] != normalized_project["one_liner"]:
        raise InitRequestError("selected_idea.one_liner must equal project.one_liner")
    if normalized_selected["protagonist_flaw"] != normalized_protagonist["flaw"]:
        raise InitRequestError("selected_idea.protagonist_flaw must equal protagonist.flaw")
    if (
        normalized_selected["antagonist_mirror"]
        and normalized_relationship["antagonist_mirror"]
        and normalized_selected["antagonist_mirror"]
        != normalized_relationship["antagonist_mirror"]
    ):
        raise InitRequestError(
            "selected_idea.antagonist_mirror must equal relationship.antagonist_mirror"
        )

    reference_payload = payload.get("reference_candidate")
    if reference_payload is not None and not isinstance(reference_payload, dict):
        raise InitRequestError("reference_candidate must be a JSON object")
    # _normalize_reference owns the nested field contract.
    reference = _normalize_reference(reference_payload or {})
    if origin in {"reference_adopted", "mixed"} and (
        reference is None or reference.get("status") != "adopted"
    ):
        raise InitRequestError(
            "a reference-derived selected idea requires an adopted reference candidate"
        )
    if reference is not None and reference.get("status") == "adopted":
        if reference.get("binding_marker") != build_reference_binding_marker(reference):
            raise InitRequestError(
                "reference_candidate.binding_marker is stale or scoped to different source/route inputs"
            )
        expected_confirmation = build_reference_adoption_confirmation(
            project_root=project_root,
            selected_idea=normalized_selected,
            reference_candidate=reference,
        )
        if reference.get("user_confirmation") != expected_confirmation:
            raise InitRequestError(
                "reference_candidate.user_confirmation is stale or scoped to different inputs"
            )

    return {
        "schema_version": INIT_REQUEST_SCHEMA,
        "workspace_root": workspace_root,
        "project_root": project_root,
        "project_slug": slug,
        "project": normalized_project,
        "protagonist": normalized_protagonist,
        "relationship": normalized_relationship,
        "golden_finger": normalized_golden_finger,
        "world": normalized_world,
        "constraints": {
            "selected_idea": normalized_selected,
            "core_selling_points": _string_list(constraints, "core_selling_points"),
            "creativity_refusal_reason": refusal,
        },
        "reference_candidate": reference,
    }


def load_init_request(request_file: str | Path) -> dict[str, Any]:
    """Read one UTF-8 request strictly below ``WEBNOVEL_HOME/tmp/init``."""

    raw_path = Path(request_file)
    if not raw_path.is_absolute():
        raise InitRequestError("config-json must be an absolute path")
    if _is_linklike(raw_path):
        raise InitRequestError("config-json must be a regular non-symlink file")
    configured_temp_root = resolve_webnovel_home() / "tmp" / "init"
    expected_temp_root = configured_temp_root.resolve(strict=False)
    lexical_path = Path(os.path.abspath(str(raw_path)))
    resolved_path = raw_path.resolve(strict=False)
    if not _same_path(lexical_path, resolved_path):
        raise InitRequestError("config-json path must not traverse a symlink, junction, or '..'")
    configured_absolute = Path(os.path.abspath(str(configured_temp_root)))
    if not _same_path(configured_absolute, expected_temp_root) or _is_linklike(configured_temp_root):
        raise InitRequestError("WEBNOVEL_HOME/tmp/init must not traverse a symlink or junction")
    if not _inside(resolved_path, expected_temp_root):
        raise InitRequestError("config-json must stay under WEBNOVEL_HOME/tmp/init")
    try:
        path = raw_path.resolve(strict=True)
        temp_root = expected_temp_root.resolve(strict=True)
    except OSError as exc:
        raise InitRequestError(f"config-json path is unavailable: {exc}") from exc
    if not path.is_file() or _is_linklike(path):
        raise InitRequestError("config-json must be a regular non-symlink file")
    if not _inside(path, temp_root):
        raise InitRequestError("config-json must stay under WEBNOVEL_HOME/tmp/init")
    current = path.parent
    while current != temp_root:
        if _is_linklike(current):
            raise InitRequestError("config-json path must not traverse a symlink or junction")
        if current.parent == current:
            raise InitRequestError("config-json path escaped WEBNOVEL_HOME/tmp/init")
        current = current.parent
    if _is_linklike(temp_root):
        raise InitRequestError("WEBNOVEL_HOME/tmp/init must not be a symlink or junction")
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_size <= 0 or before.st_size > MAX_REQUEST_BYTES:
                raise InitRequestError(
                    f"config-json size must be 1..{MAX_REQUEST_BYTES} bytes and the entry must be regular"
                )
            chunks: list[bytes] = []
            remaining = MAX_REQUEST_BYTES + 1
            while remaining > 0:
                chunk = os.read(fd, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            after = os.fstat(fd)
        finally:
            os.close(fd)
        current = os.stat(path, follow_symlinks=False)
        stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
        if (
            len(raw) > MAX_REQUEST_BYTES
            or any(getattr(before, field, None) != getattr(after, field, None) for field in stable_fields)
            or any(getattr(after, field, None) != getattr(current, field, None) for field in stable_fields)
            or _is_linklike(path)
        ):
            raise InitRequestError("config-json changed while it was being read")
        if raw.startswith(b"\xef\xbb\xbf"):
            raise InitRequestError("config-json must be UTF-8 without BOM")
        payload = json.loads(raw.decode("utf-8"))
    except InitRequestError:
        raise
    except OSError as exc:
        raise InitRequestError(f"config-json cannot be read stably: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise InitRequestError("config-json must be valid UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise InitRequestError(f"config-json must contain one JSON object: {exc}") from exc
    if not isinstance(payload, dict):
        raise InitRequestError("config-json top level must be a JSON object")
    return _normalize_payload(payload)
