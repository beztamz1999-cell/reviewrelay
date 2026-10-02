"""Read-only recovery offers from bound durable state; execution rechecks all guards."""
from __future__ import annotations

import hashlib
import json
from enum import Enum

from .controller_store import ControllerState as S
from .errors import ReviewRelayError


class RecoveryAction(str, Enum):
    CONTINUE_WORKER = "Continue Same Worker"
    CHECK_WORKER = "Check Worker Completion"
    RETRY_REVIEW = "Retry Reviewer Send"
    RECOVER_REVIEW = "Recover Reviewer Response"


def recovery_action(controller, identity):
    """Offer only a supported reconciliation path, never authorize an external effect.

    Git, server terminal status and the visible prompt/reply are independently
    checked by the existing locked recovery operations when the Owner proceeds.
    """
    try:
        task, config = controller._worker_binding(identity)
        store = controller.store
        if (task.state is not S.PAUSED_ERROR or task.manual_pending or task.review_invalidated
                or store.control(identity.project_id, identity.task_id)):
            return None
        record = store.state.get(identity.project_id, identity.task_id)
        key = (task.pending.get("key") if task.pending.get("message_kind") else
            controller._key(task, task.pending.get("worker_kind", ""), task.pending.get("number", -1)))
        effect = store.effect(key) if key else None
        if (not effect or effect["project_id"] != identity.project_id or effect["task_id"] != identity.task_id
                or store.db.execute("SELECT 1 FROM controller_effects WHERE project_id=? AND task_id=? "
                    "AND effect_key!=? AND status NOT IN ('PLANNED','COMPLETED','RECONCILED_TERMINAL') LIMIT 1",
                    (identity.project_id, identity.task_id, key)).fetchone()):
            return None
        if task.pending.get("worker_kind"):
            if (task.candidate_sha or task.published or task.review_cycle or not task.worker_turn_id
                    or record.worker_last_turn_id != task.worker_turn_id
                    or effect["kind"] != task.pending["worker_kind"]
                    or effect["payload"].get("thread_id") != identity.worker_thread_id
                    or effect["payload"].get("turn_id") != task.worker_turn_id):
                return None
            if (task.resume_state == S.WORKER_RUNNING.value and effect["kind"] == "WORKER_CONTINUATION"
                    and effect["status"] == "CONFIRMED"):
                return RecoveryAction.CHECK_WORKER
            terminal = (effect["status"] == "COMPLETED" and record.worker_last_turn_status == "COMPLETED"
                or effect["status"] == "RECONCILED_TERMINAL" and effect["payload"].get("proof_source") == "thread/read"
                and record.worker_last_turn_status in {"INTERRUPTED", "FAILED"}
                and effect["payload"].get("terminal_status") == record.worker_last_turn_status)
            if (task.resume_state == S.VERIFYING_CANDIDATE.value and terminal
                    and task.error_code in {"CANDIDATE_INVALID_DIRTY_WORKTREE", "WORKER_NO_NEW_COMMIT"}):
                return RecoveryAction.CONTINUE_WORKER
            return None
        if (task.resume_state != S.WAITING_REVIEW.value or task.pending.get("message_kind") != "REVIEW_SEND"
                or effect["kind"] != "REVIEW_SEND" or not task.published or store.review(key) is not None
                or not task.pending.get("prompt") or task.pending.get("candidate_sha") != task.candidate_sha
                or task.pending.get("cycle") != task.review_cycle):
            return None
        payload = {"candidate_sha": task.candidate_sha, "cycle": task.review_cycle,
            "conversation_url": config.chatgpt["conversation_url"],
            "prompt_sha256": hashlib.sha256(task.pending["prompt"].encode()).hexdigest()}
        if (task.error_code == "MESSAGE_SEND_FAILED" and effect["status"] in {"NOT_SENT", "AMBIGUOUS"}
                and effect["payload"] == payload):
            return RecoveryAction.RETRY_REVIEW
        if (task.error_code != "MESSAGE_SEND_AMBIGUOUS" or effect["status"] != "AMBIGUOUS"
                or {k: v for k, v in effect["payload"].items() if k != "send_result"} != payload):
            return None
        events = [e for e in store.events(identity.project_id, identity.task_id) if e["source"] == "CONTROLLER"]
        proofs = [e for e in events if e["kind"] == "REVIEW_PRE_CLICK_FAILURE_RECONCILED"
            and json.loads(e["payload_json"]).get("effect_key") == key]
        proof = json.loads(proofs[-1]["payload_json"]) if proofs else {}
        later = [e for e in events if proofs and e["sequence"] > proofs[-1]["sequence"] and e["kind"].endswith("_IN_FLIGHT")]
        if (proof.get("proof_source") == "typed-pre-click-error-and-exact-visible-draft"
                and proof.get("draft_cleared") is True and proof.get("prompt_sha256") == payload["prompt_sha256"]
                and proof.get("conversation_url") == payload["conversation_url"] and len(later) == 1
                and later[0]["kind"] == "REVIEW_SEND_IN_FLIGHT"
                and json.loads(later[0]["payload_json"]).get("effect_key") == key):
            return RecoveryAction.RECOVER_REVIEW
    except (ReviewRelayError, ValueError, KeyError, TypeError):
        return None
    return None
