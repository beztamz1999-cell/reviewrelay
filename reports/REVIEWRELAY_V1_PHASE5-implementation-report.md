# ReviewRelay V1 Phase 5 Implementation Report

Date: 2026-09-30. Scope: local execution of Phase 2 validated evidence requests. No reviewer or worker orchestration was added.

## Scope and baseline

Baseline HEAD was `413a9911592387adeb94aed484926aa83cc16663` on `main`, with a clean tracked and untracked worktree. Baseline regression: `python -m pytest -q` returned **324 passed in 82.90s**. Phase 4 live acceptance and same-thread resume were already accepted. This candidate adds local evidence execution only.

## Evidence executor architecture

`LocalEvidenceExecutor.execute_batch` takes a non-empty tuple of exact Phase 2 request dataclasses and an `EvidenceExecutionContext`. Each typed object is revalidated through the Phase 2 DSL. Raw dictionaries are rejected. The context binds project/task identity, configured and actual Git root, base/candidate SHA, review cycle, project configuration, and `TaskStorage`. Both SQLite and durable task metadata must match. The executor checks candidate HEAD and porcelain status before and after the batch and before each operation. A changed HEAD or worktree invalidates the entire batch and returns no upload paths.

A task-local exclusive lock prevents overlapping batches, and a unique batch directory permits subsequent evidence requests in the same review cycle. A stale lock after an abrupt crash fails closed until diagnosed. Strict candidate upload requires a clean worktree. A dirty candidate may be reported by `git_status`, but remains an incomplete, non-uploadable batch. A configured test that exits nonzero produces valid `TEST_FAIL` evidence; timeout, launch failure, or candidate mutation leaves the batch incomplete.

## Path security

Phase 2 lexical validation is repeated at execution. Path-bearing operations resolve under the configured repository root with `Path.resolve`; resolved targets outside the root are rejected. Existing targets are required for filesystem reads/search/listing. This blocks traversal, drive/UNC absolute paths, and outside symlink or Windows junction targets. Recursive grep checks every encountered entry and does not traverse `.git` or symlinks. Git path operations use a validated path and fixed Git arguments; `diff_file` and `git_log` place paths after `--`, while `git_show` uses a validated ref/path blob expression. The portable data root must be outside the task repository.

## Operations

| Phase 2 request | Local behavior |
|---|---|
| `read_file` | UTF-8 regular text only; full content or explicit size/binary failure. |
| `read_range` | Bounded file read, numbered requested lines, actual EOF range metadata. |
| `grep` | Literal text across checked roots, stable path/line output, binary skips and explicit scan/result truncation. |
| `git_show` | Bounded Git blob output for validated ref and path. |
| `diff_file` | Bounded fixed-argv Git diff between validated refs, with `--` path separation. |
| `list_dir` | Immediate stable-name listing, types and relative paths, bounded entries and explicit truncation. |
| `test` | Trusted registry ID only, no shell, bounded process tree, timeout and separate output caps. |
| `git_log` | Fixed internal format and Phase 2 entry count, optional `--` path. |
| `git_status` | HEAD, branch in porcelain `-b` output, clean flag, bounded status. |

## Test command safety

New project YAML can specify `tests.<id>.argv` as an explicit list. Existing `command` strings remain supported with deterministic double-quote/backslash tokenization; they are never passed to a shell. Single quotes and shell metacharacters are literal arguments. Ambiguous `argv` plus `command`, empty argv, and Windows batch/shell interpreter executables are rejected. The reviewer supplies only `test_id`; the selected command comes from trusted project configuration. `asyncio.create_subprocess_exec` sets repository cwd, drains stdout/stderr separately to configured caps, enforces a timeout, and cleans the process group/tree. Windows uses a kill-on-close Job Object; POSIX uses a new process group.

## Default resource limits

| Resource | Default |
|---|---:|
| `read_file` bytes | 256 KiB |
| `read_range` source/output bytes | 8 MiB / 128 KiB |
| `grep` files/scanned bytes/matches/output | 2,000 / 16 MiB / 300 / 128 KiB |
| Git output / Git timeout | 256 KiB / 20 s |
| Directory entries | 300 |
| Test stdout/stderr/timeout | 256 KiB / 128 KiB / 120 s |
| Batch artifact bytes | 2 MiB |

All limits are configurable through `ExecutionLimits`. Full-file and Git blob overflows fail instead of silently truncating. Grep, directory, and test output report truncation explicitly. Binary/NUL files and invalid UTF-8 are rejected for text reads; grep counts and skips them. A manifest that cannot fit the configured batch cap makes the result incomplete with no upload paths.

## Artifact format and upload order

Artifacts live at `<DATA_ROOT>/active/<project>/<task>/scratch/evidence/cycle-<NN>/batch-<id>/`. Each request has a numbered artifact. `evidence-manifest.json` records protocol version, task/candidate/cycle identity, HEAD before/after, creation time, complete/status, request statuses and truncation, plus SHA-256 calculated from every persisted request artifact. `EvidenceBatchResult.upload_artifacts` is exactly manifest first, then request artifacts in request order, only when the batch is complete. Existing scratch GC deletes these artifacts while preserving durable task data.

## Files changed

- `README.md`
- `src/reviewrelay/config.py`
- `src/reviewrelay/evidence_process.py`
- `src/reviewrelay/evidence_executor.py`
- `tests/test_config.py`
- `tests/test_evidence_executor.py`
- `reports/REVIEWRELAY_V1_PHASE5-implementation-report.md`
- `reports/REVIEWRELAY_V1_PHASE5-audit.md`

## Tests

Baseline: **324 passed in 82.90s**. Focused development checks: **30 passed, 1 skipped in 23.42s** for executor/config tests; the Windows junction matrix then passed **1 in 3.93s**. The full Phase 5 regression reached **346 passed, 1 skipped in 114.90s**. The ordinary symlink test skipped because this Windows host cannot create symlinks; the junction escape test executed. The exact staged final `pytest`, `compileall`, and diff check are run after this report update and are stated in the final handoff. All tests use temporary local Git repositories and configured harmless local commands.

## Deferred and known limitations

The autonomous reviewer/worker controller, automatic fix routing, secret scanner, PySide6 UI, AI Manager, and release/deployment remain deferred. Phase 5 does not contact ChatGPT or invoke Codex. A configured test is trusted project code and can itself perform arbitrary repository changes; ReviewRelay invalidates evidence if it changes the candidate, but cannot undo those changes. File and junction containment is checked at access time; an adversarial external process that replaces paths concurrently remains a filesystem race outside this phase's isolation model. An abruptly abandoned evidence lock needs local diagnosis before that task can collect more evidence.
