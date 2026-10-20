from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from digital_souls_memory.adapters.postgres_evidence import PostgresEvidenceStore
from digital_souls_memory.adapters.postgres_identity import PostgresIdentityStore
from digital_souls_memory.adapters.postgres_memory import PostgresMemoryStore
from digital_souls_memory.api import create_app
from digital_souls_memory.memory.changes import reinforce, update_fact
from digital_souls_memory.memory.domain import MemoryRelation
from tests.evidence_support import OCCURRED, RECORDED, TEXT, session, turn
from tests.memory_support import KINDS, NEW_ID, OLD_ID, memory

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("target_first", [False, True])
def test_worker_forward_relation_survives_postgres_restore_and_rebuild(
    seeded_url: str, target_first: bool
) -> None:
    from digital_souls_memory.adapters.identity_characters import IdentityCharacters
    from digital_souls_memory.evidence.materials import EvidenceMaterials
    from digital_souls_memory.memory.domain import ExtractionProposal
    from digital_souls_memory.memory.extraction import MemoryExtractor
    from digital_souls_memory.memory.use_cases import MemoryService
    from digital_souls_memory.memory.worker import MemoryWorker
    from tests.it1.test_memory_extraction import RecordingGateway

    evidence = PostgresEvidenceStore(seeded_url)
    materials = EvidenceMaterials(
        evidence,
        IdentityCharacters(lambda: PostgresIdentityStore(seeded_url)),
        lambda: RECORDED,
        timedelta(minutes=10),
    )
    store = PostgresMemoryStore(seeded_url)
    service = MemoryService(store, evidence, materials, lambda: RECORDED)
    gateway = RecordingGateway()
    fact = replace(memory(), subject="char-c", id=OLD_ID if target_first else NEW_ID)
    summary = replace(memory("summary"), id=NEW_ID if target_first else OLD_ID)
    relation = MemoryRelation(from_id=summary.id, to_id=fact.id, type="derives")
    gateway.output = ExtractionProposal(memories=(summary, fact), relations=(relation,))
    extractor = MemoryExtractor(
        evidence=evidence,
        materials=materials,
        gateway=gateway,
        non_sensitive_labels=frozenset(),
        conversation_surfaces=frozenset(),
    )
    worker = MemoryWorker(service, extractor)
    worker.process("char-b", "turn-1")
    restored = PostgresMemoryStore(seeded_url)
    service = MemoryService(restored, evidence, materials, lambda: RECORDED)
    for _ in range(2):
        with restored.transaction("char-b") as tx:
            service.rebuild(tx)
        worker.process("char-b", "turn-1")
        assert {v.id for v in service.active("char-b")} == {OLD_ID, NEW_ID}
        with restored.list_relations("char-b", summary.id) as values:
            assert tuple(values) == (relation,)


def test_postgres_pages_include_all_surfaces_and_perceivers(seeded_url: str) -> None:
    from digital_souls_memory.memory.ports import WorkCursor

    evidence = PostgresEvidenceStore(seeded_url)
    for i in range(5):
        evidence.add_turn(replace(turn(turn_id=f"page-{i}"), surface=f"page-surface-{i}"))
    store = PostgresMemoryStore(seeded_url)
    after = None
    surfaces: list[str] = []
    while True:
        page = store.surface_page(after=after, limit=2)
        assert len(page.surfaces) <= 2
        surfaces.extend(page.surfaces)
        if page.next_after is None:
            break
        after = page.next_after
    assert surfaces == sorted(["synthetic-chat", *(f"page-surface-{i}" for i in range(5))])
    items: list[WorkCursor] = []
    for surface in surfaces:
        cursor = None
        while True:
            work = store.work_page(surface, after=cursor, limit=1)
            assert len(work.items) <= 1
            items.extend(work.items)
            if work.next_after is None:
                break
            cursor = work.next_after
    assert {(v.turn_id, v.character_id) for v in items} == {
        (identifier, character)
        for identifier in ["turn-1", "turn-2", *(f"page-{i}" for i in range(5))]
        for character in ("char-b", "char-c")
    }
    assert len(items) == 14


