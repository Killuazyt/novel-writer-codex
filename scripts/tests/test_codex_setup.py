from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from data_modules import webnovel
from data_modules import codex_setup
from data_modules.codex_setup import (
    AGENT_SPECS,
    MANAGED_RECORD_RELATIVE,
    agent_spec,
    build_agent_artifacts,
    inspect_managed_agent,
    run_codex_setup,
)


def _fake_plugin_root(tmp_path: Path) -> Path:
    root = tmp_path / "plugin"
    contracts = root / "references" / "agents"
    contracts.mkdir(parents=True)
    for name, spec in AGENT_SPECS.items():
        (contracts / spec.contract_file).write_text(
            f"# {name}\n\nCanonical contract for {name}.\n",
            encoding="utf-8",
            newline="\n",
        )
    return root


def _agent_file(workspace: Path, name: str) -> Path:
    return workspace / ".codex" / "agents" / f"{name}.toml"


def _snapshot_tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _top_level_toml_values(content: str) -> dict[str, object]:
    """Parse the generator's intentionally flat JSON-compatible TOML values."""

    values: dict[str, object] = {}
    for line in content.splitlines():
        if not line or line.startswith("#"):
            continue
        key, raw = line.split(" = ", 1)
        values[key] = json.loads(raw)
    return values


def test_generator_embeds_each_canonical_contract_and_pins_routes() -> None:
    artifacts = build_agent_artifacts()

    assert list(artifacts) == list(AGENT_SPECS)
    for name, artifact in artifacts.items():
        assert not artifact.content.startswith("\ufeff")
        assert "\r" not in artifact.content
        values = _top_level_toml_values(artifact.content)
        assert values["name"] == name
        assert values["description"] == artifact.description
        assert values["sandbox_mode"] == artifact.sandbox_mode
        assert values["developer_instructions"] == artifact.contract_text
        assert artifact.contract_text == artifact.contract_path.read_text(encoding="utf-8")
        assert artifact.contract_sha256 == hashlib.sha256(
            artifact.contract_text.encode("utf-8")
        ).hexdigest()
        assert artifact.managed_sha256 == hashlib.sha256(
            artifact.content.encode("utf-8")
        ).hexdigest()

        if name == "webnovel_deconstruction_agent":
            assert "model" not in values
            assert "model_reasoning_effort" not in values
        else:
            assert values["model"] == "gpt-5.6-luna"
            assert values["model_reasoning_effort"] == "high"


def test_managed_hash_changes_with_contract_and_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin_root = _fake_plugin_root(tmp_path)
    original = build_agent_artifacts(plugin_root)["webnovel_context_agent"]

    original.contract_path.write_text(
        original.contract_text + "Additional semantic rule.\n",
        encoding="utf-8",
        newline="\n",
    )
    contract_changed = build_agent_artifacts(plugin_root)["webnovel_context_agent"]
    assert contract_changed.contract_sha256 != original.contract_sha256
    assert contract_changed.managed_sha256 != original.managed_sha256

    spec = AGENT_SPECS["webnovel_context_agent"]
    monkeypatch.setitem(
        AGENT_SPECS,
        "webnovel_context_agent",
        replace(spec, model="gpt-test-unavailable"),
    )
    model_changed = build_agent_artifacts(plugin_root)["webnovel_context_agent"]
    assert model_changed.contract_sha256 == contract_changed.contract_sha256
    assert model_changed.managed_sha256 != contract_changed.managed_sha256
    assert 'model = "gpt-test-unavailable"' in model_changed.content


def test_default_check_is_zero_write_for_windows_style_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "中文 空格 (项目) & Ω"
    workspace.mkdir()
    before = _snapshot_tree(workspace)

    code, result = run_codex_setup(workspace)

    assert code == 1
    assert result["status"] == "changes_required"
    assert len(result["created"]) == 5
    assert result["updated"] == []
    assert result["conflicts"] == []
    assert result["restart_required"] is False
    assert _snapshot_tree(workspace) == before
    assert not (workspace / ".codex").exists()


