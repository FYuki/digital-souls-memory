"""Client timing ends after full body transfer and typed response validation."""

from collections.abc import Iterator

import httpx
import pytest

from digital_souls_memory.client import MemoryClient
from digital_souls_memory.client.models import RecallRequest
from tests.st.performance import measure

pytestmark = pytest.mark.it1


@pytest.mark.parametrize("failure", [False, True])
def test_measurement_includes_response_completion_and_keeps_failures(
    monkeypatch: pytest.MonkeyPatch,
    failure: bool,
) -> None:
    clock = [0.0]
    monkeypatch.setattr("tests.st.performance.perf_counter", lambda: clock[0])

    class DelayedBody(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            yield b'{"provision_id":"p","temperament_version":null,"personality_version":null,'
            clock[0] = 0.7
            if failure:
                raise httpx.ReadError("synthetic interruption")
            yield b'"memories":[],"source_spans":[],"verbatim_anchors":[],"context":null}'

    with MemoryClient(
        base_url="http://synthetic",
        timeout=5,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=DelayedBody())),
    ) as client:
        sample, error = measure(
            client,
            "x",
            RecallRequest(
                session_id="s",
                format="materials",
                turn_id="t",
            ),
        )
    assert sample.elapsed_ms == 700
    assert sample.success is not failure
    assert sample.provision_id == (None if failure else "p")
    assert error == ("MemoryTransportError" if failure else None)
