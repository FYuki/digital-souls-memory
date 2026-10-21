from dataclasses import asdict, replace
from datetime import timedelta
from types import TracebackType
from typing import cast
from weakref import ReferenceType, WeakSet, ref

import psycopg
import pytest
from psycopg.rows import DictRow

from digital_souls_memory.adapters.recall_search import search_rows
from digital_souls_memory.evidence.materials import ReceiptState
from digital_souls_memory.recall.domain import CandidateMaterial
from tests.evidence_support import RECORDED, session, turn
from tests.recall_fixtures import SPACE, source_stamp

pytestmark = pytest.mark.ut


class Rows:
    def __init__(self, rows: list[DictRow]) -> None:
        self.rows = rows

    def fetchone(self) -> DictRow | None:
        return self.rows[0] if self.rows else None


class SearchConnection:
    def __init__(self, documents: list[DictRow]) -> None:
        self.documents = documents
        self.setting = "on"
        self.limits: list[int] = []
        self.settings: list[str] = []
        self.closed = 0
        self.fetch_sizes: list[int] = []

    def cursor(self, name: str, *, scrollable: bool, withhold: bool) -> "SearchCursor":
        assert name and not scrollable and not withhold
        return SearchCursor(self)

    def execute(self, query: str, params: tuple[object, ...] = ()) -> Rows:
        if query.startswith("SET TRANSACTION"):
            return Rows([])
        if query == "SHOW enable_seqscan":
            return Rows([{"enable_seqscan": self.setting}])
        if query.startswith("SELECT set_config"):
            self.setting = "off" if "'off'" in query else str(params[0])
            return Rows([])
        raise AssertionError(query)


class SearchCursor:
    def __init__(self, connection: SearchConnection) -> None:
        self.connection = connection
        self.position = 0
        self.width = 0

    def __enter__(self) -> "SearchCursor":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.connection.closed += 1

    def execute(self, query: str, params: tuple[object, ...]) -> None:
        self.width = int(str(params[-1]))
        self.connection.limits.append(self.width)
        self.connection.settings.append(self.connection.setting)

    def fetchmany(self, size: int) -> list[DictRow]:
        self.connection.fetch_sizes.append(size)
        result = self.connection.documents[self.position : min(self.position + size, self.width)]
        self.position += len(result)
        return result


class TrackedId(str):
    pass


class TrackedRow(dict[str, object]):
    pass


@pytest.mark.parametrize("denied_count", [64, 512])
@pytest.mark.parametrize("allowed_count", [0, 2, 3, 9])
@pytest.mark.parametrize("missing", [False, True])
def test_refill_bounds_live_rows_keys_and_sources(
    monkeypatch: pytest.MonkeyPatch,
    denied_count: int,
    allowed_count: int,
    missing: bool,
) -> None:
    # Generate only the fetched rows; weak references observe lifetime without retaining payloads.
    live_rows: list[ReferenceType[TrackedRow]] = []
    live_ids: WeakSet[TrackedId] = WeakSet()
    peaks = [0, 0]
    source = turn(text="日本語")

    def document(i: int) -> DictRow:
        identifier = TrackedId(f"candidate-{i}")
        source_id = TrackedId(f"source-{i}")
        live_ids.add(identifier)
        live_ids.add(source_id)
        row = TrackedRow(
            allowed=True,
            owner_id="char-b",
            kind="memory",
            identifier=identifier,
            payload={"source_turn_ids": [source_id]},
            dependencies={
                "values": [
                    asdict(source_stamp(source, character_id="char-b")) | {"turn_id": source_id}
                ]
            },
        )
        live_rows[:] = [value for value in live_rows if value() is not None]
        live_rows.append(ref(row))
        peaks[0] = max(peaks[0], len(live_rows))
        peaks[1] = max(peaks[1], len(live_ids))
        return row

    class GeneratedCursor(SearchCursor):
        def fetchmany(self, size: int) -> list[DictRow]:
            self.connection.fetch_sizes.append(size)
            end = min(self.position + size, self.width, denied_count + allowed_count)
            result = [document(i) for i in range(self.position, end)]
            self.position = end
            return result

    class GeneratedConnection(SearchConnection):
        def cursor(self, name: str, *, scrollable: bool, withhold: bool) -> SearchCursor:
            assert name and not scrollable and not withhold
            return GeneratedCursor(self)

    def material(connection: object, turn_id: str) -> CandidateMaterial | None:
        i = int(turn_id.removeprefix("source-"))
        assert cast(GeneratedConnection, connection).setting == "on"
        if missing and i < denied_count:
            return None
        return CandidateMaterial(
            turn=replace(source, turn_id=turn_id),
            session=session(),
            actor_is_character=True,
            receipt_state=ReceiptState(1 if i < denied_count else len(source.text), 1),
        )

    monkeypatch.setattr("digital_souls_memory.adapters.recall_search.read_material", material)
    connection = GeneratedConnection([])
    found = search_rows(
        cast(psycopg.Connection[DictRow], connection),
        character_id="char-b",
        text="日本語",
        vector="[1,0]",
        space=SPACE,
        now=RECORDED,
        timeout=timedelta(minutes=10),
        limit=3,
    )
    assert {row["identifier"] for row in found} == {
        f"candidate-{i}" for i in range(denied_count, denied_count + min(3, allowed_count))
    }
    assert peaks[0] <= 24
    assert peaks[1] <= 60
    assert connection.fetch_sizes and max(connection.fetch_sizes) <= 3
    assert connection.closed == len(connection.limits)


