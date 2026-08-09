#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import importlib
import json
import sys
from pathlib import Path

import pytest


def _ensure_scripts_on_path() -> None:
    scripts_dir = Path(__file__).resolve().parents[2]
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))


def _load_webnovel_module():
    _ensure_scripts_on_path()
    import data_modules.webnovel as webnovel_module

    return webnovel_module


def _make_cli_init_ready_project(project_root: Path) -> None:
    dirs = (
        ".webnovel/backups",
        ".webnovel/archive",
        ".webnovel/summaries",
        "设定集",
        "大纲",
        "正文",
        "审查报告",
    )
    for rel in dirs:
        (project_root / rel).mkdir(parents=True, exist_ok=True)

    (project_root / ".webnovel" / "state.json").write_text(
        json.dumps(
            {
                "project_info": {"title": "测试书", "genre": "玄幻"},
                "progress": {"current_chapter": 0},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    for rel in (
        "设定集/世界观.md",
        "设定集/力量体系.md",
        "设定集/主角卡.md",
        "设定集/反派设计.md",
        "大纲/总纲.md",
        ".env.example",
    ):
        path = project_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("placeholder\n", encoding="utf-8")


@pytest.mark.parametrize(
    "status",
    [
        "blocked",
        "recoverable",
        "failed",
        "awaiting_user",
        "paused",
        "targeted_fix_pending",
        "targeted_fix_blocked",
        "failed_validation",
        "failed_persistence",
        "stale",
    ],
)
def test_review_unified_cli_all_non_success_statuses_exit_one(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    status: str,
) -> None:
    module = _load_webnovel_module()
    from data_modules import review_workflow

    project_root = tmp_path / f"review-{status}"
    _make_cli_init_ready_project(project_root)
    monkeypatch.setattr(
        review_workflow,
        "resume_review",
        lambda root, *, run_id: {
            "schema_version": "webnovel-review-workflow/v1",
            "status": status,
            "run_id": run_id,
        },
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "webnovel",
            "--project-root",
            str(project_root),
            "review",
            "resume",
            "--run-id",
            "rv-ch0001-cli",
            "--format",
            "json",
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        module.main()

    assert int(exc_info.value.code or 0) == 1
    assert json.loads(capsys.readouterr().out)["status"] == status


def test_review_decision_cli_is_request_file_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_webnovel_module()
    from data_modules import review_workflow

    project_root = tmp_path / "review-request-file-only"
    _make_cli_init_ready_project(project_root)
    request_file = tmp_path / "decision.json"
    request_file.write_text("{}", encoding="utf-8")
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        review_workflow,
        "decide_review",
        lambda root, *, run_id, request_file: (
            calls.append((run_id, str(request_file)))
            or {
                "schema_version": "webnovel-review-workflow/v1",
                "status": "abandoned",
                "run_id": run_id,
            }
        ),
    )

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "webnovel",
            "--project-root",
            str(project_root),
            "review",
            "decide",
            "--run-id",
            "rv-ch0001-cli",
            "--request-file",
            str(request_file),
            "--format",
            "json",
        ],
    )
    with pytest.raises(SystemExit) as accepted:
        module.main()
    assert int(accepted.value.code or 0) == 0
    assert calls == [("rv-ch0001-cli", str(request_file))]
    capsys.readouterr()

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "webnovel",
            "--project-root",
            str(project_root),
            "review",
            "decide",
            "--run-id",
            "rv-ch0001-cli",
            "--request-id",
            "choice-00000000000000000000",
            "--choice",
            "report_only",
        ],
    )
    with pytest.raises(SystemExit) as rejected:
        module.main()
    assert int(rejected.value.code or 0) == 2


def test_init_does_not_resolve_existing_project_root(monkeypatch, tmp_path, capsys):
    module = _load_webnovel_module()
    from data_modules.tests.test_init_request import valid_init_payload, write_request

    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("WEBNOVEL_HOME", str(home))
    request_file = write_request(home, valid_init_payload(workspace))

    def _fail_resolve(_explicit_project_root=None):
        raise AssertionError("init 子命令不应触发 project_root 解析")

    monkeypatch.setenv("WEBNOVEL_PROJECT_ROOT", r"D:\invalid\root")
    monkeypatch.setattr(module, "_resolve_root", _fail_resolve)
    monkeypatch.setattr(
        sys,
        "argv",
        ["webnovel", "init", "--config-json", str(request_file), "--dry-run"],
    )

    with pytest.raises(SystemExit) as exc:
        module.main()

    assert int(exc.value.code or 0) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == "webnovel-init-preview/v1"
    assert payload["git_mode"] == "off"
    assert not (workspace / "星火长夜").exists()


def test_init_unified_cli_preview_apply_and_stale_token_exit_codes(monkeypatch, tmp_path, capsys):
    module = _load_webnovel_module()
    from data_modules.tests.test_init_request import valid_init_payload, write_request
    from data_modules.tests.test_init_workflow import _write_apply_authorization, tree_snapshot

    home = tmp_path / "home"
    workspace = tmp_path / "中文 工作区 (CLI) & B"
    workspace.mkdir()
    target = workspace / "星火长夜"
    monkeypatch.setenv("WEBNOVEL_HOME", str(home))
    request_file = write_request(home, valid_init_payload(workspace))

    def run_cli(argv: list[str]) -> tuple[int, dict]:
        monkeypatch.setattr(sys, "argv", ["webnovel", *argv])
        with pytest.raises(SystemExit) as exc:
            module.main()
        return int(exc.value.code or 0), json.loads(capsys.readouterr().out)

    before = tree_snapshot(workspace)
    code, preview = run_cli(
        ["init", "--config-json", str(request_file), "--dry-run"]
    )
    assert code == 0
    assert preview["status"] == "ready"
    assert preview["git_mode"] == "off"
    assert tree_snapshot(workspace) == before
    assert not target.exists()
    authorization = _write_apply_authorization(request_file, preview)

    code, missing_authorization = run_cli(
        [
            "init",
            "--config-json",
            str(request_file),
            "--apply",
            "--git-mode",
            "off",
            "--preview-token",
            preview["preview_token"],
        ]
    )
    assert code == 2
    assert missing_authorization["code"] == "invalid_request"
    assert not target.exists()

    code, invalid = run_cli(
        [
            "init",
            "--config-json",
            str(request_file),
            "--apply",
            "--preview-token",
            preview["preview_token"],
            "--authorization-json",
            str(authorization),
        ]
    )
    assert code == 2
    assert invalid["code"] == "invalid_request"
    assert not target.exists()

    code, result = run_cli(
        [
            "init",
            "--config-json",
            str(request_file),
            "--apply",
            "--git-mode",
            "off",
            "--preview-token",
            preview["preview_token"],
            "--authorization-json",
            str(authorization),
        ]
    )
    assert code == 0
    assert result["status"] == "success"
    assert result["git"]["mode"] == "off"
    after_apply = tree_snapshot(target)

    code, stale = run_cli(
        [
            "init",
            "--config-json",
            str(request_file),
            "--apply",
            "--git-mode",
            "off",
            "--preview-token",
            preview["preview_token"],
            "--authorization-json",
            str(authorization),
        ]
    )
    assert code == 1
    assert stale["code"] == "init_blocked"
    assert tree_snapshot(target) == after_apply


