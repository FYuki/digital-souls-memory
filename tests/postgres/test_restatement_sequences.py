"""Real SQL replay against an independent owner ledger, using synthetic evidence."""

from dataclasses import replace

import psycopg
import pytest
from fastapi.testclient import TestClient

from digital_souls_memory.adapters.postgres_memory import PostgresMemoryStore
from digital_souls_memory.adapters.postgres_retrieval import PostgresRetrievalStore
from digital_souls_memory.api import create_app
from digital_souls_memory.memory.domain import ExtractionProposal, MemoryRelation
from digital_souls_memory.memory.projection import collect_evidence, projection_input
from digital_souls_memory.memory.use_cases import MemoryService
from digital_souls_memory.recall.inputs import InputLabel, LocalOnly
from tests.evidence_support import RECORDED, turn
from tests.it1.test_memory_extraction import RecordingGateway
from tests.postgres.test_deletion import HEADERS, seed_evidence
from tests.postgres.test_deletion import deletion_url as deletion_url
from tests.postgres.test_deletion_recalculation import make_worker
from tests.postgres.test_memory_continuous_ingestion import document_states, publish_and_search
from tests.postgres.test_restatement_remediation import Recalculate
from tests.recall_support import RecordingEmbedding
from tests.restatement_fakes import SequenceRecalculate
from tests.restatement_model import Ledger, canonical, live
from tests.restatement_sequences import (
    Operation,
    extract,
    generated_sequence,
    metadata_reextraction_sequence,
    minimal_sequence,
    reextraction_sequence,
    withdrawal_sequences,
)

pytestmark = [pytest.mark.postgres, pytest.mark.usefixtures("deletion_url")]


@pytest.mark.parametrize(
    "operations", withdrawal_sequences(), ids=["absorbed-id", "baseline-sources", "deleted-target"]
)
def test_withdrawal_exploration_regressions(
    database_url: str, operations: tuple[Operation, ...]
) -> None:
    replay(database_url, operations)


