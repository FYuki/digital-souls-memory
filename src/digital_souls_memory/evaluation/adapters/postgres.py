"""Own a disposable database without acquiring or deleting an existing database."""

import json
import re
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from time import perf_counter
from typing import Self
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from ..models import (
    integer,
    number,
    record,
    rows,
    string,
    strings,
    validate_candidate,
    validate_vectors,
)
from ..retrieval import (
    composite_score,
    eligible_memories,
    lexical_terms,
    normalized_key,
    rank_results,
    score_components,
)


@contextmanager
def disposable_database(admin_url: str, *, name: str) -> Iterator[str]:
    if not re.fullmatch(r"memory_eval_[a-zA-Z0-9_]{1,48}", name):
        raise ValueError("Evaluation database name required")
    with psycopg.connect(admin_url, autocommit=True, connect_timeout=5) as admin:
        # CREATE failure deliberately occurs before the ownership/cleanup scope.
        admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
        try:
            yield make_conninfo(admin_url, dbname=name)
        finally:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


class PostgresSearch:
    def __init__(
        self, candidate: dict[str, object], *, admin_url: str, name: str | None = None
    ) -> None:
        validate_candidate(candidate)
        self.candidate = deepcopy(candidate)
        self.admin_url = admin_url
        self.name = "memory_eval_" + uuid4().hex if name is None else name
        self.resources = ExitStack()
        self.case: dict[str, object] | None = None
        self.measurements: dict[str, float] = {}

    def __enter__(self) -> Self:
        try:
            url = self.resources.enter_context(disposable_database(self.admin_url, name=self.name))
            self.connection = self.resources.enter_context(
                psycopg.connect(url, autocommit=True, connect_timeout=5)
            )
            for extension in ("vector", "pg_bigm", "pgroonga"):
                self.connection.execute(
                    sql.SQL("CREATE EXTENSION {}").format(sql.Identifier(extension))
                )
            self.connection.execute("CREATE TABLE turns (id text PRIMARY KEY, body jsonb NOT NULL)")
            self.connection.execute(
                sql.SQL(
                    "CREATE TABLE memories (id text PRIMARY KEY, body jsonb NOT NULL, "
                    "search_key text NOT NULL, embedding vector({}))"
                ).format(sql.Literal(integer(self.candidate["dimensions"])))
            )
            return self
        except BaseException:
            self.resources.close()
            raise

    def __exit__(self, *exc: object) -> None:
        self.resources.close()

    def index(self, case: dict[str, object], vectors: tuple[tuple[float, ...], ...]) -> None:
        memories = rows(case["memories"])
        if eligible_memories(case) != memories:
            raise ValueError("Only eligible memories may enter the search adapter")
        vectors = validate_vectors(
            vectors, count=len(memories), dimensions=integer(self.candidate["dimensions"])
        )
        self.case = None
        self.connection.execute("DROP INDEX IF EXISTS lexical_index")
        self.connection.execute("TRUNCATE memories, turns")
        started = perf_counter()
        for turn in rows(case["turns"]):
            self.connection.execute(
                "INSERT INTO turns VALUES (%s,%s)", (turn["turn_id"], Jsonb(turn))
            )
        for memory, vector in zip(memories, vectors, strict=True):
            self.connection.execute(
                "INSERT INTO memories VALUES (%s,%s,%s,%s::vector)",
                (
                    memory["id"],
                    Jsonb(memory),
                    normalized_key(string(memory["content"])),
                    str(list(vector)),
                ),
            )
        self.measurements = {"insertion_ms": (perf_counter() - started) * 1000}
        started = perf_counter()
        if self.candidate["extension"] == "pg_bigm":
            self.connection.execute(
                "CREATE INDEX lexical_index ON memories USING gin (search_key gin_bigm_ops)"
            )
        else:
            self.connection.execute(
                "CREATE INDEX lexical_index ON memories USING pgroonga (search_key)"
            )
        self.measurements["index_creation_ms"] = (perf_counter() - started) * 1000
        sizes = self.connection.execute(
            "SELECT pg_relation_size('lexical_index'), "
            "pg_table_size('memories') + pg_table_size('turns'), "
            "coalesce(sum(pg_column_size(embedding)),0) FROM memories"
        ).fetchone()
        assert sizes is not None
        self.measurements.update(
            zip(("index_bytes", "table_bytes", "vector_bytes"), map(float, sizes), strict=True)
        )
        if self.candidate["extension"] == "pgroonga":
            source = self.connection.execute(
                "SELECT pgroonga_table_name('lexical_index')"
            ).fetchone()
            assert source is not None
            source_name = string(source[0])
            lexicon_prefix = source_name.replace("Sources", "Lexicon", 1) + "_"
            objects_row = self.connection.execute(
                "SELECT pgroonga_command('object_list')"
            ).fetchone()
            assert objects_row is not None
            objects = record(json.loads(objects_row[0])[1])
            size = 0.0
            for name in objects:
                if name == source_name or name.startswith((source_name + ".", lexicon_prefix)):
                    inspected = self.connection.execute(
                        "SELECT pgroonga_command('object_inspect', ARRAY['name',%s])", (name,)
                    ).fetchone()
                    assert inspected is not None
                    size += number(record(json.loads(inspected[0])[1])["disk_usage"])
            self.measurements["index_bytes"] = size
        self.case = deepcopy(case)

    def search(self, query_vector: tuple[float, ...], *, limit: int) -> list[dict[str, object]]:
        if self.case is None:
            raise ValueError("Search must be indexed first")
        return [
            {key: value for key, value in hit.items() if key not in ("cosine", "reranker_score")}
            for hit in rank_results(self.observe(query_vector), record(self.case["protocol"]))
        ]

    def observe(self, query_vector: tuple[float, ...]) -> list[dict[str, object]]:
        if self.case is None:
            raise ValueError("Search must be indexed first")
        validate_vectors((query_vector,), count=1, dimensions=integer(self.candidate["dimensions"]))
        terms = lexical_terms(string(self.case["query"]))
        matched: dict[str, int] = {}
        for term in terms:
            if self.candidate["extension"] == "pg_bigm":
                literal = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                found = self.connection.execute(
                    "SELECT id FROM memories WHERE search_key LIKE %s", ("%" + literal + "%",)
                ).fetchall()
            else:
                found = self.connection.execute(
                    "SELECT id FROM memories WHERE search_key &@ %s", (term,)
                ).fetchall()
            for (identifier,) in found:
                matched[identifier] = matched.get(identifier, 0) + 1
        observations: list[dict[str, object]] = []
        for identifier, body, distance in self.connection.execute(
            "SELECT id,body,embedding <=> %s::vector FROM memories", (str(list(query_vector)),)
        ).fetchall():
            memory = record(body)
            observations.append(
                {
                    "memory_id": identifier,
                    "cosine": 1 - number(distance),
                    "reranker_score": None,
                    "components": score_components(
                        self.case,
                        memory,
                        semantic_distance=distance,
                        lexical=matched.get(identifier, 0) / len(terms) if terms else 0.0,
                    ),
                }
            )
        ranked = [
            {
                **o,
                "score": composite_score(o["components"], record(self.case["protocol"])["weights"]),
            }
            for o in observations
        ]
        # Evaluate the complete small synthetic set; the use case enforces the return budget.
        for hit in ranked:
            saved = self.connection.execute(
                "SELECT body FROM memories WHERE id=%s", (hit["memory_id"],)
            ).fetchone()
            assert saved is not None
            spans: list[dict[str, object]] = []
            for identifier in strings(record(saved[0])["source_turn_ids"]):
                row = self.connection.execute(
                    "SELECT body FROM turns WHERE id=%s", (identifier,)
                ).fetchone()
                assert row is not None
                turn = record(row[0])
                text = string(turn["text"])
                spans.append(
                    {
                        "turn_id": identifier,
                        "start": 0,
                        "end": len(text),
                        "text": text,
                        "actor": turn["actor"],
                        "language": turn["language"],
                    }
                )
            hit["source_spans"] = spans
        return ranked
