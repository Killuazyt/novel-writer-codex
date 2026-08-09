from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import backup_manager
import pytest
from backup_manager import (
    GitBackupManager,
    build_git_backup_authorization_token,
    build_git_backup_decision_marker,
    build_git_backup_decision_receipt,
    read_git_backup_authorization_state,
)


def _git(project_root, *args):
    return subprocess.run(
        ["git", *args],
        cwd=project_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def _init_repo(project_root):
    assert _git(project_root, "init", "-b", "main").returncode == 0
    assert _git(project_root, "config", "user.name", "Test Author").returncode == 0
    assert _git(project_root, "config", "user.email", "author@example.com").returncode == 0
    seed = project_root / "seed.txt"
    seed.write_text("seed", encoding="utf-8")
    assert _git(project_root, "add", "--", "seed.txt").returncode == 0
    assert _git(project_root, "commit", "-m", "seed").returncode == 0


def _decision_receipt(
    project_root,
    chapter,
    allowlist,
    monkeypatch,
    *,
    answer="授权一次",
    thread="backup-parent",
    session_fields=None,
):
    try:
        thread = str(uuid.UUID(thread))
    except ValueError:
        thread = str(uuid.uuid5(uuid.NAMESPACE_URL, f"webnovel-test:{thread}"))
    sessions = project_root / "codex-sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(backup_manager, "TRUSTED_CODEX_SESSIONS_ROOT", sessions.resolve())
    monkeypatch.setenv("CODEX_THREAD_ID", thread)
    rollout = sessions / f"rollout-{thread}.jsonl"
    marker = build_git_backup_decision_marker(project_root, chapter, allowlist)
    session_payload = {"id": thread, "model": "gpt-5.6-sol"}
    session_payload.update(dict(session_fields or {}))
    events = [
        {"type": "session_meta", "payload": session_payload},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": marker}],
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
    rollout.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in events) + "\n",
        encoding="utf-8",
    )
    return build_git_backup_decision_receipt(
        project_root,
        chapter,
        allowlist,
        rollout_path=rollout,
        thread_id=thread,
    )


def test_constructor_never_initializes_non_git_project(tmp_path, monkeypatch):
    monkeypatch.setattr(backup_manager, "is_git_available", lambda: True)

    manager = GitBackupManager(str(tmp_path))
    receipt = manager.backup(1, allowlist=["正文/第0001章.md"])

    assert receipt["ok"] is True
    assert receipt["code"] == "skipped_non_git"
    assert not (tmp_path / ".git").exists()
    assert not (tmp_path / ".gitignore").exists()


def test_git_backup_decision_receipt_rejects_cross_task_thread(tmp_path, monkeypatch):
    _init_repo(tmp_path)
    body = tmp_path / "正文" / "第0001章.md"
    body.parent.mkdir()
    body.write_text("正文", encoding="utf-8")
    allowlist = ["正文/第0001章.md"]
    decision = _decision_receipt(tmp_path, 1, allowlist, monkeypatch, thread="cross-task")
    token = build_git_backup_authorization_token(tmp_path, 1, allowlist)
    monkeypatch.setenv("CODEX_THREAD_ID", "33333333-3333-4333-8333-333333333333")

    receipt = GitBackupManager(str(tmp_path)).backup(
        1,
        allowlist=allowlist,
        authorization_token=token,
        decision_receipt=decision,
    )

    assert receipt["code"] == "git_backup_authorization_required"
    assert "current Codex task" in receipt["detail"]
    assert _git(tmp_path, "tag", "--list").stdout.strip() == ""


def test_git_backup_decision_receipt_allows_only_append_after_answer(tmp_path, monkeypatch):
    _init_repo(tmp_path)
    body = tmp_path / "正文" / "第0001章.md"
    body.parent.mkdir()
    body.write_text("正文", encoding="utf-8")
    allowlist = ["正文/第0001章.md"]
    decision = _decision_receipt(tmp_path, 1, allowlist, monkeypatch, thread="append-safe")
    rollout = Path(decision["rollout_path"])
    with rollout.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps({"type": "event_msg", "payload": {"message": "later"}}) + "\n")

    verified = backup_manager.verify_git_backup_decision_receipt(
        tmp_path, 1, allowlist, decision
    )

    assert verified == decision


def test_decision_marker_displays_exact_head_and_allowlist(tmp_path):
    _init_repo(tmp_path)
    body = tmp_path / "正文" / "第0001章.md"
    body.parent.mkdir()
    body.write_text("正文", encoding="utf-8")
    allowlist = ["正文/第0001章.md"]

    marker = build_git_backup_decision_marker(tmp_path, 1, allowlist)
    request = json.loads(marker.removeprefix(backup_manager.BACKUP_DECISION_PREFIX))
    prompt = request["questions"][0]["prompt"]

    assert f"HEAD={_git(tmp_path, 'rev-parse', 'HEAD').stdout.strip()}" in prompt
    assert 'exact_allowlist=["正文/第0001章.md"]' in prompt


