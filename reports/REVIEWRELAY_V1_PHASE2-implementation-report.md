# ReviewRelay V1 Phase 2 — Implementation Report

## Scope

Implemented the local, deterministic Review Protocol and Evidence DSL layer. Reviewer text is treated as untrusted input; valid responses become typed decisions bound to the current candidate SHA and review cycle. Evidence requests are validated and modeled, but never executed.

No browser/reviewer adapter, worker adapter, autonomous loop, evidence executor, secret scanner, Windows UI, OpenAI API use, or arbitrary shell execution was added.

## Baseline

- Starting SHA: `ef7b73bd07de0178134881237c88522d725ef24f`
- Starting branch: `main`
- Initial worktree: clean (`git status --short --branch` returned `## main`)
- Tested implementation commit: `3ec1c0ef31662272348faf141b71e0c5dc1905b8`
- The two requested report artifacts are committed afterward in a report-only commit; the implementation SHA above is the code, test, and README candidate verified by the recorded test run.

## Architecture Changes

- `src/reviewrelay/protocol.py` extracts exactly one literal `<RELAY_CONTROL>...</RELAY_CONTROL>` block, parses strict JSON (including duplicate-key and non-standard-number rejection), validates action-specific fields, and produces `ValidatedReview` with typed actions and findings.
- `src/reviewrelay/evidence_dsl.py` defines a typed union for the nine allowed evidence operations and validates each schema without executing requests.
- Candidate SHA and cycle are compared exactly with caller-supplied current values. A valid but stale response raises `StaleReview` (`STALE_REVIEW`) and does not return a routeable decision.
- `src/reviewrelay/cycles.py` returns a persistable `TaskRecord` with the next fix/evidence count or raises a typed limit error that signals `OWNER_ESCALATION_REQUIRED`.
- `src/reviewrelay/review_key.py` hashes a canonical JSON array `[project_id, task_id, candidate_sha, cycle]` with SHA-256 and prefixes the result with `rr.v1:`.
- Phase 1 already persisted `last_sent_review_key` and `last_review_action`; those fields remain unchanged. Phase 2 adds the two cycle counters through a versioned SQLite migration.

## Files Changed

- `README.md`
- `pyproject.toml`
- `src/reviewrelay/cycles.py`
- `src/reviewrelay/errors.py`
- `src/reviewrelay/evidence_dsl.py`
- `src/reviewrelay/models.py`
- `src/reviewrelay/protocol.py`
- `src/reviewrelay/review_key.py`
- `src/reviewrelay/state.py`
- `tests/test_cycles_review_key.py`
- `tests/test_evidence_dsl.py`
- `tests/test_protocol.py`
- `tests/test_state.py`
- `reports/REVIEWRELAY_V1_PHASE2-implementation-report.md`
- `reports/REVIEWRELAY_V1_PHASE2-audit.md`

## Protocol Contract

- Supported version: `rr.v1` only.
- Exactly one case-sensitive `<RELAY_CONTROL>` opening tag and one `</RELAY_CONTROL>` closing tag are required. Prose outside the block is allowed.
- JSON must be an object with required fields `protocol`, `candidate_sha`, `cycle`, and `action`. Duplicate keys, malformed JSON, empty blocks, ambiguous tags, and non-standard JSON constants fail closed; there is no repair step.
- The only actions are `PASS`, `FIX_REQUIRED`, `NEED_EVIDENCE`, `OWNER_DECISION_REQUIRED`, and `REVIEW_ERROR`.
- `PASS` may include findings. `FIX_REQUIRED` requires a non-empty opaque `worker_instruction` and may include findings. `NEED_EVIDENCE` requires a non-empty set of fully valid requests. Owner and reviewer error actions may include human-readable `reason` and/or `context`.
- Findings contain exactly `severity` and `summary`; severity is `blocking`, `warning`, or `info`.
- The control block is limited to 1,000,000 characters; at most 100 findings are accepted; summaries are limited to 4,096 characters and routing/context text to 16,384 characters.
- Error codes include the required parser, protocol, action, payload, stale-review, finding, evidence, test-ID, and cycle-limit codes. Cycle-limit errors carry the `OWNER_ESCALATION_REQUIRED` routing signal.

## Evidence DSL

The accepted operations and their fields are:

| Operation | Required fields | Validation |
|---|---|---|
| `read_file` | `path` | Repository-relative path, Phase 1 path validator, maximum 512 characters. |
| `read_range` | `path`, `start_line`, `end_line` | Positive integers, inclusive span at most 500 lines. |
| `grep` | `pattern`, `roots` | Literal text, non-empty, at most 256 characters; 1–8 validated repository-relative roots. Shell-looking characters remain data and are not interpreted. |
| `git_show` | `ref`, `path` | Bounded Git revision data (maximum 128 characters; option-like/control syntax rejected) and validated path. |
| `diff_file` | `path`, `base_ref`, `head_ref` | Both refs are bounded and validated as data; path is validated. |
| `list_dir` | `path` | Validated repository-relative path. |
| `test` | `test_id` | Identifier only; when a `ProjectConfig` is supplied it must exist in that configured registry. Command text and extra fields are rejected. |
| `git_log` | `max_entries`; optional `path` | Integer range 1–100 and optional validated path. |
| `git_status` | none | Strictly rejects additional parameters. |

At most 10 evidence requests are accepted per response. The complete list is validated before a typed tuple is returned; any invalid/unsafe member rejects the whole action. Generic command kinds such as `shell`, `powershell`, `cmd`, `python`, `exec`, `curl`, `delete`, and `deploy` are rejected. No request is executed in Phase 2.

## Persistence Changes

SQLite schema version is now 2. New databases are created at version 2. A version 1 database is migrated transactionally with `ALTER TABLE` additions for `fix_cycle_count` and `evidence_cycle_count`, both non-negative and defaulting to zero. Existing task identity, state, SHA values, cycle, review key/action, pack hash, and timestamps are preserved. The migration test creates a Phase 1-shaped version 1 database, opens it through `StateStore`, checks the preserved row and version, then saves and reloads non-zero counters.

Cycle helpers return an updated immutable `TaskRecord`; a later caller persists it through the existing `StateStore.save` method. They do not start a loop or mutate SQLite implicitly.

## Tests

- Targeted command: `python -m pytest -q tests/test_protocol.py tests/test_evidence_dsl.py tests/test_cycles_review_key.py tests/test_state.py` → **189 passed**.
- Full command: `python -m pytest -q` → **240 passed** (final run: 10.61 seconds). This includes the complete Phase 1 suite.
- Compile command: `python -m compileall -q src tests` → exit code 0.
- Diff check: `git diff --cached --check` → exit code 0. The earlier `git diff --check` also exited 0 and emitted only Git's LF-to-CRLF working-copy notices.

## Deferred Scope

Still unimplemented: ChatGPT Web Adapter, Codex Worker Adapter, evidence execution engine, autonomous loop, secret scanner, and Windows UI. Browser automation, OpenAI API use, generic shell execution, and AI Manager integration also remain outside Phase 2.

## Known Limitations

- The Evidence DSL validates lexical repository paths. A future executor must resolve paths and enforce repository containment at access time, including symlink handling.
- A `test` ID can only be checked against a registry when the caller supplies `ProjectConfig`; Phase 2 never runs the configured command.
- Cycle counters become durable when the returned record is saved; Phase 2 intentionally has no automatic state controller.
- Candidate binding is exact string equality. The caller must supply the authoritative current Git SHA from the Phase 1 candidate verification path.
