#!/usr/bin/env python3
from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import uuid
from pathlib import Path

import pytest

import data_modules.review_request as review_request_module
import data_modules.review_workflow as review_workflow_module
import data_modules.run_ledger as run_ledger_module
from data_modules.review_request import (
    ReviewRequestError,
    load_review_accept_request,
    load_review_decision_request,
)
from data_modules.review_schema import ReviewSchemaError, parse_review_output
from data_modules.review_workflow import (
    ReviewWorkflowError,
    _update_run,
    accept_review,
    decide_review,
    decide_review_range,
    format_review_result,
    prepare_review,
    prepare_review_range,
    resume_review,
    resume_review_range,
)
from data_modules.run_ledger import (
    RunLedgerError,
    file_signature,
    get_review_range,
    get_review_run,
    ledger_path,
    load_ledger,
    locked_ledger,
    save_ledger,
)


TEST_PARENT_THREAD_ID = "11111111-1111-4111-8111-111111111111"
OTHER_PARENT_THREAD_ID = "22222222-2222-4222-8222-222222222222"


def _write_json(path: Path, payload: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")


def _make_project(root: Path, chapters: int = 5) -> Path:
    project = root / "中文 项目&(review)"
    _write_json(
        project / ".webnovel" / "state.json",
        {"project_info": {"title": "审查书"}, "progress": {}},
    )
    for chapter in range(1, chapters + 1):
        path = project / "正文" / f"第{chapter:04d}章.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# 第{chapter}章\n中文，引号“测试”；& | $() ` 不应进入 shell。\n", encoding="utf-8")
    return project


def _route(parent_model: str = "gpt-5.6-sol", parent_effort: str | None = "high") -> dict:
    return {
        "schema_version": "webnovel-agent-route/v1",
        "workflow": "review",
        "mode": None,
        "executor": "agents",
        "parent_model": parent_model,
        "parent_reasoning_effort": parent_effort,
        "planning_model": None,
        "steps": [
            {
                "agent_name": "webnovel_reviewer",
                "model_source": "fixed",
                "requested_model": "gpt-5.6-luna",
                "requested_reasoning_effort": "medium",
                "sandbox_mode": "read-only",
                "contract_hash": "a" * 64,
                "managed_sha256": "b" * 64,
                "parent_model": parent_model,
                "parent_reasoning_effort": parent_effort,
            }
        ],
        "fallback_allowed": False,
    }


def _review_payload(chapter: int, *, blocking: bool = False, mode: str = "full") -> dict:
    issues = []
    if blocking:
        issues.append(
            {
                "severity": "critical",
                "category": "timeline",
                "location": "第2段",
                "description": "倒计时与可信上下文冲突",
                "evidence": "正文写三日，记录为一日",
                "fix_hint": "仅修正倒计时数字",
                "blocking": True,
            }
        )
    dimensions = []
    for name in ("setting", "timeline", "continuity", "character", "logic"):
        if mode == "fast" and name in {"character", "logic"}:
            conclusion = "skipped: fast mode"
        elif blocking and name == "timeline":
            conclusion = "发现1个问题：倒计时冲突"
        else:
            conclusion = "pass"
        dimensions.append({"dimension": name, "conclusion": conclusion})
    return {
        "chapter": chapter,
        "issues": issues,
        "issues_count": len(issues),
        "blocking_count": int(blocking),
        "has_blocking": blocking,
        "dimension_results": dimensions,
        "summary": "1个问题：1个阻断" if blocking else "无问题",
    }


def _accept_file(
    root: Path,
    prepared: dict,
    responses: list[dict | str],
    *,
    mode: str = "full",
    model: str = "gpt-5.6-luna",
    event_only: bool = False,
    duplicate_event_messages: bool = False,
) -> Path:
    sessions = review_workflow_module._trusted_codex_sessions_root()
    child_id = f"child-{prepared['run_id']}"
    parent_id = os.environ.get("CODEX_THREAD_ID", TEST_PARENT_THREAD_ID)
    rollout = sessions / "2026" / "08" / "08" / f"rollout-test-{child_id}.jsonl"
    rollout.parent.mkdir(parents=True, exist_ok=True)
    events: list[dict] = [
        {
            "type": "session_meta",
            "payload": {
                "id": child_id,
                "parent_thread_id": parent_id,
                "model": model,
                "source": {
                    "subagent": {
                        "thread_spawn": {
                            "parent_thread_id": parent_id,
                            "depth": 1,
                            "agent_path": "webnovel_reviewer",
                            "agent_nickname": "reviewer",
                            "agent_role": "webnovel_reviewer",
                        }
                    }
                },
            },
        },
        {
            "type": "turn_context",
            "payload": {"turn_id": "turn-1", "model": model, "effort": "medium"},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": f"{prepared['binding_marker']}\nrequest={prepared['request_file']}",
                    }
                ],
            },
        },
    ]
    for index, response in enumerate(responses, start=1):
        raw_response = response if isinstance(response, str) else json.dumps(response, ensure_ascii=False)
        if index > 1:
            events.extend(
                [
                    {
                        "type": "turn_context",
                        "payload": {
                            "turn_id": f"turn-{index}",
                            "model": model,
                            "effort": "medium",
                        },
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": f"serialization retry; {prepared['binding_marker']}",
                                }
                            ],
                        },
                    },
                ]
            )
        if not event_only:
            events.append(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "phase": "final_answer",
                        "content": [{"type": "output_text", "text": raw_response}],
                    },
                }
            )
        if event_only or duplicate_event_messages:
            events.append(
                {
                    "type": "event_msg",
                    "payload": {"type": "agent_message", "message": raw_response},
                }
            )
    rollout.write_text(
        "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n",
        encoding="utf-8",
    )
    return _write_json(
        root / "requests" / f"{prepared['run_id']}.json",
        {
            "schema_version": "webnovel-review-accept-request/v2",
            "run_id": prepared["run_id"],
            "chapter": prepared["chapter"],
            "review_mode": mode,
            "runtime": {
                "rollout_path": str(rollout.resolve()),
                "sessions_root": str(sessions.resolve()),
                "child_thread_id": child_id,
                "parent_thread_id": parent_id,
            },
            "duration_ms": 12,
        },
    ).resolve()


def _decision_file(
    root: Path,
    decision: dict,
    answer: str,
    *,
    run_id: str | None = None,
    range_id: str | None = None,
    marker: str | None = None,
    include_answer: bool = True,
    next_role: str = "user",
    parent_id: str | None = None,
    parent_model: str = "gpt-5.6-sol",
    parent_effort: str = "high",
    as_subagent: bool = False,
    sessions_root: Path | None = None,
    extra_request_fields: dict | None = None,
) -> Path:
    kind = "run" if run_id is not None else "range"
    assert (run_id is None) != (range_id is None)
    sessions = sessions_root or review_workflow_module._trusted_codex_sessions_root()
    parent_id = parent_id or os.environ.get("CODEX_THREAD_ID", TEST_PARENT_THREAD_ID)
    receipt_nonce = uuid.uuid4().hex
    rollout = (
        sessions
        / "2026"
        / "08"
        / "08"
        / f"rollout-test-{parent_id}-{receipt_nonce}.jsonl"
    )
    rollout.parent.mkdir(parents=True, exist_ok=True)
    events: list[dict] = [
        {
            "type": "session_meta",
            "payload": {
                "id": parent_id,
                "model": parent_model,
                **(
                    {
                        "parent_thread_id": "actual-parent",
                        "source": {
                            "subagent": {
                                "thread_spawn": {
                                    "parent_thread_id": "actual-parent",
                                    "agent_role": "webnovel_reviewer",
                                }
                            }
                        },
                    }
                    if as_subagent
                    else {}
                ),
            },
        },
        {
            "type": "turn_context",
            "payload": {
                "turn_id": f"turn-{uuid.uuid4().hex}",
                "model": parent_model,
                "effort": parent_effort,
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "phase": "final_answer",
                "content": [
                    {
                        "type": "output_text",
                        "text": f"请选择以下有限选项。\n{marker or decision['binding_marker']}",
                    }
                ],
            },
        },
    ]
    if include_answer:
        events.append(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": next_role,
                    "content": [{"type": "input_text", "text": answer}],
                },
            }
        )
    rollout.write_text(
        "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n",
        encoding="utf-8",
    )
    request = {
        "schema_version": "webnovel-review-decision-request/v1",
        "kind": kind,
        "run_id": run_id,
        "range_id": range_id,
        "request_id": decision["request_id"],
        "runtime": {
            "rollout_path": str(rollout.resolve()),
            "sessions_root": str(sessions.resolve()),
            "parent_thread_id": parent_id,
        },
    }
    request.update(extra_request_fields or {})
    return _write_json(
        root / "decision-requests" / f"{parent_id}-{receipt_nonce}.json",
        request,
    ).resolve()


def _decision_request_stub(
    root: Path,
    *,
    run_id: str | None = None,
    range_id: str | None = None,
    extra_fields: dict | None = None,
) -> Path:
    assert (run_id is None) != (range_id is None)
    sessions = review_workflow_module._trusted_codex_sessions_root()
    payload = {
        "schema_version": "webnovel-review-decision-request/v1",
        "kind": "run" if run_id is not None else "range",
        "run_id": run_id,
        "range_id": range_id,
        "request_id": f"choice-{'0' * 20}",
        "runtime": {
            "rollout_path": str((sessions / "missing-parent.jsonl").resolve()),
            "sessions_root": str(sessions.resolve()),
            "parent_thread_id": "parent-missing",
        },
    }
    payload.update(extra_fields or {})
    return _write_json(
        root / "decision-requests" / f"stub-{uuid.uuid4().hex}.json",
        payload,
    ).resolve()


def _choice_label(decision: dict, option_id: str) -> str:
    for option in decision.get("options") or []:
        if option.get("id") == option_id:
            return str(option["label"])
    raise AssertionError(f"missing visible choice label: {option_id}")


@pytest.fixture
def workflow_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    project = _make_project(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("CODEX_THREAD_ID", TEST_PARENT_THREAD_ID)
    monkeypatch.setattr(
        review_workflow_module,
        "_route",
        lambda workspace_root, *, parent_model, parent_reasoning_effort: _route(
            parent_model,
            parent_reasoning_effort,
        ),
    )
    monkeypatch.setattr(
        review_workflow_module,
        "_build_context",
        lambda root, chapter: {
            "chapter": chapter,
            "quoted": "中文\n'\" & ; | $() `",
            "sections": {"outline": "可信上下文"},
        },
    )
    trusted_sessions = tmp_path / "trusted-host" / ".codex" / "sessions"
    trusted_sessions.mkdir(parents=True)
    monkeypatch.setattr(
        review_workflow_module,
        "_trusted_codex_sessions_root",
        lambda: trusted_sessions.resolve(),
    )
    return project, workspace


def _prepare(project: Path, workspace: Path, *, chapter: int = 1, mode: str = "full") -> dict:
    return prepare_review(
        project,
        chapter=chapter,
        review_mode=mode,
        workspace_root=workspace,
        parent_model="gpt-5.6-sol",
        parent_reasoning_effort="high",
    )


def test_review_binds_prepare_accept_decision_and_range_to_codex_thread_id(
    tmp_path: Path,
    workflow_env: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, workspace = workflow_env
    monkeypatch.delenv("CODEX_THREAD_ID")
    with pytest.raises(ReviewWorkflowError) as exc_info:
        _prepare(project, workspace)
    assert exc_info.value.code == "parent_runtime_unavailable"

    monkeypatch.setenv("CODEX_THREAD_ID", TEST_PARENT_THREAD_ID)
    prepared = _prepare(project, workspace)
    accept_request = _accept_file(tmp_path, prepared, [_review_payload(1, blocking=True)])
    monkeypatch.setenv("CODEX_THREAD_ID", OTHER_PARENT_THREAD_ID)
    with pytest.raises(ReviewWorkflowError) as exc_info:
        accept_review(project, run_id=prepared["run_id"], request_file=accept_request)
    assert exc_info.value.code == "parent_runtime_mismatch"
    assert get_review_run(project, prepared["run_id"])["status"] == "prepared"

    monkeypatch.setenv("CODEX_THREAD_ID", TEST_PARENT_THREAD_ID)
    pending = accept_review(project, run_id=prepared["run_id"], request_file=accept_request)
    decision_request = _decision_file(
        tmp_path,
        pending["decision"],
        _choice_label(pending["decision"], "abandon"),
        run_id=prepared["run_id"],
    )
    monkeypatch.setenv("CODEX_THREAD_ID", OTHER_PARENT_THREAD_ID)
    with pytest.raises(ReviewWorkflowError) as exc_info:
        decide_review(project, run_id=prepared["run_id"], request_file=decision_request)
    assert exc_info.value.code == "parent_runtime_mismatch"
    assert get_review_run(project, prepared["run_id"])["status"] == "awaiting_decision"

    monkeypatch.setenv("CODEX_THREAD_ID", TEST_PARENT_THREAD_ID)
    prepared_range = prepare_review_range(
        project,
        start=2,
        end=2,
        review_mode="full",
        workspace_root=workspace,
        parent_model="gpt-5.6-sol",
        parent_reasoning_effort="high",
    )
    monkeypatch.setenv("CODEX_THREAD_ID", OTHER_PARENT_THREAD_ID)
    with pytest.raises(ReviewWorkflowError) as exc_info:
        resume_review_range(project, range_id=prepared_range["range_id"])
    assert exc_info.value.code == "parent_runtime_mismatch"


def test_prepare_accept_persist_and_resume_without_reviewer_rerun(
    tmp_path: Path,
    workflow_env: tuple[Path, Path],
) -> None:
    project, workspace = workflow_env
    prepared = _prepare(project, workspace)
    request = json.loads(Path(prepared["request_file"]).read_text(encoding="utf-8"))
    assert request["inputs"][0]["path"].endswith("第0001章.md")
    assert request["instructions"]["write_allowed"] is False
    accept_file = _accept_file(tmp_path, prepared, [_review_payload(1)])

    result = accept_review(project, run_id=prepared["run_id"], request_file=accept_file)

    assert result["status"] == "persisted"
    assert result["reviewer_rerun"] is False
    run = get_review_run(project, prepared["run_id"])
    assert run is not None
    assert run["actual_model"] == "gpt-5.6-luna"
    assert run["actual_reasoning_effort"] == "medium"
    assert Path(run["artifacts"]["result"]["path"]).is_file()
    assert Path(run["artifacts"]["metrics"]["path"]).is_file()
    assert Path(run["artifacts"]["report"]["path"]).is_file()
    with sqlite3.connect(project / ".webnovel" / "index.db") as conn:
        notes = conn.execute(
            "SELECT notes FROM review_metrics WHERE start_chapter=1 AND end_chapter=1"
        ).fetchone()[0]
    assert f"run_id={prepared['run_id']}" in notes
    assert resume_review(project, run_id=prepared["run_id"])["status"] == "persisted"


def test_persisted_receipt_repairs_only_missing_artifacts_or_db_without_reviewer(
    tmp_path: Path,
    workflow_env: tuple[Path, Path],
) -> None:
    project, workspace = workflow_env
    prepared = _prepare(project, workspace)
    accept_review(
        project,
        run_id=prepared["run_id"],
        request_file=_accept_file(tmp_path, prepared, [_review_payload(1)]),
    )
    run = get_review_run(project, prepared["run_id"])
    report = Path(run["artifacts"]["report"]["path"])
    report.unlink()

    repaired_report = resume_review(project, run_id=prepared["run_id"])

    assert repaired_report["status"] == "persisted"
    assert repaired_report["reviewer_rerun"] is False
    assert report.is_file()
    with sqlite3.connect(project / ".webnovel" / "index.db") as conn:
        conn.execute("DELETE FROM review_metrics WHERE start_chapter=1 AND end_chapter=1")
        conn.commit()

    repaired_db = resume_review(project, run_id=prepared["run_id"])

    assert repaired_db["status"] == "persisted"
    assert repaired_db["reviewer_rerun"] is False
    with sqlite3.connect(project / ".webnovel" / "index.db") as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM review_metrics WHERE start_chapter=1 AND end_chapter=1"
        ).fetchone()[0] == 1


def test_persisted_receipt_tamper_never_claims_terminal_success(
    tmp_path: Path,
    workflow_env: tuple[Path, Path],
) -> None:
    project, workspace = workflow_env
    prepared = _prepare(project, workspace)
    accept_review(
        project,
        run_id=prepared["run_id"],
        request_file=_accept_file(tmp_path, prepared, [_review_payload(1)]),
    )
    run = get_review_run(project, prepared["run_id"])
    report = Path(run["artifacts"]["report"]["path"])
    report.write_text("tampered", encoding="utf-8")

    resumed = resume_review(project, run_id=prepared["run_id"])

    assert resumed["status"] == "recoverable"
    assert resumed["reviewer_rerun"] is False
    assert get_review_run(project, prepared["run_id"])["status"] == "failed_persistence"


def test_blocking_requires_scoped_report_only_decision_before_persistence(
    tmp_path: Path,
    workflow_env: tuple[Path, Path],
) -> None:
    project, workspace = workflow_env
    prepared = _prepare(project, workspace)
    result = accept_review(
        project,
        run_id=prepared["run_id"],
        request_file=_accept_file(tmp_path, prepared, [_review_payload(1, blocking=True)]),
    )
    assert result["status"] == "awaiting_user"
    assert result["report_written"] is False
    assert result["decision"]["binding_marker"] in format_review_result(result, "text").splitlines()
    assert not (project / "审查报告").exists()
    assert not (project / ".webnovel" / "index.db").exists()
    with pytest.raises(ReviewWorkflowError, match="does not match"):
        decide_review(
            project,
            run_id=prepared["run_id"],
            request_file=_decision_file(
                tmp_path,
                result["decision"],
                _choice_label(result["decision"], "report_only"),
                run_id=prepared["run_id"],
                extra_request_fields={"request_id": f"choice-{'0' * 20}"},
            ),
        )

    decision = result["decision"]
    persisted = decide_review(
        project,
        run_id=prepared["run_id"],
        request_file=_decision_file(
            tmp_path,
            decision,
            _choice_label(decision, "report_only"),
            run_id=prepared["run_id"],
        ),
    )
    assert persisted["status"] == "persisted"
    assert persisted["body_changed"] is False
    selected = get_review_run(project, prepared["run_id"])["decision"]
    assert selected["runtime_receipt"]["parent_model"] == "gpt-5.6-sol"
    assert selected["runtime_receipt"]["parent_reasoning_effort"] == "high"
    assert len(selected["runtime_receipt"]["authorization_prefix_sha256"]) == 64

    ledger_path = project / ".webnovel" / "run_ledger.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    ledger["review"]["runs"][prepared["run_id"]]["decision"]["runtime_receipt"][
        "parent_thread_id"
    ] = OTHER_PARENT_THREAD_ID
    _write_json(ledger_path, ledger)
    with pytest.raises(RunLedgerError, match="reviewer parent task"):
        load_ledger(project, strict=True)


