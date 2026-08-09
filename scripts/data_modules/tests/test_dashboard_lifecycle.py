from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "中文 小说 (A&B)"
    (root / ".webnovel").mkdir(parents=True)
    (root / ".webnovel" / "state.json").write_text("{}", encoding="utf-8")
    return root.resolve()


def _running_state(module, root: Path, *, pid: int = 3210, port: int = 43210) -> dict:
    _, digest = module.project_identity(root)
    return {
        "schema_version": module.STATE_SCHEMA,
        "status": "running",
        "project_root": str(root),
        "project_hash": digest,
        "instance_id": "a" * 32,
        "pid": pid,
        "host": module.LOOPBACK_HOST,
        "port": port,
        "started_at": "2026-08-07T00:00:00+00:00",
    }


def test_status_does_not_create_runtime_directory(monkeypatch, tmp_path):
    from data_modules import dashboard_lifecycle as lifecycle

    root = _project(tmp_path)
    home = tmp_path / "isolated home"
    monkeypatch.setenv("WEBNOVEL_HOME", str(home))

    result = lifecycle.dashboard_status(root)

    assert result["status"] == "not_running"
    assert result["ok"] is True
    assert not home.exists()


def test_corrupt_or_untrusted_state_never_signals_a_pid(monkeypatch, tmp_path):
    from data_modules import dashboard_lifecycle as lifecycle

    root = _project(tmp_path)
    monkeypatch.setenv("WEBNOVEL_HOME", str(tmp_path / "runtime home"))
    paths = lifecycle.dashboard_runtime_paths(root)
    paths.runtime_dir.mkdir(parents=True)
    paths.state_file.write_text(
        json.dumps(
            {
                "schema_version": lifecycle.STATE_SCHEMA,
                "status": "running",
                "project_root": str(root),
                "project_hash": "wrong-project",
                "instance_id": "b" * 32,
                "pid": os.getpid(),
                "host": "203.0.113.9",
                "port": 80,
            }
        ),
        encoding="utf-8",
    )
    signals: list[int] = []
    monkeypatch.setattr(lifecycle, "_signal_verified_pid", lambda pid: signals.append(pid))

    status = lifecycle.dashboard_status(root)
    stopped = lifecycle.dashboard_stop(root)

    assert status["status"] == "failed"
    assert status["errors"][0]["code"] == "dashboard_state_untrusted"
    assert stopped["status"] == "failed"
    assert signals == []


def test_live_pid_with_identity_mismatch_fails_closed(monkeypatch, tmp_path):
    from data_modules import dashboard_lifecycle as lifecycle

    root = _project(tmp_path)
    monkeypatch.setenv("WEBNOVEL_HOME", str(tmp_path / "runtime home"))
    paths = lifecycle.dashboard_runtime_paths(root)
    paths.runtime_dir.mkdir(parents=True)
    paths.state_file.write_text(
        json.dumps(_running_state(lifecycle, root)), encoding="utf-8"
    )
    monkeypatch.setattr(lifecycle, "_probe_running_instance", lambda *args: (False, "token mismatch"))
    monkeypatch.setattr(lifecycle, "_pid_alive", lambda pid: True)
    signals: list[int] = []
    monkeypatch.setattr(lifecycle, "_signal_verified_pid", lambda pid: signals.append(pid))

    result = lifecycle.dashboard_stop(root)

    assert result["status"] == "blocked"
    assert result["errors"][0]["code"] == "dashboard_instance_unverified"
    assert signals == []


def test_stopped_status_cannot_hide_a_verified_live_instance(monkeypatch, tmp_path):
    from data_modules import dashboard_lifecycle as lifecycle

    root = _project(tmp_path)
    monkeypatch.setenv("WEBNOVEL_HOME", str(tmp_path / "runtime home"))
    paths = lifecycle.dashboard_runtime_paths(root)
    paths.runtime_dir.mkdir(parents=True)
    state = _running_state(lifecycle, root)
    state["status"] = "stopped"
    paths.state_file.write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(lifecycle, "_probe_running_instance", lambda *args: (True, ""))

    result = lifecycle.dashboard_status(root)

    assert result["status"] == "running"
    assert result["pid"] == state["pid"]


