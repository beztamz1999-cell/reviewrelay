from __future__ import annotations

import ast
import importlib.util
import json
import os
from pathlib import Path

import pytest

from reviewrelay.storage import PortableDataRoot
from test_controller import h, run
from test_project_ui import app
from reviewrelay.projects import ProjectRegistry
from dataclasses import replace
import asyncio


PACKAGING = Path(__file__).resolve().parents[1] / "packaging"
spec = importlib.util.spec_from_file_location("reviewrelay_frozen_smoke", PACKAGING / "reviewrelay_frozen_smoke.py")
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


def test_spec_is_windowed_onedir_with_official_runtime_hooks_only():
    text = (PACKAGING / "reviewrelay.spec").read_text()
    ast.parse(text)
    assert "COLLECT(exe, a.binaries, a.datas" in text
    assert "exclude_binaries=True" in text and "console=False" in text
    assert 'name="ReviewRelay"' in text
    assert 'hiddenimports=["playwright.async_api"]' in text
    assert ".local-browsers" in text
    assert "collect_all" not in text and "collect_submodules" not in text
    assert "sys.path" not in text
    assert "dist/" in (PACKAGING.parent / ".gitignore").read_text()
    assert "build/" in (PACKAGING.parent / ".gitignore").read_text()


def test_build_dependency_search_cannot_collect_foreign_shell_icu_or_crt():
    tree = ast.parse((PACKAGING / "reviewrelay.spec").read_text())
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "dependency_search_path")
    scope = {"Path": Path, "os": os}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "spec-dependency-path", "exec"), scope)
    actual = scope["dependency_search_path"]("C:/Python313", "C:/Windows").split(os.pathsep)
    assert actual == [str(Path("C:/Python313")), str(Path("C:/Python313/DLLs")),
        str(Path("C:/Windows/System32")), str(Path("C:/Windows"))]
    assert all("poppler" not in value and "libheif" not in value for value in actual)


def test_entry_uses_package_main_and_smoke_requires_explicit_opt_in():
    text = (PACKAGING / "reviewrelay_entry.py").read_text()
    assert "from reviewrelay.ui import main" in text
    assert 'if "--packaged-smoke" in sys.argv[1:]' in text
    assert "sys.path" not in text and "ui.py" not in text


def test_acceptance_refuses_unmarked_live_root_or_application_directory(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="disposable"):
        smoke.disposable_root(tmp_path)
    (tmp_path / smoke.COPY_MARKER).write_text("disposable")
    monkeypatch.setattr(smoke.sys, "executable", str(tmp_path / "ReviewRelay.exe"))
    with pytest.raises(ValueError, match="outside"):
        smoke.disposable_root(tmp_path)


def test_snapshot_is_read_only_stable_and_contains_hashes_not_reviewer_text(h):
    c, task = h.create()
    c.close()
    before = smoke.durable_snapshot(h.root)
    after = smoke.durable_snapshot(h.root)
    assert before == after
    assert set(before) >= {"projects", "tasks", "controller_tasks", "github_publications", "controller_reviews"}
    assert all(len(value) == 64 for value in before.values())


@pytest.mark.parametrize("mode", ["approval", "recovery"])
def test_disposable_packaged_ui_seam_uses_real_qt_bridge_and_recovery_wiring(app, h, tmp_path, mode):
    c, task = h.create()
    task = run(c.run(task.task_id))
    c.close()
    (h.root.path / smoke.COPY_MARKER).write_text("disposable fixture")
    config = dict(data_root=str(h.root.path), fixture_root=str(h.root.path), project_id=task.project_id,
        task_id=task.task_id, candidate_sha=task.candidate_sha, published=task.published,
        conversation_url=h.project.chatgpt_conversation_url)
    output = tmp_path / "result.json"
    assert smoke.UiProbe(config, mode, output).run() == 0
    result = json.loads(output.read_text())
    assert result["status"] == "PASS"
    assert result["codex_inference_turns"] == result["chatgpt_messages_sent"] == 0
    if mode == "approval":
        assert result["decisions"] == ["accept", "decline", "cancel", "cancel"]
        assert result["gui_thread"] and result["background_request"] and result["no_session_approval"]
    else:
        assert result["actions"] == ["Continue Same Worker", "Check Worker Completion", "Retry Reviewer Send", "Recover Reviewer Response"]
        assert result["calls"] == ["continue_incomplete_worker", "run", "reconcile_interrupted_continuation",
            "reconcile_unsent_review", "run", "reconcile_visible_review", "run"]


def test_transport_probe_cannot_dispatch_turn_or_send_review():
    tree = ast.parse((PACKAGING / "reviewrelay_frozen_smoke.py").read_text())
    probe = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "transport_probe")
    names = [node.func.attr for node in ast.walk(probe) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)]
    assert not set(names) & {"start_task", "send_instruction", "send_review_pack", "send_evidence", "goto", "add_cookies"}
    rpc = [node.args[0].value for node in ast.walk(probe) if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute) and node.func.attr == "request"]
    assert rpc == ["initialize"]


