from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from data_modules import write_transaction
from data_modules.codex_agent_runtime import (
    VerifiedRuntimeEvidence,
    build_canned_envelope,
)
from data_modules.chapter_commit_service import ChapterCommitService
from data_modules.projection_log import latest_projection_run, projection_status_from_run
from data_modules.write_transaction import (
    AGENT_ACCEPT_REQUEST_SCHEMA,
    AGENT_LAUNCH_INPUT_SCHEMA,
    AGENT_LAUNCH_REQUEST_SCHEMA,
    STAGE_REQUEST_SCHEMA,
    WRITE_STAGES,
    WriteRecoveryChoiceRequired,
    WriteTransactionError,
    accept_verified_agent_stage,
    accept_agent_request,
    begin_write_transaction,
    build_agent_prompt_marker,
    build_write_resume_plan,
    promote_verified_writer_artifact,
    prepare_agent_launch_request,
    record_minimal_no_review,
    record_verified_stage_request,
    record_write_stage,
    write_transaction_status,
)
from .test_project_phase import _make_contracts, _make_init_ready


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _advance_test_stage(root, run_id, stage, *, details=None, status="completed"):
    return record_write_stage(
        root,
        run_id,
        stage=stage,
        status=status,
        details=details or {},
        test_only_agent_override=stage in {
            "context_agent", "writer_draft", "reviewer", "writer_final", "data_agent"
        },
    )


def _write_stage_request(root: Path, run_id: str, stage: str, *, artifact=None, status="completed"):
    requests = root / ".webnovel" / "tmp" / "write-runs" / run_id / "requests"
    requests.mkdir(parents=True, exist_ok=True)
    path = requests / f"{stage}-{len(list(requests.glob('stage-*.json'))):03d}.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": STAGE_REQUEST_SCHEMA,
                "run_id": run_id,
                "stage": stage,
                "status": status,
                "error_code": "",
                "artifact": artifact,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path.resolve()


def _prepare_production_gates(root: Path, run_id: str, monkeypatch):
    state = root / ".webnovel" / "state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        write_transaction,
        "run_write_gate",
        lambda project_root, *, chapter, stage: {
            "schema_version": "webnovel-write-gate/v1",
            "stage": stage,
            "project_root": str(Path(project_root).resolve()),
            "chapter": chapter,
            "phase": "ready",
            "ok": True,
            "errors": [],
        },
    )
    for stage in ("preflight", "prewrite"):
        record_verified_stage_request(root, run_id, _write_stage_request(root, run_id, stage))


def _mock_route_ready(monkeypatch, *, parent_thread_id="parent-task"):
    def ready(workspace_root, route):
        return {
            "ready": True,
            "status": "ready",
            "problems": [],
            "agents": [
                {
                    "agent_name": step["agent_name"],
                    "current": True,
                    "contract_hash": step["contract_hash"],
                    "managed_sha256": step["managed_sha256"],
                }
                for step in route.get("steps") or []
            ],
        }

    monkeypatch.setattr(write_transaction, "validate_route_readiness", ready)
    monkeypatch.setattr(
        write_transaction,
        "_current_parent_host_evidence",
        lambda: {
            "thread_id": parent_thread_id,
            "rollout_path": f"C:/trusted/{parent_thread_id}.jsonl",
            "rollout_sha256": "f" * 64,
            "rollout_bytes": 123,
        },
    )


def _write_rollout(sessions_root: Path, *, role: str, thread_id: str, parent_id: str, effort="high"):
    path = sessions_root / "2026" / "08" / "08" / f"rollout-test-{thread_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    events = [
        {
            "type": "session_meta",
            "payload": {
                "id": thread_id,
                "parent_thread_id": parent_id,
                "model": "gpt-5.6-luna",
                "source": {
                    "subagent": {
                        "thread_spawn": {
                            "parent_thread_id": parent_id,
                            "depth": 1,
                            "agent_path": role,
                            "agent_nickname": "worker",
                            "agent_role": role,
                        }
                    }
                },
            },
        },
        {"type": "response_item", "payload": {"type": "developer_message"}},
        {
            "type": "turn_context",
            "payload": {"turn_id": "turn-001", "model": "gpt-5.6-luna", "effort": effort},
        },
        {"type": "response_item", "payload": {"type": "message"}},
    ]
    path.write_text(
        "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n",
        encoding="utf-8",
    )
    return path.resolve()


