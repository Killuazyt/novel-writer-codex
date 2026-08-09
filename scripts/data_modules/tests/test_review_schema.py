#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""审查 schema 测试"""
import copy
import json

import pytest
from data_modules.review_schema import (
    MAX_ISSUES,
    MAX_TEXT,
    ReviewIssue,
    ReviewResult,
    ReviewSchemaError,
    append_ai_flavor_anti_patterns,
    parse_review_output,
)


def _strict_payload(chapter=1):
    return {
        "chapter": chapter,
        "issues": [],
        "issues_count": 0,
        "blocking_count": 0,
        "has_blocking": False,
        "dimension_results": [
            {"dimension": name, "conclusion": "pass"}
            for name in ("setting", "timeline", "continuity", "character", "logic")
        ],
        "summary": "无问题",
    }


def _strict_issue(**updates):
    issue = {
        "severity": "high",
        "category": "setting",
        "location": "第1段",
        "description": "设定冲突",
        "evidence": "正文与上下文不一致",
        "fix_hint": "核对既有设定",
        "blocking": False,
    }
    issue.update(updates)
    return issue


def test_review_issue_blocking_defaults():
    """critical severity 默认 blocking=True"""
    issue = ReviewIssue(
        severity="critical",
        category="continuity",
        location="第3段",
        description="主角使用了已失去的能力",
    )
    assert issue.blocking is True


def test_review_issue_non_critical_not_blocking():
    """非 critical 默认 blocking=False"""
    issue = ReviewIssue(
        severity="high",
        category="setting",
        location="第7段",
        description="时间线矛盾",
    )
    assert issue.blocking is False


def test_review_result_counts():
    """blocking_count 自动计算"""
    result = ReviewResult(
        chapter=10,
        issues=[
            ReviewIssue(severity="critical", category="continuity", location="p1", description="d1"),
            ReviewIssue(severity="high", category="setting", location="p2", description="d2"),
            ReviewIssue(severity="high", category="timeline", location="p3", description="d3", blocking=True),
        ],
        summary="测试",
    )
    assert result.blocking_count == 2
    assert result.issues_count == 3
    assert result.has_blocking is True


def test_review_result_no_issues():
    result = ReviewResult(chapter=10, issues=[], summary="无问题")
    assert result.blocking_count == 0
    assert result.has_blocking is False


def test_review_result_to_dict_roundtrip():
    result = ReviewResult(
        chapter=10,
        issues=[
            ReviewIssue(severity="medium", category="ai_flavor", location="p5", description="AI味重",
                        evidence="'稳住心神'出现3次", fix_hint="替换为具体动作描写"),
        ],
        summary="1个AI味问题",
    )
    d = result.to_dict()
    assert d["chapter"] == 10
    assert d["blocking_count"] == 0
    assert len(d["issues"]) == 1
    assert d["issues"][0]["category"] == "ai_flavor"
    assert d["issues"][0]["fix_hint"] == "替换为具体动作描写"


def test_parse_review_output_from_dict():
    raw = {
        "issues": [
            {"severity": "critical", "category": "continuity", "location": "p1",
             "description": "矛盾", "evidence": "证据", "fix_hint": "修复"},
        ],
        "summary": "1个严重问题",
    }
    result = parse_review_output(chapter=5, raw=raw)
    assert result.chapter == 5
    assert result.blocking_count == 1


def test_parse_review_output_tolerates_missing_fields():
    raw = {
        "issues": [
            {"severity": "low", "description": "小问题"},
        ],
        "summary": "轻微",
    }
    result = parse_review_output(chapter=1, raw=raw)
    assert result.issues[0].category == "other"
    assert result.issues[0].location == ""


