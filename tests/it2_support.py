"""Resolve explicit real-model prerequisites only at test execution time."""

import json
import os
from pathlib import Path

from digital_souls_memory.recall.settings import EMBEDDING_MODEL, RERANKER_MODEL


def load_model_endpoints() -> tuple[str, str]:
    required = (
        "MEMORY_IT2_MODEL_MANIFEST",
        "MEMORY_EMBEDDING_ENDPOINT",
        "MEMORY_RERANKER_ENDPOINT",
    )
    for name in required:
        if not os.environ.get(name, "").strip():
            raise ValueError(f"{name} is required for real-model IT2")
    manifest = json.loads(Path(os.environ[required[0]]).read_text())
    if manifest != {"embedding": EMBEDDING_MODEL, "reranker": RERANKER_MODEL}:
        raise ValueError(
            "MEMORY_IT2_MODEL_MANIFEST must declare approved model identities and digests"
        )
    return os.environ[required[1]], os.environ[required[2]]
