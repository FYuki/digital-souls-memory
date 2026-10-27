from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from digital_souls_memory.evidence.materials import ReceiptState, material_decision
from digital_souls_memory.memory.extraction import CloudInput, LocalOnly
from digital_souls_memory.recall.domain import CandidateMaterial, SourceStamp
from tests.context_gateway_support import EpisodeGateway
from tests.context_store_support import FakeContextStore
from tests.evidence_support import INSTRUCTION, RECORDED, session, turn

if TYPE_CHECKING:
    from digital_souls_memory.resident.domain import ResolvedWindow
    from digital_souls_memory.resident.worker import ContextWorker

pytestmark = pytest.mark.ut


def resolved_window(*, sensitivity: str = "synthetic-public") -> "ResolvedWindow":
    from digital_souls_memory.resident.domain import (
        ContextKey,
        ContextSnapshot,
        ResolvedContextTurn,
        ResolvedWindow,
    )

    source_session = replace(session(reports_delivery=False), ended_at=None)
    key = ContextKey(character_id="char-b", session_id="session-1", surface="synthetic-chat")
    turns = []
    stamps = []
    for index, text in enumerate(("挨拶🙂!", "予定表"), start=1):
        source = replace(
            turn(f"turn-{index}", text=text, sensitivity=sensitivity),
            occurred_at=RECORDED + timedelta(seconds=index),
        )
        decision = material_decision(
            character_id=key.character_id,
            session=source_session,
            turn=source,
            actor_is_character=True,
            receipts=(),
            now=RECORDED,
            delivery_timeout=timedelta(minutes=10),
        )
        turns.append(
            ResolvedContextTurn(
                material=CandidateMaterial(
                    turn=source,
                    session=source_session,
                    actor_is_character=True,
                    receipt_state=ReceiptState(None, 0),
                ),
                decision=decision,
            )
        )
        stamps.append(
            SourceStamp(
                turn_id=source.turn_id,
                character_id=key.character_id,
                basis=decision.basis,
                receipt_version=0,
                available_end=len(text),
                used_end=len(text),
                deadline=decision.deadline,
            )
        )
    return ResolvedWindow(
        key=key,
        session=source_session,
        turns=tuple(turns),
        snapshot=ContextSnapshot(
            key=key,
            notification_version=1,
            signature="synthetic-input-1",
            dependencies=tuple(stamps),
            session_ended_at=None,
            next_due_at=None,
            delivery_timeout=timedelta(minutes=10),
        ),
    )


def worker_for(
    store: FakeContextStore,
    gateway: EpisodeGateway,
    ids: tuple[str, ...],
    clock: Callable[[], datetime] | None = None,
) -> "ContextWorker":
    from digital_souls_memory.resident.worker import ContextWorker

    sequence = iter(ids)
    return ContextWorker(
        store=store,
        gateway=gateway,
        clock=clock if clock is not None else lambda: RECORDED,
        non_sensitive_labels=frozenset({"synthetic-public"}),
        state_ttl=timedelta(minutes=30),
        episode_id_factory=lambda: next(sequence),
    )


def test_episode_batch_has_distinct_ids_grounded_spans_times_and_generation_metadata() -> None:
    window = resolved_window()
    store, gateway = FakeContextStore(window), EpisodeGateway()
    worker_for(store, gateway, ("episode-1", "episode-2")).process_episodes(window.key)
    values = store.list_episodes(window.key)
    assert [(v.episode_id, v.topic) for v in values] == [
        ("episode-1", "挨拶🙂!"),
        ("episode-2", "予定表"),
    ]
    for value, source in zip(values, window.turns, strict=True):
        (span,) = value.turn_span
        assert (span.turn_id, span.start, span.end) == (
            source.material.turn.turn_id,
            0,
            len(source.material.turn.text),
        )
        assert value.time_range == (source.material.turn.occurred_at,) * 2
        assert (value.model, value.prompt_version, value.created_at) == (
            "synthetic-model",
            "synthetic-v1",
            RECORDED,
        )
        assert (value.character_id, value.session_id, value.surface) == (
            window.key.character_id,
            window.key.session_id,
            window.key.surface,
        )
        assert value.dependencies == window.snapshot.dependencies
        assert value.input_signature == window.snapshot.signature
    work = store.get_work("episodes", window.key)
    assert work is not None and work.status == "done"
    assert work.completed_signature == window.snapshot.signature
    assert work.token is None and work.lease_until is None


