"""HTTP liveness entry point, independent of storage and model providers."""

import os
import time
from collections.abc import AsyncIterator, Callable
from contextlib import ExitStack, asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import FastAPI

from digital_souls_memory.adapters.core_authorization import CoreAuthorizer
from digital_souls_memory.adapters.identity_characters import IdentityCharacters
from digital_souls_memory.adapters.postgres_evidence import PostgresEvidenceStore
from digital_souls_memory.adapters.postgres_identity import PostgresIdentityStore
from digital_souls_memory.adapters.postgres_memory import PostgresMemoryStore
from digital_souls_memory.adapters.postgres_provision import (
    PostgresProvisionStore,
    PostgresSessionIdentityStore,
)
from digital_souls_memory.adapters.postgres_retrieval import PostgresRetrievalStore
from digital_souls_memory.adapters.postgres_voice import PostgresVoiceStore
from digital_souls_memory.adapters.recall_models import (
    LlamaCppEmbedding,
    LlamaCppReranker,
    LlamaCppTokenCounter,
)
from digital_souls_memory.deletion_http import deletion_router
from digital_souls_memory.episodes_http import episodes_router
from digital_souls_memory.evidence.materials import EvidenceMaterials
from digital_souls_memory.evidence.ports import EvidenceStore, EvidenceUnavailable
from digital_souls_memory.evidence.provision import ProvisionUnavailable
from digital_souls_memory.evidence.use_cases import Clock, EvidenceService
from digital_souls_memory.evidence_http import evidence_router
from digital_souls_memory.governance.ports import (
    Authorizer,
    AuthorUnavailable,
    DeletionStore,
    RecalculationGateway,
)
from digital_souls_memory.governance.recalculation import MemoryRecalculator
from digital_souls_memory.governance.use_cases import DeletionService
from digital_souls_memory.identity.ports import (
    IdentityStore,
    IdentityUnavailable,
    LLMGateway,
    Reviewer,
)
from digital_souls_memory.identity.sessions import SessionIdentityStore
from digital_souls_memory.identity.use_cases import IdentityService
from digital_souls_memory.identity_http import identity_router
from digital_souls_memory.memory.extraction import ExtractionGateway, MemoryExtractor
from digital_souls_memory.memory.inputs import MemoryInputs
from digital_souls_memory.memory.ports import MemoryStore, MemoryUnavailable
from digital_souls_memory.memory.use_cases import MemoryService
from digital_souls_memory.memory.worker import MemoryWorker
from digital_souls_memory.memory_http import memory_router
from digital_souls_memory.recall.ports import (
    Embedding,
    ProvisionStore,
    Reranker,
    Retriever,
    TokenCounter,
)
from digital_souls_memory.recall.settings import (
    EMBEDDING_MODEL,
    RERANKER_MODEL,
    RecallSettings,
    approved_space,
)
from digital_souls_memory.recall.use_cases import RecallService
from digital_souls_memory.recall.worker import IndexWorker
from digital_souls_memory.recall_http import recall_router
from digital_souls_memory.resident.ports import (
    ContextGateway,
    ContextStore,
    ProfileStore,
    StoredCandidateReader,
)
from digital_souls_memory.resident.profiles import ProfileService
from digital_souls_memory.resident.states import ContextReader
from digital_souls_memory.resident.use_cases import ResidentService
from digital_souls_memory.resident.worker import ContextWorker
from digital_souls_memory.resident_http import resident_router
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
    retriever: Retriever | None = None,
    embedding: Embedding | None = None,
    reranker: Reranker | None = None,
    token_counter: TokenCounter | None = None,
    provision_store: ProvisionStore | None = None,
    session_identity_store: SessionIdentityStore | None = None,
    provision_id_factory: Callable[[], str] = lambda: str(uuid4()),
    monotonic: Callable[[], float] = time.monotonic,
    recall_settings: RecallSettings | None = None,
    profile_store: ProfileStore | None = None,
    resident_candidates: StoredCandidateReader | None = None,
    context_store: ContextStore | None = None,
    context_gateway: ContextGateway | None = None,
    state_ttl: timedelta = timedelta(minutes=30),
    episode_id_factory: Callable[[], str] = lambda: str(uuid4()),
    authorizer: Authorizer | None = None,
    deletion_store: DeletionStore | None = None,
    recalculation_gateway: RecalculationGateway | None = None,
) -> FastAPI:
    if state_ttl <= timedelta(0):
        raise ValueError("State TTL must be positive")
    if delivery_timeout < timedelta(0):
        raise ValueError("Delivery timeout must be nonnegative")
    owned_models: list[LlamaCppEmbedding | LlamaCppReranker | LlamaCppTokenCounter] = []
    embedding_endpoint = os.environ.get("MEMORY_EMBEDDING_ENDPOINT")
    reranker_endpoint = os.environ.get("MEMORY_RERANKER_ENDPOINT")
    with ExitStack() as setup:
        if embedding is None and embedding_endpoint:
            model_embedding = setup.enter_context(
                LlamaCppEmbedding(EMBEDDING_MODEL, endpoint=embedding_endpoint)
            )
            embedding = model_embedding
            owned_models.append(model_embedding)
        if token_counter is None and embedding_endpoint:
            model_counter = setup.enter_context(
                LlamaCppTokenCounter(EMBEDDING_MODEL, endpoint=embedding_endpoint)
            )
            token_counter = model_counter
            owned_models.append(model_counter)
        if reranker is None and reranker_endpoint:
            model_reranker = setup.enter_context(
                LlamaCppReranker(RERANKER_MODEL, endpoint=reranker_endpoint)
            )
            reranker = model_reranker
            owned_models.append(model_reranker)
        setup.pop_all()
    index_store: PostgresRetrievalStore | None = None
    database_url = os.environ.get("MEMORY_DATABASE_URL")
    if database_url:
        if retriever is None:
            index_store = PostgresRetrievalStore(database_url, delivery_timeout=delivery_timeout)
            retriever = index_store
        if provision_store is None:
            provision_store = PostgresProvisionStore(
                database_url, delivery_timeout=delivery_timeout
            )
        if session_identity_store is None:
            session_identity_store = PostgresSessionIdentityStore(database_url)
        from digital_souls_memory.adapters.postgres_resident import (
            PostgresContextStore,
            PostgresProfileStore,
        )

        if profile_store is None:
            profile_store = PostgresProfileStore(database_url, delivery_timeout=delivery_timeout)
        if resident_candidates is None:
            resident_candidates = PostgresRetrievalStore(
                database_url, delivery_timeout=delivery_timeout
            )
        if context_store is None:
            context_store = PostgresContextStore(database_url, delivery_timeout=delivery_timeout)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        with ExitStack() as cleanup:
            for model in owned_models:
                cleanup.callback(model.close)
            if context_gateway is not None:
                if context_store is None:
                    raise EvidenceUnavailable()
                context_worker = ContextWorker(
                    store=context_store,
                    gateway=context_gateway,
                    clock=registration_clock,
                    non_sensitive_labels=non_sensitive_labels,
                    state_ttl=state_ttl,
                    episode_id_factory=episode_id_factory,
                )
                context_worker.start()
                cleanup.callback(context_worker.close)
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
                cleanup.callback(worker.close)
            if index_store is not None and embedding is not None:
                index_worker = IndexWorker(
                    index_store, embedding, space=approved_space(), clock=registration_clock
                )
                index_worker.start()
                cleanup.callback(index_worker.close)
            yield

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

    def recall_service() -> RecallService:
        if (
            retriever is None
            or embedding is None
            or reranker is None
            or token_counter is None
            or provision_store is None
            or session_identity_store is None
        ):
            raise ProvisionUnavailable()
        return RecallService(
            evidence=resolve_evidence_store(),
            characters=IdentityCharacters(resolve_identity_store),
            retriever=retriever,
            embedding=embedding,
            reranker=reranker,
            counter=token_counter,
            provisions=provision_store,
            selections=session_identity_store,
            clock=registration_clock,
            monotonic=monotonic,
            id_factory=provision_id_factory,
            delivery_timeout=delivery_timeout,
            non_sensitive_labels=non_sensitive_labels,
            settings=recall_settings if recall_settings is not None else RecallSettings(),
        )

    app.include_router(recall_router(recall_service))

    def profile_service() -> ProfileService:
        if profile_store is None or resident_candidates is None or token_counter is None:
            raise ProvisionUnavailable()
        return ProfileService(
            evidence=resolve_evidence_store(),
            characters=IdentityCharacters(resolve_identity_store),
            store=profile_store,
            candidates=resident_candidates,
            counter=token_counter,
            clock=registration_clock,
            delivery_timeout=delivery_timeout,
            non_sensitive_labels=non_sensitive_labels,
        )

    def resident_service() -> ResidentService:
        profile = profile_service()
        if provision_store is None or session_identity_store is None:
            raise ProvisionUnavailable()
        resolved_voice = voice_store
        if resolved_voice is None:
            if database_url is None:
                raise VoiceUnavailable()
            resolved_voice = PostgresVoiceStore(database_url)
        return ResidentService(
            evidence=resolve_evidence_store(),
            identity=resolve_identity_store(),
            characters=IdentityCharacters(resolve_identity_store),
            profiles=profile.store,
            candidates=profile.candidates,
            voice=resolved_voice,
            counter=profile.counter,
            provisions=provision_store,
            selections=session_identity_store,
            clock=registration_clock,
            monotonic=monotonic,
            id_factory=provision_id_factory,
            delivery_timeout=delivery_timeout,
            non_sensitive_labels=non_sensitive_labels,
        )

    def context_reader() -> ContextReader:
        if context_store is None:
            raise EvidenceUnavailable()
        evidence = resolve_evidence_store()
        return ContextReader(
            context_store,
            evidence,
            EvidenceMaterials(
                evidence,
                IdentityCharacters(resolve_identity_store),
                registration_clock,
                delivery_timeout,
            ),
            registration_clock,
        )

    app.include_router(resident_router(resident_service, profile_service, context_reader))
    app.include_router(episodes_router(context_reader))

    def resolve_authorizer() -> Authorizer:
        if authorizer is not None:
            return authorizer
        author_id = os.environ.get("MEMORY_AUTHOR_ID")
        token = os.environ.get("MEMORY_AUTHOR_TOKEN")
        if author_id is None or token is None:
            raise AuthorUnavailable()
        return CoreAuthorizer(author_id=author_id, token=token)

    def deletion_service() -> DeletionService:
        resolved_deletion = deletion_store
        if resolved_deletion is None:
            url = os.environ.get("MEMORY_DELETION_DATABASE_URL")
            if not url:
                raise MemoryUnavailable()
            from digital_souls_memory.adapters.postgres_deletion import PostgresDeletionStore

            resolved_deletion = PostgresDeletionStore(url)
        return DeletionService(
            resolved_deletion,
            MemoryRecalculator(
                recalculation_gateway,
                delivery_timeout=delivery_timeout,
                non_sensitive_labels=non_sensitive_labels,
            ),
            registration_clock,
        )

    app.include_router(deletion_router(resolve_authorizer, deletion_service))
    return app
