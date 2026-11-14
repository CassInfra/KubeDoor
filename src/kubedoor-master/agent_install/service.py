"""安装草稿、预检、异步安装及重启后的任务恢复。"""

import asyncio
import base64
import copy
import hashlib
import json
import os
import re
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import aiohttp
import db
from auth import audit
from auth.crypto import decrypt_secret, encrypt_secret
from auth.store import hash_password, store as auth_store, verify_local
from kubedoor_tools.config import parse_kubeconfig
from loguru import logger

from .bundle import WRITE_KEY, defaults, normalize_options, public_options, render_agent
from .kubernetes import InstallError, Kubernetes
from .repository import Repository

OWNER = 'kubedoor.io/installation'
MANAGED = 'kubedoor.io/managed-by'
MAX_CONFIG = 1024 * 1024
STEP_NAMES = ['验证安装计划', '创建命名空间和凭据', '应用安装资源', '等待工作负载就绪', '等待 Agent 上线', '验证指标采集']
METRICS_STEP = len(STEP_NAMES) - 1
# 完整接入要验证的抓取任务：vmagent 自采集、对象状态、节点、容器，都要有 Agent 上线后的成功样本
METRICS_JOBS = ('vmagent', 'kube-state-metrics', 'k8s-node-exporter', 'k8s-cadvisor')
# 安装向导在本集群安装 Agent 时生成的 kubeconfig 的 context 名（kubedoor_setup.agent.CONTEXT）
SETUP_CONTEXT = 'in-cluster'
# Agent 清单里 Agent 服务账号的长期令牌（manifests/10-agent.yaml 的 Secret，权限与 Agent 相同）
AGENT_TOKEN_SECRET = 'kubedoor-agent'
# 自动配置的 AI Kubeconfig 里的 context / user 名，「AI Kubeconfig」列显示它
AI_CONTEXT = 'kubedoor-agent'
AI_URL = 'http://kubedoor-ai:8000'
AI_MANUAL_HINT = 'AI 仍可经 Agent 执行，需要时可在 Agent 管理的「AI Kubeconfig」列手动上传'


def utcnow():
    return datetime.now(timezone.utc)


def conflict(kind, name, namespace, reason):
    """目标集群里已有、需要强制覆盖才能接管的资源"""
    return {'kind': kind, 'name': name, 'namespace': namespace or '', 'reason': reason}


def merge_conflicts(*groups):
    """同一个资源只列一次，原因合并"""
    merged = {}
    for item in (item for group in groups for item in group):
        key = (item['kind'], item['namespace'], item['name'])
        if key not in merged:
            merged[key] = dict(item)
        elif item['reason'] not in merged[key]['reason']:
            merged[key]['reason'] += '；' + item['reason']
    return list(merged.values())


class TakeoverRequired(InstallError):
    """目标集群里已有 Agent 资源：管理员确认后可以强制覆盖安装（页面据 forceable 显示按钮）"""

    def __init__(self, items):
        listed = '、'.join(f'{item["kind"]}/{item["name"]}（{item["reason"]}）' for item in items[:6])
        more = f' 等 {len(items)} 个资源' if len(items) > 6 else ''
        super().__init__(f'目标集群已有 Agent 资源，不会直接覆盖：{listed}{more}。确认可以接管这些资源后，选择「强制覆盖安装」', 409)
        self.items = items


def ai_kubeconfig(parsed, token):
    """自动配置的 AI Kubeconfig：集群段（API Server 地址、CA、tls-server-name）沿用这次安装用的 kubeconfig，
    凭据换成 Agent 服务账号的令牌。上传的 kubeconfig 本身仍然用完即删，不长期保存。"""
    cluster = copy.deepcopy(parsed['config']['clusters'][0]['cluster'])
    return {'apiVersion': 'v1', 'kind': 'Config', 'current-context': AI_CONTEXT,
            'clusters': [{'name': AI_CONTEXT, 'cluster': cluster}],
            'users': [{'name': AI_CONTEXT, 'user': {'token': token}}],
            'contexts': [{'name': AI_CONTEXT, 'context': {'cluster': AI_CONTEXT, 'user': AI_CONTEXT, 'namespace': 'default'}}]}


def metrics_query(label, env, installation):
    """每个抓取任务最近一次成功抓取（up == 1）的样本时间。

    timestamp() 要直接作用在原始序列上：VictoriaMetrics 对比较运算的结果取 timestamp()，得到的是按 5 分钟对齐的
    计算点，不是样本时间，「上线后有新样本」要等到下一个 5 分钟整点才成立，验证常常超时。
    """
    selector = 'up{' + label + '=' + json.dumps(env) + ',kubedoor_installation=' + json.dumps(installation) + '}'
    return f'max by (job) (timestamp({selector}) and ({selector} == 1))'


