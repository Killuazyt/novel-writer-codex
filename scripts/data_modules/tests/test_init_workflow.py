from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from argparse import Namespace
from copy import deepcopy
from pathlib import Path

import pytest

import data_modules.init_workflow as init_workflow
from data_modules.codex_m3_smoke import derive_agent_task_name
from data_modules.init_request import (
    build_reference_adoption_confirmation,
    build_reference_binding_marker,
    load_init_request,
)
from data_modules.init_workflow import InitWorkflowError, apply_init as _runtime_apply_init, preview_init
from data_modules.project_phase import PHASE_PLAN_IN_PROGRESS, resolve_project_phase
from data_modules.tests.test_init_request import valid_init_payload, write_request


CURRENT_CODEX_THREAD_ID = "01911111-1111-7111-8111-111111111111"
OTHER_CODEX_THREAD_ID = "01922222-2222-7222-8222-222222222222"


def tree_snapshot(root: Path) -> dict[str, str]:
    if not root.exists():
        return {}
    snapshot: dict[str, str] = {}
    for path in sorted(root.rglob("*"), key=lambda item: str(item)):
        relative = str(path.relative_to(root)).replace("\\", "/")
        if path.is_symlink():
            snapshot[relative] = "symlink"
        elif path.is_dir():
            snapshot[relative] = "directory"
        elif path.is_file():
            snapshot[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return snapshot


def prepared_request(tmp_path: Path, monkeypatch, *, slug: str = "星火长夜") -> tuple[Path, Path, Path, dict]:
    home = tmp_path / "isolated webnovel home"
    workspace = tmp_path / "中文 工作区 (A) & B 🚀"
    workspace.mkdir()
    monkeypatch.setenv("WEBNOVEL_HOME", str(home))
    monkeypatch.setenv("CODEX_THREAD_ID", CURRENT_CODEX_THREAD_ID)
    payload = valid_init_payload(workspace)
    payload["project_slug"] = slug
    request_file = write_request(home, payload)
    return request_file, workspace, workspace / slug, payload


def _strict_deconstruction_output(payload: dict, source_path: Path) -> dict:
    selected = payload["constraints"]["selected_idea"]
    transformation = "只采用压力递增结构，人物、地点和事件全部重构"
    return {
        "source": {
            "title": "参考作品",
            "platform": "本地文本",
            "input_type": "full_text",
            "text_path": str(source_path.resolve()),
        },
        "analysis_mode": "deep",
        "reader_promise": {
            "core_desire": "守住共同体",
            "promise_delivery": "代价递增",
            "risk": "避免复刻人物与事件",
        },
        "opening_hook_patterns": [],
        "cool_point_loops": [],
        "protagonist_patterns": [],
        "antagonist_pressure_patterns": [],
        "pacing_notes": {
            "golden_three": "三章建立代价",
            "arc_cycle": "每十章升级压力",
            "information_density": "逐级公开",
            "chapter_end_strategy": "以选择收束",
        },
        "borrowable_structures": [],
        "do_not_copy": ["原作人物名"],
        "differentiation_requirements": ["改写人物、地点与事件"],
        "init_candidates": [
            {
                "one_liner": selected["one_liner"],
                "anti_trope": selected["anti_trope"],
                "hard_constraints": list(selected["hard_constraints"]),
                "protagonist_flaw": selected["protagonist_flaw"],
                "antagonist_mirror": selected["antagonist_mirror"],
                "opening_hook": selected["opening_hook"],
                "source_patterns_used": ["压力递增"],
                "transformation_notes": transformation,
            }
        ],
        "quality": {
            "confidence": 0.91,
            "coverage": 0.9,
            "overlap": 0.1,
            "passed": True,
            "warnings": [],
        },
        "resume_state": {
            "current_stage": "complete",
            "processed_chapters": [1],
            "next_action": "present candidates",
            "character_merges": [],
            "quality_checks": ["passed"],
        },
        "orphan_plot_fallback": [],
        "canon_contamination_warnings": ["禁止复刻原作名场面"],
    }


def _write_reference_rollout(
    sessions_root: Path,
    *,
    child_thread_id: str,
    parent_thread_id: str,
    parent_model: str,
    effort: str,
    binding_marker: str,
    output: dict,
    agent_path: str,
    depth: int = 1,
    include_legacy_marker: bool = False,
    extra_final: bool = False,
) -> Path:
    path = sessions_root / "2026" / "08" / "08" / f"rollout-test-{child_thread_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    events = [
        {
            "type": "session_meta",
            "payload": {
                "id": child_thread_id,
                "parent_thread_id": parent_thread_id,
                "model": parent_model,
                "originator": "codex_desktop",
                "thread_source": "subagent",
                "source": {
                    "subagent": {
                        "thread_spawn": {
                            "parent_thread_id": parent_thread_id,
                            "depth": depth,
                            "agent_path": agent_path,
                            "agent_nickname": "deconstruction",
                            "agent_role": "webnovel_deconstruction_agent",
                        }
                    }
                },
            },
        },
        {
            "type": "turn_context",
            "payload": {"turn_id": "turn-1", "model": parent_model, "effort": effort},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": '{"ignored":"commentary"}'}],
                "phase": "commentary",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps(output, ensure_ascii=False, sort_keys=True),
                    }
                ],
                "phase": "final_answer",
            },
        },
    ]
    if include_legacy_marker:
        events.insert(
            2,
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": binding_marker}],
                    "phase": "commentary",
                },
            },
        )
    if extra_final:
        events.append(deepcopy(events[-1]))
    path.write_text(
        "\n".join(json.dumps(event, ensure_ascii=False, separators=(",", ":")) for event in events)
        + "\n",
        encoding="utf-8",
    )
    return path


def _write_parent_rollout(
    sessions_root: Path,
    *,
    parent_thread_id: str,
    parent_model: str,
    effort: str,
) -> Path:
    path = sessions_root / "2026" / "08" / "08" / f"rollout-test-{parent_thread_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    events = [
        {
            "type": "session_meta",
            "payload": {"id": parent_thread_id, "model": parent_model},
        },
        {
            "type": "turn_context",
            "payload": {"turn_id": "parent-turn-1", "model": parent_model, "effort": effort},
        },
    ]
    path.write_text(
        "\n".join(json.dumps(event, ensure_ascii=False, separators=(",", ":")) for event in events)
        + "\n",
        encoding="utf-8",
    )
    return path


def _append_parent_reference_choice(path: Path, *, marker: str, answer: str = "Adopt") -> None:
    events = [
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": marker}],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": answer}],
            },
        },
    ]
    with path.open("a", encoding="utf-8", newline="") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")


def _attach_strict_reference(
    payload: dict,
    *,
    workspace: Path,
    target: Path,
    sessions_root: Path,
    agent_path: str | None = None,
    depth: int = 1,
    task_name_marker: str | None = None,
    include_legacy_marker: bool = False,
    extra_final: bool = False,
) -> None:
    source = workspace / "reference source.txt"
    source.write_text("可靠参考正文：这里只提供结构，不提供可复制的专名。", encoding="utf-8")
    output = _strict_deconstruction_output(payload, source)
    parent_model = "gpt-5.6-sol"
    effort = "high"
    route = init_workflow.build_workflow_route(
        "init_reference",
        parent_model=parent_model,
        parent_reasoning_effort=effort,
        plugin_root=Path(init_workflow.__file__).resolve().parents[2],
    )
    step = route["steps"][0]
    child_thread_id = "child-init-reference-001"
    parent_thread_id = os.environ["CODEX_THREAD_ID"]
    parent_rollout = _write_parent_rollout(
        sessions_root,
        parent_thread_id=parent_thread_id,
        parent_model=parent_model,
        effort=effort,
    )
    runtime = {
        "rollout_path": str(
            sessions_root
            / "2026"
            / "08"
            / "08"
            / f"rollout-test-{child_thread_id}.jsonl"
        ),
        "sessions_root": str(sessions_root.resolve()),
        "child_thread_id": child_thread_id,
        "parent_thread_id": parent_thread_id,
        "parent_model": parent_model,
        "parent_reasoning_effort": effort,
        "parent_rollout_path": str(parent_rollout.resolve()),
        "parent_identity_sha256": init_workflow._canonical_sha256(
            {
                "rollout_path": str(parent_rollout.resolve()),
                "thread_id": parent_thread_id,
                "model": parent_model,
                "reasoning_effort": effort,
            }
        ),
        "parent_rollout_sha256": hashlib.sha256(parent_rollout.read_bytes()).hexdigest(),
        "rollout_sha256": "0" * 64,
    }
    reference = {
        "status": "adopted",
        "candidate_id": "candidate-safe",
        "source_title": "参考作品",
        "source_path": str(source.resolve()),
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "output_sha256": init_workflow._canonical_sha256(output),
        "confidence": 0.91,
        "transformation_notes": "只采用压力递增结构，人物、地点和事件全部重构",
        "do_not_copy": ["原作人物名"],
        "canon_contamination_warnings": ["禁止复刻原作名场面"],
        "route_sha256": init_workflow._canonical_sha256(route),
        "contract_hash": step["contract_hash"],
        "deconstruction_output": output,
        "runtime": runtime,
    }
    reference["binding_marker"] = build_reference_binding_marker(reference)
    task_name = derive_agent_task_name(
        task_name_marker or reference["binding_marker"],
        prefix="wni",
    )
    rollout = _write_reference_rollout(
        sessions_root,
        child_thread_id=child_thread_id,
        parent_thread_id=parent_thread_id,
        parent_model=parent_model,
        effort=effort,
        binding_marker=reference["binding_marker"],
        output=output,
        agent_path=agent_path or f"/root/{task_name}",
        depth=depth,
        include_legacy_marker=include_legacy_marker,
        extra_final=extra_final,
    )
    runtime["rollout_sha256"] = hashlib.sha256(rollout.read_bytes()).hexdigest()
    payload["constraints"]["selected_idea"]["origin"] = "mixed"
    provisional_confirmation = build_reference_adoption_confirmation(
        project_root=str(target.resolve()),
        selected_idea=payload["constraints"]["selected_idea"],
        reference_candidate=reference,
    )
    _append_parent_reference_choice(
        parent_rollout,
        marker=provisional_confirmation["choice_marker"],
    )
    runtime["parent_rollout_sha256"] = hashlib.sha256(parent_rollout.read_bytes()).hexdigest()
    reference["user_confirmation"] = build_reference_adoption_confirmation(
        project_root=str(target.resolve()),
        selected_idea=payload["constraints"]["selected_idea"],
        reference_candidate=reference,
    )
    payload["reference_candidate"] = reference


