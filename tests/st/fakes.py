"""Synthetic model ports only; extraction gates and DB retrieval stay real."""

from dataclasses import replace
from threading import RLock
from uuid import NAMESPACE_URL, uuid5

from digital_souls_memory.memory.domain import DerivedMemory, ExtractionProposal, MemoryRelation
from digital_souls_memory.memory.extraction import CloudInput, LocalOnly
from tests.memory_support import memory


class ScenarioGateway:
    def __init__(self) -> None:
        self.lock = RLock()
        self.candidates: dict[str, DerivedMemory] = {}
        self.origins: dict[tuple[str, str], str] = {}

    def extract_local(self, value: LocalOnly) -> ExtractionProposal:
        return self._propose(value)

    def extract_cloud(self, value: CloudInput) -> ExtractionProposal:
        return self._propose(value)

    def _propose(self, value: LocalOnly | CloudInput) -> ExtractionProposal:
        primary = value.turns[0]
        identifier = str(
            uuid5(NAMESPACE_URL, f"core-st:{value.character_id}:{primary.turn_id}:{primary.text}")
        )
        candidate = replace(
            memory(),
            id=identifier,
            character_id=value.character_id,
            content=primary.text,
            object=primary.text,
            subject=primary.actor,
            source_turn_ids=(primary.turn_id,),
            shared_with=(),
            valid_from=primary.occurred_at,
        )
        # Deliberately submit forbidden and low-confidence proposals to the real M5 gate.
        low = replace(
            candidate,
            id=str(uuid5(NAMESPACE_URL, identifier + ":low")),
            epistemics=replace(
                candidate.epistemics,
                confidence_features=replace(
                    candidate.epistemics.confidence_features,
                    retrieval=0,
                    source=0,
                    contradiction=1,
                    temporal=0,
                    corroboration=0,
                    self_assessment=1,
                ),
            ),
        )
        relationship = replace(
            memory("relationship"),
            id=str(uuid5(NAMESPACE_URL, identifier + ":relationship")),
            character_id=value.character_id,
            source_turn_ids=(primary.turn_id,),
            counterpart="user-a",
            content=primary.text,
        )
        commitment = replace(
            memory("commitment"),
            id=str(uuid5(NAMESPACE_URL, identifier + ":commitment")),
            character_id=value.character_id,
            source_turn_ids=(primary.turn_id,),
            verbatim_turn_id=primary.turn_id,
            counterpart="user-a",
            content=primary.text,
        )
        with self.lock:
            self.candidates[identifier] = candidate
            self.origins[(value.character_id, primary.turn_id)] = identifier
            target = self.origins.get((value.character_id, primary.reply_to_turn_id or ""))
            relations = (
                (MemoryRelation(from_id=identifier, to_id=target, type="extends"),)
                if target is not None and primary.actor == value.character_id
                else ()
            )
        return ExtractionProposal(
            memories=(candidate, low, relationship, commitment),
            relations=relations,
        )

    def recalculate_local(
        self,
        value: LocalOnly,
        *,
        memory_id: str,
        kind: str,
    ) -> DerivedMemory:
        return self._recalculate(value, memory_id, kind)

    def recalculate_cloud(
        self,
        value: CloudInput,
        *,
        memory_id: str,
        kind: str,
    ) -> DerivedMemory:
        return self._recalculate(value, memory_id, kind)

    def _recalculate(
        self,
        value: LocalOnly | CloudInput,
        memory_id: str,
        kind: str,
    ) -> DerivedMemory:
        with self.lock:
            template = self.candidates[memory_id]
        assert template.kind == kind
        text = " / ".join(t.text for t in value.turns)
        return replace(
            template,
            content=text,
            object=text,
            source_turn_ids=tuple(t.turn_id for t in value.turns),
        )
