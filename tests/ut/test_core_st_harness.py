"""Fixed performance conditions and synthetic model proposals have explicit contracts."""

from datetime import timedelta
from pathlib import Path

import pytest

from digital_souls_memory.memory.extraction import LocalOnly, LocalTurn
from digital_souls_memory.recall.settings import EMBEDDING_MODEL, RERANKER_MODEL
from tests.evidence_support import RECORDED
from tests.st.app import TestClock as ScenarioClock
from tests.st.fakes import ScenarioGateway
from tests.st.performance import Manifest
from tests.st.performance import run as run_performance

pytestmark = pytest.mark.ut


def manifest_payload() -> dict[str, object]:
    return {
        "revision": "a" * 40,
        "environment": "synthetic Ubuntu",
        "hardware": "synthetic CPU/RAM",
        "db_image": "synthetic-image@sha256:a",
        "client_image": "synthetic-client@sha256:b",
        "db_version": "18.6",
        "embedding": EMBEDDING_MODEL,
        "reranker": RERANKER_MODEL,
        "dimensions": 1024,
        "data_counts": [100, 1000, 10000],
        "input_text": "日本語",
        "input_length": 3,
        "candidate_limit": 200,
        "rerank_limit": 20,
        "result_limit": 5,
        "token_limit": 1500,
        "relevance_threshold": -2,
        "repetitions": 100,
        "warm_up": 5,
        "concurrency": 1,
        "cold_condition": "new database, loaded models",
        "period": "synthetic period",
        "aggregation": "nearest-rank",
    }


def test_manifest_accepts_frozen_approved_conditions() -> None:
    manifest = Manifest.model_validate(manifest_payload())
    assert manifest.data_counts == (100, 1000, 10000)
    assert manifest.model_dump(mode="json")["embedding"] == EMBEDDING_MODEL


@pytest.mark.parametrize(
    "field,value",
    [
        ("hardware", ""),
        ("embedding", {}),
        ("reranker", {}),
        ("dimensions", 512),
        ("data_counts", []),
        ("data_counts", [0]),
        ("data_counts", [100, 100]),
        ("input_length", 99),
        ("repetitions", 99),
        ("concurrency", 0),
        ("aggregation", "mean"),
    ],
)
def test_manifest_refuses_unfixed_or_changed_conditions(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        Manifest.model_validate(manifest_payload() | {field: value})


def test_clock_keeps_one_instance_across_deadline() -> None:
    clock = ScenarioClock()
    assert clock() == RECORDED
    assert clock.advance(599) == RECORDED + timedelta(seconds=599)
    assert clock.advance(1) == RECORDED + timedelta(minutes=10)
    assert clock() == RECORDED + timedelta(minutes=10)


def test_synthetic_gateway_submits_forbidden_candidates_and_recalculates_remaining_text() -> None:
    gateway = ScenarioGateway()
    turn = LocalTurn(
        turn_id="original",
        text="逐語🙂",
        actor="user-a",
        kind="utterance",
        addressees=(),
        address_mode="self",
        reply_to_turn_id=None,
        occurred_at=RECORDED,
        language="ja",
    )
    value = LocalOnly(character_id="char-x", turns=(turn,))
    proposal = gateway.extract_local(value)
    assert {m.kind for m in proposal.memories} == {"fact", "relationship", "commitment"}
    assert any(m.epistemics.confidence_features.score() < 0.5 for m in proposal.memories)
    base = proposal.memories[0]
    assert base.content == turn.text and base.source_turn_ids == ("original",)
    recalculated = gateway.recalculate_local(value, memory_id=base.id, kind="fact")
    assert recalculated.content == turn.text
    assert recalculated.epistemics.source_type == base.epistemics.source_type
    assert recalculated.epistemics.trust_tier == base.epistemics.trust_tier


def test_performance_setup_failure_keeps_failed_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    from digital_souls_memory.client import MemoryTransportError

    def unavailable() -> None:
        raise MemoryTransportError("synthetic unavailable service")

    monkeypatch.setattr("tests.st.performance.new_client", unavailable)
    output = tmp_path / "performance.json"
    with pytest.raises(MemoryTransportError):
        run_performance(Manifest.model_validate(manifest_payload()), output)
    result = json.loads(output.read_text())
    assert result["status"] == "FAIL"
    assert result["error"] == "MemoryTransportError"
    assert result["runs"] == []
    assert result["started_at"] <= result["finished_at"]
