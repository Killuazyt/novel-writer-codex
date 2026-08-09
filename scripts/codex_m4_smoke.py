#!/usr/bin/env python3
"""Bounded, isolated Windows/POSIX smoke for the M4 read-only alpha."""

from __future__ import annotations

import http.client
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
ENTRY = PLUGIN_ROOT / "scripts" / "webnovel.py"
SCRIPTS = PLUGIN_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from data_modules.codex_agent_runtime import snapshot_protected_state  # noqa: E402


class SmokeFailure(RuntimeError):
    pass


def _run_cli(project: Path, env: dict[str, str], *args: str) -> tuple[int, dict[str, Any]]:
    completed = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            str(ENTRY),
            "--project-root",
            str(project),
            *args,
            "--format",
            "json",
        ],
        cwd=str(PLUGIN_ROOT),
        env=env,
        shell=False,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise SmokeFailure(
            f"CLI did not return JSON (code={completed.returncode}): {completed.stdout!r} {completed.stderr!r}"
        ) from exc
    return int(completed.returncode), payload


def _get(host: str, port: int, path: str) -> tuple[int, dict[str, Any]]:
    connection = http.client.HTTPConnection(host, port, timeout=3)
    try:
        connection.request("GET", path, headers={"Connection": "close"})
        response = connection.getresponse()
        raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise SmokeFailure(f"response too large: {path}")
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise SmokeFailure(f"response is not an object: {path}")
        return int(response.status), payload
    finally:
        connection.close()


def _owned_process_cleanup(pid: int) -> None:
    if pid <= 0:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return


def _cleanup_owned_dashboard(project: Path, env: dict[str, str], pid: int) -> None:
    cleanly_stopped = False
    try:
        code, stopped = _run_cli(project, env, "dashboard", "stop")
        if code == 0 and stopped.get("status") == "stopped":
            status_code, status = _run_cli(project, env, "dashboard", "status")
            cleanly_stopped = status_code == 0 and status.get("status") == "not_running"
    except Exception:
        cleanly_stopped = False
    if not cleanly_stopped:
        _owned_process_cleanup(pid)


def run_smoke() -> dict[str, Any]:
    smoke_root = Path(tempfile.mkdtemp(prefix="webnovel-m4-smoke-"))
    project = smoke_root / "中文 小说 (M4) & loopback"
    webnovel = project / ".webnovel"
    webnovel.mkdir(parents=True)
    for folder in ("正文", "设定集", "大纲"):
        (project / folder).mkdir()
    (project / "正文" / "第0001章.md").write_text("只读 smoke\n", encoding="utf-8")
    (webnovel / "state.json").write_text(
        json.dumps(
            {
                "project_info": {"title": "M4 中文 Smoke", "genre": "玄幻"},
                "progress": {"current_chapter": 0},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    isolated_home = smoke_root / "isolated webnovel home"
    env = os.environ.copy()
    env["WEBNOVEL_HOME"] = str(isolated_home)
    env["PYTHONUTF8"] = "1"
    before = snapshot_protected_state(project)
    owned_pid = 0
    stopped = False
    try:
        code, started = _run_cli(
            project,
            env,
            "dashboard",
            "start",
            "--host",
            "127.0.0.1",
            "--port",
            "0",
            "--no-browser",
        )
        if code != 0 or started.get("status") != "running":
            raise SmokeFailure(f"dashboard start failed: code={code}, result={started}")
        owned_pid = int(started.get("pid") or 0)
        port = int(started.get("port") or 0)
        if not owned_pid or not 1 <= port <= 65535:
            raise SmokeFailure(f"invalid start identity: {started}")
        if "instance_id" in started:
            raise SmokeFailure("private instance token leaked in CLI output")
        log_path = Path(str(started.get("log_path") or ""))
        try:
            log_path.resolve().relative_to(isolated_home.resolve())
        except ValueError as exc:
            raise SmokeFailure("dashboard log escaped isolated WEBNOVEL_HOME") from exc

        code, status = _run_cli(project, env, "dashboard", "status")
        if code != 0 or status.get("status") != "running":
            raise SmokeFailure(f"dashboard status failed: code={code}, result={status}")
        for key in ("pid", "port", "project_root"):
            if status.get(key) != started.get(key):
                raise SmokeFailure(f"status identity changed for {key}")

        project_status, project_info = _get("127.0.0.1", port, "/api/project/info")
        health_status, health = _get("127.0.0.1", port, "/api/story-runtime/health")
        traversal_status, _ = _get(
            "127.0.0.1", port, "/api/files/read?path=%2E%2E%2Foutside.txt"
        )
        if project_status != 200 or project_info.get("project_info", {}).get("title") != "M4 中文 Smoke":
            raise SmokeFailure(f"project/info failed: {project_status}, {project_info}")
        if health_status != 200 or "mainline_ready" not in health:
            raise SmokeFailure(f"story-runtime/health failed: {health_status}, {health}")
        if traversal_status != 403:
            raise SmokeFailure(f"path traversal returned {traversal_status}, expected 403")

        code, stop = _run_cli(project, env, "dashboard", "stop")
        if code != 0 or stop.get("status") != "stopped":
            raise SmokeFailure(f"dashboard stop failed: code={code}, result={stop}")
        stopped = True
        code, final_status = _run_cli(project, env, "dashboard", "status")
        if code != 0 or final_status.get("status") != "not_running":
            raise SmokeFailure(f"final status failed: code={code}, result={final_status}")
        if snapshot_protected_state(project) != before:
            raise SmokeFailure("protected novel facts changed during Dashboard lifecycle")

        return {
            "schema_version": "webnovel-m4-smoke/v1",
            "ok": True,
            "platform": sys.platform,
            "project_path_features": ["Chinese", "spaces", "parentheses", "ampersand"],
            "dashboard": {
                "start": "running",
                "status": "running",
                "dynamic_port": True,
                "project_info_http": project_status,
                "story_runtime_health_http": health_status,
                "path_traversal_http": traversal_status,
                "stop": "stopped",
                "final_status": "not_running",
                "no_browser": True,
                "windows_hidden_process": os.name == "nt",
            },
            "protected_state_unchanged": True,
            "runtime_outside_project": True,
        }
    finally:
        if owned_pid and not stopped:
            _cleanup_owned_dashboard(project, env, owned_pid)
        shutil.rmtree(smoke_root, ignore_errors=True)


def main() -> None:
    try:
        result = run_smoke()
    except Exception as exc:
        print(
            json.dumps(
                {
                    "schema_version": "webnovel-m4-smoke/v1",
                    "ok": False,
                    "error": str(exc),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
