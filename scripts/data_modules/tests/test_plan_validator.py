from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from data_modules import plan_validator
from data_modules.plan_validator import (
    VALIDATION_SCHEMA_VERSION,
    compute_plan_content_sha256,
    expected_targets,
    validate_plan_manifest,
)
from data_modules.tests.plan_test_helpers import make_valid_plan


def _write_manifest(path: Path, manifest: dict, *, refresh_content: bool = False):
    if refresh_content:
        manifest["content_sha256"] = compute_plan_content_sha256(manifest)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def _codes(report):
    return {item["code"] for item in report["problems"]}


def test_valid_plan_manifest_passes_without_writing_facts(tmp_path):
    manifest_path, manifest = make_valid_plan(tmp_path)
    facts_before = sorted((tmp_path / "大纲").glob("**/*")) if (tmp_path / "大纲").exists() else []

    report = validate_plan_manifest(tmp_path, manifest_path)

    assert report["schema_version"] == VALIDATION_SCHEMA_VERSION
    assert report["ok"] is True
    assert report["content_sha256"] == manifest["content_sha256"]
    assert set(report["artifact_hashes"]) == {"beat", "timeline", "outline", "writeback"}
    assert (sorted((tmp_path / "大纲").glob("**/*")) if (tmp_path / "大纲").exists() else []) == facts_before
    assert expected_targets(3)["outline"] == "大纲/第3卷-详细大纲.md"


@pytest.mark.parametrize(
    ("code", "mutate"),
    [
        ("parent_only_violation", lambda m: m.update(executor="agent")),
        ("unresolved_blockers", lambda m: m.update(blockers=["冲突"])),
        ("insufficient_crises", lambda m: m["beat"].update(crises=m["beat"]["crises"][:2])),
        ("midpoint_missing", lambda m: m["beat"].update(midpoint={"event": "", "reason_if_none": ""})),
        ("final_hook_missing", lambda m: m["beat"].update(final_open_question="")),
        ("chapter_coverage_mismatch", lambda m: m["chapters"][1].update(chapter=3)),
        ("chapter_goal_missing", lambda m: m["chapters"][0].update(goal="")),
        ("invalid_time_offset", lambda m: m["chapters"][0].update(time_offset_minutes=-1)),
        ("invalid_time_span", lambda m: m["chapters"][0].update(span_minutes=0)),
        ("transition_missing", lambda m: m["chapters"][0].update(transition="")),
        ("invalid_time_mode", lambda m: m["chapters"][0].update(time_mode="warp")),
        ("flashback_unmarked", lambda m: m["chapters"][0].update(time_mode="flashback")),
        ("invalid_node", lambda m: m["chapters"][0]["cbn"].update(subject="")),
        ("invalid_cpn_count", lambda m: m["chapters"][0].update(cpns=m["chapters"][0]["cpns"][:1])),
        ("invalid_must_cover", lambda m: m["chapters"][0].update(must_cover_nodes=["a", "b", "c", "d", "e"])),
        ("invalid_forbidden_zones", lambda m: m["chapters"][0].update(forbidden_zones=["a", "b", "c", "d", "e", "f"])),
        ("chapter_hook_missing", lambda m: m["chapters"][0].update(chapter_end_open_question="")),
        ("invalid_countdowns", lambda m: m["chapters"][0].update(countdowns=[])),
        ("invalid_countdown", lambda m: m["chapters"][0].update(countdowns={"封门": -1})),
        ("countdown_mismatch", lambda m: m["chapters"][1].update(countdowns={"封门": 59})),
        (
            "timeline_not_monotonic",
            lambda m: (
                m["chapters"][0].update(time_offset_minutes=60),
                m["chapters"][1].update(time_offset_minutes=0),
            ),
        ),
        ("cen_cbn_handoff_mismatch", lambda m: m["chapters"][1]["cbn"].update(handoff_id="wrong")),
        ("final_hook_mismatch", lambda m: m["chapters"][1].update(chapter_end_open_question="另一个问题？")),
    ],
)
def test_structural_plan_failures_are_deterministic(tmp_path, code, mutate):
    manifest_path, manifest = make_valid_plan(tmp_path)
    mutate(manifest)
    _write_manifest(manifest_path, manifest)

    report = validate_plan_manifest(tmp_path, manifest_path)

    assert report["ok"] is False
    assert code in _codes(report)
    assert not (tmp_path / "大纲").exists()


