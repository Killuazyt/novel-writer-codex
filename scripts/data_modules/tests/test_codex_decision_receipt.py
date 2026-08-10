from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from data_modules import codex_decision_receipt as decisions
from data_modules.codex_decision_receipt import (
    DecisionReceiptError,
    build_scope_bound_decision_request,
    select_scope_bound_decision,
    verify_scope_bound_decision_receipt,
)


THREAD_ID = "aaaaaaaa-1111-4111-8111-111111111111"
OTHER_THREAD_ID = "bbbbbbbb-2222-4222-8222-222222222222"
MODEL = "gpt-5.6-sol"
EFFORT = "high"


def _options(count: int = 3) -> list[dict[str, object]]:
    values: list[dict[str, object]] = [
        {
            "id": "replace_with_verified",
            "label": "覆盖",
            "description": "以本轮已经验证的内容覆盖冲突目标。",
            "recommended": True,
        },
        {
            "id": "keep_current",
            "label": "保留",
            "description": "保留作者当前内容并停止本轮提升。",
            "recommended": False,
        },
        {
            "id": "cancel",
            "label": "取消",
            "description": "取消本次恢复，不修改正文。",
            "recommended": False,
        },
    ]
    return values[:count]


def _request(monkeypatch, *, scope: dict[str, object] | None = None, option_count: int = 3):
    monkeypatch.setenv("CODEX_THREAD_ID", THREAD_ID)
    return build_scope_bound_decision_request(
        scope or {"workflow": "write", "run_id": "write-1", "target_sha256": "a" * 64},
        question_id="recovery_action",
        prompt="正文发生冲突，请选择唯一恢复方式。",
        options=_options(option_count),
        expected_parent_thread_id=THREAD_ID,
        expected_parent_model=MODEL,
        expected_parent_reasoning_effort=EFFORT,
    )


def _message(role: str, text: str) -> dict[str, object]:
    kind = "input_text" if role == "user" else "output_text"
    return {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": role,
            "content": [{"type": kind, "text": text}],
        },
    }


def _base_events(
    marker: str,
    *,
    answer: str | None = "replace_with_verified",
    thread_id: str = THREAD_ID,
    model: str = MODEL,
    effort: str = EFFORT,
    session_extra: dict[str, object] | None = None,
) -> list[dict[str, object]]:
    session: dict[str, object] = {
        "id": thread_id,
        "source": "codex_desktop",
        "model": model,
    }
    session.update(session_extra or {})
    events: list[dict[str, object]] = [
        {"type": "session_meta", "payload": session},
        {
            "type": "turn_context",
            "payload": {"turn_id": "turn-1", "model": model, "effort": effort},
        },
        _message("assistant", f"请确认。\n{marker}"),
    ]
    if answer is not None:
        events.append(_message("user", answer))
    return events


def _write_rollout(
    tmp_path: Path,
    events: list[dict[str, object]],
    *,
    thread_id: str = THREAD_ID,
) -> tuple[Path, Path]:
    sessions = tmp_path / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    rollout = sessions / f"rollout-{thread_id}.jsonl"
    rollout.write_text(
        "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events),
        encoding="utf-8",
    )
    return sessions, rollout


@pytest.mark.parametrize("option_count", [2, 3])
def test_builds_deterministic_scope_bound_two_or_three_option_request(monkeypatch, option_count):
    request = _request(monkeypatch, option_count=option_count)

    assert request["schema_version"] == decisions.DECISION_REQUEST_SCHEMA
    assert len(request["choice_request"]["questions"][0]["options"]) == option_count
    assert request["choice_request"]["authorization"]["write_allowed"] is False
    assert request["choice_request"]["authorization"]["selected_branches"] == {}
    assert request["binding_marker"].startswith(decisions.DECISION_MARKER_SCHEMA + " ")
    for field in (
        "scope_sha256",
        "request_sha256",
        "binding_marker_sha256",
    ):
        assert len(request[field]) == 64

    again = _request(monkeypatch, option_count=option_count)
    assert again == request


@pytest.mark.parametrize("thread_id", ["", "00000000-0000-0000-0000-000000000000", THREAD_ID.upper()])
def test_request_requires_current_canonical_nonzero_thread(monkeypatch, thread_id):
    monkeypatch.setenv("CODEX_THREAD_ID", thread_id)
    with pytest.raises(DecisionReceiptError) as caught:
        build_scope_bound_decision_request(
            {"workflow": "write", "run_id": "one"},
            question_id="recovery_action",
            prompt="请选择。",
            options=_options(),
            expected_parent_thread_id=THREAD_ID,
            expected_parent_model=MODEL,
            expected_parent_reasoning_effort=EFFORT,
        )
    assert caught.value.code in {"invalid_parent_thread", "cross_thread_decision"}


