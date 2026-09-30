# ReviewRelay V1 Phase 3 Live Acceptance Audit

## Baseline and candidate

```text
BASE_SHA=bdcfcfe458e42d0eba315861af4524d51c500efc
BRANCH=main
INITIAL_WORKTREE_CLEAN=YES
IMPLEMENTATION_COMMIT_SHA=68a180233956bafc902d423c56a647b24ae201dd
```

The requested reviewer conversation was `https://chatgpt.com/c/6abc2ec9-1848-83ec-b1cd-a105f0941738`. No other conversation URL was used. Phase 4 was not started.

## Live environment

```text
OS=Windows 11 Pro (build 26200)
PYTHON=3.13.15
PLAYWRIGHT=1.63.0
GOOGLE_CHROME=154.0.8037.92
CHATGPT_ORIGIN=https://chatgpt.com
HEADFUL=YES
PROFILE=browser-profile/reviewer-chrome (beneath the configured portable data root)
CDP_BIND=127.0.0.1; ephemeral port
```

The installed Google Chrome ran with a dedicated ReviewRelay `--user-data-dir`. Auth Mode had no Playwright or remote-debugging flags. The Owner reported completing sign-in and closing Auth Mode Chrome; the process was confirmed closed before Automation Mode began. Automation Mode relaunched the same profile, enabled CDP only on `127.0.0.1`, and Playwright attached to it. No default Chrome profile was selected. No cookie, token, or credential material was read, transferred, or injected.

## Authentication incompatibility and adopted bootstrap

The initial Playwright-managed Chromium live attempt reached Google authentication, which rejected that browser with “This browser or app may not be secure.” No bypass or credential automation was attempted. Phase 3 live startup now supports the two-mode installed-Chrome flow: normal Chrome for manual Owner authentication, then a separate Automation Mode launch of the same dedicated profile with localhost-only CDP. A cross-process profile lock prevents the two ReviewRelay modes from overlapping. Playwright Chromium remains the offline fixture backend.

A later Windows cleanup run exposed that Ctrl-Break is provided by Python's `signal` module on this host, not `subprocess`; Chrome cleanup now uses the supported signal and the Automation Mode process exited. Profile-process inspection after the final live attempt found zero Chrome processes using the ReviewRelay profile.

## Live acceptance result

The authenticated-profile Automation Mode attached successfully and reported Chrome 154.0.8037.92. Navigation to the one configured reviewer conversation returned HTTP 403 on both the first post-authentication run and the final Automation-only retry. The page did not expose a detected ChatGPT login control, so the evidence does not establish whether this is an expired session or conversation access denial. The adapter fails closed with `CONVERSATION_NAVIGATION_FAILED`; it does not mislabel an unrecognized 403 as `LOGIN_REQUIRED`.

Both attempts stopped before the send boundary. The harmless generated `relay-smoke.txt` was the only attachment prepared. No smoke prompt was submitted and no response was captured.

```text
AUTH_MODE_OWNER_REPORTED_SIGN_IN=YES
AUTH_MODE_CHROME_CLOSED=YES
AUTOMATION_MODE_CDP_ATTACH=YES
CONFIGURED_CONVERSATION_HTTP_STATUS=403
LOGIN_REQUIRED_UI_DETECTED=NO
MESSAGE_SENT_ONCE=NO
OWNED_RESPONSE_DETECTED=NOT_REACHED
RESPONSE_COMPLETION=NOT_REACHED
RAW_RESPONSE_CAPTURED=NOT_REACHED
EXPECTED_MARKER_FOUND=NOT_REACHED
PHASE3_LIVE_CHATGPT_SMOKE=FAIL
```

No selector change was justified: ChatGPT rejected navigation before the adapter could establish a usable conversation/composer. The minimum transport correction checks for explicit login UI before classifying a 4xx response, so an expired session that renders a login page returns `LOGIN_REQUIRED`. Regression coverage exercises that 403-plus-login-page case. An unrecognized 403 remains a typed navigation failure; no alternate conversation, private endpoint, or authentication workaround was attempted.

## Regression evidence

Commands run on the final implementation before this report was written:

- `python -m pytest -q` — **267 passed in 28.78s**.
- `python -m compileall -q src tests` — passed.
- `git diff --check` — passed.

Coverage includes offline Playwright Chromium, backend selection, Auth/Automation Chrome command construction, same-profile mode locking, localhost CDP endpoint parsing, Windows Ctrl-Break cleanup, explicit login UI on an HTTP 403 response, and prior Phase 1–3 regressions.

Live commands used the exact configured URL above. The final retry was:

```text
python -m reviewrelay.dev.chatgpt_smoke --data-root G:\REVIEW_RELAY_DATA --conversation-url https://chatgpt.com/c/6abc2ec9-1848-83ec-b1cd-a105f0941738 --automation-only --send
```

It exited before message preparation because conversation navigation returned HTTP 403. The explicit `--send` flag authorizes only the one harmless smoke message if and when the page is usable; it did not result in a submission on these runs.

