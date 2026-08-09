#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
webnovel 统一入口（面向 skills / agents 的稳定 CLI）

设计目标：
- 只有一个入口命令，避免到处拼 `python -m data_modules.xxx ...` 导致参数位置/引号/路径炸裂。
- 自动解析正确的 book project_root（包含 `.webnovel/state.json` 的目录）。
- 所有写入类命令在解析到 project_root 后，统一前置 `--project-root` 传给具体模块。

典型用法（推荐，不依赖 PYTHONPATH / 不要求 cd）：
  python "<SCRIPTS_DIR>/webnovel.py" preflight
  python "<SCRIPTS_DIR>/webnovel.py" where
  python "<SCRIPTS_DIR>/webnovel.py" use "<PROJECT_ROOT>"
  python "<SCRIPTS_DIR>/webnovel.py" --project-root "<PROJECT_ROOT>" index stats
  python "<SCRIPTS_DIR>/webnovel.py" --project-root "<PROJECT_ROOT>" state process-chapter --chapter 100 --data @payload.json
  python "<SCRIPTS_DIR>/webnovel.py" --project-root "<PROJECT_ROOT>" extract-context --chapter 100 --format json

也支持（不推荐，容易踩 PYTHONPATH/cd/参数顺序坑）：
  python -m data_modules.webnovel where
"""

from __future__ import annotations

import argparse
import importlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Optional

from runtime_compat import enable_windows_utf8_stdio, normalize_windows_path
from project_locator import (
    bind_current_project,
    confirm_current_workspace,
    resolve_project,
    resolve_project_root,
)

from .story_runtime_health import build_story_runtime_health


if sys.platform == "win32":
    enable_windows_utf8_stdio(skip_in_pytest=True)


def _scripts_dir() -> Path:
    # data_modules/webnovel.py -> data_modules -> scripts
    return Path(__file__).resolve().parent.parent


def _resolve_root(explicit_project_root: Optional[str]) -> Path:
    # 显式路径必须是包含 `.webnovel/state.json` 的书项目根目录。
    if explicit_project_root is not None:
        return resolve_project_root(explicit_project_root)
    return resolve_project_root()


def _strip_project_root_args(argv: list[str]) -> list[str]:
    """
    下游工具统一由本入口注入 `--project-root`，避免重复传参导致 argparse 报错/歧义。
    """
    out: list[str] = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--project-root":
            i += 2
            continue
        if tok.startswith("--project-root="):
            i += 1
            continue
        out.append(tok)
        i += 1
    return out


PASSTHROUGH_TOOLS = {
    "index",
    "state",
    "rag",
    "style",
    "entity",
    "context",
    "memory",
    "migrate",
    "status",
    "update-state",
    "backup",
    "archive",
    "story-system",
    "memory-contract",
    "project-memory",
    "plan-request",
    "plan-validate",
    "plan-transaction",
    "write-transaction",
}


def _passthrough_tail(argv: list[str], tool: str) -> list[str]:
    i = 0
    while i < len(argv):
        token = argv[i]
        if token == "--project-root":
            i += 2
            continue
        if token.startswith("--project-root="):
            i += 1
            continue
        if token == tool:
            return list(argv[i + 1 :])
        i += 1
    return []


def _run_data_module(module: str, argv: list[str]) -> int:
    """
    Import `data_modules.<module>` and call its main(), while isolating sys.argv.
    """
    mod = importlib.import_module(f"data_modules.{module}")
    main = getattr(mod, "main", None)
    if not callable(main):
        raise RuntimeError(f"data_modules.{module} 缺少可调用的 main()")

    old_argv = sys.argv
    try:
        sys.argv = [f"data_modules.{module}"] + argv
        try:
            main()
            return 0
        except SystemExit as e:
            return int(e.code or 0)
    finally:
        sys.argv = old_argv


def _run_script(script_name: str, argv: list[str]) -> int:
    """
    Run a script under `.claude/scripts/` via a subprocess.

    用途：兼容没有 main() 的脚本。
    """
    script_path = _scripts_dir() / script_name
    if not script_path.is_file():
        raise FileNotFoundError(f"未找到脚本: {script_path}")
    proc = subprocess.run([sys.executable, str(script_path), *argv])
    return int(proc.returncode or 0)


def cmd_where(args: argparse.Namespace) -> int:
    try:
        resolution = resolve_project(args.project_root)
    except FileNotFoundError as exc:
        print(_project_root_diagnostic(args.project_root, exc), file=sys.stderr)
        return 2 if args.project_root is not None else 1
    if args.format == "json":
        print(json.dumps(resolution.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(str(resolution.project_root))
    return 0


def cmd_codex_setup(args: argparse.Namespace) -> int:
    """Check or provision this workspace's managed project agents."""

    from .codex_setup import format_setup_result, run_codex_setup

    code, result = run_codex_setup(
        args.workspace_root,
        apply=bool(args.apply),
    )
    print(format_setup_result(result, args.format))
    return code


def _project_root_diagnostic(
    explicit_project_root: Optional[str], exc: FileNotFoundError
) -> str:
    if explicit_project_root is not None:
        return (
            "未找到有效书项目根目录（需要包含 .webnovel/state.json）: "
            f"{explicit_project_root}\n"
            f"detail: {exc}"
        )
    return (
        "当前工作区还没有激活的书项目（未找到 .webnovel/state.json）。\n"
        "请先运行 webnovel init 创建项目，或运行 webnovel use <project_root> 绑定已有书项目。\n"
        f"detail: {exc}"
    )


