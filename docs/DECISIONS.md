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

---

## ADR 012: Refresh Token Rotation and Family Reuse Detection

- **Context**: Refresh tokens are long-lived (typically 30 days) and pose a severe credential compromise risk if intercepted.
- **Decision**: Every refresh request rotates the token immediately, generating a new opaque token within the same `family_id`. If an already replaced or revoked token in that family is submitted again, the system treats it as a security incident and revokes all tokens in that `family_id` across the tenant, committing the revocation immediately before returning HTTP 401.
- **Rationale**:
  - Prevents replay and theft attacks: once a token is used, it cannot be reused.
  - Automatic invalidation shuts down both the attacker and legitimate client if a compromised token is replayed, forcing re-authentication.

---

## ADR 013: RBAC Role Resolution from Database on Every Request

- **Context**: Access tokens contain the user ID and tenant ID. Placing the role directly in the JWT payload risks stale authorization if a user is demoted or removed while their access token is still within its 15-minute validity window.
- **Decision**: The API resolves the user's active membership and role directly from PostgreSQL (`memberships` table) during dependency injection (`get_current_user_and_session`), cached per-request in the request state.
- **Rationale**:
  - Immediate revocation and role demotion enforcement without waiting for access token expiration.
  - Keeps JWT payloads lightweight and prevents authorization drift.

---

## ADR 014: 404 Not Found vs 403 Forbidden for Cross-Tenant Queries

- **Context**: When a user queries a resource (user, group, document) belonging to another tenant, returning HTTP 403 confirms the resource ID exists in the system (information oracle).
- **Decision**: All cross-tenant lookups return HTTP 404 Not Found. HTTP 403 Forbidden is reserved strictly for authenticated members of the *same* tenant lacking sufficient role privileges (e.g. Member trying to invite another user).
---

## ADR 015: Owner-Scoped Deduplication (Preventing Cross-User Information Leaks)

- **Context**: File uploads should avoid storing redundant copies of identical documents. However, in a multi-tenant system with private document visibility, deduplication scoping has security and privacy implications.
- **Decision**: Scope deduplication uniquely to `(tenant_id, owner_id, checksum_sha256)` rather than tenant-wide.
- **Rationale**:
  - If deduplication were scoped across the entire tenant, User A uploading a document (e.g., a confidential contract, medical report, or whistleblowing document) would receive a "duplicate detected" response if User B had previously uploaded that exact file. This would function as an existence oracle, revealing that someone else in the organization possesses that specific document.
  - Scoping to `(tenant_id, owner_id, checksum_sha256)` guarantees that deduplication saves storage for re-uploads by the same owner while ensuring absolute confidentiality between users.
- **Trade-offs**:
  - Two distinct users uploading the same public or corporate file store two separate physical copies and distinct Document records. This slight storage overhead is an acceptable trade-off for zero information leakage.

---

## ADR 016: Transactional Outbox as an Unscoped System Table

- **Context**: To guarantee at-least-once delivery to Kafka without dual-write inconsistencies, all state changes must emit outbox events in the same database transaction. A background relay worker process must query and publish these events.
- **Decision**: The `outbox_events` table is designed deliberately as a system-level table: it has NO `tenant_id` column and NO PostgreSQL Row-Level Security (RLS) policies. It is explicitly allowlisted in the RLS meta-test alongside `users` and `alembic_version`.
- **Rationale**:
  - The outbox relay worker runs independently of tenant HTTP requests and must poll unpublished events across all tenants efficiently using `SELECT ... FOR UPDATE SKIP LOCKED`.
  - Imposing RLS on `outbox_events` would require the relay to know all tenant IDs in advance, iterate across tenants in separate transactions, or run with `BYPASSRLS` privileges (violating the principle of least privilege for the runtime application role).
  - Security is preserved because outbox payloads carry identifiers and metadata pointers only (e.g. `document_id`, `chunking_config_id`), never raw file contents, parsed text, or vector embeddings.

---

## ADR 017: Content Accessibility vs. Metadata Visibility Permissions

- **Context**: Document management requires administrative oversight (viewing document status, error messages, storage size, or retriggering ingestion) while respecting end-user privacy for document contents.
- **Decision**: Establish two distinct, centralized permission clause builders in `app.services.permissions`:
  1. `metadata_visible(user)`: Evaluates to `True` for tenant Admins and Auditors, document owners, or users with explicit ACL or Tenant visibility grants. Admins and Auditors can view document listings and lifecycle statuses.
  2. `content_accessible(user)`: Evaluates to `True` strictly for the document owner, users matching an explicit `document_acl` entry (by group or user ID), or if visibility is `TENANT`. Admins and Auditors do NOT receive an automatic bypass.
