# Architecture Decision Records (ADR)

This document records the architectural and design decisions made for TenantRAG. Each decision includes context, rationale, alternatives considered, and trade-offs.

---

## ADR-001: Redpanda as Kafka-Compatible Message Broker

- **Status**: Accepted
- **Date**: 2026-10-14
- **Context**: The ingestion pipeline requires asynchronous event streaming (`document.uploaded`, `document.parsed`, chunk/embed workers, retries, and dead-letter queues).
- **Decision**: Use Redpanda (`docker.redpanda.com/redpandadata/redpanda:v24.1.2`) in docker-compose, consumed via `aiokafka`.
- **Rationale**:
  1. Redpanda is 100% Kafka API compatible with zero code changes in standard Kafka clients.
  2. Does not require Apache ZooKeeper or JVM overhead, starting in under 1 second with ~50MB initial memory footprint.
  3. Dramatically reduces local developer machine resource strain during evaluation and testing compared to full Apache Kafka cluster.
- **Alternatives Considered**:
  - *Apache Kafka with KRaft*: Viable, but higher memory overhead and slower container initialization.
  - *RabbitMQ / Celery*: Lacks partition-based replay, native offset commits, and event log semantics desired for event-driven document processing.

---

## ADR-002: Unified PostgreSQL 16 (pgvector + tsvector) for Hybrid Storage

- **Status**: Accepted
- **Date**: 2026-10-14
- **Context**: TenantRAG requires relational metadata (users, tenants, ACLs, usage logs), vector embeddings (top-N semantic search), and keyword search (full-text search).
- **Decision**: Use PostgreSQL 16 with the `pgvector` extension and built-in `tsvector` / GIN indexing.
- **Rationale**:
  1. *Transactional Consistency*: Document deletion, chunk updates, and ACL changes are atomic in a single ACID transaction. Dedicated vector DBs (e.g., Pinecone, Milvus) introduce eventual consistency and sync failure modes.
  2. *Pre-Filtering Security*: Row-Level Security (RLS) and document ACLs execute directly in SQL before ranking, preventing data leakage across tenant boundaries.
  3. *Hybrid Search in a Single Query Engine*: Reciprocal Rank Fusion (RRF) can combine pgvector cosine distance and PostgreSQL full-text search without orchestrating two distinct databases.
- **Alternatives Considered**:
  - *Pinecone / Qdrant + Postgres*: Increases infrastructure complexity, lacks unified ACID transactions, and complicates cross-database tenant isolation.

---

## ADR-003: Async SQLAlchemy 2.0 with asyncpg

- **Status**: Accepted
- **Date**: 2026-10-14
- **Context**: FastAPI is an asynchronous ASGI framework. Database access must not block the Python asyncio event loop.
- **Decision**: Adopt SQLAlchemy 2.0 with the `asyncpg` driver and `AsyncSession`.
- **Rationale**:
  1. Native non-blocking I/O allows a single FastAPI worker to handle high concurrency during RAG queries and ingestion status polling.
  2. SQLAlchemy 2.0 provides typed `select()` statements, strict schema validation, and smooth Alembic migration support.
- **Alternatives Considered**:
  - *Sync SQLAlchemy with `def` endpoints*: Blocks event loop threads or consumes worker thread pools under heavy query loads.

---

## ADR-004: Strict Configuration Management via Pydantic v2 Settings

- **Status**: Accepted
- **Date**: 2026-10-14
- **Context**: Multi-tenant infrastructure contains numerous credentials, URLs, thresholds, and model parameters that vary across local development, CI, and production.
- **Decision**: Use `pydantic-settings` (`BaseSettings`) with explicit type validation, `.env` file parsing, and computed properties.
- **Rationale**:
  1. Fails fast during application startup if required environment variables (e.g., `DATABASE_URL`, `JWT_SECRET_KEY`) are missing or malformed.
  2. Provides automatic type conversion (e.g., strings to integers, lists of origins, URLs).
  3. Cached via `@lru_cache` so dependency injection in FastAPI is zero-overhead.

---

## ADR-005: Defense-in-Depth Multi-Tenancy Strategy

- **Status**: Accepted
- **Date**: 2026-10-14
- **Context**: Cross-tenant data leakage is unacceptable in a production multi-tenant RAG platform.
- **Decision**: Enforce tenant isolation across three orthogonal layers:
  1. *Token Level*: Every authenticated request decodes `tenant_id`, `user_id`, and group memberships from an HMAC-SHA256 JWT.
  2. *Application / ORM Level*: All SQL queries explicitly filter by `tenant_id == current_user.tenant_id`.
  3. *Database Level (RLS)*: PostgreSQL Row-Level Security enabled on all tenant-scoped tables, parameterized by `SELECT set_config('app.tenant_id', :tid, true)` within each transaction.

---

## ADR-006: Global User Identity with Membership-Based Tenant Scoping

