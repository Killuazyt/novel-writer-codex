from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from data_modules import plan_transaction
from data_modules.plan_transaction import (
    PlanApplyChoiceRequired,
    PlanTransactionError,
    apply_validated_plan,
    build_overwrite_token,
    create_validation_receipt,
    plan_transaction_status,
    record_downstream_stage,
    run_downstream_stage,
)
from data_modules.plan_request import build_plan_request, save_plan_request
from data_modules.tests.plan_test_helpers import (
    append_plan_decision_choice,
    create_bound_validation,
    create_plan_decision_from_choice,
    make_initialized_project,
    make_parent_evidence,
    make_valid_plan,
)


@pytest.mark.parametrize("run_id", [".", ".."])
def test_transaction_rejects_dot_segment_run_id_without_writes(tmp_path, run_id):
    with pytest.raises(PlanTransactionError, match="invalid plan run_id"):
        plan_transaction_status(tmp_path, run_id)

    assert list(tmp_path.iterdir()) == []


def test_validation_receipt_then_atomic_apply(tmp_path, monkeypatch):
    manifest_path, manifest = make_valid_plan(tmp_path)

    validation = create_bound_validation(monkeypatch, tmp_path, manifest_path, manifest)
    parent_rollout = Path(validation["parent_rollout_path"])
    with parent_rollout.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps({"type": "event_msg", "payload": {"message": "later"}}) + "\n")
    applied = apply_validated_plan(tmp_path, manifest_path, validation)

    assert validation["status"] == "validated"
    assert validation["batch_set"]["manifest_sha256"] == validation["manifest_sha256"]
    assert validation["batch_set"]["batch_set_sha256"]
    assert applied["status"] == "applied"
    assert applied["complete"] is False
    for name, spec in manifest["artifacts"].items():
        source = tmp_path / spec["path"]
        target = tmp_path / spec["target"]
        assert target.read_bytes() == source.read_bytes(), name
    status = plan_transaction_status(tmp_path, manifest["run_id"])
    assert status["next_stage"] == "master_outline"
    assert apply_validated_plan(tmp_path, manifest_path, validation) == applied


def test_invalid_plan_writes_no_runtime_receipt_or_facts(tmp_path):
    manifest_path, manifest = make_valid_plan(tmp_path)
    manifest["blockers"] = ["未裁决"]
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    report = create_validation_receipt(tmp_path, manifest_path)

    assert report["ok"] is False
    assert not (tmp_path / ".webnovel" / "plan-runs").exists()
    assert not (tmp_path / "大纲").exists()


def test_valid_plan_without_trusted_parent_evidence_writes_no_receipt(tmp_path):
    manifest_path, manifest = make_valid_plan(tmp_path)
    report = create_validation_receipt(tmp_path, manifest_path)
    assert report["status"] == "blocked"
    assert report["problems"][0]["code"] == "parent_evidence_required"
    assert not (
        tmp_path / ".webnovel" / "plan-runs" / manifest["run_id"] / "validation.json"
    ).exists()


def test_validation_rejects_manifest_alias_outside_fixed_current_run_path(tmp_path, monkeypatch):
    manifest_path, manifest = make_valid_plan(tmp_path, run_id="fixed-manifest-run")
    request_path, evidence_path = make_parent_evidence(
        monkeypatch,
        tmp_path,
        manifest_path,
        manifest,
    )
    alias = (
        tmp_path
        / ".webnovel"
        / "tmp"
        / "plan-runs"
        / "old-run"
        / "copied-manifest.json"
    )
    alias.parent.mkdir(parents=True)
    alias.write_bytes(manifest_path.read_bytes())

    report = create_validation_receipt(
        tmp_path,
        alias,
        request_file=request_path,
        parent_evidence_file=evidence_path,
    )

    assert report["status"] == "blocked"
    assert report["problems"] == [
        {
            "code": "manifest_path_mismatch",
            "expected": str(manifest_path),
        }
    ]
    assert not (
        tmp_path / ".webnovel" / "plan-runs" / manifest["run_id"] / "validation.json"
    ).exists()


def test_parent_rollout_marker_and_trusted_root_are_fail_closed(tmp_path, monkeypatch):
    manifest_path, manifest = make_valid_plan(tmp_path)
    request_path, evidence_path = make_parent_evidence(monkeypatch, tmp_path, manifest_path, manifest)
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    rollout_path = Path(evidence["rollout_path"])
    rollout_path.write_text(
        rollout_path.read_text(encoding="utf-8").replace(
            manifest["content_sha256"], "0" * 64
        ),
        encoding="utf-8",
    )
    rejected = create_validation_receipt(
        tmp_path,
        manifest_path,
        request_file=request_path,
        parent_evidence_file=evidence_path,
    )
    assert rejected["problems"][0]["code"] == "parent_evidence_rejected"
    assert not (
        tmp_path / ".webnovel" / "plan-runs" / manifest["run_id"] / "validation.json"
    ).exists()

    outside = tmp_path / "outside-rollout.jsonl"
    outside.write_bytes(rollout_path.read_bytes())
    evidence["rollout_path"] = str(outside)
    evidence_path.write_text(json.dumps(evidence, ensure_ascii=False), encoding="utf-8")
    rejected = create_validation_receipt(
        tmp_path,
        manifest_path,
        request_file=request_path,
        parent_evidence_file=evidence_path,
    )
    assert "trusted Codex sessions root" in rejected["problems"][0]["detail"]


def test_parent_rollout_rejects_reparse_sessions_root_before_resolve(tmp_path, monkeypatch):
    manifest_path, manifest = make_valid_plan(tmp_path)
    request_path, evidence_path = make_parent_evidence(
        monkeypatch, tmp_path, manifest_path, manifest
    )
    sessions_root = plan_transaction.TRUSTED_CODEX_SESSIONS_ROOT
    real_reparse = plan_transaction._is_reparse_point

    def sessions_root_is_reparse(path):
        candidate = Path(path)
        return candidate == sessions_root or real_reparse(candidate)

    monkeypatch.setattr(plan_transaction, "_is_reparse_point", sessions_root_is_reparse)
    rejected = create_validation_receipt(
        tmp_path,
        manifest_path,
        request_file=request_path,
        parent_evidence_file=evidence_path,
    )

    assert rejected["status"] == "blocked"
    assert "reparse-point parent evidence path" in rejected["problems"][0]["detail"]
    assert not (
        tmp_path / ".webnovel" / "plan-runs" / manifest["run_id"] / "validation.json"
    ).exists()


def test_parent_evidence_rejects_cross_task_codex_thread_id(tmp_path, monkeypatch):
    manifest_path, manifest = make_valid_plan(tmp_path)
    request_path, evidence_path = make_parent_evidence(
        monkeypatch, tmp_path, manifest_path, manifest
    )
    monkeypatch.setenv("CODEX_THREAD_ID", "22222222-2222-4222-8222-222222222222")

    rejected = create_validation_receipt(
        tmp_path,
        manifest_path,
        request_file=request_path,
        parent_evidence_file=evidence_path,
    )

    assert rejected["status"] == "blocked"
    assert "current Codex task" in rejected["problems"][0]["detail"]
    assert not (
        tmp_path / ".webnovel" / "plan-runs" / manifest["run_id"] / "validation.json"
    ).exists()


