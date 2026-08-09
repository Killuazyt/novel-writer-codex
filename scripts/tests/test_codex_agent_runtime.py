#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

import data_modules.codex_agent_runtime as agent_runtime
from data_modules.codex_agent_runtime import (
    AgentRuntimeError,
    VerifiedRuntimeEvidence,
    build_canned_envelope,
    build_workflow_route,
    run_canned_workflow,
    snapshot_protected_state,
    validate_agent_envelope,
    validate_agent_payload,
    validate_protected_state_snapshots,
    validate_prompt_injection_fixture,
    validate_reviewer_attempts,
    validate_route_readiness,
)


ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = ROOT / "evals" / "fixtures" / "codex_agents"


def _load(name: str) -> dict:
    return json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8"))


def _expand(value, *, project_root: Path, run_id: str):
    if isinstance(value, str):
        return value.replace("{project_root}", str(project_root)).replace("{run_id}", run_id)
    if isinstance(value, list):
        return [_expand(item, project_root=project_root, run_id=run_id) for item in value]
    if isinstance(value, dict):
        return {
            key: _expand(item, project_root=project_root, run_id=run_id)
            for key, item in value.items()
        }
    return value


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )


def _signature(path: Path) -> dict:
    raw = path.read_bytes()
    return {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def _writer_result(
    project_root: Path,
    run_id: str,
    operation: str,
    *,
    version: int,
    resolutions: object = None,
    status: str = "completed",
) -> tuple[dict, Path | None]:
    staging = project_root.resolve() / ".webnovel" / "tmp" / "write-runs" / run_id
    staging.mkdir(parents=True, exist_ok=True)
    result = {
        "schema_version": f"webnovel-writer-result/v{version}",
        "status": status,
        "run_id": run_id,
        "operation": operation,
        "artifacts": [],
        "manifest_path": "",
        "manifest_sha256": "",
        "problems": [] if status == "completed" else ["writer_blocked"],
        "warnings": [],
    }
    if version == 2:
        result["resolutions"] = [] if resolutions is None else copy.deepcopy(resolutions)
    if status != "completed":
        return result, None

    name = "draft.md" if operation == "draft" else "polished.md"
    kind = "draft" if operation == "draft" else "polished"
    output = staging / name
    text = "第一段正文。\n\n第二段正文。\n"
    output.write_text(text, encoding="utf-8")
    artifact = {
        "kind": kind,
        "path": str(output),
        **_signature(output),
        "word_count": len("".join(text.split())),
    }
    result["artifacts"] = [artifact]
    manifest_payload = {
        "schema_version": f"webnovel-writer-manifest/v{version}",
        "run_id": run_id,
        "agent_name": "webnovel_writer",
        "operation": operation,
        "status": "completed",
        "inputs": [],
        "outputs": [artifact],
        "problems": [],
        "warnings": [],
    }
    if version == 2:
        manifest_payload["resolutions"] = copy.deepcopy(result["resolutions"])
    manifest = staging / "manifest.json"
    _write_json(manifest, manifest_payload)
    result["manifest_path"] = str(manifest)
    result["manifest_sha256"] = _signature(manifest)["sha256"]
    return result, manifest


def _materialize_payload_artifacts(
    agent_name: str,
    payload: object,
    *,
    project_root: Path,
    run_id: str,
) -> object:
    if not isinstance(payload, dict):
        return payload
    if agent_name == "webnovel_writer":
        staging = project_root.resolve() / ".webnovel" / "tmp" / "write-runs" / run_id
        staging.mkdir(parents=True, exist_ok=True)
        draft = staging / "draft.md"
        text = "第一章\n安全正文。\n"
        draft.write_text(text, encoding="utf-8")
        artifact = {
            "kind": "draft",
            "path": str(draft),
            **_signature(draft),
            "word_count": len("".join(text.split())),
        }
        payload["artifacts"] = [artifact]
        manifest = staging / "manifest.json"
        _write_json(
            manifest,
            {
                "schema_version": "webnovel-writer-manifest/v1",
                "run_id": run_id,
                "agent_name": "webnovel_writer",
                "operation": "draft",
                "status": "completed",
                "inputs": [],
                "outputs": [artifact],
                "problems": payload["problems"],
                "warnings": payload["warnings"],
            },
        )
        payload["manifest_path"] = str(manifest)
        payload["manifest_sha256"] = _signature(manifest)["sha256"]
    elif agent_name == "webnovel_data_agent":
        artifact_root = project_root.resolve() / ".webnovel" / "tmp"
        documents = {
            "fulfillment_result": {
                "planned_nodes": [],
                "covered_nodes": [],
                "missed_nodes": [],
                "extra_nodes": [],
            },
            "disambiguation_result": {"pending": []},
            "extraction_result": {
                "accepted_events": [],
                "state_deltas": [],
                "entity_deltas": [],
                "entities_appeared": [],
                "scenes": [],
                "summary_text": "安全摘要",
                "chapter_meta": {},
                "dominant_strand": "",
            },
        }
        artifacts = []
        for name, document in documents.items():
            path = artifact_root / f"{name}.json"
            _write_json(path, document)
            artifacts.append({"name": name, "path": str(path), **_signature(path)})
        payload["artifacts"] = artifacts
        payload["pending_count"] = 0
        payload["missed_nodes_count"] = 0
    return payload


def _successful_minimal_run(project_root: Path, run_id: str):
    fixture = _load("prompt_injection.json")
    cases = {case["agent_name"]: case for case in fixture["cases"]}
    route = build_workflow_route("write", parent_model="gpt-5.6-sol", mode="minimal")
    payloads = []
    envelopes = []
    for step in route["steps"]:
        case = cases[step["agent_name"]]
        payload = _expand(case["payload"], project_root=project_root, run_id=run_id)
        payloads.append(
            _materialize_payload_artifacts(
                step["agent_name"],
                payload,
                project_root=project_root,
                run_id=run_id,
            )
        )
        envelopes.append(
            build_canned_envelope(step, artifacts=[{"path": "unvalidated-forged.tmp"}])
        )
    return route, envelopes, payloads


def test_fixed_write_route_is_independent_of_two_parent_models() -> None:
    fixture = _load("model_routing.json")
    routes = [
        build_workflow_route(
            "write",
            parent_model=parent,
            parent_reasoning_effort="high",
        )
        for parent in fixture["parent_models"]
    ]

    for route in routes:
        assert [step["agent_name"] for step in route["steps"]] == [
            "webnovel_context_agent",
            "webnovel_writer",
            "webnovel_reviewer",
            "webnovel_data_agent",
        ]
        assert {step["requested_model"] for step in route["steps"]} == {"gpt-5.6-luna"}
        assert {step["requested_reasoning_effort"] for step in route["steps"]} == {"medium"}
        assert route["fallback_allowed"] is False


def test_review_route_is_fixed_luna_for_different_parent_models() -> None:
    for parent in _load("model_routing.json")["parent_models"]:
        route = build_workflow_route("review", parent_model=parent)
        assert route["steps"][0]["agent_name"] == "webnovel_reviewer"
        assert route["steps"][0]["requested_model"] == "gpt-5.6-luna"
        assert route["steps"][0]["requested_reasoning_effort"] == "medium"


def test_plan_stays_on_parent_and_never_invokes_writer_or_reviewer() -> None:
    for parent in _load("model_routing.json")["parent_models"]:
        route = build_workflow_route(
            "plan",
            parent_model=parent,
            parent_reasoning_effort="high",
        )
        result = run_canned_workflow(route, [])

        assert route["executor"] == "parent"
        assert route["planning_model"] == parent
        assert route["steps"] == []
        assert result["status"] == "accepted"
        assert result["invoked_agents"] == []


def test_plan_blocks_any_writer_or_reviewer_invocation() -> None:
    route = build_workflow_route("plan", parent_model="gpt-5.6-sol")

    result = run_canned_workflow(route, [{"agent_name": "webnovel_writer"}])

    assert result["status"] == "blocked"
    assert result["code"] == "planning_subagent_forbidden"
    assert result["accepted_artifacts"] == []


def test_context_blocker_contract_accepts_only_the_strict_safe_shape(tmp_path: Path) -> None:
    payload = {
        "schema_version": "webnovel-context-blocker/v1",
        "status": "blocked",
        "code": "insufficient_context",
        "chapter": 1,
        "missing_facts": ["主角当前所在场景"],
        "conflicts": [],
        "safe_message": "需要补充可信上下文后才能继续。",
        "problems": ["missing_scene"],
    }

    result = validate_agent_payload(
        "context",
        payload,
        project_root=tmp_path,
        run_id="context-blocked",
    )

    assert result == {
        "accepted": True,
        "code": "ok",
        "accepted_artifacts": [],
    }

    for field, invalid in (
        ("code", "arbitrary_code"),
        ("chapter", True),
        ("missing_facts", "not-a-list"),
        ("conflicts", "not-a-list"),
        ("safe_message", ""),
        ("problems", "not-a-list"),
    ):
        malformed = dict(payload)
        malformed[field] = invalid
        rejected = validate_agent_payload(
            "context",
            malformed,
            project_root=tmp_path,
            run_id="context-blocked",
        )
        assert rejected == {
            "accepted": False,
            "code": "invalid_context_result",
            "accepted_artifacts": [],
        }


def test_writer_blocked_contract_carries_no_artifact_or_manifest(tmp_path: Path) -> None:
    payload = {
        "schema_version": "webnovel-writer-result/v1",
        "status": "blocked",
        "run_id": "writer-blocked",
        "operation": "draft",
        "artifacts": [],
        "manifest_path": "",
        "manifest_sha256": "",
        "problems": ["insufficient_context"],
        "warnings": [],
    }

    result = validate_agent_payload(
        "writer",
        payload,
        project_root=tmp_path,
        run_id="writer-blocked",
    )

    assert result == {
        "accepted": False,
        "code": "writer_blocked",
        "accepted_artifacts": [],
    }
    payload["artifacts"] = [{"path": "unexpected.md"}]
    assert validate_agent_payload(
        "writer",
        payload,
        project_root=tmp_path,
        run_id="writer-blocked",
    )["code"] == "invalid_writer_result"


@pytest.mark.parametrize("operation", ["draft", "polish"])
def test_writer_v1_compatibility_is_limited_to_draft_and_polish(
    tmp_path: Path,
    operation: str,
) -> None:
    project = tmp_path / operation
    project.mkdir()
    payload, _ = _writer_result(project, f"v1-{operation}", operation, version=1)

    accepted = validate_agent_payload(
        "writer",
        payload,
        project_root=project,
        run_id=f"v1-{operation}",
    )

    assert accepted["accepted"] is True
    assert accepted["code"] == "ok"


def test_writer_v1_targeted_fix_requires_resolution_contract_v2(tmp_path: Path) -> None:
    payload, _ = _writer_result(tmp_path, "v1-targeted", "targeted_fix", version=1)

    rejected = validate_agent_payload(
        "writer",
        payload,
        project_root=tmp_path,
        run_id="v1-targeted",
    )

    assert rejected == {
        "accepted": False,
        "code": "writer_resolution_contract_required",
        "accepted_artifacts": [],
    }


@pytest.mark.parametrize("operation", ["draft", "polish"])
def test_writer_v2_draft_and_polish_require_empty_resolutions(
    tmp_path: Path,
    operation: str,
) -> None:
    run_id = f"v2-{operation}"
    valid, _ = _writer_result(tmp_path, run_id, operation, version=2, resolutions=[])
    assert validate_agent_payload(
        "writer",
        valid,
        project_root=tmp_path,
        run_id=run_id,
    )["accepted"] is True

    invalid, _ = _writer_result(
        tmp_path,
        run_id + "-invalid",
        operation,
        version=2,
        resolutions=[
            {
                "issue_index": 0,
                "issue_sha256": "a" * 64,
                "status": "resolved",
                "resolution_summary": "不应出现在 draft/polish",
            }
        ],
    )
    rejected = validate_agent_payload(
        "writer",
        invalid,
        project_root=tmp_path,
        run_id=run_id + "-invalid",
    )
    assert rejected["code"] == "invalid_writer_resolutions"


def test_writer_v2_targeted_fix_accepts_exact_occurrence_resolutions(tmp_path: Path) -> None:
    resolutions = [
        {
            "issue_index": 0,
            "issue_sha256": "a" * 64,
            "status": "resolved",
            "resolution_summary": "修正第一处时间数字。",
        },
        {
            "issue_index": 3,
            "issue_sha256": "a" * 64,
            "status": "resolved",
            "resolution_summary": "同 hash 的另一 occurrence 独立修正。",
        },
    ]
    payload, _ = _writer_result(
        tmp_path,
        "v2-targeted",
        "targeted_fix",
        version=2,
        resolutions=resolutions,
    )

    accepted = validate_agent_payload(
        "writer",
        payload,
        project_root=tmp_path,
        run_id="v2-targeted",
    )

    assert accepted["accepted"] is True
    assert accepted["code"] == "ok"


def test_writer_v2_targeted_fix_accepts_bounded_resolution_summary(
    tmp_path: Path,
) -> None:
    maximum = [
        {
            "issue_index": 0,
            "issue_sha256": "a" * 64,
            "status": "resolved",
            "resolution_summary": "界" * 1024,
        }
    ]
    accepted, _ = _writer_result(
        tmp_path,
        "resolution-boundary",
        "targeted_fix",
        version=2,
        resolutions=maximum,
    )
    assert validate_agent_payload(
        "writer",
        accepted,
        project_root=tmp_path,
        run_id="resolution-boundary",
    )["accepted"] is True


@pytest.mark.parametrize("mutation", ["missing", "extra"])
def test_writer_v2_result_requires_exact_resolution_field(
    tmp_path: Path,
    mutation: str,
) -> None:
    payload, _ = _writer_result(
        tmp_path,
        "result-resolution-field",
        "draft",
        version=2,
    )
    if mutation == "missing":
        payload.pop("resolutions")
    else:
        payload["extra"] = True

    rejected = validate_agent_payload(
        "writer",
        payload,
        project_root=tmp_path,
        run_id="result-resolution-field",
    )
    assert rejected["code"] == "invalid_writer_result"


@pytest.mark.parametrize(
    "resolutions",
    [
        [],
        "not-a-list",
        ["not-an-object"],
        [{"issue_index": 0, "issue_sha256": "a" * 64, "status": "resolved"}],
        [
            {
                "issue_index": 0,
                "issue_sha256": "a" * 64,
                "status": "resolved",
                "resolution_summary": "ok",
                "extra": True,
            }
        ],
        [
            {
                "issue_index": True,
                "issue_sha256": "a" * 64,
                "status": "resolved",
                "resolution_summary": "ok",
            }
        ],
        [
            {
                "issue_index": -1,
                "issue_sha256": "a" * 64,
                "status": "resolved",
                "resolution_summary": "ok",
            }
        ],
        [
            {
                "issue_index": 0,
                "issue_sha256": "A" * 64,
                "status": "resolved",
                "resolution_summary": "ok",
            }
        ],
        [
            {
                "issue_index": 0,
                "issue_sha256": "a" * 64,
                "status": "pending",
                "resolution_summary": "ok",
            }
        ],
        [
            {
                "issue_index": 0,
                "issue_sha256": "a" * 64,
                "status": "resolved",
                "resolution_summary": "   ",
            }
        ],
        [
            {
                "issue_index": 0,
                "issue_sha256": "a" * 64,
                "status": "resolved",
                "resolution_summary": "bad\x00summary",
            }
        ],
        [
            {
                "issue_index": 0,
                "issue_sha256": "a" * 64,
                "status": "resolved",
                "resolution_summary": "x" * 1025,
            }
        ],
        [
            {
                "issue_index": 0,
                "issue_sha256": "a" * 64,
                "status": "resolved",
                "resolution_summary": "first",
            },
            {
                "issue_index": 0,
                "issue_sha256": "b" * 64,
                "status": "resolved",
                "resolution_summary": "duplicate index",
            },
        ],
        [
            {
                "issue_index": 0,
                "issue_sha256": "a" * 64,
                "status": "resolved",
                "resolution_summary": "first",
            },
            {
                "issue_index": 0,
                "issue_sha256": "a" * 64,
                "status": "resolved",
                "resolution_summary": "duplicate pair",
            },
        ],
    ],
    ids=[
        "empty",
        "not-list",
        "not-object",
        "missing-field",
        "extra-field",
        "bool-index",
        "negative-index",
        "uppercase-sha",
        "unresolved",
        "blank-summary",
        "nul-summary",
        "long-summary",
        "duplicate-index",
        "duplicate-pair",
    ],
)
def test_writer_v2_targeted_fix_rejects_invalid_resolutions(
    tmp_path: Path,
    resolutions: object,
) -> None:
    payload, _ = _writer_result(
        tmp_path,
        "invalid-resolutions",
        "targeted_fix",
        version=2,
        resolutions=resolutions,
    )

    rejected = validate_agent_payload(
        "writer",
        payload,
        project_root=tmp_path,
        run_id="invalid-resolutions",
    )

    assert rejected["accepted"] is False
    assert rejected["code"] == "invalid_writer_resolutions"


@pytest.mark.parametrize("mutation", ["missing", "mismatch", "extra", "wrong-version"])
def test_writer_v2_manifest_must_exactly_match_payload_resolutions(
    tmp_path: Path,
    mutation: str,
) -> None:
    resolutions = [
        {
            "issue_index": 0,
            "issue_sha256": "a" * 64,
            "status": "resolved",
            "resolution_summary": "修复完成。",
        }
    ]
    payload, manifest_path = _writer_result(
        tmp_path,
        "manifest-resolution",
        "targeted_fix",
        version=2,
        resolutions=resolutions,
    )
    assert manifest_path is not None
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if mutation == "missing":
        manifest.pop("resolutions")
    elif mutation == "mismatch":
        manifest["resolutions"][0]["resolution_summary"] = "另一份说明"
    elif mutation == "extra":
        manifest["extra"] = True
    else:
        manifest["schema_version"] = "webnovel-writer-manifest/v1"
    _write_json(manifest_path, manifest)
    payload["manifest_sha256"] = _signature(manifest_path)["sha256"]

    rejected = validate_agent_payload(
        "writer",
        payload,
        project_root=tmp_path,
        run_id="manifest-resolution",
    )

    assert rejected["accepted"] is False
    assert rejected["code"] == "invalid_writer_manifest"


def test_writer_v2_blocked_targeted_fix_has_no_false_resolution_claim(tmp_path: Path) -> None:
    payload, _ = _writer_result(
        tmp_path,
        "blocked-targeted",
        "targeted_fix",
        version=2,
        resolutions=[],
        status="blocked",
    )

    rejected = validate_agent_payload(
        "writer",
        payload,
        project_root=tmp_path,
        run_id="blocked-targeted",
    )

    assert rejected["code"] == "writer_blocked"


def test_agent_runtime_raw_parsers_and_bounded_reads_fail_closed(tmp_path: Path) -> None:
    managed = tmp_path / "managed"
    managed.mkdir()
    artifact = managed / "artifact.json"
    artifact.write_bytes(b"{}")

    assert agent_runtime._json_object_from_bytes(b"\xef\xbb\xbf{}") is None
    assert agent_runtime._json_object_from_bytes(b"\xff") is None
    assert agent_runtime._json_object(1) is None
    assert agent_runtime._json_object("[]") is None
    assert agent_runtime._writer_word_count("---\ntitle: test\n---\n\u6b63 \u6587") == 2
    assert agent_runtime._safe_managed_path(tmp_path / "outside.json", managed) is False
    assert agent_runtime._stable_file_snapshot(
        managed / "missing.json",
        root=managed,
        max_bytes=16,
    ) is None
    assert agent_runtime._stable_file_snapshot(
        artifact,
        root=managed,
        max_bytes=1,
    ) is None


@pytest.mark.parametrize(
    "case",
    _load("model_routing.json")["failure_cases"],
    ids=lambda case: case["id"],
)
def test_agent_model_failures_are_fail_closed(case: dict) -> None:
    route = build_workflow_route("write", parent_model="gpt-5.6-sol")
    step = route["steps"][1]
    envelope = build_canned_envelope(
        step,
        status=case.get("status", "completed"),
        actual_model=case.get("actual_model"),
        actual_reasoning_effort=case.get("actual_reasoning_effort"),
        fallback_used=case.get("fallback_used", False),
        artifacts=[{"path": "provisional.md"}],
    )

    result = validate_agent_envelope(step, envelope, allow_canned=True)

    assert result["accepted"] is False
    assert result["code"] == case["expected_code"]
    assert result["accepted_artifacts"] == []


def test_missing_agent_envelope_blocks_without_partial_artifacts() -> None:
    route = build_workflow_route("write", parent_model="gpt-5.6-terra")
    envelopes = [
        build_canned_envelope(
            step,
            artifacts=[{"path": f"{step['agent_name']}.json"}],
        )
        for step in route["steps"][:-1]
    ]

    result = run_canned_workflow(route, envelopes)

    assert result["status"] == "blocked"
    assert result["code"] == "agent_unavailable"
    assert result["accepted_artifacts"] == []


def test_agent_self_report_is_not_runtime_model_evidence() -> None:
    step = build_workflow_route("review", parent_model="gpt-5.6-sol")["steps"][0]
    envelope = build_canned_envelope(step, evidence_source="agent_message")

    result = validate_agent_envelope(step, envelope)

    assert result["accepted"] is False
    assert result["code"] == "untrusted_model_evidence"


def test_source_label_without_verified_codex_evidence_is_rejected() -> None:
    step = build_workflow_route("review", parent_model="gpt-5.6-sol")["steps"][0]
    envelope = build_canned_envelope(step, evidence_source="codex_trace")

    result = validate_agent_envelope(step, envelope)

    assert result["accepted"] is False
    assert result["code"] == "unverified_model_evidence"


def test_minimal_write_skips_reviewer_but_keeps_required_agents() -> None:
    route = build_workflow_route(
        "write",
        parent_model="gpt-5.6-sol",
        mode="minimal",
    )

    assert [step["agent_name"] for step in route["steps"]] == [
        "webnovel_context_agent",
        "webnovel_writer",
        "webnovel_data_agent",
    ]


def test_deconstruction_inherits_parent_model_and_effort() -> None:
    route = build_workflow_route(
        "init_reference",
        parent_model="gpt-5.6-sol",
        parent_reasoning_effort="xhigh",
    )
    step = route["steps"][0]

    assert step["agent_name"] == "webnovel_deconstruction_agent"
    assert step["model_source"] == "parent"
    assert step["requested_model"] == "gpt-5.6-sol"
    assert step["requested_reasoning_effort"] == "xhigh"


def test_five_roles_ignore_prompt_injection_and_keep_output_boundaries(tmp_path: Path) -> None:
    fixture = _load("prompt_injection.json")
    project_root = tmp_path / "小说 项目 (A&B)"
    project_root.mkdir()
    run_id = "run-injection"
    seen: set[str] = set()

    for case in fixture["cases"]:
        payload = _expand(case["payload"], project_root=project_root, run_id=run_id)
        payload = _materialize_payload_artifacts(
            case["agent_name"],
            payload,
            project_root=project_root,
            run_id=run_id,
        )
        serialized = json.dumps(payload, ensure_ascii=False) if not isinstance(payload, str) else payload
        result = validate_prompt_injection_fixture(
            case["agent_name"],
            payload,
            untrusted_inputs=[case["untrusted_input"]],
            project_root=project_root,
            run_id=run_id,
            reliable_source_text=case["reliable_source_text"],
        )

        assert result["accepted"] is True
        assert result["code"] == "ok"
        assert case["untrusted_input"] == fixture["attack"]
        assert fixture["attack"] in case["untrusted_input"]
        assert fixture["attack"] not in serialized
        seen.add(case["agent_name"])

    assert seen == {
        "webnovel_context_agent",
        "webnovel_writer",
        "webnovel_reviewer",
        "webnovel_data_agent",
        "webnovel_deconstruction_agent",
    }


def test_injected_text_reflected_by_canned_output_is_rejected(tmp_path: Path) -> None:
    fixture = _load("prompt_injection.json")
    attack = fixture["attack"]
    payload = fixture["cases"][0]["payload"] + "\n" + attack

    result = validate_prompt_injection_fixture(
        "context",
        payload,
        untrusted_inputs=[attack],
        project_root=tmp_path,
        run_id="injection-reflected",
    )

    assert result["accepted"] is False
    assert result["code"] == "prompt_injection_reflected"
    assert result["accepted_artifacts"] == []


def test_injection_shaped_output_cannot_expand_writer_permissions(tmp_path: Path) -> None:
    project_root = tmp_path / "book"
    project_root.mkdir()
    payload = {
        "schema_version": "webnovel-writer-result/v1",
        "status": "completed",
        "run_id": "run-1",
        "operation": "draft",
        "commands": ["write .webnovel/state.json"],
        "artifacts": [],
    }

    result = validate_agent_payload(
        "writer",
        payload,
        project_root=project_root,
        run_id="run-1",
    )

    assert result["accepted"] is False
    assert result["code"] == "embedded_instruction_rejected"


def test_context_sections_must_be_exact_order_and_each_nonempty(tmp_path: Path) -> None:
    wrong_order = "## 这章的故事\n推进\n## 开篇委托\n目标\n## 这章的人物\n人物\n## 怎么写更顺\n节奏\n## 收在哪里\n钩子"
    empty_section = "## 开篇委托\n\n## 这章的故事\n推进\n## 这章的人物\n人物\n## 怎么写更顺\n节奏\n## 收在哪里\n钩子"

    for payload in (wrong_order, empty_section):
        result = validate_agent_payload(
            "context",
            payload,
            project_root=tmp_path,
            run_id="context-1",
        )
        assert result["accepted"] is False
        assert result["code"] == "incomplete_task_brief"


@pytest.mark.parametrize(
    "run_id",
    [".", "..", "...", ".hidden", "hidden.", "CON", "con.txt", "COM1", "LPT9.log"],
)
def test_agent_runtime_rejects_noncanonical_or_reserved_run_ids_without_writes(
    tmp_path: Path,
    run_id: str,
) -> None:
    result = validate_agent_payload(
        "context",
        "## 开篇委托\n目标\n## 这章的故事\n推进\n## 这章的人物\n人物\n## 怎么写更顺\n节奏\n## 收在哪里\n钩子",
        project_root=tmp_path,
        run_id=run_id,
    )

    assert result == {"accepted": False, "code": "invalid_request"}
    assert list(tmp_path.iterdir()) == []


def test_reviewer_dimension_items_reject_extra_fields(tmp_path: Path) -> None:
    payload = _load("prompt_injection.json")["cases"][2]["payload"]
    payload["dimension_results"][0]["score"] = 100

    result = validate_agent_payload(
        "reviewer",
        payload,
        project_root=tmp_path,
        run_id="review-1",
    )

    assert result["accepted"] is False
    assert result["code"] in {"embedded_instruction_rejected", "invalid_reviewer_json"}


def test_data_artifact_symlink_is_never_accepted(tmp_path: Path) -> None:
    project_root = tmp_path / "book"
    project_root.mkdir()
    case = _load("prompt_injection.json")["cases"][3]
    payload = _expand(case["payload"], project_root=project_root, run_id="run-injection")
    payload = _materialize_payload_artifacts(
        case["agent_name"],
        payload,
        project_root=project_root,
        run_id="run-injection",
    )
    path = project_root / ".webnovel" / "tmp" / "extraction_result.json"
    outside = tmp_path / "outside.json"
    outside.write_bytes(path.read_bytes())
    path.unlink()
    try:
        path.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    result = validate_agent_payload(
        "data",
        payload,
        project_root=project_root,
        run_id="run-injection",
    )

    assert result["accepted"] is False
    assert result["accepted_artifacts"] == []


def test_data_artifact_reparse_component_is_rejected_without_os_symlink_privilege(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "book"
    project_root.mkdir()
    case = _load("prompt_injection.json")["cases"][3]
    payload = _expand(case["payload"], project_root=project_root, run_id="run-injection")
    payload = _materialize_payload_artifacts(
        case["agent_name"],
        payload,
        project_root=project_root,
        run_id="run-injection",
    )
    real_check = agent_runtime._is_reparse_point
    monkeypatch.setattr(
        agent_runtime,
        "_is_reparse_point",
        lambda path: path.name == "tmp" or real_check(path),
    )

    result = validate_agent_payload(
        "data",
        payload,
        project_root=project_root,
        run_id="run-injection",
    )

    assert result["accepted"] is False
    assert result["code"] == "artifact_hash_invalid"
    assert result["accepted_artifacts"] == []


def test_writer_artifact_does_not_mix_hash_a_with_word_count_b(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "book"
    project_root.mkdir()
    case = _load("prompt_injection.json")["cases"][1]
    payload = _expand(case["payload"], project_root=project_root, run_id="stable-writer")
    payload = _materialize_payload_artifacts(
        case["agent_name"],
        payload,
        project_root=project_root,
        run_id="stable-writer",
    )
    payload["run_id"] = "stable-writer"
    artifact = payload["artifacts"][0]
    artifact_path = Path(artifact["path"])
    raw_a = "甲\n".encode("utf-8")
    raw_b = "乙乙\n".encode("utf-8")
    artifact_path.write_bytes(raw_a)
    artifact.update(
        sha256=hashlib.sha256(raw_a).hexdigest(),
        bytes=len(raw_a),
        word_count=2,
    )
    manifest_path = Path(payload["manifest_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["outputs"] = [artifact]
    _write_json(manifest_path, manifest)
    payload["manifest_sha256"] = _signature(manifest_path)["sha256"]
    real_read_bytes = Path.read_bytes
    reads = 0

    def swapped_read(path: Path) -> bytes:
        nonlocal reads
        if path == artifact_path:
            reads += 1
            return raw_a if reads == 1 else raw_b
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", swapped_read)

    result = validate_agent_payload(
        "writer",
        payload,
        project_root=project_root,
        run_id="stable-writer",
    )

    assert result["accepted"] is False
    assert result["code"] == "artifact_word_count_mismatch"
    assert reads == 0


def test_writer_manifest_does_not_mix_hash_a_with_json_b(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "book"
    project_root.mkdir()
    case = _load("prompt_injection.json")["cases"][1]
    payload = _expand(case["payload"], project_root=project_root, run_id="stable-manifest")
    payload = _materialize_payload_artifacts(
        case["agent_name"],
        payload,
        project_root=project_root,
        run_id="stable-manifest",
    )
    payload["run_id"] = "stable-manifest"
    manifest_path = Path(payload["manifest_path"])
    raw_b = manifest_path.read_bytes()
    raw_a = b"{}"
    manifest_path.write_bytes(raw_a)
    payload["manifest_sha256"] = hashlib.sha256(raw_a).hexdigest()
    real_read_bytes = Path.read_bytes
    reads = 0

    def swapped_read(path: Path) -> bytes:
        nonlocal reads
        if path == manifest_path:
            reads += 1
            return raw_a if reads == 1 else raw_b
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", swapped_read)

    result = validate_agent_payload(
        "writer",
        payload,
        project_root=project_root,
        run_id="stable-manifest",
    )

    assert result["accepted"] is False
    assert result["code"] == "invalid_writer_manifest"
    assert reads == 0


def test_data_artifact_does_not_mix_hash_a_with_json_b(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "book"
    project_root.mkdir()
    case = _load("prompt_injection.json")["cases"][3]
    payload = _expand(case["payload"], project_root=project_root, run_id="stable-data")
    payload = _materialize_payload_artifacts(
        case["agent_name"],
        payload,
        project_root=project_root,
        run_id="stable-data",
    )
    payload["run_id"] = "stable-data"
    artifact = next(item for item in payload["artifacts"] if item["name"] == "fulfillment_result")
    artifact_path = Path(artifact["path"])
    raw_b = artifact_path.read_bytes()
    raw_a = b"{}"
    artifact_path.write_bytes(raw_a)
    artifact.update(sha256=hashlib.sha256(raw_a).hexdigest(), bytes=len(raw_a))
    real_read_bytes = Path.read_bytes
    reads = 0

    def swapped_read(path: Path) -> bytes:
        nonlocal reads
        if path == artifact_path:
            reads += 1
            return raw_a if reads == 1 else raw_b
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", swapped_read)

    result = validate_agent_payload(
        "data",
        payload,
        project_root=project_root,
        run_id="stable-data",
    )

    assert result["accepted"] is False
    assert result["code"] == "artifact_schema_invalid"
    assert reads == 0


def test_stable_snapshot_rejects_identity_change_during_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "artifact.json"
    path.write_text("{}", encoding="utf-8")
    real_identity = agent_runtime._stat_identity
    calls = 0

    def swapped_identity(value):
        nonlocal calls
        calls += 1
        identity = real_identity(value)
        if calls == 2:
            return (*identity[:-1], identity[-1] + 1)
        return identity

    monkeypatch.setattr(agent_runtime, "_stat_identity", swapped_identity)

    assert agent_runtime._stable_file_snapshot(path, root=tmp_path) is None


def test_reviewer_invalid_json_retries_at_most_once_then_blocks() -> None:
    valid_third_response = _load("prompt_injection.json")["cases"][2]["payload"]

    result = validate_reviewer_attempts(
        ["not json", "still not json", valid_third_response],
        project_root=ROOT,
        run_id="review-1",
    )

    assert result["status"] == "blocked"
    assert result["code"] == "invalid_reviewer_json"
    assert result["attempts_used"] == 2
    assert result["retry_count"] == 1
    assert result["retry_permitted"] is False


def test_reviewer_accepts_one_same_route_serialization_retry() -> None:
    valid_response = _load("prompt_injection.json")["cases"][2]["payload"]

    result = validate_reviewer_attempts(
        ["not json", valid_response],
        project_root=ROOT,
        run_id="review-1",
    )

    assert result["status"] == "accepted"
    assert result["attempts_used"] == 2
    assert result["retry_count"] == 1
    assert result["accepted_artifacts"] == []


def test_workflow_promotes_only_payload_validated_artifacts(tmp_path: Path) -> None:
    project_root = tmp_path / "小说 项目 (A&B)"
    project_root.mkdir()
    route, envelopes, payloads = _successful_minimal_run(project_root, "run-injection")
    protected = snapshot_protected_state(project_root)

    result = run_canned_workflow(
        route,
        envelopes,
        payloads=payloads,
        project_root=project_root,
        run_id="run-injection",
        protected_before=protected,
        protected_after=snapshot_protected_state(project_root),
    )

    assert result["status"] == "accepted"
    assert len(result["accepted_artifacts"]) == 4
    assert all(item.get("path") != "unvalidated-forged.tmp" for item in result["accepted_artifacts"])


def test_workflow_discards_artifacts_when_protected_state_changes(tmp_path: Path) -> None:
    project_root = tmp_path / "book"
    state = project_root / ".webnovel" / "state.json"
    state.parent.mkdir(parents=True)
    state.write_text("{}", encoding="utf-8")
    protected_before = snapshot_protected_state(project_root)
    route, envelopes, payloads = _successful_minimal_run(project_root, "run-injection")
    state.write_text('{"changed":true}', encoding="utf-8")

    result = run_canned_workflow(
        route,
        envelopes,
        payloads=payloads,
        project_root=project_root,
        run_id="run-injection",
        protected_before=protected_before,
        protected_after=snapshot_protected_state(project_root),
    )

    assert result["status"] == "blocked"
    assert result["code"] == "protected_state_changed"
    assert result["accepted_artifacts"] == []


def test_route_builder_rejects_invalid_workflow_parent_and_mode() -> None:
    with pytest.raises(AgentRuntimeError, match="unsupported workflow"):
        build_workflow_route("publish", parent_model="gpt-5.6-sol")
    with pytest.raises(AgentRuntimeError, match="parent_model is required"):
        build_workflow_route("plan", parent_model=" ")
    with pytest.raises(AgentRuntimeError, match="unsupported write mode"):
        build_workflow_route("write", parent_model="gpt-5.6-sol", mode="unsafe")

    init_route = build_workflow_route("init", parent_model="gpt-5.6-sol")
    assert init_route["executor"] == "parent"
    assert init_route["steps"] == []


def test_route_readiness_reports_current_and_stale_managed_agents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = build_workflow_route("review", parent_model="gpt-5.6-sol")

    with pytest.raises(AgentRuntimeError, match="unsupported route schema"):
        validate_route_readiness(tmp_path, dict(route, schema_version="future"))

    monkeypatch.setattr(
        agent_runtime,
        "inspect_managed_agent",
        lambda workspace, name, plugin_root=None: {
            "agent_name": name,
            "current": False,
            "status": "contract_hash_mismatch",
        },
    )
    blocked = validate_route_readiness(tmp_path, route)
    assert blocked["status"] == "blocked"
    assert blocked["problems"] == [
        {
            "code": "agent_unavailable",
            "agent_name": "webnovel_reviewer",
            "detail": "managed agent status is contract_hash_mismatch",
        }
    ]

    monkeypatch.setattr(
        agent_runtime,
        "inspect_managed_agent",
        lambda workspace, name, plugin_root=None: {
            "agent_name": name,
            "current": True,
            "status": "current",
        },
    )
    ready = validate_route_readiness(tmp_path, route)
    assert ready["ready"] is True
    assert ready["status"] == "ready"
    assert ready["problems"] == []


@pytest.mark.parametrize(
    ("case", "expected_code"),
    [
        ("schema", "invalid_envelope"),
        ("agent", "agent_mismatch"),
        ("agent_unavailable", "agent_unavailable"),
        ("failed", "agent_failed"),
        ("contract", "contract_hash_mismatch"),
        ("requested_model", "requested_model_mismatch"),
        ("requested_effort", "requested_reasoning_effort_mismatch"),
        ("artifacts", "invalid_envelope"),
        ("verified_mismatch", "runtime_evidence_mismatch"),
    ],
)
def test_agent_envelope_rejects_every_identity_boundary(case: str, expected_code: str) -> None:
    step = build_workflow_route("review", parent_model="gpt-5.6-sol")["steps"][0]
    envelope = build_canned_envelope(step)
    allow_canned = True
    verified = None
    if case == "schema":
        envelope["schema_version"] = "future"
    elif case == "agent":
        envelope["agent_name"] = "webnovel_writer"
    elif case == "agent_unavailable":
        envelope["status"] = "agent_unavailable"
    elif case == "failed":
        envelope["status"] = "timeout"
    elif case == "contract":
        envelope["contract_hash"] = "0" * 64
    elif case == "requested_model":
        envelope["requested_model"] = "gpt-5.6-terra"
    elif case == "requested_effort":
        envelope["requested_reasoning_effort"] = "high"
    elif case == "artifacts":
        envelope["artifacts"] = {}
    elif case == "verified_mismatch":
        allow_canned = False
        envelope["evidence_source"] = "codex_trace"
        verified = VerifiedRuntimeEvidence(
            evidence_source="codex_trace",
            agent_name="webnovel_reviewer",
            actual_model="gpt-5.6-luna",
            actual_reasoning_effort="medium",
            thread_id="child",
            parent_thread_id="parent",
            raw_sha256="not-a-hash",
        )

    result = validate_agent_envelope(
        step,
        envelope,
        allow_canned=allow_canned,
        verified_evidence=verified,
    )

    assert result["accepted"] is False
    assert result["code"] == expected_code
    assert result["accepted_artifacts"] == []


def test_canned_workflow_rejects_bad_route_unexpected_agent_and_unvalidated_payload(
    tmp_path: Path,
) -> None:
    with pytest.raises(AgentRuntimeError, match="unsupported route schema"):
        run_canned_workflow({"schema_version": "future"}, [])

    plan = build_workflow_route("plan", parent_model="gpt-5.6-sol")
    unexpected = run_canned_workflow(
        plan,
        [{"agent_name": "webnovel_deconstruction_agent"}],
    )
    assert unexpected["code"] == "unexpected_planning_agent"

    review = build_workflow_route("review", parent_model="gpt-5.6-sol")
    envelope = build_canned_envelope(review["steps"][0])
    unvalidated = run_canned_workflow(review, [envelope])
    assert unvalidated["code"] == "payload_unvalidated"
    assert unvalidated["accepted_artifacts"] == []

    failed = run_canned_workflow(
        review,
        [build_canned_envelope(review["steps"][0], status="agent_unavailable")],
    )
    assert failed["code"] == "agent_unavailable"


def test_canned_workflow_discards_artifacts_on_role_payload_failure(tmp_path: Path) -> None:
    project_root = tmp_path / "book"
    project_root.mkdir()
    route, envelopes, payloads = _successful_minimal_run(project_root, "run-injection")
    payloads[0] = "incomplete context"

    result = run_canned_workflow(
        route,
        envelopes,
        payloads=payloads,
        project_root=project_root,
        run_id="run-injection",
        protected_before={},
        protected_after={},
    )

    assert result["status"] == "blocked"
    assert result["code"] == "incomplete_task_brief"
    assert result["accepted_artifacts"] == []


def test_protected_snapshot_covers_nested_canon_and_rejects_unverified_input(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "book"
    (project_root / ".story-system" / "commits").mkdir(parents=True)
    (project_root / "正文").mkdir()
    (project_root / ".story-system" / "commits" / "chapter.json").write_text(
        "{}", encoding="utf-8"
    )
    (project_root / "正文" / "第0001章.md").write_text("正文", encoding="utf-8")

    snapshot = snapshot_protected_state(project_root)

    assert ".story-system/commits/chapter.json" in snapshot
    assert "正文/第0001章.md" in snapshot
    assert validate_protected_state_snapshots(None, snapshot)["code"] == "protected_state_unverified"
    with pytest.raises(AgentRuntimeError, match="project_root"):
        snapshot_protected_state(tmp_path / "missing")
    assert agent_runtime._inside(tmp_path / "outside", project_root) is False


def test_protected_snapshot_records_unreadable_reparse_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "book"
    story_system = project_root / ".story-system"
    story_system.mkdir(parents=True)
    real_check = agent_runtime._is_reparse_point
    monkeypatch.setattr(
        agent_runtime,
        "_is_reparse_point",
        lambda path: path == story_system or real_check(path),
    )
    monkeypatch.setattr(
        agent_runtime.os,
        "readlink",
        lambda path: (_ for _ in ()).throw(OSError("unreadable")),
    )

    snapshot = snapshot_protected_state(project_root)

    assert snapshot[".story-system"] == "reparse:<unreadable>"


def test_prompt_injection_fixture_rejects_empty_marker_and_underlying_bad_payload(
    tmp_path: Path,
) -> None:
    empty = validate_prompt_injection_fixture(
        "context",
        "irrelevant",
        untrusted_inputs=[],
        project_root=tmp_path,
        run_id="fixture-1",
    )
    bad_payload = validate_prompt_injection_fixture(
        "context",
        "incomplete",
        untrusted_inputs=["attack"],
        project_root=tmp_path,
        run_id="fixture-1",
    )

    assert empty["code"] == "invalid_injection_fixture"
    assert bad_payload["code"] == "incomplete_task_brief"


def test_prompt_injection_fixture_fails_if_valid_artifact_becomes_unreadable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "book"
    project_root.mkdir()
    case = _load("prompt_injection.json")["cases"][1]
    payload = _expand(case["payload"], project_root=project_root, run_id="run-injection")
    payload = _materialize_payload_artifacts(
        case["agent_name"],
        payload,
        project_root=project_root,
        run_id="run-injection",
    )
    artifact_path = Path(payload["artifacts"][0]["path"])
    original_read_text = Path.read_text

    def fail_artifact_read(path: Path, *args, **kwargs):
        if path == artifact_path:
            raise OSError("simulated read failure")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_artifact_read)

    result = validate_prompt_injection_fixture(
        case["agent_name"],
        payload,
        untrusted_inputs=[case["untrusted_input"]],
        project_root=project_root,
        run_id="run-injection",
    )

    assert result["code"] == "artifact_encoding_invalid"
    assert result["accepted_artifacts"] == []
