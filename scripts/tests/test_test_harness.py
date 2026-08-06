from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from test_isolation import NetworkAccessDisabled, child_isolation_active
from test_state_guard import compare_snapshot, create_snapshot, read_snapshot, write_snapshot


REPO_ROOT = Path(__file__).resolve().parents[2]
STATE_GUARD = REPO_ROOT / "scripts" / "test_state_guard.py"


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def test_pytest_process_uses_one_isolated_host_tree(isolated_homes):
    session_root = isolated_homes["session_root"]
    assert child_isolation_active() is True
    assert Path.home().resolve() == isolated_homes["home"].resolve()

    expected = {
        "HOME": isolated_homes["home"],
        "USERPROFILE": isolated_homes["home"],
        "CODEX_HOME": isolated_homes["codex_home"],
        "WEBNOVEL_HOME": isolated_homes["webnovel_home"],
        "CLAUDE_HOME": isolated_homes["claude_home"],
        "WEBNOVEL_CLAUDE_HOME": isolated_homes["claude_home"],
        "TMP": isolated_homes["tmp"],
        "TEMP": isolated_homes["tmp"],
        "TMPDIR": isolated_homes["tmp"],
    }
    for name, wanted in expected.items():
        actual = Path(os.environ[name])
        assert actual.resolve() == wanted.resolve(), name
        assert _inside(actual, session_root), name

    for name in (
        "APPDATA",
        "LOCALAPPDATA",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "GIT_CONFIG_GLOBAL",
        "COVERAGE_FILE",
    ):
        assert _inside(Path(os.environ[name]), session_root), name
    assert os.environ["GIT_CONFIG_NOSYSTEM"] == "1"
    assert os.environ["PYTHONUTF8"] == "1"
    assert os.environ["PYTHONIOENCODING"].casefold() == "utf-8"


def test_custom_tmp_path_leaves_room_for_nested_windows_git_objects(tmp_path):
    # Git appends ``project/.git/objects/xx/<38 hex>`` below this directory.
    # Keep the base comfortably below the legacy 260-character boundary.
    assert len(str(tmp_path.resolve())) <= 180


def test_early_isolation_clears_inherited_pointers_and_model_secrets(tmp_path):
    probe = tmp_path / "test_poisoned_environment.py"
    probe.write_text(
        """
import os

def test_environment_is_clean():
    forbidden = {
        "WEBNOVEL_PROJECT_ROOT",
        "CLAUDE_PROJECT_DIR",
        "WEBNOVEL_PLUGIN_ROOT",
        "PLUGIN_ROOT",
        "CLAUDE_PLUGIN_ROOT",
        "EMBED_API_KEY",
        "EMBED_BASE_URL",
        "RERANK_API_KEY",
        "RERANK_BASE_URL",
    }
    assert forbidden.isdisjoint(os.environ)
""".lstrip(),
        encoding="utf-8",
    )
    env = os.environ.copy()
    for name in (
        "WEBNOVEL_PROJECT_ROOT",
        "CLAUDE_PROJECT_DIR",
        "WEBNOVEL_PLUGIN_ROOT",
        "PLUGIN_ROOT",
        "CLAUDE_PLUGIN_ROOT",
        "EMBED_API_KEY",
        "EMBED_BASE_URL",
        "RERANK_API_KEY",
        "RERANK_BASE_URL",
    ):
        env[name] = "must-not-survive"

    result = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            "-m",
            "pytest",
            "-q",
            str(probe),
            "-o",
            "addopts=",
            "-p",
            "no:cacheprovider",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_network_guard_blocks_ipv4_ipv6_entry_points():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
        with pytest.raises(NetworkAccessDisabled):
            client.connect(("127.0.0.1", 9))
        with pytest.raises(NetworkAccessDisabled):
            client.connect_ex(("127.0.0.1", 9))

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        with pytest.raises(NetworkAccessDisabled):
            server.bind(("127.0.0.1", 0))

    with pytest.raises(NetworkAccessDisabled):
        socket.create_connection(("127.0.0.1", 9), timeout=0.01)
    with pytest.raises(NetworkAccessDisabled):
        socket.getaddrinfo("localhost", 80)


