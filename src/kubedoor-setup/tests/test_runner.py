import asyncio
import copy
import json
import struct
from unittest.mock import AsyncMock
from urllib.parse import quote

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

import kubedoor_setup.runner as module
from kubedoor_setup.config import default_private, ensure_secrets
from kubedoor_setup.errors import SetupError
from kubedoor_setup.kubernetes import OWNER, resource_path
from kubedoor_setup.repository import new_state
from kubedoor_setup.runner import InstallRunner, SERVICES, ServiceCheckError, pod_summary, probe_write

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
async def test_services_deploy_together_and_web_switches_last(monkeypatch):
    setup_probes(monkeypatch)
    runner = ObservedRunner(MemoryRepo(), FakeKubernetes())
    started, others = [], asyncio.Event()
    original = runner.deploy

    async def deploy(sid):
        started.append(sid)
        if sid == 'master':
            # master 还没部署完，其他服务已经开始部署：不再先等控制服务就绪
            await asyncio.wait_for(others.wait(), 1)
        elif sid != 'web':
            others.set()
        await original(sid)

    runner.deploy = deploy
    await runner.run()
    assert runner.finalized
    assert started[-1] == 'web' and set(started[:-1]) == {sid for sid, _, _ in SERVICES if sid != 'web'}


@pytest.mark.asyncio
async def test_master_failure_is_reported_while_other_services_still_get_ready(monkeypatch):
    setup_probes(monkeypatch)
    repo = MemoryRepo()
    runner = ObservedRunner(repo, FakeKubernetes(), 'master')
    await runner.run()
    assert repo.state['phase'] == 'failed' and repo.state['error'] == '控制服务未就绪，请修复后重试'
    rows = {row['id']: row['status'] for row in repo.state['services']}
    assert rows['master'] == 'failed' and rows['web'] == 'waiting'
    # 其他服务照常部署就绪，修好 master 重试时不用再等它们
    assert all(rows[sid] == 'ready' for sid in ('ai', 'alarm', 'grafana', 'vmalert', 'alertmanager'))


@pytest.mark.asyncio
async def test_unselected_services_are_skipped_without_blocking(monkeypatch):
    setup_probes(monkeypatch)
    repo = MemoryRepo({'alerting': False})
    runner = ObservedRunner(repo, FakeKubernetes())
    await runner.run()
    assert runner.finalized
    # 告警组件一起不装；Grafana 看板必装
    assert {row['id'] for row in repo.state['services'] if row['status'] == 'skipped'} == {'alarm', 'vmalert', 'alertmanager'}
    assert repo.state['selected_services'] == ['web', 'master', 'ai', 'grafana']


@pytest.mark.asyncio
async def test_older_saved_selection_still_installs_grafana(monkeypatch):
    setup_probes(monkeypatch)
    repo = MemoryRepo()
    repo.private['config']['components'] = {'grafana': False, 'vmalert': True, 'alertmanager': True}
    runner = ObservedRunner(repo, FakeKubernetes())
    await runner.run()
    assert runner.finalized
    assert all(row['status'] == 'ready' for row in repo.state['services'])


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


def probe_timestamp(raw):
    """从 probe_write 的请求体（Snappy 字面量块 + WriteRequest）里取出样本时间戳。"""
    _, offset = read_varint(raw)
    literal = raw[offset] >> 2
    offset += 1 + (literal - 59 if literal >= 60 else 0)
    series = message_fields(message_fields(raw[offset:])[0][1])
    return dict(message_fields(next(value for number, value in series if number == 2)))[2]


