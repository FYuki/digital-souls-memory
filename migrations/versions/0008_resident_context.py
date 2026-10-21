"""Resident profiles, short-lived state, episodes, work ownership and session endings."""

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE session_endings (
            session_id TEXT PRIMARY KEY REFERENCES sessions(session_id),
            ended_at TIMESTAMPTZ NOT NULL,
            reported_by TEXT NOT NULL CHECK(length(btrim(reported_by))>0),
            reported_at TIMESTAMPTZ NOT NULL
        );
        CREATE FUNCTION reject_ending_change() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'Session endings are append only'; END; $$;
        CREATE TRIGGER immutable_ending BEFORE UPDATE OR DELETE ON session_endings
            FOR EACH ROW EXECUTE FUNCTION reject_ending_change();
        CREATE TRIGGER context_ending_lock BEFORE INSERT ON session_endings
            FOR EACH STATEMENT EXECUTE FUNCTION lock_recall_state();
        CREATE TRIGGER context_turn_lock BEFORE INSERT ON turns
            FOR EACH STATEMENT EXECUTE FUNCTION lock_recall_state();
        CREATE TABLE character_profiles (
            character_id TEXT PRIMARY KEY, version BIGINT NOT NULL CHECK(version>0),
            token_limit INTEGER NOT NULL CHECK(token_limit>=0)
        );
        CREATE TRIGGER context_profile_lock BEFORE INSERT OR UPDATE OR DELETE ON character_profiles
            FOR EACH STATEMENT EXECUTE FUNCTION lock_recall_state();
        CREATE TABLE profile_entries (
        character_id TEXT NOT NULL REFERENCES character_profiles(character_id) ON DELETE
        CASCADE,
        entry_id TEXT NOT NULL CHECK(length(btrim(entry_id))>0), position INTEGER NOT NULL
        CHECK(position>=0),
            kind TEXT NOT NULL CHECK(kind IN ('turn','memory')),
            turn_id TEXT REFERENCES turns(turn_id), "start" INTEGER, "end" INTEGER,
            owner_id TEXT, memory_id TEXT,
            PRIMARY KEY(character_id,entry_id), UNIQUE(character_id,position),
            FOREIGN KEY(owner_id,memory_id) REFERENCES derived_memories(character_id,id),
            CHECK((kind='turn' AND turn_id IS NOT NULL AND "start">=0 AND "end">"start"
        AND "start" IS NOT NULL AND "end" IS NOT NULL AND owner_id IS NULL AND memory_id IS
        NULL)
               OR (kind='memory' AND owner_id IS NOT NULL AND memory_id IS NOT NULL
                    AND turn_id IS NULL AND "start" IS NULL AND "end" IS NULL))
        );
        CREATE FUNCTION check_profile_span() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.kind='turn' AND NOT EXISTS(SELECT 1 FROM turns t WHERE t.turn_id=NEW.turn_id
                AND NEW."end"<=char_length(t.text) AND NEW.character_id=ANY(t.perceived_by)) THEN
                RAISE EXCEPTION 'Invalid profile source span' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END; $$;
        CREATE TRIGGER profile_span_check BEFORE INSERT OR UPDATE ON profile_entries
            FOR EACH ROW EXECUTE FUNCTION check_profile_span();
        CREATE TABLE character_states (
            character_id TEXT NOT NULL, session_id TEXT NOT NULL REFERENCES sessions(session_id),
            surface TEXT NOT NULL, mood TEXT, relationship_temperature TEXT, current_topic TEXT,
            updated_at TIMESTAMPTZ NOT NULL, expires_at TIMESTAMPTZ NOT NULL,
            input_signature TEXT NOT NULL, dependencies JSONB NOT NULL,
            PRIMARY KEY(character_id,session_id,surface), CHECK(expires_at>updated_at)
        );
        CREATE TABLE episodes (
        episode_id TEXT PRIMARY KEY CHECK(length(btrim(episode_id))>0), character_id TEXT NOT
        NULL,
            session_id TEXT NOT NULL REFERENCES sessions(session_id), surface TEXT NOT NULL,
            topic TEXT NOT NULL CHECK(length(btrim(topic))>0),
        time_start TIMESTAMPTZ NOT NULL, time_end TIMESTAMPTZ NOT NULL
        CHECK(time_end>=time_start),
            model TEXT NOT NULL CHECK(length(btrim(model))>0),
            prompt_version TEXT NOT NULL CHECK(length(btrim(prompt_version))>0),
        created_at TIMESTAMPTZ NOT NULL, input_signature TEXT NOT NULL, dependencies JSONB
        NOT NULL
        );
        CREATE INDEX episodes_window ON episodes(character_id,session_id,surface);
        CREATE TABLE episode_spans (
            episode_id TEXT NOT NULL REFERENCES episodes(episode_id) ON DELETE CASCADE,
        position INTEGER NOT NULL CHECK(position>=0), turn_id TEXT NOT NULL REFERENCES
        turns(turn_id),
            "start" INTEGER NOT NULL CHECK("start">=0), "end" INTEGER NOT NULL CHECK("end">"start"),
            PRIMARY KEY(episode_id,position)
        );
        CREATE FUNCTION check_episode_span() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS(SELECT 1 FROM turns t JOIN episodes e ON e.episode_id=NEW.episode_id
                WHERE t.turn_id=NEW.turn_id AND t.session_id=e.session_id AND t.surface=e.surface
                AND e.character_id=ANY(t.perceived_by) AND NEW."end"<=char_length(t.text)) THEN
                RAISE EXCEPTION 'Invalid episode span' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END; $$;
        CREATE TRIGGER episode_span_check BEFORE INSERT OR UPDATE ON episode_spans
            FOR EACH ROW EXECUTE FUNCTION check_episode_span();
        CREATE FUNCTION check_episode_has_spans() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE ident TEXT;
        BEGIN
            ident := CASE WHEN TG_OP='DELETE' THEN OLD.episode_id ELSE NEW.episode_id END;
            IF EXISTS(SELECT 1 FROM episodes WHERE episode_id=ident) AND
               NOT EXISTS(SELECT 1 FROM episode_spans WHERE episode_id=ident) THEN
                RAISE EXCEPTION 'Episode requires spans' USING ERRCODE='23514';
            END IF;
            RETURN NULL;
        END; $$;
        CREATE CONSTRAINT TRIGGER episode_nonempty AFTER INSERT OR UPDATE ON episodes
            DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_episode_has_spans();
        CREATE CONSTRAINT TRIGGER episode_spans_nonempty AFTER DELETE OR UPDATE ON episode_spans
            DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_episode_has_spans();
        CREATE TABLE context_notifications (
            character_id TEXT NOT NULL, session_id TEXT NOT NULL REFERENCES sessions(session_id),
            surface TEXT NOT NULL, version BIGINT NOT NULL DEFAULT 1,
            PRIMARY KEY(character_id,session_id,surface)
        );
        CREATE TABLE context_work (
            operation TEXT NOT NULL CHECK(operation IN ('state','episodes')),
        character_id TEXT NOT NULL, session_id TEXT NOT NULL REFERENCES sessions(session_id),
        surface TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pending','running','done','failed','retry')),
            signature TEXT, completed_signature TEXT, token TEXT, lease_until TIMESTAMPTZ,
            retry_at TIMESTAMPTZ, next_due_at TIMESTAMPTZ, error TEXT,
            PRIMARY KEY(operation,character_id,session_id,surface)
        );
        CREATE FUNCTION notify_context() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_TABLE_NAME='session_endings' THEN
                UPDATE context_notifications SET version=version+1 WHERE session_id=NEW.session_id;
            ELSE
                INSERT INTO context_notifications(character_id,session_id,surface)
        SELECT unnest(t.perceived_by),t.session_id,t.surface FROM turns t WHERE
        t.turn_id=NEW.turn_id
        ON CONFLICT(character_id,session_id,surface) DO UPDATE SET
        version=context_notifications.version+1;
            END IF;
            RETURN NULL;
        END; $$;
        CREATE TRIGGER context_turn_notify AFTER INSERT ON turns FOR EACH ROW EXECUTE
        FUNCTION notify_context();
        CREATE TRIGGER context_receipt_notify AFTER INSERT ON delivery_receipts FOR EACH ROW
        EXECUTE FUNCTION notify_context();
        CREATE TRIGGER context_ending_notify AFTER INSERT ON session_endings FOR EACH ROW
        EXECUTE FUNCTION notify_context();
        INSERT INTO context_notifications(character_id,session_id,surface)
            SELECT DISTINCT unnest(perceived_by),session_id,surface FROM turns;
    """)


def downgrade() -> None:
    op.execute("""
        DROP TRIGGER context_ending_notify ON session_endings;
        DROP TRIGGER context_receipt_notify ON delivery_receipts;
        DROP TRIGGER context_turn_notify ON turns;
        DROP TRIGGER context_turn_lock ON turns;
        DROP FUNCTION notify_context();
        DROP TABLE context_work,context_notifications,episode_spans,episodes,character_states,
            profile_entries,character_profiles,session_endings;
        DROP FUNCTION
        check_episode_has_spans(),check_episode_span(),check_profile_span(),reject_ending_change();
    """)
