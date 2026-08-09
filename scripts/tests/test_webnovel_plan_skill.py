import json
from pathlib import Path

import validate_codex_adapter
import validate_plugin_package


PLUGIN_ROOT = Path(__file__).resolve().parents[2]
SKILL = PLUGIN_ROOT / "skills" / "webnovel-plan" / "SKILL.md"
OPENAI_YAML = SKILL.parent / "agents" / "openai.yaml"
EVALS = SKILL.parent / "evals" / "evals.json"


def test_plan_skill_metadata_and_prompt_are_discoverable():
    frontmatter = validate_plugin_package._frontmatter(SKILL)
    interface, error = validate_plugin_package._openai_interface(OPENAI_YAML)

    assert set(frontmatter) == {"name", "description"}
    assert frontmatter["name"] == "webnovel-plan"
    assert "规划" in frontmatter["description"]
    assert error == ""
    assert 25 <= len(interface["short_description"]) <= 64
    assert "$webnovel-plan" in interface["default_prompt"]


def test_plan_skill_is_parent_only_and_hash_gated():
    text = SKILL.read_text(encoding="utf-8")

    for token in (
        "只在当前主对话规划",
        "invoked_agents",
        "plan-manifest.json",
        "plan-transaction accept-batch",
        "batch-<start:06d>-<end:06d>.json",
        "accepted receipt",
        "只重做",
        "plan-validate",
        "plan-transaction apply",
        "scope challenge",
        "plan-transaction decision --request-file",
        "--decision-receipt",
        "binding_marker",
        "keep",
        "replace",
        "cancel",
        "零事实写",
        "rollout prefix",
        "裸 `--overwrite-token`",
        "runtime-invocation.md",
        "parent-evidence.json",
        "fail-closed",
        "CEN→CBN",
        "write-gate --stage prewrite",
    ):
        assert token in text
    assert "[TODO" not in text
    assert "allowed-tools:" not in text
    assert "CLAUDE_" not in text
    assert (SKILL.parent / "references" / "manifest-schema.md").is_file()


def test_plan_skill_evals_cover_batch_immutability_and_marker_gate():
    payload = json.loads(EVALS.read_text(encoding="utf-8"))

    assert payload["skill_name"] == "webnovel-plan"
    assert payload["fixture_only"] is True
    cases = {item["id"]: item for item in payload["cases"]}
    assert set(cases) == {
        "accepted_batch_immutable",
        "incomplete_batch_set_blocks_marker",
        "current_truth_and_parent_binding",
        "trusted_authored_conflict_decision",
    }
    combined = json.dumps(payload, ensure_ascii=False)
    for token in (
        "accept-batch",
        "only the unaccepted batch is reworked",
        "fragment bytes",
        "marker",
        "CODEX_THREAD_ID",
        "prewrite gate",
        "same CODEX_THREAD_ID rollout",
        "before and after hashes",
        "bare overwrite tokens",
        "zero novel facts",
    ):
        assert token in combined


def test_plan_upstream_reference_semantics_are_accounted_for():
    reference_root = SKILL.parent / "references" / "outlining"
    expected = {
        "chapter-planning.md",
        "conflict-design.md",
        "genre-volume-pacing.md",
        "outline-structure.md",
        "plot-frameworks.md",
    }

    assert {path.name for path in reference_root.glob("*.md")} == expected
    combined = "\n".join(
        (reference_root / name).read_text(encoding="utf-8") for name in sorted(expected)
    )
    for token in (
        "CBN",
        "2–4",
        "危机是否至少三次且代价递增",
        "默认每批 10 章",
        "总纲写回",
        "菲希特危机链",
    ):
        assert token in combined
    assert "CLAUDE_" not in combined
    assert "allowed-tools:" not in combined


def test_plan_skill_surface_is_host_neutral():
    errors = validate_codex_adapter.scan_host_neutrality(PLUGIN_ROOT)
    relevant = [item for item in errors if item["path"].startswith("skills/webnovel-plan/")]
    assert relevant == []
