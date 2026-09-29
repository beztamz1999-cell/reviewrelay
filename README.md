# ReviewRelay

ReviewRelay is a small standalone foundation for relaying implementation work to human or AI reviewers with locally generated Git evidence. `SPEC.md` is the canonical product specification.

## Status

Phase 1 and Phase 2 are implemented. Phase 3 is not implemented. This remains a local library foundation; it does not start workers, contact reviewers, or provide a user interface.

## Development

Requires Python 3.11 or newer (including Python 3.14), Git, and the project dependencies.

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
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

## Deferred scope

No ChatGPT Web Adapter, Codex Worker Adapter, evidence execution engine, autonomous review loop, secret scanner, browser automation, arbitrary shell execution, Windows UI, cloud service, or production/release logic is included. Configured test commands remain registry data and are not executed by this phase.
