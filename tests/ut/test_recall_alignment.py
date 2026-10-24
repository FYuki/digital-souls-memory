"""Product and evaluation must execute the same window and semantic rules."""

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import timedelta
from math import isclose
from types import CodeType, FrameType

import pytest

from digital_souls_memory.adapters.identity_characters import IdentityCharacters
from digital_souls_memory.evaluation.context import _window
from digital_souls_memory.evaluation.models import rows
from digital_souls_memory.evaluation.retrieval import score_components
from digital_souls_memory.evidence.provision import SourceSpan
from digital_souls_memory.recall.context import window_span
from digital_souls_memory.recall.settings import RecallSettings
from digital_souls_memory.recall.use_cases import RecallService
from tests.evidence_support import turn
from tests.memory_support import memory as product_memory
from tests.recall_fixtures import candidate_for
from tests.recall_support import RecallHarness, StepClock

pytestmark = pytest.mark.ut


@contextmanager
def recall_calls(*inputs: str | float) -> Iterator[set[CodeType]]:
    """Observe sharing required by order.md without prescribing a helper name."""
    calls: set[CodeType] = set()

    def observe(frame: FrameType, event: str, arg: object) -> None:
        if event == "call" and str(frame.f_globals.get("__name__", "")).startswith(
            "digital_souls_memory.recall."
        ):
            values = tuple(v for v in frame.f_locals.values() if isinstance(v, (str, float)))
            if all(
                any(
                    isclose(value, v, rel_tol=0, abs_tol=1e-12)
                    if isinstance(value, float) and isinstance(v, float)
                    else value == v
                    for v in values
                )
                for value in inputs
            ):
                calls.add(frame.f_code)

    previous = sys.getprofile()
    sys.setprofile(observe)
    try:
        yield calls
    finally:
        sys.setprofile(previous)


@pytest.mark.parametrize(
    "text,query,match",
    [
        (
            "日常の記録。" * 500 + "約束は11月3日。" + "日常の記録。" * 500,
            "約束の日付はいつ？",
            "約束は11月3日",
        ),
        (
            "Straße🙂e\u0301。" * 100 + "BLUE station" + "末尾。" * 100,
            "blue STATION",
            "BLUE station",
        ),
        ("無関係。" * 100, "xyz", "無関係。"),
    ],
    ids=["question-terms", "unicode-casefold", "no-match"],
)
def test_product_and_evaluation_use_one_window_selector(text: str, query: str, match: str) -> None:
    span = SourceSpan(
        turn_id="source",
        start=7,
        end=7 + len(text),
        text=text,
        actor="char-c",
        language="ja",
        truncated=False,
        original_start=7,
        original_end=7 + len(text),
    )
    with recall_calls(text, query) as product_calls:
        product = window_span(span, query=query, width=100)
    with recall_calls(text, query) as evaluation_calls:
        evaluated = _window({"memory_id": "m1", "source_spans": [asdict(span)]}, query, 100)
    (evaluation,) = rows(evaluated["source_spans"])

    assert match in product.text
    assert (product.start, product.end, product.text) == (
        evaluation["start"],
        evaluation["end"],
        evaluation["text"],
    )
    assert product.text == text[product.start - 7 : product.end - 7]
    assert product_calls & evaluation_calls, "Both paths must call the same recall function"


@pytest.mark.parametrize(
    "cosine,expected",
    [(-1.0, 0.0), (-0.6, 0.0), (0.0, 0.0), (0.4, 0.4), (1.0, 1.0), (1.000001, 1.0)],
)
def test_product_and_evaluation_use_one_semantic_conversion(
    case: dict[str, object], memory: dict[str, object], cosine: float, expected: float
) -> None:
    h = RecallHarness()
    source = turn()
    h.evidence.add_turn(source)
    h.retriever.values = [candidate_for(product_memory(), sources=(source,), semantic=cosine)]
    service = RecallService(
        evidence=h.evidence,
        characters=IdentityCharacters(lambda: h.identity),
        retriever=h.retriever,
        embedding=h.embedding,
        reranker=h.reranker,
        counter=h.counter,
        provisions=h.provisions,
        selections=h.selections,
        clock=lambda: h.now,
        monotonic=StepClock(),
        id_factory=h.new_provision_id,
        delivery_timeout=timedelta(minutes=10),
        non_sensitive_labels=frozenset(),
        settings=RecallSettings(),
    )
    with recall_calls(cosine) as product_calls:
        response = service.recall(
            character_id="char-b",
            session_id="session-1",
            format="materials",
            query="約束",
            turn_id=None,
            sensitivity="sensitive",
            classifier_version="synthetic-v1",
        )
    with recall_calls(cosine) as evaluation_calls:
        components = score_components(case, memory, semantic_distance=1 - cosine, lexical=0.8)
    record = h.provisions.get(response.provision_id)
    assert record is not None
    assert record.scores[0].semantic == pytest.approx(expected, rel=0, abs=1e-12)
    assert components["semantic"] == pytest.approx(expected, rel=0, abs=1e-12)
    assert product_calls & evaluation_calls, "Both paths must call the same recall function"
