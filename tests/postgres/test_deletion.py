"""Author deletion through HTTP and a separate, nonowner execution role."""

import os
from collections.abc import Iterator
from dataclasses import replace
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.types.json import Jsonb
from sqlalchemy.engine import make_url

from digital_souls_memory.adapters.postgres_evidence import PostgresEvidenceStore
from digital_souls_memory.adapters.postgres_identity import PostgresIdentityStore
from digital_souls_memory.adapters.postgres_memory import PostgresMemoryStore
from digital_souls_memory.adapters.postgres_provision import PostgresProvisionStore
from digital_souls_memory.adapters.recall_records import provision_payload
from digital_souls_memory.api import create_app
from digital_souls_memory.evidence.provision import ProvisionRecord, SourceSpan
from digital_souls_memory.memory.extraction import CloudInput, LocalOnly
from tests.deletion_support import RecordingRecalculationGateway
from tests.evidence_support import INSTRUCTION, RECORDED, session, turn
from tests.identity_support import seed_payload
from tests.it1.identity_fakes import FakeLLMGateway, FakeReviewer
from tests.memory_support import NEW_ID, memory
from tests.postgres.test_recall_indexing import register_characters

pytestmark = pytest.mark.postgres
HEADERS = {"Authorization": "Bearer synthetic-test-credential"}


@pytest.fixture
def deletion_url(database_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    for key in tuple(os.environ):
        if key.startswith("MEMORY_") and key not in (
            "MEMORY_DATABASE_URL",
            "MEMORY_TEST_DATABASE_URL",
        ):
            monkeypatch.delenv(key)
    monkeypatch.setenv("MEMORY_AUTHOR_ID", "synthetic-author")
    monkeypatch.setenv("MEMORY_AUTHOR_TOKEN", "synthetic-test-credential")
    role = "deletion_test_" + uuid4().hex
    with psycopg.connect(database_url, autocommit=True, connect_timeout=5) as conn:
        assert conn.execute(
            "SELECT 1 FROM pg_roles WHERE rolname='memory_deletion_executor'"
        ).fetchone() == (1,)
        conn.execute(
            sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE").format(
                sql.Identifier(role)
            )
        )
        try:
            conn.execute(
                sql.SQL("GRANT memory_deletion_executor TO {}").format(sql.Identifier(role))
            )
            conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(role)))
            conn.execute(
                sql.SQL(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {}"
                ).format(sql.Identifier(role))
            )
            conn.execute(
                sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}").format(
                    sql.Identifier(role)
                )
            )
            url = make_url(database_url).set(username=role).render_as_string(hide_password=False)
            monkeypatch.setenv("MEMORY_DELETION_DATABASE_URL", url)
            yield url
        finally:
            conn.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def seed_evidence(database_url: str) -> PostgresEvidenceStore:
    register_characters()
    store = PostgresEvidenceStore(database_url)
    store.add_session(session(reports_delivery=False))
    return store


