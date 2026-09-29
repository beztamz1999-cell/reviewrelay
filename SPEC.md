# ReviewRelay V1 — Canonical SPEC

**Status:** APPROVED FOR IMPLEMENTATION  
**Artifact:** `SPEC.md`  
**Scope:** Standalone Windows relay between Codex Worker and ChatGPT Reviewer  
**Owner authority:** Human Owner remains final authority for freeze, release, merge, deploy, production operations, and scope changes.

---

## 1. Product Definition

ReviewRelay is a lightweight standalone tool that automates the review loop between:

- **Codex Worker** — implements and fixes code.
- **ChatGPT regular chat** — performs code review, audit, reasoning, and review decisions.
- **Local machine** — collects deterministic evidence from Git, source files, tests, and repository state.
- **Human Owner** — starts tasks, handles escalations, and performs final freeze/release decisions.

ReviewRelay is **not** an AI harness, agent manager, project manager, or autonomous software organization.

Its purpose is narrow:

> Remove manual copy/paste between Codex and ChatGPT while preserving low Codex quota usage and strong audit evidence.

---

## 2. Core Goal

The target loop is:

```text
Owner
  ↓
Codex Worker
  ↓
Implementation + tests + commit + final report
  ↓
ReviewRelay
  ↓
Git/source/test evidence
  ↓
ChatGPT Reviewer
  ↓
PASS / FIX_REQUIRED / NEED_EVIDENCE / OWNER_DECISION_REQUIRED
  ↓
ReviewRelay
  ↓
Codex Worker only when code must change
  ↓
Repeat until PASS or escalation
  ↓
Owner
```

The core quota model is:

```text
Codex implementation     → Codex quota
Codex fix                → Codex quota

Git diff                 → local / free
Read source              → local / free
grep                     → local / free
Run configured tests     → local / free
Build evidence pack      → local / free

ChatGPT review           → Chat quota
ChatGPT reasoning        → Chat quota
```

### Hard invariant

**`NEED_EVIDENCE` must not consume Codex quota when the requested evidence can be collected locally by ReviewRelay.**

Codex is invoked only when code or repository content must actually be modified.

---

## 3. V1 Non-Goals

ReviewRelay V1 must not become any of the following:

- AI Manager integration.
- Multi-agent orchestration platform.
- Generic autonomous coding harness.
- IDE.
- Full CI/CD system.
- Release manager.
- Production deployment tool.
- Git hosting service integration.
- Cloud review service.
- OpenAI API client.
- Arbitrary shell execution system driven by reviewer output.
- Long-term project memory system.

These are explicitly outside V1 scope.

---

## 4. Roles

### 4.1 Owner

The Owner:

- selects project/repository;
- supplies or selects the task;
- starts or stops the relay;
- handles escalations;
- decides architecture/scope questions that require human judgment;
- performs final freeze/release/merge/deploy decisions.

ReviewRelay must never silently replace Owner authority.

### 4.2 Codex Worker

Codex Worker:

- receives implementation/fix instructions;
- changes source code;
- runs relevant development tests;
- commits the exact tested candidate state;
- returns a final textual implementation report.

Codex Worker is **not** trusted as the sole source of audit truth.

### 4.3 ChatGPT Reviewer

ChatGPT Reviewer:

- receives review packs;
- evaluates implementation correctness;
- requests additional repository evidence when needed;
- issues fix instructions;
- decides whether the candidate is review-PASS;
- escalates ambiguous decisions to the Owner.

ChatGPT Reviewer must never be granted arbitrary local shell access.

### 4.4 ReviewRelay

ReviewRelay:

- maintains task/review state;
- captures worker final output;
- verifies candidate SHA and worktree state;
- creates patch/evidence independently;
- uploads review material;
- parses reviewer control output;
- fulfills safe evidence requests locally;
- sends valid fix instructions back to the same worker session;
- protects against stale reviews and uncontrolled loops;
- cleans disposable artifacts automatically.

ReviewRelay contains **no model of its own**.

---

## 5. Trust Model

Evidence priority:

```text
Git metadata             HIGH TRUST
Git diff                 HIGH TRUST
Actual source files      HIGH TRUST
Relay-run tests          HIGH TRUST

Worker final report      LOW TRUST / narrative only
Worker claims            LOW TRUST
Reviewer interpretation  reasoning layer, not raw evidence
```