def test_selects_from_first_durable_user_answer_without_storing_plaintext(monkeypatch, tmp_path):
    request = _request(monkeypatch)
    answer = "覆盖"
    sessions, rollout = _write_rollout(
        tmp_path,
        _base_events(request["binding_marker"], answer=answer),
    )

    receipt = select_scope_bound_decision(
        request,
        sessions_root=sessions,
        rollout_path=rollout,
    )

    assert receipt["status"] == "selected"
    assert receipt["selected"] == "replace_with_verified"
    assert receipt["parent_thread_id"] == THREAD_ID
    assert receipt["parent_model"] == MODEL
    assert receipt["parent_reasoning_effort"] == EFFORT
    serialized = json.dumps(receipt, ensure_ascii=False)
    assert answer not in serialized
    assert "answer" not in receipt
    for field in (
        "scope_sha256",
        "request_sha256",
        "binding_marker_sha256",
        "answer_sha256",
        "authorization_prefix_sha256",
        "receipt_sha256",
    ):
        assert len(receipt[field]) == 64


def test_receipt_reverifies_after_rollout_only_append(monkeypatch, tmp_path):
    request = _request(monkeypatch)
    sessions, rollout = _write_rollout(
        tmp_path,
        _base_events(request["binding_marker"], answer="2"),
    )
    receipt = select_scope_bound_decision(request, sessions_root=sessions, rollout_path=rollout)

    with rollout.open("a", encoding="utf-8", newline="") as handle:
        handle.write(json.dumps(_message("assistant", "后续只读状态。"), ensure_ascii=False) + "\n")

    verified = verify_scope_bound_decision_receipt(
        request,
        receipt,
        sessions_root=sessions,
        rollout_path=rollout,
    )
    assert verified == receipt
    assert verified["selected"] == "keep_current"


@pytest.mark.parametrize(
    ("events_mutator", "code"),
    [
        (lambda events, marker: events[:-1], "decision_answer_missing"),
        (
            lambda events, marker: events[:-1] + [_message("assistant", "还未收到用户回答。")],
            "decision_answer_missing",
        ),
        (
            lambda events, marker: events[:3] + [_message("assistant", marker)] + events[3:],
            "decision_marker_not_unique",
        ),
        (
            lambda events, marker: events[:2]
            + [_message("assistant", f"{marker}\n{marker}")]
            + events[3:],
            "decision_marker_not_unique",
        ),
    ],
)
def test_missing_answer_or_duplicate_marker_is_rejected(
    monkeypatch,
    tmp_path,
    events_mutator,
    code,
):
    request = _request(monkeypatch)
    events = _base_events(request["binding_marker"])
    sessions, rollout = _write_rollout(
        tmp_path,
        events_mutator(events, request["binding_marker"]),
    )

    with pytest.raises(DecisionReceiptError) as caught:
        select_scope_bound_decision(request, sessions_root=sessions, rollout_path=rollout)
    assert caught.value.code == code


def test_duplicate_marker_appended_after_selection_invalidates_receipt(monkeypatch, tmp_path):
    request = _request(monkeypatch)
    sessions, rollout = _write_rollout(tmp_path, _base_events(request["binding_marker"]))
    receipt = select_scope_bound_decision(request, sessions_root=sessions, rollout_path=rollout)
    with rollout.open("a", encoding="utf-8", newline="") as handle:
        handle.write(json.dumps(_message("assistant", request["binding_marker"]), ensure_ascii=False) + "\n")

    with pytest.raises(DecisionReceiptError) as caught:
        verify_scope_bound_decision_receipt(
            request,
            receipt,
            sessions_root=sessions,
            rollout_path=rollout,
        )
    assert caught.value.code == "decision_marker_not_unique"


