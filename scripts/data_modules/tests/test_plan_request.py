from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from data_modules import plan_request
from data_modules.plan_request import (
    PlanRequestError,
    build_plan_request,
    plan_request_sha256,
    save_plan_request,
    validate_plan_request,
)


def test_build_plan_request_is_parent_only_and_batches_exactly(tmp_path):
    request = build_plan_request(
        tmp_path,
        volume=2,
        start_chapter=11,
        end_chapter=33,
        parent_model="gpt-5.6-sol",
        parent_reasoning_effort="high",
        batch_size=10,
        run_id="plan-v2-test",
    )

    assert request["executor"] == "parent"
    assert request["planning_model"] == "gpt-5.6-sol"
    assert request["invoked_agents"] == []
    assert request["batches"] == [
        {"start_chapter": 11, "end_chapter": 20},
        {"start_chapter": 21, "end_chapter": 30},
        {"start_chapter": 31, "end_chapter": 33},
    ]
    assert request["facts_write_allowed"] is False


@pytest.mark.parametrize(
    "run_id",
    [".", "..", "...", ".hidden", "trailing.", "CON", "con.json", "LPT1"],
)
def test_build_plan_request_rejects_noncanonical_run_id_without_writes(tmp_path, run_id):
    with pytest.raises(PlanRequestError, match="run_id must be canonical"):
        build_plan_request(
            tmp_path,
            volume=1,
            start_chapter=1,
            end_chapter=2,
            parent_model="gpt-5.6-sol",
            parent_reasoning_effort="high",
            run_id=run_id,
        )

    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("executor", "agent"),
        ("invoked_agents", ["webnovel_writer"]),
        ("planning_model", "gpt-5.6-luna"),
        ("facts_write_allowed", True),
        ("fallback_allowed", True),
        ("batch_size", 13),
        ("run_id", "../escape"),
        ("batches", []),
    ],
)
def test_plan_request_rejects_delegation_and_tampering(tmp_path, field, value):
    request = build_plan_request(
        tmp_path,
        volume=1,
        start_chapter=1,
        end_chapter=2,
        parent_model="gpt-5.6-sol",
        parent_reasoning_effort="high",
        run_id="plan-test",
    )
    request[field] = value

    with pytest.raises(PlanRequestError):
        validate_plan_request(request, project_root=tmp_path)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"volume": 0},
        {"start_chapter": 3, "end_chapter": 2},
        {"parent_model": ""},
        {"parent_reasoning_effort": None},
        {"batch_size": 0},
    ],
)
def test_build_plan_request_rejects_invalid_fields(tmp_path, kwargs):
    values = {
        "volume": 1,
        "start_chapter": 1,
        "end_chapter": 2,
        "parent_model": "gpt-5.6-sol",
        "parent_reasoning_effort": "high",
        "batch_size": 10,
        "run_id": "plan-test",
    }
    values.update(kwargs)
    with pytest.raises(PlanRequestError):
        build_plan_request(tmp_path, **values)


def test_plan_request_cli_success_and_failure(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "plan-request",
            "--project-root",
            str(tmp_path),
            "--volume",
            "1",
            "--start-chapter",
            "1",
            "--end-chapter",
            "3",
            "--parent-model",
            "gpt-5.6-sol",
            "--parent-reasoning-effort",
            "high",
            "--run-id",
            "cli-plan",
            "--save",
        ],
    )
    plan_request.main()
    saved = json.loads(capsys.readouterr().out)
    assert saved["run_id"] == "cli-plan"
    assert json.loads(Path(saved["request_path"]).read_text(encoding="utf-8")) == saved

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "plan-request",
            "--project-root",
            str(tmp_path),
            "--volume",
            "0",
            "--start-chapter",
            "1",
            "--end-chapter",
            "3",
            "--parent-model",
            "gpt-5.6-sol",
            "--parent-reasoning-effort",
            "high",
        ],
    )
    with pytest.raises(SystemExit) as caught:
        plan_request.main()
    assert caught.value.code == 2
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_saved_plan_request_is_immutable_and_hash_stable(tmp_path):
    request = build_plan_request(
        tmp_path,
        volume=1,
        start_chapter=1,
        end_chapter=2,
        parent_model="gpt-5.6-sol",
        parent_reasoning_effort="high",
        run_id="saved-plan",
    )
    path = save_plan_request(request)
    assert save_plan_request(request) == path
    assert len(plan_request_sha256(request)) == 64
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(PlanRequestError, match="different content"):
        save_plan_request(request)