def test_refill_deduplicates_candidates_and_reuses_evidence_only_within_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = turn(text="日本語")
    dependency = asdict(source_stamp(source, character_id="char-b"))
    documents = [
        {
            "allowed": True,
            "owner_id": "char-b",
            "kind": "memory",
            "identifier": identifier,
            "payload": {"source_turn_ids": [source.turn_id]},
            "dependencies": {
                "values": [
                    dependency | {"turn_id": "denied"}
                    if identifier.startswith("denied")
                    else dependency
                ]
            },
            "semantic": 0.9,
            "lexical": 0.8,
        }
        for identifier in ("denied-1", "denied-2", "allowed-1", "allowed-2")
    ]
    reads: list[str] = []
    state = ReceiptState(None, 0)

    def material(connection: object, turn_id: str) -> CandidateMaterial | None:
        if turn_id == "denied":
            return None
        reads.append(turn_id)
        return CandidateMaterial(
            turn=source, session=session(), actor_is_character=True, receipt_state=state
        )

    monkeypatch.setattr("digital_souls_memory.adapters.recall_search.read_material", material)
    connection = SearchConnection(documents)

    def run() -> tuple[DictRow, ...]:
        return search_rows(
            cast(psycopg.Connection[DictRow], connection),
            character_id="char-b",
            text="日本語",
            vector="[1,0]",
            space=SPACE,
            now=RECORDED,
            timeout=timedelta(minutes=10),
            limit=2,
        )

    state = ReceiptState(len(source.text), 1)
    assert {row["identifier"] for row in run()} == {"allowed-1", "allowed-2"}
    assert reads == [source.turn_id]
    assert connection.limits == [2, 4, 2, 4]
    assert connection.settings == ["on", "on", "off", "off"]
    assert connection.setting == "on"
    state = ReceiptState(1, 2)
    assert run() == ()
    assert reads == [source.turn_id, source.turn_id]


def test_search_uses_support_time_and_checks_extra_input_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = turn(text="日本語")
    reply = replace(source, turn_id="reply", occurred_at=RECORDED + timedelta(days=1))
    documents = [
        {
            "allowed": True,
            "owner_id": "char-b",
            "kind": "anchor",
            "identifier": "anchor",
            "payload": {"turn_id": source.turn_id},
            "dependencies": {
                "values": [
                    asdict(source_stamp(value, character_id="char-b")) for value in (source, reply)
                ]
            },
        }
    ]
    inputs = {
        value.turn_id: CandidateMaterial(
            turn=value,
            session=session(reports_delivery=False),
            actor_is_character=True,
            receipt_state=ReceiptState(None, 0),
        )
        for value in (source, reply)
    }

    def material(connection: object, turn_id: str) -> CandidateMaterial | None:
        return inputs.get(turn_id)

    monkeypatch.setattr("digital_souls_memory.adapters.recall_search.read_material", material)

    def run() -> tuple[DictRow, ...]:
        return search_rows(
            cast(psycopg.Connection[DictRow], SearchConnection(documents)),
            character_id="char-b",
            text="日本語",
            vector="[1,0]",
            space=SPACE,
            now=RECORDED,
            timeout=timedelta(minutes=10),
            limit=2,
        )

    assert run()[0]["occurred_at"] == source.occurred_at
    del inputs[reply.turn_id]
    assert run() == ()


