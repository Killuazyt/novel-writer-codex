#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
memory_cli.py — MemoryContract CLI 入口。

提供 load-context / query-entity / query-rules / read-summary /
get-open-loops / get-timeline 六个子命令，输出 JSON。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from runtime_compat import enable_windows_utf8_stdio


def _ensure_scripts_path() -> None:
    scripts_dir = Path(__file__).resolve().parent
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))


_ensure_scripts_path()

from data_modules.config import DataModulesConfig
from data_modules.memory_contract_adapter import MemoryContractAdapter
from data_modules.query_request import QueryRequestError, load_query_request
from data_modules.story_runtime_sources import load_runtime_sources
from data_modules.story_contracts import StoryContractPaths
from chapter_outline_loader import (
    load_chapter_outline,
    resolve_chapter_outline_file,
    volume_num_for_chapter_from_state,
)


QUERY_SCHEMA_VERSION = "webnovel-query-result/v1"


def _adapter(project_root: str, *, read_only: bool = False) -> MemoryContractAdapter:
    cfg = DataModulesConfig.from_project_root(project_root)
    return MemoryContractAdapter(cfg, read_only=read_only)


def _json_out(data) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _state_chapter(project_root: Path) -> int:
    try:
        payload = json.loads((project_root / ".webnovel" / "state.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 1
    progress = payload.get("progress") if isinstance(payload, dict) else {}
    try:
        return max(1, int((progress or {}).get("current_chapter") or 1))
    except (TypeError, ValueError):
        return 1


def _runtime_fallback_reasons(project_root: Path, chapter: int | None) -> list[str]:
    target = max(1, int(chapter or _state_chapter(project_root)))
    try:
        return list(load_runtime_sources(project_root, target).fallback_sources)
    except Exception as exc:
        return [f"runtime_source_error:{exc}"]


def _line_count(path: Path) -> int | None:
    try:
        return len(path.read_text(encoding="utf-8").splitlines())
    except (OSError, UnicodeError):
        return None


def _source(
    path: Path,
    *,
    kind: str,
    fallback: bool,
    label: str,
    with_lines: bool = False,
    role: str | None = None,
    line_start: int | None = None,
    line_end: int | None = None,
) -> dict[str, Any]:
    lines = _line_count(path) if with_lines and path.is_file() else None
    return {
        "kind": kind,
        "role": role
        or (
            "authoritative"
            if kind in {"story_contract", "accepted_commit"}
            else "non_authoritative"
            if kind == "commit"
            else "derived"
        ),
        "path": str(path.resolve()),
        "line_start": line_start if line_start is not None else (1 if lines else None),
        "line_end": line_end if line_end is not None else lines,
        "exists": path.is_file(),
        "fallback": bool(fallback),
        "label": label,
    }


def _text_excerpt_lines(path: Path, excerpt: str) -> tuple[int | None, int | None]:
    if not path.is_file() or not excerpt:
        return None, None
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None, None
    index = content.find(excerpt)
    if index < 0:
        index = content.find(excerpt[: min(len(excerpt), 200)])
    if index < 0:
        return None, None
    start = content[:index].count("\n") + 1
    end = start + excerpt.count("\n")
    return start, end


def _dedupe_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for source in sources:
        key = (
            str(source.get("path") or ""),
            str(source.get("kind") or ""),
            str(source.get("label") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(source)
    return result


def _provenance_sources(
    args: argparse.Namespace,
    query_type: str,
    data: Any,
) -> tuple[list[dict[str, Any]], list[str]]:
    root = Path(args.project_root).resolve()
    chapter = int(getattr(args, "chapter", 0) or 0) or None
    fallback_reasons = _runtime_fallback_reasons(root, chapter)
    fallback = bool(fallback_reasons)
    if query_type == "chapter_summary":
        path = root / ".webnovel" / "summaries" / f"ch{int(chapter or 0):04d}.md"
        return [
            _source(
                path,
                kind="derived_summary",
                fallback=fallback,
                label="legacy_projection_fallback" if fallback else "projection_read_model",
                with_lines=True,
            )
        ], fallback_reasons
    if query_type in {"world_rules", "open_loops"}:
        return [
            _source(
                root / ".webnovel" / "memory_scratchpad.json",
                kind="derived_memory",
                fallback=fallback,
                label="legacy_projection_fallback" if fallback else "projection_read_model",
            )
        ], fallback_reasons
    if query_type == "comprehensive_context":
        sections = data.get("sections") if isinstance(data, dict) else {}
        runtime_status = sections.get("runtime_status") if isinstance(sections, dict) else {}
        fallback_sources = (
            list(runtime_status.get("fallback_sources") or [])
            if isinstance(runtime_status, dict)
            else []
        )
        if fallback_sources:
            fallback_reasons = [str(item) for item in fallback_sources]
        sources: list[dict[str, Any]] = []
        story_root = root / ".story-system"
        target_chapter = int(chapter or 1)
        volume = volume_num_for_chapter_from_state(root, target_chapter) or 1
        candidates = (
            ("story_contract", story_root / "MASTER_SETTING.json"),
            ("story_contract", story_root / "volumes" / f"volume_{int(volume):03d}.json"),
            ("story_contract", story_root / "chapters" / f"chapter_{target_chapter:03d}.json"),
            ("story_contract", story_root / "reviews" / f"chapter_{target_chapter:03d}.review.json"),
            ("derived_memory", root / ".webnovel" / "memory_scratchpad.json"),
            ("sqlite_read_model", root / ".webnovel" / "index.db"),
            ("derived_state", root / ".webnovel" / "state.json"),
        )
        for kind, path in candidates:
            is_derived = kind != "story_contract"
            sources.append(
                _source(
                    path,
                    kind=kind,
                    fallback=bool(fallback_reasons) if is_derived else not path.is_file(),
                    label=(
                        "legacy_projection_fallback"
                        if is_derived and fallback_reasons
                        else "projection_read_model"
                        if is_derived
                        else "story_system_contract"
                        if path.is_file()
                        else "missing_authoritative_source"
                    ),
                    with_lines=kind == "story_contract",
                )
            )

        outline_path = resolve_chapter_outline_file(root, target_chapter)
        if outline_path is not None and outline_path.is_file():
            outline_text = str((sections or {}).get("outline") or "")
            if not outline_text:
                try:
                    outline_text = load_chapter_outline(root, target_chapter, max_chars=None)
                except (OSError, UnicodeError):
                    outline_text = ""
            line_start, line_end = _text_excerpt_lines(outline_path, outline_text)
            sources.append(
                _source(
                    outline_path,
                    kind="authored_outline",
                    role="authored_context",
                    fallback=False,
                    label="authored_project_context",
                    line_start=line_start,
                    line_end=line_end,
                )
            )

        cfg = DataModulesConfig.from_project_root(root)
        summary_window = max(2, int(getattr(cfg, "context_recent_summaries_window", 3)))
        for previous in range(max(1, target_chapter - summary_window), target_chapter):
            summary_path = root / ".webnovel" / "summaries" / f"ch{previous:04d}.md"
            if summary_path.is_file():
                sources.append(
                    _source(
                        summary_path,
                        kind="derived_summary",
                        fallback=bool(fallback_reasons),
                        label=(
                            "legacy_projection_fallback"
                            if fallback_reasons
                            else "projection_read_model"
                        ),
                        with_lines=True,
                    )
                )

        if isinstance(sections, dict) and sections.get("author_style_patterns"):
            sources.append(
                _source(
                    root / ".webnovel" / "project_memory.json",
                    kind="derived_author_memory",
                    fallback=False,
                    label="learned_author_memory",
                )
            )

        if isinstance(sections, dict) and sections.get("style_contract"):
            style_path = root / "设定集" / "风格契约.md"
            if not style_path.is_file() and (root / "设定集").is_dir():
                style_path = next(iter(sorted((root / "设定集").glob("*风格契约*.md"))), style_path)
            style_text = str(sections.get("style_contract") or "")
            line_start, line_end = _text_excerpt_lines(style_path, style_text)
            sources.append(
                _source(
                    style_path,
                    kind="authored_style_contract",
                    role="authored_context",
                    fallback=False,
                    label="authored_project_context",
                    line_start=line_start,
                    line_end=line_end,
                )
            )

        genre_source = sections.get("genre_profile_source") if isinstance(sections, dict) else {}
        if isinstance(genre_source, dict) and genre_source.get("path"):
            genre_path = Path(str(genre_source["path"]))
            genre_excerpt = str(sections.get("genre_profile_excerpt") or "")
            line_start, line_end = _text_excerpt_lines(genre_path, genre_excerpt)
            sources.append(
                _source(
                    genre_path,
                    kind="genre_profile_reference",
                    role="reference",
                    fallback=False,
                    label=str(genre_source.get("resolved_from") or "reference"),
                    line_start=line_start,
                    line_end=line_end,
                )
            )

        if isinstance(runtime_status, dict):
            commit_sources: list[tuple[str, str, Path]] = []
            for key, kind in (("latest_commit", "commit"), ("latest_accepted_commit", "accepted_commit")):
                payload = runtime_status.get(key)
                meta = payload.get("meta") if isinstance(payload, dict) else {}
                try:
                    commit_chapter = int((meta or {}).get("chapter") or 0)
                except (TypeError, ValueError):
                    commit_chapter = 0
                if commit_chapter <= 0:
                    continue
                path = StoryContractPaths.from_project_root(root).commit_json(commit_chapter)
                commit_sources.append((key, kind, path))
            accepted_paths = {
                path.resolve()
                for _, kind, path in commit_sources
                if kind == "accepted_commit"
            }
            for _, kind, path in commit_sources:
                if kind == "commit" and path.resolve() in accepted_paths:
                    continue
                sources.append(
                    _source(
                        path,
                        kind=kind,
                        fallback=False,
                        label="accepted_chapter_fact" if kind == "accepted_commit" else "chapter_commit",
                        with_lines=True,
                    )
                )
        return _dedupe_sources(sources), fallback_reasons
    return [], fallback_reasons


def _emit_query(args: argparse.Namespace, query_type: str, data: Any) -> None:
    if not bool(getattr(args, "with_provenance", False)):
        _json_out(data)
        return
    sources, fallback_reasons = _provenance_sources(args, query_type, data)
    _json_out(
        {
            "schema_version": QUERY_SCHEMA_VERSION,
            "query_type": query_type,
            "status": "success",
            "data": data,
            "sources": sources,
            "legacy_fallback": bool(fallback_reasons),
            "fallback_reasons": fallback_reasons,
        }
    )


def cmd_load_context(args: argparse.Namespace) -> None:
    adapter = _adapter(args.project_root, read_only=args.read_only)
    pack = adapter.load_context(args.chapter)
    _emit_query(args, "comprehensive_context", pack.to_dict())


def cmd_query_entity(args: argparse.Namespace) -> None:
    adapter = _adapter(args.project_root, read_only=args.read_only)
    snap = adapter.query_entity(args.id)
    if snap is None:
        _emit_query(args, "entity", {"error": "not_found", "entity_id": args.id})
    else:
        _emit_query(args, "entity", snap.to_dict())


def cmd_query_rules(args: argparse.Namespace) -> None:
    adapter = _adapter(args.project_root, read_only=args.read_only)
    rules = adapter.query_rules(domain=args.domain or "")
    _emit_query(args, "world_rules", [r.to_dict() for r in rules])


def cmd_read_summary(args: argparse.Namespace) -> None:
    adapter = _adapter(args.project_root, read_only=args.read_only)
    text = adapter.read_summary(args.chapter)
    _emit_query(args, "chapter_summary", {"chapter": args.chapter, "summary": text})


def cmd_get_open_loops(args: argparse.Namespace) -> None:
    adapter = _adapter(args.project_root, read_only=args.read_only)
    loops = adapter.get_open_loops(status=args.status or "active")
    _emit_query(args, "open_loops", [l.to_dict() for l in loops])


def cmd_get_timeline(args: argparse.Namespace) -> None:
    adapter = _adapter(args.project_root, read_only=args.read_only)
    events = adapter.get_timeline(args.from_ch, args.to_ch)
    _emit_query(args, "timeline", [e.to_dict() for e in events])


def main() -> None:
    parser = argparse.ArgumentParser(description="MemoryContract CLI")
    parser.add_argument("--project-root", required=True, help="项目根目录")
    parser.add_argument("--read-only", action="store_true", help="禁用隐式建库、建表和 SQLite 同步")
    parser.add_argument("--with-provenance", action="store_true", help="返回来源路径、行号与 fallback 标记")
    parser.add_argument("--request-file", help="绝对路径的 webnovel-query-request/v1 JSON")
    sub = parser.add_subparsers(dest="command")

    p_load = sub.add_parser("load-context", help="加载章节上下文基础包")
    p_load.add_argument("--chapter", type=int)

    p_entity = sub.add_parser("query-entity", help="查询实体快照")
    p_entity.add_argument("--id", required=True, help="实体 ID")

    p_rules = sub.add_parser("query-rules", help="查询世界规则")
    p_rules.add_argument("--domain", default=None, help="按 domain 过滤")

    p_summary = sub.add_parser("read-summary", help="读取章节摘要")
    p_summary.add_argument("--chapter", type=int)

    p_loops = sub.add_parser("get-open-loops", help="查询未闭合伏笔")
    p_loops.add_argument("--status", default=None, help="状态过滤")

    p_timeline = sub.add_parser("get-timeline", help="查询时间线事件")
    p_timeline.add_argument("--from", type=int, required=True, dest="from_ch", help="起始章节")
    p_timeline.add_argument("--to", type=int, required=True, dest="to_ch", help="结束章节")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    request_types = {
        "load-context": "comprehensive_context",
        "query-rules": "world_rules",
        "read-summary": "chapter_summary",
        "get-open-loops": "open_loops",
    }
    if args.request_file:
        expected_type = request_types.get(args.command)
        if expected_type is None:
            _json_out(
                {
                    "schema_version": QUERY_SCHEMA_VERSION,
                    "query_type": "invalid",
                    "status": "error",
                    "error": {
                        "code": "INVALID_QUERY_REQUEST",
                        "message": "该子命令不支持 --request-file。",
                    },
                    "sources": [],
                }
            )
            raise SystemExit(2)
        conflict = (
            getattr(args, "chapter", None) is not None
            or getattr(args, "domain", None) is not None
            or getattr(args, "status", None) is not None
        )
        if conflict:
            _json_out(
                {
                    "schema_version": QUERY_SCHEMA_VERSION,
                    "query_type": expected_type,
                    "status": "error",
                    "error": {
                        "code": "INVALID_QUERY_REQUEST",
                        "message": "--request-file 不能与章节、domain 或 status 标量参数混用。",
                    },
                    "sources": [],
                }
            )
            raise SystemExit(2)
        try:
            request = load_query_request(
                args.request_file,
                project_root=args.project_root,
                expected_query_types={expected_type},
            )
        except (OSError, QueryRequestError) as exc:
            _json_out(
                {
                    "schema_version": QUERY_SCHEMA_VERSION,
                    "query_type": expected_type,
                    "status": "error",
                    "error": {"code": "INVALID_QUERY_REQUEST", "message": str(exc)},
                    "sources": [],
                }
            )
            raise SystemExit(2) from None
        if "chapter" in request:
            args.chapter = request["chapter"]
        if "domain" in request:
            args.domain = request["domain"]
        if "status" in request:
            args.status = request["status"]

    if args.command in {"load-context", "read-summary"} and (
        not isinstance(args.chapter, int) or args.chapter <= 0
    ):
        parser.error("--chapter must be a positive integer or supplied by --request-file")

    dispatch = {
        "load-context": cmd_load_context,
        "query-entity": cmd_query_entity,
        "query-rules": cmd_query_rules,
        "read-summary": cmd_read_summary,
        "get-open-loops": cmd_get_open_loops,
        "get-timeline": cmd_get_timeline,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    if sys.platform == "win32":
        enable_windows_utf8_stdio()
    main()
