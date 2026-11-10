# Windline StatusLine Implementation Plan

**Goal:** Replace Paveway's two extra status rows and OMP's embedded status with one accurate, animated bottom status row, retaining the editor and transcript.

**Architecture:** Extend pinned OMP 18.16.0 StatusLine with a custom renderer and a live host-owned snapshot. Paveway publishes only identity, Workflow, Wakeup and Inbox state. The host owns animation lifecycle and repaint scheduling; the renderer owns palette, segments and cell-aware layout.

**Tech Stack:** OMP TypeScript/Bun, bundled Paveway JavaScript, Python unittest/PTY/pyte.

## Global Constraints

- No commits/pushes, forced alternate screen, transcript rewrite, runtime protocol or ownership changes. Preserve OMP's own transient resize/overlay buffer behavior.
- Model, context, cost and compaction boundaries come from OMP; Wakeup/Inbox remain Runtime-owned.
- Animation affects only unused context cells at 125ms; no refresh loop when disabled or idle.
- Preserve original editor shape and support 80/100/120/160 columns and extreme widths.
- Keep reproducible upstream patch and isolated runtime installation; never patch the global executable.

## Implementation slices

- [x] OMP bridge: `StatusLineComponent.setStatusLine`, interactive `ui.setStatusLine`, live snapshot, motion setting and cleanup; tests in the pinned checkout. Integration resources ship with Paveway.
- [x] Renderer: `bundled/paveway-statusline.mjs`; `createStatusLine(getPavewayState, ui)` returns `{render(width, snapshot)}`. Deterministic cell-width, context mapping and wave invariants in `tests/paveway-statusline.test.mjs`.
- [x] Paveway migration: `paveway-native.mjs` mounts the renderer after authenticated admission; `paveway-wakeup.mjs` publishes structured state instead of `setStatus`. Preserve all schedule/list/acknowledge operations. Stop presentation timers at shutdown.
- [x] Native startup selects the isolated patched host before ownership acquisition; wheel includes patch/installer resources. Background agents continue using the configured executable.
- [x] Run Bun unit/host tests and Python integration tests. Exercise actual native TUI with loopback provider, delayed work, cancellation, Inbox, resize and session switch. Capture 32 real terminal frames and rendered images; evidence and the extra upstream assertion failure are recorded in `docs/verification/windline-statusline.md`.

## Design tokens and critique

Night `#161d30`, violet `#8475cc`, identity blue `#35577f`, Workflow slate `#29384e`, readable ink `#dae7f3`, capacity cyan `#67cbd0`. Amber/red reserved for real warnings. Inherit terminal monospace; bold uppercase brand, regular labels, compact data. Connected color groups; only the context remainder moves.

Rejected the obvious all-field pill row: too fragmented at 80 columns. Use two connected runs, with identity on the left and quieter operational/capacity data on the right. The distinctive element is a stable cyan occupancy block adjoining a phase-shifted navy/cyan free-capacity wave, not a generic spinner or full-row glow.
