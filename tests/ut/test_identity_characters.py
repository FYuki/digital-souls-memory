from collections.abc import Callable

import pytest

from digital_souls_memory.adapters.identity_characters import IdentityCharacters
from digital_souls_memory.evidence.ports import EvidenceUnavailable
from digital_souls_memory.identity.domain import CoreIdentity, Temperament
from digital_souls_memory.identity.ports import IdentityStore, IdentityUnavailable
from tests.evidence_support import RECORDED
from tests.it1.identity_fakes import FakeIdentityStore

pytestmark = pytest.mark.ut


@pytest.mark.parametrize("registration", ["core", "temperament", "both", "absent"])
def test_registration_facts_determine_character_status(registration: str) -> None:
    store = FakeIdentityStore()
    identifier = "viewer-looking"
    if registration in ("core", "both"):
        store.cores[(identifier, 1)] = CoreIdentity(identifier, ("誠実",), (), 1, RECORDED)
    if registration in ("temperament", "both"):
        store.temperaments[(identifier, 1)] = Temperament(identifier, "合成気質", 1, RECORDED)
    registry = IdentityCharacters(lambda: store)
    assert registry.is_character(identifier) is (registration != "absent")
    assert not registry.is_character("char-unregistered")


@pytest.mark.parametrize("boundary", ["factory", "get_core", "get_temperament"])
def test_lookup_failure_propagates_and_later_lookup_can_succeed(
    monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    store = FakeIdentityStore()
    registry = IdentityCharacters(lambda: store)

    def unavailable(*args: object) -> None:
        raise IdentityUnavailable()

    def unavailable_store() -> IdentityStore:
        raise IdentityUnavailable()

    with monkeypatch.context() as patch:
        factory: Callable[[], IdentityStore] = (
            unavailable_store if boundary == "factory" else lambda: store
        )
        failing_registry = IdentityCharacters(factory)
        if boundary != "factory":
            patch.setattr(store, boundary, unavailable)
        with pytest.raises(EvidenceUnavailable):
            failing_registry.is_character("viewer")
    assert not registry.is_character("viewer")
