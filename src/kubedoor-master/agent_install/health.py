"""Agent 管理「修复」弹框里的检查：逐项看这个集群的 Agent 和各组件是否正常。

都是只读的：经在线的 Agent 查集群（Agent 离线时这几项跳过），另外查数据库里收到的 K8S 事件、时序库里这个集群的
指标，以及 kubedoor-ai 里有没有保存 AI Kubeconfig（修复时直接用它连接集群）。每一项单独限时、单独兜底，
一项出错只记在这一项里，不影响其他项。经 Agent 的调用都是现有接口，旧版 Agent 也能检查。
"""

import asyncio
import os
import re
import time
from datetime import datetime, timezone

import aiohttp
import db
import general_settings
from general_settings import probe
from loguru import logger

from .bundle import BASE

AI_URL = 'http://kubedoor-ai:8000'
# 没有网页安装记录（脚本装的）时，Agent 所在的命名空间按默认的算（Agent 管理的更新 / 重启也固定用它）
DEFAULT_NAMESPACE = 'kubedoor'
# 组件：(检查名, Pod 名前缀)。kubedoor-agent、vmagent、kube-state-metrics 是 Deployment，node-exporter 是 DaemonSet
COMPONENTS = (('kubedoor-agent', 'kubedoor-agent-'), ('vmagent', 'vmagent-'),
              ('kube-state-metrics', 'kube-state-metrics-'), ('node-exporter', 'node-exporter-'))
# 时序库里各来源的代表指标（不管是网页安装的 vmagent 还是已有的 Prometheus 采的，指标名都一样）
METRIC_SOURCES = (('kube-state-metrics', 'kube_pod_info'), ('node-exporter', 'node_uname_info'),
                  ('cAdvisor', 'container_cpu_usage_seconds_total'))
METRICS_WINDOW = '10m'
# 只看最近这么久的事件：表可能很大，限定时间才能走 lasttimestamp 的索引
EVENTS_WINDOW_HOURS = 24
EVENTS_FRESH_SECONDS = 3600
# 经 Agent 的一次调用最多等多久（秒）；整个检查再加一道总时限
AGENT_TIMEOUT = 20
CHECK_TIMEOUT = 30
# 容器累计重启这么多次就提醒（重启次数从 Pod 创建起累计，没法知道是不是最近）
RESTART_WARNING = 5
POD_MGR_PROBE = '/api/pod/task_status/kubedoor-health-check'

GROUP_AGENT, GROUP_COMPONENT, GROUP_DATA, GROUP_REPAIR = 'agent', 'component', 'data', 'repair'
OFFLINE_SKIP = 'Agent 离线，没法检查'


def item(key, group, name, status, message):
    return {'key': key, 'group': group, 'name': name, 'status': status, 'message': message}


def components_row(status, message):
    """四个组件都查不了时合成的一行"""
    return item('components', GROUP_COMPONENT, '各组件', status, f'{message}（{"、".join(name for name, _ in COMPONENTS)}）')


def ago(seconds):
    """多久以前：秒 / 分钟 / 小时 / 天"""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f'{seconds} 秒前'
    if seconds < 3600:
        return f'{seconds // 60} 分钟前'
    if seconds < 86400:
        return f'{seconds // 3600} 小时前'
    return f'{seconds // 86400} 天前'


def version_tuple(value):
    """2.6.0 → (2, 6, 0)；不是纯数字的版本号（自己打的镜像标签之类）返回 None，只能比较是否相同"""
    if not isinstance(value, str) or not re.fullmatch(r'v?\d+(\.\d+)*', value.strip()):
        return None
    return tuple(int(part) for part in value.strip().lstrip('v').split('.'))


def connection_check(client, now):
    if not client:
        return item('connection', GROUP_AGENT, '连接控制端', 'error',
                    '离线：控制端这次启动以来 Agent 还没有连上过')
    beat = client.get('last_heartbeat') or 0
    when = datetime.fromtimestamp(beat).strftime('%Y-%m-%d %H:%M:%S') if beat else '未知'
    if client.get('online'):
        return item('connection', GROUP_AGENT, '连接控制端', 'success', f'在线，最后心跳 {ago(now - beat)}')
    return item('connection', GROUP_AGENT, '连接控制端', 'error', f'离线，最后心跳 {when}（{ago(now - beat)}）')


