from __future__ import annotations

import pytest

from reviewrelay.cycles import prepare_next_evidence_cycle, prepare_next_fix_cycle
from reviewrelay.errors import EvidenceCycleLimitExceeded, ReviewCycleLimitExceeded
from reviewrelay.models import TaskRecord
from reviewrelay.review_key import make_review_key


def test_next_fix_cycle_allowed_below_configured_limit() -> None:
    record = TaskRecord("reviewrelay-tests", "task-1", fix_cycle_count=1)
    updated = prepare_next_fix_cycle(record, max_fix_cycles=3)
    assert updated.fix_cycle_count == 2
    assert record.fix_cycle_count == 1


def test_fix_cycle_limit_requires_owner_escalation() -> None:
    record = TaskRecord("reviewrelay-tests", "task-1", fix_cycle_count=3)
    with pytest.raises(ReviewCycleLimitExceeded) as error:
        prepare_next_fix_cycle(record, max_fix_cycles=3)
    assert error.value.code == "REVIEW_CYCLE_LIMIT_EXCEEDED"
    assert error.value.action == "OWNER_ESCALATION_REQUIRED"


def test_zero_fix_cycle_limit_escalates_before_any_cycle() -> None:
    with pytest.raises(ReviewCycleLimitExceeded):
        prepare_next_fix_cycle(TaskRecord("reviewrelay-tests", "task-1"), max_fix_cycles=0)


def test_next_evidence_cycle_allowed_below_configured_limit() -> None:
    record = TaskRecord("reviewrelay-tests", "task-1", evidence_cycle_count=1)
    updated = prepare_next_evidence_cycle(record, max_evidence_cycles=5)
    assert updated.evidence_cycle_count == 2


def test_evidence_cycle_limit_requires_owner_escalation() -> None:
    record = TaskRecord("reviewrelay-tests", "task-1", evidence_cycle_count=5)
    with pytest.raises(EvidenceCycleLimitExceeded) as error:
        prepare_next_evidence_cycle(record, max_evidence_cycles=5)
    assert error.value.code == "EVIDENCE_CYCLE_LIMIT_EXCEEDED"
    assert error.value.action == "OWNER_ESCALATION_REQUIRED"


@pytest.mark.parametrize("value", [-1, True, 1.5])
def test_cycle_limit_rejects_invalid_configuration(value: object) -> None:
    with pytest.raises(ValueError):
        prepare_next_fix_cycle(TaskRecord("reviewrelay-tests", "task-1"), max_fix_cycles=value)  # type: ignore[arg-type]


def test_review_key_is_stable_for_same_identity() -> None:
    first = make_review_key("reviewrelay-tests", "task-1", "a" * 40, 2)
    second = make_review_key("reviewrelay-tests", "task-1", "a" * 40, 2)
    assert first == second
    assert first.startswith("rr.v1:")


def test_review_key_changes_for_different_candidate() -> None:
    assert make_review_key("reviewrelay-tests", "task-1", "a" * 40, 2) != make_review_key(
        "reviewrelay-tests", "task-1", "b" * 40, 2
    )


def test_review_key_changes_for_different_cycle() -> None:
    assert make_review_key("reviewrelay-tests", "task-1", "a" * 40, 2) != make_review_key(
        "reviewrelay-tests", "task-1", "a" * 40, 3
    )


@pytest.mark.parametrize("project,task", [("other-project", "task-1"), ("reviewrelay-tests", "task-2")])
def test_review_key_includes_project_and_task_identity(project: str, task: str) -> None:
    assert make_review_key(project, task, "a" * 40, 2) != make_review_key(
        "reviewrelay-tests", "task-1", "a" * 40, 2
    )


@pytest.mark.parametrize("candidate,cycle", [("", 1), ("x" * 129, 1), ("sha", 0), ("sha", True)])
def test_review_key_rejects_invalid_identity(candidate: str, cycle: int) -> None:
    with pytest.raises(ValueError):
        make_review_key("reviewrelay-tests", "task-1", candidate, cycle)