def test_network_guard_is_inherited_by_child_python():
    code = """
import json
import os
import socket
from pathlib import Path

blocked = False
try:
    socket.create_connection(("127.0.0.1", 9), timeout=0.01)
except RuntimeError:
    blocked = True

print(json.dumps({
    "blocked": blocked,
    "home": str(Path.home()),
    "session": os.environ.get("WEBNOVEL_TEST_SESSION_ROOT"),
}))
"""
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", code],
        cwd=REPO_ROOT,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["blocked"] is True
    assert Path(payload["home"]).resolve() == Path(os.environ["HOME"]).resolve()
    assert Path(payload["session"]).resolve() == Path(
        os.environ["WEBNOVEL_TEST_SESSION_ROOT"]
    ).resolve()


def test_socketpair_remains_available_for_in_process_ipc():
    left, right = socket.socketpair()
    try:
        left.sendall(b"ok")
        assert right.recv(2) == b"ok"
    finally:
        left.close()
        right.close()


def test_state_guard_detects_content_create_and_delete(tmp_path):
    existing = tmp_path / "existing.json"
    created = tmp_path / "created.json"
    existing.write_text('{"value": 1}', encoding="utf-8")
    snapshot = create_snapshot([existing, created])
    assert compare_snapshot(snapshot) == []

    existing.write_text('{"value": 2}', encoding="utf-8")
    created.write_text("new", encoding="utf-8")
    changes = compare_snapshot(snapshot)
    assert {Path(change["path"]).name for change in changes} == {
        "existing.json",
        "created.json",
    }

    replacement_snapshot = create_snapshot([existing])
    existing.unlink()
    changes = compare_snapshot(replacement_snapshot)
    assert len(changes) == 1
    assert changes[0]["before"]["exists"] is True
    assert changes[0]["after"]["exists"] is False


def test_state_guard_cli_exit_codes_and_json(tmp_path):
    protected = tmp_path / "protected.toml"
    snapshot_path = tmp_path / "snapshot.json"
    protected.write_text("value = 1\n", encoding="utf-8")

    snapshot = subprocess.run(
        [
            sys.executable,
            "-S",
            "-X",
            "utf8",
            str(STATE_GUARD),
            "snapshot",
            "--output",
            str(snapshot_path),
            "--path",
            str(protected),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert snapshot.returncode == 0
    assert json.loads(snapshot.stdout)["ok"] is True
    assert read_snapshot(snapshot_path)["files"][0]["sha256"]

    unchanged = subprocess.run(
        [
            sys.executable,
            "-S",
            "-X",
            "utf8",
            str(STATE_GUARD),
            "verify",
            "--input",
            str(snapshot_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert unchanged.returncode == 0
    assert json.loads(unchanged.stdout)["changes"] == []

    protected.write_text("value = 2\n", encoding="utf-8")
    changed = subprocess.run(
        [
            sys.executable,
            "-S",
            "-X",
            "utf8",
            str(STATE_GUARD),
            "verify",
            "--input",
            str(snapshot_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert changed.returncode == 1
    assert json.loads(changed.stdout)["changes"][0]["path"] == str(
        protected.resolve()
    )


def test_session_snapshot_excludes_auth_session_and_cache_files(isolated_homes):
    snapshot = read_snapshot(isolated_homes["snapshot"])
    protected = [Path(item["path"]).as_posix().casefold() for item in snapshot["files"]]
    assert protected
    assert not any("auth" in path or "session" in path or "/cache/" in path for path in protected)


def test_harness_contract_has_one_primary_marker_and_30_second_timeout(request):
    primary = {
        name
        for name in ("runtime", "codex_contract", "upstream_contract")
        if any(request.node.iter_markers(name=name))
    }
    assert primary == {"codex_contract"}
    assert request.config.getini("timeout") == "30"
    assert request.config.getini("timeout_method") == "thread"
    assert os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    assert request.config.pluginmanager.get_plugin("anyio") is None
