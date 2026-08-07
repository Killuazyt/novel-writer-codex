#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Focused M1 coverage for runtime error handling and command adapters."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


def _ensure_scripts_on_path() -> None:
    scripts_dir = Path(__file__).resolve().parents[2]
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))


_ensure_scripts_on_path()

from data_modules import projections  # noqa: E402
from data_modules import run_ledger  # noqa: E402
from data_modules import run_logger  # noqa: E402
from data_modules import user_report  # noqa: E402
from data_modules.write_gates import (  # noqa: E402
    format_gate_report,
    gate_report,
    issue,
    run_write_gate,
)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _accepted_commit(*, projections_done: bool = True) -> dict[str, object]:
    status = "done" if projections_done else "pending"
    return {
        "meta": {"chapter": 1, "status": "accepted"},
        "projection_status": {
            "state": status,
            "index": status,
            "summary": status,
            "memory": status,
            "vector": status,
        },
    }


def test_projection_commit_reader_reports_corruption_and_io_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commit_path = tmp_path / "chapter_001.commit.json"
    commit_path.write_text("{broken", encoding="utf-8")
    payload, error = projections._read_commit(commit_path)
    assert payload == {}
    assert error.startswith("invalid_json:")

    _write_json(commit_path, ["not", "an", "object"])
    assert projections._read_commit(commit_path) == ({}, "commit_not_object")

    _write_json(commit_path, {"meta": {"chapter": 1}})
    payload, error = projections._read_commit(commit_path)
    assert error == ""
    assert payload["projection_status"] == projections.DEFAULT_PROJECTION_STATUS

    def _raise_os_error(self: Path, *args: object, **kwargs: object) -> str:
        raise OSError("simulated read failure")

    monkeypatch.setattr(Path, "read_text", _raise_os_error)
    payload, error = projections._read_commit(commit_path)
    assert payload == {}
    assert error == "read_error:simulated read failure"


def test_projection_helpers_cover_invalid_ranges_and_report_formats(tmp_path: Path) -> None:
    assert projections._projection_failed({"projection_status": ["done"]}) is True
    assert projections._projection_failed({"projection_status": {"state": "failed:disk"}}) is True
    assert projections._projection_failed({"projection_status": {"state": "done"}}) is False

    invalid = projections.replay_projections(tmp_path, start_chapter=2, end_chapter=1)
    assert invalid["error"] == "invalid_chapter_range"
    assert json.loads(projections.format_projection_report(invalid))["ok"] is False

    retry_text = projections.format_projection_report(
        {
            "action": "retry",
            "ok": False,
            "chapter": 4,
            "commit_path": "commit.json",
            "projection_status": {},
            "error": "missing_commit",
        },
        "text",
    )
    assert "ERROR projections retry" in retry_text
    assert "missing_commit" in retry_text

    replay_text = projections.format_projection_report(
        {
            "action": "replay",
            "ok": False,
            "start_chapter": 1,
            "end_chapter": 2,
            "error": "",
            "results": [
                {"chapter": 1, "ok": True, "projection_status": {"state": "done"}},
                {"chapter": 2, "ok": False, "error": "missing_commit"},
            ],
        },
        "text",
    )
    assert "chapter 1: OK" in replay_text
    assert "chapter 2: ERROR missing_commit" in replay_text