def test_init_unified_cli_invalid_and_blocked_preview_are_zero_write(monkeypatch, tmp_path, capsys):
    module = _load_webnovel_module()
    from data_modules.tests.test_init_request import valid_init_payload, write_request

    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "星火长夜"
    monkeypatch.setenv("WEBNOVEL_HOME", str(home))
    payload = valid_init_payload(workspace)
    payload["reference_candidate"] = {
        "status": "proposed",
        "candidate_id": "unconfirmed",
        "confidence": 0.99,
        "transformation_notes": "must not persist",
    }
    request_file = write_request(home, payload)

    def run_cli(argv: list[str]) -> tuple[int, dict]:
        monkeypatch.setattr(sys, "argv", ["webnovel", *argv])
        with pytest.raises(SystemExit) as exc:
            module.main()
        return int(exc.value.code or 0), json.loads(capsys.readouterr().out)

    code, blocked = run_cli(
        ["init", "--config-json", str(request_file), "--dry-run"]
    )
    assert code == 1
    assert blocked["status"] == "blocked"
    assert blocked["reference_candidate_status"] == "proposed"
    assert not target.exists()

    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    code, invalid = run_cli(
        ["init", "--config-json", str(outside), "--dry-run"]
    )
    assert code == 2
    assert invalid["code"] == "invalid_request"
    assert not target.exists()


def test_init_unified_cli_rejects_ambiguous_global_project_root(monkeypatch, tmp_path, capsys):
    module = _load_webnovel_module()
    from data_modules.tests.test_init_request import valid_init_payload, write_request

    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    other = tmp_path / "other-book"
    workspace.mkdir()
    other.mkdir()
    monkeypatch.setenv("WEBNOVEL_HOME", str(home))
    request_file = write_request(home, valid_init_payload(workspace))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "webnovel",
            "--project-root",
            str(other),
            "init",
            "--config-json",
            str(request_file),
            "--dry-run",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        module.main()

    assert int(exc.value.code or 0) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["code"] == "invalid_request"
    assert not (workspace / "星火长夜").exists()


def test_extract_context_forwards_with_resolved_project_root(monkeypatch, tmp_path):
    module = _load_webnovel_module()

    book_root = (tmp_path / "book").resolve()
    called = {}

    def _fake_resolve(explicit_project_root=None):
        return book_root

    def _fake_run_script(script_name, argv):
        called["script_name"] = script_name
        called["argv"] = list(argv)
        return 0

    monkeypatch.setattr(module, "_resolve_root", _fake_resolve)
    monkeypatch.setattr(module, "_run_script", _fake_run_script)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "webnovel",
            "--project-root",
            str(tmp_path),
            "extract-context",
            "--chapter",
            "12",
            "--format",
            "json",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        module.main()

    assert int(exc.value.code or 0) == 0
    assert called["script_name"] == "extract_chapter_context.py"
    assert called["argv"] == [
        "--project-root",
        str(book_root),
        "--chapter",
        "12",
        "--format",
        "json",
    ]


def test_backup_forwards_explicit_book_root(monkeypatch, tmp_path):
    module = _load_webnovel_module()

    workspace_root = (tmp_path / "workspace").resolve()
    book_root = (workspace_root / "book").resolve()
    (workspace_root / ".git").mkdir(parents=True, exist_ok=True)
    (book_root / ".git").mkdir(parents=True, exist_ok=True)
    (book_root / ".webnovel").mkdir(parents=True, exist_ok=True)
    (book_root / ".webnovel" / "state.json").write_text("{}", encoding="utf-8")
    called = {}

    def _fake_run_script(script_name, argv):
        called["script_name"] = script_name
        called["argv"] = list(argv)
        return 0

    monkeypatch.chdir(workspace_root)
    monkeypatch.setattr(module, "_run_script", _fake_run_script)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "webnovel",
            "--project-root",
            str(book_root),
            "backup",
            "--chapter",
            "2",
            "--chapter-title",
            "第二章",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        module.main()

    assert int(exc.value.code or 0) == 0
    assert called["script_name"] == "backup_manager.py"
    assert called["argv"] == [
        "--project-root",
        str(book_root),
        "--chapter",
        "2",
        "--chapter-title",
        "第二章",
    ]


def test_webnovel_story_system_forwards_with_resolved_project_root(monkeypatch, tmp_path):
    module = _load_webnovel_module()

    book_root = (tmp_path / "book").resolve()
    called = {}

    def _fake_resolve(explicit_project_root=None):
        return book_root

    def _fake_run_script(script_name, argv):
        called["script_name"] = script_name
        called["argv"] = list(argv)
        return 0

    monkeypatch.setattr(module, "_resolve_root", _fake_resolve)
    monkeypatch.setattr(module, "_run_script", _fake_run_script)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "webnovel",
            "--project-root",
            str(tmp_path),
            "story-system",
            "玄幻退婚流",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        module.main()

    assert int(exc.value.code or 0) == 0
    assert called["script_name"] == "story_system.py"
    assert called["argv"][:2] == ["--project-root", str(book_root)]


def test_webnovel_story_system_runtime_forwards(monkeypatch, tmp_path):
    module = _load_webnovel_module()

    project_root = (tmp_path / "book").resolve()
    called = {}

    def _fake_resolve(explicit_project_root=None):
        return project_root

    def _fake_run_script(script_name, argv):
        called["script_name"] = script_name
        called["argv"] = list(argv)
        return 0

    monkeypatch.setattr(module, "_resolve_root", _fake_resolve)
    monkeypatch.setattr(module, "_run_script", _fake_run_script)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "webnovel",
            "--project-root",
            str(project_root),
            "story-system",
            "玄幻退婚流",
            "--emit-runtime-contracts",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        module.main()

    assert int(exc.value.code or 0) == 0
    assert called["script_name"] == "story_system.py"
    assert "--emit-runtime-contracts" in called["argv"]


