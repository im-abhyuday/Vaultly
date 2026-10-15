# TenantRAG Architecture

## Overview
TenantRAG is a production-style, multi-tenant Retrieval-Augmented Generation (RAG) platform featuring:
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

