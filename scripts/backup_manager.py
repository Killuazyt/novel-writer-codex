#!/usr/bin/env python3
"""Explicitly authorized, allowlisted Git backup receipts.

The backup path never initializes Git, never stages ``.``, never reuses a
parent repository, and never mutates the user's branch or normal index.
Non-Git projects return a structured, successful skip receipt.
"""

import subprocess
import json
import os
import sys
import hashlib
import tempfile
import stat
from pathlib import Path
from collections.abc import Mapping
from uuid import UUID

from runtime_compat import enable_windows_utf8_stdio
from datetime import datetime
from typing import Any, Optional, List, Tuple

# ============================================================================
# 安全修复：导入安全工具函数（P1 MEDIUM）
# ============================================================================
from security_utils import sanitize_commit_message, is_git_available
from project_locator import resolve_project_root
from data_modules.codex_interaction import build_choice_request, resolve_choice

try:
    from filelock import FileLock, Timeout
except ImportError:  # fail closed on the production Git path
    FileLock = None  # type: ignore[assignment]

# Windows 编码兼容性修复
if sys.platform == "win32":
    enable_windows_utf8_stdio()


class BackupError(RuntimeError):
    """Git backup operation failed."""


BACKUP_RECEIPT_SCHEMA = "webnovel-backup-receipt/v1"
BACKUP_DECISION_RECEIPT_SCHEMA = "webnovel-git-backup-decision-receipt/v1"
BACKUP_AUTHORIZATION_REGISTRY_SCHEMA = "webnovel-git-backup-authorization-state/v1"
AUTHORIZATION_PREFIX = "webnovel-git-backup:"
BACKUP_DECISION_PREFIX = "WEBNOVEL_GIT_BACKUP_DECISION_REQUEST "
TRUSTED_CODEX_SESSIONS_ROOT = Path(os.path.abspath(Path.home() / ".codex" / "sessions"))
MAX_DECISION_ROLLOUT_BYTES = 32 * 1024 * 1024
MAX_DECISION_RECEIPT_BYTES = 2 * 1024 * 1024
MAX_ALLOWLIST_FILE_BYTES = 64 * 1024 * 1024
MAX_GIT_CONTROL_BYTES = 1024 * 1024
DISABLED_GIT_HOOKS_PATH = "/dev/null"


def _is_reparse_point(path: Path) -> bool:
    try:
        attrs = int(getattr(path.lstat(), "st_file_attributes", 0))
    except OSError:
        # A metadata race or unreadable leaf must never downgrade the path to
        # trusted.  Callers use this predicate only after the path appeared to
        # exist (or directly for a Git-control leaf), so ambiguity is unsafe.
        return True
    return path.is_symlink() or bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _absolute_lexical(path: str | Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _path_components(path: Path) -> list[Path]:
    absolute = _absolute_lexical(path)
    anchor = Path(absolute.anchor)
    current = anchor
    result = [anchor]
    for part in absolute.parts[1:]:
        current = current / part
        result.append(current)
    return result


def _require_safe_path(
    trusted_root: str | Path,
    path: str | Path,
    *,
    must_exist: bool,
    regular_file: bool = False,
) -> Path:
    """Validate lexical ancestors before any resolve can erase a link leaf."""

    root = _absolute_lexical(trusted_root)
    candidate = _absolute_lexical(path)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise BackupError(f"path escapes its trusted root: {candidate}") from exc
    for component in _path_components(candidate):
        if component.exists() or component.is_symlink():
            if _is_reparse_point(component):
                raise BackupError(f"reparse-point path is forbidden: {component}")
    if must_exist and not candidate.exists():
        raise BackupError(f"required path is missing: {candidate}")
    if regular_file and must_exist and not candidate.is_file():
        raise BackupError(f"required file is missing: {candidate}")
    if regular_file and (candidate.exists() or candidate.is_symlink()) and not candidate.is_file():
        raise BackupError(f"path is not a regular file: {candidate}")
    try:
        candidate.resolve(strict=must_exist).relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise BackupError(f"path escapes its trusted root: {candidate}") from exc
    return candidate


def _safe_project_root(project_root: str | Path) -> Path:
    lexical = _absolute_lexical(project_root)
    _require_safe_path(lexical, lexical, must_exist=True)
    if not lexical.is_dir():
        raise BackupError(f"project root is not a directory: {lexical}")
    return lexical.resolve(strict=True)


def _require_single_link_control_file(root: Path, path: Path, *, must_exist: bool) -> Path:
    lexical = _require_safe_path(
        root, path, must_exist=must_exist, regular_file=True
    )
    if lexical.exists():
        try:
            metadata = lexical.lstat()
        except OSError as exc:
            raise BackupError(f"cannot inspect control-file identity: {lexical}: {exc}") from exc
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise BackupError(f"hardlinked or non-regular control file is forbidden: {lexical}")
    return lexical


def _stable_read_bytes(path: Path, *, trusted_root: Path, max_bytes: int) -> bytes:
    lexical = _require_safe_path(trusted_root, path, must_exist=True, regular_file=True)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(lexical, flags)
    except OSError as exc:
        raise BackupError(f"cannot safely open file: {lexical}: {exc}") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
            raise BackupError(f"file is not regular or exceeds size limit: {lexical}")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(max_bytes + 1)
        after = os.fstat(fd)
        path_after = lexical.stat()
        before_id = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        after_id = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        path_id = (path_after.st_dev, path_after.st_ino, path_after.st_size, path_after.st_mtime_ns)
        _require_safe_path(trusted_root, lexical, must_exist=True, regular_file=True)
        if len(raw) > max_bytes or len(raw) != before.st_size or before_id != after_id or before_id != path_id:
            raise BackupError(f"file changed during bounded read: {lexical}")
        return raw
    except OSError as exc:
        raise BackupError(f"cannot safely read file: {lexical}: {exc}") from exc
    finally:
        os.close(fd)


def _stable_read_json(path: Path, *, trusted_root: Path, max_bytes: int) -> dict[str, Any]:
    raw = _stable_read_bytes(path, trusted_root=trusted_root, max_bytes=max_bytes)
    if raw.startswith(b"\xef\xbb\xbf"):
        raise BackupError(f"UTF-8 BOM is forbidden: {path}")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupError(f"invalid UTF-8 JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise BackupError(f"JSON file must contain an object: {path}")
    return value


def _safe_mkdir_chain(root: Path, target: Path) -> None:
    root = _absolute_lexical(root)
    target = _absolute_lexical(target)
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise BackupError(f"registry path escapes project: {target}") from exc
    current = root
    _require_safe_path(root, root, must_exist=True)
    for part in relative.parts:
        current = current / part
        if current.exists() or current.is_symlink():
            if _is_reparse_point(current) or not current.is_dir():
                raise BackupError(f"unsafe registry directory: {current}")
        else:
            try:
                current.mkdir()
            except FileExistsError:
                # A concurrent process may have created the same control
                # directory.  Trust it only after the same full validation.
                pass
            except OSError as exc:
                raise BackupError(f"cannot create registry directory: {current}: {exc}") from exc
            if _is_reparse_point(current) or not current.is_dir():
                raise BackupError(f"unsafe registry directory: {current}")


def _atomic_registry_write(root: Path, path: Path, payload: Mapping[str, Any]) -> None:
    registry = path.parent
    _safe_mkdir_chain(root, registry)
    _require_safe_path(root, path, must_exist=False, regular_file=True)
    raw = json.dumps(dict(payload), ensure_ascii=False, indent=2).encode("utf-8")
    fd, temp_name = tempfile.mkstemp(prefix=path.stem + "-", suffix=".tmp", dir=registry)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        _require_safe_path(root, registry, must_exist=True)
        _require_safe_path(root, path, must_exist=False, regular_file=True)
        os.replace(temp_path, path)
        if _stable_read_json(path, trusted_root=root, max_bytes=MAX_DECISION_RECEIPT_BYTES) != dict(payload):
            raise BackupError("authorization registry failed exact readback")
        try:
            directory_fd = os.open(registry, os.O_RDONLY)
        except OSError:
            directory_fd = -1
        if directory_fd >= 0:
            try:
                os.fsync(directory_fd)
            except OSError:
                pass
            finally:
                os.close(directory_fd)
    finally:
        try:
            temp_path.unlink()
        except OSError:
            pass


def _canonical_allowlist(project_root: str | Path, allowlist: List[str]) -> List[str]:
    root = _safe_project_root(project_root)
    normalized: List[str] = []
    for raw in allowlist:
        relative = Path(str(raw))
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise BackupError(f"allowlist 路径越界: {raw}")
        value = relative.as_posix()
        folded_parts = [part.casefold() for part in relative.parts]
        if (
            folded_parts[0] == ".git"
            or folded_parts[:2] == [".webnovel", "backup-authorizations"]
            or any(character in value for character in ("\x00", "\r", "\n", "\t", "*", "?"))
            or ":" in value
        ):
            raise BackupError(f"allowlist 包含 Git/control 路径或不安全字符: {raw}")
        target = root.joinpath(relative)
        try:
            target.resolve(strict=False).relative_to(root)
        except (OSError, ValueError) as exc:
            raise BackupError(f"allowlist 路径越界: {raw}") from exc
        current = root
        for part in relative.parts:
            current = current / part
            if (current.exists() or current.is_symlink()) and _is_reparse_point(current):
                raise BackupError(f"allowlist 不允许 reparse/symlink: {raw}")
        if target.exists() and not target.is_file():
            raise BackupError(f"allowlist 只允许文件或待删除文件: {raw}")
        if value not in normalized:
            normalized.append(value)
    if not normalized:
        raise BackupError("Git backup 需要非空明确 allowlist")
    return sorted(normalized)


def _clean_git_environment() -> dict[str, str]:
    """Drop every caller-controlled Git routing/config variable."""

    return {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("GIT_")
    }


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left.resolve(strict=True))) == os.path.normcase(
        str(right.resolve(strict=True))
    )


