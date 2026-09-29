"""Deterministic review-cycle guards; callers persist returned task records explicitly."""

from __future__ import annotations

from dataclasses import replace

from .errors import EvidenceCycleLimitExceeded, ReviewCycleLimitExceeded
from .models import TaskRecord, utc_now_iso


def prepare_next_fix_cycle(record: TaskRecord, *, max_fix_cycles: int) -> TaskRecord:
    """Return a persistable record with one fix cycle charged, or require Owner escalation."""
    _validate_limit(max_fix_cycles, "max_fix_cycles")
    _validate_count(record.fix_cycle_count, "fix_cycle_count")
    if record.fix_cycle_count >= max_fix_cycles:
        raise ReviewCycleLimitExceeded(
            f"Fix cycle limit {max_fix_cycles} reached; automatic worker routing must stop"
        )
    return replace(
        record,
        fix_cycle_count=record.fix_cycle_count + 1,
        updated_at=utc_now_iso(),
    )


def prepare_next_evidence_cycle(record: TaskRecord, *, max_evidence_cycles: int) -> TaskRecord:
    """Return a persistable record with one evidence cycle charged, or require Owner escalation."""
    _validate_limit(max_evidence_cycles, "max_evidence_cycles")
    _validate_count(record.evidence_cycle_count, "evidence_cycle_count")
    if record.evidence_cycle_count >= max_evidence_cycles:
        raise EvidenceCycleLimitExceeded(
            f"Evidence cycle limit {max_evidence_cycles} reached; automatic evidence routing must stop"
        )
    return replace(
        record,
        evidence_cycle_count=record.evidence_cycle_count + 1,
        updated_at=utc_now_iso(),
    )


def _validate_limit(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def _validate_count(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
