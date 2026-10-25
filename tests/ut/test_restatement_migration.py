"""Migration refuses delivery assumptions that cannot be restored from storage."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from alembic.util import load_python_file

from digital_souls_memory.evidence.materials import ReceiptState
from digital_souls_memory.memory.domain import MemoryValidationError
from digital_souls_memory.recall.domain import CandidateMaterial
from tests.evidence_support import RECORDED, session, turn
from tests.it1.evidence_fakes import FakeEvidenceStore
from tests.memory_fakes import FakeMemoryStore
from tests.memory_support import memory

pytestmark = pytest.mark.ut


@pytest.mark.parametrize("reports_delivery", [False, True])
def test_migration_needs_stored_deadline_for_unconfirmed_other_character_speech(
    monkeypatch: pytest.MonkeyPatch, reports_delivery: bool
) -> None:
    migration = load_python_file(
        str(Path(__file__).parents[2] / "migrations" / "versions"),
        "0014_restatement_contributions.py",
    )
    store = FakeMemoryStore(FakeEvidenceStore())
    original = memory()
    source = turn()
    material = CandidateMaterial(
        turn=source,
        session=session(reports_delivery=reports_delivery),
        actor_is_character=True,
        receipt_state=ReceiptState(None, 0),
    )
    connection = MagicMock()
    connection.execute.return_value.fetchall.return_value = []
    with store.transaction("char-b") as tx:
        tx.add_memory(original)
        monkeypatch.setattr(migration, "_MemoryTransaction", lambda *args: tx)
        monkeypatch.setattr(migration, "read_materials", lambda *args: {source.turn_id: material})
        if reports_delivery:
            with pytest.raises(MemoryValidationError):
                migration._reproject(connection, "char-b", RECORDED)
        else:
            migration._reproject(connection, "char-b", RECORDED)
        assert tx.memories() == (original,)
