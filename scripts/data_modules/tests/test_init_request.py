from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

import data_modules.init_request as init_request
from data_modules.init_request import INIT_REQUEST_SCHEMA, InitRequestError, load_init_request


def valid_init_payload(workspace: Path) -> dict:
    return {
        "schema_version": INIT_REQUEST_SCHEMA,
        "workspace_root": str(workspace.resolve()),
        "project_slug": "星火长夜",
        "project": {
            "title": "星火长夜",
            "genre": "玄幻",
            "target_words": 300000,
            "target_chapters": 100,
            "one_liner": "失去灵根的守夜人必须借凡人之身封住天裂。",
            "core_conflict": "每次封印天裂都会加深他与众生的误解。",
            "target_reader": "男频成长读者",
            "platform": "本地创作",
        },
        "protagonist": {
            "name": "沈砚",
            "desire": "证明凡人也能守住一座城",
            "flaw": "把所有责任都揽到自己身上",
            "archetype": "负重前行者",
            "structure": "单主角",
        },
        "relationship": {
            "heroine_config": "无女主",
            "heroine_names": [],
            "heroine_role": "",
            "co_protagonists": [],
            "co_protagonist_roles": [],
            "antagonist_tiers": {"小反派": "巡夜校尉", "大反派": "天裂意志"},
            "antagonist_level": "多层压力",
            "antagonist_mirror": "反派以牺牲少数换取秩序，主角拒绝替别人决定代价。",
        },
        "golden_finger": {
            "type": "契约流",
            "name": "余烬契约",
            "style": "沉默工具型",
            "visibility": "暗牌",
            "irreversible_cost": "每次借力都会永久失去一段私人记忆",
            "growth_rhythm": "每卷只解锁一个新契约槽",
        },
        "world": {
            "scale": "单城到多域",
            "factions": "守夜司、城盟与裂隙教团",
            "power_system_type": "契约与武道双轨",
            "social_class": "城籍、流民与契约者",
            "resource_distribution": "灯芯配额由城盟控制",
            "currency_system": "灯筹",
            "currency_exchange": "十灯筹换一夜庇护",
            "sect_hierarchy": "司主-校尉-守夜人",
            "cultivation_chain": "点灯-守火-照夜",
            "cultivation_subtiers": "初燃-稳定-圆满",
        },
        "constraints": {
            "selected_idea": {
                "title": "凡人守夜",
                "one_liner": "失去灵根的守夜人必须借凡人之身封住天裂。",
                "anti_trope": "力量越强，能被记住的人反而越少",
                "hard_constraints": [
                    "契约不能凭空创造力量",
                    "所有越级胜利必须永久失去一项私人记忆",
                ],
                "protagonist_flaw": "把所有责任都揽到自己身上",
                "antagonist_mirror": "反派以牺牲少数换取秩序，主角拒绝替别人决定代价。",
                "opening_hook": "主角在全城庆功时发现没人记得他的名字",
                "origin": "original",
            },
            "core_selling_points": ["凡人守城", "代价可见"],
            "creativity_refusal_reason": "",
        },
    }


def test_reference_builders_and_optional_object_have_safe_empty_defaults(tmp_path):
    candidate = {"candidate_id": "none"}
    confirmation = init_request.build_reference_adoption_confirmation(
        project_root=str((tmp_path / "project").resolve()),
        selected_idea={},
        reference_candidate=candidate,
    )
    assert confirmation["choice_request"]["status"] == "awaiting_user"
    assert confirmation["choice_marker"].startswith("WEBNOVEL_INIT_REFERENCE_CHOICE/v1 ")
    assert init_request.build_reference_binding_marker(candidate).startswith(
        "WEBNOVEL_INIT_REFERENCE_BINDING/v1 "
    )
    assert init_request._object({}, "optional", allowed=set(), required=False) == {}


def test_workspace_root_must_be_a_directory(tmp_path):
    workspace_file = tmp_path / "not-a-directory"
    workspace_file.write_text("file", encoding="utf-8")
    with pytest.raises(InitRequestError, match="existing directory"):
        init_request._validate_workspace(str(workspace_file.resolve()), "project")


def write_request(home: Path, payload: dict, *, name: str = "request.json", bom: bool = False) -> Path:
    request_dir = home / "tmp" / "init"
    request_dir.mkdir(parents=True, exist_ok=True)
    path = request_dir / name
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + raw)
    return path


