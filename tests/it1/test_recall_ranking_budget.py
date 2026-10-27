import json
from dataclasses import asdict, replace
from typing import TYPE_CHECKING
from uuid import UUID

import pytest

from tests.evidence_support import TEXT, turn
from tests.it1.test_recall import request
from tests.memory_support import memory

if TYPE_CHECKING:
    from tests.recall_support import RecallHarness

pytestmark = pytest.mark.it1


def counted_response(h: "RecallHarness", body: dict[str, object]) -> str:
    for payload in h.counter.payloads:
        try:
            observed = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if observed == body:
            return payload
    raise AssertionError("The complete response was not passed to the tokenizer")


def test_provision_scores_keep_candidate_references_and_all_six_terms_before_rejection(
    harness: "RecallHarness",
) -> None:
    from digital_souls_memory.recall.scoring import trust_score
    from tests.recall_fixtures import candidate_for

    h = harness
    source = turn()
    h.evidence.add_turn(source)
    h.now = source.occurred_at
    first = candidate_for(memory(), sources=(source,), semantic=0.2, lexical=0.7)
    second_memory = replace(memory(), id=str(UUID(int=99)))
    second = candidate_for(second_memory, sources=(source,), semantic=0.8, lexical=0.1)
    h.retriever.values = [first, second]
    h.reranker.scores = {first.ref: 2.5, second.ref: -2.001}

    response = h.client.post("/characters/char-b/recall", json=request())

    assert response.is_success
    assert [v["id"] for v in response.json()["memories"]] == [first.ref.memory_id]
    record = h.provisions.get(response.json()["provision_id"])
    assert record is not None
    assert {"used_by_core", "generation_model", "prompt_template"}.isdisjoint(asdict(record))
    scores = {score.ref: score for score in record.scores}
    assert set(scores) == {first.ref, second.ref}
    for candidate, value in ((first, memory()), (second, second_memory)):
        score = scores[candidate.ref]
        expected_trust = trust_score(value.epistemics)
        assert score.semantic == pytest.approx(candidate.semantic)
        assert score.lexical == pytest.approx(candidate.lexical)
        assert score.recency == pytest.approx(1.0)
        assert score.importance == pytest.approx(0.5)
        assert score.self_relevance == pytest.approx(0.4)
        assert score.trust == pytest.approx(expected_trust)
        assert score.composite == pytest.approx(
            0.55 * candidate.semantic
            + 0.25 * candidate.lexical
            + 0.05 * (1 + 0.5 + 0.4 + expected_trust)
        )
        assert score.relevance == h.reranker.scores[candidate.ref]


