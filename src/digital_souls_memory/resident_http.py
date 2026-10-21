"""Core HTTP boundary for resident composition, profiles and short-lived state."""

from collections.abc import Callable, Coroutine
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from digital_souls_memory.evidence.ports import (
    EvidenceConflict,
    EvidenceNotFound,
    EvidenceUnavailable,
)
from digital_souls_memory.evidence.provision import ProvisionConflict, ProvisionUnavailable
from digital_souls_memory.evidence_http import Identifier, Nonblank
from digital_souls_memory.identity.ports import IdentityUnavailable
from digital_souls_memory.identity_http import CoreOutput, TemperamentOutput
from digital_souls_memory.resident.domain import ContextKey
from digital_souls_memory.resident.profiles import ProfileService
from digital_souls_memory.resident.states import ContextReader
from digital_souls_memory.resident.use_cases import ResidentContext, ResidentService
from digital_souls_memory.voice.ports import VoiceUnavailable
from digital_souls_memory.voice_http import ExemplarOutput


class ResidentRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[None, None, Response]]:
        handler = super().get_route_handler()

        async def handle(request: Request) -> Response:
            try:
                return await handler(request)
            except (ValueError, RequestValidationError):
                return JSONResponse(status_code=422, content={"detail": "Invalid context value"})
            except EvidenceNotFound:
                return JSONResponse(status_code=404, content={"detail": "Context not found"})
            except (EvidenceConflict, ProvisionConflict):
                return JSONResponse(status_code=409, content={"detail": "Context conflict"})
            except (
                EvidenceUnavailable,
                ProvisionUnavailable,
                IdentityUnavailable,
                VoiceUnavailable,
            ):
                return JSONResponse(status_code=503, content={"detail": "Context unavailable"})

        return handle


class ResidentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: Identifier
    query: Nonblank | None = None


class ProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token_limit: Annotated[StrictInt, Field(ge=0)]
    entries: tuple[dict[str, object], ...]


class ProfileSourceOutput(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    turn_id: str
    start: int
    end: int
    text: str
    actor: str
    language: str
    truncated: bool
    original_start: int
    original_end: int


class ProfileEntryOutput(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    entry_id: str
    kind: str
    text: str
    source_spans: tuple[ProfileSourceOutput, ...]
    owner_id: str | None
    memory_id: str | None


class CharacterProfileOutput(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    character_id: str
    token_limit: int
    entries: tuple[ProfileEntryOutput, ...]
    version: int


class PersonalityDescriptionOutput(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    description: str
    version: int
    renderer_version: str


class ResidentOutput(BaseModel):
    provision_id: str
    core_identity: CoreOutput | None
    temperament: TemperamentOutput | None
    personality_profile: PersonalityDescriptionOutput | None
    character_profile: CharacterProfileOutput
    motivations: tuple[()]
    voice_exemplars: tuple[ExemplarOutput, ...]
    temperament_version: int | None
    personality_version: int | None

    @classmethod
    def from_context(cls, value: ResidentContext) -> "ResidentOutput":
        return cls(
            provision_id=value.provision_id,
            core_identity=CoreOutput.model_validate(value.core_identity)
            if value.core_identity
            else None,
            temperament=TemperamentOutput.model_validate(value.temperament)
            if value.temperament
            else None,
            personality_profile=PersonalityDescriptionOutput.model_validate(
                value.personality_profile
            )
            if value.personality_profile
            else None,
            character_profile=CharacterProfileOutput.model_validate(value.character_profile),
            motivations=(),
            voice_exemplars=tuple(ExemplarOutput.model_validate(v) for v in value.voice_exemplars),
            temperament_version=value.temperament_version,
            personality_version=value.personality_version,
        )


class DependencyOutput(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    turn_id: str
    character_id: str
    basis: str
    receipt_version: int
    available_end: int
    used_end: int
    deadline: datetime


class CharacterStateOutput(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    mood: str | None
    relationship_temperature: str | None
    current_topic: str | None
    character_id: str
    session_id: str
    surface: str
    updated_at: datetime
    expires_at: datetime
    input_signature: str
    dependencies: tuple[DependencyOutput, ...]


class StateOutput(BaseModel):
    state: CharacterStateOutput | None


def resident_router(
    service: Callable[[], ResidentService],
    profiles: Callable[[], ProfileService],
    reader: Callable[[], ContextReader],
) -> APIRouter:
    router = APIRouter(route_class=ResidentRoute)

    @router.put("/characters/{character_id}/profile")
    def replace_profile(character_id: str, request: ProfileRequest) -> CharacterProfileOutput:
        value = profiles().replace(
            character_id=character_id, token_limit=request.token_limit, entries=request.entries
        )
        return CharacterProfileOutput.model_validate(value)

    @router.post("/characters/{character_id}/resident")
    def provide(character_id: str, request: ResidentRequest) -> ResidentOutput:
        value = service().provide(
            character_id=character_id, session_id=request.session_id, query=request.query
        )
        return ResidentOutput.from_context(value)

    @router.get("/characters/{character_id}/sessions/{session_id}/state")
    def get_state(character_id: str, session_id: str, surface: str) -> StateOutput:
        value = reader().state(
            ContextKey(character_id=character_id, session_id=session_id, surface=surface)
        )
        return StateOutput(
            state=CharacterStateOutput.model_validate(value) if value is not None else None
        )

    return router
