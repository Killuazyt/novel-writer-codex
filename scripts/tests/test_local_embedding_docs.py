from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_readme_documents_local_embedding_install_and_download() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "## 本地嵌入模型（默认，不需要 API Key）" in text
    assert "不再要求\n`EMBED_API_KEY`" in text
    assert "sentence-transformers" in text
    assert "huggingface-hub" in text
    assert "hf download Qwen/Qwen3-Embedding-0.6B" in text
    assert "modelscope download --model Qwen/Qwen3-Embedding-0.6B" in text
    assert "EMBED_API_TYPE=local" in text
    assert "EMBED_MODEL_PATH=" in text
    assert "RERANK_API_TYPE=disabled" in text
    assert "$webnovel-doctor" in text


def test_runtime_requirements_include_local_embedding_stack() -> None:
    text = (ROOT / "scripts" / "requirements.txt").read_text(encoding="utf-8")

    for requirement in (
        "sentence-transformers>=2.7.0",
        "transformers>=4.51.0",
        "huggingface-hub>=0.28.0",
    ):
        assert requirement in text
