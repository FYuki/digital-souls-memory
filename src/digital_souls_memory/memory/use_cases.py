"""Read and rebuild projections using the same temporal evaluation rules."""

from dataclasses import replace

from digital_souls_memory.evidence.use_cases import Clock
from digital_souls_memory.memory.domain import DerivedMemory, memory_valid_at
from digital_souls_memory.memory.inputs import MemoryInputs
from digital_souls_memory.memory.ports import MemoryListing, MemoryStore, MemoryTransaction
from digital_souls_memory.memory.projection import (
    collect_evidence,
    projection_input,
    save_projection,
)


class MemoryService:
    def __init__(
        self,
        store: MemoryStore,
        inputs: MemoryInputs,
        clock: Clock,
        listing: MemoryListing | None = None,
    ) -> None:
        self.store = store
        self.inputs = inputs
        self.evidence = inputs.evidence
        self.clock = clock
        self.listing = listing

    def active(self, character_id: str) -> tuple[DerivedMemory, ...]:
        now = self.clock()
        if self.listing is not None:
            snapshot, evidence = self.listing.read(character_id, now)
        else:
            with self.store.transaction(character_id) as tx:
                snapshot = projection_input(tx)
            evidence = collect_evidence(snapshot, character_id, self.inputs)
        result = evidence.project(snapshot, now)
        return tuple(v for v in result.memories.values() if memory_valid_at(v, now))

    def affected(self, tx: MemoryTransaction, turn_id: str) -> frozenset[str]:
        identifiers = tx.contribution_ids(turn_id)
        source_only = frozenset(
            c.fact_id
            for c in tx.source_contributions(identifiers)
            if c.extraction_turn_id == turn_id
        )
        if source_only:
            snapshot = projection_input(tx, source_only)
            remaining = replace(
                snapshot,
                contributions=tuple(c for c in snapshot.contributions if c.turn_id != turn_id),
                reinforcements=tuple(
                    c for c in snapshot.reinforcements if c.extraction_turn_id != turn_id
                ),
                effects=tuple(e for e in snapshot.effects if e.turn_id != turn_id),
            )
            now = self.clock()
            evidence = tx.projection_evidence(remaining, self.inputs, now)
            survivors = evidence.project(remaining, now).memories
            source_only = frozenset(
                identifier
                for identifier in source_only
                if identifier in survivors and memory_valid_at(survivors[identifier], now)
            )
        while True:
            expanded = (
                identifiers | tx.projection_neighbors(identifiers, source_only)
                if identifiers
                else identifiers
            )
            if expanded == identifiers:
                return identifiers
            identifiers = expanded

    def requeue(self, turn_id: str) -> None:
        with self.store.requeue_transaction(turn_id) as transactions:
            for tx in transactions:
                affected = self.affected(tx, turn_id)
                tx.set_source_contributions(turn_id, ())
                tx.set_relation_effects(turn_id, ())
                self.rebuild(tx, affected)

    def rebuild(self, tx: MemoryTransaction, identifiers: frozenset[str] | None = None) -> None:
        if identifiers == frozenset():
            return
        snapshot = projection_input(tx, identifiers)
        if not snapshot.existing and not snapshot.contributions:
            return
        evidence = tx.projection_evidence(snapshot, self.inputs, self.clock())
        result = evidence.project(snapshot, self.clock())
        save_projection(tx, snapshot, result)
