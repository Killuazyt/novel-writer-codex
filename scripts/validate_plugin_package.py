#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Validate the single-root Novel Writer Codex plugin package."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

import sync_plugin_version
import validate_codex_adapter


SCHEMA_VERSION = "novel-writer-codex-package-validator/v1"
PLUGIN_NAME = sync_plugin_version.PLUGIN_NAME
KEBAB_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SEMVER_RE = sync_plugin_version.VERSION_PATTERN
LOCAL_ABSOLUTE_RE = re.compile(r"(?i)(?:[a-z]:\\users\\|/users/[^/\s]+/|/home/[^/\s]+/)")


def _issue(
    code: str,
    *,
    message: str,
    severity: str = "error",
    path: str = "",
    repair: str = "",
) -> dict[str, str]:
    return {
        "code": code,
        "severity": severity,
        "message": message,
        "path": path,
        "repair": repair,
    }


def _load_json(path: Path) -> tuple[dict[str, Any], str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, "missing"
    except json.JSONDecodeError as exc:
        return {}, f"invalid_json:{exc}"
    except OSError as exc:
        return {}, f"read_error:{exc}"
    if not isinstance(payload, dict):
        return {}, "not_object"
    return payload, ""


def _frontmatter(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    result: dict[str, str] = {}
    for line in text[3:end].splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        result[key.strip()] = value.strip()
    return result


def _openai_interface(path: Path) -> tuple[dict[str, str], str]:
    """Read the small interface block without adding a YAML dependency."""

    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, "missing"
    except UnicodeDecodeError:
        return {}, "not_utf8"
    except OSError as exc:
        return {}, f"read_error:{exc}"

    interface: dict[str, str] = {}
    in_interface = False
    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        stripped = raw_line.strip()
        if indent == 0:
            in_interface = stripped == "interface:"
            continue
        if not in_interface or indent != 2 or ":" not in stripped:
            continue
        key, _, raw_value = stripped.partition(":")
        value = raw_value.strip()
        if value.startswith('"') and value.endswith('"'):
            try:
                decoded = json.loads(value)
            except json.JSONDecodeError:
                return {}, f"invalid_quoted_value:{key.strip()}"
            if not isinstance(decoded, str):
                return {}, f"non_string_value:{key.strip()}"
            value = decoded
        elif value.startswith("'") and value.endswith("'"):
            value = value[1:-1].replace("''", "'")
        interface[key.strip()] = value
    if not interface:
        return {}, "interface_missing"
    return interface, ""


def _marketplace_plugin(payload: dict[str, Any]) -> dict[str, Any] | None:
    plugins = payload.get("plugins")
    if not isinstance(plugins, list):
        return None
    for item in plugins:
        if isinstance(item, dict) and item.get("name") == PLUGIN_NAME:
            return item
    return None


def _plugin_root(root: Path) -> Path:
    """The Codex repository is itself the plugin root; no nested fallback."""

    return root.expanduser().resolve()


def _check_manifest(root: Path, issues: list[dict[str, str]]) -> tuple[str, str]:
    plugin_json = _plugin_root(root) / ".codex-plugin" / "plugin.json"
    payload, error = _load_json(plugin_json)
    if error:
        issues.append(
            _issue(
                "manifest.plugin_json",
                message=error,
                path=str(plugin_json),
                repair="恢复 .codex-plugin/plugin.json。",
            )
        )
        return "", ""

    name = str(payload.get("name") or "")
    version = str(payload.get("version") or "")
    if not KEBAB_RE.fullmatch(name):
        issues.append(
            _issue(
                "manifest.name",
                message=f"invalid plugin name: {name}",
                path=str(plugin_json),
                repair="使用 kebab-case 插件名。",
            )
        )
    elif name != PLUGIN_NAME:
        issues.append(
            _issue(
                "manifest.name",
                message=f"unexpected plugin name: {name}",
                path=str(plugin_json),
                repair=f"name 必须为 {PLUGIN_NAME}。",
            )
        )
    if not SEMVER_RE.fullmatch(version):
        issues.append(
            _issue(
                "manifest.version",
                message=f"invalid semver: {version}",
                path=str(plugin_json),
                repair="使用 X.Y.Z 版本号。",
            )
        )
    if not str(payload.get("description") or "").strip():
        issues.append(
            _issue(
                "manifest.description",
                message="plugin description missing",
                path=str(plugin_json),
                repair="补齐 description。",
            )
        )
    return name, version


def _check_marketplace(root: Path, plugin_version: str, issues: list[dict[str, str]]) -> None:
    """Validate Marketplace metadata only when the later-milestone file exists."""

    marketplace = _plugin_root(root) / sync_plugin_version.MARKETPLACE_REL
    if not marketplace.exists():
        return
    payload, error = _load_json(marketplace)
    if error:
        issues.append(
            _issue(
                "marketplace.json",
                message=error,
                path=str(marketplace),
                repair="修复 .agents/plugins/marketplace.json。",
            )
        )
        return
    plugin = _marketplace_plugin(payload)
    if plugin is None:
        issues.append(
            _issue(
                "marketplace.plugin",
                message=f"{PLUGIN_NAME} missing from marketplace",
                path=str(marketplace),
                repair=f"在 plugins[] 中加入 {PLUGIN_NAME}。",
            )
        )
        return
    marketplace_version = plugin.get("version")
    if marketplace_version is not None and plugin_version and str(marketplace_version) != plugin_version:
        issues.append(
            _issue(
                "version.marketplace",
                message=f"plugin.json={plugin_version}, marketplace.json={marketplace_version}",
                path=str(marketplace),
                repair="运行 sync_plugin_version.py --version X.Y.Z。",
            )
        )


def _check_readme_version(root: Path, plugin_version: str, issues: list[dict[str, str]]) -> None:
    """Compare an optional M2 marker; release documentation is not required yet."""

    readme = _plugin_root(root) / "README.md"
    try:
        content = readme.read_text(encoding="utf-8")
    except FileNotFoundError:
        issues.append(
            _issue(
                "readme.missing",
                message="README.md missing",
                path=str(readme),
                repair="恢复 README.md。",
            )
        )
        return
    except OSError as exc:
        issues.append(
            _issue(
                "readme.read",
                message=f"read_error:{exc}",
                path=str(readme),
                repair="修复 README.md 读取权限。",
            )
        )
        return
    readme_version = sync_plugin_version.find_readme_version(content)
    if readme_version is not None and plugin_version and readme_version != plugin_version:
        issues.append(
            _issue(
                "version.readme",
                message=f"plugin.json={plugin_version}, README.md={readme_version}",
                path=str(readme),
                repair="更新 README 的 novel-writer-codex-version 标记。",
            )
        )


def _check_frontmatter(root: Path, issues: list[dict[str, str]]) -> None:
    for skill in sorted((_plugin_root(root) / "skills").glob("*/SKILL.md")):
        fm = _frontmatter(skill)
        for field in ("name", "description"):
            if not fm.get(field):
                issues.append(
                    _issue(
                        "skill.frontmatter",
                        message=f"skill missing {field}",
                        path=str(skill),
                        repair="Codex Skill frontmatter 必须包含 name 与 description。",
                    )
                )
        extra_fields = sorted(set(fm) - {"name", "description"})
        if extra_fields:
            issues.append(
                _issue(
                    "skill.frontmatter_fields",
                    message=f"unsupported frontmatter fields: {', '.join(extra_fields)}",
                    path=str(skill),
                    repair="Codex Skill frontmatter 只保留 name 与 description。",
                )
            )
        skill_name = fm.get("name", "").strip().strip('"\'')
        if skill_name and skill_name != skill.parent.name:
            issues.append(
                _issue(
                    "skill.name",
                    message=f"frontmatter name={skill_name!r}, directory={skill.parent.name!r}",
                    path=str(skill),
                    repair="让 Skill name 与目录名保持一致。",
                )
            )

        metadata = skill.parent / "agents" / "openai.yaml"
        interface, error = _openai_interface(metadata)
        if error:
            issues.append(
                _issue(
                    "skill.openai_yaml",
                    message=error,
                    path=str(metadata),
                    repair="为 Skill 添加可解析的 agents/openai.yaml interface 元数据。",
                )
            )
            continue
        missing = [
            field
            for field in ("display_name", "short_description", "default_prompt")
            if not interface.get(field, "").strip()
        ]
        if missing:
            issues.append(
                _issue(
                    "skill.openai_interface",
                    message=f"missing interface fields: {', '.join(missing)}",
                    path=str(metadata),
                    repair="补齐 display_name、short_description 与 default_prompt。",
                )
            )
        default_prompt = interface.get("default_prompt", "")
        if skill_name and f"${skill_name}" not in default_prompt:
            issues.append(
                _issue(
                    "skill.default_prompt",
                    message=f"default_prompt must explicitly invoke ${skill_name}",
                    path=str(metadata),
                    repair=f"在 default_prompt 中显式加入 ${skill_name}。",
                )
            )


def _check_required_assets(root: Path, issues: list[dict[str, str]]) -> None:
    plugin_root = _plugin_root(root)
    if not (plugin_root / "LICENSE").is_file():
        issues.append(
            _issue(
                "license",
                message="LICENSE missing",
                path=str(plugin_root / "LICENSE"),
                repair="恢复插件 LICENSE。",
            )
        )

    # Dashboard dist and Marketplace are intentionally deferred and therefore
    # are not warnings: --strict must remain usable during M2.
    hooks_json = plugin_root / "hooks" / "hooks.json"
    if hooks_json.exists():
        payload, error = _load_json(hooks_json)
        if error:
            issues.append(
                _issue(
                    "hooks.schema",
                    message=error,
                    path=str(hooks_json),
                    repair="修复 hooks/hooks.json。",
                )
            )
        elif "description" not in payload or not isinstance(payload.get("hooks"), dict):
            issues.append(
                _issue(
                    "hooks.wrapper",
                    message="hooks.json must contain description and hooks",
                    path=str(hooks_json),
                    repair="外层包含 description 与 hooks object。",
                )
            )


def _check_portability(root: Path, issues: list[dict[str, str]]) -> None:
    plugin_root = _plugin_root(root)
    targets = list((plugin_root / "skills").glob("*/SKILL.md"))
    targets.extend((plugin_root / ".codex" / "agents").glob("*.toml"))
    hooks_json = plugin_root / "hooks" / "hooks.json"
    if hooks_json.is_file():
        targets.append(hooks_json)

    for path in targets:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if LOCAL_ABSOLUTE_RE.search(text):
            repair = (
                "hook 配置使用 ${PLUGIN_ROOT}。"
                if path == hooks_json
                else "Skill/Agent 通过自身安装位置推导 runtime，禁止本机绝对路径。"
            )
            issues.append(
                _issue(
                    "portability.local_absolute_path",
                    message="local absolute path found in plugin component",
                    severity="warning",
                    path=str(path),
                    repair=repair,
                )
            )


def _check_host_neutrality(root: Path, issues: list[dict[str, str]]) -> None:
    for error in validate_codex_adapter.scan_host_neutrality(_plugin_root(root)):
        issues.append(
            _issue(
                error["code"],
                message=error["message"],
                path=error["path"],
                repair=error["repair"],
            )
        )


def validate_package(root: str | Path | None = None, *, strict: bool = False) -> dict[str, Any]:
    repo_root = _plugin_root(Path(root) if root is not None else Path(__file__).resolve().parents[1])
    issues: list[dict[str, str]] = []
    _, plugin_version = _check_manifest(repo_root, issues)
    _check_marketplace(repo_root, plugin_version, issues)
    _check_readme_version(repo_root, plugin_version, issues)
    _check_frontmatter(repo_root, issues)
    _check_required_assets(repo_root, issues)
    _check_portability(repo_root, issues)
    _check_host_neutrality(repo_root, issues)
    blocking = [
        item for item in issues if item["severity"] == "error" or (strict and item["severity"] == "warning")
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": not blocking,
        "strict": strict,
        "root": str(repo_root),
        "error_count": sum(1 for item in issues if item["severity"] == "error"),
        "warning_count": sum(1 for item in issues if item["severity"] == "warning"),
        "issues": issues,
    }


def format_report(report: dict[str, Any], output_format: str = "text") -> str:
    if output_format == "json":
        return json.dumps(report, ensure_ascii=False, indent=2)
    status = "OK" if report.get("ok") else "ERROR"
    lines = [
        f"{status} plugin package",
        f"errors: {report.get('error_count')} warnings: {report.get('warning_count')}",
    ]
    for item in report.get("issues") or []:
        lines.append(f"{item.get('severity', '').upper()} {item.get('code')}: {item.get('message')}")
        if item.get("path"):
            lines.append(f"  path: {item.get('path')}")
        if item.get("repair"):
            lines.append(f"  repair: {item.get('repair')}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="", help="仓库根目录，默认由脚本位置推导")
    parser.add_argument("--strict", action="store_true", help="warning 也视为失败")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args()

    root = _plugin_root(
        Path(args.root) if args.root else Path(__file__).resolve().parents[1]
    )
    try:
        report = validate_package(root, strict=args.strict)
        print(format_report(report, args.format))
        return 0 if report.get("ok") else 1
    except Exception as exc:
        report = {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "strict": args.strict,
            "root": str(root),
            "error_count": 1,
            "warning_count": 0,
            "issues": [
                _issue(
                    "validator.internal",
                    message=f"{type(exc).__name__}: {exc}",
                    path=str(root),
                    repair="检查 validator 实现或文件系统后重试。",
                )
            ],
        }
        print(format_report(report, args.format))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