Worker reports are useful for context but must not replace repository-derived evidence.

---

## 6. High-Level Architecture

```text
┌─────────────────────────────────────────────────────┐
│                  ReviewRelay Core                   │
│                                                     │
│  State Controller                                   │
│       │                                             │
│       ├── Git / Candidate Verifier                  │
│       ├── Evidence Collector                        │
│       ├── Test Registry Runner                      │
│       ├── Secret Scanner                            │
│       ├── Review Pack Builder                       │
│       ├── Reviewer Protocol Parser                  │
│       ├── ChatGPT Web Adapter                       │
│       ├── Codex Worker Adapter                      │
│       ├── Storage / GC Manager                      │
│       └── SQLite Audit State                        │
└───────────────┬───────────────────┬─────────────────┘
                │                   │
                ▼                   ▼
       ChatGPT regular chat      Codex Worker
```

---

## 7. Portable Storage Requirement

### 7.1 No primary runtime storage under user profile

ReviewRelay must not use these as its main persistent data location:

```text
%LOCALAPPDATA%
%APPDATA%
%TEMP%
C:\Users\<user>\...
```

The user selects a **portable data root** once.

Example:

```text
G:\REVIEW_RELAY_DATA\
```

or:

```text
D:\Tools\ReviewRelayData\
```

### 7.2 Proposed structure

```text
<DATA_ROOT>\
├── config\
│   ├── global.yaml
│   └── projects\
│       ├── gacha-v4.yaml
│       ├── fhomes.yaml
│       └── veo-factory.yaml
│
├── browser-profile\
│
├── db\
│   └── relay.db
│
├── active\
│   └── <project_id>\
│       └── <task_id>\
│           ├── durable\
│           └── scratch\
│
├── archive\
│
└── logs\
```

### 7.3 Durable vs scratch data

Each active task is split into:

```text
task\
├── durable\
│   ├── task.json
│   ├── state.json
│   ├── worker-report.md
│   └── review history metadata
│
└── scratch\
    ├── patch\
    ├── source\
    ├── grep\
    ├── tests\
    └── upload\
```

Anything under `scratch\` must be reproducible from:

```text
repository + BASE_SHA + HEAD_SHA + project config
```

Therefore scratch content may be deleted and regenerated.

---

## 8. Automatic Storage Garbage Collection

The user must not need to manually clean ReviewRelay daily.

Recommended defaults:

```yaml
storage:
  completed:
    compact_immediately: true
    retention_days: 30

  failed:
    retention_days: 14

  aborted:
    retention_days: 7

  upload_staging:
    delete_after_send: true

  max_total_size_gb: 5
```

### 8.1 On PASS

Immediately delete:

- full changed-source snapshots;
- temporary patch chunks;
- temporary upload copies;
- temporary grep results;
- temporary test staging;
- duplicate evidence files.

Retain compact audit artifacts:

```text
final.json
manifest.json
worker-report.md
final-review.md
final.patch        # configurable
```

### 8.2 Storage cap

When total managed storage exceeds configured maximum:

1. never delete active task state;
2. compact completed tasks first;
3. delete oldest expired archives;
4. never delete configuration/database required for current operation.

---

## 9. Task Identity

Every review task must have:

```text
PROJECT_ID
TASK_ID
BASE_SHA
HEAD_SHA
REVIEW_CYCLE
```

Example:

```text
PROJECT_ID=gacha-v4
TASK_ID=M4-PT-SC1-R3
BASE_SHA=3b638933...
HEAD_SHA=d674d8a...
REVIEW_CYCLE=1
```

A reviewer result is valid only for the exact candidate SHA it references.

---

## 10. Strict Candidate Mode

V1 default:

```text
STRICT_COMMIT_MODE=true
```

### 10.1 Before task execution

ReviewRelay must verify:

```bash
git status --porcelain
```

Expected:

```text
empty output
```

If baseline is dirty:

```text
BLOCKED_DIRTY_BASELINE
```

Task must not start automatically.

### 10.2 Candidate requirements

A candidate is reviewable only when:

- repository exists;
- HEAD exists;
- HEAD differs from BASE_SHA when implementation changes are expected;
- worktree is clean;
- candidate is committed;
- exact candidate SHA is captured.

### 10.3 Dirty post-worker state

If worker reports completion but:

```bash
git status --porcelain
```

is non-empty:

```text
CANDIDATE_INVALID_DIRTY_WORKTREE
```

Relay must not send the candidate to ChatGPT.

It may instruct the same worker session to finish testing/commit the exact candidate without broadening scope.

---

## 11. Worker Lifecycle

### 11.1 Relay owns task start metadata

Before sending work to Codex:

```text
BASE_SHA = git rev-parse HEAD
```

Relay stores this before worker execution.

### 11.2 Worker completion marker

Worker instructions must require a final marker:

```text
RELAY_WORKER_DONE
```

Recommended final structure:

```text
RELAY_WORKER_DONE
TASK_ID=<task>
HEAD_SHA=<worker-reported-sha>

