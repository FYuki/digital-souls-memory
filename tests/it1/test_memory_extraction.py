from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from typing import cast

import pytest

from digital_souls_memory.adapters.identity_characters import IdentityCharacters
from digital_souls_memory.evidence.materials import EvidenceMaterials
from digital_souls_memory.memory.domain import DerivedMemory, ExtractionProposal
from digital_souls_memory.memory.extraction import (
    CloudInput,
    LocalOnly,
    MemoryExtractor,
    PublicTurn,
)
from tests.evidence_support import INSTRUCTION, RECORDED, TEXT, receipt, session, turn
from tests.it1.evidence_fakes import FakeEvidenceStore, registered_identities
from tests.memory_support import memory

pytestmark = pytest.mark.it1


class RecordingGateway:
    def __init__(self) -> None:
        self.local_calls: list[LocalOnly] = []
        self.cloud_calls: list[CloudInput] = []
        self.output: tuple[DerivedMemory, ...] | ExtractionProposal = (memory(),)
        self.fail = False

    def extract_local(self, value: LocalOnly) -> tuple[DerivedMemory, ...] | ExtractionProposal:
        self.local_calls.append(value)
        if self.fail:
            raise RuntimeError("Synthetic gateway unavailable")
        return self.output

    def extract_cloud(self, value: CloudInput) -> tuple[DerivedMemory, ...] | ExtractionProposal:
        self.cloud_calls.append(value)
        if self.fail:
            raise RuntimeError("Synthetic gateway unavailable")
        return self.output


class Harness:
    def __init__(self, *, reports_delivery: bool = True) -> None:
        self.now = RECORDED
        self.evidence = FakeEvidenceStore()
        self.identity = registered_identities()
        self.gateway = RecordingGateway()
        self.evidence.add_session(session(reports_delivery=reports_delivery))
        self.materials = EvidenceMaterials(
            self.evidence,
            IdentityCharacters(lambda: self.identity),
            lambda: self.now,
            timedelta(minutes=10),
        )
        self.extractor = MemoryExtractor(
            evidence=self.evidence,
            materials=self.materials,
            gateway=self.gateway,
            non_sensitive_labels=frozenset({"synthetic-public"}),
            conversation_surfaces=frozenset({"synthetic-chat"}),
        )


@pytest.mark.parametrize("kind", ["fact", "preference"])
def test_observation_exception_does_not_bypass_conversation_subject_or_preference(
    kind: str,
) -> None:
    h = Harness(reports_delivery=False)
    h.evidence.add_turn(turn(kind="observation"))
    h.evidence.add_turn(turn(turn_id="turn-2", actor="user-a"))
    h.gateway.output = (replace(memory(kind), subject="B", source_turn_ids=("turn-1",)),)
    assert bool(h.extractor.propose(character_id="char-b", turn_ids=("turn-1",)).memories) == (
        kind == "fact"
    )
    h.gateway.output = (replace(memory(kind), subject="B", source_turn_ids=("turn-1", "turn-2")),)
    assert h.extractor.propose(character_id="char-b", turn_ids=("turn-1", "turn-2")).memories == ()


@pytest.mark.parametrize("status,end", [("partial", 9), ("none", 0), ("full", len(TEXT))])
@pytest.mark.parametrize("reader", ["char-b", "char-c"])
def test_gateway_receives_only_delivered_unicode_prefix_except_for_speaker(
    status: str, end: int, reader: str
) -> None:
    h = Harness()
    h.evidence.add_turn(turn())
    h.evidence.add_receipt(receipt(status, end))
    # No candidate is needed to observe the actual text crossing the model boundary.
    h.gateway.output = ()
    h.extractor.propose(character_id=reader, turn_ids=("turn-1",))
    expected = TEXT if reader == "char-c" else TEXT[:end]
    if expected:
        assert len(h.gateway.local_calls) == 1
        assert [value.text for value in h.gateway.local_calls[0].turns] == [expected]
    else:
        assert h.gateway.local_calls == []
    assert h.gateway.cloud_calls == []
    assert h.evidence.get_turn("turn-1") == turn()


@pytest.mark.parametrize("delivery", [False, True])
def test_thought_never_reaches_another_character_or_waits_for_receipt(delivery: bool) -> None:
    h = Harness(reports_delivery=delivery)
    h.evidence.add_turn(turn(kind="thought", perceived_by=("char-c",), address_mode="self"))
    h.gateway.output = ()
    assert h.extractor.propose(character_id="char-b", turn_ids=("turn-1",)).memories == ()
    assert h.gateway.local_calls == []
    assert h.gateway.cloud_calls == []
    h.extractor.propose(character_id="char-c", turn_ids=("turn-1",))
    assert [value.text for value in h.gateway.local_calls[0].turns] == [TEXT]


@pytest.mark.parametrize(
    "actor,kind", [("char-c", "utterance"), ("viewer", "utterance"), ("char-c", "observation")]
)
def test_declaration_allows_immediate_extraction_without_advancing_clock(
    actor: str, kind: str
) -> None:
    h = Harness(reports_delivery=False)
    h.evidence.add_turn(turn(actor=actor, kind=kind))
    h.gateway.output = ()
    h.extractor.propose(character_id="char-b", turn_ids=("turn-1",))
    assert [value.text for value in h.gateway.local_calls[0].turns] == [TEXT]
    assert h.now == RECORDED


