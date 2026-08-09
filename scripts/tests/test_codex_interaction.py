#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import data_modules.codex_interaction as interaction
from data_modules.codex_interaction import (
    ChoiceProtocolError,
    build_choice_request,
    execute_selected_branches,
    load_pending_choice,
    pending_choice_path,
    persist_pending_choice,
    resolve_choice,
)


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "evals" / "fixtures" / "codex_interaction" / "choice_cases.json"


def _questions() -> list[dict]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["questions"]


def _two_questions() -> list[dict]:
    return _questions() + [
        {
            "id": "git_mode",
            "prompt": "Git 如何处理？",
            "options": [
                {
                    "id": "off",
                    "label": "不初始化",
                    "description": "保持 Git 关闭。",
                    "recommended": True,
                },
                {
                    "id": "init",
                    "label": "仅初始化",
                    "description": "只初始化仓库，不创建提交。",
                    "recommended": False,
                },
            ],
        }
    ]


def test_structured_choice_is_business_decision_and_never_pre_authorized() -> None:
    request = build_choice_request(_questions(), transport="structured_choice")

    assert request["status"] == "awaiting_user"
    assert request["kind"] == "business_decision"
    assert request["authorization"] == {
        "write_allowed": False,
        "selected_branches": {},
        "native_permission_requested": False,
    }
    assert request["questions"][0]["options"][0]["recommended"] is True
    assert all(
        option["recommended"] is False
        for option in request["questions"][0]["options"][1:]
    )


def test_numbered_fallback_has_same_options_and_waits() -> None:
    request = build_choice_request(_questions(), transport="numbered_fallback")

    assert "1. 安装并重启（推荐）" in request["prompt"]
    assert "2. 只检查" in request["prompt"]
    assert "3. 取消" in request["prompt"]
    assert "收到明确回答前不会执行写入" in request["prompt"]
    assert resolve_choice(request, None)["write_allowed"] is False


def test_unanswered_choice_cannot_execute_or_write(tmp_path: Path) -> None:
    request = build_choice_request(_questions())
    resolution = resolve_choice(request)
    marker = tmp_path / "must-not-exist.txt"

    with pytest.raises(ChoiceProtocolError, match="has not authorized"):
        execute_selected_branches(
            request,
            resolution,
            {"setup_action": {"apply": lambda: marker.write_text("bad", encoding="utf-8")}},
        )

    assert not marker.exists()


def test_numbered_answer_executes_only_selected_branch(tmp_path: Path) -> None:
    request = build_choice_request(_questions(), transport="numbered_fallback")
    resolution = resolve_choice(request, "2")
    apply_marker = tmp_path / "apply.txt"
    check_marker = tmp_path / "check.txt"
    cancel_marker = tmp_path / "cancel.txt"

    result = execute_selected_branches(
        request,
        resolution,
        {
            "setup_action": {
                "apply": lambda: apply_marker.write_text("apply", encoding="utf-8"),
                "check_only": lambda: check_marker.write_text("check", encoding="utf-8"),
                "cancel": lambda: cancel_marker.write_text("cancel", encoding="utf-8"),
            }
        },
    )

    assert result["setup_action"]["option_id"] == "check_only"
    assert check_marker.read_text(encoding="utf-8") == "check"
    assert not apply_marker.exists()
    assert not cancel_marker.exists()


def test_structured_answer_executes_only_named_branch(tmp_path: Path) -> None:
    request = build_choice_request(_questions())
    resolution = resolve_choice(request, {"answers": {"setup_action": "cancel"}})
    calls: list[str] = []

    execute_selected_branches(
        request,
        resolution,
        {
            "setup_action": {
                "apply": lambda: calls.append("apply"),
                "check_only": lambda: calls.append("check_only"),
                "cancel": lambda: calls.append("cancel"),
            }
        },
    )

    assert calls == ["cancel"]
    assert list(tmp_path.iterdir()) == []


