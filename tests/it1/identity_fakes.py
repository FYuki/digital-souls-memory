"""Port doubles for registration; domain validation remains in production code."""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime

from digital_souls_memory.identity.domain import (
    CoreIdentity,
    PersonalityProfile,
    Temperament,
    select_current_profile,
    validate_write,
)
from digital_souls_memory.identity.ports import IdentityConflict, IdentityUnavailable, ReviewResult
from tests.identity_support import DESCRIPTION


class FakeIdentityTransaction:
    def __init__(self, store: "FakeIdentityStore", character_id: str, source: str) -> None:
        self.character_id = character_id
        self.source = source
        self.cores = store.cores.copy()
        self.temperaments = store.temperaments.copy()
        self.profiles = store.profiles.copy()
        self.store = store

    def get_core(self, version: int | None = None) -> CoreIdentity | None:
        values = [v for (c, _), v in self.cores.items() if c == self.character_id]
        return (
            next((v for v in values if v.version == version), None)
            if version is not None
            else max(values, key=lambda v: v.version, default=None)
        )

    def get_temperament(self, version: int | None = None) -> Temperament | None:
        values = [v for (c, _), v in self.temperaments.items() if c == self.character_id]
        return (
            next((v for v in values if v.version == version), None)
            if version is not None
            else max(values, key=lambda v: v.version, default=None)
        )

    def get_profile(self, version: int | None = None) -> PersonalityProfile | None:
        values = [v for (c, _), v in self.profiles.items() if c == self.character_id]
        return (
            next((v for v in values if v.version == version), None)
            if version is not None
            else max(values, key=lambda v: v.version, default=None)
        )

    def add_core(self, value: CoreIdentity) -> None:
        validate_write(source=self.source, operation="core_identity", has_profile=False)
        if value.character_id != self.character_id:
            raise PermissionError("Transaction belongs to another character")
        key = (value.character_id, value.version)
        if key in self.cores:
            raise IdentityConflict()
        self.cores[key] = value

    def add_temperament(self, value: Temperament) -> None:
        validate_write(source=self.source, operation="temperament", has_profile=False)
        if value.character_id != self.character_id:
            raise PermissionError("Transaction belongs to another character")
        key = (value.character_id, value.version)
        if key in self.temperaments:
            raise IdentityConflict()
        self.temperaments[key] = value

    def add_profile(self, value: PersonalityProfile) -> None:
        validate_write(source=self.source, operation="author_seed", has_profile=False)
        if value.character_id != self.character_id:
            raise PermissionError("Transaction belongs to another character")
        if self.get_profile() is not None:
            raise IdentityConflict()
        self.profiles[(value.character_id, value.version)] = value
        if self.store.fail_at == "insert":
            raise IdentityUnavailable()


class FakeIdentityStore:
    def __init__(self) -> None:
        self.cores: dict[tuple[str, int], CoreIdentity] = {}
        self.temperaments: dict[tuple[str, int], Temperament] = {}
        self.profiles: dict[tuple[str, int], PersonalityProfile] = {}
        self.fail_at: str | None = None

    @contextmanager
    def transaction(self, character_id: str, *, source: str) -> Iterator[FakeIdentityTransaction]:
        transaction = FakeIdentityTransaction(self, character_id, source)
        yield transaction
        if self.fail_at == "commit":
            raise IdentityUnavailable()
        self.cores = transaction.cores
        self.temperaments = transaction.temperaments
        self.profiles = transaction.profiles

    def get_core(self, character_id: str, version: int | None = None) -> CoreIdentity | None:
        return FakeIdentityTransaction(self, character_id, "read").get_core(version)

    def get_temperament(self, character_id: str, version: int | None = None) -> Temperament | None:
        return FakeIdentityTransaction(self, character_id, "read").get_temperament(version)

    def get_profile(
        self, character_id: str, version: int | None = None
    ) -> PersonalityProfile | None:
        return FakeIdentityTransaction(self, character_id, "read").get_profile(version)

    def get_current(
        self, character_id: str, at: datetime
    ) -> tuple[CoreIdentity | None, Temperament | None, PersonalityProfile | None]:
        return (
            self.get_core(character_id),
            self.get_temperament(character_id),
            select_current_profile(
                [p for (c, _), p in self.profiles.items() if c == character_id], at
            ),
        )


class FakeLLMGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[dict[str, float], Temperament, str]] = []
        self.description = DESCRIPTION
        self.fail = False

    def render_personality(
        self, *, aspects: Mapping[str, float], temperament: Temperament, renderer_version: str
    ) -> str:
        self.calls.append((dict(aspects), temperament, renderer_version))
        if self.fail:
            raise IdentityUnavailable()
        return self.description


class FakeReviewer:
    def __init__(self) -> None:
        self.calls: list[tuple[Temperament, PersonalityProfile]] = []
        self.result = ReviewResult(consistent=True, reason="合成入力は整合している")
        self.fail = False

    def review_identity(
        self, *, temperament: Temperament, profile: PersonalityProfile
    ) -> ReviewResult:
        self.calls.append((temperament, profile))
        if self.fail:
            raise IdentityUnavailable()
        return self.result