def version_check(client, recommended):
    version = str((client or {}).get('ver') or '')
    if not version or version == 'unknown':
        return item('version', GROUP_AGENT, 'Agent 版本', 'skipped', '不知道当前版本：Agent 连上控制端后才知道')
    current, target = version_tuple(version), version_tuple(recommended)
    if current is not None and target is not None and current < target:
        missing = []
        if client.get('online'):
            if not client.get('ai_tools'):
                missing.append('AI 经 Agent 执行')
            if not client.get('data_tools'):
                missing.append('AI 查询数据库 / Redis / ES')
            if not client.get('notify'):
                missing.append('运维操作通知按集群选群发送')
        tail = f'；旧版不支持：{"、".join(missing)}' if missing else ''
        return item('version', GROUP_AGENT, 'Agent 版本', 'warning',
                    f'当前 {version}，推荐 {recommended}，可以在版本号菜单里「更新」{tail}')
    if current is None and version != recommended:
        return item('version', GROUP_AGENT, 'Agent 版本', 'warning', f'当前 {version}，推荐 {recommended}')
    return item('version', GROUP_AGENT, 'Agent 版本', 'success', f'当前 {version}（推荐 {recommended}）')


def agent_error(response):
    """经 Agent 调用失败时的原因：agent 回的 {success: false, error / message / data}"""
    if isinstance(response, dict):
        for key in ('error', 'message', 'data'):
            value = response.get(key)
            if isinstance(value, str) and value:
                return ' '.join(value.split())[:200]
    return '返回的格式不对'


def pod_ready(pod):
    containers = [c for c in pod.get('containers') or [] if not c.get('is_init')]
    return pod.get('status') == 'Running' and bool(containers) and all(c.get('ready') for c in containers)


def pod_problem(pod):
    """没就绪的 Pod 卡在哪：容器的等待原因（CrashLoopBackOff、ImagePullBackOff 等）或退出原因"""
    reasons = []
    for container in pod.get('containers') or []:
        if container.get('ready'):
            continue
        info = container.get('state_info') or {}
        reason = info.get('reason') or (info.get('terminated') or {}).get('reason') or container.get('state') or ''
        last = ((container.get('last_state') or {}).get('terminated') or {}).get('reason')
        if last and last != reason:
            reason = f'{reason}，上次退出：{last}' if reason else f'上次退出：{last}'
        if reason:
            reasons.append(f'{container.get("name")} {reason}')
    status = pod.get('status') or 'Unknown'
    detail = '、'.join(reasons) if reasons else status
    return f'{pod.get("name")}：{detail}'


def component_check(name, pods, namespace):
    key = f'component:{name}'
    if not pods:
        if name == 'kubedoor-agent':
            return item(key, GROUP_COMPONENT, name, 'warning',
                        f'命名空间 {namespace} 里没有找到 kubedoor-agent 的 Pod（Agent 可能装在别的命名空间）')
        return item(key, GROUP_COMPONENT, name, 'skipped',
                    f'命名空间 {namespace} 里没有：没有随 Agent 安装（用集群里已有的，或者不需要）')
    ready = sum(1 for pod in pods if pod_ready(pod))
    restarts = sum(int(pod.get('restart_count') or 0) for pod in pods)
    text = f'{ready}/{len(pods)} 个 Pod 就绪' + (f'，累计重启 {restarts} 次' if restarts else '')
    problems = [pod_problem(pod) for pod in pods if not pod_ready(pod)]
    if problems:
        shown = '；'.join(problems[:3]) + (f' 等 {len(problems)} 个' if len(problems) > 3 else '')
        return item(key, GROUP_COMPONENT, name, 'error', f'{text}。{shown}')
    if restarts >= RESTART_WARNING:
        return item(key, GROUP_COMPONENT, name, 'warning', text)
    return item(key, GROUP_COMPONENT, name, 'success', text)


