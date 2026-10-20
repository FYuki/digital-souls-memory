import socket

import pytest
from pytest_socket import SocketBlockedError  # type: ignore[import-untyped]

from digital_souls_memory.api import health

pytestmark = pytest.mark.ut


def test_health_is_liveness_only() -> None:
    assert health() == {"status": "ok"}


def test_network_sockets_are_disabled() -> None:
    with pytest.raises(SocketBlockedError):
        socket.socket()