def test_user_deletion_resolves_union_and_clears_only_surviving_reply_reference(
    database_url: str, app_database_url: str, deletion_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = seed_evidence(database_url)
    values = (
        turn("t1", actor="U", kind="observation"),
        turn("t2", actor="V", addressees=("U",), address_mode="direct"),
        turn("t3", actor="U", addressees=("U",), address_mode="direct"),
        turn("t4", actor="V", text='{"actor":"U"}'),
        turn("t5", actor="V", reply_to_turn_id="t1"),
    )
    for value in values:
        evidence.add_turn(value)
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    with TestClient(create_app(clock=lambda: RECORDED)) as client:
        response = client.post(
            "/deletion-requests", headers=HEADERS, json={"request_id": "delete-1", "user_id": "U"}
        )
        assert response.status_code == 200
        body = response.json()
        assert set(body["target_turn_ids"]) == {"t1", "t2", "t3"}
        assert len(body["target_turn_ids"]) == 3
        assert set(body["cascade_result"]["deleted_turn_ids"]) == {"t1", "t2", "t3"}
        assert body["requested_by"] == "synthetic-author"
        assert client.get("/deletion-requests/delete-1", headers=HEADERS).json() == body
        for identifier in ("t1", "t2", "t3"):
            assert client.get(f"/turns/{identifier}").status_code == 404
        assert evidence.get_turn("t4") == values[3]
        assert evidence.get_turn("t5") == replace(values[4], reply_to_turn_id=None)
        evidence.add_turn(turn("t6", actor="U"))
        repeated = client.post(
            "/deletion-requests", headers=HEADERS, json={"request_id": "delete-1", "user_id": "U"}
        )
        assert repeated.status_code == 200
        assert repeated.json() == body
        assert evidence.get_turn("t6") is not None
        assert (
            client.post(
                "/deletion-requests",
                headers=HEADERS,
                json={"request_id": "delete-1", "target_turn_ids": ["t4"]},
            ).status_code
            == 409
        )
        assert evidence.get_turn("t4") == values[3]


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE turns SET reply_to_turn_id=NULL WHERE turn_id='turn-1'",
        "DELETE FROM turns WHERE turn_id='turn-1'",
        "SET memory.author_deletion='on'; DELETE FROM turns WHERE turn_id='turn-1'",
        "ALTER TABLE turns DISABLE TRIGGER ALL; DELETE FROM turns WHERE turn_id='turn-1'",
        "SET session_replication_role='replica'; DELETE FROM turns WHERE turn_id='turn-1'",
        "SET ROLE memory_deletion_owner; DELETE FROM turns WHERE turn_id='turn-1'",
        "SET ROLE memory_deletion_executor; DELETE FROM turns WHERE turn_id='turn-1'",
        "SELECT public.apply_author_deletion('forged','forged')",
    ],
)
def test_normal_connection_cannot_enable_author_deletion(
    database_url: str, app_database_url: str, deletion_url: str, statement: str
) -> None:
    evidence = seed_evidence(database_url)
    source = turn()
    evidence.add_turn(source)
    with psycopg.connect(app_database_url, connect_timeout=5) as conn:
        with pytest.raises(psycopg.Error):
            conn.execute(statement)
        conn.rollback()
    assert evidence.get_turn("turn-1") == source
    with TestClient(create_app(clock=lambda: RECORDED)) as client:
        assert (
            client.post(
                "/deletion-requests",
                headers=HEADERS,
                json={"request_id": "delete-1", "target_turn_ids": ["turn-1"]},
            ).status_code
            == 200
        )
    assert evidence.get_turn("turn-1") is None


def test_recalculation_failure_preserves_originals_and_retry_commits_all_copies(
    database_url: str,
    deletion_url: str,
) -> None:
    evidence = seed_evidence(database_url)
    evidence.add_turn(turn("turn-1", text="削除する合成事実"))
    evidence.add_turn(turn("turn-2", text="残す合成事実"))
    store = PostgresMemoryStore(database_url)
    original = replace(memory(), source_turn_ids=("turn-1", "turn-2"), content="削除前の派生内容")
    single = replace(memory("summary"), id=NEW_ID)
    with store.transaction("char-b") as tx:
        tx.add_memory(original)
        tx.add_memory(single)
    revised = replace(original, source_turn_ids=("turn-2",), content="残す合成事実の再計算")
    gateway = RecordingRecalculationGateway(revised)
    gateway.failure = RuntimeError("削除する合成事実: synthetic-test-credential")
    with TestClient(create_app(recalculation_gateway=gateway, clock=lambda: RECORDED)) as client:
        request = {"request_id": "delete-1", "target_turn_ids": ["turn-1"]}
        assert client.post("/deletion-requests", headers=HEADERS, json=request).status_code == 503
        assert evidence.get_turn("turn-1") is not None
        assert store.get_memory("char-b", original.id) == original
        assert store.get_memory("char-b", single.id) == single
        audit = client.get("/deletion-requests/delete-1", headers=HEADERS)
        assert audit.status_code == 200
        assert audit.json()["status"] == "failed"
        assert audit.json()["error"] not in (None, "")
        assert "削除する合成事実" not in audit.text
        assert "synthetic-test-credential" not in audit.text
        gateway.failure = None
        response = client.post("/deletion-requests", headers=HEADERS, json=request)
        assert response.status_code == 200
        assert response.json()["status"] == "done"
        assert evidence.get_turn("turn-1") is None
        assert store.get_memory("char-b", original.id) == revised
        assert store.get_memory("char-b", single.id) is None
        with store.transaction("char-b") as tx:
            assert tx.baselines() == (revised,)
        for value, identifier, kind in gateway.calls:
            assert tuple(t.turn_id for t in value.turns) == ("turn-2",)
            assert tuple(t.text for t in value.turns) == ("残す合成事実",)
            assert identifier == original.id
            assert kind == original.kind


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE turns SET text='forged' WHERE turn_id='turn-1'",
        "UPDATE turns SET reply_to_turn_id=NULL WHERE turn_id='turn-1'",
        "DELETE FROM turns WHERE turn_id='turn-1'",
    ],
)
def test_deletion_executor_cannot_mutate_evidence_without_a_verified_request(
    database_url: str,
    deletion_url: str,
    statement: str,
) -> None:
    evidence = seed_evidence(database_url)
    source = turn()
    evidence.add_turn(source)
    with psycopg.connect(deletion_url, connect_timeout=5) as conn:
        with pytest.raises(psycopg.Error):
            conn.execute(statement)
        conn.rollback()
    assert evidence.get_turn(source.turn_id) == source


