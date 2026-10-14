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
  3. *Database Level (RLS)*: PostgreSQL Row-Level Security enabled on all tenant-scoped tables, parameterized by `SET LOCAL app.tenant_id = :tenant_id` within each transaction.
