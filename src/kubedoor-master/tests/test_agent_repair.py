"""修复 Agent：用「AI Kubeconfig」列保存的 kubeconfig（或上传的）覆盖安装已接入的集群，以及修复弹框里的检查。

复用 test_agent_installations 的假集群、假仓库和夹具；kubedoor-ai 用本地 aiohttp 服务模拟。
"""

import copy
import json
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agent_install import health
from agent_install.kubernetes import InstallError
from auth import policy
from general_settings import probe
from test_agent_installations import (CONFIG, PRESETS, FakeCluster, MemoryRepository, environment, module,  # noqa: F401
                                      settings)

ENV = 'production'


class RepairCluster(FakeCluster):
    """目标集群里的 Agent 配置按命名空间存：全集群按名称列出 kubedoor-agent-config，按命名空间读"""

    def __init__(self):
        super().__init__()
        self.configs = {}

    def add_agent(self, namespace, env=ENV, web_installed=True):
        labels = {'app': 'kubedoor-agent'}
        if web_installed:
            labels[module.MANAGED] = 'web-installer'
        config = {'apiVersion': 'v1', 'kind': 'ConfigMap',
                  'metadata': {'name': 'kubedoor-agent-config', 'namespace': namespace, 'labels': labels},
                  'data': {'PROM_K8S_TAG_VALUE': env}}
        self.configs[namespace] = config
        self.resources[('Namespace', namespace)] = {'kind': 'Namespace', 'metadata': {'name': namespace}}
        if not web_installed:
            # 脚本装的：资源没有网页安装的标签，覆盖安装要强制接管
            self.resources[('ConfigMap', 'kubedoor-agent-config')] = copy.deepcopy(config)

    async def request(self, method, path, **kwargs):
        if path.endswith('/configmaps/kubedoor-agent-config'):
            return copy.deepcopy(self.configs.get(path.split('/')[4]))
        return await super().request(method, path, **kwargs)

    async def list(self, path, params=None):
        if path == '/api/v1/configmaps':
            assert params == {'fieldSelector': 'metadata.name=kubedoor-agent-config'}
            return copy.deepcopy(list(self.configs.values()))
        return await super().list(path, params)


@pytest.fixture
def repairing(environment, monkeypatch):
    """已接入、现在离线的集群 production；kubedoor-ai 里保存的 AI Kubeconfig 由 ai_server 提供"""
    service, _, repo = environment
    cluster = RepairCluster()
    cluster.clients = service.clients
    service.kubernetes = cluster
    service.clients[ENV] = {'online': False, 'ver': '2.5.0', 'last_heartbeat': time.time() - 600}
    monkeypatch.setenv('AI_INTERNAL_TOKEN', 'internal-secret')
    monkeypatch.setattr(health.db, 'pg_fetchval', AsyncMock(return_value=None))
    return service, cluster, repo


class FakeAI:
    """假 kubedoor-ai：GET /api/ai/internal/connections/{env}/kubeconfig"""

    def __init__(self, status=200, reply=None):
        self.status, self.requests = status, []
        self.reply = reply if reply is not None else {'env': ENV, 'context': 'production', 'origin': 'agent-install',
                                                      'content': CONFIG}

    async def handle(self, request):
        self.requests.append({'env': request.match_info['env'], 'headers': dict(request.headers)})
        if isinstance(self.reply, str):
            return web.Response(text=self.reply, status=self.status)
        return web.json_response(self.reply, status=self.status)


async def repair_with(service, monkeypatch, ai, username='admin'):
    app = web.Application()
    app.router.add_get('/api/ai/internal/connections/{env}/kubeconfig', ai.handle)
    async with TestServer(app) as server:
        monkeypatch.setattr(module, 'AI_URL', f'http://{server.host}:{server.port}')
        return await service.repair(ENV, username)


async def installed_before(repo, namespace='ops-agent', **options):
    """这个集群以前网页安装成功过的记录（集群 UID 和假集群一样）"""
    record = {'id': '11111111-1111-4111-8111-111111111111', 'status': 'succeeded', 'revision': 3, 'env': ENV,
              'target_uid': 'cluster-uid', 'encrypted': '', 'expires_at': module.utcnow(),
              'created_at': module.utcnow() - timedelta(days=30), 'updated_at': module.utcnow(),
              'payload': {'created_by': 'admin', 'options': {
                  'env': ENV, 'namespace': namespace, 'mode': 'agent', 'vmagent': 'reuse', 'ksm': 'reuse',
                  'node_exporter': 'reuse', 'master_url': 'https://intranet.example.test', 'agent_tag': '2.0.0',
                  **options}}}
    await repo.create(record)
    return record