def _build_preflight_report(explicit_project_root: Optional[str]) -> dict:
    scripts_dir = _scripts_dir().resolve()
    plugin_root = scripts_dir.parent
    skill_root = plugin_root / "skills" / "webnovel-write"
    runtime_package = scripts_dir / "data_modules" / "__init__.py"
    entry_script = scripts_dir / "webnovel.py"
    extract_script = scripts_dir / "extract_chapter_context.py"

    checks: list[dict[str, object]] = []
    errors: list[dict[str, object]] = []

    def add_check(
        name: str,
        path: Path | str,
        ok: bool,
        *,
        code: str,
        message: str,
        repair: str,
    ) -> None:
        rendered_path = str(path)
        check: dict[str, object] = {
            "name": name,
            "ok": bool(ok),
            "path": rendered_path,
        }
        if not ok:
            check["error"] = message
            errors.append(
                {
                    "code": code,
                    "severity": "blocker",
                    "name": name,
                    "ok": False,
                    "message": message,
                    "path": rendered_path,
                    "repair": repair,
                }
            )
        checks.append(check)

    add_check(
        "scripts_dir",
        scripts_dir,
        scripts_dir.is_dir(),
        code="scripts_dir_missing",
        message="运行时 scripts 目录不存在",
        repair="重新安装完整插件，确认发布包包含 scripts/。",
    )
    add_check(
        "runtime_package",
        runtime_package,
        runtime_package.is_file(),
        code="runtime_package_missing",
        message="Python runtime package 不完整",
        repair="重新安装完整插件，确认 scripts/data_modules/__init__.py 存在。",
    )
    add_check(
        "entry_script",
        entry_script,
        entry_script.is_file(),
        code="unified_cli_missing",
        message="统一 CLI 入口 scripts/webnovel.py 不存在",
        repair="重新安装完整插件，确认统一 CLI 入口已包含在发布包中。",
    )
    add_check(
        "extract_context_script",
        extract_script,
        extract_script.is_file(),
        code="extract_context_missing",
        message="extract-context 入口 scripts/extract_chapter_context.py 不存在",
        repair="重新安装完整插件，确认上下文提取入口已包含在发布包中。",
    )

    project_root = ""
    project_root_error = ""
    story_runtime: dict = {}
    resolved_root: Path | None = None
    try:
        resolved_root = _resolve_root(explicit_project_root)
        project_root = str(resolved_root)
    except FileNotFoundError as exc:
        project_root_error = _project_root_diagnostic(explicit_project_root, exc)
        add_check(
            "project_root",
            explicit_project_root or "",
            False,
            code="project_root_not_found",
            message=project_root_error,
            repair="运行 webnovel init，或用 webnovel use <project_root> 绑定已有书项目。",
        )
    except Exception as exc:
        project_root_error = f"解析项目根目录失败: {exc}"
        add_check(
            "project_root",
            explicit_project_root or "",
            False,
            code="project_root_resolution_failed",
            message=project_root_error,
            repair="检查项目路径、pointer 和 registry 的格式与访问权限后重试。",
        )
    else:
        add_check(
            "project_root",
            resolved_root,
            True,
            code="project_root_not_found",
            message="",
            repair="",
        )

    if resolved_root is not None:
        try:
            story_runtime = build_story_runtime_health(resolved_root)
        except Exception as exc:
            add_check(
                "story_runtime_health",
                resolved_root / ".story-system",
                False,
                code="story_runtime_health_failed",
                message=f"读取 Story Runtime 健康状态失败: {exc}",
                repair="检查 Story System JSON、目录权限和 runtime 文件完整性后重试。",
            )
        else:
            add_check(
                "story_runtime_health",
                resolved_root / ".story-system",
                True,
                code="story_runtime_health_failed",
                message="",
                repair="",
            )

    return {
        "schema_version": "webnovel-preflight/v1",
        "ok": not errors,
        "project_root": project_root,
        "scripts_dir": str(scripts_dir),
        "skill_root": str(skill_root),
        "checks": checks,
        "errors": errors,
        "project_root_error": project_root_error,
        "story_runtime": story_runtime,
    }


def cmd_preflight(args: argparse.Namespace) -> int:
    report = _build_preflight_report(args.project_root)
    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for item in report["checks"]:
            status = "OK" if item["ok"] else "ERROR"
            path = item.get("path") or ""
            print(f"{status} {item['name']}: {path}")
            if item.get("error"):
                print(f"  detail: {item['error']}")
        story_runtime = report.get("story_runtime") or {}
        if story_runtime:
            print(
                "INFO story_runtime: "
                f"chapter={story_runtime.get('chapter')} "
                f"mainline_ready={story_runtime.get('mainline_ready')} "
                f"latest_commit_status={story_runtime.get('latest_commit_status')}"
            )
    return 0 if report["ok"] else 1


def cmd_project_status(args: argparse.Namespace) -> int:
    from .project_status import build_project_status, format_project_status

    try:
        root: Path | str | None = _resolve_root(args.project_root)
    except FileNotFoundError:
        root = args.project_root or None
    report = build_project_status(root, chapter=args.chapter)
    print(format_project_status(report, args.format))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from .doctor import build_doctor_report, format_doctor_report

    preflight_report = _build_preflight_report(args.project_root)
    root: Path | str | None = preflight_report.get("project_root") or args.project_root or None
    report = build_doctor_report(
        root,
        chapter=args.chapter,
        deep=bool(args.deep),
        preflight_report=preflight_report,
    )
    print(format_doctor_report(report, args.format))
    return 0 if report.get("ok") else 1


def cmd_dashboard(args: argparse.Namespace) -> int:
    """Start, inspect, or stop the project-scoped local Dashboard."""

    from .dashboard_lifecycle import (
        dashboard_exit_code,
        dashboard_start,
        dashboard_status,
        dashboard_stop,
        format_dashboard_result,
    )

    root = _resolve_root(args.project_root)
    if args.dashboard_action == "start":
        result = dashboard_start(root, host=args.host, port=args.port)
    elif args.dashboard_action == "status":
        result = dashboard_status(root)
    else:
        result = dashboard_stop(root)
    print(format_dashboard_result(result, args.format))
    return dashboard_exit_code(result)


