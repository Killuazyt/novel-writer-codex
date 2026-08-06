#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
from pathlib import Path

import validate_repository_hygiene as hygiene


PLUGIN_ROOT = Path(__file__).resolve().parents[2]


def test_frozen_lock_is_complete_and_internally_consistent():
    errors = []

    hygiene._validate_lock(PLUGIN_ROOT, None, errors)

    assert errors == []
    payload = json.loads((PLUGIN_ROOT / "upstream-lock.json").read_text(encoding="utf-8"))
    assert payload["hashes"]["file_count"] == len(payload["hashes"]["files"]) == 330


def test_high_confidence_secret_is_reported_without_echoing_value(tmp_path):
    root = tmp_path / "plugin"
    path = root / "scripts" / "leaked.py"
    path.parent.mkdir(parents=True)
    secret = "sk-proj-" + "A" * 48
    path.write_text(f"TOKEN = {secret!r}\n", encoding="utf-8")
    errors = []

    hygiene._validate_candidate(root, path, errors)

    secret_errors = [item for item in errors if item["code"] == "high_confidence_secret_detected"]
    assert len(secret_errors) == 1
    assert secret not in json.dumps(secret_errors, ensure_ascii=False)


def test_archive_and_non_allowlisted_top_level_are_rejected(tmp_path):
    root = tmp_path / "plugin"
    path = root / "release" / "plugin.zip"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not a real archive")
    errors = []

    hygiene._validate_candidate(root, path, errors)

    assert {item["code"] for item in errors} >= {
        "archive_path_not_allowlisted",
        "archive_binary_present",
    }
