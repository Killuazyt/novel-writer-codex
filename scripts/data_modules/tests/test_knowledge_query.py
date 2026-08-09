#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""KnowledgeQuery 时序查询测试。"""
import json
import hashlib
import sqlite3
from pathlib import Path

import pytest

from data_modules.knowledge_query import KnowledgeQuery


@pytest.fixture
def setup_db(tmp_path):
    db_path = tmp_path / ".webnovel" / "index.db"
    db_path.parent.mkdir(parents=True)

    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS entities (
            id TEXT PRIMARY KEY,
            canonical_name TEXT,
            type TEXT DEFAULT '角色',
            current_json TEXT DEFAULT '{}',
            created_at TEXT,
            updated_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS state_changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_id TEXT,
            field TEXT,
            old_value TEXT,
            new_value TEXT,
            chapter INTEGER,
            created_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS relationship_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_entity TEXT,
            to_entity TEXT,
            type TEXT NOT NULL,
            action TEXT DEFAULT '',
            polarity TEXT DEFAULT '',
            strength REAL DEFAULT 0.0,
            description TEXT,
            chapter INTEGER,
            scene_index INTEGER DEFAULT 0,
            evidence TEXT DEFAULT '',
            confidence REAL DEFAULT 1.0,
            created_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS aliases (
            alias TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            entity_type TEXT NOT NULL DEFAULT '角色',
            PRIMARY KEY (alias, entity_id, entity_type)
        )
    """)

    conn.execute(
        "INSERT INTO entities (id, canonical_name, current_json) VALUES (?, ?, ?)",
        ("hanli", "韩立", json.dumps({"realm": "筑基中期", "location": "乱星海"})),
    )
    conn.execute(
        "INSERT INTO state_changes (entity_id, field, old_value, new_value, chapter) VALUES (?, ?, ?, ?, ?)",
        ("hanli", "realm", "练气圆满", "筑基初期", 30),
    )
    conn.execute(
        "INSERT INTO state_changes (entity_id, field, old_value, new_value, chapter) VALUES (?, ?, ?, ?, ?)",
        ("hanli", "realm", "筑基初期", "筑基中期", 50),
    )
    conn.execute(
        "INSERT INTO relationship_events (from_entity, to_entity, type, chapter) VALUES (?, ?, ?, ?)",
        ("hanli", "陈巧倩", "同门", 20),
    )
    conn.execute(
        "INSERT INTO relationship_events (from_entity, to_entity, type, chapter) VALUES (?, ?, ?, ?)",
        ("hanli", "陈巧倩", "合作", 45),
    )
    conn.execute(
        "INSERT INTO aliases (alias, entity_id, entity_type) VALUES (?, ?, ?)",
        ("韩老魔", "hanli", "角色"),
    )
    conn.commit()
    conn.close()
    return tmp_path


def test_entity_state_at_chapter_before_first_change(setup_db):
    kq = KnowledgeQuery(setup_db)
    result = kq.entity_state_at_chapter("hanli", 10)
    assert result["entity_id"] == "hanli"
    assert result["state_at_chapter"] == {}


def test_entity_state_at_chapter_after_first_breakthrough(setup_db):
    kq = KnowledgeQuery(setup_db)
    result = kq.entity_state_at_chapter("hanli", 35)
    assert result["state_at_chapter"]["realm"] == "筑基初期"


def test_entity_state_at_chapter_after_second_breakthrough(setup_db):
    kq = KnowledgeQuery(setup_db)
    result = kq.entity_state_at_chapter("hanli", 60)
    assert result["state_at_chapter"]["realm"] == "筑基中期"


def test_relationships_at_chapter_before_any(setup_db):
    kq = KnowledgeQuery(setup_db)
    result = kq.entity_relationships_at_chapter("hanli", 10)
    assert result["relationships"] == []


def test_relationships_at_chapter_after_first(setup_db):
    kq = KnowledgeQuery(setup_db)
    result = kq.entity_relationships_at_chapter("hanli", 25)
    assert len(result["relationships"]) == 1
    assert result["relationships"][0]["to_entity"] == "陈巧倩"
    assert result["relationships"][0]["relationship_type"] == "同门"


def test_relationships_at_chapter_after_update(setup_db):
    kq = KnowledgeQuery(setup_db)
    result = kq.entity_relationships_at_chapter("hanli", 50)
    rels = result["relationships"]
    assert len(rels) == 1
    assert rels[0]["relationship_type"] == "合作"


def test_entity_query_resolves_chinese_canonical_name_and_alias(setup_db):
    kq = KnowledgeQuery(setup_db)

    by_name = kq.entity_state_at_chapter("韩立", 60)
    by_alias = kq.entity_state_at_chapter("韩老魔", 60)

    assert by_name["entity_id"] == "hanli"
    assert by_name["resolution"]["status"] == "canonical_name"
    assert by_alias["entity_id"] == "hanli"
    assert by_alias["resolution"]["status"] == "alias"


def test_entity_query_reports_ambiguous_chinese_name_without_selecting(setup_db):
    db_path = setup_db / ".webnovel" / "index.db"
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "INSERT INTO entities (id, canonical_name, current_json) VALUES (?, ?, ?)",
            ("hanli-shadow", "韩立", "{}"),
        )

    result = KnowledgeQuery(setup_db).entity_state_at_chapter("韩立", 60)

    assert result["entity_id"] == ""
    assert result["state_at_chapter"] == {}
    assert result["resolution"]["status"] == "ambiguous"
    assert {item["entity_id"] for item in result["resolution"]["candidates"]} == {"hanli", "hanli-shadow"}


def test_knowledge_query_missing_database_does_not_create_it(tmp_path):
    db_path = tmp_path / ".webnovel" / "index.db"

    with pytest.raises(FileNotFoundError):
        KnowledgeQuery(tmp_path).entity_state_at_chapter("韩立", 1)

    assert not db_path.exists()


def test_knowledge_query_is_read_only_and_marks_legacy_projection(setup_db):
    db_path = setup_db / ".webnovel" / "index.db"
    before = hashlib.sha256(db_path.read_bytes()).hexdigest()
    before_files = {path.name for path in db_path.parent.iterdir()}

    result = KnowledgeQuery(setup_db).entity_relationships_at_chapter("韩立", 60)

    after = hashlib.sha256(db_path.read_bytes()).hexdigest()
    after_files = {path.name for path in db_path.parent.iterdir()}
    assert before == after
    assert before_files == after_files
    assert result["sources"][0]["line_start"] is None
    assert result["sources"][0]["label"] == "legacy_projection_fallback"


def test_knowledge_query_wal_mode_preserves_data_files_without_new_sidecars(tmp_path):
    from sqlite3 import dbapi2

    project = tmp_path / "WAL 中文 (A&B)"
    webnovel = project / ".webnovel"
    webnovel.mkdir(parents=True)
    (webnovel / "state.json").write_text("{}", encoding="utf-8")
    db_path = webnovel / "index.db"
    writer = dbapi2.connect(str(db_path))
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal"
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE entities (id TEXT PRIMARY KEY, canonical_name TEXT)")
        writer.execute(
            "CREATE TABLE state_changes (id INTEGER PRIMARY KEY, entity_id TEXT, field TEXT, new_value TEXT, chapter INTEGER)"
        )
        writer.execute(
            "CREATE TABLE relationship_events (id INTEGER PRIMARY KEY, from_entity TEXT, to_entity TEXT, type TEXT, description TEXT, chapter INTEGER)"
        )
        writer.execute("INSERT INTO entities VALUES ('hanli', '韩立')")
        writer.execute("INSERT INTO state_changes VALUES (1, 'hanli', 'realm', '筑基', 1)")
        writer.commit()

        before_names = {path.name for path in webnovel.iterdir()}
        before_db = hashlib.sha256(db_path.read_bytes()).hexdigest()
        before_wal = hashlib.sha256((webnovel / "index.db-wal").read_bytes()).hexdigest()
        # WAL readers update transient lock slots in an already-existing -shm file.
        # `immutable=1` would avoid that but would also ignore uncheckpointed WAL facts,
        # so the hard boundary is no new sidecar plus unchanged DB/WAL content.
        assert (webnovel / "index.db-shm").is_file()

        result = KnowledgeQuery(project).entity_state_at_chapter("韩立", 1)

        assert result["state_at_chapter"] == {"realm": "筑基"}
        assert {path.name for path in webnovel.iterdir()} == before_names
        assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before_db
        assert hashlib.sha256((webnovel / "index.db-wal").read_bytes()).hexdigest() == before_wal
        assert (webnovel / "index.db-shm").is_file()
    finally:
        writer.close()


def test_read_only_query_chain_refuses_wal_without_existing_shm(setup_db):
    from data_modules.config import DataModulesConfig
    from data_modules.index_manager import IndexManager

    db_path = setup_db / ".webnovel" / "index.db"
    wal_path = Path(f"{db_path}-wal")
    shm_path = Path(f"{db_path}-shm")
    wal_path.write_bytes(b"synthetic recovery marker")
    shm_path.unlink(missing_ok=True)
    before_names = {path.name for path in db_path.parent.iterdir()}

    with pytest.raises(sqlite3.OperationalError, match="refusing to create"):
        KnowledgeQuery(setup_db).entity_state_at_chapter("韩立", 1)

    manager = IndexManager(
        DataModulesConfig.from_project_root(setup_db), read_only=True
    )
    with pytest.raises(sqlite3.OperationalError, match="refusing to create"):
        with manager._get_conn():
            pass

    assert {path.name for path in db_path.parent.iterdir()} == before_names
    assert not shm_path.exists()