@pytest.mark.parametrize(
    ("receipt_kwargs", "answer", "expected_code"),
    [
        ({"include_answer": False}, "report_only", "decision_answer_missing"),
        ({"next_role": "assistant"}, "report_only", "decision_answer_missing"),
        ({}, "please do whatever is best", "invalid_decision_answer"),
        ({"marker": "WEBNOVEL_REVIEW_DECISION/v1 stale"}, "report_only", "invalid_decision_receipt"),
        ({"parent_model": "gpt-5.6-terra"}, "report_only", "invalid_decision_receipt"),
        ({"parent_effort": "low"}, "report_only", "invalid_decision_receipt"),
        ({"parent_id": OTHER_PARENT_THREAD_ID}, "report_only", "invalid_decision_receipt"),
        ({"as_subagent": True}, "report_only", "invalid_decision_receipt"),
    ],
)
def test_blocking_decision_receipt_failures_are_zero_write(
    tmp_path: Path,
    workflow_env: tuple[Path, Path],
    receipt_kwargs: dict,
    answer: str,
    expected_code: str,
) -> None:
    project, workspace = workflow_env
    prepared = _prepare(project, workspace)
    pending = accept_review(
        project,
        run_id=prepared["run_id"],
        request_file=_accept_file(tmp_path, prepared, [_review_payload(1, blocking=True)]),
    )
    before = (project / "正文" / "第0001章.md").read_bytes()
    request_file = _decision_file(
        tmp_path,
        pending["decision"],
        answer,
        run_id=prepared["run_id"],
        **receipt_kwargs,
    )

    with pytest.raises(ReviewWorkflowError) as exc_info:
        decide_review(project, run_id=prepared["run_id"], request_file=request_file)

    assert exc_info.value.code == expected_code
    run = get_review_run(project, prepared["run_id"])
    assert run["status"] == "awaiting_decision"
    assert run["decision"]["status"] == "awaiting_user"
    assert (project / "正文" / "第0001章.md").read_bytes() == before
    assert not (project / "审查报告").exists()
    assert not (project / ".webnovel" / "index.db").exists()


def test_blocking_decision_rejects_cross_run_attacker_root_and_replay(
    tmp_path: Path,
    workflow_env: tuple[Path, Path],
) -> None:
    project, workspace = workflow_env
    first = _prepare(project, workspace, chapter=1)
    second = _prepare(project, workspace, chapter=2)
    first_pending = accept_review(
        project,
        run_id=first["run_id"],
        request_file=_accept_file(tmp_path / "first", first, [_review_payload(1, blocking=True)]),
    )
    second_pending = accept_review(
        project,
        run_id=second["run_id"],
        request_file=_accept_file(tmp_path / "second", second, [_review_payload(2, blocking=True)]),
    )
    first_receipt = _decision_file(
        tmp_path,
        first_pending["decision"],
        _choice_label(first_pending["decision"], "abandon"),
        run_id=first["run_id"],
    )
    with pytest.raises(ReviewWorkflowError) as exc_info:
        decide_review(project, run_id=second["run_id"], request_file=first_receipt)
    assert exc_info.value.code == "cross_run_decision"
    assert get_review_run(project, second["run_id"])["status"] == "awaiting_decision"

    attacker_sessions = tmp_path / "attacker" / ".codex" / "sessions"
    attacker_sessions.mkdir(parents=True)
    attacker_receipt = _decision_file(
        tmp_path,
        second_pending["decision"],
        _choice_label(second_pending["decision"], "report_only"),
        run_id=second["run_id"],
        sessions_root=attacker_sessions,
    )
    with pytest.raises(ReviewWorkflowError) as exc_info:
        decide_review(project, run_id=second["run_id"], request_file=attacker_receipt)
    assert exc_info.value.code == "untrusted_sessions_root"
    assert get_review_run(project, second["run_id"])["status"] == "awaiting_decision"

    abandoned = decide_review(project, run_id=first["run_id"], request_file=first_receipt)
    assert abandoned["status"] == "abandoned"
    with pytest.raises(ReviewWorkflowError) as exc_info:
        decide_review(project, run_id=first["run_id"], request_file=first_receipt)
    assert exc_info.value.code == "decision_not_pending"


def test_blocking_decision_rejects_parent_rollout_reparse_leaf(
    tmp_path: Path,
    workflow_env: tuple[Path, Path],
) -> None:
    project, workspace = workflow_env
    prepared = _prepare(project, workspace)
    pending = accept_review(
        project,
        run_id=prepared["run_id"],
        request_file=_accept_file(tmp_path, prepared, [_review_payload(1, blocking=True)]),
    )
    request_file = _decision_file(
        tmp_path,
        pending["decision"],
        "report_only",
        run_id=prepared["run_id"],
    )
    request = json.loads(request_file.read_text(encoding="utf-8"))
    rollout = Path(request["runtime"]["rollout_path"])
    link = rollout.with_name(f"linked-{rollout.name}")
    _symlink_or_skip(link, rollout)
    request["runtime"]["rollout_path"] = str(link.absolute())
    _write_json(request_file, request)

    with pytest.raises(ReviewWorkflowError) as exc_info:
        decide_review(project, run_id=prepared["run_id"], request_file=request_file)

    assert exc_info.value.code == "invalid_decision_receipt"
    assert get_review_run(project, prepared["run_id"])["status"] == "awaiting_decision"


@pytest.mark.parametrize(
    ("choice", "expected_status"),
    [("abandon", "abandoned"), ("targeted_fix", "targeted_fix_pending")],
)
def test_blocking_abandon_or_targeted_fix_never_changes_body(
    tmp_path: Path,
    workflow_env: tuple[Path, Path],
    choice: str,
    expected_status: str,
) -> None:
    project, workspace = workflow_env
    prepared = _prepare(project, workspace)
    before = (project / "正文" / "第0001章.md").read_bytes()
    pending = accept_review(
        project,
        run_id=prepared["run_id"],
        request_file=_accept_file(tmp_path, prepared, [_review_payload(1, blocking=True)]),
    )
    result = decide_review(
        project,
        run_id=prepared["run_id"],
        request_file=_decision_file(
            tmp_path,
            pending["decision"],
            _choice_label(pending["decision"], choice),
            run_id=prepared["run_id"],
        ),
    )
    assert result["status"] == expected_status
    assert result["body_changed"] is False
    assert (project / "正文" / "第0001章.md").read_bytes() == before
    assert not (project / "审查报告").exists()


def test_accepted_chapter_targeted_fix_fails_closed(
    tmp_path: Path,
    workflow_env: tuple[Path, Path],
) -> None:
    project, workspace = workflow_env
    prepared = _prepare(project, workspace)
    pending = accept_review(
        project,
        run_id=prepared["run_id"],
        request_file=_accept_file(tmp_path, prepared, [_review_payload(1, blocking=True)]),
    )
    _write_json(
        project / ".story-system" / "commits" / "chapter_001.commit.json",
        {"meta": {"chapter": 1, "status": "accepted"}},
    )
    result = decide_review(
        project,
        run_id=prepared["run_id"],
        request_file=_decision_file(
            tmp_path,
            pending["decision"],
            _choice_label(pending["decision"], "targeted_fix"),
            run_id=prepared["run_id"],
        ),
    )
    assert result["status"] == "blocked"
    assert result["code"] == "accepted_chapter_transaction_required"


def test_invalid_first_response_uses_only_one_serialization_retry(
    tmp_path: Path,
    workflow_env: tuple[Path, Path],
) -> None:
    project, workspace = workflow_env
    prepared = _prepare(project, workspace)
    bad = _review_payload(1)
    bad["issues_count"] = 3
    result = accept_review(
        project,
        run_id=prepared["run_id"],
        request_file=_accept_file(tmp_path, prepared, [bad, _review_payload(1)]),
    )
    assert result["status"] == "persisted"
    run = get_review_run(project, prepared["run_id"])
    assert run is not None
    assert [item["status"] for item in run["attempts"]] == ["invalid", "accepted"]


def test_raw_invalid_json_retry_is_read_from_same_bound_child_rollout(
    tmp_path: Path,
    workflow_env: tuple[Path, Path],
) -> None:
    project, workspace = workflow_env
    prepared = _prepare(project, workspace)
    result = accept_review(
        project,
        run_id=prepared["run_id"],
        request_file=_accept_file(tmp_path, prepared, ["{not-json", _review_payload(1)]),
    )
    assert result["status"] == "persisted"
    run = get_review_run(project, prepared["run_id"])
    assert run is not None
    assert len(run["attempts"]) == 2
    assert run["attempts"][0]["schema_status"].startswith("invalid_json:")
    assert run["attempts"][1]["status"] == "accepted"


def test_second_response_after_valid_first_result_is_rejected(
    tmp_path: Path,
    workflow_env: tuple[Path, Path],
) -> None:
    project, workspace = workflow_env
    prepared = _prepare(project, workspace)
    with pytest.raises(ReviewWorkflowError, match="only after an invalid first response"):
        accept_review(
            project,
            run_id=prepared["run_id"],
            request_file=_accept_file(
                tmp_path,
                prepared,
                [_review_payload(1), _review_payload(1)],
            ),
        )
    run = get_review_run(project, prepared["run_id"])
    assert run is not None
    assert run["status"] == "failed_validation"
    assert run["stages"]["reviewer"]["code"] == "unexpected_reviewer_retry"
    assert run["problems"][-1]["code"] == "unexpected_reviewer_retry"
    assert [item["status"] for item in run["attempts"]] == ["accepted"]


def test_response_item_is_authoritative_and_event_only_is_legacy_fallback(
    tmp_path: Path,
    workflow_env: tuple[Path, Path],
) -> None:
    project, workspace = workflow_env
    first = _prepare(project, workspace, chapter=1)
    first_result = accept_review(
        project,
        run_id=first["run_id"],
        request_file=_accept_file(
            tmp_path / "duplicate",
            first,
            [_review_payload(1)],
            duplicate_event_messages=True,
        ),
    )
    assert first_result["status"] == "persisted"
    assert len(get_review_run(project, first["run_id"])["attempts"]) == 1

    second = _prepare(project, workspace, chapter=2)
    second_result = accept_review(
        project,
        run_id=second["run_id"],
        request_file=_accept_file(
            tmp_path / "event-only",
            second,
            [_review_payload(2)],
            event_only=True,
        ),
    )
    assert second_result["status"] == "persisted"
    assert len(get_review_run(project, second["run_id"])["attempts"]) == 1


def test_more_than_two_bound_response_items_is_rejected(
    tmp_path: Path,
    workflow_env: tuple[Path, Path],
) -> None:
    project, workspace = workflow_env
    prepared = _prepare(project, workspace)
    with pytest.raises(ReviewWorkflowError, match="at most one retry"):
        accept_review(
            project,
            run_id=prepared["run_id"],
            request_file=_accept_file(
                tmp_path,
                prepared,
                [_review_payload(1), _review_payload(1), _review_payload(1)],
            ),
        )
    assert get_review_run(project, prepared["run_id"])["status"] == "failed_validation"


