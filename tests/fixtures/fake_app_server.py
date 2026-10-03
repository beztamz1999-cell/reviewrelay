"""Deterministic stdio child, exercising the public v2 shapes without model calls."""

import json
import os
import sys
import time
from pathlib import Path

state_path, mode = Path(sys.argv[1]), sys.argv[2]
requests_path = state_path.with_suffix(".requests.jsonl")
initialized = False
current_thread = None
current_turn = None


def send(value):
    print(json.dumps(value), flush=True)


def event(method, params):
    send({"method": method, "params": params})


def result(request, value):
    send({"id": request["id"] + 100 if mode == "wrong-id" else request["id"], "result": value})


def save(thread):
    state_path.write_text(json.dumps(thread), encoding="utf-8")


def complete(status="completed"):
    global current_thread
    final = {"id": "agent-final", "type": "agentMessage", "phase": "final_answer",
             "text": "RELAY_WORKER_DONE\nfixture final" if mode == "failed" else "fixture final without marker"}
    p = {"threadId": current_thread["id"], "turnId": current_turn["id"]}
    event("item/agentMessage/delta", p | {"itemId": "agent-final", "delta": final["text"]})
    event("item/completed", p | {"completedAtMs": 1, "item": final})
    turn = current_turn | {"status": status, "items": [final]}
    current_thread["turns"] = [turn]
    save(current_thread)
    event("turn/completed", {"threadId": current_thread["id"], "turn": turn})


print("diagnostic is not JSON; access_token=fixture-secret", file=sys.stderr, flush=True)
for line in sys.stdin:
    request = json.loads(line)
    with requests_path.open("a", encoding="utf-8") as log:
        log.write(json.dumps(request) + "\n")
    method, params = request.get("method"), request.get("params", {})
    if method == "initialize":
        if mode == "init-timeout":
            continue
        if mode == "init-dies":
            os._exit(7)
        if mode == "init-garbage":
            print("not json", flush=True)
            continue
        if mode == "init-error":
            send({"id": request["id"], "error": {"code": -32600, "message": "Rejected"}})
            continue
        result(request, {"userAgent": "" if mode == "init-invalid" else "fixture/1", "codexHome": str(state_path.parent),
                         "platformFamily": "windows", "platformOs": "windows"})
    elif method == "initialized":
        initialized = True
    elif method in {"thread/start", "thread/resume"}:
        assert initialized
        if mode == "slow-start":
            time.sleep(.3)
        if method == "thread/resume" and mode == "resume-fail":
            send({"id": request["id"], "error": {"code": -32000, "message": "No such thread"}})
            continue
        if method == "thread/start":
            current_thread = {"id": "fixture-thread", "cwd": params["cwd"], "turns": [],
                "source": "appServer", "ephemeral": False, "name": "Fixture worker", "updatedAt": 1, "status": {"type": "idle"}}
            save(current_thread)
        else:
            current_thread = json.loads(state_path.read_text(encoding="utf-8"))
            assert params["threadId"] == current_thread["id"]
        returned = current_thread.copy()
        if mode == "thread-cwd":
            returned["cwd"] = str(state_path.parent)
        if mode == "resume-other-id" and method == "thread/resume":
            returned["id"] = "wrong-thread"
        result(request, {"thread": returned, "cwd": returned["cwd"], "model": "fixture-model"})
    elif method == "turn/start":
        assert current_thread["id"] == params["threadId"]
        assert params["cwd"] == current_thread["cwd"]
        assert params["input"][0]["type"] == "text"
        current_turn = {"id": f"fixture-turn-{len(current_thread['turns']) + 1}", "status": "inProgress", "items": []}
        current_thread["turns"] = [current_turn]
        save(current_thread)
        if mode != "events-before-response":
            result(request, {"turn": current_turn})
        event("turn/started", {"threadId": current_thread["id"], "turn": current_turn})
        if mode == "turn-dies":
            os._exit(9)
        if mode == "turn-garbage":
            print("garbage", flush=True)
            continue
        p = {"threadId": current_thread["id"], "turnId": current_turn["id"]}
        if mode == "malformed-event":
            event("item/agentMessage/delta", p | {"itemId": "bad", "delta": 42})
            continue
        if mode == "interactive":
            send({"id": "request-approval", "method": "item/commandExecution/requestApproval", "params": p})
            continue
        if mode.startswith("approval"):
            send({"id": "request-approval", "method": "item/commandExecution/requestApproval", "params": p | {
                "threadId": "unrelated" if mode == "approval-foreign" else current_thread["id"],
                "environmentId": "remote-host" if mode == "approval-remote" else "local",
                "itemId": "approved-command", "command": "git status --short", "cwd": current_thread["cwd"]}})
            continue
        if mode == "auth":
            send({"id": "request-auth", "method": "account/chatgptAuthTokens/refresh", "params": {}})
            continue
        if mode in {"hold", "idle", "hang-close"}:
            continue
        if mode == "foreign-events":
            event("turn/completed", {"threadId": "unrelated", "turn": {"id": current_turn["id"], "status": "completed", "items": []}})
            event("turn/completed", {"threadId": current_thread["id"], "turn": {"id": "unrelated-turn", "status": "completed", "items": []}})
        event("future/valid", p | {"access_token": "secret-test", "password": "secret-test", "value": "retained"})
        event("item/reasoning/textDelta", p | {"delta": "hidden-test"})
        event("codex/event/agent_reasoning", {"text": "hidden-test"})
        commentary = {"id": "commentary", "type": "agentMessage", "phase": "commentary", "text": "Working now"}
        event("item/completed", p | {"item": commentary, "completedAtMs": 1})
        command = {"id": "command", "type": "commandExecution", "command": "echo fixture", "exitCode": None}
        event("item/started", p | {"item": command})
        event("item/completed", p | {"item": command | {"exitCode": 0}, "completedAtMs": 1})
        event("item/completed", p | {"item": {"id": "edit", "type": "fileChange", "changes": [{"path": "fixture.txt"}]}, "completedAtMs": 1})
        event("item/completed", p | {"item": {"id": "tool", "type": "mcpToolCall"}, "completedAtMs": 1})
        complete("failed" if mode == "failed" else "interrupted" if mode == "interrupted" else "completed")
        if mode == "events-before-response":
            result(request, {"turn": current_turn})
    elif method == "turn/interrupt":
        result(request, {})
        complete("interrupted")
    elif method == "thread/read":
        result(request, {"thread": json.loads(state_path.read_text(encoding="utf-8"))})
    elif method == "thread/list":
        result(request, {"data": [json.loads(state_path.read_text(encoding="utf-8"))] if state_path.exists() else [], "nextCursor": None})
    elif request.get("id") == "request-approval" and "result" in request:
        complete("completed" if request["result"]["decision"] == "accept" else "interrupted")
if mode == "hang-close":
    time.sleep(60)
