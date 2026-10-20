"""HTTP liveness entry point, independent of storage and model providers."""

import os
from datetime import UTC, datetime

from fastapi import FastAPI

from digital_souls_memory.adapters.postgres_evidence import PostgresEvidenceStore
from digital_souls_memory.evidence.ports import EvidenceStore, EvidenceUnavailable
from digital_souls_memory.evidence.use_cases import Clock, EvidenceService
from digital_souls_memory.evidence_http import evidence_router


def health() -> dict[str, str]:
    """Report process liveness without probing or disclosing dependencies."""
    return {"status": "ok"}


def create_app(*, store: EvidenceStore | None = None, clock: Clock | None = None) -> FastAPI:
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
    return app
