from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import validate_codex_adapter
import validate_plugin_package


PLUGIN_ROOT = Path(__file__).resolve().parents[2]
SKILL = PLUGIN_ROOT / "skills" / "webnovel-learn" / "SKILL.md"
OPENAI_YAML = SKILL.parent / "agents" / "openai.yaml"


def test_learn_skill_metadata_and_controlled_write_contract():
    frontmatter = validate_plugin_package._frontmatter(SKILL)
    interface, error = validate_plugin_package._openai_interface(OPENAI_YAML)
    text = SKILL.read_text(encoding="utf-8")

    assert set(frontmatter) == {"name", "description"}
    assert frontmatter["name"] == "webnovel-learn"
    assert error == ""
    assert "$webnovel-learn" in interface["default_prompt"]
    assert "webnovel-learn-request/v1" in text
    assert "--input-json" in text
    assert "Never edit `.webnovel/project_memory.json` directly" in text
    assert "User text must never appear in a PowerShell command string" in text


def test_learn_skill_is_host_neutral():
    errors = validate_codex_adapter.scan_host_neutrality(PLUGIN_ROOT)
    assert [item for item in errors if item["path"].startswith("skills/webnovel-learn/")] == []


def _powershell_literal(value: Path) -> str:
    return "'" + str(value.resolve()).replace("'", "''") + "'"


def _powershell() -> str:
    executable = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    if executable is None:
        import pytest

        pytest.skip("PowerShell is required for the command-string boundary smoke")
    return executable


def test_learn_request_keeps_user_text_out_of_real_powershell_command(tmp_path):
    project = tmp_path / "中文 小说 (Learn) & 安全"
    webnovel = project / ".webnovel"
    webnovel.mkdir(parents=True)
    state = webnovel / "state.json"
    state.write_text('{"progress":{"current_chapter":7}}', encoding="utf-8")
    state_before = state.read_bytes()
    sentinel = tmp_path / "learn-shell-injection-sentinel"
    description = (
        "写法\"'\n"
        f"$(New-Item -ItemType File -LiteralPath '{sentinel}') &;|` 不应执行"
    )
    request = tmp_path / "learn request.json"
    request.write_text(
        json.dumps(
            {
                "schema_version": "webnovel-learn-request/v1",
                "pattern_type": "other",
                "description": description,
                "importance": "medium",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    command = " ".join(
        [
            "&",
            _powershell_literal(Path(sys.executable)),
            "-X utf8",
            _powershell_literal(PLUGIN_ROOT / "scripts" / "webnovel.py"),
            "--project-root",
            _powershell_literal(project),
            "project-memory add-pattern --input-json",
            _powershell_literal(request),
        ]
    )
    assert description not in command

    completed = subprocess.run(
        [_powershell(), "-NoProfile", "-NonInteractive", "-Command", command],
        shell=False,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    memory = json.loads((webnovel / "project_memory.json").read_text(encoding="utf-8"))
    assert payload["schema_version"] == "webnovel-learn-result/v1"
    assert payload["status"] == "success"
    assert memory["patterns"][0]["description"] == description
    assert not sentinel.exists()
    assert state.read_bytes() == state_before
