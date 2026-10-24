import json
from dataclasses import replace
from datetime import timedelta

import pytest

from digital_souls_memory.evidence.domain import DeliveryReceipt
from digital_souls_memory.evidence.materials import ReceiptState, aggregate_receipts
from digital_souls_memory.identity.sessions import SessionIdentity
from digital_souls_memory.recall.context import assemble_response, serialize_response, window_span
from digital_souls_memory.recall.domain import CandidateMaterial
from digital_souls_memory.recall.eligibility import memory_allowed, resolve_candidate
from tests.evidence_support import INSTRUCTION, RECORDED, TEXT, receipt, session, turn
from tests.memory_support import memory
from tests.recall_fixtures import anchor_candidate, candidate_for, source_stamp
from tests.voice_support import anchor

pytestmark = pytest.mark.ut
TIMEOUT = timedelta(minutes=10)


@pytest.mark.parametrize("kind", ["fact", "preference"])
@pytest.mark.parametrize(
    "reader,shared,allowed",
    [
        ("char-b", (), True),
        ("char-c", (), False),
        ("char-c", ("char-c",), True),
    ],
)
def test_memory_permission_checks_owner_and_explicit_sharing(
    kind: str,
    reader: str,
    shared: tuple[str, ...],
    allowed: bool,
) -> None:
    value = replace(memory(kind), shared_with=shared)
    assert memory_allowed(value, character_id=reader, now=RECORDED) is allowed


@pytest.mark.parametrize("state", ["expired", "superseded", "future"])
def test_shared_memory_must_still_be_current(state: str) -> None:
    value = memory()
    if state == "expired":
        value = replace(value, valid_to=RECORDED)
    elif state == "superseded":
        value = replace(value, superseded_at=RECORDED)
    else:
        value = replace(value, valid_from=RECORDED + timedelta(seconds=1))
    assert not memory_allowed(value, character_id="char-c", now=RECORDED)


@pytest.mark.parametrize(
    "perceived,addressees,allowed",
    [
        (("char-b", "char-c"), ("user-a",), True),
        (("char-c",), ("char-b",), False),
        ((), ("char-b",), False),
    ],
)
def test_candidate_perception_does_not_follow_the_addressee(
    perceived: tuple[str, ...],
    addressees: tuple[str, ...],
    allowed: bool,
) -> None:
    source = turn(
        actor="user-a", perceived_by=perceived, addressees=addressees, address_mode="direct"
    )
    result = resolve_candidate(
        candidate_for(memory(), sources=(source,)),
        character_id="char-b",
        materials=(
            CandidateMaterial(
                turn=source,
                session=session(reports_delivery=False),
                actor_is_character=False,
                receipt_state=ReceiptState(None, 0),
            ),
        ),
        now=RECORDED,
        delivery_timeout=TIMEOUT,
    )
    assert (result is not None) is allowed


@pytest.mark.parametrize("kind", ["utterance", "thought"])
@pytest.mark.parametrize("reader,allowed", [("char-c", True), ("char-b", False)])
def test_own_whisper_and_thought_are_available_only_to_the_perceiving_character(
    kind: str,
    reader: str,
    allowed: bool,
) -> None:
    source = turn(kind=kind, perceived_by=("char-c",), address_mode="self")
    value = replace(memory(), character_id=reader)
    result = resolve_candidate(
        candidate_for(value, sources=(source,)),
        character_id=reader,
        materials=(
            CandidateMaterial(
                turn=source,
                session=session(),
                actor_is_character=True,
                receipt_state=ReceiptState(None, 0),
            ),
        ),
        now=RECORDED,
        delivery_timeout=TIMEOUT,
    )
    assert (result is not None) is allowed


def test_missing_source_cannot_be_replaced_with_the_derived_key() -> None:
    assert (
        resolve_candidate(
            candidate_for(memory(), sources=(turn(),)),
            character_id="char-b",
            materials=(),
            now=RECORDED,
            delivery_timeout=TIMEOUT,
        )
        is None
    )


@pytest.mark.parametrize(
    "elapsed,receipts,allowed",
    [
        (599, (), False),
        (600, (), True),
        (600, (receipt("partial", 9),), False),
        (600, (receipt("none", 0),), False),
    ],
)
def test_candidate_used_range_must_fit_current_delivery(
    elapsed: int,
    receipts: tuple[DeliveryReceipt, ...],
    allowed: bool,
) -> None:
    candidate = candidate_for(memory(), sources=(turn(),))
    result = resolve_candidate(
        candidate,
        character_id="char-b",
        materials=(
            CandidateMaterial(
                turn=turn(),
                session=session(),
                actor_is_character=True,
                receipt_state=aggregate_receipts(receipts),
            ),
        ),
        now=RECORDED + timedelta(seconds=elapsed),
        delivery_timeout=TIMEOUT,
    )
    assert (result is not None) is allowed


