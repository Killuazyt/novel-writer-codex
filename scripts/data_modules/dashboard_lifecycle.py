#!/usr/bin/env python3
"""Safe lifecycle controller for the local, read-only Dashboard service."""

from __future__ import annotations

import hashlib
import http.client
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from filelock import FileLock, Timeout
except ImportError:  # Keep dependency preflight reachable and structured.
    FileLock = None  # type: ignore[assignment]

    class Timeout(Exception):
        pass

from host_paths import resolve_plugin_root, resolve_webnovel_home
from security_utils import atomic_write_json


STATE_SCHEMA = "webnovel-dashboard-state/v1"
READY_SCHEMA = "webnovel-dashboard-ready/v1"
RESULT_SCHEMA = "webnovel-dashboard-result/v1"
LOOPBACK_HOST = "127.0.0.1"
INSTANCE_RE = re.compile(r"^[0-9a-f]{32}$")
MAX_CONTROL_BYTES = 1024 * 1024


@dataclass(frozen=True)
class DashboardRuntimePaths:
    runtime_dir: Path
    state_file: Path
    ready_file: Path
    log_file: Path
    lock_file: Path


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_dashboard_host(host: str) -> str:
    normalized = str(host or "").strip().lower()
    if normalized in {"localhost", LOOPBACK_HOST}:
        return LOOPBACK_HOST
    raise ValueError("Dashboard 只允许绑定数字 IPv4 loopback 127.0.0.1")


def project_identity(project_root: str | Path) -> tuple[Path, str]:
    root = Path(project_root).resolve()
    normalized = os.path.normcase(str(root)).replace("\\", "/")
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return root, digest


def dashboard_runtime_paths(project_root: str | Path) -> DashboardRuntimePaths:
    _, digest = project_identity(project_root)
    runtime_dir = resolve_webnovel_home() / "runtime" / "dashboard" / digest
    return DashboardRuntimePaths(
        runtime_dir=runtime_dir,
        state_file=runtime_dir / "state.json",
        ready_file=runtime_dir / "ready.json",
        log_file=runtime_dir / "dashboard.log",
        lock_file=runtime_dir / "lifecycle.lock",
    )


def _base_result(project_root: Path, paths: DashboardRuntimePaths) -> dict[str, Any]:
    return {
        "schema_version": RESULT_SCHEMA,
        "project_root": str(project_root),
        "runtime_dir": str(paths.runtime_dir),
        "status": "failed",
        "ok": False,
        "errors": [],
    }


def _error_result(
    project_root: Path,
    paths: DashboardRuntimePaths,
    *,
    status: str,
    code: str,
    message: str,
    repair: str = "",
) -> dict[str, Any]:
    result = _base_result(project_root, paths)
    result.update(
        {
            "status": status,
            "errors": [
                {
                    "code": code,
                    "severity": "blocker" if status == "blocked" else "error",
                    "message": message,
                    "repair": repair,
                }
            ],
        }
    )
    return result


def _public_running_result(
    project_root: Path,
    paths: DashboardRuntimePaths,
    state: dict[str, Any],
    *,
    status: str = "running",
) -> dict[str, Any]:
    host = LOOPBACK_HOST
    port = int(state["port"])
    result = _base_result(project_root, paths)
    result.update(
        {
            "status": status,
            "ok": True,
            "pid": int(state["pid"]),
            "host": host,
            "port": port,
            "url": f"http://{host}:{port}",
            "log_path": str(paths.log_file),
            "health_endpoints": [
                f"http://{host}:{port}/api/project/info",
                f"http://{host}:{port}/api/story-runtime/health",
            ],
        }
    )
    return result