# Implementation Report

...
```

### 11.3 Worker report capture

ReviewRelay must capture the **final response text from the Codex worker session** and store it itself as:

```text
worker-report.md
```

Codex must not be required to create this file manually.

### 11.4 Worker-reported SHA is untrusted

Relay independently executes:

```bash
git rev-parse HEAD
```

If:

```text
worker_reported_sha != actual_head_sha
```

record:

```text
WORKER_REPORTED_SHA_MISMATCH=true
```

Actual Git SHA is authoritative.

---

## 12. Patch Generation

Codex does not generate the canonical patch.

ReviewRelay generates it independently.

Required Git evidence:

```bash
git diff BASE_SHA..HEAD_SHA
git diff --stat BASE_SHA..HEAD_SHA
git diff --name-status BASE_SHA..HEAD_SHA
git status --porcelain
```

Artifacts:

```text
changes.patch
diff-stat.txt
changed-files.txt
git-status.txt
```

The patch must represent exactly:

```text
BASE_SHA..HEAD_SHA
```

---

## 13. Review Pack

Default review pack:

```text
review-pack\
├── manifest.json
├── task.md
├── worker-report.md
├── changes.patch
├── changed-files.txt
├── diff-stat.txt
├── git-status.txt
├── test-results.txt
└── changed-source\
    └── ...
```

### 13.1 Manifest

Example:

```json
{
  "protocol": "review-relay/1",
  "project_id": "gacha-v4",
  "task_id": "M4-PT-SC1-R3",
  "cycle": 1,
  "base_sha": "3b638933...",
  "head_sha": "d674d8a...",
  "worktree_clean": true,
  "files_changed": 6,
  "insertions": 184,
  "deletions": 47
}
```

Recommended addition:

- SHA-256 for each packed artifact;
- pack creation timestamp;
- project config version;
- relay version.

---

## 14. Evidence Upload Strategy

Do not upload an entire repository.

Default strategy:

```text
PATCH_FIRST
```

First review sends:

- task/spec context;
- manifest;
- worker report;
- patch;
- changed-files list;
- relevant test output;
- full changed files only when below configured threshold.

Config example:

```yaml
review:
  full_changed_files_limit: 12
```

For large changes, reviewer requests only the extra context it needs.

---

## 15. Large Patch Handling

Large patch files may be split by changed file.

Example:

```text
patch\
├── 001_BookingService.patch
├── 002_PaymentService.patch
├── 003_BookingApi.patch
└── 004_Tests.patch
```

Rules:

- do not split inside a diff hunk unless technically unavoidable;
- retain stable ordering;
- retain path mapping in manifest;
- preserve candidate SHA across all chunks.

---

## 16. NEED_EVIDENCE Design

`NEED_EVIDENCE` is a first-class reviewer action.

The reviewer may request additional repository evidence.

ReviewRelay should fulfill the request locally whenever possible.

Flow:

```text
ChatGPT
   ↓
NEED_EVIDENCE
   ↓
ReviewRelay validates request
   ↓
Local evidence collection
   ↓
Upload additional evidence
   ↓