def test_init_request_normalizes_confirmed_payload(tmp_path, monkeypatch):
    home = tmp_path / "isolated home"
    workspace = tmp_path / "中文 工作区 (A) & B 🚀"
    workspace.mkdir()
    monkeypatch.setenv("WEBNOVEL_HOME", str(home))
    request_file = write_request(home, valid_init_payload(workspace))

    loaded = load_init_request(request_file)

    assert loaded["schema_version"] == INIT_REQUEST_SCHEMA
    assert loaded["project_root"] == str((workspace / "星火长夜").resolve())
    assert loaded["constraints"]["selected_idea"]["hard_constraints"] == [
        "契约不能凭空创造力量",
        "所有越级胜利必须永久失去一项私人记忆",
    ]
    assert loaded["reference_candidate"] is None


@pytest.mark.parametrize("slug", ["", ".", "..", ".codex", "../逃逸", "CON", "bad/name", "尾点."])
def test_init_request_rejects_unsafe_slug(tmp_path, monkeypatch, slug):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("WEBNOVEL_HOME", str(home))
    payload = valid_init_payload(workspace)
    payload["project_slug"] = slug
    request_file = write_request(home, payload)

    with pytest.raises(InitRequestError):
        load_init_request(request_file)


def test_init_request_requires_exact_temp_root_utf8_and_fields(tmp_path, monkeypatch):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("WEBNOVEL_HOME", str(home))
    payload = valid_init_payload(workspace)

    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(InitRequestError, match="WEBNOVEL_HOME/tmp/init"):
        load_init_request(outside)

    with pytest.raises(InitRequestError, match="without BOM"):
        load_init_request(write_request(home, payload, name="bom.json", bom=True))

    payload["unexpected"] = True
    with pytest.raises(InitRequestError, match="unknown fields"):
        load_init_request(write_request(home, payload, name="unknown.json"))


def test_init_request_rejects_unconfirmed_reference_as_selected_origin(tmp_path, monkeypatch):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("WEBNOVEL_HOME", str(home))
    payload = valid_init_payload(workspace)
    payload["constraints"]["selected_idea"]["origin"] = "reference_adopted"
    payload["reference_candidate"] = {
        "status": "proposed",
        "candidate_id": "candidate-1",
        "confidence": 0.99,
    }

    with pytest.raises(InitRequestError, match="requires an adopted reference"):
        load_init_request(write_request(home, payload))


def test_init_request_requires_all_reference_adoption_gates(tmp_path, monkeypatch):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("WEBNOVEL_HOME", str(home))
    payload = valid_init_payload(workspace)
    payload["constraints"]["selected_idea"]["origin"] = "mixed"
    payload["reference_candidate"] = {
        "status": "adopted",
        "candidate_id": "candidate-1",
        "source_title": "仅作结构参考",
        "source_path": str((workspace / "source.txt").resolve()),
        "source_sha256": "a" * 64,
        "output_sha256": "b" * 64,
        "confidence": 0.84,
        "transformation_notes": "已去除专名并改变冲突兑现方式",
        "route_sha256": "c" * 64,
        "contract_hash": "d" * 64,
        "binding_marker": "marker",
    }

    with pytest.raises(InitRequestError, match="confidence >= 0.85"):
        load_init_request(write_request(home, payload))


def test_init_request_rejects_workspace_inside_plugin(tmp_path, monkeypatch):
    plugin_root = Path(__file__).resolve().parents[3]
    home = tmp_path / "home"
    monkeypatch.setenv("WEBNOVEL_HOME", str(home))
    payload = valid_init_payload(plugin_root)

    with pytest.raises(InitRequestError, match="plugin directory"):
        load_init_request(write_request(home, payload))


