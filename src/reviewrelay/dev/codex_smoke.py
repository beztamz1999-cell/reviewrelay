"""Explicit two-turn live acceptance in one disposable, managed Git repository."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path
from uuid import uuid4

from ..config import ProjectConfig, RepoConfig
from ..state import StateStore
from ..storage import PortableDataRoot
from ..task import begin_task
from ..worker import CodexAppServerAdapter, CodexWorkerSettings, WorkerError, WorkerTimeouts


MARKER = "REVIEWRELAY_CODEX_SMOKE"


def cleanup_disposable_repo(root: PortableDataRoot, repo: Path, task_id: str) -> None:
    """Check the absolute target and handle Git's read-only object files on Windows."""
    checked = root.assert_managed_path(repo)
    expected = root.safe_path(Path("logs") / task_id / "repo")
    if checked != expected or repo.is_symlink():
        raise RuntimeError("SMOKE_CLEANUP_TARGET_INVALID")

    def readonly_retry(function, path, exc_info):
        item = Path(path).resolve(strict=False)
        if item != checked and not item.is_relative_to(checked):
            raise RuntimeError("SMOKE_CLEANUP_CHILD_INVALID")
        if not isinstance(exc_info[1], PermissionError):
            raise exc_info[1]
        os.chmod(item, stat.S_IREAD | stat.S_IWRITE)
        function(item)

    if checked.exists():
        shutil.rmtree(checked, onerror=readonly_retry)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, shell=False, capture_output=True, check=True)


async def smoke(root: PortableDataRoot, settings: CodexWorkerSettings) -> dict:
    root.create()
    task_id = "codex-smoke-" + uuid4().hex
    repo = root.safe_path(Path("logs") / task_id / "repo")
    repo.mkdir(parents=True)
    _git(repo, "init", "-b", "main")
    _git(repo, "-c", "user.name=ReviewRelay Smoke", "-c", "user.email=smoke@example.invalid", "commit", "--allow-empty", "-m", "Disposable smoke baseline")
    config = ProjectConfig("phase4-live", RepoConfig(str(repo)))
    begin_task(config, task_id, root)
    first = CodexAppServerAdapter(root, config, task_id, settings)
    second = None
    evidence = {"task_id": task_id, "repo": str(repo), "live_smoke": "FAIL", "thread_resume": "NOT_RUN"}
    try:
        print("CODEX_SMOKE_TURN1_STARTED", flush=True)
        await first.start_task(
            "In this disposable repository only, create relay-worker-smoke.txt containing exactly "
            "the UTF-8 text REVIEWRELAY_CODEX_SMOKE followed by one newline. "
            "Use a local file tool or command, then reply briefly with completion. "
            "Do not run tests, use the network, create commits, or change any other file.")
        created = await first.wait_until_done()
        evidence.update(thread_id=created.thread_id, turn1_id=created.turn_id, turn1_status=created.status.value,
                        turn1_trace=str(created.trace_path), turn1_pid=first._transport.process.pid,
                        events1=sorted({event.kind for event in first.timeline}))
        file = repo / "relay-worker-smoke.txt"
        if not file.is_file() or file.read_bytes() != (MARKER + "\n").encode():
            raise RuntimeError("SMOKE_FILE_CONTENT_MISMATCH")
        evidence["file_content_verified"] = True
        await first.close()
        print("CODEX_SMOKE_PROCESS_RESTART_AND_EXACT_THREAD_RESUME", flush=True)
        second = CodexAppServerAdapter(root, config, task_id, settings)
        resumed = await second.resume_task()
        if resumed != created.thread_id:
            raise RuntimeError("SMOKE_THREAD_ID_MISMATCH")
        await second.send_instruction(
            "Read relay-worker-smoke.txt and reply with its exact content only. "
            "Do not modify files, run tests, use the network, or create commits.")
        read = await second.wait_until_done()
        if read.thread_id != created.thread_id or MARKER not in read.final_response:
            raise RuntimeError("SMOKE_RESUME_RESPONSE_MISMATCH")
        if file.read_bytes() != (MARKER + "\n").encode():
            raise RuntimeError("SMOKE_FILE_CHANGED_DURING_READ")
        with StateStore(root) as store:
            record = store.get(config.project_id, task_id)
            if record.worker_thread_id != created.thread_id or record.worker_last_turn_id != read.turn_id:
                raise RuntimeError("SMOKE_PERSISTED_ID_MISMATCH")
        evidence.update(live_smoke="PASS", thread_resume="PASS", same_thread_id=True,
                        turn2_id=read.turn_id, turn2_status=read.status.value, turn2_trace=str(read.trace_path),
                        turn2_pid=second._transport.process.pid,
                        process_restarted=second._transport.process.pid != evidence["turn1_pid"],
                        events2=sorted({event.kind for event in second.timeline}), final_response_contains=MARKER)
    except WorkerError as exc:
        evidence["error_code"] = exc.code
    except Exception as exc:
        evidence["error_code"] = type(exc).__name__
        # Only local verification failures carry known fixed labels; no server/auth text is copied.
        if isinstance(exc, RuntimeError):
            evidence["verification_error"] = str(exc)
    finally:
        await first.close()
        if second:
            await second.close()
        destination = root.safe_path(Path("logs") / task_id / "result.json")
        # Preserve the original transport outcome even if cleanup encounters a filesystem error.
        evidence["disposable_repo_removed"] = False
        destination.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
        try:
            cleanup_disposable_repo(root, repo, task_id)
            evidence["disposable_repo_removed"] = not repo.exists()
        except OSError:
            evidence["cleanup_error"] = "DISPOSABLE_REPO_CLEANUP_FAILED"
            evidence["live_smoke"] = "FAIL"
        destination.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
        evidence["evidence_path"] = str(destination)
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--executable", default="codex")
    parser.add_argument("--model")
    parser.add_argument("--reasoning-effort")
    args = parser.parse_args()
    settings = CodexWorkerSettings(executable=args.executable, model=args.model, reasoning_effort=args.reasoning_effort,
                                   timeouts=WorkerTimeouts(idle_seconds=180, overall_seconds=600))
    evidence = asyncio.run(smoke(PortableDataRoot(args.data_root), settings))
    print(json.dumps(evidence, indent=2))
    return 0 if evidence["live_smoke"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
