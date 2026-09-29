"""Explicit manual ChatGPT UI smoke; never invoked by automated tests."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys

from reviewrelay.config import validate_identifier
from reviewrelay.reviewer import ChatGPTWebAdapter, ChatGPTWebSettings, LoginRequired
from reviewrelay.storage import PortableDataRoot


SMOKE_PROMPT = "ReviewRelay transport smoke test.\nReply with the exact marker:\nREVIEWRELAY_SMOKE_OK"


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manually verify ReviewRelay ChatGPT browser transport.")
    parser.add_argument("--data-root", required=True, help="Explicit portable ReviewRelay data root")
    parser.add_argument("--conversation-url", required=True, help="Existing ChatGPT conversation URL")
    parser.add_argument("--base-url", default="https://chatgpt.com/")
    parser.add_argument("--browser-profile", default="default")
    parser.add_argument("--project-id", default="relay-smoke")
    parser.add_argument("--task-id", default="transport-smoke")
    parser.add_argument("--send", action="store_true", help="Required explicit acknowledgement to send one harmless test prompt")
    args = parser.parse_args(argv)
    if not args.send:
        parser.error("--send is required; this command sends one harmless message to the configured conversation")
    return args


async def _run(args: argparse.Namespace) -> int:
    project_id = validate_identifier(args.project_id, "project_id")
    task_id = validate_identifier(args.task_id, "task_id")
    root = PortableDataRoot(args.data_root).create()
    upload_dir = root.safe_path(Path("active") / project_id / task_id / "scratch" / "upload")
    upload_dir.mkdir(parents=True, exist_ok=True)
    smoke_file = upload_dir / "relay-smoke.txt"
    root.assert_managed_path(smoke_file)
    smoke_file.write_text(
        "Harmless ReviewRelay transport smoke fixture. No project or source data.\n",
        encoding="utf-8",
    )
    settings = ChatGPTWebSettings(
        base_url=args.base_url,
        browser_profile=args.browser_profile,
        conversation_url=args.conversation_url,
        headless=False,
    )
    adapter = ChatGPTWebAdapter(root, settings, project_id=project_id, task_id=task_id)
    try:
        try:
            await adapter.open_task_conversation()
        except LoginRequired:
            print("Log in manually in the opened ReviewRelay browser. Do not enter credentials into ReviewRelay.")
            input("After the conversation is available, press Enter to continue: ")
            await adapter.open_task_conversation()
        print("Sending the explicit harmless ReviewRelay transport smoke prompt.")
        sent = await adapter.send_review_pack(
            prompt=SMOKE_PROMPT,
            review_key="reviewrelay-live-smoke-v1",
            attachment_paths=[smoke_file],
        )
        response = await adapter.wait_response(sent)
        print("\n--- Raw ChatGPT response (transport only) ---\n")
        print(response.text)
        return 0
    finally:
        await adapter.close()


def main(argv: list[str] | None = None) -> int:
    args = _arguments(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