def test_runtime_identity_mismatch_generates_no_report(
    tmp_path: Path,
    workflow_env: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, workspace = workflow_env
    prepared = _prepare(project, workspace)
    with pytest.raises(ReviewWorkflowError, match="model mismatch"):
        accept_review(
            project,
            run_id=prepared["run_id"],
            request_file=_accept_file(
                tmp_path,
                prepared,
                [_review_payload(1)],
                model="gpt-5.6-sol",
            ),
        )
    assert not (project / "审查报告").exists()
    assert get_review_run(project, prepared["run_id"])["status"] == "failed_validation"


def test_metrics_failure_resumes_only_database_stage(
    tmp_path: Path,
    workflow_env: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, workspace = workflow_env
    prepared = _prepare(project, workspace)
    runtime_globals = accept_review.__globals__
    real_save = runtime_globals["_save_metrics"]
    calls = {"count": 0}

    def flaky(root: Path, metrics: dict) -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            raise sqlite3.OperationalError("database is busy")
        real_save(root, metrics)

    monkeypatch.setitem(runtime_globals, "_save_metrics", flaky)
    first = accept_review(
        project,
        run_id=prepared["run_id"],
        request_file=_accept_file(tmp_path, prepared, [_review_payload(1)]),
    )
    assert first["status"] == "recoverable"
    assert first["resume_from"] == "metrics_db"
    second = resume_review(project, run_id=prepared["run_id"])
    assert second["status"] == "persisted"
    assert second["reviewer_rerun"] is False
    assert calls["count"] == 2


def test_fast_schema_and_strict_negative_cases() -> None:
    result = parse_review_output(2, _review_payload(2, mode="fast"), review_mode="fast", strict=True)
    assert result.dimension_results[-1].conclusion == "skipped: fast mode"
    wrong = _review_payload(2, mode="fast")
    wrong["dimension_results"][-1]["conclusion"] = "pass"
    with pytest.raises(ReviewSchemaError, match="skipped conclusions"):
        parse_review_output(2, wrong, review_mode="fast", strict=True)
    extra = _review_payload(2)
    extra["score"] = 100
    with pytest.raises(ReviewSchemaError, match="seven contract fields"):
        parse_review_output(2, extra, review_mode="full", strict=True)


def test_accept_request_rejects_inside_project_bom_and_unknown_fields(tmp_path: Path) -> None:
    project = _make_project(tmp_path, chapters=1)
    base = {
        "schema_version": "webnovel-review-accept-request/v2",
        "run_id": "rv-ch0001-abcdef",
        "chapter": 1,
        "review_mode": "full",
        "runtime": {
            "rollout_path": str((tmp_path / "sessions" / "child.jsonl").resolve()),
            "sessions_root": str((tmp_path / "sessions").resolve()),
            "child_thread_id": "child",
            "parent_thread_id": "parent",
        },
    }
    inside = _write_json(project / "accept.json", base).resolve()
    with pytest.raises(ReviewRequestError, match="outside"):
        load_review_accept_request(inside, project_root=project)
    unknown = dict(base, unexpected=True)
    outside = _write_json(tmp_path / "outside.json", unknown).resolve()
    with pytest.raises(ReviewRequestError, match="unknown fields"):
        load_review_accept_request(outside, project_root=project)
    bom = tmp_path / "bom.json"
    bom.write_bytes(b"\xef\xbb\xbf" + json.dumps(base).encode("utf-8"))
    with pytest.raises(ReviewRequestError, match="without BOM"):
        load_review_accept_request(bom.resolve(), project_root=project)


def test_accept_request_strict_contract_rejects_malformed_payloads(tmp_path: Path) -> None:
    project = _make_project(tmp_path / "project", chapters=1)
    sessions = (tmp_path / "sessions").resolve()
    base = {
        "schema_version": "webnovel-review-accept-request/v2",
        "run_id": "rv-ch0001-abcdef",
        "chapter": 1,
        "review_mode": "full",
        "runtime": {
            "rollout_path": str(sessions / "child.jsonl"),
            "sessions_root": str(sessions),
            "child_thread_id": " child ",
            "parent_thread_id": "parent",
        },
    }

    valid = _write_json(tmp_path / "valid.json", base).resolve()
    loaded = load_review_accept_request(valid, project_root=project)
    assert loaded["runtime"]["child_thread_id"] == "child"
    assert loaded["duration_ms"] == 0

    cases: list[tuple[str, object, str]] = [
        ("top", [], "top level"),
        ("missing", {key: value for key, value in base.items() if key != "runtime"}, "missing fields"),
        ("schema", {**base, "schema_version": "v0"}, "schema_version"),
        ("run-empty", {**base, "run_id": " "}, "run_id must"),
        ("run-nul", {**base, "run_id": "rv-bad\x00"}, "NUL"),
        ("run-shape", {**base, "run_id": "bad"}, "run_id is invalid"),
        ("run-leading-dot", {**base, "run_id": "rv-.hidden"}, "run_id is invalid"),
        ("run-trailing-dot", {**base, "run_id": "rv-hidden."}, "run_id is invalid"),
        ("run-trailing-space", {**base, "run_id": "rv-hidden "}, "run_id is invalid"),
        ("run-reserved", {**base, "run_id": "rv-CON"}, "run_id is invalid"),
        ("run-reserved-extension", {**base, "run_id": "rv-com1.txt"}, "run_id is invalid"),
        ("chapter-bool", {**base, "chapter": True}, "positive integer"),
        ("mode", {**base, "review_mode": "brief"}, "full or fast"),
        ("self-reported-responses", {**base, "responses": [_review_payload(1)]}, "unknown fields"),
        ("runtime-type", {**base, "runtime": []}, "exactly the four"),
        ("runtime-fields", {**base, "runtime": {"rollout_path": "x"}}, "exactly the four"),
        ("duration-bool", {**base, "duration_ms": True}, "non-negative integer"),
        ("duration-negative", {**base, "duration_ms": -1}, "non-negative integer"),
        (
            "rollout-relative",
            {**base, "runtime": {**base["runtime"], "rollout_path": "child.jsonl"}},
            "absolute path",
        ),
        (
            "sessions-empty",
            {**base, "runtime": {**base["runtime"], "sessions_root": ""}},
            "non-empty string",
        ),
        (
            "child-long",
            {**base, "runtime": {**base["runtime"], "child_thread_id": "x" * 129}},
            "too long",
        ),
    ]
    for name, payload, message in cases:
        request = _write_json(tmp_path / f"invalid-{name}.json", payload).resolve()
        with pytest.raises(ReviewRequestError, match=message):
            load_review_accept_request(request, project_root=project)

    duplicate = tmp_path / "invalid-duplicate.json"
    raw_base = json.dumps(base, ensure_ascii=False)
    duplicate.write_text(
        raw_base.replace(
            '"schema_version":',
            '"schema_version":"webnovel-review-accept-request/v2","schema_version":',
            1,
        ),
        encoding="utf-8",
    )
    with pytest.raises(ReviewRequestError, match="duplicate JSON key"):
        load_review_accept_request(duplicate.resolve(), project_root=project)

    relative = Path("relative-review-request.json")
    with pytest.raises(ReviewRequestError, match="absolute path"):
        load_review_accept_request(relative, project_root=project)
    with pytest.raises(ReviewRequestError, match="unavailable"):
        load_review_accept_request((tmp_path / "missing.json").resolve(), project_root=project)


def test_sessions_root_cannot_be_redirected_by_request_or_codex_home(
    tmp_path: Path,
    workflow_env: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, workspace = workflow_env
    prepared = _prepare(project, workspace)
    request_path = _accept_file(tmp_path / "trusted", prepared, [_review_payload(1)])
    request = json.loads(request_path.read_text(encoding="utf-8"))
    trusted = review_workflow_module._trusted_codex_sessions_root()
    attacker = tmp_path / "attacker-codex" / "sessions"
    attacker.mkdir(parents=True)
    fake_rollout = attacker / Path(request["runtime"]["rollout_path"]).name
    fake_rollout.write_bytes(Path(request["runtime"]["rollout_path"]).read_bytes())
    request["runtime"]["sessions_root"] = str(attacker.resolve())
    request["runtime"]["rollout_path"] = str(fake_rollout.resolve())
    _write_json(request_path, request)
    monkeypatch.setenv("CODEX_HOME", str(attacker.parent))

    assert review_workflow_module._trusted_codex_sessions_root() == trusted
    with pytest.raises(ReviewWorkflowError, match="host-owned"):
        accept_review(project, run_id=prepared["run_id"], request_file=request_path)
    assert get_review_run(project, prepared["run_id"])["status"] == "failed_validation"


def test_trusted_sessions_root_rejects_reparse_in_any_ancestor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_codex = tmp_path / "real-codex"
    (real_codex / "sessions").mkdir(parents=True)
    linked_codex = tmp_path / "linked-codex"
    _symlink_or_skip(linked_codex, real_codex)
    monkeypatch.setattr(
        review_workflow_module,
        "TRUSTED_CODEX_SESSIONS_ROOT",
        linked_codex / "sessions",
    )

    with pytest.raises(ReviewWorkflowError) as exc_info:
        review_workflow_module._trusted_codex_sessions_root()

    assert exc_info.value.code == "trusted_sessions_unavailable"


def test_request_artifact_hash_is_revalidated_before_rollout_acceptance(
    tmp_path: Path,
    workflow_env: tuple[Path, Path],
) -> None:
    project, workspace = workflow_env
    prepared = _prepare(project, workspace)
    request_artifact = Path(prepared["request_file"])
    payload = json.loads(request_artifact.read_text(encoding="utf-8"))
    payload["instructions"]["return"] = "tampered"
    _write_json(request_artifact, payload)
    with pytest.raises(ReviewWorkflowError, match="request artifact hash changed"):
        accept_review(
            project,
            run_id=prepared["run_id"],
            request_file=_accept_file(tmp_path, prepared, [_review_payload(1)]),
        )


def test_child_and_rollout_cannot_be_replayed_across_review_runs(
    tmp_path: Path,
    workflow_env: tuple[Path, Path],
) -> None:
    project, workspace = workflow_env
    first = _prepare(project, workspace, chapter=1)
    first_request = _accept_file(tmp_path / "first", first, [_review_payload(1)])
    first_runtime = json.loads(first_request.read_text(encoding="utf-8"))["runtime"]
    assert accept_review(
        project,
        run_id=first["run_id"],
        request_file=first_request,
    )["status"] == "persisted"

    second = _prepare(project, workspace, chapter=2)
    second_request = _accept_file(tmp_path / "second", second, [_review_payload(2)])
    reused_rollout = Path(first_runtime["rollout_path"])
    extra_events = [
        {
            "type": "turn_context",
            "payload": {
                "turn_id": "turn-replay",
                "model": "gpt-5.6-luna",
                "effort": "medium",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": second["binding_marker"]}],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "phase": "final_answer",
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps(_review_payload(2), ensure_ascii=False),
                    }
                ],
            },
        },
    ]
    with reused_rollout.open("a", encoding="utf-8", newline="\n") as handle:
        for event in extra_events:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    request = json.loads(second_request.read_text(encoding="utf-8"))
    request["runtime"] = first_runtime
    _write_json(second_request, request)

    with pytest.raises(ReviewWorkflowError, match="already bound"):
        accept_review(project, run_id=second["run_id"], request_file=second_request)
    assert not (project / "审查报告" / f"第0002章-{second['run_id']}.md").exists()

    empty = tmp_path / "empty.json"
    empty.write_bytes(b"")
    with pytest.raises(ReviewRequestError, match="size must"):
        load_review_accept_request(empty.resolve(), project_root=project)
    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"x" * (review_request_module.MAX_REQUEST_BYTES + 1))
    with pytest.raises(ReviewRequestError, match="size must"):
        load_review_accept_request(oversized.resolve(), project_root=project)
    invalid_utf8 = tmp_path / "invalid-utf8.json"
    invalid_utf8.write_bytes(b"\xff")
    with pytest.raises(ReviewRequestError, match="valid UTF-8"):
        load_review_accept_request(invalid_utf8.resolve(), project_root=project)
    invalid_json = tmp_path / "invalid-json.json"
    invalid_json.write_text("{", encoding="utf-8")
    with pytest.raises(ReviewRequestError, match="one JSON object"):
        load_review_accept_request(invalid_json.resolve(), project_root=project)


def test_prepare_rejects_ambiguous_chapter_and_changed_input(
    tmp_path: Path,
    workflow_env: tuple[Path, Path],
) -> None:
    project, workspace = workflow_env
    duplicate = project / "正文" / "第1卷" / "第001章-重复.md"
    duplicate.parent.mkdir()
    duplicate.write_text("重复", encoding="utf-8")
    with pytest.raises(ReviewWorkflowError, match="more than one"):
        _prepare(project, workspace)
    duplicate.unlink()
    prepared = _prepare(project, workspace)
    (project / "正文" / "第0001章.md").write_text("作者已修改", encoding="utf-8")
    with pytest.raises(ReviewWorkflowError, match="input hash"):
        accept_review(
            project,
            run_id=prepared["run_id"],
            request_file=_accept_file(tmp_path, prepared, [_review_payload(1)]),
        )


def test_range_is_serial_bounded_and_requires_stop_or_continue(
    tmp_path: Path,
    workflow_env: tuple[Path, Path],
) -> None:
    project, workspace = workflow_env
    with pytest.raises(ReviewWorkflowError, match="at most five"):
        prepare_review_range(
            project,
            start=1,
            end=6,
            review_mode="full",
            workspace_root=workspace,
            parent_model="gpt-5.6-sol",
            parent_reasoning_effort="high",
        )
    prepared = prepare_review_range(
        project,
        start=1,
        end=5,
        review_mode="full",
        workspace_root=workspace,
        parent_model="gpt-5.6-sol",
        parent_reasoning_effort="high",
    )
    range_id = prepared["range_id"]
    entry = get_review_range(project, range_id)
    assert entry["run_ids"][1:] == [None, None, None, None]
    first_run = entry["run_ids"][0]
    first_accept = accept_review(
        project,
        run_id=first_run,
        request_file=_accept_file(
            tmp_path / "range-first",
            prepared["current_run"],
            [_review_payload(1, blocking=True)],
        ),
    )
    decide_review(
        project,
        run_id=first_run,
        request_file=_decision_file(
            tmp_path,
            first_accept["decision"],
            _choice_label(first_accept["decision"], "report_only"),
            run_id=first_run,
        ),
    )
    pending = resume_review_range(project, range_id=range_id)
    assert pending["status"] == "awaiting_user"
    continued = decide_review_range(
        project,
        range_id=range_id,
        request_file=_decision_file(
            tmp_path,
            pending["decision"],
            _choice_label(pending["decision"], "continue"),
            range_id=range_id,
        ),
    )
    assert continued["current_chapter"] == 2
    entry = get_review_range(project, range_id)
    assert entry["decision_history"]["0"]["runtime_receipt"]["request_id"] == pending["decision"]["request_id"]
    second_run = entry["run_ids"][1]
    with pytest.raises(ReviewWorkflowError) as exc_info:
        accept_review(
            project,
            run_id=second_run,
            request_file=_accept_file(
                tmp_path / "range-second",
                continued["current_run"],
                ["not valid reviewer JSON"],
            ),
        )
    assert exc_info.value.code == "invalid_reviewer_json"
    pending = resume_review_range(project, range_id=range_id)
    stopped = decide_review_range(
        project,
        range_id=range_id,
        request_file=_decision_file(
            tmp_path,
            pending["decision"],
            _choice_label(pending["decision"], "stop"),
            range_id=range_id,
        ),
    )
    assert stopped["status"] == "stopped"
    assert get_review_range(project, range_id)["run_ids"][2:] == [None, None, None]


def test_range_decision_requires_exact_parent_rollout_receipt(
    tmp_path: Path,
    workflow_env: tuple[Path, Path],
) -> None:
    project, workspace = workflow_env
    prepared = prepare_review_range(
        project,
        start=1,
        end=2,
        review_mode="full",
        workspace_root=workspace,
        parent_model="gpt-5.6-sol",
        parent_reasoning_effort="high",
    )
    range_id = prepared["range_id"]
    first_run = get_review_range(project, range_id)["run_ids"][0]
    accepted = accept_review(
        project,
        run_id=first_run,
        request_file=_accept_file(
            tmp_path,
            prepared["current_run"],
            [_review_payload(1, blocking=True)],
        ),
    )
    decide_review(
        project,
        run_id=first_run,
        request_file=_decision_file(
            tmp_path,
            accepted["decision"],
            _choice_label(accepted["decision"], "report_only"),
            run_id=first_run,
        ),
    )
    pending = resume_review_range(project, range_id=range_id)
    assert pending["status"] == "awaiting_user"
    missing_answer = _decision_file(
        tmp_path,
        pending["decision"],
        _choice_label(pending["decision"], "continue"),
        range_id=range_id,
        include_answer=False,
    )

    with pytest.raises(ReviewWorkflowError) as exc_info:
        decide_review_range(project, range_id=range_id, request_file=missing_answer)

    assert exc_info.value.code == "decision_answer_missing"
    entry = get_review_range(project, range_id)
    assert entry["status"] == "awaiting_decision"
    assert entry["run_ids"][1] is None


def test_range_decision_receipt_cannot_be_replayed_into_another_slot(
    tmp_path: Path,
    workflow_env: tuple[Path, Path],
) -> None:
    project, workspace = workflow_env
    prepared = prepare_review_range(
        project,
        start=1,
        end=2,
        review_mode="full",
        workspace_root=workspace,
        parent_model="gpt-5.6-sol",
        parent_reasoning_effort="high",
    )
    range_id = prepared["range_id"]
    first_run = get_review_range(project, range_id)["run_ids"][0]
    accepted = accept_review(
        project,
        run_id=first_run,
        request_file=_accept_file(
            tmp_path,
            prepared["current_run"],
            [_review_payload(1, blocking=True)],
        ),
    )
    decide_review(
        project,
        run_id=first_run,
        request_file=_decision_file(
            tmp_path,
            accepted["decision"],
            _choice_label(accepted["decision"], "report_only"),
            run_id=first_run,
        ),
    )
    pending = resume_review_range(project, range_id=range_id)
    decide_review_range(
        project,
        range_id=range_id,
        request_file=_decision_file(
            tmp_path,
            pending["decision"],
            _choice_label(pending["decision"], "continue"),
            range_id=range_id,
        ),
    )
    ledger_path = project / ".webnovel" / "run_ledger.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    entry = ledger["review"]["ranges"][range_id]
    entry["decision_history"]["1"] = entry["decision_history"]["0"]
    entry["overrides"]["1"] = "continue"
    _write_json(ledger_path, ledger)
    with pytest.raises(RunLedgerError, match="reviewer parent task"):
        load_ledger(project, strict=True)


@pytest.mark.parametrize(
    ("choice", "expected_status"),
    [("stop", "stopped"), ("continue", "in_progress")],
)
def test_range_failed_persistence_reaches_stop_or_continue_decision(
    tmp_path: Path,
    workflow_env: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    choice: str,
    expected_status: str,
) -> None:
    project, workspace = workflow_env
    prepared = prepare_review_range(
        project,
        start=1,
        end=2,
        review_mode="full",
        workspace_root=workspace,
        parent_model="gpt-5.6-sol",
        parent_reasoning_effort="high",
    )
    range_id = prepared["range_id"]
    run_id = get_review_range(project, range_id)["run_ids"][0]
    monkeypatch.setattr(review_workflow_module, "_db_record_matches", lambda *args: False)
    monkeypatch.setattr(
        review_workflow_module,
        "_save_metrics",
        lambda *args: (_ for _ in ()).throw(sqlite3.OperationalError("database is busy")),
    )
    failed = accept_review(
        project,
        run_id=run_id,
        request_file=_accept_file(tmp_path, prepared["current_run"], [_review_payload(1)]),
    )
    assert failed["status"] == "recoverable"
    assert get_review_run(project, run_id)["status"] == "failed_persistence"

    pending = resume_review_range(project, range_id=range_id)

    assert pending["status"] == "awaiting_user"
    decided = decide_review_range(
        project,
        range_id=range_id,
        request_file=_decision_file(
            tmp_path,
            pending["decision"],
            _choice_label(pending["decision"], choice),
            range_id=range_id,
        ),
    )
    assert decided["status"] == expected_status
    if choice == "continue":
        assert decided["current_chapter"] == 2