def auth_hint(task, exc):
    """连接目标集群返回 401 时，说明是哪份凭据失效、该怎么处理；其它错误原样返回。"""
    if getattr(exc, 'http_status', None) != 401:
        return exc
    if task['payload'].get('context') == SETUP_CONTEXT:
        return InstallError('这条安装记录来自安装向导：在本集群安装 Agent 用的是安装向导签发的临时令牌，安装完成后已撤销，'
                            '不能再用它连接集群（已安装的 Agent 不受影响）。需要重新安装或调整配置时，请点「安装新集群」上传 kubeconfig',
                            409, 401)
    return InstallError('kubeconfig 认证失败（HTTP 401）：凭据无效或已过期，请重新上传 kubeconfig', 409, 401)


def seal(value):
    if not re.fullmatch(r'[0-9a-fA-F]{64}', os.environ.get('AI_ENCRYPTION_KEY', '')):
        raise InstallError('控制端尚未配置有效的加密密钥，请重新应用新版 master 安装配置', 503)
    return encrypt_secret(json.dumps(value, ensure_ascii=False))


def unseal(task):
    if not task.get('encrypted') or task['expires_at'] <= utcnow():
        raise InstallError('临时安装凭据已过期，请重新上传 kubeconfig', 409)
    try:
        return json.loads(decrypt_secret(task['encrypted']))
    except ValueError:
        raise InstallError('安装凭据无法解密，请重新上传 kubeconfig', 409) from None


def parse(raw, context=None):
    try:
        return parse_kubeconfig(raw, context)
    except (ValueError, TypeError, KeyError):
        raise InstallError('kubeconfig 无效或依赖本地文件/登录插件，请使用内嵌 CA、token 或客户端证书的配置，并选择有效 context') from None


def summary(resource):
    return {key: resource['metadata'].get(key, '') for key in ('name', 'namespace')} | {'kind': resource['kind']}


def fingerprint(resources):
    return hashlib.sha256(json.dumps(resources, sort_keys=True).encode()).hexdigest()


def address_without_auth(value):
    parsed = urlsplit(value)
    return urlunsplit((parsed.scheme, parsed.netloc.rsplit('@', 1)[-1], parsed.path.rstrip('/'), '', ''))


def error_message(exc):
    if isinstance(exc, TimeoutError):
        return '安装预检超时，请检查 master 到目标 API Server 的网络后重试'
    return str(exc) if isinstance(exc, InstallError) else f'安装服务执行失败（{type(exc).__name__}），请查看对应步骤后重试'


def normalize(raw, default=None):
    try:
        return normalize_options(raw, default)
    except ValueError as exc:
        raise InstallError(str(exc)) from None


def render(options, **credential):
    """渲染安装资源；参数问题（例如完整接入却没有指标写入地址）按安装错误返回给页面。"""
    try:
        return render_agent(options, **credential)
    except ValueError as exc:
        raise InstallError(str(exc)) from None


