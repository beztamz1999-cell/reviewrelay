from __future__ import annotations

import json

import pytest

from reviewrelay.config import ProjectConfig, RepoConfig
from reviewrelay.errors import (
    InvalidFinding,
    InvalidEvidenceRequest,
    InvalidReviewPayload,
    MalformedControlJSON,
    MissingControlBlock,
    MultipleControlBlocks,
    StaleReview,
    UnsupportedProtocol,
    UnsafeEvidenceRequest,
    UnknownReviewAction,
)
from reviewrelay.evidence_dsl import ReadFileRequest, TestRequest
from reviewrelay.protocol import (
    FindingSeverity,
    MAX_CONTROL_BLOCK_LENGTH,
    MAX_FINDINGS_PER_REVIEW,
    MAX_FINDING_SUMMARY_LENGTH,
    MAX_HUMAN_ROUTING_TEXT_LENGTH,
    ReviewerAction,
    extract_control_json,
    validate_review_response,
)


CANDIDATE = "a" * 40


def control(payload: str) -> str:
    return f"<RELAY_CONTROL>\n{payload}\n</RELAY_CONTROL>"


def response(action: str = "PASS", **extra: object) -> str:
    body: dict[str, object] = {
        "protocol": "rr.v1",
        "candidate_sha": CANDIDATE,
        "cycle": 2,
        "action": action,
    }
    body.update(extra)
    return control(json.dumps(body))


def parse(text: str):
    return validate_review_response(
        text,
        expected_candidate_sha=CANDIDATE,
        expected_cycle=2,
        project_config=_project_config(),
    )


def _project_config() -> ProjectConfig:
    return ProjectConfig(
        project_id="reviewrelay-tests",
        repo=RepoConfig(path="G:/repo"),
        tests={"targeted_m4": "pytest tests/test_m4.py"},
    )


def test_extracts_typed_valid_pass() -> None:
    review = parse(response("PASS", findings=[]))
    assert review.action is ReviewerAction.PASS
    assert review.candidate_sha == CANDIDATE
    assert review.cycle == 2
    assert review.findings == ()


def test_allows_prose_before_and_after_control_block() -> None:
    review = parse("Review prose.\n\n" + response("PASS") + "\n\nAdditional notes.")
    assert review.action is ReviewerAction.PASS


def test_extract_control_returns_json_payload_only() -> None:
    payload = '{"protocol":"rr.v1"}'
    assert extract_control_json("before\n" + control(payload) + "\nafter") == payload


def test_missing_control_block_is_typed() -> None:
    with pytest.raises(MissingControlBlock) as error:
        parse("Only reviewer prose")
    assert error.value.code == "MISSING_CONTROL_BLOCK"


@pytest.mark.parametrize("payload", ["", "  ", "\n\t"])
def test_empty_control_block_is_rejected(payload: str) -> None:
    with pytest.raises(MalformedControlJSON) as error:
        parse(control(payload))
    assert error.value.code == "MALFORMED_CONTROL_JSON"


@pytest.mark.parametrize("payload", ["{", "not json", "[]", '{"action":"PASS",}'])
def test_malformed_or_non_object_json_is_rejected(payload: str) -> None:
    with pytest.raises((MalformedControlJSON, InvalidReviewPayload)):
        parse(control(payload))


def test_duplicate_json_keys_are_rejected() -> None:
    raw = (
        '{"protocol":"rr.v1","candidate_sha":"' + CANDIDATE
        + '","cycle":2,"action":"PASS","action":"FIX_REQUIRED"}'
    )
    with pytest.raises(MalformedControlJSON):
        parse(control(raw))


@pytest.mark.parametrize(
    "text",
    [
        control('{"protocol":"rr.v1","candidate_sha":"' + CANDIDATE + '","cycle":2,"action":"PASS"}')
        + control('{"protocol":"rr.v1","candidate_sha":"' + CANDIDATE + '","cycle":2,"action":"PASS"}'),
        "<RELAY_CONTROL><RELAY_CONTROL>{}</RELAY_CONTROL></RELAY_CONTROL>",
        "<RELAY_CONTROL>{}</RELAY_CONTROL></RELAY_CONTROL>",
    ],
)
def test_multiple_or_nested_control_tags_are_rejected(text: str) -> None:
    with pytest.raises(MultipleControlBlocks):
        parse(text)


