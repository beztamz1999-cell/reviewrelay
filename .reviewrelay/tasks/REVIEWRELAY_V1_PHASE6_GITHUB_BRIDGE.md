# Phase 6: GitHub Review Bridge

Owner-approved task contract, created before implementation on the clean Phase 5 baseline `2fd9398692c9f50e26f7c595812d5f2cd87d50f2`.

Local Git is execution truth. GitHub is a reviewer-readable mirror. ReviewRelay verifies a clean exact local candidate, publishes its SHA to one safely named task branch without force, verifies the remote SHA, and sends one compact GitHub notification with no source/report/patch attachments. Codex owns local implementation, tests and commits; ReviewRelay owns publishing and optional PR management.

Provide a small injected candidate-publisher boundary, optional GitHub configuration, branch-only mode and one optional PR per task, bounded fixed-argv Git/gh processes, durable SQLite publish state/events, read-only reconciliation after uncertain effects, and idempotent push/PR behavior. Preserve Phase 1–5 interfaces and protections. Bind ordinary implementation tasks to a committed `.reviewrelay/tasks/<TASK_ID>.md` before worker execution and reject unexpected spec mutation. Intentional spec-changing tasks require an explicit binding at task start.

Notifications identify repository, task, branch/PR, BASE_SHA, HEAD_SHA, review cycle and task spec. Capture owned raw responses and strict RELAY_CONTROL decisions; reject changed local or remote candidates. Do not implement autonomous worker/fix/evidence routing. Keep Phase 5 as the local verification executor and preserve its DSL. Attachment review remains legacy/internal capability.

Update canonical architecture documentation, preserving historical reports. No new implementation/audit report files are required. Tests use local bare remotes and fake PR/reviewer services, including exact push, remote verification, divergence, mutation, dirty state, timeouts, recovery, no duplicates, schema migration, compact notification and zero attachments. Run targeted and full regression, then final staged regression, compileall and diff checks before one local commit. Keep the worktree clean. No development-repository push and no Phase 7.

Live GitHub/reviewer smoke is optional and requires an already authorized test remote; record NOT_RUN when unavailable. Do not invent destinations or copy credentials into configuration/ChatGPT. Private repository access through ChatGPT's GitHub connection is an Owner prerequisite.
