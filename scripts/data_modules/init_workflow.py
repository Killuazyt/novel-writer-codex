#!/usr/bin/env python3
"""Preview-token-gated, missing-only initialization workflow.

This module intentionally has no CLI parser. ``data_modules.webnovel`` owns
the stable command surface and can call :func:`preview_init` or
:func:`apply_init` after parsing ``--config-json``.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
from datetime import date
from pathlib import Path
from typing import Any, Mapping
from uuid import UUID

try:
    from filelock import FileLock, Timeout
except ImportError:  # pragma: no cover - surfaced as a structured runtime blocker
    FileLock = None  # type: ignore[assignment]
    Timeout = TimeoutError  # type: ignore[assignment,misc]

from genre_taxonomy import resolve_genre_input
from host_paths import resolve_plugin_root, resolve_webnovel_home
from init_project import (
    _apply_label_replacements,
    _ensure_state_schema,
    _inject_volume_rows,
    _needs_heroine_card,
    _needs_protagonist_group,
    _render_team_rows,
)
from security_utils import atomic_write_json, sanitize_commit_message

from .codex_agent_runtime import (
    ENVELOPE_SCHEMA_VERSION,
    AgentRuntimeError,
    build_workflow_route,
    validate_agent_envelope,
    validate_agent_payload,
    validate_route_readiness,
)
from .codex_interaction import ChoiceProtocolError, build_choice_request, resolve_choice
from .codex_m3_smoke import (
    SmokeEvidenceError,
    parse_parent_rollout_identity,
    parse_rollout_runtime_evidence,
)
from .init_request import (
    INIT_REQUEST_SCHEMA,
    build_reference_adoption_confirmation,
    load_init_request,
)
from .project_phase import PHASE_PLAN_IN_PROGRESS, resolve_project_phase
from .story_contracts import render_anti_patterns_markdown, render_master_markdown
from .story_system_engine import StorySystemEngine, StorySystemRoutingError


INIT_PREVIEW_SCHEMA = "webnovel-init-preview/v1"
INIT_RESULT_SCHEMA = "webnovel-init-result/v1"
INIT_APPLY_AUTH_SCHEMA = "webnovel-init-apply-authorization/v1"
INIT_APPLY_CHOICE_SCHEMA = "WEBNOVEL_INIT_APPLY_CHOICE/v1"
GIT_MODES = {"off", "init", "initial-commit"}
MAX_REFERENCE_SOURCE_BYTES = 64 * 1024 * 1024
MAX_REFERENCE_ROLLOUT_BYTES = 32 * 1024 * 1024
MAX_EXISTING_INIT_BYTES = 16 * 1024 * 1024
TRUSTED_CODEX_SESSIONS_ROOT = Path(
    os.path.abspath(str(Path.home() / ".codex" / "sessions"))
)
CURRENT_CODEX_THREAD_ENV = "CODEX_THREAD_ID"
REFERENCE_CLAIMS_SCHEMA = "webnovel-init-reference-claims/v1"

_CORE_JSON_PATHS = {
    ".webnovel/state.json",
    ".webnovel/idea_bank.json",
    ".story-system/MASTER_SETTING.json",
    ".story-system/anti_patterns.json",
}
_INIT_MARKDOWN_RULES: dict[str, dict[str, tuple[str, ...]]] = {
    ".story-system/MASTER_SETTING.md": {
        "anchors": ("# MASTER_SETTING",),
        "identity_prefixes": ("- 题材：", "- 调性：", "- 节奏："),
    },
    ".story-system/anti_patterns.md": {
        "anchors": ("# ANTI_PATTERNS",),
    },
    "设定集/世界观.md": {
        "anchors": (
            "# 世界观设定",
            "## 世界一句话",
            "## 世界结构",
            "## 核心规则",
            "## 世界运转机制",
        ),
        "identity_prefixes": (
            "- 核心势力：",
            "- 社会阶层：",
            "- 资源分配规则：",
            "- 宗门/组织层级：",
            "- 硬约束（不可违背）：",
            "- 货币体系：",
            "- 兑换规则：",
        ),
        "body_after": ("## 世界一句话",),
    },
    "设定集/力量体系.md": {
        "anchors": (
            "# 力量体系设定",
            "## 体系公理",
            "## 体系类型",
            "## 晋级条件",
            "## 与创意约束对齐",
        ),
        "identity_prefixes": (
            "- 体系类型：",
            "- 典型境界链（可选）：",
            "- 小境界划分：",
            "- 反套路规则如何体现：",
            "- 硬约束如何绑定体系：",
        ),
    },
    "设定集/主角卡.md": {
        "anchors": (
            "# 主角卡",
            "## 基本信息",
            "## 动机与目标",
            "## 缺陷与代价",
            "## OOC 警戒",
        ),
        "identity_prefixes": (
            "- 姓名：",
            "- 真正渴望（可能不自知）：",
            "- 性格缺陷：",
            "- 反派道路：",
            "- 类型：",
            "- 代价/限制：",
            "- 核心卖点：",
        ),
    },
    "设定集/反派设计.md": {
        "anchors": ("# 反派设计", "## 反派分层（必须）", "## 镜像对抗"),
        "identity_prefixes": (
            "- 与主角共享欲望/缺陷：",
            "- 反派道路：",
            "- 实力层级：",
        ),
    },
    "设定集/女主卡.md": {
        "anchors": ("# 女主卡", "## 基本信息", "## 动机与目标", "## 缺陷与代价"),
        "identity_prefixes": (
            "- 姓名：",
            "- 与主角关系定位（对手/盟友/共谋/牵制）：",
        ),
    },
    "设定集/主角组.md": {
        "anchors": (
            "# 主角组设定（多主角）",
            "## 主角列表",
            "## 共同目标",
            "## 角色分工",
            "## 内部冲突",
        ),
    },
    "大纲/总纲.md": {
        "anchors": (
            "# 总纲",
            "## 故事一句话",
            "## 创意约束",
            "## 核心主线",
            "## 卷划分",
            "## 伏笔表",
        ),
        "identity_prefixes": (
            "- 反套路规则：",
            "- 硬约束（世界/能力/行为）：",
            "- 主角缺陷：",
            "- 反派镜像：",
            "- 主线目标：",
            "- 主要阻力：",
            "- 世界观要点：",
            "- 力量体系要点：",
        ),
        "body_after": ("## 故事一句话",),
    },
}
_BASE_DIRECTORIES = (
    ".webnovel",
    ".webnovel/backups",
    ".webnovel/archive",
    ".webnovel/summaries",
    ".story-system",
    "设定集",
    "大纲",
    "正文",
    "审查报告",
)
_GITIGNORE = """# Python
__pycache__/
*.py[cod]
*.so

# Env (keep .env.example)
.env
.env.*
!.env.example

# Temporary files
*.tmp
*.bak
.DS_Store

# IDE
.vscode/
.idea/

