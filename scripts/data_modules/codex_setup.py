#!/usr/bin/env python3
"""Provision the project-scoped Codex agents managed by this plugin.

The canonical role contracts live under ``references/agents``.  This module
renders those contracts into standalone Codex agent TOML files and owns only
the paths recorded in ``managed-agents.json``.  Checks never create files or
directories; apply refuses to overwrite an unmanaged or locally modified
agent.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from host_paths import resolve_plugin_root
from runtime_compat import normalize_windows_path


SCHEMA_VERSION = 1
MANAGER_ID = "novel-writer-codex"
MANAGED_RECORD_RELATIVE = Path(".codex/novel-writer-codex/managed-agents.json")
AGENTS_DIRECTORY_RELATIVE = Path(".codex/agents")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class AgentSpec:
    """Stable generator inputs for one project-scoped Codex agent."""

    name: str
    contract_file: str
    description: str
    sandbox_mode: str
    model: str | None = None
    model_reasoning_effort: str | None = None


@dataclass(frozen=True)
class AgentArtifact:
    """One fully rendered managed agent."""

    name: str
    relative_path: Path
    contract_path: Path
    description: str
    sandbox_mode: str
    model: str | None
    model_reasoning_effort: str | None
    contract_text: str
    contract_sha256: str
    content: str
    managed_sha256: str


@dataclass
class _SetupPlan:
    workspace_root: Path
    artifacts: dict[str, AgentArtifact]
    manifest_path: Path
    manifest_before: dict[str, Any] | None
    manifest_before_bytes: bytes | None
    manifest_expected: dict[str, Any]
    manifest_current: bool
    created: list[str]
    updated: list[str]
    unchanged: list[str]
    conflicts: list[dict[str, str]]
    agent_status: dict[str, str]
    write_agents: list[str]
    old_agent_bytes: dict[str, bytes]


class CodexSetupError(RuntimeError):
    """Invalid input or an unsafe filesystem layout."""

    def __init__(self, reason: str, detail: str, *, path: str = "") -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.path = path


class CodexSetupConflict(RuntimeError):
    """A concurrent or unmanaged change prevents a safe apply."""

    def __init__(self, path: str, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.path = path
        self.reason = reason
        self.detail = detail


AGENT_SPECS: dict[str, AgentSpec] = {
    "webnovel_context_agent": AgentSpec(
        name="webnovel_context_agent",
        contract_file="webnovel_context_agent.md",
        description="Assemble a complete read-only webnovel writing brief and report factual blockers.",
        model="gpt-5.6-luna",
        model_reasoning_effort="high",
        sandbox_mode="read-only",
    ),
    "webnovel_writer": AgentSpec(
        name="webnovel_writer",
        contract_file="webnovel_writer.md",
        description="Draft, repair, and polish one chapter only inside its authorized staging directory.",
        model="gpt-5.6-luna",
        model_reasoning_effort="high",
        sandbox_mode="workspace-write",
    ),
    "webnovel_reviewer": AgentSpec(
        name="webnovel_reviewer",
        contract_file="webnovel_reviewer.md",
        description="Review one webnovel chapter and return strict structured findings without editing files.",
        model="gpt-5.6-luna",
        model_reasoning_effort="high",
        sandbox_mode="read-only",
    ),
    "webnovel_data_agent": AgentSpec(
        name="webnovel_data_agent",
        contract_file="webnovel_data_agent.md",
        description="Generate only the three approved post-write artifacts in the authorized staging area.",
        model="gpt-5.6-luna",
        model_reasoning_effort="high",
        sandbox_mode="workspace-write",
    ),
    "webnovel_deconstruction_agent": AgentSpec(
        name="webnovel_deconstruction_agent",
        contract_file="webnovel_deconstruction_agent.md",
        description="Analyze supplied reference text without inventing missing sources or modifying canon.",
        sandbox_mode="read-only",
    ),
}

_AGENT_ALIASES = {
    "context": "webnovel_context_agent",
    "writer": "webnovel_writer",
    "reviewer": "webnovel_reviewer",
    "data": "webnovel_data_agent",
    "deconstruction": "webnovel_deconstruction_agent",
}


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical_agent_name(agent_name: str) -> str:
    canonical = _AGENT_ALIASES.get(agent_name, agent_name)
    if canonical not in AGENT_SPECS:
        raise KeyError(f"Unknown managed Codex agent: {agent_name}")
    return canonical


def _read_contract(path: Path) -> str:
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise CodexSetupError(
            "contract_missing",
            f"Canonical agent contract is missing: {path}",
            path=str(path),
        ) from exc
    except OSError as exc:
        raise CodexSetupError(
            "contract_unreadable",
            f"Cannot read canonical agent contract {path}: {exc}",
            path=str(path),
        ) from exc

    if raw.startswith(b"\xef\xbb\xbf"):
        raise CodexSetupError(
            "contract_bom_forbidden",
            f"Canonical agent contract must be UTF-8 without BOM: {path}",
            path=str(path),
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CodexSetupError(
            "contract_invalid_utf8",
            f"Canonical agent contract is not valid UTF-8: {path}",
            path=str(path),
        ) from exc

    # The repository pins LF.  Normalizing here also makes installed output
    # stable if a third-party archive tool converted the checkout line endings.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        raise CodexSetupError(
            "contract_empty",
            f"Canonical agent contract is empty: {path}",
            path=str(path),
        )
    return text


def _toml_string(value: str) -> str:
    """Return a TOML basic string using JSON's compatible escaping rules."""

    return json.dumps(value, ensure_ascii=False)


