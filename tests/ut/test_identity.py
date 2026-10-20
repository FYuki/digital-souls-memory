from dataclasses import replace
from datetime import timedelta
from typing import cast

import pytest

from digital_souls_memory.identity.domain import (
    CoreIdentity,
    PersonalityProfile,
    Temperament,
    select_current_profile,
    validate_aspects,
    validate_write,
)
from digital_souls_memory.identity.ports import (
    IdentityConflict,
    IdentityNotFound,
    IdentityUnavailable,
)
from digital_souls_memory.identity.use_cases import IdentityService
from tests.identity_support import (
    ASPECT_KEYS,
    DESCRIPTION,
    INSTRUCTION,
    REGISTERED,
    aspects,
)
from tests.it1.identity_fakes import FakeIdentityStore, FakeLLMGateway, FakeReviewer

pytestmark = pytest.mark.ut


@pytest.mark.parametrize("key", ASPECT_KEYS)
@pytest.mark.parametrize("invalid", [-0.001, 1.001, float("nan"), float("inf"), -float("inf")])
def test_each_aspect_rejects_out_of_range_and_nonfinite_values(key: str, invalid: float) -> None:
    values = aspects() | {key: invalid}
    with pytest.raises((ValueError, TypeError)):
        validate_aspects(values)


@pytest.mark.parametrize("key", ASPECT_KEYS)
def test_every_aspect_is_required(key: str) -> None:
    values = aspects()
    del values[key]
    with pytest.raises((ValueError, TypeError)):
        validate_aspects(values)


@pytest.mark.parametrize("invalid", [True, "0.5", None])
def test_aspect_values_are_not_coerced(invalid: object) -> None:
    with pytest.raises((ValueError, TypeError)):
        validate_aspects(aspects() | {"Volatility": cast(float, invalid)})


def test_unknown_aspect_is_rejected() -> None:
    with pytest.raises((ValueError, TypeError)):
        validate_aspects(aspects() | {"Unknown": 0.5})


@pytest.mark.parametrize("boundary", [0, 1])
def test_all_aspects_accept_both_inclusive_boundaries(boundary: int) -> None:
    values = dict.fromkeys(ASPECT_KEYS, boundary)
    assert dict(validate_aspects(values)) == values


@pytest.mark.parametrize("source", ["reflection", "memory", "dream", "fact", "store"])
@pytest.mark.parametrize("operation", ["core_identity", "temperament", "author_seed"])
def test_nonregistration_sources_cannot_write_identity(source: str, operation: str) -> None:
    with pytest.raises((ValueError, PermissionError)):
        validate_write(source=source, operation=operation, has_profile=False)


@pytest.mark.parametrize("operation", ["core_identity", "temperament", "author_seed"])
def test_registration_source_can_register_initial_identity(operation: str) -> None:
    validate_write(source="registration", operation=operation, has_profile=False)


@pytest.mark.parametrize("operation", ["author_seed", "personality_update"])
def test_registration_does_not_permit_reseed_or_free_personality_change(operation: str) -> None:
    with pytest.raises((ValueError, PermissionError)):
        validate_write(source="registration", operation=operation, has_profile=True)


def test_free_personality_change_cannot_use_the_initial_seed_exception() -> None:
    with pytest.raises((ValueError, PermissionError)):
        validate_write(source="registration", operation="personality_update", has_profile=False)


def test_instruction_text_cannot_authorize_nonregistration_write() -> None:
    value = Temperament("char-a", INSTRUCTION, 1, REGISTERED)
    assert value.description == INSTRUCTION
    with pytest.raises((ValueError, PermissionError)):
        validate_write(source="memory", operation="temperament", has_profile=False)


def profile() -> PersonalityProfile:
    return PersonalityProfile(
        character_id="char-a",
        aspects=aspects(),
        description=DESCRIPTION,
        renderer_version="synthetic-renderer-v1",
        temperament_version=1,
        version=1,
        valid_from=REGISTERED,
        valid_to=None,
        evidence_belief_ids=(),
        change_kind="author_seed",
    )


def test_author_seed_preserves_values_without_inventing_beliefs() -> None:
    value = profile()
    assert dict(value.aspects) == aspects()
    assert value.evidence_belief_ids == ()
    assert value.change_kind == "author_seed"


