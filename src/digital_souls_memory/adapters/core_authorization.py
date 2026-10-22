"""Resolve a server-configured Core credential to author authority."""

from hmac import compare_digest

from digital_souls_memory.governance.domain import AuthorPrincipal
from digital_souls_memory.governance.ports import AuthorUnauthorized, AuthorUnavailable


class CoreAuthorizer:
    def __init__(self, *, author_id: str, token: str) -> None:
        if not author_id.strip() or not token.strip():
            raise AuthorUnavailable("Author configuration unavailable")
        self._author_id = author_id
        self._token = token.encode("utf-8")

    def authorize(self, authorization: str | None) -> AuthorPrincipal:
        if authorization is None:
            raise AuthorUnauthorized("Author credential required")
        scheme, separator, credential = authorization.partition(" ")
        if (
            scheme.lower() != "bearer"
            or not separator
            or any(character.isspace() for character in credential)
            or not compare_digest(credential.encode("utf-8"), self._token)
        ):
            raise AuthorUnauthorized("Author credential rejected")
        return AuthorPrincipal(author_id=self._author_id)