def test_packaged_owner_ui_displays_stored_worker_and_keeps_diagnostics_read_only(app, h, tmp_path):
    with ProjectRegistry(h.root) as registry:
        registry.save(replace(h.project, codex_worker_thread_id="stored-owner-worker",
            codex_worker_repo_path=str(h.repo), codex_worker_title="Owner worker", codex_worker_source="appServer",
            codex_worker_verified_at="fixture"), event="PROJECT_WORKER_BOUND")
    (h.root.path / smoke.COPY_MARKER).write_text("disposable fixture")
    config = dict(data_root=str(h.root.path), project_id=h.project.project_id)
    output = tmp_path / "owner.json"
    assert smoke.UiProbe(config, "owner", output).run() == 0
    result = json.loads(output.read_text())
    assert result["status"] == "PASS" and result["stored_worker_displayed"]
    assert result["prompt_only"] and result["diagnostics_hidden_by_default"] and result["diagnostics_available"]
    assert result["durable_unchanged"] and result["codex_inference_turns"] == result["chatgpt_messages_sent"] == 0


def test_chatgpt_probe_evaluates_only_read_only_webdriver_and_cannot_send_or_navigate():
    tree = ast.parse((PACKAGING / "reviewrelay_frozen_smoke.py").read_text())
    probe = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "chatgpt_cdp_probe")
    calls = [n for n in ast.walk(probe) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
    assert not {n.func.attr for n in calls} & {"goto", "reload", "add_cookies", "send_review_pack",
        "send_evidence", "send_instruction", "start_task", "add_init_script", "request"}
    evaluations = [n.args[0].value for n in calls if n.func.attr == "evaluate"]
    assert evaluations == ["navigator.webdriver"]


@pytest.mark.parametrize("webdriver", [False, True])
def test_disposable_chatgpt_probe_records_only_boolean_and_never_dispatches(tmp_path, monkeypatch, webdriver):
    from types import SimpleNamespace
    root = PortableDataRoot(tmp_path/'data').create()
    (root.path / smoke.COPY_MARKER).write_text("disposable")
    url = "https://chatgpt.com/c/packaged-read-only"
    with ProjectRegistry(root) as registry:
        project = registry.create("Probe", str(tmp_path/'repo'), "EXISTING")
        registry.save(replace(project, chatgpt_conversation_url=url,
            reviewer_settings={"browser_backend":"google-chrome-cdp", "browser_profile":"dedicated"}))
    expressions, opened, closed = [], [], []
    class Page:
        def __init__(self): self.url=url
        def is_closed(self): return False
        async def evaluate(self, expression):
            expressions.append(expression)
            return webdriver
        async def title(self): return "ChatGPT"
        def locator(self, selector):
            async def count(): return 0
            return SimpleNamespace(count=count)
    class Reviewer:
        def __init__(self,*args):
            self.page=Page(); self._cdp_port=54321; self._chrome_process=object()
            self._context=SimpleNamespace(pages=[self.page]); self.browser_version="Chrome-fixture"
        async def start(self): pass
        async def close(self): closed.append(True)
        def _same_conversation(self, actual,target): return actual==target
        async def open_task_conversation(self,target): opened.append(target)
        async def _find_composer(self): return object()
    monkeypatch.setattr(smoke,"ChatGPTWebAdapter",Reviewer)
    monkeypatch.setattr(smoke,"chrome_listener_ready",lambda *args:True)
    result = asyncio.run(smoke.chatgpt_cdp_probe(dict(data_root=str(root.path),project_id=project.project_id,
        browser_data_root=str(tmp_path/'same-dedicated-browser-root'), acceptance_seconds=10)))
    assert result["navigator_webdriver"] is webdriver
    assert result["status"] == ("PASS" if webdriver is False else "FAIL")
    assert result["nonzero_cdp_port"] and result["localhost_only"] and result["restored_tab"]
    assert result["cloudflare_loop"] == "NO"
    assert expressions==["navigator.webdriver"] and opened==[url] and closed==[True]
    assert result["codex_inference_turns"] == result["chatgpt_messages_sent"] == 0


def test_packaged_main_reuses_explicit_harness_root_without_a_data_root_dialog(app, h, tmp_path):
    c, task = h.create()
    task = run(c.run(task.task_id))
    c.close()
    (h.root.path / smoke.COPY_MARKER).write_text("disposable fixture")
    config = dict(data_root=str(h.root.path), project_id=task.project_id, task_id=task.task_id)
    output = tmp_path / "ui.json"
    assert smoke.UiProbe(config, "ui", output).run() == 0
    result = json.loads(output.read_text())
    assert result["status"] == "PASS"
    assert Path(result["data_root"]) == h.root.path
    assert result["snapshot"] == result["snapshot_after"]
    assert result["recovery_hidden"]
    assert result["data_root_dialog"] == "ABSENT"
    assert result["advanced_setup_available"] and result["project_worker_card"]