async def preflight(service, task, force=False, **changes):
    options = {**task['payload']['options'], **changes}
    return await service.preflight(task['id'], task['payload']['context'], options, force=force)


def check(task, name):
    return next((row for row in task['payload']['checks'] if row['name'] == name), None)


# ---- 建修复任务 ----

@pytest.mark.asyncio
async def test_repair_uses_the_saved_kubeconfig_and_keeps_the_last_install_settings(repairing, monkeypatch):
    service, _, repo = repairing
    await installed_before(repo)
    ai = FakeAI()
    result = await repair_with(service, monkeypatch, ai, username='ops-admin')
    task = result['task']
    assert result['message'] == '' and task['status'] == 'draft'
    assert task['payload']['repair'] == {'env': ENV, 'source': 'ai-connection', 'origin': 'agent-install'}
    options = task['payload']['options']
    # 集群标识固定；命名空间、安装方式、采集组件、KubeDoor 地址沿用上次；Agent 版本用推荐版本
    assert options['env'] == ENV and options['namespace'] == 'ops-agent' and options['mode'] == 'agent'
    assert (options['vmagent'], options['ksm'], options['node_exporter']) == ('reuse', 'reuse', 'reuse')
    assert options['master_url'] == 'https://intranet.example.test'
    assert options['agent_tag'] == module.defaults()['agent_tag']
    assert task['payload']['context'] == 'production'
    # 以管理员身份带内部令牌取；kubeconfig 只进加密材料，不在对外字段里
    assert ai.requests[0]['env'] == ENV and ai.requests[0]['headers']['X-Kubedoor-Token'] == 'internal-secret'
    assert ai.requests[0]['headers']['X-User-Name'] == 'ops-admin' and ai.requests[0]['headers']['X-User-Permission'] == 'admin'
    assert 'private-kube-token' not in json.dumps(service.public(task), ensure_ascii=False, default=str)
    assert 'private-kube-token' in module.decrypt_secret(task['encrypted'])


@pytest.mark.asyncio
@pytest.mark.parametrize('status, reply, message', [
    (404, {'error': {'code': 'connection_missing', 'message': '这个集群还没有保存 AI Kubeconfig'}}, '这个集群还没有保存 AI Kubeconfig'),
    (404, 'Not Found', 'kubedoor-ai 版本较旧'),
    (409, {'error': {'code': 'connection_broken', 'message': '保存的 AI Kubeconfig 无法解密'}}, '无法解密'),
    (200, {'env': ENV, 'context': 'production', 'origin': 'upload', 'content': '{"kind": "Config"}'}, '不能用来安装'),
])
async def test_without_a_usable_saved_kubeconfig_the_page_asks_for_an_upload(repairing, monkeypatch, status, reply, message):
    service, _, repo = repairing
    result = await repair_with(service, monkeypatch, FakeAI(status, reply))
    assert result['task'] is None and message in result['message']
    assert await repo.list() == []


@pytest.mark.asyncio
async def test_repair_explains_when_kubedoor_ai_is_unreachable_or_not_configured(repairing, monkeypatch):
    service, _, _ = repairing
    monkeypatch.setattr(module, 'AI_URL', 'http://127.0.0.1:9')
    assert (await service.repair(ENV, 'admin'))['message'] == '连不上 kubedoor-ai，取不到保存的 AI Kubeconfig'
    monkeypatch.delenv('AI_INTERNAL_TOKEN')
    assert 'AI_INTERNAL_TOKEN' in (await service.repair(ENV, 'admin'))['message']


@pytest.mark.asyncio
async def test_repair_only_for_connected_clusters_and_returns_a_running_one(repairing, monkeypatch):
    service, _, repo = repairing
    with pytest.raises(InstallError) as caught:
        await service.repair('never-connected', 'admin')
    assert caught.value.status == 404
    with pytest.raises(InstallError):
        await service.repair('bad name!', 'admin')
    running = await installed_before(repo, status='running')
    running['status'] = 'running'
    repo.data[running['id']]['status'] = 'running'
    result = await repair_with(service, monkeypatch, FakeAI())
    assert result['task']['id'] == running['id'] and '正在安装或修复' in result['message']


# ---- 修复任务的预检 ----