def events_check(latest, now):
    """latest 是最近 EVENTS_WINDOW_HOURS 小时里这个集群最新事件的时间（没有为 None）"""
    if latest is None:
        return item('events', GROUP_DATA, 'K8S 事件上报', 'warning',
                    f'最近 {EVENTS_WINDOW_HOURS} 小时没有收到这个集群的 K8S 事件（Agent 离线或事件监听异常；集群很安静时也可能这样）')
    seconds = now - latest.timestamp()
    if seconds <= EVENTS_FRESH_SECONDS:
        return item('events', GROUP_DATA, 'K8S 事件上报', 'success', f'最新的事件在 {ago(seconds)}')
    return item('events', GROUP_DATA, 'K8S 事件上报', 'warning',
                f'最新的事件在 {ago(seconds)}（集群很安静时也可能这样）')


def metrics_check(found, tag_key, env):
    names = [name for name, _ in METRIC_SOURCES]
    missing = [name for name in names if name not in found]
    if not missing:
        return item('metrics', GROUP_DATA, '指标采集', 'success',
                    f'最近 10 分钟 {"、".join(names)} 都有 {tag_key}="{env}" 的指标')
    if len(missing) == len(names):
        return item('metrics', GROUP_DATA, '指标采集', 'error',
                    f'时序库最近 10 分钟没有 {tag_key}="{env}" 的指标：采集没在工作，或 external_labels 里的集群名不是 {env}')
    return item('metrics', GROUP_DATA, '指标采集', 'warning',
                f'最近 10 分钟没有 {"、".join(missing)} 的指标（{"、".join(n for n in names if n in found)} 正常），对应的监控页面没有数据')


def kubeconfig_check(connection):
    """connection：kubedoor-ai 里这个集群的 AI Kubeconfig 摘要；None 表示没保存；字符串表示查不到的原因"""
    if isinstance(connection, str):
        return item('kubeconfig', GROUP_REPAIR, 'AI Kubeconfig', 'warning',
                    f'{connection}；修复时取不到就需要上传 kubeconfig')
    if not connection:
        return item('kubeconfig', GROUP_REPAIR, 'AI Kubeconfig', 'warning',
                    '没有保存：修复时需要上传这个集群的 kubeconfig')
    source = ('安装 Agent 时自动配置（Agent 服务账号）' if connection.get('origin') == 'agent-install'
              else f'手动上传（context：{connection.get("context") or "未知"}）')
    return item('kubeconfig', GROUP_REPAIR, 'AI Kubeconfig', 'success', f'已保存，{source}；修复时直接用它连接集群')


