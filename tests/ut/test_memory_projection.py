from dataclasses import replace
from datetime import timedelta
from hashlib import sha256

import pytest

from digital_souls_memory.adapters.identity_characters import IdentityCharacters
from digital_souls_memory.evidence.materials import EvidenceMaterials
from digital_souls_memory.memory.domain import (
    DeliveryBasis,
    DerivedMemory,
    ExtractionProposal,
    ExtractionState,
    MemoryRelation,
)
from digital_souls_memory.memory.inputs import MemoryInputs
from digital_souls_memory.memory.use_cases import MemoryService
from digital_souls_memory.memory.worker import MemoryWorker
from tests.evidence_support import RECORDED, TEXT, receipt, session, turn
from tests.it1.evidence_fakes import FakeEvidenceStore, registered_identities
from tests.it1.test_memory_extraction import Harness
from tests.memory_fakes import FakeMemoryStore
from tests.memory_support import memory

pytestmark = pytest.mark.ut


def test_active_excludes_invalid_baseline_and_its_dependent_without_missing_target_error() -> None:
    h = Harness()
    store = FakeMemoryStore(h.evidence)
    service = MemoryService(store, h.extractor.inputs, lambda: h.now)
    worker = MemoryWorker(service, h.extractor)
    h.evidence.add_turn(turn())
    h.evidence.add_receipt(receipt("full", len(TEXT)))
    target = memory()
    with store.transaction("char-b") as tx:
        tx.add_memory(target)
    h.evidence.add_turn(turn("turn-2"))
    h.evidence.add_receipt(replace(receipt("full", len(TEXT)), turn_id="turn-2"))
    candidate = replace(memory("summary"), id="dependent", source_turn_ids=("turn-2",))
    h.gateway.output = ExtractionProposal(
        memories=(candidate,),
        relations=(MemoryRelation(from_id=candidate.id, to_id=target.id, type="derives"),),
    )
    worker.process("char-b", "turn-2")
    assert {v.id for v in service.active("char-b")} == {target.id, candidate.id}

    h.evidence.add_receipt(receipt("none", 0))
    assert service.active("char-b") == ()


@pytest.mark.parametrize("origin", ["baseline", "candidate"])
def test_projection_recounts_support_and_read_does_not_write(origin: str) -> None:
    evidence = FakeEvidenceStore()
    identities = registered_identities()
    evidence.add_session(session(reports_delivery=False))
    evidence.add_turn(turn(actor="user-a"))
    materials = EvidenceMaterials(
        evidence,
        IdentityCharacters(lambda: identities),
        lambda: RECORDED,
        timedelta(minutes=10),
    )
    inputs = MemoryInputs(
        evidence,
        materials,
        conversation_surfaces=frozenset({"synthetic-chat"}),
        comment_surfaces=frozenset(),
    )
    store = FakeMemoryStore(evidence)
    service = MemoryService(store, inputs, lambda: RECORDED)
    value = replace(memory(), epistemics=replace(memory().epistemics, independent_evidence_count=8))
    with store.transaction("char-b") as tx:
        if origin == "baseline":
            tx.add_memory(value)
        else:
            resolved = inputs.resolve("char-b", ("turn-1",))
            decision = resolved.turns[0].decision
            assert decision is not None
            tx.save_extraction(
                ExtractionState(
                    character_id="char-b",
                    turn_id="turn-1",
                    delivery_basis=DeliveryBasis(decision.basis),
                    signature=resolved.signature(tx.notification_version("turn-1")),
                    status="done",
                    token=None,
                    lease_until=None,
                    deadline=decision.deadline,
                    error=None,
                    text_hash=None
                    if decision.text is None
                    else sha256(decision.text.encode("utf-8")).hexdigest(),
                    used_end=None if decision.text is None else len(decision.text),
                    receipt_version=decision.receipt_version,
                )
            )
            tx.set_candidates("turn-1", (value,), dependencies=resolved.dependencies())
        service.rebuild(tx)
    saved = store.get_memory("char-b", value.id)
    assert saved is not None
    assert saved.epistemics.independent_evidence_count == 1
    before = dict(store.values), dict(store.states), list(store.relations)
    assert service.active("char-b") == (saved,)
    assert (store.values, store.states, store.relations) == before
    assert replace(saved.epistemics, independent_evidence_count=8) == value.epistemics


