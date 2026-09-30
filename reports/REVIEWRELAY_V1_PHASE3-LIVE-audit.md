# ReviewRelay V1 Phase 3 Live Acceptance Audit

## Baseline and candidate

```text
BASE_SHA=bdcfcfe458e42d0eba315861af4524d51c500efc
BRANCH=main
INITIAL_WORKTREE_CLEAN=YES
IMPLEMENTATION_COMMIT_SHA=68a180233956bafc902d423c56a647b24ae201dd
PRE_FINALIZATION_HEAD_SHA=11466ec5a6aaadea4d03b89b4dfafc1a74c0e9de
PRE_FINALIZATION_WORKTREE_STATUS=PHASE3_CHANGES_PENDING_COMMIT
```

The only configured reviewer conversation used in this task was `https://chatgpt.com/c/6abc2ec9-1848-83ec-b1cd-a105f0941738`. No other conversation URL was opened. Phase 4 was not started.

## Live environment and authentication safeguards

```text
OS=Windows 11 Pro (build 26200)
PYTHON=3.13.15
PLAYWRIGHT=1.63.0
GOOGLE_CHROME=154.0.8037.92
PROFILE=browser-profile/reviewer-chrome under the portable data root
CDP_BIND=127.0.0.1; ephemeral port
```

Google rejected Playwright-managed Chromium with “This browser or app may not be secure.” The adopted two-mode bootstrap uses installed Google Chrome and a dedicated ReviewRelay `--user-data-dir`. Auth Mode has no CDP or Playwright; the Owner signs in and opens the configured conversation manually. Automation Mode relaunches the same dedicated profile with CDP bound to localhost, then Playwright attaches. The Owner's default Chrome profile is never used. A profile lock prevents Auth and Automation modes from overlapping. No credentials, cookies, or tokens were automated, read, exported, injected, or copied.

## Exact-conversation access matrix (2026-09-30)

The same configured URL and dedicated profile were used throughout. The earlier B observation that reported `FAIL` was invalid: Chrome had opened `about:blank`, and the Owner had not yet entered the URL. That result is superseded by the corrected matrix below.

```text
NORMAL_CHROME_CONVERSATION_ACCESS=PASS
CDP_ONLY_CONVERSATION_ACCESS=PASS
CDP_PLAYWRIGHT_EXISTING_TAB_ACCESS=PASS
RESTORED_TAB_AFTER_CLEAN_CHROME_EXIT=PASS
RESTORED_TAB_COMPOSER_READY=PASS
DIRECT_PAGE_GOTO_HTTP_STATUS=403 (PRIOR LIVE RUNS; SAME EXACT URL)
DIRECT_NAVIGATION_RETRIED_AFTER_MATRIX=NO
```

For A, the Owner manually opened the exact conversation in installed Chrome Auth Mode and confirmed that the conversation and composer worked, then exited Chrome cleanly. For B, the Owner manually entered the exact URL in installed Chrome Automation Mode with loopback CDP enabled and Playwright detached, and confirmed the conversation and composer worked. The Owner left that window open for C. The HTTP status for B was not captured.

For C, Playwright attached to the already-open CDP browser. It found one tab whose URL matched the exact configured conversation. On that existing tab, the `Ask ChatGPT` textbox was visible, enabled, and editable. No navigation was performed. After the Owner cleanly exited Chrome, Automation Mode was launched again with session restore enabled. The exact reviewer tab returned in the same dedicated profile; Playwright attached and again verified the composer without navigating. These checks read no message text and sent no prompt.

Prior live attempts using `page.goto(exact_conversation_url)` returned HTTP 403. Combined with A, B, C, and the restored-tab observation, the failure is localized to programmatic direct navigation, not to the account/session, CDP startup, or Playwright attachment to an existing tab. The prior 403 evidence was not reproduced after the matrix; no repeat request was made.

## Production transport change