def _write_apply_authorization(
    config_json: str | Path,
    preview: dict,
    *,
    answer: str = "Apply",
) -> Path:
    request_file = Path(config_json)
    request = load_init_request(request_file)
    reference = request.get("reference_candidate") or {}
    reference_runtime = reference.get("runtime") or {}
    if reference.get("status") == "adopted":
        sessions_root = Path(reference_runtime["sessions_root"])
        parent_rollout = Path(reference_runtime["parent_rollout_path"])
        parent_thread_id = reference_runtime["parent_thread_id"]
        parent_model = reference_runtime["parent_model"]
        effort = reference_runtime["parent_reasoning_effort"]
    else:
        sessions_root = request_file.parent / "trusted-apply-sessions"
        sessions_root.mkdir(exist_ok=True)
        parent_thread_id = os.environ["CODEX_THREAD_ID"]
        parent_model = "gpt-5.6-sol"
        effort = "high"
        parent_rollout = _write_parent_rollout(
            sessions_root,
            parent_thread_id=parent_thread_id,
            parent_model=parent_model,
            effort=effort,
        )
    init_workflow.TRUSTED_CODEX_SESSIONS_ROOT = sessions_root
    marker = preview["apply_choice"]["choice_marker"]
    _append_parent_reference_choice(parent_rollout, marker=marker, answer=answer)
    authorization = {
        "schema_version": "webnovel-init-apply-authorization/v1",
        "preview_token": preview["preview_token"],
        "choice_request_id": preview["apply_choice"]["choice_request"]["request_id"],
        "choice_marker_sha256": hashlib.sha256(marker.encode("utf-8")).hexdigest(),
        "runtime": {
            "sessions_root": str(sessions_root.resolve()),
            "parent_rollout_path": str(parent_rollout.resolve()),
            "parent_thread_id": parent_thread_id,
            "parent_model": parent_model,
            "parent_reasoning_effort": effort,
            "parent_rollout_sha256": hashlib.sha256(parent_rollout.read_bytes()).hexdigest(),
        },
    }
    path = request_file.parent / f"apply-authorization-{preview['preview_token'][:16]}.json"
    path.write_text(json.dumps(authorization, ensure_ascii=False), encoding="utf-8")
    return path


def apply_init(
    config_json: str | Path,
    *,
    git_mode: str | None,
    preview_token: str | None,
    authorization_json: str | Path | None = None,
) -> dict:
    """Test helper that supplies a real synthetic parent rollout receipt."""

    if authorization_json is None and git_mode in init_workflow.GIT_MODES and isinstance(
        preview_token, str
    ) and len(preview_token) == 64:
        preview = preview_init(config_json, git_mode=git_mode)
        authorization_json = _write_apply_authorization(config_json, preview)
    return _runtime_apply_init(
        config_json,
        git_mode=git_mode,
        preview_token=preview_token,
        authorization_json=authorization_json,
    )


def test_init_preview_is_true_zero_write_and_lists_exact_target(tmp_path, monkeypatch):
    request_file, workspace, target, _ = prepared_request(tmp_path, monkeypatch)
    before_workspace = tree_snapshot(workspace)
    before_home = tree_snapshot(request_file.parents[2])

    preview = preview_init(request_file, git_mode="off")

    assert preview["schema_version"] == "webnovel-init-preview/v1"
    assert preview["status"] == "ready"
    assert preview["project_root"] == str(target.resolve())
    assert preview["git_mode"] == "off"
    assert ".webnovel/state.json" in preview["write_list"]
    assert ".webnovel/idea_bank.json" in preview["write_list"]
    assert ".story-system/MASTER_SETTING.json" in preview["write_list"]
    assert ".gitignore" not in preview["write_list"]
    assert not target.exists()
    assert tree_snapshot(workspace) == before_workspace
    assert tree_snapshot(request_file.parents[2]) == before_home


def test_init_apply_requires_token_and_explicit_git_mode(tmp_path, monkeypatch):
    request_file, _, target, _ = prepared_request(tmp_path, monkeypatch)
    preview = preview_init(request_file, git_mode="off")

    with pytest.raises(InitWorkflowError, match="explicit git_mode"):
        apply_init(request_file, git_mode=None, preview_token=preview["preview_token"])
    with pytest.raises(InitWorkflowError, match="preview_token"):
        apply_init(request_file, git_mode="off", preview_token=None)
    with pytest.raises(InitWorkflowError, match="stale"):
        apply_init(request_file, git_mode="init", preview_token=preview["preview_token"])
    assert not target.exists()


def test_init_apply_requires_real_parent_rollout_authorization(tmp_path, monkeypatch):
    request_file, _, target, _ = prepared_request(tmp_path, monkeypatch)
    preview = preview_init(request_file, git_mode="off")

    with pytest.raises(InitWorkflowError) as exc:
        _runtime_apply_init(
            request_file,
            git_mode="off",
            preview_token=preview["preview_token"],
            authorization_json=None,
        )
    assert exc.value.code == "apply_authorization_required"
    assert not target.exists()


@pytest.mark.parametrize("current_thread_id", [None, "not-a-uuid"])
def test_init_apply_requires_valid_host_current_thread_without_writes(
    tmp_path,
    monkeypatch,
    current_thread_id,
):
    request_file, _, target, _ = prepared_request(tmp_path, monkeypatch)
    preview = preview_init(request_file, git_mode="off")
    authorization = _write_apply_authorization(request_file, preview)
    if current_thread_id is None:
        monkeypatch.delenv("CODEX_THREAD_ID")
    else:
        monkeypatch.setenv("CODEX_THREAD_ID", current_thread_id)

    with pytest.raises(InitWorkflowError) as exc:
        _runtime_apply_init(
            request_file,
            git_mode="off",
            preview_token=preview["preview_token"],
            authorization_json=authorization,
        )
    assert exc.value.code == "apply_authorization_invalid"
    assert "CODEX_THREAD_ID is missing or is not a canonical UUID" in str(exc.value)
    assert not target.exists()


def test_init_apply_rejects_coherent_other_parent_task_without_writes(tmp_path, monkeypatch):
    request_file, _, target, _ = prepared_request(tmp_path, monkeypatch)
    preview = preview_init(request_file, git_mode="off")
    monkeypatch.setenv("CODEX_THREAD_ID", OTHER_CODEX_THREAD_ID)
    authorization = _write_apply_authorization(request_file, preview)
    monkeypatch.setenv("CODEX_THREAD_ID", CURRENT_CODEX_THREAD_ID)

    with pytest.raises(InitWorkflowError) as exc:
        _runtime_apply_init(
            request_file,
            git_mode="off",
            preview_token=preview["preview_token"],
            authorization_json=authorization,
        )
    assert exc.value.code == "apply_authorization_invalid"
    assert "parent thread does not match the current Codex task" in str(exc.value)
    assert not target.exists()


