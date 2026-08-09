#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import sys
from pathlib import Path

import validate_codex_adapter as validator


def _write_text(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")


def _make_valid_root(tmp_path: Path) -> Path:
    root = tmp_path / "plugin"
    manifest = {
        "name": "novel-writer-codex",
        "version": "0.0.1",
        "description": "test plugin",
        "author": {"name": "test"},
        "license": "GPL-3.0",
        "interface": {"displayName": "Test"},
    }
    hooks = {
        "description": "test hooks",
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "apply_patch",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "python ${PLUGIN_ROOT}/hooks/test.py",
                            "commandWindows": "python ${PLUGIN_ROOT}/hooks/test.py",
                        }
                    ],
                }
            ]
        },
    }
    _write_text(root / ".codex-plugin" / "plugin.json", json.dumps(manifest))
    _write_text(root / "hooks" / "hooks.json", json.dumps(hooks))
    for required in ("LICENSE", "README.md", "UPSTREAM.md", "AGENTS.md", "docs/PORTING.md"):
        _write_text(root / required, "test\n")
    return root


def _run_main(monkeypatch, capsys, root: Path) -> tuple[int, dict, str]:
    monkeypatch.setattr(
        sys,
        "argv",
        ["validate_codex_adapter.py", "--root", str(root), "--format", "json"],
    )
    code = validator.main()
    captured = capsys.readouterr()
    return code, json.loads(captured.out), captured.err


def test_validator_json_success_is_single_structured_object(monkeypatch, capsys, tmp_path):
    code, report, stderr = _run_main(monkeypatch, capsys, _make_valid_root(tmp_path))

    assert code == 0
    assert report["schema_version"] == "codex-adapter-validator/v1"
    assert report["ok"] is True
    assert report["error_count"] == 0
    assert report["errors"] == []
    assert stderr == ""


def test_validator_missing_manifest_is_structured_validation_failure(monkeypatch, capsys, tmp_path):
    root = _make_valid_root(tmp_path)
    (root / ".codex-plugin" / "plugin.json").unlink()

    code, report, stderr = _run_main(monkeypatch, capsys, root)

    assert code == 1
    assert report["ok"] is False
    assert report["error_count"] == 1
    assert report["errors"][0]["code"] == "manifest_missing"
    assert set(report["errors"][0]) == {"code", "path", "message", "repair"}
    assert "Traceback" not in stderr


def test_validator_malformed_json_is_structured_validation_failure(monkeypatch, capsys, tmp_path):
    root = _make_valid_root(tmp_path)
    _write_text(root / ".codex-plugin" / "plugin.json", "{broken")

    code, report, stderr = _run_main(monkeypatch, capsys, root)

    assert code == 1
    assert report["errors"][0]["code"] == "manifest_invalid_json"
    assert "Traceback" not in stderr


def test_validator_rejects_old_plugin_identity(monkeypatch, capsys, tmp_path):
    root = _make_valid_root(tmp_path)
    manifest_path = root / ".codex-plugin" / "plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["name"] = "webnovel-writer"
    _write_text(manifest_path, json.dumps(manifest))

    code, report, stderr = _run_main(monkeypatch, capsys, root)

    assert code == 1
    assert any(item["code"] == "manifest_name_unexpected" for item in report["errors"])
    assert "Traceback" not in stderr


def test_static_scan_rejects_plugin_root_dependency_in_skill(tmp_path):
    root = _make_valid_root(tmp_path)
    skill = root / "skills" / "demo" / "SKILL.md"
    _write_text(
        skill,
        "---\nname: demo\ndescription: demo\n---\n\nRun ${PLUGIN_ROOT}/scripts/webnovel.py.\n",
    )

    errors = validator.validate(root)

    assert any(item["code"] == "skill_plugin_root_dependency" for item in errors)


def test_static_scan_rejects_claude_frontmatter_tools_and_bash_only_syntax(tmp_path):
    root = _make_valid_root(tmp_path)
    skill = root / "skills" / "demo" / "SKILL.md"
    _write_text(
        skill,
        "---\n"
        "name: demo\n"
        "description: demo\n"
        "allowed-tools: Read, Bash\n"
        "---\n\n"
        "export PROJECT_ROOT=/tmp/book\n",
    )

    errors = validator.validate(root)
    codes = {item["code"] for item in errors}

    assert "claude_frontmatter" in codes
    assert "claude_tool_name" in codes
    assert "bash_export" in codes


