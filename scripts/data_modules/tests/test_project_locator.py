#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest


def _ensure_scripts_on_path() -> None:
    scripts_dir = Path(__file__).resolve().parents[2]
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))


@pytest.fixture(autouse=True)
def isolate_project_locator_environment(monkeypatch, tmp_path):
    monkeypatch.delenv("WEBNOVEL_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "empty-codex-home"))
    monkeypatch.setenv("WEBNOVEL_HOME", str(tmp_path / "empty-webnovel-home"))
    monkeypatch.setenv("WEBNOVEL_CLAUDE_HOME", str(tmp_path / "empty-claude-home"))


def _registry_payload(workspace: Path, project_root: Path) -> dict:
    workspace = workspace.resolve()
    project_root = project_root.resolve()
    return {
        "schema_version": 1,
        "workspaces": {
            os.path.normcase(str(workspace)): {
                "workspace_root": str(workspace),
                "current_project_root": str(project_root),
                "updated_at": "2026-08-06T00:00:00",
            }
        },
        "last_used_project_root": str(project_root),
        "updated_at": "2026-08-06T00:00:00",
    }


def _write_registry(path: Path, workspace: Path, project_root: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_registry_payload(workspace, project_root), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _file_fingerprint(path: Path) -> tuple[int, str]:
    return path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest()


def test_resolve_project_root_prefers_cwd_project(tmp_path):
    _ensure_scripts_on_path()

    from project_locator import resolve_project_root

    (tmp_path / ".git").mkdir(parents=True, exist_ok=True)
    project_root = tmp_path / "workspace"
    (project_root / ".webnovel").mkdir(parents=True, exist_ok=True)
    (project_root / ".webnovel" / "state.json").write_text("{}", encoding="utf-8")

    resolved = resolve_project_root(cwd=project_root)
    assert resolved == project_root.resolve()


def test_resolve_project_root_stops_at_git_root(tmp_path):
    _ensure_scripts_on_path()

    from project_locator import resolve_project_root

    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True, exist_ok=True)

    nested = repo_root / "sub" / "dir"
    nested.mkdir(parents=True, exist_ok=True)

    outside_project = tmp_path / "outside_project"
    (outside_project / ".webnovel").mkdir(parents=True, exist_ok=True)
    (outside_project / ".webnovel" / "state.json").write_text("{}", encoding="utf-8")

    try:
        resolve_project_root(cwd=nested)
        assert False, "Expected FileNotFoundError when only parent outside git root has project"
    except FileNotFoundError:
        pass


def test_resolve_project_root_finds_default_subdir_within_git_root(tmp_path):
    _ensure_scripts_on_path()

    from project_locator import resolve_project_root

    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True, exist_ok=True)

    default_project = repo_root / "webnovel-project"
    (default_project / ".webnovel").mkdir(parents=True, exist_ok=True)
    (default_project / ".webnovel" / "state.json").write_text("{}", encoding="utf-8")

    nested = repo_root / "sub" / "dir"
    nested.mkdir(parents=True, exist_ok=True)

    resolved = resolve_project_root(cwd=nested)
    assert resolved == default_project.resolve()


def test_resolve_project_root_uses_workspace_pointer(tmp_path):
    _ensure_scripts_on_path()

    from project_locator import resolve_project_root, write_current_project_pointer

    workspace = tmp_path / "workspace"
    (workspace / ".claude").mkdir(parents=True, exist_ok=True)

    project_root = workspace / "凡人资本论"
    (project_root / ".webnovel").mkdir(parents=True, exist_ok=True)
    (project_root / ".webnovel" / "state.json").write_text("{}", encoding="utf-8")

    pointer_file = write_current_project_pointer(project_root, workspace_root=workspace)
    assert pointer_file is not None
    assert pointer_file.is_file()

    resolved = resolve_project_root(cwd=workspace)
    assert resolved == project_root.resolve()


