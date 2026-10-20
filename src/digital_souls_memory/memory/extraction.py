"""Construct restricted model inputs and validate evidence-grounded candidates."""

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol

from digital_souls_memory.evidence.domain import Turn
from digital_souls_memory.evidence.materials import EvidenceMaterials
from digital_souls_memory.evidence.ports import EvidenceStore
from digital_souls_memory.memory.domain import (
    DerivedMemory,
    ExtractionProposal,
    MemoryRelation,
    MemoryValidationError,
)


@dataclass(frozen=True, kw_only=True)
class LocalTurn:
    turn_id: str
    text: str
    actor: str
    kind: str
    addressees: tuple[str, ...]
    address_mode: str
    reply_to_turn_id: str | None
    occurred_at: datetime


@dataclass(frozen=True, kw_only=True)
class PublicTurn(LocalTurn):
    """Constructed only after configured label resolution at the input boundary."""


@dataclass(frozen=True, kw_only=True)
class LocalOnly:
    character_id: str
    turns: tuple[LocalTurn, ...]


@dataclass(frozen=True, kw_only=True)
class CloudInput:
    character_id: str
    turns: tuple[PublicTurn, ...]

    def __post_init__(self) -> None:
        if not self.turns or any(type(value) is not PublicTurn for value in self.turns):
            raise MemoryValidationError("Cloud input requires public materials")


class ExtractionGateway(Protocol):
    def extract_local(self, value: LocalOnly) -> tuple[DerivedMemory, ...] | ExtractionProposal: ...
    def extract_cloud(
        self, value: CloudInput
    ) -> tuple[DerivedMemory, ...] | ExtractionProposal: ...


class MemoryExtractor:
    def __init__(
        self,
        *,
        evidence: EvidenceStore,
        materials: EvidenceMaterials,
        gateway: ExtractionGateway,
        non_sensitive_labels: frozenset[str],
        conversation_surfaces: frozenset[str],
        comment_surfaces: frozenset[str] = frozenset(),
    ) -> None:
        if conversation_surfaces & comment_surfaces:
            raise MemoryValidationError("Surface classifications overlap")
        self.evidence = evidence
        self.materials = materials
        self.gateway = gateway
        self.non_sensitive_labels = non_sensitive_labels
        self.conversation_surfaces = conversation_surfaces
        self.comment_surfaces = comment_surfaces

    def eligible(self, character_id: str, turn: Turn) -> bool:
        if self.materials.characters.is_character(turn.actor) or turn.kind != "utterance":
            return True
        if turn.surface in self.conversation_surfaces:
            return True
        if turn.surface in self.comment_surfaces:
            with self.evidence.list_perceived_turns(character_id) as values:
                return any(
                    v.actor == character_id and v.reply_to_turn_id == turn.turn_id for v in values
                )
        return False

    def propose(self, *, character_id: str, turn_ids: tuple[str, ...]) -> ExtractionProposal:
        turns: dict[str, Turn] = {}
        for identifier in turn_ids:
            turn = self.evidence.get_turn(identifier)
            if (
                turn is not None
                and self.materials.get_text(character_id, identifier)
                and self.eligible(character_id, turn)
            ):
                turns[identifier] = turn
        if not turns:
            return ExtractionProposal(memories=())
        public = all(v.sensitivity in self.non_sensitive_labels for v in turns.values())
        inputs: list[LocalTurn] = []
        for turn in turns.values():
            text = self.materials.get_text(character_id, turn.turn_id)
            if not text:
                return ExtractionProposal(memories=())
            if turn.reply_to_turn_id is not None:
                # A reply reference supplies identity metadata, never extra text or perception.
                if self.evidence.get_turn(turn.reply_to_turn_id) is None:
                    raise MemoryValidationError("Missing reply target")
            cls = PublicTurn if public else LocalTurn
            inputs.append(
                cls(
                    turn_id=turn.turn_id,
                    text=text,
                    actor=turn.actor,
                    kind=turn.kind,
                    addressees=turn.addressees,
                    address_mode=turn.address_mode,
                    reply_to_turn_id=turn.reply_to_turn_id,
                    occurred_at=turn.occurred_at,
                )
            )
        if public:
            cloud_turns = tuple(v for v in inputs if isinstance(v, PublicTurn))
            result = self.gateway.extract_cloud(
                CloudInput(character_id=character_id, turns=cloud_turns)
            )
        else:
            result = self.gateway.extract_local(
                LocalOnly(character_id=character_id, turns=tuple(inputs))
            )
        proposal = (
            result
            if isinstance(result, ExtractionProposal)
            else ExtractionProposal(memories=result)
        )
        result = proposal.memories
        if not isinstance(result, tuple) or any(not isinstance(v, DerivedMemory) for v in result):
            raise MemoryValidationError("Invalid extraction output")
        accepted: list[DerivedMemory] = []
        identifiers: set[str] = set()
        for candidate in result:
            candidate = replace(candidate)
            if candidate.id in identifiers or candidate.character_id != character_id:
                raise MemoryValidationError("Invalid candidate owner or identifier")
            identifiers.add(candidate.id)
            if any(source not in turns for source in candidate.source_turn_ids):
                raise MemoryValidationError("Candidate contains evidence outside input")
            sources = [turns[i] for i in candidate.source_turn_ids]
            if candidate.kind in ("relationship", "commitment") and any(
                v.address_mode == "self" for v in sources
            ):
                continue
            targets = {target for v in sources for target in (v.actor, *v.addressees)}
            counterparts = targets - {character_id}
            if candidate.kind == "relationship" and candidate.counterpart not in counterparts:
                continue
            observation_fact = candidate.kind == "fact" and all(
                v.kind == "observation" for v in sources
            )
            if (
                candidate.kind in ("fact", "preference")
                and not observation_fact
                and candidate.subject not in targets
            ):
                continue
            if candidate.kind == "commitment":
                if not any(v.addressees for v in sources):
                    continue
                if candidate.counterpart is None and len(counterparts) == 1:
                    candidate = replace(candidate, counterpart=next(iter(counterparts)))
                elif candidate.counterpart not in counterparts:
                    continue
            if candidate.epistemics.confidence_features.score() < 0.5:
                continue
            accepted.append(
                replace(
                    candidate,
                    epistemics=replace(
                        candidate.epistemics,
                        independent_evidence_count=len(set(candidate.source_turn_ids)),
                    ),
                )
            )
        accepted_ids = {v.id for v in accepted}
        if not isinstance(proposal.relations, tuple) or any(
            not isinstance(r, MemoryRelation) or r.from_id not in identifiers
            for r in proposal.relations
        ):
            raise MemoryValidationError("Invalid proposed relations")
        return ExtractionProposal(
            memories=tuple(accepted),
            relations=tuple(r for r in proposal.relations if r.from_id in accepted_ids),
        )
