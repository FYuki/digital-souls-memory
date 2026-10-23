import os
from copy import deepcopy
from typing import cast

import pytest

from digital_souls_memory.evaluation.adapters.postgres import PostgresSearch

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("extension", ["pg_bigm", "pgroonga"])
def test_postgres_observes_every_eligible_candidate_before_threshold(
    candidate: dict[str, object],
    v2_suite: dict[str, object],
    memory: dict[str, object],
    extension: str,
) -> None:
    candidate["extension"] = extension
    case = deepcopy(cast(list[dict[str, object]], v2_suite["cases"])[0])
    case["protocol"] = {**cast(dict[str, object], v2_suite["protocol"]), "threshold": 0.9}
    case["memories"] = [{**memory, "id": f"m{i}"} for i in range(7)]
    vectors = tuple((1.0, 0.0) if i == 0 else (-1.0, 0.0) for i in range(7))
    with PostgresSearch(candidate, admin_url=os.environ["MEMORY_TEST_DATABASE_URL"]) as search:
        search.index(case, vectors)
        legacy = search.search((1.0, 0.0), limit=5)
        observed = search.observe((1.0, 0.0))
        assert {o["memory_id"] for o in observed} == {f"m{i}" for i in range(7)}
        negative = next(o for o in observed if o["memory_id"] == "m6")
        assert negative["cosine"] == pytest.approx(-1.0)
        assert cast(dict[str, object], negative["components"])["semantic"] == 0.0
        assert set(cast(dict[str, object], negative["components"])) == {
            "semantic",
            "lexical",
            "recency",
            "importance",
            "self_relevance",
            "trust",
        }
        assert negative["reranker_score"] is None
        assert search.search((1.0, 0.0), limit=5) == legacy
        assert all(float(str(o["score"])) < 0.9 for o in observed)