@pytest.mark.parametrize("denied", [False, True])
def test_dependencies_survive_cache_eviction_without_changing_support_time(
    monkeypatch: pytest.MonkeyPatch,
    denied: bool,
) -> None:
    source = turn(text="日本語")
    dependencies = (
        [source]
        + [
            replace(source, turn_id=f"extra-{i}", occurred_at=RECORDED + timedelta(days=1))
            for i in range(16)
        ]
        + [source]
    )
    row = {
        "allowed": True,
        "owner_id": "char-b",
        "kind": "anchor",
        "identifier": "anchor",
        "payload": {"turn_id": source.turn_id},
        "dependencies": {
            "values": [asdict(source_stamp(v, character_id="char-b")) for v in dependencies]
        },
    }
    reads: list[str] = []

    def material(connection: object, turn_id: str) -> CandidateMaterial:
        reads.append(turn_id)
        value = next(v for v in dependencies if v.turn_id == turn_id)
        return CandidateMaterial(
            turn=value,
            session=session(),
            actor_is_character=True,
            receipt_state=ReceiptState(
                1 if denied and turn_id == "extra-15" else len(value.text), 1
            ),
        )

    monkeypatch.setattr("digital_souls_memory.adapters.recall_search.read_material", material)
    found = search_rows(
        cast(psycopg.Connection[DictRow], SearchConnection([row])),
        character_id="char-b",
        text="日本語",
        vector="[1,0]",
        space=SPACE,
        now=RECORDED,
        timeout=timedelta(minutes=10),
        limit=2,
    )
    if denied:
        assert found == ()
    else:
        assert found[0]["occurred_at"] == source.occurred_at
        assert reads.count(source.turn_id) >= 2
    assert set(reads) == {v.turn_id for v in dependencies}


@pytest.mark.parametrize("stage", ["declare", "fetch", "evidence"])
@pytest.mark.parametrize("lexical", [False, True])
def test_search_closes_cursor_and_propagates_failure(
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    lexical: bool,
) -> None:
    class FailingCursor(SearchCursor):
        def execute(self, query: str, params: tuple[object, ...]) -> None:
            super().execute(query, params)
            if (self.connection.setting == "off") == lexical and stage == "declare":
                raise psycopg.OperationalError()

        def fetchmany(self, size: int) -> list[DictRow]:
            if (self.connection.settings[-1] == "off") != lexical:
                return []
            if stage == "fetch":
                raise psycopg.OperationalError()
            return super().fetchmany(size)

    class FailingConnection(SearchConnection):
        def cursor(self, name: str, *, scrollable: bool, withhold: bool) -> SearchCursor:
            assert name and not scrollable and not withhold
            return FailingCursor(self)

    def material(connection: object, turn_id: str) -> CandidateMaterial:
        assert cast(SearchConnection, connection).setting == "on"
        raise psycopg.OperationalError()

    monkeypatch.setattr("digital_souls_memory.adapters.recall_search.read_material", material)
    connection = FailingConnection(
        [
            {
                "allowed": True,
                "owner_id": "char-b",
                "kind": "anchor",
                "identifier": "anchor",
                "payload": {"turn_id": "turn-1"},
                "dependencies": {"values": [{"turn_id": "turn-1"}]},
            }
        ]
    )
    with pytest.raises(psycopg.OperationalError):
        search_rows(
            cast(psycopg.Connection[DictRow], connection),
            character_id="char-b",
            text="日本語",
            vector="[1,0]",
            space=SPACE,
            now=RECORDED,
            timeout=timedelta(minutes=10),
            limit=2,
        )
    assert connection.closed == len(connection.limits)
    if stage != "declare":
        assert connection.setting == "on"
