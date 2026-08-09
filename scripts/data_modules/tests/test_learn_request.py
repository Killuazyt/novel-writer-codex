from __future__ import annotations

import json
from pathlib import Path

import pytest

from data_modules.learn_request import MAX_REQUEST_BYTES, LearnRequestError, load_learn_request


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "小说项目"
    (project / ".webnovel").mkdir(parents=True)
    (project / ".webnovel" / "state.json").write_text("{}", encoding="utf-8")
    return project


def test_learn_request_preserves_unicode_newlines_and_shell_metacharacters(tmp_path):
    project = _project(tmp_path)
    description = "节奏\"'\n&;|$()` 必须保持"
    request = tmp_path / "learn request.json"
    request.write_text(
        json.dumps(
            {
                "schema_version": "webnovel-learn-request/v1",
                "pattern_type": "pacing",
                "description": description,
                "category": "节奏",
                "importance": "high",
                "source_chapter": 8,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = load_learn_request(request, project_root=project)

    assert result["description"] == description
    assert result["source_chapter"] == 8


@pytest.mark.parametrize(
    "mutation",
    [
        {"schema_version": "wrong"},
        {"unknown": "field"},
        {"pattern_type": "invalid"},
        {"importance": "urgent"},
        {"source_chapter": True},
    ],
)
def test_learn_request_rejects_invalid_schema_and_fields(tmp_path, mutation):
    project = _project(tmp_path)
    payload = {
        "schema_version": "webnovel-learn-request/v1",
        "pattern_type": "other",
        "description": "有效经验",
        **mutation,
    }
    request = tmp_path / "request.json"
    request.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(LearnRequestError):
        load_learn_request(request, project_root=project)


def test_learn_request_rejects_inside_project_bom_relative_and_leaf_symlink(
    tmp_path, monkeypatch
):
    project = _project(tmp_path)
    payload = b'{"schema_version":"webnovel-learn-request/v1","pattern_type":"other","description":"ok"}'
    inside = project / "request.json"
    inside.write_bytes(payload)
    bom = tmp_path / "bom.json"
    bom.write_bytes(b"\xef\xbb\xbf" + payload)

    with pytest.raises(LearnRequestError, match="outside the novel project"):
        load_learn_request(inside, project_root=project)
    with pytest.raises(LearnRequestError, match="without BOM"):
        load_learn_request(bom, project_root=project)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(LearnRequestError, match="absolute path"):
        load_learn_request("bom.json", project_root=project)

    link = tmp_path / "request-link.json"
    original_is_symlink = Path.is_symlink

    def _is_symlink(path):
        return path == link or original_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", _is_symlink)
    with pytest.raises(LearnRequestError, match="non-symlink"):
        load_learn_request(link, project_root=project)


def test_learn_request_rejects_oversize_before_unbounded_read(tmp_path):
    project = _project(tmp_path)
    request = tmp_path / "oversize.json"
    request.write_bytes(b"{" + b"x" * MAX_REQUEST_BYTES)

    with pytest.raises(LearnRequestError, match="size"):
        load_learn_request(request, project_root=project)
