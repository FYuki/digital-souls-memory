"""Verbatim overlapping recall chunks with document-scoped publication."""

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE recall_document_sources DROP CONSTRAINT
            recall_document_sources_owner_id_kind_identifier_fkey;
        ALTER TABLE recall_document_sources DROP CONSTRAINT recall_document_sources_pkey;
        ALTER TABLE recall_documents DROP CONSTRAINT recall_documents_pkey;
        ALTER TABLE recall_documents
            ADD COLUMN chunk_index INTEGER NOT NULL DEFAULT 0 CHECK(chunk_index>=0),
            ADD COLUMN chunk_start INTEGER NOT NULL DEFAULT 0 CHECK(chunk_start>=0),
            ADD COLUMN chunk_end INTEGER NOT NULL DEFAULT 0 CHECK(chunk_end>=chunk_start),
            ADD COLUMN chunk_text TEXT NOT NULL DEFAULT '',
            ADD PRIMARY KEY(owner_id,kind,identifier,chunk_index);
        ALTER TABLE recall_document_sources
            ADD COLUMN chunk_index INTEGER NOT NULL DEFAULT 0,
            ADD PRIMARY KEY(owner_id,kind,identifier,chunk_index,turn_id),
            ADD CONSTRAINT recall_document_sources_chunk_fkey
                FOREIGN KEY(owner_id,kind,identifier,chunk_index) REFERENCES
                recall_documents(owner_id,kind,identifier,chunk_index) ON DELETE CASCADE;
        CREATE OR REPLACE FUNCTION queue_recall_document(oid TEXT, k TEXT, ident TEXT)
            RETURNS void LANGUAGE plpgsql AS $$
        BEGIN
            INSERT INTO recall_documents(owner_id,kind,identifier,chunk_index) VALUES(oid,k,ident,0)
            ON CONFLICT(owner_id,kind,identifier,chunk_index) DO UPDATE SET
                version=recall_documents.version+1,ready=FALSE,pending=TRUE,
                claim_token=NULL,lease_until=NULL,not_before=NULL;
            UPDATE recall_documents SET ready=FALSE,pending=FALSE,
                claim_token=NULL,lease_until=NULL,not_before=NULL,
                version=(SELECT version FROM recall_documents WHERE
                    owner_id=oid AND kind=k AND identifier=ident AND chunk_index=0)
            WHERE owner_id=oid AND kind=k AND identifier=ident AND chunk_index<>0;
        END; $$;
        UPDATE recall_documents SET ready=FALSE,pending=TRUE,version=version+1,
            claim_token=NULL,lease_until=NULL,not_before=NULL;
    """)


def downgrade() -> None:
    op.execute("""
        DELETE FROM recall_documents WHERE chunk_index<>0;
        UPDATE recall_documents SET ready=FALSE,pending=TRUE,version=version+1,
            claim_token=NULL,lease_until=NULL,not_before=NULL;
        ALTER TABLE recall_document_sources DROP CONSTRAINT
            recall_document_sources_chunk_fkey;
        ALTER TABLE recall_document_sources DROP CONSTRAINT recall_document_sources_pkey;
        ALTER TABLE recall_document_sources DROP COLUMN chunk_index;
        ALTER TABLE recall_documents DROP CONSTRAINT recall_documents_pkey;
        ALTER TABLE recall_documents DROP COLUMN chunk_index,DROP COLUMN chunk_start,
            DROP COLUMN chunk_end,DROP COLUMN chunk_text;
        ALTER TABLE recall_documents ADD PRIMARY KEY(owner_id,kind,identifier);
        ALTER TABLE recall_document_sources ADD PRIMARY KEY(owner_id,kind,identifier,turn_id),
            ADD FOREIGN KEY(owner_id,kind,identifier) REFERENCES
                recall_documents(owner_id,kind,identifier) ON DELETE CASCADE;
        CREATE OR REPLACE FUNCTION queue_recall_document(oid TEXT, k TEXT, ident TEXT)
            RETURNS void LANGUAGE plpgsql AS $$
        BEGIN
            INSERT INTO recall_documents(owner_id,kind,identifier) VALUES(oid,k,ident)
            ON CONFLICT(owner_id,kind,identifier) DO UPDATE SET
                version=recall_documents.version+1,ready=FALSE,pending=TRUE,
                claim_token=NULL,lease_until=NULL,not_before=NULL;
        END; $$;
        DELETE FROM recall_embeddings e WHERE NOT EXISTS (
            SELECT 1 FROM recall_documents d WHERE d.content_digest=e.content_digest);
    """)