ChatGPT continues same review
```

Codex is not called for ordinary evidence retrieval.

---

## 17. Evidence DSL

ChatGPT reviewer must never return arbitrary local shell commands for execution.

ReviewRelay accepts only whitelisted operations.

V1 operations:

| Operation | Purpose |
|---|---|
| `read_file` | Read full repository file |
| `read_range` | Read a specified line range |
| `grep` | Search text/symbol in allowed roots |
| `git_show` | Read file/content from Git ref |
| `diff_file` | Diff one file between known refs |
| `list_dir` | List allowed repository path |
| `test` | Run a configured test registry entry |
| `git_log` | Read bounded Git history |
| `git_status` | Read repository state |

No generic:

```text
shell
powershell
cmd
bash
python
curl
rm
del
deploy
```

may be issued by the reviewer.

---

## 18. Evidence Request Examples

### 18.1 Read file

```json
{
  "kind": "read_file",
  "path": "src/Game.Persistence/M4Persistence.cs"
}
```

### 18.2 Grep

```json
{
  "kind": "grep",
  "pattern": "ActivityCompleted",
  "roots": ["src", "tests"]
}
```

### 18.3 Test

```json
{
  "kind": "test",
  "test_id": "targeted_m4"
}
```

---

## 19. Test Registry

Tests are configured per project.

Example:

```yaml
tests:
  unit:
    command: "dotnet test tests/Game.Tests"

  persistence:
    command: "dotnet test tests/Game.Persistence.Tests"

  targeted_m4:
    command: "dotnet test --filter M4LearningNarrative"
```

Reviewer may request only:

```text
test_id
```

not an arbitrary command.

ReviewRelay validates the ID and runs the preconfigured command.

---

## 20. Reviewer Protocol

Human-readable review prose is allowed.

The final machine-readable control block must use:

```text
<RELAY_CONTROL>
...
</RELAY_CONTROL>
```

### 20.1 Allowed actions

```text
PASS
FIX_REQUIRED
NEED_EVIDENCE
OWNER_DECISION_REQUIRED
REVIEW_ERROR
```

No additional action names are valid in V1.

---

## 21. PASS Example

```text
<RELAY_CONTROL>
{
  "protocol": "rr.v1",
  "candidate_sha": "d674d8a...",
  "cycle": 2,
  "action": "PASS",
  "findings": []
}
</RELAY_CONTROL>
```

Relay must verify that:

```text
candidate_sha == current review candidate
cycle == current cycle
```

before accepting PASS.

---

## 22. FIX_REQUIRED Example

```text
<RELAY_CONTROL>
{
  "protocol": "rr.v1",
  "candidate_sha": "d674d8a...",
  "cycle": 1,
  "action": "FIX_REQUIRED",
  "findings": [
    {
      "severity": "blocking",
      "summary": "Persistence provenance is not validated fail-closed."
    }
  ],
  "worker_instruction": "Fix the provenance validation without broadening scope..."
}
</RELAY_CONTROL>
```

Only `worker_instruction` is routed to the worker.

---

## 23. NEED_EVIDENCE Example

```text
<RELAY_CONTROL>
{
  "protocol": "rr.v1",
  "candidate_sha": "d674d8a...",
  "cycle": 1,
  "action": "NEED_EVIDENCE",
  "evidence_requests": [
    {
      "kind": "read_file",
      "path": "src/Game.Persistence/M4Persistence.cs"
    },
    {
      "kind": "grep",
      "pattern": "ActivityCompleted",
      "roots": ["src", "tests"]
    }
  ]
}
</RELAY_CONTROL>
```

---

## 24. OWNER_DECISION_REQUIRED

Reviewer uses:

```text
OWNER_DECISION_REQUIRED
```

when a decision involves:

- scope ambiguity;
- architecture trade-off not authorized by spec;
- destructive changes;
- migration strategy requiring human approval;
- product behavior decision;
- security exception;
- release/freeze choice;
- conflict between canonical requirements.

Relay pauses the task.

---

## 25. Stale Review Protection

Before applying any reviewer result:

```text
reviewed_candidate_sha == current_candidate_sha
```

must be true.

If repository HEAD changes while review is pending:

```text
STALE_REVIEW
```

The old review must not be applied automatically.

A new candidate/review cycle is required.

---

## 26. Candidate Mutation During Evidence Collection

During `NEED_EVIDENCE`, ReviewRelay records HEAD before and after evidence collection.

If:

```text
HEAD_before != HEAD_after
```

then:

```text
CANDIDATE_MUTATED_DURING_REVIEW
```

Current review is invalidated.

---

## 27. FIX_REQUIRED Worker Flow

When reviewer returns `FIX_REQUIRED`, ReviewRelay sends the instruction to the **same Codex worker session** whenever possible.

Suggested wrapper:

```text
You are continuing the existing task.