def test_rebuild_does_not_write_an_unchanged_projection(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.memory_fakes import FakeMemoryTransaction

    evidence = FakeEvidenceStore()
    evidence.add_session(session(reports_delivery=False))
    evidence.add_turn(turn(actor="user-a"))
    identities = registered_identities()
    materials = EvidenceMaterials(
        evidence, IdentityCharacters(lambda: identities), lambda: RECORDED, timedelta(minutes=10)
    )
    store = FakeMemoryStore(evidence)
    service = MemoryService(
        store,
        MemoryInputs(
            evidence,
            materials,
            conversation_surfaces=frozenset({"synthetic-chat"}),
            comment_surfaces=frozenset(),
        ),
        lambda: RECORDED,
    )
    with store.transaction("char-b") as tx:
        tx.add_memory(memory())
        service.rebuild(tx)
    writes: list[str] = []
    replace_projection = FakeMemoryTransaction.replace_projection

    def observe(tx: FakeMemoryTransaction, value: DerivedMemory) -> None:
        writes.append(value.id)
        replace_projection(tx, value)

    monkeypatch.setattr(FakeMemoryTransaction, "replace_projection", observe)
    with store.transaction("char-b") as tx:
        service.rebuild(tx)
    assert store.get_memory("char-b", memory().id) == memory()
    assert writes == [], "Unchanged projection writes invalidate its recall document"


@pytest.mark.parametrize("relation_type", ["updates", "extends", "contradicts", "derives"])
@pytest.mark.parametrize("withdrawn", [False, True])
@pytest.mark.parametrize("saved_relation", [False, True])
def test_affected_memories_include_effects_and_dependents_but_not_derives_references(
    relation_type: str,
    withdrawn: bool,
    saved_relation: bool,
) -> None:
    evidence = FakeEvidenceStore()
    identities = registered_identities()
    materials = EvidenceMaterials(
        evidence, IdentityCharacters(lambda: identities), lambda: RECORDED, timedelta(minutes=10)
    )
    store = FakeMemoryStore(evidence)
    service = MemoryService(
        store,
        MemoryInputs(
            evidence,
            materials,
            conversation_surfaces=frozenset({"synthetic-chat"}),
            comment_surfaces=frozenset(),
        ),
        lambda: RECORDED,
    )
    seed = replace(memory(), id="seed", object="seed")
    target = replace(memory(), id="target", object="target")
    dependent = replace(memory(), id="dependent", object="dependent")
    same_fact = replace(seed, id="same-fact")
    unrelated = replace(memory(), id="unrelated", object="unrelated")
    with store.transaction("char-b") as tx:
        tx.add_memory(target)
        tx.add_memory(unrelated)
        tx.set_candidates("origin", (seed,), dependencies=())
        tx.set_candidates("another-origin", (dependent, same_fact), dependencies=())
        tx.set_relations(
            "origin", (MemoryRelation(from_id="seed", to_id="target", type=relation_type),)
        )
        tx.set_relations(
            "another-origin", (MemoryRelation(from_id="dependent", to_id="seed", type="derives"),)
        )
        if saved_relation:
            tx.add_relation(MemoryRelation(from_id="seed", to_id="target", type=relation_type))
            tx.set_relations("origin", ())
        if withdrawn:
            store.withdrawn.update({("char-b", "target"), ("char-b", "unrelated")})
        affected = service.affected(tx, "origin")
        assert affected == frozenset(
            {"seed", "same-fact", "dependent"}
            | ({"target"} if relation_type != "derives" or withdrawn else set())
        )
        assert service.affected(tx, "absent") == frozenset()


def test_scoped_rebuild_restores_only_the_affected_withdrawn_projection() -> None:
    evidence = FakeEvidenceStore()
    evidence.add_session(session(reports_delivery=False))
    evidence.add_turn(turn(actor="user-a"))
    identities = registered_identities()
    materials = EvidenceMaterials(
        evidence, IdentityCharacters(lambda: identities), lambda: RECORDED, timedelta(minutes=10)
    )
    store = FakeMemoryStore(evidence)
    service = MemoryService(
        store,
        MemoryInputs(
            evidence,
            materials,
            conversation_surfaces=frozenset({"synthetic-chat"}),
            comment_surfaces=frozenset(),
        ),
        lambda: RECORDED,
    )
    affected, unrelated = memory(), replace(memory(), id="unrelated", object="tea")
    with store.transaction("char-b") as tx:
        tx.add_memory(affected)
        tx.add_memory(unrelated)
    store.withdrawn = {("char-b", affected.id), ("char-b", unrelated.id)}
    with store.transaction("char-b") as tx:
        service.rebuild(tx, frozenset({affected.id}))
    assert store.get_memory("char-b", affected.id) == affected
    assert store.withdrawn == {("char-b", unrelated.id)}


@pytest.mark.parametrize(
    "replacement_id,recorded_delay",
    [("z-reextracted", 0), ("a-reextracted", 1)],
    ids=["id-only", "later-recorded-at"],
)
def test_reextraction_after_repetition_keeps_update_and_saved_relation(
    replacement_id: str, recorded_delay: int
) -> None:
    h = Harness(reports_delivery=False)
    store = FakeMemoryStore(h.evidence)
    service = MemoryService(store, h.extractor.inputs, lambda: h.now)
    worker = MemoryWorker(service, h.extractor)
    original = replace(memory(), id="a-original")
    repetition = replace(original, id="b-repetition", source_turn_ids=("turn-2",))
    updated = replace(
        original,
        id="c-update",
        object="tea",
        content="紅茶",
        source_turn_ids=("turn-3",),
        valid_from=RECORDED,
    )
    for identifier in ("turn-1", "turn-2", "turn-3"):
        h.evidence.add_turn(
            replace(
                turn(identifier, actor="user-a"),
                occurred_at=RECORDED if identifier == "turn-3" else original.valid_from,
            )
        )
    h.gateway.output = (original,)
    worker.process("char-b", "turn-1")
    assert service.active("char-b") == (original,)
    h.gateway.output = (repetition,)
    worker.process("char-b", "turn-2")
    assert [(v.id, v.object, v.source_turn_ids) for v in service.active("char-b")] == [
        (original.id, "coffee", ("turn-1", "turn-2"))
    ]
    with store.list_relations("char-b", repetition.id) as relations:
        assert tuple(relations) == (
            MemoryRelation(from_id=repetition.id, to_id=original.id, type="extends"),
        )
    h.gateway.output = ExtractionProposal(
        memories=(updated,),
        relations=(MemoryRelation(from_id=updated.id, to_id=original.id, type="updates"),),
    )
    worker.process("char-b", "turn-3")
    assert service.active("char-b") == (updated,)
    with store.list_relations("char-b", updated.id) as relations:
        assert tuple(relations) == (
            MemoryRelation(from_id=updated.id, to_id=original.id, type="updates"),
        )

    h.gateway.output = (
        replace(
            original,
            id=replacement_id,
            recorded_at=original.recorded_at + timedelta(seconds=recorded_delay),
        ),
    )
    store.requeue("turn-1")
    worker.process("char-b", "turn-1")
    for identifier in ("turn-1", "turn-2", "turn-3"):
        state = store.get_extraction("char-b", identifier)
        assert state is not None and state.status == "done"
    with store.list_relations("char-b", updated.id) as relations:
        saved_relations = tuple(relations)
    assert ([(v.id, v.object) for v in service.active("char-b")], saved_relations) == (
        [(updated.id, "tea")],
        (MemoryRelation(from_id=updated.id, to_id=replacement_id, type="updates"),),
    )


def test_update_of_absorbed_repetition_targets_the_representative() -> None:
    h = Harness(reports_delivery=False)
    store = FakeMemoryStore(h.evidence)
    service = MemoryService(store, h.extractor.inputs, lambda: h.now)
    worker = MemoryWorker(service, h.extractor)
    original = replace(memory(), id="a-original")
    repetition = replace(original, id="b-repetition", source_turn_ids=("turn-2",))
    for identifier, candidate in (("turn-1", original), ("turn-2", repetition)):
        h.evidence.add_turn(turn(identifier, actor="user-a"))
        h.gateway.output = (candidate,)
        worker.process("char-b", identifier)
    assert [(v.id, v.object) for v in service.active("char-b")] == [(original.id, "coffee")]
    updated = replace(
        original,
        id="c-update",
        object="tea",
        content="紅茶",
        valid_from=RECORDED,
        source_turn_ids=("turn-3",),
    )
    h.evidence.add_turn(replace(turn("turn-3", actor="user-a"), occurred_at=RECORDED))
    h.gateway.output = ExtractionProposal(
        memories=(updated,),
        relations=(MemoryRelation(from_id=updated.id, to_id=repetition.id, type="updates"),),
    )
    worker.process("char-b", "turn-3")
    state = store.get_extraction("char-b", "turn-3")
    assert state is not None and state.status == "done"
    with store.list_relations("char-b", updated.id) as relations:
        saved_relations = tuple(relations)
    assert ([(v.id, v.object) for v in service.active("char-b")], saved_relations) == (
        [(updated.id, "tea")],
        (MemoryRelation(from_id=updated.id, to_id=original.id, type="updates"),),
    )
