"""Replace absorbed Fact identities with reversible source contributions."""

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE memory_source_contributions (
            character_id TEXT NOT NULL,
            fact_id TEXT NOT NULL,
            extraction_turn_id TEXT NOT NULL REFERENCES turns(turn_id) ON DELETE CASCADE,
            source_turn_id TEXT NOT NULL REFERENCES turns(turn_id) ON DELETE CASCADE,
            occurred_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY(character_id,fact_id,extraction_turn_id,source_turn_id),
            FOREIGN KEY(character_id,fact_id) REFERENCES derived_memories(character_id,id)
                ON DELETE CASCADE
        );
        CREATE INDEX memory_contribution_origin
            ON memory_source_contributions(character_id,extraction_turn_id,fact_id);
        CREATE TABLE memory_fact_sources (
            character_id TEXT NOT NULL, memory_id TEXT NOT NULL,
            turn_id TEXT NOT NULL REFERENCES turns(turn_id) ON DELETE CASCADE,
            position BIGINT NOT NULL,
            PRIMARY KEY(character_id,memory_id,turn_id)
        );
        ALTER TABLE memory_candidate_relations ADD COLUMN effect JSONB;
        CREATE TEMP TABLE absorbed ON COMMIT DROP AS
        WITH RECURSIVE links AS (
            SELECT character_id,absorbed_id,representative_id FROM memory_representatives
            UNION SELECT r.character_id,r.from_id,r.to_id FROM memory_relations r
            JOIN derived_memories a ON (a.character_id,a.id)=(r.character_id,r.from_id)
            JOIN derived_memories b ON (b.character_id,b.id)=(r.character_id,r.to_id)
            WHERE r.type='extends' AND a.kind='fact' AND b.kind='fact'
            AND (a.subject,a.predicate,a.object)=(b.subject,b.predicate,b.object)
        ), paths AS (
            SELECT character_id,absorbed_id,representative_id,ARRAY[absorbed_id] AS seen FROM links
            UNION ALL
            SELECT p.character_id,p.absorbed_id,l.representative_id,p.seen||l.absorbed_id
            FROM paths p JOIN links l ON (l.character_id,l.absorbed_id)=
                (p.character_id,p.representative_id) WHERE NOT l.absorbed_id=ANY(p.seen)
        )
        SELECT DISTINCT p.character_id,p.absorbed_id,p.representative_id FROM paths p
        WHERE NOT EXISTS(SELECT 1 FROM links l WHERE (l.character_id,l.absorbed_id)=
            (p.character_id,p.representative_id));
        INSERT INTO memory_source_contributions
        SELECT DISTINCT a.character_id,a.representative_id,COALESCE(g.turn_id,s.turn_id),
            s.turn_id,t.occurred_at FROM absorbed a
        JOIN (SELECT character_id,memory_id,turn_id FROM memory_generated_sources
              UNION SELECT b.character_id,b.memory_id,b.turn_id FROM memory_baseline_sources b
                WHERE NOT EXISTS(SELECT 1 FROM memory_generated g
                    WHERE (g.character_id,g.memory_id)=(b.character_id,b.memory_id))
              UNION SELECT m.character_id,m.memory_id,m.turn_id FROM memory_sources m
                WHERE NOT EXISTS(SELECT 1 FROM memory_generated g
                    WHERE (g.character_id,g.memory_id)=(m.character_id,m.memory_id))
                AND NOT EXISTS(SELECT 1 FROM memory_baselines b
                    WHERE (b.character_id,b.memory_id)=(m.character_id,m.memory_id))) s
            ON (s.character_id,s.memory_id)=(a.character_id,a.absorbed_id)
        JOIN turns t ON t.turn_id=s.turn_id
        LEFT JOIN memory_generated g ON (g.character_id,g.memory_id)=(a.character_id,a.absorbed_id)
        ON CONFLICT DO NOTHING;
        INSERT INTO memory_fact_sources
        SELECT s.character_id,s.memory_id,s.turn_id,min(s.position)
        FROM (SELECT g.* FROM memory_generated_sources g
              UNION ALL SELECT b.* FROM memory_baseline_sources b
                WHERE NOT EXISTS(SELECT 1 FROM memory_generated g
                    WHERE (g.character_id,g.memory_id)=(b.character_id,b.memory_id))
                AND NOT EXISTS(SELECT 1 FROM memory_source_contributions c
                    WHERE (c.character_id,c.fact_id,c.source_turn_id)=
                        (b.character_id,b.memory_id,b.turn_id))) s
        WHERE NOT EXISTS(SELECT 1 FROM absorbed a WHERE
            (a.character_id,a.absorbed_id)=(s.character_id,s.memory_id))
        GROUP BY s.character_id,s.memory_id,s.turn_id;
        UPDATE memory_baselines b SET temporal_origin=
            b.temporal_origin || jsonb_build_object('last_reinforced_at',
                (SELECT g.payload->'last_reinforced_at' FROM memory_generated g
                 WHERE (g.character_id,g.memory_id)=(b.character_id,b.memory_id)))
        WHERE EXISTS(SELECT 1 FROM memory_source_contributions c
            WHERE (c.character_id,c.fact_id)=(b.character_id,b.memory_id));
        ALTER TABLE memory_candidate_relations
            DROP CONSTRAINT memory_candidate_relations_character_id_turn_id_fkey;
        ALTER TABLE memory_candidate_relations ADD CONSTRAINT memory_relation_origin
            FOREIGN KEY(turn_id) REFERENCES turns(turn_id) ON DELETE CASCADE;
        CREATE TEMP TABLE rewritten_relations ON COMMIT DROP AS
        SELECT r.character_id,COALESCE(f.representative_id,r.from_id) AS from_id,
            COALESCE(t.representative_id,r.to_id) AS to_id,r.type,
            r.from_id AS original_from
        FROM memory_relations r
        LEFT JOIN absorbed f ON (f.character_id,f.absorbed_id)=(r.character_id,r.from_id)
        LEFT JOIN absorbed t ON (t.character_id,t.absorbed_id)=(r.character_id,r.to_id)
        WHERE f.absorbed_id IS NOT NULL OR t.absorbed_id IS NOT NULL;
        CREATE TEMP TABLE rewritten_candidates ON COMMIT DROP AS
        SELECT r.character_id,r.turn_id,COALESCE(f.representative_id,r.from_id) AS from_id,
            COALESCE(t.representative_id,r.to_id) AS to_id,r.type,
            CASE WHEN f.absorbed_id IS NULL THEN r.effect ELSE
                COALESCE(g.payload,b.payload) || jsonb_build_object(
                    'id',f.representative_id,'source_turn_ids',
                    COALESCE(
                        (SELECT jsonb_agg(s.turn_id ORDER BY s.position)
                         FROM memory_generated_sources s
                         WHERE (s.character_id,s.memory_id)=(r.character_id,r.from_id)),
                        (SELECT jsonb_agg(s.turn_id ORDER BY s.position)
                         FROM memory_baseline_sources s
                         WHERE (s.character_id,s.memory_id)=(r.character_id,r.from_id))
                    )
                ) END AS effect
        FROM memory_candidate_relations r
        LEFT JOIN absorbed f ON (f.character_id,f.absorbed_id)=(r.character_id,r.from_id)
        LEFT JOIN absorbed t ON (t.character_id,t.absorbed_id)=(r.character_id,r.to_id)
        LEFT JOIN memory_generated g ON (g.character_id,g.memory_id)=(r.character_id,r.from_id)
        LEFT JOIN memory_baselines b ON (b.character_id,b.memory_id)=(r.character_id,r.from_id);
        DELETE FROM memory_candidate_relations;
        INSERT INTO memory_candidate_relations
            SELECT * FROM rewritten_candidates WHERE from_id<>to_id ON CONFLICT DO NOTHING;
        INSERT INTO memory_candidate_relations(character_id,turn_id,from_id,to_id,type,effect)
        SELECT r.character_id,COALESCE(g.turn_id,src.origin),r.from_id,r.to_id,r.type,
            COALESCE(g.payload,b.payload) || jsonb_build_object(
                'id',r.from_id,'source_turn_ids',src.sources)
        FROM rewritten_relations r
        JOIN absorbed a ON (a.character_id,a.absorbed_id)=(r.character_id,r.original_from)
        LEFT JOIN memory_generated g
            ON (g.character_id,g.memory_id)=(r.character_id,r.original_from)
        LEFT JOIN memory_baselines b
            ON (b.character_id,b.memory_id)=(r.character_id,r.original_from)
        CROSS JOIN LATERAL (
            SELECT min(s.turn_id) AS origin,jsonb_agg(s.turn_id) AS sources
            FROM (
                SELECT turn_id FROM memory_generated_sources
                    WHERE (character_id,memory_id)=(r.character_id,r.original_from)
                UNION SELECT turn_id FROM memory_baseline_sources
                    WHERE (character_id,memory_id)=(r.character_id,r.original_from)
            ) s
        ) src
        WHERE r.from_id<>r.to_id AND src.origin IS NOT NULL ON CONFLICT DO NOTHING;
        DELETE FROM memory_relations r USING absorbed a WHERE r.character_id=a.character_id
            AND (r.from_id=a.absorbed_id OR r.to_id=a.absorbed_id);
        INSERT INTO memory_relations
            SELECT character_id,from_id,to_id,type FROM rewritten_relations
            WHERE from_id<>to_id ON CONFLICT DO NOTHING;
        DELETE FROM memory_candidates c USING absorbed a
            WHERE (c.character_id,c.memory_id)=(a.character_id,a.absorbed_id);
        DELETE FROM memory_generated g USING absorbed a
            WHERE (g.character_id,g.memory_id)=(a.character_id,a.absorbed_id);
        DELETE FROM memory_baselines b USING absorbed a
            WHERE (b.character_id,b.memory_id)=(a.character_id,a.absorbed_id);
        DROP TABLE memory_representatives;
        DELETE FROM derived_memories m USING absorbed a
            WHERE (m.character_id,m.id)=(a.character_id,a.absorbed_id);
        UPDATE derived_memories m SET search_eligible=TRUE
        WHERE EXISTS(SELECT 1 FROM memory_baselines b
            WHERE (b.character_id,b.memory_id)=(m.character_id,m.id))
        AND NOT EXISTS(SELECT 1 FROM memory_baseline_dependencies d
            WHERE (d.character_id,d.memory_id)=(m.character_id,m.id));
        CREATE FUNCTION withdraw_recall_contribution() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            UPDATE derived_memories m SET search_eligible=FALSE WHERE EXISTS (
                SELECT 1 FROM memory_input_dependencies d
                JOIN turns t USING(turn_id)
                WHERE d.character_id=m.character_id AND d.turn_id=NEW.turn_id
                AND t.actor<>m.character_id AND (
                    EXISTS(SELECT 1 FROM memory_source_contributions c
                        WHERE c.character_id=d.character_id AND c.fact_id=m.id
                        AND c.extraction_turn_id=d.extraction_turn_id)
                    OR EXISTS(SELECT 1 FROM memory_candidate_relations r
                        WHERE r.character_id=d.character_id AND r.turn_id=d.extraction_turn_id
                        AND (r.from_id=m.id OR r.to_id=m.id))));
            RETURN NULL;
        END; $$;
        CREATE TRIGGER recall_contribution_receipt AFTER INSERT ON delivery_receipts
            FOR EACH ROW EXECUTE FUNCTION withdraw_recall_contribution();
    """)


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS(SELECT 1 FROM memory_source_contributions) THEN
                RAISE EXCEPTION 'Cannot downgrade: contributions cannot reconstruct absorbed IDs';
            END IF;
        END; $$;
        DROP TRIGGER recall_contribution_receipt ON delivery_receipts;
        DROP FUNCTION withdraw_recall_contribution();
        ALTER TABLE memory_candidate_relations DROP COLUMN effect;
        ALTER TABLE memory_candidate_relations DROP CONSTRAINT memory_relation_origin;
        ALTER TABLE memory_candidate_relations
            ADD CONSTRAINT memory_candidate_relations_character_id_turn_id_fkey
            FOREIGN KEY(character_id,turn_id)
                REFERENCES memory_extractions(character_id,turn_id);
        DROP TABLE memory_source_contributions,memory_fact_sources;
        CREATE TABLE memory_representatives (
            character_id TEXT NOT NULL,absorbed_id TEXT NOT NULL,representative_id TEXT NOT NULL,
            PRIMARY KEY(character_id,absorbed_id),CHECK(absorbed_id<>representative_id),
            FOREIGN KEY(character_id,absorbed_id) REFERENCES derived_memories(character_id,id)
                ON DELETE CASCADE,
            FOREIGN KEY(character_id,representative_id) REFERENCES derived_memories(character_id,id)
                ON DELETE CASCADE
        );
        CREATE INDEX memory_representative_target
            ON memory_representatives(character_id,representative_id);
    """)
