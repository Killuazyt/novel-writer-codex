from __future__ import annotations

from collections import Counter
import inspect
import os
import shutil
import sqlite3
import tempfile
import uuid
from pathlib import Path

import pytest

from test_isolation import activate_test_isolation, install_network_guard
from test_state_guard import compare_snapshot, read_snapshot


_ORIGINAL_SQLITE_CONNECT = sqlite3.connect
_ORIGINAL_TEMPORARY_DIRECTORY = tempfile.TemporaryDirectory
_TEMPORARY_DIRECTORY_SUPPORTS_DELETE = (
    "delete" in inspect.signature(_ORIGINAL_TEMPORARY_DIRECTORY).parameters
)
_ISOLATED_HOMES = activate_test_isolation(Path(__file__).resolve().parents[1])
install_network_guard()

_PRIMARY_MARKERS = {"runtime", "codex_contract", "upstream_contract"}
_UPSTREAM_CONTRACT_FILES = {
    "test_prompt_integrity.py",
    "test_run_behavior_evals.py",
    "test_validate_plugin_package.py",
    "test_validate_release_notes.py",
}
_CODEX_CONTRACT_FILES = {
    "test_hooks.py",
    "test_project_status.py",
    "test_pytest_isolation.py",
    "test_test_harness.py",
    "test_validate_codex_adapter.py",
    "test_validate_repository_hygiene.py",
}
_INTEGRATION_FILES = {
    "test_backup_manager.py",
    "test_dashboard_app.py",
    "test_dashboard_security.py",
    "test_dashboard_watcher.py",
    "test_entity_linker_cli.py",
    "test_hooks.py",
    "test_memory_cli.py",
    "test_projections_cli.py",
    "test_reference_search.py",
    "test_story_system_cli.py",
    "test_style_sampler_cli.py",
    "test_test_harness.py",
    "test_update_state_add_review_cli.py",
    "test_validate_csv.py",
    "test_webnovel_unified_cli.py",
}
_FAILURE_NAME_PARTS = (
    "block",
    "corrupt",
    "denied",
    "error",
    "fail",
    "forbid",
    "invalid",
    "locked",
    "malformed",
    "missing",
    "reject",
    "timeout",
    "traversal",
    "unsafe",
)
_STATE_GUARD_PROBLEMS: list[dict[str, object]] = []


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _tmp_root() -> Path:
    root = _ISOLATED_HOMES["tmp"]
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_mkdtemp(suffix: str | None = None, prefix: str | None = None, dir: str | os.PathLike[str] | None = None) -> str:
    """Avoid WindowsApps Python creating inaccessible 0o700 temp dirs."""
    suffix = "" if suffix is None else suffix
    prefix = "tmp" if prefix is None else prefix
    root = Path(dir) if dir is not None else _tmp_root()
    root.mkdir(parents=True, exist_ok=True)

    for _ in range(100):
        path = root / f"{prefix}{uuid.uuid4().hex}{suffix}"
        try:
            path.mkdir()
        except FileExistsError:
            continue
        return str(path.resolve())

    raise FileExistsError(f"Unable to create unique temporary directory under {root}")


def _install_safe_tempfile() -> None:
    root = _tmp_root()
    for name in ("TMP", "TEMP", "TMPDIR"):
        os.environ[name] = str(root)
    os.environ["WEBNOVEL_TEST_RELAX_ATOMIC_REPLACE"] = "1"
    tempfile.tempdir = str(root)
    tempfile.mkdtemp = _safe_mkdtemp
    tempfile.TemporaryDirectory = _SafeTemporaryDirectory


class _SafeTemporaryDirectory(_ORIGINAL_TEMPORARY_DIRECTORY):
    def __init__(self, suffix=None, prefix=None, dir=None, ignore_cleanup_errors=True, *, delete=True):
        kwargs = {
            "suffix": suffix,
            "prefix": prefix,
            "dir": dir,
            "ignore_cleanup_errors": ignore_cleanup_errors,
        }
        if _TEMPORARY_DIRECTORY_SUPPORTS_DELETE:
            kwargs["delete"] = delete
        super().__init__(**kwargs)


def _safe_sqlite_connect(*args, **kwargs):
    conn = _ORIGINAL_SQLITE_CONNECT(*args, **kwargs)
    try:
        conn.execute("PRAGMA journal_mode=MEMORY")
    except sqlite3.DatabaseError:
        pass
    return conn