@pytest.mark.asyncio
async def test_repair_cannot_change_the_cluster_name(repairing):
    service, cluster, _ = repairing
    task = await service.inspect(CONFIG, 'admin', repair={'env': ENV, 'source': 'upload'})
    with pytest.raises(InstallError) as caught:
        await preflight(service, task, env='another-name')
    assert caught.value.status == 409 and ENV in str(caught.value)


@pytest.mark.asyncio
async def test_repair_follows_the_namespace_of_the_existing_agent(repairing):
    service, cluster, _ = repairing
    cluster.add_agent('ops-agent')
    task = await service.inspect(CONFIG, 'admin', repair={'env': ENV, 'source': 'upload'})
    assert task['payload']['options']['namespace'] == 'kubedoor'   # 没有安装记录：默认值
    done = await preflight(service, task, **PRESETS['agent'])
    assert done['status'] == 'ready', done['payload']['checks']
    # 页面上的命名空间换成现有 Agent 所在的，并说明
    assert done['payload']['options']['namespace'] == 'ops-agent'
    assert '命名空间 ops-agent 里有集群 production 的 Agent（网页安装）' in check(done, module.REPAIR_CHECK)['message']
    assert all(item['namespace'] in ('ops-agent', '') for item in done['payload']['resources'])
    # 「这个集群标识已有 Agent 登记」不再拦修复
    assert ENV in service.clients


@pytest.mark.asyncio
async def test_repairing_a_script_installed_agent_needs_takeover_confirmation(repairing):
    service, cluster, _ = repairing
    cluster.add_agent('kubedoor', web_installed=False)
    task = await service.inspect(CONFIG, 'admin', repair={'env': ENV, 'source': 'upload'})
    checked = await preflight(service, task, **PRESETS['agent'])
    assert checked['status'] == 'draft' and check(checked, '安装预检')['forceable'] is True
    assert any(item['reason'].startswith('脚本安装的 Agent') for item in checked['payload']['takeover'])
    forced = await preflight(service, checked, force=True, **PRESETS['agent'])
    assert forced['status'] == 'ready', forced['payload']['checks']
    assert '将接管目标集群里已有的' in check(forced, '强制覆盖')['message']


@pytest.mark.asyncio
async def test_no_agent_in_the_target_cluster_needs_confirmation_unless_installed_here_before(repairing):
    service, cluster, repo = repairing
    task = await service.inspect(CONFIG, 'admin', repair={'env': ENV, 'source': 'upload'})
    checked = await preflight(service, task, **PRESETS['agent'])
    error = check(checked, '安装预检')
    assert checked['status'] == 'draft' and error['forceable'] is True
    assert error['message'].startswith('目标集群里没有找到集群 production 的 Agent')
    assert '目标集群已有 Agent 资源' not in error['message']
    assert checked['payload']['takeover'] == [
        {'kind': 'Agent', 'name': ENV, 'namespace': '', 'reason': '目标集群里没有找到集群 production 的 Agent，请确认 kubeconfig 连的是集群 production'}]
    forced = await preflight(service, checked, force=True, **PRESETS['agent'])
    assert forced['status'] == 'ready'
    messages = [row['message'] for row in forced['payload']['checks'] if row['name'] == '强制覆盖']
    assert messages == ['目标集群里没有找到集群 production 的 Agent，已确认用这个 kubeconfig 覆盖安装']

    # 以前在这个集群（同一个集群 UID）网页安装过：Agent 被删了也直接按记录重装
    await installed_before(repo)
    again = await service.inspect(CONFIG, 'admin', repair={'env': ENV, 'source': 'upload'})
    assert again['payload']['options']['namespace'] == 'ops-agent'
    ready = await preflight(service, again)
    assert ready['status'] == 'ready', ready['payload']['checks']
    assert '按上次的安装记录重新安装到命名空间 ops-agent' in check(ready, module.REPAIR_CHECK)['message']


