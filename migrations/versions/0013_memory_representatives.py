"""Persist same-Fact absorption targets separately from historical relations."""

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE memory_representatives (
            character_id TEXT NOT NULL,
            absorbed_id TEXT NOT NULL,
            representative_id TEXT NOT NULL,
            PRIMARY KEY(character_id,absorbed_id),
            CHECK(absorbed_id<>representative_id),
            FOREIGN KEY(character_id,absorbed_id)
                REFERENCES derived_memories(character_id,id) ON DELETE CASCADE,
            FOREIGN KEY(character_id,representative_id)
                REFERENCES derived_memories(character_id,id) ON DELETE CASCADE
        );
        CREATE INDEX memory_representative_target
            ON memory_representatives(character_id,representative_id);
        INSERT INTO memory_representatives
        SELECT r.character_id,r.from_id,r.to_id FROM memory_relations r
        JOIN derived_memories absorbed ON
            (absorbed.character_id,absorbed.id)=(r.character_id,r.from_id)
        JOIN derived_memories representative ON
            (representative.character_id,representative.id)=(r.character_id,r.to_id)
        WHERE r.type='extends' AND absorbed.kind='fact' AND representative.kind='fact'
            AND (absorbed.subject,absorbed.predicate,absorbed.object)=
                (representative.subject,representative.predicate,representative.object);
    """)


def downgrade() -> None:
    op.execute("DROP TABLE memory_representatives")
