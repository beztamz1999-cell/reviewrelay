# ReviewRelay Owner-first UX Reset — Audit

Date: 2026-10-05 (Asia/Bangkok)

## Change attribution

The checkout began at `572045533cb48d9ef9c2aee99f81c6680ed52391` with uncommitted setup/data-root changes and five untracked runtime directories. Those changes were preserved.

This task added:

- `src/reviewrelay/owner_ui.py`: landing, guided setup, inline Worker selection and chat presentation, reusing existing Qt background jobs and TaskWindow operations.
- `src/reviewrelay/owner_view.py`: read-only status/history/recency presentation.
- `tests/test_owner_ux.py`: Owner navigation, display boundaries, actual service/submit seams, durable concurrent-job blocking, read-only diagnostics, dark-theme readability and synthetic packaged acceptance.
- `packaging/reviewrelay_owner_smoke.py`: opt-in synthetic TEMP fixture and read-only frozen UI acceptance.
- This Report/Audit pair.

This task updated `ui.py` to start OwnerMainWindow and keep explicit legacy import accessible from empty-root advanced settings; updated the bootstrap test, extended packaged smoke dispatch with `ux`, made the legacy smoke select only its exact fixture-root window, and documented the Owner workflow in README.

No new diff exists for controller/controller_store, worker transport/discovery, reviewer, GitHub publisher, state schema or protocol. Pre-existing pending files were preserved, including project_setup.py, storage.py, legacy_import.py, acceptance_workspace.py and their tests/documentation.

Unchanged pending backend hashes during this reset:

| File | SHA-256 |
| --- | --- |
| `src/reviewrelay/project_setup.py` | `252b396e8ba062be61f11969f5d807ccf8cf5aa3def13c3bf7f245b525eb0e0c` |
| `src/reviewrelay/storage.py` | `b3b0b129b308ed36335c5c6793aedde3645886a2a265bfd08fd8b443bb5205db` |

## Safety observations

- No operation targeted Gacha's repository, database, Task 2 or thread. No Gacha inference or ChatGPT message was used.
- The five pre-existing source-root directories `active`, `browser-profile`, `db`, `config`, `browser-profile-locks` were preserved. No credentials, cookies or reviewer bodies from Owner state were inspected or copied.
- Setup actions delegate to existing services and their lock/identity/publication guards. Selecting a Worker calls existing `select`, which reads and verifies the exact thread. There is no UUID input field or replacement thread logic.
- The chat composer delegates to `submit_request`; it does not implement an orchestration loop. Recovery actions and command approval are inherited from TaskWindow.
- Command approval intentionally retains exact safety metadata in its exceptional approval dialog. Hiding technical identity in ordinary Owner screens does not remove this existing approval boundary.
- Normal Send checks existing durable Project availability, not only an in-memory busy flag. Existing unfinished jobs and unresolved effects still block dispatch.
- PASS presentation requires COMPLETE plus `ready_for_owner_review` and no review invalidation. Raw effect payloads are not rendered in the chat.
- No production data-root chooser, implicit legacy adoption, automatic migration, runtime-root cleanup, controller redesign or WORKER_TIMEOUT fix was introduced.

## Final test command

Interpreter: `C:\Users\Admin\AppData\Local\Programs\Python\Python313\python.exe`

```powershell
python -m pytest -q tests/test_owner_ux.py tests/test_project_ui.py tests/test_self_managed_data_root.py tests/test_ui_localization.py tests/test_projects.py::test_existing_clean_repository_is_ready_immediately_and_detects_remote_without_publishing tests/test_projects.py::test_existing_remote_detection_is_only_a_proposal tests/test_project_worker.py::test_exact_cwd_unique_discovery_reads_before_binding_and_persists tests/test_project_worker.py::test_multiple_candidates_return_choices_then_selected_identity_is_read_again tests/test_project_worker.py::test_none_never_creates_and_explicit_creation_starts_once_then_reads tests/test_project_worker.py::test_owner_prompt_only_auto_id_local_title_auto_review_and_next_job_same_thread tests/test_owner_recovery_ui.py::test_production_factory_injects_bounded_approval tests/test_packaging.py::test_packaged_main_reuses_explicit_harness_root_without_a_data_root_dialog tests/test_packaging.py::test_entry_uses_package_main_and_smoke_requires_explicit_opt_in tests/test_packaging.py::test_spec_is_windowed_onedir_with_official_runtime_hooks_only --maxfail=1
python -m compileall -q src tests packaging
git diff --check
```

Result: `79 passed in 55.20s`; compileall PASS; diff check PASS. Use the explicit interpreter above if `python` does not resolve to that installation.

The full suite was not run in this task. The previous full-suite PROJECT_JOB_ACTIVE failure is still an unresolved historical gate. No claim of full regression PASS is made.

## Packaged evidence

Build command used the existing spec with `--distpath` and `--workpath` inside the newly created OS TEMP acceptance workspace. The final source was compiled and built after the readability fix.

```powershell
python -m PyInstaller --noconfirm --distpath <TEMP-workspace>/dist --workpath <TEMP-workspace>/build packaging/reviewrelay.spec
ReviewRelay.exe --packaged-smoke --mode ux --config <synthetic-config.json> --output <bounded-diagnostics.json>
```

`prepare_fixture` rejects non-TEMP workspaces and refuses overwriting an existing fixture. The fixture contains only invented Project/Worker/task metadata. UI acceptance rejects a missing disposable marker, an unexpected startup chooser, a foreign-root window, hidden text caused by theme collisions, technical identity in normal screens, missing chat milestones, or changed durable hashes. Discovery/inference and reviewer sending are disabled in the read-only harness.

The canonical artifact was staged through `publish_verified_onedir`, verified, swapped into the canonical location, then verified again from a different cwd. Both checks exited 0 with `status=PASS`, `frozen=true`, `application_root=G:\Relay GPT-CODEX`, durable state unchanged, inference 0 and ChatGPT messages 0. The accepted and copied EXE SHA-256 is `80d15cc1aa195cccbdc209f5b3002831c9da02b003fdd5aecdcfd80b069e507f`.

All five native packaged screenshots were visually inspected. The first rendered build exposed white text on white controls under the dark Windows theme; explicit palette/stylesheet colors fixed it, with a regression test and packaged contrast guards. The final screenshots show readable Project cards, step buttons, Worker titles, composer placeholder and technical details.

Diagnostics reside at `C:\Users\Admin\AppData\Local\Temp\ReviewRelay\Diagnostics\owner-ux-86048c7afc584181a0e8f43a496637cd`. The exact newly created acceptance workspace was removed after successful canonical verification; old acceptance/Owner directories were not removed.

## Final repository result

- HEAD/remote main: `572045533cb48d9ef9c2aee99f81c6680ed52391`.
- Tracked working tree clean: NO.
- Commit/push: NOT PERFORMED.
- Canonical packaged UI smoke: PASS.
- Ready for Owner visual test: YES.
- Live Task/backend release acceptance: NOT CLAIMED.
