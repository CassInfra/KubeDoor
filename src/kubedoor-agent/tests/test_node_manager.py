from unittest.mock import AsyncMock, Mock

import pytest

from test_workload_cache import FakeResp


@pytest.mark.asyncio
async def test_node_pod_count_uses_raw_json(node_manager_module):
    core_v1 = Mock()
    core_v1.list_pod_for_all_namespaces = AsyncMock(side_effect=[
        FakeResp(body={"items": [{"metadata": {"name": "a"}}] * 2, "metadata": {"continue": "tok"}}),
        FakeResp(body={"items": [{"metadata": {"name": "c"}}], "metadata": {}}),
    ])
    assert await node_manager_module._get_node_pod_count(core_v1, "node-1") == 3
    first = core_v1.list_pod_for_all_namespaces.await_args_list[0].kwargs
    assert first["field_selector"] == "spec.nodeName=node-1,status.phase!=Failed,status.phase!=Succeeded"
    assert first["_preload_content"] is False


@pytest.mark.asyncio
async def test_node_pod_count_returns_zero_on_api_error(node_manager_module):
    core_v1 = Mock()
    core_v1.list_pod_for_all_namespaces = AsyncMock(side_effect=[FakeResp(status=403, body={})])
    assert await node_manager_module._get_node_pod_count(core_v1, "node-1") == 0