- **Rationale**:
  - Administrative governance is separated from content access. An administrator can verify that an employee's private document failed parsing and retrigger it, but cannot read the extracted chunks or retrieve private text segments through RAG queries (Phase 3).
  - Consistent SQL expressions prevent divergence between REST endpoints and future search/retrieval query pipelines.

---

## ADR 018: StorageBackend Protocol with Atomic File Persistence

- **Context**: Ingestion handles file uploads up to 25MB. Files must be safely stored on disk without holding open database transactions, without memory bloat, and without risk of partial/corrupt files after a crash.
- **Decision**: Define a `StorageBackend` protocol with a `LocalStorage` implementation. File writes stream in 64KB chunks to a temporary file (`.tmp.<uuid>`), calculate SHA-256 on the fly, fsync, and atomically replace the destination file via `os.replace`.
- **Rationale**:
  - Never loads entire documents into application memory.
  - Path traversal is prevented by resolving target paths and verifying that `resolved.relative_to(root_path)` succeeds and does not match root. User-supplied filenames are never used in storage keys (layout is strictly `tenants/{tenant_id}/documents/{document_id}/original`).
  - Atomicity guarantee: if the database transaction fails after the file is written, the file is unlinked immediately in the error handler. If a crash occurs between write and commit, the outbox event and document row are absent, preventing orphan files from being served.

---

## ADR 019: At-Least-Once Delivery with Idempotent Consumer Processing

- **Status**: Accepted
- **Date**: 2026-10-15
- **Context**: Kafka consumers process events under at-least-once delivery semantics. Network partitions, consumer group rebalances, and retries will cause duplicate event delivery. Consumers must never create duplicate chunks, corrupt document status, or re-parse completed documents.
- **Decision**: All consumers (`ParseConsumer`, `IndexConsumer`) enforce strict idempotency at the database row level:
  1. *State Machine Claiming*: Workers attempt an atomic transition (e.g. `UPLOADED -> PARSING` or `PARSED -> INDEXING`). If 0 rows match, the document has either moved forward or is claimed by another worker.
  2. *Duplicate Bypass*: If a message arrives for a document whose status is already at or beyond the expected target stage (`PARSED` or `READY`), the consumer logs an idempotent skip and immediately commits the Kafka offset without error.
  3. *Chunk Replacement*: If `IndexConsumer` re-indexes a document, it unconditionally deletes existing chunks for that `document_id` within the same transaction before inserting newly embedded chunks.
- **Rationale**: Eliminates race conditions and guarantees eventual consistency even under unpredictable consumer crash-and-restart cycles.

---

## ADR 020: Transactional Outbox Pattern for Asynchronous Decoupling

- **Status**: Accepted
- **Date**: 2026-10-15
- **Context**: Modifying database state (e.g. document uploaded, document parsed) and publishing messages to Kafka in separate steps creates a distributed dual-write problem. If the Kafka publish fails after a DB commit, the document hangs permanently; if the DB commit fails after Kafka publish, workers process non-existent records.
- **Decision**: Use the Transactional Outbox pattern. All pipeline stage transitions insert an `outbox_events` record in the exact same ACID database transaction as the document update. An independent background worker (`OutboxRelay`) polls unpublished events and forwards them to Kafka.
- **Rationale**:
  1. Guarantees zero message loss without requiring two-phase commit (2PC) or distributed transactions.
  2. Protects the API layer from Kafka availability hiccups—file uploads succeed even if Kafka is momentarily unreachable.
  3. Supports horizontal scaling: multiple `OutboxRelay` instances safely co-exist using `SELECT ... FOR UPDATE SKIP LOCKED`.

---

## ADR 021: Atomic State Machine Transitions & Worker Row Claiming

- **Status**: Accepted
- **Date**: 2026-10-15
- **Context**: When scaling worker replicas, multiple consumer processes across consumer groups or partitions could attempt to process the same document concurrently (e.g., reprocess endpoint called while initial ingestion is completing).
- **Decision**: Use atomic SQL updates with status guardrails and worker claiming:
  `UPDATE documents SET status = :target, claimed_by = :worker_id, claimed_at = :now WHERE id = :id AND status = :expected`.
- **Rationale**:
  1. Eliminates the need for external distributed lock managers (e.g. Redis Redlock).
  2. Atomic row-level locking guarantees that exactly one worker acquires ownership of a stage.
  3. If a worker process dies mid-execution, stale claims (`claimed_at < cutoff`) can be safely identified and reclaimed or marked as failed.

---

## ADR 022: StorageBackend Object Store vs. Kafka Message Payload Size

