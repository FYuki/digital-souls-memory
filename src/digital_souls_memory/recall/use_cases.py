"""Recall orchestration with a commit-before-success boundary."""

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from math import isfinite
from typing import cast

from digital_souls_memory.evidence.materials import aggregate_receipts, material_decision
from digital_souls_memory.evidence.ports import CharacterRegistry, EvidenceStore
from digital_souls_memory.evidence.provision import (
    CandidateScore,
    ProvidedReference,
    ProvisionRecord,
    ProvisionUnavailable,
)
from digital_souls_memory.identity.sessions import SessionIdentityStore
from digital_souls_memory.recall.context import (
    RecallResponse,
    assemble_response,
    serialize_response,
    window_candidate,
)
from digital_souls_memory.recall.domain import (
    CandidateMaterial,
    CandidateRef,
    ProvisionSnapshot,
    RecallCandidate,
    RerankDocument,
    RerankInput,
    ResolvedCandidate,
    SourceStamp,
)
from digital_souls_memory.recall.eligibility import resolve_candidate
from digital_souls_memory.recall.inputs import InputLabel, ModelInput, make_model_input
from digital_souls_memory.recall.ports import (
    Embedding,
    ProvisionStore,
    Reranker,
    Retriever,
    TokenCounter,
)
from digital_souls_memory.recall.scoring import (
    composite_score,
    recency,
    semantic_score,
    trust_score,
)
from digital_souls_memory.recall.settings import RecallSettings


class RecallNotFound(Exception):
    """Session, character or permitted origin is absent."""


