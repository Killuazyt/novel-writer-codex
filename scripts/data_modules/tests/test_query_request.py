from __future__ import annotations

import json
from pathlib import Path

import pytest

from data_modules.query_request import QueryRequestError, load_query_request


def _project(tmp_path):
    project = tmp_path / "novel"
    project.mkdir()
    return project


def test_query_request_preserves_unicode_newlines_and_shell_metacharacters(tmp_path):
    project = _project(tmp_path)
    entity = "韩立\"'\n&;|$()`"
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "schema_version": "webnovel-query-request/v1",
                "query_type": "entity_state",
                "entity": entity,
                "at_chapter": 35,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = load_query_request(
        request, project_root=project, expected_query_types={"entity_state"}
    )

    assert result["entity"] == entity
    assert result["at_chapter"] == 35


@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": "wrong", "query_type": "world_rules"},
        {
            "schema_version": "webnovel-query-request/v1",
            "query_type": "world_rules",
            "unknown": "field",
        },
        {
            "schema_version": "webnovel-query-request/v1",
            "query_type": "comprehensive_context",
            "chapter": True,
        },
    ],
)
def test_query_request_rejects_schema_unknown_fields_and_wrong_types(tmp_path, payload):
    project = _project(tmp_path)
    request = tmp_path / "request.json"
    request.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(QueryRequestError):
        load_query_request(
            request,
            project_root=project,
            expected_query_types={str(payload.get("query_type") or "world_rules")},
        )


def test_query_request_rejects_project_files_bom_and_relative_paths(tmp_path, monkeypatch):
    project = _project(tmp_path)
    inside = project / "request.json"
    inside.write_text(
        '{"schema_version":"webnovel-query-request/v1","query_type":"world_rules"}',
        encoding="utf-8",
    )
    bom = tmp_path / "bom.json"
    bom.write_bytes(
        b"\xef\xbb\xbf"
        + b'{"schema_version":"webnovel-query-request/v1","query_type":"world_rules"}'
    )

    with pytest.raises(QueryRequestError, match="outside the novel project"):
        load_query_request(inside, project_root=project, expected_query_types={"world_rules"})
    with pytest.raises(QueryRequestError, match="without BOM"):
        load_query_request(bom, project_root=project, expected_query_types={"world_rules"})
    monkeypatch.chdir(tmp_path)
    with pytest.raises(QueryRequestError, match="absolute path"):
        load_query_request("bom.json", project_root=project, expected_query_types={"world_rules"})


def test_query_request_rejects_leaf_symlink_before_resolution(tmp_path, monkeypatch):
    project = _project(tmp_path)
    request = tmp_path / "request-link.json"
    original_is_symlink = Path.is_symlink

    def _is_symlink(path):
        if path == request:
            return True
        return original_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", _is_symlink)

    with pytest.raises(QueryRequestError, match="non-symlink"):
        load_query_request(
            request,
            project_root=project,
            expected_query_types={"entity_state"},
        )
