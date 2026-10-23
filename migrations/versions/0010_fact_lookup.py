"""Bound Fact candidate lookup without indexing unbounded original attributes."""

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    for table in ("memory_generated", "memory_baselines"):
        op.execute(f"""
            CREATE INDEX {table}_fact_lookup ON {table}
                (character_id, md5(payload->>'subject'), md5(payload->>'predicate'),
                 md5(payload->>'object'))
                WHERE payload->>'kind'='fact'
        """)


def downgrade() -> None:
    for table in ("memory_generated", "memory_baselines"):
        op.execute(f"DROP INDEX {table}_fact_lookup")
