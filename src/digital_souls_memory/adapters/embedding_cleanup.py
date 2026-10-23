"""Collect only digests whose final document reference has ended."""

import psycopg
from psycopg.rows import DictRow


def collect_embeddings(connection: psycopg.Connection[DictRow], digests: list[str]) -> None:
    connection.execute(
        "DELETE FROM recall_embeddings e WHERE content_digest=ANY(%s) AND NOT EXISTS "
        "(SELECT 1 FROM recall_documents d WHERE d.content_digest=e.content_digest)",
        (digests,),
    )
