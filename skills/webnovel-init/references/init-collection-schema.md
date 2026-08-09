# Initialization request contract

The temporary request uses schema `webnovel-init-request/v1`. It is written as UTF-8 without BOM strictly under `<WEBNOVEL_HOME>/tmp/init/`. The runtime rejects relative paths, symlinks/junctions, oversized files, unknown fields, wrong types, and requests outside that directory.

```json
{
  "schema_version": "webnovel-init-request/v1",
  "workspace_root": "ABSOLUTE_EXISTING_DIRECTORY",
  "project_slug": "one-safe-component",
  "project": {
    "title": "",
    "genre": "",
    "target_words": 0,
    "target_chapters": 0,
    "one_liner": "",
    "core_conflict": "",
    "target_reader": "",
    "platform": ""
  },
  "protagonist": {
    "name": "",
    "desire": "",
    "flaw": "",
    "archetype": "",
    "structure": "单主角"
  },
  "relationship": {
    "heroine_config": "",
    "heroine_names": [],
    "heroine_role": "",
    "co_protagonists": [],
    "co_protagonist_roles": [],
    "antagonist_tiers": {},
    "antagonist_level": "",
    "antagonist_mirror": ""
  },
  "golden_finger": {
    "type": "",
    "name": "",
    "style": "",
    "visibility": "",
    "irreversible_cost": "",
    "growth_rhythm": ""
  },
  "world": {
    "scale": "",
    "factions": "",
    "power_system_type": "",
    "social_class": "",
    "resource_distribution": "",
    "currency_system": "",
    "currency_exchange": "",
    "sect_hierarchy": "",
    "cultivation_chain": "",
    "cultivation_subtiers": ""
  },
  "constraints": {
    "selected_idea": {
      "title": "",
      "one_liner": "",
      "anti_trope": "",
      "hard_constraints": [],
      "protagonist_flaw": "",
      "antagonist_mirror": "",
      "opening_hook": "",
      "origin": "original"
    },
    "core_selling_points": [],
    "creativity_refusal_reason": ""
  }
}
```

At least one target-size field must be positive; the runtime deterministically derives the other at 3,000 words per chapter. `selected_idea.one_liner` and `protagonist_flaw` must exactly match the confirmed project and protagonist values. A normal package requires one anti-trope and at least two hard constraints; explicit refusal is recorded instead of silently inventing them.

An optional `reference_candidate` may have status `proposed`, `discarded`, or `adopted`. `proposed` blocks apply. An adopted candidate uses no trust booleans. It requires all of the following exact, fail-closed evidence:

- `candidate_id`, `source_title`, absolute `source_path`, `source_sha256`, canonical `output_sha256`, `confidence >= 0.85`, transformation notes, do-not-copy warnings, and canon-contamination warnings;
- the complete strict `deconstruction_output` JSON object;
- current `route_sha256` and managed `contract_hash`;
- a deterministic `WEBNOVEL_INIT_REFERENCE_BINDING/v1` marker that occurred in the trusted child prompt before exactly one final assistant JSON answer;
- `runtime` with explicit child and parent rollout paths/SHA-256 values, the host-owned Codex sessions root, child/parent thread IDs, inherited parent model/effort, and a parent-identity SHA-256;
- `user_confirmation` using `webnovel-init-reference-confirmation/v1`, containing the exact finite `Adopt` / `Discard` / `Cancel` choice request and `WEBNOVEL_INIT_REFERENCE_CHOICE/v1` assistant marker bound to this project root, selected idea, source, output, route, contract, child binding marker, child rollout, and parent identity.

The deterministic confirmation object is a scope binding, not proof of consent. The runtime independently re-hashes the source, output, route, contract, and child rollout; verifies the trusted Codex sessions root without traversing a symlink, junction, or reparse point; reads the parent rollout to prove the parent thread's actual model/effort; requires both that parent and the child rollout's parent ID to equal the canonical nonzero UUID inherited from host-owned `CODEX_THREAD_ID`; finds exactly one scoped assistant marker followed by the user's real `Adopt` answer; and verifies the parent rollout prefix SHA-256 through that answer. It also validates the managed Agent envelope/payload, requires the selected idea to match exactly one transformed candidate, and globally rejects reuse of a child thread or child rollout by another Init scope. Request-provided `quality_passed`, `user_confirmed`, `runtime_evidence_accepted`, or `decision: adopt` fields are invalid. Never put raw reference text in this request.

## Apply authorization contract

The zero-write `webnovel-init-preview/v1` response supplies `apply_choice`, including its exact finite choice request and `WEBNOVEL_INIT_APPLY_CHOICE/v1` marker. `preview_token` only binds current filesystem state, request, Git mode, blockers, and write list. It never proves that the user chose Apply.

After the assistant emits that exact marker and the user answers `Apply` in the same trusted parent rollout, create one UTF-8-without-BOM object strictly under `<WEBNOVEL_HOME>/tmp/init/`:

```json
{
  "schema_version": "webnovel-init-apply-authorization/v1",
  "preview_token": "LOWERCASE_SHA256",
  "choice_request_id": "EXACT_PREVIEW_REQUEST_ID",
  "choice_marker_sha256": "LOWERCASE_SHA256",
  "runtime": {
    "sessions_root": "HOST_OWNED_CODEX_SESSIONS_ROOT",
    "parent_rollout_path": "ABSOLUTE_TRUSTED_ROLLOUT_PATH",
    "parent_thread_id": "CURRENT_PARENT_THREAD_ID",
    "parent_model": "ACTUAL_PARENT_MODEL",
    "parent_reasoning_effort": "ACTUAL_PARENT_REASONING_EFFORT",
    "parent_rollout_sha256": "SHA256_OF_ROLLOUT_PREFIX_THROUGH_USER_ANSWER"
  }
}
```

The runtime bounded-reads and re-parses this artifact under the project lock, validates the host-owned sessions root and parent rollout identity, requires `parent_thread_id` to equal the canonical nonzero UUID inherited from host-owned `CODEX_THREAD_ID`, requires a unique exact marker followed by the first real user answer resolving to `Apply`, and compares the authorization-prefix SHA-256. Unknown fields, caller-authored decision booleans, `Revise`, `Cancel`, free-form answers, cross-project markers, stale receipts, and replay against a different preview all fail closed.

The command must inherit `CODEX_THREAD_ID` unchanged; neither the request nor a command-line assignment may supply or override it. Missing, malformed, or mismatched identity is a pending live gate and causes zero target writes. This host-bound Codex Desktop evidence is not a cryptographic defense against a local process capable of tampering with its environment.
