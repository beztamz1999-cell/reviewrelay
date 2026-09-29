# ReviewRelay V1 Phase 3 Implementation Report

## Scope

Implemented the Phase 3 ChatGPT Web Adapter as a transport-only layer. It launches a persistent Chromium profile under the selected ReviewRelay data root, navigates to an explicitly configured existing ChatGPT conversation, supports manual login, uploads explicit managed scratch files, sends review or evidence prompts once, waits for an owned completed assistant turn, and returns its visible raw text. It does not decide review outcomes or execute evidence requests.

## Baseline

- Starting HEAD: `7410155b844cf10fa2a705e9a788badeca4fd5ba`
- Branch: `main`
- Initial worktree: clean
- Phase 2 tested implementation parent: `3ec1c0ef31662272348faf141b71e0c5dc1905b8`; the starting HEAD also includes the subsequent Phase 2 report-only commit.

## Browser Architecture

- `ReviewerAdapter` is an asynchronous protocol. `ChatGPTWebAdapter` owns one Playwright persistent Chromium context and one active conversation page.
- Playwright is imported lazily inside browser startup. The profile path is `<DATA_ROOT>/browser-profile/<browser_profile>` and is checked with `PortableDataRoot` before launch. ReviewRelay does not select or copy the Owner's normal Chrome profile.
- `ChatGPTWebSettings` centralizes the default `https://chatgpt.com/` base URL, visible-browser default, existing conversation URL, profile name, and bounded timeouts. A conversation URL must use the configured origin and a `/c/<id>` or `/g/<id>/c/<id>` route. HTTP is accepted only for loopback fixtures.
- `ChatGPTSelectors` centralizes accessible-role/name strategies followed by stable test IDs and semantic fallbacks. The composer must be visible, enabled, editable, and explicitly composer-labeled; search controls are not used. UI diagnostics log selector counts and a query-free URL, not profile or authentication state.
- Files are passed explicitly, never discovered recursively. Resolved files must be regular files under `active/<project>/<task>/scratch/`; when project/task identity is configured, the path must match it. The adapter enforces 10 attachments, 20 MiB per file, and 50 MiB total before upload. It uses the normal file input, rejects visible pre-existing attachments, and waits for visible ordered attachment chips before filling or sending; unexpected or reordered chips stop the send.
- Before preparation, the adapter records visible user and assistant turn identities plus a main-frame navigation generation. It preserves a non-empty Owner composer draft, checks the conversation baseline again immediately before send, and requires one matching new user turn after the single send click.
- Response waiting is bounded. A new assistant turn must be the sole assistant turn beyond the baseline, the conversation and relay user turn must remain unchanged, the stop-generation control must be absent, the composer and send control must be visible, and response text must remain stable for the configured interval. Extraction returns the visible turn text as-is; it does not call the Phase 2 parser.

## Idempotency / Ambiguity Design

- The caller supplies `review_key`. The adapter hashes conversation identity, message kind, prompt, ordered attachment paths, and attachment contents to bind a key to one request.
- A repeated key with the same signature returns its prior result without another click. A key reused for different content raises `REVIEW_KEY_CONFLICT`.
- A pre-click failure carries `NOT_SENT`. A click without deterministic UI confirmation is stored as `SEND_AMBIGUOUS`; the adapter refuses to retry that key. A matching new user turn yields `SEND_CONFIRMED`. A complete owned assistant response yields `RESPONSE_RECEIVED`.
- The send registry is process-local. The later state/controller layer must persist send metadata to protect against duplicates across process or adapter restarts.

## Files Changed

- `README.md`
- `pyproject.toml`
- `src/reviewrelay/reviewer/__init__.py`
- `src/reviewrelay/reviewer/base.py`
- `src/reviewrelay/reviewer/chatgpt_web.py`
- `src/reviewrelay/reviewer/errors.py`
- `src/reviewrelay/reviewer/selectors.py`
- `src/reviewrelay/dev/__init__.py`
- `src/reviewrelay/dev/chatgpt_smoke.py`
- `tests/fixtures/chatgpt_ui.html`
- `tests/test_reviewer_adapter.py`
- `reports/REVIEWRELAY_V1_PHASE3-implementation-report.md`
- `reports/REVIEWRELAY_V1_PHASE3-audit.md`

## Configuration Changes

`chatgpt` accepts `base_url`, `browser_profile`, `conversation_url`, `headless`, and `timeouts`. Defaults are `https://chatgpt.com/`, `default`, no implicit conversation, `false`, and 30-second navigation / 60-second upload / 600-second response / 2-second stability intervals. Configurable bounds reject non-finite values and unreasonable waits.

Install the optional browser dependency with `python -m pip install -e ".[dev,browser]"`, then install Chromium with `python -m playwright install chromium`.

## Tests

- `python -m pytest tests/test_reviewer_adapter.py -q` — 19 passed.
- `python -m pytest -q` — 259 passed, including the existing Phase 1 and Phase 2 regression suite.
- `python -m compileall -q src tests` — passed.
- `python -m reviewrelay.dev.chatgpt_smoke --help` — verified the explicit smoke CLI without sending a message.
- Final staged-tree verification repeats the full pytest suite, compile check, and `git diff --cached --check`; results are recorded in the audit report.

## Live Smoke

`NOT_RUN`. No authenticated ChatGPT conversation was opened and no live message was sent.

## Deferred

Codex Worker Adapter, Evidence Executor, autonomous loop, secret scanner, and Windows UI remain deferred. The Phase 2 Evidence DSL remains validation-only.

## Known Limitations

- ChatGPT may change accessible names, test IDs, or upload presentation. Attachment readiness currently depends on the visible `attachment-chip` test ID and fails closed if that marker or the expected order cannot be verified; real-account selector compatibility was not tested.
- The send idempotency registry is not persisted and is scoped to one adapter instance/process.
- Playwright may use OS-managed temporary files outside the data root internally. ReviewRelay-owned persistent profile and task staging data remain under the explicit data root.
- Live login, upload, send, and response capture require the Owner to invoke the smoke command manually.