Reviewer found blocking issues in candidate:
<CANDIDATE_SHA>

Apply ONLY the review instruction below.

Do not broaden scope.
Do not redesign unrelated areas.
Run the configured relevant tests.
Commit the exact tested source state.

REVIEW INSTRUCTION:
<worker_instruction>

When complete, return RELAY_WORKER_DONE and a final implementation report.
```

After worker completion, ReviewRelay independently verifies the new candidate and creates the next review cycle.

---

## 28. Same Worker Session Requirement

V1 should reuse the same Codex session for all fix cycles of one task when technically possible.

Reason:

- preserves worker context;
- avoids restating the entire task;
- reduces unnecessary token/quota use;
- keeps implementation continuity.

One task should not spawn a fresh worker session for every review fix.

---

## 29. Loop Guards

Default:

```text
MAX_FIX_CYCLES=3
MAX_EVIDENCE_CYCLES=5
```

### 29.1 Fix limit

If three fix cycles fail to reach PASS:

```text
OWNER_ESCALATION_REQUIRED
```

Relay stops sending automatic fix instructions.

### 29.2 Evidence limit

If evidence requests continue beyond the configured threshold:

```text
OWNER_ESCALATION_REQUIRED
```

This prevents infinite evidence loops.

---

## 30. ChatGPT Transport

V1 uses normal ChatGPT web UI.

Preferred implementation:

```text
Playwright
+
persistent Chromium profile
```

Do not use:

- OpenAI API;
- undocumented/private web endpoints;
- reverse-engineered internal ChatGPT APIs.

Expected UI operations:

1. open the configured reviewer conversation;
2. attach review files;
3. enter review prompt;
4. send;
5. wait for assistant response;
6. capture the complete latest response;
7. parse `<RELAY_CONTROL>`.

---

## 31. Browser Profile

Browser session data must live under portable data root:

```text
<DATA_ROOT>\browser-profile\
```

Not under the default user profile.

User performs normal ChatGPT login manually when required.

ReviewRelay reuses the persistent authenticated profile.

---

## 32. Reviewer Conversation Policy

Recommended:

```text
one major task = one reviewer conversation
```

All review cycles for the same task remain in the same conversation.

Example:

```text
M4-PT-SC1 Reviewer
  ├── cycle 1
  ├── evidence request
  ├── cycle 2
  └── final PASS
```

New unrelated task → new reviewer conversation.

Reason:

- reduces context drift;
- keeps task history together;
- limits unrelated context accumulation.

---

## 33. Reviewer Conversation Bootstrap

At task start, the reviewer conversation should receive:

- task ID;
- canonical task requirements/spec;
- review protocol;
- BASE_SHA;
- review expectations;
- reviewer authority boundaries.

The reviewer should be instructed to end every review response with a valid `RELAY_CONTROL` block.

---

## 34. Secret Scanning

Before any artifact is uploaded to ChatGPT, ReviewRelay runs a local secret check.

Default excluded paths/patterns:

```text
.env
.env.*
*.pem
*.key
credentials*
secrets*
database backups
production dumps
browser profile files
cookies
session storage
node_modules
bin
obj
dist
```

Detect likely:

- API keys;
- bearer tokens;
- private keys;
- passwords;
- connection strings;
- authentication cookies;
- secret environment values.

On high-confidence detection:

```text
BLOCKED_SECRET_DETECTED
```

Artifact is not uploaded automatically.

Owner intervention is required.

---

## 35. Production Safety Boundary

ReviewRelay must never autonomously perform:

```text
deploy production
push protected branch
merge branch
delete branch
run production migration
modify production database
edit production secrets
publish release
modify DNS
modify billing/payment provider production state
```

The reviewer protocol has no command capable of invoking these operations.

---

## 36. State Machine

```text
IDLE
 ↓
PRECHECK
 ↓
WORKER_RUNNING
 ↓
VERIFY_CANDIDATE
 ↓
BUILD_REVIEW_PACK
 ↓
SEND_REVIEW
 ↓
WAIT_REVIEW
 ↓
