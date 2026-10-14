# TenantRAG Benchmark & Evaluation Results

This document records the empirical results gathered from the TenantRAG evaluation module (`scripts/run_ablation.py`) and load testing (`scripts/load_test.py`).

---

## 1. Retrieval & Generation Ablation Results

> Dataset: Documented multi-tenant test corpus (eval questions with verified ground-truth chunk hints).
> Embedding Model: `BAAI/bge-small-en-v1.5`
> Reranker: `BAAI/bge-reranker-base`
> LLM: `llama3:8b` (Temperature: 0.0)

| Config ID | Retrieval Mode | Chunk Size / Overlap | Reranker | Recall@5 | Precision@5 | MRR | nDCG@5 | Faithfulness | Correct Refusal Rate |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| *Pending Phase 6 benchmark execution* | - | - | - | - | - | - | - | - | - |

---

## 2. Semantic Cache Performance

| Scenario | Cache Hit Rate (%) | Avg Latency (No Cache) | Avg Latency (Cache Hit) | Latency Reduction (%) | Estimated Cost Saved ($) |
| :--- | :--- | :--- | :--- | :--- | :--- |
| *Pending Phase 4 & Phase 8 benchmark execution* | - | - | - | - | - |

---

## 3. Load Testing Performance (Locust)

| Concurrent Users | Queries/sec (RPS) | p50 Latency (ms) | p95 Latency (ms) | p99 Latency (ms) | Error Rate (%) |
| :--- | :--- | :--- | :--- | :--- | :--- |
| *Pending Phase 8 load testing execution* | - | - | - | - | - |
