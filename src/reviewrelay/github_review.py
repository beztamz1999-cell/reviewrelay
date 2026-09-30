"""Explicit GitHub review steps; no autonomous worker/fix/evidence routing."""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import asdict, replace
from pathlib import Path

from .evidence_dsl import validate_evidence_request
from .github_publish import CandidatePublisher, GitHubPublishError, PublishedCandidate
from .models import TaskState, utc_now_iso
from .protocol import Finding, FindingSeverity, ReviewerAction, ValidatedReview, validate_review_response
from .review_key import make_review_key
from .reviewer.base import ReviewerAdapter, SendDisposition, SendResult
from .state import StateStore
from .storage import PortableDataRoot, TaskStorage, _atomic_write
from .worker.lock import WorkerTaskLock


def review_notification(candidate: PublishedCandidate) -> str:
    return ("REVIEWRELAY_REVIEW_REQUEST\n\n"
        f"TASK_ID={candidate.task_id}\nREPO={candidate.repository}\nPR={candidate.pr_url or 'NONE'}\n"
        f"BRANCH={candidate.branch}\nBASE_SHA={candidate.base_sha}\nHEAD_SHA={candidate.head_sha}\n"
        f"REVIEW_CYCLE={candidate.review_cycle}\nTASK_SPEC_PATH={candidate.task_spec_path}\n\n"
        "Review this exact GitHub candidate against the task spec at HEAD_SHA. Inspect the diff, source, related "
        "files and tests directly from GitHub, using the exact BASE_SHA and HEAD_SHA above. GitHub is the shared "
        "review mirror; local Git is execution truth. No patch, source, worker report or audit attachments are needed. "
        "Request local NEED_EVIDENCE only for genuinely local/runtime verification that GitHub cannot provide. "
        "Do not invent access or repository facts. If repository/commit access is unavailable, return REVIEW_ERROR "
        "with reason GITHUB_REVIEW_ACCESS_REQUIRED.\n"
        "Finish with exactly one valid <RELAY_CONTROL> JSON block: protocol rr.v1, candidate_sha equal to HEAD_SHA, "
        "cycle equal to REVIEW_CYCLE, and one Phase 2 action. PASS requires findings (severity blocking|warning|info, "
        "summary). FIX_REQUIRED requires findings and worker_instruction; NEED_EVIDENCE requires evidence_requests; "
        "OWNER_DECISION_REQUIRED/REVIEW_ERROR require reason and optional context. Use only action-specific fields. "
        "PASS means ready for Owner review, not release authority.")


