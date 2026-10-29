import json
from dataclasses import asdict, replace
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg.types.json import Jsonb

from digital_souls_memory.adapters.memory_records import candidate_record
from digital_souls_memory.adapters.postgres_evidence import PostgresEvidenceStore
from digital_souls_memory.adapters.postgres_memory import PostgresMemoryStore
from digital_souls_memory.adapters.postgres_retrieval import PostgresRetrievalStore
from tests.evidence_support import session, turn
from tests.memory_support import memory
from tests.postgres.test_recall_chunks import index_pending, search
from tests.postgres.test_recall_indexing import register_characters
from tests.recall_fixtures import SPACE, source_stamp
from tests.recall_support import RecordingEmbedding

pytestmark = pytest.mark.postgres


def test_upgrade_from_previous_head_and_reapply_preserve_originals_and_rebuild_chunks(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", database_url)
    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    command.downgrade(config, "0014")
    register_characters()
    evidence = PostgresEvidenceStore(database_url)
    evidence.add_session(session(reports_delivery=False))
    source = turn(text="原文🙂e\u0301" * 100)
    evidence.add_turn(source)
    value = replace(memory(), content="Ａ" * 448 + "末尾の事実" * 30)
    memories = PostgresMemoryStore(database_url)
    with memories.transaction("char-b") as tx:
        tx.add_memory(value)
    # Simulate an index published before the chunk migration using its SQL row contract.
    with psycopg.connect(database_url) as conn:
        conn.execute(
            "UPDATE recall_documents SET ready=TRUE,pending=FALSE,search_text=%s,"
            "embedding=%s::vector,model=%s,revision=%s,digest=%s,dimensions=%s,content_digest=%s,"
            "payload=%s,dependencies=%s,labels=%s",
            (
                value.content,
                "[1," + "0," * 1022 + "0]",
                SPACE.model,
                SPACE.revision,
                SPACE.digest,
                1024,
                "b" * 64,
                Jsonb(candidate_record(value)),
                Jsonb(
                    {
                        "values": [
                            json.loads(
                                json.dumps(
                                    asdict(source_stamp(source, character_id="char-b")),
                                    default=lambda at: at.isoformat(),
                                )
                            )
                        ]
                    }
                ),
                Jsonb(
                    [
                        {
                            "sensitivity": source.sensitivity,
                            "classifier_version": source.classifier_version,
                        }
                    ]
                ),
            ),
        )
        conn.execute(
            "INSERT INTO recall_document_sources(owner_id,kind,identifier,turn_id) "
            "VALUES('char-b','memory',%s,%s)",
            (value.id, source.turn_id),
        )

    for reapply in (False, True):
        if reapply:
            command.downgrade(config, "0014")
        command.upgrade(config, "head")
        command.upgrade(config, "head")
        assert evidence.get_turn(source.turn_id) == source
        assert memories.get_memory("char-b", value.id) == value
        index = PostgresRetrievalStore(database_url)
        assert search(index) == ()
        embedding = RecordingEmbedding()
        index_pending(index, embedding)
        assert sorted(search(index)) == sorted((value.content[:512], value.content[448:]))
        assert evidence.get_turn(source.turn_id) == source
