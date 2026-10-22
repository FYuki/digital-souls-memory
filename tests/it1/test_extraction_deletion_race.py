"""Use barriers to invalidate evidence between the worker read and its transaction."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event

import pytest

from digital_souls_memory.memory.ports import MemoryTransaction
from tests.evidence_support import turn
from tests.it1.test_memory_worker import setup_worker

pytestmark = pytest.mark.it1


@pytest.mark.parametrize("transaction_number", [1, 2], ids=["claim", "result"])
def test_worker_cannot_commit_evidence_removed_before_transaction_lock(
    monkeypatch: pytest.MonkeyPatch, transaction_number: int
) -> None:
    h, store, service, worker = setup_worker()
    h.evidence.add_turn(turn(actor="user-a"))
    waiting, release = Event(), Event()
    original = store.transaction
    calls = 0

    @contextmanager
    def pause_before_lock(character: str) -> Iterator[MemoryTransaction]:
        nonlocal calls
        calls += 1
        if calls == transaction_number:
            waiting.set()
            assert release.wait(5)
        with original(character) as tx:
            yield tx

    monkeypatch.setattr(store, "transaction", pause_before_lock)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(worker.process, "char-b", "turn-1")
        try:
            assert waiting.wait(3)
            # This is the store boundary after a committed deletion, not an author API fake.
            with store.lock:
                h.evidence.turns.pop("turn-1")
                store.states.pop(("char-b", "turn-1"), None)
                store.proposals.pop(("char-b", "turn-1"), None)
                store.dependencies.pop(("char-b", "turn-1"), None)
        finally:
            release.set()
        pending.result(timeout=5)
    assert store.get_extraction("char-b", "turn-1") is None
    assert store.proposals == {}
    assert store.dependencies == {}
    assert store.values == {}
    assert service.active("char-b") == ()