def replay(database_url: str, operations: tuple[Operation, ...]) -> None:
    evidence = seed_evidence(database_url)
    store, gateway = PostgresMemoryStore(database_url), RecordingGateway()
    worker = make_worker(database_url, store, gateway)
    ledger = Ledger(RECORDED)
    bodies = {v.id: v for op in operations for v in op.bodies}
    recalculation = SequenceRecalculate(*bodies.values())
    unrelated_index: tuple[object, ...] | None = None
    with TestClient(
        create_app(
            clock=lambda: RECORDED,
            conversation_surfaces=frozenset({"synthetic-chat"}),
            recalculation_gateway=recalculation,
        )
    ) as client:
        for index, op in enumerate(operations):
            context = f"step={index} operation={op!r} sequence={operations!r}"
            if op.kind == "extract":
                if op.origin not in ledger.turns:
                    source = replace(
                        turn(
                            op.origin,
                            actor=op.actor,
                            addressees=("user-a",) if op.actor != "user-a" else (),
                            address_mode="direct" if op.actor != "user-a" else "broadcast",
                            reply_to_turn_id=op.reply_to_turn_id,
                        ),
                        occurred_at=op.occurred_at,
                    )
                    evidence.add_turn(source)
                    ledger.turns[op.origin] = source
                elif store.get_extraction("char-b", op.origin) is not None:
                    worker.service.requeue(op.origin)
                gateway.fail = False
                gateway.output = ExtractionProposal(memories=op.bodies, relations=op.relations)
                worker.process("char-b", op.origin)
                state = store.get_extraction("char-b", op.origin)
                assert state is not None and state.status == "done", (context, state)
                ledger.extract(op.origin, op.bodies, op.relations)
            elif op.kind == "retry":
                worker.service.requeue(op.origin)
                gateway.fail = True
                worker.process("char-b", op.origin)
                state = store.get_extraction("char-b", op.origin)
                assert state is not None and state.status == "retry", (context, state)
                ledger.withdraw(op.origin)
            elif op.kind == "delete":
                recalculation.outcome = op.recalculation
                if op.recalculation == "fail":
                    failed = client.post(
                        "/deletion-requests",
                        headers=HEADERS,
                        json={"request_id": f"delete-{index}", "target_turn_ids": [op.origin]},
                    )
                    assert failed.status_code == 503
                    with store.transaction("char-b") as tx:
                        assert canonical({v.id: v for v in tx.memories()}) == ledger.saved
                        assert set(tx.applied_relations()) == ledger.expected()[1]
                    assert evidence.get_turn(op.origin) == ledger.turns[op.origin]
                    recalculation.outcome = "preserve"
                response = client.post(
                    "/deletion-requests",
                    headers=HEADERS,
                    json={"request_id": f"delete-{index}", "target_turn_ids": [op.origin]},
                )
                assert response.status_code == 200, (context, response.json())
                assert response.json()["status"] == "done", context
                ledger.delete(op.origin, recalculation=recalculation.outcome)
            else:
                raise ValueError(op)
            expected, relations = ledger.checkpoint()
            # Persistence is read before any full projection or recall operation.
            with store.transaction("char-b") as tx:
                saved = canonical({v.id: v for v in tx.memories()})
                applied = set(tx.applied_relations())
                baselines = canonical({v.id: v for v in tx.baselines()})
                body_sources = tx.fact_sources(frozenset(baselines))
                temporal_origins = tx.temporal_origins(frozenset(baselines))
                snapshot = projection_input(tx)
            assert saved == expected, (context, "saved", saved, expected)
            assert applied == relations, (context, "relations", applied, relations)
            assert baselines == canonical(ledger.baselines), (
                context,
                "baselines",
                baselines,
                ledger.baselines,
            )
            original_bodies = {
                v.id: v for owner in ledger.owners.values() for v in owner.bodies
            } | ledger.baseline_temporal
            assert {i: tuple(sorted(s)) for i, s in body_sources.items()} == {
                i: ledger.baseline_sources[i]
                if i in ledger.baseline_sources
                else tuple(sorted(original_bodies[i].source_turn_ids))
                for i in baselines
            }, (
                context,
                "baseline body sources",
            )
            assert temporal_origins == {
                i: {
                    "valid_to": v.valid_to,
                    "superseded_at": v.superseded_at,
                    "last_reinforced_at": v.last_reinforced_at,
                }
                for i, v in original_bodies.items()
                if i in baselines
            }, (context, "temporal origins")
            active = {i: v for i, v in expected.items() if live(v, RECORDED)}
            assert canonical({v.id: v for v in worker.service.active("char-b")}) == active, context
            full = collect_evidence(snapshot, "char-b", worker.service.inputs).project(
                snapshot, RECORDED
            )
            assert canonical(full.memories) == expected, (context, "full projection")
            assert set(full.relations) == relations, context

            def unexpected_projection(*args: object, **kwargs: object) -> None:
                pytest.fail("Recall must consume persisted state")

            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(MemoryService, "active", unexpected_projection)
                patch.setattr(MemoryService, "rebuild", unexpected_projection)
                # Previously published documents may be withheld while dirty, but
                # must never serve an old payload after the operation has committed.
                query = LocalOnly(texts=("飲み物",), labels=(InputLabel("sensitive", "v1"),))
                result = PostgresRetrievalStore(database_url).search(
                    character_id="char-b",
                    query=query,
                    query_vector=RecordingEmbedding().embed(query, purpose="query"),
                    now=RECORDED,
                    limit=20,
                )
                for candidate in result.candidates:
                    if candidate.memory is not None:
                        assert candidate.memory.id in active, context
                        assert canonical({candidate.memory.id: candidate.memory}) == {
                            candidate.memory.id: active[candidate.memory.id]
                        }, context
                if unrelated_index is not None:
                    assert document_states(database_url)["unrelated"] == unrelated_index, context
                assert publish_and_search(database_url) == set(active), context
                published = PostgresRetrievalStore(database_url).search(
                    character_id="char-b",
                    query=query,
                    query_vector=RecordingEmbedding().embed(query, purpose="query"),
                    now=RECORDED,
                    limit=20,
                )
                assert (
                    canonical(
                        {
                            c.memory.id: c.memory
                            for c in published.candidates
                            if c.memory is not None
                        }
                    )
                    == active
                ), context
                if "unrelated" in active:
                    unrelated_index = document_states(database_url)["unrelated"]


@pytest.mark.parametrize("relation_type", ["derives", "updates", "contradicts"])
def test_original_source_deletion_removes_owned_relation_and_retains_supported_fact(
    database_url: str,
    relation_type: str,
) -> None:
    replay(database_url, minimal_sequence(relation_type, origin_first=True, actor="user-a")[:4])


@pytest.mark.parametrize("seed", [0, 3, 7, 19, 32, 86])
def test_fixed_seed_replay_matches_independent_ledger(database_url: str, seed: int) -> None:
    replay(database_url, generated_sequence(seed))


@pytest.mark.parametrize("seed", [0, 1, 2, 3])
def test_reextraction_replay_matches_independent_ledger(database_url: str, seed: int) -> None:
    replay(database_url, reextraction_sequence(seed))


@pytest.mark.parametrize("relation_type", ["derives", "updates", "contradicts"])
@pytest.mark.parametrize("actor", ["user-a", "user-b"])
def test_retry_after_original_source_deletion_separates_baseline_from_projection(
    database_url: str, relation_type: str, actor: str
) -> None:
    base = minimal_sequence(relation_type, origin_first=True, actor=actor)
    replay(database_url, (*base[:4], Operation("retry", "turn-b")))


def test_metadata_reextraction_preserves_support_and_relation_until_retry(
    database_url: str,
) -> None:
    replay(database_url, metadata_reextraction_sequence())


@pytest.mark.parametrize("include_context", [False, True])
def test_reply_evidence_count_matches_ledger(database_url: str, include_context: bool) -> None:
    first = extract("a", "one", "coffee")
    second = replace(extract("b", "two", "coffee"), actor="user-b", reply_to_turn_id="one")
    third = replace(
        extract("d", "three", "coffee"),
        actor="user-a",
        reply_to_turn_id="two" if include_context else "one",
    )
    context = first if include_context else replace(first, bodies=())
    replay(database_url, (context, second, third))