@pytest.mark.parametrize(
    "bad_thread",
    [
        "00000000-0000-0000-0000-000000000000",
        "ABCDEF12-3456-4789-ABCD-EF1234567890",
    ],
)
def test_decision_receipt_requires_canonical_nonzero_current_thread(
    tmp_path, monkeypatch, bad_thread
):
    _init_repo(tmp_path)
    body = tmp_path / "正文" / "第0001章.md"
    body.parent.mkdir()
    body.write_text("正文", encoding="utf-8")
    allowlist = ["正文/第0001章.md"]
    sessions = tmp_path / "codex-sessions"
    sessions.mkdir()
    monkeypatch.setattr(backup_manager, "TRUSTED_CODEX_SESSIONS_ROOT", sessions.resolve())
    monkeypatch.setenv("CODEX_THREAD_ID", bad_thread)
    rollout = sessions / f"rollout-{bad_thread}.jsonl"
    marker = build_git_backup_decision_marker(tmp_path, 1, allowlist)
    rollout.write_text(
        "\n".join(
            json.dumps(item, ensure_ascii=False)
            for item in [
                {"type": "session_meta", "payload": {"id": bad_thread}},
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": marker}],
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "授权一次"}],
                    },
                },
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(backup_manager.BackupError, match="canonical nonzero UUID"):
        build_git_backup_decision_receipt(
            tmp_path,
            1,
            allowlist,
            rollout_path=rollout,
            thread_id=bad_thread,
        )


@pytest.mark.parametrize(
    "session_fields",
    [
        {"parent_thread_id": "11111111-1111-4111-8111-111111111111"},
        {"parent_thread_id": 0},
        {"source": {"subagent": {"thread_spawn": {}}}},
    ],
)
def test_decision_receipt_rejects_subagent_rollout(tmp_path, monkeypatch, session_fields):
    _init_repo(tmp_path)
    body = tmp_path / "正文" / "第0001章.md"
    body.parent.mkdir()
    body.write_text("正文", encoding="utf-8")

    with pytest.raises(backup_manager.BackupError, match="top-level Codex task"):
        _decision_receipt(
            tmp_path,
            1,
            ["正文/第0001章.md"],
            monkeypatch,
            thread="subagent-decision",
            session_fields=session_fields,
        )


def test_decision_receipt_rejects_later_revocation_or_duplicate_choice(tmp_path, monkeypatch):
    _init_repo(tmp_path)
    body = tmp_path / "正文" / "第0001章.md"
    body.parent.mkdir()
    body.write_text("正文", encoding="utf-8")
    allowlist = ["正文/第0001章.md"]
    decision = _decision_receipt(tmp_path, 1, allowlist, monkeypatch, thread="revoked")
    rollout = Path(decision["rollout_path"])
    with rollout.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "不创建备份"}],
                    },
                },
                ensure_ascii=False,
            )
            + "\n"
        )

    with pytest.raises(backup_manager.BackupError, match="superseding selected answers"):
        backup_manager.verify_git_backup_decision_receipt(tmp_path, 1, allowlist, decision)


def test_decision_receipt_rejects_mutated_authorized_prefix(tmp_path, monkeypatch):
    _init_repo(tmp_path)
    body = tmp_path / "正文" / "第0001章.md"
    body.parent.mkdir()
    body.write_text("正文", encoding="utf-8")
    allowlist = ["正文/第0001章.md"]
    decision = _decision_receipt(tmp_path, 1, allowlist, monkeypatch, thread="prefix-mutation")
    rollout = Path(decision["rollout_path"])
    raw = rollout.read_text(encoding="utf-8")
    rollout.write_text(raw.replace("gpt-5.6-sol", "gpt-5.6-alt", 1), encoding="utf-8")

    with pytest.raises(backup_manager.BackupError, match="stale|does not match"):
        backup_manager.verify_git_backup_decision_receipt(tmp_path, 1, allowlist, decision)


def test_nested_parent_repo_is_not_reused(tmp_path):
    _init_repo(tmp_path)
    project = tmp_path / "books" / "小说"
    project.mkdir(parents=True)
    before = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

    manager = GitBackupManager(str(project))
    receipt = manager.backup(1, allowlist=["正文/第0001章.md"])

    assert receipt["code"] == "skipped_non_git"
    assert "no .git entry" in receipt["detail"]
    assert _git(tmp_path, "rev-parse", "HEAD").stdout.strip() == before
    assert not (project / ".git").exists()


def test_external_gitfile_is_probe_failure_and_never_touches_external_repo(tmp_path):
    external = tmp_path / "external"
    external.mkdir()
    _init_repo(external)
    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").write_text(
        f"gitdir: {(external / '.git').as_posix()}\n", encoding="utf-8"
    )
    external_head = _git(external, "rev-parse", "HEAD").stdout.strip()

    manager = GitBackupManager(str(project))
    receipt = manager.backup(1, allowlist=["正文/第0001章.md"])

    assert manager.repository_status == "error"
    assert receipt["code"] == "git_repository_probe_failed"
    assert _git(external, "rev-parse", "HEAD").stdout.strip() == external_head
    assert _git(external, "tag", "--list").stdout.strip() == ""


