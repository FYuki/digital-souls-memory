from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from digital_souls_memory.api import create_app
from digital_souls_memory.identity.domain import PersonalityProfile, Temperament
from digital_souls_memory.memory.extraction import CloudInput, LocalOnly
from tests.context_gateway_support import EpisodeGateway, eventually
from tests.context_store_support import EvidenceContextStore, FakeContextStore
from tests.evidence_support import INSTRUCTION, RECORDED, STARTED, TEXT, session, turn_payload
from tests.identity_support import aspects
from tests.it1.evidence_fakes import FakeEvidenceStore, registered_identities
from tests.ut.test_context_worker import resolved_window, worker_for

pytestmark = pytest.mark.it1
STATE = "/characters/char-b/sessions/session-1/state"
EPISODES = "/characters/char-b/sessions/session-1/episodes"
PARAMS = {"surface": "synthetic-chat"}


@pytest.mark.parametrize("expiry", [False, True], ids=["ending", "ttl"])
def test_fake_context_http_reads_worker_results_and_invalidates_state_in_the_same_app(
    monkeypatch: pytest.MonkeyPatch,
    expiry: bool,
) -> None:
    for name in ("MEMORY_DATABASE_URL", "MEMORY_EMBEDDING_ENDPOINT", "MEMORY_RERANKER_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    window = resolved_window()
    store, gateway = FakeContextStore(window), EpisodeGateway()
    evidence = FakeEvidenceStore()
    evidence.add_session(window.session)
    for value in window.turns:
        evidence.add_turn(value.material.turn)
    identities = registered_identities()
    worker_for(store, gateway, ("episode-1", "episode-2")).process_state(window.key)
    worker_for(store, gateway, ("episode-1", "episode-2")).process_episodes(window.key)
    now = [RECORDED]
    with TestClient(
        create_app(
            store=evidence, identity_store=identities, context_store=store, clock=lambda: now[0]
        )
    ) as client:
        response = client.get(STATE, params=PARAMS)
        assert response.is_success
        state = response.json()["state"]
        assert state["current_topic"] == "挨拶🙂!予定表"
        assert set(state) == {
            "mood",
            "relationship_temperature",
            "current_topic",
            "character_id",
            "session_id",
            "surface",
            "updated_at",
            "expires_at",
            "input_signature",
            "dependencies",
        }
        assert state["mood"] == "穏やか" and state["relationship_temperature"] is None
        assert (state["character_id"], state["session_id"], state["surface"]) == (
            "char-b",
            "session-1",
            "synthetic-chat",
        )
        saved = store.get_state(window.key)
        assert saved is not None
        assert datetime.fromisoformat(state["updated_at"]) == saved.updated_at
        assert datetime.fromisoformat(state["expires_at"]) == saved.expires_at
        assert state["input_signature"] == saved.input_signature
        assert len(state["dependencies"]) == len(saved.dependencies)
        for actual, dependency in zip(state["dependencies"], saved.dependencies, strict=True):
            assert set(actual) == {
                "turn_id",
                "character_id",
                "basis",
                "receipt_version",
                "available_end",
                "used_end",
                "deadline",
            }
            assert (
                actual["turn_id"],
                actual["character_id"],
                actual["basis"],
                actual["receipt_version"],
                actual["available_end"],
                actual["used_end"],
            ) == (
                dependency.turn_id,
                dependency.character_id,
                dependency.basis,
                dependency.receipt_version,
                dependency.available_end,
                dependency.used_end,
            )
            assert datetime.fromisoformat(actual["deadline"]) == dependency.deadline
        values = client.get(EPISODES, params=PARAMS).json()["episodes"]
        assert len(values) == 2
        for value, episode in zip(values, store.list_episodes(window.key), strict=True):
            assert set(value) == {
                "episode_id",
                "character_id",
                "session_id",
                "surface",
                "topic",
                "turn_span",
                "time_range",
                "model",
                "prompt_version",
                "created_at",
                "input_signature",
                "dependencies",
                "source_spans",
            }
            assert (
                value["episode_id"],
                value["topic"],
                value["model"],
                value["prompt_version"],
                value["input_signature"],
            ) == (
                episode.episode_id,
                episode.topic,
                episode.model,
                episode.prompt_version,
                episode.input_signature,
            )
            assert (
                tuple(datetime.fromisoformat(v) for v in value["time_range"]) == episode.time_range
            )
            assert datetime.fromisoformat(value["created_at"]) == episode.created_at
            for span in value["source_spans"]:
                assert set(span) == {"turn_id", "start", "end", "text", "actor", "language"}
        for path, params in (
            (STATE, {"surface": "another-surface"}),
            ("/characters/char-z/sessions/session-1/state", PARAMS),
        ):
            result = client.get(path, params=params)
            assert result.is_success and result.json()["state"] is None
        if expiry:
            now[0] = datetime.fromisoformat(state["expires_at"]) - timedelta(microseconds=1)
            assert client.get(STATE, params=PARAMS).json()["state"] is not None
            now[0] += timedelta(microseconds=1)
        else:
            assert (
                client.post(
                    "/sessions/session-1/endings",
                    json={"ended_at": now[0].isoformat(), "reported_by": "synthetic-window"},
                ).status_code
                == 201
            )
        assert client.get(STATE, params=PARAMS).json() == {"state": None}


def test_fake_context_background_processing_does_not_write_identity_or_author_seed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("MEMORY_DATABASE_URL", "MEMORY_EMBEDDING_ENDPOINT", "MEMORY_RERANKER_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    window = resolved_window(sensitivity="synthetic-sensitive")
    first = window.turns[0]
    source = replace(first.material.turn, text=INSTRUCTION)
    first = replace(
        first,
        material=replace(first.material, turn=source),
        decision=replace(first.decision, text=INSTRUCTION),
    )
    window = replace(
        window,
        turns=(first,),
        snapshot=replace(
            window.snapshot,
            dependencies=(
                replace(
                    window.snapshot.dependencies[0],
                    available_end=len(INSTRUCTION),
                    used_end=len(INSTRUCTION),
                ),
            ),
        ),
    )
    store, gateway = FakeContextStore(window), EpisodeGateway()
    evidence = FakeEvidenceStore()
    evidence.add_session(window.session)
    evidence.add_turn(source)
    identities = registered_identities()
    identities.temperaments[("char-b", 1)] = Temperament("char-b", "合成気質", 1, STARTED)
    identities.profiles[("char-b", 1)] = PersonalityProfile(
        character_id="char-b",
        aspects=aspects(),
        description="合成人格",
        renderer_version="synthetic-v1",
        temperament_version=1,
        version=1,
        valid_from=STARTED,
        valid_to=None,
        evidence_belief_ids=(),
        change_kind="author_seed",
    )
    before = (identities.cores.copy(), identities.temperaments.copy(), identities.profiles.copy())
    identifiers = iter(("episode-1",))
    app = create_app(
        store=evidence,
        identity_store=identities,
        context_store=store,
        context_gateway=gateway,
        clock=lambda: RECORDED,
        episode_id_factory=lambda: next(identifiers),
    )
    with TestClient(app) as client:
        try:

            def completed() -> bool:
                state = store.get_work("state", window.key)
                episodes = store.get_work("episodes", window.key)
                return bool(state and episodes and state.status == episodes.status == "done")

            eventually(completed)
            assert client.get(STATE, params=PARAMS).json()["state"]["current_topic"] == INSTRUCTION
            values = client.get(EPISODES, params=PARAMS).json()["episodes"]
            assert [v["topic"] for v in values] == [INSTRUCTION]
            assert (identities.cores, identities.temperaments, identities.profiles) == before
            assert evidence.turns[source.turn_id] == source
        finally:
            gateway.release.set()
            gateway.episode_release.set()


@pytest.mark.parametrize(
    "labels",
    [
        ("synthetic-public",),
        ("synthetic-sensitive",),
        ("synthetic-public", "synthetic-sensitive"),
    ],
)
def test_fake_http_episode_registration_is_async_and_routes_complete_input(
    monkeypatch: pytest.MonkeyPatch,
    labels: tuple[str, ...],
) -> None:
    from digital_souls_memory.resident.domain import ContextKey

    for name in ("MEMORY_DATABASE_URL", "MEMORY_EMBEDDING_ENDPOINT", "MEMORY_RERANKER_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    evidence = FakeEvidenceStore()
    evidence.add_session(replace(session(reports_delivery=False), ended_at=None))
    key = ContextKey(character_id="char-b", session_id="session-1", surface="synthetic-chat")
    store, gateway = EvidenceContextStore(evidence, key, RECORDED), EpisodeGateway()
    gateway.episode_release.clear()
    with TestClient(
        create_app(
            store=evidence,
            identity_store=registered_identities(),
            context_store=store,
            context_gateway=gateway,
            clock=lambda: RECORDED,
            non_sensitive_labels=frozenset({"synthetic-public"}),
        )
    ) as client:
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                for index, label in enumerate(labels, start=1):
                    pending = pool.submit(
                        client.post,
                        "/turns",
                        json=turn_payload()
                        | {
                            "turn_id": f"turn-{index}",
                            "sensitivity": label,
                        },
                    )
                    assert pending.result(timeout=2).status_code == 201
                assert gateway.episode_started.wait(2)
                assert client.get(EPISODES, params=PARAMS).json() == {"episodes": []}
                gateway.episode_release.set()
            expected = {f"turn-{i}" for i in range(1, len(labels) + 1)}

            def current_batch() -> bool:
                values = client.get(EPISODES, params=PARAMS).json()["episodes"]
                return {s["turn_id"] for v in values for s in v["turn_span"]} == expected

            eventually(current_batch)
            with gateway.lock:
                calls = tuple(gateway.episode_calls)
            complete = [
                (route, value)
                for route, value in calls
                if {t.turn_id for t in value.turns} == expected
            ]
            assert complete
            route = "cloud" if set(labels) == {"synthetic-public"} else "local"
            for actual, value in complete:
                assert actual == route
                assert isinstance(value, CloudInput if route == "cloud" else LocalOnly)
                assert [(t.text, t.language) for t in value.turns] == [(TEXT, "ja")] * len(labels)
            # No cloud call may include even one sensitive source during intermediate scans.
            sensitive = {
                f"turn-{i}" for i, label in enumerate(labels, 1) if label == "synthetic-sensitive"
            }
            for actual, value in calls:
                if actual == "cloud":
                    assert {t.turn_id for t in value.turns}.isdisjoint(sensitive)
            assert all(evidence.turns[t].text == TEXT for t in expected)
        finally:
            gateway.release.set()
            gateway.episode_release.set()


def test_fake_http_rejects_entire_invalid_episode_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from digital_souls_memory.episodes.domain import EpisodePart, EpisodeProposal, TurnSpan
    from digital_souls_memory.resident.domain import ContextKey

    for name in ("MEMORY_DATABASE_URL", "MEMORY_EMBEDDING_ENDPOINT", "MEMORY_RERANKER_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    evidence = FakeEvidenceStore()
    evidence.add_session(replace(session(reports_delivery=False), ended_at=None))
    key = ContextKey(character_id="char-b", session_id="session-1", surface="synthetic-chat")
    store, gateway = EvidenceContextStore(evidence, key, RECORDED), EpisodeGateway()
    gateway.proposal = EpisodeProposal(
        model="synthetic",
        prompt_version="v1",
        episodes=(
            EpisodePart(topic="valid", turn_span=(TurnSpan(turn_id="turn-1", start=0, end=1),)),
            EpisodePart(topic="invalid", turn_span=(TurnSpan(turn_id="missing", start=0, end=1),)),
        ),
    )
    with TestClient(
        create_app(
            store=evidence,
            identity_store=registered_identities(),
            context_store=store,
            context_gateway=gateway,
            clock=lambda: RECORDED,
        )
    ) as client:
        try:
            assert client.post("/turns", json=turn_payload()).status_code == 201

            def failed() -> bool:
                work = store.get_work("episodes", key)
                return work is not None and work.status == "failed"

            eventually(failed)
            assert client.get(EPISODES, params=PARAMS).json() == {"episodes": []}
            assert store.list_episodes(key) == ()
            assert evidence.turns["turn-1"].text == TEXT
        finally:
            gateway.release.set()
            gateway.episode_release.set()
