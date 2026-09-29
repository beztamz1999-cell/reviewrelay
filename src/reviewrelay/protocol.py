"""Strict Phase 2 parser for reviewer responses and candidate-bound decisions."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .config import ProjectConfig
from .errors import (
    InvalidFinding,
    InvalidReviewPayload,
    MalformedControlJSON,
    MissingControlBlock,
    MultipleControlBlocks,
    StaleReview,
    UnsupportedProtocol,
    UnknownReviewAction,
)
from .evidence_dsl import (
    DEFAULT_EVIDENCE_LIMITS,
    EvidenceLimits,
    EvidenceRequest,
    validate_evidence_requests,
)


PROTOCOL_VERSION = "rr.v1"
MAX_CONTROL_BLOCK_LENGTH = 1_000_000
MAX_FINDINGS_PER_REVIEW = 100
MAX_FINDING_SUMMARY_LENGTH = 4_096
MAX_HUMAN_ROUTING_TEXT_LENGTH = 16_384
_OPEN_TAG = "<RELAY_CONTROL>"
_CLOSE_TAG = "</RELAY_CONTROL>"
_CONTROL_TAG_START_RE = re.compile(r"</?\s*RELAY_CONTROL", re.IGNORECASE)
_CANDIDATE_MAX_LENGTH = 128


class ReviewerAction(str, Enum):
    PASS = "PASS"
    FIX_REQUIRED = "FIX_REQUIRED"
    NEED_EVIDENCE = "NEED_EVIDENCE"
    OWNER_DECISION_REQUIRED = "OWNER_DECISION_REQUIRED"
    REVIEW_ERROR = "REVIEW_ERROR"


class FindingSeverity(str, Enum):
    BLOCKING = "blocking"
    WARNING = "warning"
    INFO = "info"


@dataclass(frozen=True)
class Finding:
    severity: FindingSeverity
    summary: str


@dataclass(frozen=True)
class ValidatedReview:
    """Validated, candidate-bound routing input. It contains no raw JSON mapping."""

    protocol: str
    candidate_sha: str
    cycle: int
    action: ReviewerAction
    findings: tuple[Finding, ...] = ()
    worker_instruction: str | None = None
    evidence_requests: tuple[EvidenceRequest, ...] = ()
    reason: str | None = None
    context: str | None = None


class _DuplicateJSONKey(ValueError):
    pass


def extract_control_json(response_text: str) -> str:
    """Extract exactly one literal control block, rejecting malformed tag shapes."""
    if not isinstance(response_text, str):
        raise InvalidReviewPayload("Reviewer response must be text")

    starts = list(_CONTROL_TAG_START_RE.finditer(response_text))
    if not starts:
        raise MissingControlBlock("Reviewer response does not contain a RELAY_CONTROL block")

    opens = [match.start() for match in re.finditer(re.escape(_OPEN_TAG), response_text)]
    closes = [match.start() for match in re.finditer(re.escape(_CLOSE_TAG), response_text)]
    if len(starts) > 2 or len(opens) > 1 or len(closes) > 1:
        raise MultipleControlBlocks("Reviewer response contains multiple or nested RELAY_CONTROL tags")
    if len(opens) != 1 or len(closes) != 1:
        raise InvalidReviewPayload("Reviewer response contains malformed RELAY_CONTROL tags")

    # The only tag-like tokens allowed are the one exact opening and closing tag.
    if len(starts) != 2 or starts[0].start() != opens[0] or starts[1].start() != closes[0]:
        raise InvalidReviewPayload("Reviewer response contains ambiguous RELAY_CONTROL tags")
    open_end = opens[0] + len(_OPEN_TAG)
    if closes[0] < open_end:
        raise InvalidReviewPayload("RELAY_CONTROL closing tag precedes its opening tag")
    payload = response_text[open_end:closes[0]].strip()
    if not payload:
        raise MalformedControlJSON("RELAY_CONTROL block is empty")
    if len(payload) > MAX_CONTROL_BLOCK_LENGTH:
        raise MalformedControlJSON(
            f"RELAY_CONTROL block exceeds the {MAX_CONTROL_BLOCK_LENGTH}-character maximum"
        )
    return payload


def validate_review_response(
    response_text: str,
    *,
    expected_candidate_sha: str,
    expected_cycle: int,
    project_config: ProjectConfig | None = None,
    evidence_limits: EvidenceLimits = DEFAULT_EVIDENCE_LIMITS,
) -> ValidatedReview:
    """Parse strictly, validate the complete action, then bind it to current identity.

    A stale result raises ``StaleReview`` without returning or exposing a routeable action.
    """
    if not isinstance(expected_candidate_sha, str) or not expected_candidate_sha.strip():
        raise ValueError("expected_candidate_sha must be non-empty text")
    if isinstance(expected_cycle, bool) or not isinstance(expected_cycle, int) or expected_cycle < 1:
        raise ValueError("expected_cycle must be an integer >= 1")

    payload = extract_control_json(response_text)
    try:
        raw = json.loads(
            payload,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_non_json_constant,
        )
    except (json.JSONDecodeError, _DuplicateJSONKey, ValueError, RecursionError) as exc:
        raise MalformedControlJSON(f"RELAY_CONTROL must contain strict JSON: {exc}") from exc
    if not isinstance(raw, dict) or any(not isinstance(key, str) for key in raw):
        raise InvalidReviewPayload("RELAY_CONTROL JSON must be an object with string keys")

    common = {"protocol", "candidate_sha", "cycle", "action"}
    missing_common = common - raw.keys()
    if missing_common:
        raise InvalidReviewPayload("Missing required field(s): " + ", ".join(sorted(missing_common)))
    protocol = raw["protocol"]
    if protocol != PROTOCOL_VERSION:
        raise UnsupportedProtocol(f"Unsupported reviewer protocol {protocol!r}; expected {PROTOCOL_VERSION!r}")
    candidate_sha = raw["candidate_sha"]
    if (
        not isinstance(candidate_sha, str)
        or not candidate_sha.strip()
        or len(candidate_sha) > _CANDIDATE_MAX_LENGTH
        or any(character.isspace() for character in candidate_sha)
    ):
        raise InvalidReviewPayload("candidate_sha must be non-empty bounded text without whitespace")
    cycle = raw["cycle"]
    if isinstance(cycle, bool) or not isinstance(cycle, int) or cycle < 1:
        raise InvalidReviewPayload("cycle must be an integer >= 1")

    action_value = raw["action"]
    if not isinstance(action_value, str):
        raise InvalidReviewPayload("action must be a string")
    try:
        action = ReviewerAction(action_value)
    except ValueError as exc:
        raise UnknownReviewAction(f"Unknown reviewer action {action_value!r}") from exc

    allowed_fields = common | _fields_for_action(action)
    extra_fields = raw.keys() - allowed_fields
    if extra_fields:
        raise InvalidReviewPayload("Unexpected field(s): " + ", ".join(sorted(extra_fields)))

    findings = _parse_findings(raw["findings"]) if "findings" in raw else ()
    instruction: str | None = None
    evidence_requests: tuple[EvidenceRequest, ...] = ()
    reason = _optional_text(raw, "reason")
    context = _optional_text(raw, "context")

    if action is ReviewerAction.FIX_REQUIRED:
        if "worker_instruction" not in raw:
            raise InvalidReviewPayload("FIX_REQUIRED requires worker_instruction")
        instruction = raw["worker_instruction"]
        if (
            not isinstance(instruction, str)
            or not instruction.strip()
            or "\x00" in instruction
            or len(instruction) > MAX_HUMAN_ROUTING_TEXT_LENGTH
        ):
            raise InvalidReviewPayload(
                f"worker_instruction must be non-empty opaque text of at most {MAX_HUMAN_ROUTING_TEXT_LENGTH} characters"
            )
    elif action is ReviewerAction.NEED_EVIDENCE:
        if "evidence_requests" not in raw:
            raise InvalidReviewPayload("NEED_EVIDENCE requires evidence_requests")
        evidence_requests = validate_evidence_requests(
            raw["evidence_requests"], project_config=project_config, limits=evidence_limits
        )

    # Binding is deliberately last: stale but otherwise-valid responses never expose a
    # ValidatedReview, so future routing code cannot accidentally apply their action.
    if candidate_sha != expected_candidate_sha or cycle != expected_cycle:
        raise StaleReview(
            "Reviewer response is stale for the current candidate SHA or review cycle"
        )

    return ValidatedReview(
        protocol=PROTOCOL_VERSION,
        candidate_sha=candidate_sha,
        cycle=cycle,
        action=action,
        findings=findings,
        worker_instruction=instruction,
        evidence_requests=evidence_requests,
        reason=reason,
        context=context,
    )


def _fields_for_action(action: ReviewerAction) -> set[str]:
    if action is ReviewerAction.PASS:
        return {"findings"}
    if action is ReviewerAction.FIX_REQUIRED:
        return {"worker_instruction", "findings"}
    if action is ReviewerAction.NEED_EVIDENCE:
        return {"evidence_requests", "findings"}
    return {"reason", "context"}


def _parse_findings(value: Any) -> tuple[Finding, ...]:
    if not isinstance(value, list):
        raise InvalidFinding("findings must be an array")
    if len(value) > MAX_FINDINGS_PER_REVIEW:
        raise InvalidFinding(f"findings may contain at most {MAX_FINDINGS_PER_REVIEW} entries")
    parsed: list[Finding] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict) or any(not isinstance(key, str) for key in item):
            raise InvalidFinding(f"finding at index {index} must be an object")
        if item.keys() != {"severity", "summary"}:
            raise InvalidFinding(f"finding at index {index} must contain exactly severity and summary")
        severity_value = item["severity"]
        summary = item["summary"]
        if not isinstance(severity_value, str):
            raise InvalidFinding(f"finding at index {index} severity must be a string")
        try:
            severity = FindingSeverity(severity_value)
        except ValueError as exc:
            raise InvalidFinding(f"finding at index {index} has unknown severity {severity_value!r}") from exc
        if (
            not isinstance(summary, str)
            or not summary.strip()
            or "\x00" in summary
            or len(summary) > MAX_FINDING_SUMMARY_LENGTH
        ):
            raise InvalidFinding(
                f"finding at index {index} summary must be non-empty text of at most {MAX_FINDING_SUMMARY_LENGTH} characters"
            )
        parsed.append(Finding(severity=severity, summary=summary))
    return tuple(parsed)


def _optional_text(raw: dict[str, Any], field_name: str) -> str | None:
    if field_name not in raw:
        return None
    value = raw[field_name]
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\x00" in value
        or len(value) > MAX_HUMAN_ROUTING_TEXT_LENGTH
    ):
        raise InvalidReviewPayload(
            f"{field_name} must be non-empty human-readable text of at most {MAX_HUMAN_ROUTING_TEXT_LENGTH} characters"
        )
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJSONKey(f"duplicate object key {key!r}")
        result[key] = value
    return result


def _reject_non_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant {value!r} is not allowed")
