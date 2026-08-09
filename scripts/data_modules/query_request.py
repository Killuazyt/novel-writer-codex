#!/usr/bin/env python3
"""Strict request-file contract for shell-neutral read-only queries."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable


QUERY_REQUEST_SCHEMA = "webnovel-query-request/v1"
MAX_REQUEST_BYTES = 64 * 1024
MAX_TEXT_CHARS = 4096

_FIELDS: dict[str, dict[str, tuple[type, bool]]] = {
    "entity_state": {"entity": (str, True), "at_chapter": (int, True)},
    "relationships": {"entity": (str, True), "at_chapter": (int, True)},
    "world_rules": {"domain": (str, False)},
    "open_loops": {"status": (str, False)},
    "comprehensive_context": {"chapter": (int, True)},
    "chapter_summary": {"chapter": (int, True)},
}


class QueryRequestError(ValueError):
    """Raised when a query request does not match the frozen schema."""


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _validate_value(field: str, value: Any, expected: type) -> Any:
    if expected is int:
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 2_147_483_647:
            raise QueryRequestError(f"{field} must be a positive integer")
        return value
    if not isinstance(value, str):
        raise QueryRequestError(f"{field} must be a string")
    if "\x00" in value:
        raise QueryRequestError(f"{field} must not contain NUL")
    if len(value) > MAX_TEXT_CHARS:
        raise QueryRequestError(f"{field} exceeds {MAX_TEXT_CHARS} characters")
    return value


def load_query_request(
    request_file: str | Path,
    *,
    project_root: str | Path,
    expected_query_types: Iterable[str],
) -> dict[str, Any]:
    """Read one absolute, out-of-project UTF-8 JSON query request."""

    raw_path = Path(request_file)
    if not raw_path.is_absolute():
        raise QueryRequestError("request-file must be an absolute path")
    if raw_path.is_symlink():
        raise QueryRequestError("request-file must be a regular non-symlink file")
    try:
        path = raw_path.resolve(strict=True)
        project = Path(project_root).resolve(strict=True)
    except OSError as exc:
        raise QueryRequestError(f"request-file path is unavailable: {exc}") from exc
    if not path.is_file() or path.is_symlink():
        raise QueryRequestError("request-file must be a regular non-symlink file")
    if _inside(path, project):
        raise QueryRequestError("request-file must be outside the novel project")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise QueryRequestError(f"request-file cannot be inspected: {exc}") from exc
    if size <= 0 or size > MAX_REQUEST_BYTES:
        raise QueryRequestError(f"request-file size must be 1..{MAX_REQUEST_BYTES} bytes")
    try:
        raw = path.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            raise QueryRequestError("request-file must be UTF-8 without BOM")
        payload = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise QueryRequestError("request-file must be valid UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise QueryRequestError(f"request-file must contain one JSON object: {exc}") from exc
    if not isinstance(payload, dict):
        raise QueryRequestError("request-file top level must be a JSON object")
    if payload.get("schema_version") != QUERY_REQUEST_SCHEMA:
        raise QueryRequestError("request-file schema_version is invalid")
    query_type = payload.get("query_type")
    expected = {str(item) for item in expected_query_types}
    if not isinstance(query_type, str) or query_type not in expected:
        raise QueryRequestError(
            "request-file query_type must be one of: " + ", ".join(sorted(expected))
        )
    contract = _FIELDS.get(query_type)
    if contract is None:
        raise QueryRequestError(f"unsupported query_type: {query_type}")
    allowed = {"schema_version", "query_type", *contract}
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise QueryRequestError("request-file contains unknown fields: " + ", ".join(unknown))
    normalized: dict[str, Any] = {
        "schema_version": QUERY_REQUEST_SCHEMA,
        "query_type": query_type,
    }
    for field, (field_type, required) in contract.items():
        if field not in payload:
            if required:
                raise QueryRequestError(f"request-file is missing required field: {field}")
            continue
        normalized[field] = _validate_value(field, payload[field], field_type)
    return normalized
