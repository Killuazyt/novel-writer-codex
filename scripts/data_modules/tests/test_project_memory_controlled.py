from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import project_memory
from data_modules.config import DataModulesConfig
from data_modules.memory_contract_adapter import MemoryContractAdapter


def _project(tmp_path: Path, chapter: int = 3) -> Path:
    project = tmp_path / "小说项目"
    (project / ".webnovel").mkdir(parents=True)
    (project / ".webnovel" / "state.json").write_text(
        json.dumps({"progress": {"current_chapter": chapter}}, ensure_ascii=False),
        encoding="utf-8",
    )
    return project


def test_project_memory_append_duplicate_backup_lock_and_utf8(tmp_path):
    project = _project(tmp_path)
    memory = project / ".webnovel" / "project_memory.json"

    first = project_memory.add_pattern(
        project,
        pattern_type="hook",
        description="章末用未完成动作收束",
        category="钩子",
        importance="high",
    )
    first_bytes = memory.read_bytes()
    second = project_memory.add_pattern(
        project,
        pattern_type="dialogue",
        description="冲突场景让对白带隐含目标",
        importance="medium",
        source_chapter=9,
    )
    before_duplicate = hashlib.sha256(memory.read_bytes()).hexdigest()
    duplicate = project_memory.add_pattern(
        project,
        pattern_type="dialogue",
        description="冲突场景让对白带隐含目标",
        importance="low",
    )

    payload = json.loads(memory.read_text(encoding="utf-8"))
    assert first["schema_version"] == "webnovel-learn-result/v1"
    assert first["learned"]["source_chapter"] == 3
    assert second["learned"]["source_chapter"] == 9
    assert duplicate["status"] == "skipped"
    assert len(payload["patterns"]) == 2
    assert hashlib.sha256(memory.read_bytes()).hexdigest() == before_duplicate
    assert memory.with_suffix(".json.bak").read_bytes() == first_bytes
    assert not memory.read_bytes().startswith(b"\xef\xbb\xbf")


def test_project_memory_corrupt_json_is_preserved(tmp_path):
    project = _project(tmp_path)
    memory = project / ".webnovel" / "project_memory.json"
    memory.write_text('{"patterns": [', encoding="utf-8")
    before = memory.read_bytes()

    with pytest.raises(ValueError, match="JSON 解析失败"):
        project_memory.add_pattern(
            project,
            pattern_type="other",
            description="不得覆盖损坏文件",
        )

    assert memory.read_bytes() == before


def test_project_memory_concurrent_appends_do_not_lose_records(tmp_path):
    project = _project(tmp_path)

    def _append(index: int):
        return project_memory.add_pattern(
            project,
            pattern_type="other",
            description=f"并发经验 {index}",
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(_append, range(8)))

    payload = json.loads(
        (project / ".webnovel" / "project_memory.json").read_text(encoding="utf-8")
    )
    assert all(item["status"] == "success" for item in results)
    assert {item["description"] for item in payload["patterns"]} == {
        f"并发经验 {index}" for index in range(8)
    }


def test_new_pattern_is_consumed_by_read_only_context(tmp_path):
    project = _project(tmp_path)
    project_memory.add_pattern(
        project,
        pattern_type="payoff",
        description="每个悬念在三章内给一次微兑现",
        importance="high",
    )

    pack = MemoryContractAdapter(
        DataModulesConfig.from_project_root(project), read_only=True
    ).load_context(4)

    assert pack.sections["author_style_patterns"][0]["description"] == "每个悬念在三章内给一次微兑现"


@pytest.mark.parametrize("chapter", [0, -3, True, "3", "not-a-number"])
def test_invalid_current_chapter_is_not_persisted(tmp_path, chapter):
    project = _project(tmp_path)
    (project / ".webnovel" / "state.json").write_text(
        json.dumps({"progress": {"current_chapter": chapter}}, ensure_ascii=False),
        encoding="utf-8",
    )

    result = project_memory.add_pattern(
        project,
        pattern_type="other",
        description="无效进度不得成为来源章节",
    )

    assert result["learned"]["source_chapter"] is None