def test_init_request_validation_matrix_covers_nested_types_and_consistency(tmp_path, monkeypatch):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("WEBNOVEL_HOME", str(home))
    base = valid_init_payload(workspace)
    cases: list[tuple[str, object]] = [
        ("schema", lambda p: p.update(schema_version="wrong")),
        ("project-object", lambda p: p.update(project=[])),
        ("project-unknown", lambda p: p["project"].update(unexpected=True)),
        ("title-type", lambda p: p["project"].update(title=1)),
        ("title-empty", lambda p: p["project"].update(title="")),
        ("title-nul", lambda p: p["project"].update(title="bad\x00title")),
        ("target-type", lambda p: p["project"].update(target_words=True)),
        ("target-zero", lambda p: p["project"].update(target_words=0, target_chapters=0)),
        ("list-type", lambda p: p["constraints"]["selected_idea"].update(hard_constraints="x")),
        ("list-item-type", lambda p: p["constraints"]["selected_idea"].update(hard_constraints=[1, "x"])),
        ("list-item-empty", lambda p: p["constraints"]["selected_idea"].update(hard_constraints=["", "x"])),
        ("map-type", lambda p: p["relationship"].update(antagonist_tiers=[])),
        ("map-value-type", lambda p: p["relationship"].update(antagonist_tiers={"小反派": 1})),
        ("map-key-empty", lambda p: p["relationship"].update(antagonist_tiers={"": "x"})),
        ("slug-type", lambda p: p.update(project_slug=1)),
        ("slug-space", lambda p: p.update(project_slug=" 星火")),
        ("slug-nfkc", lambda p: p.update(project_slug="ＡＢＣ")),
        ("slug-long", lambda p: p.update(project_slug="长" * 121)),
        ("workspace-type", lambda p: p.update(workspace_root=None)),
        ("workspace-relative", lambda p: p.update(workspace_root="relative")),
        ("workspace-missing", lambda p: p.update(workspace_root=str(tmp_path / "missing"))),
        ("creativity", lambda p: p["constraints"]["selected_idea"].update(anti_trope="", hard_constraints=[])),
        ("origin", lambda p: p["constraints"]["selected_idea"].update(origin="unknown")),
        ("one-liner", lambda p: p["constraints"]["selected_idea"].update(one_liner="conflict")),
        ("flaw", lambda p: p["constraints"]["selected_idea"].update(protagonist_flaw="conflict")),
        ("mirror", lambda p: p["constraints"]["selected_idea"].update(antagonist_mirror="conflict")),
        ("reference-object", lambda p: p.update(reference_candidate=[])),
        ("reference-unknown", lambda p: p.update(reference_candidate={"status": "discarded", "extra": 1})),
        ("reference-status", lambda p: p.update(reference_candidate={"status": "unknown"})),
        ("reference-confidence-type", lambda p: p.update(reference_candidate={"status": "discarded", "confidence": "high"})),
        ("reference-confidence-range", lambda p: p.update(reference_candidate={"status": "discarded", "confidence": 2})),
        ("reference-bool", lambda p: p.update(reference_candidate={"status": "discarded", "quality_passed": "yes"})),
        (
            "reference-hash",
            lambda p: p.update(reference_candidate={"status": "discarded", "source_sha256": "BAD"}),
        ),
        (
            "reference-required",
            lambda p: p.update(
                reference_candidate={
                    "status": "adopted",
                    "candidate_id": "x",
                    "confidence": 0.9,
                    "quality_passed": True,
                    "user_confirmed": True,
                    "runtime_evidence_accepted": True,
                }
            ),
        ),
    ]
    for index, (label, mutate) in enumerate(cases):
        payload = deepcopy(base)
        mutate(payload)
        with pytest.raises(InitRequestError):
            load_init_request(write_request(home, payload, name=f"invalid-{index}-{label}.json"))

    derive_words = deepcopy(base)
    derive_words["project"]["target_words"] = 0
    loaded_words = load_init_request(write_request(home, derive_words, name="derive-words.json"))
    assert loaded_words["project"]["target_words"] == 300000
    derive_chapters = deepcopy(base)
    derive_chapters["project"]["target_chapters"] = 0
    loaded_chapters = load_init_request(write_request(home, derive_chapters, name="derive-chapters.json"))
    assert loaded_chapters["project"]["target_chapters"] == 100


def test_init_request_file_shape_and_encoding_fail_closed(tmp_path, monkeypatch):
    home = tmp_path / "home"
    request_dir = home / "tmp" / "init"
    request_dir.mkdir(parents=True)
    monkeypatch.setenv("WEBNOVEL_HOME", str(home))

    with pytest.raises(InitRequestError, match="absolute"):
        load_init_request("relative.json")
    with pytest.raises(InitRequestError, match="unavailable"):
        load_init_request(request_dir / "missing.json")

    empty = request_dir / "empty.json"
    empty.write_bytes(b"")
    with pytest.raises(InitRequestError, match="size"):
        load_init_request(empty)
    oversized = request_dir / "oversized.json"
    oversized.write_bytes(b"x" * (256 * 1024 + 1))
    with pytest.raises(InitRequestError, match="size"):
        load_init_request(oversized)
    invalid_utf8 = request_dir / "invalid-utf8.json"
    invalid_utf8.write_bytes(b"\xff")
    with pytest.raises(InitRequestError, match="valid UTF-8"):
        load_init_request(invalid_utf8)
    malformed = request_dir / "malformed.json"
    malformed.write_text("{", encoding="utf-8")
    with pytest.raises(InitRequestError, match="one JSON object"):
        load_init_request(malformed)
    array = request_dir / "array.json"
    array.write_text("[]", encoding="utf-8")
    with pytest.raises(InitRequestError, match="top level"):
        load_init_request(array)
    directory = request_dir / "directory.json"
    directory.mkdir()
    with pytest.raises(InitRequestError, match="regular non-symlink"):
        load_init_request(directory)