def test_worker_closes_page_connections_before_model_and_consumes_all_pages(
    seeded_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from collections.abc import Iterator
    from contextlib import contextmanager

    import psycopg
    from psycopg.rows import DictRow

    from digital_souls_memory.adapters.identity_characters import IdentityCharacters
    from digital_souls_memory.evidence.materials import EvidenceMaterials
    from digital_souls_memory.memory.domain import DerivedMemory
    from digital_souls_memory.memory.extraction import LocalOnly, MemoryExtractor
    from digital_souls_memory.memory.ports import WorkCursor, WorkPage
    from digital_souls_memory.memory.use_cases import MemoryService
    from digital_souls_memory.memory.worker import MemoryWorker
    from tests.memory_fakes import EchoGateway

    store = PostgresMemoryStore(seeded_url)
    evidence = PostgresEvidenceStore(seeded_url)
    materials = EvidenceMaterials(
        evidence,
        IdentityCharacters(lambda: PostgresIdentityStore(seeded_url)),
        lambda: RECORDED,
        timedelta(minutes=10),
    )
    service = MemoryService(store, evidence, materials, lambda: RECORDED)
    gateway = EchoGateway()
    connections: list[psycopg.Connection[DictRow]] = []
    original_connection = store._connection
    original_extract = gateway.extract_local
    original_page = store.work_page
    read_pages: list[int] = []

    @contextmanager
    def connection() -> Iterator[psycopg.Connection[DictRow]]:
        with original_connection() as conn:
            connections.append(conn)
            try:
                yield conn
            finally:
                connections.remove(conn)

    def extract(value: LocalOnly) -> tuple[DerivedMemory, ...]:
        assert connections == []
        return original_extract(value)

    def page(surface: str, *, after: WorkCursor | None, limit: int) -> WorkPage:
        result = original_page(surface, after=after, limit=1)
        assert connections == []
        read_pages.append(len(result.items))
        return result

    monkeypatch.setattr(store, "_connection", connection)
    monkeypatch.setattr(store, "work_page", page)
    monkeypatch.setattr(gateway, "extract_local", extract)
    extractor = MemoryExtractor(
        evidence=evidence,
        materials=materials,
        gateway=gateway,
        non_sensitive_labels=frozenset(),
        conversation_surfaces=frozenset(),
    )
    worker = MemoryWorker(service, extractor)
    worker._surface("synthetic-chat")
    assert connections == []
    assert read_pages == [1, 1, 1, 1, 0]
    assert gateway.calls.count("char-b") == gateway.calls.count("char-c") == 2
    for character in ("char-b", "char-c"):
        assert len(service.active(character)) == 1
        sources = service.active(character)[0].source_turn_ids
        assert set(sources) == {"turn-1", "turn-2"} and len(sources) == 2


@pytest.fixture
def seeded_url(app_database_url: str) -> str:
    with TestClient(create_app(identity_store=PostgresIdentityStore(app_database_url))) as client:
        for identifier in ("char-b", "char-c"):
            assert client.post(
                f"/characters/{identifier}/core-identities",
                json={"values": ["誠実"], "prohibitions": []},
            ).is_success
    evidence = PostgresEvidenceStore(app_database_url)
    evidence.add_session(session(reports_delivery=False))
    evidence.add_turn(turn())
    evidence.add_turn(turn(turn_id="turn-2"))
    return app_database_url


@pytest.mark.parametrize("kind", KINDS)
def test_all_memory_attributes_roundtrip_through_new_adapter(seeded_url: str, kind: str) -> None:
    store = PostgresMemoryStore(seeded_url)
    value = memory(kind)
    with store.transaction("char-b") as tx:
        tx.add_memory(value)
    restored = PostgresMemoryStore(seeded_url)
    assert restored.get_memory("char-b", OLD_ID) == value
    assert restored.get_memory("char-c", OLD_ID) is None
    with restored.list_memories("char-b") as values:
        assert list(values) == [value]
    assert PostgresEvidenceStore(seeded_url).get_turn("turn-1") == turn()


@pytest.mark.parametrize("relation_type", ["updates", "extends", "derives", "contradicts"])
def test_relation_endpoints_and_type_survive_restore(seeded_url: str, relation_type: str) -> None:
    store = PostgresMemoryStore(seeded_url)
    relation = MemoryRelation(from_id=NEW_ID, to_id=OLD_ID, type=relation_type)
    with store.transaction("char-b") as tx:
        tx.add_memory(memory())
        tx.add_memory(replace(memory(), id=NEW_ID, source_turn_ids=("turn-2",)))
        tx.add_relation(relation)
    with PostgresMemoryStore(seeded_url).list_relations("char-b", NEW_ID) as values:
        assert list(values) == [relation]


def test_fact_update_persists_both_versions_and_relation(seeded_url: str) -> None:
    store = PostgresMemoryStore(seeded_url)
    old = memory()
    at = OCCURRED + timedelta(days=1)
    candidate = replace(
        old,
        id=NEW_ID,
        content="利用者Aは紅茶を好む",
        object="tea",
        valid_from=at,
        source_turn_ids=("turn-2",),
    )
    with store.transaction("char-b") as tx:
        tx.add_memory(old)
    closed, added = update_fact(old, candidate)
    with store.transaction("char-b") as tx:
        tx.replace_memory(closed)
        tx.add_memory(added)
        tx.add_relation(MemoryRelation(from_id=NEW_ID, to_id=OLD_ID, type="updates"))
    restored = PostgresMemoryStore(seeded_url)
    assert restored.get_memory("char-b", OLD_ID) == replace(old, valid_to=at)
    assert restored.get_memory("char-b", NEW_ID) == candidate
    assert closed.last_reinforced_at is None
    source = PostgresEvidenceStore(seeded_url).get_turn("turn-1")
    assert source is not None
    assert source.text == TEXT


def test_identifier_collision_rolls_back_old_interval_sources_and_relation(
    seeded_url: str,
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
) -> None:
    store = PostgresMemoryStore(seeded_url)
    old = memory()
    with store.transaction("char-b") as tx:
        tx.add_memory(old)
        tx.add_memory(replace(old, id=NEW_ID))
    candidate = replace(
        old,
        id=NEW_ID,
        content="利用者Aは紅茶を好む",
        object="tea",
        valid_from=OCCURRED + timedelta(days=1),
        source_turn_ids=("turn-2",),
    )
    closed, added = update_fact(old, candidate)
    with rejected(), store.transaction("char-b") as tx:
        tx.replace_memory(closed)
        tx.add_memory(added)
        tx.add_relation(MemoryRelation(from_id=NEW_ID, to_id=OLD_ID, type="updates"))
    assert PostgresMemoryStore(seeded_url).get_memory("char-b", OLD_ID) == old
    assert store.get_memory("char-b", NEW_ID) == replace(old, id=NEW_ID)
    with store.list_relations("char-b", NEW_ID) as values:
        assert list(values) == []


@pytest.mark.parametrize("sources", [(), ("absent",), ("turn-1", "private")])
def test_invalid_sources_cannot_partially_commit_a_batch(
    seeded_url: str,
    sources: tuple[str, ...],
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
) -> None:
    evidence = PostgresEvidenceStore(seeded_url)
    evidence.add_turn(turn(turn_id="private", perceived_by=("char-c",)))
    valid = memory()
    invalid = replace(valid, id=NEW_ID)
    # Exercise persistence independently of the dataclass's constructor guard.
    object.__setattr__(invalid, "source_turn_ids", sources)
    store = PostgresMemoryStore(seeded_url)
    with rejected(), store.transaction("char-b") as tx:
        tx.add_memory(valid)
        tx.add_memory(invalid)
    assert store.get_memory("char-b", OLD_ID) is None
    assert store.get_memory("char-b", NEW_ID) is None


def test_missing_commitment_original_reference_is_rejected_atomically(
    seeded_url: str,
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
) -> None:
    store = PostgresMemoryStore(seeded_url)
    with rejected(), store.transaction("char-b") as tx:
        tx.add_memory(replace(memory("commitment"), verbatim_turn_id="absent"))
    assert store.get_memory("char-b", OLD_ID) is None


def test_reinforcement_time_and_unique_sources_survive_restart_and_duplicate_input(
    seeded_url: str,
) -> None:
    store = PostgresMemoryStore(seeded_url)
    old = replace(memory(), last_reinforced_at=RECORDED)
    with store.transaction("char-b") as tx:
        tx.add_memory(old)
    source = turn(turn_id="turn-2")
    once = reinforce(old, source, relation_type="extends")
    with store.transaction("char-b") as tx:
        tx.replace_memory(once)
    restored = PostgresMemoryStore(seeded_url)
    saved = restored.get_memory("char-b", OLD_ID)
    assert saved is not None
    with restored.transaction("char-b") as tx:
        tx.replace_memory(reinforce(saved, source, relation_type="extends"))
    saved = restored.get_memory("char-b", OLD_ID)
    assert saved is not None
    assert saved.last_reinforced_at == RECORDED
    assert set(saved.source_turn_ids) == {"turn-1", "turn-2"}
    assert len(saved.source_turn_ids) == 2
    assert saved.epistemics.source_type == "inferred"
    assert saved.epistemics.trust_tier == "D"


@pytest.mark.parametrize("invalid", ["empty", "missing", "unperceived"])
def test_direct_sql_cannot_bypass_required_existing_perceived_sources(
    seeded_url: str, invalid: str
) -> None:
    import psycopg
    from psycopg import sql

    store = PostgresMemoryStore(seeded_url)
    with store.transaction("char-b") as tx:
        tx.add_memory(memory())
    PostgresEvidenceStore(seeded_url).add_turn(turn(turn_id="private", perceived_by=("char-c",)))
    with pytest.raises(psycopg.IntegrityError), psycopg.connect(seeded_url) as conn:
        columns = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='derived_memories' ORDER BY ordinal_position"
        ).fetchall()
        names = [row[0] for row in columns]
        selections = [sql.Placeholder() if name == "id" else sql.Identifier(name) for name in names]
        conn.execute(
            sql.SQL(
                "INSERT INTO derived_memories ({}) SELECT {} FROM derived_memories "
                "WHERE character_id=%s AND id=%s"
            ).format(sql.SQL(",").join(map(sql.Identifier, names)), sql.SQL(",").join(selections)),
            (NEW_ID, "char-b", OLD_ID),
        )
        if invalid != "empty":
            conn.execute(
                "INSERT INTO memory_sources(character_id,memory_id,turn_id,position) "
                "VALUES(%s,%s,%s,0)",
                ("char-b", NEW_ID, "absent" if invalid == "missing" else "private"),
            )
    assert store.get_memory("char-b", NEW_ID) is None
    assert store.get_memory("char-b", OLD_ID) == memory()


def test_direct_sql_cannot_move_last_source_away_from_existing_memory(seeded_url: str) -> None:
    import psycopg

    store = PostgresMemoryStore(seeded_url)
    first = memory()
    second = replace(first, id=NEW_ID, source_turn_ids=("turn-2",))
    with store.transaction("char-b") as tx:
        tx.add_memory(first)
        tx.add_memory(second)
    with pytest.raises(psycopg.IntegrityError), psycopg.connect(seeded_url) as conn:
        conn.execute(
            "UPDATE memory_sources SET memory_id=%s,position=1 "
            "WHERE character_id=%s AND memory_id=%s",
            (NEW_ID, "char-b", OLD_ID),
        )
    assert store.get_memory("char-b", OLD_ID) == first
    assert store.get_memory("char-b", NEW_ID) == second
