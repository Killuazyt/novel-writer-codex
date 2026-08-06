#!/usr/bin/env python3
# -*- coding: utf-8 -*-

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
