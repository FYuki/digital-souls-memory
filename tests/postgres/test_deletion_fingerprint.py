"""Full conflict checks consume unrelated evidence in bounded batches."""

import os
import tracemalloc

import psycopg
import pytest

from digital_souls_memory.adapters.postgres_deletion import PostgresDeletionStore
from digital_souls_memory.governance.domain import AuthorPrincipal, DeletionCommand
from tests.evidence_support import RECORDED, turn
from tests.postgres.test_deletion import deletion_url as deletion_url
from tests.postgres.test_deletion import seed_evidence

pytestmark = [pytest.mark.postgres, pytest.mark.usefixtures("deletion_url")]


def test_fingerprint_memory_does_not_grow_with_all_unaffected_texts() -> None:
    url = os.environ["MEMORY_TEST_DATABASE_URL"]
    store = PostgresDeletionStore(os.environ["MEMORY_DELETION_DATABASE_URL"])
    empty = store.begin(
        DeletionCommand(request_id="empty-scan", target_turn_ids=("absent",)),
        principal=AuthorPrincipal(author_id="synthetic-author"),
        now=RECORDED,
    )
    assert store.commit(empty, replacements=(), now=RECORDED).status == "done"
    evidence = seed_evidence(url)
    evidence.add_turn(turn())
    peaks = []
    for start, end in ((1, 128), (129, 2048)):
        with psycopg.connect(url) as conn:
            conn.execute(
                "INSERT INTO turns SELECT (jsonb_populate_record(NULL::turns, to_jsonb(t) || "
                "jsonb_build_object('turn_id','bulk-'||i,'text',repeat('x',8192)))).* "
                "FROM turns t CROSS JOIN generate_series(%s::integer,%s::integer) i "
                "WHERE t.turn_id='turn-1'",
                (start, end),
            )
        tracemalloc.start()
        try:
            attempt = store.begin(
                DeletionCommand(request_id=f"scan-{end}", target_turn_ids=("absent",)),
                principal=AuthorPrincipal(author_id="synthetic-author"),
                now=RECORDED,
            )
            assert attempt.snapshot is not None and attempt.snapshot.copies == ()
            assert store.commit(attempt, replacements=(), now=RECORDED).status == "done"
            peaks.append(tracemalloc.get_traced_memory()[1])
        finally:
            tracemalloc.stop()
    print(f"fingerprint traced peaks: {peaks}")
    # Adding 15 MiB of unrelated originals must not retain them together in Python.
    assert peaks[1] < peaks[0] + 4 * 1024 * 1024
