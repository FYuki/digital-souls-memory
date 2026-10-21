"""Compose resident context and commit the matching provision before returning."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from digital_souls_memory.evidence.materials import EvidenceMaterials
from digital_souls_memory.evidence.ports import CharacterRegistry, EvidenceNotFound, EvidenceStore
from digital_souls_memory.evidence.provision import (
    ProvisionRecord,
    ProvisionUnavailable,
    ResidentCandidateRef,
    ResidentCandidateScore,
)
from digital_souls_memory.evidence.use_cases import Clock
from digital_souls_memory.identity.domain import CoreIdentity, Temperament
from digital_souls_memory.identity.ports import IdentityStore
from digital_souls_memory.identity.sessions import SessionIdentityStore
from digital_souls_memory.recall.domain import ProvisionSnapshot, SourceStamp
from digital_souls_memory.recall.lexical import lexical_score, lexical_terms, normalized_key
from digital_souls_memory.recall.ports import ProvisionStore, TokenCounter
from digital_souls_memory.resident.domain import CharacterProfile
from digital_souls_memory.resident.ports import ProfileStore, StoredCandidateReader
from digital_souls_memory.resident.profiles import ProfileService
from digital_souls_memory.voice.domain import VoiceExemplar
from digital_souls_memory.voice.ports import VoiceStore
from digital_souls_memory.voice.use_cases import VoiceService


@dataclass(frozen=True, kw_only=True)
class PersonalityDescription:
    description: str
    version: int
    renderer_version: str


@dataclass(frozen=True, kw_only=True)
class ResidentContext:
    provision_id: str
    core_identity: CoreIdentity | None
    temperament: Temperament | None
    personality_profile: PersonalityDescription | None
    character_profile: CharacterProfile
    motivations: tuple[object, ...]
    voice_exemplars: tuple[VoiceExemplar, ...]
    temperament_version: int | None
    personality_version: int | None


class ResidentService:
    def __init__(
        self,
        *,
        evidence: EvidenceStore,
        identity: IdentityStore,
        characters: CharacterRegistry,
        profiles: ProfileStore,
        candidates: StoredCandidateReader,
        voice: VoiceStore,
        counter: TokenCounter,
        provisions: ProvisionStore,
        selections: SessionIdentityStore,
        clock: Clock,
        monotonic: Callable[[], float],
        id_factory: Callable[[], str],
        delivery_timeout: timedelta,
        non_sensitive_labels: frozenset[str],
    ) -> None:
        self.evidence, self.identity, self.characters = evidence, identity, characters
        self.profiles = ProfileService(
            evidence=evidence,
            characters=characters,
            store=profiles,
            candidates=candidates,
            counter=counter,
            clock=clock,
            delivery_timeout=delivery_timeout,
            non_sensitive_labels=non_sensitive_labels,
        )
        self.materials = EvidenceMaterials(evidence, characters, clock, delivery_timeout)
        self.voice = VoiceService(voice, identity, self.materials, None, None)
        self.provisions, self.selections, self.clock = provisions, selections, clock
        self.monotonic, self.id_factory = monotonic, id_factory

    def provide(self, *, character_id: str, session_id: str, query: str | None) -> ResidentContext:
        start = self.monotonic()
        session = self.evidence.get_session(session_id)
        if session is None or not self.characters.is_character(character_id):
            raise EvidenceNotFound()
        selection = self.selections.select(character_id=character_id, session=session)
        core = self.identity.get_core(character_id)
        temperament = (
            self.identity.get_temperament(character_id, selection.temperament_version)
            if selection.temperament_version is not None
            else None
        )
        personality = (
            self.identity.get_profile(character_id, selection.personality_version)
            if selection.personality_version is not None
            else None
        )
        profile, profile_snapshot = self.profiles.current(character_id)
        exemplars = tuple(
            v
            for v in self.voice.list_exemplars(character_id)
            if v.trait_version == selection.personality_version
        )
        terms = lexical_terms(query) if query is not None else ()
        exemplars = tuple(
            sorted(
                exemplars,
                key=lambda v: (-lexical_score(normalized_key(v.text), terms), v.exemplar_id),
            )
        )
        sources = list(profile_snapshot.sources)
        for value in exemplars:
            if value.turn_id is None:
                continue
            decision = self.materials.get_decision(character_id, value.turn_id)
            if decision is None or decision.text is None or value.text not in decision.text:
                raise ProvisionUnavailable()
            end = decision.text.index(value.text) + len(value.text)
            sources.append(
                SourceStamp(
                    turn_id=value.turn_id,
                    character_id=character_id,
                    basis=decision.basis,
                    receipt_version=decision.receipt_version,
                    available_end=len(decision.text),
                    used_end=end,
                    deadline=decision.deadline,
                )
            )
        snapshot = ProvisionSnapshot(
            character_id=character_id,
            session_id=session_id,
            temperament_version=selection.temperament_version,
            personality_version=selection.personality_version,
            sources=tuple(sources),
            documents=profile_snapshot.documents,
            profile_version=profile.version,
            core_identity_version=core.version if core is not None else None,
            voice_exemplars=exemplars,
        )
        identifier = self.id_factory()
        record = ProvisionRecord(
            provision_id=identifier,
            kind="resident",
            character_id=character_id,
            session_id=session_id,
            surface=session.surface,
            query=query,
            turn_id=None,
            memory_ids=tuple(v.memory_id for v in profile.entries if v.memory_id is not None),
            source_spans=tuple(s for v in profile.entries for s in v.source_spans),
            verbatim_anchor_ids=(),
            voice_exemplar_ids=tuple(v.exemplar_id for v in exemplars),
            motivation_ids=(),
            temperament_version=selection.temperament_version,
            personality_version=selection.personality_version,
            profile_entry_ids=tuple(v.entry_id for v in profile.entries),
            core_identity_version=snapshot.core_identity_version,
            scores=tuple(
                ResidentCandidateScore(
                    ref=ResidentCandidateRef(kind="profile", identifier=v.entry_id), lexical=None
                )
                for v in profile.entries
            )
            + tuple(
                ResidentCandidateScore(
                    ref=ResidentCandidateRef(kind="voice", identifier=v.exemplar_id),
                    lexical=lexical_score(normalized_key(v.text), terms)
                    if query is not None
                    else None,
                )
                for v in exemplars
            ),
            duration_ms=(self.monotonic() - start) * 1000,
            provided_at=self.clock(),
        )
        self.provisions.save(record, snapshot=snapshot)
        return ResidentContext(
            provision_id=identifier,
            core_identity=core,
            temperament=temperament,
            personality_profile=PersonalityDescription(
                description=personality.description,
                version=personality.version,
                renderer_version=personality.renderer_version,
            )
            if personality is not None
            else None,
            character_profile=profile,
            motivations=(),
            voice_exemplars=exemplars,
            temperament_version=selection.temperament_version,
            personality_version=selection.personality_version,
        )
