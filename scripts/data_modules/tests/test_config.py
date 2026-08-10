#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Config tests
"""

import hashlib
import os

from data_modules import config as config_module
from data_modules.config import DataModulesConfig, get_config, set_project_root


def test_config_paths_and_defaults(tmp_path):
    cfg = DataModulesConfig.from_project_root(tmp_path)
    assert cfg.project_root == tmp_path
    assert cfg.webnovel_dir.name == ".webnovel"
    assert cfg.state_file.name == "state.json"
    assert cfg.scratchpad_file.name == "memory_scratchpad.json"
    assert cfg.index_db.name == "index.db"
    assert cfg.rag_db.name == "rag.db"
    assert cfg.vector_db.name == "vectors.db"
    assert cfg.embed_api_type == "local"
    assert cfg.embed_model == "Qwen/Qwen3-Embedding-0.6B"
    assert cfg.rerank_api_type == "disabled"

    cfg.ensure_dirs()
    assert cfg.webnovel_dir.exists()


def test_local_embedding_path_defaults_to_webnovel_home(monkeypatch, tmp_path):
    webnovel_home = tmp_path / "runtime-home"
    monkeypatch.setenv("WEBNOVEL_HOME", str(webnovel_home))
    monkeypatch.delenv("EMBED_MODEL_PATH", raising=False)

    cfg = DataModulesConfig.from_project_root(tmp_path / "book")

    assert cfg.resolved_embed_model_path == (
        webnovel_home / "models" / "Qwen3-Embedding-0.6B"
    ).resolve()


def test_get_config_and_set_project_root(tmp_path):
    set_project_root(tmp_path)
    cfg = get_config()
    assert cfg.project_root == tmp_path


def test_load_dotenv(monkeypatch, tmp_path):
    # prepare .env
    env_path = tmp_path / ".env"
    env_path.write_text("EMBED_BASE_URL=https://example.com\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("EMBED_BASE_URL", raising=False)

    # call loader explicitly
    config_module._load_dotenv()
    assert os.environ.get("EMBED_BASE_URL") == "https://example.com"


def test_dotenv_priority_and_legacy_file_remains_read_only(monkeypatch, tmp_path):
    project_root = tmp_path / "book"
    webnovel_home = tmp_path / "webnovel-home"
    legacy_home = tmp_path / "claude-home"
    project_root.mkdir()
    webnovel_home.mkdir()
    (legacy_home / "webnovel-writer").mkdir(parents=True)

    (project_root / ".env").write_text(
        "EMBED_API_TYPE=local\n"
        "EMBED_BASE_URL=https://project.invalid\n"
        "EMBED_MODEL=project-model\n"
        "EMBED_MODEL_PATH=local-model\n"
        "EMBED_DEVICE=cpu\n"
        "EMBED_API_KEY=project-key\n",
        encoding="utf-8",
    )
    (webnovel_home / ".env").write_text(
        "EMBED_MODEL=native-model\n"
        "EMBED_API_KEY=native-key\n"
        "RERANK_BASE_URL=https://native.invalid\n",
        encoding="utf-8",
    )
    legacy_env = legacy_home / "webnovel-writer" / ".env"
    legacy_env.write_text(
        "EMBED_API_KEY=legacy-key\n"
        "RERANK_BASE_URL=https://legacy.invalid\n"
        "RERANK_MODEL=legacy-model\n",
        encoding="utf-8",
    )
    legacy_before = (
        legacy_env.stat().st_mtime_ns,
        hashlib.sha256(legacy_env.read_bytes()).hexdigest(),
    )

    for key in (
        "EMBED_API_TYPE",
        "EMBED_MODEL",
        "EMBED_MODEL_PATH",
        "EMBED_DEVICE",
        "EMBED_API_KEY",
        "RERANK_BASE_URL",
        "RERANK_MODEL",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("EMBED_BASE_URL", "https://process.invalid")
    monkeypatch.setenv("WEBNOVEL_HOME", str(webnovel_home))
    monkeypatch.setenv("WEBNOVEL_CLAUDE_HOME", str(legacy_home))

    cfg = DataModulesConfig.from_project_root(project_root)

    assert cfg.embed_base_url == "https://process.invalid"
    assert cfg.embed_model == "project-model"
    assert cfg.embed_api_type == "local"
    assert cfg.embed_device == "cpu"
    assert cfg.resolved_embed_model_path == (project_root / "local-model").resolve()
    assert cfg.embed_api_key == "project-key"
    assert cfg.rerank_base_url == "https://native.invalid"
    assert cfg.rerank_model == "legacy-model"
    assert (
        legacy_env.stat().st_mtime_ns,
        hashlib.sha256(legacy_env.read_bytes()).hexdigest(),
    ) == legacy_before


def test_config_default_context_template_weights_dynamic_is_available(tmp_path):
    cfg = DataModulesConfig.from_project_root(tmp_path)
    dynamic = cfg.context_template_weights_dynamic

    assert isinstance(dynamic, dict)
    assert "early" in dynamic
    assert "mid" in dynamic
    assert "late" in dynamic
    assert "plot" in dynamic["early"]


def test_config_dynamic_template_weights_are_independent_instances(tmp_path):
    cfg1 = DataModulesConfig.from_project_root(tmp_path)
    cfg2 = DataModulesConfig.from_project_root(tmp_path)

    cfg1.context_template_weights_dynamic["early"]["plot"]["core"] = 0.77

    assert cfg2.context_template_weights_dynamic["early"]["plot"]["core"] != 0.77
