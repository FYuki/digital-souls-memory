"""Real HTTP orchestration/recalculation against shared synthetic port stores."""

import os
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from digital_souls_memory.api import create_app
from digital_souls_memory.evidence.provision import ProvisionRecord, SourceSpan
from tests.deletion_support import RecordingRecalculationGateway, SharedDeletionStore
from tests.evidence_support import RECORDED, session, turn
from tests.it1.evidence_fakes import FakeEvidenceStore, registered_identities
from tests.memory_fakes import FakeMemoryStore
from tests.memory_support import NEW_ID, memory
from tests.recall_support import FakeProvisionStore

pytestmark = pytest.mark.it1
HEADERS = {"Authorization": "Bearer synthetic-test-credential"}


@pytest.fixture(autouse=True)
def isolate_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in tuple(os.environ):
        if key.startswith("MEMORY_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("MEMORY_AUTHOR_ID", "synthetic-author")
    monkeypatch.setenv("MEMORY_AUTHOR_TOKEN", "synthetic-test-credential")


@pytest.mark.parametrize("failure", [None, "gateway", "commit"])
def test_http_deletes_selected_sources_recalculates_and_invalidates_saved_provisions(
    failure: str | None,
) -> None:
    evidence = FakeEvidenceStore()
    evidence.add_session(session(reports_delivery=False))
    sources = (
        turn("turn-1", actor="U", kind="observation", text="消す合成情報"),
        turn("addressed", actor="V", addressees=("U",), address_mode="direct"),
        turn("both", actor="U", addressees=("U",), address_mode="direct"),
        turn("turn-2", actor="V", text="残る合成情報"),
        turn("reply", actor="V", reply_to_turn_id="turn-1"),
        turn("literal", actor="V", text='{"actor":"U"}'),
    )
    for source in sources:
        evidence.add_turn(source)
    memories = FakeMemoryStore(evidence)
    original = replace(
        memory("summary"), source_turn_ids=("turn-1", "turn-2"), content="消す合成情報を含む旧要約"
    )
    single = replace(memory("summary"), id=NEW_ID)
    for value in (original, single):
        with memories.transaction("char-b") as tx:
            tx.add_memory(value)
    provisions = FakeProvisionStore(lambda _: True)
    for kind in ("turn", "resident"):
        record = ProvisionRecord(
            provision_id=f"provided-{kind}",
            kind=kind,
            character_id="char-b",
            session_id="session-1",
            surface="synthetic-chat",
            query=None,
            turn_id="turn-1" if kind == "turn" else None,
            memory_ids=(original.id,),
            source_spans=(
                SourceSpan(
                    turn_id="turn-1",
                    start=0,
                    end=len(sources[0].text),
                    text=sources[0].text,
                    actor="U",
                    language="ja",
                    truncated=False,
                    original_start=0,
                    original_end=len(sources[0].text),
                ),
            ),
            verbatim_anchor_ids=(),
            voice_exemplar_ids=(),
            motivation_ids=(),
            temperament_version=None,
            personality_version=None,
            scores=(),
            duration_ms=0,
            provided_at=RECORDED,
        )
        provisions.records[record.provision_id] = record
    deletion = SharedDeletionStore(evidence, memories, provisions)
    revised = replace(original, source_turn_ids=("turn-2",), content="残る合成情報の新要約")
    gateway = RecordingRecalculationGateway(revised)
    if failure == "gateway":
        gateway.failure = RuntimeError("消す合成情報: synthetic-test-credential")
    deletion.fail_commit = failure == "commit"
    identity = registered_identities()
    identity_before = dict(identity.cores), dict(identity.temperaments), dict(identity.profiles)
    with TestClient(
        create_app(
            store=evidence,
            memory_store=memories,
            provision_store=provisions,
            identity_store=identity,
            deletion_store=deletion,
            recalculation_gateway=gateway,
            clock=lambda: RECORDED,
        )
    ) as client:
        before = client.get("/characters/char-b/memories")
        assert before.status_code == 200
        assert {v["id"] for v in before.json()} == {original.id, single.id}
        request = {"request_id": "delete-1", "user_id": "U"}
        denied = client.post("/deletion-requests", json=request)
        assert denied.status_code == 401
        assert deletion.requests == {} and gateway.calls == []
        assert evidence.get_turn("turn-1") == sources[0]
        response = client.post("/deletion-requests", headers=HEADERS, json=request)
        if failure is not None:
            assert response.status_code == 503
            assert evidence.get_turn("turn-1") == sources[0]
            assert memories.get_memory("char-b", original.id) == original
            assert memories.get_memory("char-b", single.id) == single
            assert all(provisions.get(f"provided-{kind}") for kind in ("turn", "resident"))
            audit = client.get("/deletion-requests/delete-1", headers=HEADERS)
            assert audit.status_code == 200
            assert audit.json()["status"] == "failed"
            assert audit.json()["cascade_result"] is None
            assert audit.json()["error"]
            assert "消す合成情報" not in audit.json()["error"]
            assert "synthetic-test-credential" not in audit.json()["error"]
            gateway.failure = None
            response = client.post("/deletion-requests", headers=HEADERS, json=request)
        assert response.status_code == 200
        result = response.json()
        assert result["status"] == "done"
        assert set(result["target_turn_ids"]) == {"turn-1", "addressed", "both"}
        assert len(result["target_turn_ids"]) == 3
        cascade = result["cascade_result"]
        assert set(cascade["deleted_turn_ids"]) == {"turn-1", "addressed", "both"}
        assert cascade["recalculated_memories"] == [
            {"character_id": "char-b", "memory_id": original.id}
        ]
        assert cascade["invalidated_memories"] == [
            {"character_id": "char-b", "memory_id": single.id}
        ]
        assert set(cascade["invalidated_provision_ids"]) == {"provided-turn", "provided-resident"}
        assert client.get("/turns/turn-1").status_code == 404
        assert evidence.get_turn("turn-2") == sources[3]
        assert evidence.get_turn("reply") == replace(sources[4], reply_to_turn_id=None)
        assert evidence.get_turn("literal") == sources[5]
        assert client.get("/characters/char-b/memories").json() == [
            dict(
                next(v for v in before.json() if v["id"] == original.id),
                content=revised.content,
                source_turn_ids=["turn-2"],
                epistemics=dict(
                    next(v for v in before.json() if v["id"] == original.id)["epistemics"],
                    independent_evidence_count=1,
                ),
            )
        ]
        assert all(provisions.get(f"provided-{kind}") is None for kind in ("turn", "resident"))
        evidence.add_turn(turn("later", actor="U"))
        assert client.post("/deletion-requests", headers=HEADERS, json=request).json() == result
        assert evidence.get_turn("later") is not None
        assert client.get("/deletion-requests/delete-1", headers=HEADERS).json() == result
    assert gateway.calls
    for model_input, identifier, _ in gateway.calls:
        assert [(item.turn_id, item.text) for item in model_input.turns] == [
            ("turn-2", sources[3].text)
        ]
        assert identifier == original.id
    assert (identity.cores, identity.temperaments, identity.profiles) == identity_before


@pytest.mark.parametrize(
    "actor,perceived,addressees,mode,available",
    [
        ("char-c", ("char-b", "char-c"), (), "broadcast", True),
        ("char-c", ("char-c",), (), "broadcast", False),
        ("user-a", (), (), "broadcast", False),
        ("char-b", ("char-b",), (), "self", True),
        ("char-c", ("char-b", "char-c"), ("char-b",), "direct", True),
        ("char-c", ("char-c",), ("user-a",), "direct", False),
        ("char-c", ("char-c",), ("char-b",), "direct", False),
        ("char-b", ("char-b",), ("user-a",), "direct", True),
    ],
)
def test_http_deletion_does_not_expand_memory_perception_from_addresses(
    actor: str,
    perceived: tuple[str, ...],
    addressees: tuple[str, ...],
    mode: str,
    available: bool,
) -> None:
    evidence = FakeEvidenceStore()
    evidence.add_session(session(reports_delivery=False))
    evidence.add_turn(turn("turn-1"))
    remaining = turn(
        "turn-2", actor=actor, perceived_by=perceived, addressees=addressees, address_mode=mode
    )
    evidence.add_turn(remaining)
    memories = FakeMemoryStore(evidence)
    # Initial memory sources also obey perception; inaccessible evidence is never seeded.
    original = replace(
        memory("summary"), source_turn_ids=("turn-1", "turn-2") if available else ("turn-1",)
    )
    with memories.transaction("char-b") as tx:
        tx.add_memory(original)
    output = replace(original, source_turn_ids=("turn-2",), content="知覚した残存根拠の要約")
    gateway = RecordingRecalculationGateway(output)
    provisions = FakeProvisionStore(lambda _: True)
    with TestClient(
        create_app(
            store=evidence,
            memory_store=memories,
            identity_store=registered_identities(),
            deletion_store=SharedDeletionStore(evidence, memories, provisions),
            recalculation_gateway=gateway,
            clock=lambda: RECORDED,
        )
    ) as client:
        assert len(client.get("/characters/char-b/memories").json()) == 1
        assert (
            client.post(
                "/deletion-requests",
                headers=HEADERS,
                json={"request_id": "delete-perception", "target_turn_ids": ["turn-1"]},
            ).status_code
            == 200
        )
        response = client.get("/characters/char-b/memories")
        assert response.status_code == 200
        if available:
            assert [(v["content"], v["source_turn_ids"]) for v in response.json()] == [
                (output.content, ["turn-2"])
            ]
            assert gateway.calls
            assert all(
                [t.turn_id for t in value.turns] == ["turn-2"] for value, _, _ in gateway.calls
            )
        else:
            assert response.json() == []
            assert gateway.calls == []
        assert client.get("/characters/char-z/memories").json() == []
        assert evidence.get_turn("turn-2") == remaining
