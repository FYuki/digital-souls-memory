"""Separate perception, addressing and replies; synthetic legacy data only."""

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | None = None
depends_on: str | None = None

# Explicit roles of the supported synthetic legacy fixture, never prefix inference.
ID_ROLES = {"char-a": "character", "char-b": "character", "char-c": "character", "viewer": "user"}


def upgrade() -> None:
    connection = op.get_bind()
    identifiers = connection.execute(
        sa.text("""
        SELECT unnest(participants) FROM sessions
        UNION SELECT unnest(audience) FROM turns
        UNION SELECT actor FROM turns
    """)
    ).scalars()
    if any(identifier not in ID_ROLES for identifier in identifiers):
        raise ValueError("Unclassified legacy identifier; synthetic role mapping required")
    op.execute("ALTER TABLE sessions DISABLE TRIGGER evidence_immutable")
    op.execute("ALTER TABLE turns DISABLE TRIGGER evidence_immutable")
    op.execute("ALTER TABLE sessions ADD COLUMN reports_delivery BOOLEAN")
    op.execute("UPDATE sessions SET reports_delivery = true")
    op.execute("ALTER TABLE sessions ALTER COLUMN reports_delivery SET NOT NULL")
    op.execute("""
        ALTER TABLE turns
        ADD COLUMN perceived_by TEXT[],
        ADD COLUMN addressees TEXT[],
        ADD COLUMN address_mode TEXT,
        ADD COLUMN reply_to_turn_id TEXT REFERENCES turns(turn_id)
    """)
    for row in connection.execute(
        sa.text("""
        SELECT t.turn_id, t.actor,
            CASE WHEN cardinality(t.audience) > 0 THEN t.audience ELSE s.participants END AS legacy
        FROM turns t JOIN sessions s USING (session_id)
    """)
    ).mappings():
        perceived = [
            identifier for identifier in row["legacy"] if ID_ROLES[identifier] == "character"
        ]
        if ID_ROLES[row["actor"]] == "character" and row["actor"] not in perceived:
            perceived.append(row["actor"])
        connection.execute(
            sa.text("""
                UPDATE turns SET perceived_by = :perceived, addressees = ARRAY[]::text[],
                    address_mode = 'broadcast' WHERE turn_id = :identifier
            """),
            {"perceived": perceived, "identifier": row["turn_id"]},
        )
    op.execute("""
        CREATE FUNCTION evidence_members_valid(members TEXT[]) RETURNS BOOLEAN
        LANGUAGE SQL IMMUTABLE STRICT AS $$
            SELECT (cardinality(members) = 0 OR
                    (array_ndims(members) = 1 AND array_lower(members, 1) = 1))
                AND NOT EXISTS (SELECT 1 FROM unnest(members) member
                                WHERE member IS NULL OR member !~ '\\S')
                AND cardinality(members) = (
                    SELECT count(DISTINCT member) FROM unnest(members) member)
        $$
    """)
    op.execute("""
        CREATE FUNCTION evidence_is_character(identifier TEXT) RETURNS BOOLEAN
        LANGUAGE SQL STABLE STRICT AS $$
            SELECT EXISTS (SELECT 1 FROM core_identities WHERE character_id = identifier)
                OR EXISTS (SELECT 1 FROM temperaments WHERE character_id = identifier)
        $$
    """)
    op.execute("""
        ALTER TABLE turns
        ALTER COLUMN perceived_by SET NOT NULL,
        ALTER COLUMN addressees SET NOT NULL,
        ALTER COLUMN address_mode SET NOT NULL,
        DROP CONSTRAINT turns_kind_check,
        ADD CONSTRAINT turns_kind_check CHECK (kind IN ('utterance', 'observation', 'thought')),
        ADD CONSTRAINT turns_perceived_by_check CHECK (evidence_members_valid(perceived_by)),
        ADD CONSTRAINT turns_addressees_check CHECK (evidence_members_valid(addressees)),
        ADD CONSTRAINT turns_address_mode_check CHECK (
            (address_mode = 'direct' AND cardinality(addressees) > 0)
            OR (address_mode IN ('broadcast', 'self') AND cardinality(addressees) = 0)),
        ADD CONSTRAINT turns_reply_nonblank CHECK (reply_to_turn_id ~ '\\S')
    """)
    op.execute("""
        CREATE FUNCTION validate_turn_perception() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF EXISTS (SELECT 1 FROM unnest(NEW.perceived_by) identifier
                       WHERE NOT evidence_is_character(identifier))
                OR (evidence_is_character(NEW.actor) AND NOT (NEW.actor = ANY(NEW.perceived_by)))
                OR (NEW.kind = 'thought' AND (NOT evidence_is_character(NEW.actor)
                    OR NEW.perceived_by <> ARRAY[NEW.actor])) THEN
                RAISE EXCEPTION 'Invalid character perception' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$
    """)
    # Existing rows must meet the same registration contract before old columns disappear.
    invalid = connection.execute(
        sa.text("""
        SELECT 1 FROM turns t WHERE
            EXISTS (SELECT 1 FROM unnest(t.perceived_by) identifier
                    WHERE NOT evidence_is_character(identifier))
            OR (evidence_is_character(t.actor) AND NOT (t.actor = ANY(t.perceived_by)))
        LIMIT 1
    """)
    ).first()
    if invalid is not None:
        raise ValueError("Legacy character must be registered before migration")
    op.execute("""
        CREATE TRIGGER turn_perception BEFORE INSERT ON turns
        FOR EACH ROW EXECUTE FUNCTION validate_turn_perception()
    """)
    op.execute("""
        CREATE FUNCTION validate_receipt_eligibility() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF EXISTS (SELECT 1 FROM turns t JOIN sessions s USING (session_id)
                       WHERE t.turn_id = NEW.turn_id
                           AND (NOT s.reports_delivery OR t.kind = 'thought')) THEN
                RAISE EXCEPTION 'Turn does not accept receipts' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER receipt_eligibility BEFORE INSERT ON delivery_receipts
        FOR EACH ROW EXECUTE FUNCTION validate_receipt_eligibility()
    """)
    op.execute("ALTER TABLE turns DROP COLUMN audience")
    op.execute("ALTER TABLE sessions DROP COLUMN participants")
    op.execute("ALTER TABLE sessions ENABLE TRIGGER evidence_immutable")
    op.execute("ALTER TABLE turns ENABLE TRIGGER evidence_immutable")


