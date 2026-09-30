"""Project configuration parsing, validation, and portable persistence."""

from __future__ import annotations

import os
import math
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigError, PathSafetyError


_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def validate_identifier(value: str, label: str = "identifier") -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value) or value in {".", ".."}:
        raise PathSafetyError(f"Invalid {label}: {value!r}")
    return value


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(loader: _UniqueKeyLoader, node: yaml.nodes.MappingNode, deep: bool = False) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            if key in mapping:
                raise ConfigError(f"Duplicate YAML key: {key!r}")
        except TypeError as exc:
            raise ConfigError("YAML mapping keys must be scalar and hashable") from exc
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)


@dataclass(frozen=True)
class RepoConfig:
    path: str
    strict_commit_mode: bool = True
    require_clean_baseline: bool = True


@dataclass(frozen=True)
class ReviewConfig:
    max_fix_cycles: int = 3
    max_evidence_cycles: int = 5
    full_changed_files_limit: int = 12
    keep_final_patch: bool = True


@dataclass(frozen=True)
class SecurityConfig:
    block_secrets: bool = True
    block_env_files: bool = True


@dataclass(frozen=True)
class StorageConfig:
    completed_retention_days: int = 30
    failed_retention_days: int = 14
    aborted_retention_days: int = 7
    max_total_size_gb: float = 5.0
    compact_completed_immediately: bool = True
    delete_upload_staging_after_send: bool = True


