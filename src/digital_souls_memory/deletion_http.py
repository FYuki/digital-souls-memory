"""Core-facing author deletion routes and their exception boundary."""

from collections.abc import Callable, Coroutine
from dataclasses import asdict

from fastapi import APIRouter, Header, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict

from digital_souls_memory.governance.domain import DeletionCommand
from digital_souls_memory.governance.ports import Authorizer, AuthorUnauthorized, AuthorUnavailable
from digital_souls_memory.governance.use_cases import DeletionService
from digital_souls_memory.memory.domain import MemoryValidationError
from digital_souls_memory.memory.ports import MemoryConflict, MemoryUnavailable


class DeletionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str
    target_turn_ids: tuple[str, ...] = ()
    user_id: str | None = None

    def command(self) -> DeletionCommand:
        return DeletionCommand(**self.model_dump())


class DeletionRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[None, None, Response]]:
        handler = super().get_route_handler()

        async def handle(request: Request) -> Response:
            try:
                return await handler(request)
            except AuthorUnauthorized:
                return JSONResponse(status_code=401, content={"detail": "Author required"})
            except (AuthorUnavailable, MemoryUnavailable):
                return JSONResponse(status_code=503, content={"detail": "Deletion unavailable"})
            except MemoryValidationError:
                return JSONResponse(status_code=422, content={"detail": "Invalid deletion"})
            except MemoryConflict:
                return JSONResponse(status_code=409, content={"detail": "Deletion conflict"})

        return handle


def deletion_router(
    authorizer: Callable[[], Authorizer], service: Callable[[], DeletionService]
) -> APIRouter:
    router = APIRouter(route_class=DeletionRoute)

    @router.post("/deletion-requests")
    def execute(body: DeletionBody, authorization: str | None = Header(default=None)) -> Response:
        principal = authorizer().authorize(authorization)
        command = body.command()
        result = service().execute(command, principal=principal)
        return JSONResponse(content=jsonable_encoder(asdict(result)))

    @router.get("/deletion-requests/{request_id}")
    def get(request_id: str, authorization: str | None = Header(default=None)) -> Response:
        principal = authorizer().authorize(authorization)
        result = service().get(request_id, principal=principal)
        if result is None:
            return JSONResponse(status_code=404, content={"detail": "Deletion not found"})
        return JSONResponse(content=jsonable_encoder(asdict(result)))

    return router
