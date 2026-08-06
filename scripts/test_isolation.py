"""Early, inheritable isolation used by the pytest harness."""

from __future__ import annotations

import json
import os
import socket
import uuid
from pathlib import Path
from typing import Mapping

from test_state_guard import create_snapshot, resolve_guard_paths, write_snapshot


ISOLATION_ENV = "WEBNOVEL_TEST_ISOLATION"
SESSION_ROOT_ENV = "WEBNOVEL_TEST_SESSION_ROOT"
SNAPSHOT_ENV = "WEBNOVEL_TEST_REAL_HOME_SNAPSHOT"
REAL_CONTEXT_ENV = "WEBNOVEL_TEST_REAL_PATH_CONTEXT"

_PROJECT_POINTER_ENV = {
    "WEBNOVEL_PROJECT_ROOT",
    "CLAUDE_PROJECT_DIR",
    "WEBNOVEL_PLUGIN_ROOT",
    "PLUGIN_ROOT",
    "CLAUDE_PLUGIN_ROOT",
}
_SECRET_PREFIXES = ("EMBED_", "RERANK_")
_NETWORK_GUARD_INSTALLED = False


class NetworkAccessDisabled(RuntimeError):
    """Raised when a test attempts IPv4/IPv6 network access."""


def _resolve(path: Path) -> Path:
    try:
        return path.resolve(strict=False)
    except OSError:
        return path.absolute()


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _prepend_pythonpath(*paths: Path) -> None:
    separator = os.pathsep
    current = [entry for entry in os.environ.get("PYTHONPATH", "").split(separator) if entry]
    wanted = [str(_resolve(path)) for path in paths]
    combined: list[str] = []
    seen: set[str] = set()
    for entry in [*wanted, *current]:
        key = os.path.normcase(os.path.abspath(entry))
        if key not in seen:
            seen.add(key)
            combined.append(entry)
    os.environ["PYTHONPATH"] = separator.join(combined)


def _real_context(environ: Mapping[str, str]) -> dict[str, str]:
    real_home = _resolve(Path.home())
    codex_home = _resolve(Path(environ.get("CODEX_HOME") or real_home / ".codex"))
    webnovel_home = _resolve(
        Path(environ.get("WEBNOVEL_HOME") or codex_home / "novel-writer-codex")
    )
    claude_home = _resolve(
        Path(
            environ.get("WEBNOVEL_CLAUDE_HOME")
            or environ.get("CLAUDE_HOME")
            or real_home / ".claude"
        )
    )
    return {
        "home": str(real_home),
        "codex_home": str(codex_home),
        "webnovel_home": str(webnovel_home),
        "claude_home": str(claude_home),
    }


def _create_session_root(repo_root: Path) -> Path:
    configured = os.environ.get(SESSION_ROOT_ENV)
    if configured:
        return _resolve(Path(configured))
    session_id = f"session-{os.getpid()}-{uuid.uuid4().hex}"
    return _resolve(repo_root / ".tmp" / "pytest" / session_id)


def _set_windows_home_parts(home: Path) -> None:
    if os.name != "nt":
        return
    drive, tail = os.path.splitdrive(str(home))
    if drive:
        os.environ["HOMEDRIVE"] = drive
        os.environ["HOMEPATH"] = tail or "\\"


