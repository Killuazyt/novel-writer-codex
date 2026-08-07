#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from pathlib import Path

import pytest


def _project(root: Path) -> Path:
    (root / ".webnovel").mkdir(parents=True)
    (root / ".webnovel" / "state.json").write_text("{}", encoding="utf-8")
    return root.resolve()


def test_dashboard_server_uses_shared_codex_pointer(monkeypatch, tmp_path):
    from dashboard.server import _resolve_project_root

    workspace = tmp_path / "工作区 (A&B)"
    workspace.mkdir()
    project_root = _project(tmp_path / "小说😀")
    (workspace / ".codex").mkdir()
    (workspace / ".codex" / ".webnovel-current-project").write_text(
        str(project_root), encoding="utf-8"
    )
    monkeypatch.chdir(workspace)
    monkeypatch.delenv("WEBNOVEL_PROJECT_ROOT", raising=False)

    assert _resolve_project_root(None) == project_root


def test_dashboard_server_rejects_explicit_workspace_root(tmp_path):
    from dashboard.server import _resolve_project_root

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _project(workspace / "book")

    with pytest.raises(SystemExit) as exc:
        _resolve_project_root(str(workspace))

    assert exc.value.code == 1