class RecallService:
    def __init__(
        self,
        *,
        evidence: EvidenceStore,
        characters: CharacterRegistry,
        retriever: Retriever,
        embedding: Embedding,
        reranker: Reranker,
        counter: TokenCounter,
        provisions: ProvisionStore,
        selections: SessionIdentityStore,
        clock: Callable[[], datetime],
        monotonic: Callable[[], float],
        id_factory: Callable[[], str],
        delivery_timeout: timedelta,
        non_sensitive_labels: frozenset[str],
        settings: RecallSettings,
    ) -> None:
        self.evidence = evidence
        self.characters = characters
        self.retriever = retriever
        self.embedding = embedding
        self.reranker = reranker
        self.counter = counter
        self.provisions = provisions
        self.selections = selections
        self.clock = clock
        self.monotonic = monotonic
        self.id_factory = id_factory
        self.delivery_timeout = delivery_timeout
        self.non_sensitive_labels = non_sensitive_labels
        self.settings = settings

    def _input(self, texts: tuple[str, ...], labels: tuple[InputLabel, ...]) -> ModelInput:
        return make_model_input(
            texts=texts, labels=labels, non_sensitive_labels=self.non_sensitive_labels
        )

    def _material(self, identifier: str) -> CandidateMaterial | None:
        turn = self.evidence.get_turn(identifier)
        if turn is None:
            return None
        session = self.evidence.get_session(turn.session_id)
        if session is None:
            return None
        actor_is_character = self.characters.is_character(turn.actor)
        with self.evidence.list_receipts(identifier) as receipts:
            return CandidateMaterial(
                turn=turn,
                session=session,
                actor_is_character=actor_is_character,
                receipt_state=aggregate_receipts(receipts),
            )

    def _resolve(
        self,
        candidate: RecallCandidate,
        character_id: str,
        now: datetime,
        material_by_id: dict[str, CandidateMaterial | None],
    ) -> ResolvedCandidate | None:
        ids = dict.fromkeys(v.turn_id for v in candidate.dependencies)
        materials = tuple(v for key in ids if (v := material_by_id.get(key)) is not None)
        return resolve_candidate(
            candidate,
            character_id=character_id,
            materials=materials,
            now=now,
            delivery_timeout=self.delivery_timeout,
        )

    def recall(
        self,
        *,
        character_id: str,
        session_id: str,
        format: str,
        query: str | None,
        turn_id: str | None,
        sensitivity: str | None,
        classifier_version: str | None,
    ) -> RecallResponse:
        started = self.monotonic()
        now = self.clock()
        session = self.evidence.get_session(session_id)
        if session is None or not self.characters.is_character(character_id):
            raise RecallNotFound()
        origin: tuple[SourceStamp, ...] = ()
        if turn_id is not None:
            material = self._material(turn_id)
            if material is None:
                raise RecallNotFound()
            decision = material_decision(
                character_id=character_id,
                session=material.session,
                turn=material.turn,
                actor_is_character=material.actor_is_character,
                receipts=material.receipt_state,
                now=now,
                delivery_timeout=self.delivery_timeout,
            )
            if not decision.text:
                raise RecallNotFound()
            search_text = decision.text
            labels = (InputLabel(material.turn.sensitivity, material.turn.classifier_version),)
            origin = (
                SourceStamp(
                    turn_id=turn_id,
                    character_id=character_id,
                    basis=decision.basis,
                    receipt_version=decision.receipt_version,
                    available_end=len(search_text),
                    used_end=len(search_text),
                    deadline=decision.deadline,
                ),
            )
        else:
            assert query is not None and sensitivity is not None and classifier_version is not None
            search_text = query
            labels = (InputLabel(sensitivity, classifier_version),)
        model_query = self._input((search_text,), labels)
        selection = self.selections.select(character_id=character_id, session=session)
        vector = self.embedding.embed(model_query, purpose="query")
        with self.retriever.reading() as reader:
            retrieved = reader.search(
                character_id=character_id,
                query=model_query,
                query_vector=vector,
                now=now,
                limit=self.settings.candidate_limit,
            )
            material_by_id: dict[str, CandidateMaterial | None] = {
                v.turn.turn_id: v for v in retrieved.materials
            }
            eligible = tuple(
                v
                for c in retrieved.candidates
                if c.document.space == vector.space
                and (v := self._resolve(c, character_id, now, material_by_id)) is not None
            )
            scores: dict[tuple[CandidateRef, int], CandidateScore] = {}
            for resolved in eligible:
                c = resolved.candidate
                memory = c.memory
                terms = dict(
                    semantic=semantic_score(c.semantic),
                    lexical=c.lexical,
                    recency=recency(
                        kind=memory.kind if memory else "verbatim_anchor",
                        occurred_at=c.occurred_at,
                        last_reinforced_at=memory.last_reinforced_at if memory else None,
                        now=now,
                    ),
                    importance=c.importance,
                    self_relevance=memory.identity_links.self_relevance if memory else 1.0,
                    trust=trust_score(memory.epistemics) if memory else 1.0,
                )
                scores[c.document.key] = CandidateScore(
                    ref=c.ref,
                    **terms,
                    composite=composite_score(weights=self.settings.weights, **terms),
                    relevance=None,
                )
            ranked_scores = sorted(
                scores, key=lambda k: (-scores[k].composite, k[0].sort_key, k[1])
            )[:20]
            by_key = {v.candidate.document.key: v for v in eligible}
            relevance: dict[tuple[CandidateRef, int], float] = {}
            if ranked_scores:
                output = self.reranker.rerank(
                    RerankInput(
                        query=model_query,
                        documents=tuple(
                            RerankDocument(
                                ref=key[0],
                                chunk_index=key[1],
                                input=self._input(
                                    (by_key[key].candidate.search_text,),
                                    by_key[key].candidate.labels,
                                ),
                            )
                            for key in ranked_scores
                        ),
                    )
                )
                if (
                    len(output) != len(ranked_scores)
                    or len({(s.ref, s.chunk_index) for s in output}) != len(output)
                    or {(s.ref, s.chunk_index) for s in output} != set(ranked_scores)
                    or any(not isfinite(s.relevance) for s in output)
                ):
                    raise ProvisionUnavailable()
                relevance = {(s.ref, s.chunk_index): s.relevance for s in output}
            scores = {k: replace(s, relevance=relevance.get(k)) for k, s in scores.items()}
            representatives: dict[CandidateRef, ResolvedCandidate] = {}
            recorded_scores: dict[CandidateRef, CandidateScore] = {}
            for key in sorted(
                scores,
                key=lambda k: (
                    scores[k].relevance is None,
                    -relevance[k] if k in relevance else -scores[k].composite,
                    k[0].sort_key,
                    k[1],
                ),
            ):
                representatives.setdefault(key[0], by_key[key])
                recorded_scores.setdefault(key[0], scores[key])
            ranked = tuple(
                value
                for value in representatives.values()
                if value.candidate.document.key in relevance
                and relevance[value.candidate.document.key] >= self.settings.relevance_threshold
            )
            identifier = self.id_factory()
            accepted: tuple[ResolvedCandidate, ...] = ()
            all_labels = labels + tuple(label for v in eligible for label in v.candidate.labels)

            def response(values: tuple[ResolvedCandidate, ...]) -> RecallResponse:
                return assemble_response(
                    provision_id=identifier, selection=selection, candidates=values, format=format
                )

            def fits(values: tuple[ResolvedCandidate, ...]) -> bool:
                count = self.counter.count(
                    self._input((serialize_response(response(values)),), all_labels)
                )
                if type(count) is not int or count < 0:
                    raise ProvisionUnavailable()
                return count <= 1500

            if not fits(()):
                raise ProvisionUnavailable()
            for value in ranked:
                if any(v.candidate.ref == value.candidate.ref for v in accepted):
                    continue
                if sum(v.candidate.memory is not None for v in accepted) >= 5:
                    break
                group: tuple[ResolvedCandidate, ...] = (value,)
                if value.candidate.memory is not None:
                    anchors = reader.anchors_for(
                        character_id=character_id,
                        turn_ids=value.candidate.memory.source_turn_ids,
                        now=now,
                    )
                    material_by_id.update((v.turn.turn_id, v) for v in anchors.materials)
                    related: dict[CandidateRef, ResolvedCandidate] = {}
                    used = {v.candidate.ref for v in (*accepted, *group)}
                    for c in sorted(anchors.candidates, key=lambda c: c.document.chunk_index):
                        if c.ref in used or c.document.space != vector.space:
                            continue
                        related_value = representatives.get(c.ref)
                        if related_value is None:
                            related_value = self._resolve(c, character_id, now, material_by_id)
                        if related_value is not None:
                            related.setdefault(c.ref, related_value)
                    group += tuple(related.values())
                trial = accepted + group
                if fits(trial):
                    accepted = trial
                    continue
                width = max((len(s.text) for v in group for s in v.spans), default=0) // 2
                while width > 0:
                    smaller = tuple(
                        window_candidate(v, query=search_text, width=width) for v in group
                    )
                    if fits(accepted + smaller):
                        accepted += smaller
                        break
                    width //= 2
            result = response(accepted)
            # Count precisely the bytes of JSON that the HTTP adapter will return.
            if not fits(accepted):
                raise ProvisionUnavailable()
            documents = tuple(dict.fromkeys(v.candidate.document for v in (*eligible, *accepted)))
            sources = tuple(
                dict.fromkeys((*origin, *(s for v in (*eligible, *accepted) for s in v.sources)))
            )
            snapshot = ProvisionSnapshot(
                character_id=character_id,
                session_id=session_id,
                temperament_version=selection.temperament_version,
                personality_version=selection.personality_version,
                documents=documents,
                sources=sources,
            )
        self.provisions.save(
            ProvisionRecord(
                provision_id=identifier,
                kind="turn",
                character_id=character_id,
                session_id=session_id,
                surface=session.surface,
                query=query,
                turn_id=turn_id,
                memory_ids=tuple(v.id for v in result.memories),
                source_spans=result.source_spans,
                verbatim_anchor_ids=tuple(v.anchor_id for v in result.verbatim_anchors),
                voice_exemplar_ids=(),
                motivation_ids=(),
                temperament_version=selection.temperament_version,
                personality_version=selection.personality_version,
                scores=tuple(
                    recorded_scores[ref] for ref in dict.fromkeys(v.candidate.ref for v in eligible)
                ),
                duration_ms=(self.monotonic() - started) * 1000,
                provided_at=now,
                provided_refs=tuple(
                    ProvidedReference(
                        kind="memory" if v.candidate.ref.memory_id is not None else "anchor",
                        owner_id=v.candidate.ref.owner_id,
                        identifier=cast(
                            str, v.candidate.ref.memory_id or v.candidate.ref.anchor_id
                        ),
                    )
                    for v in accepted
                ),
            ),
            snapshot=snapshot,
        )
        return result