def _write_agent_request(
    root: Path,
    run_id: str,
    *,
    stage: str,
    rollout: Path,
    sessions_root: Path,
    envelope: dict,
    payload: str | dict,
    thread_id: str,
    parent_id: str,
    desktop_no_marker: bool = False,
):
    staging = root / ".webnovel" / "tmp" / "write-runs" / run_id
    staging.mkdir(parents=True, exist_ok=True)
    write_transaction.TRUSTED_CODEX_SESSIONS_ROOT = sessions_root.resolve()
    payload_path = staging / (f"{stage}-payload.md" if isinstance(payload, str) else f"{stage}-payload.json")
    requests = staging / "requests"
    requests.mkdir(exist_ok=True)
    input_path = staging / f"{stage}-input.txt"
    input_path.write_text("bound input", encoding="utf-8")
    transaction = write_transaction._load_transaction(root, run_id)
    receipts = write_transaction._validated_receipts(write_transaction._run_dir(root, run_id))
    progress = write_transaction._derive_progress(transaction, receipts)
    lineage = write_transaction._required_stage_lineage_pairs(
        root.resolve(),
        run_id,
        stage,
        transaction,
        progress,
    )
    input_artifacts = [
        *(
            [{"path": str(input_path.resolve()), "sha256": _sha(input_path)}]
            if stage == "context_agent"
            else []
        ),
        *[
            {"path": path, "sha256": digest}
            for path, digest in sorted(lineage)
            if Path(path).resolve() != input_path.resolve()
        ],
    ]
    if stage in {"writer_draft", "writer_final"} and isinstance(payload, dict):
        manifest_path = Path(str(payload["manifest_path"]))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["inputs"] = input_artifacts
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        payload["manifest_sha256"] = _sha(manifest_path)
    payload_bytes = (
        payload.encode("utf-8")
        if isinstance(payload, str)
        else json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    payload_path.write_bytes(payload_bytes)
    launch = {
        "schema_version": AGENT_LAUNCH_REQUEST_SCHEMA,
        "run_id": run_id,
        "stage": stage,
        "transaction_sha256": transaction["transaction_sha256"],
        "input_artifacts": input_artifacts,
    }
    launch_path = requests / f"{stage}-launch.json"
    launch_path.write_text(json.dumps(launch, ensure_ascii=False), encoding="utf-8")
    launch_spec = {"path": str(launch_path.resolve()), "sha256": _sha(launch_path)}
    marker = build_agent_prompt_marker(
        root,
        run_id,
        stage=stage,
        launch_request=launch_spec,
    )
    agent_task_name = write_transaction.derive_agent_task_name(
        marker,
        prefix=write_transaction.AGENT_TASK_NAME_PREFIX,
    )
    events = [json.loads(line) for line in rollout.read_text(encoding="utf-8").splitlines() if line]
    spawn = events[0]["payload"]["source"]["subagent"]["thread_spawn"]
    spawn["depth"] = 1
    spawn["agent_path"] = f"/root/{agent_task_name}"
    if desktop_no_marker:
        events.extend(
            [
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "phase": "commentary",
                        "content": [{"type": "output_text", "text": "正在处理受绑定的 Agent 请求。"}],
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
                                "text": (
                                    payload
                                    if isinstance(payload, str)
                                    else json.dumps(
                                        payload,
                                        ensure_ascii=False,
                                        sort_keys=True,
                                        separators=(",", ":"),
                                    )
                                ),
                            }
                        ],
                    },
                },
            ]
        )
    else:
        events.extend(
            [
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": marker}],
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
                            "text": payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                        }
                    ],
                },
            },
            ]
        )
    rollout.write_text(
        "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n",
        encoding="utf-8",
    )
    request_path = requests / f"accept-{stage}.json"
    request_path.write_text(
        json.dumps(
            {
                "schema_version": AGENT_ACCEPT_REQUEST_SCHEMA,
                "run_id": run_id,
                "stage": stage,
                "rollout": {
                    "path": str(rollout),
                    "thread_id": thread_id,
                    "parent_thread_id": parent_id,
                },
                "launch_request": launch_spec,
                "payload": {"path": str(payload_path.resolve()), "sha256": _sha(payload_path)},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return request_path.resolve()


def _writer_artifact(root: Path, run_id: str, operation: str):
    staging = root / ".webnovel" / "tmp" / "write-runs" / run_id
    staging.mkdir(parents=True, exist_ok=True)
    name = "draft.md" if operation == "draft" else "polished.md"
    kind = "draft" if operation == "draft" else "polished"
    path = staging / name
    path.write_text("第一段正文。\n\n第二段正文。", encoding="utf-8")
    artifact = {
        "kind": kind,
        "path": str(path),
        "sha256": _sha(path),
        "bytes": len(path.read_bytes()),
        "word_count": len("第一段正文。第二段正文。"),
    }
    inputs = []
    if operation != "draft":
        draft = staging / "draft.md"
        inputs = [{"path": str(draft), "sha256": _sha(draft)}]
    resolutions = (
        [
            {
                "issue_index": 0,
                "issue_sha256": "a" * 64,
                "status": "resolved",
                "resolution_summary": "已按阻断问题完成定点修复。",
            }
        ]
        if operation == "targeted_fix"
        else None
    )
    manifest = {
        "schema_version": (
            "webnovel-writer-manifest/v2"
            if operation == "targeted_fix"
            else "webnovel-writer-manifest/v1"
        ),
        "run_id": run_id,
        "agent_name": "webnovel_writer",
        "operation": operation,
        "status": "completed",
        "inputs": inputs,
        "outputs": [artifact],
        "problems": [],
        "warnings": [],
    }
    if resolutions is not None:
        manifest["resolutions"] = resolutions
    manifest_path = staging / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    payload = {
        "schema_version": (
            "webnovel-writer-result/v2"
            if operation == "targeted_fix"
            else "webnovel-writer-result/v1"
        ),
        "status": "completed",
        "run_id": run_id,
        "operation": operation,
        "artifacts": [artifact],
        "manifest_path": str(manifest_path),
        "manifest_sha256": _sha(manifest_path),
        "problems": [],
        "warnings": [],
    }
    if resolutions is not None:
        payload["resolutions"] = resolutions
    return artifact, payload


def _record_until_commit(root: Path, run_id: str, *, mode="default"):
    for stage in ("preflight", "prewrite"):
        _advance_test_stage(root, run_id, stage, details={"gate_ok": True})
    _advance_test_stage(root, run_id, "context_agent")
    draft = root / ".webnovel" / "tmp" / "write-runs" / run_id / "draft.md"
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text("草稿", encoding="utf-8")
    draft_artifact = {"path": str(draft), "sha256": _sha(draft)}
    _advance_test_stage(root, run_id, "writer_draft", details={"accepted_artifacts": [draft_artifact]})
    if mode == "minimal":
        record_minimal_no_review(root, run_id)
    else:
        _advance_test_stage(root, run_id, "reviewer")
        _advance_test_stage(root, run_id, "review_pipeline", details={"review_sha256": "a" * 64})
    polished = draft.parent / "polished.md"
    polished.write_text("终稿", encoding="utf-8")
    final_artifact = {"kind": "polished", "path": str(polished), "sha256": _sha(polished)}
    _advance_test_stage(
        root,
        run_id,
        "writer_final",
        details={"operation": "polish", "accepted_artifacts": [final_artifact]},
    )
    promote_verified_writer_artifact(root, run_id, target_path="正文/第0001章-测试.md")
    _advance_test_stage(root, run_id, "data_agent")
    _advance_test_stage(root, run_id, "precommit", details={"gate_ok": True})
    commit = root / ".story-system" / "commits" / "chapter_001.commit.json"
    commit.parent.mkdir(parents=True, exist_ok=True)
    commit.write_text('{"meta":{"chapter":1,"status":"accepted"}}', encoding="utf-8")
    _advance_test_stage(
        root,
        run_id,
        "commit",
        details={
            "commit_status": "accepted",
            "commit": {"path": str(commit), "exists": True, "sha256": _sha(commit)},
        },
    )


def test_begin_records_mode_and_exact_route(tmp_path):
    transaction = begin_write_transaction(
        tmp_path,
        chapter=7,
        mode="fast",
        parent_model="gpt-5.6-sol",
        run_id="write-fast",
        test_only=True,
    )

    assert transaction["stages"] == list(WRITE_STAGES)
    assert [step["agent_name"] for step in transaction["route"]["steps"]] == [
        "webnovel_context_agent",
        "webnovel_writer",
        "webnovel_reviewer",
        "webnovel_data_agent",
    ]
    assert write_transaction_status(tmp_path, "write-fast")["next_stage"] == "preflight"


@pytest.mark.parametrize(
    "run_id",
    [".", "..", "...", ".hidden", "hidden.", "CON", "con.txt", "COM1", "LPT9.log"],
)
def test_write_run_id_is_canonical_and_never_creates_parent_aliases(tmp_path, run_id):
    with pytest.raises(WriteTransactionError, match="invalid run_id"):
        begin_write_transaction(
            tmp_path,
            chapter=1,
            mode="default",
            parent_model="gpt-5.6-sol",
            run_id=run_id,
            test_only=True,
        )

    assert not (tmp_path / ".webnovel").exists()


def test_test_only_receipt_chain_cannot_resume_or_promote_without_process_capability(tmp_path):
    run_id = "test-capability"
    transaction = begin_write_transaction(
        tmp_path,
        chapter=1,
        mode="default",
        parent_model="gpt-5.6-sol",
        run_id=run_id,
        test_only=True,
    )
    for stage in ("preflight", "prewrite"):
        _advance_test_stage(tmp_path, run_id, stage, details={"gate_ok": True})
    _advance_test_stage(tmp_path, run_id, "context_agent")
    draft = tmp_path / ".webnovel" / "tmp" / "write-runs" / run_id / "draft.md"
    draft.write_text("草稿", encoding="utf-8")
    _advance_test_stage(
        tmp_path,
        run_id,
        "writer_draft",
        details={"accepted_artifacts": [{"path": str(draft), "sha256": _sha(draft)}]},
    )
    _advance_test_stage(tmp_path, run_id, "reviewer")
    _advance_test_stage(
        tmp_path,
        run_id,
        "review_pipeline",
        details={"review_sha256": "a" * 64},
    )
    polished = draft.parent / "polished.md"
    polished.write_text("伪造终稿", encoding="utf-8")
    _advance_test_stage(
        tmp_path,
        run_id,
        "writer_final",
        details={
            "operation": "polish",
            "accepted_artifacts": [
                {"kind": "polished", "path": str(polished), "sha256": _sha(polished)}
            ],
        },
    )
    write_transaction._ACTIVE_TEST_RUNS.discard(
        write_transaction._test_run_key(
            tmp_path.resolve(),
            run_id,
            transaction["transaction_sha256"],
        )
    )

    with pytest.raises(WriteTransactionError, match="not active in this process"):
        promote_verified_writer_artifact(
            tmp_path,
            run_id,
            target_path="正文/第0001章-伪造.md",
            recovery_decision="replace_with_verified",
        )

    assert not (tmp_path / "正文").exists()


def test_transaction_writer_rechecks_control_lock_after_wait(tmp_path, monkeypatch):
    target = tmp_path / ".webnovel" / "write-runs" / "control-swap" / "transaction.json"
    lock_path = target.with_suffix(target.suffix + ".lock")
    entered = {"value": False}
    real_reparse = write_transaction._is_reparse_point

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
        if entered["value"] and Path(path) == lock_path:
            return True
        return real_reparse(Path(path))

    monkeypatch.setattr(write_transaction, "FileLock", WaitingLock)
    monkeypatch.setattr(write_transaction, "_is_reparse_point", becomes_reparse)
    with pytest.raises(WriteTransactionError, match="reparse-point"):
        begin_write_transaction(
            tmp_path,
            chapter=1,
            mode="default",
            parent_model="gpt-5.6-sol",
            run_id="control-swap",
            test_only=True,
        )
    assert not target.exists()


@pytest.mark.parametrize("mode", ["default", "fast", "minimal"])
def test_three_modes_reach_test_only_complete_with_exact_receipts(tmp_path, mode):
    run_id = f"write-{mode}"
    begin_write_transaction(
        tmp_path,
        chapter=1,
        mode=mode,
        parent_model="gpt-5.6-sol",
        run_id=run_id,
        test_only=True,
    )
    _record_until_commit(tmp_path, run_id, mode=mode)
    _advance_test_stage(
        tmp_path,
        run_id,
        "projections",
        details={"projection_status": {name: "done" for name in ("state", "index", "summary", "memory", "vector")}},
    )
    _advance_test_stage(tmp_path, run_id, "postcommit", details={"gate_ok": True})
    _advance_test_stage(tmp_path, run_id, "backup", status="skipped", details={"code": "skipped_non_git"})
    _advance_test_stage(tmp_path, run_id, "complete")

    status = write_transaction_status(tmp_path, run_id)
    assert status["status"] == "test_only_complete"
    assert status["production_complete"] is False
    assert status["completed_stages"] == list(WRITE_STAGES)
    if mode == "minimal":
        artifact = tmp_path / ".webnovel" / "tmp" / "write-runs" / run_id / "no-review.json"
        assert json.loads(artifact.read_text(encoding="utf-8"))["run_id"] == run_id


def test_complete_status_rechecks_final_body_and_commit_truth(tmp_path):
    run_id = "truth-audit"
    begin_write_transaction(
        tmp_path,
        chapter=1,
        mode="default",
        parent_model="gpt-5.6-sol",
        run_id=run_id,
        test_only=True,
    )
    _record_until_commit(tmp_path, run_id)
    _advance_test_stage(
        tmp_path,
        run_id,
        "projections",
        details={"projection_status": {name: "done" for name in write_transaction.PROJECTION_WRITERS}},
    )
    _advance_test_stage(tmp_path, run_id, "postcommit", details={"gate_ok": True})
    _advance_test_stage(
        tmp_path,
        run_id,
        "backup",
        status="skipped",
        details={"code": "skipped_non_git"},
    )
    _advance_test_stage(tmp_path, run_id, "complete")
    assert write_transaction_status(tmp_path, run_id)["status"] == "test_only_complete"

    polished = tmp_path / ".webnovel" / "tmp" / "write-runs" / run_id / "polished.md"
    polished_raw = polished.read_bytes()
    polished.unlink()
    stale = write_transaction_status(tmp_path, run_id)
    assert stale["status"] == "stale"
    assert "final_writer_artifact_stale" in stale["truth_audit"]["problems"]
    polished.write_bytes(polished_raw)

    body = tmp_path / "正文" / "第0001章-测试.md"
    body_raw = body.read_bytes()
    body.write_text("替换正文", encoding="utf-8")
    stale = write_transaction_status(tmp_path, run_id)
    assert "promotion_target_stale" in stale["truth_audit"]["problems"]
    body.write_bytes(body_raw)

    commit = tmp_path / ".story-system" / "commits" / "chapter_001.commit.json"
    commit_raw = commit.read_bytes()
    commit.write_text('{"meta":{"chapter":1,"status":"rejected"}}', encoding="utf-8")
    stale = write_transaction_status(tmp_path, run_id)
    assert "accepted_commit_stale" in stale["truth_audit"]["problems"]
    commit.write_bytes(commit_raw)
    assert write_transaction_status(tmp_path, run_id)["status"] == "test_only_complete"


def test_production_truth_audit_rechecks_five_projections_and_postcommit(tmp_path, monkeypatch):
    commit_path = tmp_path / ".story-system" / "commits" / "chapter_001.commit.json"
    commit_path.parent.mkdir(parents=True)
    commit_payload = {"meta": {"chapter": 1, "status": "accepted"}}
    commit_path.write_text(json.dumps(commit_payload), encoding="utf-8")
    gate = {
        "schema_version": "webnovel-write-gate/v1",
        "stage": "postcommit",
        "chapter": 1,
        "phase": "accepted",
        "ok": True,
        "errors": [],
    }
    projection_run = {
        "run_id": "projection-1",
        "commit_hash": write_transaction.commit_hash(commit_payload),
        "projection_status": {name: "done" for name in write_transaction.PROJECTION_WRITERS},
    }
    monkeypatch.setattr(write_transaction, "latest_projection_run", lambda root, chapter: projection_run)
    monkeypatch.setattr(write_transaction, "run_write_gate", lambda root, *, chapter, stage: gate)
    transaction = {
        "run_id": "audit-production",
        "chapter": 1,
        "test_only": False,
        "parent_task_binding_status": "verified_current_parent",
        "parent_thread_id": "parent-audit",
        "parent_rollout_path": "C:/trusted/parent-audit.jsonl",
        "parent_rollout_sha256": "f" * 64,
    }
    monkeypatch.setattr(
        write_transaction,
        "_current_parent_host_evidence",
        lambda: {
            "thread_id": "parent-audit",
            "rollout_path": "C:/trusted/parent-audit.jsonl",
            "rollout_sha256": "f" * 64,
            "rollout_bytes": 123,
        },
    )
    progress = {
        "next_stage": None,
        "completed": {
            "commit": {
                "details": {
                    "commit": write_transaction._file_signature(
                        commit_path, trusted_root=commit_path.parent
                    )
                }
            },
            "projections": {
                "details": {
                    "projection_run_id": "projection-1",
                    "projection_commit_hash": projection_run["commit_hash"],
                }
            },
            "postcommit": {
                "details": {"gate_report_sha256": write_transaction._sha256_bytes(write_transaction._canonical_bytes(gate))}
            },
        },
    }
    assert write_transaction._audit_current_truth(tmp_path, transaction, progress)["ok"] is True
    projection_run["projection_status"]["vector"] = "failed"
    audit = write_transaction._audit_current_truth(tmp_path, transaction, progress)
    assert "projection_truth_stale" in audit["problems"]


def test_production_agent_stage_requires_verified_runtime_evidence(tmp_path, monkeypatch):
    run_id = "write-live-evidence"
    _mock_route_ready(monkeypatch)
    transaction = begin_write_transaction(
        tmp_path,
        chapter=1,
        mode="default",
        parent_model="gpt-5.6-sol",
        workspace_root=tmp_path,
        run_id=run_id,
    )
    with pytest.raises(WriteTransactionError, match="truth-source"):
        record_write_stage(tmp_path, run_id, stage="preflight", status="completed", details={"gate_ok": True})
    _prepare_production_gates(tmp_path, run_id, monkeypatch)
    step = transaction["route"]["steps"][0]
    envelope = build_canned_envelope(step)
    envelope["evidence_source"] = "codex_task_event"
    payload = "\n".join(
        f"## {heading}\n内容"
        for heading in ("开篇委托", "这章的故事", "这章的人物", "怎么写更顺", "收在哪里")
    )

    with pytest.raises(WriteTransactionError, match="request-file"):
        accept_verified_agent_stage(
            tmp_path,
            run_id,
            stage="context_agent",
            envelope=envelope,
            payload=payload,
            verified_evidence=None,
        )

    evidence = VerifiedRuntimeEvidence(
        evidence_source="codex_task_event",
        agent_name="webnovel_context_agent",
        actual_model="gpt-5.6-luna",
        actual_reasoning_effort="high",
        thread_id="child-1",
        parent_thread_id="parent-1",
        raw_sha256="b" * 64,
    )
    with pytest.raises(WriteTransactionError, match="request-file"):
        accept_verified_agent_stage(
            tmp_path,
            run_id,
            stage="context_agent",
            envelope=envelope,
            payload=payload,
            verified_evidence=evidence,
        )


def test_accept_agent_cli_parses_rollout_and_bounded_payload_files(tmp_path, monkeypatch, capsys):
    run_id = "accept-cli"
    _mock_route_ready(monkeypatch)
    transaction = begin_write_transaction(
        tmp_path,
        chapter=1,
        mode="default",
        parent_model="gpt-5.6-sol",
        workspace_root=tmp_path,
        run_id=run_id,
    )
    _prepare_production_gates(tmp_path, run_id, monkeypatch)
    step = transaction["route"]["steps"][0]
    envelope = build_canned_envelope(step, evidence_source="codex_trace")
    payload = "\n".join(
        f"## {heading}\n内容"
        for heading in ("开篇委托", "这章的故事", "这章的人物", "怎么写更顺", "收在哪里")
    )
    sessions = tmp_path / "codex-sessions"
    rollout = _write_rollout(
        sessions,
        role="webnovel_context_agent",
        thread_id="context-child",
        parent_id="parent-task",
    )
    request = _write_agent_request(
        tmp_path,
        run_id,
        stage="context_agent",
        rollout=rollout,
        sessions_root=sessions,
        envelope=envelope,
        payload=payload,
        thread_id="context-child",
        parent_id="parent-task",
        desktop_no_marker=True,
    )

    code, result = _run_write_cli(
        monkeypatch,
        capsys,
        "--project-root",
        tmp_path,
        "accept-agent",
        "--run-id",
        run_id,
        "--request-file",
        request,
    )

    assert code == 0
    assert result["stage"] == "context_agent"
    assert result["details"]["evidence_trust"] == "verified_runtime"
    assert result["details"]["payload_sha256"] == hashlib.sha256(payload.encode("utf-8")).hexdigest()
    rollout_binding = result["details"]["source_bindings"]["rollout"]
    assert rollout_binding["agent_task_name"].startswith("wnw_")
    assert rollout_binding["agent_path"] == f"/root/{rollout_binding['agent_task_name']}"
    with pytest.raises(WriteTransactionError, match="consumed only once"):
        accept_agent_request(tmp_path, run_id, request)
    accepted_prefix = rollout.read_bytes()
    with rollout.open("ab") as handle:
        handle.write(b'{"type":"event_msg","payload":{"message":"later"}}\n')
    assert write_transaction_status(tmp_path, run_id)["next_stage"] == "writer_draft"
    tampered = bytearray(rollout.read_bytes())
    tampered[0] = ord("[")
    rollout.write_bytes(bytes(tampered))
    with pytest.raises(WriteTransactionError, match="trusted rollout prefix changed"):
        write_transaction_status(tmp_path, run_id)
    rollout.write_bytes(accepted_prefix)
    receipt_path = next(
        (tmp_path / ".webnovel" / "write-runs" / run_id / "receipts").glob(
            "*-context_agent.json"
        )
    )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["details"]["source_bindings"]["rollout"].pop("agent_task_name")
    receipt["details"]["source_bindings"]["rollout"].pop("agent_path")
    receipt.pop("receipt_sha256")
    receipt["receipt_sha256"] = write_transaction._receipt_hash(receipt)
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(WriteTransactionError, match="rollout binding is invalid"):
        write_transaction_status(tmp_path, run_id)


def test_prepare_agent_launch_binds_transaction_and_explicit_inputs(tmp_path, monkeypatch):
    run_id = "prepare-agent"
    _mock_route_ready(monkeypatch)
    begin_write_transaction(
        tmp_path,
        chapter=1,
        mode="default",
        parent_model="gpt-5.6-sol",
        workspace_root=tmp_path,
        run_id=run_id,
    )
    _prepare_production_gates(tmp_path, run_id, monkeypatch)
    state = tmp_path / ".webnovel" / "state.json"
    transaction_path = tmp_path / ".webnovel" / "write-runs" / run_id / "transaction.json"
    request = tmp_path / ".webnovel" / "tmp" / "write-runs" / run_id / "requests" / "context-inputs.json"
    request.write_text(
        json.dumps(
            {
                "schema_version": AGENT_LAUNCH_INPUT_SCHEMA,
                "run_id": run_id,
                "stage": "context_agent",
                "input_artifacts": [
                    {"path": str(transaction_path.resolve()), "sha256": _sha(transaction_path)},
                    {"path": str(state.resolve()), "sha256": _sha(state)},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    prepared = prepare_agent_launch_request(tmp_path, run_id, request.resolve())
    assert prepared["prompt_marker"].startswith(write_transaction.AGENT_PROMPT_MARKER_PREFIX)
    assert prepared["agent_task_name"] == write_transaction.derive_agent_task_name(
        prepared["prompt_marker"],
        prefix=write_transaction.AGENT_TASK_NAME_PREFIX,
    )
    assert Path(prepared["launch_request"]["path"]).name == "context_agent-launch.json"
    state.write_text('{"changed":true}', encoding="utf-8")
    with pytest.raises(WriteTransactionError, match="preflight state changed"):
        prepare_agent_launch_request(tmp_path, run_id, request.resolve())


def test_accept_agent_rejects_payload_not_equal_to_rollout_final_output(tmp_path, monkeypatch):
    run_id = "accept-output-mismatch"
    _mock_route_ready(monkeypatch, parent_thread_id="output-parent")
    transaction = begin_write_transaction(
        tmp_path,
        chapter=1,
        mode="default",
        parent_model="gpt-5.6-sol",
        workspace_root=tmp_path,
        run_id=run_id,
    )
    _prepare_production_gates(tmp_path, run_id, monkeypatch)
    payload = "\n".join(
        f"## {heading}\n内容"
        for heading in ("开篇委托", "这章的故事", "这章的人物", "怎么写更顺", "收在哪里")
    )
    sessions = tmp_path / "output-sessions"
    rollout = _write_rollout(
        sessions,
        role="webnovel_context_agent",
        thread_id="output-child",
        parent_id="output-parent",
    )
    request_path = _write_agent_request(
        tmp_path,
        run_id,
        stage="context_agent",
        rollout=rollout,
        sessions_root=sessions,
        envelope=build_canned_envelope(transaction["route"]["steps"][0]),
        payload=payload,
        thread_id="output-child",
        parent_id="output-parent",
        desktop_no_marker=True,
    )
    request = json.loads(request_path.read_text(encoding="utf-8"))
    payload_path = Path(request["payload"]["path"])
    payload_path.write_bytes((payload + "\n伪造").encode("utf-8"))
    request["payload"]["sha256"] = _sha(payload_path)
    request_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(WriteTransactionError, match="do not match"):
        accept_agent_request(tmp_path, run_id, request_path)


def test_accept_agent_request_rejects_relative_request_and_rollout_mismatch(tmp_path, monkeypatch):
    run_id = "accept-reject"
    _mock_route_ready(monkeypatch)
    transaction = begin_write_transaction(
        tmp_path,
        chapter=1,
        mode="default",
        parent_model="gpt-5.6-sol",
        workspace_root=tmp_path,
        run_id=run_id,
    )
    _prepare_production_gates(tmp_path, run_id, monkeypatch)
    with pytest.raises(WriteTransactionError, match="absolute"):
        accept_agent_request(tmp_path, run_id, "relative.json")

    step = transaction["route"]["steps"][0]
    sessions = tmp_path / "sessions-bad"
    rollout = _write_rollout(
        sessions,
        role="webnovel_context_agent",
        thread_id="wrong-effort",
        parent_id="parent-task",
        effort="high",
    )
    payload = "\n".join(
        f"## {heading}\n内容"
        for heading in ("开篇委托", "这章的故事", "这章的人物", "怎么写更顺", "收在哪里")
    )
    request = _write_agent_request(
        tmp_path,
        run_id,
        stage="context_agent",
        rollout=rollout,
        sessions_root=sessions,
        envelope=build_canned_envelope(step, evidence_source="codex_trace"),
        payload=payload,
        thread_id="wrong-effort",
        parent_id="parent-task",
    )
    with pytest.raises(WriteTransactionError, match="model or effort"):
        accept_agent_request(tmp_path, run_id, request)


def test_current_parent_host_evidence_and_cross_parent_child_are_fail_closed(tmp_path, monkeypatch):
    current_parent = "44444444-4444-4444-8444-444444444444"
    other_parent = "55555555-5555-4555-8555-555555555555"
    sessions = tmp_path / "trusted-sessions"
    rollout = sessions / "2026" / "08" / "08" / f"rollout-{current_parent}.jsonl"
    rollout.parent.mkdir(parents=True)
    parent_session = {"type": "session_meta", "payload": {"id": current_parent}}
    rollout.write_text(
        "\n".join(json.dumps(event) for event in (parent_session, parent_session)) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(write_transaction, "TRUSTED_CODEX_SESSIONS_ROOT", sessions)
    monkeypatch.setenv("CODEX_THREAD_ID", current_parent)
    evidence = write_transaction._current_parent_host_evidence()
    assert evidence["thread_id"] == current_parent
    bound = {
        "parent_thread_id": evidence["thread_id"],
        "parent_rollout_path": evidence["rollout_path"],
        "parent_rollout_sha256": evidence["rollout_sha256"],
        "parent_rollout_bytes": evidence["rollout_bytes"],
    }
    with rollout.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps({"type": "event_msg", "payload": {"message": "later"}}) + "\n")
    write_transaction._assert_current_parent_binding(bound)
    duplicate = sessions / f"duplicate-{current_parent}.jsonl"
    duplicate.write_bytes(rollout.read_bytes())
    with pytest.raises(WriteTransactionError, match="exactly one"):
        write_transaction._current_parent_host_evidence()
    duplicate.unlink()

    run_id = "cross-parent-child"
    _mock_route_ready(monkeypatch, parent_thread_id=current_parent)
    transaction = begin_write_transaction(
        tmp_path,
        chapter=1,
        mode="default",
        parent_model="gpt-5.6-sol",
        workspace_root=tmp_path,
        run_id=run_id,
    )
    _prepare_production_gates(tmp_path, run_id, monkeypatch)
    child_rollout = _write_rollout(
        sessions,
        role="webnovel_context_agent",
        thread_id="cross-parent-child-agent",
        parent_id=other_parent,
    )
    payload = "\n".join(
        f"## {heading}\n内容"
        for heading in ("开篇委托", "这章的故事", "这章的人物", "怎么写更顺", "收在哪里")
    )
    request = _write_agent_request(
        tmp_path,
        run_id,
        stage="context_agent",
        rollout=child_rollout,
        sessions_root=sessions,
        envelope=build_canned_envelope(transaction["route"]["steps"][0]),
        payload=payload,
        thread_id="cross-parent-child-agent",
        parent_id=other_parent,
    )

    with pytest.raises(WriteTransactionError, match="does not match the current write task"):
        accept_agent_request(tmp_path, run_id, request)


def test_production_begin_and_accept_recheck_managed_agent_readiness(tmp_path, monkeypatch):
    monkeypatch.setattr(
        write_transaction,
        "validate_route_readiness",
        lambda workspace, route: {"ready": False, "status": "blocked", "problems": [{"code": "stale"}], "agents": []},
    )
    with pytest.raises(WriteTransactionError, match="must be current"):
        begin_write_transaction(
            tmp_path,
            chapter=1,
            mode="default",
            parent_model="gpt-5.6-sol",
            workspace_root=tmp_path,
            run_id="missing-agents",
        )

    _mock_route_ready(monkeypatch)
    begin_write_transaction(
        tmp_path,
        chapter=1,
        mode="default",
        parent_model="gpt-5.6-sol",
        workspace_root=tmp_path,
        run_id="agents-drift",
    )
    monkeypatch.setattr(
        write_transaction,
        "validate_route_readiness",
        lambda workspace, route: {"ready": False, "status": "blocked", "problems": [], "agents": []},
    )
    request_dir = tmp_path / ".webnovel" / "tmp" / "write-runs" / "agents-drift" / "requests"
    request_dir.mkdir(parents=True)
    request = request_dir / "accept.json"
    request.write_text("{}", encoding="utf-8")
    with pytest.raises(WriteTransactionError, match="no longer current"):
        accept_agent_request(tmp_path, "agents-drift", request.resolve())


def test_canned_evidence_only_advances_test_transaction(tmp_path, monkeypatch):
    _mock_route_ready(monkeypatch)
    live = begin_write_transaction(
        tmp_path,
        chapter=1,
        mode="default",
        parent_model="gpt-5.6-sol",
        workspace_root=tmp_path,
        run_id="live",
    )
    _prepare_production_gates(tmp_path, "live", monkeypatch)
    envelope = build_canned_envelope(live["route"]["steps"][0])
    payload = "\n".join(
        f"## {heading}\n内容"
        for heading in ("开篇委托", "这章的故事", "这章的人物", "怎么写更顺", "收在哪里")
    )
    with pytest.raises(WriteTransactionError, match="request-file"):
        accept_verified_agent_stage(
            tmp_path,
            "live",
            stage="context_agent",
            envelope=envelope,
            payload=payload,
            verified_evidence=None,
            allow_canned=True,
        )

    test = begin_write_transaction(
        tmp_path,
        chapter=2,
        mode="default",
        parent_model="gpt-5.6-sol",
        run_id="canned",
        test_only=True,
    )
    for stage in ("preflight", "prewrite"):
        record_write_stage(tmp_path, "canned", stage=stage, status="completed", details={"gate_ok": True})
    receipt = accept_verified_agent_stage(
        tmp_path,
        "canned",
        stage="context_agent",
        envelope=build_canned_envelope(test["route"]["steps"][0]),
        payload=payload,
        verified_evidence=None,
        allow_canned=True,
    )
    assert receipt["details"]["evidence_trust"] == "canned_test_only"


def test_canned_writer_artifact_is_role_validated(tmp_path):
    run_id = "writer-fixture"
    transaction = begin_write_transaction(
        tmp_path,
        chapter=1,
        mode="default",
        parent_model="gpt-5.6-sol",
        run_id=run_id,
        test_only=True,
    )
    for stage in ("preflight", "prewrite"):
        _advance_test_stage(tmp_path, run_id, stage, details={"gate_ok": True})
    _advance_test_stage(tmp_path, run_id, "context_agent")
    artifact, payload = _writer_artifact(tmp_path, run_id, "draft")
    writer_step = next(step for step in transaction["route"]["steps"] if step["agent_name"] == "webnovel_writer")
    receipt = accept_verified_agent_stage(
        tmp_path,
        run_id,
        stage="writer_draft",
        envelope=build_canned_envelope(writer_step, artifacts=[artifact]),
        payload=payload,
        verified_evidence=None,
        allow_canned=True,
    )
    assert receipt["details"]["accepted_artifacts"][0]["path"].endswith("draft.md")


@pytest.mark.parametrize(
    ("blocking_count", "operation", "message"),
    [
        (0, "targeted_fix", "clean review permits only"),
        (1, "polish", "blocking review permits only"),
        (1, "targeted_fix", "requires production parent evidence"),
    ],
)
def test_writer_final_review_operation_is_fail_closed(
    tmp_path, blocking_count, operation, message
):
    root = tmp_path / f"case-{blocking_count}-{operation}"
    root.mkdir()
    run_id = "writer-final-gate"
    transaction = begin_write_transaction(
        root,
        chapter=1,
        mode="default",
        parent_model="gpt-5.6-sol",
        run_id=run_id,
        test_only=True,
    )
    for stage in ("preflight", "prewrite"):
        _advance_test_stage(root, run_id, stage, details={"gate_ok": True})
    _advance_test_stage(root, run_id, "context_agent")
    draft = root / ".webnovel" / "tmp" / "write-runs" / run_id / "draft.md"
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text("草稿", encoding="utf-8")
    _advance_test_stage(
        root,
        run_id,
        "writer_draft",
        details={"accepted_artifacts": [{"path": str(draft), "sha256": _sha(draft)}]},
    )
    _advance_test_stage(root, run_id, "reviewer")
    _advance_test_stage(
        root,
        run_id,
        "review_pipeline",
        details={"review_sha256": "a" * 64, "blocking_count": blocking_count},
    )
    artifact, payload = _writer_artifact(root, run_id, operation)
    writer_step = next(
        step for step in transaction["route"]["steps"] if step["agent_name"] == "webnovel_writer"
    )

    with pytest.raises(WriteTransactionError, match=message):
        accept_verified_agent_stage(
            root,
            run_id,
            stage="writer_final",
            envelope=build_canned_envelope(writer_step, artifacts=[artifact]),
            payload=payload,
            verified_evidence=None,
            allow_canned=True,
        )


def test_rollout_rejects_multiple_assistant_outputs_after_marker(tmp_path):
    rollout = tmp_path / "rollout-child-one.jsonl"
    marker = "BOUND MARKER"
    events = [
        {
            "type": "session_meta",
            "payload": {
                "id": "child-one",
                "parent_thread_id": "parent-one",
                "model": "gpt-5.6-luna",
                "source": {
                    "subagent": {
                        "thread_spawn": {
                            "parent_thread_id": "parent-one",
                            "agent_role": "webnovel_context_agent",
                            "prompt": marker,
                        }
                    }
                },
            },
        },
        {
            "type": "turn_context",
            "payload": {"turn_id": "turn-1", "model": "gpt-5.6-luna", "effort": "high"},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "first"}],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "second"}],
            },
        },
    ]
    raw = ("\n".join(json.dumps(item) for item in events) + "\n").encode("utf-8")

    with pytest.raises(WriteTransactionError, match="multiple assistant outputs"):
        write_transaction._parse_bound_agent_rollout(
            raw,
            rollout_path=rollout,
            thread_id="child-one",
            parent_thread_id="parent-one",
            expected_agent="webnovel_context_agent",
            expected_model="gpt-5.6-luna",
            expected_effort="high",
            expected_marker=marker,
            expected_task_name=None,
        )


def test_commit_failure_resume_never_reruns_agents(tmp_path):
    run_id = "resume-after-commit"
    begin_write_transaction(
        tmp_path,
        chapter=1,
        mode="default",
        parent_model="gpt-5.6-sol",
        run_id=run_id,
        test_only=True,
    )
    _record_until_commit(tmp_path, run_id)
    _advance_test_stage(
        tmp_path,
        run_id,
        "projections",
        status="failed",
        details={"code": "sqlite_busy"},
    )

    resume = build_write_resume_plan(tmp_path, run_id)
    assert resume["action"] == "retry_projection_only"
    assert resume["must_not_rerun_agents"] is True
    with pytest.raises(WriteTransactionError, match="out of order"):
        _advance_test_stage(tmp_path, run_id, "writer_draft")

    _advance_test_stage(
        tmp_path,
        run_id,
        "projections",
        details={"projection_status": {name: "done" for name in ("state", "index", "summary", "memory", "vector")}},
    )
    assert build_write_resume_plan(tmp_path, run_id)["action"] == "run_postcommit_only"


def test_promotion_requires_choice_for_changed_or_accepted_body(tmp_path):
    run_id = "promotion-choice"
    manuscript = tmp_path / "正文"
    manuscript.mkdir()
    body = manuscript / "第0001章-测试.md"
    body.write_text("作者版本", encoding="utf-8")
    begin_write_transaction(
        tmp_path,
        chapter=1,
        mode="default",
        parent_model="gpt-5.6-sol",
        run_id=run_id,
        test_only=True,
    )
    for stage in ("preflight", "prewrite"):
        _advance_test_stage(tmp_path, run_id, stage, details={"gate_ok": True})
    _advance_test_stage(tmp_path, run_id, "context_agent")
    _advance_test_stage(tmp_path, run_id, "writer_draft")
    _advance_test_stage(tmp_path, run_id, "reviewer")
    _advance_test_stage(tmp_path, run_id, "review_pipeline", details={"review_sha256": "a" * 64})
    polished = tmp_path / ".webnovel" / "tmp" / "write-runs" / run_id / "polished.md"
    polished.parent.mkdir(parents=True, exist_ok=True)
    polished.write_text("验证终稿", encoding="utf-8")
    _advance_test_stage(
        tmp_path,
        run_id,
        "writer_final",
        details={
            "operation": "polish",
            "accepted_artifacts": [
                {"kind": "polished", "path": str(polished), "sha256": _sha(polished)}
            ],
        },
    )
    body.write_text("作者本轮手改", encoding="utf-8")

    with pytest.raises(WriteRecoveryChoiceRequired) as caught:
        promote_verified_writer_artifact(tmp_path, run_id, target_path=body)
    assert caught.value.code == "chapter_file_changed"
    assert body.read_text(encoding="utf-8") == "作者本轮手改"

    promote_verified_writer_artifact(
        tmp_path,
        run_id,
        target_path=body,
        recovery_decision="replace_with_verified",
    )
    assert body.read_text(encoding="utf-8") == "验证终稿"


def test_promotion_detects_contract_edit_after_transaction_begin(tmp_path):
    run_id = "contract-edit-after-begin"
    begin_write_transaction(
        tmp_path,
        chapter=1,
        mode="default",
        parent_model="gpt-5.6-sol",
        run_id=run_id,
        test_only=True,
    )
    for stage in ("preflight", "prewrite"):
        _advance_test_stage(tmp_path, run_id, stage, details={"gate_ok": True})
    for stage in ("context_agent", "writer_draft", "reviewer"):
        _advance_test_stage(tmp_path, run_id, stage)
    _advance_test_stage(
        tmp_path,
        run_id,
        "review_pipeline",
        details={"review_sha256": "a" * 64},
    )
    polished = tmp_path / ".webnovel" / "tmp" / "write-runs" / run_id / "polished.md"
    polished.parent.mkdir(parents=True, exist_ok=True)
    polished.write_text("已验证终稿", encoding="utf-8")
    _advance_test_stage(
        tmp_path,
        run_id,
        "writer_final",
        details={
            "operation": "polish",
            "accepted_artifacts": [
                {"kind": "polished", "path": str(polished), "sha256": _sha(polished)}
            ],
        },
    )

    changed_contract = tmp_path / ".story-system" / "chapters" / "chapter_001.json"
    changed_contract.parent.mkdir(parents=True, exist_ok=True)
    changed_contract.write_text('{"goal":"作者刚修改"}', encoding="utf-8")

    with pytest.raises(WriteRecoveryChoiceRequired) as caught:
        promote_verified_writer_artifact(
            tmp_path,
            run_id,
            target_path="正文/第0001章-测试.md",
        )
    assert caught.value.code == "contracts_changed_after_begin"
    assert not (tmp_path / "正文" / "第0001章-测试.md").exists()


def test_promotion_rechecks_author_edit_after_waiting_for_chapter_lock(tmp_path, monkeypatch):
    run_id = "promotion-lock-wait"
    body = tmp_path / "正文" / "第0001章-测试.md"
    body.parent.mkdir(parents=True)
    body.write_text("开始版本", encoding="utf-8")
    begin_write_transaction(
        tmp_path,
        chapter=1,
        mode="default",
        parent_model="gpt-5.6-sol",
        run_id=run_id,
        test_only=True,
    )
    for stage in ("preflight", "prewrite"):
        _advance_test_stage(tmp_path, run_id, stage, details={"gate_ok": True})
    for stage in ("context_agent", "writer_draft", "reviewer"):
        _advance_test_stage(tmp_path, run_id, stage)
    _advance_test_stage(
        tmp_path,
        run_id,
        "review_pipeline",
        details={"review_sha256": "a" * 64},
    )
    polished = tmp_path / ".webnovel" / "tmp" / "write-runs" / run_id / "polished.md"
    polished.parent.mkdir(parents=True, exist_ok=True)
    polished.write_text("验证终稿", encoding="utf-8")
    _advance_test_stage(
        tmp_path,
        run_id,
        "writer_final",
        details={
            "operation": "polish",
            "accepted_artifacts": [
                {"kind": "polished", "path": str(polished), "sha256": _sha(polished)}
            ],
        },
    )
    real_lock = write_transaction.FileLock
    changed = {"value": False}

    class AuthorEditWhileWaiting:
        def __init__(self, path, timeout):
            self.path = Path(path)
            self.inner = real_lock(path, timeout=timeout)

        def __enter__(self):
            value = self.inner.__enter__()
            if self.path.parent.name == "write-locks" and not changed["value"]:
                body.write_text("作者等待期间手改", encoding="utf-8")
                changed["value"] = True
            return value

        def __exit__(self, *args):
            return self.inner.__exit__(*args)

    monkeypatch.setattr(write_transaction, "FileLock", AuthorEditWhileWaiting)
    with pytest.raises(WriteRecoveryChoiceRequired, match="本轮开始后被修改"):
        promote_verified_writer_artifact(tmp_path, run_id, target_path=body)
    assert body.read_text(encoding="utf-8") == "作者等待期间手改"


def test_accepted_commit_cannot_be_overwritten_even_with_replace_choice(tmp_path):
    run_id = "accepted-no-rewrite"
    body = tmp_path / "正文" / "第0001章-旧版.md"
    body.parent.mkdir(parents=True)
    body.write_text("已提交正文", encoding="utf-8")
    begin_write_transaction(
        tmp_path,
        chapter=1,
        mode="default",
        parent_model="gpt-5.6-sol",
        run_id=run_id,
        test_only=True,
    )
    for stage in ("preflight", "prewrite"):
        _advance_test_stage(tmp_path, run_id, stage, details={"gate_ok": True})
    _advance_test_stage(tmp_path, run_id, "context_agent")
    _advance_test_stage(tmp_path, run_id, "writer_draft")
    _advance_test_stage(tmp_path, run_id, "reviewer")
    _advance_test_stage(tmp_path, run_id, "review_pipeline", details={"review_sha256": "a" * 64})
    polished = tmp_path / ".webnovel" / "tmp" / "write-runs" / run_id / "polished.md"
    polished.parent.mkdir(parents=True, exist_ok=True)
    polished.write_text("新终稿", encoding="utf-8")
    _advance_test_stage(
        tmp_path,
        run_id,
        "writer_final",
        details={
            "operation": "polish",
            "accepted_artifacts": [
                {"kind": "polished", "path": str(polished), "sha256": _sha(polished)}
            ],
        },
    )
    commit = tmp_path / ".story-system" / "commits" / "chapter_001.commit.json"
    commit.parent.mkdir(parents=True)
    commit.write_text('{"meta":{"chapter":1,"status":"accepted"}}', encoding="utf-8")

    with pytest.raises(WriteTransactionError, match="amend transaction"):
        promote_verified_writer_artifact(
            tmp_path,
            run_id,
            target_path=body,
            recovery_decision="replace_with_verified",
        )
    assert body.read_text(encoding="utf-8") == "已提交正文"


def test_promotion_requires_unique_role_validated_polished_artifact(tmp_path):
    run_id = "bad-final-artifact"
    begin_write_transaction(
        tmp_path,
        chapter=1,
        mode="default",
        parent_model="gpt-5.6-sol",
        run_id=run_id,
        test_only=True,
    )
    for stage in ("preflight", "prewrite"):
        _advance_test_stage(tmp_path, run_id, stage, details={"gate_ok": True})
    for stage in ("context_agent", "writer_draft", "reviewer"):
        _advance_test_stage(tmp_path, run_id, stage)
    _advance_test_stage(tmp_path, run_id, "review_pipeline", details={"review_sha256": "a" * 64})
    wrong = tmp_path / ".webnovel" / "tmp" / "write-runs" / run_id / "other.md"
    wrong.parent.mkdir(parents=True, exist_ok=True)
    wrong.write_text("错误终稿", encoding="utf-8")
    _advance_test_stage(
        tmp_path,
        run_id,
        "writer_final",
        details={
            "operation": "polish",
            "accepted_artifacts": [
                {"kind": "polished", "path": str(wrong), "sha256": _sha(wrong)}
            ],
        },
    )
    with pytest.raises(WriteTransactionError, match="polished.md"):
        promote_verified_writer_artifact(tmp_path, run_id, target_path="正文/第0001章.md")


@pytest.mark.parametrize(
    ("stage", "details"),
    [
        ("preflight", {}),
        ("review_pipeline", {}),
        ("commit", {"commit_status": "rejected"}),
        ("projections", {"projection_status": {"state": "done"}}),
        ("postcommit", {"gate_ok": False}),
        ("backup", {"ok": False, "status": "failed"}),
    ],
)
def test_stage_specific_receipts_fail_closed(tmp_path, stage, details):
    with pytest.raises(WriteTransactionError):
        write_transaction._validate_stage_details(
            {"mode": "default"},
            {"next_stage": stage},
            stage,
            "completed",
            details,
        )


def test_precommit_binding_rejects_replaced_review_or_data_artifacts(tmp_path):
    run_id = "stale-commit-inputs"
    begin_write_transaction(
        tmp_path,
        chapter=1,
        mode="default",
        parent_model="gpt-5.6-sol",
        run_id=run_id,
        test_only=True,
    )
    for stage in ("preflight", "prewrite"):
        _advance_test_stage(tmp_path, run_id, stage, details={"gate_ok": True})
    for stage in ("context_agent", "writer_draft", "reviewer"):
        _advance_test_stage(tmp_path, run_id, stage)
    staging = tmp_path / ".webnovel" / "tmp" / "write-runs" / run_id
    runtime_tmp = tmp_path / ".webnovel" / "tmp"
    review_bound = staging / "review_results.json"
    review_current = runtime_tmp / "review_results.json"
    review_bound.write_text('{"blocking_count":0}', encoding="utf-8")
    review_current.write_bytes(review_bound.read_bytes())
    review_signature = write_transaction._file_signature(review_bound)
    _advance_test_stage(
        tmp_path,
        run_id,
        "review_pipeline",
        details={"review_sha256": _sha(review_bound), "review_artifact": review_signature},
    )
    polished = staging / "polished.md"
    polished.write_text("正文", encoding="utf-8")
    _advance_test_stage(
        tmp_path,
        run_id,
        "writer_final",
        details={
            "operation": "polish",
            "accepted_artifacts": [
                {"kind": "polished", "path": str(polished), "sha256": _sha(polished)}
            ],
        },
    )
    promote_verified_writer_artifact(tmp_path, run_id, target_path="正文/第0001章.md")
    bound_data = []
    for name in ("fulfillment_result.json", "disambiguation_result.json", "extraction_result.json"):
        bound = staging / "commit-inputs" / name
        bound.parent.mkdir(parents=True, exist_ok=True)
        bound.write_text(json.dumps({"name": name}), encoding="utf-8")
        (runtime_tmp / name).write_bytes(bound.read_bytes())
        bound_data.append(write_transaction._file_signature(bound))
    _advance_test_stage(tmp_path, run_id, "data_agent", details={"bound_artifacts": bound_data})

    hashes = write_transaction._verified_commit_input_hashes(tmp_path, run_id, write_transaction._load_transaction(tmp_path, run_id))
    assert set(hashes) == {
        "review_results.json",
        "fulfillment_result.json",
        "disambiguation_result.json",
        "extraction_result.json",
    }
    review_current.write_text("{}", encoding="utf-8")
    (runtime_tmp / "extraction_result.json").write_text("{}", encoding="utf-8")
    assert write_transaction._verified_commit_input_hashes(
        tmp_path,
        run_id,
        write_transaction._load_transaction(tmp_path, run_id),
    ) == hashes

    review_raw = review_bound.read_bytes()
    review_bound.write_text("{}", encoding="utf-8")
    with pytest.raises(WriteTransactionError, match="run-bound review snapshot changed"):
        write_transaction._verified_commit_input_hashes(
            tmp_path,
            run_id,
            write_transaction._load_transaction(tmp_path, run_id),
        )
    review_bound.write_bytes(review_raw)
    bound_extraction = staging / "commit-inputs" / "extraction_result.json"
    bound_extraction.write_text("{}", encoding="utf-8")
    with pytest.raises(WriteTransactionError, match="run-bound data artifact changed"):
        write_transaction._verified_commit_input_hashes(
            tmp_path,
            run_id,
            write_transaction._load_transaction(tmp_path, run_id),
        )


def test_receipt_chain_tamper_is_detected(tmp_path):
    run_id = "tamper"
    begin_write_transaction(
        tmp_path,
        chapter=1,
        mode="default",
        parent_model="gpt-5.6-sol",
        run_id=run_id,
        test_only=True,
    )
    record_write_stage(tmp_path, run_id, stage="preflight", status="completed", details={"gate_ok": True})
    receipt_path = next((tmp_path / ".webnovel" / "write-runs" / run_id / "receipts").glob("*.json"))
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["details"]["gate_ok"] = False
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises(WriteTransactionError, match="hash mismatch"):
        write_transaction_status(tmp_path, run_id)


def _run_write_cli(monkeypatch, capsys, *args):
    monkeypatch.setattr(sys, "argv", ["write-transaction", *map(str, args)])
    with pytest.raises(SystemExit) as caught:
        write_transaction.main()
    output = json.loads(capsys.readouterr().out)
    return caught.value.code, output


def test_write_transaction_cli_routes_and_failures(tmp_path, monkeypatch, capsys):
    _mock_route_ready(monkeypatch)
    state = tmp_path / ".webnovel" / "state.json"
    state.parent.mkdir(parents=True)
    state.write_text("{}", encoding="utf-8")
    code, begun = _run_write_cli(
        monkeypatch,
        capsys,
        "--project-root",
        tmp_path,
        "begin",
        "--chapter",
        "2",
        "--mode",
        "fast",
        "--parent-model",
        "gpt-5.6-sol",
        "--workspace-root",
        tmp_path,
        "--run-id",
        "cli-live",
    )
    assert code == 0
    assert begun["mode"] == "fast"
    preflight_request = _write_stage_request(tmp_path, "cli-live", "preflight")
    code, staged = _run_write_cli(
        monkeypatch,
        capsys,
        "--project-root",
        tmp_path,
        "stage",
        "--run-id",
        "cli-live",
        "--request-file",
        preflight_request,
    )
    assert code == 0
    assert staged["stage"] == "preflight"
    code, status = _run_write_cli(
        monkeypatch,
        capsys,
        "--project-root",
        tmp_path,
        "status",
        "--run-id",
        "cli-live",
    )
    assert code == 0
    assert status["next_stage"] == "prewrite"
    code, resume = _run_write_cli(
        monkeypatch,
        capsys,
        "--project-root",
        tmp_path,
        "resume",
        "--run-id",
        "cli-live",
    )
    assert code == 0
    assert resume["action"] == "resume_prewrite"

    invalid_request = tmp_path / ".webnovel" / "tmp" / "write-runs" / "cli-live" / "requests" / "invalid.json"
    invalid_request.write_text("{", encoding="utf-8")
    code, failed = _run_write_cli(
        monkeypatch,
        capsys,
        "--project-root",
        tmp_path,
        "stage",
        "--run-id",
        "cli-live",
        "--request-file",
        invalid_request.resolve(),
    )
    assert code == 2
    assert failed["status"] == "failed"

    run_id = "cli-prepared"
    begin_write_transaction(
        tmp_path,
        chapter=1,
        mode="minimal",
        parent_model="gpt-5.6-sol",
        run_id=run_id,
        test_only=True,
    )
    for stage_name in ("preflight", "prewrite"):
        _advance_test_stage(tmp_path, run_id, stage_name, details={"gate_ok": True})
    _advance_test_stage(tmp_path, run_id, "context_agent")
    draft = tmp_path / ".webnovel" / "tmp" / "write-runs" / run_id / "draft.md"
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text("草稿", encoding="utf-8")
    _advance_test_stage(
        tmp_path,
        run_id,
        "writer_draft",
        details={"accepted_artifacts": [{"path": str(draft), "sha256": _sha(draft)}]},
    )
    code, minimal = _run_write_cli(
        monkeypatch,
        capsys,
        "--project-root",
        tmp_path,
        "minimal-no-review",
        "--run-id",
        run_id,
    )
    assert code == 2
    assert "public CLI" in minimal["error"]
    minimal = record_minimal_no_review(tmp_path, run_id)
    assert set(minimal) == {"artifact", "reviewer_receipt", "review_pipeline_receipt"}
    polished = tmp_path / ".webnovel" / "tmp" / "write-runs" / run_id / "polished.md"
    polished.write_text("终稿", encoding="utf-8")
    _advance_test_stage(
        tmp_path,
        run_id,
        "writer_final",
        details={
            "operation": "polish",
            "accepted_artifacts": [
                {"kind": "polished", "path": str(polished), "sha256": _sha(polished)}
            ],
        },
    )
    target = tmp_path / "正文" / "第0001章-测试.md"
    code, promoted = _run_write_cli(
        monkeypatch,
        capsys,
        "--project-root",
        tmp_path,
        "promote",
        "--run-id",
        run_id,
        "--target",
        target,
    )
    assert code == 2
    assert "public CLI" in promoted["error"]
    promoted = promote_verified_writer_artifact(tmp_path, run_id, target_path=target)
    assert promoted["stage"] == "promotion"

    choice_run = "cli-choice"
    target.write_text("作者版本", encoding="utf-8")
    begin_write_transaction(
        tmp_path,
        chapter=1,
        mode="default",
        parent_model="gpt-5.6-sol",
        run_id=choice_run,
        test_only=True,
    )
    for stage_name in ("preflight", "prewrite"):
        _advance_test_stage(tmp_path, choice_run, stage_name, details={"gate_ok": True})
    _advance_test_stage(tmp_path, choice_run, "context_agent")
    _advance_test_stage(tmp_path, choice_run, "writer_draft")
    _advance_test_stage(tmp_path, choice_run, "reviewer")
    _advance_test_stage(tmp_path, choice_run, "review_pipeline", details={"review_sha256": "a" * 64})
    choice_polished = tmp_path / ".webnovel" / "tmp" / "write-runs" / choice_run / "polished.md"
    choice_polished.write_text("验证终稿", encoding="utf-8")
    _advance_test_stage(
        tmp_path,
        choice_run,
        "writer_final",
        details={
            "operation": "polish",
            "accepted_artifacts": [
                {"kind": "polished", "path": str(choice_polished), "sha256": _sha(choice_polished)}
            ],
        },
    )
    target.write_text("作者手改", encoding="utf-8")
    code, choice = _run_write_cli(
        monkeypatch,
        capsys,
        "--project-root",
        tmp_path,
        "promote",
        "--run-id",
        choice_run,
        "--target",
        target,
    )
    assert code == 2
    assert "public CLI" in choice["error"]


def test_verified_stage_request_rechecks_each_runtime_truth_source(tmp_path, monkeypatch):
    """Exercise production stage routing without trusting request booleans/maps."""

    import project_locator

    run_id = "verified-stage-matrix"
    transaction = {
        "schema_version": write_transaction.TRANSACTION_SCHEMA_VERSION,
        "run_id": run_id,
        "project_root": str(tmp_path.resolve()),
        "chapter": 1,
        "mode": "default",
        "test_only": False,
    }
    request = {
        "schema_version": STAGE_REQUEST_SCHEMA,
        "run_id": run_id,
        "stage": "preflight",
        "status": "completed",
        "error_code": "",
        "artifact": None,
        "_request_sha256": "a" * 64,
    }
    recorded = []

    monkeypatch.setattr(write_transaction, "_load_transaction", lambda root, selected: transaction)
    monkeypatch.setattr(write_transaction, "_load_run_request", lambda root, selected, path: dict(request))
    monkeypatch.setattr(write_transaction, "_assert_current_parent_binding", lambda payload: None)
    monkeypatch.setattr(project_locator, "resolve_project_root", lambda value, cwd: tmp_path.resolve())
    monkeypatch.setattr(
        write_transaction,
        "record_write_stage",
        lambda root, selected, **kwargs: recorded.append(kwargs) or kwargs,
    )
    monkeypatch.setattr(
        write_transaction,
        "run_write_gate",
        lambda root, *, chapter, stage: {
            "schema_version": "webnovel-write-gate/v1",
            "chapter": chapter,
            "stage": stage,
            "phase": "ready",
            "ok": True,
            "errors": [],
        },
    )
    monkeypatch.setattr(
        write_transaction,
        "_verified_commit_input_hashes",
        lambda root, selected, payload, **kwargs: {"review_results.json": "b" * 64},
    )
    monkeypatch.setattr(
        write_transaction,
        "_replayed_progress",
        lambda root, payload: ([], {"completed": {}}),
    )
    monkeypatch.setattr(
        write_transaction,
        "_sync_commit_review_truth",
        lambda root, payload, progress: {"sha256": "b" * 64},
    )
    monkeypatch.setattr(
        write_transaction,
        "_verified_materialized_commit_truth",
        lambda root, payload, progress, **kwargs: {"commit_status": "accepted"},
    )

    state = tmp_path / ".webnovel" / "state.json"
    state.parent.mkdir(parents=True)
    state.write_text("{}", encoding="utf-8")
    for stage in ("preflight", "prewrite", "precommit", "postcommit"):
        request["stage"] = stage
        result = record_verified_stage_request(tmp_path, run_id, tmp_path / "ignored.json")
        assert result["stage"] == stage
        assert result["details"]["gate_ok"] is True

    commit_path = tmp_path / ".story-system" / "commits" / "chapter_001.commit.json"
    commit_path.parent.mkdir(parents=True)
    commit_payload = {"meta": {"chapter": 1, "status": "accepted"}}
    commit_path.write_text(json.dumps(commit_payload), encoding="utf-8")
    request["stage"] = "commit"
    assert record_verified_stage_request(tmp_path, run_id, tmp_path / "ignored.json")["details"][
        "commit_status"
    ] == "accepted"

    projection_run = {
        "run_id": "projection-run",
        "commit_hash": write_transaction.commit_hash(commit_payload),
        "commit_path": str(commit_path.resolve()),
        "commit_status": "accepted",
    }
    monkeypatch.setattr(
        write_transaction,
        "latest_projection_run",
        lambda root, chapter: projection_run,
    )
    monkeypatch.setattr(
        write_transaction,
        "projection_status_from_run",
        lambda run: {name: "done" for name in write_transaction.PROJECTION_WRITERS},
    )
    request["stage"] = "projections"
    projected = record_verified_stage_request(tmp_path, run_id, tmp_path / "ignored.json")
    assert set(projected["details"]["projection_status"]) == write_transaction.PROJECTION_WRITERS

    monkeypatch.setattr(
        write_transaction,
        "_verified_backup_details",
        lambda root, payload, supplied: (
            "skipped",
            {"ok": True, "status": "skipped", "code": "skipped_non_git"},
        ),
    )
    request["stage"] = "backup"
    assert record_verified_stage_request(tmp_path, run_id, tmp_path / "ignored.json")["status"] == "skipped"

    request["stage"] = "complete"
    assert record_verified_stage_request(tmp_path, run_id, tmp_path / "ignored.json")["details"] == {
        "verified": True
    }

    request.update(stage="preflight", status="failed", error_code="project_missing")
    failed = record_verified_stage_request(tmp_path, run_id, tmp_path / "ignored.json")
    assert failed["details"]["code"] == "project_missing"

    request.update(stage="promotion", status="completed", error_code="")
    with pytest.raises(WriteTransactionError, match="dedicated acceptance"):
        record_verified_stage_request(tmp_path, run_id, tmp_path / "ignored.json")
    request["stage"] = "unknown"
    with pytest.raises(WriteTransactionError, match="no truth-source verifier"):
        record_verified_stage_request(tmp_path, run_id, tmp_path / "ignored.json")


def test_review_pipeline_stage_binds_exact_reviewer_payload_snapshot(tmp_path, monkeypatch):
    run_id = "review-stage-bind"
    staging = tmp_path / ".webnovel" / "tmp" / "write-runs" / run_id
    staging.mkdir(parents=True)
    raw_review = staging / "reviewer-payload.json"
    raw_review.write_text('{"source":"reviewer"}', encoding="utf-8")
    normalized = {
        "schema_version": "webnovel-review-result/v1",
        "chapter": 1,
        "issues": [],
        "blocking_count": 0,
    }
    runtime_review = tmp_path / ".webnovel" / "tmp" / "review_results.json"
    runtime_review.write_text(json.dumps(normalized), encoding="utf-8")
    transaction = {
        "run_id": run_id,
        "chapter": 1,
        "mode": "default",
        "test_only": False,
    }
    request = {
        "schema_version": STAGE_REQUEST_SCHEMA,
        "run_id": run_id,
        "stage": "review_pipeline",
        "status": "completed",
        "error_code": "",
        "artifact": {"path": str(runtime_review.resolve()), "sha256": _sha(runtime_review)},
        "_request_sha256": "c" * 64,
    }
    reviewer_receipt = {
        "details": {
            "source_bindings": {
                "payload": {"path": str(raw_review.resolve()), "sha256": _sha(raw_review)}
            }
        }
    }

    monkeypatch.setattr(write_transaction, "_load_transaction", lambda root, selected: transaction)
    monkeypatch.setattr(write_transaction, "_load_run_request", lambda root, selected, path: dict(request))
    monkeypatch.setattr(write_transaction, "_assert_current_parent_binding", lambda payload: None)
    monkeypatch.setattr(
        write_transaction,
        "_validated_receipts",
        lambda run_dir, **kwargs: [],
    )
    monkeypatch.setattr(
        write_transaction,
        "_derive_progress",
        lambda payload, receipts: {"completed": {"reviewer": reviewer_receipt}},
    )
    monkeypatch.setattr(
        write_transaction,
        "_replayed_progress",
        lambda root, payload: ([], {"completed": {"reviewer": reviewer_receipt}}),
    )
    monkeypatch.setattr(
        write_transaction,
        "parse_review_output",
        lambda chapter, raw, review_mode, strict: SimpleNamespace(to_dict=lambda: normalized),
    )
    monkeypatch.setattr(
        write_transaction,
        "record_write_stage",
        lambda root, selected, **kwargs: kwargs,
    )

    receipt = record_verified_stage_request(tmp_path, run_id, tmp_path / "ignored.json")
    bound = staging / "review_results.json"
    assert bound.read_bytes() == runtime_review.read_bytes()
    assert receipt["details"]["review_artifact"]["sha256"] == _sha(bound)
    assert receipt["details"]["resolution_status"] == "not_required"


def test_verified_backup_details_rechecks_skip_tag_and_no_change_truth(tmp_path, monkeypatch):
    import backup_manager

    transaction = {"run_id": "backup-verify", "chapter": 4}

    class NonGitManager:
        repository_status = "not_repo"
        repository_error = ""

        def __init__(self, project_root):
            self.project_root = project_root

    monkeypatch.setattr(backup_manager, "GitBackupManager", NonGitManager)
    status, details = write_transaction._verified_backup_details(
        tmp_path, transaction, {"artifact": None}
    )
    assert status == "skipped"
    assert details["code"] == "skipped_non_git"
    with pytest.raises(WriteTransactionError, match="must not trust"):
        write_transaction._verified_backup_details(
            tmp_path, transaction, {"artifact": {"path": "x", "sha256": "0" * 64}}
        )

    class ProbeErrorManager:
        repository_status = "error"
        repository_error = "injected Git probe error"

        def __init__(self, project_root):
            self.project_root = project_root

    monkeypatch.setattr(backup_manager, "GitBackupManager", ProbeErrorManager)
    with pytest.raises(WriteTransactionError, match="Git repository probe failed"):
        write_transaction._verified_backup_details(
            tmp_path, transaction, {"artifact": None}
        )

    class GitManager:
        repository_status = "exact"
        repository_error = ""

        def __init__(self, project_root):
            self.project_root = project_root

    monkeypatch.setattr(backup_manager, "GitBackupManager", GitManager)
    staging = tmp_path / ".webnovel" / "tmp" / "write-runs" / transaction["run_id"]
    staging.mkdir(parents=True)
    decision = {"receipt_sha256": "d" * 64}
    monkeypatch.setattr(
        backup_manager,
        "verify_git_backup_decision_receipt",
        lambda root, chapter, allowlist, receipt: decision,
    )

    receipt_path = staging / "backup-receipt.json"

    def verify_receipt(receipt):
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        monkeypatch.setattr(
            backup_manager,
            "verify_git_backup_authorization_state",
            lambda root, verified_decision: {
                "status": "completed",
                "binding": {"receipt_sha256": verified_decision["receipt_sha256"]},
                "result": receipt,
            },
        )
        return write_transaction._verified_backup_details(
            tmp_path,
            transaction,
            {"artifact": {"path": str(receipt_path.resolve()), "sha256": _sha(receipt_path)}},
        )

    completed_receipt = {
        "schema_version": backup_manager.BACKUP_RECEIPT_SCHEMA,
        "project_root": str(tmp_path.resolve()),
        "chapter": 4,
        "ok": True,
        "status": "completed",
        "code": "git_backup_created",
        "allowlist": ["正文/第0004章.md"],
        "changed_paths": ["正文/第0004章.md"],
        "commit": "commit-4",
        "tag": "ch0004",
        "authorization_token_sha256": "e" * 64,
        "decision_receipt_sha256": decision["receipt_sha256"],
        "decision_receipt": {"trusted": True},
    }

    status, details = verify_receipt(completed_receipt)
    assert status == "completed"
    assert details["receipt_artifact"]["sha256"] == _sha(receipt_path)

    no_change_receipt = {
        **completed_receipt,
        "status": "skipped",
        "code": "no_allowlisted_changes",
        "head": "head-4",
    }
    for key in ("changed_paths", "commit", "tag", "authorization_token_sha256"):
        no_change_receipt.pop(key, None)

    status, details = verify_receipt(no_change_receipt)
    assert status == "skipped"
    assert details["code"] == "no_allowlisted_changes"


def test_data_agent_acceptance_snapshots_all_three_commit_inputs(tmp_path, monkeypatch):
    run_id = "data-bind"
    route = write_transaction.build_workflow_route(
        "write", parent_model="gpt-5.6-sol", mode="default"
    )
    transaction = {
        "run_id": run_id,
        "test_only": True,
        "route": route,
    }
    artifact_root = tmp_path / ".webnovel" / "tmp"
    artifact_root.mkdir(parents=True)
    artifacts = []
    for name in (
        "fulfillment_result.json",
        "disambiguation_result.json",
        "extraction_result.json",
    ):
        path = artifact_root / name
        path.write_text(json.dumps({"name": name}), encoding="utf-8")
        artifacts.append({"name": name[:-5], "path": str(path.resolve()), "sha256": _sha(path)})

    monkeypatch.setattr(write_transaction, "_load_transaction", lambda root, selected: transaction)
    monkeypatch.setattr(
        write_transaction,
        "validate_agent_envelope",
        lambda *args, **kwargs: {"accepted": True},
    )
    monkeypatch.setattr(
        write_transaction,
        "validate_agent_payload",
        lambda *args, **kwargs: {"accepted": True, "accepted_artifacts": artifacts},
    )
    monkeypatch.setattr(
        write_transaction,
        "record_write_stage",
        lambda root, selected, **kwargs: kwargs,
    )
    step = next(item for item in route["steps"] if item["agent_name"] == "webnovel_data_agent")
    receipt = accept_verified_agent_stage(
        tmp_path,
        run_id,
        stage="data_agent",
        envelope=build_canned_envelope(step, artifacts=artifacts),
        payload={"schema_version": "webnovel-data-result/v1"},
        verified_evidence=None,
        allow_canned=True,
    )

    bound = receipt["details"]["bound_artifacts"]
    assert {Path(item["path"]).name for item in bound} == {
        "fulfillment_result.json",
        "disambiguation_result.json",
        "extraction_result.json",
    }
    assert all(Path(item["path"]).parent.name == "commit-inputs" for item in bound)


def test_current_truth_audit_revalidates_backup_registry_and_tag(tmp_path, monkeypatch):
    import backup_manager

    transaction = {
        "run_id": "backup-audit",
        "chapter": 4,
        "test_only": False,
        "parent_task_binding_status": "verified_current_parent",
    }
    decision = {"receipt_sha256": "f" * 64}
    details = {
        "schema_version": backup_manager.BACKUP_RECEIPT_SCHEMA,
        "project_root": str(tmp_path.resolve()),
        "chapter": 4,
        "ok": True,
        "status": "completed",
        "code": "git_backup_created",
        "allowlist": ["正文/第0004章.md"],
        "commit": "commit-audit",
        "tag": "ch0004",
        "decision_receipt": {"trusted": True},
    }

    class GitManager:
        repository_status = "exact"
        repository_error = ""

        def __init__(self, project_root):
            self.project_root = project_root

    monkeypatch.setattr(backup_manager, "GitBackupManager", GitManager)
    monkeypatch.setattr(
        backup_manager,
        "verify_git_backup_decision_receipt",
        lambda root, chapter, allowlist, receipt: decision,
    )
    monkeypatch.setattr(
        backup_manager,
        "verify_git_backup_authorization_state",
        lambda root, verified_decision: {
            "status": "completed",
            "result": details,
        },
    )
    progress = {"next_stage": "complete", "completed": {"backup": {"details": details}}}

    assert write_transaction._audit_current_truth(tmp_path, transaction, progress)["ok"] is True

    monkeypatch.setattr(
        backup_manager,
        "verify_git_backup_authorization_state",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("moved tag")),
    )
    audit = write_transaction._audit_current_truth(tmp_path, transaction, progress)
    assert any(problem.startswith("backup_truth_stale:") for problem in audit["problems"])


def test_production_promotion_is_blocked_without_current_parent_binding(tmp_path, monkeypatch):
    run_id = "parent-pending"
    transaction = {
        "run_id": run_id,
        "chapter": 1,
        "test_only": False,
        "parent_task_binding_status": "pending_live_evidence",
    }
    monkeypatch.setattr(write_transaction, "_load_transaction", lambda root, selected: transaction)

    with pytest.raises(WriteTransactionError, match="current-parent rollout evidence"):
        promote_verified_writer_artifact(
            tmp_path,
            run_id,
            target_path="正文/第0001章.md",
        )

    assert not (tmp_path / "正文" / "第0001章.md").exists()


def test_begin_and_transaction_descriptor_fail_closed_matrix(tmp_path, monkeypatch):
    for kwargs, message in (
        ({"chapter": 0, "mode": "default", "parent_model": "gpt-5.6-sol"}, "positive integer"),
        ({"chapter": 1, "mode": "turbo", "parent_model": "gpt-5.6-sol"}, "unsupported write mode"),
        ({"chapter": 1, "mode": "default", "parent_model": ""}, "parent_model"),
        (
            {"chapter": 1, "mode": "default", "parent_model": "gpt-5.6-sol", "run_id": "../bad"},
            "invalid run_id",
        ),
    ):
        with pytest.raises(WriteTransactionError, match=message):
            begin_write_transaction(tmp_path, test_only=True, **kwargs)

    with pytest.raises(WriteTransactionError, match="explicit workspace_root"):
        begin_write_transaction(
            tmp_path,
            chapter=1,
            mode="default",
            parent_model="gpt-5.6-sol",
            run_id="missing-workspace",
        )
    monkeypatch.setattr(
        write_transaction,
        "validate_route_readiness",
        lambda workspace, route: (_ for _ in ()).throw(RuntimeError("readiness boom")),
    )
    with pytest.raises(WriteTransactionError, match="readiness check failed"):
        begin_write_transaction(
            tmp_path,
            chapter=1,
            mode="default",
            parent_model="gpt-5.6-sol",
            workspace_root=tmp_path,
            run_id="readiness-error",
        )

    transaction = begin_write_transaction(
        tmp_path,
        chapter=1,
        mode="default",
        parent_model="gpt-5.6-sol",
        run_id="descriptor-errors",
        test_only=True,
    )
    transaction_path = tmp_path / ".webnovel" / "write-runs" / "descriptor-errors" / "transaction.json"
    original = transaction_path.read_bytes()
    with pytest.raises(WriteTransactionError, match="invalid run_id"):
        write_transaction._load_transaction(tmp_path, "../escape")
    for field, value, message in (
        ("schema_version", "bad", "unsupported transaction schema"),
        ("transaction_sha256", "0" * 64, "descriptor hash mismatch"),
    ):
        tampered = dict(transaction)
        tampered[field] = value
        transaction_path.write_text(json.dumps(tampered), encoding="utf-8")
        with pytest.raises(WriteTransactionError, match=message):
            write_transaction._load_transaction(tmp_path, "descriptor-errors")
    transaction_path.write_bytes(original)


def test_agent_acceptance_rejection_matrix_records_stable_failure_codes(tmp_path, monkeypatch):
    route = write_transaction.build_workflow_route(
        "write", parent_model="gpt-5.6-sol", mode="default"
    )
    transaction = {"run_id": "agent-reject", "test_only": True, "route": route}
    recorded = []
    monkeypatch.setattr(write_transaction, "_load_transaction", lambda root, selected: transaction)
    monkeypatch.setattr(
        write_transaction,
        "record_write_stage",
        lambda root, selected, **kwargs: recorded.append(kwargs) or kwargs,
    )
    monkeypatch.setattr(
        write_transaction,
        "validate_agent_envelope",
        lambda *args, **kwargs: {"accepted": False, "code": "identity_bad"},
    )
    monkeypatch.setattr(
        write_transaction,
        "validate_agent_payload",
        lambda *args, **kwargs: {"accepted": True, "accepted_artifacts": []},
    )
    context_step = next(
        item for item in route["steps"] if item["agent_name"] == "webnovel_context_agent"
    )
    envelope = build_canned_envelope(context_step)

    with pytest.raises(WriteTransactionError, match="not an agent stage"):
        accept_verified_agent_stage(
            tmp_path,
            "agent-reject",
            stage="preflight",
            envelope=envelope,
            payload="x",
            verified_evidence=None,
            allow_canned=True,
        )
    with pytest.raises(WriteTransactionError, match="identity_bad"):
        accept_verified_agent_stage(
            tmp_path,
            "agent-reject",
            stage="context_agent",
            envelope=envelope,
            payload="x",
            verified_evidence=None,
            allow_canned=True,
        )
    assert recorded[-1]["details"]["code"] == "identity_bad"

    monkeypatch.setattr(
        write_transaction,
        "validate_agent_envelope",
        lambda *args, **kwargs: {"accepted": True},
    )
    monkeypatch.setattr(
        write_transaction,
        "validate_agent_payload",
        lambda *args, **kwargs: {"accepted": False, "code": "payload_bad"},
    )
    with pytest.raises(WriteTransactionError, match="payload_bad"):
        accept_verified_agent_stage(
            tmp_path,
            "agent-reject",
            stage="context_agent",
            envelope=envelope,
            payload="x",
            verified_evidence=None,
            allow_canned=True,
        )
    assert recorded[-1]["details"]["code"] == "payload_bad"

    artifact = {"path": str((tmp_path / "artifact.md").resolve()), "sha256": "a" * 64}
    monkeypatch.setattr(
        write_transaction,
        "validate_agent_payload",
        lambda *args, **kwargs: {"accepted": True, "accepted_artifacts": [artifact]},
    )
    with pytest.raises(WriteTransactionError, match="envelope artifacts"):
        accept_verified_agent_stage(
            tmp_path,
            "agent-reject",
            stage="context_agent",
            envelope=envelope,
            payload="x",
            verified_evidence=None,
            allow_canned=True,
        )
    assert recorded[-1]["details"]["code"] == "envelope_artifact_mismatch"

    transaction["test_only"] = False
    with pytest.raises(WriteTransactionError, match="canned evidence is forbidden"):
        accept_verified_agent_stage(
            tmp_path,
            "agent-reject",
            stage="context_agent",
            envelope=envelope,
            payload="x",
            verified_evidence=None,
            allow_canned=True,
            _source_bindings={},
        )


def test_stage_detail_and_minimal_no_review_negative_matrix(tmp_path, monkeypatch):
    with pytest.raises(WriteTransactionError, match="fresh minimal_mode"):
        write_transaction._validate_stage_details(
            {"mode": "minimal"}, {}, "reviewer", "skipped", {"code": "old"}
        )
    with pytest.raises(WriteTransactionError, match="may not be skipped"):
        write_transaction._validate_stage_details(
            {"mode": "default"}, {}, "prewrite", "skipped", {}
        )
    with pytest.raises(WriteTransactionError, match="stable error code"):
        write_transaction._validate_stage_details(
            {"mode": "default"}, {}, "prewrite", "failed", {}
        )
    with pytest.raises(WriteTransactionError, match="must skip review_pipeline"):
        write_transaction._validate_stage_details(
            {"mode": "minimal"}, {}, "review_pipeline", "completed", {"review_sha256": "a" * 64}
        )
    with pytest.raises(WriteTransactionError, match="done or skipped"):
        write_transaction._validate_stage_details(
            {"mode": "default"},
            {},
            "projections",
            "completed",
            {"projection_status": {name: "failed" for name in write_transaction.PROJECTION_WRITERS}},
        )
    with pytest.raises(WriteTransactionError, match="before every prior receipt"):
        write_transaction._validate_stage_details(
            {"mode": "default"}, {"next_stage": "backup"}, "complete", "completed", {}
        )

    transaction = {
        "run_id": "minimal-negative",
        "mode": "default",
        "chapter": 1,
        "test_only": True,
    }
    monkeypatch.setattr(write_transaction, "_load_transaction", lambda root, selected: transaction)
    with pytest.raises(WriteTransactionError, match="only valid in minimal mode"):
        record_minimal_no_review(tmp_path, "minimal-negative")
    transaction["mode"] = "minimal"
    monkeypatch.setattr(
        write_transaction,
        "_validated_receipts",
        lambda run_dir, **kwargs: [],
    )
    monkeypatch.setattr(
        write_transaction,
        "_derive_progress",
        lambda payload, receipts: {"next_stage": "context_agent", "completed": {}},
    )
    with pytest.raises(WriteTransactionError, match="out of order"):
        record_minimal_no_review(tmp_path, "minimal-negative")
    monkeypatch.setattr(
        write_transaction,
        "_derive_progress",
        lambda payload, receipts: {"next_stage": "reviewer", "completed": {}},
    )
    with pytest.raises(WriteTransactionError, match="accepted draft artifact"):
        record_minimal_no_review(tmp_path, "minimal-negative")


def test_verified_backup_details_rejects_each_stale_truth_source(tmp_path, monkeypatch):
    import backup_manager

    class GitManager:
        repository_status = "exact"
        repository_error = ""

        def __init__(self, project_root):
            self.project_root = project_root

    transaction = {"run_id": "backup-negative", "chapter": 2}
    receipt = {
        "schema_version": backup_manager.BACKUP_RECEIPT_SCHEMA,
        "project_root": str(tmp_path.resolve()),
        "chapter": 2,
        "ok": True,
        "status": "completed",
        "code": "git_backup_created",
        "allowlist": ["正文/第0002章.md"],
        "changed_paths": ["正文/第0002章.md"],
        "commit": "commit-2",
        "tag": "ch0002",
        "authorization_token_sha256": "a" * 64,
        "decision_receipt_sha256": "b" * 64,
        "decision_receipt": {"trusted": True},
    }
    decision = {"receipt_sha256": "b" * 64}
    monkeypatch.setattr(backup_manager, "GitBackupManager", GitManager)
    monkeypatch.setattr(
        write_transaction,
        "_request_artifact",
        lambda *args, **kwargs: (
            tmp_path / "receipt.json",
            {"path": "receipt", "sha256": "c" * 64},
            json.dumps(receipt, ensure_ascii=False).encode("utf-8"),
        ),
    )
    monkeypatch.setattr(
        backup_manager,
        "verify_git_backup_decision_receipt",
        lambda *args, **kwargs: decision,
    )
    monkeypatch.setattr(
        backup_manager,
        "verify_git_backup_authorization_state",
        lambda *args, **kwargs: {
            "status": "completed",
            "binding": {"receipt_sha256": decision["receipt_sha256"]},
            "result": dict(receipt),
        },
    )

    receipt["status"] = "failed"
    with pytest.raises(WriteTransactionError, match="identity/status"):
        write_transaction._verified_backup_details(tmp_path, transaction, {"artifact": {}})
    receipt.update(status="completed", allowlist=[])
    with pytest.raises(WriteTransactionError, match="allowlist is invalid"):
        write_transaction._verified_backup_details(tmp_path, transaction, {"artifact": {}})
    receipt["allowlist"] = ["正文/第0002章.md"]
    monkeypatch.setattr(
        backup_manager,
        "verify_git_backup_decision_receipt",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("bad decision")),
    )
    with pytest.raises(WriteTransactionError, match="user-decision receipt"):
        write_transaction._verified_backup_details(tmp_path, transaction, {"artifact": {}})
    monkeypatch.setattr(
        backup_manager,
        "verify_git_backup_decision_receipt",
        lambda *args, **kwargs: decision,
    )
    monkeypatch.setattr(
        backup_manager,
        "verify_git_backup_authorization_state",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("bad registry")),
    )
    with pytest.raises(WriteTransactionError, match="authorization registry"):
        write_transaction._verified_backup_details(tmp_path, transaction, {"artifact": {}})
    monkeypatch.setattr(
        backup_manager,
        "verify_git_backup_authorization_state",
        lambda *args, **kwargs: {"status": "failed", "binding": {}, "result": {}},
    )
    with pytest.raises(WriteTransactionError, match="does not complete"):
        write_transaction._verified_backup_details(tmp_path, transaction, {"artifact": {}})

    monkeypatch.setattr(
        backup_manager,
        "verify_git_backup_authorization_state",
        lambda *args, **kwargs: {
            "status": "completed",
            "binding": {"receipt_sha256": decision["receipt_sha256"]},
            "result": dict(receipt),
        },
    )
    receipt["tag"] = "ch9999"
    with pytest.raises(WriteTransactionError, match="tag identity"):
        write_transaction._verified_backup_details(tmp_path, transaction, {"artifact": {}})

    receipt["tag"] = "ch0002"
    receipt["authorization_token_sha256"] = "bad"
    with pytest.raises(WriteTransactionError, match="authorization binding"):
        write_transaction._verified_backup_details(tmp_path, transaction, {"artifact": {}})

    receipt.update(
        status="skipped",
        code="no_allowlisted_changes",
        head="head-2",
        authorization_token_sha256="a" * 64,
    )
    monkeypatch.setattr(
        backup_manager,
        "verify_git_backup_authorization_state",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("stale no-change truth")),
    )
    with pytest.raises(WriteTransactionError, match="authorization registry"):
        write_transaction._verified_backup_details(tmp_path, transaction, {"artifact": {}})

    receipt.update(status="skipped", code="unknown", authorization_token_sha256="a" * 64)
    monkeypatch.setattr(
        backup_manager,
        "verify_git_backup_authorization_state",
        lambda *args, **kwargs: {
            "status": "completed",
            "binding": {"receipt_sha256": decision["receipt_sha256"]},
            "result": dict(receipt),
        },
    )
    with pytest.raises(WriteTransactionError, match="unsupported Git backup skip"):
        write_transaction._verified_backup_details(tmp_path, transaction, {"artifact": {}})


