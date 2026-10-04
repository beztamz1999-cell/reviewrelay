"""Read-only Owner presentation of existing Project and controller records."""
from __future__ import annotations

from datetime import datetime, timezone
import json

from .controller_store import ControllerState as S
from .projects import ConnectionStatus as C


def project_ready(project):
    return bool(project.ready and project.codex_worker_thread_id and project.codex_worker_verified_at)


def setup_step(project):
    if project.local_status is not C.READY:
        return "project"
    if not project.repository_ready:
        return "github"
    if project.chatgpt_status is not C.READY:
        return "chatgpt"
    if project.codex_status is not C.READY or not project_ready(project):
        return "worker"
    return "done"


def local_blocker(project):
    code = project.setup.get("local_inspection", {}).get("classification") or project.last_error_code
    return {
        "DIRTY_WORKTREE": "Project đang có thay đổi Git chưa hoàn tất. ReviewRelay cần project ở trạng thái an toàn trước khi bắt đầu.",
        "DETACHED_HEAD": "Project chưa đứng trên nhánh làm việc. Hãy chọn nhánh làm việc trước khi tiếp tục.",
        "NO_HEAD": "Project Git chưa có commit đầu tiên. Bạn có thể xem trước và tạo bản lưu ban đầu trong Cài đặt nâng cao.",
        "NOT_GIT": "Thư mục này chưa có Git. Bạn có thể khởi tạo Git trong Cài đặt nâng cao.",
        "WRONG_REPOSITORY_ROOT": "Bạn đã chọn thư mục con. Hãy thêm lại project bằng thư mục gốc Git.",
    }.get(code, "ReviewRelay cần kiểm tra project trước khi bắt đầu.")


def friendly_error(code):
    if code in {"LOGIN_REQUIRED", "REVIEWER_LOGIN_REQUIRED"}:
        return "Cần đăng nhập ChatGPT."
    if code == "WORKER_TIMEOUT":
        return "Codex chưa hoàn tất trong thời gian cho phép. Công việc đã tạm dừng."
    if code in {"BLOCKED_DIRTY_BASELINE", "CANDIDATE_INVALID_DIRTY_WORKTREE"}:
        return "Project còn thay đổi chưa hoàn tất. Công việc đã tạm dừng để giữ an toàn."
    if code in {"MESSAGE_SEND_AMBIGUOUS", "WORKER_TURN_AMBIGUOUS", "GITHUB_PUSH_AMBIGUOUS"}:
        return "Cần xác nhận kết quả thao tác vừa rồi. ReviewRelay đã tạm dừng để tránh thực hiện hai lần."
    if code == "CONVERSATION_NOT_READY":
        return "Chưa mở được cuộc trò chuyện ChatGPT. Kiểm tra kết nối trong Cài đặt."
    return "Công việc đã tạm dừng. Xem Cài đặt → Chi tiết kỹ thuật để biết nguyên nhân."


def task_message(task):
    if task.state is S.COMPLETE:
        return "✓ Hoàn tất — ChatGPT đã PASS" if task.ready_for_owner_review and not task.review_invalidated else "Cần kiểm tra lại kết quả trước khi duyệt."
    if task.state is S.WORKER_RUNNING and task.pending.get("worker_kind") == "WORKER_FIX":
        return "ChatGPT yêu cầu chỉnh sửa. Đã gửi lại cho Codex."
    if task.state is S.PAUSED_ERROR:
        return friendly_error(task.error_code)
    return {
        S.DRAFT: "Đang chuẩn bị yêu cầu…", S.READY: "Yêu cầu đã sẵn sàng.",
        S.WORKER_RUNNING: "Codex đang làm…",
        S.VERIFYING_CANDIDATE: "Worker đã hoàn tất. Đang kiểm tra thay đổi…",
        S.PUBLISHING: "Đang đồng bộ candidate lên GitHub…",
        S.WAITING_REVIEW: "ChatGPT đang review…", S.PROCESSING_REVIEW: "Đang kiểm tra phản hồi ChatGPT…",
        S.COLLECTING_EVIDENCE: "Đang thu thập thông tin ChatGPT yêu cầu…",
        S.SENDING_EVIDENCE: "Đang gửi thông tin kiểm chứng cho ChatGPT…",
        S.PAUSED_OWNER: "Cần quyết định của bạn.", S.PAUSED_USER: "Đã tạm dừng theo yêu cầu của bạn.",
        S.PAUSED_OWNER_STEER: "Đã tạm dừng tự động. Bạn có thể gửi chỉ dẫn cho Worker.",
        S.STOPPED: "Đã dừng theo yêu cầu của bạn.",
    }.get(task.state, "Đang cập nhật công việc…")


_EVENT_MESSAGES = {
    "WORKER_INITIAL_IN_FLIGHT": "Codex đang làm…",
    "WORKER_TURN_COMPLETED": "Worker đã hoàn tất. Đang kiểm tra thay đổi…",
    "GITHUB_PUSH_STARTED": "Đang đồng bộ candidate lên GitHub…",
    "REVIEW_SEND_COMPLETED": "ChatGPT đang review…",
    "REVIEW_FIX_REQUIRED": "ChatGPT yêu cầu chỉnh sửa. Đã gửi lại cho Codex.",
    "EVIDENCE_REQUESTED": "Đang thu thập thông tin ChatGPT yêu cầu…",
    "TASK_PAUSED": "Đã tạm dừng theo yêu cầu của bạn.",
    "TASK_STOPPED": "Đã dừng theo yêu cầu của bạn.",
}


def chat_history(tasks, events_for):
    """Only proven milestones and Owner text; never render raw journal payloads."""
    messages = []
    for task in sorted(tasks, key=lambda t: (t.created_at, t.task_id)):
        messages.append(("Bạn", task.spec))
        last = None
        for event in events_for(task.task_id):
            message = _EVENT_MESSAGES.get(event["kind"])
            if event["kind"] == "TASK_ERROR":
                try:
                    message = friendly_error(json.loads(event["payload_json"]).get("error_code"))
                except (ValueError, TypeError):
                    message = friendly_error(None)
            if message and message != last:
                messages.append(("ReviewRelay", message))
                last = message
        final = task_message(task)
        if final != last:
            messages.append(("ReviewRelay", final))
        if task.state is S.PAUSED_OWNER:
            for text in (task.reason, task.context):
                if text:
                    messages.append(("ReviewRelay", text))
    return tuple(messages)


def recency(value, *, now=None):
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        minutes = max(0, int(((now or datetime.now(timezone.utc)) - stamp).total_seconds() // 60))
        if minutes < 1:
            return "Vừa hoạt động"
        if minutes < 60:
            return f"Hoạt động {minutes} phút trước"
        if minutes < 1440:
            return f"Hoạt động {minutes // 60} giờ trước"
        return f"Hoạt động {minutes // 1440} ngày trước"
    except (ValueError, TypeError, AttributeError):
        return "Chưa có thông tin hoạt động"
