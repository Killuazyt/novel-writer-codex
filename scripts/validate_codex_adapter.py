#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Small, dependency-free validation for the Codex adapter scaffold."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


KEBAB_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")


def _load_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _display_path(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _issue(
    code: str,
    path: str,
    message: str,
    repair: str,
) -> dict[str, str]:
    return {
        "code": code,
        "path": path,
        "message": message,
        "repair": repair,
    }


def _load_error(
    root: Path,
    path: Path,
    label: str,
    exc: Exception,
) -> dict[str, str]:
    rendered_path = _display_path(root, path)
    if isinstance(exc, FileNotFoundError):
        code = f"{label}_missing"
        message = f"required JSON file is missing: {rendered_path}"
    elif isinstance(exc, UnicodeDecodeError):
        code = f"{label}_not_utf8"
        message = f"JSON file is not valid UTF-8: {rendered_path}"
    elif isinstance(exc, json.JSONDecodeError):
        code = f"{label}_invalid_json"
        message = f"invalid JSON in {rendered_path}: {exc.msg}"
    elif isinstance(exc, ValueError):
        code = f"{label}_not_object"
        message = str(exc)
    else:
        code = f"{label}_read_failed"
        message = f"cannot read {rendered_path}: {exc}"
    return _issue(
        code,
        rendered_path,
        message,
        "Restore a UTF-8 JSON object at this path, then rerun the validator.",
    )


def validate(root: Path) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    manifest_path = root / ".codex-plugin" / "plugin.json"
    hooks_path = root / "hooks" / "hooks.json"

    try:
        manifest = _load_object(manifest_path)
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        return [_load_error(root, manifest_path, "manifest", exc)]

    name = str(manifest.get("name") or "")
    version = str(manifest.get("version") or "")
    if not KEBAB_RE.fullmatch(name):
        errors.append(
            _issue(
                "manifest_name_invalid",
                ".codex-plugin/plugin.json",
                f"manifest.name is not kebab-case: {name!r}",
                "Use lowercase kebab-case for manifest.name.",
            )
        )
    if not SEMVER_RE.fullmatch(version):
        errors.append(
            _issue(
                "manifest_version_invalid",
                ".codex-plugin/plugin.json",
                f"manifest.version is not semver: {version!r}",
                "Set manifest.version to a valid semantic version.",
            )
        )
    for field in ("description", "author", "license", "interface"):
        if not manifest.get(field):
            errors.append(
                _issue(
                    "manifest_field_missing",
                    ".codex-plugin/plugin.json",
                    f"manifest.{field} is required",
                    f"Add a non-empty {field!r} field to the manifest.",
                )
            )
    if manifest.get("license") != "GPL-3.0":
        errors.append(
            _issue(
                "manifest_license_invalid",
                ".codex-plugin/plugin.json",
                "manifest.license must remain GPL-3.0 for this derivative port",
                "Set manifest.license to GPL-3.0.",
            )
        )

    skill_files = list((root / "skills").glob("*/SKILL.md"))
    if manifest.get("skills") and not skill_files:
        errors.append(
            _issue(
                "advertised_skills_missing",
                "skills",
                "manifest advertises skills but no adapted SKILL.md exists",
                "Add the advertised Codex skills or remove the manifest claim.",
            )
        )

    try:
        hooks = _load_object(hooks_path)
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        errors.append(_load_error(root, hooks_path, "hooks", exc))
    else:
        if not hooks.get("description") or not isinstance(hooks.get("hooks"), dict):
            errors.append(
                _issue(
                    "hooks_contract_invalid",
                    "hooks/hooks.json",
                    "hooks/hooks.json must contain description and hooks",
                    "Add a description and a hooks JSON object.",
                )
            )
        serialized = json.dumps(hooks, ensure_ascii=False)
        if "${PLUGIN_ROOT}" not in serialized:
            errors.append(
                _issue(
                    "hooks_plugin_root_missing",
                    "hooks/hooks.json",
                    "hook commands must use ${PLUGIN_ROOT}",
                    "Resolve hook scripts through ${PLUGIN_ROOT}.",
                )
            )
        if "commandWindows" not in serialized:
            errors.append(
                _issue(
                    "hooks_windows_command_missing",
                    "hooks/hooks.json",
                    "hook commands must define commandWindows",
                    "Add a Windows-safe commandWindows entry to every hook command.",
                )
            )
        if "apply_patch" not in serialized:
            errors.append(
                _issue(
                    "hooks_apply_patch_guard_missing",
                    "hooks/hooks.json",
                    "PreToolUse must cover Codex apply_patch",
                    "Include apply_patch in the protected-write matcher.",
                )
            )

    for required in ("LICENSE", "README.md", "UPSTREAM.md", "AGENTS.md", "docs/PORTING.md"):
        if not (root / required).is_file():
            errors.append(
                _issue(
                    "required_project_file_missing",
                    required,
                    f"required project file missing: {required}",
                    "Restore the required attribution or project documentation file.",
                )
            )

    return errors


def _result(root: Path, errors: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "schema_version": "codex-adapter-validator/v1",
        "ok": not errors,
        "error_count": len(errors),
        "root": str(root),
        "errors": errors,
    }


def _print_result(result: dict[str, Any], output_format: str) -> None:
    if output_format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if result["ok"]:
        print("OK Codex adapter")
        return
    print("ERROR Codex adapter")
    for error in result["errors"]:
        print(f"- [{error['code']}] {error['path']}: {error['message']}")
        print(f"  repair: {error['repair']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args()

    root = args.root
    try:
        root = args.root.resolve()
        errors = validate(root)
        result = _result(root, errors)
        _print_result(result, args.format)
        return 0 if not errors else 1
    except Exception as exc:  # unexpected validator bug or filesystem failure
        errors = [
            _issue(
                "validator_internal_error",
                str(root),
                f"validator internal error: {type(exc).__name__}: {exc}",
                "Inspect the validator implementation or filesystem, then rerun.",
            )
        ]
        _print_result(_result(root, errors), args.format)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