@pytest.mark.asyncio
async def test_another_clusters_agent_or_cluster_cannot_be_repaired_into(repairing):
    service, cluster, repo = repairing
    cluster.add_agent('kubedoor', env='staging')
    task = await service.inspect(CONFIG, 'admin', repair={'env': ENV, 'source': 'upload'})
    checked = await preflight(service, task, **PRESETS['agent'])
    error = check(checked, '安装预检')
    assert checked['status'] == 'draft' and 'forceable' not in error
    assert '命名空间 kubedoor 里是集群 staging 的 Agent，不是 production' in error['message']

    # 这个集群以前装在另一个集群（UID 不同）上：kubeconfig 连错了集群
    cluster.configs.clear()
    record = await installed_before(repo)
    repo.data[record['id']]['target_uid'] = 'another-cluster'
    checked = await preflight(service, await service.inspect(CONFIG, 'admin', repair={'env': ENV, 'source': 'upload'}))
    assert '集群标识已被其它集群使用' in check(checked, '安装预检')['message']


@pytest.mark.asyncio
async def test_two_agents_with_the_same_name_must_be_cleaned_up_first(repairing):
    service, cluster, _ = repairing
    cluster.add_agent('kubedoor')
    cluster.add_agent('kubedoor-old')
    checked = await preflight(service, await service.inspect(CONFIG, 'admin', repair={'env': ENV, 'source': 'upload'}))
    assert '有 2 套集群名为 production 的 Agent（命名空间 kubedoor、kubedoor-old）' in check(checked, '安装预检')['message']


@pytest.mark.asyncio
async def test_a_normal_install_still_refuses_a_registered_cluster_name(repairing):
    service, _, _ = repairing
    task = await service.inspect(CONFIG, 'admin')
    checked = await preflight(service, task, **PRESETS['agent'])
    assert '这个集群标识已有 Agent 登记' in check(checked, '安装预检')['message']


@pytest.mark.asyncio
async def test_repair_installs_and_brings_the_agent_back_online(repairing):
    service, cluster, repo = repairing
    cluster.add_agent('ops-agent')
    task = await preflight(service, await service.inspect(CONFIG, 'admin', repair={'env': ENV, 'source': 'upload'}),
                           **PRESETS['agent'])
    await service.start(task['id'], task['revision'])
    await service.tasks[task['id']]
    done = await repo.get(task['id'])
    assert done['status'] == 'succeeded', done['payload'].get('error')
    assert all(item['namespace'] in ('ops-agent', '') for item in done['payload']['applied'])
    assert service.clients[ENV]['online'] is True


@pytest.mark.asyncio
async def test_install_stops_when_the_agent_moved_after_the_check(repairing):
    service, cluster, repo = repairing
    cluster.add_agent('ops-agent')
    task = await preflight(service, await service.inspect(CONFIG, 'admin', repair={'env': ENV, 'source': 'upload'}),
                           **PRESETS['agent'])
    cluster.configs = {'elsewhere': {**cluster.configs['ops-agent'],
                                     'metadata': {**cluster.configs['ops-agent']['metadata'], 'namespace': 'elsewhere'}}}
    await service.start(task['id'], task['revision'])
    await service.tasks[task['id']]
    done = await repo.get(task['id'])
    assert done['status'] == 'failed' and '现在在命名空间 elsewhere，和检测时不同' in done['payload']['error']


# ---- 修复弹框里的检查 ----

def test_connection_and_version_checks():
    now = time.time()
    assert health.connection_check(None, now)['status'] == 'error'
    online = health.connection_check({'online': True, 'last_heartbeat': now - 3}, now)
    assert online['status'] == 'success' and online['message'] == '在线，最后心跳 3 秒前'
    offline = health.connection_check({'online': False, 'last_heartbeat': now - 7200}, now)
    assert offline['status'] == 'error' and offline['message'].endswith('（2 小时前）')
    old = health.version_check({'ver': '2.4.1', 'online': True, 'ai_tools': True}, '2.6.0')
    assert old['status'] == 'warning' and '当前 2.4.1，推荐 2.6.0' in old['message']
    assert 'AI 查询数据库 / Redis / ES' in old['message'] and 'AI 经 Agent 执行' not in old['message']
    assert health.version_check({'ver': '2.6.0'}, '2.6.0')['status'] == 'success'
    assert health.version_check({'ver': '2.10.0'}, '2.6.0')['status'] == 'success'
    assert health.version_check({'ver': 'unknown'}, '2.6.0')['status'] == 'skipped'
    assert health.version_check({'ver': 'my-build'}, '2.6.0')['status'] == 'warning'


def pod(name, ready=True, restarts=0, reason='', namespace='kubedoor', kind='ReplicaSet'):
    """Agent /api/agent/pods 返回的一项"""
    container = {'name': 'main', 'ready': ready, 'is_init': False, 'restart_count': restarts,
                 'state': 'Running' if ready else 'Waiting', 'state_info': {'reason': reason} if reason else {},
                 'last_state': {'terminated': {'reason': 'OOMKilled'}} if restarts else {}}
    return {'name': name, 'namespace': namespace, 'status': 'Running', 'containers': [container],
            'restart_count': restarts, 'controlled_by': kind}


