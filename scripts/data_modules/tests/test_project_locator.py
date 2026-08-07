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

    # A parent repository/project marker must not leak through the nearest
    # repository boundary and capture this nested checkout.
    (tmp_path / ".webnovel").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".webnovel" / "state.json").write_text("{}", encoding="utf-8")

    try:
        resolve_project_root(cwd=nested)
        assert False, "Expected FileNotFoundError when only parent outside git root has project"
    except FileNotFoundError:
        pass


@pytest.mark.parametrize("registry_kind", ["native", "legacy"])
@pytest.mark.parametrize("boundary_kind", ["git", "plugin"])
def test_registry_does_not_cross_nearest_repository_boundary(
    tmp_path, registry_kind, boundary_kind
):
    _ensure_scripts_on_path()

    from project_locator import resolve_project

    outer_workspace = tmp_path / "outer-workspace"
    nested_repo = outer_workspace / "nested-repo"
    cwd = nested_repo / "src"
    if boundary_kind == "git":
        (nested_repo / ".git").mkdir(parents=True)
    else:
        (nested_repo / ".codex-plugin").mkdir(parents=True)
        (nested_repo / ".codex-plugin" / "plugin.json").write_text(
            "{}", encoding="utf-8"
        )
    cwd.mkdir()
    previous_book = outer_workspace / "previous-book"
    (previous_book / ".webnovel").mkdir(parents=True)
    (previous_book / ".webnovel" / "state.json").write_text("{}", encoding="utf-8")

    if registry_kind == "native":
        registry = Path(os.environ["WEBNOVEL_HOME"]) / "workspaces.json"
    else:
        registry = (
            Path(os.environ["WEBNOVEL_CLAUDE_HOME"])
            / "webnovel-writer"
            / "workspaces.json"
        )
    _write_registry(registry, outer_workspace, previous_book)

    with pytest.raises(FileNotFoundError):
        resolve_project(cwd=cwd)


def test_resolve_project_root_does_not_guess_default_child(tmp_path):
    _ensure_scripts_on_path()

    from project_locator import resolve_project_root

    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True, exist_ok=True)

    default_project = repo_root / "webnovel-project"
    (default_project / ".webnovel").mkdir(parents=True, exist_ok=True)
    (default_project / ".webnovel" / "state.json").write_text("{}", encoding="utf-8")

    nested = repo_root / "sub" / "dir"
    nested.mkdir(parents=True, exist_ok=True)

    with pytest.raises(FileNotFoundError):
        resolve_project_root(cwd=nested)


def test_resolve_project_root_stops_at_plugin_cache_root(tmp_path):
    _ensure_scripts_on_path()

    from project_locator import resolve_project_root

    (tmp_path / ".webnovel").mkdir(parents=True)
    (tmp_path / ".webnovel" / "state.json").write_text("{}", encoding="utf-8")
    plugin_root = tmp_path / ".codex" / "plugins" / "cache" / "novel-writer-codex"
    (plugin_root / ".codex-plugin").mkdir(parents=True)
    (plugin_root / ".codex-plugin" / "plugin.json").write_text("{}", encoding="utf-8")
    nested = plugin_root / "scripts" / "nested"
    nested.mkdir(parents=True)

    with pytest.raises(FileNotFoundError):
        resolve_project_root(cwd=nested)


def test_bind_and_resolve_project_uses_codex_pointer(tmp_path):
    _ensure_scripts_on_path()

    from project_locator import bind_current_project, resolve_project

    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    project_root = workspace / "凡人资本论"
    (project_root / ".webnovel").mkdir(parents=True, exist_ok=True)
    (project_root / ".webnovel" / "state.json").write_text("{}", encoding="utf-8")

    binding = bind_current_project(project_root, workspace_root=workspace)
    assert binding.pointer_path == workspace / ".codex" / ".webnovel-current-project"
    assert binding.pointer_path.is_file()
    assert binding.registry_path == Path(os.environ["WEBNOVEL_HOME"]) / "workspaces.json"
    assert not (workspace / ".claude").exists()

    resolved = resolve_project(cwd=workspace)
    assert resolved.project_root == project_root.resolve()
    assert resolved.resolved_from == "codex_pointer"
    assert resolved.compatibility_mode == "native"