def test_reranker_receives_composite_top_twenty_and_applies_raw_relevance_threshold_and_ties(
    harness: "RecallHarness", monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.recall_fixtures import candidate_for

    h = harness
    h.evidence.add_turn(turn())
    values = [
        candidate_for(
            replace(memory(), id=str(UUID(int=i + 1))),
            sources=(turn(),),
            semantic=1 - i / 100,
            lexical=0,
        )
        for i in range(20)
    ]
    lexical_winner = candidate_for(
        replace(memory(), id=str(UUID(int=99))), sources=(turn(),), semantic=0.79, lexical=1
    )
    h.retriever.values = [*values, lexical_winner]
    h.reranker.scores = {c.ref: -2.001 for c in h.retriever.values}
    h.reranker.scores[values[0].ref] = -2
    h.reranker.scores[values[1].ref] = 3
    h.reranker.scores[lexical_winner.ref] = 3
    monkeypatch.setattr(h.counter, "count", lambda text: 1)
    response = h.client.post("/characters/char-b/recall", json=request())
    assert response.is_success
    reranked = h.reranker.calls[-1].documents
    assert len(reranked) == 20
    assert lexical_winner.ref in {c.ref for c in reranked}
    assert values[-1].ref not in {c.ref for c in reranked}
    assert [m["id"] for m in response.json()["memories"]] == [
        values[1].ref.memory_id,
        lexical_winner.ref.memory_id,
        values[0].ref.memory_id,
    ]
    record = h.provisions.get(response.json()["provision_id"])
    assert record is not None
    scores = {score.ref: score for score in record.scores}
    assert set(scores) == {candidate.ref for candidate in h.retriever.values}
    assert scores[values[-1].ref].relevance is None
    assert all(scores[c.ref].relevance == h.reranker.scores[c.ref] for c in reranked)


@pytest.mark.parametrize("form", ["materials", "assembled"])
@pytest.mark.parametrize("count", [5, 6])
def test_response_keeps_at_most_five_ranked_memories(
    harness: "RecallHarness", monkeypatch: pytest.MonkeyPatch, form: str, count: int
) -> None:
    h = harness
    for i in range(count):
        h.seed(
            turn(turn_id=f"turn-{i}"),
            replace(memory(), id=str(UUID(int=i + 1)), source_turn_ids=(f"turn-{i}",)),
        )
    monkeypatch.setattr(h.counter, "count", lambda text: 1)
    response = h.client.post("/characters/char-b/recall", json=request(form=form))
    assert response.is_success
    assert len(response.json()["memories"]) == min(count, 5)
    h.assert_record_matches(response.json())


@pytest.mark.parametrize("form", ["materials", "assembled"])
def test_token_counter_observes_the_complete_final_http_json(
    harness: "RecallHarness", form: str
) -> None:
    h = harness
    h.seed(turn(), memory())
    response = h.client.post("/characters/char-b/recall", json=request(form=form))
    assert response.is_success
    final = counted_response(h, response.json())
    assert (len(final) + 3) // 4 <= 1500


@pytest.mark.parametrize("form", ["materials", "assembled"])
@pytest.mark.parametrize("size", [1500, 1501])
def test_token_budget_accepts_exact_limit_and_excludes_a_result_that_cannot_fit(
    harness: "RecallHarness", monkeypatch: pytest.MonkeyPatch, form: str, size: int
) -> None:
    h = harness
    h.seed(turn(), memory())

    def count(value: object) -> int:
        from digital_souls_memory.recall.inputs import CloudInput, LocalOnly

        assert isinstance(value, (CloudInput, LocalOnly))
        (text,) = value.texts
        try:
            body = json.loads(text)
        except json.JSONDecodeError:
            return (len(text) + 3) // 4
        if isinstance(body, dict) and "provision_id" in body and "personality_version" in body:
            return size if body["memories"] else 100
        return (len(text) + 3) // 4

    monkeypatch.setattr(h.counter, "count", count)
    response = h.client.post("/characters/char-b/recall", json=request(form=form))
    assert response.is_success
    assert len(response.json()["memories"]) == (1 if size == 1500 else 0)
    h.assert_record_matches(response.json())


@pytest.mark.parametrize("form", ["materials", "assembled"])
def test_long_multilingual_source_is_a_verbatim_window_around_the_match(
    harness: "RecallHarness", form: str
) -> None:
    h = harness
    original = "前置き🙂e\u0301" * 600 + "約束は11月3日 Blue station" + "後書き\n" * 600
    h.seed(turn(text=original), memory())
    response = h.client.post(
        "/characters/char-b/recall", json=request(form=form) | {"query": "11月3日 Blue station"}
    )
    assert response.is_success
    spans = response.json()["source_spans"]
    assert spans
    assert any("11月3日 Blue station" in s["text"] for s in spans)
    for span in spans:
        assert 0 <= span["start"] < span["end"] <= len(original)
        assert span["text"] == original[span["start"] : span["end"]]
        assert span["truncated"] is True
        assert (span["original_start"], span["original_end"]) == (0, len(original))
    final = counted_response(h, response.json())
    assert (len(final) + 3) // 4 <= 1500
    h.assert_record_matches(response.json())


def test_multiple_sources_are_preserved_instead_of_silently_dropping_one_for_budget(
    harness: "RecallHarness",
) -> None:
    h = harness
    h.evidence.add_turn(turn(turn_id="other", text="English original"))
    h.seed(turn(), replace(memory(), source_turn_ids=("turn-1", "other")))
    from tests.recall_fixtures import candidate_for

    h.retriever.values = [
        candidate_for(
            replace(memory(), source_turn_ids=("turn-1", "other")),
            sources=(turn(), turn(turn_id="other", text="English original")),
        )
    ]
    response = h.client.post("/characters/char-b/recall", json=request())
    assert response.is_success
    assert {s["turn_id"] for s in response.json()["source_spans"]} == {"turn-1", "other"}
    assert {s["text"] for s in response.json()["source_spans"]} == {TEXT, "English original"}


@pytest.mark.parametrize("form", ["materials", "assembled"])
def test_question_terms_keep_the_answer_in_the_long_original_and_provision(
    harness: "RecallHarness", form: str
) -> None:
    h = harness
    original = "日常の記録🙂e\u0301。" * 1200 + "約束は11月3日。" + "日常の記録。" * 1200
    h.seed(turn(text=original), memory())

    response = h.client.post(
        "/characters/char-b/recall", json=request(form=form) | {"query": "約束の日付はいつ？"}
    )

    assert response.is_success
    body = response.json()
    assert any("約束は11月3日" in span["text"] for span in body["source_spans"])
    for span in body["source_spans"]:
        assert span["text"] == original[span["start"] : span["end"]]
    assert (len(counted_response(h, body)) + 3) // 4 <= 1500
    h.assert_record_matches(body)


@pytest.mark.parametrize("form", ["materials", "assembled"])
@pytest.mark.parametrize(
    "original,query,answer",
    [
        (
            "日常の記録。" * 1200 + "約束は11月3日。" + "日常の記録。" * 1200,
            "約束の日付はいつ？" + "補足" * 4000,
            "約束は11月3日。",
        ),
        (
            "雑記。" * 3000 + "blue station: November 3." + "雑記。" * 3000,
            "BLUE STATION?",
            "blue station: November 3.",
        ),
        (
            "雑記。" * 3000 + "Straße station: November 3." + "雑記。" * 3000,
            "STRASSE STATION?",
            "Straße station: November 3.",
        ),
    ],
    ids=["long-question", "partial-casefold", "partial-expansion"],
)
def test_partial_match_survives_budgeting_in_http_and_provision(
    harness: "RecallHarness", form: str, original: str, query: str, answer: str
) -> None:
    h = harness
    h.seed(turn(text=original), memory())
    response = h.client.post(
        "/characters/char-b/recall", json=request(form=form) | {"query": query}
    )
    assert response.status_code == 200
    body = response.json()
    assert [m["id"] for m in body["memories"]] == [memory().id]
    assert any(answer in s["text"] for s in body["source_spans"])
    for span in body["source_spans"]:
        assert span["text"] == original[span["start"] : span["end"]]
        assert span["truncated"] is True
        assert (span["original_start"], span["original_end"]) == (0, len(original))
    assert body["context"] == (
        "\n".join(s["text"] for s in body["source_spans"]) if form == "assembled" else None
    )
    assert (len(counted_response(h, body)) + 3) // 4 <= 1500
    h.assert_record_matches(body)


@pytest.mark.parametrize("form", ["materials", "assembled"])
def test_full_match_that_exceeds_budget_is_not_cut_to_return_a_memory(
    harness: "RecallHarness", form: str
) -> None:
    h = harness
    query = "検索全文" * 2000
    h.seed(turn(text="前置き。" + query + "後置き。"), memory())
    response = h.client.post(
        "/characters/char-b/recall", json=request(form=form) | {"query": query}
    )
    assert response.status_code == 200
    assert response.json()["memories"] == []
    assert response.json()["source_spans"] == []
    h.assert_record_matches(response.json())


def test_negative_cosine_is_clamped_before_top_twenty_and_score_recording(
    harness: "RecallHarness", monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.recall_fixtures import candidate_for

    h = harness
    source = turn()
    h.evidence.add_turn(source)
    leaders = [
        candidate_for(
            replace(memory(), id=str(UUID(int=i + 1))), sources=(source,), semantic=0.9, lexical=0
        )
        for i in range(19)
    ]
    negative = candidate_for(
        replace(memory(), id=str(UUID(int=99))), sources=(source,), semantic=-0.6, lexical=1
    )
    zero = candidate_for(
        replace(memory(), id=str(UUID(int=100))), sources=(source,), semantic=0, lexical=0
    )
    h.retriever.values = [*leaders, negative, zero]
    monkeypatch.setattr(h.counter, "count", lambda value: 1)

    response = h.client.post("/characters/char-b/recall", json=request())

    assert response.is_success
    refs = {document.ref for document in h.reranker.calls[-1].documents}
    assert refs == {candidate.ref for candidate in [*leaders, negative]}
    record = h.provisions.get(response.json()["provision_id"])
    assert record is not None
    scores = {score.ref: score for score in record.scores}
    assert scores[negative.ref].semantic == scores[zero.ref].semantic == 0
    negative_composite = scores[negative.ref].composite
    zero_composite = scores[zero.ref].composite
    assert negative_composite is not None and zero_composite is not None
    assert negative_composite - zero_composite == pytest.approx(0.25)
    assert scores[negative.ref].relevance is not None
    assert scores[zero.ref].relevance is None


def test_tokenizer_failure_does_not_fall_back_to_byte_estimation_or_empty_success(
    harness: "RecallHarness", monkeypatch: pytest.MonkeyPatch
) -> None:
    h = harness

    def unavailable(value: object) -> int:
        raise RuntimeError("Synthetic tokenizer unavailable")

    monkeypatch.setattr(h.counter, "count", unavailable)
    response = h.client.post("/characters/char-b/recall", json=request())
    assert not response.is_success
    assert h.provisions.records == {}


@pytest.mark.parametrize("form", ["materials", "assembled"])
def test_budget_keeps_the_higher_ranked_memory_with_its_source_and_anchor_as_a_group(
    harness: "RecallHarness",
    monkeypatch: pytest.MonkeyPatch,
    form: str,
) -> None:
    from digital_souls_memory.recall.inputs import ModelInput
    from tests.recall_fixtures import anchor_candidate
    from tests.voice_support import anchor

    h = harness
    high = memory()
    low = replace(memory(), id=str(UUID(int=99)), source_turn_ids=("low",))
    h.seed(turn(), high)
    h.seed(turn(turn_id="low", text="低順位の原文"), low)
    h.reranker.scores = {h.retriever.values[0].ref: 10, h.retriever.values[1].ref: 5}
    value = replace(anchor(), character_id="char-b")
    h.retriever.anchors.append(anchor_candidate(value, source=turn()))
    with h.voice.transaction("char-b") as tx:
        tx.add_anchor(value)

    def count(value: ModelInput) -> int:
        body = json.loads(value.texts[0])
        return 1501 if len(body["memories"]) > 1 else 1500

    monkeypatch.setattr(h.counter, "count", count)
    response = h.client.post("/characters/char-b/recall", json=request(form=form))
    assert response.is_success
    body = response.json()
    assert [v["id"] for v in body["memories"]] == [high.id]
    assert {v["turn_id"] for v in body["source_spans"]} == {"turn-1"}
    assert [v["anchor_id"] for v in body["verbatim_anchors"]] == [value.anchor_id]
    h.assert_record_matches(body)