def cmd_init(args: argparse.Namespace) -> int:
    """Preview or apply one strictly confirmed initialization request."""

    from .init_request import InitRequestError
    from .init_workflow import InitWorkflowError, apply_init, preview_init

    try:
        if args.project_root is not None:
            raise InitRequestError(
                "init does not accept --project-root; the confirmed target comes only from --config-json"
            )
        if args.apply:
            if args.git_mode is None:
                raise InitRequestError(
                    "--apply requires an explicit --git-mode off|init|initial-commit"
                )
            if not args.preview_token:
                raise InitRequestError("--apply requires --preview-token from the matching dry-run")
            if not args.authorization_json:
                raise InitRequestError(
                    "--apply requires --authorization-json with trusted parent user Apply evidence"
                )
            result = apply_init(
                args.config_json,
                git_mode=args.git_mode,
                preview_token=args.preview_token,
                authorization_json=args.authorization_json,
            )
        else:
            if args.preview_token:
                raise InitRequestError("--preview-token is valid only with --apply")
            if args.authorization_json:
                raise InitRequestError("--authorization-json is valid only with --apply")
            result = preview_init(args.config_json, git_mode=args.git_mode or "off")
    except InitRequestError as exc:
        result = {
            "schema_version": "webnovel-init-error/v1",
            "status": "error",
            "code": "invalid_request",
            "error": str(exc),
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2
    except InitWorkflowError as exc:
        result = {
            "schema_version": "webnovel-init-error/v1",
            "status": "blocked",
            "code": getattr(exc, "code", "init_blocked"),
            "error": str(exc),
            "details": getattr(exc, "details", {}),
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1
    except (OSError, UnicodeError, subprocess.SubprocessError, TimeoutError) as exc:
        result = {
            "schema_version": "webnovel-init-error/v1",
            "status": "blocked",
            "code": "init_operational_error",
            "error": str(exc) or exc.__class__.__name__,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") in {"ready", "success"} else 1


def cmd_write_gate(args: argparse.Namespace) -> int:
    from .write_gates import format_gate_report, run_write_gate

    root = _resolve_root(args.project_root)
    report = run_write_gate(root, chapter=args.chapter, stage=args.stage)
    print(format_gate_report(report, args.format))
    return 0 if report.get("ok") else 1


def cmd_projections(args: argparse.Namespace) -> int:
    from .projections import format_projection_report, replay_projections, retry_projection

    root = _resolve_root(args.project_root)
    if args.projection_action == "retry":
        report = retry_projection(root, chapter=args.chapter)
    else:
        report = replay_projections(
            root,
            start_chapter=args.from_chapter,
            end_chapter=args.to_chapter,
        )
    print(format_projection_report(report, args.format))
    return 0 if report.get("ok") else 1


def cmd_user_report(args: argparse.Namespace) -> int:
    from .user_report import build_user_report, format_user_report

    root = _resolve_root(args.project_root)
    report = build_user_report(
        root,
        stage=args.stage,
        chapter=args.chapter,
        volume=args.volume,
    )
    print(format_user_report(report, args.format))
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    from .review_request import ReviewRequestError
    from .review_workflow import (
        ReviewWorkflowError,
        decide_review,
        decide_review_range,
        error_payload,
        format_review_result,
        prepare_review,
        prepare_review_range,
        resume_review,
        resume_review_range,
        accept_review,
    )
    from .run_ledger import RunLedgerError

    try:
        root = _resolve_root(args.project_root)
        action = args.review_action
        if action == "prepare":
            payload = prepare_review(
                root,
                chapter=args.chapter,
                review_mode=args.mode,
                workspace_root=args.workspace_root,
                parent_model=args.parent_model,
                parent_reasoning_effort=args.parent_effort or None,
            )
        elif action == "accept":
            payload = accept_review(root, run_id=args.run_id, request_file=args.request_file)
        elif action == "decide":
            payload = decide_review(
                root,
                run_id=args.run_id,
                request_file=args.request_file,
            )
        elif action == "resume":
            payload = resume_review(root, run_id=args.run_id)
        elif action == "range-prepare":
            payload = prepare_review_range(
                root,
                start=args.start,
                end=args.end,
                review_mode=args.mode,
                workspace_root=args.workspace_root,
                parent_model=args.parent_model,
                parent_reasoning_effort=args.parent_effort or None,
            )
        elif action == "range-resume":
            payload = resume_review_range(root, range_id=args.range_id)
        else:
            payload = decide_review_range(
                root,
                range_id=args.range_id,
                request_file=args.request_file,
            )
    except (ReviewWorkflowError, ReviewRequestError, RunLedgerError, OSError, ValueError) as exc:
        payload = error_payload(exc)
        print(format_review_result(payload, args.format))
        invalid_codes = {
            "invalid_request",
            "invalid_chapter",
            "invalid_range",
            "range_too_large",
            "invalid_review_mode",
            "invalid_choice",
            "invalid_run_id",
            "invalid_range_id",
        }
        return 2 if payload.get("code") in invalid_codes else 1
    print(format_review_result(payload, args.format))
    non_success_statuses = {
        "blocked",
        "recoverable",
        "failed",
        "awaiting_user",
        "paused",
        "targeted_fix_pending",
        "targeted_fix_blocked",
        "failed_validation",
        "failed_persistence",
        "stale",
    }
    return 1 if payload.get("status") in non_success_statuses else 0


def cmd_run_ledger(args: argparse.Namespace) -> int:
    from .run_ledger import (
        build_write_resume_plan,
        format_resume_plan,
        record_write_step,
    )

    root = _resolve_root(args.project_root)
    if args.ledger_action == "record-write-step":
        try:
            inputs = json.loads(args.inputs_json)
            outputs = json.loads(args.outputs_json)
            problems = json.loads(args.problems_json)
            auto_handled = json.loads(args.auto_handled_json)
        except json.JSONDecodeError as exc:
            print(f"ledger JSON 参数不合法: {exc}", file=sys.stderr)
            return 2
        if not isinstance(inputs, dict) or not isinstance(outputs, dict):
            print("inputs-json / outputs-json 必须是 JSON object", file=sys.stderr)
            return 2
        if not isinstance(problems, list) or not isinstance(auto_handled, list):
            print("problems-json / auto-handled-json 必须是 JSON list", file=sys.stderr)
            return 2
        entry = record_write_step(
            root,
            chapter=args.chapter,
            step=args.step,
            status=args.status,
            mode=args.mode,
            inputs={str(key): str(value) for key, value in inputs.items()},
            outputs={str(key): str(value) for key, value in outputs.items()},
            problems=[str(item) for item in problems],
            auto_handled=[str(item) for item in auto_handled],
            duration_ms=args.duration_ms,
        )
        if args.format == "json":
            print(json.dumps(entry, ensure_ascii=False, indent=2))
        else:
            print(f"{entry['step']}: {entry['status']}")
        return 0
    if args.ledger_action == "write-resume":
        report = build_write_resume_plan(
            root,
            chapter=args.chapter,
            mode=args.mode,
        )
        print(format_resume_plan(report, args.format))
        return 0
    return 2


def cmd_run_log(args: argparse.Namespace) -> int:
    from .run_logger import write_run_log

    try:
        root = _resolve_root(args.project_root)
    except FileNotFoundError:
        root = normalize_windows_path(args.project_root).expanduser()
        try:
            root = root.resolve()
        except Exception:
            root = root
    try:
        payload = json.loads(args.payload_json)
    except json.JSONDecodeError as exc:
        print(f"payload-json 不是合法 JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(payload, dict):
        print("payload-json 必须是 JSON object", file=sys.stderr)
        return 2
    result = write_run_log(root, event=args.event, payload=payload, append=args.append)
    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(result["path"])
    return 0


def cmd_use(args: argparse.Namespace) -> int:
    project_root = normalize_windows_path(args.project_root).expanduser()
    try:
        project_root = project_root.resolve()
    except Exception as exc:
        print(f"⚠️ path.resolve() 失败 ({project_root}): {exc}", file=sys.stderr)
        project_root = project_root

    workspace_root: Optional[Path] = None
    if args.workspace_root:
        workspace_root = normalize_windows_path(args.workspace_root).expanduser()
        try:
            workspace_root = workspace_root.resolve()
        except Exception as exc:
            print(f"⚠️ path.resolve() 失败 ({workspace_root}): {exc}", file=sys.stderr)
            workspace_root = workspace_root

    if workspace_root is None:
        workspace_root = confirm_current_workspace(project_root)
        if workspace_root is None:
            print(
                "无法安全确认当前 workspace；请显式传入 --workspace-root，"
                "不会猜测书项目父目录或写入插件目录。",
                file=sys.stderr,
            )
            return 2

    try:
        binding = bind_current_project(project_root, workspace_root=workspace_root)
    except (FileNotFoundError, OSError, UnicodeError, ValueError) as exc:
        print(f"无法绑定书项目: {exc}", file=sys.stderr)
        return 2
    if binding.pointer_path is not None:
        print(f"workspace pointer: {binding.pointer_path}")
    else:
        print("workspace pointer: (skipped)")

    if binding.registry_path is not None:
        print(f"global registry: {binding.registry_path}")
    else:
        print("global registry: (skipped)")

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="webnovel unified CLI")
    parser.add_argument("--project-root", help="书项目根目录（必须包含 .webnovel/state.json；可选，默认自动检测）")

    sub = parser.add_subparsers(dest="tool", required=True)

    p_where = sub.add_parser("where", help="打印解析出的 project_root")
    p_where.add_argument("--format", choices=["text", "json"], default="text", help="输出格式")
    p_where.set_defaults(func=cmd_where)

    p_codex_setup = sub.add_parser("codex-setup", help="检查或安装项目级 Codex Agent")
    p_codex_setup.add_argument("--workspace-root", required=True, help="要安装项目 Agent 的工作区根目录")
    setup_mode = p_codex_setup.add_mutually_exclusive_group()
    setup_mode.add_argument("--check", action="store_true", help="只检查，不写入（默认）")
    setup_mode.add_argument("--apply", action="store_true", help="安装或更新已管理的 Agent")
    p_codex_setup.add_argument("--format", choices=["text", "json"], default="text", help="输出格式")
    p_codex_setup.set_defaults(func=cmd_codex_setup)

    p_preflight = sub.add_parser("preflight", help="校验统一 CLI 运行环境与 project_root")
    p_preflight.add_argument("--format", choices=["text", "json"], default="text", help="输出格式")
    p_preflight.set_defaults(func=cmd_preflight)

    p_project_status = sub.add_parser("project-status", help="输出机器可读的项目短状态")
    p_project_status.add_argument("--chapter", type=int, default=None, help="目标章节号")
    p_project_status.add_argument("--format", choices=["summary", "json"], default="summary", help="输出格式")
    p_project_status.set_defaults(func=cmd_project_status)

    p_doctor = sub.add_parser("doctor", help="阶段感知的只读项目体检")
    p_doctor.add_argument("--chapter", type=int, default=None, help="目标章节号")
    p_doctor.add_argument("--deep", action="store_true", help="包含 dashboard 等较深检查")
    p_doctor.add_argument("--format", choices=["text", "json"], default="text", help="输出格式")
    p_doctor.set_defaults(func=cmd_doctor)

    p_dashboard = sub.add_parser("dashboard", help="管理项目级只读本地 Dashboard")
    dashboard_sub = p_dashboard.add_subparsers(dest="dashboard_action", required=True)
    p_dashboard_start = dashboard_sub.add_parser("start", help="在数字 loopback 上启动 Dashboard")
    p_dashboard_start.add_argument("--host", default="127.0.0.1", help="仅允许 localhost/127.0.0.1")
    p_dashboard_start.add_argument("--port", type=int, default=0, help="监听端口；0 为动态端口")
    p_dashboard_start.add_argument(
        "--no-browser",
        action="store_true",
        help="兼容参数；Dashboard 始终不会自动打开浏览器",
    )
    p_dashboard_start.add_argument("--format", choices=["text", "json"], default="text", help="输出格式")
    p_dashboard_start.set_defaults(func=cmd_dashboard)
    p_dashboard_status = dashboard_sub.add_parser("status", help="只读检查 Dashboard 状态")
    p_dashboard_status.add_argument("--format", choices=["text", "json"], default="text", help="输出格式")
    p_dashboard_status.set_defaults(func=cmd_dashboard)
    p_dashboard_stop = dashboard_sub.add_parser("stop", help="停止已验证身份的 Dashboard")
    p_dashboard_stop.add_argument("--format", choices=["text", "json"], default="text", help="输出格式")
    p_dashboard_stop.set_defaults(func=cmd_dashboard)

    p_write_gate = sub.add_parser("write-gate", help="写章自然边界校验")
    p_write_gate.add_argument("--chapter", type=int, required=True, help="目标章节号")
    p_write_gate.add_argument("--stage", choices=["prewrite", "precommit", "postcommit"], required=True, help="校验阶段")
    p_write_gate.add_argument("--format", choices=["json", "text"], default="json", help="输出格式")
    p_write_gate.set_defaults(func=cmd_write_gate)

    p_projections = sub.add_parser("projections", help="从已有 commit 补跑或重放 projection")
    projections_sub = p_projections.add_subparsers(dest="projection_action", required=True)
    p_projection_retry = projections_sub.add_parser("retry", help="补跑单章 projection")
    p_projection_retry.add_argument("--chapter", type=int, required=True, help="目标章节号")
    p_projection_retry.add_argument("--format", choices=["json", "text"], default="json", help="输出格式")
    p_projection_retry.set_defaults(func=cmd_projections)
    p_projection_replay = projections_sub.add_parser("replay", help="按章节范围重放 projection")
    p_projection_replay.add_argument("--from-chapter", type=int, required=True, help="起始章节号")
    p_projection_replay.add_argument("--to-chapter", type=int, required=True, help="结束章节号")
    p_projection_replay.add_argument("--format", choices=["json", "text"], default="json", help="输出格式")
    p_projection_replay.set_defaults(func=cmd_projections)

    p_user_report = sub.add_parser("user-report", help="渲染作者友好的最终报告")
    p_user_report.add_argument("--stage", choices=["init", "plan", "write", "review"], required=True, help="报告阶段")
    p_user_report.add_argument("--chapter", type=int, default=None, help="目标章节号")
    p_user_report.add_argument("--volume", type=int, default=None, help="目标卷号")
    p_user_report.add_argument("--format", choices=["text", "json"], default="text", help="输出格式")
    p_user_report.set_defaults(func=cmd_user_report)

    p_review = sub.add_parser("review", help="严格单章或最多五章串行审查")
    review_sub = p_review.add_subparsers(dest="review_action", required=True)
    p_review_prepare = review_sub.add_parser("prepare", help="生成 reviewer 的只读请求包")
    p_review_prepare.add_argument("--chapter", type=int, required=True)
    p_review_prepare.add_argument("--mode", choices=["full", "fast"], default="full")
    p_review_prepare.add_argument("--workspace-root", required=True)
    p_review_prepare.add_argument("--parent-model", required=True)
    p_review_prepare.add_argument("--parent-effort", default="")
    p_review_prepare.add_argument("--format", choices=["json", "text"], default="json")
    p_review_prepare.set_defaults(func=cmd_review)
    p_review_accept = review_sub.add_parser("accept", help="验证 runtime evidence 与 reviewer JSON")
    p_review_accept.add_argument("--run-id", required=True)
    p_review_accept.add_argument("--request-file", required=True)
    p_review_accept.add_argument("--format", choices=["json", "text"], default="json")
    p_review_accept.set_defaults(func=cmd_review)
    p_review_decide = review_sub.add_parser("decide", help="处理 blocking 三选一裁决")
    p_review_decide.add_argument("--run-id", required=True)
    p_review_decide.add_argument("--request-file", required=True)
    p_review_decide.add_argument("--format", choices=["json", "text"], default="json")
    p_review_decide.set_defaults(func=cmd_review)
    p_review_resume = review_sub.add_parser("resume", help="从 ledger 的最早未完成步骤恢复")
    p_review_resume.add_argument("--run-id", required=True)
    p_review_resume.add_argument("--format", choices=["json", "text"], default="json")
    p_review_resume.set_defaults(func=cmd_review)
    p_range_prepare = review_sub.add_parser("range-prepare", help="准备最多五章的串行范围审查")
    p_range_prepare.add_argument("--start", type=int, required=True)
    p_range_prepare.add_argument("--end", type=int, required=True)
    p_range_prepare.add_argument("--mode", choices=["full", "fast"], default="full")
    p_range_prepare.add_argument("--workspace-root", required=True)
    p_range_prepare.add_argument("--parent-model", required=True)
    p_range_prepare.add_argument("--parent-effort", default="")
    p_range_prepare.add_argument("--format", choices=["json", "text"], default="json")
    p_range_prepare.set_defaults(func=cmd_review)
    p_range_resume = review_sub.add_parser("range-resume", help="恢复范围审查当前章节")
    p_range_resume.add_argument("--range-id", required=True)
    p_range_resume.add_argument("--format", choices=["json", "text"], default="json")
    p_range_resume.set_defaults(func=cmd_review)
    p_range_decide = review_sub.add_parser("range-decide", help="范围 blocker/失败后的停止或继续裁决")
    p_range_decide.add_argument("--range-id", required=True)
    p_range_decide.add_argument("--request-file", required=True)
    p_range_decide.add_argument("--format", choices=["json", "text"], default="json")
    p_range_decide.set_defaults(func=cmd_review)

    p_run_ledger = sub.add_parser("run-ledger", help="记录或查询写章断点续跑状态")
    run_ledger_sub = p_run_ledger.add_subparsers(dest="ledger_action", required=True)
    p_record_write_step = run_ledger_sub.add_parser("record-write-step", help="记录写章步骤状态")
    p_record_write_step.add_argument("--chapter", type=int, required=True, help="目标章节号")
    p_record_write_step.add_argument("--step", choices=["draft", "review", "data", "commit", "projection", "backup"], required=True)
    p_record_write_step.add_argument("--status", required=True)
    p_record_write_step.add_argument("--mode", default="default")
    p_record_write_step.add_argument("--inputs-json", default="{}")
    p_record_write_step.add_argument("--outputs-json", default="{}")
    p_record_write_step.add_argument("--problems-json", default="[]")
    p_record_write_step.add_argument("--auto-handled-json", default="[]")
    p_record_write_step.add_argument("--duration-ms", type=int, default=0)
    p_record_write_step.add_argument("--format", choices=["json", "text"], default="json", help="输出格式")
    p_record_write_step.set_defaults(func=cmd_run_ledger)
    p_write_resume = run_ledger_sub.add_parser("write-resume", help="输出写章断点续跑建议")
    p_write_resume.add_argument("--chapter", type=int, required=True, help="目标章节号")
    p_write_resume.add_argument("--mode", default="default", help="写章模式")
    p_write_resume.add_argument("--format", choices=["json", "text"], default="json", help="输出格式")
    p_write_resume.set_defaults(func=cmd_run_ledger)

    p_run_log = sub.add_parser("run-log", help="写入脱敏运行日志")
    p_run_log.add_argument("--event", required=True, help="事件名")
    p_run_log.add_argument("--payload-json", default="{}", help="要写入日志的 JSON 对象")
    p_run_log.add_argument("--append", action="store_true", help="追加而不是覆盖 run_last.log")
    p_run_log.add_argument("--format", choices=["json", "text"], default="json", help="输出格式")
    p_run_log.set_defaults(func=cmd_run_log)

    p_use = sub.add_parser("use", help="绑定当前工作区使用的书项目（写入指针/registry）")
    p_use.add_argument("project_root", help="书项目根目录（必须包含 .webnovel/state.json）")
    p_use.add_argument(
        "--workspace-root",
        help="工作区根目录；省略时仅在当前目录能安全确认工作区时自动使用",
    )
    p_use.set_defaults(func=cmd_use)

    # Pass-through to data modules
    p_index = sub.add_parser("index", help="转发到 index_manager")
    p_index.add_argument("args", nargs=argparse.REMAINDER)

    p_state = sub.add_parser("state", help="转发到 state_manager")
    p_state.add_argument("args", nargs=argparse.REMAINDER)

    p_rag = sub.add_parser("rag", help="转发到 rag_adapter")
    p_rag.add_argument("args", nargs=argparse.REMAINDER)

    p_style = sub.add_parser("style", help="转发到 style_sampler")
    p_style.add_argument("args", nargs=argparse.REMAINDER)

    p_entity = sub.add_parser("entity", help="转发到 entity_linker")
    p_entity.add_argument("args", nargs=argparse.REMAINDER)

    p_context = sub.add_parser("context", help="转发到 context_manager")
    p_context.add_argument("args", nargs=argparse.REMAINDER)

    p_memory = sub.add_parser("memory", help="转发到 memory.store")
    p_memory.add_argument("args", nargs=argparse.REMAINDER)

    p_migrate = sub.add_parser("migrate", help="转发到 migrate_state_to_sqlite")
    p_migrate.add_argument("args", nargs=argparse.REMAINDER)

    # Pass-through to scripts
    p_status = sub.add_parser("status", help="转发到 status_reporter.py")
    p_status.add_argument("args", nargs=argparse.REMAINDER)

    p_update_state = sub.add_parser("update-state", help="转发到 update_state.py")
    p_update_state.add_argument("args", nargs=argparse.REMAINDER)

    p_backup = sub.add_parser("backup", help="转发到 backup_manager.py")
    p_backup.add_argument("args", nargs=argparse.REMAINDER)

    p_archive = sub.add_parser("archive", help="转发到 archive_manager.py")
    p_archive.add_argument("args", nargs=argparse.REMAINDER)

    p_init = sub.add_parser("init", help="两阶段、missing-only 的受控项目初始化")
    p_init.add_argument("--config-json", required=True, help="WEBNOVEL_HOME/tmp/init 下的严格请求 JSON")
    init_mode = p_init.add_mutually_exclusive_group(required=True)
    init_mode.add_argument("--dry-run", action="store_true", help="零目标写入预览")
    init_mode.add_argument("--apply", action="store_true", help="应用匹配的已确认预览")
    p_init.add_argument(
        "--git-mode",
        choices=["off", "init", "initial-commit"],
        default=None,
        help="dry-run 默认 off；apply 必须显式提供",
    )
    p_init.add_argument("--preview-token", default="", help="apply 所需的 matching dry-run token")
    p_init.add_argument(
        "--authorization-json",
        default="",
        help="apply 所需的可信 parent rollout 有限选择授权 JSON",
    )
    p_init.set_defaults(func=cmd_init)

    p_extract_context = sub.add_parser("extract-context", help="转发到 extract_chapter_context.py")
    p_extract_context.add_argument("--chapter", type=int, required=True, help="目标章节号")
    p_extract_context.add_argument("--format", choices=["text", "json"], default="text", help="输出格式")

    p_story_system = sub.add_parser("story-system", help="转发到 story_system.py")
    p_story_system.add_argument("args", nargs=argparse.REMAINDER)

    p_story_events = sub.add_parser("story-events", help="转发到 story_events.py")
    p_story_events.add_argument("--chapter", type=int, default=0, help="目标章节号")
    p_story_events.add_argument("--limit", type=int, default=200, help="查询条数")
    p_story_events.add_argument("--health", action="store_true", help="输出事件链健康信息")

    p_commit = sub.add_parser("chapter-commit", help="转发到 chapter_commit.py")
    p_commit.add_argument("--chapter", type=int, required=True, help="目标章节号")
    p_commit.add_argument("--review-result", default="", help="review_result JSON 文件")
    p_commit.add_argument("--fulfillment-result", default="", help="fulfillment_result JSON 文件")
    p_commit.add_argument("--disambiguation-result", default="", help="disambiguation_result JSON 文件")
    p_commit.add_argument("--extraction-result", default="", help="extraction_result JSON 文件")

    p_memory_contract = sub.add_parser("memory-contract", help="转发到 memory_cli.py")
    p_memory_contract.add_argument("args", nargs=argparse.REMAINDER)

    p_project_memory = sub.add_parser("project-memory", help="转发到 project_memory.py")
    p_project_memory.add_argument("args", nargs=argparse.REMAINDER)

    p_plan_request = sub.add_parser("plan-request", help="生成并保存当前父任务的规划请求")
    p_plan_request.add_argument("args", nargs=argparse.REMAINDER)

    p_plan_validate = sub.add_parser("plan-validate", help="严格校验规划 manifest 与 staging artifacts")
    p_plan_validate.add_argument("args", nargs=argparse.REMAINDER)

    p_plan_transaction = sub.add_parser("plan-transaction", help="验证、提升并恢复规划事务")
    p_plan_transaction.add_argument("args", nargs=argparse.REMAINDER)

    p_write_transaction = sub.add_parser("write-transaction", help="编排并恢复完整写章事务")
    p_write_transaction.add_argument("args", nargs=argparse.REMAINDER)

    p_review_pipeline = sub.add_parser("review-pipeline", help="兼容入口：仅恢复已验证的 review run")
    p_review_pipeline.add_argument("--run-id", required=True, help="已通过 Agent/runtime/hash/schema gate 的 run id")

    p_placeholder_scan = sub.add_parser("placeholder-scan", help="扫描大纲/设定集未补齐占位")
    p_placeholder_scan.add_argument("--format", choices=["json", "text"], default="json", help="输出格式")

    p_master_outline_sync = sub.add_parser("master-outline-sync", help="当前卷规划完成后写回 V+1 最小总纲锚点")
    p_master_outline_sync.add_argument("--volume", type=int, required=True, help="当前已完成规划的卷号")
    p_master_outline_sync.add_argument("--writeback-file", default="", help="显式结构化写回 JSON")
    p_master_outline_sync.add_argument("--format", choices=["json", "text"], default="json", help="输出格式")

    knowledge_parser = sub.add_parser("knowledge", help="时序知识查询")
    knowledge_sub = knowledge_parser.add_subparsers(dest="knowledge_action", required=True)

    qs_parser = knowledge_sub.add_parser("query-entity-state", help="查询实体在指定章节的状态")
    qs_parser.add_argument("--entity", help="实体 ID 或名称；不可信文本优先使用 --request-file")
    qs_parser.add_argument("--at-chapter", type=int, help="目标章节号")
    qs_parser.add_argument("--request-file", help="绝对路径的 webnovel-query-request/v1 JSON")

    qr_parser = knowledge_sub.add_parser("query-relationships", help="查询实体在指定章节的关系")
    qr_parser.add_argument("--entity", help="实体 ID 或名称；不可信文本优先使用 --request-file")
    qr_parser.add_argument("--at-chapter", type=int, help="目标章节号")
    qr_parser.add_argument("--request-file", help="绝对路径的 webnovel-query-request/v1 JSON")

    # 兼容：允许 `--project-root` 出现在任意位置（减少 agents/skills 拼命令的出错率）
    from .cli_args import normalize_global_project_root

    argv = normalize_global_project_root(sys.argv[1:])
    args, unknown_args = parser.parse_known_args(argv)

    # where/use 直接执行
    if hasattr(args, "func"):
        if unknown_args:
            parser.error(f"unrecognized arguments: {' '.join(unknown_args)}")
        code = int(args.func(args) or 0)
        raise SystemExit(code)

    tool = args.tool
    if unknown_args and tool not in PASSTHROUGH_TOOLS:
        parser.error(f"unrecognized arguments: {' '.join(unknown_args)}")

    rest = _passthrough_tail(argv, tool) if tool in PASSTHROUGH_TOOLS else list(getattr(args, "args", []) or [])
    # argparse.REMAINDER 可能以 `--` 开头占位，这里去掉
    if rest[:1] == ["--"]:
        rest = rest[1:]
    rest = _strip_project_root_args(rest)

    # 其余工具：统一解析 project_root 后前置给下游
    project_root = _resolve_root(args.project_root)
    forward_args = ["--project-root", str(project_root)]

    if tool == "index":
        raise SystemExit(_run_data_module("index_manager", [*forward_args, *rest]))
    if tool == "state":
        raise SystemExit(_run_data_module("state_manager", [*forward_args, *rest]))
    if tool == "rag":
        raise SystemExit(_run_data_module("rag_adapter", [*forward_args, *rest]))
    if tool == "style":
        raise SystemExit(_run_data_module("style_sampler", [*forward_args, *rest]))
    if tool == "entity":
        raise SystemExit(_run_data_module("entity_linker", [*forward_args, *rest]))
    if tool == "context":
        raise SystemExit(_run_data_module("context_manager", [*forward_args, *rest]))
    if tool == "memory":
        raise SystemExit(_run_data_module("memory.store", [*forward_args, *rest]))
    if tool == "migrate":
        raise SystemExit(_run_data_module("migrate_state_to_sqlite", [*forward_args, *rest]))

    if tool == "status":
        raise SystemExit(_run_script("status_reporter.py", [*forward_args, *rest]))
    if tool == "update-state":
        raise SystemExit(_run_script("update_state.py", [*forward_args, *rest]))
    if tool == "backup":
        raise SystemExit(_run_script("backup_manager.py", [*forward_args, *rest]))
    if tool == "archive":
        raise SystemExit(_run_script("archive_manager.py", [*forward_args, *rest]))
    if tool == "extract-context":
        return_args = [*forward_args, "--chapter", str(args.chapter), "--format", str(args.format)]
        raise SystemExit(_run_script("extract_chapter_context.py", return_args))
    if tool == "story-system":
        raise SystemExit(_run_script("story_system.py", [*forward_args, *rest]))
    if tool == "story-events":
        return_args = [*forward_args, "--limit", str(args.limit)]
        if args.chapter:
            return_args.extend(["--chapter", str(args.chapter)])
        if args.health:
            return_args.append("--health")
        raise SystemExit(_run_script("story_events.py", return_args))
    if tool == "chapter-commit":
        return_args = [*forward_args, "--chapter", str(args.chapter)]
        if args.review_result:
            return_args.extend(["--review-result", str(args.review_result)])
        if args.fulfillment_result:
            return_args.extend(["--fulfillment-result", str(args.fulfillment_result)])
        if args.disambiguation_result:
            return_args.extend(["--disambiguation-result", str(args.disambiguation_result)])
        if args.extraction_result:
            return_args.extend(["--extraction-result", str(args.extraction_result)])
        raise SystemExit(_run_script("chapter_commit.py", return_args))
    if tool == "memory-contract":
        raise SystemExit(_run_script("memory_cli.py", [*forward_args, *rest]))
    if tool == "project-memory":
        raise SystemExit(_run_script("project_memory.py", [*forward_args, *rest]))
    if tool == "plan-request":
        raise SystemExit(_run_data_module("plan_request", [*forward_args, *rest]))
    if tool == "plan-validate":
        raise SystemExit(_run_data_module("plan_validator", [*forward_args, *rest]))
    if tool == "plan-transaction":
        raise SystemExit(_run_data_module("plan_transaction", [*forward_args, *rest]))
    if tool == "write-transaction":
        raise SystemExit(_run_data_module("write_transaction", [*forward_args, *rest]))
    if tool == "review-pipeline":
        return_args = [*forward_args, "--run-id", str(args.run_id)]
        raise SystemExit(_run_script("review_pipeline.py", return_args))
    if tool == "placeholder-scan":
        raise SystemExit(_run_data_module("placeholder_scanner", [*forward_args, "--format", str(args.format)]))
    if tool == "master-outline-sync":
        return_args = [*forward_args, "--volume", str(args.volume), "--format", str(args.format)]
        if args.writeback_file:
            return_args.extend(["--writeback-file", str(args.writeback_file)])
        raise SystemExit(_run_script("update_master_outline.py", return_args))

    if tool == "knowledge":
        import sqlite3

        from .knowledge_query import KnowledgeQuery
        from .cli_output import build_error, print_json
        from .query_request import QueryRequestError, load_query_request

        query_type = (
            "entity_state"
            if args.knowledge_action == "query-entity-state"
            else "relationships"
        )

        def emit_query_error(
            code: str,
            message: str,
            *,
            suggestion: str = "",
            details: dict | None = None,
        ) -> None:
            payload = build_error(
                code,
                message,
                suggestion=suggestion or None,
                details=details,
            )
            payload.update(
                {
                    "schema_version": "webnovel-query-result/v1",
                    "query_type": query_type,
                    "sources": (details or {}).get("sources") or [],
                }
            )
            print_json(payload)

        if args.request_file:
            if args.entity is not None or args.at_chapter is not None:
                emit_query_error(
                    "INVALID_QUERY_REQUEST",
                    "--request-file 不能与 --entity/--at-chapter 混用。",
                )
                raise SystemExit(2)
            try:
                request = load_query_request(
                    args.request_file,
                    project_root=project_root,
                    expected_query_types={query_type},
                )
            except (OSError, QueryRequestError) as exc:
                emit_query_error("INVALID_QUERY_REQUEST", str(exc))
                raise SystemExit(2) from None
            args.entity = request["entity"]
            args.at_chapter = request["at_chapter"]
        if not isinstance(args.entity, str) or not args.entity or not isinstance(args.at_chapter, int) or args.at_chapter <= 0:
            emit_query_error(
                "INVALID_QUERY_INPUT",
                "必须提供非空实体和正整数章节；不可信文本请使用 --request-file。",
            )
            raise SystemExit(2)

        kq = KnowledgeQuery(project_root)
        try:
            if args.knowledge_action == "query-entity-state":
                result = kq.entity_state_at_chapter(args.entity, args.at_chapter)
                message = "entity_state_at_chapter"
            elif args.knowledge_action == "query-relationships":
                result = kq.entity_relationships_at_chapter(args.entity, args.at_chapter)
                message = "entity_relationships_at_chapter"
            else:
                raise SystemExit(2)
        except (FileNotFoundError, sqlite3.Error) as exc:
            emit_query_error(
                "READ_MODEL_UNAVAILABLE",
                "只读查询所需的 index.db 或表不可用。",
                suggestion="运行 $webnovel-doctor 检查投影；不会自动创建或修复数据库。",
                details={"path": str(project_root / ".webnovel" / "index.db"), "error": str(exc)},
            )
            raise SystemExit(1) from None
        if (result.get("resolution") or {}).get("status") == "ambiguous":
            emit_query_error(
                "AMBIGUOUS_ENTITY",
                "实体名称匹配到多个候选，未自动选择。",
                suggestion="使用候选中的明确 entity_id 重新查询。",
                details={
                    "query": args.entity,
                    "candidates": (result.get("resolution") or {}).get("candidates") or [],
                    "sources": result.get("sources") or [],
                },
            )
            raise SystemExit(1)
        data = dict(result)
        sources = list(data.pop("sources", []) or [])
        fallback_reasons = sorted(
            {
                str(reason)
                for source in sources
                for reason in (source.get("fallback_reasons") or [])
            }
        )
        print_json(
            {
                "schema_version": "webnovel-query-result/v1",
                "query_type": query_type,
                "status": "success",
                "message": message,
                "data": data,
                "sources": sources,
                "legacy_fallback": bool(fallback_reasons),
                "fallback_reasons": fallback_reasons,
            }
        )
        raise SystemExit(0)

    raise SystemExit(2)


if __name__ == "__main__":
    main()
