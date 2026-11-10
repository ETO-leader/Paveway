# Prompt Cache Audit Repair Implementation Plan

**Baseline:** fetched remote main and HEAD are b809568f1f6b6e1336304beb9d14c81ebcfd134f; initial worktree clean.

**Goal:** Fix Memory visibility after OMP reset, honest statistics ancestry, measured receipt storage growth, and evidence-backed compaction recovery while preserving stable system policy and append-only model prefixes.

**Constraints:** No paid provider calls, user-data changes, session cleanup, new database schema, branch-state store, cache manager, configuration switches, commits or pushes. Use actual OMP source/SDK and isolated loopback HTTP/PTY fixtures.

## Scope and order

1. Read current shared Memory and statistics implementations, OMP reset/context/leaf/compaction sources, and existing regression/real-OMP harnesses. Capture the unchanged Memory engine for quantitative baseline measurement before editing.
2. `bundled/paveway-memory-context.mjs`: derive receipt state only from the active branch after the latest reset boundary; never restore older compactions across `/clear`. Preserve legacy receipt readability. Stress evidence determines whether existing per-message delta plus existing checkpoint fields should replace full state on every update.
3. `cache_usage.py`, `tests/test_cache_usage.py`, `docs/runtime-api.md`: remove claims that a physical tail or ad-hoc activeLeafId is a reliable live leaf. Keep file-wide cumulative usage and label any last-record ancestry precisely. CLI and Native share this output.
4. `examples/stress_memory_context.mjs`: reproducible 64 KiB / hundreds-of-updates workload measuring disk bytes, model-facing delta bytes, JSON parse/state replay time and the actual 16 MiB reader boundary, baseline versus final module.
5. `examples/verify_prompt_cache.py`, `tests/test_prompt_cache_omp.py`, shared JS tests: real `/clear` followed by input, restart/resume and new input; unchanged/deleted/updated notes; compaction after reset; frozen prefix after future changes. Inspect no-summary provider-native context shapes before adding restoration code.
6. Run affected and requested complete regressions once after integration, including original A–E and failure/unavailable-usage paths. Use narrow reruns only for discovered failures/fixes.
7. Independent standards/spec source reviews of the implemented worktree versus b809568; fix evidence-backed findings. Record exact commands, results, stress data and unverified provider boundaries in a new audit report, not merely the previous report's claims.

## Interface invariants

- System composition does not depend on Memory or per-run data.
- Receipt state means accepted OMP context, not proof that a provider processed it; preparation never commits delivery state.
- Reset boundaries invalidate every earlier receipt/checkpoint for the new context but do not delete or edit disk entries.
- Future note changes cannot change any already-emitted recovery frame.
- New persistence reuses OMP custom-message delta/checkpoint metadata; no second log or history implementation.
- Statistics describe their observable persisted source, never an inferred live Active Branch.
