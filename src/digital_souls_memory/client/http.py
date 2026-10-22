"""Synchronous HTTP operations for Core."""

from math import isfinite
from types import TracebackType
from typing import Self
from urllib.parse import quote

import httpx
from pydantic import BaseModel, TypeAdapter, ValidationError

from digital_souls_memory.client import models as m


class MemoryHTTPError(Exception):
    def __init__(self, status_code: int, detail: object) -> None:
        self.status_code = status_code
        self.detail = detail
        # Detail may echo caller input; exception display never includes it or headers.
        super().__init__(f"Memory HTTP {status_code}")


class MemoryTransportError(Exception):
    """The complete response could not be transferred."""


class MemoryTimeoutError(MemoryTransportError):
    """A configured HTTP timeout elapsed."""


class MemoryResponseError(Exception):
    """The successful response violated its wire contract."""


def _segment(value: str) -> str:
    if not value or value in (".", ".."):
        raise ValueError("Invalid path identifier")
    return quote(value, safe="")


class MemoryClient:
    def __init__(
        self,
        *,
        base_url: str,
        timeout: float,
        author_token: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if isinstance(timeout, bool) or not isfinite(timeout) or timeout <= 0:
            raise ValueError("Expected a positive finite timeout")
        self._author_token = author_token
        self._client = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            transport=transport,
            trust_env=False,
            follow_redirects=False,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _request[T](
        self,
        method: str,
        path: str,
        result: TypeAdapter[T],
        body: BaseModel | None = None,
        *,
        author: bool = False,
    ) -> T:
        headers = {}
        if author and self._author_token is not None:
            headers["Authorization"] = f"Bearer {self._author_token}"
        try:
            # A stream context also closes a body interrupted during read().
            with self._client.stream(
                method,
                path,
                headers=headers,
                json=body.model_dump(mode="json", by_alias=True) if body is not None else None,
            ) as response:
                content = response.read()
                if not response.is_success:
                    try:
                        error = response.json()
                    except ValueError:
                        raise MemoryResponseError("Invalid HTTP error response") from None
                    if not isinstance(error, dict) or "detail" not in error:
                        raise MemoryResponseError("Missing HTTP error detail")
                    raise MemoryHTTPError(response.status_code, error["detail"])
                try:
                    return result.validate_json(content)
                except (ValidationError, ValueError):
                    raise MemoryResponseError("Invalid Memory response") from None
        except httpx.TimeoutException:
            raise MemoryTimeoutError("Memory request timed out") from None
        except httpx.TransportError:
            raise MemoryTransportError("Memory transfer failed") from None

    def add_session(self, body: m.SessionRequest) -> m.SessionResponse:
        return self._request("POST", "/sessions", TypeAdapter(m.SessionResponse), body)

    def get_session(self, session_id: str) -> m.SessionResponse:
        return self._request(
            "GET", f"/sessions/{_segment(session_id)}", TypeAdapter(m.SessionResponse)
        )

    def add_turn(self, body: m.TurnRequest) -> m.TurnResponse:
        return self._request("POST", "/turns", TypeAdapter(m.TurnResponse), body)

    def get_turn(self, turn_id: str) -> m.TurnResponse:
        return self._request("GET", f"/turns/{_segment(turn_id)}", TypeAdapter(m.TurnResponse))

    def add_receipt(self, turn_id: str, body: m.ReceiptRequest) -> m.ReceiptResponse:
        return self._request(
            "POST",
            f"/turns/{_segment(turn_id)}/receipts",
            TypeAdapter(m.ReceiptResponse),
            body,
        )

    def list_receipts(self, turn_id: str) -> list[m.ReceiptResponse]:
        return self._request(
            "GET",
            f"/turns/{_segment(turn_id)}/receipts",
            TypeAdapter(list[m.ReceiptResponse]),
        )

    def list_perceived_turns(self, character_id: str) -> list[m.TurnResponse]:
        return self._request(
            "GET",
            f"/characters/{_segment(character_id)}/turns",
            TypeAdapter(list[m.TurnResponse]),
        )

    def recall(self, character_id: str, body: m.RecallRequest) -> m.RecallResponse:
        return self._request(
            "POST",
            f"/characters/{_segment(character_id)}/recall",
            TypeAdapter(m.RecallResponse),
            body,
        )

    def resident(self, character_id: str, body: m.ResidentRequest) -> m.ResidentOutput:
        return self._request(
            "POST",
            f"/characters/{_segment(character_id)}/resident",
            TypeAdapter(m.ResidentOutput),
            body,
        )

    def register_core(self, character_id: str, body: m.CoreInput) -> m.CoreOutput:
        return self._request(
            "POST",
            f"/characters/{_segment(character_id)}/core-identities",
            TypeAdapter(m.CoreOutput),
            body,
        )

    def register_temperament(
        self, character_id: str, body: m.TemperamentInput
    ) -> m.TemperamentOutput:
        return self._request(
            "POST",
            f"/characters/{_segment(character_id)}/temperaments",
            TypeAdapter(m.TemperamentOutput),
            body,
        )

    def author_seed(self, character_id: str, body: m.SeedInput) -> m.ProfileOutput:
        return self._request(
            "POST",
            f"/characters/{_segment(character_id)}/personality-profiles/author-seed",
            TypeAdapter(m.ProfileOutput),
            body,
        )

    def get_identity(self, character_id: str) -> m.CurrentOutput:
        return self._request(
            "GET",
            f"/characters/{_segment(character_id)}/identity/current",
            TypeAdapter(m.CurrentOutput),
        )

    def register_exemplars(
        self, character_id: str, body: m.ExemplarSetInput
    ) -> m.RegistrationOutput:
        return self._request(
            "POST",
            f"/characters/{_segment(character_id)}/voice-exemplars",
            TypeAdapter(m.RegistrationOutput),
            body,
        )

    def list_exemplars(self, character_id: str) -> list[m.ExemplarOutput]:
        return self._request(
            "GET",
            f"/characters/{_segment(character_id)}/voice-exemplars",
            TypeAdapter(list[m.ExemplarOutput]),
        )

    def register_anchor(self, character_id: str, body: m.AnchorInput) -> m.AnchorOutput:
        return self._request(
            "POST",
            f"/characters/{_segment(character_id)}/verbatim-anchors",
            TypeAdapter(m.AnchorOutput),
            body,
        )

    def delete(self, body: m.DeletionRequest) -> m.DeletionResponse:
        return self._request(
            "POST",
            "/deletion-requests",
            TypeAdapter(m.DeletionResponse),
            body,
            author=True,
        )

    def list_memories(self, character_id: str) -> list[m.MemoryResponse]:
        return self._request(
            "GET",
            f"/characters/{_segment(character_id)}/memories",
            TypeAdapter(list[m.MemoryResponse]),
        )

    def get_extraction(self, character_id: str, turn_id: str) -> m.ExtractionResponse:
        return self._request(
            "GET",
            f"/characters/{_segment(character_id)}/extractions/{_segment(turn_id)}",
            TypeAdapter(m.ExtractionResponse),
        )

    def replace_profile(
        self, character_id: str, body: m.ProfileRequest
    ) -> m.CharacterProfileOutput:
        return self._request(
            "PUT",
            f"/characters/{_segment(character_id)}/profile",
            TypeAdapter(m.CharacterProfileOutput),
            body,
        )

    def get_state(self, character_id: str, session_id: str, *, surface: str) -> m.StateOutput:
        return self._request(
            "GET",
            f"/characters/{_segment(character_id)}/sessions/{_segment(session_id)}/state"
            f"?surface={quote(surface, safe='')}",
            TypeAdapter(m.StateOutput),
        )