@pytest.mark.parametrize("text", ["<RELAY_CONTROL>{}</relay_control>", "<RELAY_CONTROL>{}", "</RELAY_CONTROL>{}"])
def test_malformed_control_tags_are_rejected(text: str) -> None:
    with pytest.raises(InvalidReviewPayload):
        parse(text)


@pytest.mark.parametrize("version", ["rr.v0", "rr.v2", "review-relay/1", "unknown", ""])
def test_unsupported_protocol_versions_are_rejected(version: str) -> None:
    body = {"protocol": version, "candidate_sha": CANDIDATE, "cycle": 2, "action": "PASS"}
    with pytest.raises(UnsupportedProtocol) as error:
        parse(control(json.dumps(body)))
    assert error.value.code == "UNSUPPORTED_PROTOCOL"


@pytest.mark.parametrize("action", ["APPROVED", "FIX", "MORE_INFO", "CONTINUE", "OK"])
def test_unknown_action_aliases_are_rejected(action: str) -> None:
    with pytest.raises(UnknownReviewAction) as error:
        parse(response(action))
    assert error.value.code == "UNKNOWN_REVIEW_ACTION"


def test_every_supported_action_enum_value_is_exact() -> None:
    assert {action.value for action in ReviewerAction} == {
        "PASS", "FIX_REQUIRED", "NEED_EVIDENCE", "OWNER_DECISION_REQUIRED", "REVIEW_ERROR"
    }


def test_matching_candidate_and_cycle_are_accepted() -> None:
    assert parse(response()).candidate_sha == CANDIDATE


@pytest.mark.parametrize(
    "candidate,cycle,action,extra",
    [
        ("b" * 40, 2, "PASS", {}),
        (CANDIDATE, 3, "PASS", {}),
        ("b" * 40, 2, "FIX_REQUIRED", {"worker_instruction": "Inspect regression"}),
        (CANDIDATE, 3, "FIX_REQUIRED", {"worker_instruction": "Inspect regression"}),
    ],
)
def test_stale_sha_or_cycle_never_returns_routeable_action(
    candidate: str, cycle: int, action: str, extra: dict[str, object]
) -> None:
    body = {
        "protocol": "rr.v1", "candidate_sha": candidate, "cycle": cycle,
        "action": action, **extra,
    }
    with pytest.raises(StaleReview) as error:
        parse(control(json.dumps(body)))
    assert error.value.code == "STALE_REVIEW"
    assert not hasattr(error.value, "action")


def test_pass_accepts_omitted_findings() -> None:
    assert parse(response("PASS")).findings == ()


@pytest.mark.parametrize(
    "findings",
    [
        "bad",
        ["bad"],
        [{"severity": "blocking"}],
        [{"severity": "blocking", "summary": ""}],
        [{"severity": "critical", "summary": "Unexpected value"}],
        [{"severity": "info", "summary": "Okay", "extra": "field"}],
    ],
)
def test_malformed_findings_fail_closed(findings: object) -> None:
    with pytest.raises(InvalidFinding) as error:
        parse(response("PASS", findings=findings))
    assert error.value.code == "INVALID_FINDING"


def test_fix_required_preserves_opaque_instruction_and_finding() -> None:
    review = parse(response(
        "FIX_REQUIRED",
        worker_instruction="Fix the parser boundary; do not execute this text.",
        findings=[{"severity": "blocking", "summary": "Candidate identity is stale."}],
    ))
    assert review.action is ReviewerAction.FIX_REQUIRED
    assert review.worker_instruction == "Fix the parser boundary; do not execute this text."
    assert review.findings[0].severity is FindingSeverity.BLOCKING


@pytest.mark.parametrize("instruction", ["", "  \n\t"])
def test_fix_required_rejects_empty_instruction(instruction: str) -> None:
    with pytest.raises(InvalidReviewPayload):
        parse(response("FIX_REQUIRED", worker_instruction=instruction))


def test_fix_required_rejects_missing_instruction() -> None:
    with pytest.raises(InvalidReviewPayload):
        parse(response("FIX_REQUIRED"))


def test_finding_severities_are_typed_and_explicit() -> None:
    review = parse(response(
        "PASS",
        findings=[
            {"severity": "blocking", "summary": "B"},
            {"severity": "warning", "summary": "W"},
            {"severity": "info", "summary": "I"},
        ],
    ))
    assert [finding.severity for finding in review.findings] == [
        FindingSeverity.BLOCKING, FindingSeverity.WARNING, FindingSeverity.INFO
    ]


