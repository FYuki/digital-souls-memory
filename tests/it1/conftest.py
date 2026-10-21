from collections.abc import Iterator
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from tests.recall_support import RecallHarness


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Iterator["RecallHarness"]:
    from tests.recall_support import RecallHarness

    monkeypatch.delenv("MEMORY_DATABASE_URL", raising=False)
    h = RecallHarness()
    with h.client:
        yield h