PARSE_REVIEW
 ├── PASS ─────────────────────→ COMPLETE
 │
 ├── FIX_REQUIRED ─────────────→ WORKER_RUNNING
 │
 ├── NEED_EVIDENCE ────────────→ COLLECT_EVIDENCE
 │                                  ↓
 │                              SEND_EVIDENCE
 │                                  ↓
 │                              WAIT_REVIEW
 │
 ├── OWNER_DECISION_REQUIRED ──→ PAUSED_OWNER
 │
 └── REVIEW_ERROR ─────────────→ PAUSED_ERROR
```

Additional blocking states:

```text
BLOCKED_DIRTY_BASELINE
CANDIDATE_INVALID_DIRTY_WORKTREE
BLOCKED_SECRET_DETECTED
STALE_REVIEW
CANDIDATE_MUTATED_DURING_REVIEW
OWNER_ESCALATION_REQUIRED
```

---

## 37. Persistence and Crash Recovery

Use SQLite.

Minimum persisted fields:

```text
project_id
task_id
task_state
base_sha
candidate_sha
review_cycle
reviewer_chat_url_or_identity
worker_session_identity
last_sent_review_key
last_review_action
pack_hash
created_at
updated_at
```

ReviewRelay must resume from persistent state after restart.

Example:

If the app crashes after review upload but before response parsing:

```text
state = WAIT_REVIEW
```

On restart it must resume waiting/reading the existing review, not blindly upload a duplicate.

---

## 38. Idempotency

Every review send operation should have a deterministic identity:

```text
PROJECT_ID + TASK_ID + CANDIDATE_SHA + CYCLE
```

Before sending, ReviewRelay checks whether that exact review request was already sent.

Goal:

- no accidental duplicate review posts;
- no duplicate worker fixes;
- safer crash recovery.

---

## 39. Worker Adapter Interface

Core must not depend directly on one worker implementation.

Conceptual interface:

```text
WorkerAdapter:
    start_task(...)
    send_instruction(...)
    wait_until_done(...)
    get_final_response(...)
    get_session_identity(...)
    interrupt(...)
```

V1 implementation:

```text
CodexWorkerAdapter
```

Future adapters may exist without changing core state semantics.

---

## 40. Reviewer Adapter Interface

Conceptual interface:

```text
ReviewerAdapter:
    open_task_conversation(...)
    send_review_pack(...)
    send_evidence(...)
    wait_response(...)
    get_latest_response(...)
```

V1 implementation:

```text
ChatGPTWebAdapter
```

---

## 41. Project Configuration

Example:

```yaml
project_id: gacha-v4

repo:
  path: "G:\\TEST WORKBUDDY AI\\gacha-infinity"
  strict_commit_mode: true
  require_clean_baseline: true

review:
  max_fix_cycles: 3
  max_evidence_cycles: 5
  full_changed_files_limit: 12
  keep_final_patch: true

chatgpt:
  conversation_scope: task
  browser_profile: "default"

worker:
  adapter: codex
  reuse_session: true

tests:
  unit:
    command: "dotnet test tests/Game.Tests"

  persistence:
    command: "dotnet test tests/Game.Persistence.Tests"

security:
  block_secrets: true
  block_env_files: true

storage:
  completed_retention_days: 30
  failed_retention_days: 14
  aborted_retention_days: 7
  max_total_size_gb: 5
```

---

## 42. Windows UI

V1 UI must remain lightweight.

Recommended:

```text
┌──────────────────────────────────────────────┐
│ ReviewRelay                                  │
├──────────────────────────────────────────────┤
│ Project:  Gacha V4                          │
│ Task:     M4-PT-SC1-R3                      │
│                                              │
│ State:    WAIT_REVIEW                       │
│ Cycle:    2 / 3                             │
│ HEAD:     d674d8a                           │
│                                              │
│ ChatGPT:  Connected                         │
│ Codex:    Connected                         │
│ Repo:     Clean                             │
│                                              │
│ Last review: FIX_REQUIRED                   │
│                                              │
│ [Start] [Pause] [Stop]                      │
│ [Open Chat] [Open Repo] [Open Evidence]     │
├──────────────────────────────────────────────┤
│ Logs                                         │
│ ...                                          │
└──────────────────────────────────────────────┘
```

No dashboard-heavy UX.

No project management suite.

---

## 43. First-Run Setup

On first launch:

```text
ReviewRelay Data Location

[ G:\REVIEW_RELAY_DATA\                 ] [Browse]

