# ReviewRelay V1 Phase 4 Implementation Report

Date: 2026-09-30. Scope: Codex Worker Adapter only.

## Scope

Implemented the async `WorkerAdapter` boundary and `CodexAppServerAdapter`: direct stdio process transport, initialization, task/thread/repository binding, initial instructions and explicit prompt files, same-thread continuation, structured observable events, authoritative turn completion, interruption, bounded timeouts/shutdown, SQLite persistence, diagnostic traces, and automatic final worker-report capture. Added a deterministic fake subprocess and an explicit isolated live smoke tool.

There is no combined reviewer/worker controller or autonomous loop. Task-level state transitions remain the future controller's responsibility; the adapter updates worker-specific state only.

## Baseline

```text
REQUESTED_BASE_SHA=691cb8ddced06b14c065573ec747b64159180875
VERIFIED_BASE_SHA=691cb8ddced06b14c065573ec747b64159180875
BRANCH=main
INITIAL_WORKTREE_CLEAN=YES
PHASE3_LIVE_CHATGPT_SMOKE=PASS (approved existing acceptance)
```

The initial full baseline run returned **275 passed, 1 failed in 77.01s**. The failure was the existing Phase 3 hydration fixture's six-second readiness timeout. Its isolated rerun passed (**1 passed in 3.08s**) without changing reviewer code. A later staged full run repeated that timeout (**323 passed, 1 failed in 112.30s**). Only that fixture's readiness budget was increased to 20 seconds; its draft-preservation/no-send assertions and production timeouts were preserved. The final complete suite passes all prior behaviors. These failures are retained instead of presenting the initial baseline run as fully passing.

## Worker architecture

- `build_app_server_command` produces an explicit executable plus `app-server --listen stdio://`. `asyncio.create_subprocess_exec` uses separate stdin/stdout/stderr pipes, a fixed repository cwd, and no shell. `process_command` is an explicit subprocess fixture seam; there is no architecture fallback.
- Each process performs one `initialize` request with `clientInfo={name: reviewrelay, title: ReviewRelay, version: 0.1.0}`. Only valid success is followed by `initialized`; task requests never precede that handshake.
- Request IDs correlate RPC responses. One control request is outstanding at a time. Invalid JSON, duplicate keys, invalid envelopes, unknown response IDs, invalid owned activity, and unexpected stdout closure fail with typed errors. Stderr is diagnostic, never protocol.
- `start_task` requires an existing Phase 1 baseline/task record and storage. It validates the Git root, acquires task ownership, records pending startup before the external side effect, creates a thread, persists its ID, then starts a turn. A lost thread-start acknowledgement cannot silently create a replacement thread.
- `resume_task` sends `thread/resume` with the saved ID. It deliberately does not override the resumed cwd before validating the stored thread's repository. Missing, changed, or rejected identities fail closed. `send_instruction` resumes when necessary and supplies only the caller's explicit instruction to the same thread.
- Events arriving before the `turn/start` acknowledgement are buffered within a fixed limit and replayed after the exact turn ID is known. Foreign thread/turn completion cannot own the result. Simultaneous task control calls and another active turn are rejected, rather than queued as later work.
- Only owned `turn/completed` with `status: completed` yields `WorkerTurnResult(COMPLETED)`. Failed and interrupted states raise typed errors; process death, timeout, and malformed protocol remain distinct. `RELAY_WORKER_DONE` is content and cannot override transport status.
- `turn/interrupt` is the supported interruption mechanism. Acknowledgement alone is not completion; the subsequent interrupted turn is not successful. Timeout preserves identity, attempts interruption, and stops the process. Shutdown closes stdin, waits within configured bounds, and force-cleans the process group/tree if necessary. Closing during initialization also unwinds the control operation before SQLite closes.
- Model and reasoning effort are optional explicit settings, passed to the supported RPC fields. Omitted settings use Codex configuration. No model is hard-coded into the adapter and no catalog is treated as entitlement proof.

