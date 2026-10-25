"""Upgrade existing synthetic absorption history without inventing new representatives."""

from dataclasses import replace
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from psycopg.types.json import Jsonb

from digital_souls_memory.adapters.memory_records import memory_record
from digital_souls_memory.adapters.postgres_evidence import PostgresEvidenceStore
from digital_souls_memory.adapters.postgres_memory import PostgresMemoryStore, copy_payload
from digital_souls_memory.memory.domain import ExtractionProposal, MemoryRelation
from tests.evidence_support import RECORDED, session, turn
from tests.it1.test_memory_extraction import RecordingGateway
from tests.memory_support import memory
from tests.postgres.test_deletion_recalculation import make_worker
from tests.postgres.test_recall_indexing import register_characters

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("existing", [False, True], ids=["empty", "0012-history"])
def test_representative_revision_roundtrip_preserves_and_resolves_existing_history(
    database_url: str,
    existing: bool,
) -> None:
    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    command.downgrade(config, "0012")
    if existing:
        register_characters()
        evidence = PostgresEvidenceStore(database_url)
        evidence.add_session(session(reports_delivery=False))
        for number in range(1, 4):
            evidence.add_turn(turn(f"turn-{number}", actor="user-a"))
        with psycopg.connect(database_url) as connection:
            for number, identifier in enumerate(("a", "b", "c"), 1):
                value = replace(
                    memory(),
                    id=identifier,
                    source_turn_ids=(f"turn-{number}",),
                    object="water" if identifier == "c" else "coffee",
                    superseded_at=None if identifier == "a" else RECORDED,
                )
                record = memory_record(value)
                record["epistemics"] = Jsonb(record["epistemics"])
                record["identity_links"] = Jsonb(record["identity_links"])
                connection.execute(
                    sql.SQL("INSERT INTO derived_memories ({}) VALUES ({})").format(
                        sql.SQL(",").join(map(sql.Identifier, record)),
                        sql.SQL(",").join(sql.Placeholder() for _ in record),
                    ),
                    tuple(record.values()),
                )
                connection.execute(
                    "INSERT INTO memory_sources VALUES ('char-b',%s,%s,0)",
                    (identifier, f"turn-{number}"),
                )
                if identifier == "a":
                    connection.execute(
                        "INSERT INTO memory_baselines(character_id,memory_id,payload) "
                        "VALUES ('char-b',%s,%s)",
                        (identifier, Jsonb(copy_payload(value))),
                    )
                    connection.execute(
                        "INSERT INTO memory_baseline_sources VALUES ('char-b',%s,%s,0)",
                        (identifier, f"turn-{number}"),
                    )
                else:
                    connection.execute(
                        "INSERT INTO memory_generated(character_id,memory_id,turn_id,payload) "
                        "VALUES ('char-b',%s,%s,%s)",
                        (identifier, f"turn-{number}", Jsonb(copy_payload(value))),
                    )
                    connection.execute(
                        "INSERT INTO memory_generated_sources VALUES ('char-b',%s,%s,0)",
                        (identifier, f"turn-{number}"),
                    )
                    connection.execute(
                        "INSERT INTO memory_relations VALUES ('char-b',%s,'a','extends')",
                        (identifier,),
                    )
    with psycopg.connect(database_url, autocommit=True) as connection:
        before = connection.execute("SELECT * FROM derived_memories ORDER BY id").fetchall()
        sources = connection.execute("SELECT * FROM memory_sources ORDER BY memory_id").fetchall()
        for _ in range(2):
            command.upgrade(config, "0013")
            command.upgrade(config, "0013")
            assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
                "0013",
            )
            assert (
                connection.execute("SELECT * FROM derived_memories ORDER BY id").fetchall()
                == before
            )
            assert (
                connection.execute("SELECT * FROM memory_sources ORDER BY memory_id").fetchall()
                == sources
            )
            command.downgrade(config, "0012")
            assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
                "0012",
            )
        command.upgrade(config, "0013")
    if not existing:
        return
    command.upgrade(config, "head")
    store, gateway = PostgresMemoryStore(database_url), RecordingGateway()
    worker = make_worker(database_url, store, gateway)
    for target in ("b", "c"):
        source, identifier = f"new-source-{target}", f"derived-{target}"
        evidence.add_turn(turn(source, actor="user-a"))
        gateway.output = ExtractionProposal(
            memories=(
                replace(memory(), id=identifier, object=identifier, source_turn_ids=(source,)),
            ),
            relations=(
                MemoryRelation(
                    from_id=identifier, to_id="a" if target == "b" else target, type="derives"
                ),
            ),
        )
        worker.process("char-b", source)
        state = store.get_extraction("char-b", source)
        assert state is not None and state.status == "done"
        if target == "b":
            assert store.get_memory("char-b", identifier) is not None
            with store.list_relations("char-b", identifier) as relations:
                assert tuple(relations) == (
                    MemoryRelation(from_id=identifier, to_id="a", type="derives"),
                )
        else:
            assert identifier not in {v.id for v in worker.service.active("char-b")}
