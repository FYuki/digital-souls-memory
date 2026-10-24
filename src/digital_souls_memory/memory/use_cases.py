"""Read and rebuild projections using the same temporal evaluation rules."""

from digital_souls_memory.evidence.use_cases import Clock
from digital_souls_memory.memory.domain import DerivedMemory, memory_valid_at
from digital_souls_memory.memory.inputs import MemoryInputs
from digital_souls_memory.memory.ports import MemoryListing, MemoryStore, MemoryTransaction
from digital_souls_memory.memory.projection import collect_evidence, projection_input


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
        while True:
            expanded = (
                identifiers | tx.projection_neighbors(identifiers) if identifiers else identifiers
            )
            if expanded == identifiers:
                return identifiers
            identifiers = expanded

    def rebuild(self, tx: MemoryTransaction, identifiers: frozenset[str] | None = None) -> None:
        if identifiers == frozenset():
            return
        snapshot = projection_input(tx, identifiers)
        owners = {v.character_id for v in snapshot.existing} | {
            v.memory.character_id for v in snapshot.contributions
        }
        if not owners:
            return
        (character_id,) = owners
        evidence = collect_evidence(snapshot, character_id, self.inputs)
        result = evidence.project(snapshot, self.clock(), select_representatives=True)
        existing = {v.id: v for v in snapshot.existing}
        withdrawn = tx.withdrawn_ids(frozenset(result.memories))
        for identifier, value in result.memories.items():
            if identifier in existing:
                if value != existing[identifier] or identifier in withdrawn:
                    tx.replace_projection(value)
            else:
                tx.add_generated(value)
        tx.sync_projection_relations(snapshot.generated, result.relations)
        tx.sync_representatives(snapshot.generated, result.representatives)
