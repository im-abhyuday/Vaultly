# Vaultly Architecture

## Overview
Vaultly is a production-style, multi-tenant Retrieval-Augmented Generation (RAG) platform featuring:
- Strict multi-tenant isolation via PostgreSQL Row-Level Security (RLS) and pre-ranking ACL SQL filters.
- Asynchronous event-driven document ingestion pipeline powered by Kafka / Redpanda.
- Hybrid search (pgvector cosine similarity + PostgreSQL full-text search) merged with Reciprocal Rank Fusion (RRF), followed by local cross-encoder reranking.
- Redis-based two-tier semantic cache (exact query match + vector similarity cache) partitioned by tenant and permission signature.
- Systematic evaluation suite measuring retrieval metrics (recall@k, MRR, nDCG@k) and LLM-as-a-judge generation metrics (faithfulness, answer relevancy, context precision).
- Per-tenant token and cost tracking for auditable usage accounting.

---

## System Component Diagram

```mermaid
flowchart TD
    Client[Web UI / API Client] -->|HTTPS / JWT| API[FastAPI Backend]

    subgraph Security & Isolation
        API -->|Resolve tenant_id & ACL| JWT[JWT Validator]
        API -->|SET LOCAL app.tenant_id| RLS[PostgreSQL RLS Engine]
    end

    subgraph Caching & Storage
        API -->|Exact & Semantic Cache| Redis[(Redis Cache)]
        API -->|Hybrid SQL + Vectors| PG[(PostgreSQL 16 + pgvector)]
    end

    subgraph Async Ingestion
        API -->|Publish document.uploaded| Kafka[Redpanda / Kafka]
        Kafka -->|Consume| ParseWorker[Parse Worker]
        ParseWorker -->|Publish document.parsed| Kafka
        Kafka -->|Consume| EmbedWorker[Chunk & Embed Worker]
        EmbedWorker -->|Write chunks + vectors| PG
    end

    subgraph Query Execution
        API -->|Vector + FTS| PG
        PG -->|Top-N Candidates| RRF[Reciprocal Rank Fusion]
        RRF -->|Ranked Chunks| Reranker[Local Cross-Encoder]
        Reranker -->|Top-k Context| LLM[LLM Provider: Ollama / OpenAI]
        LLM -->|Answer + Citations| API
    end
```

---

## Services in Docker Compose

1. **`postgres` (`pgvector/pgvector:pg16`)**:
   - Stores tenant metadata, users, documents, chunks, embeddings (`vector(384)`), full-text search tokens (`tsvector`), query logs, and evaluation runs.
   - Enforces PostgreSQL Row-Level Security policies.
2. **`redis` (`redis:7-alpine`)**:
   - Fast key-value store for exact query caching, rate limiting sliding windows, and semantic vector similarity lookups.
3. **`kafka` (`docker.redpanda.com/redpandadata/redpanda:v24.1.2`)**:
   - Event broker orchestrating document processing stages with dead-letter queue (DLQ) support and at-least-once processing guarantees.
4. **`api` (`backend/Dockerfile`)**:
   - FastAPI REST API serving `/api/v1` routes: Auth, Documents, Query, Evaluation, Usage, and System Health.
5. **`worker` (`backend/Dockerfile`)**:
   - Background consumer daemons processing parsing, chunking, and embedding jobs asynchronously.
6. **`frontend` (Phase 7)**:
   - React + TypeScript + Vite single-page application with Recharts dashboards.

---

## Multi-Tenancy & Authentication (Phase 1)

### 1. Dual Role & Row-Level Security Architecture
- **Migrations & Administrative Tasks**: Conducted by the superuser role (`tenantrag`) possessing `SUPERUSER` and `BYPASSRLS`.
- **Runtime Application Queries**: Conducted by unprivileged role (`tenantrag_app`), strictly subject to PostgreSQL `FORCE ROW LEVEL SECURITY`.
- **Tenant Context Session Variable**: Every query runs inside a transaction after executing:
  ```sql
  SELECT set_config('app.tenant_id', :tenant_id, true);
  ```
  The third parameter `true` scopes the variable to the local transaction, eliminating cross-request connection pool leakage.

### 2. Authentication Flow Sequence

```mermaid
sequenceDiagram
    autonumber
    actor Client
    participant API as FastAPI Backend
    participant DB as PostgreSQL (tenantrag_app)
    participant Redis as Redis Cache

    Client->>API: POST /api/v1/auth/login (username, password, client_id=slug)
    Note over API,DB: Connects as tenantrag_app
    API->>DB: SELECT resolve_tenant_id(slug) [SECURITY DEFINER]
    DB-->>API: tenant_id
    API->>DB: SELECT set_config('app.tenant_id', tenant_id, true)
    API->>DB: SELECT user & membership WHERE email = :email
    DB-->>API: user record + Argon2id hash + role
    API->>API: verify_password(plain, hash)
    API->>DB: INSERT INTO refresh_tokens (token_hash, family_id, ...)
    API-->>Client: 200 OK (access_token JWT, opaque refresh_token)
```