def test_apply_creates_manifest_and_is_byte_idempotent(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    code, applied = run_codex_setup(workspace, apply=True)

    assert code == 0
    assert applied["status"] == "applied"
    assert applied["restart_required"] is True
    assert applied["backup_dir"] is None
    assert len(applied["created"]) == 5
    manifest_path = workspace / MANAGED_RECORD_RELATIVE
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1
    assert manifest["manager"] == "novel-writer-codex"
    assert set(manifest["agents"]) == set(AGENT_SPECS)
    for name, entry in manifest["agents"].items():
        raw = _agent_file(workspace, name).read_bytes()
        assert entry["managed_sha256"] == hashlib.sha256(raw).hexdigest()
        assert entry["model"] == (
            None if name == "webnovel_deconstruction_agent" else "gpt-5.6-luna"
        )
        assert entry["model_reasoning_effort"] == (
            None if name == "webnovel_deconstruction_agent" else "high"
        )
    first_snapshot = _snapshot_tree(workspace)
    first_mtimes = {
        path.relative_to(workspace).as_posix(): path.stat().st_mtime_ns
        for path in workspace.rglob("*")
        if path.is_file()
    }

    check_code, checked = run_codex_setup(workspace)
    apply_code, applied_again = run_codex_setup(workspace, apply=True)

    assert check_code == 0
    assert checked["status"] == "current"
    assert len(checked["unchanged"]) == 5
    assert apply_code == 0
    assert applied_again["status"] == "applied"
    assert applied_again["created"] == []
    assert applied_again["updated"] == []
    assert len(applied_again["unchanged"]) == 5
    assert applied_again["backup_dir"] is None
    assert _snapshot_tree(workspace) == first_snapshot
    assert {
        path.relative_to(workspace).as_posix(): path.stat().st_mtime_ns
        for path in workspace.rglob("*")
        if path.is_file()
    } == first_mtimes


def test_unmanaged_same_named_agent_is_conflict_and_apply_is_zero_write(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    target = _agent_file(workspace, "webnovel_writer")
    target.parent.mkdir(parents=True)
    target.write_text('name = "my_writer"\n', encoding="utf-8")
    before = _snapshot_tree(workspace)

    check_code, checked = run_codex_setup(workspace)
    apply_code, applied = run_codex_setup(workspace, apply=True)

    assert check_code == apply_code == 1
    assert checked["status"] == applied["status"] == "conflict"
    assert any(
        conflict["reason"] == "unmanaged_existing_agent"
        for conflict in applied["conflicts"]
    )
    assert _snapshot_tree(workspace) == before
    assert not (workspace / MANAGED_RECORD_RELATIVE).exists()


def test_locally_modified_managed_agent_is_never_overwritten(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    assert run_codex_setup(workspace, apply=True)[0] == 0
    target = _agent_file(workspace, "webnovel_writer")
    target.write_text(target.read_text(encoding="utf-8") + "# user edit\n", encoding="utf-8")
    before = _snapshot_tree(workspace)

    code, result = run_codex_setup(workspace, apply=True)

    assert code == 1
    assert result["status"] == "conflict"
    assert any(
        conflict["reason"] == "managed_agent_modified"
        for conflict in result["conflicts"]
    )
    assert inspect_managed_agent(workspace, "writer")["status"] == "modified"
    assert inspect_managed_agent(workspace, "context")["status"] == "current"
    assert _snapshot_tree(workspace) == before
    assert result["backup_dir"] is None


def test_stale_managed_agent_is_backed_up_before_update(tmp_path: Path) -> None:
    plugin_root = _fake_plugin_root(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    assert run_codex_setup(
        workspace,
        apply=True,
        plugin_root=plugin_root,
    )[0] == 0
    target = _agent_file(workspace, "webnovel_reviewer")
    old_content = target.read_bytes()
    contract = plugin_root / "references" / "agents" / "webnovel_reviewer.md"
    contract.write_text(
        contract.read_text(encoding="utf-8") + "New review rule.\n",
        encoding="utf-8",
        newline="\n",
    )

    check_code, checked = run_codex_setup(
        workspace,
        plugin_root=plugin_root,
    )
    apply_code, applied = run_codex_setup(
        workspace,
        apply=True,
        plugin_root=plugin_root,
        now=datetime(2026, 8, 7, 9, 30, tzinfo=timezone.utc),
    )

    assert check_code == 1
    assert checked["updated"] == [
        ".codex/agents/webnovel_reviewer.toml"
    ]
    assert apply_code == 0
    assert applied["status"] == "applied"
    backup_dir = Path(applied["backup_dir"])
    assert backup_dir.name == "20260807T093000000000Z"
    assert (backup_dir / "webnovel_reviewer.toml").read_bytes() == old_content
    assert (backup_dir / "managed-agents.json").is_file()
    assert target.read_bytes() != old_content
    assert run_codex_setup(workspace, plugin_root=plugin_root)[1]["status"] == "current"


def test_invalid_managed_record_and_unsafe_parent_fail_closed(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    record = workspace / MANAGED_RECORD_RELATIVE
    record.parent.mkdir(parents=True)
    record.write_text("{not-json", encoding="utf-8")
    before = _snapshot_tree(workspace)

    code, result = run_codex_setup(workspace, apply=True)

    assert code == 1
    assert result["status"] == "conflict"
    assert result["conflicts"][0]["reason"] == "managed_record_invalid"
    assert _snapshot_tree(workspace) == before

    other = tmp_path / "other"
    other.mkdir()
    (other / ".codex").write_text("not a directory", encoding="utf-8")
    failed_code, failed = run_codex_setup(other, apply=True)
    assert failed_code == 2
    assert failed["status"] == "failed"
    assert failed["conflicts"][0]["reason"] == "managed_parent_not_directory"
    assert (other / ".codex").read_text(encoding="utf-8") == "not a directory"


def test_symlinked_codex_parent_cannot_escape_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    try:
        (workspace / ".codex").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")

    code, result = run_codex_setup(workspace, apply=True)

    assert code == 2
    assert result["status"] == "failed"
    assert result["conflicts"][0]["reason"] in {
        "managed_path_symlink",
        "managed_path_escape",
    }
    assert list(outside.iterdir()) == []


def test_inspection_helper_reports_contract_and_current_route(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    assert run_codex_setup(workspace, apply=True)[0] == 0

    context = inspect_managed_agent(workspace, "context")
    deconstruction = inspect_managed_agent(workspace, "deconstruction")
    context_spec = agent_spec("context")

    assert context["current"] is True
    assert context["requested_model"] == "gpt-5.6-luna"
    assert context["reasoning_effort"] == "high"
    assert context["contract_hash"] == context_spec["contract_hash"]
    assert Path(context["agent_file"]).is_file()
    assert deconstruction["current"] is True
    assert deconstruction["requested_model"] is None
    assert deconstruction["reasoning_effort"] is None


def test_unified_cli_defaults_to_check_and_emits_stable_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "工作区"
    workspace.mkdir()
    monkeypatch.setattr(
        "sys.argv",
        [
            "webnovel",
            "codex-setup",
            "--workspace-root",
            str(workspace),
            "--format",
            "json",
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        webnovel.main()

    assert exc_info.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {
        "schema_version",
        "status",
        "workspace_root",
        "created",
        "updated",
        "unchanged",
        "conflicts",
        "backup_dir",
        "restart_required",
    }
    assert payload["status"] == "changes_required"
    assert payload["schema_version"] == 1
    assert not (workspace / ".codex").exists()


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("missing", "contract_missing"),
        ("bom", "contract_bom_forbidden"),
        ("invalid_utf8", "contract_invalid_utf8"),
        ("empty", "contract_empty"),
    ],
)
def test_invalid_canonical_contract_fails_before_workspace_write(
    tmp_path: Path,
    mutation: str,
    reason: str,
) -> None:
    plugin_root = _fake_plugin_root(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    contract = (
        plugin_root
        / "references"
        / "agents"
        / AGENT_SPECS["webnovel_context_agent"].contract_file
    )
    if mutation == "missing":
        contract.unlink()
    elif mutation == "bom":
        contract.write_bytes(b"\xef\xbb\xbf# contract\n")
    elif mutation == "invalid_utf8":
        contract.write_bytes(b"\xff\xfe")
    else:
        contract.write_text(" \r\n", encoding="utf-8")

    code, result = run_codex_setup(
        workspace,
        apply=True,
        plugin_root=plugin_root,
    )

    assert code == 2
    assert result["status"] == "failed"
    assert result["conflicts"][0]["reason"] == reason
    assert not (workspace / ".codex").exists()


def test_unreadable_contract_and_unknown_agent_are_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin_root = _fake_plugin_root(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    blocked = (
        plugin_root
        / "references"
        / "agents"
        / AGENT_SPECS["webnovel_context_agent"].contract_file
    ).resolve()
    real_read_bytes = Path.read_bytes

    def denied(path: Path) -> bytes:
        if path.resolve() == blocked:
            raise OSError("simulated read denial")
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", denied)
    code, result = run_codex_setup(workspace, plugin_root=plugin_root)
    assert code == 2
    assert result["conflicts"][0]["reason"] == "contract_unreadable"
    with pytest.raises(KeyError, match="Unknown managed Codex agent"):
        agent_spec("not_an_agent", plugin_root)


@pytest.mark.parametrize("kind", ["empty", "missing", "file", "root"])
def test_invalid_workspace_roots_are_rejected(
    tmp_path: Path,
    kind: str,
) -> None:
    if kind == "empty":
        value: str | Path = ""
        expected = "workspace_root_empty"
    elif kind == "missing":
        value = tmp_path / "missing"
        expected = "workspace_root_invalid"
    elif kind == "file":
        value = tmp_path / "file.txt"
        value.write_text("x", encoding="utf-8")
        expected = "workspace_root_not_directory"
    else:
        value = Path(tmp_path.anchor)
        expected = "workspace_root_too_broad"

    code, result = run_codex_setup(value, apply=True)

    assert code == 2
    assert result["status"] == "failed"
    assert result["conflicts"][0]["reason"] == expected


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ([], "managed_record_invalid"),
        ({"schema_version": 99, "manager": "novel-writer-codex", "agents": {}}, "managed_record_schema_unsupported"),
        ({"schema_version": 1, "manager": "someone-else", "agents": {}}, "managed_record_owner_mismatch"),
        ({"schema_version": 1, "manager": "novel-writer-codex", "agents": []}, "managed_record_invalid"),
        ({"schema_version": 1, "manager": "novel-writer-codex", "agents": {"unknown": {}}}, "managed_record_unknown_agent"),
    ],
)
def test_managed_record_schema_is_strict_and_never_overwritten(
    tmp_path: Path,
    payload: object,
    reason: str,
) -> None:
    workspace = tmp_path / "workspace"
    record = workspace / MANAGED_RECORD_RELATIVE
    record.parent.mkdir(parents=True)
    original = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    record.write_bytes(original)

    code, result = run_codex_setup(workspace, apply=True)

    assert code == 1
    assert result["status"] == "conflict"
    assert result["conflicts"][0]["reason"] == reason
    assert record.read_bytes() == original


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("missing", "managed_entry_missing"),
        ("path", "managed_entry_path_mismatch"),
        ("hash", "managed_entry_hash_invalid"),
    ],
)
def test_managed_entry_must_authorize_the_exact_existing_agent(
    tmp_path: Path,
    mutation: str,
    reason: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    assert run_codex_setup(workspace, apply=True)[0] == 0
    record = workspace / MANAGED_RECORD_RELATIVE
    payload = json.loads(record.read_text(encoding="utf-8"))
    entry = payload["agents"]["webnovel_context_agent"]
    if mutation == "missing":
        del payload["agents"]["webnovel_context_agent"]
    elif mutation == "path":
        entry["path"] = ".codex/agents/someone_else.toml"
    else:
        entry["managed_sha256"] = "not-a-sha"
    record.write_text(json.dumps(payload), encoding="utf-8")
    before = _snapshot_tree(workspace)

    code, result = run_codex_setup(workspace, apply=True)

    assert code == 1
    assert any(item["reason"] == reason for item in result["conflicts"])
    assert _snapshot_tree(workspace) == before


def test_metadata_only_drift_repairs_manifest_without_rewriting_agent(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    assert run_codex_setup(workspace, apply=True)[0] == 0
    target = _agent_file(workspace, "webnovel_context_agent")
    content_before = target.read_bytes()
    mtime_before = target.stat().st_mtime_ns
    record = workspace / MANAGED_RECORD_RELATIVE
    payload = json.loads(record.read_text(encoding="utf-8"))
    payload["agents"]["webnovel_context_agent"]["model"] = "stale-metadata"
    record.write_text(json.dumps(payload), encoding="utf-8")

    check_code, checked = run_codex_setup(workspace)
    apply_code, applied = run_codex_setup(workspace, apply=True)

    assert check_code == 1
    assert checked["updated"] == [
        ".codex/agents/webnovel_context_agent.toml"
    ]
    assert apply_code == 0
    assert applied["backup_dir"] is None
    assert target.read_bytes() == content_before
    assert target.stat().st_mtime_ns == mtime_before
    repaired = json.loads(record.read_text(encoding="utf-8"))
    assert repaired["agents"]["webnovel_context_agent"]["model"] == "gpt-5.6-luna"


def test_unsafe_agent_target_and_unreadable_agent_are_conflicts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    directory_target = _agent_file(workspace, "webnovel_writer")
    directory_target.mkdir(parents=True)
    code, result = run_codex_setup(workspace, apply=True)
    assert code == 1
    assert result["conflicts"][0]["reason"] == "managed_agent_unsafe"

    other = tmp_path / "other"
    other.mkdir()
    assert run_codex_setup(other, apply=True)[0] == 0
    blocked = _agent_file(other, "webnovel_writer").resolve()
    real_read_bytes = Path.read_bytes

    def denied(path: Path) -> bytes:
        if path.resolve() == blocked:
            raise OSError("simulated agent read denial")
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", denied)
    denied_code, denied_result = run_codex_setup(other)
    assert denied_code == 1
    assert any(
        item["reason"] == "managed_agent_unreadable"
        for item in denied_result["conflicts"]
    )


def test_apply_failure_rolls_back_created_agents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    real_write = codex_setup._atomic_write_bytes
    calls = 0

    def fail_once(target: Path, raw: bytes) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("simulated disk error")
        real_write(target, raw)

    monkeypatch.setattr(codex_setup, "_atomic_write_bytes", fail_once)
    code, result = run_codex_setup(workspace, apply=True)

    assert code == 2
    assert result["status"] == "failed"
    assert result["conflicts"][0]["reason"] == "apply_failed"
    assert not (workspace / MANAGED_RECORD_RELATIVE).exists()
    assert list((workspace / ".codex" / "agents").glob("*.toml")) == []


def test_concurrent_manifest_change_is_reported_as_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def concurrent_change(plan: object, *, now: object = None) -> None:
        raise codex_setup.CodexSetupConflict(
            MANAGED_RECORD_RELATIVE.as_posix(),
            "concurrent_change",
            "changed during test",
        )

    monkeypatch.setattr(codex_setup, "_apply_plan", concurrent_change)
    code, result = run_codex_setup(workspace, apply=True)
    assert code == 1
    assert result["status"] == "conflict"
    assert result["conflicts"][0]["reason"] == "concurrent_change"
    assert not (workspace / ".codex").exists()


def test_text_renderer_and_generic_failure_are_stable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _, result = run_codex_setup(workspace)
    rendered = codex_setup.format_setup_result(result, "text")
    assert "codex-setup: changes_required" in rendered
    assert "created: 5" in rendered
    assert "restart_required: false" in rendered
    with pytest.raises(ValueError, match="Unsupported setup output format"):
        codex_setup.format_setup_result(result, "yaml")

    def invalid(_: object) -> Path:
        raise ValueError("simulated unexpected value")

    monkeypatch.setattr(codex_setup, "_resolve_workspace", invalid)
    code, failed = run_codex_setup(workspace)
    assert code == 2
    assert failed["conflicts"][0]["reason"] == "setup_failed"


def test_text_renderer_lists_structured_and_scalar_conflicts() -> None:
    rendered = codex_setup.format_setup_result(
        {
            "status": "conflict",
            "workspace_root": "C:/workspace",
            "created": [],
            "updated": [],
            "unchanged": [],
            "conflicts": [
                {
                    "path": ".codex/agents/webnovel_writer.toml",
                    "reason": "managed_agent_modified",
                    "detail": "user-owned bytes differ",
                },
                "legacy conflict detail",
            ],
            "backup_dir": None,
            "restart_required": False,
        },
        "text",
    )

    assert "managed_agent_modified - user-owned bytes differ" in rendered
    assert "legacy conflict detail" in rendered


def test_inspection_reports_manifest_conflict_for_otherwise_missing_agent(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    record = workspace / MANAGED_RECORD_RELATIVE
    record.parent.mkdir(parents=True)
    record.write_text("{not-json", encoding="utf-8")

    result = inspect_managed_agent(workspace, "context")

    assert result["status"] == "conflict"
    assert result["current"] is False


def test_replace_retry_recovers_and_then_exhausts_permission_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    sleeps: list[float] = []

    def eventually_replace(source: Path, target: Path) -> None:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise PermissionError("temporarily locked")

    monkeypatch.setattr(codex_setup.os, "replace", eventually_replace)
    monkeypatch.setattr(codex_setup.time, "sleep", sleeps.append)
    codex_setup._replace_with_retry(tmp_path / "temporary", tmp_path / "target")
    assert calls == 3
    assert sleeps == [0.02, 0.04]

    calls = 0

    def always_locked(source: Path, target: Path) -> None:
        nonlocal calls
        calls += 1
        raise PermissionError("still locked")

    monkeypatch.setattr(codex_setup.os, "replace", always_locked)
    with pytest.raises(PermissionError, match="still locked"):
        codex_setup._replace_with_retry(tmp_path / "temporary", tmp_path / "target")
    assert calls == 10


def test_setup_plan_detects_real_concurrent_manifest_and_agent_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    artifacts = build_agent_artifacts()

    manifest_plan = codex_setup._build_plan(workspace, artifacts)
    manifest_plan.manifest_path.parent.mkdir(parents=True)
    manifest_plan.manifest_path.write_text("{}", encoding="utf-8")
    with pytest.raises(codex_setup.CodexSetupConflict, match="managed record changed"):
        codex_setup._verify_plan_unchanged(manifest_plan)

    manifest_plan.manifest_path.unlink()
    appeared_plan = codex_setup._build_plan(workspace, artifacts)
    appeared = workspace / artifacts["webnovel_writer"].relative_path
    appeared.parent.mkdir(parents=True)
    appeared.write_text("new concurrent agent", encoding="utf-8")
    with pytest.raises(codex_setup.CodexSetupConflict, match="agent appeared"):
        codex_setup._verify_plan_unchanged(appeared_plan)

    appeared.unlink()
    assert run_codex_setup(workspace, apply=True)[0] == 0
    changed_plan = codex_setup._build_plan(workspace, artifacts)
    target = _agent_file(workspace, "webnovel_writer")
    target.write_text("changed concurrently", encoding="utf-8")
    with pytest.raises(codex_setup.CodexSetupConflict, match="agent changed"):
        codex_setup._verify_plan_unchanged(changed_plan)

    target.write_bytes(changed_plan.old_agent_bytes["webnovel_writer"])
    reread_plan = codex_setup._build_plan(workspace, artifacts)
    real_read_bytes = Path.read_bytes

    def denied(path: Path) -> bytes:
        if path.resolve() == target.resolve():
            raise OSError("simulated concurrent read denial")
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", denied)
    with pytest.raises(codex_setup.CodexSetupConflict, match="cannot be re-read"):
        codex_setup._verify_plan_unchanged(reread_plan)


def test_managed_relative_path_rejects_parent_traversal(tmp_path: Path) -> None:
    workspace = tmp_path.resolve()
    with pytest.raises(codex_setup.CodexSetupError, match="must stay relative"):
        codex_setup._safe_workspace_path(workspace, Path("../escape.toml"))
