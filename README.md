# ReviewRelay

ReviewRelay is a small standalone foundation for relaying implementation work to human or AI reviewers with locally generated Git evidence. `SPEC.md` is the canonical product specification.

## Status

Phase 1 core state and Git evidence are implemented. This is a library foundation; it does not start workers, contact reviewers, or provide a user interface.

## Development

Requires Python 3.11 or newer (including Python 3.14), Git, and the project dependencies.

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m pytest
```

## Phase 1 scope

- Explicit portable data-root setup and task durable/scratch storage.
- Safe project YAML parsing and persistence.
- Versioned SQLite task state.
- A task-start operation that captures and persists the actual Git BASE_SHA before worker execution.
- Git baseline and candidate checks, patch/stat/status evidence, changed-source snapshots, and a hashed review-pack manifest.
- Explicit garbage-collection operations for completed scratch and expired archives.
- A worker-report persistence API. Worker text is untrusted narrative and is not treated as evidence.

## Intentionally not implemented

No ChatGPT Web Adapter, Codex Worker Adapter, autonomous review loop, secret scanner, browser automation, arbitrary shell execution, Windows UI, cloud service, or production/release logic is included. Configured test commands are parsed and stored but are not executed by this phase.
