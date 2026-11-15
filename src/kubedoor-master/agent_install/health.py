"""Agent 管理「修复」弹框里的检查：逐项看这个集群的 Agent 和各组件是否正常。

都是只读的：经在线的 Agent 查集群（Agent 离线时这几项跳过），另外查数据库里收到的 K8S 事件、时序库里这个集群的
指标，以及 kubedoor-ai 里有没有保存 AI Kubeconfig（修复时直接用它连接集群）。每一项单独限时、单独兜底，
一项出错只记在这一项里，不影响其他项。经 Agent 的调用都是现有接口（和页面转发一样带 env），旧版 Agent 也能检查。

组件不只看 Agent 的命名空间：采集组件可能没随 Agent 安装、用的是集群里已有的（例如 monitoring 里的
kube-state-metrics），所以全集群列一次 Pod、按名字认，说明装在哪个命名空间、Pod 是否就绪，再对照时序库里
有没有它的指标判断是否正常。
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
# 没有网页安装记录（脚本装的）、集群里也没找到 kubedoor-agent 的 Pod 时，Agent 的命名空间按默认的算
DEFAULT_NAMESPACE = 'kubedoor'
# 检查的组件（按 Pod 名认，见 component_of）；vmagent 那一行在没有 vmagent 时说明用的是哪个 Prometheus
COMPONENTS = ('kubedoor-agent', 'vmagent', 'kube-state-metrics', 'node-exporter')
# Prometheus 服务端的 Pod：名字带 prometheus，但不是各种 exporter / operator / adapter 之类
PROMETHEUS_EXCLUDE = ('exporter', 'operator', 'adapter', 'alertmanager', 'pushgateway', 'kube-state-metrics',
                      'blackbox', 'grafana', 'webhook')
# 时序库里各来源的代表指标（不管是网页安装的 vmagent 还是已有的 Prometheus 采的，指标名都一样）
METRIC_SOURCES = (('kube-state-metrics', 'kube_pod_info'), ('node-exporter', 'node_uname_info'),
                  ('cAdvisor', 'container_cpu_usage_seconds_total'))
# 组件和它在时序库里对应的来源
COMPONENT_SOURCES = {'kube-state-metrics': 'kube-state-metrics', 'node-exporter': 'node-exporter'}
COMPONENT_ROLES = {'kube-state-metrics': 'K8S 对象（Pod 数、副本数等）', 'node-exporter': '节点'}
# 一个组件在集群里有多套时最多列几套
SHOWN_WORKLOADS = 3
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
STATUS_ORDER = {'success': 0, 'skipped': 0, 'warning': 1, 'error': 2}


def item(key, group, name, status, message):
    return {'key': key, 'group': group, 'name': name, 'status': status, 'message': message}


def components_row(status, message):
    """四个组件都查不了时合成的一行"""
    return item('components', GROUP_COMPONENT, '各组件', status, f'{message}（{"、".join(COMPONENTS)}）')


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


def component_of(pod_name):
    """Pod 属于哪个组件（按名字认，不管装在哪个命名空间）：COMPONENTS 里的名字，或者 'prometheus'
    （没有 vmagent 时用作采集的 Prometheus 服务端）；都不是返回 None。

    例如 prometheus-kube-state-metrics-xxx 是 kube-state-metrics，prometheus-node-exporter-xxx 是 node-exporter，
    prometheus-k8s-0 是 prometheus，prometheus-operator-xxx 不算
    """
    name = str(pod_name or '').lower()
    if name.startswith('kubedoor-agent-'):
        return 'kubedoor-agent'
    for component in ('vmagent', 'kube-state-metrics', 'node-exporter'):
        if component in name:
            return component
    if 'prometheus' in name and not any(word in name for word in PROMETHEUS_EXCLUDE):
        return 'prometheus'
    return None


def workload_of(pod):
    """Pod 所属的工作负载名：Deployment 的 Pod 去掉 ReplicaSet 和 Pod 的两段后缀，DaemonSet / StatefulSet 去掉一段"""
    name = str(pod.get('name') or '')
    kind = pod.get('controlled_by')
    if kind == 'ReplicaSet':
        return name.rsplit('-', 2)[0]
    if kind in ('DaemonSet', 'StatefulSet'):
        return name.rsplit('-', 1)[0]
    return name


def locate(pods):
    """集群里的 Pod → {组件: {(命名空间, 工作负载): [Pod, ...]}}"""
    found = {}
    for pod in pods:
        component = component_of(pod.get('name'))
        if component:
            key = (str(pod.get('namespace') or ''), workload_of(pod))
            found.setdefault(component, {}).setdefault(key, []).append(pod)
    return found


def agent_namespace(found, default):
    """Agent 实际装在哪个命名空间：集群里只有一个命名空间有 kubedoor-agent 的 Pod 就是它，否则用 default"""
    spaces = sorted({namespace for namespace, _ in (found or {}).get('kubedoor-agent', {})})
    return spaces[0] if len(spaces) == 1 else default


def workload_state(namespace, workload, pods):
    """一套工作负载的状态：(说明, 没就绪的 Pod 卡在哪, 累计重启次数)"""
    ready = sum(1 for pod in pods if pod_ready(pod))
    restarts = sum(int(pod.get('restart_count') or 0) for pod in pods)
    text = f'{namespace}/{workload} {ready}/{len(pods)} 个 Pod 就绪' + (f'，累计重启 {restarts} 次' if restarts else '')
    return text, [pod_problem(pod) for pod in pods if not pod_ready(pod)], restarts


def worse(status, other):
    return other if STATUS_ORDER[other] > STATUS_ORDER[status] else status


def describe(groups, own_namespace):
    """组件在集群里的各套（Agent 的命名空间里的排前面）→ (状态, 说明)"""
    ordered = sorted(groups.items(), key=lambda pair: (pair[0][0] != own_namespace, pair[0]))
    texts, problems, restarts = [], [], 0
    for (namespace, workload), pods in ordered[:SHOWN_WORKLOADS]:
        text, stuck, count = workload_state(namespace, workload, pods)
        texts.append(text)
        problems += stuck
        restarts = max(restarts, count)
    message = '；'.join(texts) + (f' 等 {len(ordered)} 套' if len(ordered) > SHOWN_WORKLOADS else '')
    if not any(namespace == own_namespace for namespace, _ in groups):
        message = f'不在 Agent 的命名空间 {own_namespace}，集群里已有的：{message}'
    if problems:
        shown = '；'.join(problems[:3]) + (f' 等 {len(problems)} 个' if len(problems) > 3 else '')
        return 'error', f'{message}。没就绪：{shown}'
    return ('warning' if restarts >= RESTART_WARNING else 'success'), message


def component_check(name, found, own_namespace, sources):
    """一个组件一行：装在哪、Pod 是否就绪，再对照时序库里有没有它的指标。

    found 是 locate 的结果；sources 是时序库里查到的指标来源（查不了为 None）
    """
    key = f'component:{name}'
    groups = found.get(name) or {}
    source = COMPONENT_SOURCES.get(name)
    if name == 'vmagent' and not groups and found.get('prometheus'):
        # 没有 vmagent：指标由集群里已有的 Prometheus 采集
        status, message = describe(found['prometheus'], own_namespace)
        message = f'集群里没有 vmagent，采集用的是 Prometheus：{message}'
        return item(key, GROUP_COMPONENT, name, *with_collector_metrics(status, message, sources))
    if not groups:
        return item(key, GROUP_COMPONENT, name, *missing_component(name, sources))
    status, message = describe(groups, own_namespace)
    if name == 'kubedoor-agent':
        spaces = sorted({namespace for namespace, _ in groups})
        if len(spaces) > 1:
            status = worse(status, 'warning')
            message += f'。集群里有 {len(spaces)} 个命名空间装了 kubedoor-agent（{"、".join(spaces)}），可能同时连着两套控制端'
        return item(key, GROUP_COMPONENT, name, status, message)
    if name == 'vmagent':
        return item(key, GROUP_COMPONENT, name, *with_collector_metrics(status, message, sources))
    if sources is not None and source not in sources:
        status = worse(status, 'warning')
        message += (f'；但时序库最近 10 分钟没有它的指标：采集没有抓它，或采集的 external_labels '
                    f'里集群名不对，{COMPONENT_ROLES[name]}的监控没有数据')
    elif sources is not None:
        message += '；时序库里有它的指标'
    return item(key, GROUP_COMPONENT, name, status, message)


def with_collector_metrics(status, message, sources):
    """采集器（vmagent / Prometheus）：时序库里一个来源都没有时说明它没把这个集群的指标写进来"""
    if sources is not None and not sources:
        return worse(status, 'warning'), message + '；但时序库最近 10 分钟没有这个集群的指标：检查它的远程写地址和 external_labels'
    if sources:
        return status, message + '；时序库里有这个集群的指标'
    return status, message


def missing_component(name, sources):
    """集群里没找到这个组件的 Pod → (状态, 说明)"""
    if name == 'kubedoor-agent':
        return 'warning', '集群里没有找到名为 kubedoor-agent-* 的 Pod（Agent 在线，可能部署时改了名字）'
    if name == 'vmagent':
        if sources:
            return 'success', '集群里没有 vmagent 或 Prometheus 的 Pod：指标由集群外的采集写入（时序库里有这个集群的指标）'
        if sources is None:
            return 'warning', '集群里没有 vmagent 或 Prometheus 的 Pod：指标要由集群外的采集抓取，时序库查不了，没法确认'
        return 'warning', '集群里没有 vmagent 或 Prometheus 的 Pod，时序库里也没有这个集群的指标：没有采集在工作'
    source = COMPONENT_SOURCES[name]
    if sources and source in sources:
        return 'success', f'集群里没有找到 {name} 的 Pod，但时序库里有它的指标（由集群外的采集提供）'
    if sources is None:
        return 'warning', f'集群里没有找到 {name} 的 Pod，时序库查不了，没法确认{COMPONENT_ROLES[name]}的指标有没有来源'
    return 'warning', f'集群里没有找到 {name} 的 Pod，时序库里也没有它的指标：{COMPONENT_ROLES[name]}的监控没有数据'


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
        """经 Agent 调一次；和页面经 master 转发一样带上 env（Agent 的不少接口没有 env 直接报「缺少必要参数」）。
        失败抛 RuntimeError（说明给页面看）"""
        try:
            response = await self.call_agent(self.env, path, query={'env': self.env, **(query or {})}, timeout=AGENT_TIMEOUT)
        except Exception as exc:
            raise RuntimeError(str(exc) or type(exc).__name__) from None
        if isinstance(response, dict) and (response.get('success') is False
                                           or (response.get('error') and 'data' not in response)):
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

    async def cluster_pods(self):
        """各组件要看的 Pod：全集群列一次（组件不一定在 Agent 的命名空间）。返回 (Pod 列表, 说明)；
        全集群列不出来（例如 Pod 太多超过回包上限）时退回只列 Agent 的命名空间，说明里写明；都不行抛 RuntimeError"""
        try:
            response = await self.agent('/api/agent/pods')
            note = ''
        except RuntimeError as exc:
            response = await self.agent('/api/agent/pods', {'namespaces': self.namespace})
            note = f'全集群的 Pod 列不出来（{exc}），只查了命名空间 {self.namespace}'
        pods = response.get('data') if isinstance(response, dict) else None
        if not isinstance(pods, list):
            raise RuntimeError(agent_error(response))
        return pods, note

    async def components(self, sources):
        """每个组件一行；查不了（Agent 离线、列不出 Pod）时合成一行，免得四行写同一句话。
        sources 是时序库里查到的指标来源（查不了为 None），用来判断组件是否在正常工作"""
        if not self.online:
            return [components_row('skipped', OFFLINE_SKIP)]
        try:
            pods, note = await asyncio.wait_for(self.cluster_pods(), CHECK_TIMEOUT)
        except RuntimeError as exc:
            return [components_row('error', f'查不到 Pod：{exc}')]
        except asyncio.TimeoutError:
            return [components_row('error', f'查 Pod 超时（{CHECK_TIMEOUT} 秒）')]
        found = locate(pods)
        self.namespace = agent_namespace(found, self.namespace)
        rows = [component_check(name, found, self.namespace, sources) for name in COMPONENTS]
        if note:
            for row in rows:
                row['message'] += f'（{note}）'
        return rows

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
        """返回 (检查结果, 查到的指标来源)；来源在查不了时为 None，组件检查用它判断组件是否在正常工作"""
        url, tag_key = general_settings.value('prom_url'), general_settings.value('prom_k8s_tag_key')
        if not url:
            return item('metrics', GROUP_DATA, '指标采集', 'skipped', '通用配置里还没有时序库查询地址'), None
        try:
            found = await probe.cluster_sources(url, tag_key, self.env, METRIC_SOURCES, window=METRICS_WINDOW)
        except probe.ProbeError as exc:
            return item('metrics', GROUP_DATA, '指标采集', 'warning', f'没能查询时序库：{exc}'), None
        return metrics_check(found, tag_key, self.env), found

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
        """全部检查，按显示顺序返回 (检查结果, AI Kubeconfig 摘要, Agent 实际所在的命名空间)。
        组件要对照时序库的结果，和指标检查一起跑：时序库那边先查完再判断组件"""
        settings = await agent_settings(self.env)

        async def metrics_and_components():
            try:
                metrics, sources = await asyncio.wait_for(self.metrics(), CHECK_TIMEOUT)
            except Exception as exc:
                metrics, sources = failure('metrics', GROUP_DATA, '指标采集', exc), None
            components = await guarded(self.components(sources), 'components', GROUP_COMPONENT, '各组件')
            return metrics, components

        results = await asyncio.gather(
            guarded(self.k8s_api(), 'k8s_api', GROUP_AGENT, 'K8S API 访问'),
            guarded(self.pod_mgr(), 'pod_mgr', GROUP_AGENT, 'Pod 管理服务'),
            guarded(self.admission(settings), 'admission', GROUP_AGENT, '准入控制'),
            metrics_and_components(),
            guarded(self.events(), 'events', GROUP_DATA, 'K8S 事件上报'),
            self.connection_summary())
        k8s_api, pod_mgr, admission, (metrics, components), events, connection = results
        now = time.time()
        checks = [connection_check(self.client, now), version_check(self.client, BASE['agent_tag']),
                  k8s_api, pod_mgr, admission, *components, events, metrics, kubeconfig_check(connection)]
        kubeconfig = ({'configured': True, 'origin': connection.get('origin') or 'upload', 'context': connection.get('context') or ''}
                      if isinstance(connection, dict) else {'configured': False})
        return checks, kubeconfig, self.namespace

    async def connection_summary(self):
        try:
            return await asyncio.wait_for(self.connection(), CHECK_TIMEOUT)
        except Exception as exc:
            return f'查询 kubedoor-ai 失败（{type(exc).__name__}）'


def failure(key, group, name, exc):
    """一项检查超时或意外出错时的结果"""
    if isinstance(exc, asyncio.TimeoutError):
        return item(key, group, name, 'error', f'检查超时（{CHECK_TIMEOUT} 秒）')
    logger.warning(f'检查 Agent 的「{name}」出错：{type(exc).__name__}: {exc}')
    return item(key, group, name, 'error', f'检查出错（{type(exc).__name__}）')


async def guarded(coroutine, key, group, name):
    """一项检查单独限时、单独兜底：超时或意外出错只记在这一项"""
    try:
        return await asyncio.wait_for(coroutine, CHECK_TIMEOUT)
    except Exception as exc:
        row = failure(key, group, name, exc)
    if key == 'components':
        return [components_row(row['status'], row['message'])]
    return row


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
    checks, kubeconfig, namespace = await HealthCheck(env, clients, call_agent, namespace, username).run()
    client = clients.get(env) or {}
    return {
        'env': env, 'online': bool(client.get('online')), 'version': str(client.get('ver') or ''),
        'recommended_version': BASE['agent_tag'], 'namespace': namespace,
        'checked_at': datetime.now(timezone.utc).isoformat(), 'checks': checks, 'kubeconfig': kubeconfig,
    }