@dataclass(frozen=True)
class ProjectConfig:
    project_id: str
    repo: RepoConfig
    review: ReviewConfig = field(default_factory=ReviewConfig)
    tests: dict[str, str | tuple[str, ...]] = field(default_factory=dict)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    chatgpt: dict[str, Any] = field(default_factory=dict)
    worker: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_yaml(cls, text: str) -> "ProjectConfig":
        try:
            raw = yaml.load(text, Loader=_UniqueKeyLoader)
        except ConfigError:
            raise
        except yaml.YAMLError as exc:
            raise ConfigError(f"Invalid project YAML: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError("Project YAML must contain a mapping at its root")
        _keys(raw, {"project_id", "repo", "review", "tests", "security", "storage", "chatgpt", "worker"}, "root")

        project_id = _required_string(raw, "project_id", "root")
        validate_identifier(project_id, "project_id")

        repo_raw = _mapping(raw.get("repo"), "repo", required=True)
        _keys(repo_raw, {"path", "strict_commit_mode", "require_clean_baseline"}, "repo")
        repo_path = _required_string(repo_raw, "path", "repo")
        repo = RepoConfig(
            path=repo_path,
            strict_commit_mode=_bool(repo_raw, "strict_commit_mode", True, "repo"),
            require_clean_baseline=_bool(repo_raw, "require_clean_baseline", True, "repo"),
        )

        review_raw = _mapping(raw.get("review", {}), "review")
        _keys(review_raw, {"max_fix_cycles", "max_evidence_cycles", "full_changed_files_limit", "keep_final_patch"}, "review")
        review = ReviewConfig(
            max_fix_cycles=_nonnegative_int(review_raw, "max_fix_cycles", 3, "review"),
            max_evidence_cycles=_nonnegative_int(review_raw, "max_evidence_cycles", 5, "review"),
            full_changed_files_limit=_nonnegative_int(review_raw, "full_changed_files_limit", 12, "review"),
            keep_final_patch=_bool(review_raw, "keep_final_patch", True, "review"),
        )

        tests_raw = _mapping(raw.get("tests", {}), "tests")
        tests: dict[str, str | tuple[str, ...]] = {}
        for test_id, test_value in tests_raw.items():
            if not isinstance(test_id, str) or not _ID_RE.fullmatch(test_id):
                raise ConfigError(f"Invalid test registry id: {test_id!r}")
            if isinstance(test_value, dict):
                _keys(test_value, {"command", "argv"}, f"tests.{test_id}")
                if "argv" in test_value:
                    if "command" in test_value:
                        raise ConfigError(f"tests.{test_id} cannot combine argv and command")
                    argv = test_value["argv"]
                    if not isinstance(argv, list) or not argv or any(
                        not isinstance(arg, str) or "\x00" in arg for arg in argv
                    ) or not argv[0].strip():
                        raise ConfigError(f"tests.{test_id}.argv must be a non-empty string array")
                    tests[test_id] = tuple(argv)
                    continue
                command = test_value.get("command")
            else:
                command = test_value
            if not isinstance(command, str) or not command.strip() or "\x00" in command:
                raise ConfigError(f"tests.{test_id} must be a non-empty command string")
            tests[test_id] = command

        security_raw = _mapping(raw.get("security", {}), "security")
        _keys(security_raw, {"block_secrets", "block_env_files"}, "security")
        security = SecurityConfig(
            block_secrets=_bool(security_raw, "block_secrets", True, "security"),
            block_env_files=_bool(security_raw, "block_env_files", True, "security"),
        )

        storage_raw = _mapping(raw.get("storage", {}), "storage")
        _keys(storage_raw, {
            "completed_retention_days", "failed_retention_days", "aborted_retention_days",
            "max_total_size_gb", "completed", "failed", "aborted", "upload_staging",
        }, "storage")
        completed_raw = _mapping(storage_raw.get("completed", {}), "storage.completed")
        failed_raw = _mapping(storage_raw.get("failed", {}), "storage.failed")
        aborted_raw = _mapping(storage_raw.get("aborted", {}), "storage.aborted")
        staging_raw = _mapping(storage_raw.get("upload_staging", {}), "storage.upload_staging")
        _keys(completed_raw, {"retention_days", "compact_immediately"}, "storage.completed")
        _keys(failed_raw, {"retention_days"}, "storage.failed")
        _keys(aborted_raw, {"retention_days"}, "storage.aborted")
        _keys(staging_raw, {"delete_after_send"}, "storage.upload_staging")
        storage = StorageConfig(
            completed_retention_days=_nested_nonnegative(storage_raw, completed_raw, "completed_retention_days", "retention_days", 30, "storage.completed"),
            failed_retention_days=_nested_nonnegative(storage_raw, failed_raw, "failed_retention_days", "retention_days", 14, "storage.failed"),
            aborted_retention_days=_nested_nonnegative(storage_raw, aborted_raw, "aborted_retention_days", "retention_days", 7, "storage.aborted"),
            max_total_size_gb=_positive_number(storage_raw, "max_total_size_gb", 5.0, "storage", allow_zero=False),
            compact_completed_immediately=_bool(completed_raw, "compact_immediately", True, "storage.completed"),
            delete_upload_staging_after_send=_bool(staging_raw, "delete_after_send", True, "storage.upload_staging"),
        )

        chatgpt = _mapping(raw.get("chatgpt", {}), "chatgpt")
        worker = _mapping(raw.get("worker", {}), "worker")
        return cls(project_id, repo, review, tests, security, storage, chatgpt, worker)

    @classmethod
    def load(cls, path: str | Path) -> "ProjectConfig":
        try:
            return cls.from_yaml(Path(path).read_text(encoding="utf-8"))
        except OSError as exc:
            raise ConfigError(f"Could not read project config {path}: {exc}") from exc

    def to_mapping(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "project_id": self.project_id,
            "repo": {
                "path": self.repo.path,
                "strict_commit_mode": self.repo.strict_commit_mode,
                "require_clean_baseline": self.repo.require_clean_baseline,
            },
            "review": {
                "max_fix_cycles": self.review.max_fix_cycles,
                "max_evidence_cycles": self.review.max_evidence_cycles,
                "full_changed_files_limit": self.review.full_changed_files_limit,
                "keep_final_patch": self.review.keep_final_patch,
            },
            "tests": {
                test_id: ({"command": command} if isinstance(command, str) else {"argv": list(command)})
                for test_id, command in sorted(self.tests.items())
            },
            "security": {
                "block_secrets": self.security.block_secrets,
                "block_env_files": self.security.block_env_files,
            },
            "storage": {
                "completed": {
                    "compact_immediately": self.storage.compact_completed_immediately,
                    "retention_days": self.storage.completed_retention_days,
                },
                "failed": {"retention_days": self.storage.failed_retention_days},
                "aborted": {"retention_days": self.storage.aborted_retention_days},
                "upload_staging": {"delete_after_send": self.storage.delete_upload_staging_after_send},
                "max_total_size_gb": self.storage.max_total_size_gb,
            },
        }
        if self.chatgpt:
            result["chatgpt"] = self.chatgpt
        if self.worker:
            result["worker"] = self.worker
        return result


def store_project_config(config: ProjectConfig, data_root: Any) -> Path:
    """Persist validated YAML under the portable data root's config/projects folder."""
    validate_identifier(config.project_id, "project_id")
    root = data_root.safe_path(Path("config") / "projects" / f"{config.project_id}.yaml")
    root.parent.mkdir(parents=True, exist_ok=True)
    data_root.assert_managed_path(root)
    rendered = yaml.safe_dump(config.to_mapping(), sort_keys=False, allow_unicode=True, default_flow_style=False)
    _atomic_write_text(root, rendered)
    return root


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _mapping(value: Any, label: str, required: bool = False) -> dict[str, Any]:
    if value is None and not required:
        return {}
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ConfigError(f"{label} must be a mapping")
    return value


def _keys(mapping: dict[str, Any], allowed: set[str], label: str) -> None:
    extra = sorted(set(mapping) - allowed)
    if extra:
        raise ConfigError(f"Unknown key(s) in {label}: {', '.join(extra)}")


def _required_string(mapping: dict[str, Any], key: str, label: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ConfigError(f"{label}.{key} must be a non-empty string")
    return value


def _bool(mapping: dict[str, Any], key: str, default: bool, label: str) -> bool:
    value = mapping.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{label}.{key} must be a boolean")
    return value


def _nonnegative_int(mapping: dict[str, Any], key: str, default: int, label: str) -> int:
    value = mapping.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigError(f"{label}.{key} must be a non-negative integer")
    return value


def _positive_number(mapping: dict[str, Any], key: str, default: float, label: str, allow_zero: bool) -> float:
    value = mapping.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{label}.{key} must be a number")
    if not math.isfinite(float(value)) or value < 0 or (not allow_zero and value == 0):
        raise ConfigError(f"{label}.{key} must be {'non-negative' if allow_zero else 'positive'}")
    return float(value)


def _nested_nonnegative(
    parent: dict[str, Any], nested: dict[str, Any], flat_key: str, nested_key: str, default: int, label: str
) -> int:
    if flat_key in parent and nested_key in nested:
        raise ConfigError(f"Configure either storage.{flat_key} or {label}.{nested_key}, not both")
    if flat_key in parent:
        return _nonnegative_int(parent, flat_key, default, "storage")
    if nested_key in nested:
        return _nonnegative_int(nested, nested_key, default, label)
    return default
