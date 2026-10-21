from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest

from digital_souls_memory.evidence.domain import DeliveryReceipt
from tests.evidence_support import RECORDED, receipt, session, turn

if TYPE_CHECKING:
    from tests.recall_support import RecallHarness

pytestmark = pytest.mark.it1


@pytest.mark.parametrize("count", [100, 1000, 10000])
def test_origin_streams_receipt_history_without_retaining_it(
    harness: "RecallHarness", monkeypatch: pytest.MonkeyPatch, count: int
) -> None:
    h = harness
    h.evidence.sessions["session-1"] = session()
    h.evidence.add_turn(turn(text="日本語の原文"))
    live = peak = consumed = 0
    active = False

    class TrackedReceipt(DeliveryReceipt):
        def __post_init__(self) -> None:
            nonlocal live, peak
            super().__post_init__()
            live += 1
            peak = max(peak, live)

        def __del__(self) -> None:
            nonlocal live
            live -= 1

    def generate() -> Iterator[DeliveryReceipt]:
        nonlocal consumed
        for i in range(count):
            assert active
            consumed += 1
            status, end = ("full", 6) if i < count - 1 else ("partial", 3)
            yield TrackedReceipt(**asdict(receipt(status, end)))

    @contextmanager
    def history(turn_id: str) -> Iterator[Iterator[DeliveryReceipt]]:
        nonlocal active
        assert turn_id == "turn-1"
        active = True
        try:
            yield generate()
        finally:
            active = False

    monkeypatch.setattr(h.evidence, "list_receipts", history)
    # The save double also validates the origin against the same receipt history.
    response = h.client.post(
        "/characters/char-b/recall",
        json={"session_id": "session-1", "format": "materials", "turn_id": "turn-1"},
    )
    assert response.is_success
    assert h.embedding.calls[-1][0].texts == ("日本語",)
    (stamp,) = h.provisions.snapshots[-1].sources
    assert (stamp.receipt_version, stamp.available_end, stamp.used_end) == (count, 3, 3)
    assert consumed == count * 2
    assert peak <= 3
    assert live == 0 and not active
    assert h.provisions.get(response.json()["provision_id"]) is not None


def test_origin_becomes_available_at_delivery_deadline(harness: "RecallHarness") -> None:
    h = harness
    h.evidence.sessions["session-1"] = session()
    h.evidence.add_turn(turn(text="日本語の原文"))
    payload = {"session_id": "session-1", "format": "materials", "turn_id": "turn-1"}
    h.now = RECORDED + timedelta(seconds=599)
    assert h.client.post("/characters/char-b/recall", json=payload).status_code == 404
    assert h.provisions.records == {}
    h.now += timedelta(seconds=1)
    response = h.client.post("/characters/char-b/recall", json=payload)
    assert response.is_success
    assert h.embedding.calls[-1][0].texts == ("日本語の原文",)
    (stamp,) = h.provisions.snapshots[-1].sources
    assert (stamp.basis, stamp.receipt_version, stamp.available_end) == ("assumed_full", 0, 6)
    assert h.provisions.get(response.json()["provision_id"]) is not None