def test_accepted_batch_fragment_tamper_blocks_parent_validation(tmp_path, monkeypatch):
    manifest_path, manifest = make_valid_plan(tmp_path, run_id="accepted-batch-tamper")
    request_path, evidence_path = make_parent_evidence(
        monkeypatch,
        tmp_path,
        manifest_path,
        manifest,
    )
    fragment = next(
        (tmp_path / ".webnovel" / "tmp" / "plan-runs" / manifest["run_id"] / "batches").glob(
            "batch-*.json"
        )
    )
    fragment.write_text(fragment.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    rejected = create_validation_receipt(
        tmp_path,
        manifest_path,
        request_file=request_path,
        parent_evidence_file=evidence_path,
    )

    assert rejected["status"] == "blocked"
    assert "fragment changed after acceptance" in rejected["problems"][0]["detail"]
    assert not (
        tmp_path / ".webnovel" / "plan-runs" / manifest["run_id"] / "validation.json"
    ).exists()


def test_only_unaccepted_plan_batch_can_be_reworked(tmp_path):
    manifest_path, manifest = make_valid_plan(tmp_path, run_id="batch-resume")
    request = build_plan_request(
        tmp_path,
        volume=1,
        start_chapter=1,
        end_chapter=2,
        parent_model="gpt-5.6-sol",
        parent_reasoning_effort="high",
        batch_size=1,
        run_id=manifest["run_id"],
    )
    request_path = save_plan_request(request)

    def write_fragment(chapter: int, chapters: list[dict]) -> Path:
        path = (
            tmp_path
            / ".webnovel"
            / "tmp"
            / "plan-runs"
            / manifest["run_id"]
            / "batches"
            / f"batch-{chapter:06d}-{chapter:06d}.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": plan_transaction.BATCH_FRAGMENT_SCHEMA,
                    "run_id": manifest["run_id"],
                    "volume": 1,
                    "start_chapter": chapter,
                    "end_chapter": chapter,
                    "chapters": chapters,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return path

    first = write_fragment(1, [manifest["chapters"][0]])
    first_raw = first.read_bytes()
    first_receipt = plan_transaction.accept_plan_batch(tmp_path, request_path, first)
    first.write_text(
        first.read_text(encoding="utf-8").replace("主角确认失踪线索", "篡改已验批次"),
        encoding="utf-8",
    )
    with pytest.raises(PlanTransactionError, match="changed after acceptance"):
        plan_transaction.accept_plan_batch(tmp_path, request_path, first)
    first.write_bytes(first_raw)
    assert plan_transaction.accept_plan_batch(tmp_path, request_path, first) == first_receipt

    second = write_fragment(2, [])
    with pytest.raises(PlanTransactionError, match="exact chapter range"):
        plan_transaction.accept_plan_batch(tmp_path, request_path, second)
    second_receipt = plan_transaction._batch_receipt_path(tmp_path, manifest["run_id"], 2, 2)
    assert not second_receipt.exists()
    with pytest.raises(PlanTransactionError, match="required file is missing"):
        plan_transaction.build_parent_evidence_marker(tmp_path, manifest_path, request_path)

    write_fragment(2, [manifest["chapters"][1]])
    accepted = plan_transaction.accept_plan_batch(tmp_path, request_path, second)
    assert accepted["status"] == "accepted"
    marker = plan_transaction.build_parent_evidence_marker(tmp_path, manifest_path, request_path)
    assert marker.startswith(plan_transaction.PARENT_MARKER_PREFIX)


def test_parent_rollout_parsers_reject_ambiguous_or_incomplete_identity(tmp_path):
    with pytest.raises(PlanTransactionError, match="UTF-8 JSONL"):
        plan_transaction._assistant_parent_markers(b"\xff", run_id="run")
    noisy_events = [
        1,
        {"type": "other"},
        {"type": "response_item", "payload": "bad"},
        {"type": "response_item", "payload": {"type": "message", "role": "user"}},
        {
            "type": "response_item",
            "payload": {"type": "message", "role": "assistant", "content": "bad"},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [1, {"type": "output_text", "text": "ordinary"}],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": plan_transaction.PARENT_MARKER_PREFIX + "{",
                    }
                ],
            },
        },
    ]
    with pytest.raises(PlanTransactionError, match="marker is invalid JSON"):
        plan_transaction._assistant_parent_markers(
            ("\n".join(json.dumps(event) for event in noisy_events) + "\n").encode(),
            run_id="run",
        )

    valid_marker = {"run_id": "run", "value": 1}
    valid_event = {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": plan_transaction.PARENT_MARKER_PREFIX
                    + json.dumps(valid_marker),
                }
            ],
        },
    }
    assert plan_transaction._assistant_parent_markers(
        (json.dumps(valid_event) + "\n").encode(), run_id="run"
    ) == [valid_marker]

    rollout = tmp_path / "rollout-parent-one.jsonl"
    kwargs = {
        "rollout_path": rollout,
        "thread_id": "parent-one",
        "expected_model": "gpt-5.6-sol",
        "expected_effort": "high",
    }
    with pytest.raises(PlanTransactionError, match="filename"):
        plan_transaction._parse_parent_identity(
            b"", **{**kwargs, "rollout_path": tmp_path / "wrong.txt"}
        )
    with pytest.raises(PlanTransactionError, match="must be explicit"):
        plan_transaction._parse_parent_identity(b"", **{**kwargs, "expected_effort": ""})
    with pytest.raises(PlanTransactionError, match="UTF-8 JSONL"):
        plan_transaction._parse_parent_identity(b"\xff", **kwargs)
    with pytest.raises(PlanTransactionError, match="one session_meta"):
        plan_transaction._parse_parent_identity(b"{}\n", **kwargs)

    def raw(*events):
        return ("\n".join(json.dumps(event) for event in events) + "\n").encode()

    session = {
        "type": "session_meta",
        "payload": {"id": "parent-one", "model": "gpt-5.6-sol"},
    }
    with pytest.raises(PlanTransactionError, match="session/turn identity"):
        plan_transaction._parse_parent_identity(raw(session), **kwargs)
    duplicate_turns = [
        {"type": "turn_context", "payload": {"turn_id": "one", "model": "gpt-5.6-sol", "effort": "high"}},
        {"type": "turn_context", "payload": {"turn_id": "one", "model": "gpt-5.6-sol", "effort": "high"}},
    ]
    with pytest.raises(PlanTransactionError, match="duplicated"):
        plan_transaction._parse_parent_identity(raw(session, *duplicate_turns), **kwargs)
    wrong_turn = {
        "type": "turn_context",
        "payload": {"turn_id": "one", "model": "gpt-5.6-luna", "effort": "medium"},
    }
    with pytest.raises(PlanTransactionError, match="conflicting model"):
        plan_transaction._parse_parent_identity(raw(session, wrong_turn), **kwargs)
    good_turn = {
        "type": "turn_context",
        "payload": {"turn_id": "one", "model": "gpt-5.6-sol", "effort": "high"},
    }
    wrong_session = {"type": "session_meta", "payload": {"id": "other"}}
    with pytest.raises(PlanTransactionError, match="session identity mismatch"):
        plan_transaction._parse_parent_identity(raw(wrong_session, good_turn), **kwargs)

    for child_payload in (
        {
            "id": "parent-one",
            "model": "gpt-5.6-sol",
            "parent_thread_id": "root-task",
        },
        {
            "id": "parent-one",
            "model": "gpt-5.6-sol",
            "source": {"subagent": {"thread_spawn": {"parent_thread_id": "root-task"}}},
        },
    ):
        child_session = {"type": "session_meta", "payload": child_payload}
        with pytest.raises(PlanTransactionError, match="top-level Codex task"):
            plan_transaction._parse_parent_identity(raw(child_session, good_turn), **kwargs)


def test_plan_transaction_path_and_bounded_read_safety_matrix(tmp_path, monkeypatch):
    outside = tmp_path.parent / "outside-plan-artifact"
    assert plan_transaction._inside(outside, tmp_path) is False
    assert plan_transaction._is_reparse_point(tmp_path / "missing") is False
    with pytest.raises(PlanTransactionError, match="not a directory"):
        plan_transaction._safe_project_root(tmp_path / "missing")
    with pytest.raises(PlanTransactionError, match="trusted Codex sessions root"):
        plan_transaction._require_trusted_file(tmp_path, outside)
    with pytest.raises(PlanTransactionError, match="missing from"):
        plan_transaction._require_trusted_file(tmp_path, tmp_path / "missing.jsonl")

    present = tmp_path / "present.json"
    present.write_text("{}", encoding="utf-8")
    with pytest.raises(PlanTransactionError, match="fixed run artifact"):
        plan_transaction._require_fixed_path(tmp_path, present, expected=tmp_path / "other.json")
    with pytest.raises(PlanTransactionError, match="outside the project"):
        plan_transaction._require_fixed_path(tmp_path, outside, expected=outside)
    with pytest.raises(PlanTransactionError, match="required file is missing"):
        plan_transaction._require_fixed_path(
            tmp_path, tmp_path / "missing.json", expected=tmp_path / "missing.json"
        )
    directory_target = tmp_path / "directory.json"
    directory_target.mkdir()
    with pytest.raises(PlanTransactionError, match="not a regular file"):
        plan_transaction._require_fixed_path(
            tmp_path, directory_target, expected=directory_target, must_exist=False
        )

    with pytest.raises(PlanTransactionError, match="cannot open bounded"):
        plan_transaction._read_bounded_bytes(tmp_path / "absent.json", max_bytes=10)
    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"0123456789")
    with pytest.raises(PlanTransactionError, match="exceeds size limit"):
        plan_transaction._read_bounded_bytes(oversized, max_bytes=2)
    for name, raw, message in (
        ("bom.json", b"\xef\xbb\xbf{}", "BOM"),
        ("invalid.json", b"{", "invalid UTF-8 JSON"),
        ("list.json", b"[]", "must contain an object"),
    ):
        path = tmp_path / name
        path.write_bytes(raw)
        with pytest.raises(PlanTransactionError, match=message):
            plan_transaction._read_bounded_json(path, max_bytes=100)

    with pytest.raises(PlanTransactionError, match="parent escapes project"):
        plan_transaction._prepare_atomic_json_target(tmp_path, outside / "value.json")
    unsafe_parent = tmp_path / "unsafe-parent"
    unsafe_parent.write_text("file", encoding="utf-8")
    with pytest.raises(PlanTransactionError, match="unsafe JSON parent"):
        plan_transaction._prepare_atomic_json_target(tmp_path, unsafe_parent / "value.json")
    monkeypatch.setattr(plan_transaction, "FileLock", None)
    with pytest.raises(PlanTransactionError, match="filelock is required"):
        plan_transaction._safe_json_write(
            tmp_path, tmp_path / "value.json", {"ok": True}, backup=False
        )


def test_apply_revalidates_hash_bound_receipt(tmp_path, monkeypatch):
    manifest_path, manifest = make_valid_plan(tmp_path)
    validation = create_bound_validation(monkeypatch, tmp_path, manifest_path, manifest)
    outline = tmp_path / manifest["artifacts"]["outline"]["path"]
    outline.write_text(outline.read_text(encoding="utf-8") + "\n篡改", encoding="utf-8")

    with pytest.raises(PlanTransactionError, match="no longer validates"):
        apply_validated_plan(tmp_path, manifest_path, validation)

    assert not (tmp_path / "大纲").exists()


