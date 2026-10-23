"""Add immutable identity versions and one initial personality seed."""

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE core_identities (
            character_id TEXT NOT NULL CHECK (character_id ~ '\\S'),
            "values" TEXT[] NOT NULL,
            prohibitions TEXT[] NOT NULL,
            version INTEGER NOT NULL CHECK (version > 0),
            registered_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (character_id, version)
        )
    """)
    op.execute("""
        CREATE TABLE temperaments (
            character_id TEXT NOT NULL CHECK (character_id ~ '\\S'),
            description TEXT NOT NULL CHECK (description ~ '\\S'),
            version INTEGER NOT NULL CHECK (version > 0),
            registered_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (character_id, version)
        )
    """)
    op.execute("""
        CREATE FUNCTION identity_aspects_valid(aspects JSONB) RETURNS BOOLEAN
        LANGUAGE plpgsql IMMUTABLE STRICT AS $$
        DECLARE
            keys TEXT[] := ARRAY['Volatility','Withdrawal','Compassion','Politeness',
                'Industriousness','Orderliness','Enthusiasm','Assertiveness','Intellect','Openness'];
            aspect_key TEXT;
            amount JSONB;
        BEGIN
            IF jsonb_typeof(aspects) <> 'object' THEN RETURN FALSE; END IF;
            IF (SELECT count(*) FROM jsonb_object_keys(aspects)) <> 10 THEN
                RETURN FALSE;
            END IF;
            FOREACH aspect_key IN ARRAY keys LOOP
                amount := aspects -> aspect_key;
                IF amount IS NULL OR jsonb_typeof(amount) <> 'number' THEN
                    RETURN FALSE;
                END IF;
                IF amount::numeric < 0 OR amount::numeric > 1 THEN RETURN FALSE; END IF;
            END LOOP;
            RETURN TRUE;
        END;
        $$
    """)
    op.execute("""
        CREATE TABLE personality_profiles (
            character_id TEXT NOT NULL UNIQUE CHECK (character_id ~ '\\S'),
            aspects JSONB NOT NULL CHECK (identity_aspects_valid(aspects)),
            description TEXT NOT NULL CHECK (description ~ '\\S'),
            renderer_version TEXT NOT NULL CHECK (renderer_version ~ '\\S'),
            temperament_version INTEGER NOT NULL,
            version INTEGER NOT NULL CHECK (version = 1),
            valid_from TIMESTAMPTZ NOT NULL,
            valid_to TIMESTAMPTZ CHECK (valid_to > valid_from),
            evidence_belief_ids TEXT[] NOT NULL CHECK (cardinality(evidence_belief_ids) = 0),
            change_kind TEXT NOT NULL CHECK (change_kind = 'author_seed'),
            PRIMARY KEY (character_id, version),
            FOREIGN KEY (character_id, temperament_version)
                REFERENCES temperaments(character_id, version)
        )
    """)
    op.execute("""
        CREATE FUNCTION reject_identity_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'Identity is append-only' USING ERRCODE = '42501';
        END;
        $$
    """)
    for table in ("core_identities", "temperaments", "personality_profiles"):
        op.execute(
            f"CREATE TRIGGER identity_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_identity_mutation()"
        )


def downgrade() -> None:
    op.execute("DROP TABLE personality_profiles")
    op.execute("DROP TABLE temperaments")
    op.execute("DROP TABLE core_identities")
    op.execute("DROP FUNCTION reject_identity_mutation()")
    op.execute("DROP FUNCTION identity_aspects_valid(JSONB)")