def test_reparse_git_directory_is_probe_failure_and_never_touches_target(tmp_path):
    external = tmp_path / "external"
    external.mkdir()
    _init_repo(external)
    project = tmp_path / "project"
    project.mkdir()
    git_link = project / ".git"
    try:
        git_link.symlink_to(external / ".git", target_is_directory=True)
    except OSError:
        if os.name != "nt":
            pytest.skip("directory symlinks are unavailable on this host")
        junction = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(git_link), str(external / ".git")],
            capture_output=True,
            text=True,
            check=False,
        )
        if junction.returncode != 0:
            pytest.skip("directory junctions are unavailable on this host")
    target_head = _git(external, "rev-parse", "HEAD").stdout.strip()
    try:
        receipt = GitBackupManager(str(project)).backup(
            1, allowlist=["正文/第0001章.md"]
        )
    finally:
        if git_link.is_symlink():
            git_link.unlink()
        elif git_link.exists():
            os.rmdir(git_link)

    assert receipt["code"] == "git_repository_probe_failed"
    assert _git(external, "rev-parse", "HEAD").stdout.strip() == target_head
    assert _git(external, "tag", "--list").stdout.strip() == ""


@pytest.mark.parametrize(
    ("config_key", "config_value", "detail"),
    [
        ("core.worktree", "../outside", "core.worktree"),
        ("core.bare", "true", "bare"),
    ],
)
def test_repository_probe_rejects_forbidden_git_config(
    tmp_path, config_key, config_value, detail
):
    _init_repo(tmp_path)
    assert _git(tmp_path, "config", "--local", config_key, config_value).returncode == 0

    manager = GitBackupManager(str(tmp_path))

    assert manager.repository_status == "error"
    assert detail in manager.repository_error
    assert manager.backup(1, allowlist=["seed.txt"])["code"] == "git_repository_probe_failed"


def test_repository_probe_rejects_nonempty_object_alternates(tmp_path):
    _init_repo(tmp_path)
    external_objects = tmp_path / "external-objects"
    external_objects.mkdir()
    alternates = tmp_path / ".git" / "objects" / "info" / "alternates"
    alternates.write_text(str(external_objects.resolve()) + "\n", encoding="utf-8")

    manager = GitBackupManager(str(tmp_path))

    assert manager.repository_status == "error"
    assert "alternates" in manager.repository_error
    assert manager.backup(1, allowlist=["seed.txt"])["code"] == "git_repository_probe_failed"


@pytest.mark.parametrize("control_name", ["commondir", "gitdir"])
def test_repository_probe_rejects_linked_worktree_control_files(tmp_path, control_name):
    _init_repo(tmp_path)
    (tmp_path / ".git" / control_name).write_text("../external\n", encoding="utf-8")

    manager = GitBackupManager(str(tmp_path))

    assert manager.repository_status == "error"
    assert "control files" in manager.repository_error


