#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import validate_codex_adapter
import validate_plugin_package


PLUGIN_ROOT = Path(__file__).resolve().parents[2]
SKILL = PLUGIN_ROOT / "skills" / "webnovel-query" / "SKILL.md"
OPENAI_YAML = SKILL.parent / "agents" / "openai.yaml"
FIXTURE = PLUGIN_ROOT / "evals" / "fixtures" / "codex_query" / "query_cases.json"


def test_query_skill_has_supported_metadata_and_explicit_prompt():
    frontmatter = validate_plugin_package._frontmatter(SKILL)
    interface, error = validate_plugin_package._openai_interface(OPENAI_YAML)

    assert set(frontmatter) == {"name", "description"}
    assert frontmatter["name"] == "webnovel-query"
    assert error == ""
    assert "$webnovel-query" in interface["default_prompt"]


def test_query_skill_routes_all_five_read_only_query_types():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    cases = {item["id"]: item for item in payload["cases"]}

    assert payload["schema_version"] == "webnovel-query-cases/v1"
    assert set(cases) == {"entity_state", "relationships", "world_rules", "open_loops", "context"}
    assert cases["entity_state"]["request"]["query_type"] == "entity_state"
    assert cases["relationships"]["request"]["query_type"] == "relationships"
    assert cases["world_rules"]["command"][-1] == "query-rules"
    assert cases["world_rules"]["request"]["domain"] == "力量体系"
    assert "--chapter" not in cases["world_rules"]["command"]
    assert cases["context"]["command"][-2:] == ["--chapter", "35"]


def test_query_skill_corrects_upstream_rule_and_context_invocations():
    text = SKILL.read_text(encoding="utf-8")

    assert "query-rules" in text
    assert "query-rules --chapter" not in text
    assert "load-context --chapter <N>" in text
    assert "read-summary --chapter <N>" in text
    assert "--read-only --with-provenance" in text


def test_query_skill_requires_provenance_and_explicit_legacy_fallback():
    text = SKILL.read_text(encoding="utf-8")

    for required in (
        "latest accepted",
        "derived read model",
        "legacy_projection_fallback",
        "line: not applicable",
        "real line numbers",
        "authoritative",
    ):
        assert required in text


def test_query_skill_keeps_user_text_out_of_shell_parsing():
    text = SKILL.read_text(encoding="utf-8")
    references = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((SKILL.parent / "references").rglob("*.md"))
    )

    assert "Never interpolate user prose into a shell command" in text
    assert "webnovel-query-request/v1" in text
    assert "User text must never appear in the PowerShell command string" in text
    for token in ("Chinese names", "single and double quotes", "newlines", "ampersands", "semicolons", "pipes", "backticks"):
        assert token in text
    assert "query-entity-state --entity <value>" not in references
    assert "query-relationships --entity <value>" not in references
    assert "query-rules --domain <value>" not in references
    assert "--request-file <ABSOLUTE_QUERY_REQUEST_JSON>" in references


def test_query_skill_and_private_references_are_host_neutral():
    errors = validate_codex_adapter.scan_host_neutrality(PLUGIN_ROOT)
    relevant = [item for item in errors if item["path"].startswith("skills/webnovel-query/")]

    assert relevant == []


def _powershell_literal(value: Path) -> str:
    return "'" + str(value.resolve()).replace("'", "''") + "'"


def _powershell() -> str:
    executable = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    if executable is None:
        import pytest

        pytest.skip("PowerShell is required for the Windows command-string boundary smoke")
    return executable