def test_init_apply_rejects_child_rollout_as_parent_authorization(tmp_path, monkeypatch):
    request_file, _, target, _ = prepared_request(tmp_path, monkeypatch)
    preview = preview_init(request_file, git_mode="off")
    authorization_path = _write_apply_authorization(request_file, preview)
    authorization = json.loads(authorization_path.read_text(encoding="utf-8"))
    parent_rollout = Path(authorization["runtime"]["parent_rollout_path"])
    events = [json.loads(line) for line in parent_rollout.read_text(encoding="utf-8").splitlines()]
    events[0]["payload"]["parent_thread_id"] = "upstream-parent"
    events[0]["payload"]["source"] = {
        "subagent": {"thread_spawn": {"parent_thread_id": "upstream-parent"}}
    }
    parent_rollout.write_text(
        "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n",
        encoding="utf-8",
    )
    authorization["runtime"]["parent_rollout_sha256"] = hashlib.sha256(
        parent_rollout.read_bytes()
    ).hexdigest()
    authorization_path.write_text(json.dumps(authorization, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(InitWorkflowError) as exc:
        _runtime_apply_init(
            request_file,
            git_mode="off",
            preview_token=preview["preview_token"],
            authorization_json=authorization_path,
        )
    assert exc.value.code == "apply_authorization_invalid"
    assert "top-level Codex task" in str(exc.value)
    assert not target.exists()


@pytest.mark.parametrize("answer", ["Revise", "Cancel", "自行执行"])
def test_init_apply_rejects_non_apply_or_freeform_parent_answers(
    tmp_path,
    monkeypatch,
    answer,
):
    request_file, _, target, _ = prepared_request(tmp_path, monkeypatch)
    preview = preview_init(request_file, git_mode="off")
    authorization = _write_apply_authorization(request_file, preview, answer=answer)

    with pytest.raises(InitWorkflowError) as exc:
        _runtime_apply_init(
            request_file,
            git_mode="off",
            preview_token=preview["preview_token"],
            authorization_json=authorization,
        )
    assert exc.value.code == "apply_authorization_invalid"
    assert not target.exists()


def test_init_apply_authorization_is_bound_to_exact_preview_marker(tmp_path, monkeypatch):
    request_file, _, target, _ = prepared_request(tmp_path, monkeypatch)
    preview = preview_init(request_file, git_mode="off")
    authorization = _write_apply_authorization(request_file, preview)
    payload = json.loads(authorization.read_text(encoding="utf-8"))
    payload["choice_marker_sha256"] = "0" * 64
    authorization.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(InitWorkflowError) as exc:
        _runtime_apply_init(
            request_file,
            git_mode="off",
            preview_token=preview["preview_token"],
            authorization_json=authorization,
        )
    assert exc.value.code == "apply_authorization_invalid"
    assert not target.exists()


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("relative", "must be absolute"),
        ("outside", "must stay under"),
        ("bom", "without BOM"),
        ("invalid-json", "one UTF-8 JSON object"),
        ("shape", "top-level shape"),
        ("schema", "schema is invalid"),
        ("runtime-shape", "runtime has an invalid shape"),
        ("blank-runtime", "runtime fields must be non-empty"),
        ("invalid-hash", "must be a lowercase SHA-256"),
    ],
)
def test_apply_authorization_file_contract_fails_closed(
    tmp_path,
    monkeypatch,
    case,
    message,
):
    request_file, _, _, _ = prepared_request(tmp_path, monkeypatch)
    preview = preview_init(request_file, git_mode="off")
    authorization = _write_apply_authorization(request_file, preview)

    if case == "relative":
        candidate = Path("relative-authorization.json")
    elif case == "outside":
        candidate = tmp_path / "outside-authorization.json"
        candidate.write_bytes(authorization.read_bytes())
    else:
        candidate = authorization
        if case == "bom":
            candidate.write_bytes(b"\xef\xbb\xbf" + candidate.read_bytes())
        elif case == "invalid-json":
            candidate.write_bytes(b"{")
        else:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
            if case == "shape":
                payload["unknown"] = True
            elif case == "schema":
                payload["schema_version"] = "wrong"
            elif case == "runtime-shape":
                payload["runtime"].pop("parent_model")
            elif case == "blank-runtime":
                payload["runtime"]["parent_model"] = ""
            elif case == "invalid-hash":
                payload["preview_token"] = "not-a-hash"
            candidate.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(InitWorkflowError, match=message):
        init_workflow._load_apply_authorization(candidate)


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("stale-token", "preview_token is stale"),
        ("missing-choice", "lacks its finite Apply choice"),
        ("missing-sessions", "sessions root is unavailable"),
        ("untrusted-sessions", "sessions root is not host-owned"),
        ("escaped-rollout", "escaped the trusted sessions root"),
        ("identity", "parent rollout identity is invalid"),
        ("changed-rollout", "changed during verification"),
        ("prefix", "authorization-prefix hash does not match"),
    ],
)
def test_apply_authorization_runtime_provenance_fails_closed(
    tmp_path,
    monkeypatch,
    case,
    message,
):
    request_file, _, _, _ = prepared_request(tmp_path, monkeypatch)
    preview = preview_init(request_file, git_mode="off")
    authorization = _write_apply_authorization(request_file, preview)
    payload = json.loads(authorization.read_text(encoding="utf-8"))
    checked_preview = deepcopy(preview)

    if case == "stale-token":
        payload["preview_token"] = "0" * 64
    elif case == "missing-choice":
        checked_preview.pop("apply_choice")
    elif case == "missing-sessions":
        payload["runtime"]["sessions_root"] = str((tmp_path / "missing-sessions").resolve())
    elif case == "untrusted-sessions":
        other_sessions = tmp_path / "other-sessions"
        other_sessions.mkdir()
        payload["runtime"]["sessions_root"] = str(other_sessions.resolve())
    elif case == "escaped-rollout":
        escaped = tmp_path / "escaped-rollout.jsonl"
        escaped.write_bytes(Path(payload["runtime"]["parent_rollout_path"]).read_bytes())
        payload["runtime"]["parent_rollout_path"] = str(escaped.resolve())
    elif case == "identity":
        payload["runtime"]["parent_model"] = "gpt-5.6-terra"
    elif case == "prefix":
        payload["runtime"]["parent_rollout_sha256"] = "0" * 64
    authorization.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    if case == "changed-rollout":
        real_reader = init_workflow._stable_regular_bytes
        parent_reads = 0

        def raced_reader(path, *, max_bytes, label, allow_empty=False):
            nonlocal parent_reads
            raw, resolved = real_reader(
                path,
                max_bytes=max_bytes,
                label=label,
                allow_empty=allow_empty,
            )
            if label == "init Apply parent rollout":
                parent_reads += 1
                if parent_reads == 2:
                    return raw + b"\n", resolved
            return raw, resolved

        monkeypatch.setattr(init_workflow, "_stable_regular_bytes", raced_reader)

    with pytest.raises(InitWorkflowError, match=message):
        init_workflow._validate_apply_authorization(authorization, checked_preview)


def test_parent_choice_parser_covers_malformed_and_filtered_events(tmp_path, monkeypatch):
    request_file, _, _, _ = prepared_request(tmp_path, monkeypatch)
    preview = preview_init(request_file, git_mode="off")
    choice = preview["apply_choice"]
    marker = choice["choice_marker"]
    choice_request = choice["choice_request"]

    with pytest.raises(InitWorkflowError, match="lacks a finite-choice"):
        init_workflow._resolve_parent_choice(
            b"",
            marker="",
            choice_request=choice_request,
            question_id="init_action",
            accepted_option="apply",
            label="Init Apply",
        )
    with pytest.raises(InitWorkflowError, match="not UTF-8 JSONL"):
        init_workflow._resolve_parent_choice(
            b"\n{",
            marker=marker,
            choice_request=choice_request,
            question_id="init_action",
            accepted_option="apply",
            label="Init Apply",
        )

    events = [
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": marker,
            },
        },
        {"type": "turn_context", "payload": {}},
        {
            "type": "response_item",
            "payload": {"type": "message", "role": "assistant", "content": "continue"},
        },
        {
            "type": "response_item",
            "payload": {"type": "message", "role": "user", "content": "Apply"},
        },
    ]
    raw = (
        "\n".join(json.dumps(event, ensure_ascii=False, separators=(",", ":")) for event in events)
        + "\n"
    ).encode("utf-8")
    proof = init_workflow._resolve_parent_choice(
        raw,
        marker=marker,
        choice_request=choice_request,
        question_id="init_action",
        accepted_option="apply",
        label="Init Apply",
    )
    assert proof["answer"] == "Apply"

    with pytest.raises(InitWorkflowError, match="choice answer is invalid"):
        init_workflow._resolve_parent_choice(
            raw,
            marker=marker,
            choice_request={},
            question_id="init_action",
            accepted_option="apply",
            label="Init Apply",
        )


def test_init_apply_creates_consistent_plan_ready_project_and_is_idempotent(tmp_path, monkeypatch):
    request_file, _, target, payload = prepared_request(tmp_path, monkeypatch)
    preview = preview_init(request_file, git_mode="off")

    result = apply_init(
        request_file,
        git_mode="off",
        preview_token=preview["preview_token"],
    )

    assert result["schema_version"] == "webnovel-init-result/v1"
    assert result["status"] == "success"
    assert result["plan_precondition_ready"] is True
    assert not (target / ".git").exists()
    state = json.loads((target / ".webnovel" / "state.json").read_text(encoding="utf-8"))
    idea = json.loads((target / ".webnovel" / "idea_bank.json").read_text(encoding="utf-8"))
    master = json.loads(
        (target / ".story-system" / "MASTER_SETTING.json").read_text(encoding="utf-8")
    )
    assert state["project_info"]["title"] == payload["project"]["title"]
    assert state["project_info"]["genre"] == "玄幻"
    assert idea["selected_idea"]["one_liner"] == payload["project"]["one_liner"]
    assert master["init_constraints"]["anti_trope"] == payload["constraints"]["selected_idea"]["anti_trope"]
    assert payload["project"]["one_liner"] in (target / "大纲" / "总纲.md").read_text(encoding="utf-8")
    phase = resolve_project_phase(target)
    assert phase.phase == PHASE_PLAN_IN_PROGRESS
    assert phase.missing_init_files == ()

    before = tree_snapshot(target)
    second_preview = preview_init(request_file, git_mode="off")
    assert second_preview["status"] == "ready"
    assert second_preview["write_list"] == []
    second = apply_init(
        request_file,
        git_mode="off",
        preview_token=second_preview["preview_token"],
    )
    assert second["created_files"] == []
    assert tree_snapshot(target) == before


def test_init_apply_routes_urban_mystery_to_suspense_contracts(tmp_path, monkeypatch):
    request_file, _, target, payload = prepared_request(tmp_path, monkeypatch)
    payload["project"]["genre"] = "都市悬疑"
    write_request(request_file.parents[2], payload)

    preview = preview_init(request_file, git_mode="off")
    result = apply_init(
        request_file,
        git_mode="off",
        preview_token=preview["preview_token"],
    )

    assert result["status"] == "success"
    state = json.loads((target / ".webnovel" / "state.json").read_text(encoding="utf-8"))
    master = json.loads(
        (target / ".story-system" / "MASTER_SETTING.json").read_text(encoding="utf-8")
    )
    assert state["project_info"]["genre"] == "悬疑"
    assert state["project_info"]["genre_tags"]["route"] == ["悬疑推理"]
    assert master["route"]["primary_genre"] == "悬疑推理"
    assert master["route"]["canonical_genre"] == "悬疑"
    assert master["route"]["route_source"] == "keyword_or_alias_match"