def test_git_routing_environment_is_ignored_for_probe_and_backup(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    _init_repo(project)
    _init_repo(external)
    body = project / "正文" / "第0001章.md"
    body.parent.mkdir()
    body.write_text("项目正文", encoding="utf-8")
    allowlist = ["正文/第0001章.md"]
    external_head = _git(external, "rev-parse", "HEAD").stdout.strip()
    monkeypatch.setenv("GIT_DIR", str(external / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(external))
    token = build_git_backup_authorization_token(project, 1, allowlist)
    decision = _decision_receipt(project, 1, allowlist, monkeypatch, thread="git-env")

    receipt = GitBackupManager(str(project)).backup(
        1,
        allowlist=allowlist,
        authorization_token=token,
        decision_receipt=decision,
    )

    clean_env = {
        key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")
    }
    project_tag = subprocess.run(
        ["git", "rev-parse", "refs/tags/ch0001"],
        cwd=project,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        env=clean_env,
    )
    external_tags = subprocess.run(
        ["git", "tag", "--list"],
        cwd=external,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        env=clean_env,
    )
    external_after = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=external,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        env=clean_env,
    )
    assert receipt["status"] == "completed"
    assert project_tag.stdout.strip() == receipt["commit"]
    assert external_tags.stdout.strip() == ""
    assert external_after.stdout.strip() == external_head


def test_probe_execution_error_is_blocker_not_non_git_skip(tmp_path, monkeypatch):
    _init_repo(tmp_path)

    def fail_probe(*_args, **_kwargs):
        raise backup_manager.BackupError("injected probe timeout")

    monkeypatch.setattr(backup_manager, "_run_bound_git", fail_probe)
    manager = GitBackupManager(str(tmp_path))
    receipt = manager.backup(1, allowlist=["seed.txt"])

    assert manager.repository_status == "error"
    assert receipt["ok"] is False
    assert receipt["code"] == "git_repository_probe_failed"
    assert "injected probe timeout" in receipt["detail"]


def test_silent_optional_git_probe_error_is_not_treated_as_missing_config(
    tmp_path, monkeypatch
):
    _init_repo(tmp_path)
    real_run = backup_manager._run_bound_git_status

    def fail_worktree_probe(root, git_dir, args, **kwargs):
        if args == ["config", "--local", "--get", "core.worktree"]:
            return 128, b"", b""
        return real_run(root, git_dir, args, **kwargs)

    monkeypatch.setattr(backup_manager, "_run_bound_git_status", fail_worktree_probe)
    manager = GitBackupManager(str(tmp_path))

    assert manager.repository_status == "error"
    assert "core.worktree" in manager.repository_error
    assert manager.backup(1, allowlist=["seed.txt"])["code"] == "git_repository_probe_failed"


def test_git_backup_requires_bound_authorization_and_writes_nothing(tmp_path):
    _init_repo(tmp_path)
    chapter = tmp_path / "正文" / "第0001章.md"
    chapter.parent.mkdir()
    chapter.write_text("正文", encoding="utf-8")
    head_before = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
    status_before = _git(tmp_path, "status", "--porcelain=v1").stdout

    receipt = GitBackupManager(str(tmp_path)).backup(
        1,
        allowlist=["正文/第0001章.md"],
    )

    assert receipt["status"] == "authorization_required"
    assert receipt["scope_challenge"].startswith("webnovel-git-backup:")
    assert receipt["decision_receipt_required"] is True
    assert _git(tmp_path, "rev-parse", "HEAD").stdout.strip() == head_before
    assert _git(tmp_path, "status", "--porcelain=v1").stdout == status_before
    assert _git(tmp_path, "rev-parse", "--verify", "refs/tags/ch0001").returncode != 0

    token_only = GitBackupManager(str(tmp_path)).backup(
        1,
        allowlist=["正文/第0001章.md"],
        authorization_token=build_git_backup_authorization_token(
            tmp_path, 1, ["正文/第0001章.md"]
        ),
    )
    assert token_only["status"] == "authorization_required"
    assert "user-decision receipt" in token_only["detail"]


def test_allowlisted_backup_uses_isolated_index_and_keeps_branch_and_staging(tmp_path, monkeypatch):
    _init_repo(tmp_path)
    chapter = tmp_path / "正文" / "第0001章.md"
    chapter.parent.mkdir()
    chapter.write_text("正文版本一", encoding="utf-8")
    unrelated = tmp_path / "用户草稿.md"
    unrelated.write_text("不要进入备份", encoding="utf-8")
    assert _git(tmp_path, "add", "--", "用户草稿.md").returncode == 0
    cached_before = _git(tmp_path, "diff", "--cached", "--name-only").stdout
    index_before = (tmp_path / ".git" / "index").read_bytes()
    head_before = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
    allowlist = ["正文/第0001章.md"]
    token = build_git_backup_authorization_token(tmp_path, 1, allowlist)
    decision = _decision_receipt(tmp_path, 1, allowlist, monkeypatch)

    receipt = GitBackupManager(str(tmp_path)).backup(
        1,
        "标题",
        allowlist=allowlist,
        authorization_token=token,
        decision_receipt=decision,
    )

    assert receipt["ok"] is True
    assert receipt["status"] == "completed"
    assert receipt["changed_paths"] == allowlist
    assert _git(tmp_path, "rev-parse", "HEAD").stdout.strip() == head_before
    assert _git(tmp_path, "diff", "--cached", "--name-only").stdout == cached_before
    assert (tmp_path / ".git" / "index").read_bytes() == index_before
    assert _git(tmp_path, "show", "ch0001:正文/第0001章.md").stdout == "正文版本一"
    assert _git(tmp_path, "cat-file", "-e", "ch0001:用户草稿.md").returncode != 0
    state = read_git_backup_authorization_state(tmp_path, decision["receipt_sha256"])
    assert state["status"] == "completed"
    assert state["result"] == receipt
    assert GitBackupManager(str(tmp_path)).backup(
        1,
        "标题",
        allowlist=allowlist,
        authorization_token=token,
        decision_receipt=decision,
    ) == receipt


def test_no_change_receipt_keeps_normal_index_byte_exact(tmp_path, monkeypatch):
    _init_repo(tmp_path)
    body = tmp_path / "正文" / "第0001章.md"
    body.parent.mkdir()
    body.write_text("已跟踪正文", encoding="utf-8")
    assert _git(tmp_path, "add", "--", "正文/第0001章.md").returncode == 0
    assert _git(tmp_path, "commit", "-m", "tracked chapter").returncode == 0
    allowlist = ["正文/第0001章.md"]
    token = build_git_backup_authorization_token(tmp_path, 1, allowlist)
    decision = _decision_receipt(tmp_path, 1, allowlist, monkeypatch, thread="no-change")
    index_before = (tmp_path / ".git" / "index").read_bytes()
    head_before = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

    receipt = GitBackupManager(str(tmp_path)).backup(
        1,
        allowlist=allowlist,
        authorization_token=token,
        decision_receipt=decision,
    )

    assert receipt["status"] == "skipped"
    assert receipt["code"] == "no_allowlisted_changes"
    assert (tmp_path / ".git" / "index").read_bytes() == index_before
    assert _git(tmp_path, "rev-parse", "HEAD").stdout.strip() == head_before
    assert _git(tmp_path, "rev-parse", "--verify", "refs/tags/ch0001").returncode != 0
    state = backup_manager.verify_git_backup_authorization_state(tmp_path, decision)
    assert state["status"] == "completed"
    assert state["result"] == receipt


def test_backup_disables_index_ref_hooks_and_clean_filters(tmp_path, monkeypatch):
    _init_repo(tmp_path)
    attributes = tmp_path / ".gitattributes"
    attributes.write_text("正文/第0001章.md filter=evil\n", encoding="utf-8")
    assert _git(tmp_path, "add", "--", ".gitattributes").returncode == 0
    assert _git(tmp_path, "commit", "-m", "attributes").returncode == 0
    filter_marker = tmp_path / "filter-ran.txt"
    filter_command = f"echo ran > '{filter_marker.as_posix()}' && false"
    assert _git(tmp_path, "config", "filter.evil.clean", filter_command).returncode == 0
    assert _git(tmp_path, "config", "filter.evil.required", "true").returncode == 0
    hook_markers = []
    for hook_name in ("post-index-change", "reference-transaction"):
        marker = tmp_path / f"{hook_name}-ran.txt"
        hook = tmp_path / ".git" / "hooks" / hook_name
        hook.write_text(
            f"#!/bin/sh\nprintf ran > '{marker.as_posix()}'\n",
            encoding="utf-8",
            newline="\n",
        )
        hook.chmod(0o755)
        hook_markers.append(marker)
    body = tmp_path / "正文" / "第0001章.md"
    body.parent.mkdir()
    body.write_text("未经 filter 改写的正文", encoding="utf-8")
    allowlist = ["正文/第0001章.md"]
    token = build_git_backup_authorization_token(tmp_path, 1, allowlist)
    decision = _decision_receipt(tmp_path, 1, allowlist, monkeypatch, thread="hooks-filters")

    receipt = GitBackupManager(str(tmp_path)).backup(
        1,
        allowlist=allowlist,
        authorization_token=token,
        decision_receipt=decision,
    )

    assert receipt["status"] == "completed"
    assert _git(tmp_path, "show", "ch0001:正文/第0001章.md").stdout == "未经 filter 改写的正文"
    assert not filter_marker.exists()
    assert all(not marker.exists() for marker in hook_markers)
    managed_tmp = tmp_path / ".webnovel" / "backup-authorizations" / "tmp"
    assert managed_tmp.is_dir()
    assert list(managed_tmp.iterdir()) == []


def test_allowlisted_bytes_change_after_capture_never_publishes_tag(tmp_path, monkeypatch):
    _init_repo(tmp_path)
    body = tmp_path / "正文" / "第0001章.md"
    body.parent.mkdir()
    body.write_text("A", encoding="utf-8")
    allowlist = ["正文/第0001章.md"]
    token = build_git_backup_authorization_token(tmp_path, 1, allowlist)
    decision = _decision_receipt(tmp_path, 1, allowlist, monkeypatch, thread="a-to-b")
    manager = GitBackupManager(str(tmp_path))
    real_build = manager._build_snapshot_result

    def mutate_before_build(**kwargs):
        body.write_text("B", encoding="utf-8")
        return real_build(**kwargs)

    monkeypatch.setattr(manager, "_build_snapshot_result", mutate_before_build)
    receipt = manager.backup(
        1,
        allowlist=allowlist,
        authorization_token=token,
        decision_receipt=decision,
    )

    assert receipt["status"] == "failed"
    assert receipt["code"] == "git_backup_failed"
    assert "allowlisted bytes changed" in receipt["detail"]
    assert _git(tmp_path, "rev-parse", "--verify", "refs/tags/ch0001").returncode != 0


def test_cross_project_and_old_head_decision_receipts_cannot_replay(tmp_path, monkeypatch):
    project_a = tmp_path / "a"
    project_b = tmp_path / "b"
    project_a.mkdir()
    project_b.mkdir()
    _init_repo(project_a)
    _init_repo(project_b)
    for project in (project_a, project_b):
        body = project / "正文" / "第0001章.md"
        body.parent.mkdir()
        body.write_text("正文", encoding="utf-8")
    allowlist = ["正文/第0001章.md"]
    decision_a = _decision_receipt(project_a, 1, allowlist, monkeypatch, thread="project-a")
    token_b = build_git_backup_authorization_token(project_b, 1, allowlist)

    cross = GitBackupManager(str(project_b)).backup(
        1,
        allowlist=allowlist,
        authorization_token=token_b,
        decision_receipt=decision_a,
    )

    assert cross["code"] == "git_backup_authorization_required"
    assert _git(project_b, "rev-parse", "--verify", "refs/tags/ch0001").returncode != 0

    unrelated = project_a / "later.txt"
    unrelated.write_text("later", encoding="utf-8")
    assert _git(project_a, "add", "--", "later.txt").returncode == 0
    assert _git(project_a, "commit", "-m", "later").returncode == 0
    current_token = build_git_backup_authorization_token(project_a, 1, allowlist)
    stale = GitBackupManager(str(project_a)).backup(
        1,
        allowlist=allowlist,
        authorization_token=current_token,
        decision_receipt=decision_a,
    )
    assert stale["code"] == "git_backup_authorization_required"
    assert _git(project_a, "rev-parse", "--verify", "refs/tags/ch0001").returncode != 0


def test_backup_existing_tag_is_conflict_and_never_moved(tmp_path, monkeypatch):
    _init_repo(tmp_path)
    assert _git(tmp_path, "tag", "ch0001").returncode == 0
    tag_before = _git(tmp_path, "rev-parse", "ch0001").stdout.strip()
    chapter = tmp_path / "正文" / "第0001章.md"
    chapter.parent.mkdir()
    chapter.write_text("正文", encoding="utf-8")
    allowlist = ["正文/第0001章.md"]
    decision = _decision_receipt(tmp_path, 1, allowlist, monkeypatch)

    receipt = GitBackupManager(str(tmp_path)).backup(
        1,
        allowlist=allowlist,
        authorization_token=build_git_backup_authorization_token(tmp_path, 1, allowlist),
        decision_receipt=decision,
    )

    assert receipt["status"] == "conflict"
    assert receipt["code"] == "backup_tag_exists"
    assert _git(tmp_path, "rev-parse", "ch0001").stdout.strip() == tag_before


def test_forged_pending_registry_commit_is_never_published(tmp_path, monkeypatch):
    _init_repo(tmp_path)
    body = tmp_path / "正文" / "第0002章.md"
    body.parent.mkdir()
    body.write_text("正文", encoding="utf-8")
    allowlist = ["正文/第0002章.md"]
    token = build_git_backup_authorization_token(tmp_path, 2, allowlist)
    decision = _decision_receipt(tmp_path, 2, allowlist, monkeypatch, thread="forged-pending")
    head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
    tree = _git(tmp_path, "rev-parse", "HEAD^{tree}").stdout.strip()
    oid = _git(tmp_path, "hash-object", "--no-filters", "正文/第0002章.md").stdout.strip()
    forged = {
        "schema_version": backup_manager.BACKUP_RECEIPT_SCHEMA,
        "project_root": str(tmp_path.resolve()),
        "chapter": 2,
        "created_at": "2026-08-08T00:00:00+08:00",
        "ok": True,
        "status": "completed",
        "code": "git_backup_created",
        "allowlist": allowlist,
        "changed_paths": allowlist,
        "head": decision["base_head"],
        "tree": tree,
        "commit": head,
        "tag": "ch0002",
        "commit_message": "forged",
        "path_objects": [
            {"path": allowlist[0], "state": "file", "mode": "100644", "oid": oid}
        ],
        "authorization_token_sha256": backup_manager.hashlib.sha256(
            token.encode("utf-8")
        ).hexdigest(),
        "decision_receipt_sha256": decision["receipt_sha256"],
        "decision_receipt": decision,
    }
    state_path, _ = backup_manager._authorization_paths(
        tmp_path.resolve(), decision["receipt_sha256"], create=True
    )
    forged_state = {
        "schema_version": backup_manager.BACKUP_AUTHORIZATION_REGISTRY_SCHEMA,
        "binding": backup_manager._decision_registry_binding(decision),
        "status": "failed-retryable",
        "attempts": 1,
        "created_at": "2026-08-08T00:00:00+08:00",
        "updated_at": "2026-08-08T00:00:00+08:00",
        "last_error": "injected crash",
        "pending_result": forged,
    }
    backup_manager._atomic_registry_write(tmp_path.resolve(), state_path, forged_state)

    receipt = GitBackupManager(str(tmp_path)).backup(
        2,
        allowlist=allowlist,
        authorization_token=token,
        decision_receipt=decision,
    )

    assert receipt["status"] == "failed"
    assert receipt["code"] == "git_backup_failed"
    assert _git(tmp_path, "rev-parse", "--verify", "refs/tags/ch0002").returncode != 0


def test_completed_registry_replay_rejects_tampered_tag(tmp_path, monkeypatch):
    _init_repo(tmp_path)
    body = tmp_path / "正文" / "第0001章.md"
    body.parent.mkdir()
    body.write_text("正文", encoding="utf-8")
    allowlist = ["正文/第0001章.md"]
    token = build_git_backup_authorization_token(tmp_path, 1, allowlist)
    decision = _decision_receipt(tmp_path, 1, allowlist, monkeypatch, thread="tag-tamper")
    manager = GitBackupManager(str(tmp_path))
    completed = manager.backup(
        1,
        allowlist=allowlist,
        authorization_token=token,
        decision_receipt=decision,
    )
    head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
    assert _git(tmp_path, "update-ref", "refs/tags/ch0001", head, completed["commit"]).returncode == 0

    replay = manager.backup(
        1,
        allowlist=allowlist,
        authorization_token=token,
        decision_receipt=decision,
    )

    assert replay["ok"] is False
    assert replay["code"] == "git_backup_authorization_registry_failed"
    assert _git(tmp_path, "rev-parse", "refs/tags/ch0001").stdout.strip() == head


def test_registry_strict_schema_rejects_extra_or_wrong_binding(tmp_path, monkeypatch):
    _init_repo(tmp_path)
    body = tmp_path / "正文" / "第0001章.md"
    body.parent.mkdir()
    body.write_text("正文", encoding="utf-8")
    allowlist = ["正文/第0001章.md"]
    token = build_git_backup_authorization_token(tmp_path, 1, allowlist)
    decision = _decision_receipt(tmp_path, 1, allowlist, monkeypatch, thread="bad-registry")
    state_path, _ = backup_manager._authorization_paths(
        tmp_path.resolve(), decision["receipt_sha256"], create=True
    )
    now = "2026-08-08T00:00:00+08:00"
    malformed = {
        "schema_version": backup_manager.BACKUP_AUTHORIZATION_REGISTRY_SCHEMA,
        "binding": {**backup_manager._decision_registry_binding(decision), "chapter": 99},
        "status": "claimed",
        "attempts": 0,
        "created_at": now,
        "updated_at": now,
        "unexpected": True,
    }
    backup_manager._atomic_registry_write(tmp_path.resolve(), state_path, malformed)

    receipt = GitBackupManager(str(tmp_path)).backup(
        1,
        allowlist=allowlist,
        authorization_token=token,
        decision_receipt=decision,
    )

    assert receipt["code"] == "git_backup_authorization_registry_failed"
    assert _git(tmp_path, "rev-parse", "--verify", "refs/tags/ch0001").returncode != 0


def test_backup_rejects_empty_and_escaping_allowlist(tmp_path):
    _init_repo(tmp_path)
    manager = GitBackupManager(str(tmp_path))

    assert manager.backup(1, allowlist=[])["code"] == "invalid_allowlist"
    assert manager.backup(1, allowlist=["../outside"])["code"] == "invalid_allowlist"


def test_backup_commit_tree_identity_failure_is_structured(tmp_path, monkeypatch):
    isolated_home = tmp_path / "home"
    isolated_home.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("HOME", str(isolated_home))
    monkeypatch.setenv("USERPROFILE", str(isolated_home))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    assert _git(project, "init", "-b", "main").returncode == 0
    assert _git(project, "config", "user.name", "Seed Author").returncode == 0
    assert _git(project, "config", "user.email", "seed@example.com").returncode == 0
    seed = project / "seed.txt"
    seed.write_text("seed", encoding="utf-8")
    assert _git(project, "add", "--", "seed.txt").returncode == 0
    assert _git(project, "commit", "-m", "seed").returncode == 0
    assert _git(project, "config", "--local", "user.useConfigOnly", "true").returncode == 0
    _git(project, "config", "--local", "--unset", "user.name")
    _git(project, "config", "--local", "--unset", "user.email")
    chapter = project / "正文" / "第0001章.md"
    chapter.parent.mkdir()
    chapter.write_text("正文", encoding="utf-8")
    allowlist = ["正文/第0001章.md"]
    decision = _decision_receipt(project, 1, allowlist, monkeypatch)

    receipt = GitBackupManager(str(project)).backup(
        1,
        allowlist=allowlist,
        authorization_token=build_git_backup_authorization_token(project, 1, allowlist),
        decision_receipt=decision,
    )

    assert receipt["ok"] is False
    assert receipt["code"] == "git_backup_failed"
    assert receipt["retryable"] is True
    state = read_git_backup_authorization_state(project, decision["receipt_sha256"])
    assert state["status"] == "failed-retryable"
    assert _git(project, "rev-parse", "--verify", "refs/tags/ch0001").returncode != 0


def test_commit_tree_failure_can_resume_with_same_decision_receipt(tmp_path, monkeypatch):
    _init_repo(tmp_path)
    chapter = tmp_path / "正文" / "第0001章.md"
    chapter.parent.mkdir()
    chapter.write_text("正文", encoding="utf-8")
    allowlist = ["正文/第0001章.md"]
    token = build_git_backup_authorization_token(tmp_path, 1, allowlist)
    decision = _decision_receipt(tmp_path, 1, allowlist, monkeypatch, thread="commit-retry")
    manager = GitBackupManager(str(tmp_path))
    real_run = manager._run_git_command
    failed = {"value": False}

    def fail_commit_once(args, check=True, **kwargs):
        if args and args[0] == "commit-tree" and not failed["value"]:
            failed["value"] = True
            return False, "", "injected commit-tree failure"
        return real_run(args, check=check, **kwargs)

    monkeypatch.setattr(manager, "_run_git_command", fail_commit_once)
    first = manager.backup(
        1,
        allowlist=allowlist,
        authorization_token=token,
        decision_receipt=decision,
    )
    assert first["status"] == "failed"
    assert first["retryable"] is True
    assert read_git_backup_authorization_state(tmp_path, decision["receipt_sha256"])["status"] == "failed-retryable"

    resumed = manager.backup(
        1,
        allowlist=allowlist,
        authorization_token=token,
        decision_receipt=decision,
    )
    assert resumed["status"] == "completed"
    assert _git(tmp_path, "rev-parse", "refs/tags/ch0001").stdout.strip() == resumed["commit"]


def test_tag_failure_resumes_pending_commit_without_recreating_it(tmp_path, monkeypatch):
    _init_repo(tmp_path)
    chapter = tmp_path / "正文" / "第0001章.md"
    chapter.parent.mkdir()
    chapter.write_text("正文", encoding="utf-8")
    allowlist = ["正文/第0001章.md"]
    token = build_git_backup_authorization_token(tmp_path, 1, allowlist)
    decision = _decision_receipt(tmp_path, 1, allowlist, monkeypatch, thread="tag-retry")
    manager = GitBackupManager(str(tmp_path))
    real_run = manager._run_git_command
    counts = {"tag": 0, "commit": 0}

    def fail_tag_once(args, check=True, **kwargs):
        if args and args[0] == "commit-tree":
            counts["commit"] += 1
        if args and args[0] == "update-ref":
            counts["tag"] += 1
            if counts["tag"] == 1:
                return False, "", "injected tag failure"
        return real_run(args, check=check, **kwargs)

    monkeypatch.setattr(manager, "_run_git_command", fail_tag_once)
    first = manager.backup(
        1,
        allowlist=allowlist,
        authorization_token=token,
        decision_receipt=decision,
    )
    state = read_git_backup_authorization_state(tmp_path, decision["receipt_sha256"])
    pending_commit = state["pending_result"]["commit"]
    assert first["status"] == "failed"
    assert state["status"] == "failed-retryable"

    resumed = manager.backup(
        1,
        allowlist=allowlist,
        authorization_token=token,
        decision_receipt=decision,
    )
    assert resumed["commit"] == pending_commit
    assert counts["commit"] == 1
    assert counts["tag"] == 2


def test_reparse_authorization_registry_writes_nothing_outside_project(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    _init_repo(project)
    chapter = project / "正文" / "第0001章.md"
    chapter.parent.mkdir()
    chapter.write_text("正文", encoding="utf-8")
    allowlist = ["正文/第0001章.md"]
    token = build_git_backup_authorization_token(project, 1, allowlist)
    decision = _decision_receipt(project, 1, allowlist, monkeypatch, thread="reparse")
    external = tmp_path / "external"
    external.mkdir()
    registry_link = project / ".webnovel"
    try:
        registry_link.symlink_to(external, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            pytest.skip("directory symlinks are unavailable on this host")
        junction = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(registry_link), str(external)],
            capture_output=True,
            text=True,
            check=False,
        )
        if junction.returncode != 0:
            pytest.skip("directory junctions are unavailable on this host")

    try:
        receipt = GitBackupManager(str(project)).backup(
            1,
            allowlist=allowlist,
            authorization_token=token,
            decision_receipt=decision,
        )
        external_entries = list(external.iterdir())
    finally:
        if registry_link.is_symlink():
            registry_link.unlink()
        elif registry_link.exists():
            os.rmdir(registry_link)

    assert receipt["code"] == "git_backup_authorization_registry_failed"
    assert external_entries == []
    assert _git(project, "rev-parse", "--verify", "refs/tags/ch0001").returncode != 0


def test_reparse_probe_is_fail_closed_on_lstat_error(tmp_path, monkeypatch):
    target = tmp_path / "leaf"
    target.write_text("x", encoding="utf-8")
    real_lstat = Path.lstat

    def fail_target_lstat(self):
        if self == target:
            raise OSError("injected metadata race")
        return real_lstat(self)

    monkeypatch.setattr(Path, "lstat", fail_target_lstat)

    assert backup_manager._is_reparse_point(target) is True


def test_bounded_control_read_rejects_metadata_change_during_open_handle(
    tmp_path, monkeypatch
):
    target = tmp_path / "receipt.json"
    target.write_text('{"ok":true}', encoding="utf-8")
    real_fstat = backup_manager.os.fstat
    calls = {"count": 0}

    def changed_second_fstat(fd):
        current = real_fstat(fd)
        calls["count"] += 1
        if calls["count"] != 2:
            return current
        return SimpleNamespace(
            st_mode=current.st_mode,
            st_dev=current.st_dev,
            st_ino=current.st_ino,
            st_size=current.st_size,
            st_mtime_ns=current.st_mtime_ns + 1,
        )

    monkeypatch.setattr(backup_manager.os, "fstat", changed_second_fstat)

    with pytest.raises(backup_manager.BackupError, match="changed during bounded read"):
        backup_manager._stable_read_bytes(
            target, trusted_root=tmp_path, max_bytes=backup_manager.MAX_DECISION_RECEIPT_BYTES
        )


def test_decision_receipt_leaf_symlink_is_rejected(tmp_path, monkeypatch):
    _init_repo(tmp_path)
    body = tmp_path / "正文" / "第0001章.md"
    body.parent.mkdir()
    body.write_text("正文", encoding="utf-8")
    allowlist = ["正文/第0001章.md"]
    decision = _decision_receipt(tmp_path, 1, allowlist, monkeypatch, thread="receipt-link")
    external = tmp_path / "external-receipt.json"
    external.write_text(json.dumps(decision, ensure_ascii=False), encoding="utf-8")
    receipt_link = tmp_path / "receipt-link.json"
    try:
        receipt_link.symlink_to(external)
    except OSError:
        pytest.skip("file symlinks are unavailable on this host")

    with pytest.raises(backup_manager.BackupError, match="reparse-point"):
        backup_manager.verify_git_backup_decision_receipt(
            tmp_path, 1, allowlist, receipt_link
        )


@pytest.mark.parametrize(
    "legacy_args",
    [
        ["--rollback", "1"],
        ["--create-branch", "1", "--branch-name", "alternate"],
    ],
)
def test_shared_cli_hard_rejects_legacy_git_mutations(tmp_path, legacy_args):
    _init_repo(tmp_path)
    state_dir = tmp_path / ".webnovel"
    state_dir.mkdir()
    (state_dir / "state.json").write_text("{}\n", encoding="utf-8")
    head_before = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
    status_before = _git(tmp_path, "status", "--porcelain=v1").stdout
    command = [
        sys.executable,
        str(Path(backup_manager.__file__).with_name("webnovel.py")),
        "backup",
        "--project-root",
        str(tmp_path),
        *legacy_args,
        "--format",
        "json",
    ]

    completed = subprocess.run(
        command,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )

    assert completed.returncode == 2
    assert "legacy_git_mutation_disabled" in completed.stdout
    assert _git(tmp_path, "rev-parse", "HEAD").stdout.strip() == head_before
    assert _git(tmp_path, "status", "--porcelain=v1").stdout == status_before
    assert _git(tmp_path, "branch", "--list", "alternate").stdout.strip() == ""


def test_backup_cli_help_marks_legacy_mutations_disabled():
    completed = subprocess.run(
        [sys.executable, backup_manager.__file__, "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0
    assert completed.stdout.count("旧入口（已禁用，不执行 Git 写）") == 2
    assert "# 回滚到第" not in completed.stdout
    assert "# 从第 50 章创建分支" not in completed.stdout