☑ Automatically clean temporary evidence
☑ Compact completed tasks
☑ Keep completed audit records for 30 days
☑ Limit managed storage to 5 GB
```

The chosen data root is persisted.

---

## 44. Audit History

Each task retains enough history to reconstruct:

- what candidate was reviewed;
- what changed;
- what worker reported;
- what reviewer found;
- what fix instruction was sent;
- which candidate eventually passed;
- how many cycles occurred.

Example compact archive:

```text
archive\
└── gacha-v4\
    └── M4-PT-SC1-R3\
        ├── final.json
        ├── manifest.json
        ├── worker-report.md
        ├── final-review.md
        └── final.patch
```

---

## 45. Logging

Logs should record operational events, not secrets.

Example:

```text
2026-09-30 12:01 PRECHECK_OK
2026-09-30 12:01 BASE_SHA=...
2026-09-30 12:15 WORKER_DONE
2026-09-30 12:15 ACTUAL_HEAD=...
2026-09-30 12:16 REVIEW_PACK_READY
2026-09-30 12:17 REVIEW_SENT
2026-09-30 12:22 REVIEW_ACTION=NEED_EVIDENCE
2026-09-30 12:22 EVIDENCE_REQUEST=read_file(...)
...
```

Do not log:

- authentication cookies;
- raw passwords;
- API keys;
- private keys.

---

## 46. Error Handling Principles

ReviewRelay must fail closed for:

- invalid candidate identity;
- dirty baseline;
- stale reviewer response;
- candidate mutation during review;
- malformed reviewer control block;
- unknown reviewer action;
- unsafe evidence operation;
- secret detection;
- exceeded cycle limits.

Never silently guess in these cases.

---

## 47. Malformed Reviewer Output

If `<RELAY_CONTROL>` is missing or invalid:

```text
REVIEW_ERROR
```

ReviewRelay may request the reviewer to return a corrected control block without starting a new code cycle.

No worker modification occurs until a valid action is parsed.

---

## 48. Review Prompt Contract

ReviewRelay should use a stable review instruction similar to:

```text
Audit this exact candidate against the supplied task requirements.

Use the attached Git/source/test evidence as the source of truth.
Treat the worker report as narrative context, not authoritative evidence.

If evidence is insufficient, request only the minimum additional evidence required.

Do not request arbitrary shell commands.

Return one action:
PASS
FIX_REQUIRED
NEED_EVIDENCE
OWNER_DECISION_REQUIRED
REVIEW_ERROR

End your response with exactly one valid <RELAY_CONTROL> block.
```

---

## 49. Worker Prompt Contract

ReviewRelay wraps worker tasks/fixes with rules such as:

```text
Stay within the supplied task scope.

