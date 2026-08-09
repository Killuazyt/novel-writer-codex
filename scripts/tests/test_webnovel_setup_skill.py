#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from pathlib import Path

import validate_codex_adapter
import validate_plugin_package


PLUGIN_ROOT = Path(__file__).resolve().parents[2]
SKILL = PLUGIN_ROOT / "skills" / "webnovel-setup" / "SKILL.md"
OPENAI_YAML = SKILL.parent / "agents" / "openai.yaml"


def test_setup_skill_has_only_supported_frontmatter_and_matching_metadata():
    frontmatter = validate_plugin_package._frontmatter(SKILL)
    interface, error = validate_plugin_package._openai_interface(OPENAI_YAML)

    assert set(frontmatter) == {"name", "description"}
    assert frontmatter["name"] == "webnovel-setup"
    assert error == ""
    assert interface["display_name"]
    assert interface["short_description"]
    assert "$webnovel-setup" in interface["default_prompt"]


def test_setup_skill_checks_then_waits_before_apply():
    text = SKILL.read_text(encoding="utf-8")

    check_index = text.index("--check --format json")
    wait_index = text.index("Wait for the answer")
    apply_index = text.index("--apply --format json")

    assert check_index < wait_index < apply_index
    assert "Never overwrite an unmanaged same-name agent" in text
    assert "Do not try the newly installed agents in the current task" in text


def test_setup_skill_links_the_shared_runtime_and_interaction_contracts():
    text = SKILL.read_text(encoding="utf-8")
    expected = (
        PLUGIN_ROOT / "references" / "codex" / "runtime-invocation.md",
        PLUGIN_ROOT / "references" / "codex" / "interaction-contract.md",
    )

    for path in expected:
        assert path.is_file()
        assert f"../../references/codex/{path.name}" in text


def test_m3_operational_surfaces_are_host_neutral():
    errors = validate_codex_adapter.scan_host_neutrality(PLUGIN_ROOT)
    relevant = [
        item
        for item in errors
        if item["path"].startswith(("skills/", "references/agents/", "references/codex/"))
    ]

    assert relevant == []
