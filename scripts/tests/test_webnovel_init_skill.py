from __future__ import annotations

import json
from pathlib import Path

import validate_codex_adapter
import validate_plugin_package


PLUGIN_ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = PLUGIN_ROOT / "skills" / "webnovel-init"
SKILL = SKILL_ROOT / "SKILL.md"
OPENAI_YAML = SKILL_ROOT / "agents" / "openai.yaml"
EVALS = SKILL_ROOT / "evals" / "evals.json"


def test_init_skill_metadata_and_confirmed_preview_contract():
    frontmatter = validate_plugin_package._frontmatter(SKILL)
    interface, error = validate_plugin_package._openai_interface(OPENAI_YAML)
    text = SKILL.read_text(encoding="utf-8")

    assert set(frontmatter) == {"name", "description"}
    assert frontmatter["name"] == "webnovel-init"
    assert error == ""
    assert "$webnovel-init" in interface["default_prompt"]
    assert "host-bound current parent-rollout receipt" in interface["default_prompt"]
    for required in (
        "webnovel-init-request/v1",
        "webnovel-init-preview/v1",
        "webnovel-init-result/v1",
        "--config-json",
        "--dry-run",
        "--apply",
        "--preview-token",
        "--authorization-json",
        "--git-mode <off|init|initial-commit>",
        "Apply`, `Revise`, or `Cancel",
        "token is only a deterministic state/TOCTOU binding",
        "WEBNOVEL_INIT_APPLY_CHOICE/v1",
        "webnovel-init-apply-authorization/v1",
        "CODEX_THREAD_ID",
        "Never set or override",
        "not a cryptographic defense",
    ):
        assert required in text


def test_init_skill_locks_zero_write_missing_only_and_git_boundaries():
    text = SKILL.read_text(encoding="utf-8")

    for required in (
        "this request is the only permitted write",
        "missing-only",
        "Never offer overwrite",
        "Never run `git add .`",
        "resolved novel directory",
        "Git mode defaults to `off`",
        "stale tokens",
        "stale tokens or receipts",
        "parent repositories",
        "preserves structurally consistent user-authored Markdown byte-for-byte",
        "disables hooks for that commit subprocess",
        "Do not enter planning automatically",
    ):
        assert required in text


def test_init_skill_requires_real_reference_evidence_and_user_adoption():
    text = SKILL.read_text(encoding="utf-8")

    for required in (
        "webnovel_deconstruction_agent",
        "init_reference",
        "Do not fabricate live evidence",
        "confidence >= 0.85",
        "user explicitly chose `Adopt`",
        "WEBNOVEL_INIT_REFERENCE_CHOICE/v1",
        "deterministic `user_confirmation` object scopes the choice but is not proof",
        "An unconfirmed candidate blocks apply",
        "Never put raw reference text",
        "WEBNOVEL_INIT_REFERENCE_BINDING/v1",
        "host-owned `<CODEX_HOME>/sessions`",
        "parent rollout must prove",
        "reused child thread",
        "Both IDs must equal",
    ):
        assert required in text

    schema = (SKILL_ROOT / "references" / "init-collection-schema.md").read_text(encoding="utf-8")
    assert "uses no trust booleans" in schema
    assert "Request-provided `quality_passed`" in schema
    assert "globally rejects reuse" in schema
    assert "reads the parent rollout to prove" in schema
    assert "`decision: adopt` fields are invalid" in schema
    assert "`preview_token` only binds current filesystem state" in schema
    assert "SHA256_OF_ROLLOUT_PREFIX_THROUGH_USER_ANSWER" in schema
    assert "canonical nonzero UUID inherited from host-owned `CODEX_THREAD_ID`" in schema
    assert "pending live gate" in schema


def test_init_evals_freeze_receipt_and_missing_only_contracts():
    payload = json.loads(EVALS.read_text(encoding="utf-8"))

    assert payload["skill_name"] == "webnovel-init"
    assert payload["live_runtime_gate"] == "pending_external_codex_task_evidence"
    joined = "\n".join(
        expectation
        for case in payload["evals"]
        for expectation in case["expectations"]
    )
    for required in (
        "preview token is state binding, not consent",
        "trusted parent rollout",
        "real Apply answer",
        "real Adopt answer",
        "host-owned CODEX_THREAD_ID",
        "preserves authored Markdown bytes",
        "disables Git hooks",
    ):
        assert required in joined


def test_init_private_reference_map_is_complete_and_utf8_without_bom():
    expected = {
        "genre-tropes.md",
        "init-collection-schema.md",
        "system-data-flow.md",
        "creativity/anti-trope-game.md",
        "creativity/anti-trope-rules-mystery.md",
        "creativity/anti-trope-urban.md",
        "creativity/anti-trope-xianxia.md",
        "creativity/creative-combination.md",
        "creativity/creativity-constraints.md",
        "creativity/inspiration-collection.md",
        "creativity/market-positioning.md",
        "creativity/selling-points.md",
        "worldbuilding/character-design.md",
        "worldbuilding/faction-systems.md",
        "worldbuilding/power-systems.md",
        "worldbuilding/setting-consistency.md",
        "worldbuilding/world-rules.md",
    }
    root = SKILL_ROOT / "references"
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*.md")
    }

    assert actual == expected
    for path in [SKILL, OPENAI_YAML, EVALS, *sorted(root.rglob("*.md"))]:
        raw = path.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), path
        raw.decode("utf-8")


def test_init_skill_and_private_references_are_host_neutral():
    errors = validate_codex_adapter.scan_host_neutrality(PLUGIN_ROOT)
    relevant = [item for item in errors if item["path"].startswith("skills/webnovel-init/")]

    assert relevant == []
