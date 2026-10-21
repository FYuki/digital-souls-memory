"""HTTP liveness entry point, independent of storage and model providers."""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI

from digital_souls_memory.adapters.identity_characters import IdentityCharacters
from digital_souls_memory.adapters.postgres_evidence import PostgresEvidenceStore
from digital_souls_memory.adapters.postgres_identity import PostgresIdentityStore
from digital_souls_memory.adapters.postgres_memory import PostgresMemoryStore
from digital_souls_memory.adapters.postgres_voice import PostgresVoiceStore
from digital_souls_memory.evidence.materials import EvidenceMaterials
from digital_souls_memory.evidence.ports import EvidenceStore, EvidenceUnavailable
from digital_souls_memory.evidence.use_cases import Clock, EvidenceService
from digital_souls_memory.evidence_http import evidence_router
from digital_souls_memory.identity.ports import (
    IdentityStore,
    IdentityUnavailable,
    LLMGateway,
    Reviewer,
)
from digital_souls_memory.identity.use_cases import IdentityService
from digital_souls_memory.identity_http import identity_router
from digital_souls_memory.memory.extraction import ExtractionGateway, MemoryExtractor
from digital_souls_memory.memory.inputs import MemoryInputs
from digital_souls_memory.memory.ports import MemoryStore, MemoryUnavailable
from digital_souls_memory.memory.use_cases import MemoryService
from digital_souls_memory.memory.worker import MemoryWorker
from digital_souls_memory.memory_http import memory_router
from digital_souls_memory.voice.ports import VoiceStore, VoiceUnavailable
from digital_souls_memory.voice.use_cases import VoiceService
from digital_souls_memory.voice_http import voice_router


def health() -> dict[str, str]:
    """Report process liveness without probing or disclosing dependencies."""
    return {"status": "ok"}


def create_app(
    *,
    store: EvidenceStore | None = None,
    clock: Clock | None = None,
    identity_store: IdentityStore | None = None,
    llm_gateway: LLMGateway | None = None,
    reviewer: Reviewer | None = None,
    voice_store: VoiceStore | None = None,
    delivery_timeout: timedelta = timedelta(minutes=10),
    memory_store: MemoryStore | None = None,
    extraction_gateway: ExtractionGateway | None = None,
    non_sensitive_labels: frozenset[str] = frozenset(),
    conversation_surfaces: frozenset[str] = frozenset(),
    comment_surfaces: frozenset[str] = frozenset(),
) -> FastAPI:
    if delivery_timeout < timedelta(0):
        raise ValueError("Delivery timeout must be nonnegative")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        worker = None
        if extraction_gateway is not None:
            resolved = memory_service()
            worker = MemoryWorker(
                resolved,
                MemoryExtractor(
                    inputs=resolved.inputs,
                    gateway=extraction_gateway,
                    non_sensitive_labels=non_sensitive_labels,
                ),
            )
            worker.start()
        try:
            yield
        finally:
            if worker is not None:
                worker.close()

    app = FastAPI(title="Digital Souls Memory", lifespan=lifespan)
    app.get("/health")(health)
    registration_clock = clock if clock is not None else lambda: datetime.now(UTC)

    def resolve_evidence_store() -> EvidenceStore:
        resolved_store = store
        if resolved_store is None:
            url = os.environ.get("MEMORY_DATABASE_URL")
            if not url:
                raise EvidenceUnavailable()
            resolved_store = PostgresEvidenceStore(url)
        return resolved_store

    def service() -> EvidenceService:
        return EvidenceService(
            resolve_evidence_store(), registration_clock, IdentityCharacters(resolve_identity_store)
        )

    app.include_router(evidence_router(service))

    def resolve_identity_store() -> IdentityStore:
        resolved_store = identity_store
        if resolved_store is None:
            url = os.environ.get("MEMORY_DATABASE_URL")
            if not url:
                raise IdentityUnavailable()
            resolved_store = PostgresIdentityStore(url)
        return resolved_store

    def identity_service() -> IdentityService:
        return IdentityService(resolve_identity_store(), registration_clock, llm_gateway, reviewer)

    app.include_router(identity_router(identity_service))

    def voice_service() -> VoiceService:
        resolved_voice = voice_store
        if resolved_voice is None:
            url = os.environ.get("MEMORY_DATABASE_URL")
            if not url:
                raise VoiceUnavailable()
            resolved_voice = PostgresVoiceStore(url)
        return VoiceService(
            resolved_voice,
            resolve_identity_store(),
            EvidenceMaterials(
                resolve_evidence_store(),
                IdentityCharacters(resolve_identity_store),
                registration_clock,
                delivery_timeout,
            ),
            llm_gateway,
            reviewer,
        )

    app.include_router(voice_router(voice_service, registration_clock))

    configured_memory: MemoryService | None = None

    def memory_service() -> MemoryService:
        nonlocal configured_memory
        if configured_memory is not None:
            return configured_memory
        resolved_memory = memory_store
        if resolved_memory is None:
            url = os.environ.get("MEMORY_DATABASE_URL")
            if not url:
                raise MemoryUnavailable()
            resolved_memory = PostgresMemoryStore(url)
        evidence = resolve_evidence_store()
        configured_memory = MemoryService(
            resolved_memory,
            MemoryInputs(
                evidence,
                EvidenceMaterials(
                    evidence,
                    IdentityCharacters(resolve_identity_store),
                    registration_clock,
                    delivery_timeout,
                ),
                conversation_surfaces=conversation_surfaces,
                comment_surfaces=comment_surfaces,
            ),
            registration_clock,
        )
        return configured_memory

    app.include_router(memory_router(memory_service))
    return app