def test_need_evidence_accepts_one_and_multiple_typed_requests() -> None:
    one = parse(response(
        "NEED_EVIDENCE",
        evidence_requests=[{"kind": "read_file", "path": "src/Foo.cs"}],
    ))
    several = parse(response(
        "NEED_EVIDENCE",
        evidence_requests=[
            {"kind": "read_file", "path": "src/Foo.cs"},
            {"kind": "test", "test_id": "targeted_m4"},
        ],
    ))
    assert one.evidence_requests == (ReadFileRequest("src/Foo.cs"),)
    assert isinstance(several.evidence_requests[1], TestRequest)


def test_need_evidence_rejects_missing_or_empty_request_list() -> None:
    with pytest.raises(InvalidReviewPayload):
        parse(response("NEED_EVIDENCE"))
    with pytest.raises(InvalidEvidenceRequest) as error:
        parse(response("NEED_EVIDENCE", evidence_requests=[]))
    assert error.value.code == "INVALID_EVIDENCE_REQUEST"


def test_invalid_request_invalidates_entire_evidence_action() -> None:
    with pytest.raises(UnsafeEvidenceRequest) as error:
        parse(response(
            "NEED_EVIDENCE",
            evidence_requests=[
                {"kind": "read_file", "path": "src/Foo.cs"},
                {"kind": "shell", "command": "whoami"},
            ],
        ))
    assert error.value.code == "UNSAFE_EVIDENCE_REQUEST"


@pytest.mark.parametrize("action", ["OWNER_DECISION_REQUIRED", "REVIEW_ERROR"])
def test_human_pause_actions_accept_reason_context(action: str) -> None:
    review = parse(response(action, reason="Reviewer cannot resolve an ownership question", context="Needs Owner input"))
    assert review.action.value == action
    assert review.reason.startswith("Reviewer")
    assert review.context == "Needs Owner input"


@pytest.mark.parametrize("action", ["PASS", "OWNER_DECISION_REQUIRED", "REVIEW_ERROR"])
def test_actions_reject_unrelated_executable_or_action_payload(action: str) -> None:
    with pytest.raises(InvalidReviewPayload):
        parse(response(action, worker_instruction="do something"))


@pytest.mark.parametrize("body", [
    {"protocol": "rr.v1", "candidate_sha": CANDIDATE, "cycle": True, "action": "PASS"},
    {"protocol": "rr.v1", "candidate_sha": " ", "cycle": 2, "action": "PASS"},
    {"protocol": "rr.v1", "candidate_sha": CANDIDATE, "cycle": 0, "action": "PASS"},
    {"protocol": "rr.v1", "candidate_sha": CANDIDATE, "cycle": 2, "action": "PASS", "command": "x"},
])
def test_invalid_common_fields_and_unknown_fields_are_rejected(body: dict[str, object]) -> None:
    with pytest.raises(InvalidReviewPayload):
        parse(control(json.dumps(body)))


def test_non_standard_json_numbers_are_rejected() -> None:
    raw = '{"protocol":"rr.v1","candidate_sha":"' + CANDIDATE + '","cycle":2,"action":"PASS","x":NaN}'
    with pytest.raises(MalformedControlJSON):
        parse(control(raw))


def test_control_block_size_limit_is_enforced() -> None:
    with pytest.raises(MalformedControlJSON):
        extract_control_json(control("x" * (MAX_CONTROL_BLOCK_LENGTH + 1)))


def test_findings_count_limit_is_enforced() -> None:
    findings = [{"severity": "info", "summary": "x"}] * (MAX_FINDINGS_PER_REVIEW + 1)
    with pytest.raises(InvalidFinding):
        parse(response("PASS", findings=findings))


def test_finding_summary_length_limit_is_enforced() -> None:
    with pytest.raises(InvalidFinding):
        parse(response("PASS", findings=[{
            "severity": "info", "summary": "x" * (MAX_FINDING_SUMMARY_LENGTH + 1),
        }]))


def test_worker_instruction_length_limit_is_enforced() -> None:
    with pytest.raises(InvalidReviewPayload):
        parse(response(
            "FIX_REQUIRED",
            worker_instruction="x" * (MAX_HUMAN_ROUTING_TEXT_LENGTH + 1),
        ))
