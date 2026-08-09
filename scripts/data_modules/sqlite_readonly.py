#!/usr/bin/env python3
"""Shared fail-closed URI construction for SQLite read models."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def read_only_sqlite_uri(database: str | Path) -> str:
    """Return a mode=ro URI without allowing SQLite to create a WAL sidecar."""

    resolved = Path(database).resolve()
    wal_path = Path(f"{resolved}-wal")
    shm_path = Path(f"{resolved}-shm")
    if wal_path.is_file() and not shm_path.is_file():
        raise sqlite3.OperationalError(
            "read-only SQLite WAL requires an existing -shm sidecar; refusing to create one"
        )
    return f"{resolved.as_uri()}?mode=ro"