def test_verified_stage_request_rejects_unverified_shapes_and_claims(tmp_path, monkeypatch):
    transaction = {
        "run_id": "stage-negative",
        "chapter": 1,
        "mode": "default",
        "test_only": True,
    }
    request = {
        "schema_version": STAGE_REQUEST_SCHEMA,
        "run_id": "stage-negative",
        "stage": "prewrite",
        "status": "completed",
        "error_code": "",
        "artifact": None,
        "_request_sha256": "a" * 64,
    }
    monkeypatch.setattr(write_transaction, "_load_transaction", lambda root, selected: transaction)
    monkeypatch.setattr(write_transaction, "_load_run_request", lambda root, selected, path: dict(request))
    monkeypatch.setattr(write_transaction, "_assert_current_parent_binding", lambda payload: None)
    with pytest.raises(WriteTransactionError, match="production-only"):
        record_verified_stage_request(tmp_path, "stage-negative", tmp_path / "ignored.json")

    transaction["test_only"] = False
    request["extra"] = True
    with pytest.raises(WriteTransactionError, match="malformed stage request"):
        record_verified_stage_request(tmp_path, "stage-negative", tmp_path / "ignored.json")
    request.pop("extra")
    request.update(status="failed", error_code="")
    with pytest.raises(WriteTransactionError, match="requires error_code"):
        record_verified_stage_request(tmp_path, "stage-negative", tmp_path / "ignored.json")
    request["status"] = "pending"
    with pytest.raises(WriteTransactionError, match="status=completed"):
        record_verified_stage_request(tmp_path, "stage-negative", tmp_path / "ignored.json")

    request.update(status="completed", error_code="")
    monkeypatch.setattr(
        write_transaction,
        "run_write_gate",
        lambda *args, **kwargs: {"ok": False},
    )
    with pytest.raises(WriteTransactionError, match="truth-source gate is blocked"):
        record_verified_stage_request(tmp_path, "stage-negative", tmp_path / "ignored.json")


