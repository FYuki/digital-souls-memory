"""Persist actor kinds and owned provision references; collect unused embeddings."""

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE memory_baselines ADD COLUMN temporal_origin JSONB NOT NULL DEFAULT '{}';
        UPDATE memory_baselines SET temporal_origin=jsonb_build_object(
            'valid_to',payload->'valid_to','superseded_at',payload->'superseded_at',
            'last_reinforced_at',payload->'last_reinforced_at');
        CREATE TABLE actor_kinds (
            actor_id TEXT PRIMARY KEY CHECK (actor_id ~ '\\S'),
            kind TEXT NOT NULL CHECK (kind IN ('user','character'))
        );
        INSERT INTO actor_kinds
            SELECT character_id,'character' FROM core_identities
            UNION SELECT character_id,'character' FROM temperaments;
        INSERT INTO actor_kinds SELECT DISTINCT actor,'user' FROM turns
            WHERE NOT EXISTS(SELECT 1 FROM actor_kinds WHERE actor_id=actor);
        CREATE TRIGGER actor_kind_immutable BEFORE UPDATE OR DELETE ON actor_kinds
            FOR EACH ROW EXECUTE FUNCTION reject_identity_mutation();
        CREATE OR REPLACE FUNCTION evidence_is_character(identifier TEXT) RETURNS BOOLEAN
        LANGUAGE SQL STABLE STRICT AS $$
            SELECT EXISTS(SELECT 1 FROM actor_kinds
                          WHERE actor_id=identifier AND kind='character')
        $$;
        CREATE FUNCTION claim_actor_kind() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE identifier TEXT; requested TEXT; previous TEXT;
        BEGIN
            IF TG_TABLE_NAME='turns' THEN identifier:=NEW.actor;
            ELSE identifier:=NEW.character_id; END IF;
            PERFORM pg_advisory_xact_lock(hashtextextended(identifier,0));
            SELECT kind INTO previous FROM actor_kinds WHERE actor_id=identifier;
            IF TG_TABLE_NAME='turns' THEN requested:=coalesce(previous,'user');
            ELSE requested:='character'; END IF;
            IF previous IS NOT NULL AND previous<>requested THEN
                RAISE EXCEPTION 'Actor kind conflict' USING ERRCODE='23514';
            END IF;
            INSERT INTO actor_kinds VALUES(identifier,requested) ON CONFLICT DO NOTHING;
            RETURN NEW;
        END $$;
        CREATE TRIGGER actor_kind BEFORE INSERT ON turns
            FOR EACH ROW EXECUTE FUNCTION claim_actor_kind();
        CREATE TRIGGER actor_kind BEFORE INSERT ON core_identities
            FOR EACH ROW EXECUTE FUNCTION claim_actor_kind();
        CREATE TRIGGER actor_kind BEFORE INSERT ON temperaments
            FOR EACH ROW EXECUTE FUNCTION claim_actor_kind();
        DELETE FROM recall_embeddings e WHERE NOT EXISTS (
            SELECT 1 FROM recall_documents d WHERE d.content_digest=e.content_digest);
    """)
    op.execute("""
        DO $$
        DECLARE record RECORD; kind TEXT; field TEXT; identifier TEXT;
                owners TEXT[]; refs JSONB; owner_id TEXT;
        BEGIN
            FOR record IN SELECT provision_id,payload FROM provision_records LOOP
                refs:='[]'::jsonb;
                FOREACH kind IN ARRAY ARRAY['memory','anchor','voice'] LOOP
                    field:=CASE kind WHEN 'memory' THEN 'memory_ids'
                        WHEN 'anchor' THEN 'verbatim_anchor_ids' ELSE 'voice_exemplar_ids' END;
                    FOR identifier IN SELECT jsonb_array_elements_text(record.payload->field) LOOP
                        IF record.payload->>'kind'='turn' AND kind<>'voice' THEN
                            SELECT array_agg(DISTINCT s->'ref'->>'owner_id') INTO owners
                            FROM jsonb_array_elements(record.payload->'scores') s
                            WHERE s->'ref'->>(CASE kind WHEN 'memory' THEN 'memory_id'
                                ELSE 'anchor_id' END)=identifier;
                        ELSIF kind='memory' THEN
                            SELECT array_agg(DISTINCT p.owner_id) INTO owners
                            FROM profile_entries p
                            WHERE p.character_id=record.payload->>'character_id'
                                AND p.memory_id=identifier AND p.entry_id IN
                                (SELECT jsonb_array_elements_text(
                                    record.payload->'profile_entry_ids'));
                        ELSIF kind='voice' THEN
                            SELECT array_agg(DISTINCT v.character_id) INTO owners
                            FROM voice_exemplars v
                            WHERE v.character_id=record.payload->>'character_id'
                                AND v.exemplar_id=identifier;
                        ELSE owners:=NULL;
                        END IF;
                        IF coalesce(cardinality(owners),0)<>1 OR owners[1] IS NULL THEN
                            RAISE EXCEPTION 'Cannot determine provision reference owner';
                        END IF;
                        owner_id:=owners[1];
                        refs:=refs || jsonb_build_array(jsonb_build_object(
                            'kind',kind,'owner_id',owner_id,'identifier',identifier));
                    END LOOP;
                END LOOP;
                UPDATE provision_records
                    SET payload=payload || jsonb_build_object('provided_refs',refs)
                    WHERE provision_id=record.provision_id;
            END LOOP;
        END $$;
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE memory_baselines DROP COLUMN temporal_origin;
        UPDATE provision_records SET payload=payload-'provided_refs';
        DROP TRIGGER actor_kind ON turns;
        DROP TRIGGER actor_kind ON core_identities;
        DROP TRIGGER actor_kind ON temperaments;
        DROP FUNCTION claim_actor_kind();
        CREATE OR REPLACE FUNCTION evidence_is_character(identifier TEXT) RETURNS BOOLEAN
        LANGUAGE SQL STABLE STRICT AS $$
            SELECT EXISTS(SELECT 1 FROM core_identities WHERE character_id=identifier)
                OR EXISTS(SELECT 1 FROM temperaments WHERE character_id=identifier)
        $$;
        DROP TABLE actor_kinds;
    """)
