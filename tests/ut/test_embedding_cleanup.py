"""Embedding collection keeps the changed digest scope and propagates DB failure."""

from unittest.mock import MagicMock

import psycopg
import pytest

from digital_souls_memory.adapters.embedding_cleanup import collect_embeddings

pytestmark = pytest.mark.ut


def test_embedding_collection_passes_only_changed_digests_and_does_not_hide_failure() -> None:
    connection = MagicMock()
    connection.execute.side_effect = psycopg.OperationalError("synthetic failure")
    with pytest.raises(psycopg.OperationalError):
        collect_embeddings(connection, ["changed-digest"])
    assert connection.execute.call_args.args[1] == (["changed-digest"],)
    connection.commit.assert_not_called()
