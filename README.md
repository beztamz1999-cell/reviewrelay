# ReviewRelay

ReviewRelay is a small standalone foundation for relaying implementation work to human or AI reviewers. **Local Git is execution truth; GitHub is the reviewer-readable mirror.** `SPEC.md` is the canonical product specification.

## Status

Phases 1–6 are implemented as explicit components. Phase 3 provides the ChatGPT web transport; Phase 4 provides a separately invoked Codex app-server worker adapter; Phase 5 is the Local Verification Executor; Phase 6 publishes verified candidates and sends compact GitHub review notifications. No autonomous controller or Phase 7 is implemented. Live smoke procedures are explicit development tools and are not part of automated tests.

Normal source review uploads no patch, worker report, implementation report, audit report or source snapshots. Historical reports and attachment/evidence APIs remain available for compatibility and explicit local diagnostics; they are not prerequisites or automatic fallbacks for GitHub review. New tasks share one committed `.reviewrelay/tasks/<TASK_ID>.md`, not generated narrative reports.

## Development

Requires Python 3.11 or newer (including Python 3.14), Git, and the project dependencies.

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev,browser]"
python -m playwright install chromium
python -m pytest
```

The full test suite can also be run with `python -m pytest -q`. Source compilation can be checked with `python -m compileall -q src tests`.

## Phase 1 scope

- Explicit portable data-root setup and task durable/scratch storage.
- Safe project YAML parsing and persistence.
- Versioned SQLite task state.
- A task-start operation that captures and persists the actual Git BASE_SHA before worker execution.
- Git baseline and candidate checks, patch/stat/status evidence, changed-source snapshots, and a hashed review-pack manifest.
- Explicit garbage-collection operations for completed scratch and expired archives.
- A worker-report persistence API. Worker text is untrusted narrative and is not treated as evidence.

## Phase 2 scope

- A strict `rr.v1` `<RELAY_CONTROL>` parser that returns candidate-bound typed decisions for `PASS`, `FIX_REQUIRED`, `NEED_EVIDENCE`, `OWNER_DECISION_REQUIRED`, and `REVIEW_ERROR`.
- A bounded Evidence DSL for `read_file`, `read_range`, `grep`, `git_show`, `diff_file`, `list_dir`, `test`, `git_log`, and `git_status`.
- Deterministic stale-review checks, fix/evidence cycle guards, review-key generation, and a Phase 1 SQLite migration for cycle counters.
- Evidence requests are validated and modeled only. Phase 2 does not execute evidence requests or configured test commands.

## Phase 3 scope

- `ReviewerAdapter` defines an asynchronous transport boundary; `ChatGPTWebAdapter` uses normal ChatGPT page UI. Live installed-Chrome mode attaches through localhost CDP to the dedicated authenticated profile and reuses the exact restored reviewer tab; offline fixtures retain persistent Playwright Chromium.
- Browser profile data lives under `<DATA_ROOT>/browser-profile/<profile_name>`. Login is manual; the adapter does not enter credentials, read cookies, call private endpoints, or use the OpenAI API.
- An existing same-origin `/c/<id>` conversation URL must be configured. Default base URL is `https://chatgpt.com/`; default browser mode is visible (`headless: false`). Local loopback HTTP is accepted only for offline fixtures.
- Only explicit files under `active/<project>/<task>/scratch/` can be attached. Local defensive limits are 10 files, 20 MiB per file, and 50 MiB total; these are ReviewRelay limits, not claims about ChatGPT product limits.
- Review keys prevent resending within one adapter process. The adapter records a pre-send turn baseline, confirms the relay's user turn, waits for a stable completed assistant turn, and returns its raw visible text. Phase 2 remains responsible for parsing and validating `<RELAY_CONTROL>`.
- Timeouts are configurable using `chatgpt.timeouts.navigation_seconds`, `upload_seconds`, `response_seconds`, and `stability_seconds`.

Example project configuration:

```yaml
chatgpt:
  base_url: "https://chatgpt.com/"
  browser_profile: "default"
  browser_backend: "google-chrome-cdp"
  conversation_url: "https://chatgpt.com/c/your-existing-conversation"
  headless: false
  timeouts:
    navigation_seconds: 30
    upload_seconds: 60
    response_seconds: 600
    stability_seconds: 2
```

To explicitly run the harmless live transport smoke (opens the configured conversation, creates and attaches a generated text file, and sends one marker request):

```powershell
python -m reviewrelay.dev.chatgpt_smoke --data-root "G:\REVIEW_RELAY_DATA" --browser-profile reviewer-chrome --conversation-url "https://chatgpt.com/c/your-existing-conversation" --send
```

