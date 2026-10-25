"""Recalculate each stored copy from its remaining accessible originals."""

from dataclasses import replace
from datetime import datetime, timedelta

from digital_souls_memory.evidence.materials import material_decision
from digital_souls_memory.governance.domain import DeletionSnapshot, MemoryCopy, MemoryReplacement
from digital_souls_memory.governance.ports import RecalculationGateway
from digital_souls_memory.memory.changes import apply_temporal_effect
from digital_souls_memory.memory.domain import DerivedMemory, MemoryValidationError
from digital_souls_memory.memory.evidence import independent_evidence_count
from digital_souls_memory.memory.extraction import CloudInput, make_gateway_input
from digital_souls_memory.memory.ports import MemoryUnavailable
from digital_souls_memory.memory.reinforcement import aggregate_sources
from digital_souls_memory.recall.domain import SourceStamp


class MemoryRecalculator:
    def __init__(
        self,
        gateway: RecalculationGateway | None,
        *,
        delivery_timeout: timedelta,
        non_sensitive_labels: frozenset[str],
    ) -> None:
        self.gateway = gateway
        self.delivery_timeout = delivery_timeout
        self.non_sensitive_labels = non_sensitive_labels

    def recalculate(
        self, copy: MemoryCopy, *, snapshot: DeletionSnapshot, now: datetime
    ) -> MemoryReplacement:
        old = copy.memory
        invalidated = MemoryReplacement(copy=copy, value=None)
        targets = set(snapshot.target_turn_ids)
        unavailable_origins = targets | set(copy.withdrawn_origins)
        remaining_contributions = tuple(
            c
            for c in copy.reinforcements
            if c.extraction_turn_id not in unavailable_origins and c.source_turn_id not in targets
        )
        unsupported_sources = (
            {c.source_turn_id for c in copy.reinforcements}
            - set(copy.body_source_ids)
            - {c.source_turn_id for c in remaining_contributions}
        )
        if copy.origin_turn_id in targets or old.verbatim_turn_id in targets:
            return invalidated
        materials = []
        dependencies = []
        effect_sources = {i for e in copy.temporal_effects for i in e.candidate.source_turn_ids}
        for material in snapshot.materials:
            turn = material.turn
            if (
                turn.turn_id not in (*old.source_turn_ids, *effect_sources)
                or turn.turn_id in targets
                or turn.turn_id in unsupported_sources
            ):
                continue
            decision = material_decision(
                character_id=old.character_id,
                session=material.session,
                turn=turn,
                actor_is_character=material.actor_is_character,
                receipts=material.receipt_state,
                now=now,
                delivery_timeout=self.delivery_timeout,
            )
            if not decision.text:
                continue
            materials.append((turn, decision.text))
            dependencies.append(
                SourceStamp(
                    turn_id=turn.turn_id,
                    character_id=old.character_id,
                    basis=decision.basis,
                    receipt_version=decision.receipt_version,
                    available_end=len(decision.text),
                    used_end=len(decision.text),
                    deadline=decision.deadline,
                )
            )
        originals = [(t, text) for t, text in materials if t.turn_id in old.source_turn_ids]
        if not originals:
            return invalidated
        if self.gateway is None:
            raise MemoryUnavailable()
        value = make_gateway_input(
            character_id=old.character_id,
            materials=tuple(originals),
            non_sensitive_labels=self.non_sensitive_labels,
        )
        try:
            output = (
                self.gateway.recalculate_cloud(value, memory_id=old.id, kind=old.kind)
                if isinstance(value, CloudInput)
                else self.gateway.recalculate_local(value, memory_id=old.id, kind=old.kind)
            )
        except MemoryValidationError:
            raise
        except Exception as error:
            raise MemoryUnavailable() from error
        if output is None:
            return invalidated
        if not isinstance(output, DerivedMemory):
            raise MemoryValidationError("Invalid recalculation output")
        output = replace(output)
        sources = {turn.turn_id: turn for turn, _ in materials}
        if (
            (output.id, output.character_id, output.kind) != (old.id, old.character_id, old.kind)
            or not set(output.source_turn_ids) <= {t.turn_id for t, _ in originals}
            or output.shared_with != old.shared_with
            or output.epistemics.source_type != old.epistemics.source_type
            or output.epistemics.trust_tier < old.epistemics.trust_tier
            or output.verbatim_turn_id != old.verbatim_turn_id
        ):
            raise MemoryValidationError("Recalculation changed protected attributes")
        base = copy.temporal_base if copy.temporal_base is not None else old
        result = replace(
            output,
            valid_to=base.valid_to,
            superseded_at=base.superseded_at,
            last_reinforced_at=base.last_reinforced_at,
        )
        if copy.reinforcements:
            strengthened = aggregate_sources(
                result,
                tuple(i for i in copy.body_source_ids if i in output.source_turn_ids),
                tuple(c for c in remaining_contributions if c.source_turn_id in sources),
                sources,
            )
            if strengthened is None:
                return invalidated
            result = strengthened
        for effect in copy.temporal_effects:
            candidate = effect.candidate
            if not set(candidate.source_turn_ids) <= sources.keys():
                continue
            result = apply_temporal_effect(
                result,
                candidate,
                relation_type=effect.relation_type,
                source_turns=tuple(sources.values()),
                at=effect.applied_at,
            )
        return MemoryReplacement(
            copy=copy,
            value=replace(
                result,
                epistemics=replace(
                    output.epistemics,
                    independent_evidence_count=independent_evidence_count(
                        [sources[i] for i in result.source_turn_ids]
                    ),
                ),
            ),
            dependencies=tuple(d for d in dependencies if d.turn_id in result.source_turn_ids),
        )
