#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import sqlite3
import sys
from pathlib import Path

from .test_project_phase import _make_contracts, _make_init_ready
from .test_project_phase import _write_json


def _ensure_scripts_on_path() -> None:
    scripts_dir = Path(__file__).resolve().parents[2]
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))


_ensure_scripts_on_path()

import data_modules.doctor as doctor_module  # noqa: E402
from data_modules.codex_agent_runtime import snapshot_protected_state  # noqa: E402
from data_modules.projection_log import append_projection_run  # noqa: E402


def test_doctor_init_ready_does_not_require_story_contracts(tmp_path, monkeypatch):
    _make_init_ready(tmp_path)
    monkeypatch.setattr(doctor_module, "_python_checks", lambda: [])

    report = doctor_module.build_doctor_report(tmp_path)

    assert report["ok"] is True
    assert report["phase"] == "init_ready"
    assert not [item for item in report["checks"] if str(item["id"]).startswith("file.contract.")]


def test_doctor_missing_init_file_blocks_with_repair(tmp_path, monkeypatch):
    _make_init_ready(tmp_path)
    (tmp_path / "大纲" / "总纲.md").unlink()
    monkeypatch.setattr(doctor_module, "_python_checks", lambda: [])

    report = doctor_module.build_doctor_report(tmp_path)

    assert report["ok"] is False
    matches = [item for item in report["checks"] if item["id"] == "file.required.大纲/总纲.md"]
    assert matches
    assert matches[0]["status"] == "error"
    assert matches[0]["repair"]


def test_doctor_checks_contracts_after_story_system_starts(tmp_path, monkeypatch):
    _make_init_ready(tmp_path)
    _make_contracts(tmp_path, chapter=1)
    (tmp_path / ".story-system" / "reviews" / "chapter_001.review.json").unlink()
    monkeypatch.setattr(doctor_module, "_python_checks", lambda: [])

    report = doctor_module.build_doctor_report(tmp_path)

    assert report["ok"] is False
    contract_checks = [item for item in report["checks"] if item["id"] == "file.contract.review"]
    assert contract_checks
    assert contract_checks[0]["status"] == "error"


def test_doctor_no_project_reports_repair(monkeypatch):
    monkeypatch.setattr(doctor_module, "_python_checks", lambda: [])

    report = doctor_module.build_doctor_report(None)

    assert report["ok"] is False
    assert report["phase"] == "no_project"
    assert report["recommended_actions"]


def test_doctor_warns_when_old_project_has_commit_without_projection_log(tmp_path, monkeypatch):
    _make_init_ready(tmp_path)
    _write_json(
        tmp_path / ".story-system" / "commits" / "chapter_001.commit.json",
        {
            "meta": {"chapter": 1, "status": "accepted"},
            "projection_status": {"state": "done"},
        },
    )
    monkeypatch.setattr(doctor_module, "_python_checks", lambda: [])

    report = doctor_module.build_doctor_report(tmp_path)

    assert report["ok"] is True
    matches = [item for item in report["checks"] if item["id"] == "projection_log.present"]
    assert matches
    assert matches[0]["status"] == "warning"


def test_doctor_blocks_pending_projection_log_run(tmp_path, monkeypatch):
    _make_init_ready(tmp_path)
    commit_payload = {
        "meta": {"chapter": 1, "status": "accepted"},
        "projection_status": {"state": "pending"},
    }
    commit_path = tmp_path / ".story-system" / "commits" / "chapter_001.commit.json"
    _write_json(commit_path, commit_payload)
    append_projection_run(
        tmp_path,
        commit_payload,
        {"state": {"status": "pending"}},
        commit_path=commit_path,
    )
    monkeypatch.setattr(doctor_module, "_python_checks", lambda: [])

    report = doctor_module.build_doctor_report(tmp_path)

    matches = [item for item in report["checks"] if item["id"] == "projection_log.latest_run"]
    assert matches
    assert matches[0]["status"] == "error"
    assert report["ok"] is False


def test_doctor_recognizes_planning_and_writing_stages(tmp_path, monkeypatch):
    _make_init_ready(tmp_path)
    _write_json(
        tmp_path / ".story-system" / "MASTER_SETTING.json",
        {"meta": {"contract_type": "MASTER_SETTING"}},
    )
    monkeypatch.setattr(doctor_module, "_python_checks", lambda: [])

    planning = doctor_module.build_doctor_report(tmp_path, chapter=7)

    assert planning["phase"] == "plan_in_progress"
    assert planning["expected_profile"]["target_chapter"] == 7
    assert planning["ok"] is False
    assert {
        item["id"]
        for item in planning["checks"]
        if item["status"] == "error"
    } >= {"file.contract.volume", "file.contract.chapter", "file.contract.review"}

    _make_contracts(tmp_path, chapter=7)
    ready = doctor_module.build_doctor_report(tmp_path, chapter=7)
    assert ready["phase"] == "chapter_contract_ready"
    assert ready["ok"] is True

    (tmp_path / "正文" / "第0007章.md").write_text("写作中\n", encoding="utf-8")
    writing = doctor_module.build_doctor_report(tmp_path, chapter=7)
    assert writing["phase"] == "draft_in_progress"
    assert writing["ok"] is True


