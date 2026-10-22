"""Coordinate deletion without retaining model or storage exception contents."""

from digital_souls_memory.evidence.use_cases import Clock
from digital_souls_memory.governance.domain import AuthorPrincipal, DeletionCommand, DeletionRequest
from digital_souls_memory.governance.ports import DeletionStore
from digital_souls_memory.governance.recalculation import MemoryRecalculator
from digital_souls_memory.memory.domain import MemoryValidationError
from digital_souls_memory.memory.ports import MemoryConflict


class DeletionService:
    def __init__(
        self, store: DeletionStore, recalculator: MemoryRecalculator, clock: Clock
    ) -> None:
        self.store = store
        self.recalculator = recalculator
        self.clock = clock

    def execute(self, command: DeletionCommand, *, principal: AuthorPrincipal) -> DeletionRequest:
        attempt = self.store.begin(command, principal=principal, now=self.clock())
        if attempt.request.status == "done":
            return attempt.request
        if attempt.snapshot is None or attempt.token is None:
            raise MemoryConflict()
        try:
            replacements = tuple(
                self.recalculator.recalculate(copy, snapshot=attempt.snapshot, now=self.clock())
                for copy in attempt.snapshot.copies
            )
            return self.store.commit(attempt, replacements=replacements, now=self.clock())
        except Exception as error:
            reason = (
                "invalid_output"
                if isinstance(error, MemoryValidationError)
                else "snapshot_conflict"
                if isinstance(error, MemoryConflict)
                else "dependency_unavailable"
            )
            self.store.fail(attempt, error=reason, now=self.clock())
            raise

    def get(self, request_id: str, *, principal: AuthorPrincipal) -> DeletionRequest | None:
        return self.store.get(request_id)