def test_tampered_receipt_and_cross_scope_request_are_rejected(monkeypatch, tmp_path):
    request = _request(monkeypatch)
    sessions, rollout = _write_rollout(tmp_path, _base_events(request["binding_marker"]))
    receipt = select_scope_bound_decision(request, sessions_root=sessions, rollout_path=rollout)

    tampered = dict(receipt)
    tampered["selected"] = "keep_current"
    with pytest.raises(DecisionReceiptError) as caught:
        verify_scope_bound_decision_receipt(
            request,
            tampered,
            sessions_root=sessions,
            rollout_path=rollout,
        )
    assert caught.value.code == "invalid_decision_receipt"

    other_request = _request(
        monkeypatch,
        scope={"workflow": "write", "run_id": "write-2", "target_sha256": "b" * 64},
    )
    with pytest.raises(DecisionReceiptError) as caught:
        verify_scope_bound_decision_receipt(
            other_request,
            receipt,
            sessions_root=sessions,
            rollout_path=rollout,
        )
    assert caught.value.code in {"decision_marker_not_unique", "invalid_decision_receipt"}


def test_cross_thread_reverification_is_rejected(monkeypatch, tmp_path):
    request = _request(monkeypatch)
    sessions, rollout = _write_rollout(tmp_path, _base_events(request["binding_marker"]))
    receipt = select_scope_bound_decision(request, sessions_root=sessions, rollout_path=rollout)

    monkeypatch.setenv("CODEX_THREAD_ID", OTHER_THREAD_ID)
    with pytest.raises(DecisionReceiptError) as caught:
        verify_scope_bound_decision_receipt(
            request,
            receipt,
            sessions_root=sessions,
            rollout_path=rollout,
        )
    assert caught.value.code == "cross_thread_decision"


def test_parent_session_thread_mismatch_preserves_cross_thread_error(monkeypatch, tmp_path):
    request = _request(monkeypatch)
    events = _base_events(request["binding_marker"])
    events[0]["payload"]["id"] = OTHER_THREAD_ID
    sessions, rollout = _write_rollout(tmp_path, events)

    with pytest.raises(DecisionReceiptError) as caught:
        select_scope_bound_decision(request, sessions_root=sessions, rollout_path=rollout)
    assert caught.value.code == "cross_thread_decision"


@pytest.mark.parametrize(
    ("model", "effort", "code"),
    [
        ("gpt-5.6-terra", EFFORT, "parent_model_mismatch"),
        (MODEL, "low", "parent_effort_mismatch"),
    ],
)
def test_parent_model_and_effort_are_exact(monkeypatch, tmp_path, model, effort, code):
    request = _request(monkeypatch)
    sessions, rollout = _write_rollout(
        tmp_path,
        _base_events(request["binding_marker"], model=model, effort=effort),
    )
    with pytest.raises(DecisionReceiptError) as caught:
        select_scope_bound_decision(request, sessions_root=sessions, rollout_path=rollout)
    assert caught.value.code == code


@pytest.mark.parametrize(
    "session_extra",
    [
        {"parent_thread_id": OTHER_THREAD_ID},
        {"source": {"subagent": {"thread_spawn": {"parent_thread_id": OTHER_THREAD_ID}}}},
    ],
)
def test_child_parent_rollout_is_rejected(monkeypatch, tmp_path, session_extra):
    request = _request(monkeypatch)
    sessions, rollout = _write_rollout(
        tmp_path,
        _base_events(request["binding_marker"], session_extra=session_extra),
    )
    with pytest.raises(DecisionReceiptError) as caught:
        select_scope_bound_decision(request, sessions_root=sessions, rollout_path=rollout)
    assert caught.value.code == "child_rollout_rejected"


def test_identity_equivalent_repeated_session_meta_are_a_parent_receipt(monkeypatch, tmp_path):
    request = _request(monkeypatch)
    events = _base_events(request["binding_marker"])
    with_memory = copy.deepcopy(events[0])
    with_memory["payload"]["memory_mode"] = "enabled"
    events[1:1] = [with_memory, copy.deepcopy(with_memory)]
    sessions, rollout = _write_rollout(tmp_path, events)

    receipt = select_scope_bound_decision(request, sessions_root=sessions, rollout_path=rollout)

    assert receipt["selected"] == "replace_with_verified"


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("id", OTHER_THREAD_ID, "cross_thread_decision"),
        ("parent_thread_id", OTHER_THREAD_ID, "invalid_parent_identity"),
        ("model", "gpt-5.6-terra", "invalid_parent_identity"),
        ("source", "codex_cli", "invalid_parent_identity"),
    ],
)
def test_conflicting_repeated_session_meta_fail_closed(monkeypatch, tmp_path, field, value, code):
    request = _request(monkeypatch)
    events = _base_events(request["binding_marker"])
    conflicting = copy.deepcopy(events[0])
    conflicting["payload"][field] = value
    events.insert(1, conflicting)
    sessions, rollout = _write_rollout(tmp_path, events)

    with pytest.raises(DecisionReceiptError) as caught:
        select_scope_bound_decision(request, sessions_root=sessions, rollout_path=rollout)
    assert caught.value.code == code