def test_ledger_migrates_write_v1_and_adds_review_sections(tmp_path: Path) -> None:
    project = _make_project(tmp_path, chapters=1)
    _write_json(
        project / ".webnovel" / "run_ledger.json",
        {"schema_version": "webnovel-run-ledger/v1", "write": {"chapter_001": {}}},
    )
    ledger = load_ledger(project, strict=True)
    assert ledger["write"]["chapter_001"] == {}
    assert ledger["review"] == {"runs": {}, "ranges": {}}


@pytest.mark.parametrize(
    ("runs", "ranges", "message"),
    [
        ({"rv-CON": {}}, {}, "invalid review run key"),
        ({}, {"rr-lpt1.log": {}}, "invalid review range key"),
        ({"rv-Foo": {}, "rv-foo": {}}, {}, "collide under Windows normalization"),
        ({}, {"rr-Bar": {}, "rr-bar": {}}, "collide under Windows normalization"),
    ],
)
def test_ledger_rejects_reserved_and_windows_colliding_review_ids(
    tmp_path: Path,
    runs: dict[str, object],
    ranges: dict[str, object],
    message: str,
) -> None:
    project = _make_project(tmp_path, chapters=1)
    _write_json(
        project / ".webnovel" / "run_ledger.json",
        {
            "schema_version": "webnovel-run-ledger/v2",
            "write": {},
            "review": {"runs": runs, "ranges": ranges},
        },
    )
    with pytest.raises(RunLedgerError, match=message):
        load_ledger(project, strict=True)


@pytest.mark.parametrize("corruption", ["missing_run", "mismatched_chapter", "duplicate_slot"])
def test_range_ledger_strictly_cross_references_every_run_slot(
    tmp_path: Path,
    workflow_env: tuple[Path, Path],
    corruption: str,
) -> None:
    project, workspace = workflow_env
    prepared = prepare_review_range(
        project,
        start=1,
        end=2,
        review_mode="full",
        workspace_root=workspace,
        parent_model="gpt-5.6-sol",
        parent_reasoning_effort="high",
    )
    ledger_path = project / ".webnovel" / "run_ledger.json"
    payload = json.loads(ledger_path.read_text(encoding="utf-8"))
    entry = payload["review"]["ranges"][prepared["range_id"]]
    first_run_id = entry["run_ids"][0]
    if corruption == "missing_run":
        entry["run_ids"][0] = "rv-missing-cross-reference"
    elif corruption == "mismatched_chapter":
        payload["review"]["runs"][first_run_id]["chapter"] = 2
    else:
        entry["run_ids"][1] = first_run_id
    _write_json(ledger_path, payload)

    with pytest.raises(RunLedgerError, match="missing run|mismatched provenance|attached to both"):
        load_ledger(project, strict=True)


def test_project_chapter_and_managed_path_fail_closed_edges(tmp_path: Path) -> None:
    wf = review_workflow_module
    with pytest.raises(ReviewWorkflowError, match="unavailable"):
        wf._project_root(tmp_path / "missing")
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ReviewWorkflowError, match="state.json"):
        wf._project_root(empty)
    with pytest.raises(ReviewWorkflowError, match="workspace_root is unavailable"):
        wf._workspace_root(tmp_path / "missing-workspace")
    assert wf._inside(tmp_path / "outside", empty) is False
    assert wf._safe_project_path(tmp_path / "outside", empty) is False

    project = _make_project(tmp_path / "valid", chapters=1)
    with pytest.raises(ReviewWorkflowError, match="positive integer"):
        wf._chapter_file(project, True)
    with pytest.raises(ReviewWorkflowError, match="does not exist"):
        wf._chapter_file(project, 2)
    chapter = project / "正文" / "第0001章.md"
    chapter.write_bytes(b"\xef\xbb\xbftext")
    with pytest.raises(ReviewWorkflowError, match="without BOM"):
        wf._chapter_file(project, 1)
    with pytest.raises(ReviewWorkflowError, match="run id"):
        wf._run_dir(project, "../bad")
    with pytest.raises(ReviewWorkflowError, match="range id"):
        wf._range_lock(project, "../bad")

    for run_id in (
        ".",
        "..",
        "rv-foo.",
        "rv-.x",
        "rv-CON",
        "rv-con.txt",
        "CON",
        "con.txt",
    ):
        with pytest.raises(ReviewWorkflowError, match="run id"):
            wf._run_dir(project, run_id)
    for range_id in (
        ".",
        "..",
        "rr-foo.",
        "rr-.x",
        "rr-NUL",
        "rr-lpt1.log",
        "NUL",
        "lpt1.log",
    ):
        with pytest.raises(ReviewWorkflowError, match="range id"):
            wf._range_lock(project, range_id)
    assert not (project / ".webnovel" / "tmp" / "review-ranges").exists()
    assert not (project / ".webnovel" / "tmp" / "review-runs").exists()


def test_atomic_artifact_helpers_are_idempotent_and_reject_collisions(tmp_path: Path) -> None:
    wf = review_workflow_module
    json_path = tmp_path / "artifact.json"
    first = wf._write_json_once(json_path, {"中文": "值"})
    second = wf._write_json_once(json_path, {"中文": "值"})
    assert first["sha256"] == second["sha256"]
    with pytest.raises(ReviewWorkflowError, match="content differs"):
        wf._write_json_once(json_path, {"中文": "不同"})
    json_path.write_text("{", encoding="utf-8")
    with pytest.raises(ReviewWorkflowError, match="unreadable"):
        wf._write_json_once(json_path, {"中文": "值"})
    directory = tmp_path / "directory.json"
    directory.mkdir()
    with pytest.raises(ReviewWorkflowError, match="unsafe existing"):
        wf._write_json_once(directory, {})

    text_path = tmp_path / "report.md"
    assert wf._write_text_once(text_path, "报告\n")["exists"] is True
    assert wf._write_text_once(text_path, "报告\n")["exists"] is True
    with pytest.raises(ReviewWorkflowError, match="content differs"):
        wf._write_text_once(text_path, "不同\n")
    text_dir = tmp_path / "report-dir"
    text_dir.mkdir()
    with pytest.raises(ReviewWorkflowError, match="unsafe existing"):
        wf._write_text_once(text_dir, "x")


def test_real_route_and_context_wrappers_validate_shape_and_bounds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wf = review_workflow_module
    step = _route()["steps"][0]
    monkeypatch.setattr(wf, "build_workflow_route", lambda *args, **kwargs: {"steps": [step]})
    monkeypatch.setattr(wf, "validate_route_readiness", lambda *args, **kwargs: {"ready": True})
    assert wf._route(tmp_path, parent_model="parent", parent_reasoning_effort="high")["steps"] == [step]
    monkeypatch.setattr(
        wf,
        "validate_route_readiness",
        lambda *args, **kwargs: {"ready": False, "problems": [{"code": "stale"}]},
    )
    with pytest.raises(ReviewWorkflowError, match="missing or stale"):
        wf._route(tmp_path, parent_model="parent", parent_reasoning_effort="high")
    monkeypatch.setattr(wf, "validate_route_readiness", lambda *args, **kwargs: {"ready": True})
    monkeypatch.setattr(wf, "build_workflow_route", lambda *args, **kwargs: {"steps": []})
    with pytest.raises(ReviewWorkflowError, match="only webnovel_reviewer"):
        wf._route(tmp_path, parent_model="parent", parent_reasoning_effort="high")

    class Pack:
        def __init__(self, payload):
            self.payload = payload

        def to_dict(self):
            return self.payload

    class Adapter:
        payload = {"chapter": 1}

        def __init__(self, config, *, read_only):
            assert read_only is True

        def load_context(self, chapter):
            return Pack(self.payload)

    monkeypatch.setattr(wf, "MemoryContractAdapter", Adapter)
    assert wf._build_context(tmp_path, 1) == {"chapter": 1}
    Adapter.payload = []
    with pytest.raises(ReviewWorkflowError, match="invalid context"):
        wf._build_context(tmp_path, 1)
    Adapter.payload = {"large": "x" * (wf.MAX_CONTEXT_BYTES + 1)}
    with pytest.raises(ReviewWorkflowError, match="exceeds"):
        wf._build_context(tmp_path, 1)


def test_prepare_rejects_invalid_scope_and_concurrent_run(
    workflow_env: tuple[Path, Path],
) -> None:
    project, workspace = workflow_env
    with pytest.raises(ReviewWorkflowError, match="full or fast"):
        prepare_review(
            project,
            chapter=1,
            review_mode="minimal",
            workspace_root=workspace,
            parent_model="parent",
        )
    with pytest.raises(ReviewWorkflowError, match="parent model"):
        prepare_review(
            project,
            chapter=1,
            review_mode="full",
            workspace_root=workspace,
            parent_model="",
        )
    _prepare(project, workspace)
    with pytest.raises(ReviewWorkflowError, match="active review run"):
        _prepare(project, workspace)


