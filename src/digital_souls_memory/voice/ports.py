"""Persistence and permitted original-text contracts for Voice."""

from collections.abc import Iterator
from contextlib import AbstractContextManager
from typing import Protocol

from digital_souls_memory.identity.ports import ReviewResult
from digital_souls_memory.voice.domain import VerbatimAnchor, VoiceExemplar


class VoiceConflict(Exception):
    """A material or initial check already exists."""


class VoiceNotFound(Exception):
    """A reference or accessible material is absent."""


class VoiceUnavailable(Exception):
    """A required dependency cannot serve the operation."""


class MaterialText(Protocol):
    def get_text(self, character_id: str, turn_id: str) -> str | None:
        """Return permitted verbatim text, or None when inaccessible or pending.

        Empty text means confirmed no delivery. Re-evaluate receipts and time on
        every call; this port does not persist pending work or schedule extraction.
        """
        ...


class VoiceTransaction(Protocol):
    def get_initial_check(self) -> ReviewResult | None: ...
    def save_initial_check(self, result: ReviewResult) -> None: ...
    def add_exemplar(self, value: VoiceExemplar) -> None: ...
    def add_anchor(self, value: VerbatimAnchor) -> None: ...


class VoiceStore(Protocol):
    def transaction(self, character_id: str) -> AbstractContextManager[VoiceTransaction]:
        """Serialize initial registration per character and commit all or nothing."""
        ...

    def get_exemplar(self, character_id: str, exemplar_id: str) -> VoiceExemplar | None: ...
    def get_anchor(self, character_id: str, anchor_id: str) -> VerbatimAnchor | None: ...
    def list_exemplars(
        self, character_id: str
    ) -> AbstractContextManager[Iterator[VoiceExemplar]]: ...
    def list_anchors(
        self, character_id: str
    ) -> AbstractContextManager[Iterator[VerbatimAnchor]]: ...
