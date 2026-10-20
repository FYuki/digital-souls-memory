import pytest
from fastapi.testclient import TestClient

from digital_souls_memory.api import create_app

pytestmark = pytest.mark.it1


@pytest.mark.parametrize("database_url", [None, "invalid://unreachable"])
def test_health_does_not_require_a_database(
    monkeypatch: pytest.MonkeyPatch, database_url: str | None
) -> None:
    if database_url is None:
        monkeypatch.delenv("MEMORY_DATABASE_URL", raising=False)
    else:
        monkeypatch.setenv("MEMORY_DATABASE_URL", database_url)
    with TestClient(create_app()) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_is_get_only() -> None:
    with TestClient(create_app()) as client:
        assert client.post("/health").status_code == 405


def test_domain_api_is_not_implemented() -> None:
    with TestClient(create_app()) as client:
        assert client.get("/memories").status_code == 404
