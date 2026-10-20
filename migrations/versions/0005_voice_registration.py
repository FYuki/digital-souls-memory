"""Persist approved verbatim materials and the completed initial voice check."""

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE voice_exemplars (
            character_id TEXT NOT NULL CHECK (character_id ~ '\\S'),
            exemplar_id TEXT NOT NULL CHECK (exemplar_id ~ '\\S'),
            text TEXT NOT NULL CHECK (text ~ '\\S'),
            source TEXT NOT NULL,
            turn_id TEXT REFERENCES turns(turn_id),
            situation_tags TEXT[] NOT NULL,
            emotion_tags TEXT[] NOT NULL,
            register JSONB NOT NULL CHECK (jsonb_typeof(register) = 'object'),
            trait_version INTEGER NOT NULL,
            approved_by TEXT NOT NULL CHECK (approved_by ~ '\\S'),
            registered_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (character_id, exemplar_id),
            FOREIGN KEY (character_id, trait_version)
                REFERENCES personality_profiles(character_id, version),
            CHECK ((source = 'canonical_seed' AND turn_id IS NULL)
                OR (source = 'turn_id' AND turn_id IS NOT NULL AND turn_id ~ '\\S'))
        )
    """)
    op.execute("""
        CREATE TABLE verbatim_anchors (
            character_id TEXT NOT NULL CHECK (character_id ~ '\\S'),
            anchor_id TEXT NOT NULL CHECK (anchor_id ~ '\\S'),
            turn_id TEXT NOT NULL REFERENCES turns(turn_id),
            kind TEXT NOT NULL CHECK (
                kind IN ('identity_statement', 'commitment', 'nickname', 'anniversary')),
            reason TEXT NOT NULL CHECK (reason ~ '\\S'),
            approved_by TEXT NOT NULL CHECK (approved_by ~ '\\S'),
            registered_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (character_id, anchor_id)
        )
    """)
    op.execute("""
        CREATE TABLE voice_initial_checks (
            character_id TEXT PRIMARY KEY REFERENCES personality_profiles(character_id),
            consistent BOOLEAN NOT NULL,
            reason TEXT NOT NULL
        )
    """)
    op.execute("""
        CREATE FUNCTION reject_voice_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'Voice is append-only' USING ERRCODE = '42501';
        END;
        $$
    """)
    for table in ("voice_exemplars", "verbatim_anchors", "voice_initial_checks"):
        op.execute(
            f"CREATE TRIGGER voice_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_voice_mutation()"
        )


def downgrade() -> None:
    op.execute("DROP TABLE voice_initial_checks")
    op.execute("DROP TABLE verbatim_anchors")
    op.execute("DROP TABLE voice_exemplars")
    op.execute("DROP FUNCTION reject_voice_mutation()")
