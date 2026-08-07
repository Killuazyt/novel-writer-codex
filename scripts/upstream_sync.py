#!/usr/bin/env python3
"""Inspect or stage an upstream snapshot without fetching or touching the worktree."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping, Sequence


SCHEMA_VERSION = 1
DEFAULT_REF = "upstream/master"
SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
REPORT_FILENAME = "report.json"

RESULT_FIELDS = (
    "schema_version",
    "action",
    "command",
    "status",
    "exit_code",
    "root",
    "lock_path",
    "source_kind",
    "source",
    "source_ref",
    "source_sha",
    "target_sha",
    "locked_sha",
    "source_subdirectory",
    "expected_file_count",
    "actual_file_count",
    "expected_aggregate",
    "actual_aggregate",
    "changes",
    "added",
    "removed",
    "changed",
    "staging_dir",
    "destination",
    "report_path",
    "copied_file_count",
    "reused",
    "issues",
    "message",
)


class SyncInputError(ValueError):
    """The lock, source, ref, or path is invalid or unsafe."""


@dataclass(frozen=True)
class LockData:
    path: Path
    locked_sha: str
    source_subdirectory: str
    include: tuple[str, ...]
    exclude: tuple[str, ...]
    expected_files: Mapping[str, str]
    expected_aggregate: str
    expected_file_count: int


@dataclass(frozen=True)
class Snapshot:
    files: Mapping[str, bytes]
    hashes: Mapping[str, str]
    aggregate: str


class Source:
    kind = ""
    description = ""
    ref: str | None = None
    sha = ""

    def list_files(self) -> list[str]:
        raise NotImplementedError

    def read_bytes(self, relative_path: str) -> bytes:
        raise NotImplementedError


class DirectorySource(Source):
    kind = "directory"

    def __init__(self, source_root: Path, source_subdirectory: str, sha: str):
        self.source_root = source_root.expanduser().resolve()
        self.description = str(self.source_root)
        self.sha = _validate_sha(sha, "--sha")
        if not self.source_root.is_dir():
            raise SyncInputError(f"source root is not a directory: {self.source_root}")

        nested = _safe_child(self.source_root, source_subdirectory, "source_subdirectory")
        if nested.is_dir():
            self.import_root = nested
        elif self.source_root.name == PurePosixPath(source_subdirectory).name:
            self.import_root = self.source_root
        else:
            raise SyncInputError(
                f"source subdirectory is missing: {self.source_root / source_subdirectory}"
            )

    def list_files(self) -> list[str]:
        files: list[str] = []
        root = self.import_root.resolve()
        for current, dirnames, filenames in os.walk(root, followlinks=False):
            current_path = Path(current)
            _require_within(root, current_path.resolve(), "source directory")
            for dirname in dirnames:
                directory = current_path / dirname
                if directory.is_symlink():
                    raise SyncInputError(f"source symlink is not allowed: {directory}")
                _require_within(root, directory.resolve(), "source directory")
            for filename in filenames:
                path = current_path / filename
                if path.is_symlink():
                    raise SyncInputError(f"source symlink is not allowed: {path}")
                _require_within(root, path.resolve(), "source file")
                relative = path.relative_to(root).as_posix()
                files.append(_validate_relative_path(relative, "source file"))
        return sorted(files, key=lambda value: value.encode("utf-8"))

    def read_bytes(self, relative_path: str) -> bytes:
        path = _safe_child(self.import_root, relative_path, "source file")
        if path.is_symlink() or not path.is_file():
            raise SyncInputError(f"source file is missing or unsafe: {relative_path}")
        return path.read_bytes()


class GitRefSource(Source):
    kind = "git_ref"

    def __init__(self, repository_root: Path, source_subdirectory: str, ref: str):
        self.repository_root = repository_root.resolve()
        self.description = str(self.repository_root)
        self.ref = ref.strip()
        if not self.ref or "\x00" in self.ref:
            raise SyncInputError("local ref must be a non-empty Git revision")
        try:
            resolved = _run_git(
                self.repository_root,
                ["rev-parse", "--verify", "--end-of-options", f"{self.ref}^{{commit}}"],
            ).stdout.decode("ascii", errors="strict").strip()
        except SyncInputError as exc:
            raise SyncInputError(f"local Git ref is unavailable ({self.ref}): {exc}") from exc
        self.sha = _validate_sha(resolved, "resolved local ref")
        self.source_subdirectory = source_subdirectory
        self._objects: dict[str, str] | None = None

    def list_files(self) -> list[str]:
        pathspec = self.source_subdirectory
        output = _run_git(
            self.repository_root,
            ["ls-tree", "-r", "-z", self.sha, "--", pathspec],
        ).stdout
        prefix = "" if pathspec == "." else pathspec.rstrip("/") + "/"
        objects: dict[str, str] = {}
        for record in output.split(b"\0"):
            if not record:
                continue
            try:
                metadata, raw_path = record.split(b"\t", 1)
                mode, object_type, object_id = metadata.decode("ascii").split()
                full_path = raw_path.decode("utf-8", errors="strict")
            except (ValueError, UnicodeDecodeError) as exc:
                raise SyncInputError(f"invalid Git tree entry: {exc}") from exc
            if object_type != "blob" or mode == "120000":
                raise SyncInputError(f"unsupported Git tree entry: {full_path}")
            if prefix and not full_path.startswith(prefix):
                raise SyncInputError(f"Git tree path escaped source subdirectory: {full_path}")
            relative = full_path[len(prefix) :] if prefix else full_path
            relative = _validate_relative_path(relative, "Git tree path")
            if relative in objects:
                raise SyncInputError(f"duplicate Git tree path: {relative}")
            objects[relative] = object_id
        self._objects = objects
        return sorted(objects, key=lambda value: value.encode("utf-8"))

    def read_bytes(self, relative_path: str) -> bytes:
        if self._objects is None:
            self.list_files()
        assert self._objects is not None
        object_id = self._objects.get(relative_path)
        if object_id is None:
            raise SyncInputError(f"Git source file is missing: {relative_path}")
        return _run_git(self.repository_root, ["cat-file", "blob", object_id]).stdout


def _run_git(root: Path, args: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
    try:
        process = subprocess.run(
            ["git", "-C", str(root), *args],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise SyncInputError(f"unable to run Git: {exc}") from exc
    if process.returncode != 0:
        detail = process.stderr.decode("utf-8", errors="replace").strip()
        raise SyncInputError(detail or f"Git command failed with exit code {process.returncode}")
    return process


def _validate_sha(value: str, label: str) -> str:
    normalized = str(value or "").strip().lower()
    if not SHA_RE.fullmatch(normalized):
        raise SyncInputError(f"{label} must be a full 40-character hexadecimal commit SHA")
    return normalized


def _validate_relative_path(value: str, label: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise SyncInputError(f"{label} must be a non-empty UTF-8 POSIX path")
    raw_parts = value.split("/")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or value.startswith("/")
        or re.match(r"^[A-Za-z]:", value)
        or any(part in {"", ".", ".."} for part in raw_parts)
    ):
        raise SyncInputError(f"{label} contains an absolute or traversal path: {value!r}")
    return path.as_posix()


def _validate_pattern(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value or "\\" in value:
        raise SyncInputError(f"{label} must be a non-empty POSIX glob")
    pattern = value.strip()
    if (
        pattern.startswith("/")
        or PurePosixPath(pattern).is_absolute()
        or re.match(r"^[A-Za-z]:", pattern)
    ):
        raise SyncInputError(f"{label} must be relative: {pattern!r}")
    for expanded in _expand_braces(pattern):
        if any(part in {"", ".", ".."} for part in expanded.split("/")):
            raise SyncInputError(f"{label} contains path traversal: {pattern!r}")
    return pattern


def _require_within(root: Path, candidate: Path, label: str) -> None:
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise SyncInputError(f"{label} escapes its allowed root: {candidate}") from exc


def _safe_child(root: Path, relative_path: str, label: str) -> Path:
    relative = _validate_relative_path(relative_path, label)
    resolved_root = root.resolve()
    candidate = (resolved_root / Path(*PurePosixPath(relative).parts)).resolve(strict=False)
    _require_within(resolved_root, candidate, label)
    return candidate


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _aggregate_hash(hashes: Mapping[str, str]) -> str:
    digest = hashlib.sha256()
    for path in sorted(hashes, key=lambda value: value.encode("utf-8")):
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashes[path].lower().encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _load_lock(root: Path) -> LockData:
    path = root / "upstream-lock.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SyncInputError(f"upstream lock is missing: {path}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SyncInputError(f"upstream lock cannot be read: {exc}") from exc
    if not isinstance(payload, dict):
        raise SyncInputError("upstream lock must be a JSON object")
    if payload.get("schema_version") != 1:
        raise SyncInputError("upstream lock schema_version must be 1")

    upstream = payload.get("upstream")
    import_spec = payload.get("import")
    hashes = payload.get("hashes")
    if not isinstance(upstream, dict) or not isinstance(import_spec, dict) or not isinstance(hashes, dict):
        raise SyncInputError("upstream lock is missing upstream/import/hashes objects")

    locked_sha = _validate_sha(str(upstream.get("commit") or ""), "lock upstream.commit")
    source_subdirectory = _validate_relative_path(
        str(import_spec.get("source_subdirectory") or ""),
        "lock import.source_subdirectory",
    )
    raw_include = import_spec.get("include")
    raw_exclude = import_spec.get("exclude")
    if not isinstance(raw_include, list) or not raw_include:
        raise SyncInputError("lock import.include must be a non-empty list")
    if not isinstance(raw_exclude, list):
        raise SyncInputError("lock import.exclude must be a list")
    include = tuple(
        _normalize_pattern(_validate_pattern(item, "lock include glob"), source_subdirectory)
        for item in raw_include
    )
    exclude_items: list[str] = []
    for item in raw_exclude:
        if not isinstance(item, dict):
            raise SyncInputError("each lock import.exclude item must be an object")
        exclude_items.append(
            _normalize_pattern(
                _validate_pattern(item.get("glob"), "lock exclude glob"),
                source_subdirectory,
            )
        )

    raw_files = hashes.get("files")
    if not isinstance(raw_files, dict):
        raise SyncInputError("lock hashes.files must be an object")
    if hashes.get("algorithm") != "sha256":
        raise SyncInputError("lock hashes.algorithm must be sha256")
    expected_files: dict[str, str] = {}
    for raw_path, raw_hash in raw_files.items():
        relative = _validate_relative_path(raw_path, "lock hash path")
        file_hash = str(raw_hash or "").lower()
        if not HASH_RE.fullmatch(file_hash):
            raise SyncInputError(f"invalid SHA-256 for lock path: {relative}")
        if relative == REPORT_FILENAME:
            raise SyncInputError(f"lock path is reserved by prepare: {REPORT_FILENAME}")
        if not _selected(relative, include, tuple(exclude_items)):
            raise SyncInputError(f"lock hash path is outside include/exclude rules: {relative}")
        expected_files[relative] = file_hash

    try:
        expected_file_count = int(hashes.get("file_count"))
    except (TypeError, ValueError) as exc:
        raise SyncInputError("lock hashes.file_count must be an integer") from exc
    expected_aggregate = str(hashes.get("aggregate") or "").lower()
    if not HASH_RE.fullmatch(expected_aggregate):
        raise SyncInputError("lock hashes.aggregate must be a SHA-256 value")
    calculated_aggregate = _aggregate_hash(expected_files)
    if expected_file_count != len(expected_files) or expected_aggregate != calculated_aggregate:
        raise SyncInputError("upstream lock file count or aggregate is internally inconsistent")

    return LockData(
        path=path,
        locked_sha=locked_sha,
        source_subdirectory=source_subdirectory,
        include=include,
        exclude=tuple(exclude_items),
        expected_files=expected_files,
        expected_aggregate=expected_aggregate,
        expected_file_count=expected_file_count,
    )


def _normalize_pattern(pattern: str, source_subdirectory: str) -> str:
    prefix = source_subdirectory.rstrip("/") + "/"
    if pattern.startswith(prefix):
        return pattern[len(prefix) :]
    return pattern


def _expand_braces(pattern: str) -> Iterable[str]:
    start = pattern.find("{")
    if start < 0:
        yield pattern
        return
    end = pattern.find("}", start + 1)
    if end < 0:
        raise SyncInputError(f"unclosed brace in glob: {pattern!r}")
    choices = pattern[start + 1 : end].split(",")
    if not choices or any(not choice for choice in choices):
        raise SyncInputError(f"empty brace choice in glob: {pattern!r}")
    for choice in choices:
        yield from _expand_braces(pattern[:start] + choice + pattern[end + 1 :])


def _matches(path: str, pattern: str) -> bool:
    for expanded in _expand_braces(pattern):
        candidates = [expanded]
        if expanded.startswith("**/"):
            candidates.append(expanded[3:])
        for candidate in candidates:
            if fnmatch.fnmatchcase(path, candidate):
                return True
            if candidate.endswith("/**"):
                prefix = candidate[:-3].rstrip("/")
                if path == prefix or path.startswith(prefix + "/"):
                    return True
    return False


def _selected(path: str, include: Sequence[str], exclude: Sequence[str]) -> bool:
    return any(_matches(path, pattern) for pattern in include) and not any(
        _matches(path, pattern) for pattern in exclude
    )


def _snapshot(source: Source, lock: LockData) -> Snapshot:
    contents: dict[str, bytes] = {}
    hashes: dict[str, str] = {}
    for relative in source.list_files():
        if not _selected(relative, lock.include, lock.exclude):
            continue
        if relative == REPORT_FILENAME:
            raise SyncInputError(f"source path is reserved by prepare: {REPORT_FILENAME}")
        data = source.read_bytes(relative)
        contents[relative] = data
        hashes[relative] = _sha256(data)
    return Snapshot(files=contents, hashes=hashes, aggregate=_aggregate_hash(hashes))


def _empty_result(command: str, root: Path) -> dict[str, object]:
    result: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "action": command,
        "command": command,
        "status": "invalid",
        "exit_code": 2,
        "root": str(root),
        "lock_path": str(root / "upstream-lock.json"),
        "source_kind": None,
        "source": None,
        "source_ref": None,
        "source_sha": None,
        "target_sha": None,
        "locked_sha": None,
        "source_subdirectory": None,
        "expected_file_count": None,
        "actual_file_count": None,
        "expected_aggregate": None,
        "actual_aggregate": None,
        "changes": {"added": [], "modified": [], "deleted": []},
        "added": [],
        "removed": [],
        "changed": [],
        "staging_dir": None,
        "destination": None,
        "report_path": None,
        "copied_file_count": 0,
        "reused": False,
        "issues": [],
        "message": "",
    }
    assert tuple(result) == RESULT_FIELDS
    return result


def _populate_comparison(result: dict[str, object], lock: LockData, source: Source, snapshot: Snapshot) -> bool:
    expected_paths = set(lock.expected_files)
    actual_paths = set(snapshot.hashes)
    added = sorted(actual_paths - expected_paths, key=lambda value: value.encode("utf-8"))
    removed = sorted(expected_paths - actual_paths, key=lambda value: value.encode("utf-8"))
    changed = sorted(
        (
            path
            for path in expected_paths & actual_paths
            if lock.expected_files[path] != snapshot.hashes[path]
        ),
        key=lambda value: value.encode("utf-8"),
    )
    result.update(
        {
            "source_kind": source.kind,
            "source": source.description,
            "source_ref": source.ref,
            "source_sha": source.sha,
            "target_sha": source.sha,
            "locked_sha": lock.locked_sha,
            "source_subdirectory": lock.source_subdirectory,
            "expected_file_count": lock.expected_file_count,
            "actual_file_count": len(snapshot.files),
            "expected_aggregate": lock.expected_aggregate,
            "actual_aggregate": snapshot.aggregate,
            "changes": {
                "added": added,
                "modified": changed,
                "deleted": removed,
            },
            "added": added,
            "removed": removed,
            "changed": changed,
        }
    )
    return bool(
        source.sha != lock.locked_sha
        or added
        or removed
        or changed
        or snapshot.aggregate != lock.expected_aggregate
    )


def _destination(root: Path, sha: str) -> Path:
    target = root / ".tmp" / "upstream-sync" / _validate_sha(sha, "source SHA")
    resolved = target.resolve(strict=False)
    _require_within(root, resolved, "prepare destination")
    for parent in (root / ".tmp", root / ".tmp" / "upstream-sync"):
        if parent.exists() and parent.is_symlink():
            raise SyncInputError(f"prepare parent must not be a symlink: {parent}")
    if target.exists() and target.is_symlink():
        raise SyncInputError(f"prepare destination must not be a symlink: {target}")
    return target


def _report_bytes(report: Mapping[str, object]) -> bytes:
    return (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _desired_tree(snapshot: Snapshot, report: Mapping[str, object]) -> dict[str, bytes]:
    desired = dict(snapshot.files)
    desired[REPORT_FILENAME] = _report_bytes(report)
    return desired


def _read_tree(root: Path) -> tuple[dict[str, bytes], set[str]]:
    if not root.is_dir():
        raise SyncInputError(f"existing prepare destination is not a directory: {root}")
    files: dict[str, bytes] = {}
    directories: set[str] = set()
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        _require_within(root.resolve(), current_path.resolve(), "prepared directory")
        for dirname in dirnames:
            directory = current_path / dirname
            if directory.is_symlink():
                raise SyncInputError(f"prepared tree contains a symlink: {directory}")
            relative_directory = _validate_relative_path(
                directory.relative_to(root).as_posix(), "prepared directory"
            )
            directories.add(relative_directory)
        for filename in filenames:
            path = current_path / filename
            if path.is_symlink():
                raise SyncInputError(f"prepared tree contains a symlink: {path}")
            relative = _validate_relative_path(path.relative_to(root).as_posix(), "prepared file")
            files[relative] = path.read_bytes()
    return files, directories


def _desired_directories(paths: Iterable[str]) -> set[str]:
    directories: set[str] = set()
    for relative in paths:
        parent = PurePosixPath(relative).parent
        while parent != PurePosixPath("."):
            directories.add(parent.as_posix())
            parent = parent.parent
    return directories


def _write_tree(destination: Path, desired: Mapping[str, bytes]) -> None:
    created = False
    try:
        destination.mkdir(parents=True, exist_ok=False)
        created = True
        for relative in sorted(desired, key=lambda value: value.encode("utf-8")):
            target = _safe_child(destination, relative, "prepared file")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(desired[relative])
    except Exception:
        if created:
            shutil.rmtree(destination, ignore_errors=True)
        raise


def execute(
    command: str,
    *,
    root: str | Path | None = None,
    source_root: str | Path | None = None,
    sha: str | None = None,
    ref: str | None = None,
) -> tuple[int, dict[str, object]]:
    repository_root = Path(root) if root is not None else Path(__file__).resolve().parents[1]
    repository_root = repository_root.expanduser().resolve()
    result = _empty_result(command, repository_root)
    try:
        if command not in {"check", "prepare"}:
            raise SyncInputError(f"unknown command: {command}")
        if not repository_root.is_dir():
            raise SyncInputError(f"repository root is not a directory: {repository_root}")
        if source_root is not None and ref is not None:
            raise SyncInputError("--source-root and --ref are mutually exclusive")
        if source_root is None and sha is not None:
            raise SyncInputError("--sha requires --source-root")
        if source_root is not None and sha is None:
            raise SyncInputError("--source-root requires --sha")

        lock = _load_lock(repository_root)
        result["lock_path"] = str(lock.path)
        if source_root is not None:
            source: Source = DirectorySource(Path(source_root), lock.source_subdirectory, str(sha))
        else:
            source = GitRefSource(repository_root, lock.source_subdirectory, ref or DEFAULT_REF)
        snapshot = _snapshot(source, lock)
        drift = _populate_comparison(result, lock, source, snapshot)
        if drift:
            result["issues"] = [
                {
                    "code": "lock_drift",
                    "severity": "blocker" if command == "check" else "warning",
                    "message": "upstream snapshot differs from the frozen lock",
                }
            ]

        if command == "check":
            result.update(
                {
                    "status": "drift" if drift else "current",
                    "exit_code": 1 if drift else 0,
                    "message": "upstream snapshot differs from lock" if drift else "upstream snapshot matches lock",
                }
            )
            return int(result["exit_code"]), result

        destination = _destination(repository_root, source.sha)
        report_path = destination / REPORT_FILENAME
        result.update(
            {
                "status": "prepared",
                "exit_code": 0,
                "staging_dir": str(destination),
                "destination": str(destination),
                "report_path": str(report_path),
                "copied_file_count": len(snapshot.files),
                "reused": False,
                "message": "prepared upstream snapshot",
            }
        )
        on_disk_report = dict(result)
        desired = _desired_tree(snapshot, on_disk_report)
        if destination.exists():
            if not destination.is_dir():
                result.update(
                    {
                        "status": "conflict",
                        "exit_code": 1,
                        "issues": [
                            {
                                "code": "staging_conflict",
                                "severity": "blocker",
                                "message": "prepare destination already exists with different contents",
                            }
                        ],
                        "message": "prepare destination already exists with different contents",
                    }
                )
                return 1, result
            existing_files, existing_directories = _read_tree(destination)
            if existing_files != desired or existing_directories != _desired_directories(desired):
                result.update(
                    {
                        "status": "conflict",
                        "exit_code": 1,
                        "issues": [
                            {
                                "code": "staging_conflict",
                                "severity": "blocker",
                                "message": "prepare destination already exists with different contents",
                            }
                        ],
                        "message": "prepare destination already exists with different contents",
                    }
                )
                return 1, result
            result["reused"] = True
            result["message"] = "prepared upstream snapshot already exists"
            return 0, result

        _write_tree(destination, desired)
        return 0, result
    except (SyncInputError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        message = str(exc)
        result.update(
            {
                "status": "invalid",
                "exit_code": 2,
                "issues": [
                    {
                        "code": "invalid_input",
                        "severity": "error",
                        "message": message,
                    }
                ],
                "message": message,
            }
        )
        return 2, result
    except Exception as exc:  # Keep the CLI structured and traceback-free.
        message = f"{type(exc).__name__}: {exc}"
        result.update(
            {
                "status": "invalid",
                "exit_code": 2,
                "issues": [
                    {
                        "code": "execution_error",
                        "severity": "error",
                        "message": message,
                    }
                ],
                "message": message,
            }
        )
        return 2, result


def format_result(result: Mapping[str, object], output_format: str) -> str:
    if output_format == "json":
        return json.dumps(result, ensure_ascii=False, indent=2)
    lines: list[str] = []
    for field in RESULT_FIELDS:
        value = result.get(field)
        if isinstance(value, (list, dict)) or value is None or isinstance(value, bool):
            rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        else:
            rendered = str(value)
        lines.append(f"{field}: {rendered}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "prepare"))
    parser.add_argument("--root", default="", help="Downstream repository root")
    parser.add_argument("--source-root", default="", help="Filesystem upstream repository root")
    parser.add_argument("--sha", default="", help="Full commit SHA identifying --source-root")
    parser.add_argument("--ref", default="", help=f"Local Git ref (default: {DEFAULT_REF})")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    code, result = execute(
        args.command,
        root=args.root or None,
        source_root=args.source_root or None,
        sha=args.sha or None,
        ref=args.ref or None,
    )
    print(format_result(result, args.format))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