def test_init_rerun_preserves_user_markdown_and_only_fills_missing_file(tmp_path, monkeypatch):
    request_file, _, target, _ = prepared_request(tmp_path, monkeypatch)
    first = preview_init(request_file, git_mode="off")
    apply_init(request_file, git_mode="off", preview_token=first["preview_token"])
    outline = target / "大纲" / "总纲.md"
    outline.write_text(outline.read_text(encoding="utf-8") + "\n作者自定义段落\n", encoding="utf-8")
    outline_before = outline.read_bytes()
    missing = target / "设定集" / "力量体系.md"
    missing.unlink()

    preview = preview_init(request_file, git_mode="off")
    assert preview["status"] == "ready"
    assert preview["write_list"] == ["设定集/力量体系.md"]
    assert "大纲/总纲.md" in preview["preserve_list"]
    result = apply_init(request_file, git_mode="off", preview_token=preview["preview_token"])

    assert result["created_files"] == ["设定集/力量体系.md"]
    assert result["plan_precondition_ready"] is True
    assert missing.is_file()
    assert outline.read_bytes() == outline_before

    final_preview = preview_init(request_file, git_mode="off")
    assert final_preview["status"] == "ready"
    assert final_preview["write_list"] == []


@pytest.mark.parametrize(
    "bad_bytes, detail",
    [
        (b"", "non-empty"),
        (b"\xff\xfe", "valid UTF-8"),
        ("# 总纲\n\n## 故事一句话\n与确认输入矛盾\n".encode("utf-8"), "structure anchor"),
    ],
)
def test_init_preview_blocks_damaged_or_canon_conflicting_markdown(
    tmp_path,
    monkeypatch,
    bad_bytes,
    detail,
):
    request_file, _, target, _ = prepared_request(tmp_path, monkeypatch)
    first = preview_init(request_file, git_mode="off")
    apply_init(request_file, git_mode="off", preview_token=first["preview_token"])
    outline = target / "大纲" / "总纲.md"
    outline.write_bytes(bad_bytes)
    before = tree_snapshot(target)

    preview = preview_init(request_file, git_mode="off")
    assert preview["status"] == "blocked"
    conflicts = [
        item for item in preview["blockers"] if item["code"] == "markdown_contract_conflict"
    ]
    assert len(conflicts) == 1
    assert detail in conflicts[0]["detail"]
    with pytest.raises(InitWorkflowError, match="blocked"):
        apply_init(request_file, git_mode="off", preview_token=preview["preview_token"])
    assert tree_snapshot(target) == before


def test_init_conflicting_canon_fails_closed_without_more_writes(tmp_path, monkeypatch):
    request_file, _, target, _ = prepared_request(tmp_path, monkeypatch)
    first = preview_init(request_file, git_mode="off")
    apply_init(request_file, git_mode="off", preview_token=first["preview_token"])
    state_path = target / ".webnovel" / "state.json"
    state_path.write_text("{broken", encoding="utf-8")
    before = tree_snapshot(target)

    preview = preview_init(request_file, git_mode="off")

    assert preview["status"] == "blocked"
    assert any(item["code"] == "canon_conflict" for item in preview["blockers"])
    with pytest.raises(InitWorkflowError, match="blocked"):
        apply_init(request_file, git_mode="off", preview_token=preview["preview_token"])
    assert tree_snapshot(target) == before


def test_init_nonempty_unrecognized_target_and_parent_repo_are_blocked(tmp_path, monkeypatch):
    request_file, workspace, target, _ = prepared_request(tmp_path, monkeypatch)
    target.mkdir()
    (target / "unrelated.txt").write_text("keep", encoding="utf-8")
    before = tree_snapshot(target)

    preview = preview_init(request_file, git_mode="off")
    assert preview["status"] == "blocked"
    assert any(item["code"] == "existing_target_not_initialized" for item in preview["blockers"])
    assert tree_snapshot(target) == before

    shutil.rmtree(target)
    (workspace / ".git").mkdir()
    parent_preview = preview_init(request_file, git_mode="off")
    assert parent_preview["status"] == "blocked"
    assert any(item["code"] == "wrong_parent_repository" for item in parent_preview["blockers"])
    assert not target.exists()


def test_unconfirmed_reference_candidate_never_enters_canon(tmp_path, monkeypatch):
    request_file, _, target, payload = prepared_request(tmp_path, monkeypatch)
    payload["reference_candidate"] = {
        "status": "proposed",
        "candidate_id": "candidate-untrusted",
        "source_title": "不可信参考",
        "confidence": 0.99,
        "transformation_notes": "UNCONFIRMED-CONTENT-MUST-NOT-PERSIST",
    }
    request_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    preview = preview_init(request_file, git_mode="off")

    assert preview["status"] == "blocked"
    assert preview["reference_candidate_status"] == "proposed"
    with pytest.raises(InitWorkflowError):
        apply_init(request_file, git_mode="off", preview_token=preview["preview_token"])
    assert not target.exists()


def test_legacy_reference_self_attestation_is_rejected(tmp_path, monkeypatch):
    request_file, _, target, payload = prepared_request(tmp_path, monkeypatch)
    payload["constraints"]["selected_idea"]["origin"] = "mixed"
    payload["reference_candidate"] = {
        "status": "adopted",
        "candidate_id": "candidate-safe",
        "source_title": "参考作品",
        "source_sha256": "a" * 64,
        "output_sha256": "b" * 64,
        "confidence": 0.91,
        "quality_passed": True,
        "user_confirmed": True,
        "runtime_evidence_accepted": True,
        "transformation_notes": "只采用压力递增结构，人物、地点和事件全部重构",
        "do_not_copy": ["原作人物名"],
        "canon_contamination_warnings": ["禁止复刻原作名场面"],
    }
    request_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(Exception, match="unknown fields"):
        preview_init(request_file, git_mode="off")
    assert not target.exists()


