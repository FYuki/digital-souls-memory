"""Synthetic product HTTP latency measurement on an empty disposable test database."""

import json
import os
from contextlib import contextmanager
from dataclasses import replace
from math import ceil
from pathlib import Path
from time import perf_counter

import psycopg
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from digital_souls_memory.adapters.postgres_evidence import PostgresEvidenceStore
from digital_souls_memory.adapters.postgres_identity import PostgresIdentityStore
from digital_souls_memory.adapters.postgres_memory import PostgresMemoryStore
from digital_souls_memory.adapters.postgres_retrieval import PostgresRetrievalStore
from digital_souls_memory.api import create_app
from digital_souls_memory.identity.domain import CoreIdentity
from digital_souls_memory.recall.worker import IndexWorker
from tests.evidence_support import RECORDED, session, turn
from tests.it1.test_recall import request
from tests.memory_support import memory
from tests.recall_fixtures import SPACE
from tests.recall_support import RecordingEmbedding, RecordingReranker, RecordingTokenCounter


def main() -> None:
    url = os.environ["MEMORY_TEST_DATABASE_URL"]
    os.environ["MEMORY_DATABASE_URL"] = url
    with psycopg.connect(url) as connection:
        if connection.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public'"
        ).fetchall():
            raise RuntimeError("Measurement requires an empty disposable database")
    config = Config("alembic.ini")
    command.upgrade(config, "head")
    identities = PostgresIdentityStore(url)
    for identifier in ("char-b", "char-c"):
        with identities.transaction(identifier, source="registration") as tx:
            tx.add_core(CoreIdentity(identifier, ("合成価値",), (), 1, RECORDED))
    evidence = PostgresEvidenceStore(url)
    evidence.add_session(session(reports_delivery=False))
    evidence.add_turn(turn(text="日本語の合成原文"))
    store = PostgresMemoryStore(url)
    index = PostgresRetrievalStore(url)
    connection_factory = index._connection
    plans = []
    explained = set()
    capturing = False

    class PlanCursor(psycopg.Cursor):
        def execute(self, query, params=None, **kwargs):
            if (
                capturing
                and isinstance(query, str)
                and query.lstrip().upper().startswith(("SELECT", "WITH"))
                and "recall_documents" in query
                and (query, str(params)) not in explained
            ):
                with self.connection.cursor(row_factory=dict_row) as cursor:
                    cursor.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + query, params)
                    plans.append(cursor.fetchone()["QUERY PLAN"])
                    explained.add((query, str(params)))
            return super().execute(query, params, **kwargs)

    class PlanServerCursor(psycopg.ServerCursor):
        def execute(self, query, params=None, **kwargs):
            if capturing and (query, str(params)) not in explained:
                assert self.scrollable is False and not self.withhold
                statement = psycopg.sql.SQL(
                    "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) DECLARE {} "
                    "NO SCROLL CURSOR WITHOUT HOLD FOR "
                ).format(psycopg.sql.Identifier(self.name)) + psycopg.sql.SQL(query)
                with psycopg.Cursor(self.connection, row_factory=dict_row) as cursor:
                    cursor.execute(statement, params)
                    plans.append(cursor.fetchone()["QUERY PLAN"])
                    explained.add((query, str(params)))
            return super().execute(query, params, **kwargs)

    @contextmanager
    def measured_connection():
        with connection_factory() as connection:
            connection.cursor_factory = PlanCursor
            connection.server_cursor_factory = PlanServerCursor
            yield connection

    index._connection = measured_connection
    embedding = RecordingEmbedding()
    worker = IndexWorker(index, embedding, space=SPACE, clock=lambda: RECORDED)
    previous = 0
    for count in (100, 1000, 10000):
        started = perf_counter()
        for offset in range(previous, count, 100):
            with store.transaction("char-b") as tx:
                for i in range(offset, min(offset + 100, count)):
                    tx.add_memory(
                        replace(memory(), id=f"synthetic-{i:05d}", content=f"日本語 合成記憶 {i}")
                    )
        while work := index.claim(space=SPACE, now=RECORDED, limit=32):
            for item in work:
                worker.process(item)
        build_seconds = perf_counter() - started
        with psycopg.connect(url) as connection:
            connection.execute("ANALYZE recall_documents")
            connection.execute("ANALYZE derived_memories")
        with TestClient(
            create_app(
                retriever=index,
                embedding=embedding,
                reranker=RecordingReranker(),
                token_counter=RecordingTokenCounter(),
                clock=lambda: RECORDED,
            )
        ) as client:
            plans.clear()
            explained.clear()
            durations = []
            body = {}
            for iteration in range(25):
                capturing = iteration == 0
                started = perf_counter()
                response = client.post("/characters/char-b/recall", json=request())
                elapsed = (perf_counter() - started) * 1000
                response.raise_for_status()
                body = response.json()
                assert body["memories"]
                if iteration >= 5:
                    durations.append(elapsed)
            assert len(plans) >= 2, "Product candidate plans were not captured"
            durations.sort()
            plan_path = Path(f"/tmp/recall-sql-plan-{count}.json")
            plan_path.write_text(json.dumps(plans, ensure_ascii=False, indent=2))
            print(
                json.dumps(
                    {
                        "count": count,
                        "index_build_seconds": build_seconds,
                        "warmup": 5,
                        "iterations": 20,
                        "p50_ms": durations[9],
                        "p95_ms": durations[ceil(20 * 0.95) - 1],
                        "returned_memories": len(body["memories"]),
                        "passage_calls": sum(p == "passage" for _, p in embedding.calls),
                        "sql_plan": str(plan_path),
                    }
                ),
                flush=True,
            )
        previous = count


if __name__ == "__main__":
    main()
