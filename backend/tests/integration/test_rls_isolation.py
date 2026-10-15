"""PostgreSQL Row-Level Security (RLS) and multi-tenant isolation integration tests.

All assertions run against real PostgreSQL using the unprivileged runtime role
(tenantrag_app: NOSUPERUSER, NOBYPASSRLS).
"""

import asyncio
import random
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.db.session import set_tenant_context, tenant_transaction
from app.models.enums import RoleEnum
from app.models.group import Group
from app.models.group_member import GroupMember
from app.models.invitation import Invitation
from app.models.membership import Membership
from app.models.refresh_token import RefreshToken
from app.models.tenant import Tenant
from app.models.user import User


async def seed_two_tenants(
    owner_session_maker: async_sessionmaker[AsyncSession],
) -> dict[str, uuid.UUID]:
    """Seed two distinct tenants with users, memberships, groups, and tokens."""
    async with owner_session_maker() as session:
        tid_a = uuid.uuid4()
        tid_b = uuid.uuid4()
        uid_a = uuid.uuid4()
        uid_b = uuid.uuid4()
        gid_a = uuid.uuid4()
        gid_b = uuid.uuid4()
        rf_a = uuid.uuid4()
        rf_b = uuid.uuid4()
        inv_a_id = uuid.uuid4()
        inv_b_id = uuid.uuid4()

        tenant_a = Tenant(id=tid_a, name="Tenant Alpha", slug="alpha")
        tenant_b = Tenant(id=tid_b, name="Tenant Beta", slug="beta")
        session.add_all([tenant_a, tenant_b])

        user_a = User(
            id=uid_a,
            email="user_a@alpha.com",
            password_hash="argon2_hash_a",
            full_name="User Alpha",
        )
        user_b = User(
            id=uid_b,
            email="user_b@beta.com",
            password_hash="argon2_hash_b",
            full_name="User Beta",
        )
        session.add_all([user_a, user_b])
        await session.flush()

        mem_a = Membership(
            id=uuid.uuid4(),
            tenant_id=tid_a,
            user_id=uid_a,
            role=RoleEnum.ADMIN.value,
        )
        mem_b = Membership(
            id=uuid.uuid4(),
            tenant_id=tid_b,
            user_id=uid_b,
            role=RoleEnum.MEMBER.value,
        )
        session.add_all([mem_a, mem_b])

        group_a = Group(
            id=gid_a,
            tenant_id=tid_a,
            name="Alpha Core Team",
            description="Core Alpha Group",
        )
        group_b = Group(
            id=gid_b,
            tenant_id=tid_b,
            name="Beta Core Team",
            description="Core Beta Group",
        )
        session.add_all([group_a, group_b])
        await session.flush()

        gm_a = GroupMember(tenant_id=tid_a, group_id=gid_a, user_id=uid_a)
        gm_b = GroupMember(tenant_id=tid_b, group_id=gid_b, user_id=uid_b)
        session.add_all([gm_a, gm_b])

        exp = datetime.now(UTC) + timedelta(days=7)
        rt_a = RefreshToken(
            id=rf_a,
            tenant_id=tid_a,
            user_id=uid_a,
            family_id=uuid.uuid4(),
            token_hash="token_hash_alpha_secret_value_123",
            expires_at=exp,
        )
        rt_b = RefreshToken(
            id=rf_b,
            tenant_id=tid_b,
            user_id=uid_b,
            family_id=uuid.uuid4(),
            token_hash="token_hash_beta_secret_value_456",
            expires_at=exp,
        )
        session.add_all([rt_a, rt_b])

        inv_a = Invitation(
            id=inv_a_id,
            tenant_id=tid_a,
            email="invitee@alpha.com",
            role=RoleEnum.MEMBER.value,
            token_hash="inv_hash_alpha_secret_val_123",
            invited_by=uid_a,
            expires_at=exp,
        )
        inv_b = Invitation(
            id=inv_b_id,
            tenant_id=tid_b,
            email="invitee@beta.com",
            role=RoleEnum.MEMBER.value,
            token_hash="inv_hash_beta_secret_val_456",
            invited_by=uid_b,
            expires_at=exp,
        )
        session.add_all([inv_a, inv_b])
        await session.flush()

        await session.commit()
        return {
            "tid_a": tid_a,
            "tid_b": tid_b,
            "uid_a": uid_a,
            "uid_b": uid_b,
            "gid_a": gid_a,
            "gid_b": gid_b,
            "rf_a": rf_a,
            "rf_b": rf_b,
            "inv_a": inv_a_id,
            "inv_b": inv_b_id,
        }