def test_start_uses_child_bound_port_and_persists_private_identity(monkeypatch, tmp_path):
    from data_modules import dashboard_lifecycle as lifecycle

    root = _project(tmp_path)
    monkeypatch.setenv("WEBNOVEL_HOME", str(tmp_path / "runtime home"))
    monkeypatch.setattr(lifecycle, "_preflight", lambda *args: None)
    monkeypatch.setattr(lifecycle, "_probe_running_instance", lambda *args: (True, ""))

    class FakeProcess:
        pid = 4567
        returncode = None

        def poll(self):
            return None

        def terminate(self):
            self.returncode = 0

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.returncode = -9

    def fake_spawn(project, digest, paths, *, host, port, instance_id):
        assert port == 0
        paths.ready_file.write_text(
            json.dumps(
                {
                    "schema_version": lifecycle.READY_SCHEMA,
                    "status": "ready",
                    "project_root": str(root),
                    "project_hash": digest,
                    "instance_id": instance_id,
                    "pid": FakeProcess.pid,
                    "host": lifecycle.LOOPBACK_HOST,
                    "port": 54321,
                }
            ),
            encoding="utf-8",
        )
        return FakeProcess()

    monkeypatch.setattr(lifecycle, "_spawn_dashboard", fake_spawn)

    result = lifecycle.dashboard_start(root, port=0, startup_timeout=0.5)
    paths = lifecycle.dashboard_runtime_paths(root)
    state = json.loads(paths.state_file.read_text(encoding="utf-8"))

    assert result["status"] == "running"
    assert result["port"] == 54321
    assert result["url"] == "http://127.0.0.1:54321"
    assert "instance_id" not in result
    assert state["instance_id"]
    assert state["project_hash"] == lifecycle.project_identity(root)[1]
    assert "url" not in state


def test_stop_is_idempotent_and_only_signals_verified_instance(monkeypatch, tmp_path):
    from data_modules import dashboard_lifecycle as lifecycle

    root = _project(tmp_path)
    monkeypatch.setenv("WEBNOVEL_HOME", str(tmp_path / "runtime home"))
    paths = lifecycle.dashboard_runtime_paths(root)
    paths.runtime_dir.mkdir(parents=True)
    paths.state_file.write_text(
        json.dumps(_running_state(lifecycle, root, pid=7654)), encoding="utf-8"
    )
    monkeypatch.setattr(
        lifecycle,
        "_probe_running_instance",
        lambda state, *args: (state.get("status") == "running", "stopped"),
    )
    monkeypatch.setattr(lifecycle, "_pid_alive", lambda pid: False)
    signals: list[int] = []
    monkeypatch.setattr(lifecycle, "_signal_verified_pid", lambda pid: signals.append(pid))

    first = lifecycle.dashboard_stop(root, shutdown_timeout=0.1)
    second = lifecycle.dashboard_stop(root, shutdown_timeout=0.1)

    assert first["status"] == "stopped"
    assert signals == [7654]
    assert second["status"] == "stopped"
    assert second["already_stopped"] is True


