#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import sys
from pathlib import Path


def _ensure_scripts_on_path() -> None:
    scripts_dir = Path(__file__).resolve().parents[1]
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))


_ensure_scripts_on_path()

import validate_plugin_package as package_validator  # noqa: E402
from validate_plugin_package import validate_package  # noqa: E402


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_minimal_package(
    root: Path,
    *,
    plugin_version: str = "1.2.3",
    readme_version: str | None = "1.2.3",
) -> None:
    _write_json(
        root / ".codex-plugin" / "plugin.json",
        {
            "name": "novel-writer-codex",
            "version": plugin_version,
            "description": "desc",
        },
    )
    marker = (
        f"\n<!-- novel-writer-codex-version: {readme_version} -->\n"
        if readme_version is not None
        else "\n"
    )
    (root / "README.md").write_text(f"# Test\n{marker}", encoding="utf-8")
    (root / "LICENSE").write_text("license\n", encoding="utf-8")
    skill = root / "skills" / "demo" / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text(
        "---\nname: demo\ndescription: demo\n---\n\n# Demo\n",
        encoding="utf-8",
    )


def test_validate_plugin_package_passes_direct_codex_root(tmp_path):
    _write_minimal_package(tmp_path)

    report = validate_package(tmp_path)

    assert report["ok"] is True
    assert report["error_count"] == 0
    assert report["root"] == str(tmp_path.resolve())


def test_validate_plugin_package_defers_marketplace_dashboard_and_readme_marker(tmp_path):
    _write_minimal_package(tmp_path, readme_version=None)

    report = validate_package(tmp_path, strict=True)

    assert report["ok"] is True
    assert report["warning_count"] == 0
    assert not (tmp_path / ".agents" / "plugins" / "marketplace.json").exists()
    assert not (tmp_path / "dashboard" / "frontend" / "dist").exists()


def test_validate_plugin_package_detects_optional_marketplace_version_mismatch(tmp_path):
    _write_minimal_package(tmp_path)
    _write_json(
        tmp_path / ".agents" / "plugins" / "marketplace.json",
        {
            "plugins": [
                {
                    "name": "novel-writer-codex",
                    "version": "1.2.4",
                }
            ]
        },
    )

    report = validate_package(tmp_path)

    assert report["ok"] is False
    assert any(item["code"] == "version.marketplace" for item in report["issues"])


def test_validate_plugin_package_detects_readme_marker_mismatch(tmp_path):
    _write_minimal_package(tmp_path, readme_version="1.2.2")

    report = validate_package(tmp_path)

    assert report["ok"] is False
    assert any(item["code"] == "version.readme" for item in report["issues"])


def test_validate_plugin_package_rejects_old_plugin_name(tmp_path):
    _write_minimal_package(tmp_path)
    manifest = tmp_path / ".codex-plugin" / "plugin.json"
    _write_json(manifest, {"name": "webnovel-writer", "version": "1.2.3", "description": "desc"})

    report = validate_package(tmp_path)

    assert report["ok"] is False
    assert any(item["code"] == "manifest.name" for item in report["issues"])


def test_validate_plugin_package_detects_missing_skill_frontmatter(tmp_path):
    _write_minimal_package(tmp_path)
    skill = tmp_path / "skills" / "demo" / "SKILL.md"
    skill.write_text("---\nname: demo\n---\n\n# Demo\n", encoding="utf-8")

    report = validate_package(tmp_path)

    assert report["ok"] is False
    assert any(item["code"] == "skill.frontmatter" for item in report["issues"])


def test_validate_plugin_package_static_scan_blocks_active_slash_command(tmp_path):
    _write_minimal_package(tmp_path)
    script = tmp_path / "scripts" / "user_report.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text('def next_action():\n    return "/webnovel-write 2"\n', encoding="utf-8")

    report = validate_package(tmp_path)

    assert report["ok"] is False
    assert any(item["code"] == "claude_slash_command" for item in report["issues"])


def test_validate_plugin_package_allows_plugin_root_in_hooks(tmp_path):
    _write_minimal_package(tmp_path)
    _write_json(
        tmp_path / "hooks" / "hooks.json",
        {
            "description": "hooks",
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": 'python "${PLUGIN_ROOT}/hooks/session_start.py"',
                            }
                        ]
                    }
                ]
            },
        },
    )

    report = validate_package(tmp_path)

    assert report["ok"] is True


def test_validate_plugin_package_internal_failure_is_structured_exit_two(
    monkeypatch, capsys, tmp_path
):
    _write_minimal_package(tmp_path)

    def fail_validate(*_args, **_kwargs):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(package_validator, "validate_package", fail_validate)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "validate_plugin_package.py",
            "--root",
            str(tmp_path),
            "--strict",
            "--format",
            "json",
        ],
    )

    assert package_validator.main() == 2
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is False
    assert report["issues"][0]["code"] == "validator.internal"
