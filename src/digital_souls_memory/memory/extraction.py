"""Construct restricted model inputs and validate evidence-grounded candidates."""

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol

from digital_souls_memory.evidence.domain import Turn
from digital_souls_memory.memory.domain import (
    DerivedMemory,
    ExtractionProposal,
    MemoryRelation,
    MemoryValidationError,
)
from digital_souls_memory.memory.evidence import independent_evidence_count
from digital_souls_memory.memory.inputs import MemoryInputs, ResolvedInput


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
    language: str


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


def make_gateway_input(
    *,
    character_id: str,
    materials: tuple[tuple[Turn, str], ...],
    non_sensitive_labels: frozenset[str],
) -> LocalOnly | CloudInput:
    public = bool(materials) and all(t.sensitivity in non_sensitive_labels for t, _ in materials)
    cls = PublicTurn if public else LocalTurn
    turns = tuple(
        cls(
            turn_id=t.turn_id,
            text=text,
            actor=t.actor,
            kind=t.kind,
            addressees=t.addressees,
            address_mode=t.address_mode,
            reply_to_turn_id=t.reply_to_turn_id,
            occurred_at=t.occurred_at,
            language=t.language,
        )
        for t, text in materials
    )
    if public:
        return CloudInput(
            character_id=character_id, turns=tuple(t for t in turns if isinstance(t, PublicTurn))
        )
    return LocalOnly(character_id=character_id, turns=turns)


class ExtractionGateway(Protocol):
    def extract_local(self, value: LocalOnly) -> tuple[DerivedMemory, ...] | ExtractionProposal: ...
    def extract_cloud(
        self, value: CloudInput
    ) -> tuple[DerivedMemory, ...] | ExtractionProposal: ...


class MemoryExtractor:
    def __init__(
        self,
        *,
        inputs: MemoryInputs,
        gateway: ExtractionGateway,
        non_sensitive_labels: frozenset[str],
    ) -> None:
        self.inputs = inputs
        self.gateway = gateway
        self.non_sensitive_labels = non_sensitive_labels

    def propose(self, *, character_id: str, turn_ids: tuple[str, ...]) -> ExtractionProposal:
        return self.propose_resolved(character_id, self.inputs.resolve(character_id, turn_ids))

    def propose_resolved(self, character_id: str, resolved: ResolvedInput) -> ExtractionProposal:
        available = resolved.available
        turns = {v.turn.turn_id: v.turn for v in available}
        if not turns:
            return ExtractionProposal(memories=())
        materials = []
        for material in available:
            turn = material.turn
            assert material.decision is not None and material.decision.text
            text = material.decision.text
            materials.append((turn, text))
        value = make_gateway_input(
            character_id=character_id,
            materials=tuple(materials),
            non_sensitive_labels=self.non_sensitive_labels,
        )
        if isinstance(value, CloudInput):
            result = self.gateway.extract_cloud(value)
        else:
            result = self.gateway.extract_local(value)
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
                        independent_evidence_count=independent_evidence_count(sources),
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
