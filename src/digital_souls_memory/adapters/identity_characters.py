"""Adapt Identity registration facts to Evidence's read-only character port."""

from collections.abc import Callable

from digital_souls_memory.evidence.ports import EvidenceUnavailable
from digital_souls_memory.identity.ports import IdentityStore, IdentityUnavailable


class IdentityCharacters:
    def __init__(self, store: Callable[[], IdentityStore]) -> None:
        self._store = store

    def is_character(self, identifier: str) -> bool:
        try:
            store = self._store()
            return (
                store.get_core(identifier) is not None
                or store.get_temperament(identifier) is not None
            )
        except IdentityUnavailable as error:
            raise EvidenceUnavailable() from error