def test_save_plan_request_rechecks_lock_after_wait_and_writes_nothing(tmp_path, monkeypatch):
    request = build_plan_request(
        tmp_path,
        volume=1,
        start_chapter=1,
        end_chapter=2,
        parent_model="gpt-5.6-sol",
        parent_reasoning_effort="high",
        run_id="request-lock-swap",
    )
    target = Path(request["request_path"])
    lock_path = target.with_suffix(target.suffix + ".lock")
    entered = {"value": False}
    real_reparse = plan_request._is_reparse_point

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

    monkeypatch.setattr(plan_request, "FileLock", WaitingLock)
    monkeypatch.setattr(plan_request, "_is_reparse_point", becomes_reparse)

    with pytest.raises(PlanRequestError, match="unsafe plan request control path"):
        save_plan_request(request)

    assert not target.exists()


def test_plan_request_rejects_reparse_project_root_before_resolve(tmp_path, monkeypatch):
    real_reparse = plan_request._is_reparse_point

    def project_root_is_reparse(path):
        candidate = Path(path)
        return candidate == tmp_path or real_reparse(candidate)

    monkeypatch.setattr(plan_request, "_is_reparse_point", project_root_is_reparse)

    with pytest.raises(PlanRequestError, match="reparse-point project_root"):
        build_plan_request(
            tmp_path,
            volume=1,
            start_chapter=1,
            end_chapter=2,
            parent_model="gpt-5.6-sol",
            parent_reasoning_effort="high",
            run_id="root-reparse",
        )

    assert not (tmp_path / ".webnovel").exists()


def test_plan_request_path_read_and_validation_negative_matrix(tmp_path, monkeypatch):
    with pytest.raises(PlanRequestError, match="not a directory"):
        plan_request._resolved_root(tmp_path / "missing")
    assert plan_request._is_reparse_point(tmp_path / "missing") is False
    with pytest.raises(PlanRequestError, match="escapes project"):
        plan_request._prepare_request_target(tmp_path, tmp_path.parent / "outside.json")
    unsafe_parent = tmp_path / "unsafe-parent"
    unsafe_parent.write_text("file", encoding="utf-8")
    with pytest.raises(PlanRequestError, match="unsafe plan request parent"):
        plan_request._prepare_request_target(tmp_path, unsafe_parent / "request.json")
    with pytest.raises(PlanRequestError, match="unreadable"):
        plan_request._read_request_bytes(tmp_path, tmp_path / "absent.json")
    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"x" * (plan_request._MAX_REQUEST_BYTES + 1))
    with pytest.raises(PlanRequestError, match="too large"):
        plan_request._read_request_bytes(tmp_path, oversized)

    request = build_plan_request(
        tmp_path,
        volume=1,
        start_chapter=1,
        end_chapter=2,
        parent_model="gpt-5.6-sol",
        parent_reasoning_effort="high",
        run_id="validation-paths",
    )
    for field, value, code in (
        ("project_root", str(tmp_path.parent), "project_root_mismatch"),
        ("manifest_path", str(tmp_path / "wrong-manifest.json"), "manifest_path_out_of_bounds"),
        ("request_path", str(tmp_path / "wrong-request.json"), "request_path_out_of_bounds"),
    ):
        tampered = dict(request)
        tampered[field] = value
        with pytest.raises(PlanRequestError, match=code):
            validate_plan_request(tampered, project_root=tmp_path)
    invalid_root = dict(request)
    invalid_root["project_root"] = str(tmp_path / "missing-root")
    with pytest.raises(PlanRequestError, match="invalid_project_root"):
        validate_plan_request(invalid_root)

    monkeypatch.setattr(plan_request, "FileLock", None)
    with pytest.raises(PlanRequestError, match="filelock is required"):
        save_plan_request(request)


def test_save_plan_request_rejects_existing_bom(tmp_path):
    request = build_plan_request(
        tmp_path,
        volume=1,
        start_chapter=1,
        end_chapter=2,
        parent_model="gpt-5.6-sol",
        parent_reasoning_effort="high",
        run_id="request-bom",
    )
    path = Path(request["request_path"])
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xef\xbb\xbf{}")

    with pytest.raises(PlanRequestError, match="UTF-8 BOM"):
        save_plan_request(request)
