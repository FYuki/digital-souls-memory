"""Normalize derived provenance and restrict physical deletion to a dedicated role."""

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | None = None
depends_on: str | None = None

COPIES = (
    ("memory_candidates", "memory_candidate_sources"),
    ("memory_generated", "memory_generated_sources"),
    ("memory_baselines", "memory_baseline_sources"),
)


def upgrade() -> None:
    op.execute("""
        ALTER TABLE memory_extractions ADD COLUMN text_hash TEXT;
        ALTER TABLE memory_extractions ADD COLUMN used_end INTEGER;
        UPDATE memory_extractions SET
            text_hash=encode(sha256(convert_to(used_text,'UTF8')),'hex'),
            used_end=char_length(used_text);
        ALTER TABLE memory_extractions DROP COLUMN used_text;
        ALTER TABLE memory_extractions ADD CONSTRAINT extraction_hash_range CHECK (
            (text_hash IS NULL AND used_end IS NULL) OR
            (text_hash IS NOT NULL AND used_end IS NOT NULL
                AND text_hash ~ '^[0-9a-f]{64}$' AND used_end >= 0));
    """)
    for parent, sources in COPIES:
        op.execute(f"""
            CREATE TABLE {sources} (
                character_id TEXT NOT NULL, memory_id TEXT NOT NULL,
                turn_id TEXT NOT NULL REFERENCES turns(turn_id),
                position INTEGER NOT NULL CHECK(position>=0),
                PRIMARY KEY(character_id,memory_id,turn_id),
                UNIQUE(character_id,memory_id,position),
                FOREIGN KEY(character_id,memory_id) REFERENCES
                    {parent}(character_id,memory_id) ON DELETE CASCADE
            );
            CREATE INDEX {sources}_turn ON {sources}(turn_id);
            INSERT INTO {sources}(character_id,memory_id,turn_id,position)
                SELECT p.character_id,p.memory_id,s.identifier,s.position-1
                FROM {parent} p CROSS JOIN LATERAL
                jsonb_array_elements_text(p.payload->'source_turn_ids')
                    WITH ORDINALITY s(identifier,position);
            UPDATE {parent} SET payload=payload-'source_turn_ids';
        """)
    op.execute("""
        CREATE TABLE memory_baseline_dependencies (
            character_id TEXT NOT NULL, memory_id TEXT NOT NULL,
            turn_id TEXT NOT NULL REFERENCES turns(turn_id),
            basis TEXT NOT NULL, receipt_version INTEGER NOT NULL,
            available_end INTEGER NOT NULL, used_end INTEGER NOT NULL,
            deadline TIMESTAMPTZ NOT NULL,
            PRIMARY KEY(character_id,memory_id,turn_id),
            FOREIGN KEY(character_id,memory_id) REFERENCES
                memory_baselines(character_id,memory_id) ON DELETE CASCADE
        );
        CREATE TABLE deletion_requests (
            request_id TEXT PRIMARY KEY, command JSONB NOT NULL, requested_by TEXT NOT NULL,
            target_turn_ids TEXT[] NOT NULL, status TEXT NOT NULL
                CHECK(status IN ('running','done','failed')),
            token TEXT, lease_until TIMESTAMPTZ, cascade_result JSONB, error TEXT,
            requested_at TIMESTAMPTZ NOT NULL, completed_at TIMESTAMPTZ
        );
        DO $$ BEGIN
            IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='memory_deletion_owner') THEN
                CREATE ROLE memory_deletion_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
            END IF;
            IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='memory_deletion_executor') THEN
                CREATE ROLE memory_deletion_executor NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
            END IF;
        END; $$;
        CREATE FUNCTION public.guard_deletion_requests() RETURNS trigger
            LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
        BEGIN
            IF NOT pg_has_role(current_user,'memory_deletion_executor','USAGE') AND
                current_user <> (SELECT pg_get_userbyid(relowner) FROM pg_class
                    WHERE oid=TG_RELID) THEN
                RAISE EXCEPTION 'Deletion requests require the deletion role'
                    USING ERRCODE='42501';
            END IF;
            IF TG_OP='DELETE' THEN RETURN OLD; END IF;
            RETURN NEW;
        END; $$;
        CREATE TRIGGER deletion_requests_guard BEFORE INSERT OR UPDATE OR DELETE
            ON public.deletion_requests FOR EACH ROW
            EXECUTE FUNCTION public.guard_deletion_requests();
        GRANT USAGE ON SCHEMA public TO memory_deletion_owner;
        GRANT SELECT,UPDATE ON public.deletion_requests TO memory_deletion_owner;
        GRANT SELECT,UPDATE,DELETE ON public.turns TO memory_deletion_owner;
        GRANT SELECT,DELETE ON public.delivery_receipts,public.verbatim_anchors,
            public.voice_exemplars TO memory_deletion_owner;
        CREATE OR REPLACE FUNCTION public.reject_evidence_mutation() RETURNS trigger
            LANGUAGE plpgsql AS $$
        BEGIN
            IF current_user='memory_deletion_owner' AND TG_TABLE_NAME IN
                ('turns','delivery_receipts') THEN
                IF TG_OP='DELETE' THEN RETURN OLD; END IF;
                IF TG_TABLE_NAME='turns' AND NEW.reply_to_turn_id IS NULL AND
                    (to_jsonb(NEW)-'reply_to_turn_id')=(to_jsonb(OLD)-'reply_to_turn_id')
                    THEN RETURN NEW; END IF;
            END IF;
            RAISE EXCEPTION 'Evidence is append-only' USING ERRCODE='42501';
        END; $$;
        CREATE OR REPLACE FUNCTION public.reject_voice_mutation() RETURNS trigger
            LANGUAGE plpgsql AS $$
        BEGIN
            IF current_user='memory_deletion_owner' AND TG_OP='DELETE' THEN RETURN OLD; END IF;
            RAISE EXCEPTION 'Voice is append-only' USING ERRCODE='42501';
        END; $$;
        CREATE FUNCTION public.apply_author_deletion(request_id TEXT, token TEXT) RETURNS void
            LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        DECLARE targets TEXT[];
        BEGIN
            SELECT r.target_turn_ids INTO targets FROM public.deletion_requests r
                WHERE r.request_id=$1 AND r.token=$2 AND r.status='running' FOR UPDATE;
            IF targets IS NULL THEN
                RAISE EXCEPTION 'Invalid deletion attempt' USING ERRCODE='42501';
            END IF;
            DELETE FROM public.verbatim_anchors WHERE turn_id=ANY(targets);
            DELETE FROM public.voice_exemplars WHERE turn_id=ANY(targets);
            DELETE FROM public.delivery_receipts WHERE turn_id=ANY(targets);
            UPDATE public.turns SET reply_to_turn_id=NULL WHERE reply_to_turn_id=ANY(targets);
            DELETE FROM public.turns WHERE turn_id=ANY(targets);
        END; $$;
        ALTER FUNCTION public.apply_author_deletion(TEXT,TEXT) OWNER TO memory_deletion_owner;
        REVOKE ALL ON FUNCTION public.apply_author_deletion(TEXT,TEXT) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.apply_author_deletion(TEXT,TEXT)
            TO memory_deletion_executor;
    """)


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS(SELECT 1 FROM memory_extractions e LEFT JOIN turns t USING(turn_id)
                WHERE e.text_hash IS NOT NULL AND
                (t.turn_id IS NULL OR e.used_end>char_length(t.text) OR
                 e.text_hash<>encode(sha256(convert_to(left(t.text,e.used_end),'UTF8')),'hex')))
            THEN RAISE EXCEPTION 'Extraction hash mismatch'; END IF;
        END; $$;
        ALTER TABLE memory_extractions ADD COLUMN used_text TEXT;
        UPDATE memory_extractions e SET used_text=CASE WHEN e.text_hash IS NULL THEN NULL
            ELSE left(t.text,e.used_end) END FROM turns t WHERE t.turn_id=e.turn_id;
        ALTER TABLE memory_extractions DROP CONSTRAINT extraction_hash_range;
        ALTER TABLE memory_extractions DROP COLUMN text_hash, DROP COLUMN used_end;
        DROP FUNCTION public.apply_author_deletion(TEXT,TEXT);
        CREATE OR REPLACE FUNCTION public.reject_evidence_mutation() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN
            RAISE EXCEPTION 'Evidence is append-only' USING ERRCODE='42501'; END; $$;
        CREATE OR REPLACE FUNCTION public.reject_voice_mutation() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN
            RAISE EXCEPTION 'Voice is append-only' USING ERRCODE='42501'; END; $$;
        DROP TABLE deletion_requests,memory_baseline_dependencies;
        DROP FUNCTION public.guard_deletion_requests();
    """)
    for parent, sources in reversed(COPIES):
        op.execute(f"""
            UPDATE {parent} p SET payload=p.payload || jsonb_build_object('source_turn_ids',
                (SELECT jsonb_agg(s.turn_id ORDER BY s.position) FROM {sources} s
                WHERE s.character_id=p.character_id AND s.memory_id=p.memory_id));
            DROP TABLE {sources};
        """)
    op.execute("""
        REVOKE ALL ON public.turns,public.delivery_receipts,public.verbatim_anchors,
            public.voice_exemplars FROM memory_deletion_owner;
        REVOKE USAGE ON SCHEMA public FROM memory_deletion_owner;
    """)
