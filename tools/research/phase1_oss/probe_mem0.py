"""Native Mem0/Qdrant/SQLite probes; no LLM or real embeddings."""

import hashlib
import json
import os
import socket
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(os.environ.get("OSS_PROBE_ROOT", "/tmp/phase1-oss-reassessment"))
os.environ.update(MEM0_DIR=str(ROOT / "mem0-data"), MEM0_TELEMETRY="false")


def deny_network(*args, **kwargs):
    raise RuntimeError("Network disabled by probe")


socket.socket.connect = deny_network

from mem0 import Memory  # noqa: E402
from mem0.embeddings.base import EmbeddingBase  # noqa: E402


class FixtureEmbeddings(EmbeddingBase):
    def embed(self, text, memory_action=None):
        return [(b - 127.5) / 127.5 for b in hashlib.md5(text.encode()).digest()]


class NoLLM:
    def generate_response(self, *args, **kwargs):
        raise AssertionError("LLM calls are forbidden")


def main():
    observations = []

    def check(name, result, requirement):
        assert result, name
        observations.append({"test": name, "execution": "PASS", "phase1": requirement})

    with tempfile.TemporaryDirectory(prefix="mem0-probe-", dir=ROOT) as d:
        with (
            patch("mem0.memory.main.EmbedderFactory.create", return_value=FixtureEmbeddings()),
            patch("mem0.memory.main.LlmFactory.create", return_value=NoLLM()),
        ):
            m = Memory.from_config(
                {
                    "vector_store": {
                        "provider": "qdrant",
                        "config": {
                            "collection_name": "synthetic",
                            "embedding_model_dims": 16,
                            "path": str(Path(d) / "qdrant"),
                            "on_disk": True,
                        },
                    },
                    "history_db_path": str(Path(d) / "history.sqlite"),
                }
            )
        try:
            text = "合成記録：『青い星』と呼ぶ。"
            fact = m.add(text, user_id="a", metadata={"source_turn_ids": ["turn-1"]}, infer=False)[
                "results"
            ][0]["id"]
            check(
                "infer_false_preserves_verbatim",
                m.get(fact)["memory"] == text,
                "MATCH: exact text storage",
            )
            check(
                "source_ids_metadata_roundtrip",
                m.get(fact)["metadata"]["source_turn_ids"] == ["turn-1"],
                "PARTIAL: metadata only, no FK",
            )
            second = m.add(text, user_id="a", infer=False)["results"][0]["id"]
            check(
                "same_text_creates_new_uuid",
                second != fact,
                "ALLOWED: duplicate raw registration is not itself an adoption failure",
            )
            derivative = m.add(
                "合成派生：アオの呼称は青い星",
                user_id="a",
                metadata={"source_turn_ids": ["turn-1"]},
                infer=False,
            )["results"][0]["id"]
            m.delete(fact)
            check("delete_removes_target", m.get(fact) is None, "MATCH: direct target deletion")
            history = m.history(fact)
            check(
                "delete_history_still_contains_text",
                text in json.dumps(history, ensure_ascii=False),
                "GAP: privacy erasure requires history purge",
            )
            check(
                "related_metadata_does_not_cascade",
                m.get(derivative) is not None,
                "GAP: source-aware cascade must be implemented",
            )
            # A native Memory.get is unscoped; the adapter must authorize the ID.
            check(
                "get_by_id_has_no_caller_scope",
                m.get(second) is not None,
                "PARTIAL: explicit authorization outside SDK required",
            )

            class FixtureExtractor:
                def generate_response(self, *args, **kwargs):
                    return json.dumps({"memory": [{"text": "Aoi likes tea."}]})

            m.llm = FixtureExtractor()
            first = m.add(
                "Aoi says tea is a favorite.",
                user_id="same-fact",
                metadata={"source_turn_ids": ["mention-1"]},
                infer=True,
            )["results"][0]["id"]
            duplicate = m.add(
                "Aoi again mentioned enjoying tea.",
                user_id="same-fact",
                metadata={"source_turn_ids": ["mention-2"]},
                infer=True,
            )
            records = m.get_all(filters={"user_id": "same-fact"})["results"]
            check(
                "canonical_extraction_skips_same_fact_duplicate",
                not duplicate["results"] and len(records) == 1,
                "MATCH mechanism: equal extracted fact hashes suppress duplicate writes",
            )
            check(
                "skipped_mention_does_not_merge_source_metadata",
                m.get(first)["metadata"]["source_turn_ids"] == ["mention-1"],
                "PARTIAL: one fact exists; later source provenance is not automatically combined",
            )
        finally:
            m.close()
            m.vector_store.client.close()
    result = {
        "candidate": "mem0",
        "models": "fixture embeddings; infer=False forbids LLM; infer=True uses fixture extraction",
        "observations": observations,
    }
    (ROOT / "mem0-results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