The live smoke takes an exclusive ReviewRelay profile lock and uses the installed Google Chrome executable with a dedicated profile at `<DATA_ROOT>/browser-profile/<browser-profile>`. It first launches **Auth Mode** as normal Chrome with no Playwright or CDP. The Owner signs in manually, confirms the existing conversation is visible, and closes that Chrome window. Only after the Auth Mode process exits does ReviewRelay relaunch Chrome with the exact same profile in **Automation Mode**, enable an ephemeral remote-debugging port bound to `127.0.0.1`, and attach Playwright over CDP. If authentication is no longer valid after relaunch, the smoke stops with `LOGIN_REQUIRED`; it never attempts login in Automation Mode. ReviewRelay never opens the Owner's default Chrome profile and does not export, import, read, or inject cookies or credentials. Offline adapter fixtures continue to use Playwright's bundled Chromium backend.

The smoke prints the raw response and requires the new owned response to contain `REVIEWRELAY_SMOKE_OK`; it performs no Codex action. Do not run it in CI or as part of the automated suite.

## Deferred scope

Autonomous review loop, secret scanner, Windows UI, cloud service, arbitrary reviewer-driven shell execution, and production/release logic remain deferred. Configured test commands are executed only when a validated Phase 2 `test_id` selects them during local evidence collection.

## Phase 4 worker adapter

`WorkerAdapter` is an async boundary. `CodexAppServerAdapter` launches `codex app-server --listen stdio://` directly with separate pipes, performs `initialize` / `initialized`, and creates or resumes a persistent Codex thread. Call Phase 1 `begin_task` before `start_task`. Supply exactly one prompt text or prompt file; call `wait_until_done` and `get_final_response` to capture the completed response. `send_instruction` continues the saved thread, and a new adapter uses `resume_task` after restart. Always await `close` in a `finally` block.

SQLite schema 4 preserves the Phase 4 task/repository/Codex-thread bindings and adds Phase 6 publication, event and review tables. A task lock prevents concurrent worker adapters. Only `turn/completed` with `completed` status is transport success. Failed, interrupted, timeout, dead-process, and malformed-protocol outcomes have typed errors; no replacement thread or exec fallback is created automatically. Final responses remain untrusted narrative; `TaskStorage.persist_worker_report` is retained for compatibility, with no required per-task implementation/audit report in the normal GitHub flow.

Optional `worker` configuration supports `executable`, `model`, `reasoning_effort`, `sandbox` (`workspace-write` or `read-only`), and `timeouts` (`startup_seconds`, `initialize_seconds`, `request_seconds`, `idle_seconds`, `overall_seconds`, `shutdown_seconds`). Omitted model/effort use Codex configuration. Existing local Codex login is reused through Codex itself; ReviewRelay does not read credential files. Interactive server requests stop with a typed error for caller handling.

The bounded `timeline` contains observable messages, command/file/tool activity, and lifecycle events. Known auth fields are redacted and reasoning/raw model events are omitted from diagnostic traces. Per-turn JSONL and bounded stderr diagnostics live under managed task `scratch/worker`, so existing scratch GC disposes of them. This is diagnostic redaction, not the deferred secret scanner.

Run the explicit two-turn live smoke in disposable managed storage:

```powershell
python -m reviewrelay.dev.codex_smoke --data-root "G:\REVIEW_RELAY_DATA" --model <configured-supported-model> --reasoning-effort low
```

It creates a harmless file, shuts down app-server, resumes the exact saved thread in a new process, reads the file, and removes the disposable repository. `model/list` is informational; only a completed live inference verifies account access. Automated worker tests use a fake stdio subprocess and consume no Codex quota.

## Phase 5 Local Verification Executor

`LocalEvidenceExecutor` retains its public name and all nine DSL operations. In normal GitHub review, request it for configured tests, local Git status, runtime/environment outputs or generated-artifact verification. ChatGPT reads source, diffs and related files directly from GitHub. Phase 6 does not autonomously route `NEED_EVIDENCE` or dispatch fixes.

`LocalEvidenceExecutor.execute_batch` accepts only Phase 2 typed requests with an `EvidenceExecutionContext` binding project, task, repository, base/candidate SHAs, review cycle, project configuration, and managed task storage. It supports all nine whitelisted evidence operations. Path reads resolve within the repository; Git uses fixed argument arrays; only a configured test registry ID may launch a test process. New test configuration should use an `argv` list. Existing string `command` entries use deterministic double-quote/backslash tokenization without a shell; single quotes and shell metacharacters remain literal characters.

```yaml
tests:
  unit:
    argv: [python, -m, pytest, tests/unit, -q]
```