class HealthCheck:
    """一次检查。call_agent 是 master 内部调 Agent 的函数（kubedoor-master.py 的 call_agent_api）"""

    def __init__(self, env, clients, call_agent, namespace, username):
        self.env, self.clients, self.call_agent = env, clients, call_agent
        self.namespace, self.username = namespace, username
        self.client = clients.get(env)
        self.online = bool(self.client and self.client.get('online'))

    async def agent(self, path, query=None):
        """经 Agent 调一次；失败抛 RuntimeError（说明给页面看）"""
        try:
            response = await self.call_agent(self.env, path, query=query or {}, timeout=AGENT_TIMEOUT)
        except Exception as exc:
            raise RuntimeError(str(exc) or type(exc).__name__) from None
        if isinstance(response, dict) and response.get('success') is False:
            raise RuntimeError(agent_error(response))
        return response

    async def k8s_api(self):
        if not self.online:
            return item('k8s_api', GROUP_AGENT, 'K8S API 访问', 'skipped', OFFLINE_SKIP)
        started = time.monotonic()
        try:
            response = await self.agent('/api/agent/namespaces')
        except RuntimeError as exc:
            return item('k8s_api', GROUP_AGENT, 'K8S API 访问', 'error', f'Agent 访问 K8S API 失败：{exc}')
        names = response.get('data') if isinstance(response, dict) else None
        if not isinstance(names, list):
            return item('k8s_api', GROUP_AGENT, 'K8S API 访问', 'error', f'Agent 访问 K8S API 失败：{agent_error(response)}')
        return item('k8s_api', GROUP_AGENT, 'K8S API 访问', 'success',
                    f'正常，能列出 {len(names)} 个命名空间（用时 {time.monotonic() - started:.1f} 秒）')

    async def pod_mgr(self):
        name = 'Pod 管理服务'
        if not self.online:
            return item('pod_mgr', GROUP_AGENT, name, 'skipped', OFFLINE_SKIP)
        try:
            response = await self.agent(POD_MGR_PROBE)
        except RuntimeError as exc:
            return item('pod_mgr', GROUP_AGENT, name, 'error',
                        f'没有响应（{exc}）：Pod 日志下载、删除 / 隔离 Pod、Java 诊断等功能不可用')
        if isinstance(response, dict) and 'status' in response:
            return item('pod_mgr', GROUP_AGENT, name, 'success', '正常响应')
        return item('pod_mgr', GROUP_AGENT, name, 'error', f'返回不对（{agent_error(response)}）')

    async def admission(self, settings):
        name = '准入控制'
        if not settings or not settings.get('admission'):
            return item('admission', GROUP_AGENT, name, 'skipped', '没有开启')
        if not self.online:
            return item('admission', GROUP_AGENT, name, 'skipped', f'已开启；{OFFLINE_SKIP}')
        try:
            response = await self.agent('/api/admis_switch', {'action': 'get'})
        except RuntimeError as exc:
            return item('admission', GROUP_AGENT, name, 'error', f'查不到 webhook 配置：{exc}')
        if isinstance(response, dict) and response.get('is_on') is True:
            return item('admission', GROUP_AGENT, name, 'success', '已开启，集群里的 webhook 配置正常')
        if isinstance(response, dict) and response.get('is_on') is False:
            return item('admission', GROUP_AGENT, name, 'error',
                        '页面上开着准入控制，但集群里没有 webhook 配置（kubedoor-admis-configuration），'
                        '资源管控不生效：把「准入控制」开关关掉再打开可以恢复')
        return item('admission', GROUP_AGENT, name, 'error', f'查不到 webhook 配置：{agent_error(response)}')

    async def components(self):
        """每个组件一行；查不了（Agent 离线、查 Pod 失败）时合成一行，免得四行写同一句话"""
        if not self.online:
            return [components_row('skipped', OFFLINE_SKIP)]
        try:
            response = await self.agent('/api/agent/pods', {'namespaces': self.namespace})
        except RuntimeError as exc:
            return [components_row('error', f'查不到命名空间 {self.namespace} 里的 Pod：{exc}')]
        pods = response.get('data') if isinstance(response, dict) else None
        if not isinstance(pods, list):
            return [components_row('error', f'查不到命名空间 {self.namespace} 里的 Pod：{agent_error(response)}')]
        return [component_check(name, [p for p in pods if str(p.get('name') or '').startswith(prefix)], self.namespace)
                for name, prefix in COMPONENTS]

    async def events(self):
        try:
            latest = await db.pg_fetchval(
                'SELECT lasttimestamp FROM k8s_events WHERE k8s=$1 '
                f"AND lasttimestamp > now() - interval '{EVENTS_WINDOW_HOURS} hours' ORDER BY lasttimestamp DESC LIMIT 1",
                self.env, timeout=10)
        except Exception as exc:
            logger.warning(f'检查 Agent：查询 {self.env} 的 K8S 事件失败：{type(exc).__name__}')
            return item('events', GROUP_DATA, 'K8S 事件上报', 'warning', f'查询数据库失败（{type(exc).__name__}）')
        return events_check(latest, time.time())

    async def metrics(self):
        url, tag_key = general_settings.value('prom_url'), general_settings.value('prom_k8s_tag_key')
        if not url:
            return item('metrics', GROUP_DATA, '指标采集', 'skipped', '通用配置里还没有时序库查询地址')
        try:
            found = await probe.cluster_sources(url, tag_key, self.env, METRIC_SOURCES, window=METRICS_WINDOW)
        except probe.ProbeError as exc:
            return item('metrics', GROUP_DATA, '指标采集', 'warning', f'没能查询时序库：{exc}')
        return metrics_check(found, tag_key, self.env)

    async def connection(self):
        """kubedoor-ai 里这个集群保存的 AI Kubeconfig 摘要（不取内容）：dict / None（没保存）/ 查不到的原因"""
        internal = os.environ.get('AI_INTERNAL_TOKEN', '')
        if not internal:
            return '控制端没有配置 AI 内部令牌（AI_INTERNAL_TOKEN），查不到'
        headers = {'X-Kubedoor-Token': internal, 'X-User-Name': self.username or 'kubedoor', 'X-User-Permission': 'admin'}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10, connect=5)) as session:
                async with session.get(f'{AI_URL}/api/ai/internal/connections-summary', headers=headers,
                                       allow_redirects=False) as response:
                    if response.status != 200:
                        return f'kubedoor-ai 返回 HTTP {response.status}，查不到'
                    data = await response.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return '连不上 kubedoor-ai，查不到'
        rows = data.get('connections') if isinstance(data, dict) else None
        if not isinstance(rows, list):
            return 'kubedoor-ai 返回的格式不对，查不到'
        return next((row for row in rows if isinstance(row, dict) and row.get('env') == self.env), None)

    async def run(self):
        """全部检查，按显示顺序返回；kubeconfig 是修复时要用的 AI Kubeconfig 摘要"""
        settings = await agent_settings(self.env)
        results = await asyncio.gather(
            guarded(self.k8s_api(), 'k8s_api', GROUP_AGENT, 'K8S API 访问'),
            guarded(self.pod_mgr(), 'pod_mgr', GROUP_AGENT, 'Pod 管理服务'),
            guarded(self.admission(settings), 'admission', GROUP_AGENT, '准入控制'),
            guarded(self.components(), 'components', GROUP_COMPONENT, '各组件'),
            guarded(self.events(), 'events', GROUP_DATA, 'K8S 事件上报'),
            guarded(self.metrics(), 'metrics', GROUP_DATA, '指标采集'),
            self.connection_summary())
        k8s_api, pod_mgr, admission, components, events, metrics, connection = results
        now = time.time()
        checks = [connection_check(self.client, now), version_check(self.client, BASE['agent_tag']),
                  k8s_api, pod_mgr, admission, *components, events, metrics, kubeconfig_check(connection)]
        kubeconfig = ({'configured': True, 'origin': connection.get('origin') or 'upload', 'context': connection.get('context') or ''}
                      if isinstance(connection, dict) else {'configured': False})
        return checks, kubeconfig

    async def connection_summary(self):
        try:
            return await asyncio.wait_for(self.connection(), CHECK_TIMEOUT)
        except Exception as exc:
            return f'查询 kubedoor-ai 失败（{type(exc).__name__}）'