def test_init_request_rejects_host_directory_and_plugin_target(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("WEBNOVEL_HOME", str(home))
    host_workspace = tmp_path / ".codex" / "workspace"
    host_workspace.mkdir(parents=True)
    payload = valid_init_payload(host_workspace)
    with pytest.raises(InitRequestError, match="host directory"):
        load_init_request(write_request(home, payload, name="host.json"))

    plugin_root = Path(__file__).resolve().parents[3]
    plugin_payload = valid_init_payload(plugin_root.parent)
    plugin_payload["project_slug"] = plugin_root.name
    with pytest.raises(InitRequestError, match="plugin directory"):
        load_init_request(write_request(home, plugin_payload, name="plugin-target.json"))


def test_init_request_rejects_dangling_target_link(tmp_path, monkeypatch):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("WEBNOVEL_HOME", str(home))
    target = workspace / "星火长夜"
    try:
        target.symlink_to(workspace / "missing-target", target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable in this Windows test environment")

    with pytest.raises(InitRequestError, match="symlink or junction"):
        load_init_request(write_request(home, valid_init_payload(workspace)))


def test_init_request_rejects_fstat_identity_change_during_bounded_read(tmp_path, monkeypatch):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("WEBNOVEL_HOME", str(home))
    request_file = write_request(home, valid_init_payload(workspace))
    real_fstat = os.fstat
    calls = 0

    def changing_fstat(fd):
        nonlocal calls
        calls += 1
        value = real_fstat(fd)
        return SimpleNamespace(
            st_mode=value.st_mode,
            st_dev=value.st_dev,
            st_ino=value.st_ino,
            st_size=value.st_size + (1 if calls > 1 else 0),
            st_mtime_ns=value.st_mtime_ns,
        )

    monkeypatch.setattr(init_request.os, "fstat", changing_fstat)
    with pytest.raises(InitRequestError, match="changed while it was being read"):
        load_init_request(request_file)


@pytest.mark.parametrize(
    "reference, message",
    [
        ({"status": "adopted"}, "missing provenance"),
        ({"status": "discarded", "deconstruction_output": []}, "deconstruction_output"),
        ({"status": "discarded", "runtime": []}, "runtime must"),
        (
            {"status": "discarded", "runtime": {"task_name": "wni_caller_reported"}},
            "runtime contains unknown",
        ),
        (
            {
                "status": "discarded",
                "runtime": {
                    "rollout_path": "relative",
                    "sessions_root": "relative",
                    "child_thread_id": "child",
                    "parent_thread_id": "parent",
                    "parent_model": "model",
                    "parent_reasoning_effort": "high",
                    "parent_identity_sha256": "c" * 64,
                    "parent_rollout_path": "relative",
                    "parent_rollout_sha256": "b" * 64,
                    "rollout_sha256": "a" * 64,
                },
            },
            "must be absolute",
        ),
        (
            {
                "status": "discarded",
                "runtime": {
                    "rollout_path": "C:/rollout.jsonl",
                    "sessions_root": "C:/sessions",
                    "child_thread_id": "child",
                    "parent_thread_id": "parent",
                    "parent_model": "model",
                    "parent_reasoning_effort": "high",
                    "parent_identity_sha256": "c" * 64,
                    "parent_rollout_path": "C:/parent-rollout.jsonl",
                    "parent_rollout_sha256": "b" * 64,
                    "rollout_sha256": "bad",
                },
            },
            "rollout_sha256",
        ),
        ({"status": "discarded", "user_confirmation": []}, "user_confirmation must"),
        (
            {"status": "discarded", "user_confirmation": {"schema_version": "bad"}},
            "must contain exactly",
        ),
    ],
)
def test_reference_nested_evidence_contract_fails_closed(reference, message):
    with pytest.raises(InitRequestError, match=message):
        init_request._normalize_reference(reference)
