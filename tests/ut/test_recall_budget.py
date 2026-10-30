import json
from collections import Counter
from dataclasses import replace
from datetime import timedelta

import pytest

from digital_souls_memory.adapters.identity_characters import IdentityCharacters
from digital_souls_memory.recall.context import serialize_response
from digital_souls_memory.recall.inputs import ModelInput
from digital_souls_memory.recall.settings import RecallSettings
from digital_souls_memory.recall.use_cases import RecallService
from tests.evidence_support import turn
from tests.memory_support import memory
from tests.recall_support import RecallHarness

pytestmark = pytest.mark.ut


def service(h: RecallHarness) -> RecallService:
    return RecallService(
        evidence=h.evidence,
        characters=IdentityCharacters(lambda: h.identity),
        retriever=h.retriever,
        embedding=h.embedding,
        reranker=h.reranker,
        counter=h.counter,
        provisions=h.provisions,
        selections=h.selections,
        clock=lambda: h.now,
        monotonic=lambda: 0,
        id_factory=h.new_provision_id,
        delivery_timeout=timedelta(minutes=10),
        non_sensitive_labels=frozenset(),
        settings=RecallSettings(),
    )


@pytest.mark.parametrize("form", ["materials", "assembled"])
@pytest.mark.parametrize("query", ["一致" * 600, "一致"], ids=["fixed-window", "shrinking-window"])
def test_budget_counts_each_complete_input_once_and_keeps_shrinking_until_it_fits(
    monkeypatch: pytest.MonkeyPatch, form: str, query: str
) -> None:
    h = RecallHarness()
    maximum = 100
    original = "前" * 400 + query + "後" * 400
    h.seed(turn(text=original), memory())
    inputs: list[ModelInput] = []

    def count(value: ModelInput) -> int:
        inputs.append(value)
        body = json.loads(value.texts[0])
        return 1501 if any(len(s["text"]) > maximum for s in body["source_spans"]) else 1500

    monkeypatch.setattr(h.counter, "count", count)
    result = service(h).recall(
        character_id="char-b",
        session_id="session-1",
        format=form,
        query=query,
        turn_id=None,
        sensitivity="synthetic-sensitive",
        classifier_version="synthetic-v1",
    )
    if len(query) > maximum:
        assert result.memories == ()
        assert result.source_spans == ()
    else:
        assert [m.id for m in result.memories] == [memory().id]
        # 802 -> 401 -> 200 -> 100: preserve the first fitting width.
        assert len(result.source_spans[0].text) == 100
        span = result.source_spans[0]
        assert span.text == original[span.start : span.end]
        assert query in span.text and span.truncated
    assert result.context == (
        "\n".join(s.text for s in result.source_spans) if form == "assembled" else None
    )
    record = h.provisions.get(result.provision_id)
    assert record is not None
    assert record.source_spans == result.source_spans
    assert record.memory_ids == tuple(m.id for m in result.memories)
    assert serialize_response(result) in [v.texts[0] for v in inputs]
    assert all(v.labels == inputs[0].labels for v in inputs)
    assert all(n == 1 for n in Counter(inputs).values())


def test_budget_recounts_changed_json_and_does_not_share_counts_between_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = RecallHarness()
    h.seed(turn(), memory())
    h.seed(turn(turn_id="other"), replace(memory(), id="other", source_turn_ids=("other",)))
    inputs: list[ModelInput] = []
    reject = False

    def count(value: ModelInput) -> int:
        inputs.append(value)
        body = json.loads(value.texts[0])
        return 1501 if len(body["memories"]) > (0 if reject else 1) else 1500

    monkeypatch.setattr(h.counter, "count", count)
    subject = service(h)
    for reject in (False, True):
        h.next_id = "second" if reject else "first"
        inputs.clear()
        result = subject.recall(
            character_id="char-b",
            session_id="session-1",
            format="materials",
            query="日本語",
            turn_id=None,
            sensitivity="synthetic-sensitive",
            classifier_version="synthetic-v1",
        )
        assert len(result.memories) == (0 if reject else 1)
        assert any(len(json.loads(v.texts[0])["memories"]) == 2 for v in inputs) is (not reject)
        assert serialize_response(result) in [v.texts[0] for v in inputs]


def test_failed_save_retry_recounts_identical_json_in_a_new_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from digital_souls_memory.evidence.provision import ProvisionUnavailable

    h = RecallHarness()
    h.seed(turn(), memory())
    h.next_id = "retry"
    inputs: list[ModelInput] = []
    reject = False

    def count(value: ModelInput) -> int:
        inputs.append(value)
        body = json.loads(value.texts[0])
        return 1501 if reject and body["memories"] else 1500

    monkeypatch.setattr(h.counter, "count", count)
    subject = service(h)
    h.provisions.fail_at = "insert"
    with pytest.raises(ProvisionUnavailable):
        subject.recall(
            character_id="char-b",
            session_id="session-1",
            format="materials",
            query="日本語",
            turn_id=None,
            sensitivity="synthetic-sensitive",
            classifier_version="synthetic-v1",
        )
    previous_inputs = set(inputs)
    assert h.provisions.records == {}
    h.provisions.fail_at = None
    reject = True
    inputs.clear()
    result = subject.recall(
        character_id="char-b",
        session_id="session-1",
        format="materials",
        query="日本語",
        turn_id=None,
        sensitivity="synthetic-sensitive",
        classifier_version="synthetic-v1",
    )
    assert previous_inputs.intersection(inputs)
    assert result.memories == ()
    assert h.provisions.get("retry") is not None


