"""Persistent turn recall documents, input dependencies and committed provisions."""

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("""
        CREATE EXTENSION IF NOT EXISTS pg_bigm;
        ALTER TABLE derived_memories ADD COLUMN search_eligible BOOLEAN NOT NULL DEFAULT FALSE;
        CREATE TABLE memory_input_dependencies (
            character_id TEXT NOT NULL, extraction_turn_id TEXT NOT NULL,
            turn_id TEXT NOT NULL REFERENCES turns(turn_id),
            basis TEXT NOT NULL, receipt_version INTEGER NOT NULL,
            available_end INTEGER NOT NULL, used_end INTEGER NOT NULL,
            deadline TIMESTAMPTZ NOT NULL,
            PRIMARY KEY(character_id,extraction_turn_id,turn_id)
        );
        CREATE INDEX memory_input_turn ON memory_input_dependencies(turn_id);
        CREATE TABLE recall_documents (
            owner_id TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('memory','anchor')),
            identifier TEXT NOT NULL, version BIGINT NOT NULL DEFAULT 1,
            ready BOOLEAN NOT NULL DEFAULT FALSE, pending BOOLEAN NOT NULL DEFAULT TRUE,
            not_before TIMESTAMPTZ, claim_token TEXT, lease_until TIMESTAMPTZ,
            search_text TEXT NOT NULL DEFAULT '', content_digest TEXT,
            model TEXT, revision TEXT, digest TEXT, dimensions INTEGER,
            embedding vector(1024), payload JSONB, dependencies JSONB, labels JSONB,
            PRIMARY KEY(owner_id,kind,identifier)
        );
        CREATE INDEX recall_work ON recall_documents(pending,not_before,lease_until);
        CREATE INDEX recall_vector ON recall_documents USING hnsw(embedding vector_cosine_ops);
        CREATE INDEX recall_lexical ON recall_documents USING gin(search_text gin_bigm_ops);
        CREATE TABLE recall_document_sources (
            owner_id TEXT NOT NULL, kind TEXT NOT NULL, identifier TEXT NOT NULL,
            turn_id TEXT NOT NULL REFERENCES turns(turn_id),
            PRIMARY KEY(owner_id,kind,identifier,turn_id),
            FOREIGN KEY(owner_id,kind,identifier) REFERENCES
                recall_documents(owner_id,kind,identifier) ON DELETE CASCADE
        );
        CREATE INDEX recall_source_turn ON recall_document_sources(turn_id);
        CREATE TABLE recall_embeddings (
            content_digest TEXT NOT NULL, model TEXT NOT NULL, revision TEXT NOT NULL,
            digest TEXT NOT NULL, dimensions INTEGER NOT NULL, embedding vector(1024) NOT NULL,
            PRIMARY KEY(content_digest,model,revision,digest,dimensions)
        );
        CREATE TABLE session_identity_selections (
            session_id TEXT NOT NULL REFERENCES sessions(session_id), character_id TEXT NOT NULL,
            temperament_version INTEGER, personality_version INTEGER,
            PRIMARY KEY(session_id,character_id)
        );
        CREATE TABLE provision_records (
            provision_id TEXT PRIMARY KEY, payload JSONB NOT NULL
        );
        CREATE FUNCTION lock_recall_state() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM pg_advisory_xact_lock(hashtextextended('recall-state',0));
            RETURN NULL;
        END; $$;
        CREATE TRIGGER recall_memory_lock BEFORE INSERT OR UPDATE OR DELETE ON derived_memories
            FOR EACH STATEMENT EXECUTE FUNCTION lock_recall_state();
        CREATE TRIGGER recall_sources_lock BEFORE INSERT OR UPDATE OR DELETE ON memory_sources
            FOR EACH STATEMENT EXECUTE FUNCTION lock_recall_state();
        CREATE TRIGGER recall_inputs_lock BEFORE INSERT OR UPDATE OR DELETE
            ON memory_input_dependencies
            FOR EACH STATEMENT EXECUTE FUNCTION lock_recall_state();
        CREATE TRIGGER recall_receipt_lock BEFORE INSERT ON delivery_receipts
            FOR EACH STATEMENT EXECUTE FUNCTION lock_recall_state();
        CREATE TRIGGER recall_anchor_lock BEFORE INSERT ON verbatim_anchors
            FOR EACH STATEMENT EXECUTE FUNCTION lock_recall_state();
        CREATE FUNCTION queue_recall_document(oid TEXT, k TEXT, ident TEXT)
            RETURNS void LANGUAGE plpgsql AS $$
        BEGIN
            INSERT INTO recall_documents(owner_id,kind,identifier) VALUES(oid,k,ident)
            ON CONFLICT(owner_id,kind,identifier) DO UPDATE SET
                version=recall_documents.version+1, ready=FALSE,pending=TRUE,
                claim_token=NULL,lease_until=NULL,not_before=NULL;
        END; $$;
        CREATE FUNCTION notify_recall_memory() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP='DELETE' THEN
                PERFORM queue_recall_document(OLD.character_id,'memory',OLD.memory_id);
            ELSIF TG_TABLE_NAME='derived_memories' THEN
                PERFORM queue_recall_document(NEW.character_id,'memory',NEW.id);
            ELSE
                PERFORM queue_recall_document(NEW.character_id,'memory',NEW.memory_id);
            END IF;
            RETURN NULL;
        END; $$;
        CREATE TRIGGER recall_memory_notify AFTER INSERT OR UPDATE OF content,valid_from,valid_to,
            superseded_at,last_reinforced_at,epistemics,identity_links,shared_with
            ON derived_memories FOR EACH ROW EXECUTE FUNCTION notify_recall_memory();
        CREATE TRIGGER recall_sources_notify AFTER INSERT OR DELETE ON memory_sources
            FOR EACH ROW EXECUTE FUNCTION notify_recall_memory();
        CREATE FUNCTION notify_recall_anchor() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM queue_recall_document(NEW.character_id,'anchor',NEW.anchor_id);
            RETURN NULL;
        END; $$;
        CREATE TRIGGER recall_anchor_notify AFTER INSERT ON verbatim_anchors
            FOR EACH ROW EXECUTE FUNCTION notify_recall_anchor();
        CREATE FUNCTION withdraw_recall_receipt() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            UPDATE derived_memories m SET search_eligible=FALSE
            WHERE EXISTS (SELECT 1 FROM memory_sources s JOIN turns t USING(turn_id)
                WHERE s.character_id=m.character_id AND s.memory_id=m.id
                AND s.turn_id=NEW.turn_id AND t.actor<>m.character_id)
            OR EXISTS (SELECT 1 FROM memory_input_dependencies d JOIN turns t USING(turn_id)
                WHERE d.character_id=m.character_id AND d.turn_id=NEW.turn_id
                AND t.actor<>m.character_id AND EXISTS (
                    SELECT 1 FROM memory_candidates c WHERE c.character_id=d.character_id
                    AND c.turn_id=d.extraction_turn_id AND (c.memory_id=m.id OR EXISTS (
                        SELECT 1 FROM memory_relations r WHERE r.character_id=m.character_id
                        AND r.from_id=c.memory_id AND r.to_id=m.id))));
            UPDATE recall_documents doc SET ready=FALSE,pending=TRUE,version=version+1,
                claim_token=NULL,lease_until=NULL,not_before=NULL
            WHERE EXISTS (SELECT 1 FROM recall_document_sources s JOIN turns t USING(turn_id)
                WHERE s.owner_id=doc.owner_id AND s.kind=doc.kind AND s.identifier=doc.identifier
                AND s.turn_id=NEW.turn_id AND t.actor<>doc.owner_id);
            INSERT INTO memory_notifications(turn_id)
                SELECT DISTINCT d.extraction_turn_id FROM memory_input_dependencies d
                JOIN turns t USING(turn_id) WHERE d.turn_id=NEW.turn_id AND t.actor<>d.character_id
            ON CONFLICT(turn_id) DO UPDATE SET version=memory_notifications.version+1;
            RETURN NULL;
        END; $$;
        CREATE TRIGGER recall_receipt_withdraw AFTER INSERT ON delivery_receipts
            FOR EACH ROW EXECUTE FUNCTION withdraw_recall_receipt();
        INSERT INTO recall_documents(owner_id,kind,identifier)
            SELECT character_id,'memory',id FROM derived_memories;
        INSERT INTO recall_documents(owner_id,kind,identifier)
            SELECT character_id,'anchor',anchor_id FROM verbatim_anchors;
        UPDATE derived_memories m SET search_eligible=TRUE WHERE NOT EXISTS (
            SELECT 1 FROM memory_generated g
            WHERE g.character_id=m.character_id AND g.memory_id=m.id);
        UPDATE memory_extractions SET signature='',status='retry' WHERE status='done';
    """)


def downgrade() -> None:
    op.execute("""
        DROP TRIGGER recall_receipt_withdraw ON delivery_receipts;
        DROP TRIGGER recall_anchor_notify ON verbatim_anchors;
        DROP TRIGGER recall_sources_notify ON memory_sources;
        DROP TRIGGER recall_memory_notify ON derived_memories;
        DROP TRIGGER recall_anchor_lock ON verbatim_anchors;
        DROP TRIGGER recall_receipt_lock ON delivery_receipts;
        DROP TRIGGER recall_inputs_lock ON memory_input_dependencies;
        DROP TRIGGER recall_sources_lock ON memory_sources;
        DROP TRIGGER recall_memory_lock ON derived_memories;
        DROP FUNCTION withdraw_recall_receipt(),notify_recall_anchor(),notify_recall_memory();
        DROP FUNCTION queue_recall_document(TEXT,TEXT,TEXT),lock_recall_state();
        DROP TABLE provision_records,session_identity_selections,recall_embeddings;
        DROP TABLE recall_document_sources,recall_documents,memory_input_dependencies;
        ALTER TABLE derived_memories DROP COLUMN search_eligible;
    """)