class FakeTimeSeries:
    """假的 VictoriaMetrics：记录远程写请求，查询时按需返回刚写入的检测样本。"""

    def __init__(self, write_status=204, visible_after=1):
        self.write_status, self.visible_after = write_status, visible_after
        self.writes, self.queries, self.params = [], [], []

    def app(self):
        app = web.Application()
        app.router.add_post('/api/v1/write', self.write)
        app.router.add_get('/api/v1/query', self.query)
        return app

    async def write(self, request):
        body = await request.read()
        self.writes.append({'authorization': request.headers.get('Authorization'), 'cookie': request.headers.get('Cookie'),
                            'encoding': request.headers.get('Content-Encoding'), 'timestamp': probe_timestamp(body)})
        return web.Response(status=self.write_status)

    async def query(self, request):
        self.queries.append(request.query['query'])
        self.params.append(dict(request.query))
        rows = []
        if self.writes and len(self.queries) >= self.visible_after:
            rows = [{'metric': {'installation_id': ID}, 'value': [1, str(self.writes[-1]['timestamp'])]}]
        return web.json_response({'status': 'success', 'data': {'resultType': 'vector', 'result': rows}})


def metrics_runner(server, password='p@ss:w/rd'):
    runner = ObservedRunner(MemoryRepo(), FakeKubernetes())
    runner.config, runner.installation_id = runner.repo.private['config'], ID
    origin = f"http://monit:{quote(password, safe='')}@{server.host}:{server.port}"
    runner.config['metrics'].update(user='monit', password=password, read_url=origin, write_url=origin + '/api/v1/write')
    runner.health_timeout = 5
    return runner


def check_row(repo, name):
    return next(row for row in repo.state['checks'] if row['name'] == name)


@pytest.mark.asyncio
async def test_metrics_write_goes_straight_to_time_series_and_is_queried_back():
    tsdb = FakeTimeSeries(visible_after=2)
    async with TestServer(tsdb.app()) as server:
        runner = metrics_runner(server)
        await runner.verify_metrics()
    # 直连时序库：带第 3 步写地址里的账号密码，不带 KubeDoor 的登录 Cookie，也不经过指标网关
    assert len(tsdb.writes) == 1
    assert tsdb.writes[0]['authorization'] == aiohttp.BasicAuth('monit', 'p@ss:w/rd').encode()
    assert tsdb.writes[0]['cookie'] is None and tsdb.writes[0]['encoding'] == 'snappy'
    assert tsdb.queries == ['kubedoor_install_probe{installation_id="' + ID + '"}'] * 2
    # nocache=1：不等 VictoriaMetrics 默认隐藏的最近 30 秒
    assert all(params.get('nocache') == '1' for params in tsdb.params)
    row = check_row(runner.repo, 'write')
    assert (row['status'], row['message']) == ('success', '时序库写入及查询验证通过')
    assert any(check['name'] == 'write' and check['status'] == 'running'
               for state in runner.repo.history for check in state['checks'])


@pytest.mark.asyncio
@pytest.mark.parametrize('status, expected', [(401, '时序库拒绝写入（HTTP 401）'), (500, '时序库写入验证失败（HTTP 500）')])
async def test_metrics_write_failure_marks_write_check(status, expected):
    tsdb = FakeTimeSeries(write_status=status)
    async with TestServer(tsdb.app()) as server:
        runner = metrics_runner(server)
        with pytest.raises(SetupError, match=expected):
            await runner.verify_metrics()
    assert tsdb.queries == []
    row = check_row(runner.repo, 'write')
    assert row['status'] == 'error' and row['message'].startswith(expected)


@pytest.mark.asyncio
async def test_invisible_probe_times_out_with_clear_reason():
    tsdb = FakeTimeSeries(visible_after=10 ** 6)
    async with TestServer(tsdb.app()) as server:
        runner = metrics_runner(server)
        runner.health_timeout = 0
        with pytest.raises(SetupError, match='远程写地址和远程读地址是否指向同一个时序库'):
            await runner.verify_metrics()
    assert check_row(runner.repo, 'write')['status'] == 'error'


@pytest.mark.asyncio
async def test_unreachable_time_series_is_reported():
    async with TestServer(web.Application()) as server:
        runner = metrics_runner(server)
    with pytest.raises(SetupError, match='无法连接时序库远程写地址'):
        await runner.verify_metrics()