def test_doctor_deep_json_and_text_are_read_only_on_complex_windows_path(tmp_path, monkeypatch):
    project = tmp_path / "中文 项目 (甲) & 乙 #7"
    _make_init_ready(project)
    _make_contracts(project, chapter=7)
    webnovel_dir = project / ".webnovel"
    with sqlite3.connect(str(webnovel_dir / "index.db")) as conn:
        conn.execute("CREATE TABLE chapters (chapter INTEGER PRIMARY KEY)")
    with sqlite3.connect(str(webnovel_dir / "vectors.db")) as conn:
        conn.execute("CREATE TABLE vectors (id INTEGER PRIMARY KEY)")
    monkeypatch.setattr(doctor_module, "_python_checks", lambda: [])

    before = snapshot_protected_state(project)
    report = doctor_module.build_doctor_report(project, chapter=7, deep=True)
    json_text = doctor_module.format_doctor_report(report, "json")
    human_text = doctor_module.format_doctor_report(report, "text")
    after = snapshot_protected_state(project)

    assert before == after
    assert json.loads(json_text)["mode"] == "deep"
    assert "webnovel-doctor" in human_text
    assert any(item["id"] == "dashboard.frontend.dist" for item in report["checks"])


def test_doctor_sqlite_connections_use_read_only_uri(tmp_path, monkeypatch):
    database = tmp_path / "索引 #1 & (只读).db"
    with sqlite3.connect(str(database)) as conn:
        conn.execute("CREATE TABLE chapters (chapter INTEGER PRIMARY KEY)")

    real_connect = sqlite3.connect
    calls = []

    def recording_connect(database_arg, *args, **kwargs):
        calls.append((database_arg, dict(kwargs)))
        return real_connect(database_arg, *args, **kwargs)

    monkeypatch.setattr(doctor_module.sqlite3, "connect", recording_connect)

    ok, count, error = doctor_module._sqlite_table_count(database, "chapters")

    assert (ok, count, error) == (True, 0, "")
    assert calls
    assert calls[0][1].get("uri") is True
    assert str(calls[0][0]).endswith("?mode=ro")


def test_doctor_accepts_complete_local_embedding_model_without_api_key(tmp_path, monkeypatch):
    _make_init_ready(tmp_path)
    webnovel_home = tmp_path / "runtime-home"
    model_dir = webnovel_home / "models" / "Qwen3-Embedding-0.6B"
    model_dir.mkdir(parents=True)
    for name in ("modules.json", "tokenizer.json", "model.safetensors"):
        (model_dir / name).write_bytes(b"{}")
    monkeypatch.setenv("WEBNOVEL_HOME", str(webnovel_home))
    monkeypatch.delenv("EMBED_API_KEY", raising=False)
    monkeypatch.setattr(doctor_module, "_python_checks", lambda: [])

    report = doctor_module.build_doctor_report(tmp_path)
    by_id = {item["id"]: item for item in report["checks"]}

    assert by_id["rag.embed.backend"]["actual"] == "local"
    assert by_id["rag.embed.local_dependency"]["status"] == "ok"
    assert by_id["rag.embed.local_model"]["status"] == "ok"
    assert by_id["rag.rerank.backend"]["status"] == "ok"
    assert "rag.embed.api_key" not in by_id


def test_doctor_warns_for_missing_local_model_without_attempting_download(tmp_path, monkeypatch):
    _make_init_ready(tmp_path)
    webnovel_home = tmp_path / "empty-runtime-home"
    monkeypatch.setenv("WEBNOVEL_HOME", str(webnovel_home))
    monkeypatch.setattr(doctor_module, "_python_checks", lambda: [])

    report = doctor_module.build_doctor_report(tmp_path)
    matches = [item for item in report["checks"] if item["id"] == "rag.embed.local_model"]

    assert matches
    assert matches[0]["status"] == "warning"
    assert matches[0]["path"].endswith("Qwen3-Embedding-0.6B")
    assert "README" in matches[0]["repair"]
