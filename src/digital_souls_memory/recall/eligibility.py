"""Perception, publication and delivery checks against original evidence."""

from datetime import datetime, timedelta

from digital_souls_memory.evidence.materials import material_decision
from digital_souls_memory.evidence.provision import SourceSpan
from digital_souls_memory.memory.domain import DerivedMemory, memory_valid_at
from digital_souls_memory.recall.domain import (
    CandidateMaterial,
    ProvisionSnapshot,
    RecallCandidate,
    ResolvedCandidate,
    SourceStamp,
)


def memory_allowed(value: DerivedMemory, *, character_id: str, now: datetime) -> bool:
    return memory_valid_at(value, now) and (
        value.character_id == character_id
        or (value.kind in ("fact", "preference") and character_id in value.shared_with)
    )


def resolve_candidate(
    candidate: RecallCandidate,
    *,
    character_id: str,
    materials: tuple[CandidateMaterial, ...],
    now: datetime,
    delivery_timeout: timedelta,
) -> ResolvedCandidate | None:
    if candidate.memory is not None and not memory_allowed(
        candidate.memory, character_id=character_id, now=now
    ):
        return None
    if candidate.anchor is not None and candidate.ref.owner_id != character_id:
        return None
    by_id = {v.turn.turn_id: v for v in materials}
    required = (
        candidate.memory.source_turn_ids
        if candidate.memory is not None
        else (candidate.anchor.turn_id,)
        if candidate.anchor is not None
        else ()
    )
    if not required or any(v not in by_id for v in required):
        return None
    sources: list[SourceStamp] = []
    ends: dict[str, int] = {}
    for dependency in candidate.dependencies:
        material = by_id.get(dependency.turn_id)
        if material is None:
            return None
        for reader in dict.fromkeys((dependency.character_id, character_id)):
            decision = material_decision(
                character_id=reader,
                session=material.session,
                turn=material.turn,
                actor_is_character=material.actor_is_character,
                receipts=material.receipt_state,
                now=now,
                delivery_timeout=delivery_timeout,
            )
            if decision.text is None or dependency.used_end > len(decision.text):
                return None
            sources.append(
                SourceStamp(
                    turn_id=dependency.turn_id,
                    character_id=reader,
                    basis=decision.basis,
                    receipt_version=decision.receipt_version,
                    available_end=len(decision.text),
                    used_end=dependency.used_end,
                    deadline=decision.deadline,
                )
            )
            ends[dependency.turn_id] = min(
                ends.get(dependency.turn_id, len(decision.text)), len(decision.text)
            )
    spans: list[SourceSpan] = []
    for identifier in required:
        material = by_id[identifier]
        if identifier not in ends or ends[identifier] == 0:
            return None
        end = ends[identifier]
        spans.append(
            SourceSpan(
                turn_id=identifier,
                start=0,
                end=end,
                text=material.turn.text[:end],
                actor=material.turn.actor,
                language=material.turn.language,
                truncated=False,
                original_start=0,
                original_end=end,
            )
        )
    return ResolvedCandidate(candidate=candidate, spans=tuple(spans), sources=tuple(sources))


def snapshot_matches(expected: ProvisionSnapshot, actual: ProvisionSnapshot) -> bool:
    return expected == actual
