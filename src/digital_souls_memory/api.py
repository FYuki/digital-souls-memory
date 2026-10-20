"""HTTP liveness entry point, independent of storage and model providers."""

import os
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI

from digital_souls_memory.adapters.identity_characters import IdentityCharacters
from digital_souls_memory.adapters.postgres_evidence import PostgresEvidenceStore
from digital_souls_memory.adapters.postgres_identity import PostgresIdentityStore
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
) -> FastAPI:
    if delivery_timeout < timedelta(0):
        raise ValueError("Delivery timeout must be nonnegative")
    app = FastAPI(title="Digital Souls Memory")
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
    return app
