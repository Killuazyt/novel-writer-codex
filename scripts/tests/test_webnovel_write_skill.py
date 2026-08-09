import json
from pathlib import Path

import validate_codex_adapter
import validate_plugin_package


PLUGIN_ROOT = Path(__file__).resolve().parents[2]
SKILL = PLUGIN_ROOT / "skills" / "webnovel-write" / "SKILL.md"
OPENAI_YAML = SKILL.parent / "agents" / "openai.yaml"


def test_write_skill_metadata_and_prompt_are_discoverable():
    frontmatter = validate_plugin_package._frontmatter(SKILL)
    interface, error = validate_plugin_package._openai_interface(OPENAI_YAML)

    assert set(frontmatter) == {"name", "description"}
    assert frontmatter["name"] == "webnovel-write"
    assert "default、fast、minimal" in frontmatter["description"]
    assert error == ""
    assert 25 <= len(interface["short_description"]) <= 64
    assert "$webnovel-write" in interface["default_prompt"]


def test_write_skill_preserves_transaction_and_model_boundaries():
    text = SKILL.read_text(encoding="utf-8")
    normalized = " ".join(text.split())

    for token in (
        "gpt-5.6-luna",
        "M3 runtime evidence",
        "minimal-no-review",
        "precommit",
        "chapter-commit",
        "postcommit",
        "skipped_non_git",
        "scope-bound 有限选择 receipt",
        "--build-decision-receipt",
        "canonical nonzero UUID",
        "hash-object -w --no-filters",
        "`update-ref` CAS",
        "runtime-invocation.md",
        "prepare-agent --request-file",
        "current-truth audit",
        "禁止 Git init",
        "retry_projection_only",
        "同一次 stable bytes snapshot",
        "source.subagent",
        "owned recovery",
        "manifest 的 inputs 必须与 launch inputs 精确相同",
        "Writer v1 只兼容 `draft` / `polish`",
        "Writer v2 result 与 manifest 必须精确包含同一份 `resolutions`",
        "事务级逐 issue resolution receipt",
        "targeted-fix-request",
        "targeted-fix-decide",
        "recovery-request",
        "recovery-decide",
        "promote --decision-receipt",
        "派生 `stopped`",
        "派生 `cancelled`",
    ):
        assert token in normalized
    assert "[TODO" not in text
    assert "allowed-tools:" not in text
    assert "CLAUDE_" not in text
    assert (SKILL.parent / "references" / "transaction-stages.md").is_file()
    stages = (SKILL.parent / "references" / "transaction-stages.md").read_text(encoding="utf-8")
    normalized_stages = " ".join(stages.split())
    for token in (
        "context 绑定 transaction",
        "draft 绑定 context",
        "final 绑定 draft+review",
        "data 绑定 final+promotion target+review",
        "不接受额外无关 artifact",
        "只验证并续写缺失 receipt",
        "每个可变 public 阶段",
        "stable prefix",
        "项目内临时 index",
        "`update-ref` CAS",
        "Writer v1 只兼容",
        "Writer v2 result 与 manifest 必须携带完全相同的 `resolutions`",
        "targeted-fix-request",
        "resolution receipt 与 resolved review",
        "recovery-request",
        "接受 `replace_with_verified`",
    ):
        assert token in normalized_stages


def test_write_upstream_reference_and_eval_semantics_are_accounted_for():
    reference_root = SKILL.parent / "references"
    writing_root = reference_root / "writing"
    expected_top = {
        "anti-ai-guide.md",
        "polish-guide.md",
        "style-adapter.md",
        "style-variants.md",
        "transaction-stages.md",
    }
    expected_writing = {
        "combat-scenes.md",
        "desire-description.md",
        "dialogue-writing.md",
        "emotion-psychology.md",
        "genre-hook-payoff-library.md",
        "scene-description.md",
        "typesetting.md",
    }

    assert {path.name for path in reference_root.glob("*.md")} == expected_top
    assert {path.name for path in writing_root.glob("*.md")} == expected_writing
    combined = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(reference_root.rglob("*.md"))
    )
    for token in (
        "七层终检",
        "critical/high",
        "anti_ai_force_check",
        "只改表达",
        "移动端排版",
        "组合连续三章",
    ):
        assert token in combined

    evals = json.loads((SKILL.parent / "evals" / "evals.json").read_text(encoding="utf-8"))
    assert evals["schema_version"] == "webnovel-write-evals/v1"
    assert evals["fixture_only"] is True
    assert evals["live_host_gate_satisfied"] is False
    assert {case["id"] for case in evals["cases"]} == {
        "default_verified_chain",
        "fast_explicit_dimensions",
        "minimal_fresh_skip",
        "model_or_evidence_mismatch",
        "postcommit_resume",
        "backup_authorization",
        "blocking_review_receipts",
        "current_parent_binding_required",
        "current_truth_stale",
    }
    blocking = next(case for case in evals["cases"] if case["id"] == "blocking_review_receipts")
    assert blocking["expect"] == [
        "clean review permits polish only",
        "writer v1 targeted_fix is rejected while historical draft and polish remain compatible",
        "writer v2 result and manifest carry identical resolutions",
        "draft and polish resolutions are empty while targeted_fix receipts bind issue occurrence index and sha256",
        "trusted parent choice binds the exact review and blocker occurrences",
        "only exact per-issue resolution coverage derives a zero-blocking commit review while preserving the original review",
    ]


def test_write_skill_surface_is_host_neutral():
    errors = validate_codex_adapter.scan_host_neutrality(PLUGIN_ROOT)
    relevant = [item for item in errors if item["path"].startswith("skills/webnovel-write/")]
    assert relevant == []