ALL_SOURCES = {'kube-state-metrics', 'node-exporter', 'cAdvisor'}


def test_components_are_recognised_by_pod_name_wherever_they_run():
    assert health.component_of('kubedoor-agent-7d9f8c6b5-x2x7k') == 'kubedoor-agent'
    assert health.component_of('vmagent-vm-k8s-stack-5c6d-x2x7k') == 'vmagent'
    assert health.component_of('prometheus-kube-state-metrics-6f7d8-x2x7k') == 'kube-state-metrics'
    assert health.component_of('kps-prometheus-node-exporter-x2x7k') == 'node-exporter'
    assert health.component_of('prometheus-k8s-0') == 'prometheus'
    assert health.component_of('prometheus-server-7c9d-x2x7k') == 'prometheus'
    for other in ('prometheus-operator-55f-x2x7k', 'prometheus-adapter-1-x', 'alertmanager-main-0', 'kubedoor-master-1'):
        assert health.component_of(other) is None
    assert health.workload_of(pod('kube-state-metrics-6b8f9c-x7k2p')) == 'kube-state-metrics'
    assert health.workload_of(pod('node-exporter-x7k2p', kind='DaemonSet')) == 'node-exporter'
    assert health.workload_of(pod('prometheus-k8s-0', kind='StatefulSet')) == 'prometheus-k8s'


def check_component(name, pods, sources=ALL_SOURCES, own='kubedoor'):
    return health.component_check(name, health.locate(pods), own, sources)


def test_component_in_another_namespace_says_where_it_is_and_whether_it_works():
    own = check_component('kube-state-metrics', [pod('kube-state-metrics-6b8f9c-x7k2p')])
    assert own['status'] == 'success'
    assert own['message'] == 'kubedoor/kube-state-metrics 1/1 个 Pod 就绪；时序库里有它的指标'
    elsewhere = check_component('node-exporter', [pod('prometheus-node-exporter-a1b2c', namespace='monitoring', kind='DaemonSet'),
                                                  pod('prometheus-node-exporter-d3e4f', namespace='monitoring', kind='DaemonSet')])
    assert elsewhere['status'] == 'success'
    assert elsewhere['message'] == ('不在 Agent 的命名空间 kubedoor，集群里已有的：monitoring/prometheus-node-exporter '
                                    '2/2 个 Pod 就绪；时序库里有它的指标')
    # Pod 正常但时序库里没有它的指标：采集没抓它
    unscraped = check_component('kube-state-metrics', [pod('kube-state-metrics-6b8f9c-x7k2p', namespace='monitoring')],
                                sources={'cAdvisor'})
    assert unscraped['status'] == 'warning' and '但时序库最近 10 分钟没有它的指标' in unscraped['message']
    # 时序库查不了：只说 Pod 的情况
    unknown = check_component('kube-state-metrics', [pod('kube-state-metrics-6b8f9c-x7k2p')], sources=None)
    assert unknown['message'] == 'kubedoor/kube-state-metrics 1/1 个 Pod 就绪'


def test_component_readiness_restarts_and_why_pods_are_stuck():
    restarted = check_component('vmagent', [pod('vmagent-7c9d-x2x7k', restarts=6)])
    assert restarted['status'] == 'warning' and '累计重启 6 次' in restarted['message']
    stuck = check_component('kubedoor-agent', [pod('kubedoor-agent-5f6-a1b2c', ready=False, restarts=2, reason='CrashLoopBackOff')])
    assert stuck['status'] == 'error'
    assert '没就绪：kubedoor-agent-5f6-a1b2c：main CrashLoopBackOff，上次退出：OOMKilled' in stuck['message']
    two = check_component('kubedoor-agent', [pod('kubedoor-agent-5f6-a1b2c'), pod('kubedoor-agent-7d8-c3d4e', namespace='old')])
    assert two['status'] == 'warning' and '2 个命名空间装了 kubedoor-agent（kubedoor、old）' in two['message']


