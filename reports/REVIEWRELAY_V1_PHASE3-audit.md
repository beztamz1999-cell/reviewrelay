# ReviewRelay V1 Phase 3 Audit

## Baseline

```text
BASE_SHA=7410155b844cf10fa2a705e9a788badeca4fd5ba
BRANCH=main
INITIAL_WORKTREE_CLEAN=YES
```

## Final Staged Tree

```text
CANDIDATE_TREE_SHA=NOT_RECORDED
```

The audit report itself is part of the candidate tree, so embedding its own final tree hash would change that hash. The final committed HEAD is reported in the worker handoff.

## Commands Actually Run

- `git status --short --branch` — initial state was `## main` with no changes before Phase 3 edits; run again during final verification and after commit.
- `python -m playwright install chromium` — installed the Chromium runtime required by the locally installed Playwright 1.63.0. This did not contact ChatGPT.
- `python -m compileall -q src tests` — passed.
- `python -m pytest tests/test_reviewer_adapter.py -q` — 19 passed.
- `python -m pytest -q` — final staged candidate: 259 passed; rerun after the final report restaging also passed 259.
- `python -m reviewrelay.dev.chatgpt_smoke --help` — displayed CLI usage and did not open a browser or send a message.
- `git diff --cached --check` — passed against the final staged candidate before commit.
- `git diff --cached --stat` and `git diff --cached --name-only` — used to review staged scope.
- `git rev-parse HEAD` and `git status --short --branch` — run after the final commit.

## Test Evidence

The offline fixture uses a local deterministic HTML page with Chromium. It exercises a persistent browser context and ordinary DOM, role, and file-input interactions without a ChatGPT account.

The Phase 3 test file reports `19 passed`. The complete suite reports `259 passed`, preserving all existing Phase 1 and Phase 2 tests. Against the staged candidate containing both reports, the final run of `python -m pytest -q` passed 259 tests, `python -m compileall -q src tests` passed, and `git diff --cached --check` passed. No live ChatGPT interaction is part of those checks.

## Browser Adapter Evidence

| Requirement | Evidence |
|---|---|
| Portable persistent profile | `test_persistent_profile_is_created_inside_data_root` starts Chromium and checks the profile resolves below the explicit data root; escape-name configuration is rejected. |
| Login-required flow | `test_navigation_login_and_conversation_readiness` detects the fixture login control and confirms the persistent page remains available after `LOGIN_REQUIRED`. |
| Configured conversation navigation | The same test opens a valid conversation and exercises an HTTP navigation failure and conversation-not-ready pages. |
| Composer detection | `test_navigation_login_and_conversation_readiness` covers missing, search-only, and disabled composer controls; `test_composer_and_send_failures_are_typed_and_do_not_send` checks `COMPOSER_NOT_FOUND` and missing send control. |
| Attachment validation | `test_attachment_validation_limits_and_managed_storage` covers accepted file, missing file, directory, outside-root path, wrong task, count, per-file size, and total-size limits. |
| Attachment upload and order | `test_attachment_upload_order_and_upload_failure` uses the browser's normal file input, verifies visible chips preserve input order, and detects a fixture upload error before send. `test_pre_send_manual_changes_and_owner_drafts_are_preserved` rejects an unexpected Owner attachment before the one-send boundary. |
| Single-send guard | `test_normal_ui_send_is_single_and_response_is_raw` and `test_ambiguous_send_is_remembered_and_not_retried` verify one click and no retry for a duplicate or ambiguous key. |
| Response baseline and extraction | The normal send test verifies an old assistant turn containing `PASS` is not returned and asserts the new Markdown, prose, and `<RELAY_CONTROL>` text exactly. |
| Completion and timeout | The normal send test observes fixture streaming and stable completion; `test_response_timeout_distinguishes_confirmed_send` verifies bounded timeout after a confirmed send. |
| Ambiguity guards | Interruption tests cover extra user turns, page URL changes, same-URL reload, and multiple assistant turns; pre-send tests preserve an Owner draft and stop on a manual message or attachment during upload. |

