"""HTTP scenario setup and bounded observations for a dedicated ST deployment."""

import os
from collections.abc import Callable
from datetime import datetime, timedelta
from itertools import count
from threading import Event
from time import monotonic
from uuid import uuid4

import httpx

from digital_souls_memory.client import MemoryClient, MemoryHTTPError
from digital_souls_memory.client.models import (
    CoreInput,
    RecallRequest,
    RecallResponse,
    SeedInput,
    SessionRequest,
    TemperamentInput,
    TurnRequest,
)
from tests.evidence_support import OCCURRED, session_payload, turn_payload
from tests.identity_support import seed_payload

_event_sequence = count()


def eventually(check: Callable[[], bool], *, timeout: float = 30) -> None:
    deadline = monotonic() + timeout
    wake = Event()
    while monotonic() < deadline:
        try:
            if check():
                return
        except MemoryHTTPError as error:
            if error.status_code != 404:
                raise
        wake.wait(0.05)
    assert check(), "ST observation did not reach the expected state"


def new_client() -> MemoryClient:
    return MemoryClient(
        base_url=os.environ["MEMORY_CORE_ST_URL"],
        timeout=30,
        author_token="synthetic-st-credential",
    )


def register(client: MemoryClient, character: str) -> None:
    client.register_core(character, CoreInput(values=("誠実",), prohibitions=("欺かない",)))
    client.register_temperament(character, TemperamentInput(description="合成の穏やかな気質"))
    client.author_seed(character, SeedInput.model_validate(seed_payload()))


def server_time() -> datetime:
    with httpx.Client(base_url=os.environ["MEMORY_CORE_ST_URL"], timeout=30) as client:
        response = client.post("/_st/advance", json={"seconds": 0})
        response.raise_for_status()
        return datetime.fromisoformat(response.json()["now"])


def add_session(client: MemoryClient, *, delivery: bool, started_at: datetime) -> str:
    identifier = "st-" + uuid4().hex
    client.add_session(
        SessionRequest.model_validate(
            session_payload()
            | {
                "session_id": identifier,
                "reports_delivery": delivery,
                "ended_at": None,
                "started_at": started_at.isoformat(),
            }
        )
    )
    return identifier


def add_turn(
    client: MemoryClient,
    session_id: str,
    *,
    actor: str,
    readers: list[str],
    text: str,
    mode: str = "broadcast",
    addressees: list[str] | None = None,
    reply: str | None = None,
    kind: str = "utterance",
) -> str:
    identifier = "st-" + uuid4().hex
    client.add_turn(
        TurnRequest.model_validate(
            turn_payload()
            | {
                "turn_id": identifier,
                "session_id": session_id,
                "actor": actor,
                "perceived_by": readers,
                "text": text,
                "address_mode": mode,
                "addressees": addressees if addressees is not None else [],
                "reply_to_turn_id": reply,
                "kind": kind,
                "occurred_at": (OCCURRED + timedelta(seconds=next(_event_sequence))).isoformat(),
            }
        )
    )
    return identifier


def extraction_done(client: MemoryClient, character: str, turn_id: str) -> bool:
    try:
        state = client.get_extraction(character, turn_id)
    except MemoryHTTPError as error:
        if error.status_code == 404:
            return False
        raise
    assert state.status != "failed", state.error
    return state.status == "done"


def recall(client: MemoryClient, character: str, session_id: str) -> RecallResponse:
    return client.recall(
        character,
        RecallRequest(
            session_id=session_id,
            format="materials",
            query="日本語",
            sensitivity="synthetic-sensitive",
            classifier_version="synthetic-v1",
        ),
    )
