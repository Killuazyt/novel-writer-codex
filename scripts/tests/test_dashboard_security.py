from __future__ import annotations

import importlib
import sys
from pathlib import Path

from fastapi.testclient import TestClient


def _create_dashboard_client(monkeypatch, project_root: Path) -> TestClient:
    plugin_root = Path(__file__).resolve().parents[2]
    if str(plugin_root) not in sys.path:
        monkeypatch.syspath_prepend(str(plugin_root))

    for name in list(sys.modules):
        if name == "dashboard.app":
            sys.modules.pop(name, None)

    module = importlib.import_module("dashboard.app")
    return TestClient(module.create_app(project_root))


def test_dashboard_cors_allows_localhost_origin(monkeypatch, tmp_path):
    (tmp_path / ".webnovel").mkdir(parents=True)
    (tmp_path / ".webnovel" / "state.json").write_text("{}", encoding="utf-8")
    client = _create_dashboard_client(monkeypatch, tmp_path)

    response = client.options(
        "/api/project/info",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_dashboard_cors_rejects_untrusted_origin(monkeypatch, tmp_path):
    (tmp_path / ".webnovel").mkdir(parents=True)
    (tmp_path / ".webnovel" / "state.json").write_text("{}", encoding="utf-8")
    client = _create_dashboard_client(monkeypatch, tmp_path)

    response = client.options(
        "/api/project/info",
        headers={
            "Origin": "https://example.com",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert "access-control-allow-origin" not in response.headers


def test_dashboard_file_read_rejects_large_files(monkeypatch, tmp_path):
    (tmp_path / ".webnovel").mkdir(parents=True)
    (tmp_path / ".webnovel" / "state.json").write_text("{}", encoding="utf-8")
    prose_dir = tmp_path / "正文"
    prose_dir.mkdir()
    large_file = prose_dir / "huge.md"
    large_file.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    client = _create_dashboard_client(monkeypatch, tmp_path)

    response = client.get("/api/files/read", params={"path": "正文/huge.md"})

    assert response.status_code == 413


def test_dashboard_file_read_rejects_traversal_absolute_and_encoded_paths(monkeypatch, tmp_path):
    (tmp_path / ".webnovel").mkdir(parents=True)
    (tmp_path / ".webnovel" / "state.json").write_text("{}", encoding="utf-8")
    (tmp_path / "正文").mkdir()
    outside = tmp_path.parent / "outside-dashboard-secret.txt"
    outside.write_text("secret", encoding="utf-8")
    client = _create_dashboard_client(monkeypatch, tmp_path)

    traversal = client.get("/api/files/read", params={"path": "../outside-dashboard-secret.txt"})
    absolute = client.get("/api/files/read", params={"path": str(outside.resolve())})
    encoded = client.get("/api/files/read?path=%2E%2E%2Foutside-dashboard-secret.txt")

    assert traversal.status_code == 403
    assert absolute.status_code == 403
    assert encoded.status_code == 403


def test_dashboard_file_tree_skips_out_of_root_symlinks(monkeypatch, tmp_path):
    (tmp_path / ".webnovel").mkdir(parents=True)
    (tmp_path / ".webnovel" / "state.json").write_text("{}", encoding="utf-8")
    prose = tmp_path / "正文"
    prose.mkdir()
    (prose / "safe.md").write_text("safe", encoding="utf-8")
    outside = tmp_path.parent / "outside-tree"
    outside.mkdir(exist_ok=True)
    (outside / "secret.md").write_text("secret", encoding="utf-8")
    link = prose / "outside-link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        import pytest

        pytest.skip("symlink creation is not available for this test account")
    client = _create_dashboard_client(monkeypatch, tmp_path)

    response = client.get("/api/files/tree")

    assert response.status_code == 200
    names = [item["name"] for item in response.json()["正文"]]
    assert names == ["safe.md"]