Protocol reference: [official Codex app-server documentation](https://learn.chatgpt.com/docs/app-server). The installed CLIs' generated schemas and actual live RPC behavior were also inspected; schema bundles reside in managed diagnostics, not source or credentials.

## Thread identity and persistence

The Codex thread ID identifies the worker conversation. An OS PID identifies one disposable app-server process. The accepted live smoke used two different PIDs with one unchanged thread ID.

SQLite schema version **3** adds nullable task fields:

```text
worker_thread_id
worker_repo_path
worker_last_turn_id
worker_last_turn_status
worker_last_event_at
```

A unique index prevents assigning a non-null Codex thread ID to another task. `worker_session_identity` mirrors the Codex thread ID for compatibility. Versions 1 and 2 migrate without rewriting existing task/candidate/review state; legacy opaque session identities are preserved and are not guessed to be Codex thread IDs. Task metadata is synchronized through `TaskStorage.persist_task_record`.

An OS-held managed task lock excludes other adapters and is released on close/process exit. Repository roots are resolved and checked before start, resume, and every turn. Ambiguous persisted startup or an unresolved active resumed turn prevents automatic replacement work. Recovery after a completed turn is covered by a real fake-process restart/resume seam and the live smoke.

## Event model and final response

Normalized categories are `WORKER_STARTED`, `THREAD_CREATED`, `THREAD_RESUMED`, `TURN_STARTED`, `AGENT_MESSAGE_DELTA`, `AGENT_MESSAGE_COMPLETED`, `COMMAND_STARTED`, `COMMAND_COMPLETED`, `FILE_CHANGE`, `TOOL_ACTIVITY`, `UNKNOWN_EVENT`, `TURN_COMPLETED`, `TURN_FAILED`, `TURN_INTERRUPTED`, and `WORKER_PROCESS_EXITED`.

Events carry receive time, thread/turn/item identity, observable text/command/exit/path fields, and safe raw payload. The timeline is bounded. Reasoning, legacy raw model events, and encrypted reasoning content are omitted; hidden chain-of-thought is not exposed.

Completed final-phase agent messages are captured verbatim. On older phase-less messages, the last completed agent message is used; commentary is excluded. The final text is saved using the existing `TaskStorage.persist_worker_report` API at `durable/worker-report.md`. Failed turns do not overwrite the last completed report, and `get_final_response` rejects failed transport. A completed turn with no final text remains transport-completed but raises `WORKER_RESPONSE_MISSING` when a response is requested. Git/file evidence remains authoritative.

Each turn receives a collision-resistant trace path under `active/<project>/<task>/scratch/worker/`. JSONL is bounded to 2 MiB by default and stops at complete-record boundaries; stderr diagnostics are separately bounded to 64 KiB. Known auth/token/password/cookie fields are redacted. This is focused diagnostic redaction, not a secret-scanner subsystem. Regression coverage proves existing completed-task GC removes worker traces while retaining the durable report.

## Files changed

- `README.md`
- `src/reviewrelay/models.py`
- `src/reviewrelay/state.py`
- `src/reviewrelay/worker/__init__.py`
- `src/reviewrelay/worker/base.py`
- `src/reviewrelay/worker/codex_app_server.py`
- `src/reviewrelay/worker/diagnostics.py`
- `src/reviewrelay/worker/errors.py`
- `src/reviewrelay/worker/lock.py`
- `src/reviewrelay/worker/transport.py`
- `src/reviewrelay/dev/codex_smoke.py`
- `tests/fixtures/fake_app_server.py`
- `tests/test_worker_adapter.py`
- `tests/test_codex_smoke.py`
- `tests/test_reviewer_adapter.py` (hydration fixture timing budget only)
- `reports/REVIEWRELAY_V1_PHASE4-implementation-report.md`
- `reports/REVIEWRELAY_V1_PHASE4-audit.md`

## Tests

All automated worker tests use real pipes to a deterministic fake Python subprocess, with no Codex inference quota. They cover process/handshake/correlation failures, command/tool/file events, malformed and foreign events, response selection, same-thread fixes, locks, timeouts/interruption, restart, migrations, managed-path junction escape, bounded diagnostics, and safe smoke cleanup.

```text
python -m pytest -q                         PASS: 324 passed
python -m compileall -q src tests           PASS
git diff --check                            PASS
```

The implementation check before reports returned **324 passed in 86.69s**. After both reports are staged, the same three checks are required again on the final candidate; the exact final run duration and containing commit SHA are returned in the handoff. Reports are included in that one commit and are not edited afterward.

## Live Codex smoke

Accepted command:

```powershell
python -m reviewrelay.dev.codex_smoke --data-root 'G:\REVIEW_RELAY_DATA' --executable 'C:\Users\Admin\AppData\Local\OpenAI\Codex\bin\c6fe824d725f02d7\codex.exe' --reasoning-effort low
```

Codex **0.159.2** reused supported local Codex authentication. Two completed turns in one disposable repository passed: create exactly `REVIEWRELAY_CODEX_SMOKE` plus one LF; clean app-server shutdown/restart; resume the exact saved thread; read the same file without changing its bytes; capture the exact marker response and durable report; remove the disposable repository.

```text
PHASE4_LIVE_CODEX_SMOKE=PASS
THREAD_ID=01a0f286-ceab-7142-968f-4cf232148d0c
TURN1_ID=01a0f286-cffe-7370-bd21-b28e86bce844
TURN2_ID=01a0f287-2710-7cc2-a8e5-a60c406ca6a7
TURN1_STATUS=COMPLETED
TURN2_STATUS=COMPLETED
THREAD_RESUME=PASS
SAME_THREAD_ID=YES
PROCESS_PID_1=27952
PROCESS_PID_2=51520
FINAL_RESPONSE_CONTAINS=REVIEWRELAY_CODEX_SMOKE
DISPOSABLE_REPO_REMOVED=YES
```

Two earlier attempts are documented in the audit: PATH Codex 0.147.0 rejected its configured model, then a catalog-selected model completed transport but could not use that version's Windows sandbox tools. Local sandbox diagnostics established that installed 0.159.2 worked before another inference attempt. There were four live `turn/start` calls total: two unsuccessful acceptance attempts and the accepted create/read pair. No inference retries occurred after the accepted pair. No real ReviewRelay source was modified by smoke, no exec/TUI fallback was used, and no credentials were read or copied by ReviewRelay.

## Deferred

Evidence Executor, autonomous ChatGPT↔Codex state loop, combined orchestration controller, secret scanner, PySide6 UI, AI Manager, and release/deploy behavior remain deferred. Phase 5 has not started.

## Known limitations

- This host's PATH resolves to Codex 0.147.0, which is incompatible with its configured model/native sandbox. Configure the demonstrated 0.159.2 executable explicitly; a later desktop update may change its versioned path. ReviewRelay does not silently choose another executable/model.
- Interactive approval, tool input, and auth-refresh server requests stop with typed errors for caller handling. Phase 4 provides no interaction UI or custom credentials.
- Codex retains its own supported authentication and thread history in its existing home. Losing that history can make exact-thread resume fail; ReviewRelay does not copy credentials or replace the thread.
- Known-field diagnostic redaction is not comprehensive secret scanning. Browser fixture timing remains sensitive to host load; the observed failures and the fixture-only budget adjustment are retained above. Reviewer production code was not changed in Phase 4.