def test_mutating_registration_inputs_cannot_rewrite_existing_identity() -> None:
    values = ["誠実"]
    prohibitions = ["欺かない"]
    numbers = aspects()
    core = CoreIdentity("char-a", values, prohibitions, 1, REGISTERED)
    personality = replace(profile(), aspects=numbers)
    values.clear()
    prohibitions.clear()
    numbers["Volatility"] = 0.8
    assert tuple(core.values) == ("誠実",)
    assert tuple(core.prohibitions) == ("欺かない",)
    assert dict(personality.aspects) == aspects()


@pytest.mark.parametrize("offset,expected", [(-1, None), (0, 1), (1, None)])
def test_current_profile_respects_validity_interval(offset: int, expected: int | None) -> None:
    value = replace(profile(), valid_to=REGISTERED + timedelta(seconds=1))
    selected = select_current_profile([value], REGISTERED + timedelta(seconds=offset))
    assert (None if selected is None else selected.version) == expected


def test_current_profile_does_not_choose_expired_or_future_higher_versions() -> None:
    current = profile()
    expired = replace(
        current,
        version=2,
        valid_from=REGISTERED - timedelta(days=2),
        valid_to=REGISTERED - timedelta(days=1),
    )
    future = replace(current, version=3, valid_from=REGISTERED + timedelta(days=1))
    assert select_current_profile([future, expired, current], REGISTERED) == current


def test_service_registration_and_version_reads() -> None:
    store = FakeIdentityStore()
    service = IdentityService(store, lambda: REGISTERED, FakeLLMGateway(), FakeReviewer())
    core = service.register_core("char-a", ("誠実",), (), source="registration")
    temperament = service.register_temperament("char-a", "静かに喜ぶ", source="registration")
    seeded = service.author_seed("char-a", aspects(), "v1", source="registration")
    assert service.get_core("char-a", 1) == core
    assert service.get_temperament("char-a", 1) == temperament
    assert service.get_profile("char-a", 1) == seeded
    assert service.get_current("char-a") == (core, temperament, seeded)
    with pytest.raises(IdentityConflict):
        service.author_seed("char-a", aspects(), "v1", source="registration")


@pytest.mark.parametrize("resource", ["core", "temperament", "profile"])
def test_service_version_reads_do_not_substitute_missing_versions(resource: str) -> None:
    service = IdentityService(FakeIdentityStore(), lambda: REGISTERED, None, None)
    with pytest.raises(IdentityNotFound):
        if resource == "core":
            service.get_core("char-a", 1)
        elif resource == "temperament":
            service.get_temperament("char-a", 1)
        else:
            service.get_profile("char-a", 1)


def test_service_missing_reviewer_cannot_publish_seed_or_temperament_update() -> None:
    store = FakeIdentityStore()
    service = IdentityService(store, lambda: REGISTERED, FakeLLMGateway(), None)
    initial = service.register_temperament("char-a", "静かに喜ぶ", source="registration")
    with pytest.raises(IdentityUnavailable):
        service.author_seed("char-a", aspects(), "v1", source="registration")
    assert service.get_current("char-a") == (None, initial, None)
    authorized = IdentityService(store, lambda: REGISTERED, FakeLLMGateway(), FakeReviewer())
    seeded = authorized.author_seed("char-a", aspects(), "v1", source="registration")
    with pytest.raises(IdentityUnavailable):
        service.register_temperament("char-a", "大きく喜ぶ", source="registration")
    assert service.get_current("char-a") == (None, initial, seeded)


@pytest.mark.parametrize("entity", ["core", "temperament", "profile"])
def test_fake_transaction_cannot_write_another_character(entity: str) -> None:
    store = FakeIdentityStore()
    with pytest.raises(PermissionError):
        with store.transaction("char-b", source="registration") as tx:
            if entity == "core":
                tx.add_core(CoreIdentity("char-a", (), (), 1, REGISTERED))
            elif entity == "temperament":
                tx.add_temperament(Temperament("char-a", "静かに喜ぶ", 1, REGISTERED))
            else:
                tx.add_profile(profile())
    assert store.get_current("char-a", REGISTERED) == (None, None, None)
