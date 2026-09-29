"""Stable review identity for future send deduplication; no network behavior."""

from __future__ import annotations

import hashlib
import json

from .config import validate_identifier


def make_review_key(project_id: str, task_id: str, candidate_sha: str, cycle: int) -> str:
    """Hash a canonical JSON tuple so identity boundaries are unambiguous and stable."""
    project_id = validate_identifier(project_id, "project_id")
    task_id = validate_identifier(task_id, "task_id")
    if (
        not isinstance(candidate_sha, str)
        or not candidate_sha.strip()
        or len(candidate_sha) > 128
        or any(character.isspace() for character in candidate_sha)
    ):
        raise ValueError("candidate_sha must be non-empty bounded text without whitespace")
    if isinstance(cycle, bool) or not isinstance(cycle, int) or cycle < 1:
        raise ValueError("cycle must be an integer >= 1")
    canonical = json.dumps(
        [project_id, task_id, candidate_sha, cycle],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return "rr.v1:" + hashlib.sha256(canonical).hexdigest()