def test_atomic_failure_preserves_existing_bytes(tmp_path, monkeypatch):
    project = _project(tmp_path)
    memory = project / ".webnovel" / "project_memory.json"
    memory.write_text(
        json.dumps({"patterns": [{"pattern_type": "hook", "description": "旧记录"}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    before = memory.read_bytes()
    before_names = sorted(path.name for path in memory.parent.iterdir())

    def _fail(*_args, **_kwargs):
        raise project_memory.AtomicWriteError("injected atomic failure")

    monkeypatch.setattr(project_memory, "atomic_write_json", _fail)
    with pytest.raises(project_memory.AtomicWriteError, match="injected"):
        project_memory.add_pattern(
            project,
            pattern_type="other",
            description="不能半写",
        )

    assert memory.read_bytes() == before
    assert sorted(path.name for path in memory.parent.iterdir()) == before_names


def test_unified_cli_learn_then_read_only_context_consumes_pattern(tmp_path):
    project = _project(tmp_path)
    request = tmp_path / "learn-request.json"
    request.write_text(
        json.dumps(
            {
                "schema_version": "webnovel-learn-request/v1",
                "pattern_type": "payoff",
                "description": "统一 CLI 写入后必须进入只读上下文",
                "category": "验收",
                "importance": "high",
                "source_chapter": 3,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    cli = Path(__file__).resolve().parents[2] / "webnovel.py"

    learned = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            str(cli),
            "--project-root",
            str(project),
            "project-memory",
            "add-pattern",
            "--input-json",
            str(request),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert learned.returncode == 0, learned.stderr
    assert json.loads(learned.stdout)["status"] == "success"

    context = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            str(cli),
            "--project-root",
            str(project),
            "memory-contract",
            "--read-only",
            "--with-provenance",
            "load-context",
            "--chapter",
            "4",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert context.returncode == 0, context.stderr
    payload = json.loads(context.stdout)
    patterns = payload["data"]["sections"]["author_style_patterns"]
    assert patterns[0]["description"] == "统一 CLI 写入后必须进入只读上下文"
    source = next(item for item in payload["sources"] if item["kind"] == "derived_author_memory")
    assert Path(source["path"]).resolve() == (
        project / ".webnovel" / "project_memory.json"
    ).resolve()


def test_project_memory_cli_uses_exit_two_for_bad_request_and_one_for_corrupt_memory(
    tmp_path, monkeypatch, capsys
):
    project = _project(tmp_path)
    bad_request = tmp_path / "bad-request.json"
    bad_request.write_text('{"schema_version":"wrong"}', encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "project_memory.py",
            "--project-root",
            str(project),
            "add-pattern",
            "--input-json",
            str(bad_request),
        ],
    )
    with pytest.raises(SystemExit) as invalid_exit:
        project_memory.main()
    invalid_payload = json.loads(capsys.readouterr().out)
    assert invalid_exit.value.code == 2
    assert invalid_payload["status"] == "error"

    memory = project / ".webnovel" / "project_memory.json"
    memory.write_text('{"patterns": [', encoding="utf-8")
    before = memory.read_bytes()
    valid_request = tmp_path / "valid-request.json"
    valid_request.write_text(
        json.dumps(
            {
                "schema_version": "webnovel-learn-request/v1",
                "pattern_type": "other",
                "description": "有效经验",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "project_memory.py",
            "--project-root",
            str(project),
            "add-pattern",
            "--input-json",
            str(valid_request),
        ],
    )
    with pytest.raises(SystemExit) as corrupt_exit:
        project_memory.main()
    corrupt_payload = json.loads(capsys.readouterr().out)
    assert corrupt_exit.value.code == 1
    assert corrupt_payload["status"] == "error"
    assert memory.read_bytes() == before


def test_project_memory_rejects_symlink_target_before_writing(tmp_path, monkeypatch):
    project = _project(tmp_path)
    memory = project / ".webnovel" / "project_memory.json"
    original_is_symlink = Path.is_symlink

    def _is_symlink(path):
        return path == memory or original_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", _is_symlink)
    with pytest.raises(ValueError, match="符号链接"):
        project_memory.add_pattern(
            project,
            pattern_type="other",
            description="不得越界",
        )
    assert not memory.exists()


@pytest.mark.parametrize("leaf", ["project_memory.json.lock", "project_memory.json.bak"])
def test_project_memory_rejects_linklike_control_leaves(tmp_path, monkeypatch, leaf):
    project = _project(tmp_path)
    protected = project / ".webnovel" / leaf
    original_is_symlink = Path.is_symlink

    def _is_symlink(path):
        return path == protected or original_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", _is_symlink)
    with pytest.raises(ValueError, match="符号链接"):
        project_memory.add_pattern(
            project,
            pattern_type="other",
            description="控制文件不得越界",
        )
    assert not (project / ".webnovel" / "project_memory.json").exists()


def test_project_memory_cli_reports_missing_lock_dependency_without_traceback(
    tmp_path, monkeypatch, capsys
):
    project = _project(tmp_path)
    request = tmp_path / "valid-request.json"
    request.write_text(
        json.dumps(
            {
                "schema_version": "webnovel-learn-request/v1",
                "pattern_type": "other",
                "description": "锁依赖缺失时必须结构化失败",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setitem(sys.modules, "filelock", None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "project_memory.py",
            "--project-root",
            str(project),
            "add-pattern",
            "--input-json",
            str(request),
        ],
    )

    with pytest.raises(SystemExit) as failed:
        project_memory.main()

    payload = json.loads(capsys.readouterr().out)
    assert failed.value.code == 2
    assert payload["status"] == "failed"
    assert "filelock" in payload["error"]
    assert not (project / ".webnovel" / "project_memory.json").exists()
