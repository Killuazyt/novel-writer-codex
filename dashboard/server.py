"""Private Dashboard child process used by the unified lifecycle CLI."""

from __future__ import annotations

import argparse
import os
import socket
import sys
from pathlib import Path


def _shared_resolver():
    scripts_dir = Path(__file__).resolve().parents[1] / "scripts"
    scripts_entry = str(scripts_dir)
    if scripts_entry not in sys.path:
        sys.path.insert(0, scripts_entry)
    from project_locator import resolve_project_root

    return resolve_project_root


def _resolve_project_root(cli_root: str | None) -> Path:
    """Use the same strict resolver as every runtime command."""
    try:
        return _shared_resolver()(cli_root)
    except FileNotFoundError as exc:
        print(
            f"ERROR: 无法定位 PROJECT_ROOT（需要包含 .webnovel/state.json 的目录）: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


def _bind_loopback(host: str, port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind((host, port))
        sock.listen(2048)
        sock.set_inheritable(False)
        return sock
    except BaseException:
        sock.close()
        raise


def _write_ready(path: Path, payload: dict) -> None:
    from security_utils import atomic_write_json

    atomic_write_json(path, payload, use_lock=False, backup=False)


def main() -> None:
    parser = argparse.ArgumentParser(description="Webnovel Dashboard private server")
    parser.add_argument("--project-root", type=str, required=True, help="小说项目根目录")
    parser.add_argument("--host", default="127.0.0.1", help="仅允许 127.0.0.1")
    parser.add_argument("--port", type=int, default=0, help="监听端口；0 为动态端口")
    parser.add_argument("--ready-file", required=True, help=argparse.SUPPRESS)
    parser.add_argument("--instance-id", required=True, help=argparse.SUPPRESS)
    parser.add_argument("--project-hash", required=True, help=argparse.SUPPRESS)
    parser.add_argument("--no-browser", action="store_true", help="兼容参数；始终不会打开浏览器")
    args = parser.parse_args()

    project_root = _resolve_project_root(args.project_root)

    from data_modules.dashboard_lifecycle import (
        READY_SCHEMA,
        dashboard_runtime_paths,
        normalize_dashboard_host,
        project_identity,
    )

    try:
        host = normalize_dashboard_host(args.host)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    if not 0 <= args.port <= 65535:
        print("ERROR: port 必须在 0..65535", file=sys.stderr)
        raise SystemExit(1)

    resolved_root, expected_hash = project_identity(project_root)
    expected_ready = dashboard_runtime_paths(resolved_root).ready_file.resolve()
    try:
        supplied_ready = Path(args.ready_file).resolve()
    except OSError:
        print("ERROR: ready-file 无效", file=sys.stderr)
        raise SystemExit(1) from None
    if args.project_hash != expected_hash or supplied_ready != expected_ready:
        print("ERROR: private lifecycle identity validation failed", file=sys.stderr)
        raise SystemExit(1)

    sock: socket.socket | None = None
    try:
        sock = _bind_loopback(host, args.port)
    except OSError as exc:
        _write_ready(
            supplied_ready,
            {
                "schema_version": READY_SCHEMA,
                "status": "error",
                "code": "dashboard_port_unavailable",
                "message": f"无法绑定 {host}:{args.port}: {exc}",
                "project_root": str(resolved_root),
                "project_hash": expected_hash,
                "instance_id": args.instance_id,
                "pid": os.getpid(),
            },
        )
        raise SystemExit(1) from None

    actual_port = int(sock.getsockname()[1])
    _write_ready(
        supplied_ready,
        {
            "schema_version": READY_SCHEMA,
            "status": "ready",
            "project_root": str(resolved_root),
            "project_hash": expected_hash,
            "instance_id": args.instance_id,
            "pid": os.getpid(),
            "host": host,
            "port": actual_port,
        },
    )

    import uvicorn
    from .app import create_app

    app = create_app(
        resolved_root,
        instance_id=args.instance_id,
        project_hash=expected_hash,
    )
    config = uvicorn.Config(app, host=host, port=actual_port, log_level="info")
    server = uvicorn.Server(config)
    try:
        server.run(sockets=[sock])
    finally:
        sock.close()


if __name__ == "__main__":
    main()