def test_projection_main_routes_retry_and_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    retry_report = {
        "action": "retry",
        "ok": True,
        "chapter": 3,
        "commit_path": "commit.json",
        "projection_status": {"state": "done"},
        "error": "",
    }
    monkeypatch.setattr(projections, "retry_projection", lambda root, *, chapter: retry_report)
    monkeypatch.setattr(
        sys,
        "argv",
        ["projections", "--project-root", str(tmp_path), "retry", "--chapter", "3", "--format", "text"],
    )
    with pytest.raises(SystemExit) as exc_info:
        projections.main()
    assert exc_info.value.code == 0
    assert "OK projections retry" in capsys.readouterr().out

    replay_report = {
        "action": "replay",
        "ok": False,
        "start_chapter": 2,
        "end_chapter": 3,
        "error": "missing_commit",
        "results": [],
    }
    monkeypatch.setattr(
        projections,
        "replay_projections",
        lambda root, *, start_chapter, end_chapter: replay_report,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "projections",
            "--project-root",
            str(tmp_path),
            "replay",
            "--from-chapter",
            "2",
            "--to-chapter",
            "3",
        ],
    )
    with pytest.raises(SystemExit) as exc_info:
        projections.main()
    assert exc_info.value.code == 1
    assert json.loads(capsys.readouterr().out)["action"] == "replay"


def test_write_gate_report_formats_all_diagnostics(tmp_path: Path) -> None:
    blocker = issue(
        "missing_contract",
        message="合同缺失",
        path=".story-system/contracts/chapter_001.json",
        impact="不能提交",
        repair="重新生成合同",
        details={"chapter": 1},
    )
    warning = issue("optional_note", message="可选提示", severity="warning")
    report = gate_report(
        stage="prewrite",
        project_root=tmp_path,
        chapter=1,
        phase="planning",
        errors=[blocker],
        warnings=[warning],
        details={"checked": True},
    )

    assert report["ok"] is False
    assert json.loads(format_gate_report(report))["details"] == {"checked": True}
    text = format_gate_report(report, "text")
    assert "ERROR write-gate prewrite" in text
    assert "path: .story-system/contracts/chapter_001.json" in text
    assert "impact: 不能提交" in text
    assert "repair: 重新生成合同" in text
    assert "WARNING optional_note: 可选提示" in text

    nonblocking = gate_report(
        stage="prewrite",
        project_root=tmp_path,
        chapter=1,
        phase="planning",
        errors=[issue("advisory", message="仅提示", severity="warning")],
    )
    assert nonblocking["ok"] is True
    with pytest.raises(ValueError, match="unknown write-gate stage"):
        run_write_gate(tmp_path, chapter=1, stage="unknown")


def test_run_logger_redacts_sequences_scalars_and_appends(tmp_path: Path) -> None:
    payload = ["token=abc", 7, {"password": "hidden", "safe": True}]
    assert run_logger.redact_payload(payload) == [
        "token=<redacted>",
        7,
        {"password": "<redacted>", "safe": True},
    ]

    run_logger.write_run_log(tmp_path, event="first", payload={"value": 1})
    run_logger.write_run_log(tmp_path, event="second", payload={"value": 2}, append=True)
    records = [json.loads(line) for line in run_logger.log_path(tmp_path).read_text(encoding="utf-8").splitlines()]
    assert [item["event"] for item in records] == ["first", "second"]


def test_run_logger_main_handles_errors_and_both_formats(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["run-logger", "--project-root", str(tmp_path), "--event", "bad", "--payload-json", "{"],
    )
    with pytest.raises(SystemExit, match="payload-json 不是合法 JSON"):
        run_logger.main()

    monkeypatch.setattr(
        sys,
        "argv",
        ["run-logger", "--project-root", str(tmp_path), "--event", "bad", "--payload-json", "[]"],
    )
    with pytest.raises(SystemExit, match="payload-json 必须是 JSON object"):
        run_logger.main()

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run-logger",
            "--project-root",
            str(tmp_path),
            "--event",
            "json-event",
            "--payload-json",
            '{"api_key":"secret-value"}',
        ],
    )
    run_logger.main()
    json_result = json.loads(capsys.readouterr().out)
    assert json_result["record"]["payload"]["api_key"] == "<redacted>"

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run-logger",
            "--project-root",
            str(tmp_path),
            "--event",
            "text-event",
            "--append",
            "--format",
            "text",
        ],
    )
    run_logger.main()
    assert capsys.readouterr().out.strip().endswith("run_last.log")