def _render_agent(spec: AgentSpec, contract_text: str) -> str:
    lines = [
        "# Generated by novel-writer-codex codex-setup. Do not edit this managed file.",
        f"name = {_toml_string(spec.name)}",
        f"description = {_toml_string(spec.description)}",
    ]
    if spec.model is not None:
        lines.append(f"model = {_toml_string(spec.model)}")
    if spec.model_reasoning_effort is not None:
        lines.append(
            "model_reasoning_effort = "
            + _toml_string(spec.model_reasoning_effort)
        )
    lines.extend(
        [
            f"sandbox_mode = {_toml_string(spec.sandbox_mode)}",
            f"developer_instructions = {_toml_string(contract_text)}",
        ]
    )
    return "\n".join(lines) + "\n"


def _resolve_contract_root(plugin_root: Path) -> Path:
    root = plugin_root.resolve()
    contract_root = (root / "references" / "agents").resolve()
    try:
        contract_root.relative_to(root)
    except ValueError as exc:
        raise CodexSetupError(
            "contract_path_unsafe",
            f"Agent contract directory escapes the plugin root: {contract_root}",
            path=str(contract_root),
        ) from exc
    return contract_root


def build_agent_artifacts(
    plugin_root: str | Path | None = None,
) -> dict[str, AgentArtifact]:
    """Render all five expected agents from their canonical contracts."""

    root = (
        Path(plugin_root).expanduser().resolve()
        if plugin_root is not None
        else resolve_plugin_root(__file__)
    )
    contract_root = _resolve_contract_root(root)
    artifacts: dict[str, AgentArtifact] = {}
    for name, spec in AGENT_SPECS.items():
        contract_path = (contract_root / spec.contract_file).resolve()
        try:
            contract_path.relative_to(contract_root)
        except ValueError as exc:
            raise CodexSetupError(
                "contract_path_unsafe",
                f"Agent contract escapes references/agents: {contract_path}",
                path=str(contract_path),
            ) from exc
        contract_text = _read_contract(contract_path)
        content = _render_agent(spec, contract_text)
        artifacts[name] = AgentArtifact(
            name=name,
            relative_path=AGENTS_DIRECTORY_RELATIVE / f"{name}.toml",
            contract_path=contract_path,
            description=spec.description,
            sandbox_mode=spec.sandbox_mode,
            model=spec.model,
            model_reasoning_effort=spec.model_reasoning_effort,
            contract_text=contract_text,
            contract_sha256=_sha256(contract_text.encode("utf-8")),
            content=content,
            managed_sha256=_sha256(content.encode("utf-8")),
        )
    return artifacts


