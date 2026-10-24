"""Context port fake for synchronous worker tests, using already resolved windows.

Input resolution and PostgreSQL transactions are deliberately outside this fake.
"""

from dataclasses import replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from digital_souls_memory.episodes.domain import Episode
    from digital_souls_memory.evidence.ports import EvidenceStore
    from digital_souls_memory.resident.domain import (
        CharacterState,
        ContextClaim,
        ContextKey,
        ContextSnapshot,
        ContextWorkCursor,
        ContextWorkPage,
        ContextWorkState,
        ResolvedWindow,
    )

Operation = Literal["state", "episodes"]


class FakeContextStore:
    def __init__(self, window: "ResolvedWindow") -> None:
        self.window = window
        self.work: dict[tuple[Operation, ContextKey], ContextWorkState] = {}
        self.states: dict[ContextKey, CharacterState] = {}
        self.episodes: dict[ContextKey, tuple[Episode, ...]] = {}
        self.sequence = 0

    def load_window(self, key: "ContextKey", *, now: datetime) -> "ResolvedWindow":
        if key == self.window.key:
            return self.window
        assert key.session_id == self.window.session.session_id
        return replace(
            self.window,
            key=key,
            turns=(),
            snapshot=replace(
                self.window.snapshot,
                key=key,
                signature="synthetic-empty-window",
                dependencies=(),
                next_due_at=None,
            ),
        )

    def work_page(
        self, *, after: "ContextWorkCursor | None", now: datetime, limit: int
    ) -> "ContextWorkPage":
        from digital_souls_memory.resident.domain import (
            ContextWorkCursor,
            ContextWorkItem,
            ContextWorkPage,
        )

        items = []
        operations: tuple[Operation, ...] = ("episodes", "state")
        for op in operations:
            previous = self.get_work(op, self.window.key)
            cursor = (
                self.window.key.surface,
                self.window.key.session_id,
                self.window.key.character_id,
                op,
            )
            if after is not None and cursor <= (
                after.surface,
                after.session_id,
                after.character_id,
                after.operation,
            ):
                continue
            if previous is not None and previous.lease_until is not None:
                if previous.lease_until > now:
                    continue
            if previous is not None and previous.signature == self.window.snapshot.signature:
                if previous.status in ("done", "failed"):
                    if previous.next_due_at is None or previous.next_due_at > now:
                        continue
                if previous.retry_at is not None and previous.retry_at > now:
                    continue
            items.append(ContextWorkItem(operation=op, key=self.window.key))
        selected = tuple(items[:limit])
        next_after = None
        if len(items) > limit:
            last = selected[-1]
            next_after = ContextWorkCursor(
                surface=last.key.surface,
                session_id=last.key.session_id,
                character_id=last.key.character_id,
                operation=last.operation,
            )
        return ContextWorkPage(items=selected, next_after=next_after)

    def claim(
        self, *, operation: Operation, key: "ContextKey", now: datetime
    ) -> "ContextClaim | None":
        from digital_souls_memory.resident.domain import ContextClaim, ContextWorkState

        previous = self.get_work(operation, key)
        if previous is not None:
            if previous.lease_until is not None and previous.lease_until > now:
                return None
            if previous.signature == self.window.snapshot.signature:
                if previous.status in ("done", "failed"):
                    if previous.next_due_at is None or previous.next_due_at > now:
                        return None
                if previous.retry_at is not None and previous.retry_at > now:
                    return None
        self.sequence += 1
        claim = ContextClaim(
            operation=operation,
            key=key,
            token=str(self.sequence),
            lease_until=now + timedelta(minutes=5),
        )
        self.work[(operation, key)] = ContextWorkState(
            operation=operation,
            key=key,
            status="running",
            signature=None,
            completed_signature=(previous.completed_signature if previous is not None else None),
            token=claim.token,
            lease_until=claim.lease_until,
            retry_at=None,
            next_due_at=None,
            error=None,
            evaluated_version=None,
            delivery_timeout=None,
        )
        return claim

    def _owns(self, claim: "ContextClaim") -> bool:
        current = self.get_work(claim.operation, claim.key)
        return current is not None and current.token == claim.token

    def begin(self, claim: "ContextClaim", *, snapshot: "ContextSnapshot", now: datetime) -> bool:
        if not self._owns(claim) or snapshot != self.window.snapshot:
            return False
        current = self.work[(claim.operation, claim.key)]
        self.work[(claim.operation, claim.key)] = replace(
            current,
            signature=snapshot.signature,
            evaluated_version=snapshot.notification_version,
            delivery_timeout=snapshot.delivery_timeout,
        )
        return True

    def _complete(self, claim: "ContextClaim", snapshot: "ContextSnapshot") -> bool:
        if not self._owns(claim) or snapshot != self.window.snapshot:
            return False
        current = self.work[(claim.operation, claim.key)]
        self.work[(claim.operation, claim.key)] = replace(
            current,
            status="done",
            signature=snapshot.signature,
            completed_signature=snapshot.signature,
            token=None,
            lease_until=None,
            retry_at=None,
            next_due_at=snapshot.next_due_at,
            error=None,
        )
        return True

    def complete_state(
        self,
        claim: "ContextClaim",
        value: "CharacterState | None",
        *,
        snapshot: "ContextSnapshot",
        now: datetime,
    ) -> bool:
        if not self._complete(claim, snapshot):
            return False
        if value is None:
            self.states.pop(claim.key, None)
        else:
            self.states[claim.key] = value
        return True

    def complete_episodes(
        self,
        claim: "ContextClaim",
        values: "tuple[Episode, ...]",
        *,
        snapshot: "ContextSnapshot",
        now: datetime,
    ) -> bool:
        from digital_souls_memory.evidence.ports import EvidenceConflict

        identifiers = [value.episode_id for value in values]
        saved = {value.episode_id for batch in self.episodes.values() for value in batch}
        if len(set(identifiers)) != len(identifiers) or saved.intersection(identifiers):
            raise EvidenceConflict("Synthetic episode ID collision")
        if not self._complete(claim, snapshot):
            return False
        self.episodes[claim.key] = values
        return True

    def fail(self, claim: "ContextClaim", *, error: str, retry_at: datetime | None) -> None:
        if self._owns(claim):
            current = self.work[(claim.operation, claim.key)]
            self.work[(claim.operation, claim.key)] = replace(
                current,
                status="retry" if retry_at is not None else "failed",
                error=error,
                token=None,
                lease_until=None,
                retry_at=retry_at,
            )

    def get_work(self, operation: Operation, key: "ContextKey") -> "ContextWorkState | None":
        return self.work.get((operation, key))

    def get_state(self, key: "ContextKey") -> "CharacterState | None":
        return self.states.get(key)

    def list_episodes(self, key: "ContextKey") -> "tuple[Episode, ...]":
        return self.episodes.get(key, ())


