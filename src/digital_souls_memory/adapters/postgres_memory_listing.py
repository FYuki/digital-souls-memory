"""Read all projection and evidence inputs in one request-owned PostgreSQL snapshot."""

from datetime import datetime, timedelta

from digital_souls_memory.adapters.postgres_connections import PostgresConnections
from digital_souls_memory.adapters.postgres_memory import _MemoryTransaction
from digital_souls_memory.adapters.postgres_projection_evidence import read_projection_evidence
from digital_souls_memory.evidence.provision import ProvisionUnavailable
from digital_souls_memory.memory.ports import MemoryUnavailable
from digital_souls_memory.memory.projection import (
    ProjectionEvidence,
    ProjectionInput,
    projection_input,
)


class PostgresMemoryListing:
    def __init__(
        self,
        url: str,
        *,
        delivery_timeout: timedelta,
        conversation_surfaces: frozenset[str],
        comment_surfaces: frozenset[str],
    ) -> None:
        self.url = url
        self.delivery_timeout = delivery_timeout
        self.conversation_surfaces = conversation_surfaces
        self.comment_surfaces = comment_surfaces

    def read(self, character_id: str, now: datetime) -> tuple[ProjectionInput, ProjectionEvidence]:
        connections = PostgresConnections(self.url)
        try:
            with (
                connections.request(),
                connections.snapshot(),
                connections.connection() as connection,
            ):
                snapshot = projection_input(
                    _MemoryTransaction(connection, character_id, read_only=True)
                )
                evidence = read_projection_evidence(
                    connection,
                    snapshot,
                    character_id,
                    now,
                    delivery_timeout=self.delivery_timeout,
                    conversation_surfaces=self.conversation_surfaces,
                    comment_surfaces=self.comment_surfaces,
                )
            return snapshot, evidence
        except ProvisionUnavailable as error:
            raise MemoryUnavailable() from error