def agent_spec(
    agent_name: str,
    plugin_root: str | Path | None = None,
) -> dict[str, Any]:
    """Return a plain mapping for callers that validate agent routing."""

    canonical = _canonical_agent_name(agent_name)
    artifact = build_agent_artifacts(plugin_root)[canonical]
    return {
        "name": artifact.name,
        "description": artifact.description,
        "model": artifact.model,
        "model_reasoning_effort": artifact.model_reasoning_effort,
        "sandbox_mode": artifact.sandbox_mode,
        "contract_file": str(artifact.contract_path),
        "contract_hash": artifact.contract_sha256,
        "managed_sha256": artifact.managed_sha256,
        "content": artifact.content,
    }


def _manifest_entry(artifact: AgentArtifact) -> dict[str, Any]:
    return {
        "path": artifact.relative_path.as_posix(),
        "managed_sha256": artifact.managed_sha256,
        "contract_sha256": artifact.contract_sha256,
        "model": artifact.model,
        "model_reasoning_effort": artifact.model_reasoning_effort,
        "sandbox_mode": artifact.sandbox_mode,
    }


def _expected_manifest(
    artifacts: Mapping[str, AgentArtifact],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "manager": MANAGER_ID,
        "agents": {
            name: _manifest_entry(artifacts[name])
            for name in sorted(artifacts)
        },
    }


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    rendered = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    return (rendered + "\n").encode("utf-8")


def _resolve_workspace(workspace_root: str | Path) -> Path:
    if not str(workspace_root).strip():
        raise CodexSetupError(
            "workspace_root_empty",
            "--workspace-root must not be empty.",
        )
    raw = normalize_windows_path(workspace_root).expanduser()
    try:
        resolved = raw.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise CodexSetupError(
            "workspace_root_invalid",
            f"Workspace root does not exist or cannot be resolved: {raw}",
            path=str(raw),
        ) from exc
    if not resolved.is_dir():
        raise CodexSetupError(
            "workspace_root_not_directory",
            f"Workspace root is not a directory: {resolved}",
            path=str(resolved),
        )
    if resolved == Path(resolved.anchor):
        raise CodexSetupError(
            "workspace_root_too_broad",
            f"Refusing to install project agents at a filesystem root: {resolved}",
            path=str(resolved),
        )
    return resolved


def _safe_workspace_path(
    workspace_root: Path,
    relative_path: Path,
    *,
    leaf_may_be_symlink: bool = False,
) -> Path:
    if (
        relative_path.is_absolute()
        or not relative_path.parts
        or ".." in relative_path.parts
    ):
        raise CodexSetupError(
            "managed_path_unsafe",
            f"Managed path must stay relative to the workspace: {relative_path}",
            path=str(relative_path),
        )

    current = workspace_root
    for index, part in enumerate(relative_path.parts):
        current = current / part
        is_leaf = index == len(relative_path.parts) - 1
        if current.is_symlink() and not (is_leaf and leaf_may_be_symlink):
            raise CodexSetupError(
                "managed_path_symlink",
                f"Managed path contains a symbolic link: {current}",
                path=str(current),
            )

    candidate = workspace_root / relative_path
    try:
        candidate.resolve(strict=False).relative_to(workspace_root)
    except (OSError, ValueError) as exc:
        raise CodexSetupError(
            "managed_path_escape",
            f"Managed path escapes the workspace: {candidate}",
            path=str(candidate),
        ) from exc
    return candidate


def _validate_parent_layout(workspace_root: Path) -> None:
    for relative in (
        Path(".codex"),
        AGENTS_DIRECTORY_RELATIVE,
        MANAGED_RECORD_RELATIVE.parent,
        MANAGED_RECORD_RELATIVE.parent / "backups",
    ):
        candidate = _safe_workspace_path(workspace_root, relative)
        if candidate.exists() and not candidate.is_dir():
            raise CodexSetupError(
                "managed_parent_not_directory",
                f"Managed parent path is not a directory: {candidate}",
                path=str(candidate),
            )


def _conflict(path: str, reason: str, detail: str) -> dict[str, str]:
    return {"path": path, "reason": reason, "detail": detail}