def test_webnovel_commit_forwards(monkeypatch, tmp_path):
    module = _load_webnovel_module()
    project_root = tmp_path / "book"
    (project_root / ".webnovel").mkdir(parents=True, exist_ok=True)
    (project_root / ".webnovel" / "state.json").write_text("{}", encoding="utf-8")
    called = {}

    def _fake_run_script(script_name, argv):
        called["script_name"] = script_name
        called["argv"] = list(argv)
        return 0

    monkeypatch.setattr(module, "_run_script", _fake_run_script)
    monkeypatch.setattr(sys, "argv", ["webnovel", "--project-root", str(project_root), "chapter-commit", "--chapter", "3"])

    with pytest.raises(SystemExit) as exc:
        module.main()

    assert int(exc.value.code or 0) == 0
    assert called["script_name"] == "chapter_commit.py"


def test_webnovel_story_events_forwards(monkeypatch, tmp_path):
    module = _load_webnovel_module()
    project_root = tmp_path / "book"
    (project_root / ".webnovel").mkdir(parents=True, exist_ok=True)
    (project_root / ".webnovel" / "state.json").write_text("{}", encoding="utf-8")
    called = {}

    def _fake_run_script(script_name, argv):
        called["script_name"] = script_name
        called["argv"] = list(argv)
        return 0

    monkeypatch.setattr(module, "_run_script", _fake_run_script)
    monkeypatch.setattr(
        sys,
        "argv",
        ["webnovel", "--project-root", str(project_root), "story-events", "--chapter", "3"],
    )

    with pytest.raises(SystemExit) as exc:
        module.main()

    assert int(exc.value.code or 0) == 0
    assert called["script_name"] == "story_events.py"