def test_fixed_source_and_anchor_do_not_stop_another_source_from_shrinking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.recall_fixtures import anchor_candidate, candidate_for
    from tests.voice_support import anchor

    h = RecallHarness()
    query = "一致" * 500
    first = turn(text=query)
    second = turn(turn_id="second-source", text="別" * 3200)
    for source in (first, second):
        h.evidence.add_turn(source)
    value = replace(memory(), source_turn_ids=(first.turn_id, second.turn_id))
    h.retriever.values = [candidate_for(value, sources=(first, second))]
    attached = replace(anchor(), character_id="char-b")
    h.retriever.anchors = [anchor_candidate(attached, source=first)]

    def count(value: ModelInput) -> int:
        spans = json.loads(value.texts[0])["source_spans"]
        return (
            1501
            if any(s["turn_id"] == second.turn_id and len(s["text"]) > 400 for s in spans)
            else 1500
        )

    monkeypatch.setattr(h.counter, "count", count)
    result = service(h).recall(
        character_id="char-b",
        session_id="session-1",
        format="materials",
        query=query,
        turn_id=None,
        sensitivity="synthetic-sensitive",
        classifier_version="synthetic-v1",
    )
    assert [m.id for m in result.memories] == [value.id]
    assert [(s.turn_id, s.text) for s in result.source_spans] == [
        (first.turn_id, query),
        (second.turn_id, second.text[:400]),
    ]
    assert [(a.anchor_id, a.text) for a in result.verbatim_anchors] == [(attached.anchor_id, query)]
    record = h.provisions.get(result.provision_id)
    assert record is not None
    assert record.source_spans == result.source_spans
    assert record.verbatim_anchor_ids == (attached.anchor_id,)


def test_fixed_window_reduces_tokenization_and_window_work_without_changing_the_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from digital_souls_memory.evidence.provision import SourceSpan
    from digital_souls_memory.recall import context
    from digital_souls_memory.recall.context import window_span
    from tests.postgres import recall_legacy

    query = "一致" * 600
    results = []
    records = []
    counts = []
    windows = []
    calls: list[ModelInput] = []
    widths: list[int] = []

    def count(value: ModelInput) -> int:
        calls.append(value)
        return 1501 if json.loads(value.texts[0])["memories"] else 1500

    def observe(
        span: SourceSpan, *, query: str, width: int, match: tuple[int, int] | None = None
    ) -> SourceSpan:
        widths.append(width)
        return window_span(span, query=query, width=width, match=match)

    for legacy in (True, False):
        h = RecallHarness()
        h.seed(turn(text="前" * 400 + query + "後" * 400), memory())
        calls.clear()
        widths.clear()
        monkeypatch.setattr(h.counter, "count", count)
        subject = service(h)
        operation = recall_legacy.LegacyRecallService(**vars(subject)) if legacy else subject
        with monkeypatch.context() as patch:
            patch.setattr(recall_legacy if legacy else context, "window_span", observe)
            result = operation.recall(
                character_id="char-b",
                session_id="session-1",
                format="materials",
                query=query,
                turn_id=None,
                sensitivity="synthetic-sensitive",
                classifier_version="synthetic-v1",
            )
        results.append(result)
        records.append(h.provisions.get(result.provision_id))
        counts.append(len(calls))
        windows.append(len(widths))
    assert results[0] == results[1]
    assert records[0] == records[1]
    assert counts[1] < counts[0]
    assert windows[1] < windows[0]
    print(json.dumps({"tokenize_calls": counts, "window_calls": windows}))


@pytest.mark.parametrize("invalid", [True, -1, 1.5])
def test_invalid_token_count_is_rejected_before_caching_or_saving(
    monkeypatch: pytest.MonkeyPatch,
    invalid: object,
) -> None:
    from typing import cast

    from digital_souls_memory.evidence.provision import ProvisionUnavailable

    h = RecallHarness()
    monkeypatch.setattr(h.counter, "count", lambda value: cast(int, invalid))
    with pytest.raises(ProvisionUnavailable):
        service(h).recall(
            character_id="char-b",
            session_id="session-1",
            format="materials",
            query="日本語",
            turn_id=None,
            sensitivity="synthetic-sensitive",
            classifier_version="synthetic-v1",
        )
    assert h.provisions.records == {}