def _load_manifest(
    manifest_path: Path,
    artifacts: Mapping[str, AgentArtifact],
) -> tuple[dict[str, Any] | None, bytes | None, dict[str, str] | None]:
    if manifest_path.is_symlink():
        return None, None, _conflict(
            MANAGED_RECORD_RELATIVE.as_posix(),
            "managed_record_unsafe",
            "The managed record must be a regular file inside the workspace.",
        )
    if not manifest_path.exists():
        return None, None, None
    if not manifest_path.is_file():
        return None, None, _conflict(
            MANAGED_RECORD_RELATIVE.as_posix(),
            "managed_record_unsafe",
            "The managed record must be a regular file inside the workspace.",
        )
    try:
        raw = manifest_path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, None, _conflict(
            MANAGED_RECORD_RELATIVE.as_posix(),
            "managed_record_invalid",
            f"The managed record cannot be parsed as UTF-8 JSON: {exc}",
        )
    if not isinstance(payload, dict):
        return None, raw, _conflict(
            MANAGED_RECORD_RELATIVE.as_posix(),
            "managed_record_invalid",
            "The managed record root must be a JSON object.",
        )
    if payload.get("schema_version") != SCHEMA_VERSION:
        return None, raw, _conflict(
            MANAGED_RECORD_RELATIVE.as_posix(),
            "managed_record_schema_unsupported",
            "The managed record schema version is not supported.",
        )
    if payload.get("manager") != MANAGER_ID:
        return None, raw, _conflict(
            MANAGED_RECORD_RELATIVE.as_posix(),
            "managed_record_owner_mismatch",
            "The managed record is not owned by novel-writer-codex.",
        )
    agents = payload.get("agents")
    if not isinstance(agents, dict):
        return None, raw, _conflict(
            MANAGED_RECORD_RELATIVE.as_posix(),
            "managed_record_invalid",
            "The managed record agents field must be an object.",
        )
    unknown = sorted(set(agents) - set(artifacts))
    if unknown:
        return None, raw, _conflict(
            MANAGED_RECORD_RELATIVE.as_posix(),
            "managed_record_unknown_agent",
            "The managed record contains unknown agents: " + ", ".join(unknown),
        )
    return payload, raw, None


def _entry_authorizes_existing_file(
    entry: object,
    artifact: AgentArtifact,
    actual_sha256: str,
) -> tuple[bool, str, str]:
    if not isinstance(entry, dict):
        return False, "managed_entry_missing", "No valid management entry exists for this agent."
    if entry.get("path") != artifact.relative_path.as_posix():
        return False, "managed_entry_path_mismatch", "The recorded managed path does not match this agent."
    recorded_hash = entry.get("managed_sha256")
    if not isinstance(recorded_hash, str) or not _SHA256_RE.fullmatch(recorded_hash):
        return False, "managed_entry_hash_invalid", "The recorded managed SHA-256 is invalid."
    if recorded_hash != actual_sha256:
        return False, "managed_agent_modified", "The agent differs from the last plugin-managed content."
    return True, "", ""