@pytest.mark.asyncio
async def test_final_checks_run_endpoints_and_metrics_together_without_probe_account():
    runner = ObservedRunner(MemoryRepo(), FakeKubernetes())
    runner.config, runner.installation_id = runner.repo.private['config'], ID
    runner.config['metrics']['write_url'] = 'http://vm.example.invalid:8428/api/v1/write'
    calls = []
    metrics_started = asyncio.Event()

    async def request(method, url, **kwargs):
        calls.append((method, url.removeprefix(runner.config['public_url'])))
        if url.endswith('/setup-status.json'):
            # 写入验证在对外入口验证结束前就开始了：两项是一起跑的
            await asyncio.wait_for(metrics_started.wait(), 1)
            return {'installation_id': ID}
        return {'success': True, 'status': 'ok'}

    async def verify_metrics():
        metrics_started.set()
        calls.append(('verify_metrics', ''))

    runner.request = request
    runner.verify_metrics = verify_metrics
    # ObservedRunner 把 verify_endpoints 换成了桩，这里要测真实的入口验证
    runner.verify_endpoints = lambda: InstallRunner.verify_endpoints(runner)
    assert await InstallRunner.final_checks(runner) == ''
    assert ('verify_metrics', '') in calls and ('POST', '/api/auth/logout') in calls
    assert not any('/api/admin/users' in url or 'metrics/write' in url for _, url in calls)
    assert check_row(runner.repo, 'endpoints')['status'] == 'success'


@pytest.mark.asyncio
async def test_one_final_check_failing_does_not_hide_the_other_result():
    runner = ObservedRunner(MemoryRepo(), FakeKubernetes())
    runner.config, runner.installation_id = runner.repo.private['config'], ID
    runner.config['metrics']['write_url'] = 'http://vm.example.invalid:8428/api/v1/write'
    runner.request = AsyncMock(side_effect=lambda method, url, **kwargs:
                               {'installation_id': ID} if url.endswith('/setup-status.json') else {'success': True, 'status': 'ok'})
    runner.verify_metrics = AsyncMock(side_effect=SetupError('时序库拒绝写入（HTTP 401）', 502))
    assert await InstallRunner.final_checks(runner) == '时序库拒绝写入（HTTP 401）'
    assert check_row(runner.repo, 'endpoints')['status'] == 'success'


@pytest.mark.asyncio
async def test_query_only_metrics_skip_write_check():
    runner = ObservedRunner(MemoryRepo(), FakeKubernetes())
    runner.config, runner.installation_id = runner.repo.private['config'], ID
    runner.request = AsyncMock(side_effect=lambda method, url, **kwargs:
                               {'installation_id': ID} if url.endswith('/setup-status.json') else {'success': True, 'status': 'ok'})
    runner.verify_metrics = AsyncMock()
    assert await InstallRunner.final_checks(runner) == ''
    runner.verify_metrics.assert_not_awaited()
    assert check_row(runner.repo, 'write')['status'] == 'skipped'
    assert check_row(runner.repo, 'endpoints')['status'] == 'success'


@pytest.mark.asyncio
async def test_service_rechecks_all_finish_before_an_unexpected_error_is_raised(monkeypatch):
    setup_probes(monkeypatch)
    runner = ObservedRunner(MemoryRepo(), FakeKubernetes())
    rechecked = []

    async def recheck(sid):
        if sid == 'master':
            raise RuntimeError('boom')
        await asyncio.sleep(.05)
        rechecked.append(sid)
        return ''

    runner.recheck = recheck
    with pytest.raises(RuntimeError, match='boom'):
        await runner.run()
    # 其他服务的复查都已结束，没有留在后台继续改安装状态
    assert set(rechecked) == {sid for sid, _, _ in SERVICES if runner.selected[sid]} - {'master'}
    assert not runner.finalized


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


OLD_ALARM_IMAGE = 'registry.example.invalid/starsl.cn/kubedoor-alarm:2.1.0'


