"""Independent state and episode jobs, with claim ownership and snapshot validation."""

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Event, Thread

from digital_souls_memory.episodes.domain import Episode
from digital_souls_memory.episodes.use_cases import build_episodes
from digital_souls_memory.evidence.ports import EvidenceConflict
from digital_souls_memory.evidence.use_cases import Clock
from digital_souls_memory.memory.extraction import CloudInput, make_gateway_input
from digital_souls_memory.resident.domain import (
    CharacterState,
    ContextKey,
    Operation,
    StateProposal,
)
from digital_souls_memory.resident.ports import ContextGateway, ContextStore

logger = logging.getLogger(__name__)


class ContextWorker:
    def __init__(
        self,
        *,
        store: ContextStore,
        gateway: ContextGateway,
        clock: Clock,
        non_sensitive_labels: frozenset[str],
        state_ttl: timedelta,
        episode_id_factory: Callable[[], str],
    ) -> None:
        if state_ttl <= timedelta(0):
            raise ValueError("State TTL must be positive")
        self.store, self.gateway, self.clock = store, gateway, clock
        self.non_sensitive_labels, self.state_ttl = non_sensitive_labels, state_ttl
        self.episode_id_factory = episode_id_factory
        self._stop = Event()
        self._thread: Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("Context worker already started")
        self._thread = Thread(target=self._run, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()

    def _run(self) -> None:
        with ThreadPoolExecutor(max_workers=2) as pool:
            while not self._stop.is_set():
                try:
                    after = None
                    while not self._stop.is_set():
                        page = self.store.work_page(after=after, now=self.clock(), limit=64)
                        futures = [
                            pool.submit(self._process, item.operation, item.key)
                            for item in page.items
                            if not self._stop.is_set()
                        ]
                        for future in futures:
                            future.result()
                        after = page.next_after
                        if after is None:
                            break
                except Exception:
                    logger.warning("Context scan dependency unavailable")
                self._stop.wait(0.05)

    def process_state(self, key: ContextKey) -> None:
        self._process("state", key)

    def process_episodes(self, key: ContextKey) -> None:
        self._process("episodes", key)

    def _process(self, operation: Operation, key: ContextKey) -> None:
        now = self.clock()
        claim = self.store.claim(operation=operation, key=key, now=now)
        if claim is None:
            return
        try:
            window = self.store.load_window(key, now=now)
            if not self.store.begin(claim, snapshot=window.snapshot, now=now):
                self.store.fail(claim, error="input_changed", retry_at=now)
                return
            value = make_gateway_input(
                character_id=key.character_id,
                materials=tuple(
                    (v.material.turn, v.decision.text) for v in window.turns if v.decision.text
                ),
                non_sensitive_labels=self.non_sensitive_labels,
            )
            if operation == "state":
                state = None
                if value.turns and window.session.ended_at is None:
                    proposal = (
                        self.gateway.update_state_cloud(value)
                        if isinstance(value, CloudInput)
                        else self.gateway.update_state_local(value)
                    )
                    if not isinstance(proposal, StateProposal):
                        raise ValueError("Invalid state proposal")
                    proposal = replace(proposal)
                    state = CharacterState(
                        character_id=key.character_id,
                        session_id=key.session_id,
                        surface=key.surface,
                        mood=proposal.mood,
                        relationship_temperature=proposal.relationship_temperature,
                        current_topic=proposal.current_topic,
                        updated_at=now,
                        expires_at=now + self.state_ttl,
                        input_signature=window.snapshot.signature,
                        dependencies=window.snapshot.dependencies,
                    )
                complete = self.store.complete_state(
                    claim, state, snapshot=window.snapshot, now=self.clock()
                )
            else:
                episodes: tuple[Episode, ...] = ()
                if value.turns:
                    proposal_episodes = (
                        self.gateway.split_episodes_cloud(value)
                        if isinstance(value, CloudInput)
                        else self.gateway.split_episodes_local(value)
                    )
                    episodes = build_episodes(
                        proposal_episodes, window, now=now, id_factory=self.episode_id_factory
                    )
                complete = self.store.complete_episodes(
                    claim, episodes, snapshot=window.snapshot, now=self.clock()
                )
            if not complete:
                self.store.fail(claim, error="input_changed", retry_at=self.clock())
        except (ValueError, EvidenceConflict):
            self.store.fail(claim, error="invalid_output", retry_at=None)
        except Exception:
            self.store.fail(
                claim, error="dependency_unavailable", retry_at=self.clock() + timedelta(seconds=30)
            )
