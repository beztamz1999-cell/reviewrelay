# ReviewRelay

ReviewRelay is a small standalone foundation for relaying implementation work to human or AI reviewers with locally generated Git evidence. `SPEC.md` is the canonical product specification.

## Status

Phases 1–4 are implemented. Phase 3 provides the ChatGPT web transport; Phase 4 provides a separately invoked Codex app-server worker adapter. Live smoke procedures are explicit development tools and are not part of automated tests.

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

- `ReviewerAdapter` defines an asynchronous transport boundary; `ChatGPTWebAdapter` uses Playwright's persistent Chromium context and normal ChatGPT page UI.
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

Evidence execution engine, autonomous review loop, secret scanner, Windows UI, cloud service, arbitrary reviewer-driven shell execution, and production/release logic remain deferred. Configured test commands remain registry data and are not executed by ReviewRelay.

## Phase 4 worker adapter

`WorkerAdapter` is an async boundary. `CodexAppServerAdapter` launches `codex app-server --listen stdio://` directly with separate pipes, performs `initialize` / `initialized`, and creates or resumes a persistent Codex thread. Call Phase 1 `begin_task` before `start_task`. Supply exactly one prompt text or prompt file; call `wait_until_done` and `get_final_response` to capture the completed response. `send_instruction` continues the saved thread, and a new adapter uses `resume_task` after restart. Always await `close` in a `finally` block.

SQLite schema 3 binds one task to one repository and Codex thread, records the latest turn/status/event timestamp, and preserves earlier state. A task lock prevents concurrent adapters. Only `turn/completed` with `completed` status is transport success. Failed, interrupted, timeout, dead-process, and malformed-protocol outcomes have typed errors; no replacement thread or exec fallback is created automatically. Responses are narrative, and reports are persisted through `TaskStorage.persist_worker_report`.

Optional `worker` configuration supports `executable`, `model`, `reasoning_effort`, `sandbox` (`workspace-write` or `read-only`), and `timeouts` (`startup_seconds`, `initialize_seconds`, `request_seconds`, `idle_seconds`, `overall_seconds`, `shutdown_seconds`). Omitted model/effort use Codex configuration. Existing local Codex login is reused through Codex itself; ReviewRelay does not read credential files. Interactive server requests stop with a typed error for caller handling.

The bounded `timeline` contains observable messages, command/file/tool activity, and lifecycle events. Known auth fields are redacted and reasoning/raw model events are omitted from diagnostic traces. Per-turn JSONL and bounded stderr diagnostics live under managed task `scratch/worker`, so existing scratch GC disposes of them. This is diagnostic redaction, not the deferred secret scanner.

Run the explicit two-turn live smoke in disposable managed storage:

```powershell
python -m reviewrelay.dev.codex_smoke --data-root "G:\REVIEW_RELAY_DATA" --model <configured-supported-model> --reasoning-effort low
```

It creates a harmless file, shuts down app-server, resumes the exact saved thread in a new process, reads the file, and removes the disposable repository. `model/list` is informational; only a completed live inference verifies account access. Automated worker tests use a fake stdio subprocess and consume no Codex quota.
