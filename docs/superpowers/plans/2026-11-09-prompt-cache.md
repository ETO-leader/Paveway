# Prompt Cache Implementation Plan

**Goal:** Keep dynamic Runtime Memory outside stable system policy, append only meaningful note changes, expose honest OMP cache telemetry.

**Architecture:** Composer owns only stable Core/Role/User/Persona plus OMP-owned template variables. One shared Runtime Memory planner compares scoped per-note versions/content, with delivery evidence held by OMP messages rather than a pre-send database flag. Native uses supported extension hooks; Scheduler reuses its event channel. Compaction recovery must inspect exact retained context, not assume a hash proves facts survived. Diagnostics read OMP's durable usage and distinguish normalized data from provider provenance.

**Constraints:** No real user data changes, session cleanup, commits, pushes, new database tables, second tool system, model calls solely for injection/monitoring, or claimed cache improvements without provider evidence.

## 1. Establish request evidence
- Inspect installed binary and pinned source versions, template rendering, appended project/eval blocks, provider transforms, extension preparation cancellation, session persistence, resume and compaction.
- Identify exact delivery and compaction preservation seams before integrating Memory.

## 2. Stable composition
- `prompt_composer.py`, `customization.py`: remove Memory serialization; retain compatible optional arguments without system effect; bump composer contract.
- Existing composition/rendering tests: verify run independence, Memory mutation invariance, ordering, Skills/MCP retention.

## 3. Runtime Memory
- Shared planner: compare scoped identities, versions and content, emit only upserts and explicit withdrawals; distinguish bounded-selection omissions from deletion/expiry.
- Native extension: fetch current notes before request via authenticated runtime, stage message context without premature state commit; preserve cancellation, main-agent/task scope and ownership fences.
- Scheduler/RPC: carry delta in existing user event, using durable OMP receipt evidence on resume; preserve dispatch intent and uncertainty semantics.
- Compaction: preserve exact bounded Memory state using OMP facilities and restore only missing necessary data, without repeated history growth.
- Legacy sessions: explicit supported migration/version boundary, never mutate or discard old history.

## 4. Usage diagnostics
- `cache_usage.py`, CLI/runtime/native entrypoints: one parser over durable per-call OMP usage, cumulative session totals, conservative unavailable semantics, costs labelled by actual source.
- No prompt injection or model calls for metrics.

## 5. Verification
- Behavioral tests: unchanged/new/updated/deleted/expired scoped notes, initial/restart/background delivery, failure/cancel/retry/crash, compaction retention/idempotence, identity/config changes and unavailable usage.
- Real isolated OMP loopback wire experiments A–E, Native PTY and existing affected integration suites. Loopback usage is never real provider-cache evidence.
- Second review against user requirements, fix evidence-backed edge issues, update memory/customization/runtime documentation with API and verification limits.