# Runtime caches and locks
.webnovel/context_cache.json
.webnovel/*.lock
.webnovel/*.bak
"""
_ENV_EXAMPLE = """# Webnovel Writer 配置示例（复制为 .env 后填写）
# 注意：请勿将包含真实 API_KEY 的 .env 提交到版本库。

# Embedding
EMBED_BASE_URL=https://api-inference.modelscope.cn/v1
EMBED_MODEL=Qwen/Qwen3-Embedding-8B
EMBED_API_KEY=

# Rerank
RERANK_BASE_URL=https://api.jina.ai/v1
RERANK_MODEL=jina-reranker-v3
RERANK_API_KEY=
"""


class InitWorkflowError(RuntimeError):
    """Raised when preview/apply cannot safely proceed."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "init_blocked",
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _is_linklike(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if is_junction and is_junction():
            return True
        attributes = getattr(os.lstat(path), "st_file_attributes", 0)
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    except FileNotFoundError:
        return False
    except OSError:
        return True


def _lexists(path: Path) -> bool:
    return os.path.lexists(str(path))


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_sha256(payload: object) -> str:
    return _sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left)) == os.path.normcase(str(right))


def _trusted_codex_sessions_root() -> Path:
    lexical = Path(os.path.abspath(str(TRUSTED_CODEX_SESSIONS_ROOT)))
    current = lexical
    while True:
        if _is_linklike(current):
            raise InitWorkflowError("trusted Codex sessions root traverses a reparse point")
        if current.parent == current:
            break
        current = current.parent
    try:
        root = lexical.resolve(strict=True)
    except OSError as exc:
        raise InitWorkflowError(f"trusted Codex sessions root is unavailable: {exc}") from exc
    if not root.is_dir() or not _same_path(lexical, root):
        raise InitWorkflowError("trusted Codex sessions root must be a real direct directory")
    return root


def _current_codex_thread_id() -> str:
    """Return the host-injected current task UUID or fail closed."""

    value = os.environ.get(CURRENT_CODEX_THREAD_ENV)
    if not isinstance(value, str) or not value or value.strip() != value:
        raise InitWorkflowError(
            f"{CURRENT_CODEX_THREAD_ENV} is missing or is not a canonical UUID"
        )
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as exc:
        raise InitWorkflowError(
            f"{CURRENT_CODEX_THREAD_ENV} is missing or is not a canonical UUID"
        ) from exc
    if parsed.int == 0 or str(parsed) != value:
        raise InitWorkflowError(
            f"{CURRENT_CODEX_THREAD_ENV} is missing or is not a canonical UUID"
        )
    return value


def _stable_regular_bytes(
    path: Path,
    *,
    max_bytes: int,
    label: str,
    allow_empty: bool = False,
) -> tuple[bytes, Path]:
    """Open one explicit regular file once and reject path/content races."""

    if not path.is_absolute() or _is_linklike(path):
        raise InitWorkflowError(f"{label} must be an absolute regular non-symlink file")
    lexical = Path(os.path.abspath(str(path)))
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise InitWorkflowError(f"{label} is unavailable: {exc}") from exc
    if not _same_path(lexical, resolved):
        raise InitWorkflowError(f"{label} must not traverse a symlink, junction, or '..'")
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
        try:
            before = os.fstat(fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or (before.st_size <= 0 and not allow_empty)
                or before.st_size > max_bytes
            ):
                raise InitWorkflowError(
                    f"{label} must be a bounded regular file"
                )
            chunks: list[bytes] = []
            remaining = max_bytes + 1
            while remaining > 0:
                chunk = os.read(fd, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            after = os.fstat(fd)
        finally:
            os.close(fd)
        current = os.stat(path, follow_symlinks=False)
    except InitWorkflowError:
        raise
    except OSError as exc:
        raise InitWorkflowError(f"{label} cannot be read stably: {exc}") from exc
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    if (
        len(raw) > max_bytes
        or any(getattr(before, field, None) != getattr(after, field, None) for field in fields)
        or any(getattr(after, field, None) != getattr(current, field, None) for field in fields)
        or _is_linklike(path)
    ):
        raise InitWorkflowError(f"{label} changed while it was being read")
    return raw, resolved


def _rollout_final_json(raw: bytes, *, binding_marker: str) -> dict[str, Any]:
    """Extract exactly one final assistant JSON object from a Codex rollout."""

    if raw.startswith(b"\xef\xbb\xbf"):
        raise InitWorkflowError("reference rollout must be UTF-8 without BOM")
    try:
        events = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InitWorkflowError("reference rollout is not UTF-8 JSONL") from exc
    finals: list[tuple[int, str]] = []
    binding_indexes: list[int] = []
    for event_index, event in enumerate(events):
        if not isinstance(event, dict) or event.get("type") != "response_item":
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        if payload.get("type") != "message":
            continue
        content = payload.get("content")
        texts: list[str] = []
        if isinstance(content, str):
            texts = [content]
        elif isinstance(content, list):
            for item in content:
                if (
                    isinstance(item, dict)
                    and item.get("type") in {"input_text", "output_text", "text"}
                    and isinstance(item.get("text"), str)
                ):
                    texts.append(item["text"])
        text = "".join(texts)
        if payload.get("role") == "user" and binding_marker in text:
            binding_indexes.append(event_index)
            continue
        if payload.get("role") != "assistant" or payload.get("phase") != "final_answer":
            continue
        if (
            not isinstance(content, list)
            or len(content) != 1
            or not isinstance(content[0], dict)
            or content[0].get("type") != "output_text"
            or len(texts) != 1
            or not text
        ):
            raise InitWorkflowError("reference rollout final answer must contain one output_text")
        finals.append((event_index, text))
    if len(binding_indexes) != 1:
        raise InitWorkflowError("reference rollout must contain exactly one bound invocation marker")
    if len(finals) != 1 or binding_indexes[0] >= finals[0][0]:
        raise InitWorkflowError("reference rollout must contain exactly one final assistant answer")
    try:
        output = json.loads(finals[0][1])
    except json.JSONDecodeError as exc:
        raise InitWorkflowError("reference rollout final answer must be one strict JSON object") from exc
    if not isinstance(output, dict):
        raise InitWorkflowError("reference rollout final answer must be one strict JSON object")
    return output


def _rollout_message_text(payload: Mapping[str, Any]) -> str | None:
    if payload.get("type") != "message":
        return None
    content = payload.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    texts = [
        str(item.get("text"))
        for item in content
        if isinstance(item, Mapping)
        and item.get("type") in {"input_text", "output_text", "text"}
        and isinstance(item.get("text"), str)
    ]
    return "".join(texts) if texts else None


def _resolve_parent_choice(
    raw: bytes,
    *,
    marker: str,
    choice_request: Mapping[str, Any],
    question_id: str,
    accepted_option: str,
    label: str,
) -> dict[str, Any]:
    """Verify one scoped assistant choice marker and the next real user answer."""

    if not marker or not isinstance(choice_request, Mapping):
        raise InitWorkflowError(f"{label} lacks a finite-choice request and marker")
    records: list[tuple[int, int, Mapping[str, Any]]] = []
    offset = 0
    try:
        for line in raw.splitlines(keepends=True):
            start = offset
            offset += len(line)
            if not line.strip():
                continue
            event = json.loads(line.decode("utf-8"))
            if isinstance(event, Mapping):
                records.append((start, offset, event))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InitWorkflowError(f"{label} parent rollout is not UTF-8 JSONL") from exc

    marker_records: list[int] = []
    for index, (_, _, event) in enumerate(records):
        if event.get("type") != "response_item":
            continue
        payload = event.get("payload")
        if not isinstance(payload, Mapping) or payload.get("role") != "assistant":
            continue
        text = _rollout_message_text(payload)
        if text is not None and marker in text.splitlines():
            marker_records.append(index)
    if len(marker_records) != 1:
        raise InitWorkflowError(
            f"{label} parent rollout must contain exactly one scoped assistant choice marker"
        )

    marker_index = marker_records[0]
    answer_text = ""
    answer_end = 0
    for _, end, event in records[marker_index + 1 :]:
        if event.get("type") != "response_item":
            continue
        payload = event.get("payload")
        if not isinstance(payload, Mapping) or payload.get("role") != "user":
            continue
        text = _rollout_message_text(payload)
        if text is not None and text.strip():
            answer_text = text.strip()
            answer_end = end
            break
    if not answer_text or answer_end <= 0:
        raise InitWorkflowError(f"{label} has no user answer after the scoped choice marker")
    try:
        resolution = resolve_choice(choice_request, answer_text)
    except ChoiceProtocolError as exc:
        raise InitWorkflowError(f"{label} choice answer is invalid: {exc}") from exc
    if (
        resolution.get("status") != "selected"
        or resolution.get("write_allowed") is not True
        or (resolution.get("selected_branches") or {}).get(question_id) != accepted_option
    ):
        raise InitWorkflowError(f"{label} was not explicitly selected by the user")
    return {
        "answer": answer_text,
        "request_id": resolution.get("request_id"),
        "prefix_sha256": _sha256(raw[:answer_end]),
    }


def _resolve_parent_reference_choice(
    raw: bytes,
    confirmation: Mapping[str, Any],
) -> dict[str, Any]:
    choice_request = confirmation.get("choice_request")
    return _resolve_parent_choice(
        raw,
        marker=str(confirmation.get("choice_marker") or ""),
        choice_request=choice_request if isinstance(choice_request, Mapping) else {},
        question_id="reference_action",
        accepted_option="adopt",
        label="reference adoption",
    )


def _validate_reference_adoption(request: Mapping[str, Any]) -> dict[str, Any] | None:
    """Verify route, live identity, output bytes, source bytes, and user scope."""

    reference = request.get("reference_candidate")
    if not isinstance(reference, Mapping) or reference.get("status") != "adopted":
        return None
    runtime = reference.get("runtime")
    output = reference.get("deconstruction_output")
    confirmation = reference.get("user_confirmation")
    if not isinstance(runtime, Mapping) or not isinstance(output, Mapping) or not isinstance(confirmation, Mapping):
        raise InitWorkflowError("reference adoption lacks explicit rollout, output, or confirmation")
    current_thread_id = _current_codex_thread_id()
    if runtime.get("parent_thread_id") != current_thread_id:
        raise InitWorkflowError(
            "reference parent thread does not match the current Codex task"
        )

    source_raw, source_path = _stable_regular_bytes(
        Path(str(reference.get("source_path") or "")),
        max_bytes=MAX_REFERENCE_SOURCE_BYTES,
        label="reference source",
    )
    if _sha256(source_raw) != reference.get("source_sha256"):
        raise InitWorkflowError("reference source hash does not match source_sha256")

    trusted_sessions_root = _trusted_codex_sessions_root()
    try:
        supplied_sessions_raw = Path(str(runtime["sessions_root"]))
        supplied_sessions = supplied_sessions_raw.resolve(strict=True)
    except (KeyError, OSError) as exc:
        raise InitWorkflowError(f"reference sessions root is invalid: {exc}") from exc
    if (
        not _same_path(supplied_sessions, trusted_sessions_root)
        or not _same_path(Path(os.path.abspath(str(supplied_sessions_raw))), supplied_sessions)
        or _is_linklike(supplied_sessions_raw)
    ):
        raise InitWorkflowError(
            "reference sessions_root does not equal the host-owned Codex sessions root"
        )

    parent_rollout_raw, parent_rollout_path = _stable_regular_bytes(
        Path(str(runtime["parent_rollout_path"])),
        max_bytes=MAX_REFERENCE_ROLLOUT_BYTES,
        label="reference parent rollout",
    )
    if not _inside(parent_rollout_path, trusted_sessions_root):
        raise InitWorkflowError("reference parent rollout escaped the trusted Codex sessions root")
    try:
        parent_evidence = parse_parent_rollout_identity(
            parent_rollout_path,
            expected_thread_id=str(runtime["parent_thread_id"]),
            expected_model=str(runtime["parent_model"]),
            expected_reasoning_effort=str(runtime["parent_reasoning_effort"]),
            sessions_root=trusted_sessions_root,
        )
    except (KeyError, SmokeEvidenceError) as exc:
        raise InitWorkflowError(f"reference parent Codex rollout evidence is invalid: {exc}") from exc
    parent_identity_sha256 = _canonical_sha256(
        {
            "rollout_path": str(parent_rollout_path),
            "thread_id": parent_evidence.thread_id,
            "model": parent_evidence.model,
            "reasoning_effort": parent_evidence.reasoning_effort,
        }
    )
    if parent_identity_sha256 != runtime.get("parent_identity_sha256"):
        raise InitWorkflowError("reference parent rollout identity hash does not match the request")
    parent_rollout_after, parent_rollout_after_path = _stable_regular_bytes(
        parent_rollout_path,
        max_bytes=MAX_REFERENCE_ROLLOUT_BYTES,
        label="reference parent rollout",
    )
    if (
        not _same_path(parent_rollout_after_path, parent_rollout_path)
        or parent_rollout_after != parent_rollout_raw
    ):
        raise InitWorkflowError("reference parent rollout changed after identity verification")

    plugin_root = resolve_plugin_root(__file__).resolve()
    try:
        route = build_workflow_route(
            "init_reference",
            parent_model=parent_evidence.model,
            parent_reasoning_effort=str(parent_evidence.reasoning_effort or ""),
            plugin_root=plugin_root,
        )
        readiness = validate_route_readiness(
            request["workspace_root"],
            route,
            plugin_root=plugin_root,
        )
    except AgentRuntimeError as exc:
        raise InitWorkflowError(f"reference Agent route is invalid: {exc}") from exc
    if not readiness.get("ready"):
        raise InitWorkflowError("managed deconstruction Agent is missing or stale")
    route_sha256 = _canonical_sha256(route)
    if route_sha256 != reference.get("route_sha256"):
        raise InitWorkflowError("reference route hash changed or does not match the request")
    step = route.get("steps", [None])[0]
    if not isinstance(step, Mapping) or step.get("contract_hash") != reference.get("contract_hash"):
        raise InitWorkflowError("reference deconstruction contract hash changed")

    rollout_raw, rollout_path = _stable_regular_bytes(
        Path(str(runtime["rollout_path"])),
        max_bytes=MAX_REFERENCE_ROLLOUT_BYTES,
        label="reference rollout",
    )
    if not _inside(rollout_path, trusted_sessions_root):
        raise InitWorkflowError("reference rollout escaped the trusted Codex sessions root")
    if _same_path(rollout_path, parent_rollout_path):
        raise InitWorkflowError("reference child and parent rollouts must be distinct")

    try:
        evidence = parse_rollout_runtime_evidence(
            rollout_path,
            expected_thread_id=str(runtime["child_thread_id"]),
            expected_parent_thread_id=str(runtime["parent_thread_id"]),
            expected_agent_role=str(step["agent_name"]),
            expected_model=str(step["requested_model"]),
            expected_reasoning_effort=str(step["requested_reasoning_effort"]),
            sessions_root=trusted_sessions_root,
        )
    except (KeyError, SmokeEvidenceError) as exc:
        raise InitWorkflowError(f"reference Codex rollout evidence is invalid: {exc}") from exc
    if evidence.raw_sha256 != runtime.get("rollout_sha256"):
        raise InitWorkflowError("reference rollout hash does not match the confirmed request")
    rollout_after, rollout_after_path = _stable_regular_bytes(
        rollout_path,
        max_bytes=MAX_REFERENCE_ROLLOUT_BYTES,
        label="reference rollout",
    )
    if (
        not _same_path(rollout_after_path, rollout_path)
        or rollout_after != rollout_raw
        or _sha256(rollout_raw) != evidence.raw_sha256
    ):
        raise InitWorkflowError("reference rollout changed after identity verification")
    rollout_output = _rollout_final_json(
        rollout_raw,
        binding_marker=str(reference.get("binding_marker") or ""),
    )
    if _canonical_sha256(rollout_output) != reference.get("output_sha256"):
        raise InitWorkflowError("reference rollout output hash does not match output_sha256")
    if _canonical_sha256(output) != reference.get("output_sha256") or dict(output) != rollout_output:
        raise InitWorkflowError("reference request output is not the rollout final output")

    envelope = {
        "schema_version": ENVELOPE_SCHEMA_VERSION,
        "agent_name": step.get("agent_name"),
        "status": "completed",
        "requested_model": step.get("requested_model"),
        "actual_model": evidence.actual_model,
        "requested_reasoning_effort": step.get("requested_reasoning_effort"),
        "actual_reasoning_effort": evidence.actual_reasoning_effort,
        "parent_model": step.get("parent_model"),
        "parent_reasoning_effort": step.get("parent_reasoning_effort"),
        "contract_hash": step.get("contract_hash"),
        "evidence_source": evidence.evidence_source,
        "fallback_used": False,
        "artifacts": [],
    }
    envelope_result = validate_agent_envelope(step, envelope, verified_evidence=evidence)
    if not envelope_result.get("accepted"):
        raise InitWorkflowError(
            "reference Agent envelope was rejected: " + str(envelope_result.get("code") or "unknown")
        )
    payload_result = validate_agent_payload(
        "webnovel_deconstruction_agent",
        output,
        project_root=request["workspace_root"],
        run_id="init-reference",
        reliable_source_text=True,
    )
    if not payload_result.get("accepted"):
        raise InitWorkflowError(
            "reference deconstruction payload was rejected: " + str(payload_result.get("code") or "unknown")
        )

    source = output.get("source")
    quality = output.get("quality")
    if not isinstance(source, Mapping) or not isinstance(quality, Mapping):
        raise InitWorkflowError("reference deconstruction lacks source or quality provenance")
    try:
        output_source = Path(str(source.get("text_path") or "")).resolve(strict=True)
    except OSError as exc:
        raise InitWorkflowError(f"reference output source path is unavailable: {exc}") from exc
    if (
        not _same_path(output_source, source_path)
        or source.get("title") != reference.get("source_title")
        or source.get("input_type") == "title"
    ):
        raise InitWorkflowError("reference output source provenance does not match the confirmed source")
    if (
        quality.get("passed") is not True
        or float(quality.get("confidence", 0.0)) < 0.85
        or float(quality.get("confidence", 0.0)) != float(reference.get("confidence", 0.0))
    ):
        raise InitWorkflowError("reference quality must pass with matching confidence >= 0.85")

    selected = request["constraints"]["selected_idea"]
    candidate_fields = (
        "one_liner",
        "anti_trope",
        "hard_constraints",
        "protagonist_flaw",
        "antagonist_mirror",
        "opening_hook",
    )
    candidates = [
        item
        for item in output.get("init_candidates", [])
        if isinstance(item, Mapping)
        and all(item.get(field) == selected.get(field) for field in candidate_fields)
        and item.get("transformation_notes") == reference.get("transformation_notes")
    ]
    if len(candidates) != 1:
        raise InitWorkflowError("selected idea is not exactly one transformed deconstruction candidate")
    if list(output.get("do_not_copy") or []) != list(reference.get("do_not_copy") or []):
        raise InitWorkflowError("reference do-not-copy warnings are incomplete")
    if list(output.get("canon_contamination_warnings") or []) != list(
        reference.get("canon_contamination_warnings") or []
    ):
        raise InitWorkflowError("reference canon-contamination warnings are incomplete")

    expected_confirmation = build_reference_adoption_confirmation(
        project_root=str(request["project_root"]),
        selected_idea=dict(selected),
        reference_candidate=dict(reference),
    )
    if dict(confirmation) != expected_confirmation:
        raise InitWorkflowError("reference adoption confirmation is stale or scoped elsewhere")
    choice_proof = _resolve_parent_reference_choice(parent_rollout_raw, confirmation)
    if choice_proof["request_id"] != confirmation.get("request_id"):
        raise InitWorkflowError("reference adoption choice request id is stale")
    if choice_proof["prefix_sha256"] != runtime.get("parent_rollout_sha256"):
        raise InitWorkflowError(
            "reference parent rollout authorization-prefix hash does not match the request"
        )
    return {
        "route_sha256": route_sha256,
        "contract_hash": step.get("contract_hash"),
        "runtime_evidence_sha256": evidence.raw_sha256,
        "child_thread_id": evidence.thread_id,
        "parent_thread_id": evidence.parent_thread_id,
        "parent_rollout_path": str(parent_rollout_path),
        "parent_identity_sha256": parent_identity_sha256,
        "parent_rollout_sha256": choice_proof["prefix_sha256"],
        "output_sha256": reference.get("output_sha256"),
        "source_sha256": reference.get("source_sha256"),
        "confirmation_request_id": confirmation.get("request_id"),
        "rollout_path": str(rollout_path),
    }


def _claim_reference_evidence(
    request: Mapping[str, Any],
    proof: Mapping[str, Any],
) -> None:
    """Globally bind one trusted child thread/rollout to one Init scope."""

    if FileLock is None:
        raise InitWorkflowError("filelock is required to claim reference rollout evidence")
    temp_root = (resolve_webnovel_home() / "tmp" / "init").resolve(strict=True)
    claims_path = temp_root / "reference-evidence-claims.json"
    claims_lock = temp_root / "reference-evidence-claims.lock"
    for path, kind in ((claims_path, "claims registry"), (claims_lock, "claims lock")):
        if _lexists(path) and (_is_linklike(path) or not path.is_file()):
            raise InitWorkflowError(f"reference {kind} has an unsafe path type")
    reference = request.get("reference_candidate") or {}
    runtime = reference.get("runtime") or {}
    claim = {
        "project_root": str(request["project_root"]),
        "confirmation_request_id": proof.get("confirmation_request_id"),
        "evidence_sha256": proof.get("runtime_evidence_sha256"),
        "child_thread_id": proof.get("child_thread_id"),
        "parent_thread_id": proof.get("parent_thread_id"),
        "parent_rollout_path": proof.get("parent_rollout_path"),
        "parent_rollout_sha256": proof.get("parent_rollout_sha256"),
        "parent_identity_sha256": proof.get("parent_identity_sha256"),
        "rollout_path": proof.get("rollout_path"),
        "rollout_sha256": runtime.get("rollout_sha256"),
        "source_sha256": proof.get("source_sha256"),
        "output_sha256": proof.get("output_sha256"),
        "route_sha256": proof.get("route_sha256"),
        "contract_hash": proof.get("contract_hash"),
    }
    try:
        with FileLock(str(claims_lock), timeout=10):
            if _is_linklike(claims_lock) or not claims_lock.is_file():
                raise InitWorkflowError("reference claims lock changed to an unsafe path type")
            if _lexists(claims_path):
                raw, _ = _stable_regular_bytes(
                    claims_path,
                    max_bytes=2 * 1024 * 1024,
                    label="reference claims registry",
                )
                try:
                    registry = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise InitWorkflowError("reference claims registry is corrupt") from exc
            else:
                registry = {"schema_version": REFERENCE_CLAIMS_SCHEMA, "claims": []}
            if (
                not isinstance(registry, dict)
                or set(registry) != {"schema_version", "claims"}
                or registry.get("schema_version") != REFERENCE_CLAIMS_SCHEMA
                or not isinstance(registry.get("claims"), list)
                or any(not isinstance(item, dict) for item in registry["claims"])
            ):
                raise InitWorkflowError("reference claims registry has an invalid schema")
            for existing in registry["claims"]:
                collision = (
                    existing.get("evidence_sha256") == claim["evidence_sha256"]
                    or existing.get("child_thread_id") == claim["child_thread_id"]
                    or existing.get("rollout_path") == claim["rollout_path"]
                )
                if collision and existing != claim:
                    raise InitWorkflowError(
                        "reference rollout or child thread was already claimed by another Init scope",
                        code="reference_runtime_evidence_reused",
                    )
                if existing == claim:
                    return
            registry["claims"].append(claim)
            atomic_write_json(claims_path, registry, use_lock=False, backup=False)
    except Timeout as exc:
        raise InitWorkflowError("timed out claiming reference rollout evidence") from exc


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False).encode("utf-8")


def _text_bytes(text: str) -> bytes:
    return (text.rstrip() + "\n").encode("utf-8")


def _template(plugin_root: Path, name: str) -> str:
    path = plugin_root / "templates" / "output" / name
    if not path.is_file():
        raise InitWorkflowError(f"required init template is missing: {path}")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise InitWorkflowError(f"required init template is unreadable: {path}: {exc}") from exc


def _replace_literal(text: str, marker: str, value: str) -> str:
    return text.replace(marker, value or "（待规划）")


def _tier_rows(text: str, tiers: Mapping[str, str]) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        replaced = False
        for tier, stage in (("小反派", "前期"), ("中反派", "中期"), ("大反派", "后期")):
            if stripped.startswith(f"| {tier}"):
                lines.append(f"| {tier} | {tiers.get(tier, '')} | {stage} | | |")
                replaced = True
                break
        if not replaced:
            lines.append(line)
    return "\n".join(lines)


def _team_card(text: str, names: list[str], roles: list[str]) -> str:
    if not names:
        return text
    rows = _render_team_rows(names, roles)
    output: list[str] = []
    replaced = False
    for line in text.splitlines():
        if line.strip().startswith("| 主角A"):
            output.extend(rows)
            replaced = True
            continue
        if replaced and line.strip().startswith("| 主角"):
            continue
        output.append(line)
    return "\n".join(output)


def _build_state(request: Mapping[str, Any], canonical_genre: str, genre_resolution: Any) -> dict[str, Any]:
    project = request["project"]
    protagonist = request["protagonist"]
    relationship = request["relationship"]
    golden = request["golden_finger"]
    world = request["world"]
    constraints = request["constraints"]
    selected = constraints["selected_idea"]
    stable_day = date.today().isoformat()

    state = _ensure_state_schema({})
    state["project_info"] = {
        "title": project["title"],
        "genre": canonical_genre,
        "genre_label": project["genre"],
        "genre_tags": {
            "route": list(genre_resolution.route_tags),
            "trope": list(genre_resolution.trope_tags),
            "format": list(genre_resolution.format_tags),
            "templates": [Path(item).stem for item in genre_resolution.template_files],
        },
        "created_at": stable_day,
        "target_words": project["target_words"],
        "target_chapters": project["target_chapters"],
        "one_liner": project["one_liner"],
        "core_conflict": project["core_conflict"],
        "target_reader": project["target_reader"],
        "platform": project["platform"],
        "golden_finger_name": golden["name"],
        "golden_finger_type": golden["type"],
        "golden_finger_style": golden["style"],
        "golden_finger_growth_rhythm": golden["growth_rhythm"],
        "core_selling_points": ",".join(constraints["core_selling_points"]),
        "protagonist_structure": protagonist["structure"],
        "heroine_config": relationship["heroine_config"],
        "heroine_names": ",".join(relationship["heroine_names"]),
        "heroine_role": relationship["heroine_role"],
        "co_protagonists": ",".join(relationship["co_protagonists"]),
        "co_protagonist_roles": ",".join(relationship["co_protagonist_roles"]),
        "antagonist_tiers": ";".join(
            f"{key}:{value}" for key, value in relationship["antagonist_tiers"].items()
        ),
        "antagonist_level": relationship["antagonist_level"],
        "antagonist_mirror": relationship["antagonist_mirror"],
        "world_scale": world["scale"],
        "factions": world["factions"],
        "power_system_type": world["power_system_type"],
        "social_class": world["social_class"],
        "resource_distribution": world["resource_distribution"],
        "gf_visibility": golden["visibility"],
        "gf_irreversible_cost": golden["irreversible_cost"],
        "currency_system": world["currency_system"],
        "currency_exchange": world["currency_exchange"],
        "sect_hierarchy": world["sect_hierarchy"],
        "cultivation_chain": world["cultivation_chain"],
        "cultivation_subtiers": world["cultivation_subtiers"],
        "init_constraints": {
            "selected_idea_title": selected["title"],
            "anti_trope": selected["anti_trope"],
            "hard_constraints": list(selected["hard_constraints"]),
            "opening_hook": selected["opening_hook"],
            "origin": selected["origin"],
            "creativity_refusal_reason": constraints["creativity_refusal_reason"],
        },
    }
    state["progress"]["last_updated"] = f"{stable_day} 00:00:00"
    state["protagonist_state"]["name"] = protagonist["name"]
    if golden["type"].casefold() in {"无", "无金手指", "none"}:
        state["protagonist_state"]["golden_finger"] = {
            "name": "无金手指",
            "level": 0,
            "cooldown": 0,
            "skills": [],
        }
    else:
        state["protagonist_state"]["golden_finger"]["name"] = (
            golden["name"] or "未命名金手指"
        )
    return state


def _build_idea_bank(request: Mapping[str, Any]) -> dict[str, Any]:
    constraints = request["constraints"]
    selected = constraints["selected_idea"]
    payload: dict[str, Any] = {
        "schema_version": "webnovel-idea-bank/v1",
        "selected_idea": {
            "title": selected["title"],
            "one_liner": selected["one_liner"],
            "anti_trope": selected["anti_trope"],
            "hard_constraints": list(selected["hard_constraints"]),
            "origin": selected["origin"],
        },
        "constraints_inherited": {
            "anti_trope": selected["anti_trope"],
            "hard_constraints": list(selected["hard_constraints"]),
            "protagonist_flaw": selected["protagonist_flaw"],
            "antagonist_mirror": selected["antagonist_mirror"],
            "opening_hook": selected["opening_hook"],
        },
        "core_selling_points": list(constraints["core_selling_points"]),
        "creativity_refusal_reason": constraints["creativity_refusal_reason"],
    }
    reference = request.get("reference_candidate")
    if reference and reference.get("status") == "adopted":
        runtime = reference.get("runtime") or {}
        confirmation = reference.get("user_confirmation") or {}
        payload["reference_adoption"] = {
            "candidate_id": reference["candidate_id"],
            "source_title": reference["source_title"],
            "source_sha256": reference["source_sha256"],
            "output_sha256": reference["output_sha256"],
            "confidence": reference["confidence"],
            "transformation_notes": reference["transformation_notes"],
            "do_not_copy": list(reference["do_not_copy"]),
            "canon_contamination_warnings": list(reference["canon_contamination_warnings"]),
            "route_sha256": reference["route_sha256"],
            "contract_hash": reference["contract_hash"],
            "runtime_evidence_sha256": runtime.get("rollout_sha256"),
            "child_thread_id": runtime.get("child_thread_id"),
            "parent_thread_id": runtime.get("parent_thread_id"),
            "parent_identity_sha256": runtime.get("parent_identity_sha256"),
            "parent_rollout_sha256": runtime.get("parent_rollout_sha256"),
            "confirmation_request_id": confirmation.get("request_id"),
            "confirmation_scope_sha256": confirmation.get("scope_sha256"),
            "choice_scope_sha256": confirmation.get("choice_scope_sha256"),
        }
    return payload


def _build_story_seed(
    request: Mapping[str, Any],
    *,
    plugin_root: Path,
    canonical_genre: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    project = request["project"]
    selected = request["constraints"]["selected_idea"]
    query = " ".join(
        part
        for part in (project["genre"], project["one_liner"], project["core_conflict"])
        if part
    )
    engine = StorySystemEngine(csv_dir=plugin_root / "references" / "csv")
    try:
        contract = engine.build(query=query, genre=canonical_genre, chapter=None)
    except StorySystemRoutingError as exc:
        raise InitWorkflowError(str(exc)) from exc
    master = dict(contract["master_setting"])
    master["init_constraints"] = {
        "title": project["title"],
        "one_liner": project["one_liner"],
        "core_conflict": project["core_conflict"],
        "anti_trope": selected["anti_trope"],
        "hard_constraints": list(selected["hard_constraints"]),
        "protagonist_flaw": selected["protagonist_flaw"],
        "antagonist_mirror": selected["antagonist_mirror"],
        "opening_hook": selected["opening_hook"],
        "origin": selected["origin"],
    }
    reference = request.get("reference_candidate")
    if reference and reference.get("status") == "adopted":
        runtime = reference.get("runtime") or {}
        confirmation = reference.get("user_confirmation") or {}
        master["init_constraints"]["reference_adoption"] = {
            "candidate_id": reference["candidate_id"],
            "source_sha256": reference["source_sha256"],
            "output_sha256": reference["output_sha256"],
            "confidence": reference["confidence"],
            "transformation_notes": reference["transformation_notes"],
            "route_sha256": reference["route_sha256"],
            "contract_hash": reference["contract_hash"],
            "runtime_evidence_sha256": runtime.get("rollout_sha256"),
            "child_thread_id": runtime.get("child_thread_id"),
            "parent_thread_id": runtime.get("parent_thread_id"),
            "parent_identity_sha256": runtime.get("parent_identity_sha256"),
            "parent_rollout_sha256": runtime.get("parent_rollout_sha256"),
            "confirmation_request_id": confirmation.get("request_id"),
            "confirmation_scope_sha256": confirmation.get("scope_sha256"),
            "choice_scope_sha256": confirmation.get("choice_scope_sha256"),
        }
    anti_patterns = [dict(item) for item in contract.get("anti_patterns") or []]
    anti_trope = selected["anti_trope"]
    if anti_trope and not any(str(item.get("text") or "").strip() == anti_trope for item in anti_patterns):
        anti_patterns.append(
            {
                "text": anti_trope,
                "source": "user_confirmed_init",
                "category": "anti_trope",
            }
        )
    return master, anti_patterns


def _build_markdown_artifacts(
    request: Mapping[str, Any],
    *,
    plugin_root: Path,
    genre_resolution: Any,
) -> dict[str, bytes]:
    project = request["project"]
    protagonist = request["protagonist"]
    relationship = request["relationship"]
    golden = request["golden_finger"]
    world = request["world"]
    constraints = request["constraints"]
    selected = constraints["selected_idea"]
    hard_constraints = "；".join(selected["hard_constraints"])

    worldview = _template(plugin_root, "设定集-世界观.md")
    worldview = _apply_label_replacements(
        worldview,
        {
            "大陆/位面数量": world["scale"],
            "核心势力": world["factions"],
            "社会阶层": world["social_class"],
            "资源分配规则": world["resource_distribution"],
            "宗门/组织层级": world["sect_hierarchy"],
            "货币体系": world["currency_system"],
            "兑换规则": world["currency_exchange"],
            "硬约束（不可违背）": hard_constraints,
        },
    )
    worldview = _replace_literal(worldview, "{一句话概括世界的规则与核心矛盾}", project["core_conflict"])
    genre_blocks: list[str] = []
    for filename in genre_resolution.template_files:
        path = plugin_root / "templates" / "genres" / filename
        if path.is_file():
            genre_blocks.append(path.read_text(encoding="utf-8").strip())
    if genre_blocks:
        worldview = worldview.rstrip() + "\n\n## 题材模板参考\n\n" + "\n\n---\n\n".join(genre_blocks)

    power = _apply_label_replacements(
        _template(plugin_root, "设定集-力量体系.md"),
        {
            "体系类型": world["power_system_type"],
            "典型境界链（可选）": world["cultivation_chain"],
            "小境界划分": world["cultivation_subtiers"],
            "反套路规则如何体现": selected["anti_trope"],
            "硬约束如何绑定体系": hard_constraints,
        },
    )
    protagonist_card = _apply_label_replacements(
        _template(plugin_root, "设定集-主角卡.md"),
        {
            "姓名": protagonist["name"],
            "真正渴望（可能不自知）": protagonist["desire"],
            "性格缺陷": protagonist["flaw"],
            "类型": golden["type"],
            "代价/限制": golden["irreversible_cost"],
            "核心卖点": "；".join(constraints["core_selling_points"]),
            "反派道路": selected["antagonist_mirror"],
        },
    )
    antagonist = _apply_label_replacements(
        _tier_rows(
            _template(plugin_root, "设定集-反派设计.md"),
            relationship["antagonist_tiers"],
        ),
        {
            "实力层级": relationship["antagonist_level"],
            "与主角共享欲望/缺陷": protagonist["flaw"],
            "反派道路": relationship["antagonist_mirror"],
        },
    )
    outline = _inject_volume_rows(
        _template(plugin_root, "大纲-总纲.md"), project["target_chapters"]
    )
    outline = _replace_literal(outline, "{一句话概括主线矛盾与成长方向}", project["one_liner"])
    outline = _apply_label_replacements(
        outline,
        {
            "反套路规则": selected["anti_trope"],
            "硬约束（世界/能力/行为）": hard_constraints,
            "主角缺陷": protagonist["flaw"],
            "反派镜像": relationship["antagonist_mirror"],
            "主线目标": project["one_liner"],
            "主要阻力": project["core_conflict"],
            "小反派（前期）": relationship["antagonist_tiers"].get("小反派", ""),
            "中反派（中期）": relationship["antagonist_tiers"].get("中反派", ""),
            "大反派（后期）": relationship["antagonist_tiers"].get("大反派", ""),
            "世界观要点": world["scale"] + "；" + world["factions"],
            "力量体系要点": world["power_system_type"],
        },
    )

    artifacts = {
        "设定集/世界观.md": _text_bytes(worldview),
        "设定集/力量体系.md": _text_bytes(power),
        "设定集/主角卡.md": _text_bytes(protagonist_card),
        "设定集/反派设计.md": _text_bytes(antagonist),
        "大纲/总纲.md": _text_bytes(outline),
        ".env.example": _text_bytes(_ENV_EXAMPLE),
    }
    if _needs_heroine_card(
        relationship["heroine_config"], ",".join(relationship["heroine_names"])
    ):
        heroine = _apply_label_replacements(
            _template(plugin_root, "设定集-女主卡.md"),
            {
                "姓名": "、".join(relationship["heroine_names"]),
                "与主角关系定位（对手/盟友/共谋/牵制）": relationship["heroine_role"],
            },
        )
        artifacts["设定集/女主卡.md"] = _text_bytes(heroine)
    if _needs_protagonist_group(protagonist["structure"]):
        team = _team_card(
            _template(plugin_root, "设定集-主角组.md"),
            relationship["co_protagonists"],
            relationship["co_protagonist_roles"],
        )
        artifacts["设定集/主角组.md"] = _text_bytes(team)
    return artifacts


def build_desired_artifacts(
    request: Mapping[str, Any], *, git_mode: str
) -> tuple[dict[str, bytes], dict[str, Any]]:
    """Build all deterministic file bytes without touching the target project."""

    plugin_root = resolve_plugin_root(__file__).resolve()
    genre_resolution = resolve_genre_input(request["project"]["genre"])
    canonical_genre = str(genre_resolution.canonical_genre or "").strip()
    if not canonical_genre or canonical_genre == "全部":
        raise InitWorkflowError(
            f"genre cannot be resolved to one canonical Chinese genre: {request['project']['genre']}"
        )
    state = _build_state(request, canonical_genre, genre_resolution)
    idea_bank = _build_idea_bank(request)
    master, anti_patterns = _build_story_seed(
        request, plugin_root=plugin_root, canonical_genre=canonical_genre
    )
    artifacts = _build_markdown_artifacts(
        request, plugin_root=plugin_root, genre_resolution=genre_resolution
    )
    artifacts.update(
        {
            ".webnovel/state.json": _json_bytes(state),
            ".webnovel/idea_bank.json": _json_bytes(idea_bank),
            ".story-system/MASTER_SETTING.json": _json_bytes(master),
            ".story-system/anti_patterns.json": _json_bytes(anti_patterns),
            ".story-system/MASTER_SETTING.md": _text_bytes(render_master_markdown(master)),
            ".story-system/anti_patterns.md": _text_bytes(
                render_anti_patterns_markdown(anti_patterns)
            ),
        }
    )
    if git_mode != "off":
        artifacts[".gitignore"] = _text_bytes(_GITIGNORE)
    return artifacts, {
        "canonical_genre": canonical_genre,
        "title": request["project"]["title"],
        "target_words": request["project"]["target_words"],
        "target_chapters": request["project"]["target_chapters"],
        "selected_idea": idea_bank["selected_idea"],
        "idea_bank": idea_bank,
        "required_anti_trope": request["constraints"]["selected_idea"]["anti_trope"],
        "state_init_constraints": state["project_info"]["init_constraints"],
        "init_constraints": master["init_constraints"],
    }


def _load_existing_json(path: Path) -> Any:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ValueError("UTF-8 BOM is not allowed")
    return json.loads(raw.decode("utf-8"))


def _consistent_core_json(relative: str, path: Path, expected: Mapping[str, Any]) -> tuple[bool, str]:
    try:
        value = _load_existing_json(path)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        return False, f"invalid existing JSON: {exc}"
    if relative == ".webnovel/state.json":
        if not isinstance(value, dict) or not isinstance(value.get("project_info"), dict):
            return False, "existing state.json has no project_info object"
        info = value["project_info"]
        checks = {
            "title": expected["title"],
            "genre": expected["canonical_genre"],
            "target_words": expected["target_words"],
            "target_chapters": expected["target_chapters"],
        }
        for field, expected_value in checks.items():
            if info.get(field) != expected_value:
                return False, f"existing state.json conflicts at project_info.{field}"
        existing_constraints = info.get("init_constraints")
        if (
            existing_constraints is not None
            and existing_constraints != expected["state_init_constraints"]
        ):
            # Older initialized states may not have init_constraints; an explicit
            # conflicting object is never overwritten silently.
            return False, "existing state.json init_constraints conflict"
        return True, "existing state matches the confirmed project identity"
    if relative == ".webnovel/idea_bank.json":
        if not isinstance(value, dict):
            return False, "existing idea_bank.json is not an object"
        for field in (
            "selected_idea",
            "constraints_inherited",
            "core_selling_points",
            "creativity_refusal_reason",
            "reference_adoption",
        ):
            expected_value = expected["idea_bank"].get(field)
            if value.get(field) != expected_value:
                return False, f"existing idea_bank.json conflicts at {field}"
        return True, "existing selected idea matches"
    if relative == ".story-system/MASTER_SETTING.json":
        if not isinstance(value, dict):
            return False, "existing MASTER_SETTING.json is not an object"
        route = value.get("route") or {}
        canonical = route.get("canonical_genre") or route.get("genre_filter")
        if canonical != expected["canonical_genre"]:
            return False, "existing MASTER_SETTING.json genre conflicts"
        if value.get("init_constraints") != expected["init_constraints"]:
            return False, "existing MASTER_SETTING.json init_constraints conflict"
        return True, "existing master contract matches"
    if relative == ".story-system/anti_patterns.json":
        if not isinstance(value, list) or any(
            not isinstance(item, dict) or not isinstance(item.get("text"), str)
            for item in value
        ):
            return False, "existing anti_patterns.json is not an array of text objects"
        required = str(expected.get("required_anti_trope") or "").strip()
        if required and not any(str(item.get("text") or "").strip() == required for item in value):
            return False, "existing anti_patterns.json dropped the confirmed anti-trope"
        return True, "existing anti-pattern list is preserved"
    return False, "unrecognized controlled JSON path"


def _markdown_body_after(lines: list[str], heading: str) -> str:
    """Return the first non-empty body line in one required Markdown section."""

    try:
        index = lines.index(heading)
    except ValueError:
        return ""
    for line in lines[index + 1 :]:
        if line.startswith("#"):
            return ""
        if line:
            return line
    return ""


def _consistent_init_markdown(
    relative: str,
    raw: bytes,
    desired: bytes,
    expected: Mapping[str, Any],
) -> tuple[bool, str]:
    """Accept authored extensions while preserving structural/canonical identity."""

    rules = _INIT_MARKDOWN_RULES.get(relative)
    if rules is None:
        return False, "unrecognized controlled Markdown path"
    if raw.startswith(b"\xef\xbb\xbf"):
        return False, "UTF-8 BOM is not allowed"
    try:
        text = raw.decode("utf-8")
        desired_text = desired.decode("utf-8")
    except UnicodeDecodeError:
        return False, "Markdown must be valid UTF-8"
    if not text.strip() or "\x00" in text:
        return False, "Markdown must be non-empty and contain no NUL"

    lines = [line.strip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    desired_lines = [
        line.strip()
        for line in desired_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    ]
    for anchor in rules.get("anchors", ()):
        if lines.count(anchor) != 1:
            return False, f"required structure anchor is missing or duplicated: {anchor}"
    for prefix in rules.get("identity_prefixes", ()):
        desired_matches = [line for line in desired_lines if line.startswith(prefix)]
        current_matches = [line for line in lines if line.startswith(prefix)]
        if len(desired_matches) != 1 or current_matches != desired_matches:
            return False, f"canonical identity conflicts at {prefix}"
    for heading in rules.get("body_after", ()):
        desired_body = _markdown_body_after(desired_lines, heading)
        current_body = _markdown_body_after(lines, heading)
        if not desired_body or current_body != desired_body:
            return False, f"canonical section body conflicts after {heading}"

    if relative == ".story-system/anti_patterns.md":
        required = str(expected.get("required_anti_trope") or "").strip()
        if required and lines.count(f"- {required}") != 1:
            return False, "confirmed anti-trope is missing or duplicated"
    if relative == "设定集/主角组.md":
        desired_rows = [
            line
            for line in desired_lines
            if line.startswith("| ")
            and not line.startswith("| 名称 ")
            and "---" not in line
        ]
        if any(lines.count(row) != 1 for row in desired_rows):
            return False, "canonical protagonist rows conflict"
    return True, "existing authored Markdown preserves required structure and canonical identity"


def _find_parent_git(workspace: Path) -> Path | None:
    for candidate in (workspace, *workspace.parents):
        marker = candidate / ".git"
        if marker.exists() or marker.is_symlink():
            return candidate
    return None


def _effective_parent_git(workspace: Path) -> Path | None:
    """Ignore only the repository that contains the isolated pytest sandbox."""

    parent = _find_parent_git(workspace)
    if parent is None:
        return None
    session_raw = os.environ.get("WEBNOVEL_TEST_SESSION_ROOT", "")
    if os.environ.get("WEBNOVEL_TEST_ISOLATION") == "1" and session_raw:
        session = Path(session_raw).resolve()
        if _inside(workspace, session) and not _inside(parent, session):
            return None
    return parent


def _run_git(target: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", "-C", str(target), *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        shell=False,
    )
    if check and completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "git command failed").strip()
        raise InitWorkflowError(detail)
    return completed


def _remove_readonly_for_rmtree(function: Any, path: str, _exc_info: object) -> None:
    """Windows Git objects can be read-only; make only the exact failed leaf writable."""

    os.chmod(path, stat.S_IWRITE)
    function(path)


def _git_top_level(target: Path) -> Path | None:
    if not (target / ".git").exists():
        return None
    completed = _run_git(target, "rev-parse", "--show-toplevel")
    if completed.returncode != 0:
        return None
    try:
        return Path(completed.stdout.strip()).resolve(strict=True)
    except OSError:
        return None


def _git_identity_available() -> bool:
    if all(
        os.environ.get(name)
        for name in (
            "GIT_AUTHOR_NAME",
            "GIT_AUTHOR_EMAIL",
            "GIT_COMMITTER_NAME",
            "GIT_COMMITTER_EMAIL",
        )
    ):
        return True
    for key in ("user.name", "user.email"):
        completed = subprocess.run(
            ["git", "config", "--get", key],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            shell=False,
        )
        if completed.returncode != 0 or not completed.stdout.strip():
            return False
    return True


def _git_blockers(target: Path, git_mode: str) -> list[dict[str, str]]:
    blockers: list[dict[str, str]] = []
    if git_mode == "off":
        return blockers
    if shutil.which("git") is None:
        return [{"code": "git_unavailable", "detail": "Git is not available"}]
    git_marker = target / ".git"
    if _lexists(git_marker):
        if _is_linklike(git_marker) or not git_marker.is_dir():
            blockers.append(
                {
                    "code": "unsafe_git_marker",
                    "detail": ".git must be a local regular directory, not a link or worktree file",
                }
            )
            return blockers
        top = _git_top_level(target)
        if top is None or os.path.normcase(str(top)) != os.path.normcase(str(target)):
            blockers.append(
                {
                    "code": "wrong_git_root",
                    "detail": "existing Git metadata does not resolve to the novel root",
                }
            )
            return blockers
        if git_mode == "initial-commit":
            if _run_git(target, "rev-parse", "--verify", "HEAD").returncode == 0:
                blockers.append(
                    {
                        "code": "git_history_exists",
                        "detail": "initial-commit requires a repository without HEAD",
                    }
                )
            if _run_git(target, "diff", "--cached", "--quiet").returncode not in {0}:
                blockers.append(
                    {
                        "code": "git_index_not_clean",
                        "detail": "initial-commit refuses a pre-populated Git index",
                    }
                )
    if git_mode == "initial-commit" and not _git_identity_available():
        blockers.append(
            {
                "code": "git_identity_missing",
                "detail": "Git user.name and user.email are required for initial-commit",
            }
        )
    return blockers


def _operation(path: str, kind: str, status: str, **extra: Any) -> dict[str, Any]:
    return {"path": path, "kind": kind, "status": status, **extra}


def _canonical_token_payload(
    request: Mapping[str, Any],
    git_mode: str,
    operations: list[dict[str, Any]],
    blockers: list[dict[str, str]],
) -> bytes:
    return json.dumps(
        {
            "request": request,
            "git_mode": git_mode,
            "operations": operations,
            "blockers": blockers,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _build_apply_choice(
    *,
    project_root: str,
    git_mode: str,
    preview_token: str,
    write_list: list[str],
) -> dict[str, Any]:
    scope = {
        "project_root": project_root,
        "git_mode": git_mode,
        "preview_token": preview_token,
        "write_list": list(write_list),
    }
    scope_sha256 = _canonical_sha256(scope)
    choice_request = build_choice_request(
        [
            {
                "id": "init_action",
                "prompt": (
                    f"确认对 {Path(project_root).name} 执行 Init 写入 "
                    f"（Git={git_mode}，范围 {scope_sha256[:12]}）？"
                ),
                "options": [
                    {
                        "id": "apply",
                        "label": "Apply",
                        "description": "按已展示 write_list 和 Git 模式执行。",
                        "recommended": True,
                    },
                    {
                        "id": "revise",
                        "label": "Revise",
                        "description": "返回修改初始化输入，不执行写入。",
                        "recommended": False,
                    },
                    {
                        "id": "cancel",
                        "label": "Cancel",
                        "description": "取消本次初始化，不执行写入。",
                        "recommended": False,
                    },
                ],
            }
        ]
    )
    marker = INIT_APPLY_CHOICE_SCHEMA + " " + json.dumps(
        {
            **scope,
            "choice_request_sha256": _canonical_sha256(choice_request),
            "scope_sha256": scope_sha256,
            "request_id": choice_request["request_id"],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "schema_version": "webnovel-init-apply-choice/v1",
        "scope_sha256": scope_sha256,
        "choice_request": choice_request,
        "choice_marker": marker,
    }


def build_init_preview(request: Mapping[str, Any], *, git_mode: str = "off") -> dict[str, Any]:
    """Inspect and render an exact plan without writing any filesystem state."""

    if request.get("schema_version") != INIT_REQUEST_SCHEMA:
        raise InitWorkflowError("unsupported normalized init request schema")
    if git_mode not in GIT_MODES:
        raise InitWorkflowError("git_mode must be off, init, or initial-commit")
    workspace = Path(str(request["workspace_root"]))
    target = Path(str(request["project_root"]))
    blockers: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    operations: list[dict[str, Any]] = []
    target_preexisting_initialized = False

    reference = request.get("reference_candidate")
    if reference and reference.get("status") == "proposed":
        blockers.append(
            {
                "code": "reference_candidate_unconfirmed",
                "detail": "adopt or discard the proposed reference candidate before apply",
            }
        )
    elif reference and reference.get("status") == "adopted":
        try:
            _validate_reference_adoption(request)
        except (InitWorkflowError, OSError, UnicodeError, ValueError) as exc:
            blockers.append(
                {
                    "code": "reference_adoption_unverified",
                    "detail": str(exc),
                }
            )

    parent_git = _effective_parent_git(workspace)
    if parent_git is not None:
        blockers.append(
            {
                "code": "wrong_parent_repository",
                "detail": f"workspace is inside Git repository {parent_git}",
            }
        )

    if _lexists(target):
        if _is_linklike(target) or not target.is_dir():
            blockers.append(
                {"code": "unsafe_target", "detail": "target exists but is not a regular directory"}
            )
            operations.append(_operation(".", "directory", "conflict"))
        else:
            operations.append(_operation(".", "directory", "skip"))
            core_markers = (
                target / ".webnovel" / "state.json",
                target / ".webnovel" / "idea_bank.json",
                target / ".story-system" / "MASTER_SETTING.json",
            )
            target_preexisting_initialized = any(path.is_file() for path in core_markers)
            visible_entries = [item for item in target.iterdir() if item.name != ".git"]
            if visible_entries and not target_preexisting_initialized:
                blockers.append(
                    {
                        "code": "existing_target_not_initialized",
                        "detail": "non-empty target has no trusted webnovel initialization marker",
                    }
                )
    else:
        operations.append(_operation(".", "directory", "create"))

    try:
        artifacts, expected = build_desired_artifacts(request, git_mode=git_mode)
    except (InitWorkflowError, OSError, UnicodeError, ValueError) as exc:
        artifacts = {}
        expected = {}
        blockers.append({"code": "render_failed", "detail": str(exc)})

    for relative in _BASE_DIRECTORIES:
        path = target / relative
        if not _lexists(path):
            operations.append(_operation(relative, "directory", "create"))
        elif _is_linklike(path) or not path.is_dir():
            operations.append(_operation(relative, "directory", "conflict"))
            blockers.append(
                {
                    "code": "path_type_conflict",
                    "detail": f"required directory is unsafe or has the wrong type: {relative}",
                }
            )
        else:
            operations.append(_operation(relative, "directory", "skip"))

    for relative, desired in sorted(artifacts.items()):
        path = target / relative
        if not _lexists(path):
            operations.append(
                _operation(relative, "file", "create", desired_sha256=_sha256(desired))
            )
            continue
        if _is_linklike(path) or not path.is_file():
            operations.append(_operation(relative, "file", "conflict"))
            blockers.append(
                {
                    "code": "path_type_conflict",
                    "detail": f"required file is unsafe or has the wrong type: {relative}",
                }
            )
            continue
        try:
            existing, _ = _stable_regular_bytes(
                path,
                max_bytes=MAX_EXISTING_INIT_BYTES,
                label=f"existing init file {relative}",
                allow_empty=True,
            )
        except (InitWorkflowError, OSError) as exc:
            operations.append(_operation(relative, "file", "conflict"))
            blockers.append(
                {"code": "unreadable_existing_file", "detail": f"{relative}: {exc}"}
            )
            continue
        existing_sha = _sha256(existing)
        if existing == desired:
            operations.append(
                _operation(relative, "file", "skip", existing_sha256=existing_sha)
            )
        elif relative in _CORE_JSON_PATHS:
            consistent, detail = _consistent_core_json(relative, path, expected)
            if consistent:
                operations.append(
                    _operation(
                        relative,
                        "file",
                        "preserve",
                        existing_sha256=existing_sha,
                        detail=detail,
                    )
                )
            else:
                operations.append(
                    _operation(
                        relative,
                        "file",
                        "conflict",
                        existing_sha256=existing_sha,
                        detail=detail,
                    )
                )
                blockers.append({"code": "canon_conflict", "detail": f"{relative}: {detail}"})
        elif relative in _INIT_MARKDOWN_RULES:
            consistent, detail = _consistent_init_markdown(
                relative,
                existing,
                desired,
                expected,
            )
            if consistent:
                operations.append(
                    _operation(
                        relative,
                        "file",
                        "preserve",
                        existing_sha256=existing_sha,
                        detail=detail,
                    )
                )
                warnings.append(
                    {
                        "code": "authored_markdown_preserved",
                        "detail": f"{relative} differs only outside its required init contract",
                    }
                )
            else:
                operations.append(
                    _operation(
                        relative,
                        "file",
                        "conflict",
                        existing_sha256=existing_sha,
                        detail=detail,
                    )
                )
                blockers.append(
                    {"code": "markdown_contract_conflict", "detail": f"{relative}: {detail}"}
                )
        else:
            operations.append(
                _operation(
                    relative,
                    "file",
                    "preserve",
                    existing_sha256=existing_sha,
                    detail="existing user-authored file will not be overwritten",
                )
            )

    if git_mode == "initial-commit" and target_preexisting_initialized:
        blockers.append(
            {
                "code": "initial_commit_existing_project",
                "detail": "initial-commit is allowed only while creating a new or empty target",
            }
        )
    blockers.extend(_git_blockers(target, git_mode))
    token = _sha256(_canonical_token_payload(request, git_mode, operations, blockers))
    write_list = [item["path"] for item in operations if item["status"] == "create"]
    preserve_list = [item["path"] for item in operations if item["status"] == "preserve"]
    apply_choice = _build_apply_choice(
        project_root=str(target),
        git_mode=git_mode,
        preview_token=token,
        write_list=write_list,
    )
    return {
        "schema_version": INIT_PREVIEW_SCHEMA,
        "status": "blocked" if blockers else "ready",
        "mode": "dry-run",
        "workspace_root": str(workspace),
        "project_slug": request["project_slug"],
        "project_root": str(target),
        "git_mode": git_mode,
        "preview_token": token,
        "operations": operations,
        "write_list": write_list,
        "preserve_list": preserve_list,
        "blockers": blockers,
        "warnings": warnings,
        "reference_candidate_status": (reference or {}).get("status", "none"),
        "apply_choice": apply_choice,
    }


def preview_init(config_json: str | Path, *, git_mode: str = "off") -> dict[str, Any]:
    """Load a strict request and return its zero-write preview."""

    return build_init_preview(load_init_request(config_json), git_mode=git_mode)


def _load_apply_authorization(path_value: str | Path) -> dict[str, Any]:
    path = Path(path_value)
    temp_root = (resolve_webnovel_home() / "tmp" / "init").resolve(strict=True)
    if not path.is_absolute():
        raise InitWorkflowError("apply authorization path must be absolute")
    raw, resolved = _stable_regular_bytes(
        path,
        max_bytes=256 * 1024,
        label="init apply authorization",
    )
    if not _inside(resolved, temp_root):
        raise InitWorkflowError("apply authorization must stay under WEBNOVEL_HOME/tmp/init")
    if raw.startswith(b"\xef\xbb\xbf"):
        raise InitWorkflowError("apply authorization must be UTF-8 without BOM")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InitWorkflowError("apply authorization must contain one UTF-8 JSON object") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "preview_token",
        "choice_request_id",
        "choice_marker_sha256",
        "runtime",
    }:
        raise InitWorkflowError("apply authorization has an invalid top-level shape")
    if payload.get("schema_version") != INIT_APPLY_AUTH_SCHEMA:
        raise InitWorkflowError("apply authorization schema is invalid")
    runtime = payload.get("runtime")
    runtime_fields = {
        "sessions_root",
        "parent_rollout_path",
        "parent_thread_id",
        "parent_model",
        "parent_reasoning_effort",
        "parent_rollout_sha256",
    }
    if not isinstance(runtime, dict) or set(runtime) != runtime_fields:
        raise InitWorkflowError("apply authorization runtime has an invalid shape")
    if any(not isinstance(value, str) or not value.strip() for value in runtime.values()):
        raise InitWorkflowError("apply authorization runtime fields must be non-empty strings")
    for field in ("preview_token", "choice_marker_sha256"):
        value = payload.get(field)
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise InitWorkflowError(f"apply authorization {field} must be a lowercase SHA-256")
    return payload


def _validate_apply_authorization(
    authorization_json: str | Path,
    preview: Mapping[str, Any],
) -> dict[str, Any]:
    authorization = _load_apply_authorization(authorization_json)
    if authorization.get("preview_token") != preview.get("preview_token"):
        raise InitWorkflowError("apply authorization preview_token is stale")
    choice = preview.get("apply_choice")
    if not isinstance(choice, Mapping):
        raise InitWorkflowError("init preview lacks its finite Apply choice")
    choice_request = choice.get("choice_request")
    marker = str(choice.get("choice_marker") or "")
    if (
        not isinstance(choice_request, Mapping)
        or authorization.get("choice_request_id") != choice_request.get("request_id")
        or authorization.get("choice_marker_sha256") != _sha256(marker.encode("utf-8"))
    ):
        raise InitWorkflowError("apply authorization does not bind the current choice marker")

    runtime = authorization["runtime"]
    current_thread_id = _current_codex_thread_id()
    if runtime["parent_thread_id"] != current_thread_id:
        raise InitWorkflowError(
            "init Apply parent thread does not match the current Codex task"
        )
    trusted_sessions_root = _trusted_codex_sessions_root()
    supplied_sessions_raw = Path(runtime["sessions_root"])
    try:
        supplied_sessions = supplied_sessions_raw.resolve(strict=True)
    except OSError as exc:
        raise InitWorkflowError(f"apply authorization sessions root is unavailable: {exc}") from exc
    if (
        not _same_path(supplied_sessions, trusted_sessions_root)
        or not _same_path(Path(os.path.abspath(str(supplied_sessions_raw))), supplied_sessions)
        or _is_linklike(supplied_sessions_raw)
    ):
        raise InitWorkflowError("apply authorization sessions root is not host-owned")
    rollout_raw, rollout_path = _stable_regular_bytes(
        Path(runtime["parent_rollout_path"]),
        max_bytes=MAX_REFERENCE_ROLLOUT_BYTES,
        label="init Apply parent rollout",
    )
    if not _inside(rollout_path, trusted_sessions_root):
        raise InitWorkflowError("init Apply parent rollout escaped the trusted sessions root")
    try:
        evidence = parse_parent_rollout_identity(
            rollout_path,
            expected_thread_id=runtime["parent_thread_id"],
            expected_model=runtime["parent_model"],
            expected_reasoning_effort=runtime["parent_reasoning_effort"],
            sessions_root=trusted_sessions_root,
        )
    except SmokeEvidenceError as exc:
        raise InitWorkflowError(f"init Apply parent rollout identity is invalid: {exc}") from exc
    rollout_after, rollout_after_path = _stable_regular_bytes(
        rollout_path,
        max_bytes=MAX_REFERENCE_ROLLOUT_BYTES,
        label="init Apply parent rollout",
    )
    if not _same_path(rollout_after_path, rollout_path) or rollout_after != rollout_raw:
        raise InitWorkflowError("init Apply parent rollout changed during verification")
    proof = _resolve_parent_choice(
        rollout_raw,
        marker=marker,
        choice_request=choice_request,
        question_id="init_action",
        accepted_option="apply",
        label="Init Apply",
    )
    if proof["prefix_sha256"] != runtime["parent_rollout_sha256"]:
        raise InitWorkflowError("init Apply authorization-prefix hash does not match")
    return {
        "choice_request_id": proof["request_id"],
        "parent_thread_id": evidence.thread_id,
        "parent_model": evidence.model,
        "parent_reasoning_effort": evidence.reasoning_effort,
        "parent_rollout_path": str(rollout_path),
        "parent_rollout_sha256": proof["prefix_sha256"],
    }


def _revalidate_preview_state(target: Path, preview: Mapping[str, Any]) -> None:
    """Re-check every observed type/hash immediately before the first write."""

    for item in preview.get("operations") or []:
        if not isinstance(item, Mapping):
            raise InitWorkflowError("preview operation is malformed")
        relative = str(item.get("path") or "")
        path = target if relative == "." else target / relative
        _assert_apply_path(target, path)
        kind = item.get("kind")
        status = item.get("status")
        if status == "create":
            if _lexists(path):
                raise InitWorkflowError(f"preview target changed before apply: {relative}")
            continue
        if kind == "directory":
            if not path.is_dir() or _is_linklike(path):
                raise InitWorkflowError(f"preview directory changed before apply: {relative}")
            continue
        if kind != "file" or status not in {"skip", "preserve"}:
            if status == "conflict":
                raise InitWorkflowError(f"blocked preview operation reached apply: {relative}")
            continue
        raw, _ = _stable_regular_bytes(
            path,
            max_bytes=MAX_EXISTING_INIT_BYTES,
            label=f"existing init file {relative}",
            allow_empty=True,
        )
        if _sha256(raw) != item.get("existing_sha256"):
            raise InitWorkflowError(f"existing file changed after preview: {relative}")


def _assert_apply_path(target: Path, path: Path) -> None:
    if not _inside(path, target) and path != target:
        raise InitWorkflowError(f"apply path escapes target project: {path}")
    if _is_linklike(path):
        raise InitWorkflowError(f"apply path is a symlink or junction: {path}")
    current = path if _lexists(path) else path.parent
    while current != target.parent:
        if _is_linklike(current):
            raise InitWorkflowError(f"apply path traverses a symlink or junction: {current}")
        if current == target:
            break
        current = current.parent


def _mkdir_missing(target: Path, path: Path) -> bool:
    _assert_apply_path(target, path)
    if _lexists(path):
        if _is_linklike(path) or not path.is_dir():
            raise InitWorkflowError(f"directory changed after preview: {path}")
        return False
    try:
        path.mkdir()
    except FileExistsError:
        if _is_linklike(path) or not path.is_dir():
            raise InitWorkflowError(f"directory raced with apply: {path}")
        return False
    return True


def _write_new_file(target: Path, path: Path, data: bytes) -> None:
    _assert_apply_path(target, path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise InitWorkflowError(f"file changed after preview: {path}") from exc
    completed = False
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        completed = True
    finally:
        if not completed:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


def _apply_git(
    target: Path,
    *,
    git_mode: str,
    title: str,
    allowlist: list[str],
) -> dict[str, Any]:
    if git_mode == "off":
        return {"mode": "off", "initialized": False, "committed": False}
    git_marker = target / ".git"
    marker_preexisting = _lexists(git_marker)
    if marker_preexisting and (_is_linklike(git_marker) or not git_marker.is_dir()):
        raise InitWorkflowError("Git metadata changed after preview")
    index_path = git_marker / "index"
    index_preexisting = marker_preexisting and _lexists(index_path)
    index_before: bytes | None = None
    if index_preexisting:
        if _is_linklike(index_path) or not index_path.is_file():
            raise InitWorkflowError("Git index changed to an unsafe path type")
        index_before = index_path.read_bytes()

    initialized = False
    try:
        if not marker_preexisting:
            _run_git(target, "init", check=True)
            initialized = True
        top = _git_top_level(target)
        if top is None or os.path.normcase(str(top)) != os.path.normcase(str(target)):
            raise InitWorkflowError("Git top-level is not the resolved novel root")
        if git_mode == "init":
            return {"mode": "init", "initialized": initialized, "committed": False}

        stage_paths = sorted(relative for relative in allowlist if (target / relative).is_file())
        if not stage_paths:
            raise InitWorkflowError("initial-commit has no generated allowlisted files to stage")
        if _run_git(target, "diff", "--cached", "--quiet").returncode != 0:
            raise InitWorkflowError("initial-commit refuses a non-empty Git index")
        _run_git(target, "add", "--", *stage_paths, check=True)
        staged = _run_git(target, "diff", "--cached", "--name-only", "-z", check=True).stdout
        staged_paths = {item.replace("\\", "/") for item in staged.split("\x00") if item}
        allowed = {item.replace("\\", "/") for item in stage_paths}
        unexpected = sorted(staged_paths - allowed)
        if unexpected:
            raise InitWorkflowError(
                "Git index contains paths outside the init allowlist: " + ", ".join(unexpected)
            )
        message = "初始化网文项目：" + sanitize_commit_message(title)
        # A user/system ``core.hooksPath`` may execute arbitrary hooks, including
        # post-commit hooks that ``--no-verify`` does not disable.  Init commits
        # are deterministic runtime plumbing, so override hooks for this exact
        # subprocess without changing repository or global Git configuration.
        _run_git(
            target,
            "-c",
            f"core.hooksPath={os.devnull}",
            "commit",
            "-m",
            message,
            check=True,
        )
        return {
            "mode": "initial-commit",
            "initialized": initialized,
            "committed": True,
            "staged_paths": sorted(staged_paths),
        }
    except Exception as exc:
        try:
            if not marker_preexisting and _lexists(git_marker):
                if (
                    git_marker.parent != target
                    or git_marker.name != ".git"
                    or _is_linklike(git_marker)
                    or not git_marker.is_dir()
                ):
                    raise InitWorkflowError("new Git metadata cannot be rolled back safely")
                shutil.rmtree(git_marker, onerror=_remove_readonly_for_rmtree)
            elif marker_preexisting and git_mode == "initial-commit":
                if _lexists(index_path):
                    if _is_linklike(index_path) or not index_path.is_file():
                        raise InitWorkflowError("Git index cannot be rolled back safely")
                    index_path.unlink()
                if index_preexisting and index_before is not None:
                    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                    if hasattr(os, "O_BINARY"):
                        flags |= os.O_BINARY
                    fd = os.open(index_path, flags, 0o600)
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(index_before)
                        handle.flush()
                        os.fsync(handle.fileno())
        except Exception as rollback_exc:
            raise InitWorkflowError(
                f"Git apply failed and rollback was incomplete: {rollback_exc}"
            ) from exc
        if isinstance(exc, InitWorkflowError):
            raise
        raise InitWorkflowError(f"Git operation failed safely: {exc}") from exc


def _validate_applied_project(
    request: Mapping[str, Any],
    target: Path,
    *,
    artifacts: Mapping[str, bytes],
    expected: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate canon identity, generated Markdown, and the real phase gate."""

    for relative in (
        ".webnovel/state.json",
        ".webnovel/idea_bank.json",
        ".story-system/MASTER_SETTING.json",
        ".story-system/anti_patterns.json",
        ".story-system/MASTER_SETTING.md",
        ".story-system/anti_patterns.md",
        "设定集/世界观.md",
        "设定集/力量体系.md",
        "设定集/主角卡.md",
        "设定集/反派设计.md",
        "大纲/总纲.md",
    ):
        path = target / relative
        if not path.is_file() or _is_linklike(path):
            raise InitWorkflowError(f"post-apply validation is missing a required file: {relative}")

    for relative in sorted(_CORE_JSON_PATHS):
        consistent, detail = _consistent_core_json(relative, target / relative, expected)
        if not consistent:
            raise InitWorkflowError(f"post-apply {relative} is inconsistent: {detail}")

    blockers: list[dict[str, str]] = []
    state = _load_existing_json(target / ".webnovel" / "state.json")
    info = state.get("project_info") if isinstance(state, dict) else None
    if not isinstance(info, dict) or info.get("init_constraints") != expected["state_init_constraints"]:
        blockers.append(
            {
                "code": "state_constraints_incomplete",
                "detail": ".webnovel/state.json does not carry the confirmed init_constraints",
            }
        )

    markdown_paths = {
        relative
        for relative in artifacts
        if relative.endswith(".md")
        and (
            relative.startswith("设定集/")
            or relative == "大纲/总纲.md"
            or relative.startswith(".story-system/")
        )
    }
    for relative in sorted(markdown_paths):
        path = target / relative
        try:
            current, _ = _stable_regular_bytes(
                path,
                max_bytes=MAX_EXISTING_INIT_BYTES,
                label=f"post-apply Markdown {relative}",
            )
        except (InitWorkflowError, OSError) as exc:
            blockers.append(
                {
                    "code": "markdown_unreadable",
                    "detail": f"{relative} cannot be read: {exc}",
                }
            )
            continue
        consistent, detail = _consistent_init_markdown(
            relative,
            current,
            artifacts[relative],
            expected,
        )
        if not consistent:
            blockers.append(
                {
                    "code": "markdown_contract_conflict",
                    "detail": f"{relative}: {detail}",
                }
            )

    phase = resolve_project_phase(target)
    if phase.phase != PHASE_PLAN_IN_PROGRESS:
        blockers.append(
            {
                "code": "plan_phase_not_ready",
                "detail": f"project phase is {phase.phase}, expected {PHASE_PLAN_IN_PROGRESS}",
            }
        )
    blockers.extend(
        {"code": "project_phase_blocker", "detail": detail}
        for detail in phase.blocking
    )
    return {
        "status": "ready" if not blockers else "blocked",
        "ready": not blockers,
        "phase": phase.phase,
        "target_chapter": phase.target_chapter,
        "blockers": blockers,
    }


def _init_lock_path(request: Mapping[str, Any]) -> Path:
    temp_root = resolve_webnovel_home() / "tmp" / "init"
    if _is_linklike(temp_root):
        raise InitWorkflowError("WEBNOVEL_HOME/tmp/init became a symlink or junction")
    try:
        resolved_temp = temp_root.resolve(strict=True)
    except OSError as exc:
        raise InitWorkflowError(f"WEBNOVEL_HOME/tmp/init is unavailable: {exc}") from exc
    lock_root = temp_root / "locks"
    if _lexists(lock_root) and (_is_linklike(lock_root) or not lock_root.is_dir()):
        raise InitWorkflowError("init lock directory has an unsafe path type")
    lock_root.mkdir(exist_ok=True)
    if _is_linklike(lock_root) or not _inside(lock_root.resolve(strict=True), resolved_temp):
        raise InitWorkflowError("init lock directory escaped WEBNOVEL_HOME/tmp/init")
    key = _sha256(os.path.normcase(str(request["project_root"])).encode("utf-8"))
    lock_path = lock_root / f"project-{key}.lock"
    if _lexists(lock_path) and (_is_linklike(lock_path) or not lock_path.is_file()):
        raise InitWorkflowError("project init lock has an unsafe path type")
    return lock_path


def _rollback_created_paths(
    target: Path,
    *,
    created_files: list[str],
    created_dirs: list[str],
    artifacts: Mapping[str, bytes],
) -> None:
    problems: list[str] = []
    for relative in reversed(created_files):
        path = target / relative
        try:
            _assert_apply_path(target, path)
            if not _lexists(path):
                continue
            if _is_linklike(path) or not path.is_file():
                problems.append(f"unsafe changed file: {relative}")
                continue
            raw, _ = _stable_regular_bytes(
                path,
                max_bytes=MAX_EXISTING_INIT_BYTES,
                label=f"rollback file {relative}",
                allow_empty=True,
            )
            if raw != artifacts.get(relative):
                problems.append(f"changed file preserved: {relative}")
                continue
            path.unlink()
        except (InitWorkflowError, OSError) as exc:
            problems.append(f"{relative}: {exc}")
    for relative in reversed(created_dirs):
        path = target if relative == "." else target / relative
        try:
            _assert_apply_path(target, path)
            if _lexists(path):
                if _is_linklike(path) or not path.is_dir():
                    problems.append(f"unsafe changed directory: {relative}")
                else:
                    path.rmdir()
        except OSError:
            # A non-empty directory can contain pre-existing or externally
            # created facts; never recursively remove it during rollback.
            continue
        except InitWorkflowError as exc:
            problems.append(f"{relative}: {exc}")
    if problems:
        raise InitWorkflowError("init rollback preserved changed paths: " + "; ".join(problems))


def apply_init(
    config_json: str | Path,
    *,
    git_mode: str | None,
    preview_token: str | None,
    authorization_json: str | Path | None,
) -> dict[str, Any]:
    """Apply only files marked ``create`` by a fresh matching preview."""

    if git_mode is None or git_mode not in GIT_MODES:
        raise InitWorkflowError("apply requires an explicit git_mode: off, init, or initial-commit")
    if not isinstance(preview_token, str) or len(preview_token) != 64:
        raise InitWorkflowError("apply requires the exact preview_token returned by dry-run")
    if authorization_json is None or not str(authorization_json).strip():
        raise InitWorkflowError(
            "apply requires a trusted parent-rollout finite-choice authorization file",
            code="apply_authorization_required",
        )
    initial_request = load_init_request(config_json)
    if FileLock is None:
        raise InitWorkflowError("filelock is required for guarded init apply")
    lock_path = _init_lock_path(initial_request)
    try:
        with FileLock(str(lock_path), timeout=10):
            if _is_linklike(lock_path) or not lock_path.is_file():
                raise InitWorkflowError("project init lock changed to an unsafe path type")
            # Re-open through the bounded stable reader while holding the
            # target-scoped lock; a swapped request cannot redirect this apply.
            request = load_init_request(config_json)
            if request["project_root"] != initial_request["project_root"]:
                raise InitWorkflowError("config-json changed project_root while waiting for init lock")
            preview = build_init_preview(request, git_mode=git_mode)
            if preview["preview_token"] != preview_token:
                raise InitWorkflowError("preview token is stale or belongs to different inputs")
            if preview["status"] != "ready":
                details = "; ".join(item["detail"] for item in preview["blockers"])
                raise InitWorkflowError("init preview is blocked: " + details)

            try:
                authorization_proof = _validate_apply_authorization(
                    authorization_json,
                    preview,
                )
            except InitWorkflowError as exc:
                raise InitWorkflowError(
                    f"Init Apply authorization was rejected: {exc}",
                    code="apply_authorization_invalid",
                ) from exc

            reference_proof = _validate_reference_adoption(request)
            if reference_proof is not None:
                _claim_reference_evidence(request, reference_proof)

            target = Path(request["project_root"])
            workspace = Path(request["workspace_root"])
            if _is_linklike(target) or (_lexists(target) and not target.is_dir()):
                raise InitWorkflowError("resolved project root changed to an unsafe path type")
            if _effective_parent_git(workspace) is not None:
                raise InitWorkflowError("workspace entered a parent Git repository after preview")
            artifacts, expected = build_desired_artifacts(request, git_mode=git_mode)
            _revalidate_preview_state(target, preview)

            created_dirs: list[str] = []
            created_files: list[str] = []
            try:
                if _mkdir_missing(target, target):
                    created_dirs.append(".")
                for relative in _BASE_DIRECTORIES:
                    path = target / relative
                    if _mkdir_missing(target, path):
                        created_dirs.append(relative)
                for item in preview["operations"]:
                    if item["kind"] != "file" or item["status"] != "create":
                        continue
                    relative = item["path"]
                    desired = artifacts.get(relative)
                    if desired is None:
                        raise InitWorkflowError(
                            f"preview referenced an unknown generated file: {relative}"
                        )
                    path = target / relative
                    if not path.parent.exists() or _is_linklike(path.parent):
                        raise InitWorkflowError(f"generated file parent is missing or unsafe: {relative}")
                    _write_new_file(target, path, desired)
                    created_files.append(relative)

                plan_precondition = _validate_applied_project(
                    request,
                    target,
                    artifacts=artifacts,
                    expected=expected,
                )
                if not plan_precondition["ready"]:
                    raise InitWorkflowError(
                        "initialized files do not satisfy the Plan precondition",
                        code="plan_precondition_blocked",
                        details=plan_precondition,
                    )
                git_result = _apply_git(
                    target,
                    git_mode=git_mode,
                    title=request["project"]["title"],
                    allowlist=created_files,
                )
            except Exception as exc:
                try:
                    _rollback_created_paths(
                        target,
                        created_files=created_files,
                        created_dirs=created_dirs,
                        artifacts=artifacts,
                    )
                except InitWorkflowError as rollback_exc:
                    raise InitWorkflowError(
                        f"init apply failed and rollback was incomplete: {rollback_exc}"
                    ) from exc
                raise

            return {
                "schema_version": INIT_RESULT_SCHEMA,
                "status": "success",
                "project_root": str(target),
                "project_slug": request["project_slug"],
                "git_mode": git_mode,
                "preview_token": preview_token,
                "apply_authorization": authorization_proof,
                "created_directories": created_dirs,
                "created_files": created_files,
                "preserved_files": list(preview["preserve_list"]),
                "git": git_result,
                "reference_candidate_status": preview["reference_candidate_status"],
                "reference_live_gate": (
                    "verified"
                    if preview["reference_candidate_status"] == "adopted"
                    else "not_requested"
                ),
                "plan_precondition_ready": bool(plan_precondition["ready"]),
                "plan_precondition": plan_precondition,
            }
    except Timeout as exc:
        raise InitWorkflowError("timed out waiting for the project init lock") from exc
