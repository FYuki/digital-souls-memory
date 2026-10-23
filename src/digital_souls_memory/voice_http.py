"""Core-facing approved Voice registration and retrieval."""

from collections.abc import Callable, Coroutine
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from digital_souls_memory.evidence.ports import EvidenceUnavailable
from digital_souls_memory.evidence.use_cases import Clock
from digital_souls_memory.identity.ports import IdentityUnavailable, ReviewResult
from digital_souls_memory.voice.domain import VerbatimAnchor, VoiceExemplar, VoiceValidationError
from digital_souls_memory.voice.ports import VoiceConflict, VoiceNotFound, VoiceUnavailable
from digital_souls_memory.voice.use_cases import VoiceService

Identifier = Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9_-]+$")]
Nonblank = Annotated[StrictStr, Field(pattern=r"\S")]


class VoiceRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[None, None, Response]]:
        handler = super().get_route_handler()

        async def handle(request: Request) -> Response:
            try:
                return await handler(request)
            except (VoiceValidationError, RequestValidationError):
                return JSONResponse(status_code=422, content={"detail": "Invalid voice value"})
            except PermissionError:
                return JSONResponse(status_code=403, content={"detail": "Voice write forbidden"})
            except VoiceConflict:
                return JSONResponse(status_code=409, content={"detail": "Voice already exists"})
            except VoiceNotFound:
                return JSONResponse(status_code=404, content={"detail": "Voice not found"})
            except (VoiceUnavailable, IdentityUnavailable, EvidenceUnavailable):
                return JSONResponse(status_code=503, content={"detail": "Voice unavailable"})

        return handle


class ExemplarInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    exemplar_id: Identifier
    text: Nonblank
    source: StrictStr
    turn_id: Identifier | None
    situation_tags: tuple[Nonblank, ...]
    emotion_tags: tuple[Nonblank, ...]
    speech_register: dict[StrictStr, StrictStr] = Field(alias="register")
    trait_version: Annotated[StrictInt, Field(gt=0)]
    approved_by: Nonblank

    def to_value(self, character_id: str, at: datetime) -> VoiceExemplar:
        return VoiceExemplar(
            character_id=character_id, registered_at=at, **self.model_dump(by_alias=True)
        )


class ExemplarSetInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    exemplars: Annotated[list[ExemplarInput], Field(min_length=1)]


class AnchorInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    anchor_id: Identifier
    turn_id: Identifier
    kind: StrictStr
    reason: Nonblank
    approved_by: Nonblank

    def to_value(self, character_id: str, at: datetime) -> VerbatimAnchor:
        return VerbatimAnchor(character_id=character_id, registered_at=at, **self.model_dump())


class ExemplarOutput(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    character_id: str
    exemplar_id: str
    text: str
    source: str
    turn_id: str | None
    situation_tags: tuple[str, ...]
    emotion_tags: tuple[str, ...]
    speech_register: dict[str, str] = Field(alias="register")
    trait_version: int
    approved_by: str
    registered_at: datetime


class AnchorOutput(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    character_id: str
    anchor_id: str
    turn_id: str
    kind: str
    reason: str
    approved_by: str
    registered_at: datetime
    text: str

    @classmethod
    def from_material(cls, material: tuple[VerbatimAnchor, str]) -> "AnchorOutput":
        value, text = material
        return cls(
            character_id=value.character_id,
            anchor_id=value.anchor_id,
            turn_id=value.turn_id,
            kind=value.kind,
            reason=value.reason,
            approved_by=value.approved_by,
            registered_at=value.registered_at,
            text=text,
        )


class CheckOutput(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    consistent: bool
    reason: str


class RegistrationOutput(BaseModel):
    consistency_check: CheckOutput

    @classmethod
    def from_result(cls, result: ReviewResult) -> "RegistrationOutput":
        return cls(consistency_check=CheckOutput.model_validate(result))


def voice_router(service: Callable[[], VoiceService], clock: Clock) -> APIRouter:
    router = APIRouter(prefix="/characters/{character_id}", route_class=VoiceRoute)
    Service = Annotated[VoiceService, Depends(service)]

    @router.post("/voice-exemplars", response_model=RegistrationOutput, status_code=201)
    def register_exemplars(
        character_id: Identifier, body: ExemplarSetInput, svc: Service
    ) -> RegistrationOutput:
        at = clock()
        result = svc.register_exemplars(
            character_id, [item.to_value(character_id, at) for item in body.exemplars]
        )
        return RegistrationOutput.from_result(result)

    @router.get("/voice-exemplars", response_model=list[ExemplarOutput])
    def list_exemplars(character_id: Identifier, svc: Service) -> list[VoiceExemplar]:
        return svc.list_exemplars(character_id)

    @router.get("/voice-exemplars/{exemplar_id}", response_model=ExemplarOutput)
    def get_exemplar(
        character_id: Identifier, exemplar_id: Identifier, svc: Service
    ) -> VoiceExemplar:
        return svc.get_exemplar(character_id, exemplar_id)

    @router.post("/verbatim-anchors", response_model=AnchorOutput, status_code=201)
    def register_anchor(character_id: Identifier, body: AnchorInput, svc: Service) -> AnchorOutput:
        return AnchorOutput.from_material(svc.register_anchor(body.to_value(character_id, clock())))

    @router.get("/verbatim-anchors", response_model=list[AnchorOutput])
    def list_anchors(character_id: Identifier, svc: Service) -> list[AnchorOutput]:
        return [AnchorOutput.from_material(value) for value in svc.list_anchors(character_id)]

    @router.get("/verbatim-anchors/{anchor_id}", response_model=AnchorOutput)
    def get_anchor(character_id: Identifier, anchor_id: Identifier, svc: Service) -> AnchorOutput:
        return AnchorOutput.from_material(svc.get_anchor(character_id, anchor_id))

    return router