async def guarded(coroutine, key, group, name):
    """一项检查单独限时、单独兜底：超时或意外出错只记在这一项"""
    try:
        return await asyncio.wait_for(coroutine, CHECK_TIMEOUT)
    except asyncio.TimeoutError:
        failure = item(key, group, name, 'error', f'检查超时（{CHECK_TIMEOUT} 秒）')
    except Exception as exc:
        logger.warning(f'检查 Agent 的「{name}」出错：{type(exc).__name__}: {exc}')
        failure = item(key, group, name, 'error', f'检查出错（{type(exc).__name__}）')
    if key == 'components':
        return [components_row(failure['status'], failure['message'])]
    return failure


async def agent_settings(env):
    """k8s_agent_status 里这个集群的准入控制开关；查不到返回 None"""
    try:
        row = await db.pg_fetchrow('SELECT admission, admission_namespace FROM k8s_agent_status WHERE env=$1', env, timeout=10)
    except Exception as exc:
        logger.warning(f'检查 Agent：读取 {env} 的 Agent 设置失败：{type(exc).__name__}')
        return None
    return dict(row) if row else None


async def registered(env, clients):
    """这个集群接入过 KubeDoor：连过控制端（k8s_agent_status 有它），或这次启动以来连上过"""
    if env in clients:
        return True
    return bool(await db.pg_fetchval('SELECT 1 FROM k8s_agent_status WHERE env=$1', env, timeout=10))


async def check(env, clients, call_agent, namespace, username):
    """GET /api/admin/agent-health 的返回"""
    checks, kubeconfig = await HealthCheck(env, clients, call_agent, namespace, username).run()
    client = clients.get(env) or {}
    return {
        'env': env, 'online': bool(client.get('online')), 'version': str(client.get('ver') or ''),
        'recommended_version': BASE['agent_tag'], 'namespace': namespace,
        'checked_at': datetime.now(timezone.utc).isoformat(), 'checks': checks, 'kubeconfig': kubeconfig,
    }