def test_spawn_uses_argv_shell_false_and_platform_isolation(monkeypatch, tmp_path):
    from data_modules import dashboard_lifecycle as lifecycle

    root = _project(tmp_path)
    monkeypatch.setenv("WEBNOVEL_HOME", str(tmp_path / "runtime home"))
    paths = lifecycle.dashboard_runtime_paths(root)
    paths.runtime_dir.mkdir(parents=True)
    _, digest = lifecycle.project_identity(root)
    captured: dict = {}

    class FakeProcess:
        pid = 9876

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    lifecycle._spawn_dashboard(
        root,
        digest,
        paths,
        host=lifecycle.LOOPBACK_HOST,
        port=0,
        instance_id="c" * 32,
    )

    assert isinstance(captured["argv"], list)
    assert captured["kwargs"]["shell"] is False
    assert captured["argv"][captured["argv"].index("--port") + 1] == "0"
    assert captured["argv"][captured["argv"].index("--project-root") + 1] == str(root)
    if os.name == "nt":
        assert captured["kwargs"]["creationflags"] & subprocess.CREATE_NO_WINDOW
        assert captured["kwargs"]["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert captured["kwargs"]["start_new_session"] is True


def test_preflight_reports_missing_assets_without_spawning(monkeypatch, tmp_path):
    from data_modules import dashboard_lifecycle as lifecycle

    root = _project(tmp_path)
    monkeypatch.setenv("WEBNOVEL_HOME", str(tmp_path / "runtime home"))
    empty_plugin = tmp_path / "empty plugin"
    empty_plugin.mkdir()
    monkeypatch.setattr(lifecycle, "resolve_plugin_root", lambda anchor: empty_plugin)
    spawned: list[bool] = []
    monkeypatch.setattr(lifecycle, "_spawn_dashboard", lambda *a, **k: spawned.append(True))

    result = lifecycle.dashboard_start(root)

    assert result["status"] == "blocked"
    assert result["errors"][0]["code"] == "dashboard_frontend_missing"
    assert spawned == []


def test_host_port_and_state_schema_validation_fail_closed(monkeypatch, tmp_path):
    from data_modules import dashboard_lifecycle as lifecycle

    root = _project(tmp_path)
    monkeypatch.setenv("WEBNOVEL_HOME", str(tmp_path / "runtime home"))
    _, digest = lifecycle.project_identity(root)
    assert lifecycle.normalize_dashboard_host("localhost") == lifecycle.LOOPBACK_HOST
    with pytest.raises(ValueError):
        lifecycle.normalize_dashboard_host("0.0.0.0")

    base = _running_state(lifecycle, root)
    mutations = [
        {"schema_version": "wrong"},
        {"project_hash": "wrong"},
        {"project_root": str(tmp_path / "other")},
        {"status": "paused"},
        {"host": "localhost"},
        {"instance_id": "bad"},
        {"pid": True},
        {"port": 0},
    ]
    for mutation in mutations:
        candidate = {**base, **mutation}
        valid, detail = lifecycle._validate_state(candidate, root, digest)
        assert valid is False
        assert detail

    forbidden_host = lifecycle.dashboard_start(root, host="192.0.2.1")
    invalid_port = lifecycle.dashboard_start(root, port=70000)
    assert forbidden_host["errors"][0]["code"] == "dashboard_host_forbidden"
    assert invalid_port["errors"][0]["code"] == "dashboard_port_invalid"


def test_control_probe_uses_bounded_json_http_connection(monkeypatch):
    from data_modules import dashboard_lifecycle as lifecycle

    events = []

    class FakeResponse:
        status = 200

        def read(self, limit):
            events.append(("read", limit))
            return b'{"ok": true}'

    class FakeConnection:
        def __init__(self, host, port, timeout):
            events.append(("init", host, port, timeout))

        def request(self, method, path, headers):
            events.append(("request", method, path, headers))

        def getresponse(self):
            return FakeResponse()

        def close(self):
            events.append(("close",))

    monkeypatch.setattr(lifecycle.http.client, "HTTPConnection", FakeConnection)

    status, payload = lifecycle._probe_endpoint("127.0.0.1", 43210, "/health")

    assert status == 200
    assert payload == {"ok": True}
    assert events[0][:3] == ("init", "127.0.0.1", 43210)
    assert events[-1] == ("close",)


def test_status_marks_dead_verified_record_stale(monkeypatch, tmp_path):
    from data_modules import dashboard_lifecycle as lifecycle

    root = _project(tmp_path)
    monkeypatch.setenv("WEBNOVEL_HOME", str(tmp_path / "runtime home"))
    paths = lifecycle.dashboard_runtime_paths(root)
    paths.runtime_dir.mkdir(parents=True)
    paths.state_file.write_text(json.dumps(_running_state(lifecycle, root)), encoding="utf-8")
    monkeypatch.setattr(lifecycle, "_probe_running_instance", lambda *args: (False, "refused"))
    monkeypatch.setattr(lifecycle, "_pid_alive", lambda pid: False)

    result = lifecycle.dashboard_status(root)

    assert result["status"] == "not_running"
    assert result["stale"] is True
    assert result["detail"] == "refused"


def test_dependency_preflight_is_structured_and_never_installs(monkeypatch, tmp_path):
    from data_modules import dashboard_lifecycle as lifecycle

    root = _project(tmp_path)
    monkeypatch.setenv("WEBNOVEL_HOME", str(tmp_path / "runtime home"))
    plugin = tmp_path / "plugin"
    assets = plugin / "dashboard" / "frontend" / "dist" / "assets"
    assets.mkdir(parents=True)
    (assets.parent / "index.html").write_text("<html></html>", encoding="utf-8")
    (assets / "app.js").write_text("", encoding="utf-8")
    (plugin / "dashboard" / "requirements.txt").write_text("watchdog\n", encoding="utf-8")
    monkeypatch.setattr(lifecycle, "resolve_plugin_root", lambda anchor: plugin)
    real_find_spec = lifecycle.importlib.util.find_spec
    monkeypatch.setattr(
        lifecycle.importlib.util,
        "find_spec",
        lambda name: None if name == "watchdog" else real_find_spec(name),
    )

    result = lifecycle._preflight(root, lifecycle.dashboard_runtime_paths(root))

    assert result is not None
    assert result["errors"][0]["code"] == "dashboard_dependency_missing"
    assert "requirements.txt" in result["errors"][0]["repair"]


def test_missing_filelock_keeps_start_and_stop_errors_structured(monkeypatch, tmp_path):
    from data_modules import dashboard_lifecycle as lifecycle

    root = _project(tmp_path)
    monkeypatch.setenv("WEBNOVEL_HOME", str(tmp_path / "runtime home"))
    plugin = tmp_path / "plugin"
    assets = plugin / "dashboard" / "frontend" / "dist" / "assets"
    assets.mkdir(parents=True)
    (assets.parent / "index.html").write_text("<html></html>", encoding="utf-8")
    (assets / "app.js").write_text("", encoding="utf-8")
    (plugin / "dashboard" / "requirements.txt").write_text("filelock\n", encoding="utf-8")
    monkeypatch.setattr(lifecycle, "resolve_plugin_root", lambda anchor: plugin)
    monkeypatch.setattr(lifecycle, "FileLock", None)

    start_result = lifecycle._preflight(root, lifecycle.dashboard_runtime_paths(root))
    paths = lifecycle.dashboard_runtime_paths(root)
    paths.runtime_dir.mkdir(parents=True)
    stop_result = lifecycle.dashboard_stop(root)

    assert start_result is not None
    assert start_result["errors"][0]["code"] == "dashboard_dependency_missing"
    assert "filelock" in start_result["errors"][0]["message"]
    assert "scripts" in start_result["errors"][0]["repair"]
    assert "requirements.txt" in start_result["errors"][0]["repair"]
    assert stop_result["status"] == "blocked"
    assert stop_result["errors"][0]["code"] == "dashboard_dependency_missing"
    assert "scripts/requirements.txt" in stop_result["errors"][0]["repair"]


def test_stop_revalidates_state_after_status_before_signalling(monkeypatch, tmp_path):
    from data_modules import dashboard_lifecycle as lifecycle

    root = _project(tmp_path)
    monkeypatch.setenv("WEBNOVEL_HOME", str(tmp_path / "runtime home"))
    paths = lifecycle.dashboard_runtime_paths(root)
    paths.runtime_dir.mkdir(parents=True)
    trusted = _running_state(lifecycle, root, pid=7654)
    paths.state_file.write_text(json.dumps(trusted), encoding="utf-8")
    current = lifecycle._public_running_result(root, paths, trusted)
    malicious = {**trusted, "project_hash": "wrong", "pid": os.getpid()}

    def _swap_state(_project_root):
        paths.state_file.write_text(json.dumps(malicious), encoding="utf-8")
        return current

    monkeypatch.setattr(lifecycle, "dashboard_status", _swap_state)
    signals: list[int] = []
    monkeypatch.setattr(lifecycle, "_signal_verified_pid", lambda pid: signals.append(pid))

    result = lifecycle.dashboard_stop(root)

    assert result["status"] == "failed"
    assert result["errors"][0]["code"] == "dashboard_state_untrusted"
    assert signals == []


def test_lifecycle_result_formats_and_exit_codes(tmp_path):
    from data_modules import dashboard_lifecycle as lifecycle

    root = _project(tmp_path)
    paths = lifecycle.dashboard_runtime_paths(root)
    blocked = lifecycle._error_result(
        root,
        paths,
        status="blocked",
        code="blocked_example",
        message="blocked",
        repair="repair manually",
    )
    failed = lifecycle._error_result(
        root, paths, status="failed", code="failed_example", message="failed"
    )
    success = {**blocked, "status": "not_running", "ok": True, "errors": []}

    assert lifecycle.dashboard_exit_code(success) == 0
    assert lifecycle.dashboard_exit_code(blocked) == 1
    assert lifecycle.dashboard_exit_code(failed) == 2
    assert json.loads(lifecycle.format_dashboard_result(blocked, "json"))["status"] == "blocked"
    text = lifecycle.format_dashboard_result(blocked, "text")
    assert "BLOCKER blocked_example" in text
    assert "repair manually" in text


@pytest.mark.skipif(os.name != "nt", reason="Windows process-handle implementation")
def test_windows_pid_query_detects_current_and_missing_process():
    from data_modules import dashboard_lifecycle as lifecycle

    assert lifecycle._pid_alive(os.getpid()) is True
    assert lifecycle._pid_alive(0x7FFFFFFF) is False
