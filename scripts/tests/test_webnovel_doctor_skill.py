#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from pathlib import Path

import validate_codex_adapter
import validate_plugin_package


PLUGIN_ROOT = Path(__file__).resolve().parents[2]
SKILL = PLUGIN_ROOT / "skills" / "webnovel-doctor" / "SKILL.md"
OPENAI_YAML = SKILL.parent / "agents" / "openai.yaml"


def test_doctor_skill_has_supported_metadata_and_explicit_prompt():
    frontmatter = validate_plugin_package._frontmatter(SKILL)
    interface, error = validate_plugin_package._openai_interface(OPENAI_YAML)

    assert set(frontmatter) == {"name", "description"}
    assert frontmatter["name"] == "webnovel-doctor"
    assert error == ""
    assert interface["display_name"]
    assert interface["short_description"]
    assert "$webnovel-doctor" in interface["default_prompt"]


def test_doctor_skill_uses_status_then_phase_aware_json_doctor():
    text = SKILL.read_text(encoding="utf-8")

    status_index = text.index("project-status --format json")
    doctor_index = text.index("doctor --format json")

    assert status_index < doctor_index
    assert "--chapter <POSITIVE_INTEGER>" in text
    assert "--deep" in text
    assert "exit codes" in text
    assert "`1`: the doctor found" in text


def test_doctor_skill_is_read_only_and_redacts_secrets():
    text = SKILL.read_text(encoding="utf-8")

    for boundary in (
        "Do not repair files",
        "install dependencies",
        "start Dashboard",
        "open a browser",
        "change Git state",
        "access the network",
        "Never print API-key values",
    ):
        assert boundary in text


def test_doctor_skill_surface_is_host_neutral():
    errors = validate_codex_adapter.scan_host_neutrality(PLUGIN_ROOT)
    relevant = [item for item in errors if item["path"].startswith("skills/webnovel-doctor/")]

    assert relevant == []
