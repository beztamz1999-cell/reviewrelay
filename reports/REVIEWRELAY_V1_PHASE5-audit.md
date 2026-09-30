# ReviewRelay V1 Phase 5 Audit

Date: 2026-09-30. Evidence is from the local repository and temporary Git fixture runs. No live model smoke was requested or run.

## Candidate

```text
BASE_SHA=413a9911592387adeb94aed484926aa83cc16663
BRANCH=main
INITIAL_WORKTREE_CLEAN=YES
BASELINE_TEST_RESULT=324 passed in 82.90s
```

The final Phase 5 commit contains source, regression tests, and both reports in one tested candidate. Final SHA and the final regression result are emitted in the handoff after the commit; there is no report-only follow-up commit.

## Path escape evidence

Phase 2 tests already reject `..`, absolute drive and UNC paths for every path-bearing DSL operation. New executor tests reject typed requests that attempt to smuggle those forms past the typed boundary. On this Windows host, a real directory junction pointed from a temporary Git repository to an external directory. `read_file`, `read_range`, `grep`, `list_dir`, `git_show`, `diff_file`, and `git_log` each returned `UNSAFE_PATH` and no upload artifacts. The direct junction regression passed **1 in 3.93s**. Ordinary symlink creation is unavailable on this host, so that test is explicitly skipped; the junction check is actual Windows filesystem coverage, not a mocked path.

## Operation matrix

| Operation | Implemented | Tested | Bounded |
|---|---|---|---|
| `read_file` | YES | YES, text/binary/missing/oversize | Full-file byte cap |
| `read_range` | YES | YES, lines/EOF and junction rejection | Source and output caps; Phase 2 line span |
| `grep` | YES | YES, literal, multiple roots, binary skip, caps and junction | File/byte/match/output caps |
| `git_show` | YES | YES, historical blob, missing path, cap, junction | Output and time caps |
| `diff_file` | YES | YES, fixed refs/path and junction | Output and time caps |
| `list_dir` | YES | YES, entries/truncation and junction | Immediate entry cap |
| `test` | YES | YES, pass/fail/timeout/literal/legacy/output caps/mutation | Time, stdout, stderr and process tree |
| `git_log` | YES | YES, fixed format/path and junction | Phase 2 entries, output and time |
| `git_status` | YES | YES, clean/dirty | Output and time |

## Test runner evidence

```text
SHELL_INVOCATION=NO
EXECUTABLE_SOURCE=trusted project test registry only
REVIEWER_INPUT=test_id only
PROCESS_CWD=bound repository root
PASS=TEST_PASS
NONZERO=TEST_FAIL, valid test evidence
TIMEOUT=TEST_TIMEOUT, incomplete batch
LAUNCH_FAILURE=TEST_EXECUTION_ERROR, incomplete batch
```

The fixture passes `; $(whoami)` as one literal argument and observes it unchanged in the output. Legacy double-quoted arguments are split deterministically, without shell expansion. Config tests cover explicit argv round-trip and rejection of a command/argv combination. Output cap tests verify separate `stdout_truncated` and `stderr_truncated` markers. A Windows Job Object or POSIX process group bounds descendant lifecycle.

## Candidate mutation evidence

A configured harmless fixture test commits a new file after the evidence batch captures candidate A. The batch observes HEAD B, returns `CANDIDATE_MUTATED_DURING_REVIEW`, marks a later request `NOT_EXECUTED`, writes an incomplete manifest, and exposes zero upload artifacts. Binding tests reject wrong task, repository and candidate before execution. A dirty worktree `git_status` result may be recorded locally but is explicitly incomplete and non-uploadable.

## Artifact and storage evidence

The local batch fixture collects `read_file`, `grep`, `git_status`, and a passing configured test. Its result is complete, with upload paths in manifest-first request order. Independently calculated SHA-256 values match every persisted artifact. Repeated batches in one review cycle receive distinct directories. Task scratch contains all evidence, and existing GC removes it while retaining a durable report. A task lock rejects overlapping batch use. Total byte limit tests invalidate overlarge output with no upload paths.

## Codex and ChatGPT quota evidence

```text
CODEX_INVOCATIONS=0
CHATGPT_INVOCATIONS=0
```

The Phase 5 module imports neither adapter and never calls either service. Local verification uses real temporary Git repositories and harmless configured Python commands. Earlier Phase 4 live smoke is not repeated.

## Regression

Baseline full suite: **324 passed in 82.90s**. Focused Phase 5/config verification: **30 passed, 1 skipped in 23.42s**, followed by an expanded real Windows junction matrix **1 passed in 3.93s**. The full Phase 5 regression reached **346 passed, 1 skipped in 114.90s**. The exact final staged full-suite, compilation, and diff results are returned in the final handoff after this report update enters the candidate.

## Remaining risks

Ordinary symlink creation could not be tested on this Windows host; the equivalent external Windows junction escape matrix passed. A trusted configured test can change the repository, so candidate verification invalidates its evidence instead of rolling the test back. Concurrent hostile filesystem replacement between path resolution and file opening is not eliminated by Phase 5 containment checks. An abandoned task evidence lock fails closed and requires local inspection before another batch.