def test_agent_launch_requires_current_run_lineage_and_rejects_other_run(tmp_path, monkeypatch):
    parent_id = "11111111-1111-4111-8111-111111111111"
    _mock_route_ready(monkeypatch, parent_thread_id=parent_id)
    current_id = "lineage-current"
    other_id = "lineage-other"
    begin_write_transaction(
        tmp_path,
        chapter=1,
        mode="default",
        parent_model="gpt-5.6-sol",
        workspace_root=tmp_path,
        run_id=current_id,
    )
    begin_write_transaction(
        tmp_path,
        chapter=2,
        mode="default",
        parent_model="gpt-5.6-sol",
        workspace_root=tmp_path,
        run_id=other_id,
    )
    _prepare_production_gates(tmp_path, current_id, monkeypatch)
    request = (
        tmp_path
        / ".webnovel"
        / "tmp"
        / "write-runs"
        / current_id
        / "requests"
        / "context-lineage.json"
    )
    state = tmp_path / ".webnovel" / "state.json"

    def write_inputs(items):
        request.write_text(
            json.dumps(
                {
                    "schema_version": AGENT_LAUNCH_INPUT_SCHEMA,
                    "run_id": current_id,
                    "stage": "context_agent",
                    "input_artifacts": items,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    write_inputs([{"path": str(state.resolve()), "sha256": _sha(state)}])
    with pytest.raises(WriteTransactionError, match="missing current-run lineage"):
        prepare_agent_launch_request(tmp_path, current_id, request.resolve())

    current_transaction = tmp_path / ".webnovel" / "write-runs" / current_id / "transaction.json"
    other_transaction = tmp_path / ".webnovel" / "write-runs" / other_id / "transaction.json"
    write_inputs(
        [
            {"path": str(current_transaction.resolve()), "sha256": _sha(current_transaction)},
            {"path": str(other_transaction.resolve()), "sha256": _sha(other_transaction)},
        ]
    )
    with pytest.raises(WriteTransactionError, match="another write run artifact"):
        prepare_agent_launch_request(tmp_path, current_id, request.resolve())
    assert not (request.parent / "context_agent-launch.json").exists()


def test_current_parent_must_be_top_level_and_public_stage_rechecks_thread(tmp_path, monkeypatch):
    current_id = "22222222-2222-4222-8222-222222222222"
    sessions = tmp_path / "top-level-sessions"
    rollout = sessions / "2026" / "08" / "08" / f"rollout-{current_id}.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {
                    "id": current_id,
                    "parent_thread_id": "33333333-3333-4333-8333-333333333333",
                    "source": {"subagent": {"thread_spawn": {"depth": 1}}},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(write_transaction, "TRUSTED_CODEX_SESSIONS_ROOT", sessions)
    monkeypatch.setenv("CODEX_THREAD_ID", current_id)
    with pytest.raises(WriteTransactionError, match="top-level"):
        write_transaction._current_parent_host_evidence()

    _mock_route_ready(monkeypatch, parent_thread_id=current_id)
    run_id = "parent-recheck"
    begin_write_transaction(
        tmp_path,
        chapter=1,
        mode="default",
        parent_model="gpt-5.6-sol",
        workspace_root=tmp_path,
        run_id=run_id,
    )
    request = _write_stage_request(tmp_path, run_id, "preflight")
    other_id = "44444444-4444-4444-8444-444444444444"
    monkeypatch.setattr(
        write_transaction,
        "_current_parent_host_evidence",
        lambda: {
            "thread_id": other_id,
            "rollout_path": f"C:/trusted/{other_id}.jsonl",
            "rollout_sha256": "e" * 64,
        },
    )
    with pytest.raises(WriteTransactionError, match="changed after write begin"):
        record_verified_stage_request(tmp_path, run_id, request)
    with pytest.raises(WriteTransactionError, match="changed after write begin"):
        write_transaction.write_transaction_status(tmp_path, run_id)


def test_request_artifact_signature_and_json_share_one_stable_snapshot(tmp_path, monkeypatch):
    path = tmp_path / "artifact.json"
    raw_a = b'{"value":"A"}'
    raw_b = b'{"value":"B"}'
    path.write_bytes(raw_a)
    real_snapshot = write_transaction._stable_read_snapshot
    swapped = {"done": False}

    def swap_after_snapshot(selected, **kwargs):
        raw, stat_result = real_snapshot(selected, **kwargs)
        if Path(selected) == path and not swapped["done"]:
            path.write_bytes(raw_b)
            swapped["done"] = True
        return raw, stat_result

    monkeypatch.setattr(write_transaction, "_stable_read_snapshot", swap_after_snapshot)
    selected, signature, raw = write_transaction._request_artifact(
        tmp_path.resolve(),
        "snapshot-run",
        {"path": str(path.resolve()), "sha256": hashlib.sha256(raw_a).hexdigest()},
        allowed_root=tmp_path.resolve(),
    )
    assert selected == path.resolve()
    assert signature["sha256"] == hashlib.sha256(raw_a).hexdigest()
    assert write_transaction._json_object_from_bytes(raw, selected) == {"value": "A"}
    assert json.loads(path.read_text(encoding="utf-8")) == {"value": "B"}


def test_writer_manifest_inputs_must_exactly_match_bound_launch(tmp_path):
    run_id = "writer-manifest-lineage"
    staging = tmp_path / ".webnovel" / "tmp" / "write-runs" / run_id
    staging.mkdir(parents=True)
    expected_input = tmp_path / "expected.txt"
    other_input = tmp_path / "other.txt"
    expected_input.write_text("expected", encoding="utf-8")
    other_input.write_text("other", encoding="utf-8")
    manifest = {
        "inputs": [{"path": str(expected_input.resolve()), "sha256": _sha(expected_input)}]
    }
    manifest_path = staging / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    payload = {
        "manifest_path": str(manifest_path.resolve()),
        "manifest_sha256": _sha(manifest_path),
    }
    launch = {
        "input_artifacts": [{"path": str(other_input.resolve()), "sha256": _sha(other_input)}]
    }
    with pytest.raises(WriteTransactionError, match="manifest inputs do not match"):
        write_transaction._verified_writer_manifest_binding(
            tmp_path.resolve(),
            run_id,
            "writer_draft",
            payload,
            launch,
        )

    launch["input_artifacts"] = manifest["inputs"]
    binding = write_transaction._verified_writer_manifest_binding(
        tmp_path.resolve(),
        run_id,
        "writer_draft",
        payload,
        launch,
    )
    assert binding["sha256"] == _sha(manifest_path)


def test_every_agent_stage_lineage_is_derived_from_current_run_receipts(tmp_path):
    run_id = "all-lineage"
    transaction = begin_write_transaction(
        tmp_path,
        chapter=1,
        mode="default",
        parent_model="gpt-5.6-sol",
        run_id=run_id,
        test_only=True,
    )
    staging = tmp_path / ".webnovel" / "tmp" / "write-runs" / run_id
    context = {"path": str((staging / "context.md").resolve()), "sha256": "1" * 64}
    draft = {"path": str((staging / "draft.md").resolve()), "sha256": "2" * 64}
    review = {"path": str((staging / "review.json").resolve()), "sha256": "3" * 64}
    final = {"path": str((staging / "polished.md").resolve()), "sha256": "4" * 64}
    target = {"path": str((tmp_path / "正文" / "第0001章.md").resolve()), "sha256": "5" * 64}
    progress = {
        "completed": {
            "context_agent": {"details": {"source_bindings": {"payload": context}}},
            "writer_draft": {"details": {"accepted_artifacts": [draft]}},
            "review_pipeline": {"details": {"review_artifact": review}},
            "writer_final": {"details": {"accepted_artifacts": [final]}},
            "promotion": {"details": {"target": target}},
        }
    }
    assert write_transaction._required_stage_lineage_pairs(
        tmp_path.resolve(), run_id, "writer_draft", transaction, progress
    ) == {(context["path"], context["sha256"])}
    assert write_transaction._required_stage_lineage_pairs(
        tmp_path.resolve(), run_id, "reviewer", transaction, progress
    ) == {(draft["path"], draft["sha256"])}
    assert write_transaction._required_stage_lineage_pairs(
        tmp_path.resolve(), run_id, "writer_final", transaction, progress
    ) == {
        (draft["path"], draft["sha256"]),
        (review["path"], review["sha256"]),
    }
    assert write_transaction._required_stage_lineage_pairs(
        tmp_path.resolve(), run_id, "data_agent", transaction, progress
    ) == {
        (final["path"], final["sha256"]),
        (target["path"], target["sha256"]),
        (review["path"], review["sha256"]),
    }
    with pytest.raises(WriteTransactionError, match="unrelated artifacts"):
        write_transaction._validate_stage_launch_lineage(
            tmp_path.resolve(),
            run_id,
            "writer_draft",
            transaction,
            progress,
            [context, {"path": str((tmp_path / "unrelated.md").resolve()), "sha256": "9" * 64}],
        )

    transaction["mode"] = "minimal"
    no_review = {"path": str((staging / "no-review.json").resolve()), "sha256": "6" * 64}
    progress["completed"]["review_pipeline"]["details"] = {"no_review": no_review}
    assert write_transaction._required_stage_lineage_pairs(
        tmp_path.resolve(), run_id, "writer_final", transaction, progress
    ) == {
        (draft["path"], draft["sha256"]),
        (no_review["path"], no_review["sha256"]),
    }
    with pytest.raises(WriteTransactionError, match="fresh run-bound no-review"):
        write_transaction._required_stage_lineage_pairs(
            tmp_path.resolve(), run_id, "reviewer", transaction, progress
        )


def test_production_writer_acceptance_binds_launch_and_manifest_lineage(tmp_path, monkeypatch):
    run_id = "production-writer-lineage"
    parent_id = "77777777-7777-4777-8777-777777777777"
    _mock_route_ready(monkeypatch, parent_thread_id=parent_id)
    transaction = begin_write_transaction(
        tmp_path,
        chapter=1,
        mode="default",
        parent_model="gpt-5.6-sol",
        workspace_root=tmp_path,
        run_id=run_id,
    )
    _prepare_production_gates(tmp_path, run_id, monkeypatch)
    sessions = tmp_path / "writer-lineage-sessions"
    context_rollout = _write_rollout(
        sessions,
        role="webnovel_context_agent",
        thread_id="context-lineage-child",
        parent_id=parent_id,
    )
    context_payload = "\n".join(
        f"## {heading}\n内容"
        for heading in ("开篇委托", "这章的故事", "这章的人物", "怎么写更顺", "收在哪里")
    )
    context_request = _write_agent_request(
        tmp_path,
        run_id,
        stage="context_agent",
        rollout=context_rollout,
        sessions_root=sessions,
        envelope=build_canned_envelope(transaction["route"]["steps"][0]),
        payload=context_payload,
        thread_id="context-lineage-child",
        parent_id=parent_id,
    )
    accept_agent_request(tmp_path, run_id, context_request)

    artifact, writer_payload = _writer_artifact(tmp_path, run_id, "draft")
    writer_rollout = _write_rollout(
        sessions,
        role="webnovel_writer",
        thread_id="writer-lineage-child",
        parent_id=parent_id,
    )
    writer_step = next(
        step for step in transaction["route"]["steps"] if step["agent_name"] == "webnovel_writer"
    )
    writer_request = _write_agent_request(
        tmp_path,
        run_id,
        stage="writer_draft",
        rollout=writer_rollout,
        sessions_root=sessions,
        envelope=build_canned_envelope(writer_step, artifacts=[artifact]),
        payload=writer_payload,
        thread_id="writer-lineage-child",
        parent_id=parent_id,
    )
    orphan_evidence = (
        tmp_path
        / ".webnovel"
        / "tmp"
        / "write-runs"
        / run_id
        / "evidence"
        / "writer_draft-manifest.json"
    )
    orphan_evidence.parent.mkdir(parents=True)
    orphan_evidence.write_text('{"orphaned":"failed-attempt"}', encoding="utf-8")
    receipt = accept_agent_request(tmp_path, run_id, writer_request)
    manifest_binding = receipt["details"]["source_bindings"]["writer_manifest"]
    launch_binding = receipt["details"]["source_bindings"]["launch_request"]
    assert manifest_binding["sha256"] == writer_payload["manifest_sha256"]
    manifest_evidence = Path(manifest_binding["path"])
    assert manifest_evidence.parent.name == "evidence"
    assert manifest_evidence.name == "writer_draft-manifest.json"
    assert launch_binding["sha256"] == json.loads(writer_request.read_text(encoding="utf-8"))[
        "launch_request"
    ]["sha256"]
    mutable_manifest = Path(writer_payload["manifest_path"])
    mutable_manifest.write_text('{"replaced":"by-later-writer-stage"}', encoding="utf-8")
    assert write_transaction_status(tmp_path, run_id)["next_stage"] == "reviewer"
    evidence_raw = manifest_evidence.read_bytes()
    manifest_evidence.write_text("{}", encoding="utf-8")
    with pytest.raises(WriteTransactionError, match="immutable writer manifest changed"):
        write_transaction_status(tmp_path, run_id)
    manifest_evidence.write_bytes(evidence_raw)


def test_production_self_hashed_agent_receipt_is_semantically_replayed(tmp_path, monkeypatch):
    run_id = "forged-self-hash"
    _mock_route_ready(monkeypatch)
    transaction = begin_write_transaction(
        tmp_path,
        chapter=1,
        mode="default",
        parent_model="gpt-5.6-sol",
        workspace_root=tmp_path,
        run_id=run_id,
    )
    _prepare_production_gates(tmp_path, run_id, monkeypatch)
    receipts = write_transaction._validated_receipts(
        write_transaction._run_dir(tmp_path, run_id),
        transaction=transaction,
    )
    forged = {
        "schema_version": write_transaction.RECEIPT_SCHEMA_VERSION,
        "run_id": run_id,
        "transaction_sha256": transaction["transaction_sha256"],
        "sequence": 3,
        "stage": "context_agent",
        "status": "completed",
        "created_at": "2026-08-08T00:00:00+00:00",
        "previous_receipt_sha256": receipts[-1]["receipt_sha256"],
        "details": {"agent_name": "webnovel_context_agent"},
        "test_only": False,
    }
    forged["receipt_sha256"] = write_transaction._receipt_hash(forged)
    receipt_path = (
        tmp_path
        / ".webnovel"
        / "write-runs"
        / run_id
        / "receipts"
        / "003-context_agent.json"
    )
    receipt_path.write_text(json.dumps(forged, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(WriteTransactionError, match="invalid exact schema"):
        write_transaction_status(tmp_path, run_id)
    assert not (tmp_path / "正文").exists()


def test_data_replay_uses_only_strict_run_bound_copies(tmp_path):
    run_id = "data-replay"
    staging = tmp_path / ".webnovel" / "tmp" / "write-runs" / run_id
    bound_root = staging / "commit-inputs"
    bound_root.mkdir(parents=True)
    documents = {
        "fulfillment_result": {
            "planned_nodes": [],
            "covered_nodes": [],
            "missed_nodes": [],
            "extra_nodes": [],
        },
        "disambiguation_result": {"pending": []},
        "extraction_result": {
            "accepted_events": [],
            "state_deltas": [],
            "entity_deltas": [],
        },
    }
    artifacts = []
    bound = []
    for name, document in documents.items():
        filename = f"{name}.json"
        path = bound_root / filename
        path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        signature = write_transaction._file_signature(path, trusted_root=bound_root)
        bound.append(signature)
        artifacts.append(
            {
                "name": name,
                "path": str((tmp_path / ".webnovel" / "tmp" / filename).resolve()),
                "sha256": signature["sha256"],
                "bytes": signature["bytes"],
            }
        )
    payload = {
        "schema_version": "webnovel-data-result/v1",
        "status": "completed",
        "run_id": run_id,
        "artifacts": artifacts,
        "pending_count": 0,
        "missed_nodes_count": 0,
        "problems": [],
        "warnings": [],
    }

    accepted, replayed = write_transaction._bound_data_payload(
        tmp_path.resolve(),
        run_id,
        payload,
        {"bound_artifacts": bound},
    )
    assert accepted == artifacts
    assert replayed == bound

    global_path = tmp_path / ".webnovel" / "tmp" / "extraction_result.json"
    global_path.write_text('{"attacker":"replacement"}', encoding="utf-8")
    assert write_transaction._bound_data_payload(
        tmp_path.resolve(), run_id, payload, {"bound_artifacts": bound}
    )[0] == artifacts
    (bound_root / "extraction_result.json").write_text("{}", encoding="utf-8")
    with pytest.raises(WriteTransactionError, match="run-bound artifact changed"):
        write_transaction._bound_data_payload(
            tmp_path.resolve(), run_id, payload, {"bound_artifacts": bound}
        )


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("payload_schema", "payload schema is invalid"),
        ("payload_semantics", "payload semantics are invalid"),
        ("artifact_declaration", "artifact declaration is invalid"),
        ("global_path", "artifact path changed"),
        ("missing_bound_set", "run-bound artifact set is missing"),
        ("invalid_signature", "run-bound signature is invalid"),
        ("bound_path", "run-bound path is invalid"),
        ("changed_copy", "run-bound artifact changed"),
        ("declaration_mismatch", "payload no longer binds"),
        ("document_schema", "run-bound artifact schema is invalid"),
    ],
)
def test_data_replay_rejects_each_broken_run_bound_contract(
    tmp_path: Path,
    case: str,
    message: str,
) -> None:
    run_id = f"data-negative-{case}"
    bound_root = (
        tmp_path
        / ".webnovel"
        / "tmp"
        / "write-runs"
        / run_id
        / "commit-inputs"
    )
    bound_root.mkdir(parents=True)
    documents = {
        "fulfillment_result": {
            "planned_nodes": [],
            "covered_nodes": [],
            "missed_nodes": [],
            "extra_nodes": [],
        },
        "disambiguation_result": {"pending": []},
        "extraction_result": {
            "accepted_events": [],
            "state_deltas": [],
            "entity_deltas": [],
        },
    }
    artifacts = []
    bound = []
    for name, document in documents.items():
        filename = f"{name}.json"
        path = bound_root / filename
        path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        signature = write_transaction._file_signature(path, trusted_root=bound_root)
        bound.append(signature)
        artifacts.append(
            {
                "name": name,
                "path": str((tmp_path / ".webnovel" / "tmp" / filename).resolve()),
                "sha256": signature["sha256"],
                "bytes": signature["bytes"],
            }
        )
    payload = {
        "schema_version": "webnovel-data-result/v1",
        "status": "completed",
        "run_id": run_id,
        "artifacts": artifacts,
        "pending_count": 0,
        "missed_nodes_count": 0,
        "problems": [],
        "warnings": [],
    }
    details = {"bound_artifacts": bound}

    if case == "payload_schema":
        payload["extra"] = True
    elif case == "payload_semantics":
        payload["status"] = "failed"
    elif case == "artifact_declaration":
        artifacts[0]["bytes"] = True
    elif case == "global_path":
        artifacts[0]["path"] = str((tmp_path / "wrong.json").resolve())
    elif case == "missing_bound_set":
        details["bound_artifacts"] = []
    elif case == "invalid_signature":
        bound[0] = "not-a-signature"
    elif case == "bound_path":
        bound[0]["path"] = str((tmp_path / "outside.json").resolve())
    elif case == "changed_copy":
        Path(bound[0]["path"]).write_text("{}", encoding="utf-8")
    elif case == "declaration_mismatch":
        artifacts[0]["sha256"] = "0" * 64
    elif case == "document_schema":
        payload["pending_count"] = 1
    else:
        raise AssertionError(case)

    with pytest.raises(WriteTransactionError, match=message):
        write_transaction._bound_data_payload(
            tmp_path.resolve(),
            run_id,
            payload,
            details,
        )


def test_control_path_and_json_primitives_fail_closed(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path.resolve()
    outside = root.parent / f"{root.name}-outside"
    directory = root / "directory"
    directory.mkdir()
    regular = root / "regular.txt"
    regular.write_text("content", encoding="utf-8")

    assert write_transaction._is_reparse_point(root / "missing-link") is True
    with pytest.raises(WriteTransactionError, match="not a directory"):
        write_transaction._safe_project_root(root / "missing-project")
    with monkeypatch.context() as isolated:
        original = write_transaction._is_reparse_point
        isolated.setattr(
            write_transaction,
            "_is_reparse_point",
            lambda path: path == root or original(path),
        )
        with pytest.raises(WriteTransactionError, match="reparse-point project root"):
            write_transaction._safe_project_root(root)

    with pytest.raises(WriteTransactionError, match="escapes trusted root"):
        write_transaction._require_safe_path(root, outside, must_exist=False)
    with pytest.raises(WriteTransactionError, match="required path is missing"):
        write_transaction._require_safe_path(root, root / "missing", must_exist=True)
    with pytest.raises(WriteTransactionError, match="required file is missing"):
        write_transaction._require_safe_path(
            root,
            directory,
            must_exist=True,
            regular_file=True,
        )
    with pytest.raises(WriteTransactionError, match="not a regular file"):
        write_transaction._require_safe_path(
            root,
            directory,
            must_exist=False,
            regular_file=True,
        )
    with pytest.raises(WriteTransactionError, match="directory escapes project"):
        write_transaction._safe_mkdir_chain(root, outside / "nested")
    with pytest.raises(WriteTransactionError, match="unsafe transaction directory"):
        write_transaction._safe_mkdir_chain(root, regular / "nested")
    assert write_transaction._safe_relative_path(root, outside, root) is False

    with pytest.raises(WriteTransactionError, match="exceeds size limit"):
        write_transaction._stable_read_snapshot(
            regular,
            trusted_root=root,
            max_bytes=1,
        )
    with monkeypatch.context() as isolated:
        isolated.setattr(write_transaction.os, "open", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("denied")))
        with pytest.raises(WriteTransactionError, match="file is unreadable"):
            write_transaction._stable_read_snapshot(
                regular,
                trusted_root=root,
                max_bytes=100,
            )

    for raw in (
        b'{"duplicate":1,"duplicate":2}',
        b'{"value":NaN}',
        b"\xef\xbb\xbf{}",
        b"\xff",
    ):
        with pytest.raises(WriteTransactionError, match="invalid transaction JSON"):
            write_transaction._json_object_from_bytes(raw, regular)
    with pytest.raises(WriteTransactionError, match="must be an object"):
        write_transaction._json_object_from_bytes(b"[]", regular)

    bom = root / "bom.txt"
    bom.write_bytes(b"\xef\xbb\xbftext")
    with pytest.raises(WriteTransactionError, match="without BOM"):
        write_transaction._read_bounded_utf8(bom, trusted_root=root)
    invalid_utf8 = root / "invalid-utf8.txt"
    invalid_utf8.write_bytes(b"\xff")
    with pytest.raises(WriteTransactionError, match="not UTF-8"):
        write_transaction._read_bounded_utf8(invalid_utf8, trusted_root=root)


def test_immutable_control_writes_cover_idempotence_and_failure_recovery(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path.resolve()
    json_path = root / "controls" / "receipt.json"
    evidence_path = root / "controls" / "evidence.bin"

    with monkeypatch.context() as isolated:
        isolated.setattr(write_transaction, "FileLock", None)
        with pytest.raises(WriteTransactionError, match="filelock is required"):
            write_transaction._write_json_once(root, json_path, {"value": 1})
        with pytest.raises(WriteTransactionError, match="filelock is required"):
            write_transaction._write_bytes_once(root, evidence_path, b"one")

    write_transaction._write_json_once(root, json_path, {"value": 1})
    write_transaction._write_json_once(root, json_path, {"value": 1})
    with pytest.raises(WriteTransactionError, match="immutable transaction file differs"):
        write_transaction._write_json_once(root, json_path, {"value": 2})

    write_transaction._write_bytes_once(root, evidence_path, b"one")
    write_transaction._write_bytes_once(root, evidence_path, b"one")
    with pytest.raises(WriteTransactionError, match="immutable transaction evidence differs"):
        write_transaction._write_bytes_once(root, evidence_path, b"two")

    run_id = "accepted-evidence"
    begin_write_transaction(
        root,
        chapter=1,
        mode="minimal",
        parent_model="gpt-5.6-sol",
        run_id=run_id,
        test_only=True,
    )
    _advance_test_stage(root, run_id, "preflight", details={"gate_ok": True})
    accepted_path = root / ".webnovel" / "tmp" / "write-runs" / run_id / "accepted.bin"
    write_transaction._write_bytes_once(root, accepted_path, b"before")
    with pytest.raises(WriteTransactionError, match="accepted transaction evidence is immutable"):
        write_transaction._write_bytes_once(
            root,
            accepted_path,
            b"after",
            replace_before_stage=(run_id, "preflight"),
        )

    class BusyLock:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            raise write_transaction.Timeout("busy")

        def __exit__(self, *args):
            return False

    with monkeypatch.context() as isolated:
        isolated.setattr(write_transaction, "FileLock", BusyLock)
        with pytest.raises(WriteTransactionError, match="control lock is busy"):
            write_transaction._write_json_once(root, root / "busy.json", {"value": 1})
        with pytest.raises(WriteTransactionError, match="evidence lock is busy"):
            write_transaction._write_bytes_once(root, root / "busy.bin", b"value")


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("schema", "unsupported receipt schema"),
        ("fields", "receipt fields are invalid"),
        ("sequence", "receipt sequence gap"),
        ("path", "fixed stage path"),
        ("previous", "receipt chain mismatch"),
        ("binding", "receipt transaction binding is invalid"),
    ],
)
def test_receipt_loader_rejects_each_forged_chain_dimension(
    tmp_path: Path,
    case: str,
    message: str,
) -> None:
    run_id = f"receipt-{case}"
    transaction = begin_write_transaction(
        tmp_path,
        chapter=1,
        mode="minimal",
        parent_model="gpt-5.6-sol",
        run_id=run_id,
        test_only=True,
    )
    _advance_test_stage(tmp_path, run_id, "preflight", details={"gate_ok": True})
    receipt_path = (
        tmp_path
        / ".webnovel"
        / "write-runs"
        / run_id
        / "receipts"
        / "001-preflight.json"
    )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if case == "schema":
        receipt["schema_version"] = "forged/v1"
    elif case == "fields":
        receipt["extra"] = True
    elif case == "sequence":
        receipt["sequence"] = 2
    elif case == "path":
        forged_path = receipt_path.with_name("001-alias.json")
        receipt_path.rename(forged_path)
        receipt_path = forged_path
    elif case == "previous":
        receipt["previous_receipt_sha256"] = "f" * 64
    elif case == "binding":
        receipt["status"] = "forged"
    else:
        raise AssertionError(case)
    if case != "path":
        check = dict(receipt)
        check.pop("receipt_sha256", None)
        receipt["receipt_sha256"] = write_transaction._receipt_hash(check)
        receipt_path.write_text(json.dumps(receipt, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(WriteTransactionError, match=message):
        write_transaction._validated_receipts(
            write_transaction._run_dir(tmp_path, run_id),
            transaction=transaction,
        )


def test_progress_and_stage_recording_reject_impossible_transitions(
    tmp_path: Path,
    monkeypatch,
) -> None:
    transaction = {"stages": ["first"]}
    completed = {"stage": "first", "status": "completed", "receipt_sha256": "a"}
    with pytest.raises(WriteTransactionError, match="after complete"):
        write_transaction._derive_progress(transaction, [completed, completed])
    with pytest.raises(WriteTransactionError, match="stage out of order"):
        write_transaction._derive_progress(
            transaction,
            [{"stage": "other", "status": "completed", "receipt_sha256": "a"}],
        )
    with pytest.raises(WriteTransactionError, match="invalid stage status"):
        write_transaction._derive_progress(
            transaction,
            [{"stage": "first", "status": "pending", "receipt_sha256": "a"}],
        )

    minimal = {"mode": "minimal"}
    with pytest.raises(WriteTransactionError, match="fresh run-bound no-review"):
        write_transaction._validate_stage_details(
            minimal,
            {},
            "reviewer",
            "completed",
            {},
        )
    with pytest.raises(WriteTransactionError, match="accepted commit"):
        write_transaction._validate_stage_details(
            {"mode": "default"},
            {},
            "commit",
            "completed",
            {"commit": {"exists": True}, "commit_status": "draft"},
        )

    run_id = "stage-record-errors"
    begin_write_transaction(
        tmp_path,
        chapter=1,
        mode="minimal",
        parent_model="gpt-5.6-sol",
        run_id=run_id,
        test_only=True,
    )
    first = _advance_test_stage(tmp_path, run_id, "preflight", details={"gate_ok": True})
    assert _advance_test_stage(
        tmp_path,
        run_id,
        "preflight",
        details={"gate_ok": True},
    ) == first
    with monkeypatch.context() as isolated:
        isolated.setattr(write_transaction, "FileLock", None)
        with pytest.raises(WriteTransactionError, match="filelock is required"):
            record_write_stage(
                tmp_path,
                run_id,
                stage="writer_draft",
                status="completed",
                test_only_agent_override=True,
            )
    with pytest.raises(WriteTransactionError, match="invalid stage status"):
        record_write_stage(
            tmp_path,
            run_id,
            stage="writer_draft",
            status="pending",
            test_only_agent_override=True,
        )
    with pytest.raises(WriteTransactionError, match="runtime and payload acceptance"):
        record_write_stage(
            tmp_path,
            run_id,
            stage="writer_draft",
            status="completed",
        )


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("fixed_name", "fixed stage filename"),
        ("shape", "invalid shape"),
        ("binding", "does not bind this transaction stage"),
        ("empty_inputs", "needs 1-32 explicit input artifacts"),
        ("duplicate_inputs", "contains duplicate inputs"),
    ],
)
def test_agent_launch_loader_rejects_forged_request_dimensions(
    tmp_path: Path,
    case: str,
    message: str,
) -> None:
    run_id = f"launch-{case}"
    transaction = begin_write_transaction(
        tmp_path,
        chapter=1,
        mode="minimal",
        parent_model="gpt-5.6-sol",
        run_id=run_id,
        test_only=True,
    )
    requests = tmp_path / ".webnovel" / "tmp" / "write-runs" / run_id / "requests"
    requests.mkdir(parents=True, exist_ok=True)
    transaction_path = tmp_path / ".webnovel" / "write-runs" / run_id / "transaction.json"
    input_spec = {"path": str(transaction_path.resolve()), "sha256": _sha(transaction_path)}
    launch = {
        "schema_version": AGENT_LAUNCH_REQUEST_SCHEMA,
        "run_id": run_id,
        "stage": "context_agent",
        "transaction_sha256": transaction["transaction_sha256"],
        "input_artifacts": [input_spec],
    }
    launch_path = requests / "context_agent-launch.json"
    if case == "fixed_name":
        launch_path = requests / "alias.json"
    elif case == "shape":
        launch["extra"] = True
    elif case == "binding":
        launch["transaction_sha256"] = "0" * 64
    elif case == "empty_inputs":
        launch["input_artifacts"] = []
    elif case == "duplicate_inputs":
        launch["input_artifacts"] = [input_spec, input_spec]
    else:
        raise AssertionError(case)
    launch_path.write_text(json.dumps(launch, ensure_ascii=False), encoding="utf-8")
    spec = {"path": str(launch_path.resolve()), "sha256": _sha(launch_path)}

    with pytest.raises(WriteTransactionError, match=message):
        write_transaction._load_agent_launch_request(
            tmp_path.resolve(),
            run_id,
            "context_agent",
            transaction,
            spec,
        )


def test_run_request_and_artifact_loaders_reject_untrusted_shapes(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    run_id = "request-loader"
    begin_write_transaction(
        root,
        chapter=1,
        mode="minimal",
        parent_model="gpt-5.6-sol",
        run_id=run_id,
        test_only=True,
    )
    requests = root / ".webnovel" / "tmp" / "write-runs" / run_id / "requests"
    requests.mkdir(parents=True, exist_ok=True)
    outside = root / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    with pytest.raises(WriteTransactionError, match="inside this run requests directory"):
        write_transaction._load_run_request(root, run_id, outside.resolve())
    invalid = requests / "invalid.json"
    invalid.write_text("{", encoding="utf-8")
    with pytest.raises(WriteTransactionError, match="invalid request JSON"):
        write_transaction._load_run_request(root, run_id, invalid.resolve())
    wrong_run = requests / "wrong-run.json"
    wrong_run.write_text(json.dumps({"run_id": "other"}), encoding="utf-8")
    with pytest.raises(WriteTransactionError, match="bound to this run"):
        write_transaction._load_run_request(root, run_id, wrong_run.resolve())

    with pytest.raises(WriteTransactionError, match="exactly path and sha256"):
        write_transaction._request_artifact(
            root,
            run_id,
            {},
            allowed_root=root,
        )
    with pytest.raises(WriteTransactionError, match="path/hash is invalid"):
        write_transaction._request_artifact(
            root,
            run_id,
            {"path": "relative.json", "sha256": "bad"},
            allowed_root=root,
        )
    with pytest.raises(WriteTransactionError, match="outside its allowed root"):
        write_transaction._request_artifact(
            root,
            run_id,
            {"path": str((root / "missing.json").resolve()), "sha256": "f" * 64},
            allowed_root=root,
        )
    with pytest.raises(WriteTransactionError, match="hash mismatch"):
        write_transaction._request_artifact(
            root,
            run_id,
            {"path": str(outside.resolve()), "sha256": "f" * 64},
            allowed_root=root,
        )

    transaction = write_transaction._load_transaction(root, run_id)
    with pytest.raises(WriteTransactionError, match="Agent stage"):
        build_agent_prompt_marker(
            root,
            run_id,
            stage="preflight",
            launch_request={},
        )
    with pytest.raises(WriteTransactionError, match="production-only"):
        prepare_agent_launch_request(root, run_id, outside.resolve())
    assert transaction["run_id"] == run_id


def test_lineage_helpers_reject_missing_unrelated_and_wrong_writer_manifest(
    tmp_path: Path,
) -> None:
    root = tmp_path.resolve()
    run_id = "lineage-errors"
    transaction = begin_write_transaction(
        root,
        chapter=1,
        mode="default",
        parent_model="gpt-5.6-sol",
        run_id=run_id,
        test_only=True,
    )
    with pytest.raises(WriteTransactionError, match="required agent"):
        write_transaction._expected_route_step(transaction, "missing_agent")
    with pytest.raises(WriteTransactionError, match="lineage details"):
        write_transaction._receipt_details({}, "writer_draft")
    with pytest.raises(WriteTransactionError, match="artifact is missing"):
        write_transaction._lineage_pair(None, label="draft")
    with pytest.raises(WriteTransactionError, match="identity is invalid"):
        write_transaction._lineage_pair(
            {"path": "relative.md", "sha256": "bad"},
            label="draft",
        )

    for stage, completed, message in (
        ("reviewer", {"writer_draft": {"details": {"accepted_artifacts": []}}}, "writer_draft"),
        ("writer_final", {"writer_draft": {"details": {"accepted_artifacts": []}}}, "writer_draft"),
        ("data_agent", {"writer_final": {"details": {"accepted_artifacts": []}}}, "writer_final"),
    ):
        with pytest.raises(WriteTransactionError, match=message):
            write_transaction._required_stage_lineage_pairs(
                root,
                run_id,
                stage,
                transaction,
                {"completed": completed},
            )
    with pytest.raises(WriteTransactionError, match="unsupported Agent lineage stage"):
        write_transaction._required_stage_lineage_pairs(
            root,
            run_id,
            "unsupported",
            transaction,
            {"completed": {}},
        )
    with pytest.raises(WriteTransactionError, match="payload is not an object"):
        write_transaction._verified_writer_manifest_binding(
            root,
            run_id,
            "writer_draft",
            "not-an-object",
            {},
        )
    with pytest.raises(WriteTransactionError, match="current-run manifest"):
        write_transaction._verified_writer_manifest_binding(
            root,
            run_id,
            "writer_draft",
            {"manifest_path": str(root / "elsewhere.json")},
            {},
        )
    manifest = root / ".webnovel" / "tmp" / "write-runs" / run_id / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    with pytest.raises(WriteTransactionError, match="changed before lineage binding"):
        write_transaction._verified_writer_manifest_binding(
            root,
            run_id,
            "writer_draft",
            {"manifest_path": str(manifest.resolve()), "manifest_sha256": "f" * 64},
            {},
        )


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("filename", "filename must identify"),
        ("jsonl", "not UTF-8 JSONL"),
        ("session_count", "lacks session_meta"),
        ("session_payload", "session_meta payloads must be objects"),
        ("conflicting_session", "conflicting session_meta payloads"),
        ("not_child", "not a Codex child Agent"),
        ("identity", "session_meta thread id mismatch"),
        ("no_turn", "lacks turn_context"),
        ("turn_payload", "turn_context payloads must be objects"),
        ("conflicting_turn", "conflicting turn_context payload"),
        ("duplicate_marker", "duplicate Agent prompt markers"),
        ("no_output", "lacks the bound prompt marker or final assistant output"),
    ],
)
def test_bound_rollout_parser_rejects_each_identity_and_transcript_forgery(
    tmp_path: Path,
    case: str,
    message: str,
) -> None:
    thread_id = "child-thread"
    parent_id = "parent-thread"
    role = "webnovel_context_agent"
    marker = "WEBNOVEL_AGENT_PROMPT:bound"
    task_name = write_transaction.derive_agent_task_name(
        marker,
        prefix=write_transaction.AGENT_TASK_NAME_PREFIX,
    )
    session = {
        "id": thread_id,
        "parent_thread_id": parent_id,
        "model": "gpt-5.6-luna",
        "source": {
            "subagent": {
                "thread_spawn": {
                    "parent_thread_id": parent_id,
                    "depth": 1,
                    "agent_path": f"/root/{task_name}",
                    "agent_role": role,
                    "prompt": marker,
                }
            }
        },
    }
    turn = {
        "type": "turn_context",
        "payload": {"turn_id": "turn-1", "model": "gpt-5.6-luna", "effort": "high"},
    }
    assistant = {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "final"}],
        },
    }
    events = [{"type": "session_meta", "payload": session}, turn, assistant]
    rollout_path = tmp_path / f"rollout-{thread_id}.jsonl"
    if case == "filename":
        rollout_path = tmp_path / "rollout-other.jsonl"
    elif case == "session_count":
        events.pop(0)
    elif case == "session_payload":
        events[0]["payload"] = []
    elif case == "conflicting_session":
        events.insert(
            1,
            {
                "type": "session_meta",
                "payload": {**session, "model": "gpt-5.6-terra"},
            },
        )
    elif case == "not_child":
        session["source"] = {}
    elif case == "identity":
        session["id"] = "other-child"
    elif case == "no_turn":
        events.remove(turn)
    elif case == "turn_payload":
        turn["payload"] = "invalid"
    elif case == "conflicting_turn":
        events.insert(
            2,
            {
                "type": "turn_context",
                "payload": {**turn["payload"], "effort": "high"},
            },
        )
    elif case == "duplicate_marker":
        events.insert(
            2,
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": marker}],
                },
            },
        )
    elif case == "no_output":
        events.remove(assistant)
    elif case != "jsonl":
        raise AssertionError(case)
    raw = (
        b"\xff"
        if case == "jsonl"
        else ("\n".join(json.dumps(event) for event in events) + "\n").encode("utf-8")
    )

    with pytest.raises(WriteTransactionError, match=message):
        write_transaction._parse_bound_agent_rollout(
            raw,
            rollout_path=rollout_path,
            thread_id=thread_id,
            parent_thread_id=parent_id,
            expected_agent=role,
            expected_model="gpt-5.6-luna",
            expected_effort="high",
            expected_marker=marker,
            expected_task_name=task_name,
        )


