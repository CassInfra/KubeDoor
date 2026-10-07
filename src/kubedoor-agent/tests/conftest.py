"""Load agent modules in isolation from similarly named master modules."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import pytest


AGENT_ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def jvm_module():
    return load_module("agent_test_jvm_config", AGENT_ROOT / "func_manager" / "jvm_config.py")


@pytest.fixture
def admission_module(monkeypatch, jvm_module):
    fake_utils = ModuleType("utils")
    fake_utils.PROM_K8S_TAG_VALUE = "test-cluster"
    fake_utils.send_msg = Mock()
    monkeypatch.setitem(sys.modules, "utils", fake_utils)
    package = ModuleType("func_manager")
    package.__path__ = [str(AGENT_ROOT / "func_manager")]
    monkeypatch.setitem(sys.modules, "func_manager", package)
    monkeypatch.setitem(sys.modules, "func_manager.jvm_config", jvm_module)
    return load_module("agent_test_admission", AGENT_ROOT / "func_manager" / "admis_service.py")


@pytest.fixture
def workload_modules(monkeypatch):
    """workload_status / workload_cache / workload_streamer,挂在假的 func_manager 包下加载"""
    from types import SimpleNamespace

    package = ModuleType("func_manager")
    package.__path__ = [str(AGENT_ROOT / "func_manager")]
    monkeypatch.setitem(sys.modules, "func_manager", package)
    status = load_module("func_manager.workload_status", AGENT_ROOT / "func_manager" / "workload_status.py")
    monkeypatch.setitem(sys.modules, "func_manager.workload_status", status)
    cache = load_module("agent_test_workload_cache", AGENT_ROOT / "func_manager" / "workload_cache.py")
    streamer = load_module("agent_test_workload_streamer", AGENT_ROOT / "func_manager" / "workload_streamer.py")
    return SimpleNamespace(status=status, cache=cache, streamer=streamer)


@pytest.fixture
def mcp_module(monkeypatch, workload_modules, client_manager_module):
    from unittest.mock import AsyncMock

    monkeypatch.setitem(sys.modules, "k8s_client_manager", client_manager_module)
    fake_utils = ModuleType("utils")
    fake_utils.parse_cpu = Mock(return_value=0)
    fake_utils.parse_memory = Mock(return_value=0)
    monkeypatch.setitem(sys.modules, "utils", fake_utils)
    res_package = ModuleType("res_manager")
    res_package.__path__ = []
    pod_manager = ModuleType("res_manager.pod_manager")
    pod_manager._list_pod_metrics = AsyncMock(return_value={})
    monkeypatch.setitem(sys.modules, "res_manager", res_package)
    monkeypatch.setitem(sys.modules, "res_manager.pod_manager", pod_manager)
    return load_module("agent_test_mcp_service", AGENT_ROOT / "func_manager" / "mcp_service.py")


@pytest.fixture
def client_manager_module():
    return load_module("agent_test_k8s_client_manager", AGENT_ROOT / "k8s_client_manager.py")


@pytest.fixture
def event_monitor_module(monkeypatch):
    fake_utils = ModuleType("utils")
    fake_utils.PROM_K8S_TAG_VALUE = "test-cluster"
    fake_utils.MSG_TOKEN = "test-token"
    monkeypatch.setitem(sys.modules, "utils", fake_utils)
    package = ModuleType("func_manager")
    package.__path__ = [str(AGENT_ROOT / "func_manager")]
    monkeypatch.setitem(sys.modules, "func_manager", package)
    return load_module("agent_test_k8s_event_monitor", AGENT_ROOT / "func_manager" / "k8s_event_monitor.py")


@pytest.fixture
def pod_manager_module(monkeypatch, client_manager_module):
    monkeypatch.setitem(sys.modules, "k8s_client_manager", client_manager_module)
    fake_utils = ModuleType("utils")
    fake_utils.parse_cpu = Mock(return_value=250.0)
    fake_utils.parse_memory = Mock(return_value=512.0)
    monkeypatch.setitem(sys.modules, "utils", fake_utils)
    return load_module("agent_test_pod_manager", AGENT_ROOT / "res_manager" / "pod_manager.py")


@pytest.fixture
def node_manager_module(monkeypatch, client_manager_module):
    monkeypatch.setitem(sys.modules, "k8s_client_manager", client_manager_module)
    fake_utils = ModuleType("utils")
    for name in ("parse_cpu", "parse_memory", "parse_storage_to_gib", "bytes_to_gib", "parse_pods"):
        setattr(fake_utils, name, Mock(return_value=0))
    monkeypatch.setitem(sys.modules, "utils", fake_utils)
    return load_module("agent_test_node_manager", AGENT_ROOT / "res_manager" / "node_manager.py")