## Acceptance matrix

| Gate | Result | Evidence |
|---|---|---|
| `PHASE3_LIVE_AUTH_MODE_MANUAL` | PASS | Owner reported sign-in; normal Auth Mode Chrome was closed before Automation Mode. |
| `PHASE3_LIVE_DEDICATED_PROFILE` | PASS | Same managed ReviewRelay profile path used in both modes; no default profile. |
| `PHASE3_LIVE_PROFILE_LOCK` | PASS | Cross-process lock test passed; Auth and Automation cannot acquire the same ReviewRelay lock concurrently. |
| `PHASE3_LIVE_CDP_LOOPBACK` | PASS | Chrome command binds to `127.0.0.1`; adapter endpoint is built from the ephemeral port as a loopback URL. |
| `PHASE3_LIVE_AUTOMATION_ATTACH` | PASS | Playwright attached to installed Chrome 154.0.8037.92 using the dedicated profile. |
| `PHASE3_LIVE_LOGIN_REQUIRED_FLOW` | PASS (fixture) | Explicit login UI on HTTP 403 returns `LOGIN_REQUIRED`; live page did not expose a recognized login signal. |
| `PHASE3_LIVE_CONVERSATION_NAVIGATION` | FAIL | Exact configured conversation returned HTTP 403 twice. |
| `PHASE3_LIVE_SINGLE_SEND` | NOT_REACHED | No usable conversation; zero prompts submitted. |
| `PHASE3_LIVE_OWNED_RESPONSE` | NOT_REACHED | No response was generated. |
| `PHASE3_LIVE_RESPONSE_COMPLETION` | NOT_REACHED | No response was generated. |
| `PHASE3_LIVE_RAW_EXTRACTION` | NOT_REACHED | No response was generated. |
| `PHASE3_LIVE_SMOKE_MARKER` | NOT_REACHED | No response was generated. |
| `PHASE1_REGRESSION` | PASS | Full suite: 267 passed. |
| `PHASE2_REGRESSION` | PASS | Full suite: 267 passed. |
| `PHASE3_OFFLINE_REGRESSION` | PASS | Full suite: 267 passed. |

## Safety and remaining blocker

- Auth Mode launches installed Chrome without Playwright or CDP. Automation Mode uses the same dedicated ReviewRelay profile and localhost-only CDP.
- Auth Mode and Automation Mode share the profile lock; the Owner's default Chrome profile is not used.
- No cookies or tokens were read, exported, injected, or copied. No credentials or Google login were automated.
- Prompt/response content was not sent or captured. Only the generated harmless `relay-smoke.txt` was prepared.
- Live acceptance remains blocked by HTTP 403 on the configured conversation. A valid account session alone was insufficient evidence of access to that conversation. No bypass was attempted.


## Follow-up browser boundary diagnostic (2026-09-30)

The diagnostic used the same exact reviewer URL and the same dedicated ReviewRelay profile. The ReviewRelay profile lock was held across both browser modes. Auth Mode was launched with installed Google Chrome, without CDP or Playwright. Automation Mode used the same Chrome executable and profile with `--remote-debugging-address=127.0.0.1`; Playwright remained unattached during the manual B observation.

```text
NORMAL_CHROME_CONVERSATION_ACCESS=PASS
CDP_ONLY_CONVERSATION_ACCESS=FAIL
CDP_PLAYWRIGHT_EXISTING_TAB_ACCESS=NOT_RUN
CDP_ONLY_HTTP_STATUS=NOT_CAPTURED_BY_OWNER
DIRECT_PAGE_GOTO_HTTP_STATUS=403 (PREVIOUS RUNS; SAME EXACT URL)
DIRECT_NAVIGATION_RETRIED_AFTER_MATRIX=NO
```

For A, the Owner manually opened the exact conversation in normal Auth Mode Chrome and reported that the conversation and composer were usable. For B, the Owner manually opened the same exact URL in Chrome with localhost-only CDP and no Playwright attached, and reported that the conversation/composer were not usable. The Owner did not report an HTTP status for B. C was not run because its stated precondition, B passing, was false. No Playwright attachment or page navigation occurred during B.

The first observed failing boundary is the transition from normal Chrome Auth Mode to CDP-enabled Chrome Automation Mode, before Playwright attaches. Prior runs of `page.goto(exact_conversation_url)` in CDP + Playwright returned HTTP 403, but the B failure without Playwright means `page.goto()` is not the only condition associated with the access failure. The evidence localizes the problem to the CDP-enabled browser/session path; it does not isolate the CDP flag from other startup-mode differences, and the B HTTP status was not captured. The same URL was not requested again after the matrix because the 403 had already been observed.

The dedicated profile had zero Chrome processes after the Owner selected Chrome menu → Exit. The profile lock was then released. No production code or selectors changed: B failed, so existing-tab reuse through Playwright was not established and the restored-tab strategy was not adopted. The prior regression result remains applicable to the unchanged code: `python -m pytest -q` — 267 passed in 28.78s.
