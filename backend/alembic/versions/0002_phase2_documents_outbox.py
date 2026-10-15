"""Phase 2: Documents, ACL, Chunks, and Outbox schema with RLS.

Revision ID: 0002_phase2_documents_outbox
Revises: 0001_phase1_auth_rls
Create Date: 2026-10-15 12:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import pgvector
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0002_phase2_documents_outbox"
down_revision: str | None = "0001_phase1_auth_rls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --------------------------------------------------------------------------
    # 1. chunking_configs table
    # --------------------------------------------------------------------------
    op.create_table(
        "chunking_configs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(64), nullable=False, server_default="default"),
        sa.Column("strategy", sa.String(32), nullable=False, server_default="recursive"),
        sa.Column("chunk_size_tokens", sa.Integer(), nullable=False, server_default="256"),
        sa.Column("chunk_overlap_tokens", sa.Integer(), nullable=False, server_default="32"),
        sa.Column("embedding_model", sa.String(128), nullable=False, server_default="BAAI/bge-small-en-v1.5"),
        sa.Column("embedding_dim", sa.Integer(), nullable=False, server_default="384"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_chunking_configs_tenant_id"),
    )
    op.create_index("ix_chunking_configs_tenant_id", "chunking_configs", ["tenant_id"])
    op.create_index(
        "uq_chunking_configs_active_tenant",
        "chunking_configs",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("is_active = true"),
    )

    # --------------------------------------------------------------------------
    # 2. documents table
    # --------------------------------------------------------------------------
    op.create_table(
        "documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("detected_type", sa.String(32), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("checksum_sha256", sa.String(64), nullable=False),
        sa.Column("storage_key", sa.String(512), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="UPLOADED"),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("failed_stage", sa.String(32), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("visibility", sa.String(32), nullable=False, server_default="PRIVATE"),
        sa.Column(
            "chunking_config_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chunking_configs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("parsed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("tenant_id", "id", name="uq_documents_tenant_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "owner_id"],
            ["memberships.tenant_id", "memberships.user_id"],
            ondelete="CASCADE",
            name="fk_documents_membership_owner",
        ),
        sa.UniqueConstraint("tenant_id", "owner_id", "checksum_sha256", name="uq_documents_owner_checksum"),
    )
    op.create_index("ix_documents_tenant_id", "documents", ["tenant_id"])
    op.create_index("ix_documents_tenant_owner", "documents", ["tenant_id", "owner_id"])
    op.create_index("ix_documents_tenant_status", "documents", ["tenant_id", "status"])

    # --------------------------------------------------------------------------
    # 3. document_acl table
    # --------------------------------------------------------------------------
    op.create_table(
        "document_acl",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("group_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "(group_id IS NOT NULL AND user_id IS NULL) OR (group_id IS NULL AND user_id IS NOT NULL)",
            name="ck_document_acl_target",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_document_acl_tenant_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "document_id"],
            ["documents.tenant_id", "documents.id"],
            ondelete="CASCADE",
            name="fk_document_acl_documents",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "group_id"],
            ["groups.tenant_id", "groups.id"],
            ondelete="CASCADE",
            name="fk_document_acl_groups",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["memberships.tenant_id", "memberships.user_id"],
            ondelete="CASCADE",
            name="fk_document_acl_memberships",
        ),
    )
    op.create_index("ix_document_acl_tenant_id", "document_acl", ["tenant_id"])
    op.create_index("ix_document_acl_tenant_doc", "document_acl", ["tenant_id", "document_id"])
    op.create_index(
        "uq_document_acl_group",
        "document_acl",
        ["tenant_id", "document_id", "group_id"],
        unique=True,
        postgresql_where=sa.text("group_id IS NOT NULL"),
    )
    op.create_index(
        "uq_document_acl_user",
        "document_acl",
        ["tenant_id", "document_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("user_id IS NOT NULL"),
    )

    # --------------------------------------------------------------------------
    # 4. chunks table
    # --------------------------------------------------------------------------
    op.create_table(
        "chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "chunking_config_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chunking_configs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("embedding", pgvector.sqlalchemy.Vector(384), nullable=True),
        sa.Column(
            "tsv",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english', text)", persisted=True),
            nullable=True,
        ),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("document_id", "chunking_config_id", "ordinal", name="uq_chunks_doc_config_ordinal"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "document_id"],
            ["documents.tenant_id", "documents.id"],
            ondelete="CASCADE",
            name="fk_chunks_documents",
        ),
    )
    op.create_index("ix_chunks_tenant_id", "chunks", ["tenant_id"])
    op.create_index("ix_chunks_tenant_doc", "chunks", ["tenant_id", "document_id"])
    op.create_index("ix_chunks_tenant_config", "chunks", ["tenant_id", "chunking_config_id"])
    op.create_index("ix_chunks_tsv", "chunks", ["tsv"], postgresql_using="gin")
    op.execute(
        sa.text("""
        CREATE INDEX ix_chunks_embedding_hnsw ON chunks 
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64);
        """)
    )

    # --------------------------------------------------------------------------
    # 5. outbox_events system table (no tenant_id, no RLS)
    # --------------------------------------------------------------------------
    op.create_table(
        "outbox_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("topic", sa.String(128), nullable=False),
        sa.Column("key", sa.String(255), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "headers",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_outbox_events_unpublished",
        "outbox_events",
        ["created_at"],
        postgresql_where=sa.text("published_at IS NULL"),
    )

    # --------------------------------------------------------------------------
    # 6. Row-Level Security: ENABLE & FORCE on all tenant-scoped tables
    # --------------------------------------------------------------------------
    tenant_tables = ["chunking_configs", "documents", "document_acl", "chunks"]
    for table_name in tenant_tables:
        op.execute(sa.text(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY;"))
        op.execute(sa.text(f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY;"))
        op.execute(
            sa.text(f"""
            CREATE POLICY tenant_isolation_policy ON {table_name}
            FOR ALL
            TO PUBLIC
            USING (tenant_id = current_tenant_id())
            WITH CHECK (tenant_id = current_tenant_id());
            """)
        )

    # --------------------------------------------------------------------------
    # 7. Grant access to runtime role tenantrag_app
    # --------------------------------------------------------------------------
    all_new_tables = ["chunking_configs", "documents", "document_acl", "chunks", "outbox_events"]
    for table_name in all_new_tables:
        op.execute(sa.text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table_name} TO tenantrag_app;"))


def downgrade() -> None:
    # Drop RLS policies
    for table_name in ["chunks", "document_acl", "documents", "chunking_configs"]:
        op.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation_policy ON {table_name};"))
        op.execute(sa.text(f"ALTER TABLE IF EXISTS {table_name} NO FORCE ROW LEVEL SECURITY;"))
        op.execute(sa.text(f"ALTER TABLE IF EXISTS {table_name} DISABLE ROW LEVEL SECURITY;"))

    op.drop_table("outbox_events")
    op.drop_table("chunks")
    op.drop_table("document_acl")
    op.drop_table("documents")
    op.drop_table("chunking_configs")
