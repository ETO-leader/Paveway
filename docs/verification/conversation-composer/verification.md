# Conversation / Composer verification

## Scope and evidence

Real Paveway native TUI in an isolated PTY; fixed OMP 18.16.0, commit `650f3a108229ee11bf68ba3377f4e8c3391cd424`, final combined patch hash prefix `c11cda667144`. Model responses are local loopback fixtures, not paid inference. The captures contain fixture content only.

Screenshots are rasterizations of actual decoded terminal cells, not mockups. Blank/startup rows are cropped from the first two images; conversation and composer cells are retained. Ordinary DejaVu Sans Mono with Microsoft YaHei/Segoe UI Emoji fallback is used for evidence rendering, not required by Paveway. Plain status-line caps were explicitly selected in the new conversation cases so these captures do not require Nerd Font; the existing status-line suite separately retains its original cap coverage.

- [Markdown and draft during a streamed answer, 100 columns](chat-markdown.png)
- [Multiline Unicode input, 80 columns](composer-multiline.png)
- [Submitted multiline input, 40 columns](chat-narrow.png)
- [No-color / ASCII preset, 40 × 12](chat-short-none.png)

Reviewed: short prompts occupy one row; user and GPT prose share a two-column inset; GPT label appears once before answer text; Markdown headings, lists, math, fenced code and quotes remain native. Composer has a transparent thin rail, native `> ` gutter and quiet right-edge hints. Hints disappear under space/height pressure. No extra persistent animation was added. No obvious new clipping, frame misalignment or caret displacement was observed in these captures. Static images cannot prove absence of terminal flicker; stable-prefix tests cover the byte invariant during incremental output.

## Reproduction

Prepare the pinned host using `packaging/omp/install.py`, then obtain its `source` and `bun` paths from the public runtime manifest. Run from repository root:

```sh
PAVEWAY_OMP_SOURCE="$SOURCE" "$BUN" test tests/paveway-composer.test.mjs tests/paveway-statusline.test.mjs tests/paveway-wakeup-status.test.mjs
PAVEWAY_TEST_NATIVE=1 PAVEWAY_STATUSLINE_EVIDENCE=/tmp/paveway-conversation-final PYTHONPATH=src:tests .venv/bin/python -m unittest test_native_conversation test_native_statusline -v
PAVEWAY_TEST_NATIVE=1 PYTHONPATH=src:tests .venv/bin/python -m unittest test_native_capabilities.NativeCapabilityTests.test_native_subagent_uses_its_own_session_and_returns_result -v
```

From the patched source root:

```sh
"$BUN" test packages/tui/test/conversation-presentation.test.ts packages/tui/test/status-line-extension.test.ts
"$BUN" run --cwd packages/tui check:types
"$BUN" test packages/tui/test/editor.test.ts packages/tui/test/assistant-message-prose-color.test.ts packages/tui/test/assistant-message-narration.test.ts packages/tui/test/transcript-container.test.ts packages/tui/test/transcript-seam-loss.test.ts packages/tui/test/native/transcript-redesign-native.test.ts
```

The first native-package download hit a TLS timeout; direct and configured-proxy fetch attempts also timed out. For this local verification only, provisioning reused the same-version native artifact directory from the previously installed pinned host; copied `.node` files were compared by SHA-256. Product downloader behavior and its SHA-512 checks are unchanged. A clean installation still needs access to the pinned package download.

## Executed results

| Check | Result |
| --- | --- |
| Paveway composer/status-line/wakeup Bun tests | 69 passed |
| Patched message-presentation/status-line bridge tests | 25 passed |
| Existing native editor, prose, narration, transcript and native-surface regressions | 254 passed |
| Real PTY conversation + existing status-line suite | 15 passed |
| Real native subagent/tool interleaving | 1 passed |
| `packages/tui` typecheck | Passed |

The cancellation scenario printed two `ConnectionResetError` traces from the loopback HTTP fixture after clients disconnected; its assertions passed. These were not suppressed or presented as production errors.

Two initial test assumptions were corrected after tracing native behavior: bracketed paste normalizes canonically equivalent text to NFC; Markdown publishes a stable thinking prefix after append/render, not its first paint. Visual inspection also found and fixed a one-column prose-inset mismatch before the final runs.


## Boundaries

- Public `ComposerStyle` and `CustomEditor` remain responsible for layout and input. The two source patches expose explicit opt-in seams; they do not depend on prototype changes or private field access.
- Tool-only, thinking-only, synthetic/system-attributed input, custom events and recap retain separate rendering paths. Real native subagent/tool execution and final-answer rendering were exercised against the loopback fixture.
- IME candidate/preedit behavior, mouse selection and actual Windows Terminal/font rasterization were **not** manually exercised. Native input paths and IME cursor markers remain in place; marker/Unicode tests are not a claim of physical IME acceptance.
- Native startup splash, session metadata/title, existing status-line animation and all unrelated UI remain unchanged.

## Capture provenance

Raw paired JSON/ANSI captures are temporary under `/tmp/paveway-conversation-final`. SHA-256:

```text
chat-markdown JSON 4e6e63cbaa428c4b95f74b99bce7e8b6edba3609832469ebbe9fce8d9574ae48
chat-markdown ANSI d4a96e1b5f22cb1367531ca84ae09a2a7e8b77fc96379b657f901cd8014b92fd
composer-multiline JSON c78611cf5e04240e484e94952c06cc5f7622bef8c97d0fa0d5a515a959130695
composer-multiline ANSI d7383e08b8007258537dbd3fd1f3863439d06e1c7786c7f33c27cb4e8d0b8d72
chat-short-none JSON 4a95d3fcaecfd69b14c85f03051d65fa76bc185423512eddc8e2538ad1ac3dfd
chat-short-none ANSI 58204ad467d5c3aa5ecb9033115e017f940a9e1c589664b03701965a83f88339
chat-narrow JSON 59910c566af25aa03b6ecf5e74f292e105c0e0b581e1d98616d9736a0b6717fb
chat-narrow ANSI 54bd93e768e14f7d8af95f38e5c9e145054f3d0154029e66520e0a2e9e414004
```
