#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from .placeholder_scanner import scan_placeholders


class PrewriteValidator:
    def __init__(self, project_root: Path):
        self.project_root = Path(project_root)

    def build(
        self,
        chapter: int,
        review_contract: Dict[str, Any],
        plot_structure: Dict[str, Any],
        story_contract: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        errors: list[Dict[str, Any]] = []
        state = self._load_state(errors)
        pending = state.get("disambiguation_pending") or []
        warnings = state.get("disambiguation_warnings") or []
        contract_provided = story_contract is not None
        story_contract = story_contract or {}
        missing_contracts = []
        if contract_provided:
            missing_contracts = [
                name
                for name in ("master_setting", "chapter_brief", "volume_brief", "review_contract")
                if not story_contract.get(name)
            ]
        blocking_reasons: list[str] = []
        if pending:
            blocking_reasons.append("存在高优先级 disambiguation_pending")
        if missing_contracts:
            blocking_reasons.append(
                "缺少 Story System 合同: " + ", ".join(missing_contracts)
            )
        related_placeholders = self._related_placeholders(story_contract, errors)
        blocking_reasons[0:0] = [str(item["message"]) for item in errors]
        if related_placeholders:
            blocking_reasons.append("当前章节相关设定存在未补齐占位")
        blocking = bool(blocking_reasons)
        return {
            "schema_version": "webnovel-prewrite-validation/v1",
            "ok": not blocking,
            "errors": errors,
            "warnings": [],
            "chapter": chapter,
            "blocking": blocking,
            "blocking_reasons": blocking_reasons,
            "missing_contracts": missing_contracts,
            "related_placeholders": related_placeholders,
            "forbidden_zones": list(review_contract.get("blocking_rules") or []),
            "disambiguation_domain": {
                "pending_count": len(pending),
                "warning_count": len(warnings),
                "allowed_mentions": [
                    item.get("mention", "")
                    for item in warnings
                    if isinstance(item, dict) and item.get("mention")
                ],
            },
            "fulfillment_seed": {
                "planned_nodes": list(plot_structure.get("mandatory_nodes") or []),
                "prohibitions": list(plot_structure.get("prohibitions") or []),
            },
        }

    def _load_state(self, errors: list[Dict[str, Any]]) -> Dict[str, Any]:
        state_path = self.project_root / ".webnovel" / "state.json"
        try:
            raw = state_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            errors.append(
                self._issue(
                    "state_missing",
                    "缺少 .webnovel/state.json，无法验证写章状态",
                    state_path,
                    "先初始化或修复项目，再重新运行 prewrite gate。",
                )
            )
            return {}
        except UnicodeDecodeError:
            errors.append(
                self._issue(
                    "state_not_utf8",
                    "state.json 不是有效 UTF-8，无法安全读取",
                    state_path,
                    "将 state.json 修复为 UTF-8 编码，或从可信备份恢复。",
                )
            )
            return {}
        except OSError as exc:
            errors.append(
                self._issue(
                    "state_read_failed",
                    f"读取 state.json 失败: {exc}",
                    state_path,
                    "检查文件权限和磁盘状态后重试。",
                )
            )
            return {}

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            errors.append(
                self._issue(
                    "state_invalid_json",
                    f"state.json 不是合法 JSON: {exc.msg}",
                    state_path,
                    "修复 JSON 格式，或从可信备份恢复。",
                )
            )
            return {}
        if not isinstance(payload, dict):
            errors.append(
                self._issue(
                    "state_not_object",
                    "state.json 顶层必须是 JSON object",
                    state_path,
                    "将顶层值修复为 JSON object，或从可信备份恢复。",
                )
            )
            return {}
        return payload

    @staticmethod
    def _issue(code: str, message: str, path: Path, repair: str) -> Dict[str, Any]:
        return {
            "code": code,
            "severity": "blocker",
            "message": message,
            "path": str(path),
            "repair": repair,
        }

    def _related_placeholders(
        self,
        story_contract: Dict[str, Any],
        errors: list[Dict[str, Any]],
    ) -> list[Dict[str, Any]]:
        chapter_brief = story_contract.get("chapter_brief") or {}
        directive = chapter_brief.get("chapter_directive") or {}
        entity_terms = [
            str(item or "").strip()
            for item in directive.get("key_entities") or []
            if str(item or "").strip()
        ]
        if not entity_terms:
            return []

        related: list[Dict[str, Any]] = []
        try:
            placeholders = scan_placeholders(self.project_root)
        except Exception as exc:
            errors.append(
                self._issue(
                    "placeholder_scan_failed",
                    f"扫描当前章节相关占位符失败: {exc}",
                    self.project_root,
                    "检查设定集和大纲文件的编码、权限与格式后重试。",
                )
            )
            return []
        for item in placeholders:
            context = str(item.get("context") or "")
            file_name = Path(str(item.get("file") or "")).stem
            if any(term in context or term in file_name for term in entity_terms):
                related.append(item)
        return related