def test_plan_validator_detects_artifact_tamper_and_missing(tmp_path):
    manifest_path, manifest = make_valid_plan(tmp_path)
    outline = tmp_path / manifest["artifacts"]["outline"]["path"]
    outline.write_text(outline.read_text(encoding="utf-8") + "\n篡改", encoding="utf-8")
    assert "artifact_hash_mismatch" in _codes(validate_plan_manifest(tmp_path, manifest_path))

    outline.unlink()
    assert "artifact_missing" in _codes(validate_plan_manifest(tmp_path, manifest_path))


def test_plan_validator_rejects_placeholder_bom_marker_and_mismatch(tmp_path):
    manifest_path, manifest = make_valid_plan(tmp_path)
    beat = tmp_path / manifest["artifacts"]["beat"]["path"]
    beat.write_text(beat.read_text(encoding="utf-8") + "\n[TODO]", encoding="utf-8")
    manifest["artifacts"]["beat"]["sha256"] = hashlib.sha256(beat.read_bytes()).hexdigest()
    _write_manifest(manifest_path, manifest)
    assert "artifact_placeholder" in _codes(validate_plan_manifest(tmp_path, manifest_path))

    beat.write_bytes(b"\xef\xbb\xbf" + "内容".encode("utf-8"))
    manifest["artifacts"]["beat"]["sha256"] = hashlib.sha256(beat.read_bytes()).hexdigest()
    _write_manifest(manifest_path, manifest)
    assert "artifact_read_failed" in _codes(validate_plan_manifest(tmp_path, manifest_path))

    manifest_path, manifest = make_valid_plan(tmp_path, run_id="marker-test")
    timeline = tmp_path / manifest["artifacts"]["timeline"]["path"]
    timeline.write_text(timeline.read_text(encoding="utf-8").replace(manifest["content_sha256"], "0" * 64), encoding="utf-8")
    manifest["artifacts"]["timeline"]["sha256"] = hashlib.sha256(timeline.read_bytes()).hexdigest()
    _write_manifest(manifest_path, manifest)
    assert "content_marker_mismatch" in _codes(validate_plan_manifest(tmp_path, manifest_path))


def test_plan_validator_rejects_out_of_bounds_and_wrong_target(tmp_path):
    manifest_path, manifest = make_valid_plan(tmp_path)
    manifest["artifacts"]["beat"]["path"] = "../outside.md"
    manifest["artifacts"]["timeline"]["target"] = "正文/第1章.md"
    _write_manifest(manifest_path, manifest)

    codes = _codes(validate_plan_manifest(tmp_path, manifest_path))
    assert "artifact_path_out_of_bounds" in codes
    assert "artifact_target_mismatch" in codes


