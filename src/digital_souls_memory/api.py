"""HTTP liveness entry point, independent of storage and model providers."""

import os
from datetime import UTC, datetime

from fastapi import FastAPI

from digital_souls_memory.adapters.postgres_evidence import PostgresEvidenceStore
from digital_souls_memory.adapters.postgres_identity import PostgresIdentityStore
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
) -> FastAPI:
    app = FastAPI(title="Digital Souls Memory")
    app.get("/health")(health)
    registration_clock = clock if clock is not None else lambda: datetime.now(UTC)

    def service() -> EvidenceService:
        resolved_store = store
        if resolved_store is None:
            url = os.environ.get("MEMORY_DATABASE_URL")
            if not url:
                raise EvidenceUnavailable()
            resolved_store = PostgresEvidenceStore(url)
        return EvidenceService(resolved_store, registration_clock)

    app.include_router(evidence_router(service))

    def identity_service() -> IdentityService:
        resolved_store = identity_store
        if resolved_store is None:
            url = os.environ.get("MEMORY_DATABASE_URL")
            if not url:
                raise IdentityUnavailable()
            resolved_store = PostgresIdentityStore(url)
        return IdentityService(resolved_store, registration_clock, llm_gateway, reviewer)

    app.include_router(identity_router(identity_service))
    return app