def downgrade() -> None:
    if op.get_bind().execute(sa.text("SELECT 1 FROM turns WHERE kind = 'thought' LIMIT 1")).first():
        raise ValueError("Cannot downgrade evidence containing thought")
    op.execute("ALTER TABLE sessions DISABLE TRIGGER evidence_immutable")
    op.execute("ALTER TABLE turns DISABLE TRIGGER evidence_immutable")
    op.execute("ALTER TABLE sessions ADD COLUMN participants TEXT[]")
    op.execute("ALTER TABLE turns ADD COLUMN audience TEXT[]")
    op.execute("UPDATE sessions SET participants = ARRAY[]::text[]")
    op.execute("UPDATE turns SET audience = perceived_by")
    op.execute("ALTER TABLE sessions ALTER COLUMN participants SET NOT NULL")
    op.execute("ALTER TABLE turns ALTER COLUMN audience SET NOT NULL")
    op.execute("DROP TRIGGER receipt_eligibility ON delivery_receipts")
    op.execute("DROP FUNCTION validate_receipt_eligibility()")
    op.execute("DROP TRIGGER turn_perception ON turns")
    op.execute("DROP FUNCTION validate_turn_perception()")
    op.execute("""
        ALTER TABLE turns DROP COLUMN perceived_by, DROP COLUMN addressees,
            DROP COLUMN address_mode, DROP COLUMN reply_to_turn_id,
            DROP CONSTRAINT turns_kind_check,
            ADD CONSTRAINT turns_kind_check CHECK (kind IN ('utterance', 'observation'))
    """)
    op.execute("DROP FUNCTION evidence_is_character(TEXT)")
    op.execute("DROP FUNCTION evidence_members_valid(TEXT[])")
    op.execute("ALTER TABLE sessions DROP COLUMN reports_delivery")
    op.execute("ALTER TABLE sessions ENABLE TRIGGER evidence_immutable")
    op.execute("ALTER TABLE turns ENABLE TRIGGER evidence_immutable")
