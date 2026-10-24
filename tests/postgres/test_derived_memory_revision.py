from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from digital_souls_memory.adapters.postgres_evidence import PostgresEvidenceStore
from digital_souls_memory.adapters.postgres_resident import PostgresContextStore
from digital_souls_memory.resident.domain import ContextKey
from tests.context_gateway_support import EpisodeGateway
from tests.evidence_support import RECORDED, session, turn
from tests.postgres.test_context_work_selection import context_worker
from tests.postgres.test_recall_indexing import register_characters

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("existing", [False, True])
def test_single_revision_roundtrip_rechecks_existing_context_work(
    database_url: str, existing: bool
) -> None:
    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    scripts = ScriptDirectory.from_config(config)
    revision = scripts.get_revision("0012")
    assert revision is not None and revision.down_revision == "0011"
    assert scripts.get_heads() == ["0013"]
    command.downgrade(config, "0011")
    before = []
    artifacts = []
    if existing:
        register_characters()
        evidence = PostgresEvidenceStore(database_url)
        evidence.add_session(replace(session(reports_delivery=False), ended_at=None))
        evidence.add_turn(turn(actor="user-a", perceived_by=("char-b",)))
        for identifier in ("completed", "unprocessed"):
            evidence.add_session(
                replace(session(identifier, reports_delivery=False), ended_at=None)
            )
            evidence.add_turn(
                turn(identifier, session_id=identifier, actor="user-a", perceived_by=("char-b",))
            )
        with psycopg.connect(database_url) as connection:
            before = connection.execute("SELECT * FROM turns").fetchall()
            connection.execute(
                "INSERT INTO character_states(character_id,session_id,surface,current_topic,"
                "updated_at,expires_at,input_signature,dependencies) "
                "VALUES ('char-b','session-1','synthetic-chat','合成の既存状態',%s,%s,"
                "'synthetic-old-signature','{\"values\":[]}')",
                (RECORDED, RECORDED + timedelta(minutes=30)),
            )
            artifacts = connection.execute("SELECT * FROM character_states").fetchall()
            for operation, status, lease, retry in (
                ("state", "running", RECORDED + timedelta(minutes=5), None),
                ("episodes", "retry", None, RECORDED + timedelta(seconds=30)),
            ):
                connection.execute(
                    "INSERT INTO context_work(operation,character_id,session_id,surface,status,"
                    "token,lease_until,retry_at) VALUES (%s,'char-b','session-1','synthetic-chat',"
                    "%s,%s,%s,%s)",
                    (operation, status, "synthetic-lease" if lease else None, lease, retry),
                )
            for operation, status in (("state", "done"), ("episodes", "failed")):
                connection.execute(
                    "INSERT INTO context_work(operation,character_id,session_id,surface,status,"
                    "signature,completed_signature) VALUES (%s,'char-b','completed',"
                    "'synthetic-chat',%s,'synthetic-old','synthetic-old')",
                    (operation, status),
                )
    for _ in range(2):
        command.upgrade(config, "0012")
        command.upgrade(config, "0012")
        with psycopg.connect(database_url) as connection:
            assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
                "0012",
            )
            assert connection.execute("SELECT * FROM turns").fetchall() == before
            assert connection.execute("SELECT * FROM character_states").fetchall() == artifacts
            if existing:
                assert connection.execute(
                    "SELECT operation,status,token,lease_until,retry_at "
                    "FROM context_work WHERE session_id='session-1' ORDER BY operation"
                ).fetchall() == [
                    ("episodes", "retry", None, None, RECORDED + timedelta(seconds=30)),
                    ("state", "running", "synthetic-lease", RECORDED + timedelta(minutes=5), None),
                ]
                assert connection.execute(
                    "SELECT operation,status FROM context_work WHERE session_id='completed' "
                    "ORDER BY operation",
                ).fetchall() == [("episodes", "failed"), ("state", "done")]
        command.downgrade(config, "0011")
    command.upgrade(config, "0012")
    if existing:
        store = PostgresContextStore(database_url)
        gateway = EpisodeGateway()
        worker = context_worker(store, gateway)
        key = ContextKey(character_id="char-b", session_id="session-1", surface="synthetic-chat")
        later = RECORDED + timedelta(minutes=6)
        worker.clock = lambda: later
        page = store.work_page(after=None, now=later, limit=10)
        assert {(item.operation, item.key.session_id) for item in page.items} == {
            (operation, session_id)
            for operation in ("state", "episodes")
            for session_id in (key.session_id, "completed", "unprocessed")
        }
        for session_id in (key.session_id, "completed", "unprocessed"):
            current = replace(key, session_id=session_id)
            worker.process_state(current)
            worker.process_episodes(current)
            assert store.get_state(current) is not None
            assert len(store.list_episodes(current)) == 1
        assert store.work_page(after=None, now=later, limit=10).items == ()
