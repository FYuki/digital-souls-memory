"""Bound real SQL work while equivalent extraction history accumulates."""

from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta, timezone
from typing import cast

import psycopg
import pytest
from psycopg import sql
from psycopg.abc import Params, Query
from psycopg.rows import DictRow

from digital_souls_memory.adapters.postgres_evidence import PostgresEvidenceStore
from digital_souls_memory.adapters.postgres_memory import PostgresMemoryStore
from digital_souls_memory.memory.domain import ExtractionProposal, MemoryRelation
from digital_souls_memory.memory.relations import equivalent_ids
from tests.evidence_support import RECORDED, session, turn
from tests.it1.test_memory_extraction import RecordingGateway
from tests.memory_support import memory
from tests.postgres.test_memory_continuous_ingestion import (
    LockMeasurements,
    document_states,
    extraction_worker,
    publish_and_search,
)
from tests.postgres.test_memory_fact_lookup import explain_queries, plan_nodes
from tests.postgres.test_recall_indexing import register_characters

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("kind", ["fact", "summary"])
def test_filtered_candidate_set_matches_previous_query_at_join_boundaries(
    database_url: str, kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    register_characters()
    evidence = PostgresEvidenceStore(database_url)
    evidence.add_session(session(reports_delivery=False))
    for origin in ("turn-1", "other-origin", "unfinished"):
        evidence.add_turn(turn(origin, actor="user-a"))
    worker = extraction_worker(database_url)
    worker.process("char-b", "turn-1")
    store = PostgresMemoryStore(database_url)
    state = store.get_extraction("char-b", "turn-1")
    assert state is not None and state.status == "done"
    base = replace(memory(kind), id="old", content="一致する本文")
    ambiguous = replace(base, id="ambiguous-old", content="曖昧な本文")
    coarse = replace(base, id="coarse-old", content="必要条件だけ一致")
    retained = replace(base, id="retained", content="現在も同じID")
    duplicate = replace(base, id="duplicate-old")
    cross_turn = replace(base, id="cross-turn-old", content="別Turnだけの履歴")
    cross_character = replace(base, id="cross-character-old", content="別キャラだけの履歴")
    current = (
        replace(base, id="new"),
        replace(ambiguous, id="ambiguous-a"),
        replace(ambiguous, id="ambiguous-b"),
        replace(coarse, id="coarse-new", valid_to=RECORDED),
        retained,
        replace(base, id="different-content", content="別内容"),
        replace(base, id="different-kind", kind="key" if kind == "summary" else "preference"),
        replace(base, id="different-subject", subject="another-subject"),
        replace(base, id="different-predicate", predicate="another-predicate"),
        replace(base, id="different-object", object="another-object"),
        replace(cross_turn, id="cross-turn-candidate"),
        replace(cross_character, id="cross-character-candidate"),
        replace(base, id="unselected-history", content="指定外の履歴"),
    )
    with store.transaction("char-b") as tx:
        tx.set_candidates("turn-1", (base, ambiguous, coarse, retained, duplicate), dependencies=())
        tx.set_candidates("turn-1", current, dependencies=())
        tx.save_extraction(replace(state, turn_id="other-origin"))
        tx.set_candidates("other-origin", (cross_turn,), dependencies=())
        tx.save_extraction(replace(state, turn_id="unfinished", status="retry"))
        tx.set_candidates("unfinished", (replace(base, id="not-done"),), dependencies=())
    with store.transaction("char-c") as tx:
        tx.save_extraction(replace(state, character_id="char-c"))
        tx.set_candidates(
            "turn-1", (replace(cross_character, character_id="char-c"),), dependencies=()
        )
    selected_history = frozenset(
        v.id for v in (base, ambiguous, coarse, retained, duplicate, cross_turn, cross_character)
    )
    # Preserve the old predicate as an independent comparison, not a production fallback.
    with psycopg.connect(database_url) as connection:
        connection.execute(
            "DELETE FROM memory_generated WHERE character_id='char-b' "
            "AND memory_id='unselected-history'"
        )
        rows = connection.execute(
            "SELECT c.memory_id FROM memory_candidates c "
            "WHERE c.character_id=%s AND c.turn_id=ANY(%s) "
            "AND (%s::text[] IS NULL OR EXISTS (SELECT 1 FROM memory_generated h "
            "WHERE h.character_id=c.character_id AND h.turn_id=c.turn_id "
            "AND h.memory_id=ANY(%s) AND (h.memory_id=c.memory_id OR "
            "(h.payload->'kind'=c.payload->'kind' AND h.payload->'content'=c.payload->'content' "
            "AND h.payload->'subject' IS NOT DISTINCT FROM c.payload->'subject' "
            "AND h.payload->'predicate' IS NOT DISTINCT FROM c.payload->'predicate' "
            "AND h.payload->'object' IS NOT DISTINCT FROM c.payload->'object'))))",
            ("char-b", ["turn-1"], list(selected_history), list(selected_history)),
        ).fetchall()
    expected = {"new", "ambiguous-a", "ambiguous-b", "coarse-new", "retained"}
    assert {r[0] for r in rows} == expected
    returned_rows: list[int] = []
    execute = cast(Callable[..., psycopg.Cursor[DictRow]], psycopg.Cursor.execute)

    def observed_execute(
        cursor: psycopg.Cursor[DictRow],
        query: Query,
        params: Params | None = None,
        *,
        prepare: bool | None = None,
        binary: bool | None = None,
    ) -> psycopg.Cursor[DictRow]:
        result = execute(cursor, query, params, prepare=prepare, binary=binary)
        rendered = (
            query.as_string(cursor.connection) if isinstance(query, sql.Composable) else query
        )
        if isinstance(rendered, str) and all(
            table in rendered for table in ("memory_candidates", "memory_generated")
        ):
            returned_rows.append(cursor.rowcount)
        return result

    with store.transaction("char-b") as tx:
        with monkeypatch.context() as patch:
            patch.setattr(psycopg.Cursor, "execute", observed_execute)
            values = tx.candidates_for_turns(frozenset({"turn-1"}), history_ids=selected_history)
        assert returned_rows == [len(expected)]
        assert {v.memory.id for v in values} == expected
        assert len(values) == len(expected)
        assert tuple(v.memory for v in values) == tuple(
            sorted(
                (v for v in current if v.id in expected),
                key=lambda v: (v.valid_from, v.recorded_at, v.id),
            )
        )
        assert equivalent_ids(tx.history(selected_history), values) == {
            "old": "new",
            "duplicate-old": "new",
        }
        for origins, history in (
            (frozenset(), selected_history),
            (frozenset({"turn-1"}), frozenset()),
            (frozenset({"turn-1"}), frozenset({"absent"})),
            (frozenset({"unfinished"}), frozenset({"not-done"})),
        ):
            assert tx.candidates_for_turns(origins, history_ids=history) == ()
        assert {v.memory.id for v in tx.candidates_for_turns(frozenset({"turn-1"}))} == {
            v.id for v in current
        }
        assert tx.candidates_for_turns(frozenset({"unfinished"})) == ()


@pytest.mark.parametrize("case", ["equivalent", "ambiguous", "current"])
def test_filtered_candidates_preserve_equivalence_and_uniqueness(
    database_url: str, case: str
) -> None:
    register_characters()
    evidence = PostgresEvidenceStore(database_url)
    evidence.add_session(session(reports_delivery=False))
    evidence.add_turn(turn("turn-1", actor="user-a"))
    worker = extraction_worker(database_url)
    gateway = RecordingGateway()
    worker.extractor.gateway = gateway
    old = replace(memory("summary"), id="old")
    gateway.output = (old,)
    worker.process("char-b", "turn-1")
    worker.service.requeue("turn-1")
    replacement = replace(
        old,
        id="new",
        recorded_at=old.recorded_at + timedelta(seconds=1),
        valid_from=old.valid_from.astimezone(timezone(timedelta(hours=9))),
        derivation=replace(old.derivation, model="another-synthetic-model"),
        epistemics=replace(
            old.epistemics,
            confidence_features=replace(old.epistemics.confidence_features, self_assessment=0.9),
        ),
    )
    unrelated = replace(old, id="unrelated", content="別内容")
    gateway.output = (replacement, unrelated) + (
        (replace(replacement, id="another"),)
        if case == "ambiguous"
        else (old,)
        if case == "current"
        else ()
    )
    worker.process("char-b", "turn-1")
    state = worker.service.store.get_extraction("char-b", "turn-1")
    assert state is not None and state.status == "done", state
    with worker.service.store.transaction("char-b") as tx:
        history = tx.history(frozenset({"old"}))
        current = tx.candidates_for_turns(frozenset({"turn-1"}), history_ids=frozenset({"old"}))
        assert {c.memory.id for c in current} == (
            {"new", "another"}
            if case == "ambiguous"
            else {"new", "old"}
            if case == "current"
            else {"new"}
        )
        assert equivalent_ids(history, current) == ({"old": "new"} if case == "equivalent" else {})
        assert tx.candidates_for_turns(frozenset({"turn-1"}), history_ids=frozenset()) == ()
        assert {c.memory.id for c in tx.candidates_for_turns(frozenset({"turn-1"}))} == {
            c.id for c in gateway.output
        }


@pytest.mark.parametrize("growth", ["retired_ids", "other_candidates"])
def test_restatement_work_is_bounded_as_same_origin_data_grows(
    app_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Callable[[str, object], None],
    growth: str,
) -> None:
    monkeypatch.setenv("MEMORY_DATABASE_URL", app_database_url)
    register_characters()
    evidence = PostgresEvidenceStore(app_database_url)
    evidence.add_session(session(reports_delivery=False))
    for identifier in ("turn-1", "support", "unrelated-origin"):
        evidence.add_turn(replace(turn(identifier, actor="user-a"), occurred_at=RECORDED))
    worker = extraction_worker(app_database_url)
    store = worker.service.store
    gateway = RecordingGateway()
    worker.extractor.gateway = gateway
    # Keep the extraction row present before every measured retry, including the first.
    gateway.output = ()
    worker.process("char-b", "support")
    unrelated = replace(
        memory(), id="unrelated", object="water", source_turn_ids=("unrelated-origin",)
    )
    gateway.output = (unrelated,)
    worker.process("char-b", "unrelated-origin")
    assert publish_and_search(app_database_url) == {"unrelated"}
    unrelated_index = document_states(app_database_url)["unrelated"]
    observations: dict[int, dict[str, int]] = {}
    seeded = -1
    for count in (1, 30, 100) if growth == "retired_ids" else (1, 100):
        worker.service.requeue("support")
        pending = store.get_extraction("char-b", "support")
        assert pending is not None and pending.status == "retry", pending
        if growth == "retired_ids":
            for number in range(seeded + 1, count + 1):
                worker.service.requeue("turn-1")
                current = replace(memory(), id=f"original-{number}")
                gateway.output = (current,)
                worker.process("char-b", "turn-1")
                state = store.get_extraction("char-b", "turn-1")
                assert state is not None and state.status == "done", state
            seeded = count
        else:
            worker.service.requeue("turn-1")
            current = replace(memory(), id="original")
            gateway.output = (
                current,
                *(
                    replace(memory("summary"), id=f"other-{n}", content=f"別内容 {n}")
                    for n in range(count)
                ),
            )
            worker.process("char-b", "turn-1")
            state = store.get_extraction("char-b", "turn-1")
            assert state is not None and state.status == "done", state
        gateway.output = (replace(current, id="repeated", source_turn_ids=("support",)),)
        with monkeypatch.context() as patch:
            measured = LockMeasurements(patch)
            worker.process("char-b", "support")
        state = store.get_extraction("char-b", "support")
        assert state is not None and state.status == "done", state
        assert measured.hold_seconds and not measured.acquired
        with store.transaction("char-b") as tx:
            closure = worker.service.affected(tx, "support")
        observations[count] = {
            "closure": len(closure),
            "queries": measured.queries,
            "rows": measured.rows,
        }
        record_property(f"{growth}_{count}", observations[count])
        saved = store.get_memory("char-b", current.id)
        assert saved is not None
        assert set(saved.source_turn_ids) == {"turn-1", "support"}
        assert saved.last_reinforced_at == RECORDED
        assert saved.epistemics.independent_evidence_count == 1
        assert store.get_memory("char-b", "repeated") is None
        assert store.get_memory("char-b", "unrelated") == unrelated
        assert document_states(app_database_url)["unrelated"] == unrelated_index
    baseline = observations[1]
    assert all(
        value[metric] <= baseline[metric]
        for value in observations.values()
        for metric in ("closure", "queries", "rows")
    ), observations


def test_turn_lookup_scan_is_bounded_after_unrelated_history_growth(
    database_url: str,
    record_property: Callable[[str, object], None],
) -> None:
    register_characters()
    evidence = PostgresEvidenceStore(database_url)
    evidence.add_session(session(reports_delivery=False))
    worker = extraction_worker(database_url)
    gateway = RecordingGateway()
    worker.extractor.gateway = gateway
    for origin, identifier in (("turn-1", "target"), ("other-origin", "other")):
        evidence.add_turn(turn(origin, actor="user-a"))
        gateway.output = (
            replace(memory(), id=identifier, object=identifier, source_turn_ids=(origin,)),
        )
        worker.process("char-b", origin)
        state = worker.service.store.get_extraction("char-b", origin)
        assert state is not None and state.status == "done", state
    inspected: dict[int, dict[str, float]] = {}
    queries: list[tuple[str, Params | None]] = []
    execute = cast(Callable[..., psycopg.Cursor[DictRow]], psycopg.Cursor.execute)

    def observed_execute(
        cursor: psycopg.Cursor[DictRow],
        query: Query,
        params: Params | None = None,
        *,
        prepare: bool | None = None,
        binary: bool | None = None,
    ) -> psycopg.Cursor[DictRow]:
        rendered = (
            query.as_string(cursor.connection) if isinstance(query, sql.Composable) else query
        )
        if (
            isinstance(rendered, str)
            and rendered.lstrip().upper().startswith(("SELECT", "WITH"))
            and "turn_id" in rendered
            and any(table in rendered for table in ("memory_generated", "memory_candidates"))
        ):
            queries.append((rendered, params))
        return execute(cursor, query, params, prepare=prepare, binary=binary)

    seeded = 0
    for count in (100, 10_100):
        with psycopg.connect(database_url) as connection:
            # All noise has the same owner, but belongs to a different extraction Turn.
            for table, source_table in (
                ("memory_generated", "memory_generated_sources"),
                ("memory_candidates", "memory_candidate_sources"),
            ):
                connection.execute(
                    sql.SQL(
                        "INSERT INTO {}(character_id,memory_id,turn_id,payload) "
                        "SELECT character_id,'noise-' || n,turn_id,"
                        "payload || jsonb_build_object('id','noise-' || n,'object','noise-' || n) "
                        "FROM {} CROSS JOIN generate_series(%s::integer,%s::integer) n "
                        "WHERE character_id='char-b' AND memory_id='other'"
                    ).format(sql.Identifier(table), sql.Identifier(table)),
                    (seeded, count - 1),
                )
                connection.execute(
                    sql.SQL(
                        "INSERT INTO {}(character_id,memory_id,turn_id,position) "
                        "SELECT character_id,'noise-' || n,turn_id,position "
                        "FROM {} CROSS JOIN generate_series(%s::integer,%s::integer) n "
                        "WHERE character_id='char-b' AND memory_id='other'"
                    ).format(sql.Identifier(source_table), sql.Identifier(source_table)),
                    (seeded, count - 1),
                )
                connection.execute(sql.SQL("ANALYZE {}").format(sql.Identifier(table)))
        seeded = count
        queries.clear()
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(psycopg.Cursor, "execute", observed_execute)
            with worker.service.store.transaction("char-b") as tx:
                tx.projection_neighbors(frozenset({"target"}), frozenset())
        assert queries, "The measured operation must exercise the production turn lookup"
        plans = explain_queries(database_url, queries)
        scans = [
            node
            for plan in plans
            for node in plan_nodes(plan)
            if node.get("Relation Name") in ("memory_generated", "memory_candidates")
        ]
        inspected[count] = {
            table: sum(
                (node["Actual Rows"] + node.get("Rows Removed by Filter", 0)) * node["Actual Loops"]
                for node in scans
                if node.get("Relation Name") == table
            )
            for table in ("memory_generated", "memory_candidates")
        }
        record_property(f"plans_{count}", plans)
        record_property(f"inspected_{count}", inspected[count])
    assert all(
        0 < inspected[10_100][table] <= inspected[100][table]
        for table in ("memory_generated", "memory_candidates")
    ), inspected


@pytest.mark.parametrize(
    "content",
    ["合成の本文", 'a、`a`、"a"、{"to_id":"a"}\n```text\na\n```\n~~~text\na'],
    ids=["plain", "literal-identifiers"],
)
def test_reextraction_resolves_structured_reference_and_preserves_body(
    database_url: str, content: str
) -> None:
    register_characters()
    evidence = PostgresEvidenceStore(database_url)
    evidence.add_session(session(reports_delivery=False))
    worker = extraction_worker(database_url)
    gateway = RecordingGateway()
    worker.extractor.gateway = gateway
    original = replace(memory(), id="a", content=content)
    evidence.add_turn(turn("turn-1", actor="user-a"))
    gateway.output = (original,)
    worker.process("char-b", "turn-1")
    evidence.add_turn(turn("reference-origin", actor="user-a"))
    referrer = replace(memory("summary"), id="referrer", source_turn_ids=("reference-origin",))
    gateway.output = ExtractionProposal(
        memories=(referrer,),
        relations=(MemoryRelation(from_id="referrer", to_id="a", type="derives"),),
    )
    worker.process("char-b", "reference-origin")
    worker.service.requeue("turn-1")
    gateway.output = (replace(original, id="z"),)
    worker.process("char-b", "turn-1")
    state = worker.service.store.get_extraction("char-b", "turn-1")
    assert state is not None and state.status == "done", state
    saved = worker.service.store.get_memory("char-b", "z")
    assert saved is not None and saved.content == content
    with worker.service.store.list_relations("char-b", "referrer") as relations:
        assert tuple(relations) == (MemoryRelation(from_id="referrer", to_id="z", type="derives"),)