- **Context**: Parsed text extracted from large PDFs or DOCX files can reach tens of megabytes. Packing large text payloads directly into Kafka messages strains broker memory buffers, degrades throughput, and risks exceeding default message limits (e.g., `message.max.bytes=1MB`).
- **Decision**: Kafka message envelopes remain lean (<2KB), containing tenant IDs, document IDs, configuration references, and metadata. Extracted text from `ParseConsumer` is streamed directly to `StorageBackend` (`tenants/{tenant_id}/documents/{document_id}/parsed.json`). `IndexConsumer` loads and streams this parsed JSON on-demand.
- **Rationale**:
  1. Keeps Kafka brokers fast, lightweight, and within standard configuration parameters.
  2. Offloads bulk payload I/O to storage backends optimized for large object storage (local disk in development, S3/GCS in production).
  3. Enables direct inspection and auditing of parsed intermediate representations without querying Kafka event streams.

---

## ADR 023: In-Process Exponential Backoff vs. Kafka Retry Topics

- **Context**: Ingestion pipeline operations can encounter transient failures (e.g. temporary database connection spikes, momentary filesystem locks). Creating complex retry topic hierarchies (`topic.retry-1`, `topic.retry-2`) increases operational overhead and can disorder partition messages.
- **Decision**: Handle transient errors using in-process exponential backoff with full jitter (`min(max_delay, base * 2^attempt) * uniform(0, 1)`), retrying up to a configured threshold (`MAX_ATTEMPTS=3`). Permanent errors (unsupported MIME types, corrupt files, schema validation failures) bypass retry loops entirely and route straight to the Dead-Letter Queue (DLQ).
- **Rationale**:
  1. Rapidly resolves short-lived transient blips within seconds without re-queuing overhead.
  2. Preserves strict FIFO processing within Kafka topic partitions.
  3. Keeps Kafka topic provisioning simple and easy to reason about.

---

## ADR 024: Dead Letter Queue (DLQ) Architecture & Diagnostic Headers

- **Context**: Poison pill messages (corrupt bytes, unparseable JSON, invalid envelope schemas) and unrecoverable worker errors must never halt consumer loops or block healthy documents in the partition.
- **Decision**: Implement dead-letter topics (`vaultly.document.uploaded.dlq` and `vaultly.document.parsed.dlq`). On permanent failure or retry exhaustion:
  1. Publish the original raw byte payload to the appropriate `.dlq` topic.
  2. Inject diagnostic Kafka record headers: `error_code`, `error_message`, `stage`, `original_topic`, and ISO-8601 timestamp.
  3. Update the document status in PostgreSQL to `FAILED` with explicit `error_code` and human-readable message.
  4. Commit the consumer offset to advance the partition past the poison pill.
- **Rationale**: Prevents pipeline paralysis, ensures total visibility into failure causes, and allows operational teams to replay or debug failed events directly from DLQ topics.

---

## ADR 025: pgvector HNSW Indexing Strategy with Multi-Tenant Row-Level Security

- **Status**: Accepted
- **Date**: 2026-10-15
- **Context**: Chunks must be indexed for high-recall cosine similarity search across millions of vectors while maintaining strict multi-tenant Row-Level Security isolation.
- **Decision**: Index the `embedding` column in `chunks` using pgvector's `hnsw` index (`vector_cosine_ops`), configured with `m = 16` and `ef_construction = 64`. Create a composite B-tree index on `(tenant_id, document_id)`.
- **Rationale**:
  1. *Immediate Read-After-Write*: Unlike `ivfflat`, HNSW does not require an upfront training step or a minimum dataset size; newly inserted chunks are queryable immediately.
  2. *High Recall Under Concurrent Mutation*: HNSW maintains high recall even as documents are frequently added, deleted, or reprocessed.
  3. *Seamless RLS Interaction*: Pre-filtering on `tenant_id = current_setting('app.tenant_id')` integrates cleanly with HNSW index traversal in PostgreSQL 16.

---

## ADR 026: Thread Pool Offloading for CPU-Bound Document Parsing & Embedding

- **Status**: Accepted
- **Date**: 2026-10-15
- **Context**: PDF/DOCX text extraction and embedding model inference (tokenization and matrix operations) are CPU-bound tasks. Running them synchronously inside an `async def` consumer blocks the Python asyncio event loop, preventing Kafka heartbeat transmissions and causing consumer group timeouts/rebalances.
- **Decision**: Offload all CPU-bound parsing (`pypdf.PdfReader`, `docx.Document`) and embedding generation (`SentenceTransformer.encode`) to worker thread pools using `asyncio.to_thread()`.
- **Rationale**:
  1. Maintains a responsive event loop capable of servicing Kafka heartbeats and keep-alives even during multi-megabyte document parsing.
  2. Avoids false-positive consumer evictions and unnecessary partition rebalancing storms.
  3. Provides a clean pathway to multiprocess offloading if scaling beyond single-worker CPU limits is required in future phases.