def test_existing_plan_is_fail_closed_until_trusted_user_decision_receipt(tmp_path, monkeypatch):
    manifest_path, manifest = make_valid_plan(tmp_path)
    validation = create_bound_validation(monkeypatch, tmp_path, manifest_path, manifest)
    target = tmp_path / manifest["artifacts"]["beat"]["target"]
    target.parent.mkdir(parents=True)
    target.write_text("作者旧规划", encoding="utf-8")

    with pytest.raises(PlanApplyChoiceRequired) as caught:
        apply_validated_plan(tmp_path, manifest_path, validation)

    decision = caught.value.decision
    assert decision["scope_challenge"].startswith("webnovel-plan-decision:")
    decision_request_path = Path(decision["decision_request_file"])
    assert decision_request_path.is_file()
    assert decision["choice_request"]["authorization"]["write_allowed"] is False
    decision_request = json.loads(decision_request_path.read_text(encoding="utf-8"))
    scope = decision_request["scope"]
    assert scope["run_id"] == manifest["run_id"]
    assert scope["stage"] == "apply"
    assert scope["validation_receipt_sha256"] == validation["receipt_sha256"]
    assert scope["manifest_sha256"] == validation["manifest_sha256"]
    assert scope["parent_thread_id"] == validation["parent_thread_id"]
    assert scope["parent_model"] == validation["parent_model"]
    assert scope["parent_reasoning_effort"] == validation["parent_reasoning_effort"]
    assert scope["conflicts"] == {
        str(target): {
            "before_sha256": plan_transaction.sha256_bytes("作者旧规划".encode("utf-8")),
            "after_sha256": manifest["artifacts"]["beat"]["sha256"],
        }
    }
    assert decision["binding_marker"] == decision_request["binding_marker"]
    assert target.read_text(encoding="utf-8") == "作者旧规划"

    # The old deterministic token remains public scope data and never grants
    # replacement, even when copied back exactly.
    with pytest.raises(PlanApplyChoiceRequired):
        apply_validated_plan(
            tmp_path,
            manifest_path,
            validation,
            overwrite_token=caught.value.token,
    )
    assert target.read_text(encoding="utf-8") == "作者旧规划"

    trusted = create_plan_decision_from_choice(
        tmp_path,
        validation,
        decision,
        "replace",
    )
    applied = apply_validated_plan(
        tmp_path,
        manifest_path,
        validation,
        decision_receipt=trusted["receipt_path"],
    )
    assert target.read_bytes() == (
        tmp_path / manifest["artifacts"]["beat"]["path"]
    ).read_bytes()
    assert applied["overwrite_authorized"] is True
    assert applied["decision_receipt_sha256"] == trusted["receipt_sha256"]
    assert trusted["authorization_prefix_sha256"]
    assert trusted["parent_thread_id"] == validation["parent_thread_id"]
    assert trusted["parent_model"] == validation["parent_model"]
    assert trusted["parent_reasoning_effort"] == validation["parent_reasoning_effort"]

    with Path(validation["parent_rollout_path"]).open(
        "a", encoding="utf-8", newline="\n"
    ) as handle:
        handle.write(json.dumps({"type": "event_msg", "payload": {"message": "later"}}) + "\n")
    status = plan_transaction_status(tmp_path, manifest["run_id"])
    assert status["apply"]["status"] == "applied"
    assert status["next_stage"] == "master_outline"

    rollout_path = Path(validation["parent_rollout_path"])
    rollout_events = [
        json.loads(line)
        for line in rollout_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    for event in reversed(rollout_events):
        payload = event.get("payload") or {}
        if event.get("type") == "response_item" and payload.get("role") == "user":
            payload["content"][0]["text"] = "keep"
            break
    else:  # pragma: no cover - the helper always appends one user decision
        raise AssertionError("decision user answer missing")
    rollout_path.write_text(
        "\n".join(json.dumps(event, ensure_ascii=False) for event in rollout_events) + "\n",
        encoding="utf-8",
    )
    stale = plan_transaction_status(tmp_path, manifest["run_id"])
    assert stale["status"] == "stale"
    assert stale["apply"]["status"] == "stale"
    assert stale["next_stage"] == "apply"
    with pytest.raises(PlanTransactionError, match="trusted overwrite decision"):
        apply_validated_plan(
            tmp_path,
            manifest_path,
            validation,
            decision_receipt=trusted["receipt_path"],
        )


@pytest.mark.parametrize(
    ("answer", "expected_status"),
    [("keep", "kept_existing"), ("cancel", "cancelled")],
)
def test_apply_keep_and_cancel_receipts_write_no_novel_facts(
    tmp_path,
    monkeypatch,
    answer,
    expected_status,
):
    manifest_path, manifest = make_valid_plan(tmp_path, run_id=f"apply-{answer}")
    validation = create_bound_validation(monkeypatch, tmp_path, manifest_path, manifest)
    target = tmp_path / manifest["artifacts"]["outline"]["target"]
    target.parent.mkdir(parents=True)
    target.write_text("作者保留稿", encoding="utf-8")

    with pytest.raises(PlanApplyChoiceRequired) as caught:
        apply_validated_plan(tmp_path, manifest_path, validation)
    before = target.read_bytes()
    receipt = create_plan_decision_from_choice(
        tmp_path,
        validation,
        caught.value.decision,
        answer,
    )
    result = apply_validated_plan(
        tmp_path,
        manifest_path,
        validation,
        decision_receipt=receipt["receipt_path"],
    )

    assert result["status"] == expected_status
    assert result["facts_changed"] is False
    assert target.read_bytes() == before
    assert not (
        tmp_path / ".webnovel" / "plan-runs" / manifest["run_id"] / "apply.json"
    ).exists()
    assert all(
        not (tmp_path / spec["target"]).exists()
        for name, spec in manifest["artifacts"].items()
        if name != "outline"
    )


def test_plan_decision_request_is_stale_when_authored_bytes_change(tmp_path, monkeypatch):
    manifest_path, manifest = make_valid_plan(tmp_path, run_id="decision-stale-bytes")
    validation = create_bound_validation(monkeypatch, tmp_path, manifest_path, manifest)
    target = tmp_path / manifest["artifacts"]["beat"]["target"]
    target.parent.mkdir(parents=True)
    target.write_text("作者版本 A", encoding="utf-8")
    with pytest.raises(PlanApplyChoiceRequired) as caught:
        apply_validated_plan(tmp_path, manifest_path, validation)

    append_plan_decision_choice(validation, caught.value.decision, "replace")
    target.write_text("作者版本 B", encoding="utf-8")
    with pytest.raises(PlanTransactionError, match="stale conflict scope"):
        plan_transaction.create_plan_decision_receipt(
            tmp_path,
            caught.value.decision["decision_request_file"],
        )
    assert target.read_text(encoding="utf-8") == "作者版本 B"


def test_apply_failure_rolls_back_all_targets(tmp_path, monkeypatch):
    manifest_path, manifest = make_valid_plan(tmp_path)
    validation = create_bound_validation(monkeypatch, tmp_path, manifest_path, manifest)
    targets = [tmp_path / spec["target"] for spec in manifest["artifacts"].values()]
    real_write = plan_transaction._atomic_write_bytes
    calls = {"count": 0, "failed": False}

    def fail_once(path, raw):
        calls["count"] += 1
        if calls["count"] == 2 and not calls["failed"]:
            calls["failed"] = True
            raise OSError("injected")
        return real_write(path, raw)

    monkeypatch.setattr(plan_transaction, "_atomic_write_bytes", fail_once)
    with pytest.raises(OSError, match="injected"):
        apply_validated_plan(tmp_path, manifest_path, validation)

    assert all(not path.exists() for path in targets)
    assert not (tmp_path / ".webnovel" / "plan-runs" / manifest["run_id"] / "apply.json").exists()


def test_apply_rollback_accepts_promoted_target_that_already_disappeared(tmp_path, monkeypatch):
    manifest_path, manifest = make_valid_plan(tmp_path, run_id="apply-disappeared-target")
    validation = create_bound_validation(monkeypatch, tmp_path, manifest_path, manifest)
    first_target = tmp_path / manifest["artifacts"]["beat"]["target"]
    real_stable = plan_transaction._stable_artifact_bytes
    injected = {"done": False}

    def disappear_after_promote(path_root, path, *, must_exist, max_bytes=64 * 1024 * 1024):
        if path == first_target and must_exist and path.is_file() and not injected["done"]:
            injected["done"] = True
            path.unlink()
            raise PlanTransactionError("injected promoted target disappearance")
        return real_stable(
            path_root,
            path,
            must_exist=must_exist,
            max_bytes=max_bytes,
        )

    monkeypatch.setattr(plan_transaction, "_stable_artifact_bytes", disappear_after_promote)
    with pytest.raises(PlanTransactionError, match="promoted target disappearance"):
        apply_validated_plan(tmp_path, manifest_path, validation)

    assert injected["done"] is True
    assert all(not (tmp_path / spec["target"]).exists() for spec in manifest["artifacts"].values())
    assert not (
        tmp_path / ".webnovel" / "plan-runs" / manifest["run_id"] / "apply.json"
    ).exists()


def test_safe_json_writer_rechecks_target_after_lock_wait(tmp_path, monkeypatch):
    target = tmp_path / ".webnovel" / "state.json"
    target.parent.mkdir(parents=True)
    target.write_text('{"value": 1}', encoding="utf-8")
    original = target.read_bytes()
    entered = {"value": False}
    real_reparse = plan_transaction._is_reparse_point

    class WaitingLock:
        def __init__(self, path, timeout):
            self.path = Path(path)

        def __enter__(self):
            self.path.touch(exist_ok=True)
            entered["value"] = True
            return self

        def __exit__(self, *_args):
            return False

    def becomes_reparse(path):
        if entered["value"] and Path(path) == target:
            return True
        return real_reparse(Path(path))

    monkeypatch.setattr(plan_transaction, "FileLock", WaitingLock)
    monkeypatch.setattr(plan_transaction, "_is_reparse_point", becomes_reparse)

    with pytest.raises(PlanTransactionError, match="reparse-point"):
        plan_transaction._safe_json_write(tmp_path, target, {"value": 2}, backup=True)

    assert target.read_bytes() == original


def test_apply_rechecks_lock_path_after_acquire(tmp_path, monkeypatch):
    manifest_path, manifest = make_valid_plan(tmp_path, run_id="apply-lock-swap")
    validation = create_bound_validation(monkeypatch, tmp_path, manifest_path, manifest)
    lock_path = tmp_path / ".webnovel" / "plan-runs" / ".volume-000001.lifecycle.lock"
    entered = {"value": False}
    real_reparse = plan_transaction._is_reparse_point

    class SwappedApplyLock:
        def __init__(self, path, timeout):
            self.path = Path(path)

        def __enter__(self):
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.touch(exist_ok=True)
            entered["value"] = True
            return self

        def __exit__(self, *_args):
            return False

    def becomes_reparse(path):
        if entered["value"] and Path(path) == lock_path:
            return True
        return real_reparse(Path(path))

    monkeypatch.setattr(plan_transaction, "FileLock", SwappedApplyLock)
    monkeypatch.setattr(plan_transaction, "_is_reparse_point", becomes_reparse)

    with pytest.raises(PlanTransactionError, match="reparse-point"):
        apply_validated_plan(tmp_path, manifest_path, validation)

    for spec in manifest["artifacts"].values():
        assert not (tmp_path / spec["target"]).exists()


def test_apply_rechecks_authored_target_after_waiting_for_shared_volume_lock(tmp_path, monkeypatch):
    manifest_path, manifest = make_valid_plan(tmp_path, run_id="apply-target-wait")
    validation = create_bound_validation(monkeypatch, tmp_path, manifest_path, manifest)
    target = tmp_path / manifest["artifacts"]["beat"]["target"]
    lock_path = tmp_path / ".webnovel" / "plan-runs" / ".volume-000001.lifecycle.lock"

    class WaitingVolumeLock:
        def __init__(self, path, timeout):
            self.path = Path(path)

        def __enter__(self):
            assert self.path == lock_path
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.touch(exist_ok=True)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("作者在等待锁期间写入的规划", encoding="utf-8")
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(plan_transaction, "FileLock", WaitingVolumeLock)

    with pytest.raises(PlanApplyChoiceRequired):
        apply_validated_plan(tmp_path, manifest_path, validation)

    assert target.read_text(encoding="utf-8") == "作者在等待锁期间写入的规划"
    assert not (tmp_path / ".webnovel" / "plan-runs" / manifest["run_id"] / "apply.json").exists()


def test_apply_rechecks_target_ancestor_reparse_after_shared_lock(tmp_path, monkeypatch):
    manifest_path, manifest = make_valid_plan(tmp_path, run_id="apply-target-reparse")
    validation = create_bound_validation(monkeypatch, tmp_path, manifest_path, manifest)
    target_parent = tmp_path / "大纲"
    target_parent.mkdir()
    entered = {"value": False}
    real_reparse = plan_transaction._is_reparse_point

    class WaitingVolumeLock:
        def __init__(self, path, timeout):
            self.path = Path(path)

        def __enter__(self):
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.touch(exist_ok=True)
            entered["value"] = True
            return self

        def __exit__(self, *_args):
            return False

    def target_parent_becomes_reparse(path):
        candidate = Path(path)
        if entered["value"] and candidate == target_parent:
            return True
        return real_reparse(candidate)

    monkeypatch.setattr(plan_transaction, "FileLock", WaitingVolumeLock)
    monkeypatch.setattr(plan_transaction, "_is_reparse_point", target_parent_becomes_reparse)

    with pytest.raises(PlanTransactionError, match="reparse-point"):
        apply_validated_plan(tmp_path, manifest_path, validation)

    assert list(target_parent.iterdir()) == []


def test_existing_apply_receipt_requires_all_four_current_targets(tmp_path, monkeypatch):
    manifest_path, manifest = make_valid_plan(tmp_path, run_id="apply-empty-targets")
    validation = create_bound_validation(monkeypatch, tmp_path, manifest_path, manifest)
    forged = {
        "schema_version": plan_transaction.APPLY_RECEIPT_SCHEMA,
        "status": "applied",
        "complete": False,
        "created_at": "2026-08-08T00:00:00+00:00",
        "project_root": str(tmp_path.resolve()),
        "run_id": manifest["run_id"],
        "volume": manifest["volume"],
        "validation_receipt_sha256": validation["receipt_sha256"],
        "manifest_sha256": validation["manifest_sha256"],
        "content_sha256": validation["content_sha256"],
        "targets": {},
        "downstream_required": list(plan_transaction.DOWNSTREAM_STAGES),
        "overwrite_authorized": False,
        "decision_receipt_path": None,
        "decision_receipt_sha256": None,
    }
    forged["receipt_sha256"] = plan_transaction._receipt_hash(forged)
    apply_path = tmp_path / ".webnovel" / "plan-runs" / manifest["run_id"] / "apply.json"
    apply_path.write_text(json.dumps(forged, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(PlanTransactionError, match="all promoted plan artifacts"):
        apply_validated_plan(tmp_path, manifest_path, validation)

    for spec in manifest["artifacts"].values():
        assert not (tmp_path / spec["target"]).exists()


def test_master_outline_rechecks_conflict_after_waiting_for_lifecycle_lock(tmp_path, monkeypatch):
    make_initialized_project(tmp_path)
    manifest_path, manifest = make_valid_plan(tmp_path, run_id="master-wait-conflict")
    validation = create_bound_validation(monkeypatch, tmp_path, manifest_path, manifest)
    apply_validated_plan(tmp_path, manifest_path, validation)
    master = tmp_path / "大纲" / "总纲.md"
    lock_path = tmp_path / ".webnovel" / "plan-runs" / ".volume-000001.lifecycle.lock"
    authored_row = "| 2 | 作者续卷 | 3-4 | 作者冲突 | 作者高潮 |\n"

    class WaitingLifecycleLock:
        def __init__(self, path, timeout):
            self.path = Path(path)

        def __enter__(self):
            assert self.path == lock_path
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.touch(exist_ok=True)
            master.write_text(master.read_text(encoding="utf-8") + authored_row, encoding="utf-8")
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(plan_transaction, "FileLock", WaitingLifecycleLock)

    with pytest.raises(plan_transaction.PlanDownstreamChoiceRequired):
        run_downstream_stage(tmp_path, manifest["run_id"], stage="master_outline")

    assert master.read_text(encoding="utf-8").endswith(authored_row)
    assert not (
        tmp_path / ".webnovel" / "plan-runs" / manifest["run_id"] / "master-outline.before"
    ).exists()


def test_master_outline_truth_source_failure_restores_atomic_backup(tmp_path, monkeypatch):
    make_initialized_project(tmp_path)
    manifest_path, manifest = make_valid_plan(tmp_path, run_id="master-rollback")
    validation = create_bound_validation(monkeypatch, tmp_path, manifest_path, manifest)
    apply_validated_plan(tmp_path, manifest_path, validation)
    master = tmp_path / "大纲" / "总纲.md"
    before = master.read_bytes()

    def write_then_fail(*_args, **_kwargs):
        master.write_text("BROKEN AFTER TRUTH-SOURCE WRITE", encoding="utf-8")
        raise plan_transaction.MasterOutlineSyncError("injected master failure")

    monkeypatch.setattr(plan_transaction, "sync_master_outline", write_then_fail)
    with pytest.raises(PlanTransactionError, match="injected master failure"):
        run_downstream_stage(tmp_path, manifest["run_id"], stage="master_outline")

    backup = (
        tmp_path
        / ".webnovel"
        / "plan-runs"
        / manifest["run_id"]
        / "master-outline.before"
    )
    assert master.read_bytes() == before
    assert backup.read_bytes() == before


def test_downstream_stages_retry_in_order_and_complete(tmp_path, monkeypatch):
    make_initialized_project(tmp_path)
    manifest_path, manifest = make_valid_plan(tmp_path)
    validation = create_bound_validation(monkeypatch, tmp_path, manifest_path, manifest)
    apply_validated_plan(tmp_path, manifest_path, validation)
    real_sync = plan_transaction.sync_master_outline
    monkeypatch.setattr(
        plan_transaction,
        "sync_master_outline",
        lambda *args, **kwargs: (_ for _ in ()).throw(PlanTransactionError("disk busy")),
    )
    with pytest.raises(PlanTransactionError, match="disk busy"):
        run_downstream_stage(tmp_path, manifest["run_id"], stage="master_outline")
    with pytest.raises(PlanTransactionError, match="out of order"):
        run_downstream_stage(tmp_path, manifest["run_id"], stage="state")
    monkeypatch.setattr(plan_transaction, "sync_master_outline", real_sync)
    for stage in ("master_outline", "state", "contracts", "prewrite"):
        receipt = run_downstream_stage(tmp_path, manifest["run_id"], stage=stage)
        assert receipt["status"] == "completed"

    status = plan_transaction_status(tmp_path, manifest["run_id"])
    assert status["complete"] is True
    assert status["next_stage"] is None
    assert len(list((tmp_path / ".webnovel" / "plan-runs" / manifest["run_id"]).glob("stage-master_outline-*.json"))) == 2


def test_contract_stage_rolls_back_partial_outputs_and_can_resume(tmp_path, monkeypatch):
    make_initialized_project(tmp_path)
    manifest_path, manifest = make_valid_plan(tmp_path, run_id="contracts-rollback")
    validation = create_bound_validation(monkeypatch, tmp_path, manifest_path, manifest)
    apply_validated_plan(tmp_path, manifest_path, validation)
    run_downstream_stage(tmp_path, manifest["run_id"], stage="master_outline")
    run_downstream_stage(tmp_path, manifest["run_id"], stage="state")
    expected = plan_transaction._expected_contracts(tmp_path, validation, manifest)
    assert all(not path.exists() for path in expected)

    real_write = plan_transaction._safe_json_write
    calls = {"count": 0}

    def fail_second(path_root, path, payload, **kwargs):
        calls["count"] += 1
        if calls["count"] == 2:
            raise OSError("injected contract write failure")
        return real_write(path_root, path, payload, **kwargs)

    monkeypatch.setattr(plan_transaction, "_safe_json_write", fail_second)
    with pytest.raises(PlanTransactionError, match="injected contract write failure"):
        run_downstream_stage(tmp_path, manifest["run_id"], stage="contracts")
    assert all(not path.exists() for path in expected)

    monkeypatch.setattr(plan_transaction, "_safe_json_write", real_write)
    completed = run_downstream_stage(tmp_path, manifest["run_id"], stage="contracts")
    assert completed["status"] == "completed"
    assert all(path.is_file() for path in expected)


def test_state_stage_rolls_back_when_writer_fails_after_replace(tmp_path, monkeypatch):
    make_initialized_project(tmp_path)
    manifest_path, manifest = make_valid_plan(tmp_path, run_id="state-postwrite-failure")
    validation = create_bound_validation(monkeypatch, tmp_path, manifest_path, manifest)
    apply_validated_plan(tmp_path, manifest_path, validation)
    run_downstream_stage(tmp_path, manifest["run_id"], stage="master_outline")
    state_path = tmp_path / ".webnovel" / "state.json"
    before = state_path.read_bytes()
    real_write = plan_transaction._safe_json_write_locked

    def write_then_fail(path_root, path, payload, **kwargs):
        real_write(path_root, path, payload, **kwargs)
        if path == state_path:
            raise OSError("injected state post-replace failure")

    monkeypatch.setattr(plan_transaction, "_safe_json_write_locked", write_then_fail)
    with pytest.raises(PlanTransactionError, match="state post-replace failure"):
        run_downstream_stage(tmp_path, manifest["run_id"], stage="state")

    assert state_path.read_bytes() == before
    monkeypatch.setattr(plan_transaction, "_safe_json_write_locked", real_write)
    assert run_downstream_stage(tmp_path, manifest["run_id"], stage="state")["status"] == "completed"


def test_contract_stage_rolls_back_path_when_writer_fails_after_replace(tmp_path, monkeypatch):
    make_initialized_project(tmp_path)
    manifest_path, manifest = make_valid_plan(tmp_path, run_id="contracts-postwrite-failure")
    validation = create_bound_validation(monkeypatch, tmp_path, manifest_path, manifest)
    apply_validated_plan(tmp_path, manifest_path, validation)
    run_downstream_stage(tmp_path, manifest["run_id"], stage="master_outline")
    run_downstream_stage(tmp_path, manifest["run_id"], stage="state")
    expected = plan_transaction._expected_contracts(tmp_path, validation, manifest)
    real_write = plan_transaction._safe_json_write
    calls = {"count": 0}

    def write_then_fail(path_root, path, payload, **kwargs):
        calls["count"] += 1
        result = real_write(path_root, path, payload, **kwargs)
        if calls["count"] == 2:
            raise OSError("injected post-replace readback failure")
        return result

    monkeypatch.setattr(plan_transaction, "_safe_json_write", write_then_fail)
    with pytest.raises(PlanTransactionError, match="post-replace readback failure"):
        run_downstream_stage(tmp_path, manifest["run_id"], stage="contracts")

    assert all(not path.exists() for path in expected)
    monkeypatch.setattr(plan_transaction, "_safe_json_write", real_write)
    assert run_downstream_stage(tmp_path, manifest["run_id"], stage="contracts")["status"] == "completed"


def test_downstream_completed_receipt_becomes_stale_if_fact_changes(tmp_path, monkeypatch):
    make_initialized_project(tmp_path)
    manifest_path, manifest = make_valid_plan(tmp_path)
    validation = create_bound_validation(monkeypatch, tmp_path, manifest_path, manifest)
    apply_validated_plan(tmp_path, manifest_path, validation)
    run_downstream_stage(tmp_path, manifest["run_id"], stage="master_outline")
    master = tmp_path / "大纲" / "总纲.md"
    master.write_text(master.read_text(encoding="utf-8") + "\n篡改\n", encoding="utf-8")
    status = plan_transaction_status(tmp_path, manifest["run_id"])
    assert status["stages"]["master_outline"]["status"] == "stale"
    assert status["complete"] is False


def test_forged_self_hashed_stage_receipts_cannot_skip_truth_sources(tmp_path, monkeypatch):
    make_initialized_project(tmp_path)
    manifest_path, manifest = make_valid_plan(tmp_path, run_id="forged-stage-receipts")
    validation = create_bound_validation(monkeypatch, tmp_path, manifest_path, manifest)
    applied = apply_validated_plan(tmp_path, manifest_path, validation)
    runtime_dir = tmp_path / ".webnovel" / "plan-runs" / manifest["run_id"]
    decoy = manifest_path
    for stage in plan_transaction.DOWNSTREAM_STAGES:
        forged = {
            "schema_version": plan_transaction.STAGE_RECEIPT_SCHEMA,
            "run_id": manifest["run_id"],
            "stage": stage,
            "status": "completed",
            "created_at": "2026-08-08T00:00:00+00:00",
            "apply_receipt_sha256": applied["receipt_sha256"],
            "outputs": {
                "decoy": {
                    "path": str(decoy),
                    "sha256": plan_transaction.file_sha256(decoy),
                }
            },
            "verification": {},
            "decision_receipt_path": None,
            "decision_receipt_sha256": None,
            "detail": "",
        }
        forged["receipt_sha256"] = plan_transaction._receipt_hash(forged)
        (runtime_dir / f"stage-{stage}-001.json").write_text(
            json.dumps(forged, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    status = plan_transaction_status(tmp_path, manifest["run_id"])

    assert status["complete"] is False
    assert status["next_stage"] == "master_outline"
    assert all(
        status["stages"][stage]["status"] == "stale"
        for stage in plan_transaction.DOWNSTREAM_STAGES
    )
    with pytest.raises(PlanTransactionError, match="out of order"):
        run_downstream_stage(tmp_path, manifest["run_id"], stage="state")


@pytest.mark.parametrize("mutation", ["delete", "modify"])
def test_complete_status_becomes_stale_when_promoted_plan_fact_changes(
    tmp_path, monkeypatch, mutation
):
    make_initialized_project(tmp_path)
    manifest_path, manifest = make_valid_plan(tmp_path, run_id=f"status-plan-{mutation}")
    validation = create_bound_validation(monkeypatch, tmp_path, manifest_path, manifest)
    apply_validated_plan(tmp_path, manifest_path, validation)
    for stage in plan_transaction.DOWNSTREAM_STAGES:
        run_downstream_stage(tmp_path, manifest["run_id"], stage=stage)
    assert plan_transaction_status(tmp_path, manifest["run_id"])["complete"] is True

    outline = tmp_path / manifest["artifacts"]["outline"]["target"]
    if mutation == "delete":
        outline.unlink()
    else:
        outline.write_text(outline.read_text(encoding="utf-8") + "\n作者改动\n", encoding="utf-8")

    status = plan_transaction_status(tmp_path, manifest["run_id"])
    assert status["status"] == "stale"
    assert status["complete"] is False
    assert status["next_stage"] == "apply"
    assert status["apply"]["status"] == "stale"
    assert any("promoted plan artifact is stale" in item for item in status["integrity_errors"])


def test_generic_downstream_receipt_cannot_manufacture_success(tmp_path):
    with pytest.raises(PlanTransactionError, match="direct downstream"):
        record_downstream_stage(
            tmp_path,
            "forged",
            stage="prewrite",
            status="completed",
            outputs={},
            verification={"gate_ok": True},
        )


def _run_plan_transaction_cli(monkeypatch, capsys, *args):
    monkeypatch.setattr(sys, "argv", ["plan-transaction", *map(str, args)])
    with pytest.raises(SystemExit) as caught:
        plan_transaction.main()
    output = json.loads(capsys.readouterr().out)
    return caught.value.code, output


def test_plan_transaction_cli_validate_apply_status_choice_and_error(tmp_path, monkeypatch, capsys):
    manifest_path, manifest = make_valid_plan(tmp_path, run_id="plan-cli")
    request_path, evidence_path = make_parent_evidence(monkeypatch, tmp_path, manifest_path, manifest)
    fragment_path = next(
        (tmp_path / ".webnovel" / "tmp" / "plan-runs" / manifest["run_id"] / "batches").glob(
            "batch-*.json"
        )
    )
    code, batch = _run_plan_transaction_cli(
        monkeypatch,
        capsys,
        "--project-root",
        tmp_path,
        "accept-batch",
        "--request-file",
        request_path,
        "--fragment-file",
        fragment_path,
    )
    assert code == 0
    assert batch["status"] == "accepted"
    code, marker = _run_plan_transaction_cli(
        monkeypatch,
        capsys,
        "--project-root",
        tmp_path,
        "marker",
        "--manifest",
        manifest_path,
        "--request-file",
        request_path,
    )
    assert code == 0
    assert marker["marker"].startswith(plan_transaction.PARENT_MARKER_PREFIX)
    code, validation = _run_plan_transaction_cli(
        monkeypatch,
        capsys,
        "--project-root",
        tmp_path,
        "validate",
        "--manifest",
        manifest_path,
        "--request-file",
        request_path,
        "--parent-evidence-file",
        evidence_path,
    )
    assert code == 0
    receipt_path = tmp_path / ".webnovel" / "plan-runs" / "plan-cli" / "validation.json"
    code, applied = _run_plan_transaction_cli(
        monkeypatch,
        capsys,
        "--project-root",
        tmp_path,
        "apply",
        "--manifest",
        manifest_path,
        "--receipt",
        receipt_path,
    )
    assert code == 0
    assert applied["status"] == "applied"
    code, status = _run_plan_transaction_cli(
        monkeypatch,
        capsys,
        "--project-root",
        tmp_path,
        "status",
        "--run-id",
        manifest["run_id"],
    )
    assert code == 0
    assert status["next_stage"] == "master_outline"

    conflict_manifest, conflict = make_valid_plan(tmp_path, run_id="plan-cli-conflict")
    conflict_target = tmp_path / conflict["artifacts"]["beat"]["target"]
    conflict_target.write_text("作者版本", encoding="utf-8")
    conflict_validation = create_bound_validation(
        monkeypatch, tmp_path, conflict_manifest, conflict
    )
    conflict_receipt = tmp_path / ".webnovel" / "plan-runs" / "plan-cli-conflict" / "validation.json"
    code, choice = _run_plan_transaction_cli(
        monkeypatch,
        capsys,
        "--project-root",
        tmp_path,
        "apply",
        "--manifest",
        conflict_manifest,
        "--receipt",
        conflict_receipt,
    )
    assert code == 1
    assert choice["scope_challenge"].startswith("webnovel-plan-decision:")
    assert choice["authorization_gate"] == "trusted_parent_decision_required"
    append_plan_decision_choice(conflict_validation, choice, "replace")
    code, decision = _run_plan_transaction_cli(
        monkeypatch,
        capsys,
        "--project-root",
        tmp_path,
        "decision",
        "--request-file",
        choice["decision_request_file"],
    )
    assert code == 0
    assert decision["selected"] == "replace"
    code, replaced = _run_plan_transaction_cli(
        monkeypatch,
        capsys,
        "--project-root",
        tmp_path,
        "apply",
        "--manifest",
        conflict_manifest,
        "--receipt",
        conflict_receipt,
        "--decision-receipt",
        decision["receipt_path"],
    )
    assert code == 0
    assert replaced["overwrite_authorized"] is True

    code, failed = _run_plan_transaction_cli(
        monkeypatch,
        capsys,
        "--project-root",
        tmp_path,
        "apply",
        "--manifest",
        conflict_manifest,
        "--receipt",
        tmp_path / "missing.json",
    )
    assert code == 2
    assert failed["status"] == "failed"


def test_validation_and_relative_receipt_paths_are_idempotent(tmp_path, monkeypatch):
    manifest_path, manifest = make_valid_plan(tmp_path)
    request_path, evidence_path = make_parent_evidence(monkeypatch, tmp_path, manifest_path, manifest)
    first = create_validation_receipt(
        tmp_path,
        manifest_path,
        request_file=request_path,
        parent_evidence_file=evidence_path,
    )
    assert create_validation_receipt(
        tmp_path,
        manifest_path,
        request_file=request_path,
        parent_evidence_file=evidence_path,
    ) == first
    receipt_path = tmp_path / ".webnovel" / "plan-runs" / manifest["run_id"] / "validation.json"

    applied = apply_validated_plan(
        tmp_path,
        manifest_path.relative_to(tmp_path),
        receipt_path.relative_to(tmp_path),
    )
    assert applied["status"] == "applied"


@pytest.mark.parametrize("tamper", ["schema", "hash", "stale"])
def test_validation_receipt_integrity_failures(tmp_path, monkeypatch, tamper):
    manifest_path, manifest = make_valid_plan(tmp_path)
    receipt = create_bound_validation(monkeypatch, tmp_path, manifest_path, manifest)
    changed = dict(receipt)
    if tamper == "schema":
        changed["schema_version"] = "bad"
    elif tamper == "hash":
        changed["receipt_sha256"] = "0" * 64
    else:
        changed["content_sha256"] = "0" * 64
        unsigned = dict(changed)
        unsigned.pop("receipt_sha256")
        changed["receipt_sha256"] = plan_transaction._receipt_hash(unsigned)

    with pytest.raises(PlanTransactionError):
        apply_validated_plan(tmp_path, manifest_path, changed)


def test_plan_receipt_json_and_apply_state_fail_closed(tmp_path, monkeypatch):
    bad = tmp_path / "bad.json"
    bad.write_bytes(b"\xef\xbb\xbf{}")
    with pytest.raises(PlanTransactionError, match="invalid JSON"):
        plan_transaction._read_json(bad)
    bad.write_text("[]", encoding="utf-8")
    with pytest.raises(PlanTransactionError, match="object"):
        plan_transaction._read_json(bad)

    immutable = tmp_path / "immutable.json"
    plan_transaction._write_receipt_once(immutable, {"value": 1})
    with pytest.raises(PlanTransactionError, match="immutable"):
        plan_transaction._write_receipt_once(immutable, {"value": 2})

    manifest_path, manifest = make_valid_plan(tmp_path)
    receipt = create_bound_validation(monkeypatch, tmp_path, manifest_path, manifest)
    apply_validated_plan(tmp_path, manifest_path, receipt)
    target = tmp_path / manifest["artifacts"]["beat"]["target"]
    target.unlink()
    with pytest.raises(PlanTransactionError, match="no longer matches"):
        apply_validated_plan(tmp_path, manifest_path, receipt)

    second_manifest, second = make_valid_plan(tmp_path, run_id="no-filelock")
    second_receipt = create_bound_validation(monkeypatch, tmp_path, second_manifest, second)
    monkeypatch.setattr(plan_transaction, "FileLock", None)
    with pytest.raises(PlanTransactionError, match="filelock"):
        apply_validated_plan(tmp_path, second_manifest, second_receipt)


def test_downstream_argument_and_output_failures(tmp_path, monkeypatch):
    manifest_path, manifest = make_valid_plan(tmp_path)
    receipt = create_bound_validation(monkeypatch, tmp_path, manifest_path, manifest)

    with pytest.raises(PlanTransactionError, match="unknown"):
        run_downstream_stage(tmp_path, manifest["run_id"], stage="bad")
    with pytest.raises(PlanTransactionError, match="direct downstream"):
        record_downstream_stage(tmp_path, manifest["run_id"], stage="master_outline", status="skipped")
    with pytest.raises(PlanTransactionError, match="invalid JSON"):
        run_downstream_stage(tmp_path, manifest["run_id"], stage="master_outline")

    apply_validated_plan(tmp_path, manifest_path, receipt)
    with pytest.raises(PlanTransactionError, match="required file is missing"):
        run_downstream_stage(tmp_path, manifest["run_id"], stage="master_outline")


def test_batch_fragment_and_accepted_receipt_fail_closed_branches(tmp_path, monkeypatch):
    manifest_path, manifest = make_valid_plan(tmp_path, run_id="batch-integrity-errors")
    request_path, _ = make_parent_evidence(monkeypatch, tmp_path, manifest_path, manifest)
    fragment_path = (
        tmp_path
        / ".webnovel"
        / "tmp"
        / "plan-runs"
        / manifest["run_id"]
        / "batches"
        / "batch-000001-000002.json"
    )
    receipt_path = (
        tmp_path
        / ".webnovel"
        / "plan-runs"
        / manifest["run_id"]
        / "batches"
        / "batch-000001-000002.accepted.json"
    )
    original_fragment = fragment_path.read_bytes()
    original_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))

    with pytest.raises(PlanTransactionError, match="must be absolute"):
        plan_transaction.accept_plan_batch(tmp_path, request_path, Path("batch-000001-000002.json"))
    with pytest.raises(PlanTransactionError, match="filename is invalid"):
        plan_transaction.accept_plan_batch(tmp_path, request_path, tmp_path / "wrong.json")

    fragment_path.write_text("{}", encoding="utf-8")
    with pytest.raises(PlanTransactionError, match="invalid shape"):
        plan_transaction.accept_plan_batch(tmp_path, request_path, fragment_path)
    fragment = json.loads(original_fragment.decode("utf-8"))
    fragment["volume"] = 2
    fragment_path.write_text(json.dumps(fragment, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(PlanTransactionError, match="does not bind"):
        plan_transaction.accept_plan_batch(tmp_path, request_path, fragment_path)
    fragment_path.write_bytes(original_fragment)

    malformed = {**original_receipt, "extra": True}
    receipt_path.write_text(json.dumps(malformed, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(PlanTransactionError, match="invalid shape"):
        plan_transaction.accept_plan_batch(tmp_path, request_path, fragment_path)
    bad_hash = dict(original_receipt)
    bad_hash["receipt_sha256"] = "0" * 64
    receipt_path.write_text(json.dumps(bad_hash, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(PlanTransactionError, match="hash mismatch"):
        plan_transaction.accept_plan_batch(tmp_path, request_path, fragment_path)
    wrong_binding = dict(original_receipt)
    wrong_binding["request_sha256"] = "0" * 64
    unsigned = dict(wrong_binding)
    unsigned.pop("receipt_sha256")
    wrong_binding["receipt_sha256"] = plan_transaction._receipt_hash(unsigned)
    receipt_path.write_text(json.dumps(wrong_binding, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(PlanTransactionError, match="does not bind"):
        plan_transaction.accept_plan_batch(tmp_path, request_path, fragment_path)


def test_accepted_batch_must_equal_final_manifest_and_marker_requires_valid_plan(tmp_path):
    manifest_path, manifest = make_valid_plan(tmp_path, run_id="batch-manifest-mismatch")
    request = build_plan_request(
        tmp_path,
        volume=1,
        start_chapter=1,
        end_chapter=2,
        parent_model="gpt-5.6-sol",
        parent_reasoning_effort="high",
        run_id=manifest["run_id"],
    )
    request_path = save_plan_request(request)
    fragment_path = request_path.parent / "batches" / "batch-000001-000002.json"
    fragment_path.parent.mkdir()
    fragment_chapters = json.loads(json.dumps(manifest["chapters"], ensure_ascii=False))
    fragment_chapters[0]["goal"] = "接受批次中的不同目标"
    fragment_path.write_text(
        json.dumps(
            {
                "schema_version": plan_transaction.BATCH_FRAGMENT_SCHEMA,
                "run_id": manifest["run_id"],
                "volume": 1,
                "start_chapter": 1,
                "end_chapter": 2,
                "chapters": fragment_chapters,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    plan_transaction.accept_plan_batch(tmp_path, request_path, fragment_path)

    with pytest.raises(PlanTransactionError, match="does not match the final plan manifest"):
        plan_transaction.build_parent_evidence_marker(tmp_path, manifest_path, request_path)

    manifest["blockers"] = ["仍有待裁决冲突"]
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(PlanTransactionError, match="plan does not validate"):
        plan_transaction.build_parent_evidence_marker(tmp_path, manifest_path, request_path)


def test_parent_evidence_path_shape_binding_and_current_thread_fail_closed(tmp_path, monkeypatch):
    manifest_path, manifest = make_valid_plan(tmp_path, run_id="parent-evidence-errors")
    request_path, evidence_path = make_parent_evidence(
        monkeypatch, tmp_path, manifest_path, manifest
    )
    report = plan_transaction.validate_plan_manifest(tmp_path, manifest_path)
    original = json.loads(evidence_path.read_text(encoding="utf-8"))

    with pytest.raises(PlanTransactionError, match="evidence path must be absolute"):
        plan_transaction._verify_parent_evidence(
            tmp_path, report, request_path, Path("parent-evidence.json")
        )
    evidence_path.write_text(
        json.dumps({**original, "extra": True}, ensure_ascii=False), encoding="utf-8"
    )
    with pytest.raises(PlanTransactionError, match="invalid shape"):
        plan_transaction._verify_parent_evidence(tmp_path, report, request_path, evidence_path)
    evidence_path.write_text(
        json.dumps({**original, "request_sha256": "0" * 64}, ensure_ascii=False),
        encoding="utf-8",
    )
    with pytest.raises(PlanTransactionError, match="does not bind"):
        plan_transaction._verify_parent_evidence(tmp_path, report, request_path, evidence_path)
    evidence_path.write_text(
        json.dumps({**original, "rollout_path": "relative.jsonl"}, ensure_ascii=False),
        encoding="utf-8",
    )
    with pytest.raises(PlanTransactionError, match="trusted Codex sessions root"):
        plan_transaction._verify_parent_evidence(tmp_path, report, request_path, evidence_path)

    evidence_path.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("CODEX_THREAD_ID", "not-a-uuid")
    with pytest.raises(PlanTransactionError, match="must be non-empty UUIDs"):
        plan_transaction._verify_parent_evidence(tmp_path, report, request_path, evidence_path)


def test_existing_apply_receipt_rejects_shape_hash_binding_and_target_forgery(tmp_path, monkeypatch):
    manifest_path, manifest = make_valid_plan(tmp_path, run_id="apply-receipt-errors")
    validation = create_bound_validation(monkeypatch, tmp_path, manifest_path, manifest)
    apply_validated_plan(tmp_path, manifest_path, validation)
    apply_path = tmp_path / ".webnovel" / "plan-runs" / manifest["run_id"] / "apply.json"
    original = json.loads(apply_path.read_text(encoding="utf-8"))

    variants = []
    variants.append(({**original, "extra": True}, "invalid shape"))
    bad_hash = dict(original)
    bad_hash["receipt_sha256"] = "0" * 64
    variants.append((bad_hash, "hash mismatch"))
    bad_binding = dict(original)
    bad_binding["volume"] = 2
    variants.append((bad_binding, "does not bind"))
    bad_item = json.loads(json.dumps(original))
    bad_item["targets"]["beat"]["extra"] = True
    variants.append((bad_item, "target is invalid"))
    bad_path = json.loads(json.dumps(original))
    bad_path["targets"]["beat"]["path"] = str(tmp_path / "wrong.md")
    variants.append((bad_path, "target binding mismatch"))

    for changed, message in variants:
        if message != "hash mismatch":
            unsigned = dict(changed)
            unsigned.pop("receipt_sha256", None)
            changed["receipt_sha256"] = plan_transaction._receipt_hash(unsigned)
        apply_path.write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")
        with pytest.raises(PlanTransactionError, match=message):
            apply_validated_plan(tmp_path, manifest_path, validation)
    apply_path.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")


def test_apply_rechecks_source_and_target_during_locked_promotion(tmp_path, monkeypatch):
    source_root = tmp_path / "source-race"
    manifest_path, manifest = make_valid_plan(source_root, run_id="source-before-apply")
    validation = create_bound_validation(monkeypatch, source_root, manifest_path, manifest)
    real_artifacts = plan_transaction._manifest_artifacts

    def mutate_after_artifact_binding(path, root):
        result = real_artifacts(path, root)
        source = result["beat"][0]
        source.write_bytes(source.read_bytes() + b"\nchanged-after-validation")
        return result

    with monkeypatch.context() as patcher:
        patcher.setattr(plan_transaction, "_manifest_artifacts", mutate_after_artifact_binding)
        with pytest.raises(PlanTransactionError, match="source hash changed before apply"):
            apply_validated_plan(source_root, manifest_path, validation)

    target_root = tmp_path / "target-race"
    target_manifest_path, target_manifest = make_valid_plan(
        target_root, run_id="target-during-apply"
    )
    target_validation = create_bound_validation(
        monkeypatch, target_root, target_manifest_path, target_manifest
    )
    target = target_root / target_manifest["artifacts"]["beat"]["target"]
    real_stable = plan_transaction._stable_artifact_bytes
    target_reads = {"count": 0}

    def authored_during_apply(path_root, path, *, must_exist, max_bytes=64 * 1024 * 1024):
        if path == target and not must_exist:
            target_reads["count"] += 1
            if target_reads["count"] == 2:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("作者在 apply 循环中写入", encoding="utf-8")
        return real_stable(path_root, path, must_exist=must_exist, max_bytes=max_bytes)

    with monkeypatch.context() as patcher:
        patcher.setattr(plan_transaction, "_stable_artifact_bytes", authored_during_apply)
        with pytest.raises(PlanTransactionError, match="target changed during apply"):
            apply_validated_plan(
                target_root, target_manifest_path, target_validation
            )
    assert target.read_text(encoding="utf-8") == "作者在 apply 循环中写入"


def test_apply_skips_identical_target_and_cleans_receipt_after_final_verification_failure(
    tmp_path, monkeypatch
):
    identical_root = tmp_path / "identical"
    manifest_path, manifest = make_valid_plan(identical_root, run_id="identical-target")
    validation = create_bound_validation(monkeypatch, identical_root, manifest_path, manifest)
    source = identical_root / manifest["artifacts"]["beat"]["path"]
    target = identical_root / manifest["artifacts"]["beat"]["target"]
    target.parent.mkdir(parents=True)
    target.write_bytes(source.read_bytes())
    before = target.stat().st_mtime_ns
    apply_validated_plan(identical_root, manifest_path, validation)
    assert target.stat().st_mtime_ns == before

    cleanup_root = tmp_path / "cleanup"
    cleanup_manifest_path, cleanup_manifest = make_valid_plan(
        cleanup_root, run_id="cleanup-apply-receipt"
    )
    cleanup_validation = create_bound_validation(
        monkeypatch, cleanup_root, cleanup_manifest_path, cleanup_manifest
    )

    def reject_final_receipt(*_args, **_kwargs):
        raise PlanTransactionError("injected final apply receipt verification failure")

    with monkeypatch.context() as patcher:
        patcher.setattr(plan_transaction, "_verify_apply_receipt", reject_final_receipt)
        with pytest.raises(PlanTransactionError, match="final apply receipt verification"):
            apply_validated_plan(
                cleanup_root, cleanup_manifest_path, cleanup_validation
            )
    assert not (
        cleanup_root
        / ".webnovel"
        / "plan-runs"
        / cleanup_manifest["run_id"]
        / "apply.json"
    ).exists()
    assert all(
        not (cleanup_root / spec["target"]).exists()
        for spec in cleanup_manifest["artifacts"].values()
    )


def test_apply_reports_incomplete_rollback_when_promoted_bytes_are_unexpected(tmp_path, monkeypatch):
    manifest_path, manifest = make_valid_plan(tmp_path, run_id="apply-rollback-unexpected")
    validation = create_bound_validation(monkeypatch, tmp_path, manifest_path, manifest)
    first_target = tmp_path / manifest["artifacts"]["beat"]["target"]
    real_atomic = plan_transaction._atomic_write_bytes

    def corrupt_promoted_target(path, raw):
        if path == first_target:
            return real_atomic(path, b"unexpected-author-bytes")
        return real_atomic(path, raw)

    monkeypatch.setattr(plan_transaction, "_atomic_write_bytes", corrupt_promoted_target)
    with pytest.raises(PlanTransactionError, match="rollback was incomplete"):
        apply_validated_plan(tmp_path, manifest_path, validation)
    assert first_target.read_bytes() == b"unexpected-author-bytes"


def test_status_is_fail_closed_for_missing_and_malformed_runtime_truth(tmp_path, monkeypatch):
    missing_root = tmp_path / "missing"
    missing_root.mkdir()
    missing = plan_transaction_status(missing_root, "missing-run")
    assert missing["status"] == "missing"
    assert missing["next_stage"] is None

    malformed_root = tmp_path / "malformed"
    malformed_root.mkdir()
    runtime = malformed_root / ".webnovel" / "plan-runs" / "malformed-run"
    runtime.mkdir(parents=True)
    (runtime / "validation.json").write_text("{", encoding="utf-8")
    malformed = plan_transaction_status(malformed_root, "malformed-run")
    assert malformed["status"] == "stale"
    assert malformed["next_stage"] == "validate"

    apply_only_root = tmp_path / "apply-only"
    apply_only_root.mkdir()
    apply_runtime = apply_only_root / ".webnovel" / "plan-runs" / "apply-only-run"
    apply_runtime.mkdir(parents=True)
    (apply_runtime / "apply.json").write_text("{}", encoding="utf-8")
    apply_only = plan_transaction_status(apply_only_root, "apply-only-run")
    assert apply_only["status"] == "stale"
    assert any("current validation" in item for item in apply_only["integrity_errors"])

    active_root = tmp_path / "active"
    manifest_path, manifest = make_valid_plan(active_root, run_id="malformed-stage")
    validation = create_bound_validation(monkeypatch, active_root, manifest_path, manifest)
    apply_validated_plan(active_root, manifest_path, validation)
    stage_path = (
        active_root
        / ".webnovel"
        / "plan-runs"
        / manifest["run_id"]
        / "stage-master_outline-001.json"
    )
    stage_path.write_text("{", encoding="utf-8")
    stale_stage = plan_transaction_status(active_root, manifest["run_id"])
    assert stale_stage["stages"]["master_outline"]["status"] == "stale"


def test_cli_stage_surfaces_downstream_choice_and_status_missing(tmp_path, monkeypatch, capsys):
    make_initialized_project(tmp_path)
    manifest_path, manifest = make_valid_plan(tmp_path, run_id="cli-stage-choice")
    validation = create_bound_validation(monkeypatch, tmp_path, manifest_path, manifest)
    apply_validated_plan(tmp_path, manifest_path, validation)

    code, completed = _run_plan_transaction_cli(
        monkeypatch,
        capsys,
        "--project-root",
        tmp_path,
        "stage",
        "--run-id",
        manifest["run_id"],
        "--stage",
        "master_outline",
    )
    assert code == 0
    assert completed["status"] == "completed"

    state_path = tmp_path / ".webnovel" / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["progress"]["volumes_planned"] = [
        {"volume": 1, "chapters_range": "99-100", "planned_at": "2026-08-08"}
    ]
    state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    code, choice = _run_plan_transaction_cli(
        monkeypatch,
        capsys,
        "--project-root",
        tmp_path,
        "stage",
        "--run-id",
        manifest["run_id"],
        "--stage",
        "state",
    )
    assert code == 1
    assert choice["code"] == "plan_downstream_overwrite_requires_user_choice"
    assert choice["stage"] == "state"

    code, missing = _run_plan_transaction_cli(
        monkeypatch,
        capsys,
        "--project-root",
        tmp_path,
        "status",
        "--run-id",
        "never-created",
    )
    assert code == 2
    assert missing["status"] == "missing"


def test_trusted_replace_receipts_cover_all_authored_downstream_stages(tmp_path, monkeypatch):
    make_initialized_project(tmp_path)
    manifest_path, manifest = make_valid_plan(tmp_path, run_id="downstream-decisions")
    validation = create_bound_validation(monkeypatch, tmp_path, manifest_path, manifest)
    apply_validated_plan(tmp_path, manifest_path, validation)

    master = tmp_path / "大纲" / "总纲.md"
    with master.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write("| 2 | 作者续卷 | 3-4 | 作者冲突 | 作者高潮 |\n")
    with pytest.raises(plan_transaction.PlanDownstreamChoiceRequired) as master_choice:
        run_downstream_stage(tmp_path, manifest["run_id"], stage="master_outline")
    master_decision = create_plan_decision_from_choice(
        tmp_path,
        validation,
        master_choice.value.decision,
        "replace",
    )
    master_receipt = run_downstream_stage(
        tmp_path,
        manifest["run_id"],
        stage="master_outline",
        decision_receipt=master_decision["receipt_path"],
    )
    assert master_receipt["decision_receipt_sha256"] == master_decision["receipt_sha256"]

    state_path = tmp_path / ".webnovel" / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["progress"]["volumes_planned"] = [
        {"volume": 1, "chapters_range": "99-100", "planned_at": "2026-08-08"}
    ]
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    with pytest.raises(plan_transaction.PlanDownstreamChoiceRequired) as state_choice:
        run_downstream_stage(tmp_path, manifest["run_id"], stage="state")
    state_before_replay = state_path.read_bytes()
    with pytest.raises(PlanTransactionError, match="stale conflict scope"):
        run_downstream_stage(
            tmp_path,
            manifest["run_id"],
            stage="state",
            decision_receipt=master_decision["receipt_path"],
        )
    assert state_path.read_bytes() == state_before_replay
    state_decision = create_plan_decision_from_choice(
        tmp_path,
        validation,
        state_choice.value.decision,
        "replace",
    )
    state_receipt = run_downstream_stage(
        tmp_path,
        manifest["run_id"],
        stage="state",
        decision_receipt=state_decision["receipt_path"],
    )
    assert state_receipt["decision_receipt_sha256"] == state_decision["receipt_sha256"]

    expected = plan_transaction._expected_contracts(tmp_path, validation, manifest)
    first_contract = sorted(expected)[0]
    first_contract.parent.mkdir(parents=True, exist_ok=True)
    first_contract.write_text("{}", encoding="utf-8")
    with pytest.raises(plan_transaction.PlanDownstreamChoiceRequired) as contract_choice:
        run_downstream_stage(tmp_path, manifest["run_id"], stage="contracts")
    contract_decision = create_plan_decision_from_choice(
        tmp_path,
        validation,
        contract_choice.value.decision,
        "replace",
    )
    contract_receipt = run_downstream_stage(
        tmp_path,
        manifest["run_id"],
        stage="contracts",
        decision_receipt=contract_decision["receipt_path"],
    )
    assert contract_receipt["decision_receipt_sha256"] == contract_decision["receipt_sha256"]

    with Path(validation["parent_rollout_path"]).open(
        "a", encoding="utf-8", newline="\n"
    ) as handle:
        handle.write(json.dumps({"type": "event_msg", "payload": {"message": "append"}}) + "\n")
    status = plan_transaction_status(tmp_path, manifest["run_id"])
    for stage in ("master_outline", "state", "contracts"):
        assert status["stages"][stage]["status"] == "completed"
    assert status["next_stage"] == "prewrite"


def test_downstream_idempotence_contract_conflicts_and_prewrite_blocker(tmp_path, monkeypatch):
    make_initialized_project(tmp_path)
    manifest_path, manifest = make_valid_plan(tmp_path, run_id="downstream-errors")
    validation = create_bound_validation(monkeypatch, tmp_path, manifest_path, manifest)
    apply_validated_plan(tmp_path, manifest_path, validation)
    master_receipt = run_downstream_stage(tmp_path, manifest["run_id"], stage="master_outline")
    assert run_downstream_stage(tmp_path, manifest["run_id"], stage="master_outline") == master_receipt
    run_downstream_stage(tmp_path, manifest["run_id"], stage="state")

    state_receipt = next(
        (tmp_path / ".webnovel" / "plan-runs" / manifest["run_id"]).glob("stage-state-*.json")
    )
    state_receipt.unlink()
    assert run_downstream_stage(tmp_path, manifest["run_id"], stage="state")["status"] == "completed"

    expected = plan_transaction._expected_contracts(tmp_path, validation, manifest)
    first_contract = sorted(expected)[0]
    first_contract.parent.mkdir(parents=True, exist_ok=True)
    first_contract.write_text("{", encoding="utf-8")
    with pytest.raises(PlanTransactionError, match="existing contract is unreadable"):
        run_downstream_stage(tmp_path, manifest["run_id"], stage="contracts")
    first_contract.write_text("{}", encoding="utf-8")
    with pytest.raises(plan_transaction.PlanDownstreamChoiceRequired):
        run_downstream_stage(tmp_path, manifest["run_id"], stage="contracts")
    first_contract.unlink()
    assert run_downstream_stage(tmp_path, manifest["run_id"], stage="contracts")["status"] == "completed"

    monkeypatch.setattr(
        plan_transaction,
        "run_write_gate",
        lambda *_args, **_kwargs: {"ok": False, "chapter": 1, "stage": "prewrite"},
    )
    with pytest.raises(PlanTransactionError, match="prewrite gate is blocking"):
        run_downstream_stage(tmp_path, manifest["run_id"], stage="prewrite")
