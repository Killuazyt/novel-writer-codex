#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""memory_cli.py 测试。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_scripts_dir = str(Path(__file__).resolve().parent.parent.parent)
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)


def _ensure_scripts_on_path():
    scripts_dir = Path(__file__).resolve().parent.parent.parent
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))


def _make_project(tmp_path: Path):
    webnovel_dir = tmp_path / ".webnovel"
    webnovel_dir.mkdir(parents=True, exist_ok=True)
    (webnovel_dir / "state.json").write_text("{}", encoding="utf-8")
    (webnovel_dir / "summaries").mkdir(exist_ok=True)
    return tmp_path


def test_load_context_cli(tmp_path, capsys):
    _ensure_scripts_on_path()
    import memory_cli

    project = _make_project(tmp_path)
    old_argv = sys.argv
    sys.argv = ["memory_cli", "--project-root", str(project), "load-context", "--chapter", "1"]
    try:
        memory_cli.main()
    finally:
        sys.argv = old_argv

    output = json.loads(capsys.readouterr().out)
    assert output["chapter"] == 1
    assert "sections" in output


def test_query_entity_not_found(tmp_path, capsys):
    _ensure_scripts_on_path()
    import memory_cli

    project = _make_project(tmp_path)
    old_argv = sys.argv
    sys.argv = ["memory_cli", "--project-root", str(project), "query-entity", "--id", "nobody"]
    try:
        memory_cli.main()
    finally:
        sys.argv = old_argv

    output = json.loads(capsys.readouterr().out)
    assert output["error"] == "not_found"


