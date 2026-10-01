# Phase 7: Autonomous Task Controller

Owner-approved implementation contract on clean Phase 6 HEAD `9f8ff2f0a3452a7c0bf01f8a1d9724141550f9f1`.

Extend Project-first setup with Task creation/spec commit and a persistent controller. One Task owns one Codex thread and one GitHub task branch. Local Git is execution truth; GitHub is the reviewer-readable mirror. Verify clean exact candidates independently, publish through the accepted publisher, verify remote equality and send compact zero-attachment source-review notifications through the accepted owned-response adapter. Parse all responses through Phase 2 before routing PASS, same-thread FIX_REQUIRED, local NEED_EVIDENCE, Owner pause or review error.

Persist explicit lifecycle, baseline/candidate/cycles, normalized events, raw/validated review history, counters, Owner input and external-effect intents in the existing database. Serialize controller ownership across processes and repository mutation across Tasks. Recover by read-only reconciliation or fail closed; never blindly repeat worker turns, push/PR creation, review or evidence sends. Preserve all Phase 1–6 guards and migration data. Pause/resume/stop and Owner decisions must be explicit and visible in a responsive minimal Task UI.

Test real temporary Git/bare remotes and StateStore with fake worker/reviewer/PR services, direct/evidence/fix/combined/Owner routes, failures, limits, mutation, duplicate ownership and crash boundaries. No automated model quota or real GitHub writes. Live smoke requires every Owner-authorized disposable public-project prerequisite; otherwise NOT_RUN. No real/private project substitution, credentials handling, autonomous merge/main publication/tag/deploy/release, normal report/source attachments or Phase 8.

Required order: implementation, targeted tests, full regression, canonical README/SPEC updates, stage, staged full regression, compileall/diff checks, one local commit, clean worktree. No Phase 7 narrative report/audit files or development-repository push.
