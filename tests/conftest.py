from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "ReviewRelay Tests")
    _git(repo, "config", "user.email", "reviewrelay-tests@example.invalid")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-m", "baseline")
    return repo


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, shell=False, text=True, capture_output=True, check=False)
    if result.returncode:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout


@pytest.fixture
def commit_change():
    def commit(repo: Path, filename: str, content: str, message: str = "change") -> str:
        path = repo / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        _git(repo, "add", "--", filename)
        _git(repo, "commit", "-m", message)
        return _git(repo, "rev-parse", "HEAD").strip()

    return commit