def test_review_result_to_metrics_dict():
    result = ReviewResult(
        chapter=10,
        issues=[
            ReviewIssue(severity="critical", category="continuity", location="p1", description="d1"),
            ReviewIssue(severity="high", category="ai_flavor", location="p2", description="d2"),
        ],
        summary="测试",
    )
    metrics = result.to_metrics_dict()
    assert metrics["chapter"] == 10
    assert metrics["start_chapter"] == 10
    assert metrics["end_chapter"] == 10
    assert metrics["issues_count"] == 2
    assert metrics["blocking_count"] == 1
    assert "continuity" in metrics["categories"]
    assert "ai_flavor" in metrics["categories"]
    assert metrics["severity_counts"]["critical"] == 1
    assert metrics["severity_counts"]["high"] == 1
    assert metrics["critical_issues"] == ["d1"]
    assert metrics["report_file"] == ""
    assert metrics["overall_score"] < 100
    assert metrics["dimension_scores"]["continuity"] < 100
    assert metrics["dimension_scores"]["ai_flavor"] < 100


def test_ai_flavor_review_issue_added_to_anti_patterns(tmp_path):
    result = ReviewResult(
        chapter=2,
        issues=[
            ReviewIssue(
                severity="medium",
                category="ai_flavor",
                evidence="唯一一个知道复利公式的人。唯一一个知道账本秘密的人。",
            ),
            ReviewIssue(severity="low", category="ai_flavor", evidence="低风险句式"),
            ReviewIssue(severity="high", category="logic", evidence="逻辑问题"),
        ],
    )

    added = append_ai_flavor_anti_patterns(tmp_path, result)

    patterns = json.loads((tmp_path / ".story-system" / "anti_patterns.json").read_text(encoding="utf-8"))
    assert added == 1
    assert any("唯一一个知道" in item["text"] for item in patterns)
    assert patterns[0]["source_id"].startswith("ch0002_issue_")


def test_ai_flavor_review_feedback_dedupes_evidence(tmp_path):
    existing = tmp_path / ".story-system" / "anti_patterns.json"
    existing.parent.mkdir(parents=True)
    existing.write_text(
        json.dumps([{"text": "第一片 / 第二片 / 第三片", "source_table": "review_extracted"}], ensure_ascii=False),
        encoding="utf-8",
    )
    result = ReviewResult(
        chapter=3,
        issues=[
            ReviewIssue(
                severity="high",
                category="ai_flavor",
                evidence="第一片 / 第二片 / 第三片",
            )
        ],
    )

    added = append_ai_flavor_anti_patterns(tmp_path, result)

    patterns = json.loads(existing.read_text(encoding="utf-8"))
    assert added == 0
    assert len(patterns) == 1


def test_legacy_normalization_and_metrics_provenance_edges():
    result = parse_review_output(
        8,
        {
            "issues": [
                "ignored",
                {"severity": "unknown", "category": "unknown", "description": "fallback"},
            ],
            "dimension_results": ["ignored", {"dimension": "setting", "conclusion": "pass"}],
        },
    )
    assert result.issues[0].severity == "medium"
    assert result.issues[0].category == "other"
    assert result.severity_counts["medium"] == 1
    payload = result.to_metrics_dict(
        provenance={"run_id": "rv-test", "review_sha256": "", "chapter_sha256": "abc"}
    )
    assert payload["provenance"]["run_id"] == "rv-test"
    assert "run_id=rv-test" in payload["notes"]
    assert "chapter_sha256=abc" in payload["notes"]


def test_strict_review_contract_accepts_complete_issue():
    payload = _strict_payload()
    payload["issues"] = [_strict_issue(severity="critical", blocking=True)]
    payload["issues_count"] = 1
    payload["blocking_count"] = 1
    payload["has_blocking"] = True
    payload["dimension_results"][0]["conclusion"] = "发现设定冲突"
    result = parse_review_output(1, payload, strict=True, review_mode="full")
    assert result.blocking_count == 1
    assert result.dimension_results[0].dimension == "setting"


