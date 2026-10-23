"""Resolve registered profile references and enforce the configured JSON token budget."""

import json
from dataclasses import asdict
from datetime import timedelta

from digital_souls_memory.evidence.materials import EvidenceMaterials, aggregate_receipts
from digital_souls_memory.evidence.ports import CharacterRegistry, EvidenceNotFound, EvidenceStore
from digital_souls_memory.evidence.provision import CandidateRef, ProvisionUnavailable, SourceSpan
from digital_souls_memory.evidence.use_cases import Clock
from digital_souls_memory.recall.domain import CandidateMaterial, DocumentStamp, SourceStamp
from digital_souls_memory.recall.eligibility import resolve_candidate
from digital_souls_memory.recall.inputs import InputLabel, make_model_input
from digital_souls_memory.recall.ports import TokenCounter
from digital_souls_memory.resident.domain import (
    CharacterProfile,
    CharacterProfileConfig,
    ProfileEntry,
    ProfileReference,
    ProfileSnapshot,
)
from digital_souls_memory.resident.ports import ProfileStore, StoredCandidateReader


def parse_reference(value: dict[str, object]) -> ProfileReference:
    entry_id, kind = value.get("entry_id"), value.get("kind")
    if not isinstance(entry_id, str):
        raise ValueError("Profile entry ID is required")
    if kind == "turn" and set(value) == {"entry_id", "kind", "turn_id", "start", "end"}:
        turn_id, start, end = value["turn_id"], value["start"], value["end"]
        if isinstance(turn_id, str) and type(start) is int and type(end) is int:
            return ProfileReference(
                entry_id=entry_id, kind="turn", turn_id=turn_id, start=start, end=end
            )
    if kind == "memory" and set(value) == {"entry_id", "kind", "owner_id", "memory_id"}:
        owner, memory = value["owner_id"], value["memory_id"]
        if isinstance(owner, str) and isinstance(memory, str):
            return ProfileReference(
                entry_id=entry_id, kind="memory", owner_id=owner, memory_id=memory
            )
    raise ValueError("Invalid profile reference")


