# Argus Memory System — Design

Converged design after several review rounds. The guiding finding: **Argus does not
have a "memory problem" — it has a retrieval problem and a knowledge-consolidation
problem.** Most of the infrastructure people build for "AI memory" (graph DB, second
vector store, extraction pipelines) is already present in the stack or unnecessary at
homelab scale. Add nothing that doesn't buy a capability.

## Capability inventory (what already exists)

| Memory type      | Existing source                                  |
|------------------|--------------------------------------------------|
| Working memory   | the model's context window                        |
| Episodic memory  | conversation DB (`argus/storage.py`, SQLite)      |
| Semantic memory  | curated docs (`tools.md`, the memory dir)         |
| Canonical truth  | the same curated docs (human-maintained)          |
| Embeddings       | nomic-embed on `anvil:8091` (CPU, 768-dim, free)  |
| Async execution  | Argus can run it itself; n8n only if durable queues are ever needed |

The only real gaps: (1) nothing auto-captures durable facts from finished chats, and
(2) the model couldn't query memory mid-conversation as a tool.

## Principles

1. **Don't add state unless it buys a capability.** No graph DB, no second vector
   store, no n8n agent loop. n8n stays a *tool lane*, never the orchestrator.
2. **The model READS memory; it never silently WRITES infra truth.** IPs, ports,
   hostnames, paths, credentials, topology stay in human-maintained docs. A
   model-generated memory that conflicts with curated docs loses, automatically.
3. **Retrieval before extraction.** Prove the model finds and *uses* memory before
   building anything that generates memory — otherwise you feed an unvalidated tool.
4. **Trust is an ordering, not a float**: `curated > verified > learned > unverified`.
5. **Evidence over confidence** (for learned facts): accumulate `support/contradict`
   and *successful reuse*, don't store a single self-reported confidence number.
6. **Derived indexes are rebuilt, never drifted.** Anything derived from a source
   (docs, chats) is regenerated; the source stays authoritative.

## What we explicitly rejected (and why)

- **Neo4j / graph DB** — the graph is ~4 hosts + a handful of services; SQL/files
  handle it. A graph adds a JVM service and a drift surface for a small expressiveness
  win. Reconsider only if transitive impact queries ("if host X dies, what breaks?")
  become a frequent real need — and even then, as a *derived index* rebuilt from the
  authoritative source, never a second source of truth.
- **Qdrant / second vector store** — embeddings + a vec path already exist; pgvector
  is the planned upgrade and unifies relational + vector in one store.
- **n8n as the memory engine / agent** — loses Prometheus telemetry + the deterministic
  loop, which are hard requirements. (n8n earns a role only if durable retry queues for
  consolidation ever matter — not yet.)
- **LLM-parsing docker-compose for infra truth** — compose doesn't even contain host
  IPs; the curated docs are more accurate. Auto-parsing would be lower quality.

## Roadmap (retrieval-first)

```
1A  lookup_memory over an IN-MEMORY embedding index of the curated docs   ← DONE
    (no schema, no facts table; index rebuilt from files at boot)
    Question it answers: does memory-as-a-tool actually change behavior usefully?
    → Use it for real conversations before building anything downstream.

1B  facts table — ONLY if 1A retrieval proves useful AND docs alone fall short.
2   unified retrieval: add learned facts + conversation summaries behind lookup_memory,
    one query surface, one ranking. Tag memory_type {fact|summary|transcript} so ranking
    can differ later (model the dimension early; build type-aware ranking only when a
    second type exists).
3   conversation summaries (still no extraction).
4   extraction: propose -> verify -> commit, DURABLE-ONLY filter.
    Verify hierarchy: ground against curated docs / live system state (REAL) >
    "challenge pass" (a second adversarial LLM read — consistency, NOT truth) >
    store unverified at low trust. A model grading its own confidence is not verification.
5   promotion: use-based, not count-based. trust = source_quality + grounding +
    successful_reuse (retrieved and helped without later contradiction). This lets a
    rare-but-critical fact (observed once) rise; a count threshold would bury it.
    NB: "successful reuse" needs reuse attribution (log which facts a turn used + a
    not-corrected signal) — a mechanism, not a column. Deferred with the phase.
```

### Risks to design against (in priority order)
1. **Memory pollution > hallucination.** "Sonarr broken Tuesday" becoming a durable
   fact is the real failure mode. Defense: aggressive durable-only extraction filter +
   bitemporal expiry (`valid_from`/`valid_until`) + contradiction demotion. Reject
   transient operational state at extraction time.
2. **Category rot** — `network/networking/wifi/host/...` proliferate. Let embeddings
   carry retrieval; keep categories broad or derived, never load-bearing.
3. **Premature dedup** — don't solve it until there are a few hundred facts and you can
   see what duplicates actually look like.

## Phase 1A — as built

- `argus/memory.py` — `DocMemory`: chunks curated docs (heading-scoped, source-tagged),
  embeds via the existing nomic client, brute-force cosine `search()`. In-memory only,
  rebuilt at boot in `build_registry`. Adaptive batching (embeddings server 500s past
  ~16/req; falls back per-item so one oversized chunk can't sink the build). Stores its
  own `embed_fn` so build and query always use the same embedder.
- `lookup_memory(query)` native tool (single required param) — surfaced by the existing
  semantic tool-selection; returns the top snippets `{source, text, score}`. Teaches
  gracefully (ModelRetry) if the index is down — never invent IPs/ports.
- Sources: `ARGUS_MEMORY_SOURCES` (colon-separated files/dirs; dirs → `*.md`); defaults
  to `tools.md` + the memory dir.
- Validated live: 181 chunks indexed; correct source on every probe (0.63–0.75); the
  80B selects, calls, and uses it end-to-end through the PWA.

### Known consideration
The curated docs contain credentials (API keys, HA token). They're indexed, so
`lookup_memory` can surface a snippet containing a secret to the model. Acceptable for
a single-user personal assistant that already holds those secrets in its tool configs;
revisit if Argus ever becomes multi-tenant.