def test_prepare_and_internal_ledger_fail_closed_branches(
    workflow_env: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, workspace = workflow_env
    with pytest.raises(ReviewWorkflowError, match="range id"):
        prepare_review(
            project,
            chapter=1,
            review_mode="full",
            workspace_root=workspace,
            parent_model="parent",
            range_id="bad-range",
        )
    monkeypatch.setattr(
        review_workflow_module,
        "validate_protected_state_snapshots",
        lambda *args: {"accepted": False, "changed_paths": [".webnovel/index.db"]},
    )
    with pytest.raises(ReviewWorkflowError, match="changed protected project state"):
        _prepare(project, workspace)

    assert review_workflow_module._active_review_conflict(
        {"review": {"runs": {"ignored": "bad"}}}, 1
    ) is None
    with pytest.raises(ReviewWorkflowError, match="does not exist"):
        _update_run(project, "rv-missing", lambda run: None)
    with pytest.raises(ReviewWorkflowError, match="does not exist"):
        review_workflow_module._update_range(project, "rr-missing", lambda entry: None)


def test_run_input_and_runtime_evidence_fail_closed_branches(
    workflow_env: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project, workspace = workflow_env
    prepared = _prepare(project, workspace)
    run = get_review_run(project, prepared["run_id"])
    assert review_workflow_module._run_input_error(project, {"inputs": {}}) == "missing chapter input signature"
    unsafe = copy_run = json.loads(json.dumps(run))
    copy_run["inputs"]["chapter"]["path"] = "relative.md"
    assert review_workflow_module._run_input_error(project, unsafe) == "unsafe chapter input path"

    monkeypatch.setattr(
        review_workflow_module,
        "parse_rollout_runtime_evidence",
        lambda *args, **kwargs: (_ for _ in ()).throw(review_workflow_module.SmokeEvidenceError("bad trace")),
    )
    accept_path = _accept_file(tmp_path, prepared, [_review_payload(1)])
    runtime = json.loads(accept_path.read_text(encoding="utf-8"))["runtime"]
    with pytest.raises(ReviewWorkflowError, match="bad trace"):
        review_workflow_module._verified_runtime_evidence(
            runtime,
            _route()["steps"][0],
            expected_parent_thread_id=TEST_PARENT_THREAD_ID,
            binding_marker=prepared["binding_marker"],
        )


def test_accept_fails_on_protected_change_route_drift_and_two_bad_responses(
    tmp_path: Path,
    workflow_env: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, workspace = workflow_env
    prepared = _prepare(project, workspace)
    state = project / ".webnovel" / "state.json"
    state.write_text('{"changed": true}', encoding="utf-8")
    with pytest.raises(ReviewWorkflowError, match="protected project state changed"):
        accept_review(
            project,
            run_id=prepared["run_id"],
            request_file=_accept_file(tmp_path, prepared, [_review_payload(1)]),
        )

    project2 = _make_project(tmp_path / "route", chapters=1)
    workspace2 = tmp_path / "route-workspace"
    workspace2.mkdir()
    prepared2 = _prepare(project2, workspace2)
    drifted = _route()
    drifted["steps"][0]["contract_hash"] = "f" * 64
    monkeypatch.setattr(review_workflow_module, "_route", lambda *args, **kwargs: drifted)
    with pytest.raises(ReviewWorkflowError, match="route changed"):
        accept_review(
            project2,
            run_id=prepared2["run_id"],
            request_file=_accept_file(tmp_path / "route", prepared2, [_review_payload(1)]),
        )

    project3 = _make_project(tmp_path / "bad", chapters=1)
    workspace3 = tmp_path / "bad-workspace"
    workspace3.mkdir()
    monkeypatch.setattr(
        review_workflow_module,
        "_route",
        lambda workspace_root, *, parent_model, parent_reasoning_effort: _route(
            parent_model,
            parent_reasoning_effort,
        ),
    )
    prepared3 = _prepare(project3, workspace3)
    bad = _review_payload(1)
    bad["has_blocking"] = 1
    with pytest.raises(ReviewWorkflowError, match="permitted retry"):
        accept_review(
            project3,
            run_id=prepared3["run_id"],
            request_file=_accept_file(tmp_path / "bad", prepared3, [bad, bad]),
        )
    assert len(get_review_run(project3, prepared3["run_id"])["attempts"]) == 2


def test_accept_rejects_request_scope_and_resumes_existing_run(
    tmp_path: Path,
    workflow_env: tuple[Path, Path],
) -> None:
    project, workspace = workflow_env
    prepared = _prepare(project, workspace)
    wrong_run = dict(prepared, run_id="rv-ch0001-other")
    with pytest.raises(ReviewWorkflowError, match="does not match --run-id"):
        accept_review(
            project,
            run_id=prepared["run_id"],
            request_file=_accept_file(tmp_path / "wrong-run", wrong_run, [_review_payload(1)]),
        )

    wrong_scope = _accept_file(tmp_path / "wrong-scope", prepared, [_review_payload(1)])
    payload = json.loads(wrong_scope.read_text(encoding="utf-8"))
    payload["chapter"] = 2
    _write_json(wrong_scope, payload)
    with pytest.raises(ReviewWorkflowError, match="chapter or mode is stale"):
        accept_review(project, run_id=prepared["run_id"], request_file=wrong_scope)

    accepted = accept_review(
        project,
        run_id=prepared["run_id"],
        request_file=_accept_file(tmp_path / "accepted", prepared, [_review_payload(1)]),
    )
    assert accepted["status"] == "persisted"
    repeated = accept_review(
        project,
        run_id=prepared["run_id"],
        request_file=_accept_file(tmp_path / "repeat", prepared, [_review_payload(1)]),
    )
    assert repeated["status"] == "persisted"


def test_accept_records_strict_schema_failure_after_runtime_payload_gate(
    tmp_path: Path,
    workflow_env: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, workspace = workflow_env
    prepared = _prepare(project, workspace)
    monkeypatch.setattr(
        review_workflow_module,
        "validate_agent_payload",
        lambda *args, **kwargs: {"accepted": True, "code": "ok"},
    )
    invalid = _review_payload(1)
    invalid["summary"] = ""
    with pytest.raises(ReviewWorkflowError, match="permitted retry"):
        accept_review(
            project,
            run_id=prepared["run_id"],
            request_file=_accept_file(tmp_path, prepared, [invalid]),
        )
    attempt = get_review_run(project, prepared["run_id"])["attempts"][0]
    assert attempt["status"] == "invalid"
    assert "must not be empty" in attempt["schema_status"]


def test_persistence_artifact_and_readback_failures_are_recoverable(
    tmp_path: Path,
    workflow_env: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, workspace = workflow_env
    prepared = _prepare(project, workspace)
    pending = accept_review(
        project,
        run_id=prepared["run_id"],
        request_file=_accept_file(tmp_path, prepared, [_review_payload(1, blocking=True)]),
    )
    run = get_review_run(project, prepared["run_id"])
    result_path = Path(run["artifacts"]["result"]["path"])
    result_path.write_text("{}", encoding="utf-8")
    recovered = decide_review(
        project,
        run_id=prepared["run_id"],
        request_file=_decision_file(
            tmp_path,
            pending["decision"],
            _choice_label(pending["decision"], "report_only"),
            run_id=prepared["run_id"],
        ),
    )
    assert recovered["status"] == "recoverable"
    assert recovered["resume_from"] == "artifacts"

    project2 = _make_project(tmp_path / "readback", chapters=1)
    workspace2 = tmp_path / "readback-workspace"
    workspace2.mkdir()
    prepared2 = _prepare(project2, workspace2)
    monkeypatch.setattr(review_workflow_module, "_db_record_matches", lambda *args: False)
    monkeypatch.setattr(review_workflow_module, "_save_metrics", lambda *args: None)
    result = accept_review(
        project2,
        run_id=prepared2["run_id"],
        request_file=_accept_file(tmp_path / "readback", prepared2, [_review_payload(1)]),
    )
    assert result["status"] == "recoverable"
    assert result["code"] == "metrics_readback_mismatch"


def test_review_metrics_controlled_recovery_accepts_valid_wal_without_existing_shm(
    tmp_path: Path,
) -> None:
    project = _make_project(tmp_path, chapters=1)
    db = project / ".webnovel" / "index.db"
    wal = Path(f"{db}-wal")
    shm = Path(f"{db}-shm")
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0].casefold() == "wal"
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("CREATE TABLE recovery_probe (value TEXT)")
        conn.execute("INSERT INTO recovery_probe VALUES ('中文')")
        conn.commit()
        wal_bytes = wal.read_bytes()
    finally:
        conn.close()
    wal.write_bytes(wal_bytes)
    shm.unlink(missing_ok=True)
    assert wal.is_file() and not shm.exists()

    review_workflow_module._recover_sqlite_wal_if_needed(project)

    review_workflow_module._validate_sqlite_bundle(project)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT value FROM recovery_probe").fetchone()[0] == "中文"


def test_review_metrics_invalid_wal_without_shm_is_structured_recoverable(
    tmp_path: Path,
) -> None:
    project = _make_project(tmp_path, chapters=1)
    db = project / ".webnovel" / "index.db"
    sqlite3.connect(db).close()
    Path(f"{db}-wal").write_bytes(b"not-a-sqlite-wal")
    assert not Path(f"{db}-shm").exists()

    with pytest.raises(ReviewWorkflowError) as exc_info:
        review_workflow_module._recover_sqlite_wal_if_needed(project)
    assert exc_info.value.code == "sqlite_wal_recovery_failed"


def test_sqlite_database_and_sidecar_reparse_paths_never_touch_external_targets(
    tmp_path: Path,
) -> None:
    project = _make_project(tmp_path / "db-link", chapters=1)
    outside_db = tmp_path / "outside-index.db"
    outside_db.write_bytes(b"outside-db-sentinel")
    db = project / ".webnovel" / "index.db"
    _symlink_or_skip(db, outside_db)
    before = outside_db.read_bytes()
    with pytest.raises(ReviewWorkflowError, match="database path is unsafe"):
        review_workflow_module._validate_sqlite_bundle(project)
    assert outside_db.read_bytes() == before

    db.unlink()
    sqlite3.connect(db).close()
    outside_wal = tmp_path / "outside-index.db-wal"
    outside_wal.write_bytes(b"outside-wal-sentinel")
    wal = Path(f"{db}-wal")
    _symlink_or_skip(wal, outside_wal)
    before_wal = outside_wal.read_bytes()
    with pytest.raises(ReviewWorkflowError, match="wal path is unsafe"):
        review_workflow_module._validate_sqlite_bundle(project)
    assert outside_wal.read_bytes() == before_wal


@pytest.mark.parametrize("suffix", ["", ".lock", ".bak"])
def test_run_ledger_reparse_paths_are_rejected_without_external_write(
    tmp_path: Path,
    suffix: str,
) -> None:
    project = _make_project(tmp_path / f"ledger-{suffix or 'main'}", chapters=1)
    ledger = project / ".webnovel" / "run_ledger.json"
    candidate = Path(f"{ledger}{suffix}")
    outside = tmp_path / f"outside-ledger-{suffix or 'main'}.json"
    outside.write_text('{"sentinel": true}', encoding="utf-8")
    _symlink_or_skip(candidate, outside)
    before = outside.read_bytes()
    with pytest.raises(RunLedgerError, match="unsafe run ledger"):
        load_ledger(project, strict=True)
    assert outside.read_bytes() == before


def test_review_run_and_range_lock_reparse_leaves_are_rejected(
    tmp_path: Path,
    workflow_env: tuple[Path, Path],
) -> None:
    project, workspace = workflow_env
    prepared = _prepare(project, workspace)
    outside = tmp_path / "outside-lock.txt"
    outside.write_text("sentinel", encoding="utf-8")
    before = outside.read_bytes()
    run_lock = project / ".webnovel" / "tmp" / "review-runs" / prepared["run_id"] / ".workflow.lock"
    run_lock.unlink(missing_ok=True)
    _symlink_or_skip(run_lock, outside)
    with pytest.raises(ReviewWorkflowError) as exc_info:
        resume_review(project, run_id=prepared["run_id"])
    assert exc_info.value.code == "unsafe_run_path"
    assert outside.read_bytes() == before

    run_lock.unlink()
    ranged = prepare_review_range(
        project,
        start=2,
        end=3,
        review_mode="full",
        workspace_root=workspace,
        parent_model="gpt-5.6-sol",
        parent_reasoning_effort="high",
    )
    range_lock = project / ".webnovel" / "tmp" / "review-ranges" / f"{ranged['range_id']}.lock"
    range_lock.unlink(missing_ok=True)
    _symlink_or_skip(range_lock, outside)
    with pytest.raises(ReviewWorkflowError) as exc_info:
        resume_review_range(project, range_id=ranged["range_id"])
    assert exc_info.value.code == "unsafe_range_path"
    assert outside.read_bytes() == before


def test_review_lock_leaf_is_revalidated_after_acquire(
    tmp_path: Path,
    workflow_env: tuple[Path, Path],
) -> None:
    project, workspace = workflow_env
    prepared = _prepare(project, workspace)
    wrapper = review_workflow_module._run_lock(project, prepared["run_id"])
    outside = tmp_path / "outside-after-acquire.txt"
    outside.write_text("sentinel", encoding="utf-8")

    class SwapLock:
        def acquire(self) -> None:
            _symlink_or_skip(wrapper.path, outside)

        def release(self) -> None:
            return None

    wrapper._lock = SwapLock()
    with pytest.raises(ReviewWorkflowError) as exc_info:
        wrapper.__enter__()
    assert exc_info.value.code == "unsafe_run_path"
    assert outside.read_text(encoding="utf-8") == "sentinel"


def test_corrupt_review_ledger_entries_raise_structured_errors_not_keyerror(
    tmp_path: Path,
    workflow_env: tuple[Path, Path],
) -> None:
    project, workspace = workflow_env
    prepared = _prepare(project, workspace)
    ledger_path = project / ".webnovel" / "run_ledger.json"
    payload = json.loads(ledger_path.read_text(encoding="utf-8"))
    del payload["review"]["runs"][prepared["run_id"]]["inputs"]
    _write_json(ledger_path, payload)
    with pytest.raises(RunLedgerError, match="missing fields") as exc_info:
        get_review_run(project, prepared["run_id"])
    assert not isinstance(exc_info.value.__cause__, KeyError)


def test_resume_and_decision_invalid_states_fail_closed(
    tmp_path: Path,
    workflow_env: tuple[Path, Path],
) -> None:
    project, workspace = workflow_env
    with pytest.raises(ReviewWorkflowError, match="does not exist"):
        resume_review(project, run_id="rv-missing")
    prepared = _prepare(project, workspace)
    assert resume_review(project, run_id=prepared["run_id"])["status"] == "prepared"
    with pytest.raises(ReviewRequestError, match="exactly the six"):
        decide_review(
            project,
            run_id=prepared["run_id"],
            request_file=_decision_request_stub(
                tmp_path,
                run_id=prepared["run_id"],
                extra_fields={"choice": "freeform"},
            ),
        )
    with pytest.raises(ReviewWorkflowError, match="no pending"):
        decide_review(
            project,
            run_id=prepared["run_id"],
            request_file=_decision_request_stub(tmp_path, run_id=prepared["run_id"]),
        )
    with pytest.raises(ReviewWorkflowError, match="does not exist"):
        decide_review(
            project,
            run_id="rv-missing",
            request_file=_decision_request_stub(tmp_path, run_id="rv-missing"),
        )


def test_artifact_loader_and_persistence_authorization_fail_closed(
    tmp_path: Path,
    workflow_env: tuple[Path, Path],
) -> None:
    project, workspace = workflow_env
    prepared = _prepare(project, workspace)
    run = get_review_run(project, prepared["run_id"])
    with pytest.raises(ReviewWorkflowError, match="missing from ledger"):
        review_workflow_module._load_result_artifact(project, run)
    with pytest.raises(ReviewWorkflowError, match="not authorized"):
        review_workflow_module.persist_review_run(project, run_id=prepared["run_id"])
    with pytest.raises(ReviewWorkflowError, match="does not exist"):
        review_workflow_module.persist_review_run(project, run_id="rv-missing")

    pending = accept_review(
        project,
        run_id=prepared["run_id"],
        request_file=_accept_file(tmp_path, prepared, [_review_payload(1, blocking=True)]),
    )
    with pytest.raises(RunLedgerError, match="trusted decision receipt"):
        _update_run(
            project,
            prepared["run_id"],
            lambda value: value.update({"status": "validated"}),
        )
    with pytest.raises(ReviewWorkflowError, match="not authorized"):
        review_workflow_module.persist_review_run(project, run_id=prepared["run_id"])
    assert pending["status"] == "awaiting_user"


def test_resume_awaiting_decision_and_corrupt_accepted_commit_fail_closed(
    tmp_path: Path,
    workflow_env: tuple[Path, Path],
) -> None:
    project, workspace = workflow_env
    prepared = _prepare(project, workspace)
    pending = accept_review(
        project,
        run_id=prepared["run_id"],
        request_file=_accept_file(tmp_path, prepared, [_review_payload(1, blocking=True)]),
    )
    resumed = resume_review(project, run_id=prepared["run_id"])
    assert resumed["status"] == "awaiting_user"
    commit = project / ".story-system" / "commits" / "chapter_001.commit.json"
    commit.parent.mkdir(parents=True)
    commit.write_text("{", encoding="utf-8")
    result = decide_review(
        project,
        run_id=prepared["run_id"],
        request_file=_decision_file(
            tmp_path,
            pending["decision"],
            _choice_label(pending["decision"], "targeted_fix"),
            run_id=prepared["run_id"],
        ),
    )
    assert result["code"] == "accepted_chapter_transaction_required"


def test_single_chapter_range_completes_and_terminal_resume_is_idempotent(
    tmp_path: Path,
    workflow_env: tuple[Path, Path],
) -> None:
    project, workspace = workflow_env
    with pytest.raises(ReviewWorkflowError, match="positive and ascending"):
        prepare_review_range(
            project,
            start=2,
            end=1,
            review_mode="full",
            workspace_root=workspace,
            parent_model="parent",
        )
    prepared = prepare_review_range(
        project,
        start=1,
        end=1,
        review_mode="full",
        workspace_root=workspace,
        parent_model="gpt-5.6-sol",
        parent_reasoning_effort="high",
    )
    entry = get_review_range(project, prepared["range_id"])
    accept_review(
        project,
        run_id=entry["run_ids"][0],
        request_file=_accept_file(
            tmp_path / "single-range",
            prepared["current_run"],
            [_review_payload(1)],
        ),
    )
    done = resume_review_range(project, range_id=prepared["range_id"])
    assert done["status"] == "completed"
    assert resume_review_range(project, range_id=prepared["range_id"])["status"] == "completed"
    persisted_run = get_review_run(project, entry["run_ids"][0])
    Path(persisted_run["artifacts"]["report"]["path"]).write_text("tampered", encoding="utf-8")
    terminal = resume_review_range(project, range_id=prepared["range_id"])
    assert terminal["status"] == "recoverable"
    assert terminal["code"] == "range_receipt_invalid"
    assert terminal["current_run"]["reviewer_rerun"] is False
    with pytest.raises(ReviewWorkflowError, match="no pending"):
        decide_review_range(
            project,
            range_id=prepared["range_id"],
            request_file=_decision_request_stub(tmp_path, range_id=prepared["range_id"]),
        )


def test_range_resume_atomically_recovers_legacy_orphan_without_self_conflict(
    tmp_path: Path,
    workflow_env: tuple[Path, Path],
) -> None:
    project, workspace = workflow_env
    prepared_range = prepare_review_range(
        project,
        start=1,
        end=2,
        review_mode="full",
        workspace_root=workspace,
        parent_model="gpt-5.6-sol",
        parent_reasoning_effort="high",
    )
    range_id = prepared_range["range_id"]
    first_run_id = prepared_range["current_run"]["run_id"]
    assert accept_review(
        project,
        run_id=first_run_id,
        request_file=_accept_file(
            tmp_path / "orphan-first",
            prepared_range["current_run"],
            [_review_payload(1)],
        ),
    )["status"] == "persisted"
    orphan = _prepare(project, workspace, chapter=2)
    with locked_ledger(project, strict=True) as ledger:
        orphan_run = ledger["review"]["runs"][orphan["run_id"]]
        orphan_run["range_id"] = range_id
        range_entry = ledger["review"]["ranges"][range_id]
        range_entry["current_index"] = 1
        range_entry["run_ids"][1] = None
        range_entry["status"] = "in_progress"

    resumed = resume_review_range(project, range_id=range_id)
    assert resumed["status"] == "in_progress"
    assert resumed["current_run"]["run_id"] == orphan["run_id"]
    assert resumed["current_run"]["orphan_recovered"] is True
    entry = get_review_range(project, range_id)
    assert entry["run_ids"] == [first_run_id, orphan["run_id"]]


def test_range_missing_pause_prepare_failure_and_stale_input_paths(
    tmp_path: Path,
    workflow_env: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, workspace = workflow_env
    with pytest.raises(ReviewWorkflowError, match="does not exist"):
        resume_review_range(project, range_id="rr-missing")
    with pytest.raises(ReviewWorkflowError, match="does not exist"):
        decide_review_range(
            project,
            range_id="rr-missing",
            request_file=_decision_request_stub(tmp_path, range_id="rr-missing"),
        )

    prepared = prepare_review_range(
        project,
        start=1,
        end=2,
        review_mode="full",
        workspace_root=workspace,
        parent_model="gpt-5.6-sol",
        parent_reasoning_effort="high",
    )
    paused = resume_review_range(project, range_id=prepared["range_id"])
    assert paused["status"] == "paused"

    entry = get_review_range(project, prepared["range_id"])
    first_run = entry["run_ids"][0]
    accept_review(
        project,
        run_id=first_run,
        request_file=_accept_file(
            tmp_path / "stale-range",
            prepared["current_run"],
            [_review_payload(1)],
        ),
    )
    (project / "正文" / "第0001章.md").write_text("范围中被修改", encoding="utf-8")
    stale = resume_review_range(project, range_id=prepared["range_id"])
    assert stale["status"] == "awaiting_user"
    assert get_review_run(project, first_run)["status"] == "stale"

    project2 = _make_project(tmp_path / "failed-range", chapters=1)
    workspace2 = tmp_path / "failed-range-workspace"
    workspace2.mkdir()
    monkeypatch.setattr(
        review_workflow_module,
        "_build_context",
        lambda *args: (_ for _ in ()).throw(ReviewWorkflowError("context", "context failed")),
    )
    with pytest.raises(ReviewWorkflowError, match="context failed"):
        prepare_review_range(
            project2,
            start=1,
            end=1,
            review_mode="full",
            workspace_root=workspace2,
            parent_model="parent",
        )
    ledger = load_ledger(project2, strict=True)
    failed_range = next(iter(ledger["review"]["ranges"].values()))
    assert failed_range["status"] == "failed"


def test_format_and_error_payloads_are_stable() -> None:
    wf = review_workflow_module
    decision = {
        "request_id": "review-abc",
        "options": [{"id": "abandon", "label": "放弃"}],
    }
    text = wf.format_review_result(
        {"status": "awaiting_user", "run_id": "rv-test", "chapter": 1, "decision": decision},
        "text",
    )
    assert "decision_request_id: review-abc" in text
    assert "abandon - 放弃" in text
    assert json.loads(wf.format_review_result({"status": "ok"}, "json"))["status"] == "ok"
    assert wf.error_payload(ReviewWorkflowError("code", "message"))["code"] == "code"
    assert wf.error_payload(ReviewRequestError("bad"))["code"] == "invalid_request"
    assert wf.error_payload(RuntimeError("bad"))["code"] == "review_workflow_failed"


@pytest.mark.parametrize(
    ("corruption", "message"),
    [
        ("entry_type", "entry is corrupt"),
        ("missing_field", "missing fields"),
        ("schema", "unsupported schema"),
        ("chapter", "invalid chapter"),
        ("mode", "invalid mode"),
        ("status", "invalid status"),
        ("range_id", "invalid range id"),
        ("hash", "invalid project_root_hash"),
        ("agent", "invalid agent"),
        ("requested_runtime", "invalid requested runtime identity"),
        ("workspace", "invalid workspace root"),
        ("parent_thread", "invalid parent thread id"),
        ("parent_model", "invalid parent model"),
        ("parent_effort", "invalid parent reasoning effort"),
        ("inputs", "invalid inputs"),
        ("input_signature", "invalid input signatures"),
        ("artifacts", "invalid artifacts"),
        ("request_artifact", "invalid request artifact"),
        ("safety_state", "invalid attempts, stages, or safety state"),
    ],
)
def test_strict_ledger_rejects_corrupt_prepare_time_review_truth(
    workflow_env: tuple[Path, Path],
    corruption: str,
    message: str,
) -> None:
    project, workspace = workflow_env
    prepared = _prepare(project, workspace)
    ledger_path = project / ".webnovel" / "run_ledger.json"
    payload = json.loads(ledger_path.read_text(encoding="utf-8"))
    runs = payload["review"]["runs"]
    run = runs[prepared["run_id"]]

    if corruption == "entry_type":
        runs[prepared["run_id"]] = []
    elif corruption == "missing_field":
        run.pop("agent_name")
    elif corruption == "schema":
        run["schema_version"] = "webnovel-review-workflow/v0"
    elif corruption == "chapter":
        run["chapter"] = True
    elif corruption == "mode":
        run["review_mode"] = "summary"
    elif corruption == "status":
        run["status"] = "claimed_success"
    elif corruption == "range_id":
        run["range_id"] = "../other-range"
    elif corruption == "hash":
        run["project_root_hash"] = "not-a-sha256"
    elif corruption == "agent":
        run["agent_name"] = "untrusted_reviewer"
    elif corruption == "requested_runtime":
        run["requested_model"] = "gpt-5.6-sol"
    elif corruption == "workspace":
        run["workspace_root"] = ""
    elif corruption == "parent_thread":
        run["parent_thread_id"] = "not-a-uuid"
    elif corruption == "parent_model":
        run["parent_model"] = ""
    elif corruption == "parent_effort":
        run["parent_reasoning_effort"] = " "
    elif corruption == "inputs":
        run["inputs"] = []
    elif corruption == "input_signature":
        run["inputs"]["chapter"]["sha256"] = "tampered"
    elif corruption == "artifacts":
        run["artifacts"] = []
    elif corruption == "request_artifact":
        run["artifacts"]["request"]["path"] = ""
    elif corruption == "safety_state":
        run["protected_before"] = []
    else:
        raise AssertionError(corruption)

    _write_json(ledger_path, payload)
    with pytest.raises(RunLedgerError, match=message):
        load_ledger(project, strict=True)


@pytest.mark.parametrize(
    ("corruption", "message"),
    [
        ("evidence_type", "invalid runtime evidence"),
        ("evidence_hash", "invalid runtime evidence hash"),
        ("evidence_shape", "malformed runtime evidence"),
        ("evidence_parent", "not from its prepare-time parent task"),
        ("accepted_identity", "incomplete accepted evidence"),
        ("raw_artifact", "invalid raw artifact"),
        ("metrics_artifact", "invalid metrics artifact"),
    ],
)
def test_strict_ledger_rejects_tampered_accepted_evidence_and_artifacts(
    tmp_path: Path,
    workflow_env: tuple[Path, Path],
    corruption: str,
    message: str,
) -> None:
    project, workspace = workflow_env
    prepared = _prepare(project, workspace)
    accept_review(
        project,
        run_id=prepared["run_id"],
        request_file=_accept_file(tmp_path, prepared, [_review_payload(1)]),
    )
    ledger_path = project / ".webnovel" / "run_ledger.json"
    payload = json.loads(ledger_path.read_text(encoding="utf-8"))
    run = payload["review"]["runs"][prepared["run_id"]]

    if corruption == "evidence_type":
        run["runtime_evidence"] = []
    elif corruption == "evidence_hash":
        run["runtime_evidence"]["evidence_sha256"] = "tampered"
    elif corruption == "evidence_shape":
        run["runtime_evidence"]["output_sha256s"] = []
    elif corruption == "evidence_parent":
        run["runtime_evidence"]["parent_thread_id"] = OTHER_PARENT_THREAD_ID
    elif corruption == "accepted_identity":
        run["actual_model"] = "gpt-5.6-sol"
    elif corruption == "raw_artifact":
        run["artifacts"]["raw"]["sha256"] = "tampered"
    elif corruption == "metrics_artifact":
        run["artifacts"]["metrics"]["path"] = ""
    else:
        raise AssertionError(corruption)

    _write_json(ledger_path, payload)
    with pytest.raises(RunLedgerError, match=message):
        load_ledger(project, strict=True)


@pytest.mark.parametrize(
    ("corruption", "message"),
    [
        ("decision_type", "invalid trusted decision receipt"),
        ("receipt_type", "invalid trusted decision receipt"),
        ("receipt_fields", "invalid trusted decision receipt"),
        ("receipt_source", "invalid trusted decision receipt"),
        ("receipt_identity", "invalid trusted decision receipt"),
        ("receipt_hash", "invalid trusted decision receipt"),
        ("request_id", "invalid trusted decision receipt"),
        ("marker_binding", "not bound to its marker"),
        ("reviewer_parent", "not from its reviewer parent task"),
    ],
)
def test_strict_ledger_rejects_tampered_selected_review_receipts(
    tmp_path: Path,
    workflow_env: tuple[Path, Path],
    corruption: str,
    message: str,
) -> None:
    project, workspace = workflow_env
    prepared = _prepare(project, workspace)
    pending = accept_review(
        project,
        run_id=prepared["run_id"],
        request_file=_accept_file(tmp_path, prepared, [_review_payload(1, blocking=True)]),
    )
    decide_review(
        project,
        run_id=prepared["run_id"],
        request_file=_decision_file(
            tmp_path,
            pending["decision"],
            _choice_label(pending["decision"], "report_only"),
            run_id=prepared["run_id"],
        ),
    )
    ledger_path = project / ".webnovel" / "run_ledger.json"
    payload = json.loads(ledger_path.read_text(encoding="utf-8"))
    run = payload["review"]["runs"][prepared["run_id"]]
    decision = run["decision"]

    if corruption == "decision_type":
        run["decision"] = []
    elif corruption == "receipt_type":
        decision["runtime_receipt"] = []
    elif corruption == "receipt_fields":
        decision["runtime_receipt"].pop("evidence_source")
    elif corruption == "receipt_source":
        decision["runtime_receipt"]["evidence_source"] = "request_file"
    elif corruption == "receipt_identity":
        decision["runtime_receipt"]["parent_model"] = ""
    elif corruption == "receipt_hash":
        decision["runtime_receipt"]["answer_sha256"] = "tampered"
    elif corruption == "request_id":
        decision["runtime_receipt"]["request_id"] = "choice-unbound"
    elif corruption == "marker_binding":
        decision["binding_marker"] += " tampered"
    elif corruption == "reviewer_parent":
        decision["runtime_receipt"]["parent_thread_id"] = OTHER_PARENT_THREAD_ID
    else:
        raise AssertionError(corruption)

    _write_json(ledger_path, payload)
    with pytest.raises(RunLedgerError, match=message):
        load_ledger(project, strict=True)


@pytest.mark.parametrize(
    ("corruption", "message"),
    [
        ("entry_type", "entry is corrupt"),
        ("missing_field", "missing fields"),
        ("schema", "unsupported schema"),
        ("status", "invalid status"),
        ("project_hash", "invalid project hash"),
        ("chapters", "invalid chapters or run ids"),
        ("run_ids_length", "invalid chapters or run ids"),
        ("run_id", "invalid run id"),
        ("current_index", "invalid current index"),
        ("mode", "invalid mode"),
        ("workspace", "invalid workspace root"),
        ("parent_thread", "invalid parent thread id"),
        ("parent_model", "invalid parent model"),
        ("parent_effort", "invalid parent reasoning effort"),
        ("recovery_state", "invalid recovery state"),
        ("history_index", "invalid decision history index"),
        ("override", "unreceipted continuation override"),
        ("stopped", "lacks a trusted stop receipt"),
    ],
)
def test_strict_ledger_rejects_corrupt_review_range_truth(
    workflow_env: tuple[Path, Path],
    corruption: str,
    message: str,
) -> None:
    project, workspace = workflow_env
    prepared = prepare_review_range(
        project,
        start=1,
        end=2,
        review_mode="full",
        workspace_root=workspace,
        parent_model="gpt-5.6-sol",
        parent_reasoning_effort="high",
    )
    ledger_path = project / ".webnovel" / "run_ledger.json"
    payload = json.loads(ledger_path.read_text(encoding="utf-8"))
    ranges = payload["review"]["ranges"]
    entry = ranges[prepared["range_id"]]

    if corruption == "entry_type":
        ranges[prepared["range_id"]] = []
    elif corruption == "missing_field":
        entry.pop("review_mode")
    elif corruption == "schema":
        entry["schema_version"] = "webnovel-review-range/v0"
    elif corruption == "status":
        entry["status"] = "claimed_success"
    elif corruption == "project_hash":
        entry["project_root_hash"] = "tampered"
    elif corruption == "chapters":
        entry["chapters"] = [1, 3]
    elif corruption == "run_ids_length":
        entry["run_ids"] = [entry["run_ids"][0]]
    elif corruption == "run_id":
        entry["run_ids"][0] = "../other-run"
    elif corruption == "current_index":
        entry["current_index"] = True
    elif corruption == "mode":
        entry["review_mode"] = "summary"
    elif corruption == "workspace":
        entry["workspace_root"] = ""
    elif corruption == "parent_thread":
        entry["parent_thread_id"] = "not-a-uuid"
    elif corruption == "parent_model":
        entry["parent_model"] = ""
    elif corruption == "parent_effort":
        entry["parent_reasoning_effort"] = " "
    elif corruption == "recovery_state":
        entry["skipped"] = {}
    elif corruption == "history_index":
        entry["decision_history"] = {"00": {}}
    elif corruption == "override":
        entry["overrides"] = {"0": "continue"}
    elif corruption == "stopped":
        entry["status"] = "stopped"
    else:
        raise AssertionError(corruption)

    _write_json(ledger_path, payload)
    with pytest.raises(RunLedgerError, match=message):
        load_ledger(project, strict=True)


@pytest.mark.parametrize(
    ("corruption", "message"),
    [
        ("completed_empty", "empty run slot"),
        ("duplicate_attachment", "is attached to both"),
        ("missing_run", "references missing run"),
        ("provenance", "mismatched provenance"),
        ("missing_range", "references missing range"),
        ("terminal_orphan", "is orphaned from range"),
    ],
)
def test_strict_ledger_rejects_review_range_linkage_corruption(
    workflow_env: tuple[Path, Path],
    corruption: str,
    message: str,
) -> None:
    project, workspace = workflow_env
    prepared = prepare_review_range(
        project,
        start=1,
        end=2,
        review_mode="full",
        workspace_root=workspace,
        parent_model="gpt-5.6-sol",
        parent_reasoning_effort="high",
    )
    ledger_path = project / ".webnovel" / "run_ledger.json"
    payload = json.loads(ledger_path.read_text(encoding="utf-8"))
    range_id = prepared["range_id"]
    entry = payload["review"]["ranges"][range_id]
    run_id = entry["run_ids"][0]
    run = payload["review"]["runs"][run_id]

    if corruption == "completed_empty":
        entry["status"] = "completed"
    elif corruption == "duplicate_attachment":
        entry["run_ids"][1] = run_id
    elif corruption == "missing_run":
        entry["run_ids"][0] = "rv-ch0001-missing"
    elif corruption == "provenance":
        run["workspace_root"] = str(workspace / "other")
    elif corruption == "missing_range":
        payload["review"]["ranges"].pop(range_id)
    elif corruption == "terminal_orphan":
        entry["run_ids"][0] = None
        run["status"] = "failed_validation"
    else:
        raise AssertionError(corruption)

    _write_json(ledger_path, payload)
    with pytest.raises(RunLedgerError, match=message):
        load_ledger(project, strict=True)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            {"schema_version": "webnovel-run-ledger/v0", "write": {}, "review": {}},
            "unsupported run ledger schema",
        ),
        (
            {"schema_version": "webnovel-run-ledger/v2", "write": [], "review": {}},
            "write section must be an object",
        ),
        (
            {"schema_version": "webnovel-run-ledger/v2", "write": {}, "review": []},
            "review section must be an object",
        ),
        (
            {
                "schema_version": "webnovel-run-ledger/v2",
                "write": {},
                "review": {"runs": [], "ranges": {}},
            },
            "review runs/ranges must be objects",
        ),
    ],
)
def test_ledger_schema_sections_fail_closed_but_nonstrict_reads_are_empty(
    tmp_path: Path,
    payload: object,
    message: str,
) -> None:
    project = _make_project(tmp_path, chapters=1)
    _write_json(project / ".webnovel" / "run_ledger.json", payload)

    with pytest.raises(RunLedgerError, match=message):
        load_ledger(project, strict=True)

    recovered = load_ledger(project, strict=False)
    assert recovered["schema_version"] == "webnovel-run-ledger/v2"
    assert recovered["review"] == {"runs": {}, "ranges": {}}


@pytest.mark.parametrize("corruption", ["empty", "bom", "invalid_json", "array", "oversized"])
def test_ledger_bytes_are_bounded_utf8_json_objects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    corruption: str,
) -> None:
    project = _make_project(tmp_path, chapters=1)
    path = project / ".webnovel" / "run_ledger.json"
    if corruption == "empty":
        raw = b""
    elif corruption == "bom":
        raw = b"\xef\xbb\xbf{}"
    elif corruption == "invalid_json":
        raw = b"{not-json"
    elif corruption == "array":
        raw = b"[]"
    elif corruption == "oversized":
        monkeypatch.setattr(run_ledger_module, "MAX_LEDGER_BYTES", 8)
        raw = b"{" + b" " * 16 + b"}"
    else:
        raise AssertionError(corruption)
    path.write_bytes(raw)

    with pytest.raises(RunLedgerError):
        load_ledger(project, strict=True)

    recovered = load_ledger(project, strict=False)
    assert recovered["review"] == {"runs": {}, "ranges": {}}


def test_ledger_storage_root_and_leaf_must_be_real_directories_and_files(tmp_path: Path) -> None:
    missing = tmp_path / "missing-project"
    with pytest.raises(RunLedgerError, match="project root cannot be resolved safely"):
        load_ledger(missing, strict=True)

    no_webnovel = tmp_path / "no-webnovel"
    no_webnovel.mkdir()
    with pytest.raises(RunLedgerError, match="parent must be a real"):
        load_ledger(no_webnovel, strict=True)

    project = _make_project(tmp_path / "leaf", chapters=1)
    path = project / ".webnovel" / "run_ledger.json"
    path.mkdir()
    with pytest.raises(RunLedgerError, match="unsafe run ledger storage path"):
        load_ledger(project, strict=True)


def test_ledger_public_save_get_and_missing_signature_contracts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _make_project(tmp_path, chapters=1)
    empty = {
        "schema_version": "webnovel-run-ledger/v2",
        "write": {},
        "review": {"runs": {}, "ranges": {}},
    }
    expected_path = project / ".webnovel" / "run_ledger.json"
    assert ledger_path(project) == expected_path
    assert save_ledger(project, empty) == expected_path
    assert get_review_run(project, "rv-missing") is None
    assert get_review_range(project, "rr-missing") is None
    with pytest.raises(RunLedgerError, match="invalid review run id"):
        get_review_run(project, "../run")
    with pytest.raises(RunLedgerError, match="invalid review range id"):
        get_review_range(project, "../range")
    assert file_signature(tmp_path / "does-not-exist") == {
        "path": str(tmp_path / "does-not-exist"),
        "exists": False,
    }

    monkeypatch.setattr(
        run_ledger_module,
        "load_ledger",
        lambda *args, **kwargs: {
            "review": {
                "runs": {"rv-corrupt": []},
                "ranges": {"rr-corrupt": []},
            }
        },
    )
    with pytest.raises(RunLedgerError, match="review run entry is corrupt"):
        run_ledger_module.get_review_run(project, "rv-corrupt")
    with pytest.raises(RunLedgerError, match="review range entry is corrupt"):
        run_ledger_module.get_review_range(project, "rr-corrupt")


def test_ledger_lock_revalidates_after_acquire_and_before_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _make_project(tmp_path, chapters=1)
    lock = run_ledger_module._VerifiedLedgerLock(project)
    real_safe = run_ledger_module._safe_ledger_path
    calls = {"count": 0}

    def swapped_after_acquire(root: Path) -> Path:
        calls["count"] += 1
        if calls["count"] == 2:
            raise RunLedgerError("ledger leaf swapped")
        return real_safe(root)

    monkeypatch.setattr(run_ledger_module, "_safe_ledger_path", swapped_after_acquire)
    with pytest.raises(RunLedgerError, match="leaf swapped"):
        lock.__enter__()
    assert not lock._lock.is_locked

    monkeypatch.setattr(run_ledger_module, "_safe_ledger_path", real_safe)
    second = run_ledger_module._VerifiedLedgerLock(project)
    second.__enter__()
    monkeypatch.setattr(
        run_ledger_module,
        "_safe_ledger_path",
        lambda root: (_ for _ in ()).throw(RunLedgerError("ledger leaf swapped on exit")),
    )
    with pytest.raises(RunLedgerError, match="swapped on exit"):
        second.__exit__(None, None, None)
    assert not second._lock.is_locked


def test_ledger_reparse_probe_errors_and_junctions_fail_closed() -> None:
    class JunctionPath:
        @staticmethod
        def is_symlink() -> bool:
            return False

        @staticmethod
        def is_junction() -> bool:
            return True

    class BrokenPath:
        @staticmethod
        def is_symlink() -> bool:
            raise OSError("probe failed")

    assert run_ledger_module._is_reparse(JunctionPath()) is True
    assert run_ledger_module._is_reparse(BrokenPath()) is True


def test_review_json_parser_rejects_duplicate_keys_constants_and_nonobjects() -> None:
    for raw, message in (
        ('{"chapter": 1, "chapter": 2}', "duplicate JSON key"),
        ('{"score": NaN}', "invalid JSON constant"),
        ('[1, 2, 3]', "one JSON object"),
    ):
        with pytest.raises(ValueError, match=message):
            review_workflow_module._strict_json_object(raw)


def test_review_parent_and_filesystem_roots_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEX_THREAD_ID", "{123e4567-e89b-12d3-a456-426614174000}")
    with pytest.raises(ReviewWorkflowError) as exc_info:
        review_workflow_module._current_codex_thread_id()
    assert exc_info.value.code == "parent_runtime_unavailable"

    with pytest.raises(ReviewWorkflowError) as exc_info:
        review_workflow_module._project_root(tmp_path / "missing-project")
    assert exc_info.value.code == "invalid_project_root"
    state_file = _write_json(tmp_path / "state-file.json", {})
    with pytest.raises(ReviewWorkflowError) as exc_info:
        review_workflow_module._project_root(state_file)
    assert exc_info.value.code == "invalid_project_root"

    with pytest.raises(ReviewWorkflowError) as exc_info:
        review_workflow_module._workspace_root(tmp_path / "missing-workspace")
    assert exc_info.value.code == "invalid_workspace_root"
    with pytest.raises(ReviewWorkflowError) as exc_info:
        review_workflow_module._workspace_root(state_file)
    assert exc_info.value.code == "invalid_workspace_root"

    with pytest.raises(ReviewWorkflowError, match="must be absolute"):
        review_workflow_module._reject_reparse_chain(
            Path("relative/sessions"),
            code="trusted_sessions_unavailable",
            label="trusted sessions",
        )

    monkeypatch.setattr(
        review_workflow_module,
        "TRUSTED_CODEX_SESSIONS_ROOT",
        (tmp_path / "missing-sessions").absolute(),
    )
    with pytest.raises(ReviewWorkflowError) as exc_info:
        review_workflow_module._trusted_codex_sessions_root()
    assert exc_info.value.code == "trusted_sessions_unavailable"

    monkeypatch.setattr(
        review_workflow_module,
        "TRUSTED_CODEX_SESSIONS_ROOT",
        state_file.absolute(),
    )
    with pytest.raises(ReviewWorkflowError) as exc_info:
        review_workflow_module._trusted_codex_sessions_root()
    assert exc_info.value.code == "trusted_sessions_unavailable"


def test_review_binding_and_message_parsing_fail_closed() -> None:
    with pytest.raises(ReviewWorkflowError) as exc_info:
        review_workflow_module._binding_marker({"artifacts": {}})
    assert exc_info.value.code == "request_artifact_missing"
    with pytest.raises(ReviewWorkflowError) as exc_info:
        review_workflow_module._binding_marker(
            {"artifacts": {"request": {"path": "relative.json", "sha256": "x"}}}
        )
    assert exc_info.value.code == "request_artifact_invalid"

    assert review_workflow_module._message_text({"type": "event"}) is None
    assert review_workflow_module._message_text(
        {"type": "message", "role": "user", "content": "plain", "phase": "final"}
    ) == ("user", "plain", "final")
    assert review_workflow_module._message_text(
        {"type": "message", "role": "user", "content": 42}
    ) is None
    assert review_workflow_module._message_text(
        {
            "type": "message",
            "role": "assistant",
            "content": [None, {"type": "image", "text": "ignored"}],
        }
    ) is None


def _review_runtime_evidence(path: Path) -> object:
    raw = path.read_bytes() if path.is_file() else b""
    return review_workflow_module.VerifiedRuntimeEvidence(
        evidence_source="codex_trace",
        agent_name="webnovel_reviewer",
        actual_model="gpt-5.6-luna",
        actual_reasoning_effort="medium",
        thread_id="33333333-3333-4333-8333-333333333333",
        parent_thread_id=TEST_PARENT_THREAD_ID,
        raw_sha256=hashlib.sha256(raw).hexdigest(),
    )


@pytest.mark.parametrize(
    ("corruption", "expected_code"),
    [
        ("missing", "invalid_runtime_evidence"),
        ("empty", "invalid_runtime_evidence"),
        ("bom", "invalid_runtime_evidence"),
        ("invalid_json", "invalid_runtime_evidence"),
        ("primitive", "invalid_runtime_evidence"),
        ("unbound", "runtime_request_unbound"),
        ("oversized_output", "invalid_reviewer_json"),
    ],
)
def test_reviewer_rollout_stable_read_rejects_untrusted_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    corruption: str,
    expected_code: str,
) -> None:
    path = tmp_path / "child.jsonl"
    marker = "WEBNOVEL_REVIEW_BINDING/v1 exact"
    binding_event = {
        "type": "response_item",
        "payload": {"type": "message", "role": "user", "content": marker},
    }
    if corruption == "missing":
        pass
    elif corruption == "empty":
        path.write_bytes(b"")
    elif corruption == "bom":
        path.write_bytes(b"\xef\xbb\xbf{}\n")
    elif corruption == "invalid_json":
        path.write_bytes(b"{not-json\n")
    elif corruption == "primitive":
        path.write_text("[]\n", encoding="utf-8")
    elif corruption == "unbound":
        path.write_text(json.dumps({"type": "event_msg", "payload": {}}) + "\n", encoding="utf-8")
    elif corruption == "oversized_output":
        monkeypatch.setattr(review_workflow_module, "MAX_REVIEW_JSON_BYTES", 4)
        output_event = {
            "type": "response_item",
            "payload": {"type": "message", "role": "assistant", "content": "too long"},
        }
        path.write_text(
            "\n".join(json.dumps(event) for event in (binding_event, output_event)) + "\n",
            encoding="utf-8",
        )
    else:
        raise AssertionError(corruption)

    with pytest.raises(ReviewWorkflowError) as exc_info:
        review_workflow_module._extract_bound_reviewer_outputs(
            path,
            evidence=_review_runtime_evidence(path),
            binding_marker=marker,
        )
    assert exc_info.value.code == expected_code