def activate_test_isolation(repo_root: Path | None = None) -> dict[str, Path]:
    """Redirect host state before test modules and their child processes load."""

    root = _resolve(repo_root or _repo_root())
    scripts_dir = root / "scripts"
    original_env = dict(os.environ)
    first_activation = not (
        os.environ.get(ISOLATION_ENV) == "1"
        and os.environ.get(SNAPSHOT_ENV)
        and os.environ.get(REAL_CONTEXT_ENV)
    )
    context = _real_context(original_env) if first_activation else json.loads(
        os.environ.get(REAL_CONTEXT_ENV, "{}") or "{}"
    )

    session_root = _create_session_root(root)
    home = session_root / "home"
    codex_home = home / ".codex"
    webnovel_home = codex_home / "novel-writer-codex"
    claude_home = home / ".claude"
    tmp = session_root / "tmp"
    appdata = session_root / "appdata" / "roaming"
    local_appdata = session_root / "appdata" / "local"
    xdg_config = session_root / "xdg" / "config"
    xdg_cache = session_root / "xdg" / "cache"

    for directory in (
        session_root,
        home,
        codex_home,
        webnovel_home,
        claude_home,
        tmp,
        appdata,
        local_appdata,
        xdg_config,
        xdg_cache,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    if first_activation:
        snapshot_path = session_root / "real-home-before.json"
        protected = resolve_guard_paths(original_env, home=Path(context["home"]))
        write_snapshot(snapshot_path, create_snapshot(protected))
        os.environ[SNAPSHOT_ENV] = str(snapshot_path)
        os.environ[REAL_CONTEXT_ENV] = json.dumps(context, ensure_ascii=False)

    for name in list(os.environ):
        if name in _PROJECT_POINTER_ENV or name.startswith(_SECRET_PREFIXES):
            os.environ.pop(name, None)

    isolated_values = {
        "HOME": home,
        "USERPROFILE": home,
        "WEBNOVEL_HOME": webnovel_home,
        "CODEX_HOME": codex_home,
        "CLAUDE_HOME": claude_home,
        "WEBNOVEL_CLAUDE_HOME": claude_home,
        "TMP": tmp,
        "TEMP": tmp,
        "TMPDIR": tmp,
        "APPDATA": appdata,
        "LOCALAPPDATA": local_appdata,
        "XDG_CONFIG_HOME": xdg_config,
        "XDG_CACHE_HOME": xdg_cache,
        "GIT_CONFIG_GLOBAL": session_root / "gitconfig",
        "COVERAGE_FILE": session_root / ".coverage",
    }
    for name, value in isolated_values.items():
        os.environ[name] = str(value)

    _set_windows_home_parts(home)
    os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
    # The parent pytest process runs with ``-X utf8``.  Propagate the same
    # contract explicitly so Windows child processes never emit a local-code
    # page stream that their UTF-8 parent cannot decode.
    os.environ["PYTHONUTF8"] = "1"
    os.environ["PYTHONIOENCODING"] = "utf-8"
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    os.environ["WEBNOVEL_TEST_RELAX_ATOMIC_REPLACE"] = "1"
    os.environ[ISOLATION_ENV] = "1"
    os.environ[SESSION_ROOT_ENV] = str(session_root)
    _prepend_pythonpath(root, scripts_dir)

    return {
        "session_root": session_root,
        "home": home,
        "codex_home": codex_home,
        "webnovel_home": webnovel_home,
        "claude_home": claude_home,
        "tmp": tmp,
        "snapshot": Path(os.environ[SNAPSHOT_ENV]),
    }


def _deny(operation: str, target: object = None) -> None:
    suffix = "" if target is None else f": {target!r}"
    raise NetworkAccessDisabled(
        f"IPv4/IPv6 network access is disabled during tests ({operation}{suffix})"
    )


def install_network_guard() -> None:
    """Block internet-capable socket operations while preserving local IPC."""

    global _NETWORK_GUARD_INSTALLED
    if _NETWORK_GUARD_INSTALLED:
        return

    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_bind = socket.socket.bind
    original_socketpair = socket.socketpair
    socketpair_defaults = getattr(original_socketpair, "__defaults__", None)
    default_socketpair_family = (
        socketpair_defaults[0]
        if socketpair_defaults
        else getattr(socket, "AF_UNIX", socket.AF_INET)
    )

    def guarded_connect(self: socket.socket, address: object) -> object:
        if self.family in (socket.AF_INET, socket.AF_INET6):
            _deny("connect", address)
        return original_connect(self, address)

    def guarded_connect_ex(self: socket.socket, address: object) -> object:
        if self.family in (socket.AF_INET, socket.AF_INET6):
            _deny("connect_ex", address)
        return original_connect_ex(self, address)

    def guarded_bind(self: socket.socket, address: object) -> object:
        if self.family in (socket.AF_INET, socket.AF_INET6):
            _deny("bind", address)
        return original_bind(self, address)

    def guarded_create_connection(address: object, *args: object, **kwargs: object) -> object:
        _deny("create_connection", address)

    def guarded_getaddrinfo(host: object, port: object, *args: object, **kwargs: object) -> object:
        _deny("getaddrinfo", (host, port))

    def guarded_socketpair(
        family: int = default_socketpair_family,
        type: int = socket.SOCK_STREAM,
        proto: int = 0,
    ) -> tuple[socket.socket, socket.socket]:
        # Windows implements socketpair with a private loopback listener.  Use
        # the saved methods only inside this bounded IPC constructor so general
        # IPv4/IPv6 bind/connect remain fail-closed.
        if family not in (socket.AF_INET, socket.AF_INET6):
            return original_socketpair(family, type, proto)
        if type != socket.SOCK_STREAM or proto != 0:
            return original_socketpair(family, type, proto)

        host = "127.0.0.1" if family == socket.AF_INET else "::1"
        listener = socket.socket(family, type, proto)
        try:
            original_bind(listener, (host, 0))
            listener.listen()
            address, port = listener.getsockname()[:2]
            client = socket.socket(family, type, proto)
            try:
                client.setblocking(False)
                try:
                    original_connect(client, (address, port))
                except (BlockingIOError, InterruptedError):
                    pass
                client.setblocking(True)
                server, _ = listener.accept()
            except BaseException:
                client.close()
                raise
        finally:
            listener.close()
        return server, client

    socket.socket.connect = guarded_connect
    socket.socket.connect_ex = guarded_connect_ex
    socket.socket.bind = guarded_bind
    socket.create_connection = guarded_create_connection
    socket.getaddrinfo = guarded_getaddrinfo
    socket.socketpair = guarded_socketpair
    _NETWORK_GUARD_INSTALLED = True


def child_isolation_active() -> bool:
    """Return whether this interpreter inherited the isolated test session."""

    return os.environ.get(ISOLATION_ENV) == "1" and bool(
        os.environ.get(SESSION_ROOT_ENV)
    )


__all__ = [
    "NetworkAccessDisabled",
    "activate_test_isolation",
    "child_isolation_active",
    "install_network_guard",
]
