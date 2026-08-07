#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Small, dependency-free validation for the Codex adapter scaffold."""

from __future__ import annotations

import argparse
import ast
import json
import re
from pathlib import Path
from typing import Any


PLUGIN_NAME = "novel-writer-codex"
KEBAB_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
CLAUDE_SLASH_COMMAND_RE = re.compile(
    r"(?<![A-Za-z0-9._/-])/(?P<command>webnovel-(?:setup|init|plan|write|review|query|learn|dashboard|doctor))\b"
)
LEGACY_HOST_RE = re.compile(
    r"(?:\$\{?CLAUDE_(?:PLUGIN_ROOT|PROJECT_DIR)\}?|(?:^|[/\\])\.claude(?:[/\\]|$))",
    re.MULTILINE,
)
PLUGIN_ROOT_RE = re.compile(r"\$\{?PLUGIN_ROOT\}?")
BASH_ONLY_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("bash_export", re.compile(r"(?m)^\s*export\s+[A-Za-z_][A-Za-z0-9_]*="), "Use an argv-based Python command or provide PowerShell/POSIX alternatives."),
    ("bash_pwd", re.compile(r"\$PWD\b"), "Pass an explicit project path instead of relying on $PWD."),
    ("bash_substitution", re.compile(r"\$\("), "Do not use shell command substitution in distributed prompts."),
    ("bash_dev_null", re.compile(r"/dev/null\b"), "Use cross-platform process redirection."),
    (
        "bash_utility",
        re.compile(r"(?m)^\s*(?:cat|test|find|seq|printf)\s+"),
        "Use the runtime CLI or a cross-platform file/search operation.",
    ),
)
CLAUDE_FRONTMATTER_RE = re.compile(r"(?m)^\s*(?:allowed-tools|argument-hint)\s*:")
CLAUDE_TOOL_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?:AskUserQuestion|WebSearch|WebFetch|Task|Read|Write|Edit|Grep|Glob|Bash)(?![A-Za-z0-9_-])"
)
TEXT_SURFACE_SUFFIXES = {".md", ".yaml", ".yml"}


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


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _docstring_nodes(tree: ast.AST) -> set[ast.AST]:
    result: set[ast.AST] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
            result.add(first.value)
    return result


def _python_slash_matches(path: Path) -> list[tuple[int, str]]:
    """Find user-facing slash commands while ignoring Python docstrings/tests."""

    try:
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=str(path))
    except (OSError, UnicodeError, SyntaxError):
        return []

    ignored = _docstring_nodes(tree)
    matches: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if node in ignored or not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        for match in CLAUDE_SLASH_COMMAND_RE.finditer(node.value):
            line = int(getattr(node, "lineno", 1)) + node.value.count("\n", 0, match.start())
            matches.append((line, match.group(0)))
    return matches


def _iter_active_slash_files(root: Path) -> list[Path]:
    targets: list[Path] = []
    for directory in (root / "scripts", root / "dashboard", root / "hooks"):
        if not directory.is_dir():
            continue
        for path in directory.rglob("*.py"):
            relative_parts = path.relative_to(root).parts
            if "tests" in relative_parts or path.name.startswith("test_") or path.name in {
                "conftest.py",
                "pytest_bootstrap.py",
                "test_isolation.py",
                "test_state_guard.py",
            }:
                continue
            targets.append(path)
    for directory, suffixes in (
        (root / "references", {".json"}),
        (root / "templates", {".md", ".json"}),
    ):
        if directory.is_dir():
            targets.extend(path for path in directory.rglob("*") if path.suffix.lower() in suffixes)
    return sorted(set(targets))


def _surface_text_files(root: Path) -> tuple[list[Path], list[Path], list[Path]]:
    skills_root = root / "skills"
    skills = (
        [path for path in skills_root.rglob("*") if path.is_file() and path.suffix.lower() in TEXT_SURFACE_SUFFIXES]
        if skills_root.is_dir()
        else []
    )
    agents_root = root / ".codex" / "agents"
    agents = list(agents_root.glob("*.toml")) if agents_root.is_dir() else []
    hooks_path = root / "hooks" / "hooks.json"
    hooks = [hooks_path] if hooks_path.is_file() else []
    return sorted(skills), sorted(agents), hooks