def _install_safe_sqlite() -> None:
    sqlite3.connect = _safe_sqlite_connect


def pytest_configure(config: pytest.Config) -> None:
    _install_safe_tempfile()
    _install_safe_sqlite()


def _primary_marker_for(item: pytest.Item) -> str:
    filename = Path(str(item.path)).name
    if filename in _UPSTREAM_CONTRACT_FILES:
        return "upstream_contract"
    if filename in _CODEX_CONTRACT_FILES:
        return "codex_contract"
    return "runtime"


def _add_auxiliary_markers(item: pytest.Item) -> None:
    filename = Path(str(item.path)).name
    node_name = item.name.casefold()
    if filename in _INTEGRATION_FILES:
        item.add_marker("integration")
    if filename == "test_security_utils_atomic.py" and "real_windows" in node_name:
        item.add_marker("windows")
    if filename == "test_run_behavior_evals.py":
        item.add_marker("slow")
    if any(part in node_name for part in _FAILURE_NAME_PARTS):
        item.add_marker("failure")


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    """Assign and enforce exactly one host/runtime contract per test item."""

    violations: list[str] = []
    counts: Counter[str] = Counter()
    for item in items:
        expected = _primary_marker_for(item)
        existing = {
            name
            for name in _PRIMARY_MARKERS
            if any(item.iter_markers(name=name))
        }
        if not existing:
            item.add_marker(expected)
            existing = {expected}
        if existing != {expected}:
            violations.append(
                f"{item.nodeid}: expected {expected}, found {sorted(existing)}"
            )
        counts[expected] += 1
        _add_auxiliary_markers(item)

    setattr(config, "_webnovel_primary_marker_counts", counts)
    if violations:
        details = "\n".join(f"  - {line}" for line in violations)
        raise pytest.UsageError(
            "Every test must have exactly one correct primary marker:\n" + details
        )


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Fail the suite if stable files in the real host homes changed."""

    global _STATE_GUARD_PROBLEMS
    snapshot_path = os.environ.get("WEBNOVEL_TEST_REAL_HOME_SNAPSHOT")
    if not snapshot_path:
        _STATE_GUARD_PROBLEMS = [
            {
                "path": "<snapshot>",
                "error": "WEBNOVEL_TEST_REAL_HOME_SNAPSHOT is missing",
            }
        ]
    else:
        try:
            _STATE_GUARD_PROBLEMS = compare_snapshot(
                read_snapshot(Path(snapshot_path))
            )
        except (OSError, ValueError) as exc:
            _STATE_GUARD_PROBLEMS = [
                {"path": snapshot_path, "error": f"{type(exc).__name__}: {exc}"}
            ]
    if _STATE_GUARD_PROBLEMS:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(
    terminalreporter: pytest.TerminalReporter,
    exitstatus: int,
    config: pytest.Config,
) -> None:
    counts = getattr(config, "_webnovel_primary_marker_counts", Counter())
    if counts:
        terminalreporter.write_line(
            "primary markers: "
            + ", ".join(
                f"{name}={counts.get(name, 0)}"
                for name in ("runtime", "codex_contract", "upstream_contract")
            )
        )
    if _STATE_GUARD_PROBLEMS:
        terminalreporter.write_sep("!", "real host state changed during tests")
        for problem in _STATE_GUARD_PROBLEMS:
            terminalreporter.write_line(str(problem.get("path", "<unknown>")))


@pytest.fixture
def tmp_path(request: pytest.FixtureRequest) -> Path:
    safe_name = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in request.node.name)
    # Keep nested Git object paths below the legacy Windows MAX_PATH boundary.
    safe_name = (safe_name[:48].rstrip("-_") or "test")
    path = _tmp_root() / f"{safe_name}_{uuid.uuid4().hex[:12]}"
    path.mkdir(parents=True, exist_ok=False)
    try:
        yield path
    finally:
        if os.environ.get("WEBNOVEL_KEEP_TEST_TMP") != "1":
            shutil.rmtree(path, ignore_errors=True)


@pytest.fixture(scope="session")
def isolated_homes() -> dict[str, Path]:
    """Expose the early isolated roots without revealing real host contents."""

    return dict(_ISOLATED_HOMES)


_install_safe_tempfile()
_install_safe_sqlite()
