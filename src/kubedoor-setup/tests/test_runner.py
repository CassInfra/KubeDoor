import asyncio
import copy
import json
import struct
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

import kubedoor_setup.runner as module
from kubedoor_setup.config import default_private, ensure_secrets
from kubedoor_setup.errors import SetupError
from kubedoor_setup.kubernetes import OWNER, resource_path
from kubedoor_setup.repository import new_state
from kubedoor_setup.runner import InstallRunner, SERVICES, probe_write

ID = '5e4eaf75-3c7c-47ba-a181-cff711e73594'


def deployment(name):
    return {'apiVersion': 'apps/v1', 'kind': 'Deployment', 'metadata': {'name': name, 'namespace': 'kubedoor', 'annotations': {OWNER: ID}},
            'spec': {'replicas': 1, 'selector': {'matchLabels': {'app': name}},
                     'template': {'metadata': {'annotations': {'checksum/config': 'new-config', OWNER: ID}},
                                  'spec': {'containers': [{'name': name, 'image': 'example/image:version'}]}}}}


def ready(obj):
    obj = copy.deepcopy(obj)
    obj['metadata'].update(resourceVersion='5', generation=2)
    obj['status'] = {'observedGeneration': 2, 'replicas': 1, 'updatedReplicas': 1, 'readyReplicas': 1, 'availableReplicas': 1}
    return obj


class MemoryRepo:
    def __init__(self, components=None):
        self.lock = asyncio.Lock()
        self.state = new_state(ID, 'kubedoor')
        self.state['phase'] = 'running'
        self.history = []
        self.private = ensure_secrets(default_private())
        self.private['config']['public_url'] = 'https://kd.example.com'
        if components:
            self.private['config']['components'].update(components)

    async def load(self):
        return copy.deepcopy(self.state)

    async def save(self, state):
        self.state = copy.deepcopy(state)
        self.history.append(copy.deepcopy(state))
        return state

    async def load_private(self, state=None):
        return copy.deepcopy(self.private)


class FakeKubernetes:
    namespace = 'kubedoor'

    def __init__(self, existing=()):
        self.objects = {resource_path(obj): ready(obj) if obj['kind'] == 'Deployment' else copy.deepcopy(obj) for obj in existing}
        self.created = []
        self.replaced = []

    async def get(self, obj):
        return copy.deepcopy(self.objects.get(resource_path(obj)))

    async def request(self, method, path, **kwargs):
        if path == '/version':
            return {'minor': '30'}
        assert kwargs.get('params') == {'dryRun': 'All'}
        return copy.deepcopy(kwargs['body'])

    async def create(self, obj):
        self.created.append(copy.deepcopy(obj))
        self.objects[resource_path(obj)] = ready(obj) if obj['kind'] == 'Deployment' else copy.deepcopy(obj)
        return obj

    async def replace(self, obj):
        self.replaced.append(copy.deepcopy(obj))
        self.objects[resource_path(obj)] = ready(obj) if obj['kind'] == 'Deployment' else copy.deepcopy(obj)
        return obj

    async def list(self, path, params=None):
        return []


class ObservedRunner(InstallRunner):
    def __init__(self, repo, k8s, bad_service=None):
        super().__init__(repo, k8s)
        self.bad_service = bad_service
        self.health_timeout = 0
        self.timeout = .1
        self.poll_interval = 0
        self.finalized = False
        self.verified = False

    async def service_health(self, sid):
        if sid == self.bad_service:
            raise SetupError('Service health failed', 502)

    async def verify_endpoints(self):
        self.verified = True

    async def start_finalizer(self):
        self.finalized = True
        await self.update(phase='finalizing')


def setup_probes(monkeypatch):
    for name in ('check_postgres', 'reserve_database', 'check_metrics'):
        monkeypatch.setattr(module, name, AsyncMock())
    monkeypatch.setattr(module, 'render_master', lambda *_: [deployment(name) for _, _, name in SERVICES])


@pytest.mark.asyncio
async def test_all_selected_services_required_before_finalizer(monkeypatch):
    setup_probes(monkeypatch)
    repo, k8s = MemoryRepo(), FakeKubernetes()
    runner = ObservedRunner(repo, k8s)
    await runner.run()
    assert runner.finalized and runner.verified
    assert repo.state['phase'] == 'finalizing'
    assert len(repo.state['services']) == 7
    assert all(row['status'] == 'ready' for row in repo.state['services'])
    assert all(state['phase'] != 'succeeded' for state in repo.history)