def alarm_deployment(image=OLD_ALARM_IMAGE):
    obj = deployment('kubedoor-alarm')
    obj['spec']['template']['spec']['containers'][0]['image'] = image
    return obj


class HealthRunner(ObservedRunner):
    """用真实的健康检查，服务地址指向本地测试服务器。"""
    service_health = InstallRunner.service_health

    def __init__(self, repo, k8s, base):
        super().__init__(repo, k8s)
        self.base = base
        self.secrets = repo.private['secrets']
        self.installation_id = ID

    def url(self, name, port=80):
        return self.base


class PodsKubernetes(FakeKubernetes):
    def __init__(self, existing=(), pods=()):
        super().__init__(existing)
        self.pods = list(pods)

    async def list(self, path, params=None):
        return copy.deepcopy(self.pods)


RUNNING_POD = {'metadata': {'name': 'kubedoor-alarm-7d9c5-x2k4p'},
               'status': {'phase': 'Running', 'containerStatuses': [
                   {'name': 'kubedoor-alarm', 'ready': True, 'restartCount': 0, 'image': OLD_ALARM_IMAGE}]}}


@pytest.mark.asyncio
async def test_health_404_names_the_image_without_setup_health_endpoint():
    # 旧版 kubedoor-alarm 镜像没有 /internal/setup-health：Pod 正常运行，但健康接口 404
    app = web.Application()
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        runner = HealthRunner(MemoryRepo(), FakeKubernetes(), str(server.make_url('')).rstrip('/'))
        runner.http, runner.objects = session, [alarm_deployment()]
        with pytest.raises(ServiceCheckError) as caught:
            await runner.service_health('alarm')
    error = caught.value
    assert error.http_status == 404
    assert error.message == (f'kubedoor-alarm 镜像 {OLD_ALARM_IMAGE} 没有安装向导健康接口（/internal/setup-health 返回 HTTP 404），'
                             '镜像版本过旧；请换用包含安装向导的新版镜像后重试')
    assert error.probe.startswith('GET http://') and error.probe.endswith('/internal/setup-health → HTTP 404')


@pytest.mark.asyncio
@pytest.mark.parametrize('sid, status, expected', [
    ('alarm', 403, 'kubedoor-alarm 健康接口拒绝了安装服务的内部令牌（HTTP 403）'),
    ('master', 503, 'kubedoor-master 已启动，但数据库或时序库连接检查失败（健康接口返回 HTTP 503）'),
    ('ai', 503, 'kubedoor-ai 已启动，但数据库连接检查失败（健康接口返回 HTTP 503）'),
    ('ai', 500, 'kubedoor-ai 健康检查未通过（GET http://kubedoor-ai.kubedoor:8000/api/ai/internal/setup-health → HTTP 500）'),
    ('vmalert', 404, 'vmalert 健康检查未通过（GET http://vmalert.kubedoor:8080/health → HTTP 404）'),
    ('alarm', None, '无法连接 kubedoor-alarm（GET http://kubedoor-alarm.kubedoor:80/internal/setup-health → 连接失败或超时）'),
])
async def test_health_failures_explain_service_and_cause(sid, status, expected):
    runner = ObservedRunner(MemoryRepo(), FakeKubernetes())
    runner.secrets, runner.installation_id = runner.repo.private['secrets'], ID

    async def failing(method, url, **kwargs):
        if status is None:
            raise ServiceCheckError('服务连接超时或证书验证失败，请检查网络与服务状态', probe=f'{method} {url} → 连接失败或超时')
        raise ServiceCheckError(f'服务连接或健康验证失败（HTTP {status}）', http_status=status, probe=f'{method} {url} → HTTP {status}')
    runner.request = failing
    with pytest.raises(ServiceCheckError) as caught:
        await InstallRunner.service_health(runner, sid)
    assert caught.value.message.startswith(expected)
    assert caught.value.http_status == status


