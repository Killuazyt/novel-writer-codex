from __future__ import annotations

import json
import subprocess
from pathlib import Path

import upstream_sync


LOCKED_SHA = "1" * 40
NEXT_SHA = "2" * 40
SOURCE_SUBDIRECTORY = "webnovel-writer"
INCLUDE = ["README.md", "scripts/**", "templates/**"]
EXCLUDE = [
    {"glob": "**/.env*", "reason": "test", "disposition": "excluded"},
    {
        "glob": "**/{.tmp,__pycache__}/**",
        "reason": "test",
        "disposition": "excluded",
    },
]


def _write_source(source_root: Path) -> Path:
    import_root = source_root / SOURCE_SUBDIRECTORY
    (import_root / "scripts" / "__pycache__").mkdir(parents=True)
    (import_root / "templates").mkdir(parents=True)
    (import_root / "README.md").write_text("upstream\n", encoding="utf-8")
    (import_root / "scripts" / "tool.py").write_text("print('ok')\n", encoding="utf-8")
    (import_root / "templates" / "中文.md").write_text("模板\n", encoding="utf-8")
    (import_root / "scripts" / ".env.local").write_text("SECRET=ignored\n", encoding="utf-8")
    (import_root / "scripts" / "__pycache__" / "tool.pyc").write_bytes(b"ignored")
    (import_root / "outside.txt").write_text("not included\n", encoding="utf-8")
    return import_root


def _expected_files(import_root: Path) -> dict[str, str]:
    selected = ["README.md", "scripts/tool.py", "templates/中文.md"]
    return {
        path: upstream_sync._sha256(
            import_root.joinpath(*path.split("/")).read_bytes()
        )
        for path in selected
    }