@pytest.mark.parametrize("outcome", ["unavailable", "fail"])
@pytest.mark.parametrize("relation_type", ["derives", "updates", "contradicts"])
def test_deletion_recalculation_failure_and_resend_match_ledger(
    database_url: str, outcome: str, relation_type: str
) -> None:
    base = minimal_sequence(relation_type, origin_first=True, actor="user-b")
    replay(database_url, (*base[:3], replace(base[3], recalculation=outcome)))


def test_deleting_reply_context_withdraws_support_and_rewritten_relation(database_url: str) -> None:
    base = minimal_sequence("derives", origin_first=True, actor="user-b")
    support = replace(
        base[2],
        reply_to_turn_id="question",
        relations=(replace(base[1].relations[0], from_id="b"),),
    )
    replay(
        database_url,
        (
            base[0],
            replace(base[1], relations=()),
            Operation("extract", "question"),
            support,
            Operation("delete", "question"),
        ),
    )


def test_origin_deletion_preserves_another_owners_relation_and_unrelated_index(
    database_url: str,
) -> None:
    operations = minimal_sequence("derives", origin_first=True, actor="user-b")
    support = replace(
        operations[2], relations=(MemoryRelation(from_id="b", to_id="c", type="derives"),)
    )
    replay(
        database_url,
        (
            extract("unrelated", "turn-unrelated", "unrelated"),
            *operations[:2],
            support,
            *operations[3:],
        ),
    )


@pytest.mark.parametrize("retained", [False, True])
def test_origin_deletion_compares_owners_after_target_id_resolution(
    database_url: str, retained: bool
) -> None:
    base = minimal_sequence("derives", origin_first=True, actor="user-b")
    support = replace(
        base[2],
        relations=(MemoryRelation(from_id="b", to_id="c", type="derives"),) if retained else (),
    )
    renamed = replace(base[0], bodies=(replace(base[0].bodies[0], id="renamed-c"),))
    replay(database_url, (*base[:2], support, renamed, *base[3:]))


def test_structured_restatement_references_preserve_literal_body(database_url: str) -> None:
    content = 'b、`b`、{"from_id":"b"}\n```text\nb\n```\n~~~text\nb'
    base = minimal_sequence("derives", origin_first=True, actor="user-a")
    repeated = base[2]
    summary = replace(
        repeated.bodies[0],
        id="d",
        kind="summary",
        content=content,
        subject=None,
        predicate=None,
        object=None,
    )
    proposal = replace(
        repeated,
        bodies=(*repeated.bodies, summary),
        relations=(
            MemoryRelation(from_id="d", to_id="b", type="derives"),
            MemoryRelation(from_id="b", to_id="c", type="updates"),
        ),
    )
    replay(database_url, (base[0], replace(base[1], relations=()), proposal))


def test_original_source_deletion_failure_rolls_back_relations_and_can_retry(
    database_url: str,
) -> None:
    evidence = seed_evidence(database_url)
    store, gateway = PostgresMemoryStore(database_url), RecordingGateway()
    worker = make_worker(database_url, store, gateway)
    operations = minimal_sequence("derives", origin_first=True, actor="user-a")
    for op in operations[:3]:
        evidence.add_turn(turn(op.origin, actor=op.actor))
        gateway.output = ExtractionProposal(memories=op.bodies, relations=op.relations)
        worker.process("char-b", op.origin)
    with store.transaction("char-b") as tx:
        before = tuple(tx.memories()), set(tx.applied_relations()), tx.source_contributions()
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(
            "CREATE FUNCTION synthetic_delete_failure() RETURNS trigger LANGUAGE plpgsql "
            "AS $$ BEGIN RAISE EXCEPTION 'synthetic deletion failure'; END; $$"
        )
        conn.execute(
            "CREATE TRIGGER synthetic_delete_failure BEFORE DELETE ON turns "
            "FOR EACH ROW EXECUTE FUNCTION synthetic_delete_failure()"
        )
        with TestClient(
            create_app(
                clock=lambda: RECORDED,
                conversation_surfaces=frozenset({"synthetic-chat"}),
                recalculation_gateway=Recalculate(*(v for op in operations for v in op.bodies)),
            )
        ) as client:
            payload = {"request_id": "delete-atomic", "target_turn_ids": ["turn-1"]}
            try:
                assert (
                    client.post("/deletion-requests", headers=HEADERS, json=payload).status_code
                    == 503
                )
            finally:
                conn.execute("DROP TRIGGER synthetic_delete_failure ON turns")
                conn.execute("DROP FUNCTION synthetic_delete_failure()")
            with store.transaction("char-b") as tx:
                assert (
                    tuple(tx.memories()),
                    set(tx.applied_relations()),
                    tx.source_contributions(),
                ) == before
            assert evidence.get_turn("turn-1") is not None
            assert (
                client.post("/deletion-requests", headers=HEADERS, json=payload).status_code == 200
            )
            with store.list_relations("char-b", "a") as relations:
                assert tuple(relations) == ()