def test_run_ledger_load_signature_and_validation_edges(tmp_path: Path) -> None:
    ledger_file = run_ledger.ledger_path(tmp_path)
    _write_json(ledger_file, {"schema_version": run_ledger.SCHEMA_VERSION, "write": []})
    assert run_ledger.load_ledger(tmp_path)["write"] == {}

    missing = run_ledger.file_signature(tmp_path / "missing.txt")
    assert missing["exists"] is False
    with pytest.raises(ValueError, match="unknown write step"):
        run_ledger.record_write_step(tmp_path, chapter=1, step="publish", status="completed")

    assert run_ledger._same_signature(None, {}) is False
    assert run_ledger._trusted_output(None, "chapter_file") is False
    assert run_ledger._trusted_output({"outputs": []}, "chapter_file") is False
    assert run_ledger._trusted_output({"outputs": {"chapter_file": "bad"}}, "chapter_file") is False
    assert run_ledger._trusted_input(None, "chapter_file", None) is False
    assert run_ledger._trusted_input({"inputs": []}, "chapter_file", tmp_path / "missing.txt") is False
    assert run_ledger._trusted_input({"inputs": {"chapter_file": "bad"}}, "chapter_file", tmp_path / "missing.txt") is False


def test_run_ledger_projection_backup_and_contract_helpers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(run_ledger, "latest_projection_run", lambda root, *, chapter: None)
    assert run_ledger._projection_done(tmp_path, 1) is False
    assert run_ledger._backup_exists(tmp_path, 1) is False

    backup = tmp_path / ".webnovel" / "backups" / "ch0001-test.zip"
    backup.parent.mkdir(parents=True)
    backup.write_text("backup", encoding="utf-8")
    assert run_ledger._backup_exists(tmp_path, 1) is True

    contract = tmp_path / ".story-system" / "contracts" / "chapter_001.json"
    contract.parent.mkdir(parents=True)
    contract.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        run_ledger,
        "contract_files_for_chapter",
        lambda root, chapter: {"existing": contract, "missing": tmp_path / "missing.json"},
    )
    assert run_ledger._latest_contract_mtime(tmp_path, 1) == contract.stat().st_mtime_ns


def test_run_ledger_resume_handles_malformed_run_and_newer_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_json(
        run_ledger.ledger_path(tmp_path),
        {
            "schema_version": run_ledger.SCHEMA_VERSION,
            "write": {"chapter_001": ["malformed"]},
        },
    )
    empty_plan = run_ledger.build_write_resume_plan(tmp_path, chapter=1)
    assert empty_plan["resume_from"] == "draft"

    _write_json(
        run_ledger.ledger_path(tmp_path),
        {"schema_version": run_ledger.SCHEMA_VERSION, "write": {}},
    )

    chapter_file = tmp_path / "正文" / "第0001章.md"
    chapter_file.parent.mkdir(parents=True)
    chapter_file.write_text("正文", encoding="utf-8")
    run_ledger.record_write_step(
        tmp_path,
        chapter=1,
        step="draft",
        status="completed",
        outputs={"chapter_file": chapter_file},
    )
    monkeypatch.setattr(run_ledger, "_latest_contract_mtime", lambda root, chapter: chapter_file.stat().st_mtime_ns + 1)
    plan = run_ledger.build_write_resume_plan(tmp_path, chapter=1)
    assert plan["resume_from"] == "draft"
    assert any(item["code"] == "outline_newer_than_draft" for item in plan["needs_user_confirmation"])