- **Status**: Accepted
- **Date**: 2026-10-15
- **Context**: We need to decide whether user identities are scoped per-tenant (a user exists only within one tenant) or global across tenants (a user identity can belong to multiple tenants via memberships).
- **Decision**: Model `users` as a global identity table with NO `tenant_id` column and NO Row-Level Security. Tenancy is established exclusively via the `memberships` join table.
- **Rationale**:
  1. Allows a single human user (email) to belong to multiple organizations or workspaces (e.g. personal, agency, enterprise) without managing multiple passwords.
  2. Simplifies cross-tenant collaboration and invitations.
  3. Tenant isolation is maintained because application queries never access `users` directly without joining on `memberships`, where RLS strictly filters to the active `tenant_id`.
- **Trade-offs**:
  - Global email namespace requires careful authentication design to prevent email enumeration (e.g. constant-time password verification on login even if user does not exist).
  - Users table cannot be protected by RLS; security relies on joining through `memberships`.

---

## ADR-007: Dual Database Roles (Migration Owner vs. Unprivileged Runtime Role)

- **Status**: Accepted
- **Date**: 2026-10-15
- **Context**: In PostgreSQL, database superusers and roles with the `BYPASSRLS` attribute completely bypass Row-Level Security policies.
- **Decision**: Separate database access into two distinct roles:
  1. `tenantrag` (migration/owner role): Superuser, runs Alembic migrations only.
  2. `tenantrag_app` (runtime application role): `NOSUPERUSER`, `NOBYPASSRLS`, `NOCREATEDB`, `NOCREATEROLE`, with only `SELECT, INSERT, UPDATE, DELETE` grants on tables.
- **Rationale**:
  - Guarantees that runtime application queries executed by the API and workers are subject to Row-Level Security policies.
  - At API startup, an assertion verifies that `rolsuper=False` and `rolbypassrls=False`, immediately halting startup if misconfigured.
- **Trade-offs**: Requires managing two database URLs (`MIGRATION_DATABASE_URL` and `DATABASE_URL`).

---

## ADR-008: Forced Row-Level Security (FORCE RLS) and Fail-Closed Default

- **Status**: Accepted
- **Date**: 2026-10-15
- **Context**: By default in PostgreSQL, `ALTER TABLE ... ENABLE ROW LEVEL SECURITY` does not apply RLS policies to the table owner. Furthermore, if tenant context is missing, queries must not inadvertently expose data.
- **Decision**: Apply `ALTER TABLE ... FORCE ROW LEVEL SECURITY` to all tenant-scoped tables (`tenants`, `memberships`, `groups`, `group_members`, `refresh_tokens`, `invitations`). Define `current_tenant_id()` SQL helper to return `NULL` when `app.tenant_id` is unset or invalid.
- **Rationale**:
  - `FORCE RLS` ensures policies apply universally, closing any loopholes where an owner connection might otherwise bypass isolation.
  - With `USING (tenant_id = current_tenant_id())`, a `NULL` tenant context evaluates to `tenant_id = NULL` (which is unknown/false in SQL ternary logic), returning exactly zero rows (fail closed).

---

## ADR-009: Transaction-Local Configuration via Parameterized `set_config`

- **Status**: Accepted
- **Date**: 2026-10-15
- **Context**: In connection pooling environments, connection-level state (`SET SESSION`) can easily leak to subsequent requests reusing that physical connection. Additionally, `SET LOCAL` in SQL does not accept parameterized bind variables in asyncpg.
- **Decision**: Use `SELECT set_config('app.tenant_id', :tid, true)` within an explicit transaction context (`async with session.begin():`).
- **Rationale**:
  1. The third argument `true` (`is_local=true`) restricts the configuration strictly to the current transaction. When the transaction commits or rolls back, the setting automatically reverts.
  2. Unlike `SET LOCAL app.tenant_id = ...`, `set_config` is a function call that accepts parameterized bind arguments, preventing SQL injection.

---

## ADR-010: SECURITY DEFINER Function for Slug-Based Tenant Resolution

- **Status**: Accepted
- **Date**: 2026-10-15
- **Context**: When an unauthenticated user submits credentials with a tenant slug (e.g. at login), the application does not yet have a verified `tenant_id` to establish the tenant context. Because `tenants` is RLS-protected, querying `tenants` directly would return 0 rows.
- **Decision**: Create a single `SECURITY DEFINER` function `resolve_tenant_id(p_slug text)` owned by the migration role, with a locked-down `search_path = public, pg_temp`, execution revoked from `PUBLIC` and granted only to `tenantrag_app`.
- **Rationale**:
  - Allows resolving only the UUID of an active tenant matching the slug before the tenant context is set.
  - Setting `search_path` prevents search path hijacking vulnerabilities.
  - Exposes no tenant data other than the tenant UUID, strictly for active tenants.

---

## ADR-011: Composite Tenant Prefixed Tokens for Zero-Privilege Lookup

- **Status**: Accepted
- **Date**: 2026-10-15
- **Context**: Refresh tokens and invitation tokens must be verified when the user may not possess an active session or access JWT.
- **Decision**: Format all refresh and invitation tokens as `<tenant_uuid>.<random_secret>`.
- **Rationale**:
  - Allows the API to extract `<tenant_uuid>` directly from the token string before issuing any database queries.
  - The API immediately initializes the transaction-local tenant context to that `<tenant_uuid>` and queries `refresh_tokens` or `invitations` under normal RLS policies without requiring additional `SECURITY DEFINER` functions or privileged roles.

