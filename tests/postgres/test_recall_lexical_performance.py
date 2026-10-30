"""Synthetic SQL comparison; timings are evidence, never a CI time threshold."""

import json
from random import Random
from statistics import median
from typing import Self

import psycopg
import pytest
from psycopg import sql
from psycopg.abc import Params, Query
from psycopg.rows import DictRow, dict_row

from digital_souls_memory.adapters.recall_search import _candidates
from digital_souls_memory.recall.lexical import lexical_terms
from tests.evidence_support import RECORDED
from tests.postgres.recall_legacy import legacy_candidates
from tests.postgres.test_recall_search import plan_nodes
from tests.recall_fixtures import SPACE

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("body_length", [24, 1039])
@pytest.mark.parametrize("query_length", [3, 50, 200, 1039])
def test_lexical_rank_preserves_rows_and_index_with_long_synthetic_text(
    database_url: str, body_length: int, query_length: int
) -> None:
    plans: list[DictRow] = []

    class MeasuredCursor(psycopg.ServerCursor[DictRow]):
        def execute(
            self,
            query: Query,
            params: Params | None = None,
            *,
            binary: bool | None = None,
            **kwargs: object,
        ) -> Self:
            if not isinstance(query, (str, bytes, sql.SQL, sql.Composed)):
                return super().execute(query, params, binary=binary, **kwargs)
            text = (
                query
                if isinstance(query, str)
                else query.decode()
                if isinstance(query, bytes)
                else query.as_string(self.connection)
            )
            with psycopg.Cursor(self.connection) as cursor:
                cursor.execute(
                    sql.SQL(
                        "EXPLAIN (ANALYZE, BUFFERS, VERBOSE, FORMAT JSON) DECLARE {} "
                        "NO SCROLL CURSOR WITHOUT HOLD FOR "
                    ).format(sql.Identifier(self.name))
                    + sql.SQL(text),
                    params,
                )
                row = cursor.fetchone()
                assert row is not None
                plans.append(row["QUERY PLAN"][0])
            return super().execute(query, params, binary=binary, **kwargs)

    alphabet = "".join(chr(0x4E00 + i) for i in range(1039))
    query = alphabet[:query_length]
    rng = Random(118)
    vector = "[" + ",".join(["1"] + ["0"] * 1023) + "]"
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        # Temporary tables isolate the measurement from fixtures and product data.
        connection.execute(
            "CREATE TEMP TABLE recall_documents (LIKE public.recall_documents INCLUDING DEFAULTS)"
        )
        connection.execute(
            "CREATE UNIQUE INDEX measured_key ON recall_documents(owner_id,kind,identifier)"
        )
        connection.execute(
            "CREATE INDEX measured_lexical ON recall_documents USING gin(search_text gin_bigm_ops)"
        )
        with connection.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO recall_documents(owner_id,kind,identifier,ready,search_text,model,"
                "revision,digest,dimensions,embedding) "
                "VALUES('char-b','anchor',%s,TRUE,%s,%s,%s,%s,%s,%s::vector)",
                [
                    (
                        str(i),
                        (
                            alphabet[:200]
                            + "".join(chr(rng.randrange(0x5200, 0x8000)) for _ in range(839))
                        )[:body_length],
                        SPACE.model,
                        SPACE.revision,
                        SPACE.digest,
                        SPACE.dimensions,
                        vector,
                    )
                    for i in range(1000)
                ],
            )
        connection.execute("ANALYZE recall_documents")
        connection.server_cursor_factory = MeasuredCursor
        results = []
        timings: dict[str, list[float]] = {"legacy": [], "current": []}
        rank_evaluations = []
        for _ in range(3):
            for name, candidates in (("legacy", legacy_candidates), ("current", _candidates)):
                with candidates(
                    connection,
                    lexical_terms(query),
                    vector,
                    SPACE,
                    20,
                    True,
                    "char-b",
                    RECORDED,
                    True,
                ) as cursor:
                    rows = cursor.fetchall()
                results.append(rows)
                plan = plans[-1]
                timings[name].append(float(plan["Execution Time"]))
                nodes = tuple(plan_nodes(plan["Plan"]))
                assert any(n.get("Index Name") == "measured_lexical" for n in nodes)
                assert not any(
                    n["Node Type"] == "Seq Scan" and n.get("Relation Name") == "recall_documents"
                    for n in nodes
                )
                if name == "current":
                    rank_evaluations = [
                        n["Actual Loops"]
                        for n in nodes
                        if n["Node Type"] == "Result"
                        and any("||" in output for output in n.get("Output", []))
                    ]
        assert len(results[0]) == 1000
        assert all(rows == results[0] for rows in results)
        print(
            json.dumps(
                {
                    "body_length": body_length,
                    "query_length": query_length,
                    "terms": len(lexical_terms(query)),
                    "legacy_ms": median(timings["legacy"]),
                    "current_ms": median(timings["current"]),
                    "ranking_text_evaluation_loops": rank_evaluations,
                }
            )
        )