def test_native_relative_pointer_never_resolves_from_codex_directory(tmp_path):
    _ensure_scripts_on_path()

    from project_locator import resolve_project

    workspace = tmp_path / "workspace"
    hidden_book = workspace / ".codex" / "book"
    (hidden_book / ".webnovel").mkdir(parents=True)
    (hidden_book / ".webnovel" / "state.json").write_text("{}", encoding="utf-8")
    (workspace / ".codex" / ".webnovel-current-project").write_text(
        "book", encoding="utf-8"
    )

    with pytest.raises(FileNotFoundError):
        resolve_project(cwd=workspace)


def test_native_relative_pointer_resolves_from_workspace_root(tmp_path):
    _ensure_scripts_on_path()

    from project_locator import resolve_project

    workspace = tmp_path / "workspace"
    project_root = workspace / "book"
    (project_root / ".webnovel").mkdir(parents=True)
    (project_root / ".webnovel" / "state.json").write_text("{}", encoding="utf-8")
    (workspace / ".codex").mkdir()
    (workspace / ".codex" / ".webnovel-current-project").write_text(
        "book", encoding="utf-8"
    )

    result = resolve_project(cwd=workspace)

    assert result.project_root == project_root.resolve()
    assert result.resolved_from == "codex_pointer"
    assert result.compatibility_mode == "native"


def test_binding_without_workspace_context_never_guesses_project_parent(tmp_path):
    _ensure_scripts_on_path()

    from project_locator import write_current_project_pointer

    workspace = tmp_path / "unconfirmed-parent"
    project_root = workspace / "book"
    (project_root / ".webnovel").mkdir(parents=True)
    (project_root / ".webnovel" / "state.json").write_text("{}", encoding="utf-8")

    assert write_current_project_pointer(project_root) is None
    assert not (workspace / ".codex").exists()
    assert not Path(os.environ["WEBNOVEL_HOME"]).exists()


def test_confirm_current_workspace_requires_real_context_and_rejects_plugin_cache(
    monkeypatch, tmp_path
):
    _ensure_scripts_on_path()

    import project_locator as locator

    original_find_plugin_root = locator._find_plugin_root
    monkeypatch.setattr(locator, "_find_plugin_root", lambda _start: None)

    workspace = tmp_path / "workspace"
    project_root = workspace / "book"
    nested = project_root / "drafts"
    nested.mkdir(parents=True)

    assert locator.confirm_current_workspace(project_root, cwd=workspace) == workspace.resolve()
    assert locator.confirm_current_workspace(project_root, cwd=nested) == project_root.resolve()
    assert locator.confirm_current_workspace(project_root, cwd=tmp_path / "unrelated") is None

    plugin_root = tmp_path / "plugin-cache"
    (plugin_root / ".codex-plugin").mkdir(parents=True)
    (plugin_root / ".codex-plugin" / "plugin.json").write_text("{}", encoding="utf-8")
    cached_project = plugin_root / "fixture-book"
    cached_project.mkdir()
    monkeypatch.setattr(locator, "_find_plugin_root", original_find_plugin_root)
    assert locator.confirm_current_workspace(cached_project, cwd=plugin_root) is None


def test_resolve_project_root_requires_explicit_path_to_be_project(tmp_path):
    _ensure_scripts_on_path()

    from project_locator import resolve_project_root

    workspace = tmp_path / "workspace"
    (workspace / ".git").mkdir(parents=True, exist_ok=True)
    project_root = workspace / "凡人资本论"
    (project_root / ".webnovel").mkdir(parents=True, exist_ok=True)
    (project_root / ".webnovel" / "state.json").write_text("{}", encoding="utf-8")

    with pytest.raises(FileNotFoundError):
        resolve_project_root(str(workspace))


def test_resolve_project_root_ignores_stale_codex_pointer_and_uses_native_registry(tmp_path):
    _ensure_scripts_on_path()

    from project_locator import resolve_project_root

    workspace = tmp_path / "workspace"
    (workspace / ".git").mkdir(parents=True, exist_ok=True)
    (workspace / ".codex").mkdir(parents=True, exist_ok=True)
    # stale pointer
    (workspace / ".codex" / ".webnovel-current-project").write_text(
        str(workspace / "missing-project"), encoding="utf-8"
    )

    project_root = workspace / "凡人资本论"
    (project_root / ".webnovel").mkdir(parents=True, exist_ok=True)
    (project_root / ".webnovel" / "state.json").write_text("{}", encoding="utf-8")
    _write_registry(Path(os.environ["WEBNOVEL_HOME"]) / "workspaces.json", workspace, project_root)

    from project_locator import resolve_project

    resolved = resolve_project(cwd=workspace)
    assert resolved.project_root == project_root.resolve()
    assert resolved.resolved_from == "codex_registry"


