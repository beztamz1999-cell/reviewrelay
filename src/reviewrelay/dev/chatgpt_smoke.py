"""Explicit manual ChatGPT UI smoke; never invoked by automated tests."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys

from reviewrelay.config import validate_identifier
from reviewrelay.reviewer import BrowserBackend, ChatGPTWebAdapter, ChatGPTWebSettings, LoginRequired
from reviewrelay.reviewer.chrome_cdp import ChromeMode, ReviewRelayProfileLock, find_google_chrome, launch_chrome, wait_for_chrome_exit
from reviewrelay.storage import PortableDataRoot


SMOKE_PROMPT = "ReviewRelay transport smoke test.\nReply with the exact marker:\nREVIEWRELAY_SMOKE_OK"
EXPECTED_MARKER = "REVIEWRELAY_SMOKE_OK"


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manually verify ReviewRelay ChatGPT browser transport.")
    parser.add_argument("--data-root", required=True, help="Explicit portable ReviewRelay data root")
    parser.add_argument("--conversation-url", required=True, help="Existing ChatGPT conversation URL")
    parser.add_argument("--base-url", default="https://chatgpt.com/")
    parser.add_argument("--browser-profile", default="reviewer-chrome")
    parser.add_argument("--project-id", default="relay-smoke")
    parser.add_argument("--task-id", default="transport-smoke")
    parser.add_argument(
        "--automation-only",
        action="store_true",
        help="Reuse the previously authenticated dedicated ReviewRelay profile without reopening Auth Mode",
    )
    parser.add_argument("--send", action="store_true", help="Required explicit acknowledgement to send one harmless test prompt")
    args = parser.parse_args(argv)
    if not args.send:
        parser.error("--send is required; this command sends one harmless message to the configured conversation")
    return args


def _settings_from_args(args: argparse.Namespace) -> ChatGPTWebSettings:
    """Live smoke uses installed Chrome; offline adapter users keep Chromium by default."""
    return ChatGPTWebSettings(
        base_url=args.base_url,
        browser_profile=args.browser_profile,
        conversation_url=args.conversation_url,
        headless=False,
        browser_backend=BrowserBackend.GOOGLE_CHROME_CDP,
    )


async def _run(args: argparse.Namespace) -> int:
    project_id = validate_identifier(args.project_id, "project_id")
    task_id = validate_identifier(args.task_id, "task_id")
    root = PortableDataRoot(args.data_root).create()
    settings = _settings_from_args(args)
    conversation_url = settings.resolve_conversation_url()
    profile_lock = ReviewRelayProfileLock(root, args.browser_profile)
    profile_lock.acquire()
    adapter: ChatGPTWebAdapter | None = None
    auth_process = None
    try:
        upload_dir = root.safe_path(Path("active") / project_id / task_id / "scratch" / "upload")
        upload_dir.mkdir(parents=True, exist_ok=True)
        smoke_file = upload_dir / "relay-smoke.txt"
        root.assert_managed_path(smoke_file)
        smoke_file.write_text(
            "Harmless ReviewRelay transport smoke fixture. No project or source data.\n",
            encoding="utf-8",
        )

        if args.automation_only:
            print("AUTH_MODE=skipped; reusing the dedicated ReviewRelay profile as previously authenticated")
        else:
            auth_process = launch_chrome(
                find_google_chrome(),
                profile_lock.profile_path,
                mode=ChromeMode.AUTH,
                conversation_url=conversation_url,
            )
            print("AUTH_MODE=normal-google-chrome; playwright=OFF; cdp=OFF")
            print(f"REVIEWRELAY_PROFILE={args.browser_profile}")
            print("Sign in manually and confirm the existing reviewer conversation is visible.")
            input("Close the ReviewRelay Auth Mode Chrome window, then notify me to continue: ")
            while not await wait_for_chrome_exit(auth_process, timeout_seconds=0.25):
                print("Auth Mode Chrome still holds the profile. Close all its windows before continuing.")
                input("After Chrome has fully exited, press Enter to continue: ")
            print("AUTH_MODE_CHROME_CLOSED=YES")

        adapter = ChatGPTWebAdapter(
            root,
            settings,
            project_id=project_id,
            task_id=task_id,
            profile_lock=profile_lock,
        )
        await adapter.start()
        print("AUTOMATION_MODE=google-chrome-cdp")
        print(f"CHROME_VERSION={adapter.browser_version or 'UNKNOWN'}")
        try:
            await adapter.open_task_conversation()
        except LoginRequired:
            print("AUTOMATION_MODE_LOGIN_REQUIRED=YES")
            print("MESSAGE_SENT_ONCE=NO")
            print("No authentication attempt was made in Automation Mode.")
            return 3
        print("Sending the explicit harmless ReviewRelay transport smoke prompt.")
        try:
            sent = await adapter.send_review_pack(
                prompt=SMOKE_PROMPT,
                review_key="reviewrelay-live-smoke-v1",
                attachment_paths=[smoke_file],
            )
        except LoginRequired:
            print("AUTOMATION_MODE_LOGIN_REQUIRED=YES")
            print("MESSAGE_SENT_ONCE=NO")
            print("No authentication attempt was made in Automation Mode.")
            return 3
        print("MESSAGE_SENT_ONCE=YES")
        response = await adapter.wait_response(sent)
        if response.review_key != sent.review_key or response.assistant_turn_identity in sent.pre_send_baseline.assistant_turn_ids:
            raise RuntimeError("The captured response was not owned by this smoke request")
        marker_found = EXPECTED_MARKER in response.text
        print("OWNED_RESPONSE_DETECTED=YES")
        print("RESPONSE_COMPLETION=YES")
        print("RAW_RESPONSE_CAPTURED=YES")
        print(f"EXPECTED_MARKER_FOUND={'YES' if marker_found else 'NO'}")
        print("\n--- Raw ChatGPT response (transport only) ---\n")
        print(response.text)
        return 0 if marker_found else 2
    finally:
        try:
            if adapter is not None:
                await adapter.close()
        finally:
            if auth_process is not None and auth_process.poll() is None:
                print("Closing the remaining ReviewRelay Auth Mode Chrome session.")
                if not await wait_for_chrome_exit(auth_process, timeout_seconds=3):
                    print("Close its visible Chrome window manually; the profile remains locked until it exits.")
                    await wait_for_chrome_exit(auth_process)
            profile_lock.release()


def main(argv: list[str] | None = None) -> int:
    args = _arguments(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