def test_existing_turn_and_resident_provisions_cannot_restore_deleted_source(
    database_url: str,
    deletion_url: str,
) -> None:
    evidence = seed_evidence(database_url)
    deleted = turn("turn-1", text="削除する合成原文")
    evidence.add_turn(deleted)
    evidence.add_turn(turn("turn-2", text="影響のない合成原文"))
    provision_store = PostgresProvisionStore(database_url)
    records = []
    for kind, identifier in (("turn", "turn-1"), ("resident", "turn-1"), ("turn", "turn-2")):
        source = evidence.get_turn(identifier)
        assert source is not None
        records.append(
            ProvisionRecord(
                provision_id=f"provided-{kind}-{identifier}",
                kind=kind,
                character_id="char-b",
                session_id="session-1",
                surface="synthetic-chat",
                query='{"turn_id":"turn-1"}' if identifier == "turn-2" else None,
                turn_id=identifier if kind == "turn" else None,
                memory_ids=(),
                source_spans=(
                    SourceSpan(
                        turn_id=identifier,
                        start=0,
                        end=len(source.text),
                        text=source.text,
                        actor=source.actor,
                        language=source.language,
                        truncated=False,
                        original_start=0,
                        original_end=len(source.text),
                    ),
                ),
                verbatim_anchor_ids=(),
                voice_exemplar_ids=(),
                motivation_ids=(),
                temperament_version=None,
                personality_version=None,
                scores=(),
                duration_ms=0,
                provided_at=RECORDED,
            )
        )
    with psycopg.connect(database_url, autocommit=True, connect_timeout=5) as conn:
        for record in records:
            conn.execute(
                "INSERT INTO provision_records(provision_id,payload) VALUES (%s,%s)",
                (record.provision_id, Jsonb(provision_payload(record))),
            )
            assert provision_store.get(record.provision_id) == record
        with TestClient(create_app(clock=lambda: RECORDED)) as client:
            response = client.post(
                "/deletion-requests",
                headers=HEADERS,
                json={"request_id": "delete-1", "target_turn_ids": ["turn-1"]},
            )
            assert response.status_code == 200
            assert set(response.json()["cascade_result"]["invalidated_provision_ids"]) == {
                records[0].provision_id,
                records[1].provision_id,
            }
        for record in records[:2]:
            assert provision_store.get(record.provision_id) is None
            assert conn.execute(
                "SELECT payload->'source_spans' FROM provision_records WHERE provision_id=%s",
                (record.provision_id,),
            ).fetchall() in ([], [(None,)], [([],)])
        assert provision_store.get(records[2].provision_id) == records[2]


def test_database_failure_rolls_back_recalculated_copies_before_retry(
    database_url: str,
    deletion_url: str,
) -> None:
    evidence = seed_evidence(database_url)
    evidence.add_turn(turn("turn-1", text="削除する原文"))
    evidence.add_turn(turn("turn-2", text="保持する原文"))
    memories = PostgresMemoryStore(database_url)
    original = replace(memory("summary"), source_turn_ids=("turn-1", "turn-2"))
    with memories.transaction("char-b") as tx:
        tx.add_memory(original)
    revised = replace(original, source_turn_ids=("turn-2",), content="保持する原文からの要約")
    gateway = RecordingRecalculationGateway(revised)
    with psycopg.connect(database_url, autocommit=True, connect_timeout=5) as conn:
        conn.execute(
            "CREATE FUNCTION synthetic_reject_deletion() RETURNS trigger LANGUAGE plpgsql "
            "AS $$ BEGIN RAISE EXCEPTION 'synthetic storage failure'; END; $$"
        )
        conn.execute(
            "CREATE TRIGGER synthetic_deletion_failure BEFORE DELETE ON turns "
            "FOR EACH ROW EXECUTE FUNCTION synthetic_reject_deletion()"
        )
        try:
            with TestClient(
                create_app(recalculation_gateway=gateway, clock=lambda: RECORDED)
            ) as client:
                request = {"request_id": "delete-atomic", "target_turn_ids": ["turn-1"]}
                response = client.post("/deletion-requests", headers=HEADERS, json=request)
                assert response.status_code == 503
                assert gateway.calls
                assert evidence.get_turn("turn-1") is not None
                assert memories.get_memory("char-b", original.id) == original
                with memories.transaction("char-b") as tx:
                    assert tx.baselines() == (original,)
                audit = client.get("/deletion-requests/delete-atomic", headers=HEADERS)
                assert audit.status_code == 200
                assert audit.json()["status"] == "failed"
                assert audit.json()["cascade_result"] is None
                conn.execute("DROP TRIGGER synthetic_deletion_failure ON turns")
                response = client.post("/deletion-requests", headers=HEADERS, json=request)
                assert response.status_code == 200
                assert evidence.get_turn("turn-1") is None
                assert memories.get_memory("char-b", original.id) == revised
        finally:
            conn.execute("DROP TRIGGER IF EXISTS synthetic_deletion_failure ON turns")
            conn.execute("DROP FUNCTION synthetic_reject_deletion()")