class GitHubReviewBridge:
    def __init__(self, root: PortableDataRoot, config, *, publisher: CandidatePublisher, reviewer: ReviewerAdapter,
                 checkpoint_observer=None):
        self.root, self.config, self.publisher, self.reviewer = root, config, publisher, reviewer
        self.state, self.storage = StateStore(root), TaskStorage(root)
        self.observer = checkpoint_observer

    def _key(self, c):
        return make_review_key(c.project_id, c.task_id, c.head_sha, c.review_cycle)

    def _load(self, key):
        row = self.state._connection.execute("SELECT * FROM github_reviews WHERE review_key=?", (key,)).fetchone()
        return dict(row) if row else None

    def _save(self, candidate, status, metadata, *, raw=None, decision=None, event):
        key = self._key(candidate)
        with self.state._connection as db:
            db.execute("""INSERT INTO github_reviews VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(review_key) DO UPDATE SET
                status=excluded.status, metadata_json=excluded.metadata_json,
                raw_text=COALESCE(excluded.raw_text,github_reviews.raw_text),
                decision_json=COALESCE(excluded.decision_json,github_reviews.decision_json), updated_at=excluded.updated_at""",
                (key, candidate.project_id, candidate.task_id, candidate.head_sha, candidate.review_cycle, status,
                 json.dumps(metadata, sort_keys=True), raw, json.dumps(decision, sort_keys=True) if decision else None, utc_now_iso()))
            db.execute("INSERT INTO github_events(project_id,task_id,kind,payload_json,created_at) VALUES(?,?,?,?,?)",
                (candidate.project_id, candidate.task_id, event,
                 json.dumps({"review_key": key, "head_sha": candidate.head_sha, "cycle": candidate.review_cycle, "status": status}), utc_now_iso()))
        if self.observer:
            self.observer(event)

    def _lock(self, c):
        if c.project_id != self.config.project_id:
            raise GitHubPublishError("Review project identity differs", code="GITHUB_BINDING_MISMATCH")
        return WorkerTaskLock(self.root.assert_managed_path(
            self.storage.task_root(c.project_id, c.task_id) / "durable" / "github.lock"))

    async def notify_reviewer(self, candidate: PublishedCandidate, *, conversation_url: str) -> SendResult:
        lock = self._lock(candidate)
        try:
            await self.publisher.verify_current(candidate)
            if conversation_url != self.config.chatgpt.get("conversation_url") or not conversation_url:
                raise GitHubPublishError("Reviewer conversation differs", code="GITHUB_BINDING_MISMATCH")
            key = self._key(candidate)
            prior = self._load(key)
            if prior and prior["status"] != "PLANNED":
                raise GitHubPublishError("Notification already dispatched; automatic resend is blocked", code="REVIEW_NOTIFICATION_ALREADY_DISPATCHED")
            prompt = review_notification(candidate)
            metadata = {"candidate": asdict(candidate), "conversation_url": conversation_url, "prompt": prompt}
            if prior and json.loads(prior["metadata_json"]) != metadata:
                raise GitHubPublishError("Planned notification binding changed", code="GITHUB_BINDING_MISMATCH")
            self._save(candidate, "PLANNED", metadata, event="REVIEW_NOTIFICATION_PLANNED")
            await self.publisher.verify_current(candidate)
            self._save(candidate, "IN_FLIGHT", metadata, event="REVIEW_NOTIFICATION_STARTED")
            try:
                sent = await self.reviewer.send_review_pack(prompt=prompt, review_key=key, attachment_paths=(),
                                                            conversation_url=conversation_url)
                if (sent.review_key != key or sent.conversation_url != conversation_url or sent.attachment_paths
                        or sent.prompt_sha256 != hashlib.sha256(prompt.encode()).hexdigest()
                        or sent.disposition is not SendDisposition.SEND_CONFIRMED):
                    raise GitHubPublishError("Notification ownership mismatch")
            except Exception:
                self._save(candidate, "AMBIGUOUS", metadata, event="REVIEW_NOTIFICATION_UNRESOLVED")
                raise
            metadata["send_result"] = asdict(sent)
            self._save(candidate, "CONFIRMED", metadata, event="REVIEW_NOTIFICATION_SENT")
            r = self.state.get(candidate.project_id, candidate.task_id)
            self.state.save(replace(r, task_state=TaskState.WAIT_REVIEW, reviewer_chat_identity=conversation_url, last_sent_review_key=key))
            self.storage.persist_task_record(self.state.get(candidate.project_id, candidate.task_id))
            return sent
        finally:
            lock.close()

    async def capture_review(self, candidate: PublishedCandidate, sent: SendResult | None = None) -> ValidatedReview:
        lock = self._lock(candidate)
        try:
            await self.publisher.verify_current(candidate)
            row = self._load(self._key(candidate))
            if row is None:
                raise GitHubPublishError("No owned notification exists")
            if row["status"] not in {"CONFIRMED", "RESPONSE_RECEIVED", "VALIDATED"}:
                raise GitHubPublishError("Notification cannot supply an applicable decision", code="REVIEW_NOTIFICATION_UNRESOLVED")
            metadata = json.loads(row["metadata_json"])
            if metadata["candidate"] != asdict(candidate):
                raise GitHubPublishError("Persisted notification is stale", code="STALE_REVIEW")
            raw = row["raw_text"]
            if raw is None:
                if row["status"] != "CONFIRMED" or sent is None or json.loads(json.dumps(asdict(sent))) != metadata.get("send_result"):
                    # A new adapter cannot prove process-local ownership. Never send again to recover it.
                    raise GitHubPublishError("An owned current-adapter SendResult is required", code="REVIEW_NOTIFICATION_UNRESOLVED")
                waiting = asyncio.create_task(self.reviewer.wait_response(sent))
                try:
                    while not waiting.done():
                        await asyncio.wait({waiting}, timeout=1)
                        await self.publisher.verify_current(candidate)
                    response = await waiting
                finally:
                    if not waiting.done():
                        waiting.cancel()
                    await asyncio.gather(waiting, return_exceptions=True)
                await self.publisher.verify_current(candidate)
                if (response.review_key != sent.review_key or response.conversation_url != sent.conversation_url
                        or response.disposition is not SendDisposition.RESPONSE_RECEIVED
                        or response.assistant_turn_identity in sent.pre_send_baseline.assistant_turn_ids):
                    raise GitHubPublishError("Reviewer response ownership mismatch")
                raw = response.text
                path = self.root.assert_managed_path(self.storage.task_root(candidate.project_id, candidate.task_id)
                                                     / "durable" / "reviews" / f"{self._key(candidate).split(':')[-1]}.md")
                _atomic_write(path, raw.encode("utf-8"))
                metadata["response"] = asdict(response)
                self._save(candidate, "RESPONSE_RECEIVED", metadata, raw=raw, event="GITHUB_REVIEW_RECEIVED")
            if row["decision_json"]:
                parsed = json.loads(row["decision_json"])
                parsed["action"] = ReviewerAction(parsed["action"])
                parsed["findings"] = tuple(Finding(FindingSeverity(f["severity"]), f["summary"]) for f in parsed["findings"])
                parsed["evidence_requests"] = tuple(validate_evidence_request(
                    {key: value for key, value in request.items() if value is not None}, project_config=self.config)
                    for request in parsed["evidence_requests"])
                decision = ValidatedReview(**parsed)
            else:
                decision = validate_review_response(raw, expected_candidate_sha=candidate.head_sha,
                    expected_cycle=candidate.review_cycle, project_config=self.config)
                self._save(candidate, "VALIDATED", metadata, decision=asdict(decision), event="GITHUB_REVIEW_VALIDATED")
            await self.publisher.verify_current(candidate)
            r = self.state.get(candidate.project_id, candidate.task_id)
            self.state.save(replace(r, task_state=TaskState.PARSE_REVIEW, last_review_action=decision.action.value))
            self.storage.persist_task_record(self.state.get(candidate.project_id, candidate.task_id))
            return decision  # Routing is deliberately a later phase.
        except Exception as exc:
            row = self._load(self._key(candidate))
            if row:
                metadata = json.loads(row["metadata_json"])
                metadata["error_code"] = getattr(exc, "code", "GITHUB_REVIEW_FAILED")
                self._save(candidate, "INVALIDATED", metadata, event="GITHUB_REVIEW_INVALIDATED")
                record = self.state.get(candidate.project_id, candidate.task_id)
                if record and record.candidate_sha == candidate.head_sha and record.review_cycle == candidate.review_cycle:
                    self.state.save(replace(record, task_state=TaskState.PAUSED_ERROR))
                    self.storage.persist_task_record(self.state.get(candidate.project_id, candidate.task_id))
            raise
        finally:
            lock.close()

    def close(self):
        self.state.close()