The installed-Chrome live backend now searches the attached context for exactly one already-restored tab matching the configured conversation URL. It selects that tab, checks explicit login UI and the editable composer, and fails closed if the exact tab is missing, ambiguous, unauthenticated, or not ready. It never calls `page.goto()` in the live CDP path. Offline Playwright Chromium keeps its deterministic fixture navigation path. The centralized composer selector catalog now recognizes the live `Ask ChatGPT` accessible name.

Automation Mode launch includes `--restore-last-session`; the Owner opens the exact conversation during Auth Mode and exits Chrome normally so the dedicated profile restores it. CDP remains bound only to `127.0.0.1`. Auth Mode and Automation Mode cannot hold the profile concurrently.

Live UI inspection found that `Add files and more` is initially disabled during page startup, then opens several file inputs, including the exact local-upload input labelled `Attach files`. The adapter waits a bounded time for that control to become enabled, waits for the matching input to be attached, and excludes the photo/video and library inputs. In Chrome live mode, it confirms the exact selected local filenames at the file-input change event and verifies composer attachment-chip/progress counts before sending; historical message attachments are excluded. Offline fixture mode retains deterministic filename checks. CDP shutdown asks Chrome to close gracefully without closing the restored tab first, preserving session restoration.

Further live inspection found that the visually empty `Ask ChatGPT` contenteditable returns one whitespace character from `inner_text`. The previous truthiness check incorrectly classified this as an Owner draft. The guard now treats whitespace-only composer text as empty while still preserving non-whitespace drafts. The offline regression fixture now covers that live behavior; the existing non-empty draft regression still fails closed. A metadata-only live diagnostic recorded the selected textbox label/type and text lengths, without displaying or logging draft content.

The live composer renders each prompt line in separate paragraph elements; `inner_text` adds presentation newlines around those blocks. Composer reads now reconstruct the direct paragraph blocks and retain the intended line breaks, so the exact prompt is checked before send. Live attachment readiness now verifies the selected input file names at the change event and uses the visible composer chip/progress counts. This accounts for the current live UI's generic Remove accessibility label without accepting pre-existing chips or count mismatches.

The live conversation UI no longer exposes `[data-message-author-role]` on its visible turns. The centralized turn selectors now recognize the observed user bubble class and assistant Markdown container. A regression fixture with that live markup verifies exact user-turn ownership, a new assistant response, completion, and attachment handling.

## Live smoke status

Twelve smoke command executions reached the exact configured URL. The first eleven stopped before the Send click while live-only UI differences were diagnosed and fixed: asynchronous attachment controls, attachment display labels, restored-draft hydration, whitespace-only empty composer text, paragraph-based composer reads, and current live turn markup. The twelfth execution selected only the generated smoke file, filled the exact harmless prompt, and clicked Send once. The CLI then timed out while looking for the user turn because its selectors still expected the old `data-message-author-role` markup; the send result was therefore marked ambiguous by that run. No second send was attempted.

Read-only reconciliation on the same exact existing conversation found exactly one user bubble whose text matched the smoke prompt and exactly one assistant response after that bubble containing the expected marker. That response was stable across consecutive observations, no Stop control was visible, and raw response extraction returned `REVIEWRELAY_SMOKE_OK`. After the turn-selector fix and full regression, a second read-only reconciliation through the new centralized selectors confirmed the same result. No duplicate prompt was sent after the fix.

```text
SMOKE_EXECUTIONS=12
PRE_SEND_ABORTED_EXECUTIONS=11
SEND_CLICK_COUNT=1
MESSAGE_SENT_ONCE=YES (CONFIRMED BY ONE EXACT USER PROMPT IN THE CONFIGURED CONVERSATION)
CLI_SEND_RESULT=AMBIGUOUS_DUE_STALE_TURN_SELECTORS; RECONCILED_READ_ONLY
OWNED_RESPONSE_DETECTED=YES (UNIQUE EXACT PROMPT FOLLOWED BY ONE MARKER RESPONSE)
RESPONSE_COMPLETION=YES (STABLE; NO VISIBLE STOP CONTROL)
RAW_RESPONSE_CAPTURED=YES
RAW_RESPONSE=REVIEWRELAY_SMOKE_OK
EXPECTED_MARKER_FOUND=YES
POST_SELECTOR_FIX_SECOND_SEND=NO (EXISTING MESSAGE RECONCILED READ_ONLY)
POST_SELECTOR_FIX_READ_ONLY_RECONCILIATION=PASS
PHASE3_LIVE_CHATGPT_SMOKE=PASS
```

