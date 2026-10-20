"""Core-facing HTTP boundary for the evidence ledger."""

import sys
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Coroutine, Iterator
from contextlib import (
    AbstractContextManager,
    ExitStack,
    aclosing,
    asynccontextmanager,
    contextmanager,
)
from typing import Annotated, Literal

import anyio
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.routing import APIRoute
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictInt, StrictStr
from starlette.concurrency import run_in_threadpool
from starlette.types import Receive, Scope, Send

from digital_souls_memory.evidence.domain import (
    DeliveryReceipt,
    EvidenceValidationError,
    Session,
)
from digital_souls_memory.evidence.ports import (
    EvidenceConflict,
    EvidenceNotFound,
    EvidenceUnavailable,
)
from digital_souls_memory.evidence.use_cases import EvidenceService

Nonblank = Annotated[StrictStr, Field(pattern=r"\S")]
Identifier = Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9_-]+$")]


def _evidence_error(
    error: EvidenceValidationError | EvidenceConflict | EvidenceNotFound | EvidenceUnavailable,
) -> JSONResponse:
    if isinstance(error, EvidenceValidationError):
        return JSONResponse(status_code=422, content={"detail": "Invalid evidence value"})
    if isinstance(error, EvidenceConflict):
        return JSONResponse(status_code=409, content={"detail": "Evidence already exists"})
    if isinstance(error, EvidenceNotFound):
        return JSONResponse(status_code=404, content={"detail": "Evidence not found"})
    return JSONResponse(status_code=503, content={"detail": "Evidence unavailable"})


class EvidenceRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[None, None, Response]]:
        handler = super().get_route_handler()

        async def handle(request: Request) -> Response:
            try:
                return await handler(request)
            except (
                EvidenceValidationError,
                EvidenceConflict,
                EvidenceNotFound,
                EvidenceUnavailable,
            ) as error:
                return _evidence_error(error)

        return handle


class _EvidenceListResponse[T](Response):
    def __init__(
        self, source: Callable[[], AbstractContextManager[Iterator[T]]], model: type[BaseModel]
    ) -> None:
        super().__init__(media_type="application/json")
        self._source = source
        self._model = model

    @contextmanager
    def _items(self) -> Iterator[Iterator[bytes]]:
        with self._source() as values:
            yield (
                self._model.model_validate(value).model_dump_json(by_alias=True).encode("utf-8")
                for value in values
            )

    @asynccontextmanager
    async def _managed_items(self) -> AsyncIterator[tuple[Iterator[bytes], ExitStack]]:
        stack = ExitStack()
        try:
            items = await run_in_threadpool(stack.enter_context, self._items())
            yield items, stack
        except BaseException:
            with anyio.CancelScope(shield=True):
                await run_in_threadpool(stack.__exit__, *sys.exc_info())
            raise
        finally:
            with anyio.CancelScope(shield=True):
                await run_in_threadpool(stack.close)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        started = False
        try:
            async with self._managed_items() as (items, stack):
                first = await run_in_threadpool(next, items, None)

                async def body() -> AsyncGenerator[bytes]:
                    yield b"["
                    item = first
                    while item is not None:
                        yield item
                        item = await run_in_threadpool(next, items, None)
                        if item is not None:
                            yield b","
                    # Commit/close failures must precede the successful array terminator.
                    await run_in_threadpool(stack.close)
                    yield b"]"

                async with aclosing(body()) as content:
                    started = True
                    await StreamingResponse(content, media_type="application/json")(
                        scope, receive, send
                    )
        except (
            EvidenceValidationError,
            EvidenceConflict,
            EvidenceNotFound,
            EvidenceUnavailable,
        ) as error:
            if started:
                raise
            await _evidence_error(error)(scope, receive, send)


class SessionRequest(BaseModel):
    session_id: Identifier
    started_at: AwareDatetime
    ended_at: AwareDatetime | None = None
    surface: Nonblank
    participants: list[Identifier]

    def to_domain(self) -> Session:
        return Session(
            session_id=self.session_id,
            started_at=self.started_at,
            ended_at=self.ended_at,
            surface=self.surface,
            participants=tuple(self.participants),
        )


class SessionResponse(SessionRequest):
    model_config = ConfigDict(from_attributes=True)


class TurnRequest(BaseModel):
    turn_id: Identifier
    session_id: Identifier
    surface: Nonblank
    actor: Nonblank
    kind: Literal["utterance", "observation"]
    text: StrictStr
    audience: list[Identifier] = Field(default_factory=list)
    language: Nonblank
    speech_register: dict[StrictStr, StrictStr] = Field(alias="register")
    sensitivity: Nonblank
    classifier_version: Nonblank
    occurred_at: AwareDatetime


class TurnResponse(TurnRequest):
    model_config = ConfigDict(from_attributes=True)
    recorded_at: AwareDatetime


class ReceiptRequest(BaseModel):
    status: Literal["full", "partial", "none"]
    spoken_until: Annotated[StrictInt, Field(ge=0)]
    reported_by: Nonblank
    reported_at: AwareDatetime

    def to_domain(self, turn_id: str) -> DeliveryReceipt:
        return DeliveryReceipt(
            turn_id=turn_id,
            status=self.status,
            spoken_until=self.spoken_until,
            reported_by=self.reported_by,
            reported_at=self.reported_at,
        )


class ReceiptResponse(ReceiptRequest):
    model_config = ConfigDict(from_attributes=True)
    turn_id: str


def evidence_router(service: Callable[[], EvidenceService]) -> APIRouter:
    router = APIRouter(route_class=EvidenceRoute)

    @router.post("/sessions", status_code=201)
    def add_session(request: SessionRequest) -> SessionResponse:
        value = request.to_domain()
        return SessionResponse.model_validate(service().add_session(value))

    @router.get("/sessions/{session_id}")
    def get_session(session_id: str) -> SessionResponse:
        return SessionResponse.model_validate(service().get_session(session_id))

    @router.post("/turns", status_code=201)
    def add_turn(request: TurnRequest) -> TurnResponse:
        value = service().add_turn(
            turn_id=request.turn_id,
            session_id=request.session_id,
            surface=request.surface,
            actor=request.actor,
            kind=request.kind,
            text=request.text,
            audience=tuple(request.audience),
            language=request.language,
            register=request.speech_register,
            sensitivity=request.sensitivity,
            classifier_version=request.classifier_version,
            occurred_at=request.occurred_at,
        )
        return TurnResponse.model_validate(value)

    @router.get("/turns/{turn_id}")
    def get_turn(turn_id: str) -> TurnResponse:
        return TurnResponse.model_validate(service().get_turn(turn_id))

    @router.post("/turns/{turn_id}/receipts", status_code=201)
    def add_receipt(turn_id: str, request: ReceiptRequest) -> ReceiptResponse:
        value = request.to_domain(turn_id)
        return ReceiptResponse.model_validate(service().add_receipt(value))

    @router.get("/turns/{turn_id}/receipts", response_model=list[ReceiptResponse])
    def list_receipts(turn_id: str) -> Response:
        return _EvidenceListResponse(lambda: service().list_receipts(turn_id), ReceiptResponse)

    @router.get("/characters/{character_id}/turns", response_model=list[TurnResponse])
    def list_perceived_turns(character_id: str) -> Response:
        return _EvidenceListResponse(
            lambda: service().list_perceived_turns(character_id), TurnResponse
        )

    return router