def test_episode_time_range_uses_earliest_and_latest_source_times_even_for_reversed_spans() -> None:
    from digital_souls_memory.episodes.domain import EpisodePart, EpisodeProposal, TurnSpan

    window = resolved_window()
    store, gateway = FakeContextStore(window), EpisodeGateway()
    gateway.proposal = EpisodeProposal(
        model="synthetic",
        prompt_version="v1",
        episodes=(
            EpisodePart(
                topic="まとめ",
                turn_span=(
                    TurnSpan(turn_id="turn-2", start=0, end=3),
                    TurnSpan(turn_id="turn-1", start=0, end=4),
                ),
            ),
        ),
    )
    worker_for(store, gateway, ("episode-1",)).process_episodes(window.key)
    (value,) = store.list_episodes(window.key)
    assert value.time_range == (
        window.turns[0].material.turn.occurred_at,
        window.turns[1].material.turn.occurred_at,
    )


@pytest.mark.parametrize("ids", [("episode-2", "episode-1"), ("episode-2", "episode-2")])
def test_episode_id_collision_preserves_previous_batch_without_partial_insert(
    ids: tuple[str, ...],
) -> None:
    window = resolved_window()
    store, gateway = FakeContextStore(window), EpisodeGateway()
    worker_for(store, gateway, ("episode-1", "episode-old")).process_episodes(window.key)
    before = store.list_episodes(window.key)
    assert before
    store.window = replace(window, snapshot=replace(window.snapshot, signature="new-input"))
    worker_for(store, gateway, ids).process_episodes(window.key)
    assert store.list_episodes(window.key) == before
    work = store.get_work("episodes", window.key)
    assert work is not None and (work.status, work.error) == ("failed", "invalid_output")
    assert work.token is None and work.lease_until is None


@pytest.mark.parametrize("turn_id,end", [("missing", 1), ("other-session", 1), ("turn-1", 5)])
def test_episode_proposal_outside_model_input_is_rejected_as_a_whole(
    turn_id: str,
    end: int,
) -> None:
    from digital_souls_memory.episodes.domain import EpisodePart, EpisodeProposal, TurnSpan

    window = resolved_window()
    store, gateway = FakeContextStore(window), EpisodeGateway()
    gateway.proposal = EpisodeProposal(
        model="synthetic",
        prompt_version="v1",
        episodes=(
            EpisodePart(topic="valid", turn_span=(TurnSpan(turn_id="turn-2", start=0, end=3),)),
            EpisodePart(topic="invalid", turn_span=(TurnSpan(turn_id=turn_id, start=0, end=end),)),
        ),
    )
    worker = worker_for(store, gateway, ("episode-1", "episode-2"))
    worker.process_episodes(window.key)
    assert store.list_episodes(window.key) == ()
    work = store.get_work("episodes", window.key)
    assert work is not None and (work.status, work.error) == ("failed", "invalid_output")
    count = len(gateway.episode_calls)
    worker.process_episodes(window.key)
    assert len(gateway.episode_calls) == count


def test_episode_dependency_failure_releases_claim_and_retries_without_blocking_state() -> None:
    window = resolved_window()
    store, gateway = FakeContextStore(window), EpisodeGateway()
    now = [RECORDED]
    gateway.fail = True
    worker = worker_for(store, gateway, ("episode-1", "episode-2"), lambda: now[0])
    worker.process_episodes(window.key)
    failed = store.get_work("episodes", window.key)
    assert failed is not None
    assert (failed.status, failed.error) == ("retry", "dependency_unavailable")
    assert failed.token is None and failed.lease_until is None
    assert store.list_episodes(window.key) == ()
    worker.process_state(window.key)
    assert store.get_state(window.key) is not None
    assert store.get_work("episodes", window.key) == failed
    assert failed.retry_at is not None
    now[0] = failed.retry_at
    gateway.fail = False
    worker.process_episodes(window.key)
    assert len(store.list_episodes(window.key)) == 2
    completed = store.get_work("episodes", window.key)
    assert completed is not None and completed.status == "done"


@pytest.mark.parametrize("operation", ["state", "episodes"])
def test_input_change_during_model_call_cannot_commit_stale_results(operation: str) -> None:
    window = resolved_window()
    store, gateway = FakeContextStore(window), EpisodeGateway()
    worker = worker_for(store, gateway, ("episode-1", "episode-2"))
    started = gateway.started if operation == "state" else gateway.episode_started
    release = gateway.release if operation == "state" else gateway.episode_release
    release.clear()
    process = worker.process_state if operation == "state" else worker.process_episodes
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(process, window.key)
        try:
            assert started.wait(2)
            store.window = replace(window, snapshot=replace(window.snapshot, signature="corrected"))
        finally:
            release.set()
        pending.result(timeout=5)
    assert store.get_state(window.key) is None
    assert store.list_episodes(window.key) == ()
    work = store.get_work("state" if operation == "state" else "episodes", window.key)
    assert work is not None and work.status == "retry"
    assert work.token is None and work.lease_until is None