def _build_plan(
    workspace_root: Path,
    artifacts: dict[str, AgentArtifact],
) -> _SetupPlan:
    _validate_parent_layout(workspace_root)
    manifest_path = _safe_workspace_path(
        workspace_root,
        MANAGED_RECORD_RELATIVE,
        leaf_may_be_symlink=True,
    )
    manifest, manifest_bytes, manifest_conflict = _load_manifest(
        manifest_path,
        artifacts,
    )
    expected_manifest = _expected_manifest(artifacts)
    created: list[str] = []
    updated: list[str] = []
    unchanged: list[str] = []
    conflicts: list[dict[str, str]] = []
    agent_status: dict[str, str] = {}
    write_agents: list[str] = []
    old_agent_bytes: dict[str, bytes] = {}

    if manifest_conflict is not None:
        conflicts.append(manifest_conflict)

    manifest_agents = (
        manifest.get("agents", {})
        if isinstance(manifest, dict)
        else {}
    )
    for name, artifact in artifacts.items():
        relative = artifact.relative_path.as_posix()
        target = _safe_workspace_path(
            workspace_root,
            artifact.relative_path,
            leaf_may_be_symlink=True,
        )
        if target.exists() or target.is_symlink():
            if target.is_symlink() or not target.is_file():
                conflicts.append(
                    _conflict(
                        relative,
                        "managed_agent_unsafe",
                        "The target agent path is not a regular file.",
                    )
                )
                agent_status[name] = "conflict"
                continue
            try:
                actual_bytes = target.read_bytes()
            except OSError as exc:
                conflicts.append(
                    _conflict(
                        relative,
                        "managed_agent_unreadable",
                        f"Cannot read the target agent: {exc}",
                    )
                )
                agent_status[name] = "conflict"
                continue
            old_agent_bytes[name] = actual_bytes
            actual_sha256 = _sha256(actual_bytes)
            if manifest_conflict is not None:
                agent_status[name] = "conflict"
                continue
            if manifest is None:
                conflicts.append(
                    _conflict(
                        relative,
                        "unmanaged_existing_agent",
                        "A same-named agent exists without this plugin's managed record.",
                    )
                )
                agent_status[name] = "unmanaged"
                continue
            authorized, reason, detail = _entry_authorizes_existing_file(
                manifest_agents.get(name),
                artifact,
                actual_sha256,
            )
            if not authorized:
                conflicts.append(_conflict(relative, reason, detail))
                agent_status[name] = "modified"
                continue
            if actual_sha256 == artifact.managed_sha256:
                if manifest_agents.get(name) == _manifest_entry(artifact):
                    unchanged.append(relative)
                    agent_status[name] = "current"
                else:
                    updated.append(relative)
                    agent_status[name] = "metadata_stale"
            else:
                updated.append(relative)
                write_agents.append(name)
                agent_status[name] = "stale"
        else:
            created.append(relative)
            write_agents.append(name)
            agent_status[name] = "missing"

    manifest_current = manifest == expected_manifest
    return _SetupPlan(
        workspace_root=workspace_root,
        artifacts=artifacts,
        manifest_path=manifest_path,
        manifest_before=manifest,
        manifest_before_bytes=manifest_bytes,
        manifest_expected=expected_manifest,
        manifest_current=manifest_current,
        created=created,
        updated=updated,
        unchanged=unchanged,
        conflicts=conflicts,
        agent_status=agent_status,
        write_agents=write_agents,
        old_agent_bytes=old_agent_bytes,
    )


def _replace_with_retry(temp_path: Path, target: Path) -> None:
    delay = 0.02
    for attempt in range(10):
        try:
            os.replace(temp_path, target)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.5)


