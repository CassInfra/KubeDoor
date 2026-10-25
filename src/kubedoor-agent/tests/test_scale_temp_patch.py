"""扩缩容写 scale.temp 的方式:准入控制只认这次请求新写入的标记,patch 里必须是这次设置的值。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from conftest import AGENT_ROOT, load_module


@pytest.fixture
def scaler():
    return load_module("agent_test_cci_scaler", AGENT_ROOT / "scaler" / "cci_scaler.py")


def deployment(annotations, replicas=3):
    return SimpleNamespace(metadata=SimpleNamespace(annotations=annotations), spec=SimpleNamespace(replicas=replicas))


def apps_api(read=None):
    return SimpleNamespace(
        read_namespaced_deployment=AsyncMock(return_value=read),
        patch_namespaced_deployment=AsyncMock(),
        patch_namespaced_deployment_scale=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_temporary_scale_down_with_add_label_patches_the_new_marker(scaler):
    # 缩容 + add_label 会重新读一次 Deployment;以前 patch 带的是读回来的旧注解,新标记丢了,
    # 准入控制就按管控值改回,临时缩容不生效
    marker = "2026-10-25 10:00:00@3-->1"
    api = apps_api(read=deployment({"scale.temp": "2026-10-20 09:00:00@2-->3", "team": "a"}))
    await scaler.patch_deployment_replicas_with_retry(
        api, "app", "demo", 1, deployment({"scale.temp": marker}), 3, "true", 0, "true",
    )
    api.patch_namespaced_deployment.assert_awaited_once_with(
        "app", "demo", {"metadata": {"annotations": {"scale.temp": marker}}, "spec": {"replicas": 1}},
    )
    api.patch_namespaced_deployment_scale.assert_not_awaited()


@pytest.mark.asyncio
async def test_permanent_scale_removes_a_leftover_marker_and_otherwise_uses_the_scale_subresource(scaler):
    # 非临时扩缩容:scale_service 把旧标记设成 None,patch 里带 None 删掉它
    api = apps_api()
    await scaler.patch_deployment_replicas_with_retry(
        api, "app", "demo", 5, deployment({"scale.temp": None, "team": "a"}), 3, None, 1, None,
    )
    api.patch_namespaced_deployment.assert_awaited_once_with(
        "app", "demo", {"metadata": {"annotations": {"scale.temp": None}}, "spec": {"replicas": 5}},
    )
    # 没有旧标记就走 scale 子资源
    api = apps_api()
    await scaler.patch_deployment_replicas_with_retry(api, "app", "demo", 5, deployment({}), 3, None, 0, None)
    api.patch_namespaced_deployment_scale.assert_awaited_once_with("app", "demo", {"spec": {"replicas": 5}})
    api.patch_namespaced_deployment.assert_not_awaited()
