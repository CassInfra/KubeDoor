import copy
from unittest.mock import AsyncMock

import pytest

from kubedoor_setup.kubernetes import Kubernetes, workload_ready


def healthy():
    return {'kind': 'Deployment', 'metadata': {'generation': 4},
            'spec': {'replicas': 2, 'template': {'metadata': {'annotations': {'checksum/config': 'current'}}}},
            'status': {'observedGeneration': 4, 'replicas': 2, 'updatedReplicas': 2, 'readyReplicas': 2, 'availableReplicas': 2}}


@pytest.mark.parametrize('changes', [
    {'observedGeneration': 3}, {'updatedReplicas': 1}, {'replicas': 3}, {'readyReplicas': 1},
    {'availableReplicas': 1}, {'unavailableReplicas': 1},
])
def test_deployment_running_or_old_replicas_are_not_ready(changes):
    obj = healthy()
    obj['status'].update(changes)
    assert not workload_ready(obj)


def test_ready_requires_expected_configuration_and_nonzero_replicas():
    obj = healthy()
    assert workload_ready(obj, obj)
    expected = copy.deepcopy(obj)
    expected['spec']['template']['metadata']['annotations']['checksum/config'] = 'new'
    assert not workload_ready(obj, expected)
    obj['spec']['replicas'] = 0
    assert not workload_ready(obj)


def test_terminating_deployment_is_not_ready():
    obj = healthy()
    obj['metadata']['deletionTimestamp'] = '2026-01-01T00:00:00Z'
    assert not workload_ready(obj)


def test_same_checksum_with_wrong_container_image_is_not_ready():
    obj = healthy()
    obj['spec']['template']['spec'] = {'containers': [{'name': 'master', 'image': 'kubedoor-master:old'}]}
    expected = copy.deepcopy(obj)
    expected['spec']['template']['spec']['containers'][0]['image'] = 'kubedoor-master:new'
    assert not workload_ready(obj, expected)


def test_daemonset_must_cover_all_desired_nodes():
    obj = {'kind': 'DaemonSet', 'metadata': {'generation': 1}, 'spec': {},
           'status': {'observedGeneration': 1, 'desiredNumberScheduled': 3, 'updatedNumberScheduled': 3,
                      'numberAvailable': 2, 'numberReady': 2}}
    assert not workload_ready(obj)
    obj['status'].update(numberAvailable=3, numberReady=3)
    assert workload_ready(obj)
    obj['status']['desiredNumberScheduled'] = 0
    assert not workload_ready(obj)


@pytest.mark.asyncio
async def test_permissions_check_uses_namespace_and_group():
    k8s = Kubernetes('target')
    k8s.request = AsyncMock(return_value={'status': {'allowed': True}})
    assert await k8s.allowed('Deployment', 'create', version='apps/v1')
    body = k8s.request.call_args.kwargs['body']
    assert body['spec']['resourceAttributes'] == {'group': 'apps', 'resource': 'deployments', 'verb': 'create', 'namespace': 'target'}
