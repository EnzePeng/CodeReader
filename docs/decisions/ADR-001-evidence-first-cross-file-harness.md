# ADR-001: Evidence-first cross-file analysis harness

## Status

Accepted

## Date

2026-08-24

## Context

CodeReader previously assembled one static project-context prompt before each generation. That
approach failed when the model discovered a missing definition after generation had started, and
two independent project indexes could disagree about same-named symbols. Natural-language FTS
queries also over-constrained code identifiers, while separate project/evidence budgets could
overflow a small model's context window or drop a long target function entirely.

The production baseline must work with a local Qwen2.5-Coder 7B model and a 16K window. It cannot
depend on free-form ReAct output, embeddings, a network service, a writable tool, or an LSP process.
Source facts must remain auditable by project-relative path, line range, and file hash.

## Decision

Use an evidence-first hybrid harness:

1. `CodeIndex` is the only semantic fact source in `/chat` and `/explain`. Its rebuildable SQLite
   schema stores qualified Python symbols, scoped import bindings, resolved/ambiguous call edges,
   parse state, and source hashes. Schema changes build a new database before atomic replacement.
2. `ContextBroker` performs deterministic preflight on every request. It loads the current span,
   import-resolved definitions, relevant one-hop relations, verified conversation anchors, and a
   navigation-only repository map. Exact, sufficient questions use one final model generation.
3. Missing or ambiguous evidence starts `ResearchAgent`, a read-only loop limited to 3 planning
   steps, 8 tool calls, parallelism 3, duplicate/no-progress guards, and a 180-second wall clock.
   Native llama.cpp tool calls are used only after a cached contract probe; JSON Schema decisions
   are the fallback. Final answer generation is always a separate streaming request.
4. All prompt components share one rendered-request token ledger. Output and thinking reserves must
   fit below `agent.context_hard_ratio`; old tool bodies and navigation context are compacted before
   exact target evidence. Oversized definitions become verified head/focus/tail continuations.
5. Conversations are project-scoped process memory. Events retain evidence locators and hashes, not
   copied source bodies; TTL/LRU bounds are 120 minutes and 64 sessions by default. Changing the
   active file does not create a new conversation.
6. Model-visible source is untrusted data. Only seven project-contained read tools exist; absolute
   paths, traversal, symlink escapes, excessive spans, shell, network, and writes are rejected by
   code. Final citations are admitted only if their source hash still validates.

`POST /api/chat` remains backward compatible and adds optional `conversation_id` and
`history[].evidence`. Its `complete.result` includes `conversation_id`, `trace_id`, `path`, research
counts, stop reason, warnings, protocol, and cumulative evidence. Existing SSE event names remain;
`delta` carries only final-answer text.

## Alternatives Considered

### Preload the whole repository

Simple to implement, but it scales with repository size, duplicates irrelevant source, and performs
poorly at 4K–16K windows. It also cannot recover when the initial selection omitted the decisive
definition.

### Embeddings and a vector database

Useful for semantic prose search, but adds a second model and operational state without fixing
qualified import resolution or same-name correctness. Deterministic AST/FTS retrieval addresses the
current Python failure modes with less memory and better auditability.

### Always let the model run ReAct

Flexible, but small local models frequently emit invalid or repetitive free-form actions. The chosen
design skips planning for exact questions and constrains uncertain decisions through native tool
calls or JSON Schema.

### Full multi-language LSP or Tree-sitter rollout

Would broaden semantic coverage, but substantially increases packaging and lifecycle complexity.
Python AST semantics are the accepted first release; other languages keep bounded text search and
span reading behind the same future `SemanticNavigator` boundary.

## Consequences

- Cross-file Python answers can be traced to fresh source spans, and ambiguous calls no longer become
  false exact edges.
- Simple questions are faster because the research loop is bypassed; hard questions have predictable
  latency and token ceilings.
- The SQLite index remains disposable derived state; format upgrades rebuild instead of migrating
  potentially incorrect call edges.
- In-memory conversations disappear on restart. Clients can reconstruct context from recent history
  and evidence anchors without persisting source or questions server-side.
- Non-Python semantic precision remains intentionally lower until a separate navigator is adopted.

## Verification

- `uv run --group dev ruff check backend`
- `uv run --group dev mypy backend/app`
- `uv run --group dev coverage run -m unittest discover -s backend`
- `npm test && npm run typecheck && npm run build` in `frontend/`
- `backend/test_gold_context.py` contains 32 gold questions and requires exact definition Recall@1
  of 100% with no false exact target.
