#!/usr/bin/env python3
"""
Project location helpers for webnovel-writer scripts.

Problem this solves:
- Many scripts assumed CWD is the project root and used relative paths like `.webnovel/state.json`.
- Commands may be invoked from a workspace, a nested project directory, or an installed plugin.

These helpers provide a single, consistent way to locate the active project root.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Literal, Optional

from host_paths import resolve_legacy_claude_home, resolve_webnovel_home
from runtime_compat import normalize_windows_path


CODEX_CURRENT_PROJECT_POINTER_REL: Path = Path(".codex") / ".webnovel-current-project"
LEGACY_CURRENT_PROJECT_POINTER_REL: Path = Path(".claude") / ".webnovel-current-project"

# Codex 原生 registry 是唯一可写位置；Claude registry 仅用于只读兼容。
NATIVE_REGISTRY_FILENAME = "workspaces.json"
LEGACY_GLOBAL_REGISTRY_REL: Path = Path("webnovel-writer") / "workspaces.json"

# Claude Code 兼容环境变量（仅用于 legacy 读取 fallback）
ENV_CLAUDE_PROJECT_DIR = "CLAUDE_PROJECT_DIR"


ResolutionSource = Literal[
    "cli",
    "env",
    "cwd",
    "codex_pointer",
    "codex_registry",
    "legacy_pointer",
    "legacy_registry",
]
CompatibilityMode = Literal["native", "legacy_read_only"]


@dataclass(frozen=True)
class ProjectResolution:
    project_root: Path
    resolved_from: ResolutionSource
    compatibility_mode: CompatibilityMode = "native"

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "project_root": str(self.project_root),
            "resolved_from": self.resolved_from,
            "compatibility_mode": self.compatibility_mode,
        }


@dataclass(frozen=True)
class ProjectBindingResult:
    project_root: Path
    workspace_root: Path
    pointer_path: Optional[Path]
    registry_path: Optional[Path]


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _normcase_path_key(p: Path) -> str:
    """
    生成稳定的路径 key（Windows 下大小写/分隔符不敏感）。

    注意：key 仅用于映射表索引，实际路径仍以原始绝对路径字符串存储。
    """
    try:
        resolved = p.expanduser().resolve()
    except Exception:
        resolved = p.expanduser()
    return os.path.normcase(str(resolved))


def _get_user_claude_root() -> Path:
    """Compatibility alias for callers that still inspect the legacy root."""
    return resolve_legacy_claude_home()


def _global_registry_path() -> Path:
    return resolve_webnovel_home() / NATIVE_REGISTRY_FILENAME


def _legacy_global_registry_path() -> Path:
    return _get_user_claude_root() / LEGACY_GLOBAL_REGISTRY_REL


def _default_registry() -> dict:
    return {
        "schema_version": 1,
        "workspaces": {},
        "last_used_project_root": "",
        "updated_at": _now_iso(),
    }


def _load_global_registry(path: Path) -> dict:
    if not path.is_file():
        return _default_registry()
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except Exception:
        return _default_registry()
    if not isinstance(data, dict):
        return _default_registry()

    if data.get("schema_version") != 1:
        data["schema_version"] = 1
    if not isinstance(data.get("workspaces"), dict):
        data["workspaces"] = {}
    if not isinstance(data.get("last_used_project_root"), str):
        data["last_used_project_root"] = ""
    if not isinstance(data.get("updated_at"), str):
        data["updated_at"] = _now_iso()
    return data


def _save_global_registry(path: Path, data: dict) -> bool:
    # 写入是 best-effort：用户目录权限/只读盘符等情况不应阻断主流程。
    try:
        from security_utils import atomic_write_json

        data["updated_at"] = _now_iso()
        atomic_write_json(path, data, backup=False)
        return True
    except Exception:
        # 非阻断
        return False


def _resolve_project_root_from_registry_path(
    reg_path: Path,
    base: Path,
    *,
    stop_at: Optional[Path] = None,
) -> Optional[Path]:
    """Resolve one registry without mutating or repairing it on disk."""
    reg = _load_global_registry(reg_path)
    workspaces = reg.get("workspaces") or {}
    if not isinstance(workspaces, dict) or not workspaces:
        return None

    hints = [base]
    boundary_key = _normcase_path_key(stop_at) if stop_at is not None else None

    # 1) 精确匹配
    for hint in hints:
        key = _normcase_path_key(hint)
        entry = workspaces.get(key)
        if isinstance(entry, dict):
            raw = entry.get("current_project_root")
            if isinstance(raw, str) and raw.strip():
                target = normalize_windows_path(raw).expanduser()
                if not target.is_absolute():
                    continue
                if _is_project_root(target):
                    return target.resolve()

    # 2) 前缀匹配（从 workspace 子目录运行时）
    for hint in hints:
        hint_key = _normcase_path_key(hint)
        best_key: Optional[str] = None
        best_len = -1
        for ws_key in workspaces.keys():
            if not isinstance(ws_key, str) or not ws_key:
                continue
            ws_key_norm = os.path.normcase(ws_key)
            if boundary_key is not None:
                try:
                    inside_boundary = (
                        os.path.commonpath((ws_key_norm, boundary_key)) == boundary_key
                    )
                except ValueError:
                    inside_boundary = False
                if not inside_boundary:
                    continue
            try:
                contains_hint = os.path.commonpath((hint_key, ws_key_norm)) == ws_key_norm
            except ValueError:
                contains_hint = False
            if contains_hint:
                if len(ws_key_norm) > best_len:
                    best_key = ws_key
                    best_len = len(ws_key_norm)
        if best_key:
            entry = workspaces.get(best_key)
            if isinstance(entry, dict):
                raw = entry.get("current_project_root")
                if isinstance(raw, str) and raw.strip():
                    target = normalize_windows_path(raw).expanduser()
                    if target.is_absolute() and _is_project_root(target):
                        return target.resolve()

    return None


def update_global_registry_current_project(
    *,
    workspace_root: Optional[Path],
    project_root: Path,
) -> Optional[Path]:
    """
    更新用户级 registry：workspace -> current_project_root 映射。

    返回：registry 文件路径（写入失败则返回 None）。
    """
    root = normalize_windows_path(project_root).expanduser()
    try:
        root = root.resolve()
    except Exception:
        root = root
    if not _is_project_root(root):
        raise FileNotFoundError(f"Not a webnovel project root (missing .webnovel/state.json): {root}")

    ws = workspace_root
    if ws is None:
        return None

    try:
        ws = ws.expanduser().resolve()
    except Exception:
        ws = ws.expanduser()

    reg_path = _global_registry_path()
    reg = _load_global_registry(reg_path)
    workspaces = reg.get("workspaces")
    if not isinstance(workspaces, dict):
        workspaces = {}
        reg["workspaces"] = workspaces

    workspaces[_normcase_path_key(ws)] = {
        "workspace_root": str(ws),
        "current_project_root": str(root),
        "updated_at": _now_iso(),
    }
    reg["last_used_project_root"] = str(root)
    if not _save_global_registry(reg_path, reg):
        return None
    return reg_path


def _candidate_roots(cwd: Path, *, stop_at: Optional[Path] = None) -> Iterable[Path]:
    for candidate in (cwd, *cwd.parents):
        yield candidate
        if stop_at is not None and candidate == stop_at:
            break


def _is_project_root(path: Path) -> bool:
    return (path / ".webnovel" / "state.json").is_file()


def _find_search_boundary(cwd: Path) -> Optional[Path]:
    """Bound discovery at the nearest repository or installed plugin root."""
    for candidate in (cwd, *cwd.parents):
        if (candidate / ".git").exists() or (candidate / ".codex-plugin" / "plugin.json").is_file():
            return candidate
    return None


def _find_plugin_root(start: Path) -> Optional[Path]:
    for candidate in (start, *start.parents):
        if (candidate / ".codex-plugin" / "plugin.json").is_file():
            return candidate
    return None


def _pointer_candidates(
    cwd: Path,
    pointer_rel: Path,
    *,
    stop_at: Optional[Path] = None,
) -> Iterable[Path]:
    """Yield candidate pointer files from cwd up to parents (bounded by stop_at when provided)."""
    for candidate in (cwd, *cwd.parents):
        yield candidate / pointer_rel
        if stop_at is not None and candidate == stop_at:
            break


def _resolve_project_root_from_pointer(
    cwd: Path,
    pointer_rel: Path,
    *,
    stop_at: Optional[Path] = None,
    allow_pointer_dir_relative: bool = False,
) -> Optional[Path]:
    """Resolve an absolute or legacy relative workspace pointer without writing it."""
    for pointer_file in _pointer_candidates(cwd, pointer_rel, stop_at=stop_at):
        if not pointer_file.is_file():
            continue
        try:
            raw = pointer_file.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            continue
        if not raw:
            continue
        target = normalize_windows_path(raw).expanduser()
        candidates = [target]
        if not target.is_absolute():
            # Native pointers are relative to the workspace.  Only legacy
            # `.claude` pointers may also be relative to the pointer directory.
            candidates = [pointer_file.parent.parent / target]
            if allow_pointer_dir_relative:
                candidates.append(pointer_file.parent / target)
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
            except OSError:
                resolved = candidate
            if _is_project_root(resolved):
                return resolved
    return None


def confirm_current_workspace(
    project_root: Path,
    *,
    cwd: Optional[Path] = None,
) -> Optional[Path]:
    """Return a workspace only when the current directory proves the context.

    A project parent is not inherently a workspace.  We accept the exact book
    root, a directory inside that book (binding the book itself), or a current
    workspace ancestor that contains the book.  Installed/plugin checkouts are
    rejected unless the caller supplies an explicit workspace instead.
    """

    project = normalize_windows_path(project_root).expanduser().resolve()
    current = (cwd or Path.cwd()).expanduser().resolve()

    plugin_root = _find_plugin_root(current)
    if plugin_root is not None and plugin_root != project:
        return None

    if current == project or project in current.parents:
        return project
    try:
        project.relative_to(current)
    except ValueError:
        return None
    return current


def _atomic_write_pointer(path: Path, project_root: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        temp_path.write_text(f"{project_root}\n", encoding="utf-8", newline="\n")
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def bind_current_project(
    project_root: Path,
    *,
    workspace_root: Path,
) -> ProjectBindingResult:
    """Bind one workspace using only the Codex pointer and native registry."""
    root = normalize_windows_path(project_root).expanduser().resolve()
    if not _is_project_root(root):
        raise FileNotFoundError(f"Not a webnovel project root (missing .webnovel/state.json): {root}")

    ws_root = normalize_windows_path(workspace_root).expanduser().resolve()
    if not ws_root.is_dir():
        raise FileNotFoundError(f"Workspace root does not exist or is not a directory: {ws_root}")

    pointer_file = ws_root / CODEX_CURRENT_PROJECT_POINTER_REL
    try:
        _atomic_write_pointer(pointer_file, root)
    except (OSError, UnicodeError):
        pointer_file = None

    try:
        registry_path = update_global_registry_current_project(workspace_root=ws_root, project_root=root)
    except (OSError, ValueError):
        registry_path = None

    return ProjectBindingResult(
        project_root=root,
        workspace_root=ws_root,
        pointer_path=pointer_file,
        registry_path=registry_path,
    )


def write_current_project_pointer(project_root: Path, *, workspace_root: Optional[Path] = None) -> Optional[Path]:
    """Compatibility wrapper; new callers should use :func:`bind_current_project`."""
    if workspace_root is None:
        return None
    return bind_current_project(project_root, workspace_root=workspace_root).pointer_path


def resolve_project(
    explicit_project_root: Optional[str] = None,
    *,
    cwd: Optional[Path] = None,
) -> ProjectResolution:
    """Resolve a project with stable provenance and read-only legacy fallback."""
    if explicit_project_root is not None:
        if not str(explicit_project_root).strip():
            raise FileNotFoundError("Explicit project root is empty")
        root = normalize_windows_path(explicit_project_root).expanduser()
        try:
            root = root.resolve()
        except OSError as exc:
            raise FileNotFoundError(
                f"Explicit project root cannot be resolved: {root}: {exc}"
            ) from exc
        if not _is_project_root(root):
            raise FileNotFoundError(f"Not a webnovel project root (missing .webnovel/state.json): {root}")
        return ProjectResolution(root, "cli")

    env_root = os.environ.get("WEBNOVEL_PROJECT_ROOT")
    if env_root is not None:
        if not env_root.strip():
            raise FileNotFoundError("WEBNOVEL_PROJECT_ROOT is set but empty")
        root = normalize_windows_path(env_root).expanduser()
        try:
            root = root.resolve()
        except OSError as exc:
            raise FileNotFoundError(
                f"WEBNOVEL_PROJECT_ROOT cannot be resolved: {root}: {exc}"
            ) from exc
        if not _is_project_root(root):
            raise FileNotFoundError(
                f"WEBNOVEL_PROJECT_ROOT is set but invalid (missing .webnovel/state.json): {root}"
            )
        return ProjectResolution(root, "env")

    base = (cwd or Path.cwd()).resolve()
    boundary = _find_search_boundary(base)

    for candidate in _candidate_roots(base, stop_at=boundary):
        if _is_project_root(candidate):
            return ProjectResolution(candidate.resolve(), "cwd")

    pointer_root = _resolve_project_root_from_pointer(
        base,
        CODEX_CURRENT_PROJECT_POINTER_REL,
        stop_at=boundary,
    )
    if pointer_root is not None:
        return ProjectResolution(pointer_root, "codex_pointer")

    registry_root = _resolve_project_root_from_registry_path(
        _global_registry_path(),
        base,
        stop_at=boundary,
    )
    if registry_root is not None:
        return ProjectResolution(registry_root, "codex_registry")

    legacy_starts = [base]
    legacy_hint_raw = os.environ.get(ENV_CLAUDE_PROJECT_DIR)
    if legacy_hint_raw:
        legacy_hint = normalize_windows_path(legacy_hint_raw).expanduser()
        try:
            legacy_hint = legacy_hint.resolve()
        except OSError:
            pass
        if _normcase_path_key(legacy_hint) != _normcase_path_key(base):
            legacy_starts.append(legacy_hint)

    for legacy_start in legacy_starts:
        legacy_boundary = _find_search_boundary(legacy_start)
        pointer_root = _resolve_project_root_from_pointer(
            legacy_start,
            LEGACY_CURRENT_PROJECT_POINTER_REL,
            stop_at=legacy_boundary,
            allow_pointer_dir_relative=True,
        )
        if pointer_root is not None:
            return ProjectResolution(pointer_root, "legacy_pointer", "legacy_read_only")

    for legacy_start in legacy_starts:
        registry_root = _resolve_project_root_from_registry_path(
            _legacy_global_registry_path(),
            legacy_start,
            stop_at=_find_search_boundary(legacy_start),
        )
        if registry_root is not None:
            return ProjectResolution(registry_root, "legacy_registry", "legacy_read_only")

    raise FileNotFoundError(
        "Unable to locate webnovel project root. Expected `.webnovel/state.json` in the current directory "
        "or an ancestor, a Codex workspace pointer/registry, or a read-only legacy pointer/registry. "
        "Run `webnovel init`, use `webnovel use <project_root>`, pass --project-root, or set "
        "WEBNOVEL_PROJECT_ROOT."
    )


def resolve_project_root(explicit_project_root: Optional[str] = None, *, cwd: Optional[Path] = None) -> Path:
    """Compatibility API returning only the resolved project root path."""
    return resolve_project(explicit_project_root, cwd=cwd).project_root


def resolve_state_file(
    explicit_state_file: Optional[str] = None,
    *,
    explicit_project_root: Optional[str] = None,
    cwd: Optional[Path] = None,
) -> Path:
    """
    Resolve `.webnovel/state.json` path.

    If explicit_state_file is provided, returns it as-is (resolved to absolute if relative).
    Otherwise derives it from resolve_project_root().
    """
    base = (cwd or Path.cwd()).resolve()
    if explicit_state_file:
        p = Path(explicit_state_file).expanduser()
        return (base / p).resolve() if not p.is_absolute() else p.resolve()

    root = resolve_project_root(explicit_project_root, cwd=base)
    return root / ".webnovel" / "state.json"