def _write_lock(root: Path, import_root: Path, *, sha: str = LOCKED_SHA) -> dict:
    files = _expected_files(import_root)
    payload = {
        "schema_version": 1,
        "upstream": {
            "repository": "https://example.invalid/upstream.git",
            "branch": "master",
            "commit": sha,
            "version": "1.0.0",
            "version_source": f"{SOURCE_SUBDIRECTORY}/README.md",
            "version_source_sha256": files["README.md"],
        },
        "import": {
            "source_subdirectory": SOURCE_SUBDIRECTORY,
            "include": INCLUDE,
            "exclude": EXCLUDE,
        },
        "hashes": {
            "algorithm": "sha256",
            "scope": "test",
            "aggregate_algorithm": "test",
            "aggregate": upstream_sync._aggregate_hash(files),
            "file_count": len(files),
            "files": files,
        },
    }
    root.mkdir(parents=True, exist_ok=True)
    (root / "upstream-lock.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return payload


def _directory_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "downstream"
    source_root = tmp_path / "上游 源码 (M2) & Unicode"
    import_root = _write_source(source_root)
    _write_lock(root, import_root)
    return root, source_root, import_root


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def _git(root: Path, *args: str) -> str:
    process = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    return process.stdout.strip()


def test_check_directory_source_is_current_and_writes_nothing(tmp_path):
    root, source_root, _ = _directory_fixture(tmp_path)

    code, result = upstream_sync.execute(
        "check", root=root, source_root=source_root, sha=LOCKED_SHA
    )

    assert code == 0
    assert result["schema_version"] == 1
    assert result["action"] == "check"
    assert result["status"] == "current"
    assert result["source_kind"] == "directory"
    assert result["source_sha"] == LOCKED_SHA
    assert result["target_sha"] == LOCKED_SHA
    assert result["locked_sha"] == LOCKED_SHA
    assert result["changes"] == {"added": [], "modified": [], "deleted": []}
    assert result["staging_dir"] is None
    assert result["issues"] == []
    assert result["actual_file_count"] == result["expected_file_count"] == 3
    assert result["added"] == result["removed"] == result["changed"] == []
    assert tuple(result) == upstream_sync.RESULT_FIELDS
    assert not (root / ".tmp").exists()
    assert list(root.iterdir()) == [root / "upstream-lock.json"]


def test_check_reports_content_and_commit_drift_without_writing(tmp_path):
    root, source_root, import_root = _directory_fixture(tmp_path)
    (import_root / "scripts" / "tool.py").write_text("print('changed')\n", encoding="utf-8")

    code, result = upstream_sync.execute(
        "check", root=root, source_root=source_root, sha=NEXT_SHA
    )

    assert code == 1
    assert result["status"] == "drift"
    assert result["source_sha"] == NEXT_SHA
    assert result["changed"] == ["scripts/tool.py"]
    assert result["changes"]["modified"] == ["scripts/tool.py"]
    assert result["issues"][0]["code"] == "lock_drift"
    assert not (root / ".tmp").exists()


def test_prepare_copies_only_selected_files_and_is_byte_idempotent(tmp_path):
    root, source_root, _ = _directory_fixture(tmp_path)

    code, first = upstream_sync.execute(
        "prepare", root=root, source_root=source_root, sha=LOCKED_SHA
    )

    assert code == 0
    assert first["status"] == "prepared"
    assert first["reused"] is False
    destination = Path(str(first["destination"]))
    assert destination == root / ".tmp" / "upstream-sync" / LOCKED_SHA
    assert first["staging_dir"] == str(destination)
    assert set(_tree_bytes(destination)) == {
        "README.md",
        "scripts/tool.py",
        "templates/中文.md",
        "report.json",
    }
    report = json.loads((destination / "report.json").read_text(encoding="utf-8"))
    assert tuple(report) == upstream_sync.RESULT_FIELDS
    assert report["status"] == "prepared"
    assert report["reused"] is False
    before = _tree_bytes(destination)

    code, second = upstream_sync.execute(
        "prepare", root=root, source_root=source_root, sha=LOCKED_SHA
    )

    assert code == 0
    assert second["status"] == "prepared"
    assert second["reused"] is True
    assert _tree_bytes(destination) == before
    assert {path.name for path in root.iterdir()} == {"upstream-lock.json", ".tmp"}


def test_prepare_drift_snapshot_succeeds_and_preserves_comparison(tmp_path):
    root, source_root, import_root = _directory_fixture(tmp_path)
    (import_root / "scripts" / "tool.py").write_text("print('new upstream')\n", encoding="utf-8")

    code, result = upstream_sync.execute(
        "prepare", root=root, source_root=source_root, sha=NEXT_SHA
    )

    assert code == 0
    assert result["status"] == "prepared"
    assert result["source_sha"] == NEXT_SHA
    assert result["changed"] == ["scripts/tool.py"]
    assert result["changes"]["modified"] == ["scripts/tool.py"]
    assert result["issues"][0]["severity"] == "warning"
    assert Path(str(result["destination"])).name == NEXT_SHA


def test_prepare_existing_different_tree_is_conflict_and_not_overwritten(tmp_path):
    root, source_root, import_root = _directory_fixture(tmp_path)
    code, first = upstream_sync.execute(
        "prepare", root=root, source_root=source_root, sha=LOCKED_SHA
    )
    assert code == 0
    destination = Path(str(first["destination"]))
    before = _tree_bytes(destination)
    (import_root / "scripts" / "tool.py").write_text("print('collision')\n", encoding="utf-8")

    code, conflict = upstream_sync.execute(
        "prepare", root=root, source_root=source_root, sha=LOCKED_SHA
    )

    assert code == 1
    assert conflict["status"] == "conflict"
    assert conflict["issues"][0]["code"] == "staging_conflict"
    assert _tree_bytes(destination) == before


def test_invalid_source_contract_and_lock_traversal_exit_two_without_writes(tmp_path):
    root, source_root, import_root = _directory_fixture(tmp_path)

    code, missing_sha = upstream_sync.execute(
        "check", root=root, source_root=source_root
    )
    assert code == 2
    assert missing_sha["status"] == "invalid"
    assert missing_sha["issues"][0]["code"] == "invalid_input"

    payload = _write_lock(root, import_root)
    payload["hashes"]["files"]["../escape.txt"] = "0" * 64
    (root / "upstream-lock.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    code, traversal = upstream_sync.execute(
        "prepare", root=root, source_root=source_root, sha=LOCKED_SHA
    )

    assert code == 2
    assert traversal["status"] == "invalid"
    assert "traversal" in str(traversal["message"])
    assert not (root / ".tmp").exists()
    assert not (tmp_path / "escape.txt").exists()


def test_default_local_ref_is_read_without_fetch(monkeypatch, tmp_path):
    root = tmp_path / "git-downstream"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "test")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "core.autocrlf", "false")
    import_root = _write_source(root)
    _git(root, "add", "--", SOURCE_SUBDIRECTORY)
    _git(root, "commit", "-m", "upstream fixture")
    commit_sha = _git(root, "rev-parse", "HEAD")
    _git(root, "update-ref", "refs/remotes/upstream/master", commit_sha)
    _write_lock(root, import_root, sha=commit_sha)
    original_run_git = upstream_sync._run_git
    calls: list[tuple[str, ...]] = []

    def no_fetch(repository_root, args):
        calls.append(tuple(args))
        assert "fetch" not in args
        return original_run_git(repository_root, args)

    monkeypatch.setattr(upstream_sync, "_run_git", no_fetch)
    code, result = upstream_sync.execute("check", root=root)

    assert code == 0
    assert result["status"] == "current"
    assert result["source_kind"] == "git_ref"
    assert result["source_ref"] == upstream_sync.DEFAULT_REF
    assert result["source_sha"] == commit_sha
    assert calls
    assert not (root / ".tmp").exists()


def test_missing_local_ref_is_invalid_and_does_not_create_tmp(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    source_root = tmp_path / "source"
    import_root = _write_source(source_root)
    _write_lock(root, import_root)

    code, result = upstream_sync.execute("check", root=root)

    assert code == 2
    assert result["status"] == "invalid"
    assert "upstream/master" in str(result["message"])
    assert not (root / ".tmp").exists()


def test_main_emits_stable_json_and_text_fields(capsys, tmp_path):
    root, source_root, _ = _directory_fixture(tmp_path)
    args = [
        "check",
        "--root",
        str(root),
        "--source-root",
        str(source_root),
        "--sha",
        LOCKED_SHA,
    ]

    assert upstream_sync.main([*args, "--format", "json"]) == 0
    json_result = json.loads(capsys.readouterr().out)
    assert tuple(json_result) == upstream_sync.RESULT_FIELDS
    assert {
        "schema_version",
        "action",
        "source_sha",
        "target_sha",
        "locked_sha",
        "changes",
        "staging_dir",
        "issues",
    }.issubset(json_result)

    assert upstream_sync.main([*args, "--format", "text"]) == 0
    text_lines = capsys.readouterr().out.splitlines()
    assert [line.split(":", 1)[0] for line in text_lines] == list(
        upstream_sync.RESULT_FIELDS
    )