def test_strict_reference_rollout_is_bound_claimed_and_cannot_cross_project_replay(
    tmp_path,
    monkeypatch,
):
    request_file, workspace, target, payload = prepared_request(tmp_path, monkeypatch)
    sessions_root = tmp_path / "trusted-codex-sessions"
    sessions_root.mkdir()
    _attach_strict_reference(
        payload,
        workspace=workspace,
        target=target,
        sessions_root=sessions_root,
    )
    reference = payload["reference_candidate"]
    child_events = [
        json.loads(line)
        for line in Path(reference["runtime"]["rollout_path"])
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    spawn = child_events[0]["payload"]["source"]["subagent"]["thread_spawn"]
    expected_task_name = derive_agent_task_name(reference["binding_marker"], prefix="wni")
    assert spawn["depth"] == 1
    assert spawn["agent_path"] == f"/root/{expected_task_name}"
    assert reference["binding_marker"] not in Path(reference["runtime"]["rollout_path"]).read_text(
        encoding="utf-8"
    )
    request_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(init_workflow, "TRUSTED_CODEX_SESSIONS_ROOT", sessions_root)
    monkeypatch.setattr(
        init_workflow,
        "validate_route_readiness",
        lambda *args, **kwargs: {"ready": True, "status": "ready", "problems": []},
    )

    preview = preview_init(request_file, git_mode="off")
    assert preview["status"] == "ready", preview["blockers"]
    result = apply_init(
        request_file,
        git_mode="off",
        preview_token=preview["preview_token"],
    )
    assert result["reference_live_gate"] == "verified"
    idea = json.loads((target / ".webnovel" / "idea_bank.json").read_text(encoding="utf-8"))
    adoption = idea["reference_adoption"]
    assert adoption["runtime_evidence_sha256"] == payload["reference_candidate"]["runtime"]["rollout_sha256"]
    assert adoption["contract_hash"] == payload["reference_candidate"]["contract_hash"]
    assert adoption["parent_identity_sha256"] == payload["reference_candidate"]["runtime"][
        "parent_identity_sha256"
    ]
    assert adoption["confirmation_scope_sha256"] == payload["reference_candidate"][
        "user_confirmation"
    ]["scope_sha256"]
    assert adoption["choice_scope_sha256"] == payload["reference_candidate"]["user_confirmation"][
        "choice_scope_sha256"
    ]

    retry_preview = preview_init(request_file, git_mode="off")
    retry = apply_init(
        request_file,
        git_mode="off",
        preview_token=retry_preview["preview_token"],
    )
    assert retry["created_files"] == []
    assert retry["reference_live_gate"] == "verified"

    replay = deepcopy(payload)
    replay["project_slug"] = "星火长夜二"
    replay_target = workspace / replay["project_slug"]
    replay_confirmation = build_reference_adoption_confirmation(
        project_root=str(replay_target.resolve()),
        selected_idea=replay["constraints"]["selected_idea"],
        reference_candidate=replay["reference_candidate"],
    )
    parent_rollout = Path(replay["reference_candidate"]["runtime"]["parent_rollout_path"])
    _append_parent_reference_choice(
        parent_rollout,
        marker=replay_confirmation["choice_marker"],
    )
    replay["reference_candidate"]["runtime"]["parent_rollout_sha256"] = hashlib.sha256(
        parent_rollout.read_bytes()
    ).hexdigest()
    replay["reference_candidate"]["user_confirmation"] = build_reference_adoption_confirmation(
        project_root=str(replay_target.resolve()),
        selected_idea=replay["constraints"]["selected_idea"],
        reference_candidate=replay["reference_candidate"],
    )
    replay_file = write_request(
        request_file.parents[2],
        replay,
        name="reference-replay.json",
    )
    replay_preview = preview_init(replay_file, git_mode="off")
    assert replay_preview["status"] == "ready"
    with pytest.raises(InitWorkflowError) as exc:
        apply_init(
            replay_file,
            git_mode="off",
            preview_token=replay_preview["preview_token"],
        )
    assert exc.value.code == "reference_runtime_evidence_reused"
    assert not replay_target.exists()


@pytest.mark.parametrize(
    ("case", "attach_options", "message"),
    [
        ("wrong-path", {"agent_path": "/root/wni_wrong"}, "agent_path"),
        ("wrong-depth", {"depth": 2}, "depth"),
        (
            "marker-change",
            {"task_name_marker": "WEBNOVEL_INIT_REFERENCE_BINDING/v1 stale"},
            "agent_path",
        ),
        ("multiple-final", {"extra_final": True}, "exactly one final assistant answer"),
    ],
)
def test_reference_host_task_binding_fails_closed_without_target_writes(
    tmp_path,
    monkeypatch,
    case,
    attach_options,
    message,
):
    request_file, workspace, target, payload = prepared_request(tmp_path, monkeypatch)
    sessions_root = tmp_path / f"trusted-codex-sessions-{case}"
    sessions_root.mkdir()
    _attach_strict_reference(
        payload,
        workspace=workspace,
        target=target,
        sessions_root=sessions_root,
        **attach_options,
    )
    request_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(init_workflow, "TRUSTED_CODEX_SESSIONS_ROOT", sessions_root)
    monkeypatch.setattr(
        init_workflow,
        "validate_route_readiness",
        lambda *args, **kwargs: {"ready": True, "status": "ready", "problems": []},
    )

    preview = preview_init(request_file, git_mode="off")

    assert preview["status"] == "blocked"
    assert {item["code"] for item in preview["blockers"]} == {
        "reference_adoption_unverified"
    }
    assert message in preview["blockers"][0]["detail"]
    assert not target.exists()


def test_reference_rollout_rejects_request_controlled_sessions_root(tmp_path, monkeypatch):
    request_file, workspace, target, payload = prepared_request(tmp_path, monkeypatch)
    fake_sessions = tmp_path / "request-controlled-sessions"
    fake_sessions.mkdir()
    trusted_sessions = tmp_path / "trusted-codex-sessions"
    trusted_sessions.mkdir()
    _attach_strict_reference(
        payload,
        workspace=workspace,
        target=target,
        sessions_root=fake_sessions,
    )
    request_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(init_workflow, "TRUSTED_CODEX_SESSIONS_ROOT", trusted_sessions)
    monkeypatch.setattr(
        init_workflow,
        "validate_route_readiness",
        lambda *args, **kwargs: {"ready": True, "status": "ready", "problems": []},
    )

    preview = preview_init(request_file, git_mode="off")
    assert preview["status"] == "blocked"
    assert {item["code"] for item in preview["blockers"]} == {"reference_adoption_unverified"}
    assert "host-owned" in preview["blockers"][0]["detail"]
    assert not target.exists()


@pytest.mark.parametrize("link_part", ["leaf", "ancestor"])
def test_trusted_sessions_root_rejects_mocked_reparse_before_resolve(
    tmp_path,
    monkeypatch,
    link_part,
):
    sessions_root = tmp_path / "codex-home" / "sessions"
    sessions_root.mkdir(parents=True)
    lexical_root = Path(os.path.abspath(str(sessions_root)))
    linklike_path = lexical_root if link_part == "leaf" else lexical_root.parent
    monkeypatch.setattr(init_workflow, "TRUSTED_CODEX_SESSIONS_ROOT", lexical_root)
    monkeypatch.setattr(init_workflow, "_is_linklike", lambda path: path == linklike_path)
    real_resolve = Path.resolve
    resolved_sessions = False

    def tracked_resolve(path, *, strict=False):
        nonlocal resolved_sessions
        if Path(os.path.abspath(str(path))) == lexical_root:
            resolved_sessions = True
        return real_resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", tracked_resolve)
    with pytest.raises(InitWorkflowError, match="traverses a reparse point"):
        init_workflow._trusted_codex_sessions_root()
    assert resolved_sessions is False


@pytest.mark.parametrize("current_thread_id", [None, OTHER_CODEX_THREAD_ID])
def test_reference_adoption_requires_current_host_parent_without_writes(
    tmp_path,
    monkeypatch,
    current_thread_id,
):
    request_file, workspace, target, payload = prepared_request(tmp_path, monkeypatch)
    sessions_root = tmp_path / "trusted-codex-sessions"
    sessions_root.mkdir()
    _attach_strict_reference(
        payload,
        workspace=workspace,
        target=target,
        sessions_root=sessions_root,
    )
    request_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(init_workflow, "TRUSTED_CODEX_SESSIONS_ROOT", sessions_root)
    monkeypatch.setattr(
        init_workflow,
        "validate_route_readiness",
        lambda *args, **kwargs: {"ready": True, "status": "ready", "problems": []},
    )
    if current_thread_id is None:
        monkeypatch.delenv("CODEX_THREAD_ID")
    else:
        monkeypatch.setenv("CODEX_THREAD_ID", current_thread_id)

    preview = preview_init(request_file, git_mode="off")
    assert preview["status"] == "blocked"
    assert {item["code"] for item in preview["blockers"]} == {
        "reference_adoption_unverified"
    }
    if current_thread_id is None:
        assert "CODEX_THREAD_ID is missing or is not a canonical UUID" in preview["blockers"][0][
            "detail"
        ]
    else:
        assert "parent thread does not match the current Codex task" in preview["blockers"][0][
            "detail"
        ]
    assert not target.exists()


def test_reference_adoption_requires_parent_rollout_identity_and_hash(tmp_path, monkeypatch):
    request_file, workspace, target, payload = prepared_request(tmp_path, monkeypatch)
    sessions_root = tmp_path / "trusted-codex-sessions"
    sessions_root.mkdir()
    _attach_strict_reference(
        payload,
        workspace=workspace,
        target=target,
        sessions_root=sessions_root,
    )
    runtime = payload["reference_candidate"]["runtime"]
    parent_rollout = Path(runtime["parent_rollout_path"])
    parent_rollout.write_text(
        parent_rollout.read_text(encoding="utf-8").replace("gpt-5.6-sol", "gpt-5.6-terra"),
        encoding="utf-8",
    )
    runtime["parent_rollout_sha256"] = hashlib.sha256(parent_rollout.read_bytes()).hexdigest()
    payload["reference_candidate"]["binding_marker"] = build_reference_binding_marker(
        payload["reference_candidate"]
    )
    payload["reference_candidate"]["user_confirmation"] = build_reference_adoption_confirmation(
        project_root=str(target.resolve()),
        selected_idea=payload["constraints"]["selected_idea"],
        reference_candidate=payload["reference_candidate"],
    )
    request_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(init_workflow, "TRUSTED_CODEX_SESSIONS_ROOT", sessions_root)

    preview = preview_init(request_file, git_mode="off")
    assert preview["status"] == "blocked"
    assert "parent session_meta/turn_context model mismatch" in preview["blockers"][0]["detail"]
    assert not target.exists()


def test_reference_adoption_rejects_child_rollout_as_parent_without_writes(
    tmp_path,
    monkeypatch,
):
    request_file, workspace, target, payload = prepared_request(tmp_path, monkeypatch)
    sessions_root = tmp_path / "trusted-codex-sessions"
    sessions_root.mkdir()
    _attach_strict_reference(
        payload,
        workspace=workspace,
        target=target,
        sessions_root=sessions_root,
    )
    reference = payload["reference_candidate"]
    runtime = reference["runtime"]
    parent_rollout = Path(runtime["parent_rollout_path"])
    events = [json.loads(line) for line in parent_rollout.read_text(encoding="utf-8").splitlines()]
    events[0]["payload"]["parent_thread_id"] = "upstream-parent"
    events[0]["payload"]["source"] = {
        "subagent": {"thread_spawn": {"parent_thread_id": "upstream-parent"}}
    }
    parent_rollout.write_text(
        "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n",
        encoding="utf-8",
    )
    runtime["parent_rollout_sha256"] = hashlib.sha256(parent_rollout.read_bytes()).hexdigest()
    reference["binding_marker"] = build_reference_binding_marker(reference)
    reference["user_confirmation"] = build_reference_adoption_confirmation(
        project_root=str(target.resolve()),
        selected_idea=payload["constraints"]["selected_idea"],
        reference_candidate=reference,
    )
    request_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(init_workflow, "TRUSTED_CODEX_SESSIONS_ROOT", sessions_root)

    preview = preview_init(request_file, git_mode="off")
    assert preview["status"] == "blocked"
    assert "top-level Codex task" in preview["blockers"][0]["detail"]
    assert not target.exists()


def test_reference_rollout_final_json_accepts_legacy_marker_or_verified_task_binding():
    marker = "bound-marker"
    marker_event = {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": marker}],
        },
    }
    commentary_event = {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "assistant",
            "phase": "commentary",
            "content": [{"type": "output_text", "text": '{"ignored":true}'}],
        },
    }
    final_event = {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "assistant",
            "phase": "final_answer",
            "content": [{"type": "output_text", "text": '{"accepted":true}'}],
        },
    }

    legacy_raw = (
        "\n".join(json.dumps(event, separators=(",", ":")) for event in [marker_event, final_event])
        + "\n"
    ).encode("utf-8")
    current_host_raw = (
        "\n".join(
            json.dumps(event, separators=(",", ":"))
            for event in [commentary_event, final_event]
        )
        + "\n"
    ).encode("utf-8")

    assert init_workflow._rollout_final_json(
        legacy_raw,
        binding_marker=marker,
    ) == {"accepted": True}
    assert init_workflow._rollout_final_json(
        current_host_raw,
        binding_marker=marker,
        task_binding_verified=True,
    ) == {"accepted": True}


