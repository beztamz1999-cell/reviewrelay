# ReviewRelay Project Setup UX and Data Root — Audit

## Scope and safety

- The Gacha repository was checked read-only at task start and again at the final gate. No Gacha file was changed by this task; no Gacha file contents were inspected.
- Gacha changed between the two observations: HEAD moved from `36301f47551b83eac5161c05b0bba4d4d9c67ce7` to `95b3b84f8b2e22436286b31664d57541b0eb46c2`, and its status grew from one untracked `.reviewrelay/.dotnet-home/` path to 13 modified plus 5 untracked paths. The cause is unknown; the state was not investigated further.
- Did not inspect, migrate, delete, or copy any of the five existing ReviewRelay source-root runtime folders. Chrome profile contents were not inspected.
- Did not read or adopt `G:\REVIEW_RELAY_DATA` or any other legacy ReviewRelay root.
- No Codex inference or ChatGPT send was used.

## Working tree change inventory

Tracked modifications: `README.md`, `SPEC.md`, `packaging/reviewrelay_frozen_smoke.py`, `src/reviewrelay/project_setup.py`, `src/reviewrelay/storage.py`, `src/reviewrelay/ui.py`, `tests/test_packaging.py`, `tests/test_project_ui.py`, and `tests/test_projects.py`.

New source/test files: `src/reviewrelay/acceptance_workspace.py`, `src/reviewrelay/legacy_import.py`, and `tests/test_self_managed_data_root.py`.

Requested Markdown files: `reports/PROJECT_SETUP_UX_CLEANUP.md` and `reports/PROJECT_SETUP_UX_CLEANUP-audit.md`.

Existing untracked Owner/runtime data, preserved: `active/`, `browser-profile/`, `db/`, `config/`, and `browser-profile-locks/`. No files were staged. No source commit or push was made; local and remote `main` remain at `572045533cb48d9ef9c2aee99f81c6680ed52391`.

## Gacha local repository

Start classification was `DIRTY_WORKTREE`, branch `m3-stage-d`, HEAD `36301f47551b83eac5161c05b0bba4d4d9c67ce7`, with `.reviewrelay/.dotnet-home/` untracked.

At final read-only check it was still `DIRTY_WORKTREE`, branch `m3-stage-d`, HEAD `95b3b84f8b2e22436286b31664d57541b0eb46c2`. The 13 modified and 5 untracked paths are enumerated in the paired Report Markdown. No claim is made about who or what changed them.

## Test evidence

Full command: `python -m pytest -q` using the configured Python 3.13 interpreter.

Result: `804 passed, 1 skipped, 1 failed in 2121.71s`.

The sole failed node was `tests/test_task_ui.py::test_worker_panel_project_scoping_and_selected_task_only`. Creating its second Task raised `StorageError(code="PROJECT_JOB_ACTIVE")` because the prior Task had not reached a terminal state. An isolated rerun passed once. That does not establish the full-suite failure as harmless or explain its order-dependent behavior, so packaged release acceptance remains blocked.

Focused tests passed: `113 passed in 154.88s`; after the final import-availability guard, `28 passed in 2.37s`. Compilation passed with `python -m compileall -q src tests`; whitespace validation passed with `git diff --check`.

## Packaging gate

The required canonical ONEDIR rebuild, packaged launch check, and old acceptance-workspace cleanup were not run. The full suite had one unresolved failure, and `ReviewRelay.exe` remained running from the old acceptance workspace. The canonical artifact and Owner runtime data were not replaced.
