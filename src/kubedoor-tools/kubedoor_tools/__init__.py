"""Cluster-bound tools shared by the KubeDoor assistant and MCP server."""

from __future__ import annotations

import importlib
from typing import Any

__all__ = [
    "KubernetesExecutor",
    "parse_kubeconfig",
    "redact_export",
    "classify_operation",
    "classify_pod_command",
    "identify_targets",
    "default_commands",
    "OP_KEYS",
    "DATA_OPERATIONS",
    "check_data_query",
    "run_data_query",
]

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "parse_kubeconfig": (".config", "parse_kubeconfig"),
    "redact_export": (".config", "redact_export"),
    "DATA_OPERATIONS": (".data", "DATA_OPERATIONS"),
    "check_data_query": (".data", "check_data_query"),
    "run_data_query": (".data", "run_data_query"),
    "KubernetesExecutor": (".executor", "KubernetesExecutor"),
    "OP_KEYS": (".policy", "OP_KEYS"),
    "classify_operation": (".policy", "classify_operation"),
    "classify_pod_command": (".policy", "classify_pod_command"),
    "default_commands": (".policy", "default_commands"),
    "identify_targets": (".policy", "identify_targets"),
}


def __getattr__(name: str) -> Any:
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    submod, attr = target
    module = importlib.import_module(submod, __name__)
    value = getattr(module, attr)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
