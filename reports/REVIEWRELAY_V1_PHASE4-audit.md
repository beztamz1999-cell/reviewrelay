# ReviewRelay V1 Phase 4 Audit

Date: 2026-09-30. Candidate scope: worker transport, persistence, events, tests, and isolated live verification.

## Candidate baseline

```text
BASE_SHA=691cb8ddced06b14c065573ec747b64159180875
BRANCH=main
INITIAL_WORKTREE_CLEAN=YES
```

Git verified that exact HEAD with no tracked or untracked changes before implementation. The candidate is the single final commit containing both Phase 4 reports and every file listed in the implementation report. Its SHA is emitted after commit, avoiding a self-referential report SHA or a later report-only commit. No push, merge, tag, rebase, deployment, or Phase 5 work is part of this candidate.

## Protocol evidence

The installed PATH CLI was `codex-cli 0.147.0`; the desktop binary used for accepted live verification was `codex-cli 0.159.2`. `app-server --listen stdio://`, initialization, model catalog, and schema generation were exercised locally. Generated schemas are under `G:\REVIEW_RELAY_DATA\logs\phase4-protocol-schema` and `phase4-protocol-schema-0.159.2`. Reference: [official app-server protocol](https://learn.chatgpt.com/docs/app-server).

The deterministic fixture uses an actual child process with JSON lines on stdout, diagnostics on stderr, and requests on stdin. It verifies `initialize → initialized → thread/start or thread/resume → turn/start`. Responses use request IDs, and out-of-order event/ACK delivery is tested. Invalid initialization never emits `initialized` or task instructions. Unknown valid notifications are retained; malformed JSON, duplicate keys, bad envelopes/IDs, and malformed owned events fail closed. There is no interactive TUI parsing or `codex exec --json` architecture fallback.

## Thread and repository evidence

```text
THREAD_CREATED=PASS
THREAD_ID_CAPTURED=PASS
THREAD_ID_PERSISTED=PASS
THREAD_RESUME=PASS (fake subprocess restart and accepted live restart)
SAME_THREAD_ID=YES
LIVE_THREAD_ID=01a0f286-ceab-7142-968f-4cf232148d0c
```

SQLite reopen after the accepted smoke returned that same thread ID and last turn `01a0f287-2710-7cc2-a8e5-a60c406ca6a7`, status `COMPLETED`. Thread identity is independent of the app-server PIDs. A unique SQLite index rejects assignment to another task. Tests reject missing/wrong Git roots, persisted repository drift, a changed resumed ID/cwd, concurrent adapters/turns/control calls, and unresolved turn/startup outcomes. Failed resume never issues `thread/start` as a replacement.

## Turn evidence

Completed fixture and live turns yield `COMPLETED`; fixture `failed` and `interrupted` statuses raise their respective errors. A failed fixture response containing `RELAY_WORKER_DONE` still fails and writes no success report. A completed fixture response without that marker still completes. Process death, idle/overall timeout, malformed protocol, missing executable, rejected initialization, and required interactions have typed failures. Supported `turn/interrupt` is tested, including bounded cleanup if the child ignores stdin closure. Closing while initialization is pending stops the child and safely unwinds the request.

The first live unsupported-model attempt received authoritative `failed`. The second attempt received authoritative `completed` but its file tools failed: this was correctly **not** live acceptance. Transport completion and worker narrative do not establish file correctness.

## Structured event and trace evidence

The fixture verifies normalized message deltas/completed messages, command start/completion with exit code, file paths, tool activity, lifecycle states, and unknown valid activity. Hidden reasoning/legacy raw model events are omitted, and known auth fields are redacted. A foreign thread or turn cannot complete the owned result. The raw JSONL path is under managed task scratch; a Windows junction escape is rejected before any outside write. Trace/timeline caps and separate stderr redaction/storage are tested. Existing GC removes worker scratch while preserving `durable/worker-report.md`.

The accepted live create turn trace contains **55** JSON event records; the resumed read turn trace contains **31**. Both include owned `turn/completed: completed`, observable messages, and command activity. The final durable worker report, read independently after state reopen, strips to exactly `REVIEWRELAY_CODEX_SMOKE`.

## Restart evidence

The accepted live smoke closed PID **27952**, initialized a new app-server PID **51520**, sent `thread/resume` for `01a0f286-ceab-7142-968f-4cf232148d0c`, then started the read turn on that unchanged ID. The adapter validated the resumed repository before the turn. After cleanup, process inspection found **zero** remaining processes with either PID. Fixture restart coverage independently asserts one `thread/start`, one `thread/resume`, two `turn/start` calls, different process identities, and the same thread.

## Persistence evidence

Schema **3** adds the five worker state/binding fields. Existing Phase 1 migration coverage now traverses `1 → 2 → 3`; the new test explicitly migrates a populated version-2 database and checks exact preservation of candidate/review/counter fields and worker reload. The canonical database remains `<DATA_ROOT>/db/relay.db`. There is no second worker database. Tests also enforce uniqueness across tasks and startup/active-turn recovery exclusion.

## Live smoke attempts and result

All live smoke repositories were disposable and managed beneath `G:\REVIEW_RELAY_DATA\logs\codex-smoke-*/repo`. No real project implementation turn was used for smoke. Model listing and local sandbox probes made no inference requests. Only three smoke command invocations occurred, producing four turn-start requests total.

1. `python -m reviewrelay.dev.codex_smoke --data-root 'G:\REVIEW_RELAY_DATA' --reasoning-effort low`
   - PATH version 0.147.0 inherited configured `gpt-6.1-sol`; upstream rejected it for that ChatGPT-account path. Turn `01a0f27d-4dbf-74e2-937f-35571ea948d4` failed on thread `01a0f27d-4afc-7343-a209-a517f7bae6a4`. No smoke file was created.
   - Initial cleanup hit a read-only Git object and masked the error. Cleanup was corrected with an absolute checked target and bounded read-only-file retry; offline cleanup regressions pass. The original failed evidence was recovered from managed state/trace and the repo was removed.
2. The same command with `--model gpt-5.6-sol` explicitly selected the 0.147.0 catalog default.
   - Thread `01a0f281-727c-7f83-a0b7-e7d78e7f978f`, turn `01a0f281-741b-71d3-829f-b6f30cf8b66c`, transport `completed`. Tool events and final narrative reported Windows sandbox `CreateProcessWithLogonW failed: 2`; independent verification found no correct smoke file. Acceptance remained FAIL; no read turn was sent and the repo was removed.
   - Safe local native-sandbox commands reproduced failure on 0.147.0 and succeeded on the installed desktop 0.159.2 executable. Earlier probes using the documented `sandbox windows` spelling were inconclusive because this installed native CLI forwards its arguments; the corrected `sandbox <absolute-executable> ...` probe established the version difference. No OS sandbox policy, account, firewall, or credential configuration was changed.
3. Accepted invocation:

```powershell
python -m reviewrelay.dev.codex_smoke --data-root 'G:\REVIEW_RELAY_DATA' --executable 'C:\Users\Admin\AppData\Local\OpenAI\Codex\bin\c6fe824d725f02d7\codex.exe' --reasoning-effort low
```

The 0.159.2 catalog and owner configuration selected `gpt-6.1-sol`; actual completed inference demonstrated access. The create turn produced exactly UTF-8 `REVIEWRELAY_CODEX_SMOKE` plus one LF. After app-server restart, the read turn returned the marker on the same thread and the file bytes remained unchanged. No further inference was run after that pair.

```text
PHASE4_LIVE_CODEX_SMOKE=PASS
THREAD_RESUME=PASS
SAME_THREAD_ID=YES
THREAD_ID=01a0f286-ceab-7142-968f-4cf232148d0c
TURN1_ID=01a0f286-cffe-7370-bd21-b28e86bce844
TURN2_ID=01a0f287-2710-7cc2-a8e5-a60c406ca6a7
TURN1_STATUS=COMPLETED
TURN2_STATUS=COMPLETED
FILE_CONTENT_VERIFIED=YES
FINAL_RESPONSE_CONTAINS=REVIEWRELAY_CODEX_SMOKE
WORKER_REPORT_PERSISTED=YES
DISPOSABLE_REPO_REMOVED=YES
```

Accepted evidence: `G:\REVIEW_RELAY_DATA\logs\codex-smoke-d2ad9c948925455da0fb67dc1aa78b1c\result.json`; managed task traces/report: `active/phase4-live/codex-smoke-d2ad9c948925455da0fb67dc1aa78b1c/`. Earlier failure summaries remain under the corresponding smoke IDs. Authentication is reused by Codex itself; ReviewRelay did not read, export, inject, or copy credentials/tokens.

## Regression and acceptance gates

```text
python -m pytest -q                         PASS: 324 passed
python -m compileall -q src tests           PASS
git diff --check                            PASS
```

The initial baseline's Phase 3 fixture timeout and its isolated PASS are documented in the implementation report. Implementation verification before reports returned `324 passed in 86.69s`. The first staged full run then repeated the same fixture timeout (`323 passed, 1 failed in 112.30s`), so the fixture readiness budget was changed from 6 to 20 seconds. Its original draft-preservation/no-send assertions and all reviewer production code/timeouts remain unchanged. The updated staged candidate receives the same full checks before its single commit. Final runtime/SHA are emitted in the handoff; reports are not modified after commit.

```text
PHASE4_WORKER_ADAPTER_BOUNDARY=PASS
PHASE4_APP_SERVER_PROCESS=PASS
PHASE4_INITIALIZE_HANDSHAKE=PASS
PHASE4_THREAD_START=PASS
PHASE4_THREAD_ID_PERSISTENCE=PASS
PHASE4_THREAD_RESUME=PASS
PHASE4_SAME_THREAD_FIX_CONTINUITY=PASS
PHASE4_REPO_BINDING=PASS
PHASE4_TURN_START=PASS
PHASE4_TURN_COMPLETION_STATUS=PASS
PHASE4_EVENT_NORMALIZATION=PASS
PHASE4_RAW_EVENT_TRACE=PASS
PHASE4_FINAL_RESPONSE_CAPTURE=PASS
PHASE4_WORKER_REPORT_PERSISTENCE=PASS
PHASE4_TIMEOUT_FAILURE_HANDLING=PASS
PHASE4_CRASH_RECOVERY=PASS
PHASE4_SCHEMA_MIGRATION=PASS
PHASE1_REGRESSION=PASS
PHASE2_REGRESSION=PASS
PHASE3_REGRESSION=PASS
PHASE4_TESTS=PASS
PHASE4_LIVE_CODEX_SMOKE=PASS
```

## Remaining risks

The host PATH still selects incompatible 0.147.0; production configuration must identify the tested 0.159.2 executable. Its desktop-version path can change on upgrade. Interactive server requests require caller/Owner handling; no custom auth or interaction UI is implemented. Exact-thread resume depends on Codex's own persisted history. Diagnostic redaction is limited to known fields/patterns and is not the deferred secret scanner. Unacknowledged creation or unresolved active-turn state remains fail-closed rather than guessed/replaced. Evidence Executor, autonomous loop, PySide6 UI, AI Manager, and release/deploy behavior remain deferred.