def test_invalid_or_freeform_answer_never_uses_recommended_default() -> None:
    request = build_choice_request(_questions())

    invalid = resolve_choice(request, "9")
    freeform = resolve_choice(request, "请换一种不覆盖现有文件的方案")

    assert invalid["status"] == "needs_clarification"
    assert invalid["write_allowed"] is False
    assert invalid["selected_branches"] == {}
    assert freeform["status"] == "needs_clarification"
    assert freeform["write_allowed"] is False
    assert freeform["selected_branches"] == {}


def test_visible_label_match_is_exact_nfkc_casefolded_and_unambiguous() -> None:
    request = build_choice_request(
        [
            {
                "id": "init_action",
                "prompt": "执行初始化？",
                "options": [
                    {
                        "id": "apply",
                        "label": "Apply",
                        "description": "执行当前预览。",
                        "recommended": True,
                    },
                    {
                        "id": "revise",
                        "label": "Revise",
                        "description": "返回修改。",
                        "recommended": False,
                    },
                    {
                        "id": "cancel",
                        "label": "Cancel",
                        "description": "取消。",
                        "recommended": False,
                    },
                ],
            }
        ]
    )

    selected = resolve_choice(request, "  ＡＰＰＬＹ  ")
    assert selected["status"] == "selected"
    assert selected["selected_branches"] == {"init_action": "apply"}
    for unsafe in ("App", "Please Apply", "Apply now"):
        unresolved = resolve_choice(request, unsafe)
        assert unresolved["status"] == "needs_clarification"
        assert unresolved["write_allowed"] is False

    duplicate_label = copy.deepcopy(request["questions"])
    duplicate_label[0]["options"][0]["label"] = "Start"
    duplicate_label[0]["options"][1]["label"] = "ＳＴＡＲＴ"
    with pytest.raises(ChoiceProtocolError, match="selectors are ambiguous"):
        build_choice_request(duplicate_label)

    label_id_conflict = copy.deepcopy(request["questions"])
    label_id_conflict[0]["options"][0]["label"] = "Start"
    label_id_conflict[0]["options"][1]["label"] = "ＡＰＰＬＹ"
    with pytest.raises(ChoiceProtocolError, match="selectors are ambiguous"):
        build_choice_request(label_id_conflict)


def test_all_questions_must_be_answered_before_any_callback_runs() -> None:
    questions = _questions() + [
        {
            "id": "git_mode",
            "prompt": "Git 如何处理？",
            "options": [
                {
                    "id": "off",
                    "label": "不初始化",
                    "description": "保持 Git 关闭。",
                    "recommended": True,
                },
                {
                    "id": "init",
                    "label": "仅初始化",
                    "description": "只初始化仓库，不创建提交。",
                    "recommended": False,
                },
            ],
        }
    ]
    request = build_choice_request(questions, transport="numbered_fallback")
    resolution = resolve_choice(request, {"setup_action": "check_only"})
    calls: list[str] = []

    with pytest.raises(ChoiceProtocolError, match="has not authorized"):
        execute_selected_branches(request, resolution, {})

    assert resolution["unresolved_questions"] == ["git_mode"]
    assert calls == []


def test_stale_or_malformed_choice_request_is_rejected_before_execution() -> None:
    request = build_choice_request(_questions())
    request["questions"][0]["prompt"] = "被替换的问题"

    with pytest.raises(ChoiceProtocolError, match="request id"):
        resolve_choice(request, "1")


def test_choice_shape_requires_two_or_three_options_with_recommended_first() -> None:
    question = _questions()[0]
    question["options"][0]["recommended"] = False

    with pytest.raises(ChoiceProtocolError, match="first option"):
        build_choice_request([question])