def test_bound_rollout_parser_coalesces_exact_session_and_turn_duplicates(
    tmp_path: Path,
) -> None:
    thread_id = "child-duplicate"
    parent_id = "parent-duplicate"
    role = "webnovel_context_agent"
    marker = "WEBNOVEL_AGENT_PROMPT:duplicate"
    task_name = write_transaction.derive_agent_task_name(
        marker,
        prefix=write_transaction.AGENT_TASK_NAME_PREFIX,
    )
    session = {
        "type": "session_meta",
        "payload": {
            "id": thread_id,
            "parent_thread_id": parent_id,
            "model": "gpt-5.6-luna",
            "source": {
                "subagent": {
                    "thread_spawn": {
                        "parent_thread_id": parent_id,
                        "depth": 1,
                        "agent_path": f"/root/{task_name}",
                        "agent_role": role,
                        "prompt": marker,
                    }
                }
            },
        },
    }
    turn = {
        "type": "turn_context",
        "payload": {"turn_id": "turn-1", "model": "gpt-5.6-luna", "effort": "high"},
    }
    assistant = {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "final"}],
        },
    }
    events = [session, session, turn, turn, assistant]
    raw = ("\n".join(json.dumps(event) for event in events) + "\n").encode("utf-8")

    evidence, output = write_transaction._parse_bound_agent_rollout(
        raw,
        rollout_path=tmp_path / f"rollout-{thread_id}.jsonl",
        thread_id=thread_id,
        parent_thread_id=parent_id,
        expected_agent=role,
        expected_model="gpt-5.6-luna",
        expected_effort="high",
        expected_marker=marker,
        expected_task_name=task_name,
    )

    assert evidence.thread_id == thread_id
    assert evidence.parent_thread_id == parent_id
    assert output == "final"


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("path", "agent_path"),
        ("depth", "depth must equal 1"),
        ("run", "agent_path"),
        ("stage", "agent_path"),
        ("launch", "agent_path"),
    ],
)
def test_current_desktop_rollout_rejects_task_path_and_marker_scope_forgery(
    tmp_path: Path,
    case: str,
    message: str,
) -> None:
    thread_id = "desktop-child"
    parent_id = "desktop-parent"
    marker_payload = {
        "schema_version": write_transaction.AGENT_PROMPT_MARKER_SCHEMA,
        "run_id": "bound-run",
        "stage": "context_agent",
        "transaction_sha256": "a" * 64,
        "launch_request_sha256": "b" * 64,
        "input_artifacts": [{"path": "C:/bound/input.json", "sha256": "c" * 64}],
    }
    marker = write_transaction.AGENT_PROMPT_MARKER_PREFIX + write_transaction._canonical_bytes(
        marker_payload
    ).decode("utf-8")
    task_name = write_transaction.derive_agent_task_name(
        marker,
        prefix=write_transaction.AGENT_TASK_NAME_PREFIX,
    )
    forged_payload = dict(marker_payload)
    if case == "run":
        forged_payload["run_id"] = "other-run"
    elif case == "stage":
        forged_payload["stage"] = "writer_draft"
    elif case == "launch":
        forged_payload["launch_request_sha256"] = "d" * 64
    forged_marker = write_transaction.AGENT_PROMPT_MARKER_PREFIX + write_transaction._canonical_bytes(
        forged_payload
    ).decode("utf-8")
    forged_task_name = write_transaction.derive_agent_task_name(
        forged_marker,
        prefix=write_transaction.AGENT_TASK_NAME_PREFIX,
    )
    spawn = {
        "parent_thread_id": parent_id,
        "depth": 2 if case == "depth" else 1,
        "agent_path": (
            "/root/unrelated"
            if case == "path"
            else f"/root/{forged_task_name if case in {'run', 'stage', 'launch'} else task_name}"
        ),
        "agent_role": "webnovel_context_agent",
    }
    events = [
        {
            "type": "session_meta",
            "payload": {
                "id": thread_id,
                "parent_thread_id": parent_id,
                "model": "gpt-5.6-luna",
                "source": {"subagent": {"thread_spawn": spawn}},
            },
        },
        {
            "type": "turn_context",
            "payload": {"turn_id": "turn-1", "model": "gpt-5.6-luna", "effort": "high"},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "phase": "final_answer",
                "content": [{"type": "output_text", "text": "final"}],
            },
        },
    ]
    raw = ("\n".join(json.dumps(event) for event in events) + "\n").encode("utf-8")

    with pytest.raises(WriteTransactionError, match=message):
        write_transaction._parse_bound_agent_rollout(
            raw,
            rollout_path=tmp_path / f"rollout-{thread_id}.jsonl",
            thread_id=thread_id,
            parent_thread_id=parent_id,
            expected_agent="webnovel_context_agent",
            expected_model="gpt-5.6-luna",
            expected_effort="high",
            expected_marker=marker,
            expected_task_name=task_name,
        )