def test_query_request_file_keeps_metacharacters_out_of_real_powershell_command(tmp_path):
    from data_modules.codex_agent_runtime import snapshot_protected_state

    project = tmp_path / "中文 项目 (A) & B"
    webnovel = project / ".webnovel"
    webnovel.mkdir(parents=True)
    (webnovel / "state.json").write_text("{}", encoding="utf-8")
    sentinel = tmp_path / "shell-injection-sentinel"
    entity = (
        "角色\"'\n"
        f"$(New-Item -ItemType File -LiteralPath '{sentinel}') &;|` 不应执行"
    )
    with sqlite3.connect(str(webnovel / "index.db")) as conn:
        conn.execute("CREATE TABLE entities (id TEXT PRIMARY KEY, canonical_name TEXT)")
        conn.execute(
            "CREATE TABLE state_changes (id INTEGER PRIMARY KEY, entity_id TEXT, field TEXT, new_value TEXT, chapter INTEGER)"
        )
        conn.execute(
            "CREATE TABLE relationship_events (id INTEGER PRIMARY KEY, from_entity TEXT, to_entity TEXT, type TEXT, description TEXT, chapter INTEGER)"
        )
        conn.execute("INSERT INTO entities (id, canonical_name) VALUES (?, ?)", (entity, "特殊角色"))

    request_file = tmp_path / "entity query request.json"
    request_file.write_text(
        json.dumps(
            {
                "schema_version": "webnovel-query-request/v1",
                "query_type": "entity_state",
                "entity": entity,
                "at_chapter": 1,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    command = " ".join(
        [
            "&",
            _powershell_literal(Path(sys.executable)),
            "-X utf8",
            _powershell_literal(PLUGIN_ROOT / "scripts" / "webnovel.py"),
            "--project-root",
            _powershell_literal(project),
            "knowledge query-entity-state --request-file",
            _powershell_literal(request_file),
        ]
    )
    assert entity not in command
    before = snapshot_protected_state(project)
    completed = subprocess.run(
        [_powershell(), "-NoProfile", "-NonInteractive", "-Command", command],
        shell=False,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["data"]["entity_query"] == entity
    assert payload["schema_version"] == "webnovel-query-result/v1"
    assert payload["query_type"] == "entity_state"
    assert not sentinel.exists()
    assert snapshot_protected_state(project) == before


def test_memory_contract_request_file_domain_is_safe_through_powershell_passthrough(tmp_path):
    from data_modules.codex_agent_runtime import snapshot_protected_state
    from data_modules.config import DataModulesConfig
    from data_modules.memory.schema import MemoryItem
    from data_modules.memory.store import ScratchpadManager

    project = tmp_path / "规则项目 中文 & (B)"
    webnovel = project / ".webnovel"
    webnovel.mkdir(parents=True)
    (webnovel / "state.json").write_text("{}", encoding="utf-8")
    sentinel = tmp_path / "domain-injection-sentinel"
    domain = f"力量'\"\n$(New-Item -ItemType File -LiteralPath '{sentinel}') &;|`"
    ScratchpadManager(DataModulesConfig.from_project_root(project)).upsert_item(
        MemoryItem(
            id="rule-safe-domain",
            layer="semantic",
            category="world_rule",
            subject=domain,
            field="上限",
            value="九境",
            source_chapter=1,
        )
    )
    request_file = tmp_path / "domain request.json"
    request_file.write_text(
        json.dumps(
            {
                "schema_version": "webnovel-query-request/v1",
                "query_type": "world_rules",
                "domain": domain,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    command = " ".join(
        [
            "&",
            _powershell_literal(Path(sys.executable)),
            "-X utf8",
            _powershell_literal(PLUGIN_ROOT / "scripts" / "webnovel.py"),
            "--project-root",
            _powershell_literal(project),
            "memory-contract --read-only --with-provenance --request-file",
            _powershell_literal(request_file),
            "query-rules",
        ]
    )
    assert domain not in command
    before = snapshot_protected_state(project)

    completed = subprocess.run(
        [_powershell(), "-NoProfile", "-NonInteractive", "-Command", command],
        shell=False,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["schema_version"] == "webnovel-query-result/v1"
    assert payload["query_type"] == "world_rules"
    assert payload["data"][0]["subject"] == domain
    assert not sentinel.exists()
    assert snapshot_protected_state(project) == before