def _git_argv(root: Path, git_dir: Path, args: List[str]) -> list[str]:
    return [
        "git",
        "--no-replace-objects",
        "--no-optional-locks",
        f"--git-dir={git_dir}",
        f"--work-tree={root}",
        "-c",
        f"core.hooksPath={DISABLED_GIT_HOOKS_PATH}",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.untrackedCache=false",
        *args,
    ]


def _run_bound_git_status(
    root: Path,
    git_dir: Path,
    args: List[str],
    *,
    input_bytes: bytes | None = None,
    index_file: Path | None = None,
    timeout: int = 60,
) -> tuple[int, bytes, bytes]:
    clean_env = _clean_git_environment()
    if index_file is not None:
        index_file = _absolute_lexical(index_file)
        managed_tmp = _absolute_lexical(
            root / ".webnovel" / "backup-authorizations" / "tmp"
        )
        try:
            index_file.relative_to(managed_tmp)
        except ValueError as exc:
            raise BackupError("temporary Git index must stay inside the backup registry") from exc
        _require_safe_path(root, managed_tmp, must_exist=True)
        _require_safe_path(root, index_file, must_exist=False, regular_file=True)
        clean_env["GIT_INDEX_FILE"] = str(index_file)
    try:
        completed = subprocess.run(
            _git_argv(root, git_dir, args),
            cwd=root,
            input=input_bytes,
            capture_output=True,
            text=False,
            timeout=timeout,
            check=False,
            shell=False,
            env=clean_env,
        )
    except subprocess.TimeoutExpired as exc:
        raise BackupError(f"git {' '.join(args)} timed out") from exc
    except OSError as exc:
        raise BackupError(f"git {' '.join(args)} failed to start: {exc}") from exc
    return completed.returncode, completed.stdout, completed.stderr


def _run_bound_git(
    root: Path,
    git_dir: Path,
    args: List[str],
    *,
    input_bytes: bytes | None = None,
    index_file: Path | None = None,
    timeout: int = 60,
) -> tuple[bool, bytes, bytes]:
    returncode, stdout, stderr = _run_bound_git_status(
        root,
        git_dir,
        args,
        input_bytes=input_bytes,
        index_file=index_file,
        timeout=timeout,
    )
    return returncode == 0, stdout, stderr


def _decode_git_output(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


def _require_clean_git_metadata_tree(root: Path, git_dir: Path) -> None:
    """Reject gitfile/worktree/reparse metadata before Git can write through it."""

    _require_safe_path(root, git_dir, must_exist=True)
    if not git_dir.is_dir() or _is_reparse_point(git_dir):
        raise BackupError("standalone Git backup requires a regular .git directory")

    def traversal_error(exc: OSError) -> None:
        raise BackupError(f"cannot inspect Git metadata safely: {exc}")

    for current_raw, dirs, files in os.walk(
        git_dir, followlinks=False, onerror=traversal_error
    ):
        current = Path(current_raw)
        _require_safe_path(root, current, must_exist=True)
        for name in [*dirs, *files]:
            candidate = current / name
            if _is_reparse_point(candidate):
                raise BackupError(f"Git metadata contains a reparse/symlink path: {candidate}")


def _resolve_git_reported_path(root: Path, raw: bytes, *, label: str) -> Path:
    text = _decode_git_output(raw).strip()
    if not text:
        raise BackupError(f"Git probe returned an empty {label}")
    path = Path(text)
    if not path.is_absolute():
        path = root / path
    try:
        return path.resolve(strict=True)
    except OSError as exc:
        raise BackupError(f"Git probe returned an invalid {label}: {text}") from exc


def _probe_standalone_repository(project_root: str | Path) -> dict[str, Any]:
    root = _safe_project_root(project_root)
    git_dir = root / ".git"
    if not git_dir.exists() and not git_dir.is_symlink():
        return {"status": "not_repo", "root": str(root)}
    if not is_git_available():
        raise BackupError("Git executable is unavailable for an existing .git directory")
    _require_clean_git_metadata_tree(root, git_dir)
    for forbidden in (git_dir / "commondir", git_dir / "gitdir"):
        if forbidden.exists() or forbidden.is_symlink():
            raise BackupError("linked-worktree Git control files are forbidden")
    if (git_dir / "worktrees").exists() or (git_dir / "worktrees").is_symlink():
        raise BackupError("repositories with linked worktree metadata are forbidden")

    def required(*args: str) -> bytes:
        ok, stdout, stderr = _run_bound_git(root, git_dir, list(args), timeout=15)
        if not ok:
            detail = _decode_git_output(stderr or stdout).strip()
            raise BackupError(f"Git repository probe failed ({' '.join(args)}): {detail}")
        return stdout

    top = _resolve_git_reported_path(root, required("rev-parse", "--show-toplevel"), label="toplevel")
    absolute_git_dir = _resolve_git_reported_path(
        root, required("rev-parse", "--absolute-git-dir"), label="absolute git dir"
    )
    common_dir = _resolve_git_reported_path(
        root, required("rev-parse", "--git-common-dir"), label="common git dir"
    )
    objects_raw = required("rev-parse", "--git-path", "objects")
    objects_text = _decode_git_output(objects_raw).strip()
    objects_lexical = _absolute_lexical(
        Path(objects_text) if Path(objects_text).is_absolute() else root / objects_text
    )
    objects_dir = _resolve_git_reported_path(root, objects_raw, label="object dir")
    expected_git_dir = git_dir.resolve(strict=True)
    expected_objects = (git_dir / "objects").resolve(strict=True)
    if not _same_path(top, root):
        raise BackupError("novel root is not the exact Git toplevel")
    if not _same_path(absolute_git_dir, expected_git_dir) or not _same_path(common_dir, expected_git_dir):
        raise BackupError("gitfile, linked worktree, submodule, or external common dir is forbidden")
    if os.path.normcase(str(objects_lexical)) != os.path.normcase(
        str(_absolute_lexical(git_dir / "objects"))
    ) or not _same_path(objects_dir, expected_objects):
        raise BackupError("external Git object directory is forbidden")
    _require_safe_path(root, objects_dir, must_exist=True)

    ok, bare_raw, bare_error = _run_bound_git(
        root, git_dir, ["rev-parse", "--is-bare-repository"], timeout=15
    )
    if not ok:
        raise BackupError(
            "Git repository probe failed (bare): "
            + _decode_git_output(bare_error or bare_raw).strip()
        )
    if _decode_git_output(bare_raw).strip() != "false":
        raise BackupError("bare Git repositories are forbidden")
    bare_config_rc, bare_config_raw, bare_config_error = _run_bound_git_status(
        root, git_dir, ["config", "--local", "--bool", "--get", "core.bare"], timeout=15
    )
    if bare_config_rc == 0:
        if _decode_git_output(bare_config_raw).strip() != "false":
            raise BackupError("core.bare=true is forbidden")
    elif bare_config_rc != 1:
        raise BackupError(
            "Git repository probe failed (core.bare): "
            + _decode_git_output(bare_config_error or bare_config_raw).strip()
        )
    worktree_rc, worktree_raw, worktree_error = _run_bound_git_status(
        root, git_dir, ["config", "--local", "--get", "core.worktree"], timeout=15
    )
    if worktree_rc == 0:
        raise BackupError("core.worktree is forbidden for standalone backup")
    if worktree_rc != 1:
        raise BackupError(
            "Git repository probe failed (core.worktree): "
            + _decode_git_output(worktree_error or worktree_raw).strip()
        )
    for forbidden_key, detail in (
        ("extensions.partialClone", "partial-clone/promisor object stores are forbidden"),
        ("extensions.worktreeConfig", "worktree-specific Git config is forbidden"),
    ):
        value_rc, value_raw, value_error = _run_bound_git_status(
            root,
            git_dir,
            ["config", "--local", "--get", forbidden_key],
            timeout=15,
        )
        if value_rc == 0:
            raise BackupError(detail)
        if value_rc != 1:
            raise BackupError(
                f"Git repository probe failed ({forbidden_key}): "
                + _decode_git_output(value_error or value_raw).strip()
            )
    promisor_rc, promisor_raw, promisor_error = _run_bound_git_status(
        root,
        git_dir,
        ["config", "--local", "--get-regexp", r"^remote\..*\.promisor$"],
        timeout=15,
    )
    if promisor_rc == 0:
        raise BackupError("partial-clone/promisor remotes are forbidden")
    if promisor_rc != 1:
        raise BackupError(
            "Git repository probe failed (promisor remotes): "
            + _decode_git_output(promisor_error or promisor_raw).strip()
        )

    for alternate_name in ("alternates", "http-alternates"):
        alternate = objects_dir / "info" / alternate_name
        if alternate.exists() or alternate.is_symlink():
            raw = _stable_read_bytes(alternate, trusted_root=root, max_bytes=MAX_GIT_CONTROL_BYTES)
            if raw.strip():
                raise BackupError("Git object alternates are forbidden for standalone backup")

    object_format = _decode_git_output(required("rev-parse", "--show-object-format")).strip()
    if object_format not in {"sha1", "sha256"}:
        raise BackupError(f"unsupported Git object format: {object_format}")
    head = _decode_git_output(required("rev-parse", "--verify", "HEAD^{commit}")).strip()
    expected_hex = 40 if object_format == "sha1" else 64
    if len(head) != expected_hex or any(char not in "0123456789abcdef" for char in head):
        raise BackupError("Git HEAD is not a canonical commit object id")
    identity = {
        "git_dir": str(expected_git_dir),
        "common_dir": str(expected_git_dir),
        "objects_dir": str(expected_objects),
        "object_format": object_format,
    }
    return {
        "status": "exact",
        "root": str(root),
        "head": head,
        "identity": identity,
        "identity_sha256": hashlib.sha256(_canonical_bytes(identity)).hexdigest(),
    }


def _git_scope_head(root: Path) -> tuple[str, dict[str, Any]]:
    repository = _probe_standalone_repository(root)
    if repository.get("status") != "exact":
        raise BackupError("Git authorization requires a standalone repository with HEAD")
    return str(repository["head"]), dict(repository["identity"])


def _git_file_mode(path: Path) -> str:
    mode = path.stat().st_mode
    return "100755" if mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH) else "100644"