@pytest.mark.parametrize(
    ("phases", "message"),
    [(["commentary"], "lacks"), (["final", "final_answer"], "multiple")],
)
def test_current_desktop_rollout_rejects_nonfinal_or_duplicate_output(
    tmp_path: Path,
    phases: list[str],
    message: str,
) -> None:
    marker = "WEBNOVEL_AGENT_PROMPT:desktop-output"
    task_name = write_transaction.derive_agent_task_name(
        marker,
        prefix=write_transaction.AGENT_TASK_NAME_PREFIX,
    )
    events = [
        {
            "type": "session_meta",
            "payload": {
                "id": "desktop-output-child",
                "parent_thread_id": "desktop-output-parent",
                "model": "gpt-5.6-luna",
                "source": {
                    "subagent": {
                        "thread_spawn": {
                            "parent_thread_id": "desktop-output-parent",
                            "depth": 1,
                            "agent_path": f"/root/{task_name}",
                            "agent_role": "webnovel_context_agent",
                        }
                    }
                },
            },
        },
        {
            "type": "turn_context",
            "payload": {"turn_id": "turn-1", "model": "gpt-5.6-luna", "effort": "high"},
        },
        *[
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "phase": phase,
                    "content": [{"type": "output_text", "text": phase}],
                },
            }
            for phase in phases
        ],
    ]
    raw = ("\n".join(json.dumps(event) for event in events) + "\n").encode("utf-8")

    with pytest.raises(WriteTransactionError, match=message):
        write_transaction._parse_bound_agent_rollout(
            raw,
            rollout_path=tmp_path / "rollout-desktop-output-child.jsonl",
            thread_id="desktop-output-child",
            parent_thread_id="desktop-output-parent",
            expected_agent="webnovel_context_agent",
            expected_model="gpt-5.6-luna",
            expected_effort="high",
            expected_marker=marker,
            expected_task_name=task_name,
        )


def test_bound_rollout_parser_ignores_nonmapping_response_payload(tmp_path: Path) -> None:
    thread_id = "child-ignore"
    parent_id = "parent-ignore"
    role = "webnovel_context_agent"
    marker = "WEBNOVEL_AGENT_PROMPT:ignore"
    task_name = write_transaction.derive_agent_task_name(
        marker,
        prefix=write_transaction.AGENT_TASK_NAME_PREFIX,
    )
    events = [
        {
            "type": "session_meta",
            "payload": {
                "id": thread_id,
                "parent_thread_id": parent_id,
                "model": "gpt-5.6-luna",
                "source": {
                    "subagent": {
                        "thread_spawn": {
                            "parent_thread_id": parent_id,
                            "depth": 1,
                            "agent_path": f"/root/{task_name}",
                            "agent_role": role,
                            "prompt": marker,
                        }
                    }
                },
            },
        },
        {
            "type": "turn_context",
            "payload": {"turn_id": "turn-1", "model": "gpt-5.6-luna", "effort": "high"},
        },
        {"type": "response_item", "payload": "ignored"},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "phase": "commentary",
                "content": [{"type": "output_text", "text": "ignored commentary"}],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "final"}],
            },
        },
    ]
    raw = ("\n".join(json.dumps(event) for event in events) + "\n").encode("utf-8")
    evidence, output = write_transaction._parse_bound_agent_rollout(
        raw,
        rollout_path=tmp_path / f"rollout-{thread_id}.jsonl",
        thread_id=thread_id,
        parent_thread_id=parent_id,
        expected_agent=role,
        expected_model="gpt-5.6-luna",
        expected_effort="high",
        expected_marker=marker,
        expected_task_name=None,
    )
    assert evidence.thread_id == thread_id
    assert output == "final"


def test_bound_rollout_parser_rejects_legacy_commentary_without_final(tmp_path: Path) -> None:
    thread_id = "child-commentary-only"
    parent_id = "parent-commentary-only"
    role = "webnovel_context_agent"
    marker = "WEBNOVEL_AGENT_PROMPT:commentary-only"
    events = [
        {
            "type": "session_meta",
            "payload": {
                "id": thread_id,
                "parent_thread_id": parent_id,
                "model": "gpt-5.6-luna",
                "source": {
                    "subagent": {
                        "thread_spawn": {
                            "parent_thread_id": parent_id,
                            "agent_role": role,
                        }
                    }
                },
            },
        },
        {
            "type": "turn_context",
            "payload": {
                "turn_id": "turn-1",
                "model": "gpt-5.6-luna",
                "effort": "high",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": marker}],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "phase": "commentary",
                "content": [{"type": "output_text", "text": "COMMENTARY_ONLY"}],
            },
        },
    ]
    raw = ("\n".join(json.dumps(event) for event in events) + "\n").encode("utf-8")

    with pytest.raises(WriteTransactionError, match="lacks the bound prompt marker or final"):
        write_transaction._parse_bound_agent_rollout(
            raw,
            rollout_path=tmp_path / f"rollout-{thread_id}.jsonl",
            thread_id=thread_id,
            parent_thread_id=parent_id,
            expected_agent=role,
            expected_model="gpt-5.6-luna",
            expected_effort="high",
            expected_marker=marker,
            expected_task_name=None,
        )