class Service:
    def __init__(self, clients, repository=None, kubernetes=Kubernetes):
        self.clients = clients
        self.repo = repository or Repository()
        self.kubernetes = kubernetes
        self.tasks = {}
        self.locks = {}
        self.slots = asyncio.Semaphore(2)
        self.cleanup_task = None
        self.poll_interval = 3
        self.ready_timeout = 300
        self.online_timeout = 120
        self.metrics_timeout = 120
        # 指标验证期间最近一次查询的结果（各任务里已有新样本的抓取任务 / 查询失败的原因），验证失败时用来说明原因
        self.metrics_seen = {}
        self.metrics_errors = {}

    async def open(self):
        await self.repo.expire()
        for task in await self.repo.unfinished():
            if task['status'] == 'running':
                task['status'] = 'interrupted'
                task['payload']['error'] = 'master 已重启，任务暂停；点击重试将核对目标资源后继续'
                for step in task['payload'].get('steps', []):
                    if step['status'] == 'running':
                        step.update(status='error', message='master 重启中断')
                await self.repo.save(task)
        self.cleanup_task = asyncio.create_task(self.cleanup())

    async def cleanup(self):
        while True:
            await asyncio.sleep(60)
            try:
                await self.repo.expire()
            except Exception as exc:
                logger.warning(f'清理安装凭据失败：{type(exc).__name__}')

    async def close(self):
        pending = list(self.tasks.values())
        if self.cleanup_task:
            pending.append(self.cleanup_task)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    def public(self, task):
        payload = copy.deepcopy(task['payload'])
        payload.update({key: task[key] for key in ('id', 'status', 'revision')})
        for key in ('created_at', 'updated_at', 'expires_at'):
            value = task.get(key)
            payload[key] = value.isoformat() if isinstance(value, datetime) else value
        # 部分成功（只差指标验证）的重试只重新查询指标，不需要安装凭据；其余重试要用还没过期的凭据重新连接集群
        payload['retryable'] = task['status'] == 'partial' or bool(
            task.get('encrypted') and task['expires_at'] > utcnow() and task['status'] in ('failed', 'interrupted'))
        payload['target_locked'] = bool(payload.get('applied') or payload.get('target_reserved'))
        return payload

    async def get(self, task_id):
        try:
            uuid.UUID(task_id)
        except (ValueError, TypeError):
            raise InstallError('安装任务不存在', 404) from None
        task = await self.repo.get(task_id)
        if not task:
            raise InstallError('安装任务不存在', 404)
        return task

    async def inspect(self, raw, username):
        if not isinstance(raw, str) or not raw.strip() or len(raw.encode()) > MAX_CONFIG:
            raise InstallError('请上传不超过 1 MiB 的 kubeconfig YAML / JSON 文件')
        parsed = await asyncio.to_thread(parse, raw)
        selected = parsed['context'] or parsed['contexts'][0]['name']
        options = defaults()
        options['env'] = re.sub(r'[^A-Za-z0-9_-]', '-', selected).strip('-')[:63] or 'cluster'
        if not options.get('metrics_available'):
            options['mode'] = 'agent'
        options = normalize(options)
        now = utcnow()
        task = {'id': str(uuid.uuid4()), 'status': 'draft', 'revision': 0, 'env': '', 'target_uid': '',
                'encrypted': seal({'kubeconfig': raw, 'options': options}),
                'expires_at': now + timedelta(minutes=30), 'created_at': now, 'updated_at': now,
                'payload': {'created_by': username, 'context': selected, 'contexts': parsed['contexts'],
                            'options': public_options(options), 'checks': [], 'steps': [], 'resources': []}}
        await self.repo.create(task)
        audit.record_bg(username, 'agent.install.upload', target=task['id'])
        return task

    async def preflight(self, task_id, context, raw_options, force=False):
        """force：管理员已确认可以接管目标集群里已有的 Agent 资源（强制覆盖安装）"""
        async with self.locks.setdefault(task_id, asyncio.Lock()):
            task = await self.get(task_id)
            if task['status'] not in ('draft', 'ready', 'failed', 'partial', 'interrupted'):
                raise InstallError('当前任务不能修改安装计划，请创建新的安装任务', 409)
            material = unseal(task)
            options = normalize(raw_options, defaults())
            previous = material['options']
            # 界面只收到脱敏地址。没改过该地址时保留密文里的认证信息及查询参数。
            safe_previous = public_options(previous)
            for key in ('remote_write_url', 'oss_url'):
                if options.get(key) and options[key] == safe_previous.get(key):
                    options[key] = previous[key]
            if (task['payload'].get('applied') or task['payload'].get('target_reserved')) and (
                    context != task['payload']['context'] or any(options[k] != previous[k] for k in ('env', 'namespace'))):
                raise InstallError('任务已开始安装，不能更换目标 context、集群标识或命名空间；请新建安装任务', 409)
            if not options.get('env') or not options.get('master_url'):
                raise InstallError('请填写集群唯一标识和目标集群可访问的 KubeDoor 地址')
            if options['mode'] == 'full' and not (options.get('metrics_write_url') or options.get('remote_write_url')):
                raise InstallError('控制端没有配置时序库写入地址，请在高级设置中填写指标写入地址，或选择仅安装 Agent')
            options['installation_id'] = task['id']
            checks = []
            task['status'] = 'draft'
            task['payload'].update(context=context, options=public_options(options), checks=checks, error='', takeover=[], force=False)
            try:
                parsed = await asyncio.to_thread(parse, material['kubeconfig'], context)
                async with asyncio.timeout(100), self.kubernetes(parsed) as kube:
                    version = await kube.request('GET', '/version')
                    identity = await kube.request('GET', '/api/v1/namespaces/kube-system')
                    target_uid = identity['metadata']['uid']
                    checks.append({'name': '集群连接', 'status': 'success', 'message': version.get('gitVersion', '已连接')})
                    task['env'], task['target_uid'] = options['env'], target_uid
                    existing = await self.check_identity(task, options, kube)
                    await self.components(options, kube, checks)
                    resources = render(options, username='preflight', password='preflight')
                    task['payload']['resources'] = [summary(item) for item in resources]
                    takeover = merge_conflicts(existing, await self.resource_conflicts(task, kube, resources))
                    task['payload']['takeover'] = takeover
                    if takeover and not force:
                        raise TakeoverRequired(takeover)
                    task['payload']['force'] = bool(takeover)
                    await self.check_resources(kube, resources, force=bool(takeover))
                    checks.append({'name': '安装权限与资源', 'status': 'success', 'message': '权限、资源归属与可用的服务端预检通过'})
                    if takeover:
                        checks.append({'name': '强制覆盖', 'status': 'warning',
                                       'message': f'将接管目标集群里已有的 {len(takeover)} 个 Agent 资源，原来的 Agent 会改连当前控制端'})
                    checks.append({'name': '返回控制端的网络', 'status': 'warning',
                                   'message': '安装后将从 Agent 实际连接确认，请确保目标集群可访问所填 KubeDoor 地址'})
                    task['payload']['plan_digest'] = fingerprint(resources)
                    task['status'] = 'ready'
            except Exception as exc:
                checks.append({'name': '安装预检', 'status': 'error', 'message': error_message(auth_hint(task, exc)),
                               **({'forceable': True} if isinstance(exc, TakeoverRequired) else {})})
            material['options'] = options
            task['encrypted'] = seal(material)
            task['expires_at'] = utcnow() + timedelta(minutes=30)
            task['payload']['options'] = public_options(options)
            await self.repo.save(task)
            return task

    async def check_identity(self, task, options, kube):
        """核对集群标识和目标命名空间里已有的 Agent。控制端自己记录里的冲突直接报错；
        目标集群里已有、不属于这次安装的 Agent 返回成「需要强制覆盖才能接管」的清单。"""
        if options.get('image_pull_secret'):
            existing_secret = await kube.request('GET', f'/api/v1/namespaces/{options["namespace"]}/secrets/{options["image_pull_secret"]}',
                                                 missing_ok=True)
            if not existing_secret:
                raise InstallError('指定的镜像拉取 Secret 不存在，请先在目标命名空间创建它或使用可公开拉取的镜像')
        for other in await self.repo.same_cluster(options['env'], task['target_uid'], task['id']):
            if other['env'] != options['env'] or other['target_uid'] != task['target_uid']:
                raise InstallError('集群标识已被其它集群使用，或目标集群已使用另一标识接入', 409)
            if other['status'] == 'running':
                raise InstallError('目标集群已有安装任务正在执行', 409)
        cfg = await kube.request('GET', f'/api/v1/namespaces/{options["namespace"]}/configmaps/kubedoor-agent-config', missing_ok=True)
        if cfg:
            reasons = []
            name = str(cfg.get('data', {}).get('PROM_K8S_TAG_VALUE') or '')
            if name != options['env']:
                reasons.append(f'现有 Agent 的集群名称是 {name[:64] or "（空）"}')
            if cfg['metadata'].get('labels', {}).get(MANAGED) != 'web-installer':
                reasons.append('脚本安装的 Agent')
            return [conflict('ConfigMap', 'kubedoor-agent-config', options['namespace'], '；'.join(reasons))] if reasons else []
        if options['env'] in self.clients:
            raise InstallError('这个集群标识已有 Agent 登记，请选择其它标识或核对目标集群', 409)
        return []

    async def components(self, options, kube, checks):
        if options['mode'] == 'agent':
            return
        if options.get('ksm') in ('auto', 'reuse'):
            services = await kube.list('/api/v1/services', {'labelSelector': 'app.kubernetes.io/name=kube-state-metrics'})
            found = [s for s in services if any(p.get('port') == 8080 for p in s['spec'].get('ports', []))]
            if options['ksm'] == 'reuse' and not found:
                raise InstallError('未找到监听 8080 且带兼容标签的 kube-state-metrics Service')
            options['ksm'] = 'reuse' if found else 'install'
        if options.get('node_exporter') in ('auto', 'reuse'):
            nodes = await kube.list('/api/v1/nodes')
            pods = await kube.list('/api/v1/pods', {'fieldSelector': 'status.phase=Running'})
            covered = {p['spec'].get('nodeName') for p in pods if p['spec'].get('hostNetwork') and any(
                port.get('containerPort') == 9100 for c in p['spec'].get('containers', []) for port in c.get('ports', []))}
            needed = {n['metadata']['name'] for n in nodes
                      if n['metadata'].get('labels', {}).get('kubernetes.io/os', 'linux') == 'linux'}
            existing = bool(needed and needed <= covered)
            if options['node_exporter'] == 'reuse' and not existing:
                raise InstallError('未在所有目标节点检测到使用主机网络和 9100 端口的 node-exporter')
            if covered and not existing:
                raise InstallError('部分节点已有 9100 采集器，自动安装可能冲突；请先统一节点采集配置')
            options['node_exporter'] = 'reuse' if existing else 'install'
        checks.append({'name': '采集组件', 'status': 'success',
                       'message': f'kube-state-metrics：{options["ksm"]}；node-exporter：{options["node_exporter"]}；抓取结果将在安装后验证'})

    async def resource_conflicts(self, task, kube, resources):
        """目标集群里已有、但不属于这次安装的资源：没有确认强制覆盖时不能接管"""
        found = []
        for resource in resources:
            if resource['kind'] == 'Namespace':
                continue
            current = await kube.get(resource)
            if not current:
                continue
            labels = current['metadata'].get('labels', {})
            if labels.get(MANAGED) != 'web-installer':
                reason = '不是网页安装创建的：脚本安装或其他工具装的'
            else:
                owner = labels.get(OWNER)
                if not owner or owner == task['id']:
                    continue
                previous = await self.repo.get(owner)
                if previous and previous['target_uid'] == task['target_uid'] and previous['env'] == task['env']:
                    continue    # 当前控制端以前给同一个集群装的：重装就是更新它
                reason = '属于另一项安装：安装记录不在当前控制端，或集群名称不同'
            found.append(conflict(resource['kind'], resource['metadata']['name'], resource['metadata'].get('namespace', ''), reason))
        return found

    async def check_resources(self, kube, resources, force=False):
        """权限与服务端 dry-run；force 时服务端应用强制接管其他字段管理器的字段（已确认强制覆盖）"""
        namespace = next(r for r in resources if r['kind'] == 'Namespace')
        namespace_exists = await kube.get(namespace)
        permissions = set()
        for resource in resources:
            for verb in ('get', 'patch', 'create'):
                key = (resource['apiVersion'], resource['kind'], resource['metadata'].get('namespace'), verb)
                if key not in permissions:
                    if not await kube.allowed(resource, verb):
                        raise InstallError(f'缺少 {verb} {resource["kind"]} 权限')
                    permissions.add(key)
            if resource['kind'] == 'Namespace':
                if not namespace_exists:
                    await kube.apply(resource, dry_run=True)
            elif 'namespace' not in resource['metadata'] or namespace_exists:
                await kube.apply(resource, dry_run=True, force=force)

    async def takeover_check(self, task, options, kube, resources, force):
        """汇总需要强制覆盖的已有资源；没有确认强制覆盖就拒绝（列出全部资源），确认了就带着清单继续"""
        found = merge_conflicts(await self.check_identity(task, options, kube),
                                await self.resource_conflicts(task, kube, resources))
        if found and not force:
            raise TakeoverRequired(found)
        return found

    async def start(self, task_id, revision, retry=False):
        async with self.locks.setdefault(task_id, asyncio.Lock()):
            task = await self.get(task_id)
            if task['status'] == 'running':
                return task
            allowed = ('failed', 'partial', 'interrupted') if retry else ('ready',)
            if task['status'] not in allowed or (not retry and revision != task['revision']):
                raise InstallError('安装计划已变化或尚未通过预检，请重新检测', 409)
            if retry and task['status'] == 'partial':
                return await self.start_reverify(task)
            unseal(task)
            task['status'] = 'running'
            task['payload']['error'] = ''
            task['payload']['steps'] = [{'name': name, 'status': 'pending', 'message': ''} for name in STEP_NAMES]
            task['expires_at'] = utcnow() + timedelta(hours=24)
            await self.repo.save(task)
            job = asyncio.create_task(self.run(task))
            self.tasks[task_id] = job
            job.add_done_callback(lambda _: self.tasks.pop(task_id, None))
            audit.record_bg(task['payload']['created_by'], 'agent.install.start', env=task['env'], target=task_id)
            return task

    async def step(self, task, index, status, message=''):
        task['payload']['steps'][index].update(status=status, message=message)
        await self.repo.save(task)

    async def wait_for(self, predicate, timeout, message):
        end = time.monotonic() + timeout
        while True:
            if await predicate():
                return
            if time.monotonic() >= end:
                raise InstallError(message)
            await asyncio.sleep(self.poll_interval)

    async def run(self, task):
        index = 0
        try:
            async with self.slots:
                material = unseal(task)
                options = material['options']
                await self.step(task, 0, 'running')
                if WRITE_KEY not in options:
                    # 旧版本做的安装计划（当时 vmagent 写 KubeDoor 指标网关，网关已删除），模板已变，要重新检测
                    raise InstallError('安装计划由旧版本创建，请重新检测后再安装')
                parsed = await asyncio.to_thread(parse, material['kubeconfig'], task['payload']['context'])
                async with self.kubernetes(parsed) as kube:
                    try:
                        identity = await kube.request('GET', '/api/v1/namespaces/kube-system')
                    except InstallError as exc:
                        raise auth_hint(task, exc) from None
                    if identity['metadata']['uid'] != task['target_uid']:
                        raise InstallError('目标集群身份与预检不一致，安装已停止')
                    preview = render(options, username='preflight', password='preflight')
                    if fingerprint(preview) != task['payload']['plan_digest']:
                        raise InstallError('安装模板已更新，请重新创建安装计划')
                    # 预检之后集群里可能又多了别人的资源：再核对一次；预检时确认过强制覆盖才接管
                    force = bool(task['payload'].get('force'))
                    takeover = await self.takeover_check(task, options, kube, preview, force)
                    await self.check_resources(kube, preview, force=bool(takeover))
                    await self.step(task, 0, 'success')
                    index = 1
                    await self.step(task, index, 'running')
                    if 'credential' not in material:
                        credential = {'username': 'agent-' + task['id'].replace('-', ''), 'password': secrets.token_urlsafe(36)}
                        material['credential'] = credential
                        task['payload']['target_reserved'] = True
                        task['encrypted'] = seal(material)
                        await self.repo.save(task)
                    credential = material['credential']
                    user = await auth_store.get_user_by_name(credential['username'])
                    if not user:
                        user = await auth_store.create_user(credential['username'], 'local', 'agent',
                                                            password_hash=await hash_password(credential['password']),
                                                            comment='网页安装 Agent：' + task['env'])
                    elif (user['role'] != 'agent' or not user.get('enabled', True)
                          or not await verify_local(user, credential['password'])):
                        raise InstallError('安装专用账号已被修改，请创建新的安装任务')
                    await auth_store.bind_agent(user['id'], task['env'])
                    task['payload']['agent_user_id'] = user['id']
                    resources = render(options, **credential)
                    for resource in resources:
                        resource['metadata'].setdefault('labels', {}).update({OWNER: task['id'], MANAGED: 'web-installer'})
                    namespace = next(r for r in resources if r['kind'] == 'Namespace')
                    if not await kube.get(namespace):
                        await kube.apply(namespace)
                    # Namespace 预检不落盘；现在补齐 namespaced 资源的 dry-run。
                    for resource in resources:
                        if resource['kind'] != 'Namespace':
                            await kube.apply(resource, dry_run=True, force=force)
                    await self.step(task, index, 'success')
                    index = 2
                    await self.step(task, index, 'running')
                    task['payload']['started_at'] = time.time()
                    await self.repo.save(task)
                    for resource in resources:
                        if resource['kind'] == 'Namespace':
                            continue
                        await kube.apply(resource, force=force)
                        task['payload'].setdefault('applied', []).append(summary(resource))
                        await self.repo.save(task)
                    await self.step(task, index, 'success')
                    index = 3
                    await self.step(task, index, 'running')
                    workloads = [r for r in resources if r['kind'] in ('Deployment', 'DaemonSet')]

                    async def ready():
                        return all(await asyncio.gather(*(kube.ready(r) for r in workloads)))

                    try:
                        await self.wait_for(ready, self.ready_timeout, '等待工作负载就绪超时')
                    except InstallError:
                        raise InstallError(await kube.workload_diagnosis(options['namespace'])) from None
                    await self.step(task, index, 'success')
                    index = 4
                    await self.step(task, index, 'running')

                    async def online():
                        client = self.clients.get(task['env'], {})
                        return (client.get('online') and client.get('heartbeat_received')
                                and client.get('agent_user_id') == user['id']
                                and client.get('last_heartbeat', 0) >= task['payload']['started_at'])

                    await self.wait_for(online, self.online_timeout, 'Agent 未连接控制端，请检查目标集群访问 KubeDoor 地址的网络、DNS和认证')
                    await self.step(task, index, 'success')
                    # 只有安装参数里带了别名才写（安装向导第 6 步可填）；网页「安装 Agent」不填别名，
                    # 重装时保留在 Agent 列表里编辑过的别名
                    if options.get('aliases'):
                        await db.pg_execute('UPDATE k8s_agent_status SET aliases=$2 WHERE env=$1', task['env'], options['aliases'])
                    # 顺带配置该集群的 AI Kubeconfig（Agent 服务账号令牌）；失败只记结果，不影响安装
                    task['payload']['ai_connection'] = await self.configure_ai_connection(task, options, parsed, kube)
                    await self.repo.save(task)
                    task['payload']['metrics_started_at'] = time.time()
                    index = 5
                    if options['mode'] == 'full':
                        await self.step(task, index, 'running')
                        try:
                            await self.wait_for(lambda: self.metrics_received(task, options), self.metrics_timeout, '')
                        except InstallError:
                            raise InstallError(await self.metrics_diagnosis(task, options, kube)) from None
                        await self.step(task, index, 'success')
                    else:
                        await self.step(task, index, 'skipped', '仅安装 Agent，使用既有监控采集')
                    await self.finish_success(task)
        except asyncio.CancelledError:
            task['status'] = 'interrupted'
            task['payload']['error'] = '控制端退出，点击重试将核对已有资源后继续'
            try:
                await self.step(task, index, 'error', task['payload']['error'])
            except Exception:
                pass
            raise
        except Exception as exc:
            task['status'] = 'partial' if index == METRICS_STEP else 'failed'
            task['payload']['error'] = error_message(auth_hint(task, exc))
            try:
                await self.step(task, index, 'error', task['payload']['error'])
            except Exception as save_exc:
                logger.error(f'保存安装失败状态异常：{type(save_exc).__name__}')
            logger.warning(f'Agent 安装 {task["id"]} 步骤 {index + 1} 失败：{type(exc).__name__}')
        finally:
            audit.record_bg(task['payload']['created_by'], 'agent.install.finish', env=task['env'],
                            target=task['id'], success=task['status'] == 'succeeded', detail={'status': task['status']})

    async def configure_ai_connection(self, task, options, parsed, kube):
        """Agent 上线后自动配置该集群的 AI Kubeconfig，返回 {status: saved / skipped / failed, message}。

        凭据用 Agent 服务账号的长期令牌（集群里本来就有的 Secret kubedoor-agent），集群地址和 CA 沿用这次安装的
        kubeconfig：安装向导第 6 步用的临时令牌收尾时会撤销，也不长期保存用户上传的个人凭据。
        交给 kubedoor-ai 的保存接口（先测试连通再保存，不覆盖管理员手动上传的）；任何失败只记结果，不影响安装。
        """
        try:
            token = await self.agent_token(kube, options['namespace'])
        except Exception as exc:
            logger.warning(f'读取 Agent 服务账号令牌失败：{type(exc).__name__}')
            return {'status': 'failed', 'message': f'读取 Agent 服务账号令牌失败（{error_message(exc)}）。{AI_MANUAL_HINT}'}
        if not token:
            return {'status': 'failed', 'message': f'Agent 服务账号的令牌还没有生成（Secret {AGENT_TOKEN_SECRET}）。{AI_MANUAL_HINT}'}
        return await self.save_ai_connection(task, ai_kubeconfig(parsed, token))

    async def agent_token(self, kube, namespace):
        """Agent 服务账号的令牌：Secret 由 K8S 的令牌控制器异步填入，工作负载就绪后一般早就有了，没有就稍等几次"""
        path = f'/api/v1/namespaces/{quote(namespace, safe="")}/secrets/{AGENT_TOKEN_SECRET}'
        for attempt in range(10):
            secret = await kube.request('GET', path, missing_ok=True) or {}
            token = (secret.get('data') or {}).get('token')
            if token:
                return base64.b64decode(token).decode()
            if attempt < 9:
                await asyncio.sleep(self.poll_interval)
        return ''

    async def save_ai_connection(self, task, config):
        internal = os.environ.get('AI_INTERNAL_TOKEN', '')
        if not internal:
            return {'status': 'failed', 'message': f'控制端没有配置 AI 内部令牌（AI_INTERNAL_TOKEN）。{AI_MANUAL_HINT}'}
        headers = {'X-Kubedoor-Token': internal, 'X-User-Name': task['payload'].get('created_by') or 'kubedoor',
                   'X-User-Permission': 'admin'}
        body = {'content': json.dumps(config), 'context': AI_CONTEXT, 'origin': 'agent-install'}
        try:
            # kubedoor-ai 保存前会测试连通（多个 API 调用，每个最多 15 秒）
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=150, connect=10)) as session:
                async with session.put(f"{AI_URL}/api/ai/connections/{quote(task['env'], safe='')}", json=body,
                                       headers=headers, allow_redirects=False) as response:
                    status = response.status
                    data = await response.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            logger.warning(f'自动配置 AI Kubeconfig 时连不上 kubedoor-ai：{type(exc).__name__}')
            return {'status': 'failed', 'message': f'连不上 kubedoor-ai，没有自动配置 AI Kubeconfig。{AI_MANUAL_HINT}'}
        if status == 200 and isinstance(data, dict) and data.get('skipped'):
            return {'status': 'skipped', 'message': '该集群已有手动上传的 AI Kubeconfig，保留不变'}
        if status == 200:
            return {'status': 'saved', 'message': '已用 Agent 服务账号自动配置 AI Kubeconfig'}
        error = data.get('error') if isinstance(data, dict) else None
        reason = ' '.join(str((error.get('message') if isinstance(error, dict) else error) or f'HTTP {status}').split())[:200]
        if status == 422:
            reason = f'kubedoor-ai 用它连接集群的测试没有通过：{reason}'
        return {'status': 'failed', 'message': f'没有自动配置 AI Kubeconfig（{reason}）。{AI_MANUAL_HINT}'}

    async def finish_success(self, task):
        """安装成功：停用这个集群以前安装留下的 Agent 账号，销毁安装凭据。"""
        task['status'] = 'succeeded'
        user_id = task['payload'].get('agent_user_id')
        for previous in await self.repo.same_cluster(task['env'], task['target_uid'], task['id']):
            old_user = previous['payload'].get('agent_user_id')
            if old_user and old_user != user_id:
                await auth_store.update_user(old_user, enabled=False)
        task['encrypted'] = ''
        await self.repo.save(task)

    async def start_reverify(self, task):
        """部分成功（Agent 已上线，只差指标验证）的重试：只重新验证指标采集，不再连接目标集群。

        前几步都已完成，不需要安装凭据。安装向导在本集群安装时用的是临时令牌，收尾时就撤销了，再连接只会 401。
        调用方持有该任务的锁。
        """
        steps = task['payload'].get('steps') or []
        if len(steps) != len(STEP_NAMES) or not task.get('env'):
            raise InstallError('安装记录不完整，请重新安装', 409)
        task['status'] = 'running'
        task['payload']['error'] = ''
        steps[METRICS_STEP].update(status='pending', message='')
        await self.repo.save(task)
        job = asyncio.create_task(self.reverify(task))
        self.tasks[task['id']] = job
        job.add_done_callback(lambda _: self.tasks.pop(task['id'], None))
        audit.record_bg(task['payload']['created_by'], 'agent.install.start', env=task['env'], target=task['id'])
        return task

    async def reverify(self, task):
        options = {'prom_k8s_tag_key': defaults()['prom_k8s_tag_key'], 'env': task['env']}
        try:
            async with self.slots:
                await self.step(task, METRICS_STEP, 'running')
                try:
                    await self.wait_for(lambda: self.metrics_received(task, options), self.metrics_timeout, '')
                except InstallError:
                    raise InstallError(await self.metrics_diagnosis(task, options)) from None
                await self.step(task, METRICS_STEP, 'success')
                await self.finish_success(task)
        except asyncio.CancelledError:
            task['status'] = 'partial'
            task['payload']['error'] = '控制端退出，可以重新验证指标采集'
            try:
                await self.step(task, METRICS_STEP, 'error', task['payload']['error'])
            except Exception:
                pass
            raise
        except Exception as exc:
            task['status'] = 'partial'
            task['payload']['error'] = error_message(exc)
            try:
                await self.step(task, METRICS_STEP, 'error', task['payload']['error'])
            except Exception as save_exc:
                logger.error(f'保存指标验证结果异常：{type(save_exc).__name__}')
        finally:
            audit.record_bg(task['payload']['created_by'], 'agent.install.finish', env=task['env'],
                            target=task['id'], success=task['status'] == 'succeeded', detail={'status': task['status']})

    async def metrics_fresh(self, task, options):
        """Agent 上线后已有成功样本的抓取任务集合；查询不了时返回 (None, 原因)。

        带 nocache=1：VictoriaMetrics 默认隐藏最近 30 秒的数据（-search.latencyOffset），也不读结果缓存；
        Prometheus 会忽略这个参数。
        """
        base = os.environ.get('PROM_URL', '')
        if not base:
            return None, '控制端没有配置时序库查询地址（PROM_URL）'
        parsed = urlsplit(base)
        auth = aiohttp.BasicAuth(unquote(parsed.username or ''), unquote(parsed.password or '')) if parsed.username else None
        url = address_without_auth(base) + '/api/v1/query'
        query = metrics_query(options['prom_k8s_tag_key'], options['env'], task['id'])
        since = task['payload'].get('metrics_started_at', task['payload'].get('started_at', 0))
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                async with session.get(url, auth=auth, params={'query': query, 'nocache': '1'}, allow_redirects=False) as response:
                    if response.status != 200:
                        return None, f'查询时序库返回 HTTP {response.status}'
                    body = await response.json()
            rows = body.get('data', {}).get('result', [])
            return {row.get('metric', {}).get('job') for row in rows if float(row['value'][1]) >= since}, ''
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None, '连不上时序库查询地址'
        except (ValueError, KeyError, TypeError, IndexError):
            return None, '时序库返回的查询结果格式不对'

    async def metrics_received(self, task, options):
        fresh, error = await self.metrics_fresh(task, options)
        if fresh is None:
            self.metrics_errors[task['id']] = error
            return False
        self.metrics_errors.pop(task['id'], None)
        self.metrics_seen[task['id']] = fresh
        return set(METRICS_JOBS) <= fresh

    async def metrics_diagnosis(self, task, options, kube=None):
        """指标验证超时：说明哪些抓取任务没采到新指标；还能连接目标集群时，带上 vmagent 抓取目标的报错。"""
        fresh = self.metrics_seen.pop(task['id'], None)
        error = self.metrics_errors.pop(task['id'], '')
        if fresh is None:
            return f'Agent 已上线，但没能验证指标采集：{error or "查询时序库失败"}。可登录后在控制台查看监控，或稍后点「重新验证指标采集」'
        missing = [job for job in METRICS_JOBS if job not in fresh]
        reasons = await self.target_errors(kube, options.get('namespace') or 'kubedoor', missing) if kube else {}
        items = '、'.join(f'{job}（{reasons[job]}）' if reasons.get(job) else job for job in missing)
        message = f'Agent 已上线，但这些抓取任务在验证时间内没有采到新指标：{items}'
        normal = [job for job in METRICS_JOBS if job in fresh]
        if normal:
            message += '；已正常：' + '、'.join(normal)
        return message + '。可登录后在控制台查看监控，或稍后点「重新验证指标采集」'

    async def target_errors(self, kube, namespace, jobs):
        """经 apiserver 读 vmagent 的 /api/v1/targets，给没采到的抓取任务带上原因；读不到就不带。"""
        if not jobs:
            return {}
        ns = quote(namespace, safe='')
        try:
            pods = await kube.list(f'/api/v1/namespaces/{ns}/pods', {'labelSelector': 'app=vmagent'})
            pod = next((p for p in pods if p.get('status', {}).get('phase') == 'Running'), None)
            if pod is None:
                return {job: 'vmagent 没有运行中的 Pod' for job in jobs}
            name = quote(pod['metadata']['name'], safe='')
            data = await kube.request('GET', f'/api/v1/namespaces/{ns}/pods/{name}:8429/proxy/api/v1/targets')
            targets = data['data']['activeTargets']
        except Exception:
            return {}
        reasons = {}
        for job in jobs:
            mine = [t for t in targets if (t.get('labels') or {}).get('job') == job]
            healthy = sum(1 for t in mine if t.get('health') == 'up')
            if not mine:
                reasons[job] = '没有发现抓取目标'
            elif healthy:
                reasons[job] = f'{healthy}/{len(mine)} 个目标抓取正常，数据可能还没写进时序库，请检查远程写地址'
            else:
                last = next((str(t['lastError']) for t in mine if t.get('lastError')), '')
                reasons[job] = f'{len(mine)} 个目标都抓取失败' + (f'：{" ".join(last.split())[:200]}' if last else '')
        return reasons
