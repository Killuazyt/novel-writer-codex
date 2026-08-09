#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, List

from .sqlite_readonly import read_only_sqlite_uri
from .story_runtime_sources import load_runtime_sources


class KnowledgeQuery:
    def __init__(self, project_root: Path):
        self.project_root = Path(project_root)
        self._db_path = self.project_root / ".webnovel" / "index.db"

    def _connect(self) -> sqlite3.Connection:
        if not self._db_path.is_file():
            raise FileNotFoundError(f"read model missing: {self._db_path}")
        uri = read_only_sqlite_uri(self._db_path)
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        return conn

    @staticmethod
    def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
        return (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            is not None
        )

    def _resolve_entity(self, conn: sqlite3.Connection, entity_ref: str) -> Dict[str, Any]:
        raw = str(entity_ref or "")
        if not self._table_exists(conn, "entities"):
            return {"status": "unverified_id", "query": raw, "entity_id": raw, "candidates": []}

        exact = conn.execute("SELECT id FROM entities WHERE id = ?", (raw,)).fetchone()
        if exact:
            return {"status": "exact_id", "query": raw, "entity_id": str(exact["id"]), "candidates": []}

        candidates: Dict[str, Dict[str, str]] = {}
        for row in conn.execute(
            "SELECT id, canonical_name FROM entities WHERE canonical_name = ? ORDER BY id",
            (raw,),
        ).fetchall():
            entity_id = str(row["id"])
            candidates[entity_id] = {
                "entity_id": entity_id,
                "canonical_name": str(row["canonical_name"] or ""),
                "matched_by": "canonical_name",
            }

        if self._table_exists(conn, "aliases"):
            for row in conn.execute(
                """
                SELECT a.entity_id, e.canonical_name
                FROM aliases AS a
                LEFT JOIN entities AS e ON e.id = a.entity_id
                WHERE a.alias = ?
                ORDER BY a.entity_id
                """,
                (raw,),
            ).fetchall():
                entity_id = str(row["entity_id"])
                candidates.setdefault(entity_id, {
                    "entity_id": entity_id,
                    "canonical_name": str(row["canonical_name"] or ""),
                    "matched_by": "alias",
                })

        rows = list(candidates.values())
        if len(rows) == 1:
            selected = rows[0]
            return {
                "status": str(selected["matched_by"]),
                "query": raw,
                "entity_id": str(selected["entity_id"]),
                "candidates": rows,
            }
        if len(rows) > 1:
            return {"status": "ambiguous", "query": raw, "entity_id": "", "candidates": rows}
        return {"status": "not_found", "query": raw, "entity_id": raw, "candidates": []}

    def _source(self, chapter: int) -> Dict[str, Any]:
        try:
            runtime = load_runtime_sources(self.project_root, chapter)
            fallback_reasons = list(runtime.fallback_sources)
        except Exception as exc:
            fallback_reasons = [f"runtime_source_error:{exc}"]
        fallback = bool(fallback_reasons)
        return {
            "kind": "sqlite_read_model",
            "role": "derived",
            "path": str(self._db_path.resolve()),
            "line_start": None,
            "line_end": None,
            "fallback": fallback,
            "label": "legacy_projection_fallback" if fallback else "projection_read_model",
            "fallback_reasons": fallback_reasons,
            "exists": self._db_path.is_file(),
        }

    def entity_state_at_chapter(self, entity_id: str, chapter: int) -> Dict[str, Any]:
        """查询实体在指定章节时的状态（从 state_changes 反推）。"""
        conn = self._connect()
        try:
            resolution = self._resolve_entity(conn, entity_id)
            resolved_id = str(resolution.get("entity_id") or "")
            if resolution.get("status") == "ambiguous":
                return {
                    "entity_query": entity_id,
                    "entity_id": "",
                    "at_chapter": chapter,
                    "state_at_chapter": {},
                    "resolution": resolution,
                    "sources": [self._source(chapter)],
                }
            rows = conn.execute(
                """
                SELECT field, new_value
                FROM state_changes
                WHERE entity_id = ? AND chapter <= ?
                ORDER BY chapter ASC, id ASC
                """,
                (resolved_id, chapter),
            ).fetchall()

            state: Dict[str, str] = {}
            for row in rows:
                field = str(row["field"] or "").strip()
                if field:
                    state[field] = str(row["new_value"] or "").strip()

            return {
                "entity_query": entity_id,
                "entity_id": resolved_id,
                "at_chapter": chapter,
                "state_at_chapter": state,
                "resolution": resolution,
                "sources": [self._source(chapter)],
            }
        finally:
            conn.close()

    def entity_relationships_at_chapter(self, entity_id: str, chapter: int) -> Dict[str, Any]:
        """查询实体在指定章节时的所有关系。"""
        conn = self._connect()
        try:
            resolution = self._resolve_entity(conn, entity_id)
            resolved_id = str(resolution.get("entity_id") or "")
            if resolution.get("status") == "ambiguous":
                return {
                    "entity_query": entity_id,
                    "entity_id": "",
                    "at_chapter": chapter,
                    "relationships": [],
                    "resolution": resolution,
                    "sources": [self._source(chapter)],
                }
            rows = conn.execute(
                """
                SELECT from_entity, to_entity, type AS relationship_type, description, chapter
                FROM relationship_events
                WHERE (from_entity = ? OR to_entity = ?) AND chapter <= ?
                ORDER BY chapter ASC, id ASC
                """,
                (resolved_id, resolved_id, chapter),
            ).fetchall()

            latest: Dict[str, Dict[str, Any]] = {}
            for row in rows:
                from_e = str(row["from_entity"] or "").strip()
                to_e = str(row["to_entity"] or "").strip()
                pair_key = tuple(sorted([from_e, to_e]))
                latest[str(pair_key)] = {
                    "from_entity": from_e,
                    "to_entity": to_e,
                    "relationship_type": str(row["relationship_type"] or "").strip(),
                    "description": str(row["description"] or "").strip(),
                    "since_chapter": int(row["chapter"] or 0),
                }

            return {
                "entity_query": entity_id,
                "entity_id": resolved_id,
                "at_chapter": chapter,
                "relationships": list(latest.values()),
                "resolution": resolution,
                "sources": [self._source(chapter)],
            }
        finally:
            conn.close()
