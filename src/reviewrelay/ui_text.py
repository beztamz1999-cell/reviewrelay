"""Vietnamese presentation only; durable values and protocol stay unchanged."""

STATE_TEXT = {
    "READY": "Sẵn sàng", "PROJECT_READY": "Sẵn sàng", "SETUP_REQUIRED": "Chưa thiết lập",
    "PENDING": "Chưa thiết lập", "NOT_CONFIGURED": "Chưa thiết lập",
    "ERROR": "Có lỗi", "FAILED": "Có lỗi", "DRAFT": "Bản nháp",
    "WORKER_RUNNING": "Codex đang làm", "VERIFYING_CANDIDATE": "Đang kiểm tra kết quả",
    "PUBLISHING": "Đang đồng bộ GitHub", "WAITING_REVIEW": "Đang chờ ChatGPT review",
    "PROCESSING_REVIEW": "Đang xử lý review", "COLLECTING_EVIDENCE": "Đang kiểm chứng local",
    "SENDING_EVIDENCE": "Đang gửi kết quả kiểm chứng", "PAUSED_OWNER": "Đang chờ Owner",
    "PAUSED_OWNER_STEER": "Owner đang điều khiển", "PAUSED_ERROR": "Tạm dừng do lỗi",
    "COMPLETE": "Hoàn tất", "STOPPED": "Đã dừng", "IDLE": "Chưa làm việc",
    "WORKING": "Đang làm việc", "PAUSE_PENDING": "Đang chờ điểm dừng an toàn",
    "IN_PROGRESS": "Đang thực hiện", "COMPLETED": "Đã hoàn thành",
    "INTERRUPTED": "Đã gián đoạn", "PUBLISHED": "Đã đồng bộ", "VERIFIED": "Đã kiểm tra",
    "PAUSED_USER": "Đã tạm dừng", "NEEDS_OWNER": "Cần Owner xử lý",
    "SETTING_UP": "Đang thiết lập",
    "READY_TO_NOTIFY_REVIEWER": "Sẵn sàng gửi Reviewer", "LOCAL_CANDIDATE_READY": "Kết quả local đã sẵn sàng",
    "GITHUB_PUSH_PLANNED": "Đang chờ đồng bộ", "GITHUB_PUSH_IN_FLIGHT": "Đang đồng bộ",
    "GITHUB_PUSH_CONFIRMED": "Đã đồng bộ", "REMOTE_SHA_VERIFIED": "Đã xác nhận SHA trên GitHub",
    "PR_CREATED": "Đã tạo PR", "PR_REUSED": "Đang dùng PR đã có",
    "MATCHING": "Lịch sử khớp nhau", "LOCAL_AHEAD": "Local có commit mới hơn",
    "REMOTE_AHEAD": "GitHub có commit mới hơn", "REMOTE_EMPTY": "GitHub chưa có commit",
}

RECOVERY_TEXT = {
    "Continue Same Worker": "Tiếp tục Worker hiện tại",
    "Check Worker Completion": "Kiểm tra Worker",
    "Retry Reviewer Send": "Gửi lại cho Reviewer",
    "Recover Reviewer Response": "Khôi phục phản hồi Reviewer",
}


def state_text(value):
    value = getattr(value, "value", value)
    return STATE_TEXT.get(value, value)


def recovery_text(action):
    return RECOVERY_TEXT[action.value]


def detail_text(value):
    # Translate only a known application explanation; Owner/reviewer text is verbatim.
    if value == "Resolve the indicated condition before resuming; dispatched effects are not retried blindly.":
        return "Xử lý điều kiện được chỉ ra trước khi tiếp tục; không tự ý lặp lại thao tác đã bắt đầu."
    return value