@pytest.mark.parametrize(
    "label,route",
    [
        ("synthetic-public", "cloud"),
        ("synthetic-sensitive", "local"),
        ("unclassified", "local"),
        ("mixed", "local"),
    ],
)
def test_state_and_episode_routes_preserve_original_text_and_language(
    label: str, route: str
) -> None:
    window = resolved_window(sensitivity=label)
    if label == "mixed":
        first, second = window.turns
        first = replace(
            first,
            material=replace(
                first.material, turn=replace(first.material.turn, sensitivity="synthetic-public")
            ),
        )
        second = replace(
            second,
            material=replace(
                second.material,
                turn=replace(second.material.turn, sensitivity="synthetic-sensitive"),
            ),
        )
        window = replace(window, turns=(first, second))
    store, gateway = FakeContextStore(window), EpisodeGateway()
    worker = worker_for(store, gateway, ("episode-1", "episode-2"))
    worker.process_state(window.key)
    worker.process_episodes(window.key)
    for actual_route, value in (*gateway.calls, *gateway.episode_calls):
        assert actual_route == route
        assert isinstance(value, CloudInput if route == "cloud" else LocalOnly)
        assert [(v.turn_id, v.text, v.language) for v in value.turns] == [
            (v.material.turn.turn_id, v.decision.text, v.material.turn.language)
            for v in window.turns
        ]
    assert len(gateway.calls) == len(gateway.episode_calls) == 1


def test_unchanged_input_does_not_extend_state_expiry_or_regenerate_episodes() -> None:
    window = resolved_window()
    store, gateway = FakeContextStore(window), EpisodeGateway()
    now = [RECORDED]
    worker = worker_for(store, gateway, ("episode-1", "episode-2"), lambda: now[0])
    worker.process_state(window.key)
    worker.process_episodes(window.key)
    state, episodes = store.get_state(window.key), store.list_episodes(window.key)
    assert state is not None and episodes
    now[0] += timedelta(minutes=1)
    worker.process_state(window.key)
    worker.process_episodes(window.key)
    assert store.get_state(window.key) == state
    assert store.list_episodes(window.key) == episodes
    assert len(gateway.calls) == len(gateway.episode_calls) == 1


def test_instruction_topic_is_retained_as_episode_data() -> None:
    from digital_souls_memory.episodes.domain import EpisodePart, EpisodeProposal, TurnSpan

    window = resolved_window()
    store, gateway = FakeContextStore(window), EpisodeGateway()
    gateway.proposal = EpisodeProposal(
        model="synthetic",
        prompt_version="v1",
        episodes=(
            EpisodePart(topic=INSTRUCTION, turn_span=(TurnSpan(turn_id="turn-1", start=0, end=4),)),
        ),
    )
    worker_for(store, gateway, ("episode-1",)).process_episodes(window.key)
    (value,) = store.list_episodes(window.key)
    assert value.topic == INSTRUCTION
    assert value.character_id == window.key.character_id


def test_no_available_text_skips_models_and_preserves_the_pending_deadline() -> None:
    window = resolved_window()
    deadline = RECORDED + timedelta(minutes=10)
    window = replace(
        window, turns=(), snapshot=replace(window.snapshot, dependencies=(), next_due_at=deadline)
    )
    store, gateway = FakeContextStore(window), EpisodeGateway()
    worker = worker_for(store, gateway, ())
    worker.process_state(window.key)
    worker.process_episodes(window.key)
    assert gateway.calls == gateway.episode_calls == []
    assert store.get_state(window.key) is None
    assert store.list_episodes(window.key) == ()
    for operation in ("state", "episodes"):
        work = store.get_work("state" if operation == "state" else "episodes", window.key)
        assert work is not None and work.next_due_at == deadline
        assert work.token is None and work.lease_until is None


def test_worker_close_releases_inflight_claim_without_leaving_running_work() -> None:
    window = resolved_window()
    store, gateway = FakeContextStore(window), EpisodeGateway()
    gateway.episode_release.clear()
    worker = worker_for(store, gateway, ("episode-1", "episode-2"))
    closed = False
    worker.start()
    try:
        assert gateway.episode_started.wait(2)
        with ThreadPoolExecutor(max_workers=1) as pool:
            closing = pool.submit(worker.close)
            try:
                gateway.episode_release.set()
                closing.result(timeout=5)
                closed = True
            finally:
                gateway.episode_release.set()
        for operation in ("state", "episodes"):
            work = store.get_work("state" if operation == "state" else "episodes", window.key)
            if work is not None:
                assert work.status != "running"
                assert work.token is None and work.lease_until is None
    finally:
        gateway.release.set()
        gateway.episode_release.set()
        if not closed:
            worker.close()