def _capture_backup_scope(
    project_root: str | Path,
    chapter_num: int,
    allowlist: List[str],
) -> tuple[dict[str, Any], dict[str, bytes | None]]:
    root = _safe_project_root(project_root)
    paths = _canonical_allowlist(root, allowlist)
    facts: list[dict[str, Any]] = []
    captured: dict[str, bytes | None] = {}
    for relative in paths:
        target = _require_safe_path(root, root / relative, must_exist=False, regular_file=True)
        if target.is_file():
            raw = _stable_read_bytes(target, trusted_root=root, max_bytes=MAX_ALLOWLIST_FILE_BYTES)
            captured[relative] = raw
            facts.append(
                {
                    "path": relative,
                    "state": "file",
                    "mode": _git_file_mode(target),
                    "size": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                }
            )
        else:
            captured[relative] = None
            facts.append({"path": relative, "state": "missing"})
    head, repository = _git_scope_head(root)
    scope = {
        "schema": BACKUP_RECEIPT_SCHEMA,
        "root": str(root),
        "chapter": int(chapter_num),
        "allowlist": paths,
        "repository": repository,
        "base_head": head,
        "path_facts": facts,
    }
    return scope, captured


def _backup_scope(
    project_root: str | Path,
    chapter_num: int,
    allowlist: List[str],
) -> dict[str, Any]:
    scope, _ = _capture_backup_scope(project_root, chapter_num, allowlist)
    return scope


def _authorization_token_for_scope(scope: Mapping[str, Any]) -> str:
    return AUTHORIZATION_PREFIX + hashlib.sha256(_canonical_bytes(dict(scope))).hexdigest()


def build_git_backup_authorization_token(
    project_root: str | Path,
    chapter_num: int,
    allowlist: List[str],
) -> str:
    """Bind an explicit user decision to one root/chapter/allowlist tuple."""

    return _authorization_token_for_scope(_backup_scope(project_root, chapter_num, allowlist))


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _backup_choice_request_from_scope(scope_payload: Mapping[str, Any]) -> dict[str, Any]:
    chapter_num = int(scope_payload["chapter"])
    challenge = _authorization_token_for_scope(scope_payload)
    scope = hashlib.sha256(
        _canonical_bytes(
            {**dict(scope_payload), "challenge": challenge}
        )
    ).hexdigest()
    visible_allowlist = json.dumps(
        list(scope_payload["allowlist"]), ensure_ascii=False, separators=(",", ":")
    )
    return build_choice_request(
        [
            {
                "id": "git_backup",
                "prompt": (
                    f"是否为第 {int(chapter_num)} 章创建一次性 Git 备份？"
                    f"\nHEAD={scope_payload['base_head']}"
                    f"\nexact_allowlist={visible_allowlist}"
                    f"\nscope={scope}"
                ),
                "options": [
                    {
                        "id": "decline",
                        "label": "不创建备份",
                        "description": "保留当前 Git 状态，不创建 commit 或 tag。",
                        "recommended": True,
                    },
                    {
                        "id": "authorize_once",
                        "label": "授权一次",
                        "description": "只备份列出的 allowlist，并消费本次授权。",
                        "recommended": False,
                    },
                ],
            }
        ],
        transport="numbered_fallback",
    )


def _backup_choice_request(
    project_root: str | Path,
    chapter_num: int,
    allowlist: List[str],
) -> dict[str, Any]:
    return _backup_choice_request_from_scope(_backup_scope(project_root, chapter_num, allowlist))


def build_git_backup_decision_marker(
    project_root: str | Path,
    chapter_num: int,
    allowlist: List[str],
) -> str:
    """Return the exact finite-choice marker that must precede the user answer."""

    return BACKUP_DECISION_PREFIX + _canonical_bytes(
        _backup_choice_request(project_root, chapter_num, allowlist)
    ).decode("utf-8")


def _message_text(payload: Mapping[str, Any]) -> str | None:
    if payload.get("type") != "message":
        return None
    content = payload.get("content")
    if not isinstance(content, list):
        return None
    pieces = [
        str(item.get("text") or "")
        for item in content
        if isinstance(item, Mapping) and item.get("type") in {"input_text", "output_text", "text"}
    ]
    return "".join(pieces) if pieces else None


def _read_decision_rollout(path: Path) -> bytes:
    if not path.is_absolute():
        raise BackupError("decision rollout path must be absolute")
    return _stable_read_bytes(
        path,
        trusted_root=TRUSTED_CODEX_SESSIONS_ROOT,
        max_bytes=MAX_DECISION_ROLLOUT_BYTES,
    )


def _canonical_nonzero_uuid(value: object, *, label: str) -> str:
    raw = str(value or "")
    try:
        parsed = UUID(raw)
    except (ValueError, AttributeError) as exc:
        raise BackupError(f"{label} must be a canonical nonzero UUID") from exc
    canonical = str(parsed)
    if raw != canonical or parsed.int == 0:
        raise BackupError(f"{label} must be a canonical nonzero UUID")
    return canonical


def _require_current_parent_decision_rollout(thread_id: str, rollout: Path) -> str:
    supplied = os.environ.get("CODEX_THREAD_ID")
    current_thread = _canonical_nonzero_uuid(supplied, label="CODEX_THREAD_ID")
    claimed_thread = _canonical_nonzero_uuid(thread_id, label="decision thread_id")
    if claimed_thread != current_thread:
        raise BackupError("backup decision does not belong to the current Codex task")
    root = _absolute_lexical(TRUSTED_CODEX_SESSIONS_ROOT)
    _require_safe_path(root, root, must_exist=True)
    matches: list[Path] = []
    def traversal_error(exc: OSError) -> None:
        raise BackupError(f"cannot inspect trusted Codex sessions safely: {exc}")

    for current_raw, dirs, files in os.walk(
        root, followlinks=False, onerror=traversal_error
    ):
        current = Path(current_raw)
        for name in dirs:
            if _is_reparse_point(current / name):
                raise BackupError("trusted Codex sessions root contains a reparse directory")
        for name in files:
            if current_thread in name and name.lower().endswith(".jsonl"):
                matches.append(current / name)
    if len(matches) != 1 or matches[0].resolve(strict=True) != rollout.resolve(strict=True):
        raise BackupError("CODEX_THREAD_ID must uniquely identify the decision rollout")
    return current_thread


