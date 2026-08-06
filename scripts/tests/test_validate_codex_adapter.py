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