def test_missing_components_and_the_collector_in_use():
    prometheus = check_component('vmagent', [pod('prometheus-k8s-0', namespace='monitoring', kind='StatefulSet'),
                                             pod('prometheus-k8s-1', namespace='monitoring', kind='StatefulSet')])
    assert prometheus['status'] == 'success'
    assert prometheus['message'] == ('集群里没有 vmagent，采集用的是 Prometheus：不在 Agent 的命名空间 kubedoor，集群里已有的：'
                                     'monitoring/prometheus-k8s 2/2 个 Pod 就绪；时序库里有这个集群的指标')
    outside = check_component('vmagent', [])
    assert outside['status'] == 'success' and '由集群外的采集写入' in outside['message']
    nothing = check_component('vmagent', [], sources=set())
    assert nothing['status'] == 'warning' and '没有采集在工作' in nothing['message']
    external = check_component('kube-state-metrics', [])
    assert external['status'] == 'success' and '由集群外的采集提供' in external['message']
    absent = check_component('node-exporter', [], sources={'cAdvisor'})
    assert absent['status'] == 'warning' and absent['message'].endswith('节点的监控没有数据')
    assert check_component('kubedoor-agent', [])['status'] == 'warning'


def test_events_metrics_and_kubeconfig_checks():
    now = time.time()
    assert health.events_check(None, now)['status'] == 'warning'
    recent = datetime.fromtimestamp(now - 150, timezone.utc)
    assert health.events_check(recent, now)['message'] == '最新的事件在 2 分钟前'
    assert health.events_check(datetime.fromtimestamp(now - 7200, timezone.utc), now)['status'] == 'warning'
    everything = {'kube-state-metrics', 'node-exporter', 'cAdvisor'}
    assert health.metrics_check(everything, 'origin_prometheus', ENV)['status'] == 'success'
    partial = health.metrics_check({'cAdvisor'}, 'origin_prometheus', ENV)
    assert partial['status'] == 'warning' and partial['message'].startswith('最近 10 分钟没有 kube-state-metrics、node-exporter 的指标')
    assert health.metrics_check(set(), 'origin_prometheus', ENV)['status'] == 'error'
    assert '自动配置' in health.kubeconfig_check({'origin': 'agent-install'})['message']
    assert 'context：admin@prod' in health.kubeconfig_check({'origin': 'upload', 'context': 'admin@prod'})['message']
    assert health.kubeconfig_check(None)['status'] == 'warning'
    assert '连不上 kubedoor-ai' in health.kubeconfig_check('连不上 kubedoor-ai，查不到')['message']


def test_metrics_sources_are_counted_in_one_query():
    query = probe.sources_query('origin_prometheus', ENV, health.METRIC_SOURCES, '10m')
    assert query.count(' or ') == 2
    assert ('label_replace(count(last_over_time(kube_pod_info{origin_prometheus="production"}[10m])), '
            '"kubedoor_source", "kube-state-metrics", "", "")') in query


class Agent:
    """假的 master → Agent 调用：和真的 Agent 一样，没带 env 的请求回「缺少必要参数: env」。
    replies 的值是回包、异常，或者按 query 决定回包的函数"""

    def __init__(self, replies):
        self.replies, self.calls = replies, []

    async def __call__(self, env, path, query=None, timeout=None):
        query = dict(query or {})
        self.calls.append((path, query))
        if query.get('env') != env:
            return {'error': '缺少必要参数: env'}
        reply = self.replies[path]
        if callable(reply):
            reply = reply(query)
        if isinstance(reply, Exception):
            raise reply
        return copy.deepcopy(reply)


CLUSTER_PODS = [pod('kubedoor-agent-5f6c7d-a1b2c', namespace='ops'), pod('vmagent-7c9d8e-x2x7k', namespace='ops'),
                pod('kube-state-metrics-6b8f9c-x7k2p', namespace='monitoring'),
                pod('node-exporter-a1b2c', namespace='monitoring', kind='DaemonSet'),
                pod('node-exporter-d3e4f', ready=False, reason='ImagePullBackOff', namespace='monitoring', kind='DaemonSet'),
                pod('kubedoor-master-7d8e9f-c3d4e', namespace='kubedoor'), pod('order-6f7-x1y2z', namespace='b2c')]


def prepare(monkeypatch, settings, sources=ALL_SOURCES):
    monkeypatch.setattr(health.db, 'pg_fetchrow', AsyncMock(return_value={'admission': True, 'admission_namespace': '["a"]'}))
    monkeypatch.setattr(health.db, 'pg_fetchval', AsyncMock(return_value=datetime.now(timezone.utc)))
    settings['prom_url'] = 'http://vm.example.test:8428'
    monkeypatch.setattr(probe, 'cluster_sources', AsyncMock(return_value=sources))
    monkeypatch.setattr(health.HealthCheck, 'connection', AsyncMock(return_value={'env': ENV, 'origin': 'upload', 'context': 'admin@prod'}))