def test_pending_then_timeout_then_late_receipt_changes_model_input_in_same_instance() -> None:
    h = Harness()
    h.evidence.add_turn(turn())
    h.gateway.output = ()
    for elapsed in (0, 599):
        h.now = RECORDED + timedelta(seconds=elapsed)
        assert h.extractor.propose(character_id="char-b", turn_ids=("turn-1",)).memories == ()
        assert h.gateway.local_calls == []
    h.now = RECORDED + timedelta(seconds=600)
    h.extractor.propose(character_id="char-b", turn_ids=("turn-1",))
    assert [value.text for value in h.gateway.local_calls[-1].turns] == [TEXT]
    h.evidence.add_receipt(receipt("partial", 9))
    h.extractor.propose(character_id="char-b", turn_ids=("turn-1",))
    assert [value.text for value in h.gateway.local_calls[-1].turns] == [TEXT[:9]]
    call_count = len(h.gateway.local_calls)
    h.evidence.add_receipt(receipt("none", 0))
    assert h.extractor.propose(character_id="char-b", turn_ids=("turn-1",)).memories == ()
    assert len(h.gateway.local_calls) == call_count


@pytest.mark.parametrize("targets,mode", [((), "broadcast"), (("char-a",), "direct")])
def test_addressees_do_not_grant_perception(targets: tuple[str, ...], mode: str) -> None:
    h = Harness(reports_delivery=False)
    h.evidence.add_turn(turn(addressees=targets, address_mode=mode))
    assert h.extractor.propose(character_id="char-a", turn_ids=("turn-1",)).memories == ()
    assert h.gateway.local_calls == []
    assert h.gateway.cloud_calls == []


@pytest.mark.parametrize(
    "labels,local",
    [
        (("synthetic-public",), False),
        (("synthetic-sensitive",), True),
        (("unmapped-label",), True),
        (("synthetic-public", "synthetic-sensitive"), True),
    ],
)
def test_sensitive_unknown_and_mixed_input_use_local_type(
    labels: tuple[str, ...], local: bool
) -> None:
    h = Harness(reports_delivery=False)
    h.gateway.output = ()
    ids = tuple(f"turn-{i}" for i in range(len(labels)))
    for identifier, label in zip(ids, labels, strict=True):
        h.evidence.add_turn(turn(turn_id=identifier, sensitivity=label))
    h.extractor.propose(character_id="char-b", turn_ids=ids)
    if local:
        assert h.gateway.cloud_calls == []
        assert len(h.gateway.local_calls) == 1
        assert isinstance(h.gateway.local_calls[0], LocalOnly)
        assert [value.text for value in h.gateway.local_calls[0].turns] == [TEXT] * len(ids)
    else:
        assert h.gateway.local_calls == []
        assert len(h.gateway.cloud_calls) == 1
        assert isinstance(h.gateway.cloud_calls[0], CloudInput)


def test_gate_rejects_candidate_with_no_evidence_confidence() -> None:
    h = Harness(reports_delivery=False)
    h.evidence.add_turn(turn())
    value = memory()
    low = replace(
        value.epistemics.confidence_features,
        retrieval=0.0,
        source=0.0,
        contradiction=1.0,
        temporal=0.0,
        corroboration=0.0,
        self_assessment=1.0,
    )
    h.gateway.output = (
        replace(
            value, subject="char-c", epistemics=replace(value.epistemics, confidence_features=low)
        ),
    )
    assert h.extractor.propose(character_id="char-b", turn_ids=("turn-1",)).memories == ()
    assert len(h.gateway.local_calls) == 1
    assert len(h.evidence.turns) == 1


def test_candidate_with_even_one_unperceived_source_is_rejected_as_a_whole(
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
) -> None:
    h = Harness(reports_delivery=False)
    h.evidence.add_turn(turn())
    h.evidence.add_turn(turn(turn_id="private", perceived_by=("char-c",)))
    h.gateway.output = (replace(memory(), source_turn_ids=("turn-1", "private")),)
    with rejected():
        h.extractor.propose(character_id="char-b", turn_ids=("turn-1",))


def test_self_speech_keeps_fact_but_excludes_relationship_and_commitment() -> None:
    h = Harness(reports_delivery=False)
    h.evidence.add_turn(turn(address_mode="self"))
    fact = replace(memory(), subject="char-c")
    h.gateway.output = (
        fact,
        replace(memory("relationship"), id="00000000-0000-4000-8000-000000000002"),
        replace(memory("commitment"), id="00000000-0000-4000-8000-000000000003"),
    )
    result = h.extractor.propose(character_id="char-b", turn_ids=("turn-1",)).memories
    assert result == (fact,)


