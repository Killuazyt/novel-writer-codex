#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skills" / "webnovel-review" / "SKILL.md"
OPENAI_YAML = ROOT / "skills" / "webnovel-review" / "agents" / "openai.yaml"
SHARED_SCHEMA = ROOT / "references" / "review-schema.md"
EVALS = ROOT / "skills" / "webnovel-review" / "evals" / "evals.json"
GUIDELINES = ROOT / "references" / "review" / "blocking-override-guidelines.md"


def test_review_skill_is_codex_native_and_has_no_scaffold_or_global_tmp() -> None:
    text = SKILL.read_text(encoding="utf-8")
    assert "TODO" not in text
    assert "allowed-tools:" not in text
    assert "argument-hint:" not in text
    assert ".webnovel/tmp/review_results.json" not in text
    assert ".webnovel/tmp/review_metrics.json" not in text
    assert "review prepare" in text
    assert "review accept" in text
    assert "review resume" in text
    assert "review range-prepare" in text
    assert "review range-resume" in text
    assert "native child Agent" in text
    assert "Do not create a Codex top-level task" in text


def test_review_skill_requires_live_runtime_evidence_and_finite_decisions() -> None:
    text = SKILL.read_text(encoding="utf-8")
    for token in (
        "gpt-5.6-luna",
        "high",
        "rollout_path",
        "sessions_root",
        "parent_thread_id",
        "CODEX_THREAD_ID",
        "binding_marker",
        "webnovel-review-accept-request/v2",
        "webnovel-review-decision-request/v1",
        "WEBNOVEL_REVIEW_DECISION/v1",
        "--request-file",
        "trusted parent rollout",
        "first durable user answer",
        "pending",
        "targeted_fix",
        "report_only",
        "abandon",
        "stop",
        "continue",
        "at most five",
    ):
        assert token in text
    assert "Agent self-report" in text
    assert '"responses"' not in text
    assert "caller-supplied response JSON" in text
    assert "host-owned Codex sessions root" in text
    assert "do not claim the review passed" in text
    assert "Do not include `choice`" in text
    assert "--request-id" not in text
    assert "--choice" not in text


def test_review_skill_frontmatter_and_openai_metadata_are_valid() -> None:
    text = SKILL.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    frontmatter = yaml.safe_load(text.split("---", 2)[1])
    assert set(frontmatter) == {"name", "description"}
    assert frontmatter["name"] == "webnovel-review"
    metadata = yaml.safe_load(OPENAI_YAML.read_text(encoding="utf-8"))
    assert metadata["interface"]["display_name"] == "Webnovel Review"
    assert "$webnovel-review" in metadata["interface"]["default_prompt"]
    assert "trusted parent-rollout receipt" in metadata["interface"]["default_prompt"]


def test_review_skill_files_are_utf8_without_bom() -> None:
    for path in (SKILL, OPENAI_YAML, SHARED_SCHEMA, EVALS, GUIDELINES):
        raw = path.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf")
        raw.decode("utf-8")


def test_shared_review_schema_matches_the_strict_runtime_contract() -> None:
    text = SHARED_SCHEMA.read_text(encoding="utf-8")
    for token in (
        "顶层字段必须且只能",
        "Issue 的七字段合同",
        "setting",
        "timeline",
        "continuity",
        "character",
        "logic",
        'severity="critical"',
        "blocking=true",
        "skipped: fast mode",
        "issues_count",
        "blocking_count",
        "has_blocking",
        "禁止额外字段",
        "webnovel-review-artifact/v1",
        "webnovel-review-decision-request/v1",
        "binding marker",
        "可信父任务 rollout",
        "chapter_sha256",
        "context_sha256",
        "reviewer_output_sha256",
        "actual model",
        "readback",
        "reviewer_rerun=false",
        "WAL",
        "checksum",
    ):
        assert token in text
    for legacy in (
        "ai_flavor",
        "pacing",
        "other",
        "evidence | string | ❌",
        "fix_hint | string | ❌",
        "blocking | bool | ❌",
        "critical 默认",
        "其余 severity 由审查 agent",
    ):
        assert legacy not in text


def test_review_evals_and_override_reference_require_trusted_decision_receipts() -> None:
    evals_text = EVALS.read_text(encoding="utf-8")
    guidelines = GUIDELINES.read_text(encoding="utf-8")
    for token in (
        "exact assistant marker",
        "trusted parent rollout receipt",
        "rejects caller-supplied choice fields",
    ):
        assert token in evals_text
    for token in ("可信 rollout", "精确绑定 marker", "CLI 参数", "跨 run/range"):
        assert token in guidelines
