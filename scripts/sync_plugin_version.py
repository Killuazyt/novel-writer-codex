#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Check or update Codex plugin version metadata.

The Codex downstream is a single repository whose plugin manifest lives at
``.codex-plugin/plugin.json``.  Marketplace metadata and the README version
marker are optional until the release milestone; when present, they are kept
in sync without making their absence an M2 failure.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_MANIFEST_REL = Path(".codex-plugin") / "plugin.json"
MARKETPLACE_REL = Path(".agents") / "plugins" / "marketplace.json"
README_FILENAME = "README.md"
PLUGIN_NAME = "novel-writer-codex"
VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
README_VERSION_PATTERN = re.compile(
    r"<!--\s*novel-writer-codex-version:\s*(?P<version>[^\s]+)\s*-->",
    re.IGNORECASE,
)


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        payload = json.load(file)
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def save_json(path: Path, payload: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
        file.write("\n")


def load_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def save_text(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8", newline="\n")


def get_marketplace_plugin(payload: dict[str, Any]) -> dict[str, Any]:
    plugins = payload.get("plugins", [])
    if not isinstance(plugins, list):
        raise ValueError("marketplace.json plugins must be an array")
    for plugin in plugins:
        if isinstance(plugin, dict) and plugin.get("name") == PLUGIN_NAME:
            return plugin
    raise ValueError(f"Plugin {PLUGIN_NAME} not found in marketplace.json")


def find_readme_version(content: str) -> str | None:
    """Return the optional stable README version marker."""

    match = README_VERSION_PATTERN.search(content)
    return str(match.group("version")) if match else None


def get_readme_current_version(content: str) -> str:
    """Return the README marker or raise for callers that require one."""

    version = find_readme_version(content)
    if version is None:
        raise ValueError(
            "README.md version marker not found; add "
            "'<!-- novel-writer-codex-version: X.Y.Z -->' before release validation"
        )
    return version


def get_readme_badge_version(content: str) -> str:
    """Compatibility alias retained for the old validator API."""

    return get_readme_current_version(content)


def update_readme_version(content: str, version: str) -> str:
    """Update an existing marker; M2 never inserts release documentation."""

    if not README_VERSION_PATTERN.search(content):
        return content
    return README_VERSION_PATTERN.sub(
        f"<!-- novel-writer-codex-version: {version} -->",
        content,
        count=1,
    )


def update_readme_release(content: str, version: str, release_notes: str | None) -> str:
    """Compatibility wrapper; release-note authoring remains outside M2."""

    del release_notes
    return update_readme_version(content, version)


def _metadata_paths(root: str | Path | None = None) -> tuple[Path, Path, Path, Path]:
    repo_root = Path(root) if root is not None else ROOT
    repo_root = repo_root.expanduser().resolve()
    return (
        repo_root,
        repo_root / PLUGIN_MANIFEST_REL,
        repo_root / MARKETPLACE_REL,
        repo_root / README_FILENAME,
    )


def _validate_manifest_identity(payload: dict[str, Any], path: Path) -> None:
    name = str(payload.get("name") or "")
    if name != PLUGIN_NAME:
        raise ValueError(f"{path} name must be {PLUGIN_NAME!r}, got {name!r}")


def sync_versions(
    version: str | None = None,
    release_notes: str | None = None,
    *,
    root: str | Path | None = None,
) -> tuple[str, str, bool]:
    repo_root, manifest_path, marketplace_path, readme_path = _metadata_paths(root)
    del repo_root
    plugin_payload = load_json(manifest_path)
    _validate_manifest_identity(plugin_payload, manifest_path)

    previous_version = str(plugin_payload.get("version") or "")
    target_version = version or previous_version
    if not VERSION_PATTERN.fullmatch(target_version):
        raise ValueError(f"invalid semantic version: {target_version!r}")

    changed = False
    if previous_version != target_version:
        plugin_payload["version"] = target_version
        save_json(manifest_path, plugin_payload)
        changed = True

    if readme_path.is_file():
        readme_content = load_text(readme_path)
        updated_readme = update_readme_release(readme_content, target_version, release_notes)
        if updated_readme != readme_content:
            save_text(readme_path, updated_readme)
            changed = True

    # Marketplace is introduced in a later milestone.  If a development
    # fixture already has a conventional version field, keep that field in
    # sync; absence of the file or field is intentionally a no-op.
    if marketplace_path.is_file():
        marketplace_payload = load_json(marketplace_path)
        marketplace_plugin = get_marketplace_plugin(marketplace_payload)
        if "version" in marketplace_plugin and marketplace_plugin.get("version") != target_version:
            marketplace_plugin["version"] = target_version
            save_json(marketplace_path, marketplace_payload)
            changed = True

    return previous_version, target_version, changed


def check_versions(
    expected_version: str | None = None,
    *,
    root: str | Path | None = None,
) -> int:
    _, manifest_path, marketplace_path, readme_path = _metadata_paths(root)
    plugin_payload = load_json(manifest_path)
    _validate_manifest_identity(plugin_payload, manifest_path)
    plugin_version = str(plugin_payload.get("version") or "")

    mismatches: list[str] = []
    if not VERSION_PATTERN.fullmatch(plugin_version):
        mismatches.append(f"plugin.json has invalid version={plugin_version!r}")
    if expected_version and plugin_version != expected_version:
        mismatches.append(f"expected={expected_version}, plugin.json={plugin_version}")

    if readme_path.is_file():
        readme_version = find_readme_version(load_text(readme_path))
        if readme_version is not None and readme_version != plugin_version:
            mismatches.append(f"plugin.json={plugin_version}, README.md={readme_version}")

    if marketplace_path.is_file():
        marketplace_payload = load_json(marketplace_path)
        marketplace_plugin = get_marketplace_plugin(marketplace_payload)
        marketplace_version = marketplace_plugin.get("version")
        if marketplace_version is not None and str(marketplace_version) != plugin_version:
            mismatches.append(
                f"plugin.json={plugin_version}, marketplace.json={marketplace_version}"
            )

    if mismatches:
        print("Version mismatch detected:")
        for mismatch in mismatches:
            print(f"- {mismatch}")
        return 1

    print(f"Versions are in sync: {plugin_version}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync Codex plugin release metadata")
    parser.add_argument("--root", default="", help="仓库根目录，默认由脚本位置推导")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check whether present plugin metadata is in sync",
    )
    parser.add_argument("--version", help="Update metadata to the given semantic version")
    parser.add_argument(
        "--expected-version",
        help="When used with --check, require plugin.json to match this version",
    )
    parser.add_argument(
        "--release-notes",
        help="Reserved for the later release workflow; M2 does not author release notes",
    )
    args = parser.parse_args()

    if args.version and not VERSION_PATTERN.fullmatch(args.version):
        parser.error("--version must look like X.Y.Z")
    if args.expected_version and not VERSION_PATTERN.fullmatch(args.expected_version):
        parser.error("--expected-version must look like X.Y.Z")
    if args.expected_version and not args.check:
        parser.error("--expected-version can only be used together with --check")

    try:
        if args.check:
            return check_versions(
                expected_version=args.expected_version,
                root=args.root or None,
            )

        previous_version, target_version, changed = sync_versions(
            version=args.version,
            release_notes=args.release_notes,
            root=args.root or None,
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"Error: {error}")
        return 1

    if changed:
        print(f"Updated release metadata: {previous_version} -> {target_version}")
    else:
        print(f"No changes needed. Current version: {target_version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
