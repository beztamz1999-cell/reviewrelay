from __future__ import annotations

import pytest

from reviewrelay.config import ProjectConfig
from reviewrelay.errors import ConfigError
from reviewrelay.storage import PortableDataRoot


SAMPLE = r"""
project_id: gacha-v4
repo:
  path: "G:\\TEST WORKBUDDY AI\\gacha-infinity"
  strict_commit_mode: true
  require_clean_baseline: true
review:
  max_fix_cycles: 3
  max_evidence_cycles: 5
  full_changed_files_limit: 12
  keep_final_patch: true
tests:
  unit:
    command: dotnet test tests/Game.Tests
security:
  block_secrets: true
  block_env_files: true
storage:
  completed_retention_days: 30
  failed_retention_days: 14
  aborted_retention_days: 7
  max_total_size_gb: 5
"""


def test_parses_flat_project_config_and_test_commands() -> None:
    config = ProjectConfig.from_yaml(SAMPLE)
    assert config.project_id == "gacha-v4"
    assert config.repo.path == r"G:\TEST WORKBUDDY AI\gacha-infinity"
    assert config.review.full_changed_files_limit == 12
    assert config.tests["unit"] == "dotnet test tests/Game.Tests"
    assert config.storage.completed_retention_days == 30


def test_parses_canonical_nested_storage_and_optional_phase_configuration() -> None:
    config = ProjectConfig.from_yaml("""
project_id: sample
repo: {path: C:/repo}
storage:
  completed: {compact_immediately: true, retention_days: 21}
  failed: {retention_days: 8}
  aborted: {retention_days: 2}
  upload_staging: {delete_after_send: false}
  max_total_size_gb: 4
chatgpt: {conversation_scope: task, browser_profile: default}
worker: {adapter: codex, reuse_session: true}
""")
    assert config.storage.completed_retention_days == 21
    assert config.storage.failed_retention_days == 8
    assert config.storage.delete_upload_staging_after_send is False
    assert config.worker["reuse_session"] is True


@pytest.mark.parametrize("text", [
    "project_id: x\n",
    "project_id: x\nrepo: {path: ''}\n",
    "project_id: x\nrepo: {path: C:/repo}\nreview: {max_fix_cycles: -1}\n",
    "project_id: x\nrepo: {path: C:/repo}\nsecurity: {block_secrets: yesplease}\n",
    "project_id: x\nrepo: {path: C:/repo}\nwat: true\n",
    "project_id: x\nproject_id: y\nrepo: {path: C:/repo}\n",
])
def test_rejects_invalid_or_ambiguous_config(text: str) -> None:
    with pytest.raises(ConfigError):
        ProjectConfig.from_yaml(text)


def test_stores_validated_config_inside_portable_data_root(tmp_path) -> None:
    config = ProjectConfig.from_yaml(SAMPLE)
    root = PortableDataRoot(tmp_path / "portable").create()
    stored = root.store_project_config(config)
    assert stored == root.path / "config" / "projects" / "gacha-v4.yaml"
    assert ProjectConfig.load(stored) == config