@pytest.mark.parametrize(
    "corruption",
    ["missing", "empty", "hash", "bom", "invalid_json", "primitive"],
)
def test_parent_rollout_stable_read_rejects_untrusted_receipt_bytes(
    tmp_path: Path,
    corruption: str,
) -> None:
    path = tmp_path / "parent.jsonl"
    if corruption == "missing":
        raw = b""
    elif corruption == "empty":
        path.write_bytes(b"")
        raw = b""
    elif corruption == "hash":
        raw = b"{}\n"
        path.write_bytes(raw)
    elif corruption == "bom":
        raw = b"\xef\xbb\xbf{}\n"
        path.write_bytes(raw)
    elif corruption == "invalid_json":
        raw = b"{not-json\n"
        path.write_bytes(raw)
    elif corruption == "primitive":
        raw = b"[]\n"
        path.write_bytes(raw)
    else:
        raise AssertionError(corruption)
    expected_sha = "0" * 64 if corruption == "hash" else hashlib.sha256(raw).hexdigest()

    with pytest.raises(ReviewWorkflowError) as exc_info:
        review_workflow_module._stable_parent_rollout_records(
            path,
            expected_sha256=expected_sha,
        )
    assert exc_info.value.code == "invalid_decision_receipt"


def test_review_persisted_decision_reverification_requires_current_scope() -> None:
    with pytest.raises(ReviewWorkflowError) as exc_info:
        review_workflow_module._reverify_selected_decision(
            {},
            expected_parent_thread_id=TEST_PARENT_THREAD_ID,
            expected_parent_model="gpt-5.6-sol",
            expected_parent_effort="high",
            question_id="blocking_action",
        )
    assert exc_info.value.code == "invalid_decision_receipt"

    with pytest.raises(ReviewWorkflowError) as exc_info:
        review_workflow_module._validate_selected_decision_scope(
            {"run_id": "rv-stale", "status": "selected"},
            {"run_id": "rv-current", "status": "awaiting_user"},
        )
    assert exc_info.value.code == "stale_decision"

    with pytest.raises(ReviewWorkflowError) as exc_info:
        review_workflow_module._decision_parent_thread_id(
            {"parent_thread_id": TEST_PARENT_THREAD_ID, "runtime_evidence": None}
        )
    assert exc_info.value.code == "parent_runtime_unavailable"
    with pytest.raises(ReviewWorkflowError) as exc_info:
        review_workflow_module._verify_run_decision_receipt({"decision": None})
    assert exc_info.value.code == "invalid_decision_receipt"