def test_preflight_succeeds_for_valid_project_root(monkeypatch, tmp_path, capsys):
    module = _load_webnovel_module()

    project_root = tmp_path / "book"
    (project_root / ".webnovel").mkdir(parents=True, exist_ok=True)
    (project_root / ".webnovel" / "state.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(sys, "argv", ["webnovel", "--project-root", str(project_root), "preflight"])

    with pytest.raises(SystemExit) as exc:
        module.main()

    captured = capsys.readouterr()
    assert int(exc.value.code or 0) == 0
    assert "OK project_root" in captured.out
    assert str(project_root.resolve()) in captured.out


def test_preflight_fails_when_required_scripts_are_missing(monkeypatch, tmp_path, capsys):
    module = _load_webnovel_module()

    project_root = tmp_path / "book"
    (project_root / ".webnovel").mkdir(parents=True, exist_ok=True)
    (project_root / ".webnovel" / "state.json").write_text("{}", encoding="utf-8")

    fake_scripts_dir = tmp_path / "fake-scripts"
    fake_scripts_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(module, "_scripts_dir", lambda: fake_scripts_dir)
    monkeypatch.setattr(sys, "argv", ["webnovel", "--project-root", str(project_root), "preflight", "--format", "json"])

    with pytest.raises(SystemExit) as exc:
        module.main()

    captured = capsys.readouterr()
    assert int(exc.value.code or 0) == 1
    assert '"ok": false' in captured.out
    assert '"name": "entry_script"' in captured.out


def test_preflight_includes_story_runtime_health(monkeypatch, tmp_path, capsys):
    module = _load_webnovel_module()

    project_root = tmp_path / "book"
    (project_root / ".webnovel").mkdir(parents=True, exist_ok=True)
    (project_root / ".webnovel" / "state.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(
        sys,
        "argv",
        ["webnovel", "--project-root", str(project_root), "preflight", "--format", "json"],
    )

    with pytest.raises(SystemExit):
        module.main()

    captured = capsys.readouterr()
    assert '"story_runtime"' in captured.out
    assert '"mainline_ready"' in captured.out


def test_project_status_cli_outputs_json_without_reusing_status(monkeypatch, tmp_path, capsys):
    module = _load_webnovel_module()
    project_root = tmp_path / "book"
    _make_cli_init_ready_project(project_root)

    monkeypatch.setattr(
        sys,
        "argv",
        ["webnovel", "--project-root", str(project_root), "project-status", "--format", "json"],
    )

    with pytest.raises(SystemExit) as exc:
        module.main()

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert int(exc.value.code or 0) == 0
    assert report["schema_version"] == "webnovel-project-status/v1"
    assert report["project"] == "测试书"
    assert report["phase"] == "init_ready"


def test_user_report_cli_outputs_json(monkeypatch, tmp_path, capsys):
    module = _load_webnovel_module()
    project_root = tmp_path / "book"
    _make_cli_init_ready_project(project_root)

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "webnovel",
            "--project-root",
            str(project_root),
            "user-report",
            "--stage",
            "init",
            "--format",
            "json",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        module.main()

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert int(exc.value.code or 0) == 0
    assert report["schema_version"] == "webnovel-user-report/v1"
    assert report["stage"] == "init"
    assert report["overall_status"] == "completed"


def test_run_ledger_cli_records_and_reports_resume(monkeypatch, tmp_path, capsys):
    module = _load_webnovel_module()
    project_root = tmp_path / "book"
    _make_cli_init_ready_project(project_root)
    chapter_file = project_root / "正文" / "第0001章.md"
    chapter_file.write_text("正文\n", encoding="utf-8")

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "webnovel",
            "--project-root",
            str(project_root),
            "run-ledger",
            "record-write-step",
            "--chapter",
            "1",
            "--step",
            "draft",
            "--status",
            "completed",
            "--outputs-json",
            json.dumps({"chapter_file": str(chapter_file)}, ensure_ascii=False),
            "--format",
            "json",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        module.main()

    entry = json.loads(capsys.readouterr().out)
    assert int(exc.value.code or 0) == 0
    assert entry["step"] == "draft"
    assert entry["outputs"]["chapter_file"]["exists"] is True

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "webnovel",
            "--project-root",
            str(project_root),
            "run-ledger",
            "write-resume",
            "--chapter",
            "1",
            "--format",
            "json",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        module.main()

    resume = json.loads(capsys.readouterr().out)
    assert int(exc.value.code or 0) == 0
    assert resume["schema_version"] == "webnovel-run-ledger/v1"
    assert resume["steps"][0]["step"] == "draft"
    assert resume["steps"][0]["action"] == "skip"


def test_run_log_cli_redacts_sensitive_payload(monkeypatch, tmp_path, capsys):
    module = _load_webnovel_module()
    project_root = tmp_path / "book"
    _make_cli_init_ready_project(project_root)

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "webnovel",
            "--project-root",
            str(project_root),
            "run-log",
            "--event",
            "failure",
            "--payload-json",
            json.dumps({"api_key": "secret-value", "message": "ok"}, ensure_ascii=False),
            "--format",
            "json",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        module.main()

    result = json.loads(capsys.readouterr().out)
    assert int(exc.value.code or 0) == 0
    log_text = Path(result["path"]).read_text(encoding="utf-8")
    assert "secret-value" not in log_text
    assert "<redacted>" in log_text


def test_doctor_cli_reports_missing_init_file(monkeypatch, tmp_path, capsys):
    module = _load_webnovel_module()
    project_root = tmp_path / "book"
    _make_cli_init_ready_project(project_root)
    (project_root / "大纲" / "总纲.md").unlink()

    monkeypatch.setattr(
        sys,
        "argv",
        ["webnovel", "--project-root", str(project_root), "doctor", "--format", "json"],
    )

    with pytest.raises(SystemExit) as exc:
        module.main()

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert int(exc.value.code or 0) == 1
    assert report["schema_version"] == "webnovel-doctor/v1"
    assert report["ok"] is False
    assert any(item["id"] == "file.required.大纲/总纲.md" for item in report["checks"])


def test_status_command_still_forwards_to_status_reporter(monkeypatch, tmp_path):
    module = _load_webnovel_module()
    project_root = tmp_path / "book"
    _make_cli_init_ready_project(project_root)
    called = {}

    def _fake_run_script(script_name, argv):
        called["script_name"] = script_name
        called["argv"] = list(argv)
        return 0

    monkeypatch.setattr(module, "_run_script", _fake_run_script)
    monkeypatch.setattr(sys, "argv", ["webnovel", "--project-root", str(project_root), "status", "--focus", "all"])

    with pytest.raises(SystemExit) as exc:
        module.main()

    assert int(exc.value.code or 0) == 0
    assert called["script_name"] == "status_reporter.py"


def test_write_gate_cli_runs_prewrite(monkeypatch, tmp_path, capsys):
    module = _load_webnovel_module()
    project_root = tmp_path / "book"
    _make_cli_init_ready_project(project_root)
    for path, payload in (
        (project_root / ".story-system" / "MASTER_SETTING.json", {"meta": {"contract_type": "MASTER_SETTING"}}),
        (project_root / ".story-system" / "volumes" / "volume_001.json", {"meta": {"volume": 1}}),
        (project_root / ".story-system" / "chapters" / "chapter_001.json", {"chapter_directive": {"must_cover_nodes": []}}),
        (project_root / ".story-system" / "reviews" / "chapter_001.review.json", {"blocking_rules": []}),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "webnovel",
            "--project-root",
            str(project_root),
            "write-gate",
            "--chapter",
            "1",
            "--stage",
            "prewrite",
            "--format",
            "json",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        module.main()

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert int(exc.value.code or 0) == 0
    assert report["schema_version"] == "webnovel-write-gate/v1"
    assert report["stage"] == "prewrite"
    assert report["ok"] is True


def test_projections_retry_cli_runs(monkeypatch, tmp_path, capsys):
    module = _load_webnovel_module()
    project_root = tmp_path / "book"
    _make_cli_init_ready_project(project_root)
    commit_path = project_root / ".story-system" / "commits" / "chapter_001.commit.json"
    commit_path.parent.mkdir(parents=True, exist_ok=True)
    commit_path.write_text(
        json.dumps(
            {
                "meta": {"chapter": 1, "status": "rejected"},
                "review_result": {"blocking_count": 1},
                "fulfillment_result": {
                    "planned_nodes": [],
                    "covered_nodes": [],
                    "missed_nodes": [],
                    "extra_nodes": [],
                },
                "disambiguation_result": {"pending": []},
                "extraction_result": {"accepted_events": [], "state_deltas": [], "entity_deltas": []},
                "projection_status": {
                    "state": "pending",
                    "index": "pending",
                    "summary": "pending",
                    "memory": "pending",
                    "vector": "pending",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "webnovel",
            "--project-root",
            str(project_root),
            "projections",
            "retry",
            "--chapter",
            "1",
            "--format",
            "json",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        module.main()

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert int(exc.value.code or 0) == 0
    assert report["schema_version"] == "webnovel-projections/v1"
    assert report["projection_status"]["state"] == "done"


def test_where_reports_empty_workspace_without_traceback(monkeypatch, tmp_path, capsys):
    module = _load_webnovel_module()
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / ".git").mkdir(parents=True, exist_ok=True)

    monkeypatch.chdir(workspace)
    monkeypatch.delenv("WEBNOVEL_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.setenv("WEBNOVEL_CLAUDE_HOME", str(tmp_path / "empty-claude-home"))
    monkeypatch.setattr(sys, "argv", ["webnovel", "where"])

    with pytest.raises(SystemExit) as exc:
        module.main()

    captured = capsys.readouterr()
    assert int(exc.value.code or 0) == 1
    assert "还没有激活的书项目" in captured.err
    assert "Traceback" not in captured.err


def test_where_json_reports_stable_resolution_fields(monkeypatch, tmp_path, capsys):
    module = _load_webnovel_module()
    project_root = tmp_path / "中文 空格 (A&B)"
    (project_root / ".webnovel").mkdir(parents=True)
    (project_root / ".webnovel" / "state.json").write_text("{}", encoding="utf-8")

    monkeypatch.chdir(project_root)
    monkeypatch.delenv("WEBNOVEL_PROJECT_ROOT", raising=False)
    monkeypatch.setattr(sys, "argv", ["webnovel", "where", "--format", "json"])

    with pytest.raises(SystemExit) as exc:
        module.main()

    report = json.loads(capsys.readouterr().out)
    assert int(exc.value.code or 0) == 0
    assert report == {
        "schema_version": 1,
        "project_root": str(project_root.resolve()),
        "resolved_from": "cwd",
        "compatibility_mode": "native",
    }


def test_where_invalid_explicit_root_is_input_error(monkeypatch, tmp_path, capsys):
    module = _load_webnovel_module()
    invalid_root = tmp_path / "workspace-not-book"
    invalid_root.mkdir()
    monkeypatch.setattr(
        sys,
        "argv",
        ["webnovel", "--project-root", str(invalid_root), "where"],
    )

    with pytest.raises(SystemExit) as exc:
        module.main()

    captured = capsys.readouterr()
    assert int(exc.value.code or 0) == 2
    assert "有效书项目根目录" in captured.err
    assert "Traceback" not in captured.err


def test_where_empty_explicit_root_is_input_error_without_cwd_fallback(
    monkeypatch, tmp_path, capsys
):
    module = _load_webnovel_module()
    (tmp_path / ".webnovel").mkdir()
    (tmp_path / ".webnovel" / "state.json").write_text("{}", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["webnovel", "--project-root", "", "where"])

    with pytest.raises(SystemExit) as exc:
        module.main()

    captured = capsys.readouterr()
    assert int(exc.value.code or 0) == 2
    assert "有效书项目根目录" in captured.err
    assert "Explicit project root is empty" in captured.err
    assert captured.out == ""
    assert "Traceback" not in captured.err


def test_use_binds_confirmed_current_workspace_once(monkeypatch, tmp_path, capsys):
    module = _load_webnovel_module()
    import project_locator as locator

    monkeypatch.setattr(locator, "_find_plugin_root", lambda _start: None)
    workspace = tmp_path / "中文 Workspace (A&B)"
    project_root = workspace / "书😀"
    (project_root / ".webnovel").mkdir(parents=True)
    (project_root / ".webnovel" / "state.json").write_text("{}", encoding="utf-8")
    monkeypatch.chdir(workspace)
    real_bind = module.bind_current_project
    calls = []

    def counting_bind(project_root, *, workspace_root):
        calls.append((project_root, workspace_root))
        return real_bind(project_root, workspace_root=workspace_root)

    monkeypatch.setattr(module, "bind_current_project", counting_bind)
    monkeypatch.setattr(sys, "argv", ["webnovel", "use", str(project_root)])

    with pytest.raises(SystemExit) as exc:
        module.main()

    assert int(exc.value.code or 0) == 0
    assert len(calls) == 1
    assert calls[0][1] == workspace.resolve()
    assert (workspace / ".codex" / ".webnovel-current-project").is_file()
    assert "workspace pointer" in capsys.readouterr().out


def test_use_rejects_unconfirmed_workspace_without_writing(monkeypatch, tmp_path, capsys):
    module = _load_webnovel_module()
    import project_locator as locator

    monkeypatch.setattr(locator, "_find_plugin_root", lambda _start: None)
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    project_root = tmp_path / "books" / "book"
    (project_root / ".webnovel").mkdir(parents=True)
    (project_root / ".webnovel" / "state.json").write_text("{}", encoding="utf-8")
    monkeypatch.chdir(unrelated)
    monkeypatch.setattr(sys, "argv", ["webnovel", "use", str(project_root)])

    with pytest.raises(SystemExit) as exc:
        module.main()

    captured = capsys.readouterr()
    assert int(exc.value.code or 0) == 2
    assert "--workspace-root" in captured.err
    assert not (unrelated / ".codex").exists()


def test_preflight_reports_empty_workspace_without_traceback(monkeypatch, tmp_path, capsys):
    module = _load_webnovel_module()
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / ".git").mkdir(parents=True, exist_ok=True)

    monkeypatch.chdir(workspace)
    monkeypatch.delenv("WEBNOVEL_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.setenv("WEBNOVEL_CLAUDE_HOME", str(tmp_path / "empty-claude-home"))
    monkeypatch.setattr(sys, "argv", ["webnovel", "preflight", "--format", "json"])

    with pytest.raises(SystemExit) as exc:
        module.main()

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert int(exc.value.code or 0) == 1
    assert report["ok"] is False
    assert "还没有激活的书项目" in report["project_root_error"]
    assert "Traceback" not in captured.err


def test_quality_trend_report_writes_to_explicit_book_root(tmp_path, monkeypatch):
    _ensure_scripts_on_path()
    import quality_trend_report as quality_trend_report_module

    workspace_root = (tmp_path / "workspace").resolve()
    book_root = (workspace_root / "凡人资本论").resolve()

    (book_root / ".webnovel").mkdir(parents=True, exist_ok=True)
    (book_root / ".webnovel" / "state.json").write_text("{}", encoding="utf-8")

    output_path = workspace_root / "report.md"

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "quality_trend_report",
            "--project-root",
            str(book_root),
            "--limit",
            "1",
            "--output",
            str(output_path),
        ],
    )

    quality_trend_report_module.main()

    assert output_path.is_file()
    assert (book_root / ".webnovel" / "index.db").is_file()
    assert not (workspace_root / ".webnovel" / "index.db").exists()






def test_review_pipeline_builds_artifacts(tmp_path):
    _ensure_scripts_on_path()
    import review_pipeline as review_pipeline_module

    project_root = (tmp_path / "book").resolve()
    (project_root / ".webnovel").mkdir(parents=True, exist_ok=True)
    (project_root / ".webnovel" / "state.json").write_text("{}", encoding="utf-8")

    review_results_path = tmp_path / "review_results.json"
    raw_review = {
        "chapter": 20,
        "issues": [
            {
                "severity": "critical",
                "category": "timeline",
                "location": "第2段",
                "description": "时间线回跳",
                "evidence": "上章深夜，本章突然中午",
                "fix_hint": "补时间过渡",
                "blocking": True,
            },
            {
                "severity": "medium",
                "category": "logic",
                "location": "第5段",
                "description": "因果缺口",
                "evidence": "没有前提却直接得到结论",
                "fix_hint": "补一处既有事实依据",
                "blocking": False,
            },
        ],
        "issues_count": 2,
        "blocking_count": 1,
        "has_blocking": True,
        "dimension_results": [
            {"dimension": "setting", "conclusion": "pass"},
            {"dimension": "timeline", "conclusion": "发现1个问题"},
            {"dimension": "continuity", "conclusion": "pass"},
            {"dimension": "character", "conclusion": "pass"},
            {"dimension": "logic", "conclusion": "发现1个问题"},
        ],
        "summary": "1个阻断，1个中等",
    }
    review_results_path.write_text(
        json.dumps(raw_review, ensure_ascii=False),
        encoding="utf-8",
    )

    payload = review_pipeline_module.build_review_artifacts(
        project_root=project_root,
        chapter=20,
        review_results_path=review_results_path,
        report_file="审查报告/第20章.md",
    )

    assert payload["review_result"]["blocking_count"] == 1
    assert payload["review_result"]["has_blocking"] is True
    assert payload["review_result"]["issues_count"] == 2
    assert payload["metrics"]["start_chapter"] == 20
    assert payload["metrics"]["end_chapter"] == 20
    assert payload["metrics"]["issues_count"] == 2
    assert payload["metrics"]["blocking_count"] == 1
    assert payload["metrics"]["severity_counts"]["critical"] == 1
    assert payload["metrics"]["severity_counts"]["medium"] == 1
    assert payload["metrics"]["critical_issues"] == ["时间线回跳"]
    assert payload["metrics"]["overall_score"] < 100
    assert payload["metrics"]["report_file"] == "审查报告/第20章.md"

    # Artifact construction is pure; production persistence is ledger-gated.
    assert json.loads(review_results_path.read_text(encoding="utf-8")) == raw_review


def test_review_pipeline_forwards_with_resolved_project_root(monkeypatch, tmp_path):
    module = _load_webnovel_module()

    book_root = (tmp_path / "book").resolve()
    called = {}

    def _fake_resolve(explicit_project_root=None):
        return book_root

    def _fake_run_script(script_name, argv):
        called["script_name"] = script_name
        called["argv"] = list(argv)
        return 0

    monkeypatch.setattr(module, "_resolve_root", _fake_resolve)
    monkeypatch.setattr(module, "_run_script", _fake_run_script)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "webnovel",
            "--project-root",
            str(tmp_path),
            "review-pipeline",
            "--run-id",
            "rv-ch0018-fixture",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        module.main()

    assert int(exc.value.code or 0) == 0
    assert called["script_name"] == "review_pipeline.py"
    assert called["argv"] == [
        "--project-root",
        str(book_root),
        "--run-id",
        "rv-ch0018-fixture",
    ]


def test_project_memory_forwards_with_resolved_project_root(monkeypatch, tmp_path):
    module = _load_webnovel_module()

    book_root = (tmp_path / "book").resolve()
    called = {}

    def _fake_resolve(explicit_project_root=None):
        return book_root

    def _fake_run_script(script_name, argv):
        called["script_name"] = script_name
        called["argv"] = list(argv)
        return 0

    monkeypatch.setattr(module, "_resolve_root", _fake_resolve)
    monkeypatch.setattr(module, "_run_script", _fake_run_script)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "webnovel",
            "--project-root",
            str(tmp_path),
            "project-memory",
            "add-pattern",
            "--pattern-type",
            "format",
            "--description",
            '内心独白使用双引号""',
        ],
    )

    with pytest.raises(SystemExit) as exc:
        module.main()

    assert int(exc.value.code or 0) == 0
    assert called["script_name"] == "project_memory.py"
    assert called["argv"] == [
        "--project-root",
        str(book_root),
        "add-pattern",
        "--pattern-type",
        "format",
        "--description",
        '内心独白使用双引号""',
    ]


@pytest.mark.parametrize(
    ("tool", "module_name", "tail"),
    [
        (
            "plan-request",
            "plan_request",
            ["--volume", "1", "--start-chapter", "1", "--end-chapter", "3", "--parent-model", "parent", "--save"],
        ),
        ("plan-validate", "plan_validator", ["--manifest", "plan-manifest.json"]),
        ("plan-transaction", "plan_transaction", ["status", "--run-id", "plan-v1-test"]),
        (
            "plan-transaction",
            "plan_transaction",
            [
                "accept-batch",
                "--request-file",
                "C:/trusted/plan-request.json",
                "--fragment-file",
                "C:/trusted/batch-000001-000010.json",
            ],
        ),
        ("write-transaction", "write_transaction", ["status", "--run-id", "write-ch0001-test"]),
    ],
)
def test_plan_and_write_modules_forward_through_unified_cli(
    monkeypatch, tmp_path, tool, module_name, tail
):
    module = _load_webnovel_module()
    book_root = (tmp_path / "book").resolve()
    called = {}

    monkeypatch.setattr(module, "_resolve_root", lambda explicit_project_root=None: book_root)

    def _fake_run_data_module(name, argv):
        called["module"] = name
        called["argv"] = list(argv)
        return 0

    monkeypatch.setattr(module, "_run_data_module", _fake_run_data_module)
    monkeypatch.setattr(
        sys,
        "argv",
        ["webnovel", "--project-root", str(tmp_path), tool, *tail],
    )

    with pytest.raises(SystemExit) as exc:
        module.main()

    assert int(exc.value.code or 0) == 0
    assert called["module"] == module_name
    assert called["argv"] == ["--project-root", str(book_root), *tail]


def test_project_memory_add_pattern_escapes_quotes(tmp_path):
    _ensure_scripts_on_path()
    import project_memory as project_memory_module

    project_root = (tmp_path / "book").resolve()
    (project_root / ".webnovel").mkdir(parents=True, exist_ok=True)
    (project_root / ".webnovel" / "state.json").write_text(
        json.dumps({"progress": {"current_chapter": 3}}, ensure_ascii=False),
        encoding="utf-8",
    )

    description = "正文格式规范：内心独白使用双引号\"\"，系统界面保留方括号[]"
    result = project_memory_module.add_pattern(
        project_root,
        pattern_type="format",
        description=description,
        category="写作规范",
        importance="high",
    )

    memory_path = project_root / ".webnovel" / "project_memory.json"
    raw_text = memory_path.read_text(encoding="utf-8")
    payload = json.loads(raw_text)

    assert result["status"] == "success"
    assert '\\"\\"' in raw_text
    assert payload["patterns"][0]["description"] == description
    assert payload["patterns"][0]["source_chapter"] == 3


def test_review_pipeline_main_only_resumes_accepted_run(monkeypatch, tmp_path, capsys):
    _ensure_scripts_on_path()
    import review_pipeline as review_pipeline_module

    project_root = (tmp_path / "book").resolve()
    (project_root / ".webnovel").mkdir(parents=True, exist_ok=True)
    (project_root / ".webnovel" / "state.json").write_text("{}", encoding="utf-8")

    import data_modules.review_workflow as workflow_module

    called = {}

    def fake_persist(root, *, run_id):
        called.update({"root": Path(root), "run_id": run_id})
        return {"schema_version": "webnovel-review-workflow/v1", "status": "persisted", "run_id": run_id}

    monkeypatch.setattr(workflow_module, "persist_review_run", fake_persist)

    old_argv = sys.argv
    sys.argv = [
        "review_pipeline",
        "--project-root",
        str(project_root),
        "--run-id",
        "rv-ch0009-fixture",
    ]
    try:
        review_pipeline_module.main()
    finally:
        sys.argv = old_argv

    assert called == {"root": project_root, "run_id": "rv-ch0009-fixture"}
    assert json.loads(capsys.readouterr().out)["status"] == "persisted"


def test_webnovel_skill_flow_runs_story_contract_context_and_review_pipeline_with_stubbed_vector_model(
    monkeypatch, tmp_path, capsys
):
    _ensure_scripts_on_path()
    module = _load_webnovel_module()
    import data_modules.rag_adapter as rag_module
    from data_modules.config import DataModulesConfig

    project_root = (tmp_path / "book").resolve()
    cfg = DataModulesConfig.from_project_root(project_root)
    cfg.ensure_dirs()

    cfg.state_file.write_text(
        json.dumps(
            {
                "project": {"genre": "xuanhuan"},
                "progress": {
                    "current_chapter": 3,
                    "total_words": 9000,
                    "volumes_planned": [{"volume": 1, "chapters_range": "1-20"}],
                },
                "protagonist_state": {
                    "name": "萧炎",
                    "location": {"current": "天云宗外院"},
                    "power": {"realm": "斗者", "layer": 9},
                },
                "chapter_meta": {},
                "disambiguation_warnings": [],
                "disambiguation_pending": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    outline_dir = project_root / "大纲"
    outline_dir.mkdir(parents=True, exist_ok=True)
    (outline_dir / "第1卷-详细大纲.md").write_text(
        "\n".join(
            [
                "### 第3章：试炼冲突",
                "本章将聚焦萧炎与药老关系冲突，并回收旧线索真相。",
                "CBN：萧炎进入试炼场",
                "CPNs：",
                "- 药老提醒规则异常",
                "- 萧炎发现师徒分歧",
                "CEN：萧炎决定暂缓冲突",
                "必须覆盖节点：发现规则异常",
                "本章禁区：不可提前摊牌",
            ]
        ),
        encoding="utf-8",
    )

    refs_dir = project_root / ".claude" / "references"
    refs_dir.mkdir(parents=True, exist_ok=True)
    (refs_dir / "genre-profiles.md").write_text("## xuanhuan\n- 升级线清晰", encoding="utf-8")
    (refs_dir / "reading-power-taxonomy.md").write_text("## xuanhuan\n- 冲突钩优先", encoding="utf-8")

    calls = {"embed": 0, "embed_batch": 0, "rerank": 0}

    class _StubVectorClient:
        async def embed(self, texts):
            calls["embed"] += 1
            return [[1.0, 0.0] for _ in texts]

        async def embed_batch(self, texts, skip_failures=True):
            calls["embed_batch"] += 1
            return [[1.0, 0.0] for _ in texts]

        async def rerank(self, query, documents, top_n=None):
            calls["rerank"] += 1
            limit = top_n or len(documents)
            return [
                {"index": i, "relevance_score": 1.0 / (i + 1)}
                for i in range(min(limit, len(documents)))
            ]

    monkeypatch.setenv("EMBED_API_KEY", "fake-embed-key")
    monkeypatch.setattr(rag_module, "get_client", lambda config: _StubVectorClient())

    adapter = rag_module.RAGAdapter(cfg)
    asyncio.run(
        adapter.store_chunks(
            [
                {
                    "chapter": 2,
                    "scene_index": 1,
                    "content": "萧炎与药老关系紧张，线索逐步浮现，冲突升级。",
                }
            ]
        )
    )

    script_to_module = {
        "story_system.py": "story_system",
        "extract_chapter_context.py": "extract_chapter_context",
        "review_pipeline.py": "review_pipeline",
    }

    def _run_script_inproc(script_name, argv):
        module_name = script_to_module.get(script_name)
        if not module_name:
            raise AssertionError(f"unexpected script call: {script_name}")
        script_module = importlib.import_module(module_name)
        old_argv = sys.argv
        try:
            sys.argv = [module_name, *argv]
            script_module.main()
            return 0
        except SystemExit as exc:
            return int(exc.code or 0)
        finally:
            sys.argv = old_argv

    monkeypatch.setattr(module, "_run_script", _run_script_inproc)

    def _run_webnovel(argv):
        monkeypatch.setattr(sys, "argv", ["webnovel", *argv])
        with pytest.raises(SystemExit) as exc:
            module.main()
        return int(exc.value.code or 0)

    # The legacy unbound pipeline is rejected; production Review requires a
    # prepared run plus explicit Codex runtime evidence.
    assert (
        _run_webnovel(
            [
                "--project-root",
                str(project_root),
                "story-system",
                "玄幻退婚流",
                "--chapter",
                "3",
                "--persist",
                "--emit-runtime-contracts",
                "--format",
                "json",
            ]
        )
        == 0
    )
    capsys.readouterr()

    story_root = project_root / ".story-system"
    assert (story_root / "MASTER_SETTING.json").is_file()
    assert (story_root / "volumes" / "volume_001.json").is_file()
    assert (story_root / "reviews" / "chapter_003.review.json").is_file()

    assert (
        _run_webnovel(
            [
                "--project-root",
                str(project_root),
                "extract-context",
                "--chapter",
                "3",
                "--format",
                "json",
            ]
        )
        == 0
    )
    context_payload = json.loads(capsys.readouterr().out)
    assert (
        context_payload["story_contract"]["review_contract"]["meta"]["contract_type"]
        == "REVIEW_CONTRACT"
    )
    assert context_payload["prewrite_validation"]["blocking"] is False
    assert context_payload["rag_assist"]["invoked"] is True
    assert context_payload["rag_assist"]["hits"]
    assert calls["embed_batch"] >= 1
    assert calls["embed"] >= 1
    assert calls["rerank"] >= 1

    review_results_path = project_root / ".webnovel" / "tmp" / "review_results.json"
    review_results_path.parent.mkdir(parents=True, exist_ok=True)
    review_results_path.write_text(
        json.dumps(
            {
                "issues": [
                    {
                        "severity": "medium",
                        "category": "continuity",
                        "location": "第3段",
                        "description": "衔接略弱",
                        "evidence": "上章钩子未明确承接",
                        "fix_hint": "补衔接句",
                    }
                ],
                "summary": "1个中优问题",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    metrics_out = project_root / ".webnovel" / "tmp" / "review_metrics.json"
    assert (
        _run_webnovel(
            [
                "--project-root",
                str(project_root),
                "review-pipeline",
                "--chapter",
                "3",
                "--review-results",
                str(review_results_path),
                "--metrics-out",
                str(metrics_out),
                "--report-file",
                "审查报告/第3章.md",
            ]
        )
        == 2
    )
    assert not metrics_out.is_file()


def test_dashboard_status_cli_returns_stable_json(monkeypatch, tmp_path, capsys):
    module = _load_webnovel_module()
    from data_modules import dashboard_lifecycle

    project_root = tmp_path / "dashboard book"
    _make_cli_init_ready_project(project_root)
    expected_root = project_root.resolve()
    monkeypatch.setattr(
        dashboard_lifecycle,
        "dashboard_status",
        lambda root: {
            "schema_version": dashboard_lifecycle.RESULT_SCHEMA,
            "project_root": str(Path(root).resolve()),
            "runtime_dir": "isolated-runtime",
            "status": "not_running",
            "ok": True,
            "errors": [],
        },
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "webnovel",
            "dashboard",
            "status",
            "--format",
            "json",
            "--project-root",
            str(project_root),
        ],
    )

    with pytest.raises(SystemExit) as exc:
        module.main()

    payload = json.loads(capsys.readouterr().out)
    assert int(exc.value.code or 0) == 0
    assert payload["schema_version"] == dashboard_lifecycle.RESULT_SCHEMA
    assert payload["project_root"] == str(expected_root)
    assert payload["status"] == "not_running"


def test_dashboard_start_cli_forwards_safe_dynamic_port(monkeypatch, tmp_path, capsys):
    module = _load_webnovel_module()
    from data_modules import dashboard_lifecycle

    project_root = tmp_path / "dashboard book"
    _make_cli_init_ready_project(project_root)
    called = {}

    def fake_start(root, *, host, port):
        called.update({"root": Path(root), "host": host, "port": port})
        return {
            "schema_version": dashboard_lifecycle.RESULT_SCHEMA,
            "project_root": str(Path(root).resolve()),
            "runtime_dir": "isolated-runtime",
            "status": "running",
            "ok": True,
            "errors": [],
            "pid": 123,
            "port": 45678,
            "url": "http://127.0.0.1:45678",
        }

    monkeypatch.setattr(dashboard_lifecycle, "dashboard_start", fake_start)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "webnovel",
            "--project-root",
            str(project_root),
            "dashboard",
            "start",
            "--host",
            "localhost",
            "--port",
            "0",
            "--no-browser",
            "--format",
            "json",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        module.main()

    payload = json.loads(capsys.readouterr().out)
    assert int(exc.value.code or 0) == 0
    assert called == {"root": project_root.resolve(), "host": "localhost", "port": 0}
    assert payload["status"] == "running"


def test_knowledge_cli_missing_database_and_table_return_schema_error_without_creation(
    monkeypatch, tmp_path, capsys
):
    module = _load_webnovel_module()
    project_root = tmp_path / "query book"
    _make_cli_init_ready_project(project_root)
    db_path = project_root / ".webnovel" / "index.db"

    def run_query():
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "webnovel",
                "--project-root",
                str(project_root),
                "knowledge",
                "query-entity-state",
                "--entity",
                "韩立",
                "--at-chapter",
                "1",
            ],
        )
        with pytest.raises(SystemExit) as exc:
            module.main()
        return int(exc.value.code or 0), json.loads(capsys.readouterr().out)

    code, missing_db = run_query()
    assert code == 1
    assert missing_db["schema_version"] == "webnovel-query-result/v1"
    assert missing_db["query_type"] == "entity_state"
    assert missing_db["error"]["code"] == "READ_MODEL_UNAVAILABLE"
    assert not db_path.exists()

    import sqlite3

    with sqlite3.connect(str(db_path)) as connection:
        connection.execute("CREATE TABLE entities (id TEXT PRIMARY KEY, canonical_name TEXT)")
    code, missing_table = run_query()
    assert code == 1
    assert missing_table["error"]["code"] == "READ_MODEL_UNAVAILABLE"
    assert db_path.is_file()


def test_knowledge_cli_ambiguous_entity_returns_candidates_in_query_schema(
    monkeypatch, tmp_path, capsys
):
    import sqlite3

    module = _load_webnovel_module()
    project_root = tmp_path / "query book"
    _make_cli_init_ready_project(project_root)
    db_path = project_root / ".webnovel" / "index.db"
    with sqlite3.connect(str(db_path)) as connection:
        connection.execute("CREATE TABLE entities (id TEXT PRIMARY KEY, canonical_name TEXT)")
        connection.execute(
            "CREATE TABLE state_changes (id INTEGER PRIMARY KEY, entity_id TEXT, field TEXT, new_value TEXT, chapter INTEGER)"
        )
        connection.execute(
            "CREATE TABLE relationship_events (id INTEGER PRIMARY KEY, from_entity TEXT, to_entity TEXT, type TEXT, description TEXT, chapter INTEGER)"
        )
        connection.executemany(
            "INSERT INTO entities (id, canonical_name) VALUES (?, ?)",
            [("hanli-a", "韩立"), ("hanli-b", "韩立")],
        )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "webnovel",
            "--project-root",
            str(project_root),
            "knowledge",
            "query-entity-state",
            "--entity",
            "韩立",
            "--at-chapter",
            "1",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        module.main()

    payload = json.loads(capsys.readouterr().out)
    assert int(exc.value.code or 0) == 1
    assert payload["schema_version"] == "webnovel-query-result/v1"
    assert payload["query_type"] == "entity_state"
    assert payload["error"]["code"] == "AMBIGUOUS_ENTITY"
    assert {item["entity_id"] for item in payload["error"]["details"]["candidates"]} == {
        "hanli-a",
        "hanli-b",
    }
