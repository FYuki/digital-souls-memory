from collections.abc import Callable
from datetime import datetime
from typing import cast

import pytest

from tests.evidence_support import RECORDED

pytestmark = pytest.mark.ut


def test_session_ending_retains_the_report_and_its_provenance() -> None:
    from digital_souls_memory.evidence.domain import SessionEnding

    value = SessionEnding(
        session_id="session-1",
        ended_at=RECORDED,
        reported_by="synthetic-window",
        reported_at=RECORDED,
    )
    assert (value.session_id, value.ended_at, value.reported_by, value.reported_at) == (
        "session-1",
        RECORDED,
        "synthetic-window",
        RECORDED,
    )


@pytest.mark.parametrize("field", ["session_id", "reported_by"])
@pytest.mark.parametrize("invalid", ["", " \t\n"])
def test_session_ending_rejects_blank_identifiers(
    field: str, invalid: str, rejected: Callable[[], pytest.RaisesExc[BaseException]]
) -> None:
    from digital_souls_memory.evidence.domain import SessionEnding

    with rejected():
        SessionEnding(
            session_id=invalid if field == "session_id" else "session-1",
            ended_at=RECORDED,
            reported_by=invalid if field == "reported_by" else "synthetic-window",
            reported_at=RECORDED,
        )


@pytest.mark.parametrize("field", ["ended_at", "reported_at"])
def test_session_ending_rejects_timezone_naive_timestamps(
    field: str, rejected: Callable[[], pytest.RaisesExc[BaseException]]
) -> None:
    from digital_souls_memory.evidence.domain import SessionEnding

    naive = RECORDED.replace(tzinfo=None)
    with rejected():
        SessionEnding(
            session_id="session-1",
            ended_at=naive if field == "ended_at" else RECORDED,
            reported_by="synthetic-window",
            reported_at=naive if field == "reported_at" else RECORDED,
        )


def test_session_ending_rejects_missing_end_time(
    rejected: Callable[[], pytest.RaisesExc[BaseException]],
) -> None:
    from digital_souls_memory.evidence.domain import SessionEnding

    with rejected():
        SessionEnding(
            session_id="session-1",
            ended_at=cast(datetime, None),
            reported_by="synthetic-window",
            reported_at=RECORDED,
        )
