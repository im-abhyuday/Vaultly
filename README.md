# TenantRAG

A production-style, multi-tenant Retrieval-Augmented Generation (RAG) platform with built-in evaluation, access control, semantic caching, and cost tracking.

---

## Key Features

- **Multi-Tenant Isolation (Defense-in-Depth)**: Enforced via PostgreSQL Row-Level Security (RLS), JWT tenant claims, and SQL-level pre-filtering.
- **Asynchronous Ingestion Pipeline**: Event-driven architecture with Kafka / Redpanda topics (`document.uploaded`, `document.parsed`), idempotent chunking/embedding workers, backoff retries, and dead-letter queues (DLQ).
- **Hybrid Retrieval & Reranking**: Reciprocal Rank Fusion (RRF) combining vector similarity (`pgvector` cosine top-N) and keyword search (PostgreSQL `tsvector` FTS), reranked via local cross-encoder (`BAAI/bge-reranker-base`).
- **Two-Tier Semantic Caching**: Redis-backed exact-match and semantic vector cache scoped by `(tenant_id, permission_signature)`.
- **Systematic RAG Evaluation**: Retrieval benchmarks (recall@k, precision@k, MRR, nDCG@k) and LLM-as-a-judge generation metrics (faithfulness, relevancy, refusal accuracy).
- **Per-Tenant Cost & Token Tracking**: Detailed audit logs recording prompt/completion tokens, latency, and estimated dollar costs.

---

## Tech Stack

| Layer | Technology |
| :--- | :--- |
| **Backend** | Python 3.11, FastAPI, Pydantic v2, SQLAlchemy 2.0 (async), Alembic |
| **Database** | PostgreSQL 16 + `pgvector` extension + full-text search (`tsvector` / GIN) |
| **Cache & Rate Limiting** | Redis 7 |
| **Message Broker** | Redpanda / Apache Kafka (via `aiokafka`) |
| **Embeddings** | `BAAI/bge-small-en-v1.5` (running locally via `sentence-transformers`) |
| **Reranker** | `BAAI/bge-reranker-base` (local cross-encoder) |
| **LLM Interface** | Provider-agnostic interface (Ollama & OpenAI-compatible APIs) |
| **Frontend** | React, TypeScript, Vite, Recharts |
| **Infrastructure** | Docker, docker-compose |
| **Testing & Quality** | Pytest, pytest-asyncio, HTTPX, Ruff, Mypy, GitHub Actions CI |

---

## Repository Structure

```
tenantrag/
├── backend/
│   ├── alembic/              # Database schema migrations
│   ├── app/
│   │   ├── api/              # Versioned API routes (/api/v1)
│   │   ├── core/             # Configuration, logging, security
│   │   ├── db/               # Async engine, session, DeclarativeBase
│   │   ├── models/           # SQLAlchemy 2.0 ORM models
│   │   ├── schemas/          # Pydantic v2 validation models
│   │   ├── services/         # Core business logic
│   │   ├── workers/          # Kafka consumer background daemons
│   │   ├── rag/              # Hybrid retrieval, RRF, reranking, LLM
│   │   ├── eval/             # Retrieval & generation evaluation suite
│   │   └── main.py           # FastAPI entrypoint & middleware
│   ├── tests/
│   │   ├── conftest.py       # Async test fixtures
│   │   ├── unit/             # Unit tests (config, health, utils)
│   │   └── integration/      # End-to-end integration tests
│   ├── Dockerfile
│   ├── requirements.txt
│   └── requirements-dev.txt
├── docs/
│   ├── ARCHITECTURE.md       # High-level architecture & diagrams
│   ├── DECISIONS.md          # Architecture Decision Records (ADRs)
│   └── RESULTS.md            # Benchmark & evaluation ablation results
├── scripts/                  # Seed scripts, ablation runner, load test
├── .github/workflows/ci.yml  # Lint, type-check, and test automation
├── docker-compose.yml        # Orchestration (postgres, redis, kafka, api, worker)
├── Makefile                  # Developer workflow targets
└── pyproject.toml            # Ruff, Mypy, and Pytest configuration
```

---

## Quickstart

### 1. Prerequisites
- Docker & Docker Compose
- Python 3.11+
- Git

### 2. Configure Environment
```bash
cp .env.example .env
```

### 3. Start Infrastructure & Services
```bash
docker compose up -d
```
Services available:
- **FastAPI Documentation**: http://localhost:8000/api/v1/docs
- **Health Liveness Probe**: http://localhost:8000/healthz
- **System Health Status**: http://localhost:8000/api/v1/health
- **PostgreSQL**: `localhost:5432`
- **Redis**: `localhost:6379`
- **Redpanda Console / Kafka**: `localhost:19092`

---

## Local Development & Testing

```bash
# Create and activate virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install dependencies
pip install -r backend/requirements-dev.txt

# Run linter (Ruff)
ruff check backend

# Run type checker (Mypy strict)
mypy backend/app backend/tests

# Run test suite
pytest backend/tests -v
```

---

## Implementation Roadmap

- [x] **Phase 0**: Repo scaffold, docker-compose, FastAPI skeleton, config management, Alembic, lint/type/test tooling, CI.
- [ ] **Phase 1**: Auth + multi-tenancy + RBAC + RLS, with isolation tests.
- [ ] **Phase 2**: Document upload + Kafka ingestion pipeline (parse -> chunk -> embed), idempotency, retries, DLQ.
- [ ] **Phase 3**: Hybrid retrieval (vector + FTS + RRF) with permission-aware SQL, reranking, LLM answer with citations.
- [ ] **Phase 4**: Redis semantic cache + exact cache + invalidation + rate limiting.
- [ ] **Phase 5**: Usage/cost tracking + usage API.
- [ ] **Phase 6**: Evaluation module (datasets, metrics, runs, comparison) + ablation script.
- [ ] **Phase 7**: React frontend (all pages and dashboards).
- [ ] **Phase 8**: Load testing with Locust, performance tuning, observability, final docs.