@pytest.mark.asyncio
async def test_tenant_isolation_reads(
    owner_session_maker: async_sessionmaker[AsyncSession],
    app_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Tenant A context sees ONLY Tenant A rows across all tenant-scoped tables, and Tenant B likewise."""
    seeded = await seed_two_tenants(owner_session_maker)
    tid_a, tid_b = seeded["tid_a"], seeded["tid_b"]

    # Under Tenant A context:
    async with tenant_transaction(tid_a, app_session_maker) as session:
        # Tenants
        tenants = (await session.execute(select(Tenant))).scalars().all()
        assert len(tenants) == 1
        assert tenants[0].id == tid_a

        # Memberships
        mems = (await session.execute(select(Membership))).scalars().all()
        assert len(mems) == 1
        assert mems[0].tenant_id == tid_a

        # Groups
        groups = (await session.execute(select(Group))).scalars().all()
        assert len(groups) == 1
        assert groups[0].tenant_id == tid_a

        # Group Members
        gms = (await session.execute(select(GroupMember))).scalars().all()
        assert len(gms) == 1
        assert gms[0].tenant_id == tid_a

        # Refresh Tokens
        rts = (await session.execute(select(RefreshToken))).scalars().all()
        assert len(rts) == 1
        assert rts[0].tenant_id == tid_a

        # Invitations
        invs = (await session.execute(select(Invitation))).scalars().all()
        assert len(invs) == 1
        assert invs[0].tenant_id == tid_a

    # Under Tenant B context:
    async with tenant_transaction(tid_b, app_session_maker) as session:
        tenants = (await session.execute(select(Tenant))).scalars().all()
        assert len(tenants) == 1
        assert tenants[0].id == tid_b

        mems = (await session.execute(select(Membership))).scalars().all()
        assert len(mems) == 1
        assert mems[0].tenant_id == tid_b

        groups = (await session.execute(select(Group))).scalars().all()
        assert len(groups) == 1
        assert groups[0].tenant_id == tid_b

        gms = (await session.execute(select(GroupMember))).scalars().all()
        assert len(gms) == 1
        assert gms[0].tenant_id == tid_b

        rts = (await session.execute(select(RefreshToken))).scalars().all()
        assert len(rts) == 1
        assert rts[0].tenant_id == tid_b

        invs = (await session.execute(select(Invitation))).scalars().all()
        assert len(invs) == 1
        assert invs[0].tenant_id == tid_b


@pytest.mark.asyncio
async def test_no_tenant_context_returns_zero_rows(
    owner_session_maker: async_sessionmaker[AsyncSession],
    app_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """When app.tenant_id is not set, all tenant-scoped queries return 0 rows (fail closed)."""
    await seed_two_tenants(owner_session_maker)

    async with app_session_maker() as session:
        for model in [Tenant, Membership, Group, GroupMember, RefreshToken, Invitation]:
            results = (await session.execute(select(model))).scalars().all()
            assert len(results) == 0, f"Table {model.__tablename__} leaked rows without tenant context"


@pytest.mark.asyncio
async def test_cross_tenant_insert_and_mutation_rejected(
    owner_session_maker: async_sessionmaker[AsyncSession],
    app_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Inserting rows with another tenant's ID fails WITH CHECK, and UPDATE/DELETE on another tenant's rows affect 0 rows."""
    seeded = await seed_two_tenants(owner_session_maker)
    tid_a, tid_b, gid_b = seeded["tid_a"], seeded["tid_b"], seeded["gid_b"]

    # 1. Attempt cross-tenant INSERT under Tenant A context
    async with app_session_maker() as session:
        await set_tenant_context(session, tid_a)
        rogue_group = Group(
            id=uuid.uuid4(),
            tenant_id=tid_b,  # Tenant B's id while context is Tenant A
            name="Injected Group",
            description="Should fail WITH CHECK",
        )
        session.add(rogue_group)
        with pytest.raises((IntegrityError, DBAPIError)) as exc_info:
            await session.flush()
        assert "tenant_isolation_policy" in str(exc_info.value).lower() or "check" in str(exc_info.value).lower()
        await session.rollback()

    # 2. Attempt cross-tenant UPDATE and DELETE under Tenant A context
    async with tenant_transaction(tid_a, app_session_maker) as session:
        # UPDATE Tenant B's group
        update_res = await session.execute(
            update(Group).where(Group.id == gid_b).values(name="Hacked Name")
        )
        assert isinstance(update_res, CursorResult)
        assert update_res.rowcount == 0

        # DELETE Tenant B's group
        delete_res = await session.execute(
            delete(Group).where(Group.id == gid_b)
        )
        assert isinstance(delete_res, CursorResult)
        assert delete_res.rowcount == 0

    # Verify Tenant B's group remains untouched
    async with tenant_transaction(tid_b, app_session_maker) as session:
        b_group = (await session.execute(select(Group).where(Group.id == gid_b))).scalar_one_or_none()
        assert b_group is not None
        assert b_group.name == "Beta Core Team"


@pytest.mark.asyncio
async def test_app_filter_removed_query_isolation(
    owner_session_maker: async_sessionmaker[AsyncSession],
    app_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Deliberately removing application-level WHERE tenant_id filter still yields only tenant's data due to RLS."""
    seeded = await seed_two_tenants(owner_session_maker)
    tid_a = seeded["tid_a"]

    async with tenant_transaction(tid_a, app_session_maker) as session:
        # Unfiltered query with NO where clause
        all_groups = (await session.execute(select(Group))).scalars().all()
        assert len(all_groups) == 1
        assert all_groups[0].tenant_id == tid_a
        assert all_groups[0].name == "Alpha Core Team"


@pytest.mark.asyncio
async def test_composite_fk_prevents_cross_tenant_links(
    owner_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Composite FKs at the schema level prevent linking a tenant A group to a tenant B member."""
    seeded = await seed_two_tenants(owner_session_maker)
    tid_a, tid_b = seeded["tid_a"], seeded["tid_b"]
    gid_a = seeded["gid_a"]
    uid_b = seeded["uid_b"]

    async with owner_session_maker() as session:
        # 1. Try to link Tenant A group with Tenant B user under Tenant A tenant_id
        # Fails because (tid_a, uid_b) does not exist in memberships
        invalid_gm_1 = GroupMember(tenant_id=tid_a, group_id=gid_a, user_id=uid_b)
        session.add(invalid_gm_1)
        with pytest.raises(IntegrityError) as exc_info:
            await session.flush()
        assert "fk_group_members_membership" in str(exc_info.value).lower()
        await session.rollback()

        # 2. Try to link Tenant A group with Tenant B user under Tenant B tenant_id
        # Fails because (tid_b, gid_a) does not exist in groups
        invalid_gm_2 = GroupMember(tenant_id=tid_b, group_id=gid_a, user_id=uid_b)
        session.add(invalid_gm_2)
        with pytest.raises(IntegrityError) as exc_info:
            await session.flush()
        assert "fk_group_members_group" in str(exc_info.value).lower()
        await session.rollback()


@pytest.mark.asyncio
async def test_meta_rls_enabled_and_forced_on_all_tenant_tables(
    owner_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Verify every table with a tenant_id column (and tenants table) has RLS enabled AND forced with at least one policy."""
    async with owner_session_maker() as session:
        # 1. Assert runtime role tenantrag_app is neither superuser nor bypassrls
        role_res = await session.execute(
            text("SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = 'tenantrag_app'")
        )
        role_row = role_res.mappings().one()
        assert role_row["rolsuper"] is False, "tenantrag_app must not be superuser"
        assert role_row["rolbypassrls"] is False, "tenantrag_app must not have bypassrls"

        # 2. Inspect all tables that contain a tenant_id column
        tenant_tables_res = await session.execute(
            text("""
            SELECT DISTINCT c.relname
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_attribute a ON a.attrelid = c.oid
            WHERE n.nspname = 'public'
              AND c.relkind = 'r'
              AND a.attname = 'tenant_id'
              AND NOT a.attisdropped;
            """)
        )
        tenant_tables = [row[0] for row in tenant_tables_res.all()]
        # tenants table itself uses id
        tenant_tables.append("tenants")

        assert len(tenant_tables) >= 6, "Expected at least 6 tenant-scoped tables"

        for table in tenant_tables:
            # Check relrowsecurity and relforcerowsecurity
            sec_res = await session.execute(
                text("""
                SELECT relrowsecurity, relforcerowsecurity
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND c.relname = :table;
                """),
                {"table": table},
            )
            sec_row = sec_res.mappings().one()
            assert sec_row["relrowsecurity"] is True, f"Table {table} must have RLS enabled (relrowsecurity=True)"
            assert sec_row["relforcerowsecurity"] is True, f"Table {table} must have FORCE RLS (relforcerowsecurity=True)"

            # Check at least one policy exists
            pol_res = await session.execute(
                text("""
                SELECT count(*) FROM pg_policy pol
                JOIN pg_class c ON pol.polrelid = c.oid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND c.relname = :table;
                """),
                {"table": table},
            )
            policy_count = pol_res.scalar()
            assert policy_count is not None and policy_count >= 1, f"Table {table} must have at least 1 RLS policy"

        # Verify that all public tables without RLS are explicitly allowlisted system tables
        all_tables_res = await session.execute(
            text("""
            SELECT c.relname
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relkind = 'r';
            """)
        )
        allowlisted_non_rls_tables = {"users", "alembic_version", "outbox_events"}
        for (tbl_name,) in all_tables_res.all():
            if tbl_name in allowlisted_non_rls_tables:
                continue
            assert tbl_name in tenant_tables, (
                f"Table '{tbl_name}' is neither tenant-scoped nor an allowlisted system table: {allowlisted_non_rls_tables}"
            )


@pytest.mark.asyncio
async def test_harness_leakage_detection_without_rls(
    owner_session_maker: async_sessionmaker[AsyncSession],
    app_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Create a table WITHOUT RLS and prove the test harness goes red (detects cross-tenant leakage)."""
    table_name = "leak_harness_test"
    tid_a = uuid.uuid4()
    tid_b = uuid.uuid4()

    async with owner_session_maker() as session:
        await session.execute(
            text(f"""
            CREATE TABLE IF NOT EXISTS {table_name} (
                id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                tenant_id uuid NOT NULL,
                data text NOT NULL
            );
            """)
        )
        await session.execute(
            text(f"GRANT SELECT, INSERT ON {table_name} TO tenantrag_app;")
        )
        await session.execute(
            text(f"INSERT INTO {table_name} (tenant_id, data) VALUES (:ta, 'Data A'), (:tb, 'Data B');"),
            {"ta": tid_a, "tb": tid_b},
        )
        await session.commit()

    try:
        # Run under Tenant A context as the runtime app role
        async with tenant_transaction(tid_a, app_session_maker) as session:
            rows = (await session.execute(text(f"SELECT tenant_id, data FROM {table_name}"))).all()
            # Because RLS is absent, both Tenant A and Tenant B rows are returned (LEAKAGE DETECTED)
            assert len(rows) == 2, "Harness without RLS should return all rows"
            tenant_ids = {r[0] for r in rows}
            assert tid_b in tenant_ids, "Cross-tenant row for Tenant B was leaked to Tenant A as expected without RLS"
    finally:
        async with owner_session_maker() as session:
            await session.execute(text(f"DROP TABLE IF EXISTS {table_name};"))
            await session.commit()


@pytest.mark.asyncio
async def test_connection_pool_leak_under_concurrency(
    owner_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Ensure high concurrency alternating tenants on a constrained pool never leaks tenant context."""
    seeded = await seed_two_tenants(owner_session_maker)
    tid_a, tid_b = seeded["tid_a"], seeded["tid_b"]

    from app.core.config import get_settings
    settings = get_settings()

    # Constrained engine with pool_size=2, max_overflow=0 to force intensive connection reuse
    small_pool_engine: AsyncEngine = create_async_engine(
        str(settings.DATABASE_URL),
        pool_size=2,
        max_overflow=0,
        pool_timeout=30.0,
    )
    small_session_maker = async_sessionmaker(
        bind=small_pool_engine,
        class_=AsyncSession,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )

    async def execute_worker(expected_tid: uuid.UUID) -> None:
        for _ in range(5):
            async with tenant_transaction(expected_tid, small_session_maker) as session:
                await asyncio.sleep(random.uniform(0.001, 0.005))
                groups = (await session.execute(select(Group))).scalars().all()
                assert len(groups) == 1
                assert groups[0].tenant_id == expected_tid

                mems = (await session.execute(select(Membership))).scalars().all()
                assert len(mems) == 1
                assert mems[0].tenant_id == expected_tid

    tasks = []
    for i in range(30):
        target_tid = tid_a if i % 2 == 0 else tid_b
        tasks.append(asyncio.create_task(execute_worker(target_tid)))

    await asyncio.gather(*tasks)
    await small_pool_engine.dispose()


@pytest.mark.asyncio
async def test_resolve_tenant_id_security_definer(
    owner_session_maker: async_sessionmaker[AsyncSession],
    app_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Verify resolve_tenant_id function resolves active tenant slug before tenant context is set."""
    seeded = await seed_two_tenants(owner_session_maker)
    tid_a, tid_b = seeded["tid_a"], seeded["tid_b"]

    async with app_session_maker() as session:
        # 1. Resolve slug 'alpha'
        res_a = await session.execute(text("SELECT resolve_tenant_id(:slug)"), {"slug": "alpha"})
        resolved_a = res_a.scalar()
        assert resolved_a == tid_a

        # 2. Resolve slug 'beta'
        res_b = await session.execute(text("SELECT resolve_tenant_id(:slug)"), {"slug": "beta"})
        resolved_b = res_b.scalar()
        assert resolved_b == tid_b

        # 3. Resolve nonexistent slug
        res_none = await session.execute(text("SELECT resolve_tenant_id(:slug)"), {"slug": "nonexistent"})
        assert res_none.scalar() is None

    # 4. Inactive tenant should not resolve
    async with owner_session_maker() as session:
        inactive_tid = uuid.uuid4()
        inactive_tenant = Tenant(id=inactive_tid, name="Inactive Co", slug="inactive", is_active=False)
        session.add(inactive_tenant)
        await session.commit()

    async with app_session_maker() as session:
        res_inact = await session.execute(text("SELECT resolve_tenant_id(:slug)"), {"slug": "inactive"})
        assert res_inact.scalar() is None