def test_resolve_project_root_explicit_workspace_uses_unique_child_project(tmp_path):
    _ensure_scripts_on_path()

    from project_locator import resolve_project_root

    workspace = tmp_path / "workspace"
    (workspace / ".git").mkdir(parents=True, exist_ok=True)
    project_root = workspace / "凡人资本论"
    (project_root / ".webnovel").mkdir(parents=True, exist_ok=True)
    (project_root / ".webnovel" / "state.json").write_text("{}", encoding="utf-8")

    resolved = resolve_project_root(str(workspace))
    assert resolved == project_root.resolve()


def test_resolve_project_root_ignores_stale_pointer_and_fallbacks(tmp_path):
    _ensure_scripts_on_path()

    from project_locator import resolve_project_root

    workspace = tmp_path / "workspace"
    (workspace / ".git").mkdir(parents=True, exist_ok=True)
    (workspace / ".claude").mkdir(parents=True, exist_ok=True)
    # stale pointer
    (workspace / ".claude" / ".webnovel-current-project").write_text(
        str(workspace / "missing-project"), encoding="utf-8"
    )

    default_project = workspace / "webnovel-project"
    (default_project / ".webnovel").mkdir(parents=True, exist_ok=True)
    (default_project / ".webnovel" / "state.json").write_text("{}", encoding="utf-8")

    resolved = resolve_project_root(cwd=workspace)
    assert resolved == default_project.resolve()


def test_resolve_project_root_prefers_native_registry(monkeypatch, tmp_path):
    _ensure_scripts_on_path()

    from project_locator import resolve_project_root

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    native_project = tmp_path / "native-book"
    legacy_project = tmp_path / "legacy-book"
    for project in (native_project, legacy_project):
        (project / ".webnovel").mkdir(parents=True)
        (project / ".webnovel" / "state.json").write_text("{}", encoding="utf-8")

    native_registry = Path(os.environ["WEBNOVEL_HOME"]) / "workspaces.json"
    legacy_registry = Path(os.environ["WEBNOVEL_CLAUDE_HOME"]) / "webnovel-writer" / "workspaces.json"
    _write_registry(native_registry, workspace, native_project)
    _write_registry(legacy_registry, workspace, legacy_project)

    assert resolve_project_root(str(workspace)) == native_project.resolve()


def test_resolve_project_root_reads_legacy_registry_without_modifying_it(tmp_path):
    _ensure_scripts_on_path()

    from project_locator import resolve_project_root

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    legacy_project = tmp_path / "legacy-book"
    (legacy_project / ".webnovel").mkdir(parents=True)
    (legacy_project / ".webnovel" / "state.json").write_text("{}", encoding="utf-8")

    legacy_registry = Path(os.environ["WEBNOVEL_CLAUDE_HOME"]) / "webnovel-writer" / "workspaces.json"
    _write_registry(legacy_registry, workspace, legacy_project)
    before = _file_fingerprint(legacy_registry)

    assert resolve_project_root(str(workspace)) == legacy_project.resolve()
    assert _file_fingerprint(legacy_registry) == before


def test_update_global_registry_writes_native_only(tmp_path):
    _ensure_scripts_on_path()

    from project_locator import update_global_registry_current_project

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    project_root = tmp_path / "book"
    (project_root / ".webnovel").mkdir(parents=True)
    (project_root / ".webnovel" / "state.json").write_text("{}", encoding="utf-8")

    legacy_registry = Path(os.environ["WEBNOVEL_CLAUDE_HOME"]) / "webnovel-writer" / "workspaces.json"
    _write_registry(legacy_registry, workspace, project_root)
    legacy_before = _file_fingerprint(legacy_registry)

    registry_path = update_global_registry_current_project(
        workspace_root=workspace,
        project_root=project_root,
    )

    expected_native = Path(os.environ["WEBNOVEL_HOME"]) / "workspaces.json"
    assert registry_path == expected_native.resolve()
    assert expected_native.is_file()
    assert json.loads(expected_native.read_text(encoding="utf-8"))["last_used_project_root"] == str(
        project_root.resolve()
    )
    assert _file_fingerprint(legacy_registry) == legacy_before