def test_plan_validator_rejects_manifest_location_and_invalid_json(tmp_path):
    outside = tmp_path / "manifest.json"
    outside.write_text("{}", encoding="utf-8")
    assert _codes(validate_plan_manifest(tmp_path, outside)) == {"manifest_path_invalid"}

    manifest_path = tmp_path / ".webnovel" / "tmp" / "plan-runs" / "bad" / "plan-manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text("{", encoding="utf-8")
    assert "manifest_invalid" in _codes(validate_plan_manifest(tmp_path, manifest_path))


def test_manifest_bounded_read_rejects_oversize_and_midread_reparse(tmp_path, monkeypatch):
    manifest_path, _ = make_valid_plan(tmp_path, run_id="manifest-stable-read")
    monkeypatch.setattr(plan_validator, "_MAX_MANIFEST_BYTES", 1)
    assert "manifest_invalid" in _codes(validate_plan_manifest(tmp_path, manifest_path))

    monkeypatch.setattr(plan_validator, "_MAX_MANIFEST_BYTES", 2 * 1024 * 1024)
    real_reparse = plan_validator._is_reparse_point
    calls = {"manifest": 0}

    def swaps_during_read(path):
        if Path(path) == manifest_path:
            calls["manifest"] += 1
            return calls["manifest"] >= 3
        return real_reparse(Path(path))

    monkeypatch.setattr(plan_validator, "_is_reparse_point", swaps_during_read)
    report = validate_plan_manifest(tmp_path, manifest_path)
    assert "manifest_invalid" in _codes(report)
    assert "reparse-point" in report["problems"][0]["detail"]


def test_plan_validator_cross_checks_authored_outline_and_timeline(tmp_path):
    manifest_path, manifest = make_valid_plan(tmp_path)
    outline = tmp_path / manifest["artifacts"]["outline"]["path"]
    outline.write_text(outline.read_text(encoding="utf-8").replace("主角确认失踪线索", "删去目标"), encoding="utf-8")
    manifest["artifacts"]["outline"]["sha256"] = hashlib.sha256(outline.read_bytes()).hexdigest()
    timeline = tmp_path / manifest["artifacts"]["timeline"]["path"]
    timeline.write_text(timeline.read_text(encoding="utf-8").replace("T+60m", "T+61m"), encoding="utf-8")
    manifest["artifacts"]["timeline"]["sha256"] = hashlib.sha256(timeline.read_bytes()).hexdigest()
    _write_manifest(manifest_path, manifest)

    codes = _codes(validate_plan_manifest(tmp_path, manifest_path))
    assert "outline_manifest_mismatch" in codes
    assert "timeline_offset_missing" in codes


def test_plan_validator_cli_json_and_text_exit_codes(tmp_path, monkeypatch, capsys):
    manifest_path, _ = make_valid_plan(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "plan-validate",
            "--project-root",
            str(tmp_path),
            "--manifest",
            str(manifest_path),
            "--format",
            "json",
        ],
    )
    with pytest.raises(SystemExit) as caught:
        plan_validator.main()
    assert caught.value.code == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["blockers"] = ["冲突"]
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "plan-validate",
            "--project-root",
            str(tmp_path),
            "--manifest",
            str(manifest_path),
            "--format",
            "text",
        ],
    )
    with pytest.raises(SystemExit) as caught:
        plan_validator.main()
    assert caught.value.code == 2
    output = capsys.readouterr().out
    assert "status: blocked" in output
    assert "unresolved_blockers" in output


@pytest.mark.parametrize(
    ("code", "mutate"),
    [
        ("invalid_beat", lambda m: m.update(beat="bad")),
        ("chapters_missing", lambda m: m.update(chapters="bad")),
        ("invalid_chapter_range", lambda m: m.update(chapter_range=[2, 1])),
        ("artifact_set_mismatch", lambda m: m["artifacts"].pop("beat")),
        ("invalid_run_id", lambda m: m.update(run_id="../bad")),
        ("invalid_volume", lambda m: m.update(volume=0)),
        ("invalid_artifact", lambda m: m["artifacts"].update(beat="bad")),
        ("invalid_artifact_shape", lambda m: m["artifacts"]["beat"].update(extra=True)),
        ("artifact_target_out_of_bounds", lambda m: m["artifacts"]["beat"].update(target="../beat.md")),
        ("artifact_hash_invalid", lambda m: m["artifacts"]["beat"].update(sha256="bad")),
    ],
)
def test_manifest_shape_failures_are_reported(tmp_path, code, mutate):
    manifest_path, manifest = make_valid_plan(tmp_path)
    mutate(manifest)
    _write_manifest(manifest_path, manifest, refresh_content=True)

    assert code in _codes(validate_plan_manifest(tmp_path, manifest_path))


@pytest.mark.parametrize("run_id", [".", ".."])
def test_validator_rejects_dot_segment_run_id_without_runtime_writes(tmp_path, run_id):
    manifest_path, manifest = make_valid_plan(tmp_path)
    manifest["run_id"] = run_id
    _write_manifest(manifest_path, manifest, refresh_content=True)

    report = validate_plan_manifest(tmp_path, manifest_path)

    assert "invalid_run_id" in _codes(report)
    assert not (tmp_path / ".webnovel" / "plan-runs").exists()