@pytest.mark.parametrize("available_end", [3, 2])
def test_candidate_derived_from_delivered_prefix_returns_only_that_prefix(
    available_end: int,
) -> None:
    source = turn(text="日本語の原文")
    candidate = candidate_for(memory(), sources=(source,))
    candidate = replace(
        candidate,
        dependencies=(
            replace(
                candidate.dependencies[0],
                basis="receipt_confirmed",
                receipt_version=1,
                available_end=3,
                used_end=3,
            ),
        ),
    )
    result = resolve_candidate(
        candidate,
        character_id="char-b",
        materials=(
            CandidateMaterial(
                turn=source,
                session=session(),
                actor_is_character=True,
                receipt_state=aggregate_receipts((receipt("partial", available_end),)),
            ),
        ),
        now=RECORDED,
        delivery_timeout=TIMEOUT,
    )
    if available_end < 3:
        assert result is None
        return
    assert result is not None
    response = assemble_response(
        provision_id="prefix",
        selection=SessionIdentity(
            session_id="session-1",
            character_id="char-b",
            temperament_version=None,
            personality_version=None,
        ),
        candidates=(result,),
        format="materials",
    )
    spans = json.loads(serialize_response(response))["source_spans"]
    assert [(v["start"], v["end"], v["text"]) for v in spans] == [(0, 3, "日本語")]


@pytest.mark.parametrize("reader,allowed", [("char-c", True), ("char-b", False)])
def test_shared_material_applies_delivery_range_for_each_reader(reader: str, allowed: bool) -> None:
    source = turn(text="日本語の原文")
    candidate = candidate_for(
        replace(memory(), character_id="char-c", shared_with=("char-b",)), sources=(source,)
    )
    material = CandidateMaterial(
        turn=source,
        session=session(),
        actor_is_character=True,
        receipt_state=ReceiptState(spoken_until=3, count=4),
    )
    result = resolve_candidate(
        candidate,
        character_id=reader,
        materials=(material,),
        now=RECORDED,
        delivery_timeout=TIMEOUT,
    )
    assert (result is not None) is allowed
    if result is not None:
        assert result.spans[0].text == source.text
        assert result.sources[0].receipt_version == 0


def test_corrected_reply_invalidates_candidate_with_unchanged_supporting_turn() -> None:
    reply = turn(turn_id="reply", session_id="delivery-session", text="質問と機微な末尾")
    source = turn(reply_to_turn_id="reply")
    candidate = candidate_for(memory(), sources=(source,))
    candidate = replace(
        candidate,
        dependencies=(*candidate.dependencies, source_stamp(reply, character_id="char-b")),
    )
    unchanged = CandidateMaterial(
        turn=source,
        session=session(reports_delivery=False),
        actor_is_character=True,
        receipt_state=ReceiptState(None, 0),
    )
    corrected = CandidateMaterial(
        turn=reply,
        session=session("delivery-session"),
        actor_is_character=True,
        receipt_state=aggregate_receipts((replace(receipt("partial", 2), turn_id="reply"),)),
    )
    assert (
        resolve_candidate(
            candidate,
            character_id="char-b",
            materials=(unchanged, corrected),
            now=RECORDED,
            delivery_timeout=TIMEOUT,
        )
        is None
    )


@pytest.mark.parametrize("form", ["materials", "assembled"])
def test_original_and_anchor_override_a_conflicting_derived_key(form: str) -> None:
    source = turn()
    value = replace(memory(), content="The meeting is on the wrong date")
    original_memory = replace(value)
    materials = (
        CandidateMaterial(
            turn=source,
            session=session(reports_delivery=False),
            actor_is_character=True,
            receipt_state=ReceiptState(None, 0),
        ),
    )
    candidates = (
        candidate_for(value, sources=(source,)),
        anchor_candidate(replace(anchor(), character_id="char-b"), source=source),
    )
    resolved = tuple(
        resolve_candidate(
            v, character_id="char-b", materials=materials, now=RECORDED, delivery_timeout=TIMEOUT
        )
        for v in candidates
    )
    present = tuple(v for v in resolved if v is not None)
    assert len(present) == len(candidates)
    response = assemble_response(
        provision_id="synthetic",
        selection=SessionIdentity(
            session_id="session-1",
            character_id="char-b",
            temperament_version=None,
            personality_version=None,
        ),
        candidates=present,
        format=form,
    )
    body = json.loads(serialize_response(response))
    assert {
        (s["turn_id"], s["start"], s["end"], s["text"], s["actor"], s["language"])
        for s in body["source_spans"]
    } == {("turn-1", 0, len(TEXT), TEXT, "char-c", "ja")}
    assert [v["text"] for v in body["verbatim_anchors"]] == [TEXT]
    if form == "assembled":
        assert TEXT in body["context"]
        assert value.content not in body["context"]
    assert value == original_memory