def test_pending_decision_persists_only_management_metadata(tmp_path: Path) -> None:
    workspace = tmp_path / "工作区 (A&B)"
    workspace.mkdir()
    request = build_choice_request(_questions(), transport="numbered_fallback")

    path = persist_pending_choice(workspace, request)
    loaded = load_pending_choice(workspace, request["request_id"])

    assert path == pending_choice_path(workspace, request["request_id"])
    assert path.relative_to(workspace).as_posix().startswith(
        ".codex/novel-writer-codex/pending-decisions/"
    )
    assert loaded["authorization"]["write_allowed"] is False
    assert loaded["authorization"]["selected_branches"] == {}
    assert not (workspace / ".webnovel").exists()
    assert not (workspace / ".story-system").exists()


def test_pending_decision_rejects_tampering_before_disk_write(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    request = build_choice_request(_questions())
    request["questions"][0]["prompt"] = "tampered"

    with pytest.raises(ChoiceProtocolError, match="request id"):
        persist_pending_choice(workspace, request)

    assert list(workspace.iterdir()) == []


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("questions_string", "sequence"),
        ("questions_empty", "1 to 3"),
        ("questions_many", "1 to 3"),
        ("question_scalar", "question must be an object"),
        ("question_bad_id", "invalid question id"),
        ("question_duplicate", "duplicate question id"),
        ("prompt_empty", "must not be empty"),
        ("prompt_long", "exceeds"),
        ("options_string", "options must be a sequence"),
        ("options_short", "expected 2 or 3"),
        ("option_scalar", "option must be an object"),
        ("option_bad_id", "invalid option id"),
        ("option_duplicate", "duplicate option id"),
        ("label_empty", "must not be empty"),
        ("description_long", "exceeds"),
    ],
)
def test_choice_request_rejects_every_malformed_shape(case: str, message: str) -> None:
    questions: object = copy.deepcopy(_questions())
    if case == "questions_string":
        questions = "not-a-sequence"
    elif case == "questions_empty":
        questions = []
    elif case == "questions_many":
        questions = _questions() * 4
    elif case == "question_scalar":
        questions = ["bad"]
    elif case == "question_bad_id":
        questions[0]["id"] = "Bad Id"
    elif case == "question_duplicate":
        questions = _two_questions()
        questions[1]["id"] = questions[0]["id"]
    elif case == "prompt_empty":
        questions[0]["prompt"] = " "
    elif case == "prompt_long":
        questions[0]["prompt"] = "x" * 241
    elif case == "options_string":
        questions[0]["options"] = "bad"
    elif case == "options_short":
        questions[0]["options"] = questions[0]["options"][:1]
    elif case == "option_scalar":
        questions[0]["options"][0] = "bad"
    elif case == "option_bad_id":
        questions[0]["options"][0]["id"] = "Bad Id"
    elif case == "option_duplicate":
        questions[0]["options"][1]["id"] = questions[0]["options"][0]["id"]
    elif case == "label_empty":
        questions[0]["options"][0]["label"] = ""
    elif case == "description_long":
        questions[0]["options"][0]["description"] = "x" * 241

    with pytest.raises(ChoiceProtocolError, match=message):
        build_choice_request(questions)


def test_choice_request_rejects_unknown_transport() -> None:
    with pytest.raises(ChoiceProtocolError, match="unsupported choice transport"):
        build_choice_request(_questions(), transport="permission_prompt")


def test_multi_question_fallback_parses_ordered_and_named_answers() -> None:
    request = build_choice_request(_two_questions(), transport="numbered_fallback")

    ordered = resolve_choice(request, "1，2")
    named = resolve_choice(request, "setup_action=2; git_mode:1")
    malformed = resolve_choice(request, "setup_action=2; 这不是键值对")

    assert "问题 1（setup_action）" in request["prompt"]
    assert "请按问题顺序回复编号" in request["prompt"]
    assert ordered["selected_branches"] == {"setup_action": "apply", "git_mode": "init"}
    assert named["selected_branches"] == {"setup_action": "check_only", "git_mode": "off"}
    assert malformed["status"] == "needs_clarification"
    assert malformed["write_allowed"] is False