def _atomic_write_bytes(target: Path, raw: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=target.stem + "_",
        suffix=".tmp",
        dir=target.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_with_retry(temporary, target)
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def _backup_directory(
    workspace_root: Path,
    now: datetime | None,
) -> Path:
    timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    label = timestamp.strftime("%Y%m%dT%H%M%S%fZ")
    root_relative = MANAGED_RECORD_RELATIVE.parent / "backups"
    for suffix in range(1000):
        name = label if suffix == 0 else f"{label}-{suffix:03d}"
        relative = root_relative / name
        candidate = _safe_workspace_path(workspace_root, relative)
        if not candidate.exists():
            candidate.mkdir(parents=True, exist_ok=False)
            return candidate
    raise CodexSetupError(
        "backup_name_exhausted",
        "Unable to allocate a unique managed-agent backup directory.",
        path=str(workspace_root / root_relative),
    )


def _verify_plan_unchanged(plan: _SetupPlan) -> None:
    current_manifest = (
        plan.manifest_path.read_bytes()
        if plan.manifest_path.is_file() and not plan.manifest_path.is_symlink()
        else None
    )
    if current_manifest != plan.manifest_before_bytes:
        raise CodexSetupConflict(
            MANAGED_RECORD_RELATIVE.as_posix(),
            "concurrent_change",
            "The managed record changed after inspection; retry setup.",
        )

    for name, artifact in plan.artifacts.items():
        target = plan.workspace_root / artifact.relative_path
        expected_old = plan.old_agent_bytes.get(name)
        if expected_old is None:
            if target.exists() or target.is_symlink():
                raise CodexSetupConflict(
                    artifact.relative_path.as_posix(),
                    "concurrent_change",
                    "The agent appeared after inspection; retry setup.",
                )
            continue
        if target.is_symlink() or not target.is_file():
            raise CodexSetupConflict(
                artifact.relative_path.as_posix(),
                "concurrent_change",
                "The agent path changed after inspection; retry setup.",
            )
        try:
            current = target.read_bytes()
        except OSError as exc:
            raise CodexSetupConflict(
                artifact.relative_path.as_posix(),
                "concurrent_change",
                f"The agent cannot be re-read before apply: {exc}",
            ) from exc
        if current != expected_old:
            raise CodexSetupConflict(
                artifact.relative_path.as_posix(),
                "concurrent_change",
                "The agent changed after inspection; retry setup.",
            )


def _rollback_apply(plan: _SetupPlan) -> list[str]:
    failures: list[str] = []
    for name in plan.write_agents:
        artifact = plan.artifacts[name]
        target = plan.workspace_root / artifact.relative_path
        old = plan.old_agent_bytes.get(name)
        try:
            if old is not None:
                _atomic_write_bytes(target, old)
            elif target.is_file() and _sha256(target.read_bytes()) == artifact.managed_sha256:
                target.unlink()
        except OSError as exc:
            failures.append(f"{target}: {exc}")

    try:
        if plan.manifest_before_bytes is not None:
            _atomic_write_bytes(plan.manifest_path, plan.manifest_before_bytes)
        elif plan.manifest_path.is_file():
            plan.manifest_path.unlink()
    except OSError as exc:
        failures.append(f"{plan.manifest_path}: {exc}")
    return failures


def _apply_plan(
    plan: _SetupPlan,
    *,
    now: datetime | None = None,
) -> Path | None:
    _verify_plan_unchanged(plan)
    backup_dir: Path | None = None
    stale_existing = [
        name
        for name in plan.write_agents
        if name in plan.old_agent_bytes
    ]
    if stale_existing:
        backup_dir = _backup_directory(plan.workspace_root, now)
        for name in stale_existing:
            artifact = plan.artifacts[name]
            source = plan.workspace_root / artifact.relative_path
            shutil.copy2(source, backup_dir / artifact.relative_path.name)
        if plan.manifest_before_bytes is not None:
            _atomic_write_bytes(
                backup_dir / MANAGED_RECORD_RELATIVE.name,
                plan.manifest_before_bytes,
            )

    try:
        for name in plan.write_agents:
            artifact = plan.artifacts[name]
            target = plan.workspace_root / artifact.relative_path
            _atomic_write_bytes(target, artifact.content.encode("utf-8"))
        if not plan.manifest_current:
            _atomic_write_bytes(
                plan.manifest_path,
                _json_bytes(plan.manifest_expected),
            )
    except Exception as exc:
        rollback_failures = _rollback_apply(plan)
        suffix = ""
        if rollback_failures:
            suffix = " Rollback also failed: " + "; ".join(rollback_failures)
        raise CodexSetupError(
            "apply_failed",
            f"Failed to apply managed Codex agents: {exc}.{suffix}",
            path=str(plan.workspace_root),
        ) from exc
    return backup_dir


def _base_result(workspace_root: str | Path) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "failed",
        "workspace_root": str(workspace_root),
        "created": [],
        "updated": [],
        "unchanged": [],
        "conflicts": [],
        "backup_dir": None,
        "restart_required": False,
    }