@pytest.mark.asyncio
async def test_health_check_for_an_online_agent(monkeypatch, settings):
    clients = {ENV: {'online': True, 'ver': '2.6.0', 'last_heartbeat': time.time(), 'ai_tools': True,
                     'data_tools': True, 'notify': True}}
    agent = Agent({
        '/api/agent/namespaces': {'success': True, 'data': ['default', 'kubedoor']},
        '/api/agent/pods': {'success': True, 'data': CLUSTER_PODS},
        health.POD_MGR_PROBE: {'success': False, 'error': 'Cannot connect to host 127.0.0.1:81'},
        '/api/admis_switch': {'is_on': False},
    })
    prepare(monkeypatch, settings)
    result = await health.check(ENV, clients, agent, 'kubedoor', 'admin')
    rows = {row['key']: row for row in result['checks']}
    assert [row['key'] for row in result['checks']] == [
        'connection', 'version', 'k8s_api', 'pod_mgr', 'admission', 'component:kubedoor-agent', 'component:vmagent',
        'component:kube-state-metrics', 'component:node-exporter', 'events', 'metrics', 'kubeconfig']
    # 每次调 Agent 都和页面转发一样带 env（以前没带，「列出命名空间」总是报缺少参数）
    assert agent.calls and all(query.get('env') == ENV for _, query in agent.calls)
    assert rows['k8s_api']['status'] == 'success' and '2 个命名空间' in rows['k8s_api']['message']
    assert rows['pod_mgr']['status'] == 'error' and 'Cannot connect' in rows['pod_mgr']['message']
    assert rows['admission']['status'] == 'error' and '关掉再打开' in rows['admission']['message']
    # 全集群列一次 Pod：Agent 实际在 ops，kube-state-metrics、node-exporter 在 monitoring
    assert ('/api/agent/pods', {'env': ENV}) in agent.calls and result['namespace'] == 'ops'
    assert rows['component:kubedoor-agent']['message'] == 'ops/kubedoor-agent 1/1 个 Pod 就绪'
    assert rows['component:vmagent']['message'] == 'ops/vmagent 1/1 个 Pod 就绪；时序库里有这个集群的指标'
    assert rows['component:kube-state-metrics']['status'] == 'success'
    assert '集群里已有的：monitoring/kube-state-metrics 1/1 个 Pod 就绪' in rows['component:kube-state-metrics']['message']
    node = rows['component:node-exporter']
    assert node['status'] == 'error' and 'monitoring/node-exporter 1/2 个 Pod 就绪' in node['message']
    assert 'node-exporter-d3e4f：main ImagePullBackOff' in node['message']
    assert rows['events']['status'] == 'success' and rows['metrics']['status'] == 'success'
    assert result['kubeconfig'] == {'configured': True, 'origin': 'upload', 'context': 'admin@prod'}
    assert result['online'] is True and result['version'] == '2.6.0'


@pytest.mark.asyncio
async def test_too_many_pods_falls_back_to_the_agent_namespace(monkeypatch, settings):
    clients = {ENV: {'online': True, 'ver': '2.6.0', 'last_heartbeat': time.time()}}

    def pods(query):
        if 'namespaces' not in query:
            return {'success': False, 'error': '响应过大(17.2 MB)'}
        return {'success': True, 'data': [p for p in CLUSTER_PODS if p['namespace'] == query['namespaces']]}

    agent = Agent({'/api/agent/namespaces': {'success': True, 'data': []}, '/api/agent/pods': pods,
                   health.POD_MGR_PROBE: {'status': '未找到'}, '/api/admis_switch': {'is_on': True}})
    prepare(monkeypatch, settings)
    rows = {row['key']: row for row in (await health.check(ENV, clients, agent, 'ops', 'admin'))['checks']}
    assert ('/api/agent/pods', {'env': ENV, 'namespaces': 'ops'}) in agent.calls
    message = rows['component:kubedoor-agent']['message']
    assert message.startswith('ops/kubedoor-agent 1/1 个 Pod 就绪')
    assert message.endswith('（全集群的 Pod 列不出来（响应过大(17.2 MB)），只查了命名空间 ops）')