def test_resolve_project_ignores_malformed_pointer_and_context_free_last_used(tmp_path):
    _ensure_scripts_on_path()

    from project_locator import resolve_project

    workspace = tmp_path / "workspace"
    (workspace / ".codex").mkdir(parents=True)
    (workspace / ".codex" / ".webnovel-current-project").write_bytes(b"\xff\xfe")
    unrelated_project = tmp_path / "previous-book"
    (unrelated_project / ".webnovel").mkdir(parents=True)
    (unrelated_project / ".webnovel" / "state.json").write_text("{}", encoding="utf-8")
    registry = Path(os.environ["WEBNOVEL_HOME"]) / "workspaces.json"
    registry.parent.mkdir(parents=True)
    registry.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "workspaces": {},
                "last_used_project_root": str(unrelated_project.resolve()),
                "updated_at": "2026-08-07T00:00:00",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(FileNotFoundError):
        resolve_project(cwd=workspace)


def test_resolve_project_root_prefers_native_registry(monkeypatch, tmp_path):
    _ensure_scripts_on_path()

    from project_locator import resolve_project

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

    result = resolve_project(cwd=workspace)
    assert result.project_root == native_project.resolve()
    assert result.resolved_from == "codex_registry"
    assert result.compatibility_mode == "native"


def test_resolve_project_root_reads_legacy_registry_without_modifying_it(tmp_path):
    _ensure_scripts_on_path()

    from project_locator import resolve_project

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    legacy_project = tmp_path / "legacy-book"
    (legacy_project / ".webnovel").mkdir(parents=True)
    (legacy_project / ".webnovel" / "state.json").write_text("{}", encoding="utf-8")

    legacy_registry = Path(os.environ["WEBNOVEL_CLAUDE_HOME"]) / "webnovel-writer" / "workspaces.json"
    _write_registry(legacy_registry, workspace, legacy_project)
    before = _file_fingerprint(legacy_registry)

    result = resolve_project(cwd=workspace)
    assert result.project_root == legacy_project.resolve()
    assert result.resolved_from == "legacy_registry"
    assert result.compatibility_mode == "legacy_read_only"
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


def test_resolution_order_cli_env_cwd_before_pointers(monkeypatch, tmp_path):
    _ensure_scripts_on_path()

    from project_locator import resolve_project

    roots = {}
    for name in ("cli", "env", "cwd", "pointer"):
        root = tmp_path / name
        (root / ".webnovel").mkdir(parents=True)
        (root / ".webnovel" / "state.json").write_text("{}", encoding="utf-8")
        roots[name] = root.resolve()

    workspace = roots["cwd"]
    (workspace / ".codex").mkdir()
    (workspace / ".codex" / ".webnovel-current-project").write_text(
        str(roots["pointer"]), encoding="utf-8"
    )
    monkeypatch.setenv("WEBNOVEL_PROJECT_ROOT", str(roots["env"]))

    cli_result = resolve_project(str(roots["cli"]), cwd=workspace)
    assert (cli_result.project_root, cli_result.resolved_from) == (roots["cli"], "cli")

    env_result = resolve_project(cwd=workspace)
    assert (env_result.project_root, env_result.resolved_from) == (roots["env"], "env")

    monkeypatch.delenv("WEBNOVEL_PROJECT_ROOT")
    cwd_result = resolve_project(cwd=workspace)
    assert (cwd_result.project_root, cwd_result.resolved_from) == (roots["cwd"], "cwd")


def test_invalid_explicit_root_is_not_overridden_by_legacy_registry(tmp_path):
    _ensure_scripts_on_path()

    from project_locator import resolve_project

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    legacy_project = tmp_path / "legacy-book"
    (legacy_project / ".webnovel").mkdir(parents=True)
    (legacy_project / ".webnovel" / "state.json").write_text("{}", encoding="utf-8")
    _write_registry(
        Path(os.environ["WEBNOVEL_CLAUDE_HOME"]) / "webnovel-writer" / "workspaces.json",
        workspace,
        legacy_project,
    )

    with pytest.raises(FileNotFoundError):
        resolve_project(str(workspace), cwd=workspace)


