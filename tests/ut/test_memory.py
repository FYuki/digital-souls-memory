from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from typing import cast

import pytest

from digital_souls_memory.memory.changes import reinforce, supersede, update_fact
from digital_souls_memory.memory.domain import Derivation, MemoryRelation
from tests.evidence_support import OCCURRED, RECORDED, turn
from tests.memory_support import KINDS, NEW_ID, OLD_ID, features, memory

pytestmark = pytest.mark.ut


@pytest.mark.parametrize("kind", KINDS)
def test_every_kind_requires_nonempty_sources(
    kind: str, rejected: Callable[[], pytest.RaisesExc[BaseException]]
) -> None:
    with rejected():
        replace(memory(kind), source_turn_ids=())


@pytest.mark.parametrize("field", ["model", "prompt_version", "created_at"])
@pytest.mark.parametrize("invalid", [None, "", " \t"])
def test_derivation_requires_model_prompt_and_creation_time(
    field: str, invalid: object, rejected: Callable[[], pytest.RaisesExc[BaseException]]
) -> None:
    with rejected():
        if field == "created_at":
            replace(memory().derivation, created_at=cast(datetime, invalid))
        elif field == "model":
            replace(memory().derivation, model=cast(str, invalid))
        else:
            replace(memory().derivation, prompt_version=cast(str, invalid))


@pytest.mark.parametrize("kind", KINDS)
def test_every_kind_requires_derivation(
    kind: str, rejected: Callable[[], pytest.RaisesExc[BaseException]]
) -> None:
    with rejected():
        replace(memory(kind), derivation=cast(Derivation, None))


@pytest.mark.parametrize("invalid", [None, "", " \n"])
def test_commitment_requires_verbatim_turn(
    invalid: object, rejected: Callable[[], pytest.RaisesExc[BaseException]]
) -> None:
    with rejected():
        replace(memory("commitment"), verbatim_turn_id=cast(str, invalid))


def test_relationship_preserves_affection_and_trust_as_separate_values() -> None:
    value = memory("relationship")
    assert (value.counterpart, value.affection, value.trust, value.status) == (
        "user-a",
        0.8,
        0.2,
        "friend",
    )


@pytest.mark.parametrize("source_type", ["direct_user", "observed", "inferred", "reflection"])
def test_epistemics_retains_source_type_and_features(source_type: str) -> None:
    value = replace(memory().epistemics, source_type=source_type)
    assert value.source_type == source_type
    assert value.confidence_features == features()
    assert value.independent_evidence_count == 1
    assert value.contradiction_ids == ()


@pytest.mark.parametrize("trust_tier", tuple("ABCDEF"))
def test_epistemics_retains_trust_tier(trust_tier: str) -> None:
    assert replace(memory().epistemics, trust_tier=trust_tier).trust_tier == trust_tier


@pytest.mark.parametrize("relation_type", ["updates", "extends", "derives", "contradicts"])
def test_relations_preserve_both_endpoints_and_type(relation_type: str) -> None:
    relation = MemoryRelation(from_id=NEW_ID, to_id=OLD_ID, type=relation_type)
    assert (relation.from_id, relation.to_id, relation.type) == (NEW_ID, OLD_ID, relation_type)


def test_identity_links_retain_values_and_relationships() -> None:
    links = memory().identity_links
    assert (links.self_relevance, links.value_links, links.relationship_links) == (
        0.4,
        ("誠実",),
        ("user-a",),
    )


def test_fact_update_closes_old_interval_and_creates_distinct_new_fact() -> None:
    old = memory()
    at = OCCURRED + timedelta(days=1)
    candidate = replace(
        old,
        id=NEW_ID,
        content="利用者Aは紅茶を好む",
        object="tea",
        source_turn_ids=("turn-2",),
        valid_from=at,
    )
    closed, added = update_fact(old, candidate)
    assert (closed.id, closed.content, closed.source_turn_ids) == (
        OLD_ID,
        old.content,
        old.source_turn_ids,
    )
    assert closed.valid_to == at
    assert closed.last_reinforced_at is None
    assert added.id != closed.id
    assert (added.content, added.object, added.valid_from, added.valid_to) == (
        candidate.content,
        "tea",
        at,
        None,
    )
    assert added.recorded_at == RECORDED