def test_review_decision_requires_runtime_fields_and_bounded_answer(
    tmp_path: Path,
    workflow_env: tuple[Path, Path],
) -> None:
    project, workspace = workflow_env
    prepared = _prepare(project, workspace)
    pending = accept_review(
        project,
        run_id=prepared["run_id"],
        request_file=_accept_file(tmp_path, prepared, [_review_payload(1, blocking=True)]),
    )
    decision = pending["decision"]

    with pytest.raises(ReviewWorkflowError) as exc_info:
        review_workflow_module._verified_user_choice(
            {},
            decision,
            expected_parent_thread_id="",
            expected_parent_model="",
            expected_parent_effort="",
            question_id="blocking_action",
        )
    assert exc_info.value.code == "parent_runtime_unavailable"
    with pytest.raises(ReviewWorkflowError) as exc_info:
        review_workflow_module._verified_user_choice(
            {},
            {},
            expected_parent_thread_id=TEST_PARENT_THREAD_ID,
            expected_parent_model="gpt-5.6-sol",
            expected_parent_effort="high",
            question_id="blocking_action",
        )
    assert exc_info.value.code == "invalid_decision_receipt"
    with pytest.raises(ReviewWorkflowError) as exc_info:
        review_workflow_module._verified_user_choice(
            {},
            decision,
            expected_parent_thread_id=TEST_PARENT_THREAD_ID,
            expected_parent_model="gpt-5.6-sol",
            expected_parent_effort="high",
            question_id="blocking_action",
        )
    assert exc_info.value.code == "invalid_decision_receipt"

    request = _decision_file(
        tmp_path / "oversized-answer",
        decision,
        "x" * (review_workflow_module.MAX_DECISION_ANSWER_BYTES + 1),
        run_id=prepared["run_id"],
    )
    with pytest.raises(ReviewWorkflowError) as exc_info:
        decide_review(project, run_id=prepared["run_id"], request_file=request)
    assert exc_info.value.code == "invalid_decision_answer"
    assert get_review_run(project, prepared["run_id"])["status"] == "awaiting_decision"


@pytest.mark.parametrize(
    ("corruption", "expected"),
    [
        ("missing_input", "missing chapter input signature"),
        ("unsafe_input", "unsafe chapter input path"),
        ("unreadable_input", "chapter input cannot be read safely"),
        ("input_hash", "chapter input hash changed"),
        ("missing_request", "missing request artifact signature"),
        ("unsafe_request", "unsafe request artifact path"),
        ("unreadable_request", "request artifact cannot be read safely"),
        ("request_hash", "request artifact hash changed"),
        ("request_json", "request artifact is not valid UTF-8 JSON"),
        ("request_object", "request artifact is not a JSON object"),
        ("request_provenance", "request artifact provenance changed"),
        ("request_inputs", "request artifact inputs changed"),
        ("request_input_item", "request artifact inputs changed"),
    ],
)
def test_review_current_input_truth_detects_every_stale_boundary(
    workflow_env: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    corruption: str,
    expected: str,
) -> None:
    project, workspace = workflow_env
    prepared = _prepare(project, workspace)
    run = json.loads(json.dumps(get_review_run(project, prepared["run_id"])))
    request_path = Path(run["artifacts"]["request"]["path"])

    if corruption == "missing_input":
        run["inputs"].pop("chapter")
    elif corruption == "unsafe_input":
        run["inputs"]["chapter"]["path"] = "relative/chapter.md"
    elif corruption == "unreadable_input":
        real_read = review_workflow_module._stable_project_bytes

        def fail_chapter(path: Path, root: Path, *, max_bytes: int) -> bytes:
            if path == Path(run["inputs"]["chapter"]["path"]):
                raise OSError("stable read failed")
            return real_read(path, root, max_bytes=max_bytes)

        monkeypatch.setattr(review_workflow_module, "_stable_project_bytes", fail_chapter)
    elif corruption == "input_hash":
        run["inputs"]["chapter"]["sha256"] = "0" * 64
    elif corruption == "missing_request":
        run["artifacts"].pop("request")
    elif corruption == "unsafe_request":
        run["artifacts"]["request"]["path"] = str(project / "request.json")
    elif corruption == "unreadable_request":
        real_read = review_workflow_module._stable_project_bytes

        def fail_request(path: Path, root: Path, *, max_bytes: int) -> bytes:
            if path == request_path:
                raise OSError("stable read failed")
            return real_read(path, root, max_bytes=max_bytes)

        monkeypatch.setattr(review_workflow_module, "_stable_project_bytes", fail_request)
    elif corruption == "request_hash":
        run["request_sha256"] = "0" * 64
    else:
        request_payload: object
        if corruption == "request_json":
            raw = b"{not-json"
        elif corruption == "request_object":
            raw = b"[]"
        else:
            request_payload = json.loads(request_path.read_text(encoding="utf-8"))
            if corruption == "request_provenance":
                request_payload["chapter"] = 999
            elif corruption == "request_inputs":
                request_payload["inputs"] = []
            elif corruption == "request_input_item":
                request_payload["inputs"][0]["kind"] = "context"
            else:
                raise AssertionError(corruption)
            raw = json.dumps(request_payload, ensure_ascii=False).encode("utf-8")
        request_path.write_bytes(raw)
        digest = hashlib.sha256(raw).hexdigest()
        run["artifacts"]["request"]["sha256"] = digest
        run["request_sha256"] = digest

    assert review_workflow_module._run_input_error(project, run) == expected


