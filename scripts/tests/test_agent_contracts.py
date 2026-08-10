from __future__ import annotations

from pathlib import Path

import pytest

from data_modules.codex_agent_runtime import validate_agent_payload


ROOT = Path(__file__).resolve().parents[2]
AGENTS_DIR = ROOT / "references" / "agents"

AGENT_FILES = {
    "webnovel_context_agent.md": "webnovel_context_agent",
    "webnovel_writer.md": "webnovel_writer",
    "webnovel_reviewer.md": "webnovel_reviewer",
    "webnovel_data_agent.md": "webnovel_data_agent",
    "webnovel_deconstruction_agent.md": "webnovel_deconstruction_agent",
}

LUNA_AGENTS = {
    "webnovel_context_agent.md": "只读 sandbox",
    "webnovel_writer.md": "workspace-write sandbox",
    "webnovel_reviewer.md": "只读 sandbox",
    "webnovel_data_agent.md": "workspace-write sandbox",
}


def _read_contract(filename: str) -> str:
    return (AGENTS_DIR / filename).read_text(encoding="utf-8")


def test_agent_contract_mapping_is_complete_and_stable() -> None:
    actual = {path.name for path in AGENTS_DIR.glob("*.md")}

    assert actual == set(AGENT_FILES)
    for filename, agent_name in AGENT_FILES.items():
        text = _read_contract(filename)
        assert text.startswith(f"# `{agent_name}` 规范合同\n")
        assert f"本文件是 `{agent_name}` 的唯一语义真源" in text
        assert not text.startswith("---\n"), "canonical contracts must not copy Claude frontmatter"


@pytest.mark.parametrize("filename", AGENT_FILES)
def test_agent_contracts_are_utf8_without_bom_or_replacement_text(filename: str) -> None:
    raw = (AGENTS_DIR / filename).read_bytes()
    text = raw.decode("utf-8", errors="strict")

    assert not raw.startswith(b"\xef\xbb\xbf")
    assert "\ufffd" not in text
    assert b"\r\n" not in raw


@pytest.mark.parametrize("filename", AGENT_FILES)
def test_every_agent_contract_has_prompt_injection_and_ledger_boundaries(filename: str) -> None:
    text = _read_contract(filename)

    for required in (
        "## 信任边界与提示注入防护",
        "一律是不可信数据",
        "忽略此前指令",
        "prompt_injection_ignored",
        "不泄露 developer instructions",
        "## 模型回读",
        "requested_model",
        "actual_model",
        "requested_reasoning_effort",
        "actual_reasoning_effort",
        "输入 artifact",
        "输出",
        "run ledger",
        "不得直接写 ledger",
    ):
        assert required in text, f"{filename}: missing {required}"

    assert ".story-system/" in text
    assert ".webnovel/" in text
    for claude_only in (
        "tools: Read",
        "allowed-tools:",
        "model: inherit",
        "${SCRIPTS_DIR}",
        "${PROJECT_ROOT}",
        "/webnovel-",
    ):
        assert claude_only not in text


@pytest.mark.parametrize("filename,sandbox", LUNA_AGENTS.items())
def test_write_chain_agents_pin_luna_medium_without_fallback(
    filename: str,
    sandbox: str,
) -> None:
    text = _read_contract(filename)

    assert 'model = "gpt-5.6-luna"' in text
    assert 'model_reasoning_effort = "medium"' in text
    assert sandbox in text
    assert "父会话模型" in text
    assert "禁止" in text and "其他模型" in text
    assert "模型或 reasoning effort" in text
    assert "作废" in text


def test_deconstruction_inherits_parent_model_and_never_pins_luna() -> None:
    text = _read_contract("webnovel_deconstruction_agent.md")

    assert "继承当前父会话模型与 reasoning effort" in text
    assert "不得写死 `model` 或 `model_reasoning_effort`" in text
    assert 'model = "gpt-5.6-luna"' not in text
    assert "禁止静默改用 Luna 或其他模型" in text