@pytest.mark.asyncio
async def test_health_check_for_an_offline_agent_skips_what_needs_the_agent(monkeypatch, settings):
    clients = {ENV: {'online': False, 'ver': '2.4.0', 'last_heartbeat': time.time() - 90}}
    agent = Agent({})
    monkeypatch.setattr(health.db, 'pg_fetchrow', AsyncMock(side_effect=RuntimeError('db down')))
    monkeypatch.setattr(health.db, 'pg_fetchval', AsyncMock(return_value=None))
    settings['prom_url'] = ''
    monkeypatch.setattr(health.HealthCheck, 'connection', AsyncMock(return_value=None))
    result = await health.check(ENV, clients, agent, 'kubedoor', 'admin')
    rows = {row['key']: row for row in result['checks']}
    assert agent.calls == []
    assert rows['connection']['status'] == 'error'
    assert all(rows[key]['status'] == 'skipped' for key in ('k8s_api', 'pod_mgr', 'admission', 'components', 'metrics'))
    # 查不了的组件合成一行
    assert [row['key'] for row in result['checks'] if row['group'] == 'component'] == ['components']
    assert rows['components']['message'] == 'Agent 离线，没法检查（kubedoor-agent、vmagent、kube-state-metrics、node-exporter）'
    assert rows['events']['status'] == 'warning' and rows['kubeconfig']['status'] == 'warning'
    assert result['kubeconfig'] == {'configured': False} and result['namespace'] == 'kubedoor'


@pytest.mark.asyncio
async def test_one_broken_check_does_not_break_the_others(monkeypatch, settings):
    clients = {ENV: {'online': True, 'ver': '2.6.0', 'last_heartbeat': time.time()}}
    agent = Agent({'/api/agent/namespaces': {'success': True, 'data': []}, '/api/agent/pods': RuntimeError('等待超时'),
                   health.POD_MGR_PROBE: {'status': '未找到'}})
    monkeypatch.setattr(health.db, 'pg_fetchrow', AsyncMock(return_value=None))
    monkeypatch.setattr(health.db, 'pg_fetchval', AsyncMock(side_effect=RuntimeError('timeout')))
    monkeypatch.setattr(health.HealthCheck, 'metrics', AsyncMock(side_effect=ValueError('boom')))
    monkeypatch.setattr(health.HealthCheck, 'connection', AsyncMock(return_value='连不上 kubedoor-ai，查不到'))
    rows = {row['key']: row for row in (await health.check(ENV, clients, agent, 'kubedoor', 'admin'))['checks']}
    assert rows['pod_mgr']['status'] == 'success' and rows['admission']['status'] == 'skipped'
    assert rows['components']['status'] == 'error' and rows['components']['message'].startswith('查不到 Pod：等待超时')
    assert rows['events']['status'] == 'warning' and rows['metrics']['message'] == '检查出错（ValueError）'
    assert rows['kubeconfig']['status'] == 'warning'


def test_new_routes_are_admin_only():
    for method, path in (('GET', '/api/admin/agent-health'), ('POST', '/api/admin/agent-installations/repair')):
        rule = policy.match(method, path)
        assert rule is not policy.RULES[-1] and rule.kind == 'admin'


@pytest.mark.asyncio
async def test_cluster_sources_reads_the_labelled_counts_without_cache():
    seen = {}

    async def query(request):
        seen.update(request.query)
        seen['auth'] = request.headers.get('Authorization', '')
        return web.json_response({'status': 'success', 'data': {'resultType': 'vector', 'result': [
            {'metric': {'kubedoor_source': 'kube-state-metrics'}, 'value': [1, '42']},
            {'metric': {'kubedoor_source': 'cAdvisor'}, 'value': [1, '0']},
        ]}})

    app = web.Application()
    app.router.add_get('/api/v1/query', query)
    async with TestServer(app) as server:
        url = f'http://monit:secret@{server.host}:{server.port}'
        found = await probe.cluster_sources(url, 'origin_prometheus', ENV, health.METRIC_SOURCES)
    assert found == {'kube-state-metrics'}
    assert seen['nocache'] == '1' and seen['auth'].startswith('Basic ')
    assert 'origin_prometheus="production"' in seen['query']
    with pytest.raises(probe.ProbeError):
        await probe.cluster_sources('http://127.0.0.1:9', 'origin_prometheus', ENV, health.METRIC_SOURCES)