## Phase 2 Integration Evidence

`test_phase2_parser_receives_raw_adapter_text` sends a local fixture prompt through `ChatGPTWebAdapter`, captures the raw valid `rr.v1` text, and passes it to the existing `validate_review_response`; the parser returns typed action `PASS`. The adapter imports and calls no Phase 2 parser.

## Acceptance Matrix

| Gate | Result | Evidence |
|---|---|---|
| `PHASE3_REVIEWER_ADAPTER_BOUNDARY` | PASS | Async `ReviewerAdapter` protocol and `ChatGPTWebAdapter` test coverage. |
| `PHASE3_PORTABLE_BROWSER_PROFILE` | PASS | Profile path assertion and persistent Chromium fixture test. |
| `PHASE3_LOGIN_REQUIRED_FLOW` | PASS | Login control detected; context remains open for manual login. |
| `PHASE3_CONVERSATION_NAVIGATION` | PASS | Valid configured URL opens; fixture navigation failure is typed. |
| `PHASE3_COMPOSER_DETECTION` | PASS | Missing, disabled, and unrelated search textboxes are rejected. |
| `PHASE3_ATTACHMENT_VALIDATION` | PASS | Managed path, type, count, and byte-limit cases. |
| `PHASE3_ATTACHMENT_UPLOAD_FLOW` | PASS | Local Chromium file input, visible ordered chips, upload error. |
| `PHASE3_SINGLE_SEND_GUARD` | PASS | One click for normal and ambiguous/repeated-key sends. |
| `PHASE3_RESPONSE_BASELINE` | PASS | Old assistant turn excluded; relay user identity is matched. |
| `PHASE3_RESPONSE_COMPLETION` | PASS | Streaming/stop signal, visible composer/send control, stable text, timeout. |
| `PHASE3_RAW_RESPONSE_EXTRACTION` | PASS | Exact fixture response string assertion. |
| `PHASE3_AMBIGUITY_GUARDS` | PASS | Extra user, navigation, reload, multiple assistant turns, pre-send change. |
| `PHASE3_PROTOCOL_SEPARATION` | PASS | Raw text is returned without adapter-side routing or parsing. |
| `PHASE3_PHASE2_INTEGRATION` | PASS | Existing parser accepts adapter-captured valid response in integration test. |
| `PHASE1_REGRESSION` | PASS | Full suite: 259 passed, including prior Phase 1 tests. |
| `PHASE2_REGRESSION` | PASS | Full suite: 259 passed, including prior Phase 2 tests. |
| `PHASE3_TESTS` | PASS | Phase 3 test module: 19 passed. |

## Live Acceptance

```text
PHASE3_LIVE_CHATGPT_SMOKE=NOT_RUN
```

Only `python -m reviewrelay.dev.chatgpt_smoke --help` was invoked. No real account, conversation, attachment, or live message was used.

## Safety Evidence

- The adapter uses Playwright's persistent Chromium context and ordinary page navigation, role/DOM locators, and standard file-input upload.
- Browser startup does not select a normal Chrome profile. The configured profile path is resolved under `PortableDataRoot`.
- Login detection surfaces `LOGIN_REQUIRED`; there is no username/password field automation.
- The adapter source contains no OpenAI API client, internal ChatGPT endpoint invocation, cookie/local-storage/auth-header extraction, request replay, subprocess, or shell execution.
- Prompt or response text is never evaluated as script. Response content is returned as visible text and parsed only by the existing Phase 2 API when the caller chooses to do so.
- Attachments must be explicitly supplied and resolve within active task scratch storage; the adapter does not recurse or search repositories.
- Evidence requests are not executed by Phase 3.

## Unresolved Risks

- The live ChatGPT UI was not exercised. Selector names, the `attachment-chip` marker, and completion controls may change; the adapter fails closed when it cannot establish state.
- Idempotency metadata is process-local and does not survive restart. A later state controller must persist send identity before restart recovery.
- Browser lifecycle may use OS-managed temporary files outside the selected root; ReviewRelay-owned profile and task files are managed beneath the data root.
