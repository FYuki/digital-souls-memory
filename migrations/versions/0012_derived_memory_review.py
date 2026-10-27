"""Track evaluated context notifications and index baseline input dependencies."""

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE context_work ADD COLUMN evaluated_version BIGINT;
        ALTER TABLE context_work ADD COLUMN delivery_timeout INTERVAL;
        CREATE INDEX memory_baseline_dependency_turn
            ON memory_baseline_dependencies(character_id,turn_id,memory_id);
    """)


def downgrade() -> None:
    op.execute("""
        DROP INDEX memory_baseline_dependency_turn;
        ALTER TABLE context_work DROP COLUMN delivery_timeout;
        ALTER TABLE context_work DROP COLUMN evaluated_version;
    """)
