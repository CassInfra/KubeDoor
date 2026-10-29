import copy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "kubedoor-tools"))


@pytest.fixture(autouse=True)
def permission_policy(monkeypatch):
    """网关默认拿到 master 出厂配置的「权限与审批」策略，不走网络。

    测试可以改 holder.policy（例如关掉某个操作的读写权限、改审批列），或设成 None 模拟取不到策略。
    """
    try:
        from fakes import default_policy
        from kubedoor_ai import permissions
        from kubedoor_ai.gateway import Gateway
    except ImportError:   # 环境里没有网关依赖时，只跑不经过网关的测试
        yield None
        return
    holder = SimpleNamespace(policy=default_policy(), calls=0)

    async def fake_policy(self, identity):
        holder.calls += 1
        return None if holder.policy is None else permissions.normalize_policy(copy.deepcopy(holder.policy))

    monkeypatch.setattr(Gateway, "permission_policy", fake_policy)
    yield holder