@pytest.mark.parametrize(
    "case, message",
    [
        ("no-answer", "no user answer"),
        ("discard", "not explicitly selected"),
        ("other-project", "exactly one scoped assistant choice marker"),
    ],
)
def test_reference_confirmation_requires_real_scoped_parent_user_adopt(
    tmp_path,
    monkeypatch,
    case,
    message,
):
    request_file, workspace, target, payload = prepared_request(tmp_path, monkeypatch)
    sessions_root = tmp_path / "trusted-codex-sessions"
    sessions_root.mkdir()
    _attach_strict_reference(
        payload,
        workspace=workspace,
        target=target,
        sessions_root=sessions_root,
    )
    reference = payload["reference_candidate"]
    runtime = reference["runtime"]
    parent_rollout = Path(runtime["parent_rollout_path"])
    if case == "no-answer":
        lines = parent_rollout.read_text(encoding="utf-8").splitlines()
        parent_rollout.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
        runtime["parent_rollout_sha256"] = hashlib.sha256(parent_rollout.read_bytes()).hexdigest()
    elif case == "discard":
        lines = parent_rollout.read_text(encoding="utf-8").splitlines()
        answer_event = json.loads(lines[-1])
        answer_event["payload"]["content"][0]["text"] = "Discard"
        lines[-1] = json.dumps(answer_event, ensure_ascii=False, separators=(",", ":"))
        parent_rollout.write_text("\n".join(lines) + "\n", encoding="utf-8")
        runtime["parent_rollout_sha256"] = hashlib.sha256(parent_rollout.read_bytes()).hexdigest()
    else:
        payload["project_slug"] = "另一个项目"
        target = workspace / payload["project_slug"]
    reference["user_confirmation"] = build_reference_adoption_confirmation(
        project_root=str(target.resolve()),
        selected_idea=payload["constraints"]["selected_idea"],
        reference_candidate=reference,
    )
    request_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(init_workflow, "TRUSTED_CODEX_SESSIONS_ROOT", sessions_root)
    monkeypatch.setattr(
        init_workflow,
        "validate_route_readiness",
        lambda *args, **kwargs: {"ready": True, "status": "ready", "problems": []},
    )

    preview = preview_init(request_file, git_mode="off")
    assert preview["status"] == "blocked"
    assert message in preview["blockers"][0]["detail"]
    assert not target.exists()


def test_reference_adoption_provenance_matrix_fails_closed(tmp_path, monkeypatch):
    request_file, workspace, target, payload = prepared_request(tmp_path, monkeypatch)
    sessions_root = tmp_path / "trusted-codex-sessions"
    sessions_root.mkdir()
    _attach_strict_reference(
        payload,
        workspace=workspace,
        target=target,
        sessions_root=sessions_root,
    )
    request_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    request = load_init_request(request_file)
    monkeypatch.setattr(init_workflow, "TRUSTED_CODEX_SESSIONS_ROOT", sessions_root)
    monkeypatch.setattr(
        init_workflow,
        "validate_route_readiness",
        lambda *args, **kwargs: {"ready": True, "status": "ready", "problems": []},
    )

    cases = [
        ("missing-runtime", "lacks explicit rollout"),
        ("source-hash", "source hash"),
        ("parent-identity", "parent rollout identity hash"),
        ("parent-prefix", "authorization-prefix hash"),
        ("route-hash", "route hash"),
        ("contract-hash", "contract hash"),
        ("same-rollout", "must be distinct"),
        ("child-hash", "rollout hash"),
        ("output-hash", "output hash"),
        ("request-output", "request output"),
        ("source-title", "source provenance"),
        ("quality", "quality must pass"),
        ("selected", "exactly one transformed"),
        ("do-not-copy", "do-not-copy"),
        ("canon-warning", "canon-contamination"),
        ("confirmation", "confirmation is stale"),
    ]
    for case, message in cases:
        candidate = deepcopy(request)
        reference = candidate["reference_candidate"]
        runtime = reference["runtime"]
        if case == "missing-runtime":
            reference["runtime"] = None
        elif case == "source-hash":
            reference["source_sha256"] = "0" * 64
        elif case == "parent-identity":
            runtime["parent_identity_sha256"] = "0" * 64
        elif case == "parent-prefix":
            runtime["parent_rollout_sha256"] = "0" * 64
            reference["user_confirmation"] = build_reference_adoption_confirmation(
                project_root=str(candidate["project_root"]),
                selected_idea=candidate["constraints"]["selected_idea"],
                reference_candidate=reference,
            )
        elif case == "route-hash":
            reference["route_sha256"] = "0" * 64
        elif case == "contract-hash":
            reference["contract_hash"] = "0" * 64
        elif case == "same-rollout":
            runtime["rollout_path"] = runtime["parent_rollout_path"]
        elif case == "child-hash":
            runtime["rollout_sha256"] = "0" * 64
        elif case == "output-hash":
            reference["output_sha256"] = "0" * 64
        elif case == "request-output":
            reference["deconstruction_output"] = {"different": True}
        elif case == "source-title":
            reference["source_title"] = "different"
        elif case == "quality":
            reference["confidence"] = 0.99
        elif case == "selected":
            candidate["constraints"]["selected_idea"]["opening_hook"] = "different"
        elif case == "do-not-copy":
            reference["do_not_copy"] = ["different"]
        elif case == "canon-warning":
            reference["canon_contamination_warnings"] = ["different"]
        elif case == "confirmation":
            reference["user_confirmation"] = {
                **reference["user_confirmation"],
                "scope_sha256": "0" * 64,
            }
        with pytest.raises(InitWorkflowError, match=message):
            init_workflow._validate_reference_adoption(candidate)

    with monkeypatch.context() as patcher:
        patcher.setattr(
            init_workflow,
            "validate_route_readiness",
            lambda *args, **kwargs: {"ready": False, "status": "blocked", "problems": []},
        )
        with pytest.raises(InitWorkflowError, match="missing or stale"):
            init_workflow._validate_reference_adoption(request)


@pytest.mark.parametrize(
    "case, message",
    [
        ("bom", "without BOM"),
        ("invalid-jsonl", "not UTF-8 JSONL"),
        ("no-marker", "exactly one bound invocation marker"),
        ("duplicate-marker", "exactly one bound invocation marker"),
        ("marker-after-final", "exactly one final assistant answer"),
        ("bad-final-shape", "one output_text"),
        ("bad-final-json", "one strict JSON object"),
        ("array-final", "one strict JSON object"),
    ],
)
def test_reference_rollout_final_json_fails_closed(case, message):
    marker = "bound-marker"
    marker_event = {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": marker}],
        },
    }
    final_event = {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "assistant",
            "phase": "final_answer",
            "content": [{"type": "output_text", "text": "{}"}],
        },
    }
    if case == "bom":
        raw = b"\xef\xbb\xbf{}\n"
    elif case == "invalid-jsonl":
        raw = b"not-json\n"
    else:
        events = [marker_event, final_event]
        if case == "no-marker":
            events = [final_event]
        elif case == "duplicate-marker":
            events = [marker_event, marker_event, final_event]
        elif case == "marker-after-final":
            events = [final_event, marker_event]
        elif case == "bad-final-shape":
            final_event["payload"]["content"] = [{"type": "text", "text": "{}"}]
        elif case == "bad-final-json":
            final_event["payload"]["content"][0]["text"] = "not-json"
        elif case == "array-final":
            final_event["payload"]["content"][0]["text"] = "[]"
        raw = (
            "\n".join(json.dumps(event, separators=(",", ":")) for event in events) + "\n"
        ).encode("utf-8")
    with pytest.raises(InitWorkflowError, match=message):
        init_workflow._rollout_final_json(raw, binding_marker=marker)


