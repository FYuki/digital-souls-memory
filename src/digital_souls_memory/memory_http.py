"""Core-facing derived memory queries and asynchronous re-extraction requests."""

from collections.abc import Callable, Coroutine
from datetime import datetime

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict

from digital_souls_memory.evidence.ports import EvidenceUnavailable
from digital_souls_memory.memory.domain import (
    DeliveryBasis,
    Derivation,
    Epistemics,
    IdentityLinks,
    MemoryValidationError,
)
from digital_souls_memory.memory.ports import MemoryConflict, MemoryUnavailable
from digital_souls_memory.memory.use_cases import MemoryService


class MemoryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    character_id: str
    kind: str
    content: str
    source_turn_ids: tuple[str, ...]
    derivation: Derivation
    valid_from: datetime
    valid_to: datetime | None
    recorded_at: datetime
    superseded_at: datetime | None
    last_reinforced_at: datetime | None
    epistemics: Epistemics
    identity_links: IdentityLinks
    subject: str | None
    predicate: str | None
    object: str | None
    shared_with: tuple[str, ...]
    counterpart: str | None
    affection: float | None
    trust: float | None
    status: str | None
    deadline: datetime | None
    verbatim_turn_id: str | None


class ExtractionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    character_id: str
    turn_id: str
    delivery_basis: DeliveryBasis
    status: str
    deadline: datetime
    error: str | None
    receipt_version: int


class MemoryRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[None, None, Response]]:
        handler = super().get_route_handler()

        async def handle(request: Request) -> Response:
            try:
                return await handler(request)
            except MemoryValidationError:
                return JSONResponse(status_code=422, content={"detail": "Invalid memory value"})
            except MemoryConflict:
                return JSONResponse(status_code=409, content={"detail": "Memory already exists"})
            except (MemoryUnavailable, EvidenceUnavailable):
                return JSONResponse(status_code=503, content={"detail": "Memory unavailable"})

        return handle


def memory_router(service: Callable[[], MemoryService]) -> APIRouter:
    router = APIRouter(route_class=MemoryRoute)

    @router.get("/characters/{character_id}/memories")
    def memories(character_id: str) -> list[MemoryResponse]:
        return [MemoryResponse.model_validate(v) for v in service().active(character_id)]

    @router.get("/characters/{character_id}/extractions/{turn_id}")
    def state(character_id: str, turn_id: str) -> Response:
        value = service().store.get_extraction(character_id, turn_id)
        if value is None:
            return JSONResponse(status_code=404, content={"detail": "Extraction not found"})
        return Response(
            content=ExtractionResponse.model_validate(value).model_dump_json(),
            media_type="application/json",
        )

    @router.post("/turns/{turn_id}/extractions", status_code=202)
    def requeue(turn_id: str) -> JSONResponse:
        resolved = service()
        if resolved.evidence.get_turn(turn_id) is None:
            return JSONResponse(status_code=404, content={"detail": "Turn not found"})
        resolved.requeue(turn_id)
        return JSONResponse(status_code=202, content={"status": "pending"})

    return router