@pytest.mark.asyncio
async def test_identity_mismatch_names_the_running_image():
    runner = ObservedRunner(MemoryRepo(), FakeKubernetes())
    runner.secrets, runner.installation_id, runner.objects = runner.repo.private['secrets'], ID, [alarm_deployment()]
    runner.request = AsyncMock(return_value={'ok': True, 'installation_id': 'old-installation'})
    with pytest.raises(SetupError, match='安装标识不一致') as caught:
        await InstallRunner.service_health(runner, 'alarm')
    assert OLD_ALARM_IMAGE in caught.value.message


@pytest.mark.asyncio
async def test_health_retry_shows_last_reason_and_failure_keeps_image_and_pod_details():
    app = web.Application()
    repo = MemoryRepo()
    k8s = PodsKubernetes([alarm_deployment()], [RUNNING_POD])
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        runner = HealthRunner(repo, k8s, str(server.make_url('')).rstrip('/'))
        runner.http, runner.objects = session, [alarm_deployment()]
        runner.health_timeout, runner.poll_interval = .2, .05
        await runner.deploy('alarm')
    retrying = [row for state in repo.history for row in state.get('services', [])
                if row['id'] == 'alarm' and row['status'] == 'checking']
    assert retrying[0]['message'] == '副本已就绪，正在验证服务接口和依赖连接'
    assert any('上次未通过：kubedoor-alarm 镜像' in row['message'] for row in retrying[1:])
    row = runner.row(repo.state, 'alarm')
    assert row['status'] == 'failed'
    assert row['message'].startswith(f'kubedoor-alarm 镜像 {OLD_ALARM_IMAGE} 没有安装向导健康接口')
    details = row['details']
    assert details[:2] == ['Deployment：kubedoor/kubedoor-alarm', f'期望镜像：{OLD_ALARM_IMAGE}']
    assert details[2].startswith('健康检查：GET http://') and details[2].endswith('/internal/setup-health → HTTP 404')
    assert details[3] == f'Pod kubedoor-alarm-7d9c5-x2k4p：Running，就绪 1/1，重启 0 次，运行镜像 {OLD_ALARM_IMAGE}'


@pytest.mark.asyncio
async def test_new_status_clears_previous_failure_details():
    repo = MemoryRepo()
    runner = ObservedRunner(repo, FakeKubernetes())
    await runner.update(service='alarm', status='failed', message='健康检查未通过', details=['期望镜像：example'])
    assert runner.row(repo.state, 'alarm')['details'] == ['期望镜像：example']
    await runner.update(service='alarm', status='deploying', message='创建或核对部署资源')
    assert 'details' not in runner.row(repo.state, 'alarm')


@pytest.mark.asyncio
async def test_failure_details_survive_pod_listing_errors():
    class BrokenPods(FakeKubernetes):
        async def list(self, path, params=None):
            raise SetupError('安装权限不足或准入策略拒绝（HTTP 403）', 403)
    runner = ObservedRunner(MemoryRepo(), BrokenPods())
    runner.objects = [alarm_deployment()]
    assert await runner.failure_details('alarm') == [
        'Deployment：kubedoor/kubedoor-alarm', f'期望镜像：{OLD_ALARM_IMAGE}', 'Pod：读取 Pod 状态失败']
    assert await ObservedRunner(MemoryRepo(), FakeKubernetes()).failure_details('alarm') == []


def test_pod_summary_reports_waiting_reason_restarts_and_deletion():
    pod = {'metadata': {'name': 'kubedoor-ai-1', 'deletionTimestamp': '2026-11-07T00:00:00Z'},
           'status': {'phase': 'Running', 'containerStatuses': [
               {'ready': False, 'restartCount': 3, 'image': 'example/kubedoor-ai:2.5.0',
                'state': {'waiting': {'reason': 'CrashLoopBackOff'}}}]}}
    assert pod_summary(pod) == ('Pod kubedoor-ai-1：删除中，就绪 0/1，重启 3 次，等待原因 CrashLoopBackOff，'
                                '运行镜像 example/kubedoor-ai:2.5.0')
    assert pod_summary({}) == 'Pod ：状态未知，就绪 0/0，重启 0 次'