@pytest.mark.skipif(shutil.which("git") is None, reason="Git is required for isolated init-mode smoke")
def test_git_init_and_initial_commit_are_target_root_only_and_allowlisted(tmp_path, monkeypatch):
    request_file, workspace, target, _ = prepared_request(tmp_path, monkeypatch)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Webnovel Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "webnovel-test@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Webnovel Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "webnovel-test@example.invalid")
    hooks = tmp_path / "global-hooks"
    hooks.mkdir()
    hook_ran = tmp_path / "global-hook-ran.txt"
    hook = hooks / "pre-commit"
    hook.write_text(
        "#!/bin/sh\nprintf ran > '" + hook_ran.as_posix() + "'\nexit 97\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    global_config = tmp_path / "isolated-global.gitconfig"
    subprocess.run(
        ["git", "config", "--file", str(global_config), "core.hooksPath", str(hooks)],
        check=True,
        capture_output=True,
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")

    preview = preview_init(request_file, git_mode="initial-commit")
    assert preview["status"] == "ready", preview["blockers"]
    result = apply_init(
        request_file,
        git_mode="initial-commit",
        preview_token=preview["preview_token"],
    )

    assert result["git"]["committed"] is True
    top = subprocess.run(
        ["git", "-C", str(target), "rev-parse", "--show-toplevel"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()
    assert Path(top).resolve() == target.resolve()
    tracked = subprocess.run(
        ["git", "-C", str(target), "ls-files", "-z"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.split("\x00")
    assert ".webnovel/state.json" in tracked
    assert set(item for item in tracked if item) == set(result["git"]["staged_paths"])
    assert not (workspace / ".git").exists()
    assert not hook_ran.exists()


@pytest.mark.skipif(shutil.which("git") is None, reason="Git is required for rollback smoke")
def test_initial_commit_failure_rolls_back_new_git_index_and_created_project(tmp_path, monkeypatch):
    request_file, _, target, _ = prepared_request(tmp_path, monkeypatch)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Webnovel Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "webnovel-test@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Webnovel Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "webnovel-test@example.invalid")
    preview = preview_init(request_file, git_mode="initial-commit")
    assert preview["status"] == "ready"
    real_run_git = init_workflow._run_git

    def fail_commit(target_arg, *args, **kwargs):
        if "commit" in args:
            raise subprocess.TimeoutExpired(cmd=["git", "commit"], timeout=30)
        return real_run_git(target_arg, *args, **kwargs)

    monkeypatch.setattr(init_workflow, "_run_git", fail_commit)
    with pytest.raises(InitWorkflowError, match="failed safely"):
        apply_init(
            request_file,
            git_mode="initial-commit",
            preview_token=preview["preview_token"],
        )

    assert not target.exists()


@pytest.mark.skipif(shutil.which("git") is None, reason="Git is required for index rollback smoke")
def test_initial_commit_failure_restores_preexisting_empty_repository(tmp_path, monkeypatch):
    request_file, _, target, _ = prepared_request(tmp_path, monkeypatch)
    target.mkdir()
    subprocess.run(["git", "-C", str(target), "init"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(target), "read-tree", "--empty"],
        check=True,
        capture_output=True,
    )
    index_before = (target / ".git" / "index").read_bytes()
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Webnovel Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "webnovel-test@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Webnovel Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "webnovel-test@example.invalid")
    preview = preview_init(request_file, git_mode="initial-commit")
    assert preview["status"] == "ready", preview["blockers"]
    real_run_git = init_workflow._run_git

    def fail_commit(target_arg, *args, **kwargs):
        if "commit" in args:
            raise InitWorkflowError("injected commit failure")
        return real_run_git(target_arg, *args, **kwargs)

    monkeypatch.setattr(init_workflow, "_run_git", fail_commit)
    with pytest.raises(InitWorkflowError, match="injected commit failure"):
        apply_init(
            request_file,
            git_mode="initial-commit",
            preview_token=preview["preview_token"],
        )

    assert (target / ".git").is_dir()
    assert (target / ".git" / "index").read_bytes() == index_before
    assert [path.name for path in target.iterdir()] == [".git"]


@pytest.mark.skipif(shutil.which("git") is None, reason="Git is required for isolated index safety smoke")
def test_initial_commit_never_commits_preserved_or_prestaged_user_content(tmp_path, monkeypatch):
    request_file, _, target, _ = prepared_request(tmp_path, monkeypatch)
    off_preview = preview_init(request_file, git_mode="off")
    apply_init(request_file, git_mode="off", preview_token=off_preview["preview_token"])
    outline = target / "大纲" / "总纲.md"
    outline.write_text(outline.read_text(encoding="utf-8") + "\n作者未授权提交的修改\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(target), "init"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(target), "add", "--", "大纲/总纲.md"],
        check=True,
        capture_output=True,
    )

    preview = preview_init(request_file, git_mode="initial-commit")

    codes = {item["code"] for item in preview["blockers"]}
    assert "initial_commit_existing_project" in codes
    assert "git_index_not_clean" in codes
    with pytest.raises(InitWorkflowError, match="blocked"):
        apply_init(request_file, git_mode="initial-commit", preview_token=preview["preview_token"])
    assert subprocess.run(
        ["git", "-C", str(target), "rev-parse", "--verify", "HEAD"],
        check=False,
        capture_output=True,
    ).returncode != 0
    staged = subprocess.run(
        [
            "git",
            "-C",
            str(target),
            "-c",
            "core.quotepath=false",
            "diff",
            "--cached",
            "--name-only",
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout
    assert "总纲.md" in staged


def test_optional_character_cards_and_no_golden_finger_render(tmp_path, monkeypatch):
    request_file, _, target, payload = prepared_request(tmp_path, monkeypatch)
    payload["protagonist"]["structure"] = "多主角"
    payload["relationship"].update(
        heroine_config="单女主",
        heroine_names=["陆青灯"],
        heroine_role="共同守城的独立盟友",
        co_protagonists=["沈砚", "陆青灯"],
        co_protagonist_roles=["守夜", "调查"],
    )
    payload["golden_finger"].update(type="无金手指", name="")
    request_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    preview = preview_init(request_file, git_mode="off")
    assert preview["status"] == "ready"
    apply_init(request_file, git_mode="off", preview_token=preview["preview_token"])

    assert (target / "设定集" / "女主卡.md").is_file()
    assert (target / "设定集" / "主角组.md").is_file()
    state = json.loads((target / ".webnovel" / "state.json").read_text(encoding="utf-8"))
    assert state["protagonist_state"]["golden_finger"]["level"] == 0


def test_preview_reports_render_and_path_type_blockers(tmp_path, monkeypatch):
    request_file, _, target, payload = prepared_request(tmp_path, monkeypatch)
    payload["project"]["genre"] = "完全未知题材"
    request_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    unresolved = preview_init(request_file, git_mode="off")
    assert any(item["code"] == "render_failed" for item in unresolved["blockers"])

    payload["project"]["genre"] = "玄幻"
    target.write_text("not a directory", encoding="utf-8")
    request_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    unsafe_target = preview_init(request_file, git_mode="off")
    assert any(item["code"] == "unsafe_target" for item in unsafe_target["blockers"])

    target.unlink()
    target.mkdir()
    (target / ".webnovel").write_text("wrong type", encoding="utf-8")
    wrong_directory = preview_init(request_file, git_mode="off")
    assert any(item["code"] == "path_type_conflict" for item in wrong_directory["blockers"])


def test_core_json_consistency_matrix(tmp_path, monkeypatch):
    request_file, _, _, _ = prepared_request(tmp_path, monkeypatch)
    request = load_init_request(request_file)
    artifacts, expected = init_workflow.build_desired_artifacts(request, git_mode="off")
    candidate = tmp_path / "candidate.json"

    def check(relative: str, payload, expected_ok: bool) -> str:
        candidate.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        ok, detail = init_workflow._consistent_core_json(relative, candidate, expected)
        assert ok is expected_ok
        return detail

    assert "project_info" in check(".webnovel/state.json", [], False)
    state = json.loads(artifacts[".webnovel/state.json"].decode("utf-8"))
    wrong_state = deepcopy(state)
    wrong_state["project_info"]["title"] = "另一部书"
    assert "title" in check(".webnovel/state.json", wrong_state, False)
    wrong_constraints = deepcopy(state)
    wrong_constraints["project_info"]["init_constraints"]["anti_trope"] = "conflict"
    assert "init_constraints" in check(".webnovel/state.json", wrong_constraints, False)
    legacy_state = deepcopy(state)
    del legacy_state["project_info"]["init_constraints"]
    check(".webnovel/state.json", legacy_state, True)

    idea = json.loads(artifacts[".webnovel/idea_bank.json"].decode("utf-8"))
    check(".webnovel/idea_bank.json", {}, False)
    check(".webnovel/idea_bank.json", idea, True)
    wrong_idea = deepcopy(idea)
    wrong_idea["constraints_inherited"]["anti_trope"] = "conflict"
    check(".webnovel/idea_bank.json", wrong_idea, False)
    master = json.loads(artifacts[".story-system/MASTER_SETTING.json"].decode("utf-8"))
    check(".story-system/MASTER_SETTING.json", [], False)
    wrong_master_genre = deepcopy(master)
    wrong_master_genre["route"]["canonical_genre"] = "都市"
    check(".story-system/MASTER_SETTING.json", wrong_master_genre, False)
    wrong_master_constraints = deepcopy(master)
    wrong_master_constraints["init_constraints"]["anti_trope"] = "conflict"
    check(".story-system/MASTER_SETTING.json", wrong_master_constraints, False)
    check(".story-system/MASTER_SETTING.json", master, True)
    anti_patterns = json.loads(artifacts[".story-system/anti_patterns.json"].decode("utf-8"))
    check(".story-system/anti_patterns.json", {}, False)
    check(".story-system/anti_patterns.json", [{"bad": "shape"}], False)
    check(".story-system/anti_patterns.json", [], False)
    check(".story-system/anti_patterns.json", anti_patterns, True)
    check("unknown.json", {}, False)
    candidate.write_bytes(b"\xef\xbb\xbf{}")
    ok, detail = init_workflow._consistent_core_json(".webnovel/state.json", candidate, expected)
    assert not ok and "BOM" in detail


def test_preview_preserves_consistent_nonidentical_core_json(tmp_path, monkeypatch):
    request_file, _, target, _ = prepared_request(tmp_path, monkeypatch)
    first = preview_init(request_file, git_mode="off")
    apply_init(request_file, git_mode="off", preview_token=first["preview_token"])
    anti = target / ".story-system" / "anti_patterns.json"
    payload = json.loads(anti.read_text(encoding="utf-8"))
    payload.append({"text": "作者新增反模式"})
    anti.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    preview = preview_init(request_file, git_mode="off")
    assert preview["status"] == "ready"
    assert ".story-system/anti_patterns.json" in preview["preserve_list"]


def test_git_preflight_blocker_matrix(tmp_path, monkeypatch):
    target = tmp_path / "book"
    target.mkdir()
    monkeypatch.setattr(init_workflow.shutil, "which", lambda _: None)
    assert init_workflow._git_blockers(target, "init")[0]["code"] == "git_unavailable"

    monkeypatch.setattr(init_workflow.shutil, "which", lambda _: "git")
    git_marker = target / ".git"
    git_marker.mkdir()
    monkeypatch.setattr(init_workflow, "_is_linklike", lambda path: path == git_marker)
    assert init_workflow._git_blockers(target, "init")[0]["code"] == "unsafe_git_marker"

    monkeypatch.setattr(init_workflow, "_is_linklike", lambda path: False)
    monkeypatch.setattr(init_workflow, "_git_top_level", lambda _: tmp_path)
    assert init_workflow._git_blockers(target, "init")[0]["code"] == "wrong_git_root"

    monkeypatch.setattr(init_workflow, "_git_top_level", lambda _: target)
    responses = iter(
        [
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 1, "", ""),
        ]
    )
    monkeypatch.setattr(init_workflow, "_run_git", lambda *args, **kwargs: next(responses))
    monkeypatch.setattr(init_workflow, "_git_identity_available", lambda: False)
    codes = {item["code"] for item in init_workflow._git_blockers(target, "initial-commit")}
    assert codes == {"git_history_exists", "git_index_not_clean", "git_identity_missing"}


def test_low_level_apply_guards_and_git_init(tmp_path, monkeypatch):
    target = tmp_path / "target"
    target.mkdir()
    with pytest.raises(InitWorkflowError, match="escapes"):
        init_workflow._assert_apply_path(target, tmp_path / "outside")
    wrong_dir = target / "wrong"
    wrong_dir.write_text("file", encoding="utf-8")
    with pytest.raises(InitWorkflowError, match="directory changed"):
        init_workflow._mkdir_missing(target, wrong_dir)
    existing = target / "existing.txt"
    existing.write_text("keep", encoding="utf-8")
    with pytest.raises(InitWorkflowError, match="file changed"):
        init_workflow._write_new_file(target, existing, b"new")

    if shutil.which("git") is not None:
        result = init_workflow._apply_git(target, git_mode="init", title="Book", allowlist=[])
        assert result["mode"] == "init"
        assert init_workflow._git_top_level(target) == target.resolve()
    with pytest.raises(InitWorkflowError):
        init_workflow._run_git(target, "definitely-not-a-command", check=True)


def test_stable_file_reader_and_preview_revalidation_fail_closed(tmp_path, monkeypatch):
    with pytest.raises(InitWorkflowError, match="absolute regular"):
        init_workflow._stable_regular_bytes(Path("relative.txt"), max_bytes=8, label="fixture")
    with pytest.raises(InitWorkflowError, match="unavailable"):
        init_workflow._stable_regular_bytes(
            (tmp_path / "missing.txt").resolve(), max_bytes=8, label="fixture"
        )
    empty = tmp_path / "empty.txt"
    empty.write_bytes(b"")
    with pytest.raises(InitWorkflowError, match="bounded regular"):
        init_workflow._stable_regular_bytes(empty.resolve(), max_bytes=8, label="fixture")
    regular = tmp_path / "regular.txt"
    regular.write_bytes(b"payload")
    with pytest.raises(InitWorkflowError, match="bounded regular"):
        init_workflow._stable_regular_bytes(regular.resolve(), max_bytes=2, label="fixture")

    real_fstat = init_workflow.os.fstat
    calls = 0

    def changing_fstat(fd):
        nonlocal calls
        calls += 1
        value = real_fstat(fd)
        return Namespace(
            st_mode=value.st_mode,
            st_dev=value.st_dev,
            st_ino=value.st_ino,
            st_size=value.st_size + (1 if calls > 1 else 0),
            st_mtime_ns=value.st_mtime_ns,
        )

    with monkeypatch.context() as patcher:
        patcher.setattr(init_workflow.os, "fstat", changing_fstat)
        with pytest.raises(InitWorkflowError, match="changed while it was being read"):
            init_workflow._stable_regular_bytes(
                regular.resolve(), max_bytes=16, label="fixture"
            )

    target = tmp_path / "target"
    target.mkdir()
    existing = target / "existing.txt"
    existing.write_bytes(b"old")
    matrices = [
        (["bad"], "malformed"),
        ([{"path": "existing.txt", "kind": "file", "status": "create"}], "changed before apply"),
        ([{"path": "missing-dir", "kind": "directory", "status": "preserve"}], "directory changed"),
        ([{"path": "x", "kind": "file", "status": "conflict"}], "blocked preview"),
        (
            [{"path": "existing.txt", "kind": "file", "status": "preserve", "existing_sha256": "0" * 64}],
            "changed after preview",
        ),
    ]
    for operations, message in matrices:
        with pytest.raises(InitWorkflowError, match=message):
            init_workflow._revalidate_preview_state(target, {"operations": operations})


def test_post_apply_gate_reports_missing_inconsistent_and_phase_blockers(tmp_path, monkeypatch):
    request_file, _, target, _ = prepared_request(tmp_path, monkeypatch)
    preview = preview_init(request_file, git_mode="off")
    apply_init(request_file, git_mode="off", preview_token=preview["preview_token"])
    request = load_init_request(request_file)
    artifacts, expected = init_workflow.build_desired_artifacts(request, git_mode="off")

    required = target / ".story-system" / "MASTER_SETTING.md"
    saved = required.read_bytes()
    required.unlink()
    with pytest.raises(InitWorkflowError, match="missing a required file"):
        init_workflow._validate_applied_project(
            request, target, artifacts=artifacts, expected=expected
        )
    required.write_bytes(saved)

    with monkeypatch.context() as patcher:
        patcher.setattr(init_workflow, "_consistent_core_json", lambda *args: (False, "injected"))
        with pytest.raises(InitWorkflowError, match="is inconsistent"):
            init_workflow._validate_applied_project(
                request, target, artifacts=artifacts, expected=expected
            )

    unreadable = target / "设定集" / "世界观.md"
    mismatched_artifacts = dict(artifacts)
    mismatched_artifacts["设定集/力量体系.md"] = b"different"
    real_stable_reader = init_workflow._stable_regular_bytes

    def selective_stable_reader(path, *, max_bytes, label, allow_empty=False):
        if path == unreadable and label.startswith("post-apply Markdown"):
            raise OSError("injected unreadable file")
        return real_stable_reader(
            path,
            max_bytes=max_bytes,
            label=label,
            allow_empty=allow_empty,
        )

    with monkeypatch.context() as patcher:
        patcher.setattr(init_workflow, "_consistent_core_json", lambda *args: (True, "ok"))
        patcher.setattr(init_workflow, "_load_existing_json", lambda *args: {})
        patcher.setattr(init_workflow, "_stable_regular_bytes", selective_stable_reader)
        patcher.setattr(
            init_workflow,
            "resolve_project_phase",
            lambda *args: Namespace(
                phase="INITIALIZED", target_chapter=1, blocking=["injected phase blocker"]
            ),
        )
        report = init_workflow._validate_applied_project(
            request, target, artifacts=mismatched_artifacts, expected=expected
        )
    codes = {item["code"] for item in report["blockers"]}
    assert codes == {
        "state_constraints_incomplete",
        "markdown_unreadable",
        "markdown_contract_conflict",
        "plan_phase_not_ready",
        "project_phase_blocker",
    }


def test_rollback_never_deletes_changed_or_unsafe_paths(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    (target / "unsafe-file").mkdir()
    (target / "changed.txt").write_bytes(b"changed")
    (target / "unsafe-dir").write_bytes(b"file")
    nonempty = target / "nonempty"
    nonempty.mkdir()
    (nonempty / "fact.txt").write_text("keep", encoding="utf-8")

    with pytest.raises(InitWorkflowError, match="unsafe changed file") as exc:
        init_workflow._rollback_created_paths(
            target,
            created_files=["missing.txt", "unsafe-file", "changed.txt"],
            created_dirs=["unsafe-dir", "nonempty"],
            artifacts={"changed.txt": b"original"},
        )
    message = str(exc.value)
    assert "changed file preserved" in message
    assert "unsafe changed directory" in message
    assert (target / "changed.txt").read_bytes() == b"changed"
    assert (nonempty / "fact.txt").is_file()


def test_project_lock_leaf_rejects_dangling_symlink(tmp_path, monkeypatch):
    request_file, _, _, _ = prepared_request(tmp_path, monkeypatch)
    request = load_init_request(request_file)
    lock_path = init_workflow._init_lock_path(request)
    try:
        lock_path.symlink_to(lock_path.parent / "missing-lock")
    except OSError:
        pytest.skip("file symlinks are unavailable in this Windows test environment")
    with pytest.raises(InitWorkflowError, match="lock has an unsafe path type"):
        init_workflow._init_lock_path(request)


def test_preview_rejects_invalid_normalized_inputs():
    with pytest.raises(InitWorkflowError, match="schema"):
        init_workflow.build_init_preview({"schema_version": "bad"}, git_mode="off")
    with pytest.raises(InitWorkflowError, match="git_mode"):
        init_workflow.build_init_preview({"schema_version": "webnovel-init-request/v1"}, git_mode="bad")


def test_apply_rechecks_parent_repository_after_matching_preview(tmp_path, monkeypatch):
    request_file, workspace, target, _ = prepared_request(tmp_path, monkeypatch)
    preview = preview_init(request_file, git_mode="off")
    authorization = _write_apply_authorization(request_file, preview)
    calls = iter([None, workspace])
    monkeypatch.setattr(init_workflow, "_effective_parent_git", lambda _: next(calls))
    with pytest.raises(InitWorkflowError, match="entered a parent Git repository"):
        _runtime_apply_init(
            request_file,
            git_mode="off",
            preview_token=preview["preview_token"],
            authorization_json=authorization,
        )
    assert not target.exists()
