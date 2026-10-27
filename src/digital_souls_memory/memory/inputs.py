"""Resolve extraction materials and their version from the same round trip."""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass

from digital_souls_memory.evidence.domain import Turn
from digital_souls_memory.evidence.materials import EvidenceMaterials, MaterialDecision
from digital_souls_memory.evidence.ports import EvidenceStore
from digital_souls_memory.memory.domain import InputDependency, MemoryValidationError


@dataclass(frozen=True)
class ResolvedTurn:
    turn: Turn
    decision: MaterialDecision | None
    eligible: bool


@dataclass(frozen=True)
class ResolvedInput:
    turns: tuple[ResolvedTurn, ...]

    def dependencies(self) -> tuple[InputDependency, ...]:
        return tuple(
            InputDependency(
                turn_id=v.turn.turn_id,
                basis=v.decision.basis,
                receipt_version=v.decision.receipt_version,
                available_end=len(v.decision.text),
                used_end=len(v.decision.text),
                deadline=v.decision.deadline,
            )
            for v in self.available
            if v.decision is not None and v.decision.text is not None
        )

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
                hashlib.sha256(v.decision.text.encode("utf-8")).hexdigest()
                if v.decision and v.decision.text is not None
                else None,
                v.decision.basis if v.decision else None,
                v.decision.receipt_version if v.decision else None,
                v.decision.deadline.isoformat() if v.decision else None,
                v.eligible,
            )
            for v in self.turns
        ]
        return hashlib.sha256(json.dumps((raw, notification)).encode()).hexdigest()


def input_eligible(
    turn: Turn,
    *,
    actor_is_character: bool,
    conversation_surfaces: frozenset[str],
    comment_surfaces: frozenset[str],
    has_character_reply: bool,
) -> bool:
    if actor_is_character or turn.kind != "utterance":
        return True
    if turn.surface in conversation_surfaces:
        return True
    return turn.surface in comment_surfaces and has_character_reply


def resolve_input(turn_ids: tuple[str, ...], values: Mapping[str, ResolvedTurn]) -> ResolvedInput:
    turns: dict[str, ResolvedTurn] = {}
    for position, identifier in enumerate(turn_ids):
        value = values.get(identifier)
        if value is None:
            continue
        turns[identifier] = value
        if position == 0 and value.turn.reply_to_turn_id is not None:
            reply = values.get(value.turn.reply_to_turn_id)
            if reply is not None:
                turns[reply.turn.turn_id] = reply
    return ResolvedInput(tuple(turns.values()))


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
        replied = False
        actor_is_character = self.materials.characters.is_character(turn.actor)
        if (
            not actor_is_character
            and turn.kind == "utterance"
            and turn.surface in self.comment_surfaces
        ):
            with self.evidence.list_perceived_turns(character_id) as values:
                replied = any(
                    v.actor == character_id and v.reply_to_turn_id == turn.turn_id for v in values
                )
        return input_eligible(
            turn,
            actor_is_character=actor_is_character,
            conversation_surfaces=self.conversation_surfaces,
            comment_surfaces=self.comment_surfaces,
            has_character_reply=replied,
        )

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
        return resolve_input(
            turn_ids,
            {
                turn.turn_id: ResolvedTurn(
                    turn,
                    self.materials.get_decision(character_id, turn.turn_id),
                    self.eligible(character_id, turn),
                )
                for turn in turns.values()
            },
        )
