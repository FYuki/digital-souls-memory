import os

import pytest

from digital_souls_memory.evaluation.adapters.postgres import PostgresSearch
from digital_souls_memory.evaluation.models import rows

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("extension", ["pg_bigm", "pgroonga"])
def test_postgres_recovers_declared_source_offsets_from_saved_memory(
    source_case: dict[str, object],
    candidate: dict[str, object],
    new_protocol: dict[str, object],
    extension: str,
) -> None:
    source_case["protocol"] = new_protocol
    with PostgresSearch(
        {**candidate, "extension": extension}, admin_url=os.environ["MEMORY_TEST_DATABASE_URL"]
    ) as search:
        search.index(source_case, ((1.0, 0.0),))
        observed = search.observe((1.0, 0.0))
    span = rows(observed[0]["source_spans"])[0]
    assert (span["text"], span["start"], span["end"]) == ("青い駅。", 3, 7)
    assert span["actor"] == "user"
    assert span["language"] == "ja"