def test_static_scan_rejects_slash_commands_in_skill_agent_and_hook_surfaces(tmp_path):
    root = _make_valid_root(tmp_path)
    _write_text(
        root / "skills" / "demo" / "SKILL.md",
        "---\nname: demo\ndescription: demo\n---\n\nUse /webnovel-write.\n",
    )
    _write_text(
        root / ".codex" / "agents" / "demo.toml",
        'developer_instructions = "Run /webnovel-plan."\n',
    )
    hooks_path = root / "hooks" / "hooks.json"
    hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
    hooks["hooks"]["PreToolUse"][0]["hooks"][0]["command"] += " /webnovel-review"
    _write_text(hooks_path, json.dumps(hooks))

    errors = validator.validate(root)

    slash_paths = {
        item["path"].split(":", 1)[0]
        for item in errors
        if item["code"] == "claude_slash_command"
    }
    assert slash_paths == {
        "skills/demo/SKILL.md",
        ".codex/agents/demo.toml",
        "hooks/hooks.json",
    }


def test_static_scan_narrow_allowlist_excludes_migration_docs_and_test_fixtures(tmp_path):
    root = _make_valid_root(tmp_path)
    forbidden_fixture = (
        "allowed-tools: Read, Bash\n"
        "export PROJECT_ROOT=/tmp/book\n"
        "Run ${PLUGIN_ROOT}/scripts/webnovel.py or /webnovel-write.\n"
    )
    _write_text(root / "docs" / "migration-example.md", forbidden_fixture)
    _write_text(root / "scripts" / "tests" / "test_migration_fixture.py", forbidden_fixture)

    errors = validator.validate(root)

    assert errors == []


def test_static_scan_includes_canonical_codex_and_agent_references(tmp_path):
    root = _make_valid_root(tmp_path)
    _write_text(
        root / "references" / "codex" / "runtime-invocation.md",
        "Run ${PLUGIN_ROOT}/scripts/webnovel.py.\n",
    )
    _write_text(
        root / "references" / "agents" / "writer.md",
        "Use /webnovel-write.\n",
    )

    errors = validator.validate(root)

    paths = {item["path"].split(":", 1)[0] for item in errors}
    assert "references/codex/runtime-invocation.md" in paths
    assert "references/agents/writer.md" in paths


def test_static_scan_allows_plugin_root_in_hook_config(tmp_path):
    root = _make_valid_root(tmp_path)

    errors = validator.validate(root)

    assert not any(item["code"] == "skill_plugin_root_dependency" for item in errors)
    assert not any(item["code"] == "hook_legacy_host_reference" for item in errors)


def test_static_scan_rejects_legacy_hook_variable(tmp_path):
    root = _make_valid_root(tmp_path)
    hooks_path = root / "hooks" / "hooks.json"
    hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
    hooks["hooks"]["PreToolUse"][0]["hooks"][0]["command"] = (
        "python ${CLAUDE_PLUGIN_ROOT}/hooks/test.py"
    )
    _write_text(hooks_path, json.dumps(hooks))

    errors = validator.validate(root)

    assert any(item["code"] == "hook_legacy_host_reference" for item in errors)


def test_static_scan_rejects_active_user_slash_but_ignores_docs_tests_and_docstrings(tmp_path):
    root = _make_valid_root(tmp_path)
    _write_text(
        root / "scripts" / "report.py",
        '"""Historical /webnovel-write documentation."""\n\ndef next_action():\n    return "/webnovel-review"\n',
    )
    _write_text(root / "scripts" / "tests" / "test_legacy.py", 'VALUE = "/webnovel-write"\n')
    _write_text(root / "docs" / "migration.md", "Use /webnovel-write in Claude.\n")

    errors = validator.validate(root)

    slash_errors = [item for item in errors if item["code"] == "claude_slash_command"]
    assert len(slash_errors) == 1
    assert slash_errors[0]["path"].endswith("scripts/report.py:4")


def test_validator_read_failure_is_structured_validation_failure(monkeypatch, capsys, tmp_path):
    root = _make_valid_root(tmp_path)
    original_load = validator._load_object

    def fail_manifest(path):
        if path.name == "plugin.json":
            raise PermissionError("simulated permission failure")
        return original_load(path)

    monkeypatch.setattr(validator, "_load_object", fail_manifest)
    code, report, stderr = _run_main(monkeypatch, capsys, root)

    assert code == 1
    assert report["errors"][0]["code"] == "manifest_read_failed"
    assert "Traceback" not in stderr


def test_validator_internal_failure_exits_two_without_traceback(monkeypatch, capsys, tmp_path):
    root = _make_valid_root(tmp_path)

    def fail_validate(_root):
        raise RuntimeError("simulated internal failure")

    monkeypatch.setattr(validator, "validate", fail_validate)
    code, report, stderr = _run_main(monkeypatch, capsys, root)

    assert code == 2
    assert report["ok"] is False
    assert report["errors"][0]["code"] == "validator_internal_error"
    assert "Traceback" not in stderr
