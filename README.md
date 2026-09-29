# ReviewRelay

ReviewRelay is a small standalone foundation for relaying implementation work to human or AI reviewers with locally generated Git evidence. `SPEC.md` is the canonical product specification.

## Status

Phases 1, 2, and 3 are implemented. Phase 3 provides an explicit ChatGPT web transport. It does not start workers, run evidence requests, or provide a user interface. The live ChatGPT smoke procedure is manual and is not part of automated tests.

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
python -m reviewrelay.dev.chatgpt_smoke --data-root "G:\REVIEW_RELAY_DATA" --conversation-url "https://chatgpt.com/c/your-existing-conversation" --send
```

The first login may require the Owner to sign in manually in the opened persistent browser. The smoke prints the raw response and performs no Codex action. Do not run it in CI or as part of the automated suite.

## Deferred scope

Codex Worker Adapter, evidence execution engine, autonomous review loop, secret scanner, Windows UI, cloud service, arbitrary shell execution, and production/release logic remain deferred. Configured test commands remain registry data and are not executed by ReviewRelay.