def test_answer_mapping_rejects_invalid_structured_container_and_unknown_question() -> None:
    request = build_choice_request(_questions())

    with pytest.raises(ChoiceProtocolError, match="answers must be an object"):
        resolve_choice(request, {"answers": []})
    with pytest.raises(ChoiceProtocolError, match="unknown question id"):
        resolve_choice(request, {"setup_action": "apply", "foreign": "x"})

    assert resolve_choice(request, "   ")["status"] == "awaiting_user"


def test_resolve_choice_rejects_bad_schema_or_question_container() -> None:
    request = build_choice_request(_questions())
    bad_schema = dict(request, schema_version="future")
    bad_questions = dict(request, questions="bad")

    with pytest.raises(ChoiceProtocolError, match="choice schema"):
        resolve_choice(bad_schema, "1")
    with pytest.raises(ChoiceProtocolError, match="no questions"):
        resolve_choice(bad_questions, "1")


def test_branch_execution_rejects_stale_missing_or_unregistered_selections() -> None:
    request = build_choice_request(_questions())
    selected = resolve_choice(request, "1")

    with pytest.raises(ChoiceProtocolError, match="stale choice"):
        execute_selected_branches(request, dict(selected, request_id="stale"), {})
    with pytest.raises(ChoiceProtocolError, match="selected branches are missing"):
        execute_selected_branches(request, dict(selected, selected_branches=[]), {})
    with pytest.raises(ChoiceProtocolError, match="no callback registered"):
        execute_selected_branches(request, selected, {})


def test_pending_path_rejects_bad_id_or_missing_workspace(tmp_path: Path) -> None:
    with pytest.raises(ChoiceProtocolError, match="invalid choice request id"):
        pending_choice_path(tmp_path, "../escape")
    with pytest.raises(ChoiceProtocolError, match="workspace root"):
        pending_choice_path(tmp_path / "missing", "choice-" + "a" * 20)


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("schema", "choice schema"),
        ("status", "awaiting decision"),
        ("authorization_type", "must not authorize writes"),
        ("write_allowed", "must not authorize writes"),
        ("selected", "must not select a branch"),
        ("questions", "no questions"),
    ],
)
def test_persist_pending_choice_rejects_unsafe_envelopes(
    tmp_path: Path,
    case: str,
    message: str,
) -> None:
    request = build_choice_request(_questions())
    if case == "schema":
        request["schema_version"] = "future"
    elif case == "status":
        request["status"] = "selected"
    elif case == "authorization_type":
        request["authorization"] = []
    elif case == "write_allowed":
        request["authorization"]["write_allowed"] = True
    elif case == "selected":
        request["authorization"]["selected_branches"] = {"setup_action": "apply"}
    elif case == "questions":
        request["questions"] = "bad"

    with pytest.raises(ChoiceProtocolError, match=message):
        persist_pending_choice(tmp_path, request)


def test_load_pending_choice_rejects_missing_corrupt_and_nonobject_files(tmp_path: Path) -> None:
    request = build_choice_request(_questions())
    request_id = request["request_id"]

    with pytest.raises(ChoiceProtocolError, match="does not exist"):
        load_pending_choice(tmp_path, request_id)

    path = pending_choice_path(tmp_path, request_id)
    path.parent.mkdir(parents=True)
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(ChoiceProtocolError, match="cannot be read"):
        load_pending_choice(tmp_path, request_id)

    path.write_text("[]", encoding="utf-8")
    with pytest.raises(ChoiceProtocolError, match="JSON object"):
        load_pending_choice(tmp_path, request_id)


def test_load_pending_choice_revalidates_status_and_authorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = build_choice_request(_questions())
    path = persist_pending_choice(tmp_path, request)
    monkeypatch.setattr(interaction, "resolve_choice", lambda *_: {"status": "selected"})
    with pytest.raises(ChoiceProtocolError, match="not pending"):
        load_pending_choice(tmp_path, request["request_id"])

    monkeypatch.undo()
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["authorization"]["write_allowed"] = True
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ChoiceProtocolError, match="authorizes writes"):
        load_pending_choice(tmp_path, request["request_id"])
