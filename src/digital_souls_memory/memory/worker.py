"""Durable leased extraction with input-version checks and atomic projection updates."""

import hashlib
import json
import logging
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import replace
from datetime import timedelta
from threading import Event, Thread
from uuid import uuid4

from digital_souls_memory.evidence.materials import MaterialDecision
from digital_souls_memory.evidence.ports import EvidenceUnavailable
from digital_souls_memory.memory.domain import DeliveryBasis, ExtractionState, MemoryValidationError
from digital_souls_memory.memory.extraction import MemoryExtractor
from digital_souls_memory.memory.ports import MemoryConflict, MemoryUnavailable, WorkCursor
from digital_souls_memory.memory.use_cases import MemoryService

logger = logging.getLogger(__name__)


class MemoryWorker:
    def __init__(self, service: MemoryService, extractor: MemoryExtractor) -> None:
        self.service = service
        self.extractor = extractor
        self.stop = Event()
        self.thread = Thread(target=self._run, name="memory-extraction", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.stop.set()
        self.thread.join()

    def _run(self) -> None:
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="memory-surface") as pool:
            while not self.stop.is_set():
                try:
                    self._scan(pool)
                except (MemoryUnavailable, EvidenceUnavailable):
                    logger.warning("Memory storage unavailable; durable work remains pending")
                self.stop.wait(0.05)

    def _scan(self, pool: ThreadPoolExecutor) -> None:
        after = None
        while not self.stop.is_set():
            page = self.service.store.surface_page(after=after, limit=4)
            futures = [pool.submit(self._surface, surface) for surface in page.surfaces]
            wait(futures)
            for future in futures:
                future.result()
            if page.next_after is None:
                return
            after = page.next_after

    def _surface(self, surface: str) -> None:
        with self.service.store.claim_surface(surface, self.service.clock()) as acquired:
            if acquired:
                after: WorkCursor | None = None
                while not self.stop.is_set():
                    page = self.service.store.work_page(surface, after=after, limit=64)
                    for item in page.items:
                        if self.stop.is_set():
                            return
                        self.process(item.character_id, item.turn_id)
                    if page.next_after is None:
                        return
                    after = page.next_after

    def _signature(
        self, character: str, turn_id: str, decision: MaterialDecision, version: int
    ) -> str:
        turn = self.service.evidence.get_turn(turn_id)
        eligible = turn is not None and self.extractor.eligible(character, turn)
        raw = json.dumps(
            (
                decision.text,
                decision.basis,
                decision.receipt_version,
                decision.deadline.isoformat(),
                version
                if decision.basis in ("pending", "assumed_full", "receipt_confirmed")
                else 0,
                eligible,
            )
        )
        return hashlib.sha256(raw.encode()).hexdigest()

    def process(self, character: str, turn_id: str) -> None:
        decision = self.service.materials.get_decision(character, turn_id)
        if decision is None or decision.basis == "inaccessible":
            return
        now = self.service.clock()
        token = uuid4().hex
        with self.service.store.transaction(character) as tx:
            version = tx.notification_version(turn_id)
            signature = self._signature(character, turn_id, decision, version)
            old = tx.get_extraction(turn_id)
            if old is not None and old.signature == signature:
                if old.status in ("done", "failed", "pending"):
                    return
                if old.lease_until is not None and old.lease_until > now:
                    return
            state = ExtractionState(
                character_id=character,
                turn_id=turn_id,
                delivery_basis=DeliveryBasis(decision.basis),
                signature=signature,
                status="running"
                if decision.text
                else "pending"
                if decision.text is None
                else "done",
                token=token,
                lease_until=now + timedelta(minutes=5),
                deadline=decision.deadline,
                error=None,
                used_text=decision.text,
                receipt_version=decision.receipt_version,
            )
            tx.save_extraction(state)
            # Withdrawing the previous contribution precedes the fallible model call.
            self.service.rebuild(tx)
        if not decision.text:
            return
        try:
            proposal = self.extractor.propose(character_id=character, turn_ids=(turn_id,))
            current = self.service.materials.get_decision(character, turn_id)
            with self.service.store.transaction(character) as tx:
                saved = tx.get_extraction(turn_id)
                if saved is None or saved.token != token:
                    return
                if (
                    current is None
                    or self._signature(
                        character, turn_id, current, tx.notification_version(turn_id)
                    )
                    != signature
                ):
                    tx.save_extraction(replace(saved, status="retry", lease_until=now))
                    return
                tx.set_candidates(turn_id, proposal.memories)
                tx.set_relations(turn_id, proposal.relations)
                tx.save_extraction(replace(saved, status="done", token=None, lease_until=None))
                self.service.rebuild(tx)
        except (
            MemoryValidationError,
            MemoryConflict,
            RuntimeError,
            MemoryUnavailable,
            EvidenceUnavailable,
        ) as error:
            with self.service.store.transaction(character) as tx:
                saved = tx.get_extraction(turn_id)
                if saved is not None and saved.token == token:
                    permanent = isinstance(error, (MemoryValidationError, MemoryConflict))
                    tx.save_extraction(
                        replace(
                            saved,
                            status="failed" if permanent else "retry",
                            token=None,
                            lease_until=now + timedelta(seconds=1),
                            error="invalid_output" if permanent else "dependency_unavailable",
                        )
                    )