def build_git_backup_decision_receipt(
    project_root: str | Path,
    chapter_num: int,
    allowlist: List[str],
    *,
    rollout_path: str | Path,
    thread_id: str,
) -> dict[str, Any]:
    """Verify one parent rollout user answer and build a scoped one-use receipt."""

    root = _safe_project_root(project_root)
    scope_payload = _backup_scope(root, chapter_num, allowlist)
    paths = list(scope_payload["allowlist"])
    challenge = _authorization_token_for_scope(scope_payload)
    choice_request = _backup_choice_request_from_scope(scope_payload)
    marker = BACKUP_DECISION_PREFIX + _canonical_bytes(choice_request).decode("utf-8")
    supplied_rollout = Path(rollout_path)
    if not supplied_rollout.is_absolute():
        raise BackupError("decision rollout path must be absolute")
    rollout = _absolute_lexical(supplied_rollout)
    try:
        rollout.relative_to(_absolute_lexical(TRUSTED_CODEX_SESSIONS_ROOT))
    except ValueError as exc:
        raise BackupError("decision rollout must stay under the trusted Codex sessions root") from exc
    current_thread_id = _require_current_parent_decision_rollout(thread_id, rollout)
    if current_thread_id not in rollout.name or rollout.suffix.lower() != ".jsonl":
        raise BackupError("decision rollout filename must identify the parent task")
    raw = _read_decision_rollout(rollout)
    try:
        raw.decode("utf-8")
        events: list[Any] = []
        event_end_offsets: list[int] = []
        offset = 0
        for raw_line in raw.splitlines(keepends=True):
            offset += len(raw_line)
            if not raw_line.strip():
                continue
            events.append(json.loads(raw_line.decode("utf-8")))
            event_end_offsets.append(offset)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupError("decision rollout must be UTF-8 JSONL") from exc
    sessions = [event for event in events if isinstance(event, Mapping) and event.get("type") == "session_meta"]
    if len(sessions) != 1 or not isinstance(sessions[0].get("payload"), Mapping):
        raise BackupError("decision rollout must contain one session_meta")
    session_payload = sessions[0]["payload"]
    if session_payload.get("id") != current_thread_id:
        raise BackupError("decision rollout thread identity mismatch")
    source = session_payload.get("source")
    parent_thread_id = session_payload.get("parent_thread_id")
    if (
        parent_thread_id is not None and parent_thread_id != ""
        or (isinstance(source, Mapping) and source.get("subagent") is not None)
    ):
        raise BackupError("backup decision rollout must be the current top-level Codex task")
    marker_index = -1
    selected_answers: list[tuple[int, str, dict[str, Any]]] = []
    for index, event in enumerate(events):
        if not isinstance(event, Mapping) or event.get("type") != "response_item":
            continue
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            continue
        text = _message_text(payload)
        if text is None:
            continue
        if payload.get("role") == "assistant" and marker in text.splitlines():
            if marker_index >= 0:
                raise BackupError("decision rollout contains duplicate scope markers")
            marker_index = index
        elif payload.get("role") == "user" and marker_index >= 0 and index > marker_index:
            answer = text.strip()
            resolution = resolve_choice(choice_request, answer)
            if resolution.get("status") == "selected":
                selected_answers.append((index, answer, resolution))
    if marker_index < 0 or not selected_answers:
        raise BackupError("decision rollout lacks the scope marker or a later selected user answer")
    if len(selected_answers) != 1:
        raise BackupError("decision rollout contains duplicate or superseding selected answers")
    answer_index, _answer, resolution = selected_answers[0]
    if (
        resolution.get("status") != "selected"
        or resolution.get("write_allowed") is not True
        or resolution.get("selected_branches") != {"git_backup": "authorize_once"}
    ):
        raise BackupError("user did not select the one-use Git backup branch")
    rollout_prefix_bytes = event_end_offsets[answer_index]
    rollout_prefix = raw[:rollout_prefix_bytes]
    receipt = {
        "schema_version": BACKUP_DECISION_RECEIPT_SCHEMA,
        "project_root": str(root),
        "chapter": int(chapter_num),
        "allowlist": paths,
        "repository": scope_payload["repository"],
        "base_head": scope_payload["base_head"],
        "path_facts": scope_payload["path_facts"],
        "scope_sha256": hashlib.sha256(_canonical_bytes(scope_payload)).hexdigest(),
        "scope_challenge": challenge,
        "choice_schema_version": choice_request["schema_version"],
        "choice_request_id": choice_request["request_id"],
        "selected_branch": "authorize_once",
        "rollout_path": str(rollout),
        # The live parent rollout keeps growing after the user answers. Bind
        # exactly the immutable prefix through that answer so append-only
        # progress is accepted while any prior mutation is rejected.
        "rollout_prefix_bytes": rollout_prefix_bytes,
        "rollout_prefix_sha256": hashlib.sha256(rollout_prefix).hexdigest(),
        "thread_id": current_thread_id,
    }
    receipt["receipt_sha256"] = hashlib.sha256(_canonical_bytes(receipt)).hexdigest()
    return receipt


def verify_git_backup_decision_receipt(
    project_root: str | Path,
    chapter_num: int,
    allowlist: List[str],
    receipt: Mapping[str, Any] | str | Path | None,
) -> dict[str, Any]:
    """Rebuild trusted rollout evidence; self-reported resolution fields never suffice."""

    if receipt is None:
        raise BackupError("a matching Codex user-decision receipt is required")
    if isinstance(receipt, (str, Path)):
        receipt_path = Path(receipt)
        if not receipt_path.is_absolute():
            raise BackupError("decision receipt path must be absolute")
        loaded = _stable_read_json(
            receipt_path,
            trusted_root=_safe_project_root(project_root),
            max_bytes=MAX_DECISION_RECEIPT_BYTES,
        )
    else:
        loaded = dict(receipt)
    if not isinstance(loaded, dict) or loaded.get("schema_version") != BACKUP_DECISION_RECEIPT_SCHEMA:
        raise BackupError("decision receipt schema is invalid")
    claimed = str(loaded.get("receipt_sha256") or "")
    unsigned = dict(loaded)
    unsigned.pop("receipt_sha256", None)
    if claimed != hashlib.sha256(_canonical_bytes(unsigned)).hexdigest():
        raise BackupError("decision receipt hash mismatch")
    rebuilt = build_git_backup_decision_receipt(
        project_root,
        chapter_num,
        allowlist,
        rollout_path=str(loaded.get("rollout_path") or ""),
        thread_id=str(loaded.get("thread_id") or ""),
    )
    if rebuilt != loaded:
        raise BackupError("decision receipt is stale or does not match this backup scope")
    return rebuilt


def _authorization_paths(
    project_root: Path,
    receipt_sha256: str,
    *,
    create: bool,
) -> tuple[Path, Path]:
    if len(receipt_sha256) != 64 or any(char not in "0123456789abcdef" for char in receipt_sha256):
        raise BackupError("decision receipt hash is invalid")
    root = project_root.resolve()
    registry = root / ".webnovel" / "backup-authorizations"
    if create:
        _safe_mkdir_chain(root, registry)
    elif not registry.is_dir():
        raise BackupError("backup authorization registry is missing")
    _require_safe_path(root, registry, must_exist=True)
    state_path = registry / f"{receipt_sha256}.json"
    # One project-level lock serializes HEAD/path capture, object creation,
    # registry transitions, and tag publication across every receipt.
    lock_path = registry / "project-backup.lock"
    _require_safe_path(root, state_path, must_exist=False, regular_file=True)
    _require_single_link_control_file(root, lock_path, must_exist=False)
    return state_path, lock_path


def _decision_registry_binding(decision: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "receipt_sha256": str(decision.get("receipt_sha256") or ""),
        "project_root": str(decision.get("project_root") or ""),
        "chapter": decision.get("chapter"),
        "allowlist": decision.get("allowlist"),
        "repository": decision.get("repository"),
        "base_head": decision.get("base_head"),
        "path_facts": decision.get("path_facts"),
        "scope_sha256": decision.get("scope_sha256"),
        "scope_challenge": decision.get("scope_challenge"),
        "choice_request_id": decision.get("choice_request_id"),
        "thread_id": decision.get("thread_id"),
    }


def _decision_scope_payload(decision: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": BACKUP_RECEIPT_SCHEMA,
        "root": str(decision.get("project_root") or ""),
        "chapter": decision.get("chapter"),
        "allowlist": decision.get("allowlist"),
        "repository": decision.get("repository"),
        "base_head": decision.get("base_head"),
        "path_facts": decision.get("path_facts"),
    }


def _validate_backup_result_shape(result: Mapping[str, Any], decision: Mapping[str, Any]) -> None:
    common = {
        "schema_version",
        "project_root",
        "chapter",
        "created_at",
        "ok",
        "status",
        "code",
        "allowlist",
        "head",
        "decision_receipt_sha256",
        "decision_receipt",
    }
    completed_extra = {
        "changed_paths",
        "tree",
        "commit",
        "tag",
        "commit_message",
        "path_objects",
        "authorization_token_sha256",
    }
    status_value = result.get("status")
    code = result.get("code")
    expected_keys = (
        common | completed_extra
        if status_value == "completed" and code == "git_backup_created"
        else common
        if status_value == "skipped" and code == "no_allowlisted_changes"
        else set()
    )
    if not expected_keys or set(result) != expected_keys:
        raise BackupError("backup registry result schema is invalid")
    if (
        result.get("schema_version") != BACKUP_RECEIPT_SCHEMA
        or result.get("project_root") != decision.get("project_root")
        or result.get("chapter") != decision.get("chapter")
        or result.get("allowlist") != decision.get("allowlist")
        or result.get("head") != decision.get("base_head")
        or result.get("ok") is not True
        or result.get("decision_receipt_sha256") != decision.get("receipt_sha256")
        or result.get("decision_receipt") != dict(decision)
        or not isinstance(result.get("created_at"), str)
    ):
        raise BackupError("backup registry result belongs to a different authorization scope")
    if status_value != "completed":
        return
    allowlist = list(decision.get("allowlist") or [])
    changed = result.get("changed_paths")
    if (
        not isinstance(changed, list)
        or not changed
        or changed != sorted(set(changed))
        or any(path not in allowlist for path in changed)
    ):
        raise BackupError("backup registry changed paths are invalid")
    object_format = str((decision.get("repository") or {}).get("object_format") or "")
    oid_length = 40 if object_format == "sha1" else 64 if object_format == "sha256" else 0
    for field in ("tree", "commit"):
        value = str(result.get(field) or "")
        if len(value) != oid_length or any(char not in "0123456789abcdef" for char in value):
            raise BackupError(f"backup registry {field} is invalid")
    expected_tag = f"ch{int(decision['chapter']):04d}"
    if result.get("tag") != expected_tag or not isinstance(result.get("commit_message"), str):
        raise BackupError("backup registry tag/message is invalid")
    expected_token_hash = hashlib.sha256(
        str(decision.get("scope_challenge") or "").encode("utf-8")
    ).hexdigest()
    if result.get("authorization_token_sha256") != expected_token_hash:
        raise BackupError("backup registry authorization token binding is invalid")
    objects = result.get("path_objects")
    if not isinstance(objects, list) or len(objects) != len(allowlist):
        raise BackupError("backup registry path objects are invalid")
    object_paths: list[str] = []
    for item in objects:
        if not isinstance(item, Mapping) or item.get("path") not in allowlist:
            raise BackupError("backup registry path object is invalid")
        object_paths.append(str(item["path"]))
        if item.get("state") == "missing":
            if set(item) != {"path", "state"}:
                raise BackupError("missing path object carries unsupported fields")
        elif item.get("state") == "file":
            if set(item) != {"path", "state", "mode", "oid"}:
                raise BackupError("file path object carries unsupported fields")
            oid = str(item.get("oid") or "")
            if (
                item.get("mode") not in {"100644", "100755"}
                or len(oid) != oid_length
                or any(char not in "0123456789abcdef" for char in oid)
            ):
                raise BackupError("file path object mode/object id is invalid")
        else:
            raise BackupError("backup registry path object state is invalid")
    if sorted(object_paths) != sorted(allowlist) or len(set(object_paths)) != len(object_paths):
        raise BackupError("backup registry path object set is invalid")