@pytest.mark.parametrize(
    ("corruption", "expected_code"),
    [
        ("path", "artifact_out_of_bounds"),
        ("unreadable", "artifact_invalid"),
        ("json", "artifact_invalid"),
        ("schema", "artifact_invalid"),
        ("provenance", "artifact_provenance_mismatch"),
    ],
)
def test_accepted_review_artifact_recovery_revalidates_bytes_and_provenance(
    tmp_path: Path,
    workflow_env: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    corruption: str,
    expected_code: str,
) -> None:
    project, workspace = workflow_env
    prepared = _prepare(project, workspace)
    accept_review(
        project,
        run_id=prepared["run_id"],
        request_file=_accept_file(tmp_path, prepared, [_review_payload(1)]),
    )
    run = json.loads(json.dumps(get_review_run(project, prepared["run_id"])))
    result_path = Path(run["artifacts"]["result"]["path"])

    if corruption == "path":
        run["artifacts"]["result"]["path"] = str(tmp_path / "outside.json")
    elif corruption == "unreadable":
        real_read = review_workflow_module._stable_project_bytes

        def fail_result(path: Path, root: Path, *, max_bytes: int) -> bytes:
            if path == result_path:
                raise OSError("stable read failed")
            return real_read(path, root, max_bytes=max_bytes)

        monkeypatch.setattr(review_workflow_module, "_stable_project_bytes", fail_result)
    else:
        if corruption == "json":
            raw = b"{not-json"
        else:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
            if corruption == "schema":
                payload["schema_version"] = "webnovel-review-artifact/v0"
            elif corruption == "provenance":
                payload["run_id"] = "rv-other"
            else:
                raise AssertionError(corruption)
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        result_path.write_bytes(raw)
        run["artifacts"]["result"]["sha256"] = hashlib.sha256(raw).hexdigest()

    with pytest.raises(ReviewWorkflowError) as exc_info:
        review_workflow_module._load_result_artifact(project, run)
    assert exc_info.value.code == expected_code


def test_signed_review_artifact_requires_signature_and_stable_read(
    tmp_path: Path,
    workflow_env: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _ = workflow_env
    expected = project / ".webnovel" / "missing-artifact.json"
    with pytest.raises(ReviewWorkflowError) as exc_info:
        review_workflow_module._signed_artifact_bytes(
            project,
            None,
            expected_path=expected,
            label="review metrics artifact",
        )
    assert exc_info.value.code == "artifact_missing"

    expected.write_text("{}", encoding="utf-8")
    signature = {"path": str(expected), "sha256": hashlib.sha256(b"{}").hexdigest()}
    monkeypatch.setattr(
        review_workflow_module,
        "_stable_project_bytes",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("stable read failed")),
    )
    with pytest.raises(ReviewWorkflowError) as exc_info:
        review_workflow_module._signed_artifact_bytes(
            project,
            signature,
            expected_path=expected,
            label="review metrics artifact",
        )
    assert exc_info.value.code == "artifact_missing"


@pytest.mark.parametrize(
    ("corruption", "message"),
    [
        ("schema", "decision request schema_version is invalid"),
        ("kind", "decision request kind must be run or range"),
        ("run_scope", "run decision scope is invalid"),
        ("range_scope", "range decision scope is invalid"),
        ("request_id", "decision request_id is invalid"),
        ("runtime", "decision runtime must contain exactly"),
    ],
)
def test_review_decision_request_rejects_ambiguous_scope_and_runtime(
    tmp_path: Path,
    corruption: str,
    message: str,
) -> None:
    project = _make_project(tmp_path / "project", chapters=1)
    sessions = (tmp_path / "sessions").resolve()
    payload = {
        "schema_version": "webnovel-review-decision-request/v1",
        "kind": "run",
        "run_id": "rv-current",
        "range_id": None,
        "request_id": f"choice-{'0' * 20}",
        "runtime": {
            "rollout_path": str(sessions / "parent.jsonl"),
            "sessions_root": str(sessions),
            "parent_thread_id": TEST_PARENT_THREAD_ID,
        },
    }
    if corruption == "schema":
        payload["schema_version"] = "v0"
    elif corruption == "kind":
        payload["kind"] = "project"
    elif corruption == "run_scope":
        payload["range_id"] = "rr-other"
    elif corruption == "range_scope":
        payload.update({"kind": "range", "run_id": "rv-other", "range_id": "rr-current"})
    elif corruption == "request_id":
        payload["request_id"] = "review-unbound"
    elif corruption == "runtime":
        payload["runtime"].pop("parent_thread_id")
    else:
        raise AssertionError(corruption)
    request_file = _write_json(tmp_path / f"{corruption}.json", payload).resolve()

    with pytest.raises(ReviewRequestError, match=message):
        load_review_decision_request(request_file, project_root=project)


def test_persisted_review_core_artifact_corruption_becomes_failed_validation(
    tmp_path: Path,
    workflow_env: tuple[Path, Path],
) -> None:
    project, workspace = workflow_env
    prepared = _prepare(project, workspace)
    accept_review(
        project,
        run_id=prepared["run_id"],
        request_file=_accept_file(tmp_path, prepared, [_review_payload(1)]),
    )
    run = get_review_run(project, prepared["run_id"])
    Path(run["artifacts"]["raw"]["path"]).write_text("tampered reviewer output", encoding="utf-8")

    resumed = resume_review(project, run_id=prepared["run_id"])

    assert resumed["status"] == "failed_validation"
    assert resumed["code"] == "accepted_artifact_invalid"
    assert resumed["reviewer_rerun"] is False
    persisted = get_review_run(project, prepared["run_id"])
    assert persisted["status"] == "failed_validation"
    assert persisted["problems"][-1]["stage"] == "reviewer"


def test_failed_metrics_persistence_rechecks_inputs_before_recovery(
    tmp_path: Path,
    workflow_env: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, workspace = workflow_env
    prepared = _prepare(project, workspace)
    runtime_globals = accept_review.__globals__
    real_save = runtime_globals["_save_metrics"]
    monkeypatch.setitem(
        runtime_globals,
        "_save_metrics",
        lambda *args, **kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("database busy")),
    )
    first = accept_review(
        project,
        run_id=prepared["run_id"],
        request_file=_accept_file(tmp_path, prepared, [_review_payload(1)]),
    )
    assert first["status"] == "recoverable"
    assert first["resume_from"] == "metrics_db"

    monkeypatch.setitem(runtime_globals, "_save_metrics", real_save)
    (project / "正文" / "第0001章.md").write_text("changed after validation", encoding="utf-8")
    resumed = resume_review(project, run_id=prepared["run_id"])

    assert resumed["status"] == "stale"
    assert resumed["code"] == "input_hash_mismatch"
    assert resumed["reviewer_rerun"] is False
    run = get_review_run(project, prepared["run_id"])
    assert run["status"] == "stale"
    assert run["problems"][-1]["stage"] == "artifacts"


@pytest.mark.parametrize("corruption", ["metrics", "report_bom", "report_content"])
def test_persisted_receipt_rejects_resigned_but_inconsistent_derived_artifacts(
    tmp_path: Path,
    workflow_env: tuple[Path, Path],
    corruption: str,
) -> None:
    project, workspace = workflow_env
    prepared = _prepare(project, workspace)
    accept_review(
        project,
        run_id=prepared["run_id"],
        request_file=_accept_file(tmp_path, prepared, [_review_payload(1)]),
    )
    run = json.loads(json.dumps(get_review_run(project, prepared["run_id"])))

    if corruption == "metrics":
        path = Path(run["artifacts"]["metrics"]["path"])
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["timestamp"] = "1999-01-01T00:00:00+00:00"
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        path.write_bytes(raw)
        run["artifacts"]["metrics"]["sha256"] = hashlib.sha256(raw).hexdigest()
    else:
        path = Path(run["artifacts"]["report"]["path"])
        original = path.read_bytes()
        raw = b"\xef\xbb\xbf" + original if corruption == "report_bom" else original + b"\nextra"
        path.write_bytes(raw)
        run["artifacts"]["report"]["sha256"] = hashlib.sha256(raw).hexdigest()

    stage, problem = review_workflow_module._validate_persistence_receipt(project, run)
    assert stage == "artifacts"
    assert problem is not None
    if corruption == "metrics":
        assert "metrics_artifact_mismatch" in problem
    elif corruption == "report_bom":
        assert "UTF-8 BOM" in problem
    else:
        assert "report_artifact_mismatch" in problem


def test_terminal_blocking_review_resume_reverifies_receipt(
    tmp_path: Path,
    workflow_env: tuple[Path, Path],
) -> None:
    project, workspace = workflow_env
    prepared = _prepare(project, workspace)
    pending = accept_review(
        project,
        run_id=prepared["run_id"],
        request_file=_accept_file(tmp_path, prepared, [_review_payload(1, blocking=True)]),
    )
    abandoned = decide_review(
        project,
        run_id=prepared["run_id"],
        request_file=_decision_file(
            tmp_path,
            pending["decision"],
            _choice_label(pending["decision"], "abandon"),
            run_id=prepared["run_id"],
        ),
    )
    assert abandoned["status"] == "abandoned"

    resumed = resume_review(project, run_id=prepared["run_id"])

    assert resumed["status"] == "abandoned"
    assert resumed["reviewer_rerun"] is False


def test_review_prepare_and_resume_missing_scope_errors_are_structured(
    tmp_path: Path,
    workflow_env: tuple[Path, Path],
) -> None:
    project, workspace = workflow_env
    with pytest.raises(ReviewWorkflowError) as exc_info:
        prepare_review(
            project,
            chapter=1,
            review_mode="full",
            workspace_root=workspace,
            parent_model="gpt-5.6-sol",
            range_id="rr-current",
        )
    assert exc_info.value.code == "invalid_range_id"

    with pytest.raises(ReviewWorkflowError) as exc_info:
        prepare_review(
            project,
            chapter=1,
            review_mode="full",
            workspace_root=workspace,
            parent_model="gpt-5.6-sol",
            range_id="rr-missing",
            _range_index=0,
        )
    assert exc_info.value.code == "range_not_found"

    with pytest.raises(ReviewWorkflowError) as exc_info:
        resume_review(project, run_id="rv-missing")
    assert exc_info.value.code == "run_not_found"

    with pytest.raises(ReviewWorkflowError) as exc_info:
        prepare_review_range(
            project,
            start=1,
            end=1,
            review_mode="summary",
            workspace_root=workspace,
            parent_model="gpt-5.6-sol",
        )
    assert exc_info.value.code == "invalid_review_mode"
    with pytest.raises(ReviewWorkflowError) as exc_info:
        prepare_review_range(
            project,
            start=1,
            end=1,
            review_mode="full",
            workspace_root=workspace,
            parent_model="",
        )
    assert exc_info.value.code == "invalid_parent_model"


def test_review_reparse_probes_and_lock_leaves_fail_closed(
    tmp_path: Path,
    workflow_env: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _ = workflow_env

    class SymlinkPath:
        @staticmethod
        def is_symlink() -> bool:
            return True

    class JunctionPath:
        @staticmethod
        def is_symlink() -> bool:
            return False

        @staticmethod
        def is_junction() -> bool:
            return True

    class BrokenPath:
        @staticmethod
        def is_symlink() -> bool:
            raise OSError("probe failed")

    assert review_workflow_module._is_reparse(SymlinkPath()) is True
    assert review_workflow_module._is_reparse(JunctionPath()) is True
    assert review_workflow_module._is_reparse(BrokenPath()) is True

    with pytest.raises(ReviewWorkflowError, match="lock path is unsafe"):
        review_workflow_module._validate_project_lock_path(
            tmp_path / "outside.lock",
            project,
            code="unsafe_run_path",
        )
    directory_leaf = project / ".webnovel" / "tmp" / "directory.lock"
    directory_leaf.mkdir(parents=True)
    with pytest.raises(ReviewWorkflowError, match="regular non-reparse file"):
        review_workflow_module._validate_project_lock_path(
            directory_leaf,
            project,
            code="unsafe_run_path",
        )

    lock_path = project / ".webnovel" / "tmp" / "review-runs" / "swap.lock"
    lock = review_workflow_module._VerifiedProjectFileLock(
        lock_path,
        project,
        code="unsafe_run_path",
    )
    real_validate = review_workflow_module._validate_project_lock_path
    calls = {"count": 0}

    def swapped_after_acquire(path: Path, root: Path, *, code: str) -> None:
        calls["count"] += 1
        if calls["count"] == 2:
            raise ReviewWorkflowError(code, "lock swapped after acquire")
        real_validate(path, root, code=code)

    monkeypatch.setattr(
        review_workflow_module,
        "_validate_project_lock_path",
        swapped_after_acquire,
    )
    with pytest.raises(ReviewWorkflowError, match="swapped after acquire"):
        lock.__enter__()
    assert not lock._lock.is_locked

    monkeypatch.setattr(review_workflow_module, "_validate_project_lock_path", real_validate)
    second = review_workflow_module._VerifiedProjectFileLock(
        project / ".webnovel" / "tmp" / "review-runs" / "exit-swap.lock",
        project,
        code="unsafe_run_path",
    )
    second.__enter__()
    monkeypatch.setattr(
        review_workflow_module,
        "_validate_project_lock_path",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            ReviewWorkflowError("unsafe_run_path", "lock swapped before release")
        ),
    )
    with pytest.raises(ReviewWorkflowError, match="swapped before release"):
        second.__exit__(None, None, None)
    assert not second._lock.is_locked


def test_review_reparse_chain_rejects_symlink_components(
    tmp_path: Path,
    workflow_env: tuple[Path, Path],
) -> None:
    project, _ = workflow_env
    outside = tmp_path / "outside-target"
    outside.mkdir()
    linked = project / "linked-sessions"
    _symlink_or_skip(linked, outside)
    with pytest.raises(ReviewWorkflowError, match="contains a symlink"):
        review_workflow_module._reject_reparse_chain(
            linked / "trace.jsonl",
            code="invalid_runtime_evidence",
            label="review trace",
        )


def test_review_stable_project_read_rejects_size_and_handle_identity_changes(
    workflow_env: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _ = workflow_env
    chapter = project / "正文" / "第0001章.md"
    with pytest.raises(OSError, match="exceeds"):
        review_workflow_module._stable_project_bytes(chapter, project, max_bytes=1)

    real_fstat = review_workflow_module.os.fstat
    calls = {"count": 0}

    class ChangedStat:
        def __init__(self, base: object) -> None:
            self.st_dev = base.st_dev
            self.st_ino = base.st_ino
            self.st_size = base.st_size
            self.st_mtime_ns = base.st_mtime_ns + 1

    def changed_after_read(fd: int) -> object:
        calls["count"] += 1
        current = real_fstat(fd)
        return ChangedStat(current) if calls["count"] == 2 else current

    monkeypatch.setattr(review_workflow_module.os, "fstat", changed_after_read)
    with pytest.raises(OSError, match="changed while it was read"):
        review_workflow_module._stable_project_bytes(
            chapter,
            project,
            max_bytes=review_workflow_module.MAX_REVIEW_INPUT_BYTES,
        )


def test_review_rollout_revalidates_handle_and_parsed_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "WEBNOVEL_REVIEW_BINDING/v1 exact"
    events = [
        {
            "type": "response_item",
            "payload": {"type": "message", "role": "user", "content": marker},
        },
        {
            "type": "response_item",
            "payload": {"type": "message", "role": "assistant", "content": "{}"},
        },
    ]
    path = tmp_path / "child.jsonl"
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
    evidence = _review_runtime_evidence(path)

    bad_hash = review_workflow_module.VerifiedRuntimeEvidence(
        evidence_source=evidence.evidence_source,
        agent_name=evidence.agent_name,
        actual_model=evidence.actual_model,
        actual_reasoning_effort=evidence.actual_reasoning_effort,
        thread_id=evidence.thread_id,
        parent_thread_id=evidence.parent_thread_id,
        raw_sha256="0" * 64,
    )
    with pytest.raises(ReviewWorkflowError, match="changed while evidence was parsed"):
        review_workflow_module._extract_bound_reviewer_outputs(
            path,
            evidence=bad_hash,
            binding_marker=marker,
        )

    real_fstat = review_workflow_module.os.fstat
    calls = {"count": 0}

    class ChangedStat:
        def __init__(self, base: object) -> None:
            self.st_dev = base.st_dev
            self.st_ino = base.st_ino
            self.st_size = base.st_size
            self.st_mtime_ns = base.st_mtime_ns + 1

    def changed_after_read(fd: int) -> object:
        calls["count"] += 1
        current = real_fstat(fd)
        return ChangedStat(current) if calls["count"] == 2 else current

    monkeypatch.setattr(review_workflow_module.os, "fstat", changed_after_read)
    with pytest.raises(ReviewWorkflowError, match="changed while it was read"):
        review_workflow_module._extract_bound_reviewer_outputs(
            path,
            evidence=evidence,
            binding_marker=marker,
        )


def test_trusted_sessions_root_accepts_only_a_real_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = tmp_path / "host" / ".codex" / "sessions"
    sessions.mkdir(parents=True)
    monkeypatch.setattr(review_workflow_module, "TRUSTED_CODEX_SESSIONS_ROOT", sessions.absolute())
    assert review_workflow_module._trusted_codex_sessions_root() == sessions.resolve()