def test_node_crisis_and_manifest_object_failures(tmp_path):
    manifest_path, manifest = make_valid_plan(tmp_path)
    manifest["chapters"][0]["cbn"] = []
    manifest["chapters"][0]["cpns"][0]["handoff_id"] = 7
    manifest["beat"]["crises"] = [
        {"conflict": "", "cost": "代价", "result": "结果"},
        7,
        "有效危机",
    ]
    _write_manifest(manifest_path, manifest, refresh_content=True)

    codes = _codes(validate_plan_manifest(tmp_path, manifest_path))
    assert {"invalid_node", "invalid_handoff", "invalid_crisis"} <= codes

    manifest_path.write_text("[]", encoding="utf-8")
    assert "manifest_invalid" in _codes(validate_plan_manifest(tmp_path, manifest_path))


@pytest.mark.parametrize("raw", [b"", b"\xff"])
def test_artifact_empty_or_invalid_utf8_is_rejected(tmp_path, raw):
    manifest_path, manifest = make_valid_plan(tmp_path)
    beat = tmp_path / manifest["artifacts"]["beat"]["path"]
    beat.write_bytes(raw)
    manifest["artifacts"]["beat"]["sha256"] = hashlib.sha256(raw).hexdigest()
    _write_manifest(manifest_path, manifest)

    assert "artifact_read_failed" in _codes(validate_plan_manifest(tmp_path, manifest_path))


def test_artifact_size_and_staging_containment_are_enforced(tmp_path, monkeypatch):
    manifest_path, manifest = make_valid_plan(tmp_path)
    monkeypatch.setattr(plan_validator, "_MAX_ARTIFACT_BYTES", 1)
    assert "artifact_read_failed" in _codes(validate_plan_manifest(tmp_path, manifest_path))

    monkeypatch.setattr(plan_validator, "_MAX_ARTIFACT_BYTES", 2 * 1024 * 1024)
    outside = tmp_path / "elsewhere.md"
    outside.write_text("内容", encoding="utf-8")
    manifest["artifacts"]["beat"]["path"] = "elsewhere.md"
    manifest["artifacts"]["beat"]["sha256"] = hashlib.sha256(outside.read_bytes()).hexdigest()
    _write_manifest(manifest_path, manifest)
    assert "artifact_path_out_of_bounds" in _codes(validate_plan_manifest(tmp_path, manifest_path))


def test_artifact_bounded_read_rejects_path_swap(tmp_path, monkeypatch):
    manifest_path, manifest = make_valid_plan(tmp_path, run_id="artifact-stable-read")
    beat = tmp_path / manifest["artifacts"]["beat"]["path"]
    real_reparse = plan_validator._is_reparse_point
    calls = {"beat": 0}

    def swaps_during_read(path):
        if Path(path) == beat:
            calls["beat"] += 1
            return calls["beat"] >= 3
        return real_reparse(Path(path))

    monkeypatch.setattr(plan_validator, "_is_reparse_point", swaps_during_read)
    report = validate_plan_manifest(tmp_path, manifest_path)
    assert "artifact_read_failed" in _codes(report)
    assert not report["ok"]


def test_relative_manifest_project_missing_and_authored_cross_checks(tmp_path):
    manifest_path, manifest = make_valid_plan(tmp_path)
    relative = manifest_path.relative_to(tmp_path)
    assert validate_plan_manifest(tmp_path, relative)["ok"] is True
    assert _codes(validate_plan_manifest(tmp_path / "missing", relative)) == {"project_root_missing"}

    writeback = tmp_path / manifest["artifacts"]["writeback"]["path"]
    writeback.write_text("{", encoding="utf-8")
    manifest["artifacts"]["writeback"]["sha256"] = hashlib.sha256(writeback.read_bytes()).hexdigest()
    beat = tmp_path / manifest["artifacts"]["beat"]["path"]
    beat.write_text(beat.read_text(encoding="utf-8").replace("车站封闭", "删去危机"), encoding="utf-8")
    manifest["artifacts"]["beat"]["sha256"] = hashlib.sha256(beat.read_bytes()).hexdigest()
    _write_manifest(manifest_path, manifest)
    codes = _codes(validate_plan_manifest(tmp_path, manifest_path))
    assert "writeback_json_invalid" in codes
    assert "beat_manifest_mismatch" in codes
