#!/usr/bin/env python3
"""Snapshot and verify stable host files around an isolated pytest run.

The guard deliberately excludes authentication, session, and cache files.  It
only records metadata and hashes for the small set of configuration/registry
files which this test suite must never mutate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence


SCHEMA_VERSION = "webnovel-test-state-guard/v1"


def _resolve_path(raw: str | os.PathLike[str]) -> Path:
    path = Path(raw).expanduser()
    try:
        return path.resolve(strict=False)
    except OSError:
        return path.absolute()


def _deduplicate_paths(paths: Iterable[Path]) -> list[Path]:
    unique: dict[str, Path] = {}
    for path in paths:
        resolved = _resolve_path(path)
        key = os.path.normcase(str(resolved))
        unique.setdefault(key, resolved)
    return sorted(unique.values(), key=lambda value: str(value).casefold())


def resolve_guard_paths(
    environ: Mapping[str, str] | None = None,
    *,
    home: Path | None = None,
) -> list[Path]:
    """Return the stable Codex/Claude files protected by the test harness."""

    env = os.environ if environ is None else environ
    real_home = _resolve_path(home or Path.home())

    codex_roots = [_resolve_path(env.get("CODEX_HOME") or real_home / ".codex")]
    webnovel_roots = [
        _resolve_path(
            env.get("WEBNOVEL_HOME")
            or codex_roots[0] / "novel-writer-codex"
        )
    ]
    claude_roots = [
        _resolve_path(
            env.get("WEBNOVEL_CLAUDE_HOME")
            or env.get("CLAUDE_HOME")
            or real_home / ".claude"
        )
    ]

    # Explicit values may point at distinct compatibility roots.  Protect all
    # of them, not just the value that wins normal precedence.
    if env.get("CODEX_HOME"):
        codex_roots.append(_resolve_path(env["CODEX_HOME"]))
    if env.get("WEBNOVEL_HOME"):
        webnovel_roots.append(_resolve_path(env["WEBNOVEL_HOME"]))
    for name in ("WEBNOVEL_CLAUDE_HOME", "CLAUDE_HOME"):
        if env.get(name):
            claude_roots.append(_resolve_path(env[name]))

    candidates: list[Path] = []
    for root in codex_roots:
        candidates.extend(
            (
                root / "config.toml",
                root / ".webnovel-current-project",
                root / "novel-writer-codex" / "workspaces.json",
                root / "novel-writer-codex" / ".env",
            )
        )
    for root in webnovel_roots:
        candidates.extend((root / "workspaces.json", root / ".env"))
    for root in claude_roots:
        candidates.extend(
            (
                root / "settings.json",
                root / "settings.local.json",
                root / ".webnovel-current-project",
                root / "webnovel-writer" / "workspaces.json",
                root / "webnovel-writer" / ".env",
            )
        )
    return _deduplicate_paths(candidates)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_path(path: Path) -> dict[str, object]:
    """Inspect one path without following directory symlinks."""

    resolved = _resolve_path(path)
    record: dict[str, object] = {"path": str(resolved), "exists": False}
    try:
        stat = resolved.lstat()
    except FileNotFoundError:
        return record
    except OSError as exc:
        record["inspection_error"] = type(exc).__name__
        return record

    record.update(
        {
            "exists": True,
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        }
    )
    if resolved.is_symlink():
        record["kind"] = "symlink"
        try:
            record["target"] = os.readlink(resolved)
        except OSError as exc:
            record["target_error"] = type(exc).__name__
    elif resolved.is_file():
        record["kind"] = "file"
        try:
            record["sha256"] = _sha256(resolved)
        except OSError as exc:
            record["hash_error"] = type(exc).__name__
    elif resolved.is_dir():
        record["kind"] = "directory"
    else:
        record["kind"] = "other"
    return record


def create_snapshot(paths: Iterable[Path]) -> dict[str, object]:
    records = [inspect_path(path) for path in _deduplicate_paths(paths)]
    return {
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": records,
    }


def write_snapshot(path: Path, snapshot: Mapping[str, object]) -> None:
    target = _resolve_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def read_snapshot(path: Path) -> dict[str, object]:
    payload = json.loads(_resolve_path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"unsupported state-guard snapshot: {path}")
    files = payload.get("files")
    if not isinstance(files, list) or any(not isinstance(item, dict) for item in files):
        raise ValueError(f"invalid state-guard file list: {path}")
    return payload


def compare_snapshot(snapshot: Mapping[str, object]) -> list[dict[str, object]]:
    changes: list[dict[str, object]] = []
    records = snapshot.get("files")
    if not isinstance(records, list):
        raise ValueError("snapshot files must be a list")
    for before in records:
        if not isinstance(before, dict) or not isinstance(before.get("path"), str):
            raise ValueError("snapshot contains an invalid file record")
        after = inspect_path(Path(before["path"]))
        comparable_before = dict(before)
        if comparable_before != after:
            changes.append(
                {
                    "path": before["path"],
                    "before": comparable_before,
                    "after": after,
                }
            )
    return changes


def _json_result(*, ok: bool, **extra: object) -> str:
    payload = {"schema_version": SCHEMA_VERSION, "ok": ok, **extra}
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    snapshot_parser = subparsers.add_parser("snapshot")
    snapshot_parser.add_argument("--output", required=True, type=Path)
    snapshot_parser.add_argument(
        "--path",
        action="append",
        dest="paths",
        type=Path,
        help="Protect an explicit path (repeatable); defaults to stable host files.",
    )

    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--input", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        if args.command == "snapshot":
            paths = args.paths or resolve_guard_paths()
            snapshot = create_snapshot(paths)
            write_snapshot(args.output, snapshot)
            print(_json_result(ok=True, protected_count=len(snapshot["files"])))
            return 0

        snapshot = read_snapshot(args.input)
        changes = compare_snapshot(snapshot)
        print(
            _json_result(
                ok=not changes,
                protected_count=len(snapshot["files"]),
                changes=changes,
            )
        )
        return 0 if not changes else 1
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(
            _json_result(
                ok=False,
                error={"code": "state_guard_internal_error", "message": str(exc)},
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