@pytest.mark.parametrize(
    "second_actor,reply_to,expected_count",
    [("user-a", None, 1), ("user-b", "turn-2", 1), ("user-b", None, 2)],
)
def test_recalculation_recounts_remaining_opportunities_without_changing_identity(
    database_url: str,
    deletion_url: str,
    second_actor: str,
    reply_to: str | None,
    expected_count: int,
) -> None:
    evidence = seed_evidence(database_url)
    evidence.add_turn(turn("turn-1"))
    evidence.add_turn(turn("turn-2", actor="user-a", text=INSTRUCTION))
    evidence.add_turn(turn("turn-3", actor=second_actor, reply_to_turn_id=reply_to))
    value = replace(memory("summary"), source_turn_ids=("turn-1", "turn-2", "turn-3"))
    store = PostgresMemoryStore(database_url)
    with store.transaction("char-b") as tx:
        tx.add_memory(value)
    confidence = replace(value.epistemics.confidence_features, source=0.9)
    output = replace(
        value,
        source_turn_ids=("turn-2", "turn-3"),
        content=INSTRUCTION,
        epistemics=replace(
            value.epistemics, independent_evidence_count=99, confidence_features=confidence
        ),
    )
    identities = PostgresIdentityStore(database_url)
    with TestClient(
        create_app(llm_gateway=FakeLLMGateway(), reviewer=FakeReviewer(), clock=lambda: RECORDED)
    ) as client:
        assert client.post(
            "/characters/char-b/temperaments", json={"description": "合成気質"}
        ).is_success
        assert client.post(
            "/characters/char-b/personality-profiles/author-seed", json=seed_payload()
        ).is_success
    before = identities.get_current("char-b", RECORDED)
    assert all(component is not None for component in before)
    with TestClient(
        create_app(
            recalculation_gateway=RecordingRecalculationGateway(output), clock=lambda: RECORDED
        )
    ) as client:
        assert (
            client.post(
                "/deletion-requests",
                headers=HEADERS,
                json={"request_id": "delete-1", "target_turn_ids": ["turn-1"]},
            ).status_code
            == 200
        )
    saved = store.get_memory("char-b", value.id)
    assert saved is not None
    assert saved.source_turn_ids == ("turn-2", "turn-3")
    assert saved.epistemics.independent_evidence_count == expected_count
    assert saved.epistemics.trust_tier == value.epistemics.trust_tier
    assert saved.epistemics.source_type == value.epistemics.source_type
    assert saved.epistemics.confidence_features == confidence
    assert identities.get_current("char-b", RECORDED) == before


