"""Phase 1: Multi-tenancy, RBAC, RLS, and runtime role setup.

Revision ID: 0001_phase1_auth_rls
Revises:
Create Date: 2026-10-15 10:00:00.000000
"""

import os
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001_phase1_auth_rls"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --------------------------------------------------------------------------
    # 1. Idempotently create or update runtime application role tenantrag_app
    # --------------------------------------------------------------------------
    app_db_password = os.environ.get("APP_DB_PASSWORD", "tenantrag_app_secret")
    escaped_password = app_db_password.replace("'", "''")

    op.execute(
        sa.text(f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'tenantrag_app') THEN
                CREATE ROLE tenantrag_app WITH LOGIN PASSWORD '{escaped_password}';
            ELSE
                ALTER ROLE tenantrag_app WITH LOGIN PASSWORD '{escaped_password}';
            END IF;
            ALTER ROLE tenantrag_app NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
        END
        $$;
        """)
    )

    op.execute(
        sa.text("""
        DO $$
        BEGIN
            EXECUTE format('GRANT CONNECT ON DATABASE %I TO tenantrag_app', current_database());
        END
        $$;
        """)
    )

    op.execute(sa.text("GRANT USAGE ON SCHEMA public TO tenantrag_app;"))

    # --------------------------------------------------------------------------
    # 2. Schema: Tables, UUID primary keys, timestamptz, and constraints
    # --------------------------------------------------------------------------
    # tenants
    op.create_table(
        "tenants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("slug", name="uq_tenants_slug"),
    )
    op.create_index("ix_tenants_slug", "tenants", ["slug"])

    # users (Global identity table: NO tenant_id, NO RLS)
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("full_name", sa.String(255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.execute(sa.text("CREATE UNIQUE INDEX uq_users_lower_email ON users (lower(email));"))

    # memberships
    op.create_table(
        "memberships",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "user_id", name="uq_memberships_tenant_user"),
    )
    op.create_index("ix_memberships_tenant_id", "memberships", ["tenant_id"])
    op.create_index("ix_memberships_tenant_id_user_id", "memberships", ["tenant_id", "user_id"])
    op.create_index("ix_memberships_tenant_id_role", "memberships", ["tenant_id", "role"])

    # groups
    op.create_table(
        "groups",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "name", name="uq_groups_tenant_name"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_groups_tenant_id"),
    )
    op.create_index("ix_groups_tenant_id", "groups", ["tenant_id"])
    op.create_index("ix_groups_tenant_id_name", "groups", ["tenant_id", "name"])

    # group_members (Composite primary key & composite foreign keys)
    op.create_table(
        "group_members",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("group_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "group_id", "user_id", name="pk_group_members"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "group_id"],
            ["groups.tenant_id", "groups.id"],
            ondelete="CASCADE",
            name="fk_group_members_group",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["memberships.tenant_id", "memberships.user_id"],
            ondelete="CASCADE",
            name="fk_group_members_membership",
        ),
    )
    op.create_index("ix_group_members_tenant_id", "group_members", ["tenant_id"])
    op.create_index("ix_group_members_tenant_group", "group_members", ["tenant_id", "group_id"])
    op.create_index("ix_group_members_tenant_user", "group_members", ["tenant_id", "user_id"])

    # refresh_tokens
    op.create_table(
        "refresh_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("family_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replaced_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("refresh_tokens.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("token_hash", name="uq_refresh_tokens_token_hash"),
    )
    op.create_index("ix_refresh_tokens_tenant_id", "refresh_tokens", ["tenant_id"])
    op.create_index("ix_refresh_tokens_tenant_user", "refresh_tokens", ["tenant_id", "user_id"])
    op.create_index("ix_refresh_tokens_tenant_family", "refresh_tokens", ["tenant_id", "family_id"])

    # invitations
    op.create_table(
        "invitations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("invited_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("token_hash", name="uq_invitations_token_hash"),
    )
    op.create_index("ix_invitations_tenant_id", "invitations", ["tenant_id"])
    op.create_index("ix_invitations_tenant_email", "invitations", ["tenant_id", "email"])

    # --------------------------------------------------------------------------
    # 3. Grant table permissions and set default privileges for tenantrag_app
    # --------------------------------------------------------------------------
    op.execute(sa.text("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO tenantrag_app;"))
    op.execute(sa.text("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO tenantrag_app;"))
    op.execute(sa.text("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO tenantrag_app;"))
    op.execute(sa.text("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO tenantrag_app;"))

    # --------------------------------------------------------------------------
    # 4. Helper SQL Functions: current_tenant_id() and resolve_tenant_id()
    # --------------------------------------------------------------------------
    op.execute(
        sa.text("""
        CREATE OR REPLACE FUNCTION current_tenant_id()
        RETURNS uuid AS $$
        BEGIN
            RETURN NULLIF(current_setting('app.tenant_id', true), '')::uuid;
        EXCEPTION
            WHEN OTHERS THEN
                RETURN NULL;
        END;
        $$ LANGUAGE plpgsql STABLE;
        """)
    )
    op.execute(sa.text("GRANT EXECUTE ON FUNCTION current_tenant_id() TO tenantrag_app;"))

    op.execute(
        sa.text("""
        CREATE OR REPLACE FUNCTION resolve_tenant_id(p_slug text)
        RETURNS uuid
        SECURITY DEFINER
        SET search_path = public, pg_temp
        LANGUAGE plpgsql
        AS $$
        DECLARE
            v_id uuid;
        BEGIN
            SELECT id INTO v_id
            FROM tenants
            WHERE slug = p_slug AND is_active = true;
            RETURN v_id;
        END;
        $$;
        """)
    )
    op.execute(sa.text("REVOKE ALL ON FUNCTION resolve_tenant_id(text) FROM PUBLIC;"))
    op.execute(sa.text("GRANT EXECUTE ON FUNCTION resolve_tenant_id(text) TO tenantrag_app;"))

    # --------------------------------------------------------------------------
    # 5. Row-Level Security: ENABLE & FORCE on all tenant-scoped tables
    # --------------------------------------------------------------------------
    tenant_scoped_tables = [
        ("tenants", "id"),
        ("memberships", "tenant_id"),
        ("groups", "tenant_id"),
        ("group_members", "tenant_id"),
        ("refresh_tokens", "tenant_id"),
        ("invitations", "tenant_id"),
    ]

    for table_name, tenant_col in tenant_scoped_tables:
        op.execute(sa.text(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY;"))
        op.execute(sa.text(f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY;"))
        op.execute(
            sa.text(f"""
            CREATE POLICY tenant_isolation_policy ON {table_name}
            FOR ALL
            TO PUBLIC
            USING ({tenant_col} = current_tenant_id())
            WITH CHECK ({tenant_col} = current_tenant_id());
            """)
        )


def downgrade() -> None:
    # Drop policies and RLS
    for table_name in ["invitations", "refresh_tokens", "group_members", "groups", "memberships", "tenants"]:
        op.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation_policy ON {table_name};"))
        op.execute(sa.text(f"ALTER TABLE IF EXISTS {table_name} NO FORCE ROW LEVEL SECURITY;"))
        op.execute(sa.text(f"ALTER TABLE IF EXISTS {table_name} DISABLE ROW LEVEL SECURITY;"))

    # Drop functions
    op.execute(sa.text("DROP FUNCTION IF EXISTS resolve_tenant_id(text);"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS current_tenant_id();"))

    # Drop tables
    op.drop_table("invitations")
    op.drop_table("refresh_tokens")
    op.drop_table("group_members")
    op.drop_table("groups")
    op.drop_table("memberships")
    op.drop_table("users")
    op.drop_table("tenants")
