#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from pathlib import Path

from data_modules import webnovel


def _make_runtime_tree(tmp_path: Path) -> Path:
    scripts_dir = tmp_path / "plugin" / "scripts"
    (scripts_dir / "data_modules").mkdir(parents=True)
    (scripts_dir / "data_modules" / "__init__.py").write_text("", encoding="utf-8")
    (scripts_dir / "webnovel.py").write_text("", encoding="utf-8")
    (scripts_dir / "extract_chapter_context.py").write_text("", encoding="utf-8")
    return scripts_dir


def test_preflight_checks_runtime_capabilities_without_requiring_skill(monkeypatch, tmp_path):
    scripts_dir = _make_runtime_tree(tmp_path)
    project_root = tmp_path / "book"
    project_root.mkdir()
    monkeypatch.setattr(webnovel, "_scripts_dir", lambda: scripts_dir)
    monkeypatch.setattr(webnovel, "_resolve_root", lambda _explicit: project_root)
    monkeypatch.setattr(
        webnovel,
        "build_story_runtime_health",
        lambda _root: {"chapter": 0, "mainline_ready": False},
    )

    report = webnovel._build_preflight_report(str(project_root))

    assert report["schema_version"] == "webnovel-preflight/v1"
    assert report["ok"] is True
    assert report["errors"] == []
    assert not (scripts_dir.parent / "skills" / "webnovel-write").exists()
    assert "skill_root" not in {item["name"] for item in report["checks"]}
    assert {
        "runtime_package",
        "entry_script",
        "extract_context_script",
        "project_root",
        "story_runtime_health",
    }.issubset({item["name"] for item in report["checks"]})


def test_preflight_reports_runtime_capability_errors_structurally(monkeypatch, tmp_path):
    scripts_dir = tmp_path / "empty-scripts"
    scripts_dir.mkdir()
    project_root = tmp_path / "book"
    project_root.mkdir()
    monkeypatch.setattr(webnovel, "_scripts_dir", lambda: scripts_dir)
    monkeypatch.setattr(webnovel, "_resolve_root", lambda _explicit: project_root)
    monkeypatch.setattr(webnovel, "build_story_runtime_health", lambda _root: {})

    report = webnovel._build_preflight_report(str(project_root))

    assert report["ok"] is False
    assert {item["code"] for item in report["errors"]} == {
        "runtime_package_missing",
        "unified_cli_missing",
        "extract_context_missing",
    }
    assert all(
        {"code", "severity", "name", "ok", "message", "path", "repair"}
        <= set(item)
        for item in report["errors"]
    )


def test_preflight_keeps_project_root_when_story_health_fails(monkeypatch, tmp_path):
    scripts_dir = _make_runtime_tree(tmp_path)
    project_root = tmp_path / "book"
    project_root.mkdir()
    monkeypatch.setattr(webnovel, "_scripts_dir", lambda: scripts_dir)
    monkeypatch.setattr(webnovel, "_resolve_root", lambda _explicit: project_root)

    def fail_health(_root):
        raise OSError("simulated health read failure")

    monkeypatch.setattr(webnovel, "build_story_runtime_health", fail_health)
    report = webnovel._build_preflight_report(str(project_root))

    checks = {item["name"]: item for item in report["checks"]}
    assert report["project_root"] == str(project_root)
    assert report["project_root_error"] == ""
    assert checks["project_root"]["ok"] is True
    assert checks["story_runtime_health"]["ok"] is False
    assert report["errors"][-1]["code"] == "story_runtime_health_failed"


def test_preflight_does_not_run_health_when_project_root_resolution_fails(monkeypatch, tmp_path):
    scripts_dir = _make_runtime_tree(tmp_path)
    health_called = False
    monkeypatch.setattr(webnovel, "_scripts_dir", lambda: scripts_dir)

    def fail_root(_explicit):
        raise FileNotFoundError("missing project")

    def record_health(_root):
        nonlocal health_called
        health_called = True
        return {}

    monkeypatch.setattr(webnovel, "_resolve_root", fail_root)
    monkeypatch.setattr(webnovel, "build_story_runtime_health", record_health)
    report = webnovel._build_preflight_report(None)

    assert report["ok"] is False
    assert report["project_root"] == ""
    assert report["project_root_error"]
    assert health_called is False
    assert report["errors"][-1]["code"] == "project_root_not_found"
