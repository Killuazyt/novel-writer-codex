#!/usr/bin/env python3
"""Strict request-file contract for the controlled-write Learn workflow."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any


LEARN_REQUEST_SCHEMA = "webnovel-learn-request/v1"
MAX_REQUEST_BYTES = 64 * 1024
MAX_DESCRIPTION_CHARS = 4096
MAX_CATEGORY_CHARS = 256
PATTERN_TYPES = {"hook", "pacing", "dialogue", "payoff", "emotion", "format", "other"}
IMPORTANCE_VALUES = {"high", "medium", "low"}


class LearnRequestError(ValueError):
    """Raised when a Learn request does not match the frozen schema."""


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _is_linklike(path: Path) -> bool:
    """Recognize POSIX symlinks and Windows junction/reparse points."""

    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if callable(is_junction) and is_junction():
            return True
        attributes = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    except OSError:
        return False


def _reject_linklike_chain(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:-1]:
        current /= part
        if current.exists() or current.is_symlink():
            if _is_linklike(current):
                raise LearnRequestError("input-json path must not traverse a symlink or junction")


def _read_stable_bounded(path: Path) -> bytes:
    """Read one regular file through a stable handle without unbounded allocation."""

    try:
        with path.open("rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise LearnRequestError("input-json must be a regular non-symlink file")
            if before.st_size <= 0 or before.st_size > MAX_REQUEST_BYTES:
                raise LearnRequestError(
                    f"input-json size must be 1..{MAX_REQUEST_BYTES} bytes"
                )
            raw = handle.read(MAX_REQUEST_BYTES + 1)
            after = os.fstat(handle.fileno())
    except OSError as exc:
        raise LearnRequestError(f"input-json cannot be read: {exc}") from exc
    identity_before = (before.st_dev, before.st_ino, before.st_size)
    identity_after = (after.st_dev, after.st_ino, after.st_size)
    if identity_before != identity_after or len(raw) != before.st_size:
        raise LearnRequestError("input-json changed while it was being read")
    return raw


def load_learn_request(request_file: str | Path, *, project_root: str | Path) -> dict[str, Any]:
    """Read one absolute, out-of-project UTF-8 Learn request."""

    raw_path = Path(request_file)
    if not raw_path.is_absolute():
        raise LearnRequestError("input-json must be an absolute path")
    _reject_linklike_chain(raw_path)
    if _is_linklike(raw_path):
        raise LearnRequestError("input-json must be a regular non-symlink file")
    try:
        path = raw_path.resolve(strict=True)
        project = Path(project_root).resolve(strict=True)
    except OSError as exc:
        raise LearnRequestError(f"input-json path is unavailable: {exc}") from exc
    if not path.is_file() or _is_linklike(path):
        raise LearnRequestError("input-json must be a regular non-symlink file")
    if _inside(path, project):
        raise LearnRequestError("input-json must be outside the novel project")
    raw = _read_stable_bounded(path)
    if _is_linklike(raw_path):
        raise LearnRequestError("input-json changed to a symlink or junction while reading")
    try:
        if raw.startswith(b"\xef\xbb\xbf"):
            raise LearnRequestError("input-json must be UTF-8 without BOM")
        payload = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise LearnRequestError("input-json must be valid UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise LearnRequestError(f"input-json must contain one JSON object: {exc}") from exc
    if not isinstance(payload, dict):
        raise LearnRequestError("input-json top level must be a JSON object")
    allowed = {
        "schema_version",
        "pattern_type",
        "description",
        "category",
        "importance",
        "source_chapter",
    }
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise LearnRequestError("input-json contains unknown fields: " + ", ".join(unknown))
    if payload.get("schema_version") != LEARN_REQUEST_SCHEMA:
        raise LearnRequestError("input-json schema_version is invalid")

    pattern_type = payload.get("pattern_type")
    if not isinstance(pattern_type, str) or pattern_type not in PATTERN_TYPES:
        raise LearnRequestError("pattern_type must be one of: " + ", ".join(sorted(PATTERN_TYPES)))
    description = payload.get("description")
    if not isinstance(description, str) or not description.strip():
        raise LearnRequestError("description must be a non-empty string")
    if "\x00" in description or len(description) > MAX_DESCRIPTION_CHARS:
        raise LearnRequestError(
            f"description must not contain NUL or exceed {MAX_DESCRIPTION_CHARS} characters"
        )
    category = payload.get("category", "")
    if not isinstance(category, str) or "\x00" in category or len(category) > MAX_CATEGORY_CHARS:
        raise LearnRequestError(
            f"category must be a string of at most {MAX_CATEGORY_CHARS} characters without NUL"
        )
    importance = payload.get("importance", "medium")
    if not isinstance(importance, str) or importance not in IMPORTANCE_VALUES:
        raise LearnRequestError("importance must be one of: high, medium, low")
    source_chapter = payload.get("source_chapter")
    if source_chapter is not None and (
        isinstance(source_chapter, bool)
        or not isinstance(source_chapter, int)
        or not 1 <= source_chapter <= 2_147_483_647
    ):
        raise LearnRequestError("source_chapter must be null or a positive integer")
    return {
        "schema_version": LEARN_REQUEST_SCHEMA,
        "pattern_type": pattern_type,
        "description": description.strip(),
        "category": category.strip(),
        "importance": importance,
        "source_chapter": source_chapter,
    }
