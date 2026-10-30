from collections.abc import Iterator

import psycopg
import pytest
from psycopg.rows import DictRow, dict_row

from digital_souls_memory.adapters.recall_search import _candidates
from tests.evidence_support import RECORDED
from tests.postgres.recall_legacy import legacy_candidates
from tests.recall_fixtures import SPACE

pytestmark = pytest.mark.postgres

VECTOR = "[" + ",".join(["1"] + ["0"] * 1023) + "]"


@pytest.fixture
def candidate_connection(database_url: str) -> Iterator[psycopg.Connection[DictRow]]:
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        # Match the existing SQL tests without borrowing application fixture documents.
        connection.execute(
            "CREATE TEMP TABLE recall_documents (LIKE public.recall_documents INCLUDING DEFAULTS)"
        )
        connection.execute(
            "CREATE UNIQUE INDEX tie_key ON recall_documents(owner_id,kind,identifier,chunk_index)"
        )
        connection.execute(
            "CREATE INDEX tie_lexical ON recall_documents USING gin(search_text gin_bigm_ops)"
        )
        yield connection


def insert_document(
    connection: psycopg.Connection[DictRow],
    key: tuple[str, str, str, int],
    text: str,
    vector: str,
) -> None:
    connection.execute(
        "INSERT INTO recall_documents(owner_id,kind,identifier,chunk_index,ready,search_text,"
        "model,revision,digest,dimensions,embedding) "
        "VALUES(%s,%s,%s,%s,TRUE,%s,%s,%s,%s,%s,%s::vector)",
        (*key, text, SPACE.model, SPACE.revision, SPACE.digest, SPACE.dimensions, vector),
    )


@pytest.mark.parametrize(
    "lexical,exact",
    [(True, True), (False, True), (False, False)],
    ids=["lexical", "exact-vector", "approximate-vector"],
)
def test_equal_score_candidates_use_document_key_independent_of_insertion_order(
    candidate_connection: psycopg.Connection[DictRow], lexical: bool, exact: bool
) -> None:
    keys = [("char-b", "anchor", f"synthetic-{i:02d}", 0) for i in range(24)]
    if lexical:
        # Lexical SQL returns denied rows too; eligibility is applied by search_rows.
        keys += [("char-a", "anchor", "synthetic-00", 0), ("char-b", "memory", "synthetic-00", 0)]
    for key in reversed(sorted(keys)):
        insert_document(candidate_connection, key, "日本語", VECTOR)

    results = []
    for candidates in (_candidates, legacy_candidates):
        with candidates(
            candidate_connection, ("日本",), VECTOR, SPACE, 20, lexical, "char-b", RECORDED, exact
        ) as cursor:
            results.append(cursor.fetchall())
    expected = sorted(keys)
    if not exact:
        expected = expected[:20]
    for rows in results:
        assert [
            (row["owner_id"], row["kind"], row["identifier"], row["chunk_index"]) for row in rows
        ] == expected
    assert results[0] == results[1]


@pytest.mark.parametrize(
    "lexical,exact",
    [(True, True), (False, True), (False, False)],
    ids=["lexical", "exact-vector", "approximate-vector"],
)
def test_equal_score_chunks_use_chunk_index_as_final_tie_breaker(
    candidate_connection: psycopg.Connection[DictRow], lexical: bool, exact: bool
) -> None:
    for chunk_index in (2, 1, 0):
        insert_document(
            candidate_connection,
            ("char-b", "anchor", "synthetic-00", chunk_index),
            "日本語",
            VECTOR,
        )

    # The frozen pre-chunk oracle only supports documents with one chunk.
    with _candidates(
        candidate_connection, ("日本",), VECTOR, SPACE, 20, lexical, "char-b", RECORDED, exact
    ) as cursor:
        rows = cursor.fetchall()
    assert [(row["identifier"], row["chunk_index"]) for row in rows] == [
        ("synthetic-00", 0),
        ("synthetic-00", 1),
        ("synthetic-00", 2),
    ]


@pytest.mark.parametrize(
    "lexical,exact",
    [(True, True), (False, True), (False, False)],
    ids=["lexical", "exact-vector", "approximate-vector"],
)
def test_unequal_scores_take_priority_over_document_key(
    candidate_connection: psycopg.Connection[DictRow], lexical: bool, exact: bool
) -> None:
    farther_vector = "[" + ",".join(["0.6", "0.8"] + ["0"] * 1022) + "]"
    insert_document(candidate_connection, ("char-b", "anchor", "a-low", 0), "日本", farther_vector)
    insert_document(candidate_connection, ("char-b", "anchor", "z-high", 0), "日本語", VECTOR)

    for candidates in (_candidates, legacy_candidates):
        with candidates(
            candidate_connection,
            ("日本", "本語"),
            VECTOR,
            SPACE,
            20,
            lexical,
            "char-b",
            RECORDED,
            exact,
        ) as cursor:
            rows = cursor.fetchall()
        assert [row["identifier"] for row in rows] == ["z-high", "a-low"]