def test_direct_target_is_not_replaced_by_another_perceiver() -> None:
    h = Harness(reports_delivery=False)
    h.evidence.add_turn(turn(address_mode="direct", addressees=("user-a",)))
    h.gateway.output = (memory("relationship"),)
    result = h.extractor.propose(character_id="char-b", turn_ids=("turn-1",)).memories
    assert len(result) == 1
    assert result[0].counterpart == "user-a"


def test_instruction_text_stays_data_and_does_not_change_identity() -> None:
    h = Harness(reports_delivery=False)
    h.evidence.add_turn(turn(text=INSTRUCTION))
    before = h.identity.get_current("char-b", RECORDED)
    h.gateway.output = (replace(memory(), subject="char-c", content=INSTRUCTION),)
    h.extractor.propose(character_id="char-b", turn_ids=("turn-1",))
    assert h.identity.get_current("char-b", RECORDED) == before
    assert h.evidence.get_turn("turn-1") == turn(text=INSTRUCTION)
    assert h.gateway.local_calls[0].turns[0].text == INSTRUCTION


def test_gateway_failure_is_visible_and_retry_uses_original_evidence() -> None:
    h = Harness(reports_delivery=False)
    source = turn(address_mode="direct", addressees=("user-a",))
    h.evidence.add_turn(source)
    h.gateway.fail = True
    with pytest.raises(RuntimeError):
        h.extractor.propose(character_id="char-b", turn_ids=("turn-1",))
    h.gateway.fail = False
    assert h.extractor.propose(character_id="char-b", turn_ids=("turn-1",)).memories == (memory(),)
    assert h.evidence.get_turn("turn-1") == source


def test_local_original_cannot_be_rewrapped_as_cloud_input(
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
) -> None:
    h = Harness(reports_delivery=False)
    h.evidence.add_turn(turn())
    h.gateway.output = ()
    h.extractor.propose(character_id="char-b", turn_ids=("turn-1",))
    local = h.gateway.local_calls[0]
    with rejected():
        CloudInput(character_id=local.character_id, turns=cast(tuple[PublicTurn, ...], local.turns))
    assert h.gateway.cloud_calls == []


def test_parallel_reply_keeps_referenced_user_instead_of_most_recent_speaker() -> None:
    h = Harness(reports_delivery=False)
    h.evidence.add_turn(turn(turn_id="user-a-turn", actor="user-a"))
    h.evidence.add_turn(turn(turn_id="user-z-turn", actor="user-z"))
    h.evidence.add_turn(
        turn(address_mode="direct", addressees=("user-a",), reply_to_turn_id="user-a-turn")
    )
    h.gateway.output = (memory("relationship"),)
    result = h.extractor.propose(character_id="char-b", turn_ids=("turn-1",)).memories
    assert [v.counterpart for v in result] == ["user-a"]
    sent = h.gateway.local_calls[0].turns[0]
    assert sent.reply_to_turn_id == "user-a-turn"
    assert sent.addressees == ("user-a",)
    assert sent.actor == "char-c"


def test_viewer_comment_requires_this_characters_registered_reply_without_deleting_ledger() -> None:
    h = Harness(reports_delivery=False)
    extractor = MemoryExtractor(
        evidence=h.evidence,
        materials=h.materials,
        gateway=h.gateway,
        non_sensitive_labels=frozenset({"synthetic-public"}),
        conversation_surfaces=frozenset(),
        comment_surfaces=frozenset({"synthetic-chat"}),
    )
    h.evidence.add_turn(turn(actor="user-a"))
    assert extractor.propose(character_id="char-b", turn_ids=("turn-1",)).memories == ()
    assert h.gateway.local_calls == []
    h.evidence.add_turn(
        turn(
            turn_id="other-reply",
            actor="char-c",
            address_mode="direct",
            addressees=("user-a",),
            reply_to_turn_id="turn-1",
        )
    )
    assert extractor.propose(character_id="char-b", turn_ids=("turn-1",)).memories == ()
    assert h.gateway.local_calls == []
    h.evidence.add_turn(
        turn(
            turn_id="own-reply",
            actor="char-b",
            address_mode="direct",
            addressees=("user-a",),
            reply_to_turn_id="turn-1",
        )
    )
    assert extractor.propose(character_id="char-b", turn_ids=("turn-1",)).memories == (memory(),)
    assert len(h.evidence.turns) == 3
    assert h.evidence.get_turn("turn-1") == turn(actor="user-a")


@pytest.mark.parametrize("kind", ["fact", "preference", "relationship", "commitment"])
@pytest.mark.parametrize("target,accepted", [("user-a", True), ("unrelated-user", False)])
def test_direct_candidates_require_actor_or_addressee_for_each_targeted_kind(
    kind: str,
    target: str,
    accepted: bool,
) -> None:
    h = Harness(reports_delivery=False)
    h.evidence.add_turn(turn(address_mode="direct", addressees=("user-a",)))
    candidate = memory(kind)
    if kind in ("fact", "preference"):
        candidate = replace(candidate, subject=target)
    else:
        candidate = replace(candidate, counterpart=target)
    h.gateway.output = (candidate,)
    result = h.extractor.propose(character_id="char-b", turn_ids=("turn-1",))
    assert result.memories == ((candidate,) if accepted else ())
