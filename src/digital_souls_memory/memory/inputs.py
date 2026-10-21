"""Resolve extraction materials and their version from the same round trip."""

import hashlib
import json
from dataclasses import dataclass

from digital_souls_memory.evidence.domain import Turn
from digital_souls_memory.evidence.materials import EvidenceMaterials, MaterialDecision
from digital_souls_memory.evidence.ports import EvidenceStore
from digital_souls_memory.memory.domain import MemoryValidationError


@dataclass(frozen=True)
class ResolvedTurn:
    turn: Turn
    decision: MaterialDecision | None
    eligible: bool


@dataclass(frozen=True)
class ResolvedInput:
    turns: tuple[ResolvedTurn, ...]

    @property
    def available(self) -> tuple[ResolvedTurn, ...]:
        if not self.turns or not self.turns[0].eligible or not self.turns[0].decision:
            return ()
        if not self.turns[0].decision.text:
            return ()
        return tuple(v for v in self.turns if v.eligible and v.decision and v.decision.text)

    def signature(self, version: int) -> str:
        primary = self.turns[0].decision if self.turns else None
        notification = (
            version
            if primary and primary.basis in ("pending", "assumed_full", "receipt_confirmed")
            else 0
        )
        raw = [
            (
                v.turn.turn_id,
                v.decision.text if v.decision else None,
                v.decision.basis if v.decision else None,
                v.decision.receipt_version if v.decision else None,
                v.decision.deadline.isoformat() if v.decision else None,
                v.eligible,
            )
            for v in self.turns
        ]
        return hashlib.sha256(json.dumps((raw, notification)).encode()).hexdigest()


class MemoryInputs:
    def __init__(
        self,
        evidence: EvidenceStore,
        materials: EvidenceMaterials,
        *,
        conversation_surfaces: frozenset[str],
        comment_surfaces: frozenset[str],
    ) -> None:
        if conversation_surfaces & comment_surfaces:
            raise MemoryValidationError("Surface classifications overlap")
        self.evidence = evidence
        self.materials = materials
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

    def resolve(self, character_id: str, turn_ids: tuple[str, ...]) -> ResolvedInput:
        turns: dict[str, Turn] = {}
        for position, identifier in enumerate(turn_ids):
            turn = self.evidence.get_turn(identifier)
            if turn is None:
                continue
            turns[identifier] = turn
            if position == 0 and turn.reply_to_turn_id is not None:
                reply = self.evidence.get_turn(turn.reply_to_turn_id)
                if reply is not None:
                    turns[reply.turn_id] = reply
        return ResolvedInput(
            tuple(
                ResolvedTurn(
                    turn,
                    self.materials.get_decision(character_id, turn.turn_id),
                    self.eligible(character_id, turn),
                )
                for turn in turns.values()
            )
        )
