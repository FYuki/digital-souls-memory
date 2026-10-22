"""Explicit ST-only application factory; never used by the product entry point."""

import os
from datetime import datetime, timedelta
from threading import RLock

from fastapi import FastAPI
from pydantic import BaseModel, Field

from digital_souls_memory.api import create_app
from digital_souls_memory.recall.settings import EMBEDDING_MODEL, RERANKER_MODEL
from tests.context_gateway_support import EpisodeGateway
from tests.evidence_support import RECORDED
from tests.it1.voice_fakes import VoiceGateway, VoiceReviewer
from tests.memory_fakes import EchoGateway
from tests.recall_support import RecordingEmbedding, RecordingReranker, RecordingTokenCounter
from tests.st.fakes import ScenarioGateway


class TestClock:
    def __init__(self) -> None:
        self.now = RECORDED
        self.lock = RLock()

    def __call__(self) -> datetime:
        with self.lock:
            return self.now

    def advance(self, seconds: int) -> datetime:
        with self.lock:
            self.now += timedelta(seconds=seconds)
            return self.now


class AdvanceRequest(BaseModel):
    seconds: int = Field(ge=0, le=3600, strict=True)


def factory() -> FastAPI:
    mode = os.environ["MEMORY_CORE_ST_MODE"]
    if mode not in ("functional", "performance"):
        raise ValueError("Explicit ST mode required")
    gateway = ScenarioGateway()
    if mode == "performance":
        # Real approved embedding/reranking/tokenizer are constructed by create_app.
        app = create_app(
            llm_gateway=VoiceGateway(),
            reviewer=VoiceReviewer(),
            extraction_gateway=EchoGateway(),
            recalculation_gateway=gateway,
            conversation_surfaces=frozenset({"synthetic-chat"}),
        )
    else:
        app = _functional_app(gateway)

    @app.get("/_st/config")
    def configuration() -> dict[str, object]:
        return {
            "mode": mode,
            "embedding": EMBEDDING_MODEL,
            "reranker": RERANKER_MODEL,
            "real_models": mode == "performance",
        }

    return app


def _functional_app(gateway: ScenarioGateway) -> FastAPI:
    clock = TestClock()
    app = create_app(
        clock=clock,
        llm_gateway=VoiceGateway(),
        reviewer=VoiceReviewer(),
        extraction_gateway=gateway,
        recalculation_gateway=gateway,
        context_gateway=EpisodeGateway(),
        embedding=RecordingEmbedding(),
        reranker=RecordingReranker(),
        token_counter=RecordingTokenCounter(),
        conversation_surfaces=frozenset({"synthetic-chat"}),
    )

    @app.post("/_st/advance")
    def advance(body: AdvanceRequest) -> dict[str, datetime]:
        return {"now": clock.advance(body.seconds)}

    return app
