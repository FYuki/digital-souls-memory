"""Public Core HTTP client, wire models and caller-visible failures."""

from digital_souls_memory.client.http import (
    MemoryClient,
    MemoryHTTPError,
    MemoryResponseError,
    MemoryTimeoutError,
    MemoryTransportError,
)

__all__ = [
    "MemoryClient",
    "MemoryHTTPError",
    "MemoryResponseError",
    "MemoryTimeoutError",
    "MemoryTransportError",
]
