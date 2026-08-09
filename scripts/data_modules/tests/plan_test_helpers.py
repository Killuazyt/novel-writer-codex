from __future__ import annotations

import hashlib
import json
from pathlib import Path

from data_modules import plan_transaction
from data_modules.plan_request import build_plan_request, plan_request_sha256, save_plan_request
from data_modules.plan_validator import MANIFEST_SCHEMA_VERSION, compute_plan_content_sha256


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_valid_plan(project_root: Path, *, run_id: str = "plan-v1-test", volume: int = 1):
    staging = project_root / ".webnovel" / "tmp" / "plan-runs" / run_id
    staging.mkdir(parents=True, exist_ok=True)
    chapters = [
        {
            "chapter": 1,
            "goal": "主角确认失踪线索",
            "time_offset_minutes": 0,
            "span_minutes": 60,
            "transition": "开卷",
            "time_mode": "linear",
            "countdowns": {"封门": 120},
            "cbn": {"subject": "主角", "action": "进入", "result": "旧站", "handoff_id": ""},
            "cpns": [
                {"subject": "主角", "action": "发现", "result": "血字", "handoff_id": ""},
                {"subject": "守夜人", "action": "封锁", "result": "出口", "handoff_id": ""},
            ],
            "cen": {"subject": "广播", "action": "播出", "result": "死者声音", "handoff_id": "h-1"},
            "must_cover_nodes": ["发现血字", "听见广播"],
            "forbidden_zones": ["不得揭晓凶手"],
            "chapter_end_open_question": "死者为何仍在广播？",
        },
        {
            "chapter": 2,
            "goal": "追查广播来源",
            "time_offset_minutes": 60,
            "span_minutes": 60,
            "transition": "紧接上章",
            "time_mode": "linear",
            "countdowns": {"封门": 60},
            "cbn": {"subject": "主角", "action": "追踪", "result": "广播源", "handoff_id": "h-1"},
            "cpns": [
                {"subject": "同伴", "action": "破解", "result": "频率", "handoff_id": ""},
                {"subject": "广播", "action": "指向", "result": "地下室", "handoff_id": ""},
            ],
            "cen": {"subject": "铁门", "action": "打开", "result": "空棺", "handoff_id": "h-2"},
            "must_cover_nodes": ["破解频率", "打开铁门"],
            "forbidden_zones": ["不得新增超能力"],
            "chapter_end_open_question": "棺中人去了哪里？",
        },
    ]
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "run_id": run_id,
        "executor": "parent",
        "parent_model": "gpt-5.6-sol",
        "invoked_agents": [],
        "volume": volume,
        "chapter_range": [1, 2],
        "blockers": [],
        "beat": {
            "crises": [
                {"conflict": "车站封闭", "cost": "失去退路", "result": "被迫深入"},
                {"conflict": "广播诱导", "cost": "同伴分离", "result": "锁定频率"},
                {"conflict": "铁门倒计时", "cost": "证据将毁", "result": "打开空棺"},
            ],
            "midpoint": {"event": "广播来自死者", "reason_if_none": ""},
            "final_open_question": "棺中人去了哪里？",
        },
        "chapters": chapters,
        "artifacts": {},
    }
    content_sha = compute_plan_content_sha256(manifest)
    manifest["content_sha256"] = content_sha
    marker = f"<!-- webnovel-plan-content-sha256: {content_sha} -->\n"
    beat_tokens = []
    for crisis in manifest["beat"]["crises"]:
        beat_tokens.extend(crisis.values())
    beat_tokens.extend([manifest["beat"]["midpoint"]["event"], manifest["beat"]["final_open_question"]])
    beat = staging / f"第{volume}卷-节拍表.md"
    beat.write_text(marker + "\n".join(beat_tokens), encoding="utf-8")
    timeline = staging / f"第{volume}卷-时间线.md"
    timeline.write_text(
        marker
        + "\n".join(
            f"第{item['chapter']}章 T+{item['time_offset_minutes']}m "
            + " ".join(f"CD:{event}={remaining}m" for event, remaining in item["countdowns"].items())
            for item in chapters
        ),
        encoding="utf-8",
    )
    outline_lines = [marker]
    for item in chapters:
        outline_lines.extend([f"### 第{item['chapter']}章", item["goal"]])
        for node in [item["cbn"], *item["cpns"], item["cen"]]:
            outline_lines.append(" | ".join(node[key] for key in ("subject", "action", "result")))
        outline_lines.extend(item["must_cover_nodes"])
        outline_lines.extend(item["forbidden_zones"])
        outline_lines.append(item["chapter_end_open_question"])
    outline = staging / f"第{volume}卷-详细大纲.md"
    outline.write_text("\n".join(outline_lines), encoding="utf-8")
    writeback = staging / f"第{volume}卷-总纲写回.json"
    writeback.write_text(
        json.dumps(
            {
                "plan_content_sha256": content_sha,
                "next_volume_anchor": {
                    "volume": volume + 1,
                    "volume_name": "下一卷",
                    "core_conflict": "追查空棺",
                    "volume_end_climax": "找到失踪者",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    targets = {
        "beat": f"大纲/第{volume}卷-节拍表.md",
        "timeline": f"大纲/第{volume}卷-时间线.md",
        "outline": f"大纲/第{volume}卷-详细大纲.md",
        "writeback": f"大纲/第{volume}卷-总纲写回.json",
    }
    sources = {"beat": beat, "timeline": timeline, "outline": outline, "writeback": writeback}
    manifest["artifacts"] = {
        name: {
            "path": path.relative_to(project_root).as_posix(),
            "target": targets[name],
            "sha256": _digest(path),
        }
        for name, path in sources.items()
    }
    manifest_path = staging / "plan-manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest_path, manifest


def make_parent_evidence(monkeypatch, project_root: Path, manifest_path: Path, manifest: dict):
    request = build_plan_request(
        project_root,
        volume=int(manifest["volume"]),
        start_chapter=int(manifest["chapter_range"][0]),
        end_chapter=int(manifest["chapter_range"][1]),
        parent_model=str(manifest["parent_model"]),
        parent_reasoning_effort="high",
        run_id=str(manifest["run_id"]),
    )
    request_path = save_plan_request(request)
    for batch in request["batches"]:
        start = int(batch["start_chapter"])
        end = int(batch["end_chapter"])
        fragment_path = (
            project_root
            / ".webnovel"
            / "tmp"
            / "plan-runs"
            / str(manifest["run_id"])
            / "batches"
            / f"batch-{start:06d}-{end:06d}.json"
        )
        fragment_path.parent.mkdir(parents=True, exist_ok=True)
        fragment_path.write_text(
            json.dumps(
                {
                    "schema_version": plan_transaction.BATCH_FRAGMENT_SCHEMA,
                    "run_id": manifest["run_id"],
                    "volume": manifest["volume"],
                    "start_chapter": start,
                    "end_chapter": end,
                    "chapters": [
                        item
                        for item in manifest["chapters"]
                        if start <= int(item["chapter"]) <= end
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        plan_transaction.accept_plan_batch(project_root, request_path, fragment_path)
    sessions_root = project_root / "host-sessions"
    sessions_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(plan_transaction, "TRUSTED_CODEX_SESSIONS_ROOT", sessions_root.resolve())
    thread_id = "11111111-1111-4111-8111-111111111111"
    monkeypatch.setenv("CODEX_THREAD_ID", thread_id)
    rollout_path = sessions_root / f"rollout-{thread_id}.jsonl"
    marker = plan_transaction.build_parent_evidence_marker(project_root, manifest_path, request_path)
    events = [
        {
            "type": "session_meta",
            "payload": {
                "id": thread_id,
                "model": manifest["parent_model"],
                "source": "codex_desktop",
            },
        },
        {
            "type": "turn_context",
            "payload": {
                "turn_id": "turn-1",
                "model": manifest["parent_model"],
                "effort": "high",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": marker}],
            },
        },
    ]
    rollout_path.write_text(
        "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n",
        encoding="utf-8",
    )
    evidence_path = request_path.with_name("parent-evidence.json")
    evidence_path.write_text(
        json.dumps(
            {
                "schema_version": plan_transaction.PARENT_EVIDENCE_SCHEMA,
                "run_id": manifest["run_id"],
                "request_path": str(request_path),
                "request_sha256": plan_request_sha256(request),
                "rollout_path": str(rollout_path),
                "thread_id": thread_id,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return request_path, evidence_path


def create_bound_validation(monkeypatch, project_root: Path, manifest_path: Path, manifest: dict):
    request_path, evidence_path = make_parent_evidence(
        monkeypatch, project_root, manifest_path, manifest
    )
    return plan_transaction.create_validation_receipt(
        project_root,
        manifest_path,
        request_file=request_path,
        parent_evidence_file=evidence_path,
    )


def append_plan_decision_choice(
    validation: dict,
    decision: dict,
    answer: str,
) -> Path:
    """Append one exact parent marker and the next durable real-user answer."""

    rollout_path = Path(validation["parent_rollout_path"])
    events = [
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": decision["binding_marker"]}
                ],
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
    with rollout_path.open("a", encoding="utf-8", newline="\n") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    return rollout_path


def create_plan_decision_from_choice(
    project_root: Path,
    validation: dict,
    decision: dict,
    answer: str,
) -> dict:
    append_plan_decision_choice(validation, decision, answer)
    return plan_transaction.create_plan_decision_receipt(
        project_root,
        decision["decision_request_file"],
    )


def make_initialized_project(project_root: Path) -> None:
    for relative in (
        ".webnovel/backups",
        ".webnovel/archive",
        ".webnovel/summaries",
        "设定集",
        "大纲",
        "正文",
        "审查报告",
    ):
        (project_root / relative).mkdir(parents=True, exist_ok=True)
    state = {
        "project_info": {"title": "测试小说", "genre": "悬疑"},
        "progress": {"current_chapter": 0, "total_words": 0, "volumes_planned": []},
        "protagonist_state": {"realm": "普通人", "location": "旧站"},
        "relationships": {},
        "world_settings": {"power_system": [], "factions": [], "locations": []},
        "plot_threads": {"active_threads": [], "foreshadowing": []},
        "review_checkpoints": [],
        "disambiguation_pending": [],
        "disambiguation_warnings": [],
    }
    (project_root / ".webnovel" / "state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    for name, body in (
        ("世界观.md", "# 世界观\n旧站封闭。\n"),
        ("力量体系.md", "# 力量体系\n现实规则。\n"),
        ("主角卡.md", "# 主角卡\n调查员。\n"),
        ("反派设计.md", "# 反派设计\n广播谜团。\n"),
    ):
        (project_root / "设定集" / name).write_text(body, encoding="utf-8")
    (project_root / "大纲" / "总纲.md").write_text(
        "# 总纲\n\n## 卷划分\n| 卷号 | 卷名 | 章节范围 | 核心冲突 | 卷末高潮 |\n"
        "|------|------|----------|----------|----------|\n"
        "| 1 | 旧站 | 1-2 | 查明广播 | 打开空棺 |\n",
        encoding="utf-8",
    )
    (project_root / ".env.example").write_text("# optional local settings\n", encoding="utf-8")
    story = project_root / ".story-system"
    story.mkdir(parents=True, exist_ok=True)
    master = {
        "meta": {"contract_type": "MASTER_SETTING"},
        "route": {"primary_genre": "悬疑"},
        "master_constraints": {"core_tone": "紧张", "pacing_strategy": "逐步加压"},
    }
    (story / "MASTER_SETTING.json").write_text(
        json.dumps(master, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (story / "anti_patterns.json").write_text("[]\n", encoding="utf-8")
