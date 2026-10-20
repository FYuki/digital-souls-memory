"""Derived projections, evidence references, and durable extraction notifications."""

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(r"""
        CREATE TABLE derived_memories (
            character_id TEXT NOT NULL CHECK (character_id ~ '\S'),
            id TEXT NOT NULL CHECK (id ~ '\S'),
            kind TEXT NOT NULL CHECK (kind IN
                ('fact','preference','relationship','commitment','summary','key')),
            content TEXT NOT NULL CHECK (content ~ '\S'),
            model TEXT NOT NULL CHECK (model ~ '\S'),
            prompt_version TEXT NOT NULL CHECK (prompt_version ~ '\S'),
            created_at TIMESTAMPTZ NOT NULL,
            valid_from TIMESTAMPTZ NOT NULL,
            valid_to TIMESTAMPTZ,
            recorded_at TIMESTAMPTZ NOT NULL,
            superseded_at TIMESTAMPTZ,
            last_reinforced_at TIMESTAMPTZ,
            epistemics JSONB NOT NULL CHECK (jsonb_typeof(epistemics)='object'),
            identity_links JSONB NOT NULL CHECK (jsonb_typeof(identity_links)='object'),
            subject TEXT, predicate TEXT, object TEXT,
            shared_with TEXT[] NOT NULL,
            counterpart TEXT, affection DOUBLE PRECISION, trust DOUBLE PRECISION, status TEXT,
            deadline TIMESTAMPTZ,
            verbatim_turn_id TEXT REFERENCES turns(turn_id),
            PRIMARY KEY(character_id,id),
            CHECK (valid_to IS NULL OR valid_to >= valid_from),
            CHECK (kind NOT IN ('fact','preference') OR
                (subject IS NOT NULL AND predicate IS NOT NULL AND object IS NOT NULL)),
            CHECK (kind <> 'relationship' OR (counterpart IS NOT NULL AND status IS NOT NULL
                AND affection BETWEEN 0 AND 1 AND trust BETWEEN 0 AND 1
                AND affection IS NOT NULL AND trust IS NOT NULL)),
            CHECK (kind <> 'commitment' OR (verbatim_turn_id IS NOT NULL AND last_reinforced_at IS
                NULL))
        );
        CREATE TABLE memory_sources (
            character_id TEXT NOT NULL, memory_id TEXT NOT NULL,
            turn_id TEXT NOT NULL REFERENCES turns(turn_id),
            position INTEGER NOT NULL CHECK (position >= 0),
            PRIMARY KEY(character_id,memory_id,turn_id),
            UNIQUE(character_id,memory_id,position),
            FOREIGN KEY(character_id,memory_id) REFERENCES derived_memories(character_id,id) ON
                DELETE CASCADE
        );
        CREATE TABLE memory_relations (
            character_id TEXT NOT NULL, from_id TEXT NOT NULL, to_id TEXT NOT NULL,
            type TEXT NOT NULL CHECK (type IN ('updates','extends','derives','contradicts')),
            PRIMARY KEY(character_id,from_id,to_id,type),
            FOREIGN KEY(character_id,from_id) REFERENCES derived_memories(character_id,id),
            FOREIGN KEY(character_id,to_id) REFERENCES derived_memories(character_id,id),
            CHECK (from_id <> to_id)
        );
        CREATE TABLE memory_notifications (
            turn_id TEXT PRIMARY KEY REFERENCES turns(turn_id),
            version BIGINT NOT NULL DEFAULT 1
        );
        CREATE TABLE memory_extractions (
            character_id TEXT NOT NULL, turn_id TEXT NOT NULL REFERENCES turns(turn_id),
            delivery_basis TEXT NOT NULL CHECK (delivery_basis IN
                ('declared_full','pending','receipt_confirmed','assumed_full','original')),
            signature TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('pending','running','done','failed','retry')),
            token TEXT, lease_until TIMESTAMPTZ, deadline TIMESTAMPTZ NOT NULL, error TEXT,
            used_text TEXT, receipt_version INTEGER NOT NULL,
            PRIMARY KEY(character_id,turn_id)
        );
        CREATE TABLE memory_candidates (
            character_id TEXT NOT NULL, turn_id TEXT NOT NULL,
            memory_id TEXT NOT NULL, payload JSONB NOT NULL,
            PRIMARY KEY(character_id,memory_id),
            FOREIGN KEY(character_id,turn_id) REFERENCES memory_extractions(character_id,turn_id)
        );
        CREATE TABLE memory_generated (
            character_id TEXT NOT NULL, memory_id TEXT NOT NULL, turn_id TEXT NOT NULL REFERENCES
                turns(turn_id),
            payload JSONB NOT NULL,
            PRIMARY KEY(character_id,memory_id)
        );
        CREATE TABLE memory_baselines (
            character_id TEXT NOT NULL, memory_id TEXT NOT NULL, payload JSONB NOT NULL,
            PRIMARY KEY(character_id,memory_id),
            FOREIGN KEY(character_id,memory_id) REFERENCES derived_memories(character_id,id)
        );
        CREATE TABLE memory_candidate_relations (
            character_id TEXT NOT NULL, turn_id TEXT NOT NULL,
            from_id TEXT NOT NULL, to_id TEXT NOT NULL,
            type TEXT NOT NULL CHECK (type IN ('updates','extends','derives','contradicts')),
            PRIMARY KEY(character_id,turn_id,from_id,to_id,type),
            FOREIGN KEY(character_id,turn_id) REFERENCES memory_extractions(character_id,turn_id)
        );
        CREATE TABLE memory_surfaces (
            surface TEXT PRIMARY KEY, token TEXT, lease_until TIMESTAMPTZ
        );
        CREATE FUNCTION notify_memory() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            INSERT INTO memory_notifications(turn_id) VALUES(NEW.turn_id)
            ON CONFLICT(turn_id) DO UPDATE SET version=memory_notifications.version+1;
            RETURN NEW;
        END; $$;
        CREATE TRIGGER memory_turn_notify AFTER INSERT ON turns FOR EACH ROW EXECUTE FUNCTION
            notify_memory();
        CREATE TRIGGER memory_receipt_notify AFTER INSERT ON delivery_receipts FOR EACH ROW
            EXECUTE FUNCTION notify_memory();
        INSERT INTO memory_notifications(turn_id) SELECT turn_id FROM turns;
        CREATE FUNCTION validate_one_memory_sources(owner_id TEXT, mid TEXT)
            RETURNS void LANGUAGE plpgsql AS $$
        DECLARE original_id TEXT;
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM derived_memories WHERE character_id=owner_id AND id=mid)
                THEN
                RETURN;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM memory_sources WHERE character_id=owner_id AND
                memory_id=mid) THEN
                RAISE EXCEPTION 'Memory requires evidence' USING ERRCODE='23514';
            END IF;
            IF EXISTS (SELECT 1 FROM memory_sources s JOIN turns t USING(turn_id)
                WHERE s.character_id=owner_id AND s.memory_id=mid
                AND (NOT owner_id=ANY(t.perceived_by) OR (t.kind='thought' AND
                    t.actor<>owner_id))) THEN
                RAISE EXCEPTION 'Evidence outside perception' USING ERRCODE='23514';
            END IF;
            SELECT verbatim_turn_id INTO original_id FROM derived_memories
                WHERE character_id=owner_id AND id=mid;
            IF original_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM memory_sources
                WHERE character_id=owner_id AND memory_id=mid AND turn_id=original_id) THEN
                RAISE EXCEPTION 'Original must belong to evidence' USING ERRCODE='23514';
            END IF;
        END; $$;
        CREATE FUNCTION validate_memory_sources() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE identifier_field TEXT;
        BEGIN
            identifier_field := CASE WHEN TG_TABLE_NAME='derived_memories'
                THEN 'id' ELSE 'memory_id' END;
            IF TG_OP <> 'DELETE' THEN
                PERFORM validate_one_memory_sources(NEW.character_id,
                    to_jsonb(NEW)->>identifier_field);
            END IF;
            IF TG_OP <> 'INSERT' THEN
                PERFORM validate_one_memory_sources(OLD.character_id,
                    to_jsonb(OLD)->>identifier_field);
            END IF;
            RETURN NULL;
        END; $$;
        CREATE CONSTRAINT TRIGGER memory_requires_sources AFTER INSERT OR UPDATE ON derived_memories
            DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_memory_sources();
        CREATE CONSTRAINT TRIGGER memory_source_validation AFTER INSERT OR UPDATE OR DELETE ON
            memory_sources
            DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_memory_sources();
    """)


def downgrade() -> None:
    op.execute("""
        DROP TRIGGER memory_receipt_notify ON delivery_receipts;
        DROP TRIGGER memory_turn_notify ON turns;
        DROP FUNCTION notify_memory();
        DROP TABLE memory_candidate_relations, memory_baselines;
        DROP TABLE memory_generated, memory_candidates, memory_extractions, memory_notifications,
            memory_surfaces;
        DROP TABLE memory_relations, memory_sources, derived_memories;
        DROP FUNCTION validate_memory_sources();
        DROP FUNCTION validate_one_memory_sources(TEXT,TEXT);
    """)
