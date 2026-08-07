#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import pytest


def _plugin_fixture(tmp_path):
    plugin_root = tmp_path / "plugin-root"
    manifest = plugin_root / ".codex-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}\n", encoding="utf-8")
    anchor = plugin_root / "scripts" / "nested" / "caller.py"
    anchor.parent.mkdir(parents=True)
    anchor.write_text("# fixture\n", encoding="utf-8")
    return plugin_root, anchor

def test_webnovel_home_prefers_explicit_value(monkeypatch, tmp_path):
    from host_paths import resolve_webnovel_home

    explicit_home = tmp_path / "explicit-webnovel-home"
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("WEBNOVEL_HOME", str(explicit_home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    assert resolve_webnovel_home() == explicit_home.resolve()


def test_webnovel_home_falls_back_to_codex_home(monkeypatch, tmp_path):
    from host_paths import resolve_codex_home, resolve_webnovel_home

    codex_home = tmp_path / "codex-home"
    monkeypatch.delenv("WEBNOVEL_HOME", raising=False)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    assert resolve_codex_home() == codex_home.resolve()
    assert resolve_webnovel_home() == (codex_home / "novel-writer-codex").resolve()


def test_legacy_claude_home_is_compatibility_only_and_has_stable_priority(monkeypatch, tmp_path):
    from host_paths import resolve_legacy_claude_home

    compatibility_home = tmp_path / "compatibility-home"
    claude_home = tmp_path / "claude-home"
    monkeypatch.setenv("WEBNOVEL_CLAUDE_HOME", str(compatibility_home))
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

    assert resolve_legacy_claude_home() == compatibility_home.resolve()
    assert not compatibility_home.exists()


def test_plugin_root_is_discovered_from_calling_file(tmp_path):
    from host_paths import resolve_plugin_root

    plugin_root, anchor = _plugin_fixture(tmp_path)

    assert resolve_plugin_root(anchor) == plugin_root.resolve()


def test_reference_resolution_is_per_file_and_prefers_codex(tmp_path):
    from host_paths import resolve_reference_file

    plugin_root, anchor = _plugin_fixture(tmp_path)
    project_root = tmp_path / "中文 空格 (括号) & Ω"
    codex_reference = project_root / ".codex" / "references" / "shared.md"
    legacy_reference = project_root / ".claude" / "references" / "shared.md"
    bundled_only = plugin_root / "references" / "bundled-only.md"
    codex_reference.parent.mkdir(parents=True)
    legacy_reference.parent.mkdir(parents=True)
    bundled_only.parent.mkdir(parents=True)
    codex_reference.write_text("codex\n", encoding="utf-8")
    legacy_reference.write_text("legacy\n", encoding="utf-8")
    bundled_only.write_text("bundled\n", encoding="utf-8")

    native = resolve_reference_file(project_root, "shared.md", anchor=anchor)
    bundled = resolve_reference_file(project_root, "bundled-only.md", anchor=anchor)

    assert native is not None
    assert native.path == codex_reference.resolve()
    assert native.resolved_from == "codex_project"
    assert native.compatibility_mode == "native"
    assert bundled is not None
    assert bundled.path == bundled_only.resolve()
    assert bundled.resolved_from == "bundled"
    assert bundled.compatibility_mode == "native"


def test_reference_resolution_reads_legacy_without_writing_native_state(tmp_path):
    from host_paths import resolve_reference_file

    _, anchor = _plugin_fixture(tmp_path)
    project_root = tmp_path / "book"
    legacy_reference = project_root / ".claude" / "references" / "legacy.md"
    legacy_reference.parent.mkdir(parents=True)
    legacy_reference.write_text("legacy read only\n", encoding="utf-8")
    before = legacy_reference.read_bytes()

    resolution = resolve_reference_file(project_root, "legacy.md", anchor=anchor)

    assert resolution is not None
    assert resolution.path == legacy_reference.resolve()
    assert resolution.resolved_from == "legacy_project"
    assert resolution.compatibility_mode == "legacy_read_only"
    assert legacy_reference.read_bytes() == before
    assert not (project_root / ".codex").exists()


@pytest.mark.parametrize("relative_path", ["../escape.md", "nested/../../escape.md"])
def test_reference_resolution_rejects_path_traversal(tmp_path, relative_path):
    from host_paths import resolve_reference_file

    _, anchor = _plugin_fixture(tmp_path)

    with pytest.raises(ValueError, match="references"):
        resolve_reference_file(tmp_path / "book", relative_path, anchor=anchor)


def test_reference_resolution_rejects_absolute_path(tmp_path):
    from host_paths import resolve_reference_file

    _, anchor = _plugin_fixture(tmp_path)

    with pytest.raises(ValueError, match="references"):
        resolve_reference_file(tmp_path / "book", tmp_path / "outside.md", anchor=anchor)
