"""Core-facing identity HTTP contracts; numerical aspects stay inside Memory."""

from collections.abc import Callable, Coroutine
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from digital_souls_memory.identity.domain import (
    CoreIdentity,
    IdentityValidationError,
    PersonalityProfile,
    Temperament,
)
from digital_souls_memory.identity.ports import (
    IdentityConflict,
    IdentityInconsistent,
    IdentityNotFound,
    IdentityUnavailable,
)
from digital_souls_memory.identity.use_cases import IdentityService

Nonblank = Annotated[StrictStr, Field(pattern=r"\S")]
Identifier = Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9_-]+$")]


class IdentityRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[None, None, Response]]:
        handler = super().get_route_handler()

        async def handle(request: Request) -> Response:
            try:
                return await handler(request)
            except (IdentityValidationError, RequestValidationError):
                # Do not echo nonfinite JSON values or request contents in validation errors.
                return JSONResponse(status_code=422, content={"detail": "Invalid identity value"})
            except PermissionError:
                return JSONResponse(status_code=403, content={"detail": "Identity write forbidden"})
            except IdentityConflict:
                return JSONResponse(status_code=409, content={"detail": "Identity already exists"})
            except IdentityNotFound:
                return JSONResponse(status_code=404, content={"detail": "Identity not found"})
            except IdentityUnavailable:
                return JSONResponse(status_code=503, content={"detail": "Identity unavailable"})
            except IdentityInconsistent as error:
                return JSONResponse(status_code=422, content={"detail": str(error)})

        return handle


class CoreInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    values: tuple[Nonblank, ...]
    prohibitions: tuple[Nonblank, ...]


class TemperamentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: Nonblank


class SeedInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # The domain owns strict numeric validation, including bool and numeric strings.
    aspects: dict[str, object]
    renderer_version: Nonblank


class CoreOutput(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    character_id: str
    values: tuple[str, ...]
    prohibitions: tuple[str, ...]
    version: int
    registered_at: datetime


class TemperamentOutput(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    character_id: str
    description: str
    version: int
    registered_at: datetime


class ProfileOutput(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    character_id: str
    description: str
    renderer_version: str
    temperament_version: int
    version: int
    valid_from: datetime
    valid_to: datetime | None


class CurrentOutput(BaseModel):
    core_identity: CoreOutput | None
    temperament: TemperamentOutput | None
    personality_profile: ProfileOutput | None


def identity_router(service: Callable[[], IdentityService]) -> APIRouter:
    router = APIRouter(prefix="/characters/{character_id}", route_class=IdentityRoute)
    Service = Annotated[IdentityService, Depends(service)]

    @router.post("/core-identities", response_model=CoreOutput, status_code=201)
    def register_core(character_id: Identifier, body: CoreInput, svc: Service) -> CoreIdentity:
        return svc.register_core(
            character_id, body.values, body.prohibitions, source="registration"
        )

    @router.post("/temperaments", response_model=TemperamentOutput, status_code=201)
    def register_temperament(
        character_id: Identifier, body: TemperamentInput, svc: Service
    ) -> Temperament:
        return svc.register_temperament(character_id, body.description, source="registration")

    @router.post("/personality-profiles/author-seed", response_model=ProfileOutput, status_code=201)
    def seed(character_id: Identifier, body: SeedInput, svc: Service) -> PersonalityProfile:
        return svc.author_seed(
            character_id, body.aspects, body.renderer_version, source="registration"
        )

    @router.get("/core-identities/{version}", response_model=CoreOutput)
    def get_core(character_id: Identifier, version: int, svc: Service) -> CoreIdentity:
        return svc.get_core(character_id, version)

    @router.get("/temperaments/{version}", response_model=TemperamentOutput)
    def get_temperament(character_id: Identifier, version: int, svc: Service) -> Temperament:
        return svc.get_temperament(character_id, version)

    @router.get("/personality-profiles/{version}", response_model=ProfileOutput)
    def get_profile(character_id: Identifier, version: int, svc: Service) -> PersonalityProfile:
        return svc.get_profile(character_id, version)

    @router.get("/identity/current", response_model=CurrentOutput)
    def current(character_id: Identifier, svc: Service) -> CurrentOutput:
        core, temperament, profile = svc.get_current(character_id)
        return CurrentOutput(
            core_identity=CoreOutput.model_validate(core) if core is not None else None,
            temperament=TemperamentOutput.model_validate(temperament)
            if temperament is not None
            else None,
            personality_profile=ProfileOutput.model_validate(profile)
            if profile is not None
            else None,
        )

    return router
