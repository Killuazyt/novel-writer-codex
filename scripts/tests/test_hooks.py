#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1].parent
HOOKS_JSON = PLUGIN_ROOT / "hooks" / "hooks.json"
GUARD = PLUGIN_ROOT / "hooks" / "guard_runtime_write.py"
SESSION_START = PLUGIN_ROOT / "hooks" / "session_start.py"


def _run_guard(payload: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(GUARD)],
        input=json.dumps(payload, ensure_ascii=False),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def test_hooks_json_uses_plugin_wrapper_and_plugin_root_paths():
    payload = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))

    assert "description" in payload
    assert "hooks" in payload
    assert "SessionStart" in payload["hooks"]
    assert "PreToolUse" in payload["hooks"]
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "${PLUGIN_ROOT}" in serialized
    assert "commandWindows" in serialized
    assert "apply_patch" in serialized
    assert "C:\\Users" not in serialized


def test_guard_blocks_direct_commit_file_write():
    proc = _run_guard(
        {
            "tool_name": "Write",
            "tool_input": {"file_path": r"D:\book\.story-system\commits\chapter_001.commit.json"},
        }
    )

    assert proc.returncode == 0
    decision = json.loads(proc.stdout)
    assert decision["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert decision["hookSpecificOutput"]["permissionDecisionReason"]


def test_guard_blocks_direct_state_and_summary_writes():
    for protected_path in (
        r"D:\book\.webnovel\state.json",
        r"D:\book\.webnovel\summaries\chapter_001.md",
    ):
        proc = _run_guard(
            {
                "tool_name": "Edit",
                "tool_input": {"file_path": protected_path},
            }
        )

        assert proc.returncode == 0
        assert json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_guard_blocks_bash_state_write():
    proc = _run_guard(
        {
            "tool_name": "Bash",
            "tool_input": {"command": 'python fix_state.py > "D:/book/.webnovel/state.json"'},
        }
    )

    assert proc.returncode == 0
    assert json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_guard_still_blocks_index_db_write():
    proc = _run_guard(
        {
            "tool_name": "Edit",
            "tool_input": {"file_path": r"D:\book\.webnovel\index.db"},
        }
    )

    assert proc.returncode == 0
    assert json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_guard_blocks_codex_apply_patch_to_projection_log():
    proc = _run_guard(
        {
            "tool_name": "apply_patch",
            "tool_input": {
                "command": "*** Begin Patch\n*** Update File: book/.webnovel/projection_log.jsonl\n@@\n-old\n+new\n*** End Patch"
            },
        }
    )

    assert proc.returncode == 0
    decision = json.loads(proc.stdout)
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "apply_patch" in decision["hookSpecificOutput"]["permissionDecisionReason"]


def test_guard_blocks_codex_apply_patch_move_to_protected_path():
    proc = _run_guard(
        {
            "tool_name": "apply_patch",
            "tool_input": {
                "command": "*** Begin Patch\n*** Update File: notes.db\n*** Move to: .webnovel/index.db\n*** End Patch"
            },
        }
    )

    assert proc.returncode == 0
    assert json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_guard_allows_codex_apply_patch_to_normal_file():
    proc = _run_guard(
        {
            "tool_name": "apply_patch",
            "tool_input": {
                "command": "*** Begin Patch\n*** Update File: README.md\n@@\n-old\n+new\n*** End Patch"
            },
        }
    )

    assert proc.returncode == 0
    assert proc.stdout == ""


@pytest.mark.parametrize(
    "command",
    [
        "python scripts/webnovel.py chapter-commit --chapter 3; Set-Content .webnovel/index.db bad",
        "# webnovel.py chapter-commit\nSet-Content .webnovel/projection_log.jsonl bad",
        "Remove-Item .webnovel/index.db",
        "sqlite3 .webnovel/index.db 'delete from chunks'",
        "[IO.File]::WriteAllText('.webnovel/memory_scratchpad.json', '{}')",
    ],
)
def test_guard_blocks_any_shell_reference_to_protected_paths(command):
    proc = _run_guard({"tool_name": "Bash", "tool_input": {"command": command}})

    assert proc.returncode == 0
    assert json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_guard_allows_runtime_projection_command():
    proc = _run_guard(
        {
            "tool_name": "Bash",
            "tool_input": {
                "command": 'python -X utf8 "${SCRIPTS_DIR}/webnovel.py" --project-root "${PROJECT_ROOT}" projections retry --chapter 3'
            },
        }
    )

    assert proc.returncode == 0


def test_guard_blocks_direct_chapter_commit_script_bypass():
    proc = _run_guard(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "python scripts/chapter_commit.py --project-root book --chapter 3"},
        }
    )

    assert proc.returncode == 0
    assert json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_session_start_can_be_disabled(monkeypatch):
    monkeypatch.setenv("WEBNOVEL_DISABLE_SESSION_STATUS_HOOK", "1")
    proc = subprocess.run(
        [sys.executable, str(SESSION_START)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert proc.returncode == 0
    assert proc.stdout == ""


def test_session_start_is_silent_outside_webnovel_project(tmp_path):
    env = os.environ.copy()
    for name in (
        "WEBNOVEL_DISABLE_SESSION_STATUS_HOOK",
        "WEBNOVEL_PROJECT_ROOT",
        "CLAUDE_PROJECT_DIR",
    ):
        env.pop(name, None)

    proc = subprocess.run(
        [sys.executable, str(SESSION_START)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert proc.returncode == 0
    assert proc.stdout == ""