def test_current_parent_host_binding_rejects_invalid_host_and_prefix_evidence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(write_transaction, "TRUSTED_CODEX_SESSIONS_ROOT", sessions)
    for supplied in (None, "not-a-uuid", "00000000-0000-0000-0000-000000000000"):
        if supplied is None:
            monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
        else:
            monkeypatch.setenv("CODEX_THREAD_ID", supplied)
        with pytest.raises(WriteTransactionError, match="canonical non-zero UUID"):
            write_transaction._current_parent_host_evidence()

    thread_id = "44444444-4444-4444-8444-444444444444"
    monkeypatch.setenv("CODEX_THREAD_ID", thread_id)
    rollout = sessions / f"rollout-{thread_id}.jsonl"
    rollout.write_bytes(b"not-jsonl\n")
    with pytest.raises(WriteTransactionError, match="not UTF-8 JSONL"):
        write_transaction._current_parent_host_evidence()
    rollout.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": "other"}}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(WriteTransactionError, match="identity does not match"):
        write_transaction._current_parent_host_evidence()
    session = {"type": "session_meta", "payload": {"id": thread_id, "model": "gpt-5.6-sol"}}
    conflicting = {
        "type": "session_meta",
        "payload": {"id": thread_id, "model": "gpt-5.6-terra"},
    }
    rollout.write_text(
        "\n".join(json.dumps(event) for event in (session, conflicting)) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(WriteTransactionError, match="conflicting session_meta payloads"):
        write_transaction._current_parent_host_evidence()

    monkeypatch.setattr(
        write_transaction,
        "_current_parent_host_evidence",
        lambda: {
            "thread_id": thread_id,
            "rollout_path": str(rollout.resolve()),
            "rollout_sha256": hashlib.sha256(b"current").hexdigest(),
            "_raw": b"current",
        },
    )
    binding = {
        "parent_thread_id": thread_id,
        "parent_rollout_path": str(rollout.resolve()),
        "parent_rollout_bytes": 7,
        "parent_rollout_sha256": "0" * 64,
    }
    with pytest.raises(WriteTransactionError, match="bound append point"):
        write_transaction._assert_current_parent_binding(binding)
    binding["parent_rollout_bytes"] = 0
    with pytest.raises(WriteTransactionError, match="evidence changed"):
        write_transaction._assert_current_parent_binding(binding)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"extra": True}, "descriptor fields are invalid"),
        ({"project_root": "C:/different"}, "project_root mismatch"),
        ({"run_id": "different"}, "run_id mismatch"),
        ({"chapter": 0}, "descriptor semantics are invalid"),
        ({"route": {}}, "route no longer matches"),
        ({"parent_task_binding_status": "forged"}, "test-only transaction descriptor is invalid"),
    ],
)
def test_transaction_descriptor_rejects_rehashed_semantic_forgery(
    tmp_path: Path,
    mutation: dict,
    message: str,
) -> None:
    run_id = "descriptor-semantic"
    transaction = begin_write_transaction(
        tmp_path,
        chapter=1,
        mode="minimal",
        parent_model="gpt-5.6-sol",
        run_id=run_id,
        test_only=True,
    )
    transaction.update(mutation)
    check = dict(transaction)
    check.pop("transaction_sha256", None)
    transaction["transaction_sha256"] = write_transaction._receipt_hash(check)
    path = tmp_path / ".webnovel" / "write-runs" / run_id / "transaction.json"
    path.write_text(json.dumps(transaction, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(WriteTransactionError, match=message):
        write_transaction._load_transaction(tmp_path, run_id)


def test_status_locks_resume_backup_and_remaining_cli_routes(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    run_id = "status-locks"
    begin_write_transaction(
        tmp_path,
        chapter=1,
        mode="minimal",
        parent_model="gpt-5.6-sol",
        run_id=run_id,
        test_only=True,
    )
    with monkeypatch.context() as isolated:
        isolated.setattr(write_transaction, "FileLock", None)
        with pytest.raises(WriteTransactionError, match="filelock is required"):
            write_transaction_status(tmp_path, run_id)

    class BusyLock:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            raise write_transaction.Timeout("busy")

        def __exit__(self, *args):
            return False

    with monkeypatch.context() as isolated:
        isolated.setattr(write_transaction, "FileLock", BusyLock)
        with pytest.raises(WriteTransactionError, match="transaction lock is busy"):
            write_transaction_status(tmp_path, run_id)

    with monkeypatch.context() as isolated:
        isolated.setattr(
            write_transaction,
            "write_transaction_status",
            lambda project_root, selected: {
                "next_stage": "backup",
                "commit_done": True,
            },
        )
        resume = build_write_resume_plan(tmp_path, "production-run")
    assert resume["action"] == "retry_backup_only"
    assert resume["must_not_rerun_agents"] is True

    production = {"test_only": False}
    with monkeypatch.context() as isolated:
        isolated.setattr(write_transaction, "_safe_project_root", lambda value: tmp_path.resolve())
        isolated.setattr(write_transaction, "_load_transaction", lambda root, selected: production)
        isolated.setattr(
            write_transaction,
            "prepare_agent_launch_request",
            lambda root, selected, request: {"route": "prepare-agent"},
        )
        code, result = _run_write_cli(
            isolated,
            capsys,
            "--project-root",
            tmp_path,
            "prepare-agent",
            "--run-id",
            "production-run",
            "--request-file",
            "request.json",
        )
        assert code == 0 and result == {"route": "prepare-agent"}

        isolated.setattr(
            write_transaction,
            "record_minimal_no_review",
            lambda root, selected: {"route": "minimal-no-review"},
        )
        code, result = _run_write_cli(
            isolated,
            capsys,
            "--project-root",
            tmp_path,
            "minimal-no-review",
            "--run-id",
            "production-run",
        )
        assert code == 0 and result == {"route": "minimal-no-review"}

        isolated.setattr(
            write_transaction,
            "promote_verified_writer_artifact",
            lambda root, selected, **kwargs: {"route": "promote"},
        )
        code, result = _run_write_cli(
            isolated,
            capsys,
            "--project-root",
            tmp_path,
            "promote",
            "--run-id",
            "production-run",
            "--target",
            "chapter.md",
        )
        assert code == 0 and result == {"route": "promote"}

        def require_choice(*args, **kwargs):
            raise WriteRecoveryChoiceRequired(
                "author_conflict",
                "choose how to recover",
                ("keep_current", "replace_with_verified"),
            )

        isolated.setattr(write_transaction, "promote_verified_writer_artifact", require_choice)
        code, result = _run_write_cli(
            isolated,
            capsys,
            "--project-root",
            tmp_path,
            "promote",
            "--run-id",
            "production-run",
            "--target",
            "chapter.md",
        )
        assert code == 1
        assert result == {
            "status": "choice_required",
            "code": "author_conflict",
            "message": "choose how to recover",
            "choices": ["keep_current", "replace_with_verified"],
        }


def test_remaining_control_and_lock_fail_closed_edges(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path.resolve()
    with pytest.raises(WriteTransactionError, match="escapes trusted root"):
        write_transaction._require_safe_path(
            root,
            root / "missing-allowed" / "artifact.json",
            allowed_root=root / "missing-allowed",
            must_exist=False,
        )

    rootless = root / "rootless" / "artifact.bin"
    write_transaction._atomic_write_bytes(rootless, b"bound", root=None)
    assert rootless.read_bytes() == b"bound"

    with pytest.raises(WriteTransactionError, match="invalid write run directory"):
        write_transaction._receipt_files(Path("short"))

    run_id = "remaining-lock-edges"
    transaction = begin_write_transaction(
        root,
        chapter=1,
        mode="minimal",
        parent_model="gpt-5.6-sol",
        run_id=run_id,
        test_only=True,
    )
    receipts_path = root / ".webnovel" / "write-runs" / run_id / "receipts"
    receipts_path.write_text("not-a-directory", encoding="utf-8")
    with pytest.raises(WriteTransactionError, match="not a directory"):
        write_transaction._receipt_files(write_transaction._run_dir(root, run_id))
    receipts_path.unlink()

    class BusyLock:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            raise write_transaction.Timeout("busy")

        def __exit__(self, *args):
            return False

    with monkeypatch.context() as isolated:
        isolated.setattr(write_transaction, "FileLock", BusyLock)
        with pytest.raises(WriteTransactionError, match="write transaction lock is busy"):
            record_write_stage(
                root,
                run_id,
                stage="preflight",
                status="completed",
                details={"gate_ok": True},
            )

    transaction["test_only"] = False
    transaction["production_evidence_required"] = True
    check = dict(transaction)
    check.pop("transaction_sha256", None)
    transaction["transaction_sha256"] = write_transaction._receipt_hash(check)
    transaction_path = root / ".webnovel" / "write-runs" / run_id / "transaction.json"
    transaction_path.write_text(json.dumps(transaction, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(WriteTransactionError, match="production transaction evidence binding"):
        write_transaction._load_transaction(root, run_id)


def test_current_parent_scan_rejects_reparse_session_directory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sessions = tmp_path / "sessions"
    reparse_child = sessions / "2026"
    reparse_child.mkdir(parents=True)
    thread_id = "44444444-4444-4444-8444-444444444444"
    monkeypatch.setenv("CODEX_THREAD_ID", thread_id)
    monkeypatch.setattr(write_transaction, "TRUSTED_CODEX_SESSIONS_ROOT", sessions)
    original = write_transaction._is_reparse_point
    monkeypatch.setattr(
        write_transaction,
        "_is_reparse_point",
        lambda path: path == reparse_child or original(path),
    )

    with pytest.raises(WriteTransactionError, match="reparse directory"):
        write_transaction._current_parent_host_evidence()


def test_minimal_and_full_review_receipts_replay_bound_semantics(tmp_path):
    minimal_run = "minimal-replay"
    minimal_staging = tmp_path / ".webnovel" / "tmp" / "write-runs" / minimal_run
    minimal_staging.mkdir(parents=True)
    draft_sha = "d" * 64
    no_review = {
        "schema_version": write_transaction.NO_REVIEW_SCHEMA_VERSION,
        "run_id": minimal_run,
        "chapter": 1,
        "review_mode": "minimal",
        "review_skipped": True,
        "source_sha256": draft_sha,
        "issues": [],
        "issues_count": 0,
        "blocking_count": 0,
        "has_blocking": False,
        "summary": "minimal mode: reviewer skipped by explicit mode selection",
    }
    no_review_path = minimal_staging / "no-review.json"
    no_review_path.write_text(json.dumps(no_review, ensure_ascii=False), encoding="utf-8")
    no_review_signature = write_transaction._file_signature(
        no_review_path, trusted_root=minimal_staging
    )
    progress = {
        "completed": {
            "writer_draft": {
                "details": {"accepted_artifacts": [{"sha256": draft_sha}]}
            }
        }
    }
    details = {
        "code": "minimal_mode",
        "no_review": no_review_signature,
        "runtime_review": {"path": "", "exists": False},
    }
    write_transaction._replay_minimal_receipt(
        tmp_path.resolve(),
        {"run_id": minimal_run, "chapter": 1},
        progress,
        "reviewer",
        details,
    )
    no_review["chapter"] = 2
    no_review_path.write_text(json.dumps(no_review, ensure_ascii=False), encoding="utf-8")
    changed_signature = write_transaction._file_signature(
        no_review_path, trusted_root=minimal_staging
    )
    with pytest.raises(WriteTransactionError, match="semantics changed"):
        write_transaction._replay_minimal_receipt(
            tmp_path.resolve(),
            {"run_id": minimal_run, "chapter": 1},
            progress,
            "review_pipeline",
            {**details, "no_review": changed_signature},
        )

    run_id = "review-replay"
    staging = tmp_path / ".webnovel" / "tmp" / "write-runs" / run_id
    staging.mkdir(parents=True)
    review_payload = {
        "chapter": 1,
        "issues": [],
        "issues_count": 0,
        "blocking_count": 0,
        "has_blocking": False,
        "dimension_results": [
            {"dimension": name, "conclusion": "pass"}
            for name in ("setting", "timeline", "continuity", "character", "logic")
        ],
        "summary": "通过",
    }
    reviewer_payload_path = staging / "reviewer-payload.json"
    reviewer_payload_path.write_text(
        json.dumps(review_payload, ensure_ascii=False), encoding="utf-8"
    )
    reviewer_payload_signature = write_transaction._file_signature(
        reviewer_payload_path, trusted_root=staging
    )
    normalized = write_transaction.parse_review_output(
        1, review_payload, review_mode="full", strict=True
    ).to_dict()
    bound_path = staging / "review_results.json"
    bound_path.write_text(json.dumps(normalized, ensure_ascii=False), encoding="utf-8")
    bound_signature = write_transaction._file_signature(bound_path, trusted_root=staging)
    full_details = {
        "review_sha256": bound_signature["sha256"],
        "review_artifact": bound_signature,
        "blocking_count": 0,
        "blocking_issue_hashes": [],
        "resolution_status": "not_required",
    }
    write_transaction._replay_review_pipeline(
        tmp_path.resolve(),
        {"run_id": run_id, "chapter": 1, "mode": "default"},
        {
            "completed": {
                "reviewer": {
                    "details": {
                        "source_bindings": {
                            "payload": {
                                "path": reviewer_payload_signature["path"],
                                "sha256": reviewer_payload_signature["sha256"],
                            }
                        }
                    }
                }
            }
        },
        full_details,
    )
    full_details["resolution_status"] = "decision_pending"
    with pytest.raises(WriteTransactionError, match="receipt semantics changed"):
        write_transaction._replay_review_pipeline(
            tmp_path.resolve(),
            {"run_id": run_id, "chapter": 1, "mode": "default"},
            {
                "completed": {
                    "reviewer": {
                        "details": {
                            "source_bindings": {
                                "payload": {
                                    "path": reviewer_payload_signature["path"],
                                    "sha256": reviewer_payload_signature["sha256"],
                                }
                            }
                        }
                    }
                }
            },
            full_details,
        )


def test_production_receipt_dispatch_replays_every_non_agent_truth_stage(
    tmp_path, monkeypatch
):
    run_id = "dispatch-replay"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / ".webnovel").mkdir()
    readiness = {"ready": True, "status": "ready", "problems": [], "agents": []}
    transaction = {
        "run_id": run_id,
        "chapter": 1,
        "mode": "default",
        "workspace_root": str(workspace),
        "route": {},
        "route_readiness_sha256": write_transaction._sha256_bytes(
            write_transaction._canonical_bytes(readiness)
        ),
        "stages": list(write_transaction.WRITE_STAGES),
        "test_only": False,
    }
    monkeypatch.setattr(
        write_transaction, "validate_route_readiness", lambda workspace_root, route: readiness
    )
    replayed_agents = []
    monkeypatch.setattr(
        write_transaction,
        "_replay_agent_receipt",
        lambda root, payload, receipt, progress: replayed_agents.append(receipt["stage"]),
    )
    monkeypatch.setattr(write_transaction, "_replay_review_pipeline", lambda *args: None)
    monkeypatch.setattr(write_transaction, "_replay_recovery_decision", lambda *args: {})
    monkeypatch.setattr(write_transaction, "_signature_is_current", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        write_transaction,
        "_verified_commit_input_hashes",
        lambda *args, **kwargs: {"review_results.json": "a" * 64},
    )
    monkeypatch.setattr(
        write_transaction,
        "_verified_materialized_commit_truth",
        lambda *args, **kwargs: {"commit_status": "accepted"},
    )
    monkeypatch.setattr(
        write_transaction,
        "run_write_gate",
        lambda root, *, chapter, stage: {
            "schema_version": "webnovel-write-gate/v1",
            "phase": "ready",
            "ok": True,
            "stage": stage,
            "project_root": str(root.resolve()),
            "chapter": chapter,
        },
    )
    audit = {
        "schema_version": "webnovel-write-current-truth-audit/v1",
        "run_id": run_id,
        "ok": True,
        "problems": [],
    }
    monkeypatch.setattr(write_transaction, "_audit_current_truth", lambda *args: audit)
    gate_details = lambda stage: {
        "gate_ok": True,
        "gate_schema": "webnovel-write-gate/v1",
        "gate_phase": "ready",
        "gate_report_sha256": write_transaction._sha256_bytes(
            write_transaction._canonical_bytes(
                {
                    "schema_version": "webnovel-write-gate/v1",
                    "phase": "ready",
                    "ok": True,
                    "stage": stage,
                    "project_root": str(tmp_path.resolve()),
                    "chapter": 1,
                }
            )
        ),
        "commit_input_hashes": (
            {"review_results.json": "a" * 64} if stage == "precommit" else {}
        ),
    }
    receipts = [
        {
            "stage": "preflight",
            "status": "completed",
            "details": {
                "gate_ok": True,
                "state": write_transaction._file_signature(
                    tmp_path / ".webnovel" / "state.json"
                ),
            },
        },
        {"stage": "prewrite", "status": "completed", "details": gate_details("prewrite")},
        {"stage": "context_agent", "status": "completed", "details": {}},
        {"stage": "writer_draft", "status": "completed", "details": {}},
        {"stage": "reviewer", "status": "completed", "details": {}},
        {
            "stage": "review_pipeline",
            "status": "completed",
            "details": {
                "review_sha256": "b" * 64,
                "review_artifact": {},
                "blocking_count": 0,
                "blocking_issue_hashes": [],
                "resolution_status": "not_required",
            },
        },
        {"stage": "writer_final", "status": "completed", "details": {}},
        {
            "stage": "promotion",
            "status": "completed",
            "details": {
                "source": {"sha256": "c" * 64},
                "target": {"sha256": "c" * 64},
                "changed": True,
                "owned_recovery": False,
                "recovery_decision": None,
                "lifecycle_lock": "held",
            },
        },
        {"stage": "data_agent", "status": "completed", "details": {}},
        {"stage": "precommit", "status": "completed", "details": gate_details("precommit")},
        {
            "stage": "commit",
            "status": "completed",
            "details": {"commit_status": "accepted", "commit": {"exists": True}},
        },
        {
            "stage": "projections",
            "status": "completed",
            "details": {
                "projection_status": {
                    name: "done" for name in write_transaction.PROJECTION_WRITERS
                },
                "projection_run_id": "projection-1",
                "projection_commit_hash": "d" * 64,
            },
        },
        {"stage": "postcommit", "status": "completed", "details": gate_details("postcommit")},
        {
            "stage": "backup",
            "status": "skipped",
            "details": {
                "ok": True,
                "status": "skipped",
                "code": "skipped_non_git",
                "project_root": str(tmp_path.resolve()),
                "chapter": 1,
            },
        },
        {
            "stage": "complete",
            "status": "completed",
            "details": {
                "verified": True,
                "truth_audit_sha256": write_transaction._sha256_bytes(
                    write_transaction._canonical_bytes(audit)
                ),
            },
        },
    ]
    progress = write_transaction._replay_completed_receipts(
        tmp_path.resolve(), transaction, receipts
    )
    assert progress["next_stage"] is None
    assert replayed_agents == [
        "context_agent",
        "writer_draft",
        "reviewer",
        "writer_final",
        "data_agent",
    ]


@pytest.mark.timeout(60)
def test_real_phase_transition_replay_accepts_exact_commit_and_rejects_tamper(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Exercise real gates and projection writers across the live TOCTOU boundary."""

    root = tmp_path.resolve()
    run_id = "real-phase-transition"
    parent_id = "66666666-6666-4666-8666-666666666666"
    _make_init_ready(root)
    _make_contracts(root, chapter=1)
    _mock_route_ready(monkeypatch, parent_thread_id=parent_id)
    transaction = begin_write_transaction(
        root,
        chapter=1,
        mode="default",
        parent_model="gpt-5.6-sol",
        workspace_root=root,
        run_id=run_id,
    )
    record_verified_stage_request(root, run_id, _write_stage_request(root, run_id, "preflight"))
    prewrite = record_verified_stage_request(
        root,
        run_id,
        _write_stage_request(root, run_id, "prewrite"),
    )
    historical_prewrite_hash = prewrite["details"]["gate_report_sha256"]
    sessions = root / "real-phase-sessions"

    def accept(stage: str, role: str, thread_id: str, payload: str | dict, artifacts=()):
        rollout = _write_rollout(
            sessions,
            role=role,
            thread_id=thread_id,
            parent_id=parent_id,
        )
        step = next(
            item for item in transaction["route"]["steps"] if item["agent_name"] == role
        )
        request = _write_agent_request(
            root,
            run_id,
            stage=stage,
            rollout=rollout,
            sessions_root=sessions,
            envelope=build_canned_envelope(step, artifacts=list(artifacts)),
            payload=payload,
            thread_id=thread_id,
            parent_id=parent_id,
            desktop_no_marker=True,
        )
        return accept_agent_request(root, run_id, request)

    context = "\n".join(
        f"## {heading}\n内容"
        for heading in ("开篇委托", "这章的故事", "这章的人物", "怎么写更顺", "收在哪里")
    )
    accept("context_agent", "webnovel_context_agent", "real-context", context)
    draft_artifact, draft_payload = _writer_artifact(root, run_id, "draft")
    accept(
        "writer_draft",
        "webnovel_writer",
        "real-draft",
        draft_payload,
        [draft_artifact],
    )
    review_payload = {
        "chapter": 1,
        "issues": [],
        "issues_count": 0,
        "blocking_count": 0,
        "has_blocking": False,
        "dimension_results": [
            {"dimension": name, "conclusion": "pass"}
            for name in ("setting", "timeline", "continuity", "character", "logic")
        ],
        "summary": "通过",
    }
    accept("reviewer", "webnovel_reviewer", "real-reviewer", review_payload)
    normalized_review = write_transaction.parse_review_output(
        1,
        review_payload,
        review_mode="full",
        strict=True,
    ).to_dict()
    runtime_review = root / ".webnovel" / "tmp" / "review_results.json"
    runtime_review.write_text(
        json.dumps(normalized_review, ensure_ascii=False),
        encoding="utf-8",
    )
    record_verified_stage_request(
        root,
        run_id,
        _write_stage_request(
            root,
            run_id,
            "review_pipeline",
            artifact={"path": str(runtime_review.resolve()), "sha256": _sha(runtime_review)},
        ),
    )
    final_artifact, final_payload = _writer_artifact(root, run_id, "polish")
    accept(
        "writer_final",
        "webnovel_writer",
        "real-final",
        final_payload,
        [final_artifact],
    )
    promote_verified_writer_artifact(
        root,
        run_id,
        target_path="正文/第0001章-真实阶段.md",
    )
    chapter_contract = root / ".story-system" / "chapters" / "chapter_001.json"
    contract_raw = chapter_contract.read_bytes()
    contract_stat = chapter_contract.stat()
    chapter_contract.write_text('{"meta":{"chapter":1},"late_tamper":true}', encoding="utf-8")
    with pytest.raises(WriteTransactionError, match="contracts changed after promotion"):
        write_transaction_status(root, run_id)
    chapter_contract.write_bytes(contract_raw)
    os.utime(
        chapter_contract,
        ns=(contract_stat.st_atime_ns, contract_stat.st_mtime_ns),
    )
    data_artifacts, data_payload = _data_agent_payload(root, run_id)
    accept(
        "data_agent",
        "webnovel_data_agent",
        "real-data",
        data_payload,
        data_artifacts,
    )
    current_prewrite = write_transaction.run_write_gate(root, chapter=1, stage="prewrite")
    assert current_prewrite["ok"] is True
    assert (
        write_transaction._sha256_bytes(write_transaction._canonical_bytes(current_prewrite))
        != historical_prewrite_hash
    )
    precommit = record_verified_stage_request(
        root,
        run_id,
        _write_stage_request(root, run_id, "precommit"),
    )

    runtime_tmp = root / ".webnovel" / "tmp"
    service = ChapterCommitService(root)
    commit_payload = service.build_commit(
        1,
        json.loads((runtime_tmp / "review_results.json").read_text(encoding="utf-8")),
        json.loads((runtime_tmp / "fulfillment_result.json").read_text(encoding="utf-8")),
        json.loads((runtime_tmp / "disambiguation_result.json").read_text(encoding="utf-8")),
        json.loads((runtime_tmp / "extraction_result.json").read_text(encoding="utf-8")),
    )
    service.persist_commit(commit_payload)
    real_projection_writers = service._projection_writers

    class FailingStateWriter:
        def apply(self, payload):
            raise RuntimeError("injected state failure")

    def projection_writers_with_failed_state():
        writers = real_projection_writers()
        writers["state"] = FailingStateWriter()
        return writers

    monkeypatch.setattr(
        service,
        "_projection_writers",
        projection_writers_with_failed_state,
    )
    projected = service.apply_projections(commit_payload)
    projection_run = latest_projection_run(root, chapter=1)
    assert projected["meta"]["status"] == "accepted"
    assert projection_status_from_run(projection_run)["state"] == "failed"
    assert projected["projection_status"]["state"] == "failed:injected state failure"
    assert projection_run["status"] == "failed"
    assert write_transaction_status(root, run_id)["next_stage"] == "commit"

    state_path = root / ".webnovel" / "state.json"
    state_raw = state_path.read_bytes()
    state_stat = state_path.stat()
    state_path.write_text('{"tampered_after_failed_state_writer":true}', encoding="utf-8")
    with pytest.raises(WriteTransactionError, match="preflight state changed without an exact"):
        write_transaction_status(root, run_id)
    state_path.write_bytes(state_raw)
    os.utime(state_path, ns=(state_stat.st_atime_ns, state_stat.st_mtime_ns))
    assert write_transaction_status(root, run_id)["next_stage"] == "commit"

    projection_log = root / ".webnovel" / "projection_log.jsonl"
    projection_log_raw = projection_log.read_bytes()

    def reject_projection_log_mutation(mutate) -> None:
        bad_run = json.loads(json.dumps(projection_run, ensure_ascii=False))
        mutate(bad_run)
        projection_log.write_text(
            json.dumps(bad_run, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        with pytest.raises(WriteTransactionError, match="exact latest projection binding"):
            record_verified_stage_request(
                root,
                run_id,
                _write_stage_request(root, run_id, "commit"),
            )
        projection_log.write_bytes(projection_log_raw)

    reject_projection_log_mutation(
        lambda run: (
            run["writers"]["state"].__setitem__("status", "pending"),
            run["projection_status"].__setitem__("state", "pending"),
            run.__setitem__("status", "pending"),
        )
    )
    reject_projection_log_mutation(
        lambda run: run["writers"]["state"].__setitem__(
            "error",
            "different failure",
        )
    )
    reject_projection_log_mutation(lambda run: run.__setitem__("status", "done"))

    body = root / "正文" / "第0001章-真实阶段.md"
    body_raw = body.read_bytes()
    body.write_text("外部篡改", encoding="utf-8")
    with pytest.raises(WriteTransactionError, match="promoted body"):
        record_verified_stage_request(root, run_id, _write_stage_request(root, run_id, "commit"))
    body.write_bytes(body_raw)

    bound_extraction = (
        root
        / ".webnovel"
        / "tmp"
        / "write-runs"
        / run_id
        / "commit-inputs"
        / "extraction_result.json"
    )
    extraction_raw = bound_extraction.read_bytes()
    bound_extraction.write_text("{}", encoding="utf-8")
    with pytest.raises(WriteTransactionError, match="run-bound data artifact changed"):
        record_verified_stage_request(root, run_id, _write_stage_request(root, run_id, "commit"))
    bound_extraction.write_bytes(extraction_raw)

    receipts_dir = root / ".webnovel" / "write-runs" / run_id / "receipts"
    immutable_before = {path.name: path.read_bytes() for path in receipts_dir.glob("*.json")}
    real_record = write_transaction.record_write_stage
    injected = {"done": False}

    def advance_projection_before_candidate(*args, **kwargs):
        if kwargs.get("stage") == "commit" and not injected["done"]:
            injected["done"] = True
            service.apply_projection_writers(projected)
        return real_record(*args, **kwargs)

    monkeypatch.setattr(
        write_transaction,
        "record_write_stage",
        advance_projection_before_candidate,
    )
    with pytest.raises(WriteTransactionError, match="current truth changed before projections"):
        record_verified_stage_request(
            root,
            run_id,
            _write_stage_request(root, run_id, "commit"),
        )
    assert not list(receipts_dir.glob("*-commit.json"))
    monkeypatch.setattr(write_transaction, "record_write_stage", real_record)
    commit_receipt = record_verified_stage_request(
        root,
        run_id,
        _write_stage_request(root, run_id, "commit"),
    )
    assert commit_receipt["details"]["promotion_body"]["sha256"] == _sha(body)
    assert commit_receipt["details"]["commit_input_hashes"] == precommit["details"][
        "commit_input_hashes"
    ]
    assert commit_receipt["details"]["projection_commit_path"] == str(
        (root / ".story-system" / "commits" / "chapter_001.commit.json").resolve()
    )
    assert commit_receipt["details"]["projection_status"]["state"] == "failed"
    assert (
        commit_receipt["details"]["commit_projection_status"]["state"]
        == "failed:injected state failure"
    )
    assert all((receipts_dir / name).read_bytes() == raw for name, raw in immutable_before.items())
    initial_projection_run_id = commit_receipt["details"]["projection_run_id"]
    monkeypatch.setattr(service, "_projection_writers", real_projection_writers)
    service.apply_projection_writers(projected)
    retried_projection_run = latest_projection_run(root, chapter=1)
    assert retried_projection_run["run_id"] != initial_projection_run_id
    assert projection_status_from_run(retried_projection_run)["state"] == "done"
    assert write_transaction_status(root, run_id)["next_stage"] == "projections"
    projection_receipt = record_verified_stage_request(
        root,
        run_id,
        _write_stage_request(root, run_id, "projections"),
    )
    assert projection_receipt["details"]["projection_run_id"] != initial_projection_run_id


def test_real_prewrite_successor_replay_still_blocks_contract_and_body_tamper(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path.resolve()
    run_id = "real-prepromotion-tamper"
    parent_id = "55555555-5555-4555-8555-555555555555"
    _make_init_ready(root)
    _make_contracts(root, chapter=1)
    _mock_route_ready(monkeypatch, parent_thread_id=parent_id)
    transaction = begin_write_transaction(
        root,
        chapter=1,
        mode="default",
        parent_model="gpt-5.6-sol",
        workspace_root=root,
        run_id=run_id,
    )
    record_verified_stage_request(root, run_id, _write_stage_request(root, run_id, "preflight"))
    record_verified_stage_request(root, run_id, _write_stage_request(root, run_id, "prewrite"))
    sessions = root / "prepromotion-sessions"
    rollout = _write_rollout(
        sessions,
        role="webnovel_context_agent",
        thread_id="prepromotion-context",
        parent_id=parent_id,
    )
    context = "\n".join(
        f"## {heading}\n内容"
        for heading in ("开篇委托", "这章的故事", "这章的人物", "怎么写更顺", "收在哪里")
    )
    request = _write_agent_request(
        root,
        run_id,
        stage="context_agent",
        rollout=rollout,
        sessions_root=sessions,
        envelope=build_canned_envelope(transaction["route"]["steps"][0]),
        payload=context,
        thread_id="prepromotion-context",
        parent_id=parent_id,
        desktop_no_marker=True,
    )
    accept_agent_request(root, run_id, request)

    concurrent_body = root / "正文" / "第0001章-外部.md"
    concurrent_body.write_text("外部正文", encoding="utf-8")
    with pytest.raises(WriteTransactionError, match="chapter body changed"):
        write_transaction_status(root, run_id)
    concurrent_body.unlink()
    assert write_transaction_status(root, run_id)["next_stage"] == "writer_draft"

    chapter_contract = root / ".story-system" / "chapters" / "chapter_001.json"
    original = chapter_contract.read_bytes()
    original_stat = chapter_contract.stat()
    chapter_contract.write_text('{"meta":{"chapter":1},"tampered":true}', encoding="utf-8")
    with pytest.raises(WriteTransactionError, match="contracts changed"):
        write_transaction_status(root, run_id)
    chapter_contract.write_bytes(original)
    os.utime(
        chapter_contract,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
    )
    assert write_transaction_status(root, run_id)["next_stage"] == "writer_draft"


def test_unbound_accepted_commit_neither_deadlocks_early_replay_nor_grants_state_bypass(
    tmp_path: Path,
    monkeypatch,
) -> None:
    parent_id = "44444444-4444-4444-8444-444444444444"
    _mock_route_ready(monkeypatch, parent_thread_id=parent_id)

    preexisting = (tmp_path / "preexisting").resolve()
    _make_init_ready(preexisting)
    _make_contracts(preexisting, chapter=1)
    preexisting_commit = (
        preexisting / ".story-system" / "commits" / "chapter_001.commit.json"
    )
    preexisting_commit.parent.mkdir(parents=True, exist_ok=True)
    preexisting_commit.write_text(
        json.dumps({"meta": {"chapter": 1, "status": "accepted"}}),
        encoding="utf-8",
    )
    begin_write_transaction(
        preexisting,
        chapter=1,
        mode="default",
        parent_model="gpt-5.6-sol",
        workspace_root=preexisting,
        run_id="preexisting-accepted",
    )
    assert write_transaction_status(preexisting, "preexisting-accepted")["next_stage"] == "preflight"

    concurrent = (tmp_path / "concurrent").resolve()
    _make_init_ready(concurrent)
    _make_contracts(concurrent, chapter=1)
    run_id = "concurrent-accepted"
    begin_write_transaction(
        concurrent,
        chapter=1,
        mode="default",
        parent_model="gpt-5.6-sol",
        workspace_root=concurrent,
        run_id=run_id,
    )
    record_verified_stage_request(
        concurrent,
        run_id,
        _write_stage_request(concurrent, run_id, "preflight"),
    )
    record_verified_stage_request(
        concurrent,
        run_id,
        _write_stage_request(concurrent, run_id, "prewrite"),
    )
    commit_path = concurrent / ".story-system" / "commits" / "chapter_001.commit.json"
    commit_path.parent.mkdir(parents=True, exist_ok=True)
    commit_path.write_text(
        json.dumps({"meta": {"chapter": 1, "status": "accepted"}}),
        encoding="utf-8",
    )
    assert write_transaction_status(concurrent, run_id)["next_stage"] == "context_agent"

    state_path = concurrent / ".webnovel" / "state.json"
    state_raw = state_path.read_bytes()
    state_stat = state_path.stat()
    state_path.write_text('{"concurrent_commit_must_not_authorize":true}', encoding="utf-8")
    with pytest.raises(WriteTransactionError, match="preflight state changed without an exact"):
        write_transaction_status(concurrent, run_id)
    state_path.write_bytes(state_raw)
    os.utime(state_path, ns=(state_stat.st_atime_ns, state_stat.st_mtime_ns))
    assert write_transaction_status(concurrent, run_id)["next_stage"] == "context_agent"


def test_minimal_no_review_resumes_after_first_skip_receipt(tmp_path, monkeypatch):
    run_id = "minimal-partial-receipt"
    begin_write_transaction(
        tmp_path,
        chapter=1,
        mode="minimal",
        parent_model="gpt-5.6-sol",
        run_id=run_id,
        test_only=True,
    )
    for stage in ("preflight", "prewrite"):
        _advance_test_stage(tmp_path, run_id, stage, details={"gate_ok": True})
    _advance_test_stage(tmp_path, run_id, "context_agent")
    draft = tmp_path / ".webnovel" / "tmp" / "write-runs" / run_id / "draft.md"
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text("草稿", encoding="utf-8")
    _advance_test_stage(
        tmp_path,
        run_id,
        "writer_draft",
        details={"accepted_artifacts": [{"path": str(draft), "sha256": _sha(draft)}]},
    )
    real_record = write_transaction.record_write_stage
    failed = {"done": False}

    def fail_pipeline_once(*args, **kwargs):
        if kwargs.get("stage") == "review_pipeline" and not failed["done"]:
            failed["done"] = True
            raise WriteTransactionError("injected review pipeline receipt failure")
        return real_record(*args, **kwargs)

    monkeypatch.setattr(write_transaction, "record_write_stage", fail_pipeline_once)
    with pytest.raises(WriteTransactionError, match="injected"):
        record_minimal_no_review(tmp_path, run_id)
    assert write_transaction.write_transaction_status(tmp_path, run_id)["next_stage"] == "review_pipeline"

    monkeypatch.setattr(write_transaction, "record_write_stage", real_record)
    recovered = record_minimal_no_review(tmp_path, run_id)
    assert recovered["reviewer_receipt"]["stage"] == "reviewer"
    assert recovered["review_pipeline_receipt"]["stage"] == "review_pipeline"
    status = write_transaction.write_transaction_status(tmp_path, run_id)
    assert status["next_stage"] == "writer_final"
    receipts = write_transaction._validated_receipts(write_transaction._run_dir(tmp_path, run_id))
    assert [item["stage"] for item in receipts].count("reviewer") == 1


def test_promotion_recovers_owned_target_after_receipt_failure(tmp_path, monkeypatch):
    run_id = "promotion-owned-recovery"
    begin_write_transaction(
        tmp_path,
        chapter=1,
        mode="default",
        parent_model="gpt-5.6-sol",
        run_id=run_id,
        test_only=True,
    )
    for stage in ("preflight", "prewrite"):
        _advance_test_stage(tmp_path, run_id, stage, details={"gate_ok": True})
    for stage in ("context_agent", "writer_draft", "reviewer"):
        _advance_test_stage(tmp_path, run_id, stage)
    _advance_test_stage(
        tmp_path,
        run_id,
        "review_pipeline",
        details={"review_sha256": "a" * 64},
    )
    polished = tmp_path / ".webnovel" / "tmp" / "write-runs" / run_id / "polished.md"
    polished.parent.mkdir(parents=True, exist_ok=True)
    polished.write_text("验证终稿", encoding="utf-8")
    _advance_test_stage(
        tmp_path,
        run_id,
        "writer_final",
        details={
            "operation": "polish",
            "accepted_artifacts": [
                {"kind": "polished", "path": str(polished), "sha256": _sha(polished)}
            ],
        },
    )
    target = tmp_path / "正文" / "第0001章.md"
    real_record = write_transaction.record_write_stage
    failed = {"done": False}

    def fail_promotion_receipt_once(*args, **kwargs):
        if kwargs.get("stage") == "promotion" and not failed["done"]:
            failed["done"] = True
            raise WriteTransactionError("injected promotion receipt failure")
        return real_record(*args, **kwargs)

    monkeypatch.setattr(write_transaction, "record_write_stage", fail_promotion_receipt_once)
    with pytest.raises(WriteTransactionError, match="injected"):
        promote_verified_writer_artifact(tmp_path, run_id, target_path=target)
    assert target.read_bytes() == polished.read_bytes()
    assert write_transaction.write_transaction_status(tmp_path, run_id)["next_stage"] == "promotion"

    monkeypatch.setattr(write_transaction, "record_write_stage", real_record)
    receipt = promote_verified_writer_artifact(tmp_path, run_id, target_path=target)
    assert receipt["details"]["owned_recovery"] is True
    assert receipt["details"]["target"]["sha256"] == _sha(polished)
    assert receipt["details"]["changed"] is True


def _targeted_fix_parent_message(role: str, text: str) -> dict:
    kind = "input_text" if role == "user" else "output_text"
    return {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": role,
            "content": [{"type": kind, "text": text}],
        },
    }


def _targeted_fix_parent_rollout(
    root: Path,
    monkeypatch,
    *,
    parent_id: str = "88888888-8888-4888-8888-888888888888",
) -> tuple[Path, Path]:
    sessions = root / "targeted-fix-parent-sessions"
    rollout = (
        sessions
        / "2026"
        / "08"
        / "09"
        / f"rollout-targeted-fix-{parent_id}.jsonl"
    )
    rollout.parent.mkdir(parents=True, exist_ok=True)
    events = [
        {
            "type": "session_meta",
            "payload": {
                "id": parent_id,
                "source": "codex_desktop",
                "model": "gpt-5.6-sol",
            },
        },
        {
            "type": "turn_context",
            "payload": {
                "turn_id": "parent-turn-001",
                "model": "gpt-5.6-sol",
                "effort": "high",
            },
        },
        _targeted_fix_parent_message("user", "请继续本机写章事务。"),
    ]
    rollout.write_text(
        "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events),
        encoding="utf-8",
    )
    monkeypatch.setattr(write_transaction, "TRUSTED_CODEX_SESSIONS_ROOT", sessions)
    monkeypatch.setenv("CODEX_THREAD_ID", parent_id)
    return sessions.resolve(), rollout.resolve()


def _targeted_fix_route_ready(monkeypatch) -> None:
    def ready(workspace_root, route):
        return {
            "ready": True,
            "status": "ready",
            "problems": [],
            "agents": [
                {
                    "agent_name": step["agent_name"],
                    "current": True,
                    "contract_hash": step["contract_hash"],
                    "managed_sha256": step["managed_sha256"],
                }
                for step in route.get("steps") or []
            ],
        }

    monkeypatch.setattr(write_transaction, "validate_route_readiness", ready)


def _blocking_review_with_duplicate_occurrences() -> dict:
    blocker = {
        "severity": "critical",
        "category": "setting",
        "location": "第3段",
        "description": "倒计时数字与既有设定冲突",
        "evidence": "正文写成三息，但任务书要求五息",
        "fix_hint": "只修正倒计时数字，不改变事件顺序",
        "blocking": True,
    }
    return {
        "chapter": 1,
        "issues": [dict(blocker), dict(blocker)],
        "issues_count": 2,
        "blocking_count": 2,
        "has_blocking": True,
        "dimension_results": [
            {"dimension": "setting", "conclusion": "存在两个阻断设定问题"},
            {"dimension": "timeline", "conclusion": "pass"},
            {"dimension": "continuity", "conclusion": "pass"},
            {"dimension": "character", "conclusion": "pass"},
            {"dimension": "logic", "conclusion": "pass"},
        ],
        "summary": "两处内容相同但 occurrence 不同的阻断问题。",
    }


def _append_targeted_fix_parent_choice(
    rollout: Path,
    *,
    marker: str,
    answer: str,
) -> None:
    with rollout.open("a", encoding="utf-8", newline="\n") as handle:
        for event in (
            _targeted_fix_parent_message("assistant", f"请选择。\n{marker}"),
            _targeted_fix_parent_message("user", answer),
        ):
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def _prepare_production_targeted_fix_case(
    root: Path,
    monkeypatch,
    *,
    run_id: str,
    answer: str = "targeted_fix",
) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    _targeted_fix_route_ready(monkeypatch)
    sessions, parent_rollout = _targeted_fix_parent_rollout(root, monkeypatch)
    parent_id = "88888888-8888-4888-8888-888888888888"
    transaction = begin_write_transaction(
        root,
        chapter=1,
        mode="default",
        parent_model="gpt-5.6-sol",
        parent_reasoning_effort="high",
        workspace_root=root,
        run_id=run_id,
    )
    _prepare_production_gates(root, run_id, monkeypatch)

    context_payload = "\n".join(
        f"## {heading}\n内容"
        for heading in ("开篇委托", "这章的故事", "这章的人物", "怎么写更顺", "收在哪里")
    )
    context_rollout = _write_rollout(
        sessions,
        role="webnovel_context_agent",
        thread_id=f"{run_id}-context",
        parent_id=parent_id,
    )
    context_request = _write_agent_request(
        root,
        run_id,
        stage="context_agent",
        rollout=context_rollout,
        sessions_root=sessions,
        envelope=build_canned_envelope(transaction["route"]["steps"][0]),
        payload=context_payload,
        thread_id=f"{run_id}-context",
        parent_id=parent_id,
    )
    accept_agent_request(root, run_id, context_request)

    draft_artifact, draft_payload = _writer_artifact(root, run_id, "draft")
    writer_step = next(
        step
        for step in transaction["route"]["steps"]
        if step["agent_name"] == "webnovel_writer"
    )
    draft_rollout = _write_rollout(
        sessions,
        role="webnovel_writer",
        thread_id=f"{run_id}-writer-draft",
        parent_id=parent_id,
    )
    draft_request = _write_agent_request(
        root,
        run_id,
        stage="writer_draft",
        rollout=draft_rollout,
        sessions_root=sessions,
        envelope=build_canned_envelope(writer_step, artifacts=[draft_artifact]),
        payload=draft_payload,
        thread_id=f"{run_id}-writer-draft",
        parent_id=parent_id,
    )
    accept_agent_request(root, run_id, draft_request)

    review_payload = _blocking_review_with_duplicate_occurrences()
    reviewer_step = next(
        step
        for step in transaction["route"]["steps"]
        if step["agent_name"] == "webnovel_reviewer"
    )
    reviewer_rollout = _write_rollout(
        sessions,
        role="webnovel_reviewer",
        thread_id=f"{run_id}-reviewer",
        parent_id=parent_id,
    )
    reviewer_request = _write_agent_request(
        root,
        run_id,
        stage="reviewer",
        rollout=reviewer_rollout,
        sessions_root=sessions,
        envelope=build_canned_envelope(reviewer_step),
        payload=review_payload,
        thread_id=f"{run_id}-reviewer",
        parent_id=parent_id,
    )
    accept_agent_request(root, run_id, reviewer_request)

    runtime_review = root / ".webnovel" / "tmp" / "review_results.json"
    runtime_review.write_text(
        json.dumps(review_payload, ensure_ascii=False),
        encoding="utf-8",
    )
    review_request = _write_stage_request(
        root,
        run_id,
        "review_pipeline",
        artifact={"path": str(runtime_review.resolve()), "sha256": _sha(runtime_review)},
    )
    review_receipt = record_verified_stage_request(root, run_id, review_request)
    original_review = Path(review_receipt["details"]["review_artifact"]["path"])
    original_review_bytes = original_review.read_bytes()

    prepared = write_transaction.prepare_targeted_fix_decision(root, run_id)
    _append_targeted_fix_parent_choice(
        parent_rollout,
        marker=prepared["binding_marker"],
        answer=answer,
    )
    selected = write_transaction.record_targeted_fix_decision(
        root,
        run_id,
        Path(prepared["decision_request"]["path"]),
    )
    return {
        "root": root,
        "run_id": run_id,
        "transaction": transaction,
        "sessions": sessions,
        "parent_id": parent_id,
        "parent_rollout": parent_rollout,
        "writer_step": writer_step,
        "review_receipt": review_receipt,
        "original_review": original_review,
        "original_review_bytes": original_review_bytes,
        "prepared": prepared,
        "selected": selected,
    }


def _targeted_fix_writer_payload(
    root: Path,
    run_id: str,
    resolutions: list[dict],
) -> tuple[dict, dict]:
    staging = root / ".webnovel" / "tmp" / "write-runs" / run_id
    polished = staging / "polished.md"
    text = "第一段正文改为五息倒计时。\n\n第二段正文保持事件顺序。"
    polished.write_text(text, encoding="utf-8")
    artifact = {
        "kind": "polished",
        "path": str(polished.resolve()),
        "sha256": _sha(polished),
        "bytes": len(polished.read_bytes()),
        "word_count": len("".join(text.split())),
    }
    manifest = {
        "schema_version": "webnovel-writer-manifest/v2",
        "run_id": run_id,
        "agent_name": "webnovel_writer",
        "operation": "targeted_fix",
        "status": "completed",
        "inputs": [],
        "outputs": [artifact],
        "resolutions": [dict(item) for item in resolutions],
        "problems": [],
        "warnings": [],
    }
    manifest_path = staging / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    payload = {
        "schema_version": "webnovel-writer-result/v2",
        "status": "completed",
        "run_id": run_id,
        "operation": "targeted_fix",
        "artifacts": [artifact],
        "manifest_path": str(manifest_path.resolve()),
        "manifest_sha256": _sha(manifest_path),
        "resolutions": [dict(item) for item in resolutions],
        "problems": [],
        "warnings": [],
    }
    return artifact, payload


def _accept_targeted_fix_writer(
    case: dict,
    resolutions: list[dict],
    *,
    child_suffix: str = "writer-final",
):
    root = case["root"]
    run_id = case["run_id"]
    artifact, payload = _targeted_fix_writer_payload(root, run_id, resolutions)
    thread_id = f"{run_id}-{child_suffix}"
    rollout = _write_rollout(
        case["sessions"],
        role="webnovel_writer",
        thread_id=thread_id,
        parent_id=case["parent_id"],
    )
    request = _write_agent_request(
        root,
        run_id,
        stage="writer_final",
        rollout=rollout,
        sessions_root=case["sessions"],
        envelope=build_canned_envelope(case["writer_step"], artifacts=[artifact]),
        payload=payload,
        thread_id=thread_id,
        parent_id=case["parent_id"],
    )
    return accept_agent_request(root, run_id, request)


def _targeted_fix_resolutions(case: dict) -> list[dict]:
    return [
        {
            "issue_index": item["issue_index"],
            "issue_sha256": item["issue_sha256"],
            "status": "resolved",
            "resolution_summary": f"已修复 occurrence {item['issue_index']}。",
        }
        for item in case["prepared"]["blocking_issues"]
    ]


def _data_agent_payload(root: Path, run_id: str) -> tuple[list[dict], dict]:
    documents = {
        "fulfillment_result": {
            "planned_nodes": [],
            "covered_nodes": [],
            "missed_nodes": [],
            "extra_nodes": [],
        },
        "disambiguation_result": {"pending": []},
        "extraction_result": {
            "accepted_events": [],
            "state_deltas": [],
            "entity_deltas": [],
        },
    }
    artifacts = []
    for name, document in documents.items():
        path = root / ".webnovel" / "tmp" / f"{name}.json"
        path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        artifacts.append(
            {
                "name": name,
                "path": str(path.resolve()),
                "sha256": _sha(path),
                "bytes": len(path.read_bytes()),
            }
        )
    return artifacts, {
        "schema_version": "webnovel-data-result/v1",
        "status": "completed",
        "run_id": run_id,
        "artifacts": artifacts,
        "pending_count": 0,
        "missed_nodes_count": 0,
        "problems": [],
        "warnings": [],
    }


def test_production_targeted_fix_resolves_exact_duplicate_occurrences_and_binds_precommit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    case = _prepare_production_targeted_fix_case(
        tmp_path / "targeted-success",
        monkeypatch,
        run_id="targeted-success",
    )
    occurrences = case["prepared"]["blocking_issues"]
    assert [item["issue_index"] for item in occurrences] == [0, 1]
    assert occurrences[0]["issue_sha256"] == occurrences[1]["issue_sha256"]
    resolutions = _targeted_fix_resolutions(case)

    writer_receipt = _accept_targeted_fix_writer(case, resolutions)
    targeted = writer_receipt["details"]["targeted_fix"]
    assert writer_receipt["details"]["operation"] == "targeted_fix"
    assert targeted["blocking_issues"] == occurrences
    assert case["original_review"].read_bytes() == case["original_review_bytes"]

    original = json.loads(case["original_review_bytes"].decode("utf-8"))
    assert original["blocking_count"] == 2
    assert all(issue["blocking"] is True for issue in original["issues"])
    resolved_path = Path(targeted["resolved_review"]["path"])
    resolved = json.loads(resolved_path.read_text(encoding="utf-8"))
    assert resolved["blocking_count"] == 0
    assert resolved["has_blocking"] is False
    assert resolved["issues_count"] == 0
    assert resolved["issues"] == []
    assert resolved["dimension_results"][0] == {
        "dimension": "setting",
        "conclusion": "pass",
    }

    resolution_path = Path(targeted["resolution_receipt"]["path"])
    resolution = json.loads(resolution_path.read_text(encoding="utf-8"))
    assert set(resolution) == {
        "schema_version",
        "run_id",
        "transaction_sha256",
        "chapter",
        "decision_request",
        "decision_receipt",
        "decision_receipt_sha256",
        "draft",
        "original_review",
        "writer_payload",
        "writer_manifest",
        "writer_rollout",
        "final_artifact",
        "resolutions",
        "receipt_sha256",
    }
    resolution_body = dict(resolution)
    claimed_resolution_sha = resolution_body.pop("receipt_sha256")
    assert claimed_resolution_sha == write_transaction._receipt_hash(resolution_body)
    assert resolution["schema_version"] == write_transaction.TARGETED_FIX_RESOLUTION_SCHEMA
    assert resolution["resolutions"] == resolutions
    assert resolution["original_review"] == case["review_receipt"]["details"]["review_artifact"]
    decision_receipt = json.loads(
        Path(case["selected"]["decision_receipt"]["path"]).read_text(encoding="utf-8")
    )
    assert resolution["decision_receipt_sha256"] == decision_receipt["receipt_sha256"]

    root = case["root"]
    run_id = case["run_id"]
    promote_verified_writer_artifact(
        root,
        run_id,
        target_path="正文/第0001章-定点修复.md",
    )
    data_artifacts, data_payload = _data_agent_payload(root, run_id)
    data_step = next(
        step
        for step in case["transaction"]["route"]["steps"]
        if step["agent_name"] == "webnovel_data_agent"
    )
    data_rollout = _write_rollout(
        case["sessions"],
        role="webnovel_data_agent",
        thread_id=f"{run_id}-data",
        parent_id=case["parent_id"],
    )
    data_request = _write_agent_request(
        root,
        run_id,
        stage="data_agent",
        rollout=data_rollout,
        sessions_root=case["sessions"],
        envelope=build_canned_envelope(data_step, artifacts=data_artifacts),
        payload=data_payload,
        thread_id=f"{run_id}-data",
        parent_id=case["parent_id"],
    )
    accept_agent_request(root, run_id, data_request)
    precommit = record_verified_stage_request(
        root,
        run_id,
        _write_stage_request(root, run_id, "precommit"),
    )
    resolved_sha = targeted["resolved_review"]["sha256"]
    original_sha = case["review_receipt"]["details"]["review_artifact"]["sha256"]
    assert resolved_sha != original_sha
    assert precommit["details"]["commit_input_hashes"]["review_results.json"] == resolved_sha
    runtime_review = root / ".webnovel" / "tmp" / "review_results.json"
    assert runtime_review.read_bytes() == resolved_path.read_bytes()


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("missing", "do not cover every blocking issue occurrence"),
        ("duplicate", "invalid_writer_resolutions"),
        ("unknown", "do not cover every blocking issue occurrence"),
    ],
)
def test_production_targeted_fix_rejects_inexact_resolution_sets(
    tmp_path: Path,
    monkeypatch,
    mutation: str,
    message: str,
) -> None:
    case = _prepare_production_targeted_fix_case(
        tmp_path / mutation,
        monkeypatch,
        run_id=f"targeted-{mutation}",
    )
    resolutions = _targeted_fix_resolutions(case)
    if mutation == "missing":
        resolutions = resolutions[:1]
    elif mutation == "duplicate":
        resolutions = [dict(resolutions[0]), dict(resolutions[0])]
    else:
        resolutions[1] = {
            **resolutions[1],
            "issue_index": 99,
            "issue_sha256": "f" * 64,
        }

    with pytest.raises(WriteTransactionError, match=message):
        _accept_targeted_fix_writer(case, resolutions)
    evidence = case["root"] / ".webnovel" / "tmp" / "write-runs" / case["run_id"] / "evidence"
    assert not (evidence / "targeted-fix-resolution.json").exists()
    assert not (case["root"] / ".webnovel" / "tmp" / "write-runs" / case["run_id"] / "review_results.resolved.json").exists()
    assert write_transaction_status(case["root"], case["run_id"])["next_stage"] == "writer_final"


def test_production_targeted_fix_rejects_non_authorizing_parent_choice(
    tmp_path: Path,
    monkeypatch,
) -> None:
    case = _prepare_production_targeted_fix_case(
        tmp_path / "wrong-choice",
        monkeypatch,
        run_id="targeted-wrong-choice",
        answer="report_only",
    )
    assert case["selected"]["selected"] == "report_only"
    with pytest.raises(WriteTransactionError, match="does not authorize targeted_fix"):
        _accept_targeted_fix_writer(case, _targeted_fix_resolutions(case))


def test_production_targeted_fix_rejects_stale_decision_request(
    tmp_path: Path,
    monkeypatch,
) -> None:
    case = _prepare_production_targeted_fix_case(
        tmp_path / "stale-decision",
        monkeypatch,
        run_id="targeted-stale-decision",
    )
    request_path = Path(case["prepared"]["decision_request"]["path"])
    stale = json.loads(request_path.read_text(encoding="utf-8"))
    stale["scope"]["chapter"] = 2
    request_path.write_text(json.dumps(stale, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(WriteTransactionError, match="decision request is stale"):
        _accept_targeted_fix_writer(case, _targeted_fix_resolutions(case))


def test_production_targeted_fix_rejects_cross_scope_decision_receipt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _prepare_production_targeted_fix_case(
        tmp_path / "source-scope",
        monkeypatch,
        run_id="targeted-source-scope",
    )
    source_receipt = Path(source["selected"]["decision_receipt"]["path"]).read_bytes()
    target = _prepare_production_targeted_fix_case(
        tmp_path / "target-scope",
        monkeypatch,
        run_id="targeted-target-scope",
    )
    target_receipt = Path(target["selected"]["decision_receipt"]["path"])
    target_receipt.write_bytes(source_receipt)
    with pytest.raises(WriteTransactionError, match="decision receipt rejected"):
        _accept_targeted_fix_writer(target, _targeted_fix_resolutions(target))


def _prepare_production_recovery_case(
    root: Path,
    monkeypatch,
    *,
    run_id: str,
    accepted_before: bool = False,
) -> dict[str, object]:
    """Build a production transaction paused immediately before promotion."""

    parent_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    parent_model = "gpt-5.6-sol"
    parent_effort = "high"
    sessions = root / "trusted-parent-sessions"
    rollout = sessions / "2026" / "08" / "09" / f"rollout-{parent_id}.jsonl"
    rollout.parent.mkdir(parents=True, exist_ok=True)
    parent_events = [
        {
            "type": "session_meta",
            "payload": {
                "id": parent_id,
                "model": parent_model,
                "source": "codex_desktop",
            },
        },
        {
            "type": "turn_context",
            "payload": {
                "turn_id": "parent-turn-001",
                "model": parent_model,
                "effort": parent_effort,
            },
        },
    ]
    rollout.write_text(
        "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in parent_events),
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_THREAD_ID", parent_id)
    monkeypatch.setattr(write_transaction, "TRUSTED_CODEX_SESSIONS_ROOT", sessions.resolve())

    def route_ready(workspace_root, route):
        return {
            "ready": True,
            "status": "ready",
            "problems": [],
            "agents": [
                {
                    "agent_name": step["agent_name"],
                    "current": True,
                    "contract_hash": step["contract_hash"],
                    "managed_sha256": step["managed_sha256"],
                }
                for step in route.get("steps") or []
            ],
        }

    monkeypatch.setattr(write_transaction, "validate_route_readiness", route_ready)
    body = root / "正文" / "第0001章-恢复测试.md"
    body.parent.mkdir(parents=True, exist_ok=True)
    body.write_text("事务开始前正文", encoding="utf-8")
    if accepted_before:
        commit = root / ".story-system" / "commits" / "chapter_001.commit.json"
        commit.parent.mkdir(parents=True, exist_ok=True)
        commit.write_text(
            json.dumps({"meta": {"chapter": 1, "status": "accepted"}}, ensure_ascii=False),
            encoding="utf-8",
        )

    transaction = begin_write_transaction(
        root,
        chapter=1,
        mode="default",
        parent_model=parent_model,
        parent_reasoning_effort=parent_effort,
        workspace_root=root,
        run_id=run_id,
    )
    if not accepted_before:
        body.write_text("作者在事务中手改的正文", encoding="utf-8")
    polished = root / ".webnovel" / "tmp" / "write-runs" / run_id / "polished.md"
    polished.parent.mkdir(parents=True, exist_ok=True)
    polished.write_text("经过验证的 writer 终稿", encoding="utf-8")
    artifact = {
        "kind": "polished",
        **write_transaction._file_signature(polished, trusted_root=polished.parent),
    }
    progress = {
        "next_index": WRITE_STAGES.index("promotion"),
        "next_stage": "promotion",
        "completed": {
            "writer_final": {
                "stage": "writer_final",
                "status": "completed",
                "details": {
                    "operation": "polish",
                    "accepted_artifacts": [artifact],
                },
            }
        },
        "last_failure": None,
        "last_receipt_sha256": "",
    }
    monkeypatch.setattr(
        write_transaction,
        "_replayed_progress",
        lambda selected_root, selected_transaction: ([], progress),
    )
    return {
        "transaction": transaction,
        "progress": progress,
        "sessions": sessions.resolve(),
        "rollout": rollout.resolve(),
        "body": body.resolve(),
        "polished": polished.resolve(),
    }


def _select_production_recovery(
    root: Path,
    run_id: str,
    case: dict[str, object],
    *,
    answer: str,
    target: Path | None = None,
) -> tuple[dict[str, object], Path]:
    selected_target = target or Path(case["body"])
    prepared = write_transaction.prepare_write_recovery_decision(
        root,
        run_id,
        target_path=selected_target,
    )
    rollout = Path(case["rollout"])
    events = [
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": f"请选择本轮恢复方式。\n{prepared['binding_marker']}",
                    }
                ],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": answer}],
            },
        },
    ]
    with rollout.open("a", encoding="utf-8", newline="") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    decision = write_transaction.record_write_recovery_decision(
        root,
        run_id,
        target_path=selected_target,
        request_file=Path(prepared["decision_request"]["path"]),
    )
    return decision, Path(decision["decision_receipt"]["path"])


def test_production_recovery_replace_promotes_and_replays_trusted_parent_receipt(
    tmp_path,
    monkeypatch,
):
    run_id = "production-recovery-replace"
    case = _prepare_production_recovery_case(tmp_path, monkeypatch, run_id=run_id)
    decision, receipt_path = _select_production_recovery(
        tmp_path,
        run_id,
        case,
        answer="replace_with_verified",
    )
    assert decision["selected"] == "replace_with_verified"
    status = write_transaction.write_transaction_status(tmp_path, run_id)
    resume = write_transaction.build_write_resume_plan(tmp_path, run_id)
    assert status["status"] == "in_progress"
    assert status["next_stage"] == "promotion"
    assert status["terminal_recovery"] is None
    assert status["production_complete"] is False
    assert resume["action"] == "resume_promotion"
    monkeypatch.setattr(
        write_transaction,
        "_replay_completed_receipts",
        lambda root, transaction, receipts, **kwargs: case["progress"],
    )

    promotion = promote_verified_writer_artifact(
        tmp_path,
        run_id,
        target_path=Path(case["body"]),
        decision_receipt=receipt_path,
    )

    assert Path(case["body"]).read_bytes() == Path(case["polished"]).read_bytes()
    recovery = promotion["details"]["recovery_decision"]
    assert recovery["selected"] == "replace_with_verified"
    assert recovery["request"]["sha256"]
    assert recovery["receipt"]["sha256"]
    write_transaction._replay_recovery_decision(
        tmp_path.resolve(),
        case["transaction"],
        promotion["details"],
    )


@pytest.mark.parametrize("answer", ["keep_current", "cancel"])
def test_production_recovery_keep_or_cancel_never_writes_body(
    tmp_path,
    monkeypatch,
    answer,
):
    run_id = f"production-recovery-{answer}"
    case = _prepare_production_recovery_case(tmp_path, monkeypatch, run_id=run_id)
    before = Path(case["body"]).read_bytes()
    decision, receipt_path = _select_production_recovery(
        tmp_path,
        run_id,
        case,
        answer=answer,
    )
    assert decision["selected"] == answer

    status = write_transaction.write_transaction_status(tmp_path, run_id)
    resume = write_transaction.build_write_resume_plan(tmp_path, run_id)
    expected_status = "cancelled" if answer == "cancel" else "stopped"
    assert status["status"] == expected_status
    assert status["next_stage"] is None
    assert status["terminal_recovery"]["selected"] == answer
    assert status["terminal_recovery"]["promotion_completed"] is False
    assert status["production_complete"] is False
    assert "promotion" not in status["completed_stages"]
    assert status["rerun_agents_allowed"] is False
    assert status["new_transaction_required_to_change_choice"] is True
    assert resume["action"] == expected_status
    assert resume["must_not_rerun_agents"] is True

    with pytest.raises(WriteTransactionError, match="new transaction"):
        promote_verified_writer_artifact(
            tmp_path,
            run_id,
            target_path=Path(case["body"]),
            decision_receipt=receipt_path,
        )
    assert Path(case["body"]).read_bytes() == before


def test_production_recovery_status_only_and_accepted_commit_never_overwrite(
    tmp_path,
    monkeypatch,
):
    run_id = "production-recovery-status"
    case = _prepare_production_recovery_case(
        tmp_path,
        monkeypatch,
        run_id=run_id,
        accepted_before=True,
    )
    before = Path(case["body"]).read_bytes()
    decision, receipt_path = _select_production_recovery(
        tmp_path,
        run_id,
        case,
        answer="status_only",
    )
    assert decision["selected"] == "status_only"

    status = write_transaction.write_transaction_status(tmp_path, run_id)
    resume = write_transaction.build_write_resume_plan(tmp_path, run_id)
    assert status["status"] == "stopped"
    assert status["next_stage"] is None
    assert status["terminal_recovery"]["selected"] == "status_only"
    assert status["terminal_recovery"]["promotion_completed"] is False
    assert status["production_complete"] is False
    assert "promotion" not in status["completed_stages"]
    assert resume["action"] == "stopped"
    assert resume["must_not_rerun_agents"] is True

    with pytest.raises(WriteTransactionError, match="new transaction"):
        promote_verified_writer_artifact(
            tmp_path,
            run_id,
            target_path=Path(case["body"]),
            decision_receipt=receipt_path,
        )
    assert Path(case["body"]).read_bytes() == before


def test_terminal_recovery_is_durable_and_revalidates_the_parent_rollout(
    tmp_path,
    monkeypatch,
):
    run_id = "production-recovery-durable-stop"
    case = _prepare_production_recovery_case(tmp_path, monkeypatch, run_id=run_id)
    _, receipt_path = _select_production_recovery(
        tmp_path,
        run_id,
        case,
        answer="keep_current",
    )
    first = write_transaction.write_transaction_status(tmp_path, run_id)
    assert first["status"] == "stopped"

    Path(case["body"]).write_text("作者在终止后继续修改", encoding="utf-8")
    second = write_transaction.write_transaction_status(tmp_path, run_id)
    assert second["status"] == "stopped"
    assert second["terminal_recovery"]["receipt"]["path"] == str(receipt_path)
    with pytest.raises(WriteTransactionError, match="new transaction"):
        write_transaction.prepare_write_recovery_decision(
            tmp_path,
            run_id,
            target_path=Path(case["body"]),
        )

    request_path = Path(second["terminal_recovery"]["request"]["path"])
    request = json.loads(request_path.read_text(encoding="utf-8"))
    duplicate_marker = {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": request["binding_marker"]}],
        },
    }
    with Path(case["rollout"]).open("a", encoding="utf-8", newline="") as handle:
        handle.write(json.dumps(duplicate_marker, ensure_ascii=False) + "\n")
    with pytest.raises(WriteTransactionError, match="terminal receipt rejected"):
        write_transaction.write_transaction_status(tmp_path, run_id)


def test_production_recovery_naked_replace_string_remains_unauthorized(
    tmp_path,
    monkeypatch,
):
    run_id = "production-recovery-naked"
    case = _prepare_production_recovery_case(tmp_path, monkeypatch, run_id=run_id)
    before = Path(case["body"]).read_bytes()

    with pytest.raises(WriteRecoveryChoiceRequired):
        promote_verified_writer_artifact(
            tmp_path,
            run_id,
            target_path=Path(case["body"]),
            recovery_decision="replace_with_verified",
        )
    assert Path(case["body"]).read_bytes() == before


def test_production_recovery_stale_or_cross_target_receipt_is_rejected(
    tmp_path,
    monkeypatch,
):
    stale_run = "production-recovery-stale"
    stale = _prepare_production_recovery_case(tmp_path, monkeypatch, run_id=stale_run)
    _, stale_receipt = _select_production_recovery(
        tmp_path,
        stale_run,
        stale,
        answer="replace_with_verified",
    )
    Path(stale["body"]).write_text("作者在回答后再次修改", encoding="utf-8")
    with pytest.raises(WriteTransactionError, match="stale under the chapter lock"):
        promote_verified_writer_artifact(
            tmp_path,
            stale_run,
            target_path=Path(stale["body"]),
            decision_receipt=stale_receipt,
        )
    assert Path(stale["body"]).read_text(encoding="utf-8") == "作者在回答后再次修改"

    cross_root = tmp_path / "cross-target"
    cross_root.mkdir()
    cross_run = "production-recovery-cross-target"
    cross = _prepare_production_recovery_case(cross_root, monkeypatch, run_id=cross_run)
    _, cross_receipt = _select_production_recovery(
        cross_root,
        cross_run,
        cross,
        answer="replace_with_verified",
    )
    other_target = cross_root / "正文" / "第0001章-另一份.md"
    other_target.write_text("另一份作者正文", encoding="utf-8")
    with pytest.raises(WriteTransactionError, match="stale under the chapter lock"):
        promote_verified_writer_artifact(
            cross_root,
            cross_run,
            target_path=other_target,
            decision_receipt=cross_receipt,
        )
    assert other_target.read_text(encoding="utf-8") == "另一份作者正文"


def test_accepted_commit_appearing_after_replace_receipt_still_blocks_overwrite(
    tmp_path,
    monkeypatch,
):
    run_id = "production-recovery-late-accepted"
    case = _prepare_production_recovery_case(tmp_path, monkeypatch, run_id=run_id)
    before = Path(case["body"]).read_bytes()
    _, receipt_path = _select_production_recovery(
        tmp_path,
        run_id,
        case,
        answer="replace_with_verified",
    )
    commit = tmp_path / ".story-system" / "commits" / "chapter_001.commit.json"
    commit.parent.mkdir(parents=True, exist_ok=True)
    commit.write_text(
        json.dumps({"meta": {"chapter": 1, "status": "accepted"}}, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(WriteTransactionError, match="amend transaction"):
        promote_verified_writer_artifact(
            tmp_path,
            run_id,
            target_path=Path(case["body"]),
            decision_receipt=receipt_path,
        )
    assert Path(case["body"]).read_bytes() == before
