#!/usr/bin/env python3
"""Host-specific user paths used by the Codex downstream.

New state belongs under ``WEBNOVEL_HOME``.  Claude locations are exposed only
as compatibility read paths; callers must not write through them.
"""

from __future__ import annotations

import os
from pathlib import Path

from runtime_compat import normalize_windows_path


ENV_WEBNOVEL_HOME = "WEBNOVEL_HOME"
ENV_CODEX_HOME = "CODEX_HOME"
ENV_WEBNOVEL_CLAUDE_HOME = "WEBNOVEL_CLAUDE_HOME"
ENV_CLAUDE_HOME = "CLAUDE_HOME"


def _expanded_path(value: str | Path) -> Path:
    path = normalize_windows_path(value).expanduser()
    try:
        return path.resolve()
    except OSError:
        return path


def resolve_codex_home() -> Path:
    """Return ``CODEX_HOME`` or the native ``~/.codex`` default."""
    raw = os.environ.get(ENV_CODEX_HOME)
    if raw:
        return _expanded_path(raw)
    return _expanded_path(Path.home() / ".codex")


def resolve_webnovel_home() -> Path:
    """Return the writable downstream home for webnovel-writer state."""
    raw = os.environ.get(ENV_WEBNOVEL_HOME)
    if raw:
        return _expanded_path(raw)
    return _expanded_path(resolve_codex_home() / "novel-writer-codex")


def resolve_legacy_claude_home() -> Path:
    """Return the read-only Claude compatibility home."""
    raw = os.environ.get(ENV_WEBNOVEL_CLAUDE_HOME) or os.environ.get(ENV_CLAUDE_HOME)
    if raw:
        return _expanded_path(raw)
    return _expanded_path(Path.home() / ".claude")