def test_run_ledger_resume_retries_projection_but_skips_existing_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chapter_file = tmp_path / "正文" / "第0001章.md"
    chapter_file.parent.mkdir(parents=True)
    chapter_file.write_text("正文", encoding="utf-8")
    _write_json(
        tmp_path / ".story-system" / "commits" / "chapter_001.commit.json",
        _accepted_commit(projections_done=False),
    )
    backup = tmp_path / ".webnovel" / "backups" / "ch0001-snapshot.zip"
    backup.parent.mkdir(parents=True)
    backup.write_text("backup", encoding="utf-8")
    monkeypatch.setattr(run_ledger, "latest_projection_run", lambda root, *, chapter: None)

    plan = run_ledger.build_write_resume_plan(tmp_path, chapter=1, mode="fast")
    actions = {item["step"]: item["action"] for item in plan["steps"]}
    assert actions["projection"] == "retry"
    assert actions["backup"] == "skip"
    assert plan["resume_from"] == "projection"
    assert plan["mode"] == "fast"


def test_run_ledger_format_and_json_argument_parsers() -> None:
    plan = {
        "chapter": 2,
        "resume_from": "review",
        "steps": [{"step": "draft", "action": "skip", "reason": "正文可信"}],
        "needs_user_confirmation": [{"code": "confirm", "message": "请确认"}],
    }
    assert json.loads(run_ledger.format_resume_plan(plan))["chapter"] == 2
    text = run_ledger.format_resume_plan(plan, "text")
    assert "resume_from: review" in text
    assert "needs_user_confirmation:" in text

    assert run_ledger._parse_path_map("") == {}
    assert run_ledger._parse_path_map('{"draft":"chapter.md"}') == {"draft": "chapter.md"}
    assert run_ledger._parse_string_list("") == []
    assert run_ledger._parse_string_list('["one",2]') == ["one", "2"]
    with pytest.raises(ValueError, match="不是合法 JSON"):
        run_ledger._parse_path_map("{")
    with pytest.raises(ValueError, match="必须是 JSON object"):
        run_ledger._parse_path_map("[]")
    with pytest.raises(ValueError, match="不是合法 JSON"):
        run_ledger._parse_string_list("[")
    with pytest.raises(ValueError, match="必须是 JSON list"):
        run_ledger._parse_string_list("{}")


def test_run_ledger_main_records_and_renders_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    chapter_file = tmp_path / "正文" / "第0001章.md"
    chapter_file.parent.mkdir(parents=True)
    chapter_file.write_text("正文", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run-ledger",
            "--project-root",
            str(tmp_path),
            "record-write-step",
            "--chapter",
            "1",
            "--step",
            "draft",
            "--status",
            "completed",
            "--mode",
            "minimal",
            "--inputs-json",
            json.dumps({"chapter_file": str(chapter_file)}),
            "--outputs-json",
            json.dumps({"chapter_file": str(chapter_file)}),
            "--problems-json",
            '["problem"]',
            "--auto-handled-json",
            '["fixed"]',
            "--duration-ms",
            "25",
        ],
    )
    run_ledger.main()
    entry = json.loads(capsys.readouterr().out)
    assert entry["duration_ms"] == 25
    assert entry["problems"] == ["problem"]

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run-ledger",
            "--project-root",
            str(tmp_path),
            "record-write-step",
            "--chapter",
            "1",
            "--step",
            "draft",
            "--status",
            "completed",
            "--format",
            "text",
        ],
    )
    run_ledger.main()
    assert capsys.readouterr().out.strip() == "draft: completed"

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run-ledger",
            "--project-root",
            str(tmp_path),
            "write-resume",
            "--chapter",
            "1",
            "--format",
            "text",
        ],
    )
    run_ledger.main()
    assert "resume_from:" in capsys.readouterr().out


def test_run_ledger_main_converts_bad_json_to_cli_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run-ledger",
            "--project-root",
            str(tmp_path),
            "record-write-step",
            "--chapter",
            "1",
            "--step",
            "draft",
            "--status",
            "completed",
            "--inputs-json",
            "{",
        ],
    )
    with pytest.raises(SystemExit, match="不是合法 JSON"):
        run_ledger.main()


