# ReviewRelay Project Setup UX and Data Root — Report

**Status: blocked before packaged acceptance.** No source commit, push, packaged rebuild, legacy-state migration, or old acceptance-workspace cleanup was performed.

## Candidate and repository

- Baseline/local HEAD: `572045533cb48d9ef9c2aee99f81c6680ed52391`
- Remote `origin/main`: `572045533cb48d9ef9c2aee99f81c6680ed52391`
- Local and remote match: YES
- Worktree: NOT CLEAN; intended source/test changes, two requested reports, and five pre-existing runtime folders remain uncommitted/untracked.

## Implemented in the working tree

- Self-managed application-relative `data` root with a non-secret `.reviewrelay-root` identity marker, deterministic source/ONEDIR resolution, managed subdirectory creation, and fail-closed behavior for inaccessible or unmarked non-empty roots.
- Explicit one-time legacy import under Advanced setup. It preserves the source and does not copy the Chrome profile or runtime lock files.
- Existing repository inspection now records typed local Git blockers and detected GitHub remotes without publishing. The Project Hub presents one next setup action, Owner-readable blocker text, and secondary controls under Advanced setup.
- Packaged UI smoke no longer models a data-root chooser. ONEDIR acceptance workspace helpers target `%TEMP%\ReviewRelay\Acceptance\<unique-id>` and retain temporary diagnostics on failed verification.

## Gacha repository diagnosis (read-only)

Start snapshot:

- Classification: `DIRTY_WORKTREE`
- HEAD: `36301f47551b83eac5161c05b0bba4d4d9c67ce7`
- Branch: `m3-stage-d`
- Dirty path: `.reviewrelay/.dotnet-home/`

Final read-only snapshot, after the long ReviewRelay full-suite run:

- Classification remains `DIRTY_WORKTREE`
- HEAD: `95b3b84f8b2e22436286b31664d57541b0eb46c2`
- Branch: `m3-stage-d`
- Modified paths: `src/Game.Battle.Worker/Program.cs`; `src/Game.Core/M4/HumanStateContracts.cs`; `src/Game.Persistence.Sqlite/CampaignDatabase.cs`; `src/Game.Persistence.Sqlite/M2/M2ExpeditionService.cs`; `src/Game.Persistence.Sqlite/M3/PrivateStateStorageRegistry.cs`; `src/Game.Persistence.Sqlite/M4HumanStatePersistence.cs`; `src/Game.Persistence.Sqlite/Migrations.cs`; `src/Game.Simulation/M2/ActionGrammar.cs`; `src/Game.Simulation/M2/BattleJournal.cs`; `src/Game.Simulation/M2/BattlePrimitivePlanCodec.cs`; `src/Game.Simulation/M2/BattlePrimitiveRuntime.cs`; `src/Game.Simulation/M2/CompiledBattleAction.cs`; `src/Game.Simulation/M2/RunSimulation.cs`.
- Untracked paths: `src/Game.Core/M4/AutonomyRunContinuationContracts.cs`; `src/Game.Persistence.Sqlite/M4AutonomyRunContinuationPersistence.cs`; `src/Game.Simulation/M2/AutonomyRunContinuationHost.cs`; `tests/Game.M2.Tests/M4ExSc1bRunFixture.cs`; `tests/Game.M2.Tests/M4ExSc1bScratchProbeTests.cs`.

These read-only snapshots differ. This turn issued no write command against Gacha; the cause and author of the change are unknown. No changed file contents were inspected. Gacha Task 2 was not modified or resumed, and used no Codex inference or ChatGPT message.

## ReviewRelay validation

- Focused suite: `113 passed in 154.88s`; after the final import-availability guard, affected storage/UI suite: `28 passed in 2.37s`.
- Full suite: `804 passed, 1 skipped, 1 failed in 2121.71s`.
- Failure: `tests/test_task_ui.py::test_worker_panel_project_scoping_and_selected_task_only`.
- Failure detail: creating the second Task hit `PROJECT_JOB_ACTIVE` because the first Task was still non-terminal. The exact test passed once when rerun alone (`1 passed in 21.12s`); the full-suite-only cause is unresolved.
- `compileall`: PASS
- `git diff --check`: PASS
- Canonical ONEDIR rebuild/launch verification: NOT RUN because the full suite was not clean.

## Acceptance and preserved data

The existing `G:\ReviewRelay-Owner-UX-Acceptance-20261003-30ae58ee` workspace was not removed. Its `ReviewRelay.exe` process was still running at the last check. The five existing source-root runtime folders were left untouched: `active/` (Owner data), `browser-profile/` (Owner/auth data), `db/` (Owner data), `config/` (project locks), and `browser-profile-locks/` (runtime locks). The new canonical `G:\Relay GPT-CODEX\data` path was resolved but not created by this task.
