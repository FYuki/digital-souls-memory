"""Synthetic gateway and shared storage for deletion integration tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING

from digital_souls_memory.evidence.materials import aggregate_receipts
from digital_souls_memory.memory.domain import DerivedMemory
from digital_souls_memory.memory.extraction import CloudInput, LocalOnly
from digital_souls_memory.memory.ports import MemoryConflict, MemoryUnavailable
from digital_souls_memory.recall.domain import CandidateMaterial
from tests.it1.evidence_fakes import FakeEvidenceStore
from tests.memory_fakes import FakeMemoryStore
from tests.recall_support import FakeProvisionStore

if TYPE_CHECKING:
    from digital_souls_memory.governance.domain import (
        AuthorPrincipal,
        DeletionAttempt,
        DeletionCommand,
        DeletionRequest,
        MemoryReplacement,
    )


class RecordingRecalculationGateway:
    def __init__(self, result: DerivedMemory | None) -> None:
        self.result = result
        self.calls: list[tuple[LocalOnly | CloudInput, str, str]] = []
        self.local_calls: list[LocalOnly] = []
        self.cloud_calls: list[CloudInput] = []
        self.failure: Exception | None = None

    def recalculate_local(
        self, value: LocalOnly, *, memory_id: str, kind: str
    ) -> DerivedMemory | None:
        self.local_calls.append(value)
        self.calls.append((value, memory_id, kind))
        if self.failure is not None:
            raise self.failure
        return self.result

    def recalculate_cloud(
        self, value: CloudInput, *, memory_id: str, kind: str
    ) -> DerivedMemory | None:
        self.cloud_calls.append(value)
        self.calls.append((value, memory_id, kind))
        if self.failure is not None:
            raise self.failure
        return self.result


class SharedDeletionStore:
    """Storage boundary only; HTTP, orchestration and recalculation stay real.

    These tests seed projection/baseline copies. SQL permissions, other copy
    tables, leases and concurrent snapshot validation belong to postgres tests.
    """

    def __init__(
        self,
        evidence: FakeEvidenceStore,
        memories: FakeMemoryStore,
        provisions: FakeProvisionStore,
    ) -> None:
        self.evidence = evidence
        self.memories = memories
        self.provisions = provisions
        self.requests: dict[str, DeletionRequest] = {}
        self.fail_commit = False

    def begin(
        self,
        command: DeletionCommand,
        *,
        principal: AuthorPrincipal,
        now: datetime,
    ) -> DeletionAttempt:
        from digital_souls_memory.governance.domain import (
            DeletionAttempt,
            DeletionRequest,
            DeletionSnapshot,
            MemoryCopy,
            select_target_turn_ids,
        )

        previous = self.requests.get(command.request_id)
        if previous is not None:
            if previous.command != command or previous.requested_by != principal.author_id:
                raise MemoryConflict()
            if previous.status == "done":
                return DeletionAttempt(request=previous, token=None, snapshot=None)
            if previous.status == "running":
                raise MemoryConflict()
            targets = previous.target_turn_ids
        else:
            targets = select_target_turn_ids(tuple(self.evidence.turns.values()), command)
        request = DeletionRequest(
            command=command,
            requested_by=principal.author_id,
            target_turn_ids=targets,
            status="running",
            cascade_result=None,
            error=None,
            requested_at=previous.requested_at if previous is not None else now,
            completed_at=None,
        )
        copies = tuple(
            MemoryCopy(table=table, memory=value, origin_turn_id=None)
            for table, values in (
                ("derived_memories", self.memories.values),
                ("memory_baselines", self.memories.originals),
            )
            for value in values.values()
            if set(value.source_turn_ids) & set(targets)
        )
        source_ids = {identifier for copy in copies for identifier in copy.memory.source_turn_ids}
        materials = []
        for identifier in sorted(source_ids - set(targets)):
            source = self.evidence.get_turn(identifier)
            assert source is not None
            source_session = self.evidence.get_session(source.session_id)
            assert source_session is not None
            with self.evidence.list_receipts(identifier) as receipts:
                materials.append(
                    CandidateMaterial(
                        turn=source,
                        session=source_session,
                        actor_is_character=source.actor.startswith("char-"),
                        receipt_state=aggregate_receipts(receipts),
                    )
                )
        self.requests[command.request_id] = request
        return DeletionAttempt(
            request=request,
            token="synthetic-attempt",
            snapshot=DeletionSnapshot(
                target_turn_ids=targets,
                copies=copies,
                materials=tuple(materials),
                references=(),
                fingerprint="synthetic-snapshot",
                observed_at=now,
            ),
        )

    def commit(
        self,
        attempt: DeletionAttempt,
        *,
        replacements: tuple[MemoryReplacement, ...],
        now: datetime,
    ) -> DeletionRequest:
        from digital_souls_memory.governance.domain import CascadeResult, MemoryRef

        if self.fail_commit:
            self.fail_commit = False
            raise MemoryUnavailable()
        targets = set(attempt.request.target_turn_ids)
        values, originals = dict(self.memories.values), dict(self.memories.originals)
        recalculated, invalidated = set(), set()
        for replacement in replacements:
            old, value = replacement.copy.memory, replacement.value
            key = (old.character_id, old.id)
            destination = values if replacement.copy.table == "derived_memories" else originals
            if value is None:
                destination.pop(key, None)
                if replacement.copy.table == "derived_memories":
                    invalidated.add(key)
            else:
                destination[key] = value
                if replacement.copy.table == "derived_memories":
                    recalculated.add(key)
        for replacement in replacements:
            if replacement.copy.table != "derived_memories":
                continue
            key = (replacement.copy.memory.character_id, replacement.copy.memory.id)
            if replacement.value is None:
                originals.pop(key, None)
            else:
                originals[key] = replacement.value
        provision_ids = tuple(
            sorted(
                identifier
                for identifier, record in self.provisions.records.items()
                if record.turn_id in targets
                or any(span.turn_id in targets for span in record.source_spans)
                or any(
                    r.kind == "memory" and (r.owner_id, r.identifier) in recalculated | invalidated
                    for r in record.provided_refs
                )
            )
        )
        result = replace(
            attempt.request,
            status="done",
            completed_at=now,
            error=None,
            cascade_result=CascadeResult(
                deleted_turn_ids=tuple(sorted(targets)),
                recalculated_memories=tuple(
                    MemoryRef(character_id=owner, memory_id=identifier)
                    for owner, identifier in sorted(recalculated)
                ),
                invalidated_memories=tuple(
                    MemoryRef(character_id=owner, memory_id=identifier)
                    for owner, identifier in sorted(invalidated)
                ),
                invalidated_provision_ids=provision_ids,
                removed_references=(),
            ),
        )
        self.memories.values, self.memories.originals = values, originals
        self.evidence.turns = {
            identifier: replace(source, reply_to_turn_id=None)
            if source.reply_to_turn_id in targets
            else source
            for identifier, source in self.evidence.turns.items()
            if identifier not in targets
        }
        self.evidence.receipts = [r for r in self.evidence.receipts if r.turn_id not in targets]
        self.provisions.records = {
            identifier: record
            for identifier, record in self.provisions.records.items()
            if identifier not in provision_ids
        }
        self.requests[attempt.request.command.request_id] = result
        return result

    def fail(self, attempt: DeletionAttempt, *, error: str, now: datetime) -> None:
        self.requests[attempt.request.command.request_id] = replace(
            attempt.request,
            status="failed",
            error=error,
            completed_at=now,
        )

    def get(self, request_id: str) -> DeletionRequest | None:
        return self.requests.get(request_id)
