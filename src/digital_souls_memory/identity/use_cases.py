"""Identity registration owns the entire candidate transaction."""

from collections.abc import Callable, Mapping
from datetime import datetime

from digital_souls_memory.identity.domain import (
    CoreIdentity,
    PersonalityProfile,
    Temperament,
    validate_aspects,
    validate_write,
)
from digital_souls_memory.identity.ports import (
    IdentityConflict,
    IdentityInconsistent,
    IdentityNotFound,
    IdentityStore,
    IdentityUnavailable,
    LLMGateway,
    Reviewer,
)


class IdentityService:
    def __init__(
        self,
        store: IdentityStore,
        clock: Callable[[], datetime],
        gateway: LLMGateway | None,
        reviewer: Reviewer | None,
    ) -> None:
        self.store = store
        self.clock = clock
        self.gateway = gateway
        self.reviewer = reviewer

    def get_core(self, character_id: str, version: int) -> CoreIdentity:
        value = self.store.get_core(character_id, version)
        if value is None:
            raise IdentityNotFound()
        return value

    def get_temperament(self, character_id: str, version: int) -> Temperament:
        value = self.store.get_temperament(character_id, version)
        if value is None:
            raise IdentityNotFound()
        return value

    def get_profile(self, character_id: str, version: int) -> PersonalityProfile:
        value = self.store.get_profile(character_id, version)
        if value is None:
            raise IdentityNotFound()
        return value

    def get_current(
        self, character_id: str
    ) -> tuple[CoreIdentity | None, Temperament | None, PersonalityProfile | None]:
        return self.store.get_current(character_id, self.clock())

    def register_core(
        self,
        character_id: str,
        values: tuple[str, ...],
        prohibitions: tuple[str, ...],
        *,
        source: str,
    ) -> CoreIdentity:
        validate_write(source=source, operation="core_identity", has_profile=False)
        with self.store.transaction(character_id, source=source) as tx:
            previous = tx.get_core()
            value = CoreIdentity(
                character_id,
                values,
                prohibitions,
                1 if previous is None else previous.version + 1,
                self.clock(),
            )
            tx.add_core(value)
        return value

    def _review(self, temperament: Temperament, profile: PersonalityProfile) -> None:
        if self.reviewer is None:
            raise IdentityUnavailable()
        result = self.reviewer.review_identity(temperament=temperament, profile=profile)
        if not result.consistent:
            raise IdentityInconsistent(result.reason)

    def register_temperament(
        self, character_id: str, description: str, *, source: str
    ) -> Temperament:
        validate_write(source=source, operation="temperament", has_profile=False)
        with self.store.transaction(character_id, source=source) as tx:
            previous = tx.get_temperament()
            value = Temperament(
                character_id,
                description,
                1 if previous is None else previous.version + 1,
                self.clock(),
            )
            profile = tx.get_profile()
            if profile is not None:
                self._review(value, profile)
            tx.add_temperament(value)
        return value

    def author_seed(
        self,
        character_id: str,
        aspects: Mapping[str, object],
        renderer_version: str,
        *,
        source: str,
    ) -> PersonalityProfile:
        validate_write(source=source, operation="author_seed", has_profile=False)
        numbers = validate_aspects(aspects)
        with self.store.transaction(character_id, source=source) as tx:
            if tx.get_profile() is not None:
                raise IdentityConflict()
            temperament = tx.get_temperament()
            if temperament is None:
                raise IdentityNotFound()
            if self.gateway is None:
                raise IdentityUnavailable()
            description = self.gateway.render_personality(
                aspects=numbers, temperament=temperament, renderer_version=renderer_version
            )
            value = PersonalityProfile(
                character_id,
                numbers,
                description,
                renderer_version,
                temperament.version,
                1,
                self.clock(),
                None,
                (),
                "author_seed",
            )
            self._review(temperament, value)
            tx.add_profile(value)
        return value
