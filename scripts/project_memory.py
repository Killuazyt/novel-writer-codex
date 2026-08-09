#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Project memory writer for the Codex $webnovel-learn workflow."""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from data_modules.learn_request import (
    IMPORTANCE_VALUES,
    MAX_CATEGORY_CHARS,
    MAX_DESCRIPTION_CHARS,
    PATTERN_TYPES,
    LearnRequestError,
    load_learn_request,
)
from runtime_compat import enable_windows_utf8_stdio
from security_utils import AtomicWriteError, atomic_write_json


RESULT_SCHEMA = "webnovel-learn-result/v1"
MAX_PROJECT_MEMORY_BYTES = 16 * 1024 * 1024


class LearnExecutionError(RuntimeError):
    """Raised when the controlled write cannot be executed safely."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _is_linklike(path: Path) -> bool:
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


def _read_regular_bytes(path: Path) -> bytes:
    if _is_linklike(path):
        raise ValueError(f"受控文件不得是符号链接或目录联接: {path}")
    try:
        with path.open("rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_PROJECT_MEMORY_BYTES:
                raise ValueError(f"受控文件不是普通文件或尺寸超限: {path}")
            raw = handle.read(MAX_PROJECT_MEMORY_BYTES + 1)
            after = os.fstat(handle.fileno())
    except OSError as exc:
        raise ValueError(f"读取失败: {path}: {exc}") from exc
    if (
        (before.st_dev, before.st_ino, before.st_size)
        != (after.st_dev, after.st_ino, after.st_size)
        or len(raw) != before.st_size
    ):
        raise ValueError(f"受控文件在读取期间发生变化: {path}")
    return raw


def _load_json_bytes(path: Path, raw: bytes) -> Dict[str, Any]:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ValueError(f"JSON 解析失败: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"JSON 顶层必须是 object: {path}")
    return data


def _atomic_backup_bytes(path: Path, raw: bytes) -> None:
    """Replace the backup leaf itself, never follow a pre-existing link."""

    if _is_linklike(path):
        raise LearnExecutionError(f"备份路径不得是符号链接或目录联接: {path}")
    fd, temp_name = tempfile.mkstemp(prefix=path.stem + "_", suffix=".tmp", dir=path.parent)
    temp_path: Optional[Path] = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        temp_path = None
    except OSError as exc:
        raise LearnExecutionError(f"project_memory 备份失败: {exc}") from exc
    finally:
        if temp_path is not None and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def _current_chapter(project_root: Path) -> Optional[int]:
    state_path = project_root / ".webnovel" / "state.json"
    if not state_path.exists():
        return None
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeError, OSError):
        return None
    progress = state.get("progress") if isinstance(state, dict) else {}
    chapter = progress.get("current_chapter") if isinstance(progress, dict) else None
    try:
        if isinstance(chapter, bool) or not isinstance(chapter, int):
            return None
        normalized = chapter
        return normalized if normalized > 0 else None
    except (TypeError, ValueError):
        return None


def add_pattern(
    project_root: Path,
    *,
    pattern_type: str,
    description: str,
    category: str = "",
    importance: str = "medium",
    source_chapter: Optional[int] = None,
) -> Dict[str, Any]:
    project_root = project_root.expanduser().resolve()
    webnovel_dir = project_root / ".webnovel"
    if not webnovel_dir.is_dir() or _is_linklike(webnovel_dir):
        raise ValueError(".webnovel 必须是项目内的真实目录")
    resolved_webnovel = webnovel_dir.resolve(strict=True)
    try:
        resolved_webnovel.relative_to(project_root)
    except ValueError as exc:
        raise ValueError(".webnovel 路径越出项目根") from exc
    memory_path = resolved_webnovel / "project_memory.json"
    lock_path = memory_path.with_suffix(memory_path.suffix + ".lock")
    backup_path = memory_path.with_suffix(memory_path.suffix + ".bak")
    if any(_is_linklike(path) for path in (memory_path, lock_path, backup_path)):
        raise ValueError("project_memory.json 不得是符号链接")
    pattern_type = (pattern_type or "other").strip() or "other"
    if pattern_type not in PATTERN_TYPES:
        raise ValueError("pattern_type 非法")
    description = (description or "").strip()
    if not description:
        raise ValueError("description 不能为空")
    if "\x00" in description or len(description) > MAX_DESCRIPTION_CHARS:
        raise ValueError("description 长度或字符非法")
    category = (category or "").strip()
    if "\x00" in category or len(category) > MAX_CATEGORY_CHARS:
        raise ValueError("category 长度或字符非法")
    importance = (importance or "medium").strip() or "medium"
    if importance not in IMPORTANCE_VALUES:
        raise ValueError("importance 非法")
    if source_chapter is not None and (
        isinstance(source_chapter, bool)
        or not isinstance(source_chapter, int)
        or source_chapter <= 0
    ):
        raise ValueError("source_chapter 必须是正整数或 null")

    try:
        from filelock import FileLock, Timeout
    except ImportError as exc:
        raise LearnExecutionError("缺少受控写入所需的 filelock 依赖") from exc

    try:
        lock = FileLock(str(lock_path), timeout=10)
        lock.acquire()
    except Timeout as exc:
        raise LearnExecutionError("project_memory 写锁超时") from exc
    except OSError as exc:
        raise LearnExecutionError(f"project_memory 写锁不可用: {exc}") from exc
    try:
        if any(_is_linklike(path) for path in (memory_path, lock_path, backup_path)):
            raise ValueError("project_memory、lock 或 backup 不得是符号链接或目录联接")
        old_bytes = _read_regular_bytes(memory_path) if memory_path.exists() else None
        payload = _load_json_bytes(memory_path, old_bytes) if old_bytes is not None else {}
        patterns = payload.setdefault("patterns", [])
        if not isinstance(patterns, list):
            raise ValueError(f"patterns 必须是数组: {memory_path}")

        for item in patterns:
            if not isinstance(item, dict):
                continue
            if item.get("pattern_type") == pattern_type and item.get("description") == description:
                return {
                    "schema_version": RESULT_SCHEMA,
                    "status": "skipped",
                    "reason": "duplicate",
                    "learned": item,
                    "path": str(memory_path),
                }

        now = _utc_now_iso()
        chapter = source_chapter if source_chapter is not None else _current_chapter(project_root)
        learned: Dict[str, Any] = {
            "pattern_type": pattern_type,
            "description": description,
            "source_chapter": chapter,
            "learned_at": now,
            "updated_at": now,
        }
        if category:
            learned["category"] = category
        if importance:
            learned["importance"] = importance

        patterns.append(learned)
        prior_backup = _read_regular_bytes(backup_path) if backup_path.exists() else None
        backup_created = False
        try:
            if old_bytes is not None:
                _atomic_backup_bytes(backup_path, old_bytes)
                backup_created = True
            atomic_write_json(memory_path, payload, use_lock=False, backup=False)
        except Exception as exc:
            # A failed main-file replacement must not leave new backup state
            # that makes the operation appear partially successful.
            rollback_error: Exception | None = None
            if backup_created:
                try:
                    if prior_backup is None:
                        backup_path.unlink(missing_ok=True)
                    else:
                        _atomic_backup_bytes(backup_path, prior_backup)
                except Exception as backup_exc:  # keep the main file intact, report partial control state
                    rollback_error = backup_exc
            if rollback_error is not None:
                raise LearnExecutionError(
                    f"project_memory 主写入失败且备份回滚失败: {rollback_error}"
                ) from exc
            raise
        return {
            "schema_version": RESULT_SCHEMA,
            "status": "success",
            "learned": learned,
            "path": str(memory_path),
        }
    finally:
        try:
            lock.release()
        except OSError as exc:
            raise LearnExecutionError(f"project_memory 写锁释放失败: {exc}") from exc


def main() -> None:
    parser = argparse.ArgumentParser(description="Write .webnovel/project_memory.json safely")
    parser.add_argument("--project-root", required=True)
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add-pattern", help="追加一条项目经验记忆")
    add.add_argument("--input-json", help="绝对路径的 webnovel-learn-request/v1 JSON")
    add.add_argument("--pattern-type")
    add.add_argument("--description")
    add.add_argument("--category")
    add.add_argument("--importance")
    add.add_argument("--source-chapter", type=int)

    args = parser.parse_args()
    try:
        if args.command == "add-pattern":
            scalar_values = (
                args.pattern_type,
                args.description,
                args.category,
                args.importance,
                args.source_chapter,
            )
            if args.input_json:
                if any(value is not None for value in scalar_values):
                    raise LearnRequestError("--input-json 不能与标量 pattern 参数混用")
                request = load_learn_request(args.input_json, project_root=args.project_root)
            else:
                if not args.description:
                    raise LearnRequestError("必须提供 --input-json；兼容调用至少需要 --description")
                request = {
                    "pattern_type": args.pattern_type or "other",
                    "description": args.description,
                    "category": args.category or "",
                    "importance": args.importance or "medium",
                    "source_chapter": args.source_chapter,
                }
            result = add_pattern(
                Path(args.project_root),
                pattern_type=request["pattern_type"],
                description=request["description"],
                category=request["category"],
                importance=request["importance"],
                source_chapter=request["source_chapter"],
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
    except LearnRequestError as exc:
        print(
            json.dumps(
                {"schema_version": RESULT_SCHEMA, "status": "error", "error": str(exc)},
                ensure_ascii=False,
            )
        )
        raise SystemExit(2)
    except ValueError as exc:
        print(
            json.dumps(
                {"schema_version": RESULT_SCHEMA, "status": "error", "error": str(exc)},
                ensure_ascii=False,
            )
        )
        raise SystemExit(1)
    except (AtomicWriteError, OSError, LearnExecutionError) as exc:
        print(
            json.dumps(
                {"schema_version": RESULT_SCHEMA, "status": "failed", "error": str(exc)},
                ensure_ascii=False,
            )
        )
        raise SystemExit(2)

    raise SystemExit(2)


if __name__ == "__main__":
    if sys.platform == "win32":
        enable_windows_utf8_stdio()
    main()