@pytest.mark.parametrize(
    "invalid", ["tier", "owner", "kind", "identifier", "deleted_source", "sharing"]
)
def test_invalid_recalculation_cannot_commit_deletion(
    database_url: str,
    deletion_url: str,
    invalid: str,
) -> None:
    evidence = seed_evidence(database_url)
    evidence.add_turn(turn("turn-1"))
    evidence.add_turn(turn("turn-2", perceived_by=("char-b", "char-c")))
    value = replace(memory("summary"), source_turn_ids=("turn-1", "turn-2"))
    store = PostgresMemoryStore(database_url)
    with store.transaction("char-b") as tx:
        tx.add_memory(value)
    output = replace(value, source_turn_ids=("turn-2",), content="再計算した合成内容")
    if invalid == "tier":
        output = replace(output, epistemics=replace(output.epistemics, trust_tier="A"))
    elif invalid == "owner":
        output = replace(output, character_id="char-c")
    elif invalid == "kind":
        output = replace(output, kind="key")
    elif invalid == "identifier":
        output = replace(output, id=NEW_ID)
    elif invalid == "deleted_source":
        output = replace(output, source_turn_ids=("turn-1",))
    else:
        output = replace(output, shared_with=("char-a",))
    with TestClient(
        create_app(
            recalculation_gateway=RecordingRecalculationGateway(output), clock=lambda: RECORDED
        )
    ) as client:
        response = client.post(
            "/deletion-requests",
            headers=HEADERS,
            json={"request_id": "delete-1", "target_turn_ids": ["turn-1"]},
        )
        assert not response.is_success
    assert evidence.get_turn("turn-1") is not None
    assert store.get_memory("char-b", value.id) == value


@pytest.mark.parametrize(
    "label,local", [("synthetic-sensitive", True), ("unknown", True), ("public", False)]
)
def test_recalculation_routes_remaining_original_by_sensitivity(
    database_url: str,
    deletion_url: str,
    label: str,
    local: bool,
) -> None:
    evidence = seed_evidence(database_url)
    evidence.add_turn(turn("turn-1", text="削除する合成原文"))
    source = turn("turn-2", sensitivity=label, text="残存する合成原文")
    evidence.add_turn(source)
    value = replace(memory("summary"), source_turn_ids=("turn-1", "turn-2"))
    store = PostgresMemoryStore(database_url)
    with store.transaction("char-b") as tx:
        tx.add_memory(value)
    output = replace(value, source_turn_ids=("turn-2",), content="再計算した合成内容")
    gateway = RecordingRecalculationGateway(output)
    with TestClient(
        create_app(
            recalculation_gateway=gateway,
            non_sensitive_labels=frozenset({"public"}),
            clock=lambda: RECORDED,
        )
    ) as client:
        assert (
            client.post(
                "/deletion-requests",
                headers=HEADERS,
                json={"request_id": "delete-1", "target_turn_ids": ["turn-1"]},
            ).status_code
            == 200
        )
    assert gateway.calls
    if local:
        assert gateway.cloud_calls == []
        assert gateway.local_calls
    else:
        assert gateway.local_calls == []
        assert gateway.cloud_calls
    for value_input, identifier, kind in gateway.calls:
        assert isinstance(value_input, LocalOnly if local else CloudInput)
        assert tuple(t.turn_id for t in value_input.turns) == (source.turn_id,)
        assert tuple(t.text for t in value_input.turns) == (source.text,)
        assert identifier == value.id
        assert kind == value.kind


@pytest.mark.parametrize(
    "actor,perceived_by,addressees,mode",
    [
        ("char-c", ("char-b", "char-c"), (), "broadcast"),
        ("char-b", ("char-b",), (), "self"),
        ("char-c", ("char-b", "char-c"), ("char-b",), "direct"),
    ],
)
def test_deletion_recalculation_preserves_declared_perception_without_addressee_inference(
    database_url: str,
    deletion_url: str,
    actor: str,
    perceived_by: tuple[str, ...],
    addressees: tuple[str, ...],
    mode: str,
) -> None:
    evidence = seed_evidence(database_url)
    evidence.add_turn(turn("turn-1"))
    source = turn(
        "turn-2",
        actor=actor,
        perceived_by=perceived_by,
        addressees=addressees,
        address_mode=mode,
    )
    evidence.add_turn(source)
    value = replace(memory("summary"), source_turn_ids=("turn-1", "turn-2"))
    store = PostgresMemoryStore(database_url)
    with store.transaction("char-b") as tx:
        tx.add_memory(value)
    result = replace(value, source_turn_ids=("turn-2",), content="残存出典からの再計算")
    gateway = RecordingRecalculationGateway(result)
    with TestClient(create_app(recalculation_gateway=gateway, clock=lambda: RECORDED)) as client:
        assert (
            client.post(
                "/deletion-requests",
                headers=HEADERS,
                json={"request_id": "delete-1", "target_turn_ids": ["turn-1"]},
            ).status_code
            == 200
        )
    assert evidence.get_turn("turn-2") == source
    assert gateway.calls
    assert store.get_memory("char-b", value.id) == result
    for model_input, _, _ in gateway.calls:
        assert tuple(item.turn_id for item in model_input.turns) == (source.turn_id,)
        assert tuple(item.text for item in model_input.turns) == (source.text,)