class EvidenceContextStore(FakeContextStore):
    """Discover saved evidence on scans, leaving input resolution to production code."""

    def __init__(self, evidence: "EvidenceStore", key: "ContextKey", now: datetime) -> None:
        self.evidence = evidence
        self.key = key
        super().__init__(self.load_window(key, now=now))

    def load_window(self, key: "ContextKey", *, now: datetime) -> "ResolvedWindow":
        from digital_souls_memory.evidence.materials import aggregate_receipts
        from digital_souls_memory.recall.domain import CandidateMaterial
        from digital_souls_memory.resident.inputs import resolve_window

        session = self.evidence.get_session(key.session_id)
        assert session is not None
        materials = []
        with self.evidence.list_perceived_turns(key.character_id) as turns:
            for turn in turns:
                source_session = self.evidence.get_session(turn.session_id)
                assert source_session is not None
                with self.evidence.list_receipts(turn.turn_id) as receipts:
                    materials.append(
                        CandidateMaterial(
                            turn=turn,
                            session=source_session,
                            actor_is_character=turn.actor.startswith("char-"),
                            receipt_state=aggregate_receipts(receipts),
                        )
                    )
        return resolve_window(
            key=key,
            session=session,
            materials=tuple(materials),
            notification_version=0,
            now=now,
            delivery_timeout=timedelta(minutes=10),
        )

    def work_page(
        self, *, after: "ContextWorkCursor | None", now: datetime, limit: int
    ) -> "ContextWorkPage":
        self.window = self.load_window(self.key, now=now)
        return super().work_page(after=after, now=now, limit=limit)