@pytest.mark.asyncio
@pytest.mark.parametrize('bad_service', ['master', 'grafana', 'alarm', 'web', 'alertmanager', 'ai', 'vmalert'])
async def test_any_selected_service_failure_prevents_finalization(monkeypatch, bad_service):
    setup_probes(monkeypatch)
    repo = MemoryRepo()
    runner = ObservedRunner(repo, FakeKubernetes(), bad_service)
    await runner.run()
    assert not runner.finalized
    assert repo.state['phase'] == 'failed'
    assert runner.row(repo.state, bad_service)['status'] == 'failed'
    assert all(state['phase'] != 'succeeded' for state in repo.history)


@pytest.mark.asyncio
async def test_unselected_services_are_skipped_without_blocking(monkeypatch):
    setup_probes(monkeypatch)
    repo = MemoryRepo({'grafana': False, 'vmalert': False, 'alertmanager': False})
    runner = ObservedRunner(repo, FakeKubernetes())
    await runner.run()
    assert runner.finalized
    assert {row['id'] for row in repo.state['services'] if row['status'] == 'skipped'} == {'grafana', 'vmalert', 'alertmanager'}


@pytest.mark.asyncio
async def test_retry_preserves_matching_workload_and_rechecks_health(monkeypatch):
    setup_probes(monkeypatch)
    repo, k8s = MemoryRepo(), FakeKubernetes([deployment(name) for _, _, name in SERVICES])
    runner = ObservedRunner(repo, k8s)
    runner.service_health = AsyncMock()
    await runner.run()
    assert runner.finalized
    assert not k8s.created and not k8s.replaced
    for sid, _, _ in SERVICES:
        assert sum(call.args == (sid,) for call in runner.service_health.call_args_list) >= 2


@pytest.mark.asyncio
async def test_retry_restores_drifted_image_even_when_checksum_is_unchanged():
    expected = deployment('kubedoor-master')
    current = copy.deepcopy(expected)
    current['spec']['template']['spec']['containers'][0]['image'] = 'unexpected/image:old'
    repo, k8s = MemoryRepo(), FakeKubernetes([current])
    repo.state['services'] = [{'id': 'master', 'status': 'pending'}]
    runner = ObservedRunner(repo, k8s)
    runner.installation_id = ID
    runner.objects = [expected]
    await runner.deploy('master')
    assert len(k8s.replaced) == 1
    assert runner.row(repo.state, 'master')['status'] == 'ready'


@pytest.mark.asyncio
async def test_unrelated_workload_is_never_adopted():
    expected = deployment('kubedoor-master')
    old = copy.deepcopy(expected)
    old['metadata']['annotations'][OWNER] = 'another-install'
    repo, k8s = MemoryRepo(), FakeKubernetes([old])
    runner = ObservedRunner(repo, k8s)
    runner.installation_id = ID
    runner.objects = [expected]
    with pytest.raises(SetupError) as error:
        await runner.preflight()
    assert error.value.status == 409
    assert not k8s.created and not k8s.replaced


def test_service_replacement_preserves_cluster_assigned_addresses_and_nodeport():
    obj = {'apiVersion': 'v1', 'kind': 'Service', 'metadata': {'name': 'web', 'namespace': 'kubedoor'},
           'spec': {'type': 'NodePort', 'ports': [{'name': 'http', 'port': 80}]}}
    old = copy.deepcopy(obj)
    old['metadata']['resourceVersion'] = '6'
    old['spec'].update(clusterIP='10.96.1.2', clusterIPs=['10.96.1.2'], ipFamilies=['IPv4'])
    old['spec']['ports'][0]['nodePort'] = 31000
    replaced = InstallRunner.for_replace(obj, old)
    assert replaced['spec']['ports'][0]['nodePort'] == 31000
    assert replaced['spec']['clusterIP'] == '10.96.1.2'
    assert replaced['metadata']['resourceVersion'] == '6'


def read_varint(raw, offset=0):
    result, shift = 0, 0
    while True:
        value = raw[offset]
        offset += 1
        result |= (value & 127) << shift
        if value < 128:
            return result, offset
        shift += 7


