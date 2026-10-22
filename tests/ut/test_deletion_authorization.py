"""Core credentials identify the author independently of request content."""

import logging

import pytest

pytestmark = pytest.mark.ut

AUTHOR = "synthetic-author"
TOKEN = "synthetic-credential-é"


@pytest.mark.parametrize("scheme", ["Bearer", "bearer", "bEaReR"])
@pytest.mark.parametrize("token", ["synthetic-credential", TOKEN])
def test_valid_bearer_returns_the_configured_author(scheme: str, token: str) -> None:
    from digital_souls_memory.adapters.core_authorization import CoreAuthorizer
    from digital_souls_memory.governance.domain import AuthorPrincipal

    authorizer = CoreAuthorizer(author_id=AUTHOR, token=token)

    assert authorizer.authorize(f"{scheme} {token}") == AuthorPrincipal(author_id=AUTHOR)


@pytest.mark.parametrize(
    "authorization",
    [
        None,
        "",
        " ",
        "Bearer",
        "Bearer ",
        f"Basic {TOKEN}",
        TOKEN,
        "Bearer wrong-credential",
        f"Bearer {TOKEN.upper()}",
        "Bearer synthetic-credential-e\u0301",
        f"Bearer {TOKEN} ",
        f"Bearer {TOKEN} extra",
        f"Bearer\t{TOKEN}",
        f"Bearer  {TOKEN}",
    ],
)
def test_missing_malformed_or_different_credentials_are_unauthorized(
    authorization: str | None,
) -> None:
    from digital_souls_memory.adapters.core_authorization import CoreAuthorizer
    from digital_souls_memory.governance.ports import AuthorUnauthorized

    authorizer = CoreAuthorizer(author_id=AUTHOR, token=TOKEN)

    with pytest.raises(AuthorUnauthorized):
        authorizer.authorize(authorization)


@pytest.mark.parametrize(
    ("author_id", "token"),
    [("", TOKEN), (" ", TOKEN), ("\t\n", TOKEN), (AUTHOR, ""), (AUTHOR, " "), (AUTHOR, "\t\n")],
)
def test_blank_author_configuration_is_unavailable(author_id: str, token: str) -> None:
    from digital_souls_memory.adapters.core_authorization import CoreAuthorizer
    from digital_souls_memory.governance.ports import AuthorUnavailable

    with pytest.raises(AuthorUnavailable):
        CoreAuthorizer(author_id=author_id, token=token)


def test_constructor_arguments_supply_authority_instead_of_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from digital_souls_memory.adapters.core_authorization import CoreAuthorizer
    from digital_souls_memory.governance.domain import AuthorPrincipal
    from digital_souls_memory.governance.ports import AuthorUnauthorized

    monkeypatch.setenv("MEMORY_AUTHOR_ID", "other-synthetic-author")
    monkeypatch.setenv("MEMORY_AUTHOR_TOKEN", "other-synthetic-credential")
    authorizer = CoreAuthorizer(author_id=AUTHOR, token=TOKEN)

    assert authorizer.authorize(f"Bearer {TOKEN}") == AuthorPrincipal(author_id=AUTHOR)
    with pytest.raises(AuthorUnauthorized):
        authorizer.authorize("Bearer other-synthetic-credential")


def test_credentials_are_absent_from_repr_exceptions_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from digital_souls_memory.adapters.core_authorization import CoreAuthorizer
    from digital_souls_memory.governance.ports import AuthorUnauthorized, AuthorUnavailable

    configured = "synthetic-Q7configuredP4-R9secretK2"
    supplied = "synthetic-T3suppliedF8-V6privateJ1"
    with caplog.at_level(logging.DEBUG):
        authorizer = CoreAuthorizer(author_id=AUTHOR, token=configured)
        authorizer.authorize(f"Bearer {configured}")
        with pytest.raises(AuthorUnauthorized) as denied:
            authorizer.authorize(f"Bearer {supplied}")
        with pytest.raises(AuthorUnavailable) as unavailable:
            CoreAuthorizer(author_id="", token=configured)

    observations = [
        repr(authorizer),
        str(denied.value),
        repr(denied.value),
        str(unavailable.value),
        repr(unavailable.value),
        *(record.getMessage() for record in caplog.records),
    ]
    for observation in observations:
        for credential in (configured, supplied):
            for component in credential.split("-")[1:]:
                assert component.casefold() not in observation.casefold()
