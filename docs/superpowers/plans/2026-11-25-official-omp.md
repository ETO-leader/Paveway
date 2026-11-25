# Official OMP Extension-First Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run Paveway foreground and background on the same official OMP, without building, patching or distributing OMP.

**Architecture:** Keep the existing PTY guardian, Extension admission, Runtime ownership fencing, and RPC guardian. Resolve the executable in one Python module and pass its absolute path across process boundaries. Keep OMP behavior and settings native; use documented Extension hooks where Paveway must enforce its own contracts.

**Tech Stack:** Python/Unix IPC, JavaScript Extension API, OMP JSONL RPC, real binary releases and loopback provider fixtures.

## Global Constraints

- No OMP source changes, bundled engine, mandatory exact OMP version, or model/provider catalog in Paveway.
- Keep Scheduler, durable dispatch intent, profiles, workspaces, Skill snapshots, Memory, WakeBinding and session ownership contracts. No user-data migration or deletion.
- Only visual presentation may degrade; a missing core capability must fail closed.

## Baseline and migration inventory

The old frontend replaced `agent.executable` with a bundled installer result before `frontend.begin`. Its package installer exported OMP 18.16.0 (`650f3a1`), applied six patches, provisioned fixed Bun/native bindings and launched the patched TS CLI. The daemon used configured OMP directly. Both already build Prompt/Skill input using the same snapshot module and use separate owned OMP Session directories. Native admission authenticates a private descriptor frame, nonce, source digest, child PID, working directory, and OMP Session identity. RPC requires ready, idle state and session identity before committing dispatch intent, then requires prompt result, settlement, durable Session and clean process exit. Process-tree cleanup and token/generation fencing stay in place.

The official [19.1.1 release](https://github.com/can1357/oh-my-pi/releases/tag/v19.1.1), [Extension reference](https://github.com/can1357/oh-my-pi/blob/v19.1.1/docs/extensions.md), [Extension loading](https://github.com/can1357/oh-my-pi/blob/v19.1.1/docs/extension-loading.md) and [RPC reference](https://github.com/can1357/oh-my-pi/blob/v19.1.1/docs/rpc.md) are the comparison sources. Latest tag confirmed independently via GitHub and npm; on PATH is 19.0.0. The 19.1.1 release binary is verified with upstream SHA256SUMS. Both report GPT-6.2 Sol in `models --json`.

| Old patch | Semantics | Migration |
| --- | --- | --- |
| `statusline.patch` | Replace native line and source internal turn, context, compaction and cost ticks | Use Extension status/widget; keep native model/context/cost output; drop replacement-only animation where unavailable |
| `conversation.patch` | Opt-in role labels and restyle normal user/assistant messages while retaining stream ledgers | Keep normal transcript native; presenters do not own ordinary user/assistant row layout |
| `composer.patch` | Keep a selected chrome shape while OMP updates editor preferences | Register an official composer shape; use the session config overlay, not an editor replacement |
| `banner.patch` and preload | Replace prepaint welcome and drive animation before Extension startup | Use official rendering/UI after Extension admission; retain static wordmark and native startup notices |
| `workspace.patch` | Refuse in-place project relocation; fence resume before mutation; idle handoff with inference | Use public input/session gates, startup overlay and graceful shutdown; verify all mutation routes, explicitly block any route lacking a safe gate |
| `workflow-mcp.patch` | Cross-cwd inherit the managed Tijian server, reject conflicts, and share native tests/reauth/toggle/discovery | Use native MCP configuration; verify same transport/permission/conflict semantics or reject a launch that cannot keep them |

## Execution

- [x] **Executable and diagnostics.** Share resolver and checked absolute process identity across foreground and RPC; verify explicit selection, PATH and replacement at the same path.
- [x] **Official runtime.** Verify the established trusted Extension handshake and real RPC on the replacement runtime before removing the old installer and patches.
- [x] **Presentation and MCP.** Use `registerComposerShape`, `setWidget`, `setStatus` and native MCP discovery. Different-cwd inheritance for managed Tijian is blocked, not silently changed: upstream does not expose a conflict-aware pre-connect configuration interface.
- [x] **Clean cutover.** Remove installer, patches, old wheel entries and retired display tests. Record command results and outstanding checks in [official OMP verification](../../verification/official-omp.md).
