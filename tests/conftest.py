from collections.abc import Callable

import pytest


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Required suites may not turn skips, xfails or empty discovery into PASS."""
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    assert reporter is not None
    if session.testscollected == 0 or any(
        reporter.stats.get(status) for status in ("skipped", "xfailed", "xpassed")
    ):
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.fixture
def rejected() -> Callable[[], pytest.RaisesExc[BaseException]]:
    # A call-site failure must not count as rejection inside the validation boundary.
    def expect_rejection() -> pytest.RaisesExc[BaseException]:
        return pytest.raises(
            check=lambda error: error.__traceback__ is not None
            and error.__traceback__.tb_next is not None
        )

    return expect_rejection


@pytest.fixture
def turn() -> dict[str, object]:
    return {
        "turn_id": "t1",
        "session_id": "s1",
        "actor": "user",
        "kind": "utterance",
        "text": "約束は2026年11月3日、『青い駅』で。",
        "language": "ja",
        "audience": [],
        "sensitivity": "non_sensitive",
        "classifier_version": "synthetic-v1",
        "occurred_at": "2026-10-19T00:00:00Z",
        "recorded_at": "2026-10-19T00:00:01Z",
    }


@pytest.fixture
def memory() -> dict[str, object]:
    return {
        "id": "m1",
        "kind": "Fact",
        "content": "A meeting at the blue station in November",
        "source_turn_ids": ["t1"],
        "derivation": {"model": "synthetic", "prompt_version": "1"},
        "valid_from": "2026-10-19T00:00:00Z",
        "valid_to": None,
        "last_reinforced_at": "2026-10-19T00:00:00Z",
        "importance": 0.5,
        "self_relevance": 0.5,
        "trust": 0.5,
    }


@pytest.fixture
def case(turn: dict[str, object], memory: dict[str, object]) -> dict[str, object]:
    return {
        "id": "case-a",
        "category": "detail",
        "character_id": "character",
        "query": "What date and exact place did we agree on?",
        "as_of": "2026-10-20T00:00:00Z",
        "sessions": [{"session_id": "s1", "participants": ["user", "character"]}],
        "turns": [turn],
        "memories": [memory],
    }


@pytest.fixture
def gold() -> dict[str, object]:
    return {"case_id": "case-a", "required_ids": ["m1"], "forbidden_ids": []}


@pytest.fixture
def hit(turn: dict[str, object]) -> dict[str, object]:
    text = str(turn["text"])
    return {
        "memory_id": "m1",
        "score": 0.55,
        "source_spans": [
            {
                "turn_id": "t1",
                "start": 0,
                "end": len(text),
                "text": text,
                "language": "ja",
                "actor": "user",
            }
        ],
    }


@pytest.fixture
def protocol() -> dict[str, object]:
    # These are synthetic test settings, not the selected comparison protocol.
    return {
        "threshold": 0.5,
        "top_k": 5,
        "token_budget": 1500,
        "repetitions": 3,
        "weights": {
            "semantic": 0.4,
            "lexical": 0.2,
            "recency": 0.1,
            "importance": 0.1,
            "self_relevance": 0.1,
            "trust": 0.1,
        },
        "percentile_method": "linear",
    }


@pytest.fixture
def candidate() -> dict[str, object]:
    return {
        "id": "synthetic-e5-bigm",
        "model_alias": "synthetic-e5",
        "revision": "synthetic-v1",
        "sha256": "a" * 64,
        "dimensions": 2,
        "pooling": "mean",
        "query_prefix": "query: ",
        "passage_prefix": "passage: ",
        "extension": "pg_bigm",
    }


@pytest.fixture
def make_suite(
    case: dict[str, object], gold: dict[str, object], protocol: dict[str, object]
) -> Callable[[], dict[str, object]]:
    def build() -> dict[str, object]:
        return {
            "cases": [case],
            "gold": [gold],
            "protocol": protocol,
            "split": "acceptance",
        }

    return build


@pytest.fixture
def manifest() -> dict[str, object]:
    return {
        "memory_commit": "f" * 40,
        "core_commit": "b" * 40,
        "memory_dirty": True,
        "core_dirty": False,
        "db_image": "sha256:" + "c" * 64,
        "db_extensions": {"vector": "0.8.2", "pg_bigm": "1.2", "pgroonga": "4.1.0"},
        "llama_cpp_build": "b1234-abcdef",
        "llama_cpp_image": "sha256:" + "d" * 64,
        "gpu_preflight_confirmed": True,
    }
