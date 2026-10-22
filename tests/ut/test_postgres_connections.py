"""Request ownership and snapshot transactions with a synthetic driver."""

from unittest.mock import MagicMock

import psycopg
import pytest

from digital_souls_memory.adapters.postgres_connections import PostgresConnections
from digital_souls_memory.evidence.provision import ProvisionUnavailable

pytestmark = pytest.mark.ut


@pytest.mark.parametrize("fail", [False, True])
def test_request_reuses_connection_and_finishes_snapshot_before_save(
    monkeypatch: pytest.MonkeyPatch,
    fail: bool,
) -> None:
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connect = MagicMock(return_value=connection)
    monkeypatch.setattr(psycopg, "connect", connect)
    supply = PostgresConnections("postgresql://synthetic")
    with supply.request():
        with supply.connection() as initial:
            initial.execute("initial")
        assert connection.commit.call_count == 1
        with supply.snapshot():
            with supply.connection() as reader:
                reader.execute("search")
            with supply.connection() as reader:
                reader.execute("anchor")
            assert connection.commit.call_count == 1
        assert connection.commit.call_count == 2
        if fail:
            with pytest.raises(RuntimeError):
                with supply.connection() as writer:
                    writer.execute("insert")
                    raise RuntimeError("synthetic")
            connection.rollback.assert_called_once()
        else:
            with supply.connection() as writer:
                writer.execute("insert")
            assert connection.commit.call_count == 3
    connect.assert_called_once()
    assert supply.bound is None and not supply.reading
    connection.__exit__.assert_called_once()


def test_failed_snapshot_releases_ownership_and_next_request_can_succeed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = MagicMock()
    connection.__enter__.return_value = connection
    monkeypatch.setattr(psycopg, "connect", MagicMock(return_value=connection))
    supply = PostgresConnections("postgresql://synthetic")
    with pytest.raises(RuntimeError):
        with supply.request(), supply.snapshot():
            raise RuntimeError("synthetic")
    assert supply.bound is None and not supply.reading
    connection.rollback.assert_called_once()
    with supply.request(), supply.connection() as writer:
        writer.execute("insert")
    assert supply.bound is None


def test_connect_failure_has_the_recall_failure_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(psycopg, "connect", MagicMock(side_effect=psycopg.OperationalError()))
    supply = PostgresConnections("postgresql://synthetic")
    with pytest.raises(ProvisionUnavailable), supply.request():
        pytest.fail("A failed connection must not enter the request")
    assert supply.bound is None
