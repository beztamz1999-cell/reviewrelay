# ReviewRelay V1 Phase 2 — Audit Report

## Candidate Identity

```text
BASE_SHA=ef7b73bd07de0178134881237c88522d725ef24f
HEAD_SHA=3ec1c0ef31662272348faf141b71e0c5dc1905b8
BRANCH=main
WORKTREE_CLEAN=YES
```

`HEAD_SHA` is the tested Phase 2 implementation commit. The requested audit and implementation reports are added in a separate report-only commit after this candidate; the final worker response reports the resulting repository HEAD.

## Commands Executed

- Initial identity check: `git status --short --branch; git rev-parse HEAD; git branch --show-current; git log -1 --oneline` → clean `main` at `ef7b73bd07de0178134881237c88522d725ef24f`, matching the expected Phase 1 candidate.
- Targeted suites: `python -m pytest -q tests/test_protocol.py tests/test_evidence_dsl.py tests/test_cycles_review_key.py tests/test_state.py` → final run **189 passed**. An earlier iteration reported 7 failed / 188 passed because the tests expected an unsafe-path code for an empty path; the validator correctly classified it as an invalid request, and the tests were corrected.
- Full suite: `python -m pytest -q` → final run **240 passed in 10.61s**.
- Compile verification: `python -m compileall -q src tests` → exit code 0, no diagnostics.
- Initial tracked diff check: `git diff --check` → exit code 0; Git printed only LF-to-CRLF working-copy notices.
- Exact staged candidate diff check: `git diff --cached --check` → exit code 0.
- Candidate status: `git status --short --branch` after the implementation commit → `## main` (clean).
- Candidate summary: `git show --stat --oneline --summary HEAD` → implementation commit `3ec1c0e`, 13 files changed, 1,470 insertions and 30 deletions.

## Test Evidence

- Targeted protocol, DSL, cycle/key, and migration suite: **189 passed**.
- Entire repository suite, including existing Phase 1 tests: **240 passed**.
- Python compilation: passed with exit code 0.
- Staged whitespace/error check: passed with exit code 0.

## Acceptance Matrix

| Gate | Result | Evidence |
|---|---|---|
| `PHASE2_CONTROL_BLOCK_PARSER` | PASS | `tests/test_protocol.py`: one block, prose boundaries, missing/empty/malformed JSON, duplicate keys, multiple/nested/malformed tags. |
| `PHASE2_PROTOCOL_VERSION` | PASS | Unsupported-version parameter cases reject all values other than `rr.v1`. |
| `PHASE2_ACTION_VALIDATION` | PASS | Exact five-value enum and action-specific required/forbidden fields are tested. |
| `PHASE2_CANDIDATE_BINDING` | PASS | Matching SHA/cycle accepted; changed SHA or cycle rejected. |
| `PHASE2_STALE_REVIEW_GUARD` | PASS | Stale `PASS` and `FIX_REQUIRED` raise `STALE_REVIEW` without returning an action. |
| `PHASE2_FINDINGS_MODEL` | PASS | Allowed severities, malformed shapes, unknown severities, summary/count limits tested. |
| `PHASE2_EVIDENCE_DSL` | PASS | Each of the nine operations returns its typed request; unknown/extra fields reject. |
| `PHASE2_PATH_SAFETY` | PASS | Path-bearing operations reject traversal, drive paths, UNC paths, and POSIX absolute paths; Phase 1 normalization is reused. |
| `PHASE2_EVIDENCE_LIMITS` | PASS | Request count, range span, grep pattern/root count, path length, Git ref length, and log count limits tested. |
| `PHASE2_TEST_REGISTRY_VALIDATION` | PASS | Configured ID accepted; unknown ID and reviewer-supplied command field rejected. |
| `PHASE2_CYCLE_GUARDS` | PASS | Below-limit updates are returned; threshold and zero-limit cases raise the corresponding escalation signal. |
| `PHASE2_REVIEW_KEY` | PASS | Stable for identical inputs; changes with project/task/candidate/cycle identity. |
| `PHASE2_SCHEMA_MIGRATION` | PASS | A Phase 1 version 1 database row migrates to version 2 with old fields preserved and counters save/reload verified. |
| `PHASE1_REGRESSION` | PASS | Entire suite passed: 240 tests. |
| `PHASE2_TESTS` | PASS | Targeted suite passed: 189 tests; full suite passed: 240 tests. |

## Git Evidence

- Initial branch and worktree: `main`, clean; starting commit matched Phase 1 SHA `ef7b73bd07de0178134881237c88522d725ef24f`.
- Tested candidate commit: `3ec1c0ef31662272348faf141b71e0c5dc1905b8`.
- At the tested candidate, `git status --short --branch` returned `## main`.
- Candidate summary: 13 files—README/package metadata, Phase 2 implementation, and tests. No SQLite database, runtime output, or scratch data was committed.
- The two required report files are intentional tracked artifacts in the following report-only commit.

## Migration Evidence

`tests/test_state.py::test_phase1_database_migrates_and_preserves_task_state` constructs a SQLite database with the Phase 1 `tasks` columns and `PRAGMA user_version = 1`, inserts a populated task row, and opens it with the Phase 2 `StateStore`. It verifies schema version 2, the preserved task state, SHA values, review cycle, identities, review key/action, pack hash, and timestamps; new counters default to zero. It then saves non-zero counters and verifies they survive reopening the store.

## Safety Evidence

- Traversal and absolute path tests exercise all path-bearing request kinds against Windows drive/UNC forms, POSIX absolute paths, and `..` traversal. Validation delegates normalization to Phase 1 `normalize_repo_relative_path`.
- Forbidden command kinds (`shell`, `bash`, `cmd`, `powershell`, `python`, `exec`, `run`, `curl`, `wget`, `delete`, `deploy`) reject with `UNSAFE_EVIDENCE_REQUEST`; arbitrary fields such as a test command are rejected by strict schemas.
- Valid stale responses with mismatched SHA/cycle raise `STALE_REVIEW`; no typed routeable decision is returned.
- Evidence count and per-operation bounds reject excess instead of silently truncating. A mixed safe/unsafe request list fails as a whole.
- No Phase 2 evidence request or configured test command is executed.

## Unresolved Risks

- Phase 2 validates lexical paths only. A future executor must enforce resolved repository containment and handle symlinks before reading files.
- A caller that omits project configuration cannot verify a `test_id` against a configured registry; it receives syntax validation only.
- No known blocking Phase 2 risks.