def test_user_report_reader_and_relative_path_error_branches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload_path = tmp_path / "payload.json"
    payload_path.write_text("{broken", encoding="utf-8")
    payload, error = user_report._read_json(payload_path)
    assert payload == {}
    assert error.startswith("invalid_json:")

    _write_json(payload_path, ["not", "object"])
    assert user_report._read_json(payload_path) == ({}, "not_object")

    def _raise_os_error(self: Path, *args: object, **kwargs: object) -> str:
        raise OSError("simulated report read failure")

    monkeypatch.setattr(Path, "read_text", _raise_os_error)
    assert user_report._read_json(payload_path) == ({}, "read_error:simulated report read failure")

    def _raise_resolve_error(self: Path, *args: object, **kwargs: object) -> Path:
        raise OSError("cannot resolve")

    monkeypatch.setattr(Path, "resolve", _raise_resolve_error)
    assert user_report._rel(tmp_path, payload_path) == str(payload_path)


def test_user_report_routes_all_stages_and_infers_chapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(user_report, "build_project_status", lambda root: {"target_chapter": 7})
    monkeypatch.setattr(
        user_report,
        "build_write_report",
        lambda root, *, chapter, volume=None: {"stage": "write", "chapter": chapter, "volume": volume},
    )
    monkeypatch.setattr(
        user_report,
        "build_review_report",
        lambda root, *, chapter, volume=None: {"stage": "review", "chapter": chapter, "volume": volume},
    )
    monkeypatch.setattr(
        user_report,
        "build_init_report",
        lambda root, *, chapter=None, volume=None: {"stage": "init", "chapter": chapter, "volume": volume},
    )
    monkeypatch.setattr(
        user_report,
        "build_plan_report",
        lambda root, *, chapter=None, volume=None: {"stage": "plan", "chapter": chapter, "volume": volume},
    )

    assert user_report.build_user_report(tmp_path, stage="write")["chapter"] == 7
    assert user_report.build_user_report(tmp_path, stage="review")["chapter"] == 7
    assert user_report.build_user_report(tmp_path, stage="init", volume=2)["volume"] == 2
    assert user_report.build_user_report(tmp_path, stage="plan", chapter=3)["chapter"] == 3
    with pytest.raises(ValueError, match="unknown user report stage"):
        user_report.build_user_report(tmp_path, stage="publish")


def test_user_report_rendering_covers_empty_failed_and_timed_report() -> None:
    report = {
        "overall_status": user_report.STATUS_FAILED,
        "files": [],
        "issues": {
            "auto_handled": [],
            "needs_confirmation": [],
            "must_handle": [],
        },
        "timing": {"total_ms": 2500},
        "next_actions": [{"description": "人工检查", "command": "人工检查"}],
    }
    text = user_report.render_user_report_text(report)
    assert "暂无可确认的产物" in text
    assert "约 2 秒" in text
    assert "run_last.log" in text
    assert "- 人工检查" in text

    report["next_actions"] = []
    text = user_report.format_user_report(report)
    assert "$webnovel-doctor" in text
    encoded = user_report.format_user_report(report, "json")
    assert json.loads(encoded)["overall_status"] == user_report.STATUS_FAILED


def test_user_report_main_parses_and_prints_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}

    def _build(root: str | Path, *, stage: str, chapter: int | None, volume: int | None) -> dict[str, object]:
        captured.update(root=Path(root), stage=stage, chapter=chapter, volume=volume)
        return {"overall_status": "completed", "files": [], "issues": {}, "next_actions": []}

    monkeypatch.setattr(user_report, "build_user_report", _build)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "user-report",
            "--project-root",
            str(tmp_path),
            "--stage",
            "review",
            "--chapter",
            "5",
            "--volume",
            "2",
            "--format",
            "json",
        ],
    )
    user_report.main()
    assert captured == {"root": tmp_path, "stage": "review", "chapter": 5, "volume": 2}
    assert json.loads(capsys.readouterr().out)["overall_status"] == "completed"