Results and a SHA-256 manifest are written to `active/<project>/<task>/scratch/evidence/cycle-<NN>/batch-<id>/`. A complete result exposes `upload_artifacts` in manifest-first request order. Incomplete or mutated batches expose no upload paths. A failed configured test is valid evidence with `TEST_FAIL`; timeout and launch failure leave the batch incomplete. Strict clean candidates are required for upload. A dirty candidate can be inspected using `git_status`, but that batch is incomplete. This stage does not contact ChatGPT or Codex.

## Phase 6 GitHub Review Bridge

Optional project configuration (omission disables publishing; `mode: branch` needs only Git):

```yaml
github:
  enabled: true
  remote: origin
  base_branch: main
  mode: pr
```

Use an Owner-authorized named remote. Production supports credential-free `https://github.com/owner/repository.git` or `git@github.com:owner/repository.git`. Reuse supported local Git authentication; missing authentication stops with `GITHUB_AUTH_REQUIRED`. Tokens are not configuration fields. PR mode additionally requires the optional official `gh` CLI and its supported local authentication; branch mode does not. Private-repository access from the ChatGPT account is an Owner prerequisite: ReviewRelay never copies credentials into ChatGPT. An inaccessible candidate must produce `REVIEW_ERROR` with reason `GITHUB_REVIEW_ACCESS_REQUIRED`; no attachment fallback runs automatically.

The explicit caller flow is:

1. Intentionally create and commit `.reviewrelay/tasks/<TASK_ID>.md` before implementation; start from a clean baseline using Phase 1 `begin_task`.
2. Create `GitHubCandidatePublisher` and call `await publisher.bind_task(task_id)` before starting the worker. This binds repository, destination, baseline spec blob and spec-change intent. Ordinary tasks cannot change the spec. Only an intentionally spec-changing task uses `allow_spec_change=True` at this initial binding.
3. Invoke the Phase 4 worker separately. Read and verify local Git directly, then persist the exact candidate SHA and positive review cycle in the existing `TaskRecord`; worker claims are not authoritative.
4. Call `await publisher.publish(PublishRequest(task_id, base_sha, candidate_sha, review_cycle))`. Relay rechecks the clean candidate immediately before pushing the immutable SHA to `refs/heads/reviewrelay/<sanitized-task>-<digest>`, without force. The task branch and optional draft PR are reused across fix cycles. Remote SHA and, in PR mode, same-repository PR HEAD must equal the candidate before a `PublishedCandidate` is returned.
5. Construct `GitHubReviewBridge` with that publisher and the existing `ChatGPTWebAdapter`. Call `notify_reviewer(candidate, conversation_url=configured_url)`, then `capture_review(candidate, sent_result)`. The compact message contains repository, task, exact BASE/HEAD, branch/PR, cycle and spec path, with an empty attachment tuple. Raw owned responses and Phase 2 decisions are persisted separately. Returned actions do not dispatch workers, evidence or release operations.
6. Close the bridge and publisher stores, and await the adapter's existing close operation in the caller's cleanup.

The existing `db/relay.db` records push/PR identities, phases, timestamps, normalized events and reviews. An OS task lock serializes bridge effects. Bounded fixed-argv subprocesses use no shell and suppress interactive auth. Pushes journal planned/in-flight/confirmed states. A restart finds an already-published exact SHA without repushing; uncertain effects are never blindly retried. Public `reconcile` performs only remote/PR reads and local state writes. If no PR exists, it stops with `GITHUB_PR_REQUIRED`; a separate explicit `publish` can finish a not-yet-attempted PR creation. An uncertain PR creation is reconciled by discovery and never repeated automatically.

Unexpected remote history stops with `GITHUB_BRANCH_DIVERGED`. Local candidate mutation, remote mutation or stale cycle invalidates a pending decision. Notification dispatch is durably recorded before sending; a process restart cannot automatically resend an ambiguous or confirmed message. A response not yet captured still requires the current adapter's owned `SendResult`; stored raw responses/validated decisions can be reused after restart. This phase provides component APIs, not an end-to-end CLI or automatic routing controller.

`tests/test_github_bridge.py` uses actual local bare remotes and fake PR/reviewer services. Run it with `python -m pytest -q tests/test_github_bridge.py`, then run full regression and compilation as above. The local-remote constructor switch is an explicit offline test seam, disabled by default. Live GitHub and ChatGPT access are unverified on this development host: no authorized remote is configured, so both optional live gates are `NOT_RUN`. Historical Phase 1–5 reports are preserved; Phase 6 architecture and task intent live in `SPEC.md` and the canonical task spec.