def _read_json_object(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise ValueError("控制文件不得是符号链接")
    if path.stat().st_size > MAX_CONTROL_BYTES:
        raise ValueError("控制文件超过大小限制")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("控制文件顶层必须是 JSON object")
    return payload


def _validate_state(
    state: dict[str, Any], project_root: Path, project_hash: str
) -> tuple[bool, str]:
    if state.get("schema_version") != STATE_SCHEMA:
        return False, "状态文件 schema_version 无效"
    if state.get("project_hash") != project_hash:
        return False, "状态文件项目 hash 不匹配"
    if state.get("project_root") != str(project_root):
        return False, "状态文件项目路径不匹配"
    if state.get("status") not in {"running", "stopped"}:
        return False, "状态文件 status 无效"
    if state.get("host") != LOOPBACK_HOST:
        return False, "状态文件 host 不是受信任的数字 loopback"
    instance_id = state.get("instance_id")
    if not isinstance(instance_id, str) or not INSTANCE_RE.fullmatch(instance_id):
        return False, "状态文件 instance_id 无效"
    pid = state.get("pid")
    port = state.get("port")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False, "状态文件 pid 无效"
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        return False, "状态文件 port 无效"
    return True, ""


def _probe_endpoint(
    host: str, port: int, path: str, *, timeout: float = 0.5
) -> tuple[int, dict[str, Any]]:
    # Do not use urllib here: local control probes must never inherit proxy settings.
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        connection.request("GET", path, headers={"Connection": "close"})
        response = connection.getresponse()
        raw = response.read(MAX_CONTROL_BYTES + 1)
        if len(raw) > MAX_CONTROL_BYTES:
            raise ValueError("Dashboard 响应超过大小限制")
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Dashboard 控制端点未返回 JSON object")
        return int(response.status), payload
    finally:
        connection.close()


def _probe_running_instance(
    state: dict[str, Any], project_root: Path, project_hash: str
) -> tuple[bool, str]:
    host = LOOPBACK_HOST
    port = int(state["port"])
    try:
        status, instance = _probe_endpoint(host, port, "/api/runtime/instance")
        if status != 200:
            return False, f"instance 端点 HTTP {status}"
        expected = {
            "schema_version": "webnovel-dashboard-instance/v1",
            "instance_id": state["instance_id"],
            "project_hash": project_hash,
            "project_root": str(project_root),
            "pid": int(state["pid"]),
        }
        for key, value in expected.items():
            if instance.get(key) != value:
                return False, f"instance 端点 {key} 不匹配"

        for path in ("/api/project/info", "/api/story-runtime/health"):
            endpoint_status, _ = _probe_endpoint(host, port, path)
            if endpoint_status != 200:
                return False, f"{path} 返回 HTTP {endpoint_status}"
    except (OSError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        return False, str(exc)
    return True, ""


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel32.GetExitCodeProcess.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            process_query_limited_information = 0x1000
            still_active = 259
            handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
            if not handle:
                # Access denied means a process exists but cannot be inspected; fail closed.
                return ctypes.get_last_error() == 5
            try:
                exit_code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return True
                return int(exit_code.value) == still_active
            finally:
                kernel32.CloseHandle(handle)
        except (AttributeError, OSError, TypeError, ValueError):
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _preflight(project_root: Path, paths: DashboardRuntimePaths) -> dict[str, Any] | None:
    plugin_root = resolve_plugin_root(__file__)
    dashboard_requirements = plugin_root / "dashboard" / "requirements.txt"
    runtime_requirements = plugin_root / "scripts" / "requirements.txt"
    frontend = plugin_root / "dashboard" / "frontend" / "dist"
    missing_artifacts: list[str] = []
    if not (frontend / "index.html").is_file():
        missing_artifacts.append(str(frontend / "index.html"))
    assets = frontend / "assets"
    if not assets.is_dir() or not any(item.is_file() for item in assets.iterdir()):
        missing_artifacts.append(str(assets))
    if missing_artifacts:
        return _error_result(
            project_root,
            paths,
            status="blocked",
            code="dashboard_frontend_missing",
            message="Dashboard 前端发布物不完整: " + ", ".join(missing_artifacts),
            repair="从完整插件包恢复 dashboard/frontend/dist；不会自动运行 npm 或访问网络。",
        )

    missing_modules = []
    for name in ("fastapi", "uvicorn", "watchdog", "filelock"):
        if (name == "filelock" and FileLock is None) or importlib.util.find_spec(name) is None:
            missing_modules.append(name)
    if missing_modules:
        requirement_paths = []
        if any(name != "filelock" for name in missing_modules):
            requirement_paths.append(str(dashboard_requirements))
        if "filelock" in missing_modules:
            requirement_paths.append(str(runtime_requirements))
        return _error_result(
            project_root,
            paths,
            status="blocked",
            code="dashboard_dependency_missing",
            message="Dashboard 缺少 Python 依赖: " + ", ".join(missing_modules),
            repair="请人工审阅并按需安装 " + "、".join(requirement_paths) + "；本命令不会自动安装或联网。",
        )
    return None


def dashboard_status(project_root: str | Path) -> dict[str, Any]:
    root, digest = project_identity(project_root)
    paths = dashboard_runtime_paths(root)
    if not paths.state_file.is_file():
        result = _base_result(root, paths)
        result.update({"status": "not_running", "ok": True})
        return result

    try:
        state = _read_json_object(paths.state_file)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        return _error_result(
            root,
            paths,
            status="failed",
            code="dashboard_state_invalid",
            message=f"Dashboard 状态文件损坏: {exc}",
            repair="检查 WEBNOVEL_HOME/runtime/dashboard 下的状态文件；不会据此终止任何进程。",
        )

    valid, detail = _validate_state(state, root, digest)
    if not valid:
        return _error_result(
            root,
            paths,
            status="failed",
            code="dashboard_state_untrusted",
            message=detail,
            repair="不要手工复用该 PID；修复或移除对应项目的损坏 runtime 状态后重试。",
        )

    verified, probe_detail = _probe_running_instance(state, root, digest)
    if verified:
        return _public_running_result(root, paths, state)

    pid = int(state["pid"])
    if not _pid_alive(pid):
        result = _base_result(root, paths)
        result.update({"status": "not_running", "ok": True, "last_pid": pid})
        if state["status"] == "running":
            result.update({"stale": True, "detail": probe_detail})
        else:
            result["stopped_record"] = True
        return result

    return _error_result(
        root,
        paths,
        status="blocked",
        code="dashboard_instance_unverified",
        message=f"PID {pid} 存活，但 Dashboard 身份或健康探针无法验证: {probe_detail}",
        repair="为避免 PID reuse 误杀，本命令不会发送信号；请人工核查 runtime 日志和该进程。",
    )


def _spawn_dashboard(
    root: Path,
    digest: str,
    paths: DashboardRuntimePaths,
    *,
    host: str,
    port: int,
    instance_id: str,
) -> subprocess.Popen[Any]:
    plugin_root = resolve_plugin_root(__file__)
    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH", "")
    entries = [str(plugin_root), str(plugin_root / "scripts")]
    if existing_pythonpath:
        entries.append(existing_pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(entries)

    argv = [
        sys.executable,
        "-X",
        "utf8",
        "-m",
        "dashboard.server",
        "--project-root",
        str(root),
        "--host",
        host,
        "--port",
        str(port),
        "--ready-file",
        str(paths.ready_file),
        "--instance-id",
        instance_id,
        "--project-hash",
        digest,
        "--no-browser",
    ]
    kwargs: dict[str, Any] = {
        "cwd": str(plugin_root),
        "env": env,
        "stdin": subprocess.DEVNULL,
        "shell": False,
    }
    if os.name == "nt":
        kwargs["creationflags"] = int(
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    else:
        kwargs["start_new_session"] = True

    log_handle = paths.log_file.open("a", encoding="utf-8", buffering=1)
    try:
        kwargs["stdout"] = log_handle
        kwargs["stderr"] = subprocess.STDOUT
        return subprocess.Popen(argv, **kwargs)
    finally:
        log_handle.close()


def _terminate_spawned_process(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
        except OSError:
            pass


def dashboard_start(
    project_root: str | Path,
    *,
    host: str = LOOPBACK_HOST,
    port: int = 0,
    startup_timeout: float = 10.0,
) -> dict[str, Any]:
    root, digest = project_identity(project_root)
    paths = dashboard_runtime_paths(root)
    try:
        normalized_host = normalize_dashboard_host(host)
    except ValueError as exc:
        return _error_result(
            root, paths, status="blocked", code="dashboard_host_forbidden", message=str(exc)
        )
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        return _error_result(
            root,
            paths,
            status="blocked",
            code="dashboard_port_invalid",
            message="Dashboard 端口必须是 0 到 65535 的整数；0 表示由子进程原子绑定动态端口。",
        )

    blocker = _preflight(root, paths)
    if blocker is not None:
        return blocker

    paths.runtime_dir.mkdir(parents=True, exist_ok=True)
    try:
        with FileLock(str(paths.lock_file), timeout=5):
            current = dashboard_status(root)
            if current["status"] == "running":
                current["status"] = "already_running"
                return current
            if current["status"] in {"blocked", "failed"}:
                return current

            try:
                paths.ready_file.unlink(missing_ok=True)
            except OSError as exc:
                return _error_result(
                    root,
                    paths,
                    status="failed",
                    code="dashboard_ready_cleanup_failed",
                    message=f"无法准备 ready 文件: {exc}",
                )

            instance_id = uuid.uuid4().hex
            process = _spawn_dashboard(
                root,
                digest,
                paths,
                host=normalized_host,
                port=port,
                instance_id=instance_id,
            )
            deadline = time.monotonic() + max(0.5, float(startup_timeout))
            ready: dict[str, Any] | None = None
            ready_error = ""
            while time.monotonic() < deadline:
                if paths.ready_file.is_file():
                    try:
                        ready = _read_json_object(paths.ready_file)
                    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
                        ready_error = str(exc)
                    else:
                        break
                if process.poll() is not None:
                    ready_error = f"Dashboard 子进程提前退出，code={process.returncode}"
                    break
                time.sleep(0.05)

            if ready is None:
                _terminate_spawned_process(process)
                return _error_result(
                    root,
                    paths,
                    status="failed",
                    code="dashboard_start_timeout",
                    message=ready_error or "等待 Dashboard ready 记录超时",
                    repair=f"查看本地日志 {paths.log_file}；不会自动安装依赖或打开浏览器。",
                )
            if ready.get("status") == "error":
                _terminate_spawned_process(process)
                return _error_result(
                    root,
                    paths,
                    status="blocked",
                    code=str(ready.get("code") or "dashboard_child_failed"),
                    message=str(ready.get("message") or "Dashboard 子进程启动失败"),
                    repair=f"查看本地日志 {paths.log_file}。",
                )

            expected_ready = {
                "schema_version": READY_SCHEMA,
                "status": "ready",
                "instance_id": instance_id,
                "project_hash": digest,
                "project_root": str(root),
                "pid": int(process.pid),
                "host": LOOPBACK_HOST,
            }
            if any(ready.get(key) != value for key, value in expected_ready.items()):
                _terminate_spawned_process(process)
                return _error_result(
                    root,
                    paths,
                    status="failed",
                    code="dashboard_ready_untrusted",
                    message="Dashboard ready 记录与本次子进程身份不匹配",
                )
            actual_port = ready.get("port")
            if isinstance(actual_port, bool) or not isinstance(actual_port, int) or not 1 <= actual_port <= 65535:
                _terminate_spawned_process(process)
                return _error_result(
                    root,
                    paths,
                    status="failed",
                    code="dashboard_ready_port_invalid",
                    message="Dashboard ready 记录中的端口无效",
                )

            state = {
                "schema_version": STATE_SCHEMA,
                "status": "running",
                "project_root": str(root),
                "project_hash": digest,
                "instance_id": instance_id,
                "pid": int(process.pid),
                "host": LOOPBACK_HOST,
                "port": actual_port,
                "started_at": _utc_now(),
            }
            probe_deadline = time.monotonic() + max(0.5, float(startup_timeout))
            probe_detail = ""
            while time.monotonic() < probe_deadline:
                verified, probe_detail = _probe_running_instance(state, root, digest)
                if verified:
                    atomic_write_json(paths.state_file, state, use_lock=False, backup=False)
                    return _public_running_result(root, paths, state)
                if process.poll() is not None:
                    probe_detail = f"Dashboard 子进程退出，code={process.returncode}: {probe_detail}"
                    break
                time.sleep(0.05)

            _terminate_spawned_process(process)
            return _error_result(
                root,
                paths,
                status="failed",
                code="dashboard_health_timeout",
                message=f"Dashboard 健康探针未通过: {probe_detail}",
                repair=f"查看本地日志 {paths.log_file}。",
            )
    except Timeout:
        return _error_result(
            root,
            paths,
            status="blocked",
            code="dashboard_lifecycle_busy",
            message="另一个 Dashboard 生命周期操作仍在进行中",
        )


def _signal_verified_pid(pid: int) -> None:
    if os.name == "nt":
        os.kill(pid, signal.SIGTERM)
    else:
        os.killpg(pid, signal.SIGTERM)


def dashboard_stop(
    project_root: str | Path, *, shutdown_timeout: float = 5.0
) -> dict[str, Any]:
    root, digest = project_identity(project_root)
    paths = dashboard_runtime_paths(root)
    if not paths.runtime_dir.is_dir():
        result = _base_result(root, paths)
        result.update({"status": "stopped", "ok": True, "already_stopped": True})
        return result
    if FileLock is None:
        return _error_result(
            root,
            paths,
            status="blocked",
            code="dashboard_dependency_missing",
            message="Dashboard 生命周期缺少 Python 依赖: filelock",
            repair="请人工审阅 scripts/requirements.txt；本命令不会自动安装或联网。",
        )
    try:
        with FileLock(str(paths.lock_file), timeout=5):
            current = dashboard_status(root)
            if current["status"] == "not_running":
                current.update({"status": "stopped", "already_stopped": True})
                return current
            if current["status"] in {"blocked", "failed"}:
                return current

            try:
                state = _read_json_object(paths.state_file)
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
                return _error_result(
                    root,
                    paths,
                    status="failed",
                    code="dashboard_state_invalid",
                    message=f"Dashboard 状态文件在停止前发生变化: {exc}",
                    repair="为避免误杀，本命令没有发送信号；请人工核查 runtime 状态。",
                )
            valid, detail = _validate_state(state, root, digest)
            if not valid:
                return _error_result(
                    root,
                    paths,
                    status="failed",
                    code="dashboard_state_untrusted",
                    message=f"Dashboard 状态文件在停止前失去信任: {detail}",
                    repair="为避免误杀，本命令没有发送信号；请人工核查 runtime 状态。",
                )
            if int(state["pid"]) != int(current["pid"]) or int(state["port"]) != int(current["port"]):
                return _error_result(
                    root,
                    paths,
                    status="blocked",
                    code="dashboard_state_changed",
                    message="Dashboard 状态文件在身份验证后发生变化",
                    repair="为避免 PID reuse 误杀，本命令没有发送信号；请重新运行 status 后重试。",
                )
            verified, probe_detail = _probe_running_instance(state, root, digest)
            if not verified:
                return _error_result(
                    root,
                    paths,
                    status="blocked",
                    code="dashboard_instance_unverified",
                    message=f"Dashboard 在发送信号前无法重新验证: {probe_detail}",
                    repair="为避免 PID reuse 误杀，本命令没有发送信号；请人工核查 runtime 日志和进程。",
                )
            pid = int(state["pid"])
            try:
                _signal_verified_pid(pid)
            except (OSError, ProcessLookupError) as exc:
                if _pid_alive(pid):
                    return _error_result(
                        root,
                        paths,
                        status="failed",
                        code="dashboard_stop_signal_failed",
                        message=f"无法终止已验证的 Dashboard 进程: {exc}",
                    )

            deadline = time.monotonic() + max(0.1, float(shutdown_timeout))
            while time.monotonic() < deadline and _pid_alive(pid):
                time.sleep(0.05)
            if _pid_alive(pid):
                return _error_result(
                    root,
                    paths,
                    status="failed",
                    code="dashboard_stop_timeout",
                    message="Dashboard 进程在超时前未退出；未对未经重新验证的 PID 强制终止。",
                )

            stopped_state = dict(state)
            stopped_state.update({"status": "stopped", "stopped_at": _utc_now()})
            atomic_write_json(paths.state_file, stopped_state, use_lock=False, backup=False)
            result = _base_result(root, paths)
            result.update({"status": "stopped", "ok": True, "pid": pid})
            return result
    except Timeout:
        return _error_result(
            root,
            paths,
            status="blocked",
            code="dashboard_lifecycle_busy",
            message="另一个 Dashboard 生命周期操作仍在进行中",
        )


def dashboard_exit_code(result: dict[str, Any]) -> int:
    if result.get("ok"):
        return 0
    return 1 if result.get("status") == "blocked" else 2


def format_dashboard_result(result: dict[str, Any], output_format: str) -> str:
    if output_format == "json":
        return json.dumps(result, ensure_ascii=False, indent=2)
    lines = [
        f"Dashboard: {result.get('status', 'unknown')}",
        f"project_root: {result.get('project_root', '')}",
    ]
    if result.get("url"):
        lines.append(f"url: {result['url']}")
    if result.get("pid"):
        lines.append(f"pid: {result['pid']}")
    if result.get("log_path"):
        lines.append(f"log: {result['log_path']}")
    for error in result.get("errors") or []:
        lines.append(f"{str(error.get('severity') or 'error').upper()} {error.get('code')}: {error.get('message')}")
        if error.get("repair"):
            lines.append(f"  repair: {error['repair']}")
    return "\n".join(lines)
