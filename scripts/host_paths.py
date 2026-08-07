#!/usr/bin/env python3
"""Host-specific user paths used by the Codex downstream.

New state belongs under ``WEBNOVEL_HOME``.  Claude locations are exposed only
as compatibility read paths; callers must not write through them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from runtime_compat import normalize_windows_path


ENV_WEBNOVEL_HOME = "WEBNOVEL_HOME"
ENV_CODEX_HOME = "CODEX_HOME"
ENV_WEBNOVEL_CLAUDE_HOME = "WEBNOVEL_CLAUDE_HOME"
ENV_CLAUDE_HOME = "CLAUDE_HOME"


@dataclass(frozen=True)
class ResourceResolution:
    """A resolved read-only reference file and its host provenance."""

    path: Path
    resolved_from: str
    compatibility_mode: str

    def as_dict(self) -> dict[str, str]:
        return {
            "path": str(self.path),
            "resolved_from": self.resolved_from,
            "compatibility_mode": self.compatibility_mode,
        }


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


def resolve_plugin_root(anchor: str | Path | None = None) -> Path:
    """Locate the plugin root by walking upward from a calling file.

    The manifest is the sole root marker.  Environment variables are
    intentionally ignored so a stale host configuration cannot redirect
    package or reference reads.
    """

    start = _expanded_path(anchor or __file__)
    if start.is_file() or (not start.exists() and start.suffix):
        start = start.parent

    for candidate in (start, *start.parents):
        manifest = candidate / ".codex-plugin" / "plugin.json"
        if manifest.is_file():
            return candidate

    raise FileNotFoundError(
        f"Unable to locate .codex-plugin/plugin.json from {start}"
    )


def _safe_reference_path(root: Path, relative_path: str | Path) -> Path:
    relative = Path(relative_path)
    if not relative.parts or relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"Reference path must stay relative to references/: {relative_path}")

    root = root.resolve()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            f"Reference path escapes references/: {relative_path}"
        ) from exc
    return candidate


def resolve_reference_file(
    project_root: str | Path,
    relative_path: str | Path,
    *,
    anchor: str | Path | None = None,
) -> ResourceResolution | None:
    """Resolve one reference using the native-to-legacy read precedence.

    Each file is resolved independently so a project can override only the
    references it owns.  Legacy ``.claude`` content is never created or
    modified by this function.
    """

    project = _expanded_path(project_root)
    candidates = (
        (
            project / ".codex" / "references",
            "codex_project",
            "native",
        ),
        (
            project / ".claude" / "references",
            "legacy_project",
            "legacy_read_only",
        ),
        (
            resolve_plugin_root(anchor or __file__) / "references",
            "bundled",
            "native",
        ),
    )

    for root, resolved_from, compatibility_mode in candidates:
        candidate = _safe_reference_path(root, relative_path)
        if candidate.is_file():
            return ResourceResolution(
                path=candidate,
                resolved_from=resolved_from,
                compatibility_mode=compatibility_mode,
            )
    return None
