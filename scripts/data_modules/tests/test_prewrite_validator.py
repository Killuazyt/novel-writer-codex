#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json

import pytest

from data_modules import prewrite_validator as prewrite_validator_module
from data_modules.prewrite_validator import PrewriteValidator


def test_prewrite_validator_builds_disambiguation_domain_and_fulfillment_seed(tmp_path):
    project_root = tmp_path
    (project_root / ".webnovel").mkdir(parents=True, exist_ok=True)
    (project_root / ".webnovel" / "state.json").write_text(
        json.dumps(
            {
                "disambiguation_pending": [],
                "disambiguation_warnings": [{"mention": "宗主"}],
                "chapter_meta": {},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    review_contract = {"must_check": ["发现陷阱"], "blocking_rules": ["不可提前摊牌"]}
    plot_structure = {"mandatory_nodes": ["发现陷阱"], "prohibitions": ["不可提前摊牌"]}

    payload = PrewriteValidator(project_root).build(
        chapter=3,
        review_contract=review_contract,
        plot_structure=plot_structure,
    )

    assert payload["blocking"] is False
    assert payload["fulfillment_seed"]["planned_nodes"] == ["发现陷阱"]
    assert payload["disambiguation_domain"]["pending_count"] == 0


def test_prewrite_validator_blocks_when_required_contracts_missing(tmp_path):
    project_root = tmp_path
    (project_root / ".webnovel").mkdir(parents=True, exist_ok=True)
    (project_root / ".webnovel" / "state.json").write_text(
        json.dumps(
            {
                "disambiguation_pending": [],
                "disambiguation_warnings": [],
                "chapter_meta": {},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    payload = PrewriteValidator(project_root).build(
        chapter=3,
        review_contract={},
        plot_structure={},
        story_contract={
            "master_setting": {},
            "chapter_brief": {},
            "volume_brief": {},
            "review_contract": {},
        },
    )

    assert payload["blocking"] is True
    assert "missing_contracts" in payload
    assert set(payload["missing_contracts"]) >= {"master_setting", "review_contract"}


def test_prewrite_validator_blocks_related_entity_placeholders(tmp_path):
    project_root = tmp_path
    (project_root / ".webnovel").mkdir(parents=True, exist_ok=True)
    (project_root / ".webnovel" / "state.json").write_text(
        json.dumps(
            {
                "disambiguation_pending": [],
                "disambiguation_warnings": [],
                "chapter_meta": {},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    settings_dir = project_root / "设定集"
    settings_dir.mkdir(parents=True, exist_ok=True)
    (settings_dir / "苏云.md").write_text("苏云：第一位女主（暂名）\n", encoding="utf-8")
    (settings_dir / "远期角色.md").write_text("后续兄弟：[待补充]\n", encoding="utf-8")

    payload = PrewriteValidator(project_root).build(
        chapter=8,
        review_contract={},
        plot_structure={},
        story_contract={
            "master_setting": {"ok": True},
            "chapter_brief": {"chapter_directive": {"key_entities": ["苏云"]}},
            "volume_brief": {"ok": True},
            "review_contract": {"ok": True},
        },
    )

    assert payload["blocking"] is True
    assert any("占位" in reason for reason in payload["blocking_reasons"])
    assert [item["file"] for item in payload["related_placeholders"]] == ["设定集/苏云.md"]


@pytest.mark.parametrize(
    ("contents", "code"),
    [
        (b"\xff", "state_not_utf8"),
        (b"{broken", "state_invalid_json"),
        (b"[]", "state_not_object"),
    ],
)
def test_prewrite_validator_turns_invalid_state_into_blocker(tmp_path, contents, code):
    state_path = tmp_path / ".webnovel" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_bytes(contents)

    payload = PrewriteValidator(tmp_path).build(
        chapter=1,
        review_contract={},
        plot_structure={},
    )

    assert payload["schema_version"] == "webnovel-prewrite-validation/v1"
    assert payload["ok"] is False
    assert payload["blocking"] is True
    assert payload["errors"][0]["code"] == code
    assert payload["blocking_reasons"]


def test_prewrite_validator_turns_missing_state_into_blocker(tmp_path):
    payload = PrewriteValidator(tmp_path).build(
        chapter=1,
        review_contract={},
        plot_structure={},
    )

    assert payload["blocking"] is True
    assert payload["errors"][0]["code"] == "state_missing"


def test_prewrite_validator_turns_state_read_failure_into_blocker(monkeypatch, tmp_path):
    state_path = tmp_path / ".webnovel" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text("{}", encoding="utf-8")
    original_read_text = type(state_path).read_text

    def fail_state_read(path, *args, **kwargs):
        if path == state_path:
            raise OSError("simulated read failure")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(type(state_path), "read_text", fail_state_read)

    payload = PrewriteValidator(tmp_path).build(
        chapter=1,
        review_contract={},
        plot_structure={},
    )

    assert payload["blocking"] is True
    assert payload["errors"][0]["code"] == "state_read_failed"


def test_prewrite_validator_turns_placeholder_scan_failure_into_blocker(monkeypatch, tmp_path):
    state_path = tmp_path / ".webnovel" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text("{}", encoding="utf-8")

    def fail_scan(_project_root):
        raise OSError("simulated placeholder scan failure")

    monkeypatch.setattr(prewrite_validator_module, "scan_placeholders", fail_scan)
    payload = PrewriteValidator(tmp_path).build(
        chapter=2,
        review_contract={},
        plot_structure={},
        story_contract={
            "master_setting": {"ok": True},
            "chapter_brief": {"chapter_directive": {"key_entities": ["苏云"]}},
            "volume_brief": {"ok": True},
            "review_contract": {"ok": True},
        },
    )

    assert payload["blocking"] is True
    assert payload["errors"][0]["code"] == "placeholder_scan_failed"
    assert any("扫描" in reason for reason in payload["blocking_reasons"])
