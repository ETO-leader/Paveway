# Conversation and Composer Implementation Plan

**Goal:** Improve native Paveway chat hierarchy and input focus without replacing OMP Markdown, transcript, editor or status line.

**Architecture:** Keep OMP 18.16.0 (`650f3a108229ee11bf68ba3377f4e8c3391cd424`) pinned. Add opt-in ordinary-message presentation and a native editor style preference through reproducible source patches, then activate from the trusted Paveway extension. No runtime protocol or private monkey patch.

**Tech Stack:** OMP TypeScript/Bun; bundled JavaScript; Python unittest, PTY and existing WideScreen decoder.

## Constraints

- Preserve all existing edits, status line, tool/system/recap rendering, Markdown and incremental streaming.
- Assistant label is GPT. Do not rewrite event.md, user settings, custom Skills or prompts.
- No commits, pushes, global OMP replacement, unrelated runtime/network/MCP changes or new animations.
- Native editor owns input, Unicode, IME markers, paste, history, selection and completions; style owns only chrome.
- Ordinary Unicode/ASCII symbols; TrueColor, ANSI-256 and no-color layout stays legible.

## Implementation

1. `bundled/omp/conversation.patch`: opt-in user/assistant presentation in original renderers, retaining native Markdown and stable-row ledgers. Tests exercise short/multiline prose, code, wide characters, streaming and non-prose messages.
2. `bundled/paveway-composer.mjs`, `bundled/omp/composer.patch`: register one ComposerStyle and select it through native CustomEditor. Preserve host wiring, responsive viewport budget, focus and IME-safe cursor geometry. Native render tests cover caret, paste, wrap, focus and resize.
3. `bundled/paveway-native.mjs`: activate only in the admitted interactive main session. Installer hashes and applies all patches in order to a new isolated source cache; global/background OMP unchanged.
4. `tests/test_native_conversation.py`: run actual native TUI against loopback provider, inspect captured terminal cells, drafts through streaming/menu/resize, no-color and limited-color cases. Reuse existing statusline integration tests to detect regressions.
5. Update `docs/native-ui.md` with architecture, public API limits, reproduction commands and exercised verification. Capture real terminal output and inspect rendered images before delivery.

## Visual direction

Transparent assistant prose; compact user accent instead of a full-width slab. Violet `#8475cc`, ink `#dae7f3`, muted `#8d899b`, quiet boundary `#49415e`; native terminal monospace, brief role labels and restrained single-line chrome. Existing cyan runtime status and statusline layout unchanged. Avoid nested chat cards, repeated decorative labels and forced vertical padding.

## Verification

Run bundled Bun tests and patched-host targeted tests once integrated. Run `PAVEWAY_TEST_NATIVE=1 .venv/bin/python -m unittest discover -s tests -p test_native_conversation.py -v`, plus existing native statusline checks. Record limitations separately: physical IME composition and actual Windows Terminal/font behavior require a real frontend, not simulated key bytes.
