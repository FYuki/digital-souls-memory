"""Core HTTP boundary for turn recall."""

from collections.abc import Callable, Coroutine
from contextlib import AbstractContextManager
from typing import Literal, Self

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, model_validator

from digital_souls_memory.evidence.ports import EvidenceUnavailable
from digital_souls_memory.evidence.provision import ProvisionConflict, ProvisionUnavailable
from digital_souls_memory.identity.ports import IdentityUnavailable
from digital_souls_memory.recall.context import serialize_response
from digital_souls_memory.recall.use_cases import RecallNotFound, RecallService


class RecallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    session_id: str = Field(min_length=1)
    format: Literal["materials", "assembled"]
    query: str | None = Field(default=None, min_length=1)
    turn_id: str | None = Field(default=None, min_length=1)
    sensitivity: str | None = Field(default=None, min_length=1)
    classifier_version: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_query(self) -> Self:
        if (self.query is None) == (self.turn_id is None):
            raise ValueError("Exactly one query or turn_id is required")
        if self.query is not None and (self.sensitivity is None or self.classifier_version is None):
            raise ValueError("Query classification is required")
        return self


class RecallRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[None, None, Response]]:
        handler = super().get_route_handler()

        async def handle(request: Request) -> Response:
            try:
                return await handler(request)
            except RecallNotFound:
                return JSONResponse(status_code=404, content={"detail": "Recall source not found"})
            except ProvisionConflict:
                return JSONResponse(status_code=409, content={"detail": "Provision already exists"})
            except (ProvisionUnavailable, EvidenceUnavailable, IdentityUnavailable, RuntimeError):
                return JSONResponse(status_code=503, content={"detail": "Recall unavailable"})

        return handle


def recall_router(service: Callable[[], AbstractContextManager[RecallService]]) -> APIRouter:
    router = APIRouter(route_class=RecallRoute)

    @router.post("/characters/{character_id}/recall")
    def recall(character_id: str, request: RecallRequest) -> Response:
        with service() as configured:
            result = configured.recall(character_id=character_id, **request.model_dump())
        return Response(content=serialize_response(result), media_type="application/json")

    return router