def test_exact_duplicate_turn_context_is_coalesced(monkeypatch, tmp_path):
    request = _request(monkeypatch)
    events = _base_events(request["binding_marker"])
    events.insert(2, copy.deepcopy(events[1]))
    sessions, rollout = _write_rollout(tmp_path, events)

    receipt = select_scope_bound_decision(request, sessions_root=sessions, rollout_path=rollout)

    assert receipt["selected"] == "replace_with_verified"


def test_duplicate_turn_context_payload_conflict_fails_closed(monkeypatch, tmp_path):
    request = _request(monkeypatch)
    events = _base_events(request["binding_marker"])
    conflicting = copy.deepcopy(events[1])
    conflicting["payload"]["effort"] = "medium"
    events.insert(2, conflicting)
    sessions, rollout = _write_rollout(tmp_path, events)

    with pytest.raises(DecisionReceiptError) as caught:
        select_scope_bound_decision(request, sessions_root=sessions, rollout_path=rollout)
    assert caught.value.code == "invalid_parent_identity"


def test_freeform_answer_is_not_silently_authorized(monkeypatch, tmp_path):
    request = _request(monkeypatch)
    sessions, rollout = _write_rollout(
        tmp_path,
        _base_events(request["binding_marker"], answer="我再想想，先按你觉得好的来"),
    )
    with pytest.raises(DecisionReceiptError) as caught:
        select_scope_bound_decision(request, sessions_root=sessions, rollout_path=rollout)
    assert caught.value.code == "invalid_decision_answer"


def test_rollout_must_be_bounded_utf8_jsonl_under_exact_sessions_root(monkeypatch, tmp_path):
    request = _request(monkeypatch)
    sessions, rollout = _write_rollout(tmp_path, _base_events(request["binding_marker"]))
    outside = tmp_path / f"outside-{THREAD_ID}.jsonl"
    outside.write_bytes(rollout.read_bytes())

    with pytest.raises(DecisionReceiptError) as caught:
        select_scope_bound_decision(request, sessions_root=sessions, rollout_path=outside)
    assert caught.value.code == "invalid_rollout_path"

    rollout.write_bytes(b"\xef\xbb\xbf" + rollout.read_bytes())
    with pytest.raises(DecisionReceiptError) as caught:
        select_scope_bound_decision(request, sessions_root=sessions, rollout_path=rollout)
    assert caught.value.code == "invalid_rollout"


def test_rollout_size_bound_and_reparse_probe_fail_closed(monkeypatch, tmp_path):
    request = _request(monkeypatch)
    sessions, rollout = _write_rollout(tmp_path, _base_events(request["binding_marker"]))
    monkeypatch.setattr(decisions, "MAX_ROLLOUT_BYTES", 8)
    with pytest.raises(DecisionReceiptError) as caught:
        select_scope_bound_decision(request, sessions_root=sessions, rollout_path=rollout)
    assert caught.value.code == "invalid_rollout"

    monkeypatch.setattr(decisions, "MAX_ROLLOUT_BYTES", 32 * 1024 * 1024)
    real_reparse = decisions._is_reparse
    monkeypatch.setattr(decisions, "_is_reparse", lambda path: Path(path) == rollout or real_reparse(path))
    with pytest.raises(DecisionReceiptError) as caught:
        select_scope_bound_decision(request, sessions_root=sessions, rollout_path=rollout)
    assert caught.value.code == "invalid_rollout_path"


def test_request_scope_or_parent_binding_tamper_is_rejected_before_rollout(monkeypatch):
    request = _request(monkeypatch)
    changed_scope = copy.deepcopy(request)
    changed_scope["scope"]["run_id"] = "other"
    with pytest.raises(DecisionReceiptError) as caught:
        select_scope_bound_decision(
            changed_scope,
            sessions_root=Path("C:/not-used"),
            rollout_path=Path("C:/not-used/rollout.jsonl"),
        )
    assert caught.value.code == "cross_scope_decision"

    changed_parent = copy.deepcopy(request)
    changed_parent["parent_binding"]["model"] = "gpt-5.6-terra"
    with pytest.raises(DecisionReceiptError) as caught:
        select_scope_bound_decision(
            changed_parent,
            sessions_root=Path("C:/not-used"),
            rollout_path=Path("C:/not-used/rollout.jsonl"),
        )
    assert caught.value.code in {"invalid_decision_marker", "invalid_decision_request"}
