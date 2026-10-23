from typing import cast

import psycopg
import pytest

from digital_souls_memory.evaluation.adapters.postgres import PostgresSearch
from digital_souls_memory.evaluation.models import record
from digital_souls_memory.evaluation.retrieval import lexical_terms, normalized_key

pytestmark = pytest.mark.ut


@pytest.mark.parametrize(
    "text,key,terms",
    [
        ("", "", ()),
        (" \t\n", " \t\n", ()),
        ("%_\\", "%_\\", ()),
        ("ＡＢＣ abc 青い駅 %_\\ x", "abc abc 青い駅 %_\\ x", ("ab", "bc", "x", "い駅", "青い")),
        ("abc abc", "abc abc", ("ab", "bc")),
        ("Ａ", "a", ("a",)),
        ("青い駅", "青い駅", ("い駅", "青い")),
        ("ｶﾞｸ", "ガク", ("ガク",)),
        ("Straße", "strasse", ("as", "ra", "se", "ss", "st", "tr")),
        ("Ａ青B", "a青b", ("a青", "青b")),
    ],
)
def test_product_and_evaluation_normalize_and_split_the_same_inputs(
    text: str, key: str, terms: tuple[str, ...]
) -> None:
    assert normalized_key(text) == key
    assert lexical_terms(text) == terms

    from digital_souls_memory.recall.lexical import lexical_terms as product_terms
    from digital_souls_memory.recall.lexical import normalized_key as product_key

    assert product_key(text) == normalized_key(text) == key
    assert product_terms(text) == lexical_terms(text) == terms


class EvaluationRows:
    def __init__(self, values: list[tuple[object, ...]]) -> None:
        self.values = values

    def fetchall(self) -> list[tuple[object, ...]]:
        return self.values

    def fetchone(self) -> tuple[object, ...] | None:
        return self.values[0] if self.values else None


class EvaluationConnection:
    """Only the DB boundary is replaced; observe and score_components remain real."""

    def __init__(self, memory: dict[str, object], turn: dict[str, object]) -> None:
        self.memory = memory
        self.turn = turn

    def execute(self, query: str, params: tuple[object, ...]) -> EvaluationRows:
        if "WHERE search_key LIKE" in query:
            pattern = str(params[0])[1:-1]
            matched = pattern in normalized_key(str(self.memory["content"]))
            return EvaluationRows([(self.memory["id"],)] if matched else [])
        if "embedding <=>" in query:
            return EvaluationRows([(self.memory["id"], self.memory, 0.25)])
        if query == "SELECT body FROM memories WHERE id=%s":
            return EvaluationRows([(self.memory,)])
        if query == "SELECT body FROM turns WHERE id=%s":
            return EvaluationRows([(self.turn,)])
        raise AssertionError(query)


@pytest.mark.parametrize(
    "query,content,expected",
    [
        ("%_\\", "%_\\", 0.0),
        ("abc abc", "ab", 0.5),
        ("abc abc", "ab---bc", 1.0),
        ("abc", "zzz", 0.0),
        ("ＡＢＣ", "Ab", 0.5),
        ("青い駅", "青い空", 0.5),
        ("Ａ青B", "a青", 0.5),
        ("ｶﾞｸ", "ガク", 1.0),
        ("Straße", "STRASSE", 1.0),
        ("x", "x", 1.0),
    ],
)
def test_product_lexical_ratio_matches_evaluation_observe(
    candidate: dict[str, object],
    case: dict[str, object],
    memory: dict[str, object],
    turn: dict[str, object],
    protocol: dict[str, object],
    query: str,
    content: str,
    expected: float,
) -> None:
    value = memory | {"content": content}
    evaluation = PostgresSearch(candidate, admin_url="unused")
    evaluation.case = case | {"query": query, "memories": [value], "protocol": protocol}
    evaluation.connection = cast(
        psycopg.Connection[tuple[object, ...]], EvaluationConnection(value, turn)
    )

    (observed,) = evaluation.observe((1.0, 0.0))
    assert record(observed["components"])["lexical"] == pytest.approx(expected)

    from digital_souls_memory.recall.lexical import lexical_score

    assert lexical_score(normalized_key(content), lexical_terms(query)) == pytest.approx(expected)


@pytest.mark.parametrize("query", ["", " \t"])
def test_empty_terms_score_zero_and_evaluation_keeps_nonempty_query_validation(
    candidate: dict[str, object], case: dict[str, object], query: str
) -> None:
    from digital_souls_memory.recall.lexical import lexical_score

    assert lexical_terms(query) == ()
    assert lexical_score(normalized_key("abc"), lexical_terms(query)) == 0.0
    evaluation = PostgresSearch(candidate, admin_url="unused")
    evaluation.case = case | {"query": query}
    with pytest.raises(ValueError):
        evaluation.observe((1.0, 0.0))