def _validate_authorization_state(state: Mapping[str, Any], decision: Mapping[str, Any]) -> None:
    status_value = state.get("status")
    common_keys = {
        "schema_version",
        "binding",
        "status",
        "attempts",
        "created_at",
        "updated_at",
    }
    optional_keys: set[str] = set()
    if status_value == "completed":
        optional_keys = {"result"}
    elif status_value == "failed-retryable":
        optional_keys = {"last_error"}
        if "pending_result" in state:
            optional_keys.add("pending_result")
    elif status_value == "in_progress":
        if "pending_result" in state:
            optional_keys.add("pending_result")
    elif status_value != "claimed":
        raise BackupError("backup authorization registry status is invalid")
    if set(state) != common_keys | optional_keys:
        raise BackupError("backup authorization registry fields are invalid")
    if state.get("schema_version") != BACKUP_AUTHORIZATION_REGISTRY_SCHEMA:
        raise BackupError("backup authorization registry schema is invalid")
    if state.get("binding") != _decision_registry_binding(decision):
        raise BackupError("backup authorization registry belongs to a different scope or receipt")
    if (
        not isinstance(state.get("attempts"), int)
        or isinstance(state.get("attempts"), bool)
        or int(state["attempts"]) < 0
        or not isinstance(state.get("created_at"), str)
        or not isinstance(state.get("updated_at"), str)
    ):
        raise BackupError("backup authorization registry counters/timestamps are invalid")
    attempts = int(state["attempts"])
    if (status_value == "claimed" and attempts != 0) or (
        status_value != "claimed" and attempts < 1
    ):
        raise BackupError("backup authorization registry attempt counter is inconsistent")
    if status_value == "failed-retryable" and not str(state.get("last_error") or "").strip():
        raise BackupError("retryable backup registry state lacks an error")
    pending = state.get("pending_result")
    if pending is not None:
        if not isinstance(pending, Mapping):
            raise BackupError("backup authorization pending result is invalid")
        _validate_backup_result_shape(pending, decision)
        if pending.get("status") != "completed":
            raise BackupError("only a completed Git snapshot may be pending tag publication")
    result = state.get("result")
    if result is not None:
        if not isinstance(result, Mapping):
            raise BackupError("backup authorization terminal result is invalid")
        _validate_backup_result_shape(result, decision)


def _read_authorization_state(
    project_root: Path,
    receipt_sha256: str,
) -> dict[str, Any] | None:
    state_path, _ = _authorization_paths(project_root, receipt_sha256, create=False)
    if not state_path.is_file():
        return None
    return _stable_read_json(
        state_path,
        trusted_root=project_root.resolve(),
        max_bytes=MAX_DECISION_RECEIPT_BYTES,
    )