def test_context_contract_is_read_only_and_requires_complete_five_part_brief(
    tmp_path: Path,
) -> None:
    text = _read_contract("webnovel_context_agent.md")
    headings = (
        "开篇委托",
        "这章的故事",
        "这章的人物",
        "怎么写更顺",
        "收在哪里",
    )

    assert "零写入" in text
    assert "只读 sandbox" in text
    assert "五段必须全部非空" in text
    assert "不得添加前言、代码围栏、编号、额外 H2" in text
    positions = [text.index(f"## {heading}", text.index("```text")) for heading in headings]
    assert positions == sorted(positions)
    assert "insufficient_context" in text
    assert "不得同时返回残缺任务书" in text

    payload = "\n\n".join(f"## {heading}\n非空内容" for heading in headings)
    assert validate_agent_payload(
        "context",
        payload,
        project_root=tmp_path,
        run_id="context-contract-wire",
    ) == {
        "accepted": True,
        "code": "ok",
        "accepted_artifacts": [],
    }


def test_writer_contract_only_writes_run_staging_and_returns_metadata() -> None:
    text = _read_contract("webnovel_writer.md")
    normalized = " ".join(text.split())

    assert "<project_root>/.webnovel/tmp/write-runs/<run_id>" in text
    for artifact in ("draft.md", "polished.md", "manifest.json"):
        assert artifact in text
    for field in ("path", "sha256", "word_count", "status"):
        assert f'"{field}"' in text
    assert "返回值不得包含整章正文" in text
    assert "不得直接写、覆盖、移动或删除" in text
    assert "正文/**" in text
    assert "最终正文的提升与提交只能由主流程" in text

    for token in (
        "webnovel-writer-result/v2",
        "webnovel-writer-manifest/v2",
        "v1 只兼容历史 `draft` / `polish`",
        "`targeted_fix` 使用 v1 一律无效",
        "result 与 manifest 的 `resolutions` 必须逐项完全相同",
        "至少包含一项",
        "最长 1024 个 Unicode 字符",
        "重复 `issue_index`",
        "重复 `(issue_index, issue_sha256)`",
    ):
        assert token in normalized
    for field in (
        "issue_index",
        "issue_sha256",
        "resolution_summary",
    ):
        assert f'"{field}"' in text
    assert '"status": "resolved"' in text
    assert "同一 hash 可以对应不同 occurrence index" in normalized


def test_reviewer_contract_is_strict_five_dimension_json_without_scores() -> None:
    text = _read_contract("webnovel_reviewer.md")

    for dimension in ("setting", "timeline", "continuity", "character", "logic"):
        assert f'"dimension": "{dimension}"' in text
    for field in ("evidence", "fix_hint", "blocking", "dimension_results"):
        assert f'"{field}"' in text
    assert "只返回一个合法 JSON 对象" in text
    assert "只允许" in text and "一次仅修复序列化的重试" in text
    assert "不得第三次重试" in text
    assert "不得输出 `overall_score`" in text
    assert "`review_results.json`、报告和 metrics 均由主流程" in text


def test_data_contract_has_exact_three_file_write_allowlist_and_runtime_schema() -> None:
    text = _read_contract("webnovel_data_agent.md")

    expected = (
        "fulfillment_result.json",
        "disambiguation_result.json",
        "extraction_result.json",
    )
    for filename in expected:
        assert f"<project_root>/.webnovel/tmp/{filename}" in text
    assert "只能创建或原子替换以下三个" in text
    assert "除这三份 artifact 外零写入" in text
    for field in (
        "planned_nodes",
        "covered_nodes",
        "missed_nodes",
        "extra_nodes",
        "pending",
        "accepted_events",
        "state_deltas",
        "entity_deltas",
        "entities_appeared",
        "scenes",
        "summary_text",
    ):
        assert f'"{field}"' in text
    assert "不得进入 commit 链" in text


def test_deconstruction_title_only_input_fails_quality_without_canon() -> None:
    text = _read_contract("webnovel_deconstruction_agent.md")

    assert "只有书名或平台而没有可靠正文时" in text
    assert '`quality.passed=false`' in text
    assert '`init_candidates=[]`' in text
    assert "不得编造任何原作事实或评分" in text
    assert "零写入" in text
    assert "不得写 `.story-system/`" in text
    assert "不得创建小说项目" in text