### 3. Authenticated Request & RBAC Verification Sequence

```mermaid
sequenceDiagram
    autonumber
    actor Client
    participant API as FastAPI Backend
    participant Redis as Redis Cache
    participant DB as PostgreSQL (RLS Enforced)

    Client->>API: GET /api/v1/groups (Authorization: Bearer <jwt>)
    API->>API: Verify JWT signature, exp, iss, aud
    API->>Redis: Check if jti is denylisted (GET denylist:<jti>)
    Redis-->>API: nil (valid)
    API->>DB: SELECT set_config('app.tenant_id', jwt.tid, true)
    API->>DB: SELECT membership WHERE user_id = :uid AND tenant_id = :tid
    DB-->>API: Active membership with role (ADMIN / MEMBER / AUDITOR)
    API->>API: require_role check
    API->>DB: SELECT * FROM groups (RLS filters by tenant_id automatically)
    DB-->>API: Tenant groups
    API-->>Client: 200 OK [groups]
```

---

## Document Ingestion Pipeline & Outbox Pattern (Phase 2)

### 1. Ingestion Lifecycle Sequence

```mermaid
sequenceDiagram
    autonumber
    actor Client
    participant API as FastAPI Backend
    participant Disk as StorageBackend (Local/S3)
    participant DB as PostgreSQL (RLS Enforced)
    participant Relay as OutboxRelay Daemon
    participant Kafka as Redpanda / Kafka
    participant PC as ParseConsumer
    participant IC as IndexConsumer

    Client->>API: POST /api/v1/documents (multipart stream)
    API->>Disk: Stream file to disk (.tmp.<uuid> -> original) & SHA-256
    API->>DB: Check (tenant_id, owner_id, sha256) duplicate
    API->>DB: INSERT document (status='UPLOADED') + INSERT outbox_event
    API-->>Client: 202 Accepted (DocumentResponse)

    loop Every 500ms
        Relay->>DB: SELECT * FROM outbox_events WHERE published_at IS NULL FOR UPDATE SKIP LOCKED
        Relay->>Kafka: Publish document.uploaded (acks=all)
        Relay->>DB: UPDATE outbox_events SET published_at = NOW()
    end

    Kafka->>PC: Consume document.uploaded
    PC->>DB: UPDATE documents SET status='PARSING', claimed_by=:worker WHERE status='UPLOADED'
    PC->>Disk: Read original file
    PC->>PC: asyncio.to_thread(extract_text) [pypdf / docx / txt]
    PC->>Disk: Write parsed.json (atomic tmp -> target)
    PC->>DB: UPDATE documents SET status='PARSED' + INSERT outbox_event(document.parsed)
    PC->>Kafka: Commit offset

    Relay->>Kafka: Publish document.parsed

    Kafka->>IC: Consume document.parsed
    IC->>DB: UPDATE documents SET status='INDEXING', claimed_by=:worker WHERE status='PARSED'
    IC->>Disk: Read parsed.json
    IC->>IC: Hierarchical chunking (chunk_size, overlap)
    IC->>IC: asyncio.to_thread(embed_documents) [SentenceTransformer / Fake]
    IC->>DB: DELETE existing chunks WHERE document_id = :id
    IC->>DB: INSERT chunk records with vector(384) embeddings
    IC->>DB: UPDATE documents SET status='READY', chunk_count=N, indexed_at=NOW()
    IC->>Kafka: Commit offset
```

### 2. Document State Machine

```mermaid
stateDiagram-v2
    [*] --> UPLOADED: File uploaded & written to disk
    UPLOADED --> PARSING: ParseConsumer claims row
    PARSING --> PARSED: Text extracted & parsed.json written
    PARSING --> FAILED: Parse error (corrupt, empty, unsupported, retries exhausted)
    PARSED --> INDEXING: IndexConsumer claims row
    INDEXING --> READY: Chunks embedded & committed to pgvector
    READY --> [*]: DELETE /documents/{id} (cascaded)
    FAILED --> UPLOADED: POST /documents/{id}/reprocess
    FAILED --> [*]: Dead-Letter Queue (DLQ) emitted
```

### 3. Error Handling, Retries & Dead Letter Queue (DLQ)
- **Transient Failures** (DB connection blip, disk I/O lock): Retried in-process up to `MAX_ATTEMPTS` (default: 3) with exponential backoff and randomized jitter (`min(max_delay, base * 2^attempt) * uniform(0, 1)`).
- **Permanent Failures & Poison Pills** (corrupt file, unparseable Kafka bytes, unsupported MIME type, retries exhausted):
  1. Document transitioned to `FAILED` status with structured `error_code` and `error_message`.
  2. Original message published to DLQ topic (`vaultly.document.uploaded.dlq` or `vaultly.document.parsed.dlq`) with diagnostic Kafka headers (`error_code`, `error_message`, `stage`, `original_topic`, `timestamp`).
  3. Consumer offset committed to unblock the partition.


