#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from pathlib import Path

import validate_codex_adapter
import validate_plugin_package


PLUGIN_ROOT = Path(__file__).resolve().parents[2]
SKILL = PLUGIN_ROOT / "skills" / "webnovel-dashboard" / "SKILL.md"
OPENAI_YAML = SKILL.parent / "agents" / "openai.yaml"


def test_dashboard_skill_has_supported_metadata_and_explicit_prompt():
    frontmatter = validate_plugin_package._frontmatter(SKILL)
    interface, error = validate_plugin_package._openai_interface(OPENAI_YAML)

    assert set(frontmatter) == {"name", "description"}
    assert frontmatter["name"] == "webnovel-dashboard"
    assert error == ""
    assert 25 <= len(interface["short_description"]) <= 64
    assert "$webnovel-dashboard" in interface["default_prompt"]


def test_dashboard_skill_uses_only_unified_lifecycle_commands():
    text = SKILL.read_text(encoding="utf-8")

    for action in ("dashboard status", "dashboard start", "dashboard stop"):
        assert action in text
    assert "--host 127.0.0.1 --port 0 --no-browser --format json" in text
    assert "dashboard.server" in text
    assert "Do not invoke `dashboard.server` directly" in text
    assert "run `dashboard status --format json` once" in text


def test_dashboard_skill_preserves_lifecycle_safety_boundaries():
    text = SKILL.read_text(encoding="utf-8")

    for boundary in (
        "Bind only to `127.0.0.1`",
        "Never open a browser automatically",
        "Do not install Python or Node dependencies",
        "never signal a PID yourself",
        "Do not expose the private instance token",
        "must not change novel facts",
    ):
        assert boundary in text


def test_dashboard_skill_surface_is_host_neutral():
    errors = validate_codex_adapter.scan_host_neutrality(PLUGIN_ROOT)
    relevant = [item for item in errors if item["path"].startswith("skills/webnovel-dashboard/")]

    assert relevant == []
