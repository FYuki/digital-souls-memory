from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from typing import cast

import pytest

from digital_souls_memory.adapters.identity_characters import IdentityCharacters
from digital_souls_memory.evidence.materials import EvidenceMaterials
from digital_souls_memory.identity.use_cases import IdentityService
from digital_souls_memory.voice.domain import VerbatimAnchor, VoiceExemplar
from digital_souls_memory.voice.ports import VoiceNotFound, VoiceUnavailable
from digital_souls_memory.voice.use_cases import VoiceService
from tests.evidence_support import session, turn
from tests.identity_support import REGISTERED, aspects
from tests.it1.evidence_fakes import FakeEvidenceStore
from tests.it1.identity_fakes import FakeIdentityStore
from tests.it1.voice_fakes import FakeVoiceStore, VoiceGateway, VoiceReviewer
from tests.voice_support import INSTRUCTION, KINDS, TEXT, anchor, exemplar

pytestmark = pytest.mark.ut


@pytest.mark.parametrize("approved_by", [None, "", " \t\n"])
@pytest.mark.parametrize("factory", [exemplar, anchor])
def test_approval_requires_a_nonblank_author(
    factory: Callable[[], VoiceExemplar | VerbatimAnchor],
    approved_by: object,
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
) -> None:
    with rejected():
        replace(factory(), approved_by=cast(str, approved_by))


def test_exemplar_preserves_original_language_whitespace_and_register() -> None:
    value = exemplar()
    assert value.text == TEXT
    assert tuple(value.situation_tags) == ("約束",)
    assert tuple(value.emotion_tags) == ("穏やか",)
    assert dict(value.register) == {"first_person": "私", "politeness": "敬語"}
    assert value.approved_by == "synthetic-author"
    assert value.trait_version == 1


@pytest.mark.parametrize(
    "source,turn_id",
    [("unknown", None), ("canonical_seed", "turn-1"), ("turn_id", None), ("turn_id", " ")],
)
def test_source_and_turn_reference_must_agree(
    source: str,
    turn_id: str | None,
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
) -> None:
    with rejected():
        replace(exemplar(), source=source, turn_id=turn_id)


def test_turn_source_retains_its_reference() -> None:
    value = replace(exemplar(), source="turn_id", turn_id="turn-1")
    assert (value.source, value.turn_id) == ("turn_id", "turn-1")


@pytest.mark.parametrize("kind", KINDS)
def test_anchor_keeps_kind_turn_reason_and_approval(kind: str) -> None:
    value = replace(anchor(), kind=kind)
    assert value.kind == kind
    assert value.turn_id == "turn-1"
    assert value.reason == "作者が選んだ約束の原文"
    assert value.approved_by == "synthetic-author"


@pytest.mark.parametrize("kind", ["unknown", "", "Commitment"])
def test_anchor_rejects_unknown_kind(
    kind: str,
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
) -> None:
    with rejected():
        replace(anchor(), kind=kind)


def test_instruction_text_is_preserved_as_data() -> None:
    value = replace(exemplar(), text=INSTRUCTION)
    assert value.text == INSTRUCTION
    assert value.source == "canonical_seed"
    assert value.approved_by == "synthetic-author"


@pytest.fixture
def voice_service() -> tuple[VoiceService, FakeIdentityStore, VoiceGateway, VoiceReviewer]:
    identity = FakeIdentityStore()
    gateway, reviewer = VoiceGateway(), VoiceReviewer()
    seed = IdentityService(identity, lambda: REGISTERED, gateway, reviewer)
    seed.register_temperament("char-a", "落ち着いて応答する", source="registration")
    seed.author_seed("char-a", aspects(), "synthetic-v1", source="registration")
    evidence = FakeEvidenceStore()
    evidence.add_session(session(reports_delivery=False))
    evidence.add_turn(turn(text=TEXT, actor="char-a", perceived_by=("char-a",)))
    materials = EvidenceMaterials(
        evidence, IdentityCharacters(lambda: identity), lambda: REGISTERED, timedelta(minutes=10)
    )
    return (
        VoiceService(FakeVoiceStore(), identity, materials, gateway, reviewer),
        identity,
        gateway,
        reviewer,
    )


@pytest.mark.parametrize("text", ["私は約束を守る。", TEXT.replace("e\u0301", "é")])
def test_service_rejects_changed_original_without_publishing(
    voice_service: tuple[VoiceService, FakeIdentityStore, VoiceGateway, VoiceReviewer], text: str
) -> None:
    service, _, gateway, _ = voice_service
    with pytest.raises(ValueError):
        service.register_exemplars(
            "char-a", [replace(exemplar(), source="turn_id", turn_id="turn-1", text=text)]
        )
    assert service.list_exemplars("char-a") == []
    assert gateway.voice_calls == []


def test_service_rejects_material_outside_perception(
    voice_service: tuple[VoiceService, FakeIdentityStore, VoiceGateway, VoiceReviewer],
) -> None:
    service, _, _, _ = voice_service
    with pytest.raises(VoiceNotFound):
        service.register_anchor(replace(anchor(), character_id="char-b"))
    assert service.list_anchors("char-b") == []
    with pytest.raises(VoiceNotFound):
        service.get_anchor("char-b", "anchor-1")


@pytest.mark.parametrize("consistent", [False, True])
def test_service_reviews_small_initial_set_once_and_preserves_personality(
    voice_service: tuple[VoiceService, FakeIdentityStore, VoiceGateway, VoiceReviewer],
    consistent: bool,
) -> None:
    from digital_souls_memory.identity.ports import ReviewResult

    service, identity, gateway, reviewer = voice_service
    reviewer.voice_result = ReviewResult(consistent, "合成審査")
    before = identity.get_current("char-a", REGISTERED)
    assert service.register_exemplars("char-a", [exemplar()]) == reviewer.voice_result
    assert service.get_exemplar("char-a", "voice-1") == exemplar()
    service.register_exemplars("char-a", [replace(exemplar(), exemplar_id="second")])
    assert len(service.list_exemplars("char-a")) == 2
    assert len(gateway.voice_calls) == len(reviewer.voice_calls) == 1
    assert reviewer.voice_calls[0][2:] == ((TEXT,), gateway.responses)
    assert identity.get_current("char-a", REGISTERED) == before
    service.register_anchor(anchor())
    assert service.get_anchor("char-a", "anchor-1") == (anchor(), TEXT)
    assert service.list_anchors("char-a") == [(anchor(), TEXT)]


@pytest.mark.parametrize("missing", ["gateway", "reviewer"])
def test_service_missing_model_port_leaves_no_completed_registration(
    voice_service: tuple[VoiceService, FakeIdentityStore, VoiceGateway, VoiceReviewer],
    missing: str,
) -> None:
    service, identity, _, _ = voice_service
    before = identity.get_current("char-a", REGISTERED)
    if missing == "gateway":
        service.gateway = None
    else:
        service.reviewer = None
    with pytest.raises(VoiceUnavailable):
        service.register_exemplars("char-a", [exemplar()])
    assert service.list_exemplars("char-a") == []
    assert identity.get_current("char-a", REGISTERED) == before
