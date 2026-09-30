"""Bounded diagnostics with known credentials and reasoning payloads removed."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def redact_text(text: str) -> str:
    text = re.sub(r"(?i)\bBearer\s+\S+", "Bearer [REDACTED]", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[REDACTED]", text)
    return re.sub(r"(?i)\b(access[_-]?token|refresh[_-]?token|api[_-]?key|password|authorization|cookie)[\"']?\s*[:=]\s*[\"']?[^\s,;\"']+",
                  r"\1=[REDACTED]", text)


def safe_payload(value: Any) -> Any:
    if isinstance(value, dict):
        if value.get("type") == "reasoning":
            return {"type": "reasoning", "id": value.get("id"), "content": "[OMITTED]"}
        result = {}
        for key, item in value.items():
            canonical = key.lower().replace("_", "").replace("-", "")
            if any(part in canonical for part in ("token", "password", "secret", "authorization", "cookie", "apikey")):
                result[key] = "[REDACTED]"
            elif any(part in canonical for part in ("reasoning", "encryptedcontent")):
                result[key] = "[OMITTED]"
            else:
                result[key] = safe_payload(item)
        return result
    if isinstance(value, list):
        return [safe_payload(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


class BoundedTrace:
    def __init__(self, path: Path, limit: int) -> None:
        self.path = path
        self.limit = limit
        self.bytes_written = 0
        self.truncated = False
        path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = path.open("xb")

    def append(self, payload: dict[str, Any]) -> None:
        if self.truncated or self._stream.closed:
            return
        line = (json.dumps(safe_payload(payload), ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        if self.bytes_written + len(line) > self.limit:
            self.truncated = True
            return
        self._stream.write(line)
        self._stream.flush()
        self.bytes_written += len(line)

    def close(self) -> None:
        self._stream.close()
