#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


MAX_LINES = 8
MAX_CHARS = 1000
DISABLE_ENV = "WEBNOVEL_DISABLE_SESSION_STATUS_HOOK"
PROJECT_MARKERS = (".story-system", ".webnovel")


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _clip(text: str) -> str:
    lines = [line for line in text.splitlines() if line.strip()][:MAX_LINES]
    clipped = "\n".join(lines).strip()
    if len(clipped) > MAX_CHARS:
        clipped = clipped[: MAX_CHARS - 3].rstrip() + "..."
    return clipped


def _find_project_root(start: str | os.PathLike[str]) -> Path | None:
    try:
        start_path = Path(start).expanduser().resolve()
    except OSError:
        start_path = Path(start).expanduser()
    for candidate in (start_path, *start_path.parents):
        if any((candidate / marker).exists() for marker in PROJECT_MARKERS):
            return candidate
    return None


def main() -> int:
    if _truthy(os.environ.get(DISABLE_ENV)):
        return 0

    plugin_root = Path(
        os.environ.get("WEBNOVEL_PLUGIN_ROOT")
        or os.environ.get("PLUGIN_ROOT")
        or os.environ.get("CLAUDE_PLUGIN_ROOT")
        or Path(__file__).resolve().parents[1]
    )
    explicit_root = os.environ.get("WEBNOVEL_PROJECT_ROOT")
    workspace_candidate = explicit_root or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    workspace_root = Path(explicit_root) if explicit_root else _find_project_root(workspace_candidate)
    if workspace_root is None:
        return 0
    webnovel = plugin_root / "scripts" / "webnovel.py"
    if not webnovel.is_file():
        return 0

    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-X",
                "utf8",
                str(webnovel),
                "--project-root",
                str(workspace_root),
                "project-status",
                "--format",
                "summary",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=4,
        )
    except Exception:
        return 0

    output = _clip(proc.stdout or proc.stderr or "")
    if output:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
