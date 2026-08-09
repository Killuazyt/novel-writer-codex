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

import sync_plugin_version as sync  # noqa: E402


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_root(
    root: Path,
    *,
    version: str = "1.2.3",
    readme_marker: bool = True,
    validation_command: bool = False,
) -> None:
    _write_json(
        root / ".codex-plugin" / "plugin.json",
        {"name": "novel-writer-codex", "version": version, "description": "desc"},
    )
    marker = f"<!-- novel-writer-codex-version: {version} -->\n" if readme_marker else ""
    command = (
        f"python scripts/sync_plugin_version.py --check --expected-version {version}\n"
        if validation_command
        else ""
    )
    (root / "README.md").write_text(f"# Test\n{marker}{command}", encoding="utf-8")


def test_check_versions_uses_direct_codex_root(tmp_path, capsys):
    _write_root(tmp_path)

    code = sync.check_versions(expected_version="1.2.3", root=tmp_path)

    assert code == 0
    assert "Versions are in sync" in capsys.readouterr().out


def test_check_versions_allows_deferred_readme_marker_and_marketplace(tmp_path):
    _write_root(tmp_path, readme_marker=False)

    code = sync.check_versions(root=tmp_path)

    assert code == 0
    assert not (tmp_path / ".agents" / "plugins" / "marketplace.json").exists()


def test_check_versions_detects_optional_readme_marker_mismatch(tmp_path, capsys):
    _write_root(tmp_path)
    readme = tmp_path / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8").replace("1.2.3", "1.2.2"),
        encoding="utf-8",
    )

    code = sync.check_versions(root=tmp_path)

    assert code == 1
    assert "README.md=1.2.2" in capsys.readouterr().out


def test_sync_versions_updates_manifest_and_existing_readme_marker(tmp_path):
    _write_root(tmp_path, validation_command=True)

    previous, target, changed = sync.sync_versions("1.2.4", root=tmp_path)

    assert (previous, target, changed) == ("1.2.3", "1.2.4", True)
    manifest = json.loads((tmp_path / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["version"] == "1.2.4"
    readme = (tmp_path / "README.md").read_text(encoding="utf-8")
    assert sync.find_readme_version(readme) == "1.2.4"
    assert sync.find_readme_expected_versions(readme) == ["1.2.4"]


def test_check_versions_detects_readme_expected_version_mismatch(tmp_path, capsys):
    _write_root(tmp_path, validation_command=True)
    readme = tmp_path / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8").replace("--expected-version 1.2.3", "--expected-version 1.2.2"),
        encoding="utf-8",
    )

    code = sync.check_versions(root=tmp_path)

    assert code == 1
    assert "README.md --expected-version=1.2.2" in capsys.readouterr().out


def test_sync_versions_does_not_insert_deferred_readme_marker(tmp_path):
    _write_root(tmp_path, readme_marker=False)

    _, _, changed = sync.sync_versions("1.2.4", root=tmp_path)

    assert changed is True
    assert sync.find_readme_version((tmp_path / "README.md").read_text(encoding="utf-8")) is None


def test_check_versions_rejects_old_plugin_name(tmp_path):
    _write_root(tmp_path)
    _write_json(
        tmp_path / ".codex-plugin" / "plugin.json",
        {"name": "webnovel-writer", "version": "1.2.3", "description": "desc"},
    )

    try:
        sync.check_versions(root=tmp_path)
    except ValueError as exc:
        assert "novel-writer-codex" in str(exc)
    else:
        raise AssertionError("expected old plugin identity to be rejected")