def test_query_entity_found(tmp_path, capsys):
    _ensure_scripts_on_path()
    import memory_cli

    project = _make_project(tmp_path)
    state = {
        "entities_v3": {
            "角色": {
                "xiaoyan": {"name": "萧炎", "tier": "核心", "aliases": [], "first_appearance": 1, "last_appearance": 10}
            }
        }
    }
    (project / ".webnovel" / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    old_argv = sys.argv
    sys.argv = ["memory_cli", "--project-root", str(project), "query-entity", "--id", "xiaoyan"]
    try:
        memory_cli.main()
    finally:
        sys.argv = old_argv

    output = json.loads(capsys.readouterr().out)
    assert output["name"] == "萧炎"


def test_query_rules_empty(tmp_path, capsys):
    _ensure_scripts_on_path()
    import memory_cli

    project = _make_project(tmp_path)
    old_argv = sys.argv
    sys.argv = ["memory_cli", "--project-root", str(project), "query-rules"]
    try:
        memory_cli.main()
    finally:
        sys.argv = old_argv

    output = json.loads(capsys.readouterr().out)
    assert output == []


def test_read_summary_missing(tmp_path, capsys):
    _ensure_scripts_on_path()
    import memory_cli

    project = _make_project(tmp_path)
    old_argv = sys.argv
    sys.argv = ["memory_cli", "--project-root", str(project), "read-summary", "--chapter", "99"]
    try:
        memory_cli.main()
    finally:
        sys.argv = old_argv

    output = json.loads(capsys.readouterr().out)
    assert output["chapter"] == 99
    assert output["summary"] == ""


def test_read_summary_exists(tmp_path, capsys):
    _ensure_scripts_on_path()
    import memory_cli

    project = _make_project(tmp_path)
    (project / ".webnovel" / "summaries" / "ch0005.md").write_text("第5章摘要", encoding="utf-8")

    old_argv = sys.argv
    sys.argv = ["memory_cli", "--project-root", str(project), "read-summary", "--chapter", "5"]
    try:
        memory_cli.main()
    finally:
        sys.argv = old_argv

    output = json.loads(capsys.readouterr().out)
    assert "第5章摘要" in output["summary"]


def test_get_open_loops_empty(tmp_path, capsys):
    _ensure_scripts_on_path()
    import memory_cli

    project = _make_project(tmp_path)
    old_argv = sys.argv
    sys.argv = ["memory_cli", "--project-root", str(project), "get-open-loops"]
    try:
        memory_cli.main()
    finally:
        sys.argv = old_argv

    output = json.loads(capsys.readouterr().out)
    assert output == []


def test_get_timeline_empty(tmp_path, capsys):
    _ensure_scripts_on_path()
    import memory_cli

    project = _make_project(tmp_path)
    old_argv = sys.argv
    sys.argv = ["memory_cli", "--project-root", str(project), "get-timeline", "--from", "1", "--to", "100"]
    try:
        memory_cli.main()
    finally:
        sys.argv = old_argv

    output = json.loads(capsys.readouterr().out)
    assert output == []


def test_read_only_context_with_provenance_does_not_create_read_models(tmp_path, capsys):
    _ensure_scripts_on_path()
    import memory_cli

    project = _make_project(tmp_path / "中文 空格 (甲) & 乙 #1")
    before = sorted(str(path.relative_to(project)) for path in project.rglob("*") if path.is_file())
    old_argv = sys.argv
    sys.argv = [
        "memory_cli",
        "--project-root",
        str(project),
        "--read-only",
        "--with-provenance",
        "load-context",
        "--chapter",
        "7",
    ]
    try:
        memory_cli.main()
    finally:
        sys.argv = old_argv

    output = json.loads(capsys.readouterr().out)
    after = sorted(str(path.relative_to(project)) for path in project.rglob("*") if path.is_file())
    assert before == after
    assert not (project / ".webnovel" / "index.db").exists()
    assert output["schema_version"] == "webnovel-query-result/v1"
    assert output["query_type"] == "comprehensive_context"
    assert output["legacy_fallback"] is True
    assert "missing_accepted_commit" in output["fallback_reasons"]
    warnings = output["data"]["sections"]["memory_pack"]["warnings"]
    warning_keys = {(item.get("source"), item.get("query"), item.get("detail")) for item in warnings}
    assert len(warnings) == len(warning_keys)


def test_query_rules_with_provenance_uses_domain_not_chapter(tmp_path, capsys):
    _ensure_scripts_on_path()
    import memory_cli
    from data_modules.config import DataModulesConfig
    from data_modules.memory.schema import MemoryItem
    from data_modules.memory.store import ScratchpadManager

    project = _make_project(tmp_path)
    store = ScratchpadManager(DataModulesConfig.from_project_root(project))
    store.upsert_item(
        MemoryItem(
            id="rule-power",
            layer="semantic",
            category="world_rule",
            subject="力量体系",
            field="上限",
            value="九境",
            source_chapter=1,
        )
    )
    old_argv = sys.argv
    sys.argv = [
        "memory_cli",
        "--project-root",
        str(project),
        "--read-only",
        "--with-provenance",
        "query-rules",
        "--domain",
        "力量体系",
    ]
    try:
        memory_cli.main()
    finally:
        sys.argv = old_argv

    output = json.loads(capsys.readouterr().out)
    assert [item["value"] for item in output["data"]] == ["九境"]
    assert output["sources"][0]["kind"] == "derived_memory"
    assert output["sources"][0]["line_start"] is None
    assert output["legacy_fallback"] is True


def test_read_summary_provenance_reports_real_line_range(tmp_path, capsys):
    _ensure_scripts_on_path()
    import memory_cli

    project = _make_project(tmp_path)
    summary = project / ".webnovel" / "summaries" / "ch0005.md"
    summary.write_text("第一行\n第二行\n", encoding="utf-8")
    old_argv = sys.argv
    sys.argv = [
        "memory_cli",
        "--project-root",
        str(project),
        "--read-only",
        "--with-provenance",
        "read-summary",
        "--chapter",
        "5",
    ]
    try:
        memory_cli.main()
    finally:
        sys.argv = old_argv

    output = json.loads(capsys.readouterr().out)
    source = output["sources"][0]
    assert source["path"].endswith("ch0005.md")
    assert source["line_start"] == 1
    assert source["line_end"] == 2


def test_comprehensive_context_provenance_lists_every_actual_source(tmp_path, capsys):
    _ensure_scripts_on_path()
    import memory_cli
    from data_modules.codex_agent_runtime import snapshot_protected_state
    from data_modules.config import DataModulesConfig
    from data_modules.index_manager import IndexManager
    from data_modules.memory.schema import MemoryItem
    from data_modules.memory.store import ScratchpadManager

    project = _make_project(tmp_path / "完整来源 中文 (A&B)")
    state = {
        "project_info": {"title": "来源测试", "genre": "玄幻"},
        "progress": {
            "current_chapter": 6,
            "volumes_planned": [{"volume": 1, "chapters_range": "1-20"}],
        },
        "protagonist_state": {"name": "韩立"},
    }
    (project / ".webnovel" / "state.json").write_text(
        json.dumps(state, ensure_ascii=False), encoding="utf-8"
    )
    story = project / ".story-system"
    for relative in ("volumes", "chapters", "reviews", "commits"):
        (story / relative).mkdir(parents=True, exist_ok=True)
    contract_files = {
        story / "MASTER_SETTING.json": {"route": {"primary_genre": "玄幻"}},
        story / "volumes" / "volume_001.json": {"meta": {"volume": 1}},
        story / "chapters" / "chapter_007.json": {"meta": {"chapter": 7}},
        story / "reviews" / "chapter_007.review.json": {"meta": {"chapter": 7}},
        story / "commits" / "chapter_006.commit.json": {
            "meta": {"chapter": 6, "status": "accepted"}
        },
    }
    for path, payload in contract_files.items():
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    outline = project / "大纲" / "第1卷-详细大纲.md"
    outline.parent.mkdir(parents=True)
    outline.write_text("# 第一卷\n\n### 第7章：试炼\n承接前章并进入试炼。\n", encoding="utf-8")
    for chapter in (4, 5, 6):
        (project / ".webnovel" / "summaries" / f"ch{chapter:04d}.md").write_text(
            f"第{chapter}章摘要\n第二行\n", encoding="utf-8"
        )
    (project / ".webnovel" / "project_memory.json").write_text(
        json.dumps(
            {
                "patterns": [
                    {"pattern_type": "style", "description": "短句推进", "importance": "high"}
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    settings = project / "设定集"
    settings.mkdir()
    (settings / "风格契约.md").write_text("# 风格契约\n短句推进。\n", encoding="utf-8")
    project_refs = project / ".codex" / "references"
    project_refs.mkdir(parents=True)
    (project_refs / "genre-profiles.md").write_text(
        "# 题材\n\n## 玄幻\n升级节奏清晰。\n\n## 都市\n现实冲突。\n", encoding="utf-8"
    )

    cfg = DataModulesConfig.from_project_root(project)
    IndexManager(cfg)
    ScratchpadManager(cfg).upsert_item(
        MemoryItem(
            id="rule-source",
            layer="semantic",
            category="world_rule",
            subject="力量体系",
            field="上限",
            value="九境",
            source_chapter=1,
        )
    )
    before = snapshot_protected_state(project)
    old_argv = sys.argv
    sys.argv = [
        "memory_cli",
        "--project-root",
        str(project),
        "--read-only",
        "--with-provenance",
        "load-context",
        "--chapter",
        "7",
    ]
    try:
        memory_cli.main()
    finally:
        sys.argv = old_argv

    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "success"
    assert output["legacy_fallback"] is False
    assert output["fallback_reasons"] == []
    sources = output["sources"]
    paths = {Path(item["path"]).name: item for item in sources}
    for required in (
        "MASTER_SETTING.json",
        "volume_001.json",
        "chapter_007.json",
        "chapter_007.review.json",
        "chapter_006.commit.json",
        "state.json",
        "index.db",
        "memory_scratchpad.json",
        "ch0004.md",
        "ch0005.md",
        "ch0006.md",
        "第1卷-详细大纲.md",
        "project_memory.json",
        "风格契约.md",
        "genre-profiles.md",
    ):
        assert required in paths
    assert paths["index.db"]["line_start"] is None
    assert paths["第1卷-详细大纲.md"]["line_start"] == 3
    assert paths["风格契约.md"]["line_start"] == 1
    assert paths["genre-profiles.md"]["line_start"] == 3
    assert paths["chapter_006.commit.json"]["role"] == "authoritative"
    assert snapshot_protected_state(project) == before