def _write_authorization_state(
    project_root: Path,
    state_path: Path,
    state: Mapping[str, Any],
) -> dict[str, Any]:
    payload = dict(state)
    payload["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    _atomic_registry_write(project_root.resolve(), state_path, payload)
    return payload


def _claim_authorization_state(
    project_root: Path,
    state_path: Path,
    decision: Mapping[str, Any],
) -> dict[str, Any]:
    receipt_sha = str(decision.get("receipt_sha256") or "")
    state = _read_authorization_state(project_root, receipt_sha)
    if state is None:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        state = {
            "schema_version": BACKUP_AUTHORIZATION_REGISTRY_SCHEMA,
            "binding": _decision_registry_binding(decision),
            "status": "claimed",
            "attempts": 0,
            "created_at": now,
            "updated_at": now,
        }
        state = _write_authorization_state(project_root, state_path, state)
    _validate_authorization_state(state, decision)
    return state


def _transition_authorization_state(
    project_root: Path,
    state_path: Path,
    state: Mapping[str, Any],
    *,
    status_value: str,
    error: str | None = None,
    pending_result: Mapping[str, Any] | None = None,
    result: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if status_value not in {"claimed", "in_progress", "completed", "failed-retryable"}:
        raise BackupError("invalid authorization registry transition")
    allowed = {
        "claimed": {"in_progress", "failed-retryable"},
        "in_progress": {"in_progress", "completed", "failed-retryable"},
        "failed-retryable": {"in_progress", "failed-retryable"},
        "completed": set(),
    }
    current_status = str(state.get("status") or "")
    if status_value not in allowed.get(current_status, set()):
        raise BackupError(f"invalid authorization registry transition: {current_status} -> {status_value}")
    updated = dict(state)
    updated["status"] = status_value
    if status_value == "in_progress" and state.get("status") != "in_progress":
        updated["attempts"] = int(updated.get("attempts") or 0) + 1
    if error is not None:
        updated["last_error"] = error
    elif status_value in {"in_progress", "completed"}:
        updated.pop("last_error", None)
    if pending_result is not None:
        updated["pending_result"] = dict(pending_result)
    if result is not None:
        updated["result"] = dict(result)
        updated.pop("pending_result", None)
    return _write_authorization_state(project_root, state_path, updated)


def read_git_backup_authorization_state(
    project_root: str | Path,
    receipt_sha256: str,
) -> dict[str, Any]:
    """Read one safe registry record for status/resume verification."""

    state = _read_authorization_state(_safe_project_root(project_root), receipt_sha256)
    if state is None:
        raise BackupError("backup authorization registry record is missing")
    return state


class GitBackupManager:
    """Fail-closed standalone Git snapshot manager."""

    def __init__(self, project_root: str):
        self.project_root = _safe_project_root(project_root)
        self.git_dir = self.project_root / ".git"
        self.git_available = is_git_available()
        self.repository_error = ""
        try:
            self.repository = _probe_standalone_repository(self.project_root)
        except BackupError as exc:
            self.repository = {"status": "error", "root": str(self.project_root)}
            self.repository_error = str(exc)
        self.repository_status = str(self.repository.get("status") or "error")
        self.git_toplevel: Optional[Path] = (
            self.project_root if self.repository_status == "exact" else None
        )
        self.exact_git_root = self.repository_status == "exact"

    def _refresh_repository(self) -> dict[str, Any]:
        current = _probe_standalone_repository(self.project_root)
        if current.get("status") != "exact":
            raise BackupError("standalone Git repository disappeared during backup")
        if self.repository_status == "exact" and (
            current.get("identity") != self.repository.get("identity")
        ):
            raise BackupError("Git repository identity changed during backup")
        self.repository = current
        self.repository_status = "exact"
        self.exact_git_root = True
        self.git_toplevel = self.project_root
        return current

    def _run_git_command_bytes(
        self,
        args: List[str],
        check: bool = True,
        *,
        index_file: Path | None = None,
        input_bytes: bytes | None = None,
    ) -> Tuple[bool, bytes, bytes]:
        if not self.exact_git_root:
            return False, b"", b"novel root is not an exact standalone Git repository"
        ok, stdout, stderr = _run_bound_git(
            self.project_root,
            self.git_dir,
            args,
            input_bytes=input_bytes,
            index_file=index_file,
        )
        if check and not ok:
            message = _decode_git_output(stderr or stdout).strip()
            raise BackupError(f"git {' '.join(args)} failed: {message}")
        return ok, stdout, stderr

    def _run_git_command(
        self,
        args: List[str],
        check: bool = True,
        *,
        env: Optional[dict] = None,
        index_file: Path | None = None,
        input_bytes: bytes | None = None,
    ) -> Tuple[bool, str, str]:
        if env is not None:
            raise BackupError("caller-supplied Git environment is forbidden")
        ok, stdout, stderr = self._run_git_command_bytes(
            args,
            check=check,
            index_file=index_file,
            input_bytes=input_bytes,
        )
        return ok, _decode_git_output(stdout), _decode_git_output(stderr)

    @staticmethod
    def _format_git_output(stdout: str, stderr: str) -> str:
        return "\n".join(part.strip() for part in (stderr, stdout) if part.strip())

    def _read_tag(self, tag_name: str) -> str | None:
        ok, stdout, _ = self._run_git_command(
            ["show-ref", "--verify", "--hash", f"refs/tags/{tag_name}"], check=False
        )
        return stdout.strip() if ok and stdout.strip() else None

    def _tree_entry(self, tree: str, path: str) -> dict[str, str] | None:
        ok, stdout, stderr = self._run_git_command_bytes(
            ["ls-tree", "-z", tree, "--", path], check=False
        )
        if not ok:
            raise BackupError(_decode_git_output(stderr or stdout).strip() or "git ls-tree failed")
        records = [item for item in stdout.split(b"\0") if item]
        if not records:
            return None
        if len(records) != 1 or b"\t" not in records[0]:
            raise BackupError("Git tree contains an ambiguous allowlisted path")
        metadata, raw_path = records[0].split(b"\t", 1)
        parts = metadata.split()
        if len(parts) != 3:
            raise BackupError("Git tree entry is malformed")
        try:
            decoded_path = raw_path.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BackupError("Git tree path is not UTF-8") from exc
        return {
            "mode": parts[0].decode("ascii"),
            "type": parts[1].decode("ascii"),
            "oid": parts[2].decode("ascii"),
            "path": decoded_path,
        }

    def _require_current_authorized_scope(
        self, decision: Mapping[str, Any]
    ) -> tuple[dict[str, Any], dict[str, bytes | None]]:
        scope, captured = _capture_backup_scope(
            self.project_root,
            int(decision["chapter"]),
            list(decision["allowlist"]),
        )
        if scope != _decision_scope_payload(decision):
            raise BackupError("Git HEAD, repository identity, or allowlisted bytes changed after authorization")
        return scope, captured

    def _verify_commit_object(
        self,
        *,
        commit: str,
        tree: str,
        parent: str,
        message: str,
    ) -> None:
        ok, raw, stderr = self._run_git_command_bytes(["cat-file", "commit", commit], check=False)
        if not ok:
            raise BackupError(_decode_git_output(stderr or raw).strip() or "backup commit is missing")
        try:
            header_raw, message_raw = raw.split(b"\n\n", 1)
            headers = header_raw.decode("utf-8").splitlines()
        except (ValueError, UnicodeDecodeError) as exc:
            raise BackupError("backup commit object is malformed") from exc
        trees = [line[5:] for line in headers if line.startswith("tree ")]
        parents = [line[7:] for line in headers if line.startswith("parent ")]
        if trees != [tree] or parents != [parent] or message_raw != message.encode("utf-8") + b"\n":
            raise BackupError("backup commit tree/parent/message does not match its receipt")

    def _verify_path_objects(
        self,
        *,
        tree: str,
        captured: Mapping[str, bytes | None],
        path_objects: list[Mapping[str, Any]],
    ) -> None:
        objects_by_path = {str(item["path"]): item for item in path_objects}
        if set(objects_by_path) != set(captured):
            raise BackupError("backup path object set differs from its authorized allowlist")
        for path, raw in captured.items():
            expected = objects_by_path[path]
            entry = self._tree_entry(tree, path)
            if raw is None:
                if entry is not None or expected.get("state") != "missing":
                    raise BackupError("an authorized missing path is present in the backup tree")
                continue
            ok, oid_raw, stderr = self._run_git_command_bytes(
                ["hash-object", "--no-filters", "--stdin"],
                check=False,
                input_bytes=raw,
            )
            if not ok:
                raise BackupError(_decode_git_output(stderr or oid_raw).strip() or "git hash-object failed")
            oid = _decode_git_output(oid_raw).strip()
            if (
                entry is None
                or entry["type"] != "blob"
                or entry["path"] != path
                or entry["mode"] != expected.get("mode")
                or entry["oid"] != oid
                or expected.get("oid") != oid
            ):
                raise BackupError("backup tree blob does not match the authorized bytes")

    def _verify_backup_result(
        self,
        decision: Mapping[str, Any],
        result: Mapping[str, Any],
        *,
        require_tag: bool,
    ) -> None:
        _validate_backup_result_shape(result, decision)
        self._refresh_repository()
        _, captured = self._require_current_authorized_scope(decision)
        if result.get("status") == "skipped":
            ok, stdout, stderr = self._run_git_command_bytes(
                [
                    "status",
                    "--porcelain=v1",
                    "-z",
                    "--untracked-files=all",
                    "--",
                    *list(decision["allowlist"]),
                ],
                check=False,
            )
            if not ok or stdout:
                raise BackupError(_decode_git_output(stderr or stdout).strip() or "no-change receipt is stale")
            return
        commit = str(result["commit"])
        tree = str(result["tree"])
        self._verify_commit_object(
            commit=commit,
            tree=tree,
            parent=str(decision["base_head"]),
            message=str(result["commit_message"]),
        )
        ok, changed_raw, stderr = self._run_git_command_bytes(
            ["diff-tree", "--no-commit-id", "--name-only", "-r", "-z", commit],
            check=False,
        )
        if not ok:
            raise BackupError(_decode_git_output(stderr or changed_raw).strip() or "git diff-tree failed")
        try:
            changed = sorted(item.decode("utf-8") for item in changed_raw.split(b"\0") if item)
        except UnicodeDecodeError as exc:
            raise BackupError("backup commit contains a non-UTF-8 path") from exc
        if changed != list(result["changed_paths"]):
            raise BackupError("backup commit changed paths differ from its receipt")
        self._verify_path_objects(
            tree=tree,
            captured=captured,
            path_objects=[dict(item) for item in result["path_objects"]],
        )
        if require_tag and self._read_tag(str(result["tag"])) != commit:
            raise BackupError("backup tag does not resolve to the receipted commit")

    def _publish_tag(self, tag_name: str, commit: str, object_format: str) -> None:
        current = self._read_tag(tag_name)
        if current is not None:
            if current != commit:
                raise BackupError("backup tag exists at a different commit")
            return
        zero = "0" * (40 if object_format == "sha1" else 64)
        ok, stdout, stderr = self._run_git_command(
            ["update-ref", f"refs/tags/{tag_name}", commit, zero], check=False
        )
        if not ok:
            current = self._read_tag(tag_name)
            if current != commit:
                raise BackupError(self._format_git_output(stdout, stderr) or "git update-ref failed")
        if self._read_tag(tag_name) != commit:
            raise BackupError("backup tag CAS failed exact readback")

    def _build_snapshot_result(
        self,
        *,
        base: Mapping[str, Any],
        decision: Mapping[str, Any],
        captured: Mapping[str, bytes | None],
        chapter_title: str,
        authorization_token: str,
    ) -> dict[str, Any]:
        managed_tmp = self.project_root / ".webnovel" / "backup-authorizations" / "tmp"
        _safe_mkdir_chain(self.project_root, managed_tmp)
        _require_safe_path(self.project_root, managed_tmp, must_exist=True)
        fd, index_name = tempfile.mkstemp(
            prefix="webnovel-backup-index-", suffix=".tmp", dir=managed_tmp
        )
        os.close(fd)
        index_path = Path(index_name)
        try:
            _require_safe_path(
                self.project_root, index_path, must_exist=True, regular_file=True
            )
            index_path.unlink()
            ok, stdout, stderr = self._run_git_command(
                ["read-tree", str(decision["base_head"])], check=False, index_file=index_path
            )
            if not ok:
                raise BackupError(self._format_git_output(stdout, stderr) or "git read-tree failed")
            path_objects: list[dict[str, Any]] = []
            facts = {str(item["path"]): item for item in decision["path_facts"]}
            for path in list(decision["allowlist"]):
                raw = captured[path]
                if raw is None:
                    ok, stdout, stderr = self._run_git_command(
                        ["update-index", "--force-remove", "--", path],
                        check=False,
                        index_file=index_path,
                    )
                    if not ok:
                        raise BackupError(self._format_git_output(stdout, stderr) or "git update-index remove failed")
                    path_objects.append({"path": path, "state": "missing"})
                    continue
                ok, oid_raw, error_raw = self._run_git_command_bytes(
                    ["hash-object", "-w", "--no-filters", "--stdin"],
                    check=False,
                    input_bytes=raw,
                )
                if not ok:
                    raise BackupError(_decode_git_output(error_raw or oid_raw).strip() or "git hash-object failed")
                oid = _decode_git_output(oid_raw).strip()
                ok, stored, error_raw = self._run_git_command_bytes(
                    ["cat-file", "blob", oid], check=False
                )
                if not ok or stored != raw:
                    raise BackupError(_decode_git_output(error_raw).strip() or "Git blob failed exact readback")
                mode = str(facts[path]["mode"])
                ok, stdout, stderr = self._run_git_command(
                    ["update-index", "--add", "--cacheinfo", mode, oid, path],
                    check=False,
                    index_file=index_path,
                )
                if not ok:
                    raise BackupError(self._format_git_output(stdout, stderr) or "git update-index failed")
                path_objects.append({"path": path, "state": "file", "mode": mode, "oid": oid})

            ok, changed_raw, error_raw = self._run_git_command_bytes(
                ["diff", "--cached", "--name-only", "-z"],
                check=False,
                index_file=index_path,
            )
            if not ok:
                raise BackupError(_decode_git_output(error_raw or changed_raw).strip() or "git diff failed")
            try:
                changed_paths = sorted(
                    item.decode("utf-8") for item in changed_raw.split(b"\0") if item
                )
            except UnicodeDecodeError as exc:
                raise BackupError("temporary index contains a non-UTF-8 path") from exc
            if any(path not in decision["allowlist"] for path in changed_paths):
                raise BackupError("temporary index contains a path outside the allowlist")
            if not changed_paths:
                return {
                    **dict(base),
                    "ok": True,
                    "status": "skipped",
                    "code": "no_allowlisted_changes",
                    "allowlist": list(decision["allowlist"]),
                    "head": str(decision["base_head"]),
                    "decision_receipt_sha256": decision["receipt_sha256"],
                    "decision_receipt": dict(decision),
                }

            ok, tree, stderr = self._run_git_command(
                ["write-tree"], check=False, index_file=index_path
            )
            tree = tree.strip()
            if not ok or not tree:
                raise BackupError(self._format_git_output(tree, stderr) or "git write-tree failed")
            self._verify_path_objects(
                tree=tree,
                captured=captured,
                path_objects=path_objects,
            )
            commit_message = f"Chapter {int(decision['chapter'])}"
            if chapter_title:
                commit_message += f": {sanitize_commit_message(chapter_title)}"
            ok, commit, stderr = self._run_git_command(
                [
                    "commit-tree",
                    tree,
                    "-p",
                    str(decision["base_head"]),
                    "-m",
                    commit_message,
                ],
                check=False,
            )
            commit = commit.strip()
            if not ok or not commit:
                raise BackupError(self._format_git_output(commit, stderr) or "git commit-tree failed")
            self._verify_commit_object(
                commit=commit,
                tree=tree,
                parent=str(decision["base_head"]),
                message=commit_message,
            )
            return {
                **dict(base),
                "ok": True,
                "status": "completed",
                "code": "git_backup_created",
                "allowlist": list(decision["allowlist"]),
                "changed_paths": changed_paths,
                "head": str(decision["base_head"]),
                "tree": tree,
                "commit": commit,
                "tag": f"ch{int(decision['chapter']):04d}",
                "commit_message": commit_message,
                "path_objects": path_objects,
                "authorization_token_sha256": hashlib.sha256(
                    authorization_token.encode("utf-8")
                ).hexdigest(),
                "decision_receipt_sha256": decision["receipt_sha256"],
                "decision_receipt": dict(decision),
            }
        finally:
            try:
                index_path.unlink()
            except OSError:
                pass

    def backup(
        self,
        chapter_num: int,
        chapter_title: str = "",
        *,
        allowlist: Optional[List[str]] = None,
        authorization_token: str = "",
        decision_receipt: Mapping[str, Any] | str | Path | None = None,
    ) -> dict:
        """Create one authorized, isolated, tag-backed snapshot."""

        base = {
            "schema_version": BACKUP_RECEIPT_SCHEMA,
            "project_root": str(self.project_root),
            "chapter": int(chapter_num),
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        if chapter_num <= 0:
            return {**base, "ok": False, "status": "failed", "code": "invalid_chapter"}
        if self.repository_status == "not_repo":
            return {
                **base,
                "ok": True,
                "status": "skipped",
                "code": "skipped_non_git",
                "detail": "the novel root has no .git entry and is explicitly non-Git",
                "allowlist": [],
            }
        if self.repository_status != "exact":
            return {
                **base,
                "ok": False,
                "status": "failed",
                "code": "git_repository_probe_failed",
                "detail": self.repository_error or "standalone Git repository probe failed",
                "allowlist": [],
            }
        try:
            paths = _canonical_allowlist(self.project_root, list(allowlist or []))
        except BackupError as exc:
            return {
                **base,
                "ok": False,
                "status": "failed",
                "code": "invalid_allowlist",
                "detail": str(exc),
                "allowlist": list(allowlist or []),
            }
        try:
            expected_token = build_git_backup_authorization_token(
                self.project_root, chapter_num, paths
            )
        except BackupError as exc:
            return {
                **base,
                "ok": False,
                "status": "failed",
                "code": "git_repository_probe_failed",
                "detail": str(exc),
                "allowlist": paths,
            }
        decision: dict[str, Any] | None = None
        decision_error = ""
        if authorization_token == expected_token:
            try:
                decision = verify_git_backup_decision_receipt(
                    self.project_root, chapter_num, paths, decision_receipt
                )
            except BackupError as exc:
                decision_error = str(exc)
        if authorization_token != expected_token or decision is None:
            return {
                **base,
                "ok": False,
                "status": "authorization_required",
                "code": "git_backup_authorization_required",
                "allowlist": paths,
                "base_head": self.repository.get("head"),
                "scope_challenge": expected_token,
                "decision_marker": build_git_backup_decision_marker(
                    self.project_root, chapter_num, paths
                ),
                "decision_receipt_required": True,
                "detail": decision_error
                or "scope challenge and Codex user-decision receipt are required",
            }
        if FileLock is None:
            return {
                **base,
                "ok": False,
                "status": "failed",
                "code": "backup_dependency_missing",
                "detail": "filelock is required for the Git backup authorization registry",
                "allowlist": paths,
            }
        try:
            state_path, lock_path = _authorization_paths(
                self.project_root, str(decision["receipt_sha256"]), create=True
            )
        except BackupError as exc:
            return {
                **base,
                "ok": False,
                "status": "failed",
                "code": "git_backup_authorization_registry_failed",
                "detail": str(exc),
                "allowlist": paths,
            }
        try:
            with FileLock(str(lock_path), timeout=10):
                _require_single_link_control_file(
                    self.project_root, lock_path, must_exist=True
                )
                _require_safe_path(self.project_root, state_path, must_exist=False, regular_file=True)
                self._refresh_repository()
                _, captured = self._require_current_authorized_scope(decision)
                state = _claim_authorization_state(self.project_root, state_path, decision)
                if state["status"] == "completed":
                    result = dict(state["result"])
                    self._verify_backup_result(
                        decision, result, require_tag=result.get("status") == "completed"
                    )
                    return result
                state = _transition_authorization_state(
                    self.project_root, state_path, state, status_value="in_progress"
                )
                try:
                    pending = state.get("pending_result")
                    if isinstance(pending, Mapping):
                        result = dict(pending)
                        self._verify_backup_result(decision, result, require_tag=False)
                        self._publish_tag(
                            str(result["tag"]),
                            str(result["commit"]),
                            str(decision["repository"]["object_format"]),
                        )
                        self._verify_backup_result(decision, result, require_tag=True)
                        _transition_authorization_state(
                            self.project_root,
                            state_path,
                            state,
                            status_value="completed",
                            result=result,
                        )
                        return result
                    tag_name = f"ch{chapter_num:04d}"
                    existing_tag = self._read_tag(tag_name)
                    if existing_tag is not None:
                        conflict = {
                            **base,
                            "ok": False,
                            "status": "conflict",
                            "code": "backup_tag_exists",
                            "tag": tag_name,
                            "existing_commit": existing_tag,
                            "allowlist": paths,
                        }
                        _transition_authorization_state(
                            self.project_root,
                            state_path,
                            state,
                            status_value="failed-retryable",
                            error="backup tag already exists",
                        )
                        return conflict
                    result = self._build_snapshot_result(
                        base=base,
                        decision=decision,
                        captured=captured,
                        chapter_title=chapter_title,
                        authorization_token=authorization_token,
                    )
                    _validate_backup_result_shape(result, decision)
                    if result["status"] == "skipped":
                        self._verify_backup_result(decision, result, require_tag=False)
                        _transition_authorization_state(
                            self.project_root,
                            state_path,
                            state,
                            status_value="completed",
                            result=result,
                        )
                        return result
                    self._verify_backup_result(decision, result, require_tag=False)
                    state = _transition_authorization_state(
                        self.project_root,
                        state_path,
                        state,
                        status_value="in_progress",
                        pending_result=result,
                    )
                    self._publish_tag(
                        str(result["tag"]),
                        str(result["commit"]),
                        str(decision["repository"]["object_format"]),
                    )
                    self._verify_backup_result(decision, result, require_tag=True)
                    _transition_authorization_state(
                        self.project_root,
                        state_path,
                        state,
                        status_value="completed",
                        result=result,
                    )
                    return result
                except BackupError as exc:
                    try:
                        _transition_authorization_state(
                            self.project_root,
                            state_path,
                            state,
                            status_value="failed-retryable",
                            error=str(exc),
                        )
                    except BackupError as registry_exc:
                        return {
                            **base,
                            "ok": False,
                            "status": "failed",
                            "code": "git_backup_failed",
                            "detail": f"{exc}; registry update failed: {registry_exc}",
                            "allowlist": paths,
                        }
                    return {
                        **base,
                        "ok": False,
                        "status": "failed",
                        "code": "git_backup_failed",
                        "detail": str(exc),
                        "allowlist": paths,
                        "retryable": True,
                    }
        except Timeout:
            return {
                **base,
                "ok": False,
                "status": "failed",
                "code": "git_backup_authorization_busy",
                "detail": "backup authorization registry is locked by another operation",
                "allowlist": paths,
            }
        except BackupError as exc:
            return {
                **base,
                "ok": False,
                "status": "failed",
                "code": "git_backup_authorization_registry_failed",
                "detail": str(exc),
                "allowlist": paths,
            }

    def rollback(self, chapter_num: int) -> bool:
        """Legacy broad rollback is intentionally unavailable in Codex."""

        _ = chapter_num
        print("❌ legacy rollback is disabled; use a separately authorized recovery transaction")
        return False

    def diff(self, chapter_a: int, chapter_b: int):
        """对比两个版本的差异（Git diff）"""

        tag_a = f"ch{chapter_a:04d}"
        tag_b = f"ch{chapter_b:04d}"

        print(f"📊 对比第 {chapter_a} 章 与 第 {chapter_b} 章的差异...\n")

        success, output, error = self._run_git_command(["diff", tag_a, tag_b, "--stat"], check=False)

        if not success:
            print(f"❌ 对比失败: {self._format_git_output(output, error)}")
            return

        print("📈 文件变更统计：")
        print(output)

        # 显示 state.json 的详细差异
        print("\n📝 state.json 详细差异：")
        success, state_diff, _ = self._run_git_command(
            ["diff", tag_a, tag_b, "--", ".webnovel/state.json"],
            check=False,
        )

        if success and state_diff:
            print(state_diff[:2000])  # 限制输出长度
            if len(state_diff) > 2000:
                print("\n...(输出过长，已截断)")
        else:
            print("(无变更)")

    def list_backups(self):
        """列出所有备份（Git log + tags）"""

        print("\n📚 备份列表（Git tags）：\n")

        # 获取所有 tags
        success, tags_output, _ = self._run_git_command(["tag", "-l", "ch*"], check=False)

        if not success or not tags_output:
            print("⚠️  暂无备份")
            return

        tags = sorted(tags_output.strip().split('\n'))

        for tag in tags:
            # 提取章节号
            chapter_num = int(tag[2:])

            # 获取该 tag 的提交信息
            success, commit_info, _ = self._run_git_command(
                ["log", tag, "-1", "--format=%h %ci %s"],
                check=False,
            )

            if success:
                print(f"📖 {tag} | {commit_info.strip()}")

        print(f"\n总计：{len(tags)} 个备份")

        # 显示最近 5 次提交
        print("\n📜 最近提交历史：\n")
        success, log_output, _ = self._run_git_command(
            ["log", "--oneline", "-5"],
            check=False,
        )

        if success:
            print(log_output)

    def create_branch(self, chapter_num: int, branch_name: str) -> bool:
        """Legacy branch mutation is intentionally unavailable in Codex."""

        _ = (chapter_num, branch_name)
        print("❌ legacy create-branch is disabled; request a separately authorized Git action")
        return False


def verify_git_backup_authorization_state(
    project_root: str | Path,
    decision: Mapping[str, Any],
) -> dict[str, Any]:
    """Strictly verify one terminal registry record and its live Git truth."""

    root = _safe_project_root(project_root)
    receipt_sha = str(decision.get("receipt_sha256") or "")
    state = _read_authorization_state(root, receipt_sha)
    if state is None:
        raise BackupError("backup authorization registry record is missing")
    _validate_authorization_state(state, decision)
    if state.get("status") != "completed" or not isinstance(state.get("result"), Mapping):
        raise BackupError("backup authorization registry is not terminal")
    manager = GitBackupManager(str(root))
    if manager.repository_status != "exact":
        raise BackupError(manager.repository_error or "standalone Git repository probe failed")
    result = dict(state["result"])
    manager._verify_backup_result(
        decision,
        result,
        require_tag=result.get("status") == "completed",
    )
    return state


def _write_cli_json_artifact(
    project_root: Path,
    output_path: str | Path,
    payload: Mapping[str, Any],
) -> None:
    path = Path(output_path)
    if not path.is_absolute():
        raise BackupError("output-json must be an absolute path")
    path = _absolute_lexical(path)
    allowed = _absolute_lexical(project_root / ".webnovel" / "tmp" / "write-runs")
    try:
        path.relative_to(allowed)
    except ValueError as exc:
        raise BackupError("output-json must stay under .webnovel/tmp/write-runs") from exc
    _require_safe_path(project_root, path.parent, must_exist=True)
    if not path.parent.is_dir():
        raise BackupError("output-json parent directory is missing")
    _require_safe_path(project_root, path, must_exist=False, regular_file=True)
    raw = json.dumps(dict(payload), ensure_ascii=False, indent=2).encode("utf-8")
    if path.is_file():
        if _stable_read_bytes(path, trusted_root=project_root, max_bytes=MAX_DECISION_RECEIPT_BYTES) != raw:
            raise BackupError("output-json already exists with different bytes")
        return
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise BackupError(f"cannot create output-json safely: {exc}") from exc
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        if _stable_read_bytes(path, trusted_root=project_root, max_bytes=MAX_DECISION_RECEIPT_BYTES) != raw:
            raise BackupError("output-json failed exact readback")
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Git 集成备份管理系统",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  # 用户明确授权后，仅备份 allowlist 文件
  python backup_manager.py --chapter 45 --allowlist "正文/第0045章.md" --authorization-token CHALLENGE --decision-receipt RECEIPT --format json

  # 查看第 20 章和第 40 章的差异
  python backup_manager.py --diff 20 40

  # 列出所有备份
  python backup_manager.py --list

  # --rollback / --create-branch 是生产硬拒绝的旧入口，不会执行 Git 写。
        """
    )

    parser.add_argument('--chapter', type=int, help='备份章节号')
    parser.add_argument('--chapter-title', help='章节标题（可选）')
    parser.add_argument(
        '--allowlist',
        action='append',
        default=[],
        help='Git backup 明确路径；可重复传入，禁止使用 . 或目录通配',
    )
    parser.add_argument(
        '--authorization-token',
        default='',
        help='仅为 root/chapter/allowlist scope challenge；不能单独证明用户授权',
    )
    parser.add_argument(
        '--decision-receipt',
        default='',
        help='绑定同一 scope 且可从可信 Codex rollout 重验的一次性用户决定 receipt',
    )
    parser.add_argument(
        '--build-decision-receipt',
        action='store_true',
        help='从当前可信父 rollout 构造一次性用户决定 receipt；不执行 Git 写',
    )
    parser.add_argument('--rollout-path', default='', help='当前父任务可信 rollout 的绝对路径')
    parser.add_argument(
        '--output-json',
        default='',
        help='可选输出 artifact；必须位于项目 .webnovel/tmp/write-runs 下',
    )
    parser.add_argument('--format', choices=('json', 'text'), default='text')
    parser.add_argument(
        '--rollback', type=int, metavar='CHAPTER', help='旧入口（已禁用，不执行 Git 写）'
    )
    parser.add_argument('--diff', nargs=2, type=int, metavar=('A', 'B'), help='对比两个版本')
    parser.add_argument(
        '--create-branch', type=int, metavar='CHAPTER', help='旧入口（已禁用，不执行 Git 写）'
    )
    parser.add_argument('--branch-name', help='分支名称')
    parser.add_argument('--list', action='store_true', help='列出所有备份')
    parser.add_argument('--project-root', default='.', help='项目根目录')

    args = parser.parse_args()

    # 解析项目根目录（允许传入“工作区根目录”，统一解析到真正的 book project_root）
    try:
        project_root = str(resolve_project_root(args.project_root))
    except FileNotFoundError as exc:
        print(f"❌ 无法定位项目根目录（需要包含 .webnovel/state.json）: {exc}", file=sys.stderr)
        sys.exit(2)

    # 创建管理器
    manager = GitBackupManager(project_root)

    # 执行操作
    if args.rollback is not None or args.create_branch is not None:
        payload = {
            "ok": False,
            "status": "failed",
            "code": "legacy_git_mutation_disabled",
            "detail": "rollback/create-branch require a separate authorized recovery workflow",
        }
        if args.format == 'json':
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"backup: failed ({payload['code']})")
            print(payload["detail"])
        sys.exit(2)

    if args.build_decision_receipt:
        if args.chapter is None or args.chapter <= 0 or not args.allowlist or not args.rollout_path:
            parser.error("--build-decision-receipt requires --chapter, --allowlist, and --rollout-path")
        try:
            current_thread = _canonical_nonzero_uuid(
                os.environ.get("CODEX_THREAD_ID"), label="CODEX_THREAD_ID"
            )
            receipt = build_git_backup_decision_receipt(
                project_root,
                args.chapter,
                args.allowlist,
                rollout_path=args.rollout_path,
                thread_id=current_thread,
            )
            if args.output_json:
                _write_cli_json_artifact(Path(project_root), args.output_json, receipt)
        except BackupError as exc:
            payload = {
                "ok": False,
                "status": "failed",
                "code": "git_backup_decision_receipt_failed",
                "detail": str(exc),
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2) if args.format == 'json' else payload["detail"])
            sys.exit(2)
        print(json.dumps(receipt, ensure_ascii=False, indent=2) if args.format == 'json' else f"decision-receipt: {receipt['receipt_sha256']}")
        sys.exit(0)

    if args.chapter is not None:
        receipt = manager.backup(
            args.chapter,
            args.chapter_title or "",
            allowlist=args.allowlist,
            authorization_token=args.authorization_token,
            decision_receipt=args.decision_receipt or None,
        )
        if args.output_json:
            try:
                _write_cli_json_artifact(Path(project_root), args.output_json, receipt)
            except BackupError as exc:
                receipt = {
                    "schema_version": BACKUP_RECEIPT_SCHEMA,
                    "project_root": project_root,
                    "chapter": args.chapter,
                    "ok": False,
                    "status": "failed",
                    "code": "backup_output_artifact_failed",
                    "detail": str(exc),
                }
        if args.format == 'json':
            print(json.dumps(receipt, ensure_ascii=False, indent=2))
        else:
            print(f"backup: {receipt.get('status')} ({receipt.get('code')})")
            if receipt.get('detail'):
                print(receipt['detail'])
        if receipt.get('ok'):
            sys.exit(0)
        sys.exit(1)

    elif args.diff:
        manager.diff(args.diff[0], args.diff[1])

    elif args.list:
        manager.list_backups()

    else:
        parser.print_help()

if __name__ == "__main__":
    main()
