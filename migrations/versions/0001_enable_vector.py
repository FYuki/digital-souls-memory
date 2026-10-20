"""Enable pgvector; no domain tables are introduced in Phase 0."""

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION vector")


def downgrade() -> None:
    # Refuse to remove dependent objects silently.
    op.execute("DROP EXTENSION vector")