class ProfileService:
    def __init__(
        self,
        *,
        evidence: EvidenceStore,
        characters: CharacterRegistry,
        store: ProfileStore,
        candidates: StoredCandidateReader,
        counter: TokenCounter,
        clock: Clock,
        delivery_timeout: timedelta,
        non_sensitive_labels: frozenset[str],
    ) -> None:
        self.evidence, self.characters, self.store = evidence, characters, store
        self.candidates, self.counter, self.clock = candidates, counter, clock
        self.timeout, self.labels = delivery_timeout, non_sensitive_labels
        self.materials = EvidenceMaterials(evidence, characters, clock, delivery_timeout)

    def replace(
        self, *, character_id: str, token_limit: int, entries: tuple[dict[str, object], ...]
    ) -> CharacterProfile:
        if not self.characters.is_character(character_id):
            raise EvidenceNotFound()
        refs = tuple(parse_reference(value) for value in entries)
        previous = self.store.get(character_id)
        version = previous.version if previous is not None else 0
        config = CharacterProfileConfig(
            character_id=character_id, token_limit=token_limit, entries=refs, version=version + 1
        )
        profile, snapshot = self.resolve(config, strict=True, expected_version=version)
        self.store.replace(config, snapshot=snapshot)
        return profile

    def get(self, *, character_id: str) -> CharacterProfile:
        return self.current(character_id)[0]

    def current(self, character_id: str) -> tuple[CharacterProfile, ProfileSnapshot]:
        config = self.store.get(character_id)
        if config is None:
            config = CharacterProfileConfig(
                character_id=character_id, token_limit=0, entries=(), version=0
            )
        return self.resolve(config, strict=False, expected_version=config.version)

    def resolve(
        self, config: CharacterProfileConfig, *, strict: bool, expected_version: int
    ) -> tuple[CharacterProfile, ProfileSnapshot]:
        entries: list[ProfileEntry] = []
        sources: list[SourceStamp] = []
        documents: list[DocumentStamp] = []
        labels: list[InputLabel] = []
        now = self.clock()
        refs = tuple(
            CandidateRef(owner_id=v.owner_id, memory_id=v.memory_id, anchor_id=None)
            for v in config.entries
            if v.owner_id is not None
        )
        candidates = (
            {v.ref: v for v in self.candidates.get_candidates(refs=refs, now=now)} if refs else {}
        )
        for ref in config.entries:
            entry = None
            if ref.kind == "turn":
                assert ref.turn_id is not None and ref.start is not None and ref.end is not None
                turn = self.evidence.get_turn(ref.turn_id)
                decision = self.materials.get_decision(config.character_id, ref.turn_id)
                if (
                    turn is not None
                    and decision is not None
                    and decision.text is not None
                    and ref.end <= len(decision.text)
                ):
                    span = SourceSpan(
                        turn_id=turn.turn_id,
                        start=ref.start,
                        end=ref.end,
                        text=decision.text[ref.start : ref.end],
                        actor=turn.actor,
                        language=turn.language,
                        truncated=False,
                        original_start=ref.start,
                        original_end=ref.end,
                    )
                    entry = ProfileEntry(
                        entry_id=ref.entry_id,
                        kind=ref.kind,
                        text=span.text,
                        source_spans=(span,),
                        owner_id=None,
                        memory_id=None,
                    )
                    sources.append(
                        SourceStamp(
                            turn_id=turn.turn_id,
                            character_id=config.character_id,
                            basis=decision.basis,
                            receipt_version=decision.receipt_version,
                            available_end=len(decision.text),
                            used_end=ref.end,
                            deadline=decision.deadline,
                        )
                    )
                    labels.append(InputLabel(turn.sensitivity, turn.classifier_version))
            else:
                assert ref.owner_id is not None
                candidate = candidates.get(
                    CandidateRef(owner_id=ref.owner_id, memory_id=ref.memory_id, anchor_id=None)
                )
                if candidate is not None:
                    materials = []
                    for dependency in candidate.dependencies:
                        turn = self.evidence.get_turn(dependency.turn_id)
                        session = (
                            self.evidence.get_session(turn.session_id) if turn is not None else None
                        )
                        if turn is not None and session is not None:
                            with self.evidence.list_receipts(turn.turn_id) as receipts:
                                materials.append(
                                    CandidateMaterial(
                                        turn=turn,
                                        session=session,
                                        actor_is_character=self.characters.is_character(turn.actor),
                                        receipt_state=aggregate_receipts(receipts),
                                    )
                                )
                    resolved = resolve_candidate(
                        candidate,
                        character_id=config.character_id,
                        materials=tuple(materials),
                        now=now,
                        delivery_timeout=self.timeout,
                    )
                    if resolved is not None and candidate.memory is not None:
                        entry = ProfileEntry(
                            entry_id=ref.entry_id,
                            kind=ref.kind,
                            text=candidate.memory.content,
                            source_spans=resolved.spans,
                            owner_id=ref.owner_id,
                            memory_id=ref.memory_id,
                        )
                        sources.extend(resolved.sources)
                        documents.append(candidate.document)
                        labels.extend(candidate.labels)
            if entry is None:
                if strict:
                    raise ValueError("Profile source is unavailable")
            else:
                entries.append(entry)
        if entries:
            payload = json.dumps(
                [asdict(v) for v in entries],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            try:
                count = self.counter.count(
                    make_model_input(
                        texts=(payload,), labels=tuple(labels), non_sensitive_labels=self.labels
                    )
                )
                if type(count) is not int or count < 0:
                    raise ProvisionUnavailable()
            except Exception as error:
                raise ProvisionUnavailable() from error
            if count > config.token_limit:
                if strict:
                    raise ValueError("Profile token limit exceeded")
                raise ProvisionUnavailable()
        return CharacterProfile(
            character_id=config.character_id,
            entries=tuple(entries),
            token_limit=config.token_limit,
            version=config.version,
        ), ProfileSnapshot(
            character_id=config.character_id,
            version=expected_version,
            documents=tuple(documents),
            sources=tuple(sources),
            observed_at=now,
        )