def test_fact_update_rejects_identifier_collision_without_changing_old_fact(
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
) -> None:
    old = memory()
    candidate = replace(
        old,
        content="利用者Aは紅茶を好む",
        object="tea",
        valid_from=OCCURRED + timedelta(days=1),
        source_turn_ids=("turn-2",),
    )
    with rejected():
        update_fact(old, candidate)
    assert (old.id, old.content, old.valid_to, old.source_turn_ids) == (
        OLD_ID,
        "利用者Aはコーヒーを好む",
        None,
        ("turn-1",),
    )


@pytest.mark.parametrize(
    "actor,kind", [("char-b", "utterance"), ("user-a", "utterance"), ("sensor", "observation")]
)
@pytest.mark.parametrize("relation_type", ["same_fact", "extends"])
def test_restatement_uses_event_time_for_self_other_and_observation(
    actor: str, kind: str, relation_type: str
) -> None:
    source = replace(
        turn(turn_id="turn-2", actor=actor, kind=kind),
        occurred_at=OCCURRED + timedelta(hours=1),
    )
    result = reinforce(memory(), source, relation_type=relation_type)
    assert result.last_reinforced_at == source.occurred_at
    assert set(result.source_turn_ids) == {"turn-1", "turn-2"}
    assert (result.epistemics.source_type, result.epistemics.trust_tier) == ("inferred", "D")


def test_old_and_duplicate_evidence_do_not_rewind_or_multiply_reinforcement() -> None:
    value = replace(memory(), last_reinforced_at=OCCURRED + timedelta(hours=1))
    source = replace(turn(turn_id="turn-2"), occurred_at=OCCURRED - timedelta(days=1))
    once = reinforce(value, source, relation_type="extends")
    twice = reinforce(once, source, relation_type="extends")
    assert twice.last_reinforced_at == value.last_reinforced_at
    assert tuple(twice.source_turn_ids).count("turn-2") == 1
    assert twice.epistemics.independent_evidence_count == 2
    assert twice.epistemics.source_type == "inferred"
    assert twice.epistemics.trust_tier == "D"


@pytest.mark.parametrize("relation_type", ["updates", "derives", "contradicts"])
def test_other_relations_do_not_reinforce(relation_type: str) -> None:
    value = memory()
    result = reinforce(value, turn(turn_id="turn-2"), relation_type=relation_type)
    assert result.last_reinforced_at == value.last_reinforced_at
    assert result.source_turn_ids == value.source_turn_ids


def test_commitment_is_not_reinforced() -> None:
    value = memory("commitment")
    result = reinforce(value, turn(turn_id="turn-2"), relation_type="extends")
    assert result.last_reinforced_at is None
    assert result.source_turn_ids == value.source_turn_ids
    assert (result.verbatim_turn_id, result.deadline) == ("turn-1", RECORDED)


def test_original_contradiction_supersedes_derived_content() -> None:
    value = memory()
    result = supersede(value, at=RECORDED)
    assert result.superseded_at == RECORDED
    assert result.content == value.content
    assert result.source_turn_ids == value.source_turn_ids


def test_confidence_depends_on_evidence_features_beyond_model_self_assessment() -> None:
    high = features()
    low = replace(high, retrieval=0.1, source=0.1, temporal=0.1, corroboration=0.0)
    assert high.self_assessment == low.self_assessment
    assert low.score() < high.score()


def test_cloud_input_rejects_local_material_type() -> None:
    from digital_souls_memory.memory.domain import MemoryValidationError
    from digital_souls_memory.memory.extraction import CloudInput, LocalTurn, PublicTurn

    local = LocalTurn(
        turn_id="turn-1",
        text="合成機微原文",
        actor="char-c",
        kind="utterance",
        addressees=(),
        address_mode="broadcast",
        reply_to_turn_id=None,
        occurred_at=OCCURRED,
    )
    with pytest.raises(MemoryValidationError):
        CloudInput(character_id="char-b", turns=cast(tuple[PublicTurn, ...], (local,)))