def test_strict_review_contract_rejects_shape_type_count_and_semantic_errors():
    def changed(mutator):
        payload = copy.deepcopy(_strict_payload())
        mutator(payload)
        return payload

    cases = [
        (changed(lambda value: value.update(chapter=2)), "chapter does not match"),
        (changed(lambda value: value.update(issues={})), "issues must be a list"),
        (changed(lambda value: value.update(issues=[{}] * (MAX_ISSUES + 1))), "at most"),
        (changed(lambda value: value.update(issues=[{"severity": "high"}])), "invalid field set"),
        (changed(lambda value: value.update(issues=[_strict_issue(severity="urgent")])), "severity is invalid"),
        (changed(lambda value: value.update(issues=[_strict_issue(category="pacing")])), "category is invalid"),
        (changed(lambda value: value.update(issues=[_strict_issue(blocking=1)])), "must be boolean"),
        (
            changed(lambda value: value.update(issues=[_strict_issue(severity="critical", blocking=False)])),
            "critical issues must block",
        ),
        (changed(lambda value: value.update(dimension_results=[])), "exactly five"),
        (
            changed(lambda value: value["dimension_results"][0].update(extra=True)),
            "invalid field set",
        ),
        (
            changed(lambda value: value["dimension_results"][0].update(dimension="logic")),
            "out of order",
        ),
        (
            changed(lambda value: value["dimension_results"][0].update(conclusion="skipped")),
            "cannot be skipped",
        ),
        (
            changed(lambda value: value["dimension_results"][0].update(conclusion="unchecked")),
            "must conclude pass",
        ),
        (changed(lambda value: value.update(issues_count=True)), "issues_count does not match"),
        (changed(lambda value: value.update(blocking_count=True)), "blocking_count does not match"),
        (changed(lambda value: value.update(has_blocking=1)), "has_blocking does not match"),
        (changed(lambda value: value.update(summary="")), "must not be empty"),
        (changed(lambda value: value.update(summary=1)), "must be a string"),
        (changed(lambda value: value.update(summary="bad\x00")), "contains NUL"),
        (changed(lambda value: value.update(summary="x" * (MAX_TEXT["summary"] + 1))), "too long"),
    ]
    for payload, message in cases:
        with pytest.raises(ReviewSchemaError, match=message):
            parse_review_output(1, payload, strict=True, review_mode="full")

    with pytest.raises(ReviewSchemaError, match="JSON object"):
        parse_review_output(1, [], strict=True)  # type: ignore[arg-type]
    with pytest.raises(ReviewSchemaError, match="positive integer"):
        parse_review_output(True, _strict_payload(), strict=True)
    with pytest.raises(ReviewSchemaError, match="full or fast"):
        parse_review_output(1, _strict_payload(), strict=True, review_mode="brief")
    unserializable = _strict_payload()
    unserializable["summary"] = {1}
    with pytest.raises(ReviewSchemaError, match="JSON serializable"):
        parse_review_output(1, unserializable, strict=True)


def test_strict_review_contract_rejects_issue_text_and_mode_inconsistency():
    for field, value, message in (
        ("location", 1, "must be a string"),
        ("description", "", "must not be empty"),
        ("evidence", "bad\x00", "contains NUL"),
        ("fix_hint", "x" * (MAX_TEXT["fix_hint"] + 1), "too long"),
    ):
        payload = _strict_payload()
        payload["issues"] = [_strict_issue(**{field: value})]
        payload["issues_count"] = 1
        payload["dimension_results"][0]["conclusion"] = "发现问题"
        with pytest.raises(ReviewSchemaError, match=message):
            parse_review_output(1, payload, strict=True)

    fast = _strict_payload()
    fast["issues"] = [_strict_issue(category="character")]
    fast["issues_count"] = 1
    fast["dimension_results"][3]["conclusion"] = "发现人物问题"
    fast["dimension_results"][4]["conclusion"] = "skipped: fast mode"
    with pytest.raises(ReviewSchemaError, match="skipped dimensions"):
        parse_review_output(1, fast, strict=True, review_mode="fast")

    issue_but_pass = _strict_payload()
    issue_but_pass["issues"] = [_strict_issue()]
    issue_but_pass["issues_count"] = 1
    with pytest.raises(ReviewSchemaError, match="cannot pass"):
        parse_review_output(1, issue_but_pass, strict=True)


def test_bad_legacy_anti_pattern_file_is_reported_or_reset(tmp_path):
    path = tmp_path / ".story-system" / "anti_patterns.json"
    path.parent.mkdir(parents=True)
    path.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match="Bad JSON"):
        append_ai_flavor_anti_patterns(tmp_path, ReviewResult(chapter=1))
    path.write_text("{}", encoding="utf-8")
    assert append_ai_flavor_anti_patterns(tmp_path, ReviewResult(chapter=1)) == 0