def message_fields(raw):
    offset, result = 0, []
    while offset < len(raw):
        tag, offset = read_varint(raw, offset)
        number, wire = tag >> 3, tag & 7
        if wire == 2:
            length, offset = read_varint(raw, offset)
            value, offset = raw[offset:offset + length], offset + length
        elif wire == 1:
            value, offset = struct.unpack('<d', raw[offset:offset + 8])[0], offset + 8
        elif wire == 0:
            value, offset = read_varint(raw, offset)
        else:
            raise AssertionError(wire)
        result.append((number, value))
    return result


def test_metrics_probe_is_snappy_prometheus_write_request_with_unique_sample():
    timestamp = 1_799_999_999_123
    compressed = probe_write(ID, timestamp)
    length, offset = read_varint(compressed)
    tag = compressed[offset]
    offset += 1
    assert tag & 3 == 0  # standard Snappy literal block
    literal = tag >> 2
    if literal >= 60:
        width = literal - 59
        literal = int.from_bytes(compressed[offset:offset + width], 'little')
        offset += width
    body = compressed[offset:]
    assert len(body) == literal + 1 == length
    request = message_fields(body)
    assert len(request) == 1 and request[0][0] == 1
    fields = message_fields(request[0][1])
    labels = {dict(message_fields(value))[1].decode(): dict(message_fields(value))[2].decode()
              for number, value in fields if number == 1}
    assert labels == {'__name__': 'kubedoor_install_probe', 'installation_id': ID}
    sample = dict(message_fields(next(value for number, value in fields if number == 2)))
    assert sample == {1: float(timestamp), 2: timestamp}


@pytest.mark.asyncio
async def test_health_http_reads_complete_chunked_json():
    async def health(request):
        response = web.StreamResponse(headers={'Content-Type': 'application/json'})
        await response.prepare(request)
        await response.write(b'{"ok":')
        await asyncio.sleep(.02)
        await response.write(b'true,"installation_id":"' + ID.encode() + b'"}')
        await response.write_eof()
        return response
    app = web.Application()
    app.router.add_get('/health', health)
    runner = ObservedRunner(MemoryRepo(), FakeKubernetes())
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        runner.http = session
        result = await runner.request('GET', str(server.make_url('/health')))
    assert result == {'ok': True, 'installation_id': ID}


@pytest.mark.asyncio
async def test_external_url_ownership_checked_before_credentials_sent():
    runner = ObservedRunner(MemoryRepo(), FakeKubernetes())
    runner.config = runner.repo.private['config']
    runner.installation_id = ID
    runner.request = AsyncMock(return_value={'installation_id': 'another-instance'})
    with pytest.raises(SetupError):
        await InstallRunner.verify_endpoints(runner)
    assert runner.request.await_count == 1
    assert runner.request.call_args.args[0] == 'GET'
    assert 'json' not in runner.request.call_args.kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize('sid', ['master', 'ai', 'alarm'])
async def test_internal_health_requires_current_installation_identity(sid):
    runner = ObservedRunner(MemoryRepo(), FakeKubernetes())
    runner.secrets = runner.repo.private['secrets']
    runner.installation_id = ID
    runner.request = AsyncMock(return_value={'ok': True, 'installation_id': 'old-installation'})
    with pytest.raises(SetupError):
        await InstallRunner.service_health(runner, sid)


@pytest.mark.asyncio
async def test_ai_internal_health_sends_complete_trusted_identity():
    runner = ObservedRunner(MemoryRepo(), FakeKubernetes())
    runner.secrets = runner.repo.private['secrets']
    runner.installation_id = ID
    runner.request = AsyncMock(return_value={'ok': True, 'installation_id': ID})
    await InstallRunner.service_health(runner, 'ai')
    headers = runner.request.call_args.kwargs['headers']
    assert headers == {'X-Kubedoor-Token': runner.secrets['internal_token'], 'X-User-Name': 'kubedoor', 'X-User-Permission': 'admin'}
    assert runner.request.call_args.args[1].endswith(':8000/api/ai/internal/setup-health')


@pytest.mark.asyncio
@pytest.mark.parametrize('rule', [{'health': 'unknown'}, {}, {'health': 'err'}, {'health': 'ok', 'lastError': 'query failed'}])
async def test_vmalert_must_have_successful_rule_evaluation(rule):
    runner = ObservedRunner(MemoryRepo(), FakeKubernetes())
    runner.secrets = runner.repo.private['secrets']
    runner.request = AsyncMock(side_effect=[{}, {'status': 'success', 'data': {'groups': [{'rules': [rule]}]}}])
    with pytest.raises(SetupError):
        await InstallRunner.service_health(runner, 'vmalert')