def scan_host_neutrality(root: Path) -> list[dict[str, str]]:
    """Scan shipped operational surfaces for Claude/Bash-only expressions.

    Migration documentation, compatibility implementations, and tests are not
    scanned.  ``PLUGIN_ROOT`` and Codex hook matcher names are allowed only in
    hooks configuration; Skills and project-agent templates must self-locate or
    call the stable runtime without host-provided environment variables.
    """

    errors: list[dict[str, str]] = []
    skill_files, agent_files, hook_files = _surface_text_files(root)

    for path in [*skill_files, *agent_files]:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        display = _display_path(root, path)
        checks: tuple[tuple[str, re.Pattern[str], str], ...] = (
            (
                "legacy_host_reference",
                LEGACY_HOST_RE,
                "Use Codex-native paths; legacy Claude paths belong only in compatibility readers.",
            ),
            (
                "skill_plugin_root_dependency",
                PLUGIN_ROOT_RE,
                "Skills and project agents must locate the runtime from their own installed files.",
            ),
            (
                "claude_frontmatter",
                CLAUDE_FRONTMATTER_RE,
                "Codex Skill frontmatter may contain only supported Codex fields.",
            ),
            (
                "claude_tool_name",
                CLAUDE_TOOL_RE,
                "Replace Claude tool names with Codex-neutral instructions.",
            ),
            (
                "claude_slash_command",
                CLAUDE_SLASH_COMMAND_RE,
                "Use the matching $webnovel-* Codex Skill name or the explicit Python runtime CLI.",
            ),
            *BASH_ONLY_PATTERNS,
        )
        for code, pattern, repair in checks:
            match = pattern.search(text)
            if match:
                errors.append(
                    _issue(
                        code,
                        f"{display}:{_line_number(text, match.start())}",
                        f"forbidden host-specific expression: {match.group(0)!r}",
                        repair,
                    )
                )

    for path in hook_files:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        display = _display_path(root, path)
        hook_checks: tuple[tuple[str, re.Pattern[str], str], ...] = (
            (
                "hook_legacy_host_reference",
                LEGACY_HOST_RE,
                "Hooks may use ${PLUGIN_ROOT}, but must not depend on Claude variables or paths.",
            ),
            (
                "claude_slash_command",
                CLAUDE_SLASH_COMMAND_RE,
                "Use the matching $webnovel-* Codex Skill name or the explicit Python runtime CLI.",
            ),
            *BASH_ONLY_PATTERNS,
        )
        for code, pattern, repair in hook_checks:
            match = pattern.search(text)
            if match:
                errors.append(
                    _issue(
                        code,
                        f"{display}:{_line_number(text, match.start())}",
                        f"forbidden hook expression: {match.group(0)!r}",
                        repair,
                    )
                )

    for path in _iter_active_slash_files(root):
        display = _display_path(root, path)
        if path.suffix.lower() == ".py":
            matches = _python_slash_matches(path)
        else:
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            matches = [
                (_line_number(text, match.start()), match.group(0))
                for match in CLAUDE_SLASH_COMMAND_RE.finditer(text)
            ]
        if matches:
            line, command = matches[0]
            errors.append(
                _issue(
                    "claude_slash_command",
                    f"{display}:{line}",
                    f"active user-facing Claude slash command found: {command}",
                    "Use the matching $webnovel-* Codex Skill name or the explicit Python runtime CLI.",
                )
            )

    return errors


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
    elif name != PLUGIN_NAME:
        errors.append(
            _issue(
                "manifest_name_unexpected",
                ".codex-plugin/plugin.json",
                f"manifest.name must be {PLUGIN_NAME!r}, got {name!r}",
                f"Set manifest.name to {PLUGIN_NAME!r}.",
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

    errors.extend(scan_host_neutrality(root))

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