@pytest.mark.parametrize("explicit", ["", "   "])
def test_empty_explicit_root_never_falls_back_to_cwd(tmp_path, explicit):
    _ensure_scripts_on_path()

    from project_locator import resolve_project

    (tmp_path / ".webnovel").mkdir()
    (tmp_path / ".webnovel" / "state.json").write_text("{}", encoding="utf-8")

    with pytest.raises(FileNotFoundError, match="Explicit project root is empty"):
        resolve_project(explicit, cwd=tmp_path)


def test_empty_project_root_environment_never_falls_back_to_cwd(monkeypatch, tmp_path):
    _ensure_scripts_on_path()

    from project_locator import resolve_project

    (tmp_path / ".webnovel").mkdir()
    (tmp_path / ".webnovel" / "state.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("WEBNOVEL_PROJECT_ROOT", "")

    with pytest.raises(FileNotFoundError, match="WEBNOVEL_PROJECT_ROOT is set but empty"):
        resolve_project(cwd=tmp_path)


def test_legacy_pointer_is_read_only_and_marked(tmp_path):
    _ensure_scripts_on_path()

    from project_locator import resolve_project

    workspace = tmp_path / "旧工作区 (A&B)"
    (workspace / ".claude").mkdir(parents=True)
    project_root = tmp_path / "跨盘语义-书😀"
    (project_root / ".webnovel").mkdir(parents=True)
    (project_root / ".webnovel" / "state.json").write_text("{}", encoding="utf-8")
    pointer = workspace / ".claude" / ".webnovel-current-project"
    pointer.write_text(str(project_root.resolve()), encoding="utf-8")
    before = _file_fingerprint(pointer)

    result = resolve_project(cwd=workspace)

    assert result.project_root == project_root.resolve()
    assert result.resolved_from == "legacy_pointer"
    assert result.compatibility_mode == "legacy_read_only"
    assert _file_fingerprint(pointer) == before


def test_legacy_relative_pointer_keeps_pointer_directory_compatibility(tmp_path):
    _ensure_scripts_on_path()

    from project_locator import resolve_project

    workspace = tmp_path / "legacy-workspace"
    project_root = workspace / ".claude" / "legacy-book"
    (project_root / ".webnovel").mkdir(parents=True)
    (project_root / ".webnovel" / "state.json").write_text("{}", encoding="utf-8")
    pointer = workspace / ".claude" / ".webnovel-current-project"
    pointer.write_text("legacy-book", encoding="utf-8")

    result = resolve_project(cwd=workspace)

    assert result.project_root == project_root.resolve()
    assert result.resolved_from == "legacy_pointer"
    assert result.compatibility_mode == "legacy_read_only"


@pytest.mark.windows
def test_codex_pointer_preserves_absolute_cross_drive_unicode_path(monkeypatch, tmp_path):
    _ensure_scripts_on_path()

    import project_locator as locator

    workspace = tmp_path / "workspace"
    pointer = workspace / ".codex" / ".webnovel-current-project"
    pointer.parent.mkdir(parents=True)
    raw_target = r"Z:\中文 空格 (括号) & Unicode😀\书项目"
    pointer.write_text(raw_target, encoding="utf-8")
    expected = locator.normalize_windows_path(raw_target).expanduser()
    monkeypatch.setattr(
        locator,
        "_is_project_root",
        lambda path: os.path.normcase(str(path)) == os.path.normcase(str(expected)),
    )

    result = locator.resolve_project(cwd=workspace)

    assert os.path.normcase(str(result.project_root)) == os.path.normcase(str(expected))
    assert result.resolved_from == "codex_pointer"
    assert result.compatibility_mode == "native"


def test_registry_does_not_match_workspace_prefix_collision(tmp_path):
    _ensure_scripts_on_path()

    from project_locator import resolve_project

    registered_workspace = tmp_path / "workspace"
    current_workspace = tmp_path / "workspace-old"
    registered_workspace.mkdir()
    current_workspace.mkdir()
    project_root = tmp_path / "book"
    (project_root / ".webnovel").mkdir(parents=True)
    (project_root / ".webnovel" / "state.json").write_text("{}", encoding="utf-8")
    _write_registry(
        Path(os.environ["WEBNOVEL_HOME"]) / "workspaces.json",
        registered_workspace,
        project_root,
    )

    with pytest.raises(FileNotFoundError):
        resolve_project(cwd=current_workspace)