def run_codex_setup(
    workspace_root: str | Path,
    *,
    apply: bool = False,
    plugin_root: str | Path | None = None,
    now: datetime | None = None,
) -> tuple[int, dict[str, Any]]:
    """Inspect or provision project agents and return ``(exit_code, result)``."""

    result = _base_result(workspace_root)
    try:
        workspace = _resolve_workspace(workspace_root)
        result["workspace_root"] = str(workspace)
        artifacts = build_agent_artifacts(plugin_root)
        plan = _build_plan(workspace, artifacts)
        result.update(
            {
                "created": plan.created,
                "updated": plan.updated,
                "unchanged": plan.unchanged,
                "conflicts": plan.conflicts,
            }
        )
        if plan.conflicts:
            result["status"] = "conflict"
            return 1, result
        changes_required = bool(
            plan.created or plan.updated or not plan.manifest_current
        )
        if not apply:
            result["status"] = (
                "changes_required" if changes_required else "current"
            )
            return (1 if changes_required else 0), result

        backup_dir = _apply_plan(plan, now=now)
        result["status"] = "applied"
        result["backup_dir"] = str(backup_dir) if backup_dir else None
        result["restart_required"] = True
        return 0, result
    except CodexSetupConflict as exc:
        result["status"] = "conflict"
        result["conflicts"] = [
            _conflict(exc.path, exc.reason, exc.detail)
        ]
        return 1, result
    except CodexSetupError as exc:
        result["status"] = "failed"
        result["conflicts"] = [
            _conflict(exc.path, exc.reason, exc.detail)
        ]
        return 2, result
    except (OSError, UnicodeError, ValueError) as exc:
        result["status"] = "failed"
        result["conflicts"] = [
            _conflict(
                str(workspace_root),
                "setup_failed",
                f"Codex agent setup failed: {exc}",
            )
        ]
        return 2, result


def inspect_managed_agent(
    workspace_root: str | Path,
    agent_name: str,
    *,
    plugin_root: str | Path | None = None,
) -> dict[str, Any]:
    """Return routing and current-management state for one expected agent."""

    canonical = _canonical_agent_name(agent_name)
    workspace = _resolve_workspace(workspace_root)
    artifacts = build_agent_artifacts(plugin_root)
    artifact = artifacts[canonical]
    plan = _build_plan(workspace, artifacts)
    status = plan.agent_status.get(canonical, "conflict")
    manifest_conflict = any(
        conflict.get("path") == MANAGED_RECORD_RELATIVE.as_posix()
        for conflict in plan.conflicts
    )
    if manifest_conflict and status not in {"unmanaged", "modified"}:
        status = "conflict"
    return {
        "agent_name": canonical,
        "agent_file": str(workspace / artifact.relative_path),
        "requested_model": artifact.model,
        "reasoning_effort": artifact.model_reasoning_effort,
        "sandbox_mode": artifact.sandbox_mode,
        "contract_hash": artifact.contract_sha256,
        "managed_sha256": artifact.managed_sha256,
        "status": status,
        "current": status == "current",
    }


def format_setup_result(result: Mapping[str, Any], output_format: str) -> str:
    """Render the stable setup result as JSON or concise text."""

    if output_format == "json":
        return json.dumps(result, ensure_ascii=False, indent=2)
    if output_format != "text":
        raise ValueError(f"Unsupported setup output format: {output_format}")

    lines = [
        f"codex-setup: {result.get('status', 'failed')}",
        f"workspace_root: {result.get('workspace_root', '')}",
    ]
    for field in ("created", "updated", "unchanged"):
        values = list(result.get(field, []) or [])
        lines.append(f"{field}: {len(values)}")
        lines.extend(f"  - {value}" for value in values)
    conflicts = list(result.get("conflicts", []) or [])
    lines.append(f"conflicts: {len(conflicts)}")
    for conflict in conflicts:
        if isinstance(conflict, Mapping):
            lines.append(
                "  - "
                f"{conflict.get('path', '')}: {conflict.get('reason', 'conflict')}"
                f" - {conflict.get('detail', '')}"
            )
        else:
            lines.append(f"  - {conflict}")
    lines.append(f"backup_dir: {result.get('backup_dir') or '-'}")
    lines.append(
        "restart_required: "
        + ("true" if result.get("restart_required") else "false")
    )
    return "\n".join(lines)


__all__ = [
    "AGENT_SPECS",
    "AgentArtifact",
    "AgentSpec",
    "CodexSetupError",
    "MANAGED_RECORD_RELATIVE",
    "agent_spec",
    "build_agent_artifacts",
    "format_setup_result",
    "inspect_managed_agent",
    "run_codex_setup",
]
