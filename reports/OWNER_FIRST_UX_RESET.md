# ReviewRelay Owner-first UX Reset — Report

Date: 2026-10-05 (Asia/Bangkok)

**Owner visual test gate: READY.** This is a presentation acceptance result, not a new live transport acceptance or a full regression release gate.

## Repository and scope

- Local HEAD and verified GitHub `main`: `572045533cb48d9ef9c2aee99f81c6680ed52391`.
- Tracked working tree: NOT CLEAN. Existing pending changes were preserved, and this UX reset is uncommitted. No commit or push was requested by this task; neither was performed.
- This task changed presentation/bootstrap, read-only view models, UI tests and packaged acceptance infrastructure. It did not change controller orchestration, worker/reviewer transport, GitHub publication semantics, protocol, schema, timeout or recovery authorization.
- The previously pending setup/storage/import implementation remains part of the local working tree. Its existence must not be mistaken for a new backend change made by this reset.

## Owner experience

- Startup shows a Project list with concise readiness labels. **+ Thêm dự án → Project đã có** opens the Project folder chooser and reuses automatic existing-repository inspection.
- A compact wizard proposes detected GitHub repositories, asks for the ChatGPT conversation URL and offers Codex Workers by title and relative activity. Multiple Workers are selected inline and verified through the existing Project Worker service.
- A ready Project opens a chat workspace. A multiline prompt and **Gửi** call the existing `TaskController.submit_request` flow. Enter inserts a newline; Ctrl+Enter sends.
- Historical Owner requests and confirmed controller milestones appear chronologically. FIX routing and reviewer PASS are presented in Vietnamese. A task without a valid ready-for-Owner-review result is not shown as PASS.
- Unfinished Project jobs disable new Send. Command approval, pause/manual steer, Owner decisions and contextual recovery retain the existing backend authority.
- Technical IDs, SHAs, branches, counters and raw journals remain under settings/diagnostics. Opening diagnostics does not automatically scan or rebind Workers.
- Explicit foreground/background and placeholder colors keep the light interface readable with a dark Windows palette. This corrected a defect found by inspecting native packaged screenshots.

## Validation

- Final targeted tests: **79 passed in 55.20s**.
- `python -m compileall -q src tests packaging`: PASS.
- `git diff --check`: PASS.
- Full suite: NOT RUN, as requested for this presentation-only task. The previous recorded full-suite failure in `tests/test_task_ui.py::test_worker_panel_project_scoping_and_selected_task_only` remains unresolved; it is not relabeled as a pass by this UI gate.
- PyInstaller: 6.22.3; Windows ONEDIR build: PASS.
- Native frozen smoke: PASS from the temporary build, staged canonical candidate and final canonical executable, using a synthetic disposable database. Fixture durable-state hashes remained unchanged.
- Live Codex inference: 0. Live ChatGPT sends: 0. Gacha Task 2 was not opened, resumed or modified by this task.

## Canonical artifact

Path: `G:\Relay GPT-CODEX\dist\ReviewRelay\ReviewRelay.exe`

EXE SHA-256: `80d15cc1aa195cccbdc209f5b3002831c9da02b003fdd5aecdcfd80b069e507f`

The final packaged check launched from a different working directory and resolved its application root to `G:\Relay GPT-CODEX`. Production data-root selection was not invoked; the explicit acceptance harness supplied TEMP data. No production data root or legacy state was adopted or migrated.

## Screenshots and diagnostics

All screenshots use **synthetic preview state**. They are not captures of the live Gacha Project or Task 2.

Diagnostics directory:

`C:\Users\Admin\AppData\Local\Temp\ReviewRelay\Diagnostics\owner-ux-86048c7afc584181a0e8f43a496637cd`

- `owner-ux-project-list.png`
- `owner-ux-setup.png`
- `owner-ux-worker-chooser.png`
- `owner-ux-chat.png`
- `owner-ux-diagnostics.png`
- `canonical-check-2.json`: final packaged UI verdict.
- `canonical-publication.json`: verified bundle publication and EXE digest.
- `build-final.log`: final PyInstaller build log.

The acceptance workspace `...\ReviewRelay\Acceptance\86048c7afc584181a0e8f43a496637cd` was removed only after both canonical launch checks passed. No new `G:\ReviewRelay-*` directory was created. Pre-existing Owner runtime folders and old acceptance directories were preserved.

## Limits and next action

Owner may visually test the canonical executable. Existing legacy Project history will require the deliberate advanced import flow if it has not yet been imported; this task did not choose an authoritative legacy root or reconstruct Task 2. Source changes remain local and uncommitted. Resolve the previously recorded full-regression failure before claiming a broader source freeze.

For reproducible test/build/smoke commands, see the paired Audit. To regenerate screenshots, prepare a fresh TEMP fixture with `reviewrelay_owner_smoke.prepare_fixture`, then launch `ReviewRelay.exe --packaged-smoke --mode ux --config <fixture-config> --output <diagnostics-json>`; never point the harness at production data.