Run relevant tests.
Commit the exact tested source state.
Do not begin unrelated work.
Do not alter production systems.
Return RELAY_WORKER_DONE only after the candidate is committed and ready for review.
Provide a Markdown implementation report in your final response.
```

---

## 50. Review-Pack Reproducibility

Given:

```text
repo
BASE_SHA
HEAD_SHA
project config
```

ReviewRelay should be able to regenerate:

- patch;
- changed file list;
- diff stat;
- Git status;
- source snapshots;
- deterministic configured test outputs where tests remain reproducible.

This is why scratch data may be safely garbage-collected.

---

## 51. V1 Implementation Phases

### Phase 1 — Core State + Git Evidence

Implement:

- portable data root;
- project config;
- SQLite state;
- repo precheck;
- BASE/HEAD tracking;
- clean-worktree enforcement;
- patch generation;
- changed-file collection;
- source snapshot collection;
- storage structure;
- GC basics.

Acceptance:

- can create a correct review pack for a manually prepared commit.

### Phase 2 — Review Protocol

Implement:

- `RELAY_CONTROL` parser;
- action validation;
- candidate-SHA validation;
- stale-review protection;
- Evidence DSL;
- evidence request validator;
- loop counters.

Acceptance:

- machine-readable PASS/FIX/NEED_EVIDENCE flows work without browser automation.

### Phase 3 — ChatGPT Web Adapter

Implement:

- persistent Playwright/Chromium profile;
- open configured conversation;
- upload files;
- send review message;
- capture latest assistant response;
- reconnect/retry handling.

Acceptance:

- relay can deliver a review pack and capture a valid control block from ChatGPT web.

### Phase 4 — Codex Worker Adapter

Implement:

- task send;
- fix send;
- same-session reuse;
- completion marker capture;
- final response capture;
- worker report persistence.

Acceptance:

- worker implementation/fix response can be captured without manual copy/paste.

### Phase 5 — Autonomous Review Loop

Implement:

```text
worker
→ verify candidate
→ build pack
→ review
→ evidence/fix
→ review
→ PASS/escalate
```

Acceptance:

- one complete task can pass through at least one FIX_REQUIRED or NEED_EVIDENCE cycle without Owner copy/paste.

### Phase 6 — Safety + Recovery

Implement:

- secret scanner;
- crash recovery;
- idempotent sends;
- stale candidate protection;
- GC;
- owner escalation.

Acceptance:

- restart does not duplicate review/fix actions;
- unsafe artifacts are blocked.

### Phase 7 — Lightweight Windows UI + Packaging

Implement:

- project selector;
- task state view;
- logs;
- Start/Pause/Stop;
- Open Chat/Repo/Evidence;
- first-run data-root selection;
- Windows packaging.

Acceptance:

- normal operation does not require terminal usage.

---

## 52. V1 Acceptance Matrix

| Requirement | V1 Acceptance |
|---|---|
| Standalone | No AI Manager dependency |
| Quota strategy | Codex used only for implementation/fix |
| Worker report | Captured automatically from final worker response |
| Patch | Generated independently by Git |
| Candidate | Exact committed SHA |
| Baseline | Dirty baseline blocks start |
| Post-worker state | Dirty candidate blocks review |
| Evidence | Collected locally where possible |
| NEED_EVIDENCE | Does not call Codex for basic retrieval |
| Evidence execution | Whitelisted DSL only |
| Tests | Reviewer selects configured test IDs only |
| Chat transport | Normal ChatGPT web UI |
| OpenAI API | Not required |
| Worker session | Reused across fix cycles |
| Stale review | Rejected |
| Candidate mutation | Invalidates review |
| Secret leakage | Upload blocked |
| Fix loop | Max 3 by default |
| Evidence loop | Max 5 by default |
| Crash recovery | Resume without duplicate sends |
| Storage | Portable user-selected root |
| Temp cleanup | Automatic |
| Completed task | Compact immediately |
| Production actions | Prohibited |
| Final freeze | Owner authority |

---

## 53. Canonical V1 Invariants

The following are hard requirements and must not drift during implementation:

1. **ReviewRelay is standalone.**
2. **No AI Manager integration in V1.**
3. **Relay has no AI model of its own.**
4. **ChatGPT regular chat is the primary reviewer.**
5. **Codex is used for code/fix, not routine review evidence retrieval.**
6. **Worker report is captured from worker output by Relay.**
7. **Canonical patch is generated by Git, not trusted from worker output.**
8. **Candidate review is bound to exact BASE_SHA and HEAD_SHA.**
9. **Dirty or mutable candidate states fail closed.**
10. **NEED_EVIDENCE is fulfilled locally whenever possible.**
11. **Reviewer cannot execute arbitrary shell commands.**
12. **Same Codex worker session is reused when possible.**
13. **Automatic loops are bounded.**
14. **Secrets are scanned before upload.**
15. **Primary data lives in a portable user-selected data root.**
16. **Disposable evidence is automatically garbage-collected.**
17. **ReviewRelay never performs production deployment/release authority actions.**
18. **Owner remains final authority.**

---

## 54. Deferred Beyond V1

Possible future work, explicitly deferred:

- AI Manager integration;
- multi-worker orchestration;
- reviewer pooling;
- Claude/Gemini reviewer adapters;
- Claude Code worker adapter;
- GitHub/GitLab PR integration;
- CI server integration;
- project-memory layer;
- remote relay daemon;
- team/shared relay server;
- policy-based reviewer routing;
- cross-repo task orchestration;
- automatic release/freeze workflows.

None of these should be introduced during V1 unless the Owner explicitly reopens scope.

---

## 55. Final Product Principle

ReviewRelay should remain small enough that its behavior is obvious:

```text
Worker changes code.
Git tells Relay what actually changed.
Local machine gathers evidence.
ChatGPT reasons about the evidence.
Relay routes only the required next action.
Owner remains in control.
```

The product succeeds if it removes manual copy/paste without turning itself into another expensive autonomous harness.
