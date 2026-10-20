import json
from collections.abc import Iterator
from contextlib import contextmanager

import anyio
import psycopg
import pytest
from starlette.types import Message

from digital_souls_memory.api import create_app
from digital_souls_memory.evidence.domain import DeliveryReceipt, Turn
from digital_souls_memory.evidence.ports import EvidenceUnavailable
from tests.evidence_support import RECORDED, receipt, session, turn
from tests.it1.evidence_fakes import FakeEvidenceStore

pytestmark = pytest.mark.it1


class LazyStore(FakeEvidenceStore):
    def __init__(self, failure: str) -> None:
        super().__init__()
        self.failure = failure
        self.consumed = 0
        self.closed = False
        self.add_session(session())
        self.add_turn(turn())

    def values[T](self, value: T) -> Iterator[T]:
        for index in range(257):
            if self.failure == "driver-first" and index == 0:
                raise psycopg.OperationalError("synthetic fetch failure")
            if self.failure == "first" and index == 0:
                raise EvidenceUnavailable()
            if self.failure == "later" and index == 1:
                raise EvidenceUnavailable()
            self.consumed += 1
            yield value

    @contextmanager
    def listing[T](self, value: T) -> Iterator[Iterator[T]]:
        try:
            if self.failure == "start":
                raise EvidenceUnavailable()
            yield self.values(value)
        except psycopg.Error as error:
            raise EvidenceUnavailable() from error
        finally:
            self.closed = True
            if self.failure == "close":
                raise EvidenceUnavailable()

    @contextmanager
    def list_receipts(self, turn_id: str) -> Iterator[Iterator[DeliveryReceipt]]:
        assert turn_id == "turn-1"
        with self.listing(receipt("none", 0)) as values:
            yield values

    @contextmanager
    def list_perceived_turns(self, character_id: str) -> Iterator[Iterator[Turn]]:
        assert character_id == "char-a"
        with self.listing(turn()) as values:
            yield values


@pytest.mark.parametrize("path", ["/turns/turn-1/receipts", "/characters/char-a/turns"])
@pytest.mark.parametrize(
    "failure", ["none", "start", "first", "driver-first", "later", "close", "disconnect", "send"]
)
def test_list_stream_owns_resources_and_does_not_read_ahead(path: str, failure: str) -> None:
    store = LazyStore(failure)
    messages: list[Message] = []

    async def exercise() -> None:
        disconnected = anyio.Event()

        async def receive() -> Message:
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(message: Message) -> None:
            messages.append(message)
            if message["type"] == "http.response.body":
                body = message.get("body", b"")
                if body.startswith(b"{") and failure in {"disconnect", "send"}:
                    assert store.consumed == 1
                    if failure == "send":
                        raise OSError("synthetic send failure")
                    disconnected.set()
                    await anyio.sleep_forever()
                if body == b"]":
                    assert store.closed

        app = create_app(store=store, clock=lambda: RECORDED)
        with anyio.fail_after(10):
            await app(
                {
                    "type": "http",
                    "asgi": {"version": "3.0", "spec_version": "2.0"},
                    "http_version": "1.1",
                    "method": "GET",
                    "scheme": "http",
                    "path": path,
                    "raw_path": path.encode(),
                    "query_string": b"",
                    "root_path": "",
                    "headers": [],
                    "server": ("test", 80),
                    "client": ("test", 1),
                },
                receive,
                send,
            )

    if failure in {"later", "close", "send"}:
        with pytest.raises(OSError if failure == "send" else EvidenceUnavailable):
            anyio.run(exercise)
    else:
        anyio.run(exercise)
    assert store.closed
    starts = [message for message in messages if message["type"] == "http.response.start"]
    assert starts[0]["status"] == (503 if failure in {"start", "first", "driver-first"} else 200)
    bodies = [
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    ]
    if failure == "none":
        assert len(json.loads(b"".join(bodies))) == 257
        assert store.consumed == 257
    elif failure in {"start", "first", "driver-first"}:
        assert b"[" not in bodies
    else:
        assert b"]" not in bodies
        assert not any(message.get("more_body") is False for message in messages)
        if failure in {"later", "disconnect", "send"}:
            assert store.consumed == 1