## Regression evidence

```text
python -m pytest -q                         PASS (276 passed)
python -m compileall -q src tests            PASS
git diff --check                             PASS
```

The suite includes prior Phase 1–3 coverage, backend selection, Chrome launch command construction, profile-lock exclusion, loopback CDP endpoint parsing, explicit login handling, live `Ask ChatGPT` and `Add files and more` labels, delayed button/input readiness, opaque live attachment selection/chip-count and progress completion, Chrome session-preserving shutdown, exact existing-tab selection with no navigation, modern live user/assistant turn selectors, and fail-closed behavior when the configured tab is absent.

## Acceptance matrix

| Gate | Result | Evidence |
|---|---|---|
| `PHASE3_LIVE_AUTH_MODE_MANUAL` | PASS | Owner signed in and opened the exact conversation in normal installed Chrome. |
| `PHASE3_LIVE_DEDICATED_PROFILE` | PASS | Auth and Automation used the same managed ReviewRelay profile, never the default profile. |
| `PHASE3_LIVE_PROFILE_LOCK` | PASS | Existing cross-process lock prevents overlapping modes; full regression passes. |
| `PHASE3_LIVE_CDP_LOOPBACK` | PASS | Chrome command and live endpoint use `127.0.0.1`. |
| `PHASE3_LIVE_AUTOMATION_ATTACH` | PASS | Playwright attached to installed Chrome through CDP. |
| `PHASE3_LIVE_CONVERSATION_MATRIX` | PASS | A, B, C, and clean-exit session restoration all passed for the exact URL. |
| `PHASE3_LIVE_DIRECT_GOTO` | FAIL (prior live attempts) | Direct `page.goto()` returned HTTP 403; it was not retried after the matrix. |
| `PHASE3_LIVE_RESTORED_TAB_REUSE` | PASS | Adapter and live probe found the exact restored tab and usable composer without navigation. |
| `PHASE3_LIVE_SINGLE_SEND` | PASS | One Send click; exactly one matching user prompt appeared in the configured conversation. No retry. |
| `PHASE3_LIVE_OWNED_RESPONSE` | PASS | One assistant response appeared after the unique exact prompt and contained the marker. |
| `PHASE3_LIVE_RESPONSE_COMPLETION` | PASS | Response remained stable across consecutive reads and no Stop control was visible. |
| `PHASE3_LIVE_RAW_EXTRACTION` | PASS | Raw response extracted from the assistant Markdown container: `REVIEWRELAY_SMOKE_OK`. |
| `PHASE3_LIVE_SMOKE_MARKER` | PASS | Expected marker matched exactly. |
| `PHASE1_REGRESSION` | PASS | Full suite: 276 passed. |
| `PHASE2_REGRESSION` | PASS | Full suite: 276 passed. |
| `PHASE3_OFFLINE_REGRESSION` | PASS | Full suite: 276 passed, including whitespace-only composer, paragraph reconstruction, opaque attachments, and modern live turn markup. |

## Safety and scope

- Only the exact configured reviewer conversation was used; no alternate conversation or private endpoint was used.
- No authentication workaround, cookie/token operation, credential automation, or Google-login automation was attempted.
- The only attachment supplied by the successful live smoke was the generated harmless `relay-smoke.txt`; the live input change confirmed that exact file, and the visible composer chip/progress count reached the expected ready state.
- The exact smoke prompt was sent once. The initial CLI confirmation was ambiguous because live turn selectors had changed; read-only reconciliation confirmed the prompt and response, and no duplicate send was made after the selector fix.
- Live acceptance is PASS only on the evidence listed above: one exact user prompt, its one subsequent assistant response, stable completion, raw extraction, and the expected marker.
- Phase 4 was not started.