def test_window_uses_unicode_positions_and_keeps_the_matching_original_slice() -> None:
    from digital_souls_memory.evidence.provision import SourceSpan

    text = "前置き🙂e\u0301" * 10 + "11月3日" + "終わり" * 10
    span = SourceSpan(
        turn_id="turn-1",
        start=7,
        end=7 + len(text),
        text=text,
        actor="char-c",
        language="ja",
        truncated=False,
        original_start=7,
        original_end=7 + len(text),
    )
    result = window_span(span, query="11月3日", width=12)
    assert "11月3日" in result.text
    assert result.text == text[result.start - 7 : result.end - 7]
    assert result.end - result.start <= 12
    assert (result.original_start, result.original_end) == (7, 7 + len(text))
    assert result.truncated is True
    assert (result.actor, result.language, result.turn_id) == ("char-c", "ja", "turn-1")


def test_window_keeps_match_after_characters_with_expanding_case_folds() -> None:
    from digital_souls_memory.evidence.provision import SourceSpan

    text = "Straße" * 20 + "約束" + "末尾" * 20
    span = SourceSpan(
        turn_id="unicode",
        start=0,
        end=len(text),
        text=text,
        actor="char-c",
        language="de",
        truncated=False,
        original_start=0,
        original_end=len(text),
    )
    result = window_span(span, query="約束", width=8)
    assert "約束" in result.text
    assert result.text == text[result.start : result.end]


@pytest.mark.parametrize(
    "answer,query,width,match",
    [
        ("blue station: November 3.", "BLUE STATION?", 100, "blue station: November 3."),
        ("Straße station: November 3.", "STRASSE STATION?", 100, "Straße station: November 3."),
        ("ß.", "SS?", 1, "ß"),
        ("ßa.", "SA?", 1, "ßa"),
    ],
)
def test_partial_casefold_match_returns_original_codepoint_boundaries(
    answer: str, query: str, width: int, match: str
) -> None:
    from digital_souls_memory.evidence.provision import SourceSpan

    text = "雑記。" * 30 + answer + "雑記。" * 30
    span = SourceSpan(
        turn_id="unicode",
        start=7,
        end=7 + len(text),
        text=text,
        actor="char-c",
        language="de",
        truncated=False,
        original_start=7,
        original_end=7 + len(text),
    )
    result = window_span(span, query=query, width=width)
    assert match in result.text
    assert result.text == text[result.start - 7 : result.end - 7]
    assert result.end - result.start <= max(width, len(match))
    assert (result.original_start, result.original_end) == (7, 7 + len(text))
    assert (result.turn_id, result.actor, result.language) == ("unicode", "char-c", "de")


def test_window_preserves_an_entire_full_match_even_below_its_width() -> None:
    from digital_souls_memory.recall.windowing import window_bounds

    text = "前置き。" * 10 + "Straße station" + "後置き。" * 10
    start, end = window_bounds(text, "STRASSE STATION", 1)
    assert text[start:end] == "Straße station"


@pytest.mark.parametrize("kind", ["fact", "commitment"])
def test_resolving_and_assembling_instruction_text_does_not_reinforce_or_promote(kind: str) -> None:
    source = turn(text=INSTRUCTION)
    value = replace(memory(kind), content=INSTRUCTION)
    before = replace(value)
    candidate = candidate_for(value, sources=(source,))
    selection = SessionIdentity(
        session_id="session-1", character_id="char-b", temperament_version=1, personality_version=1
    )
    for _ in range(3):
        resolved = resolve_candidate(
            candidate,
            character_id="char-b",
            materials=(
                CandidateMaterial(
                    turn=source,
                    session=session(reports_delivery=False),
                    actor_is_character=True,
                    receipt_state=ReceiptState(None, 0),
                ),
            ),
            now=RECORDED,
            delivery_timeout=TIMEOUT,
        )
        assert resolved is not None
        body = json.loads(
            serialize_response(
                assemble_response(
                    provision_id="synthetic",
                    selection=selection,
                    candidates=(resolved,),
                    format="assembled",
                )
            )
        )
        assert [s["text"] for s in body["source_spans"]] == [INSTRUCTION]
        assert (body["temperament_version"], body["personality_version"]) == (1, 1)
        assert (value.last_reinforced_at, value.source_turn_ids, value.epistemics) == (
            before.last_reinforced_at,
            before.source_turn_ids,
            before.epistemics,
        )
        assert value == before
