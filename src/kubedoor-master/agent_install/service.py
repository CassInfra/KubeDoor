"""安装草稿、预检、异步安装及重启后的任务恢复。"""

import asyncio
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


def utcnow():
    return datetime.now(timezone.utc)


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
        payload['retryable'] = bool(task.get('encrypted') and task['expires_at'] > utcnow()
                                    and task['status'] in ('failed', 'partial', 'interrupted'))
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

    async def preflight(self, task_id, context, raw_options):
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
            task['payload'].update(context=context, options=public_options(options), checks=checks, error='')
            try:
                parsed = await asyncio.to_thread(parse, material['kubeconfig'], context)
                async with asyncio.timeout(100), self.kubernetes(parsed) as kube:
                    version = await kube.request('GET', '/version')
                    identity = await kube.request('GET', '/api/v1/namespaces/kube-system')
                    target_uid = identity['metadata']['uid']
                    checks.append({'name': '集群连接', 'status': 'success', 'message': version.get('gitVersion', '已连接')})
                    task['env'], task['target_uid'] = options['env'], target_uid
                    await self.check_identity(task, options, kube)
                    await self.components(options, kube, checks)
                    resources = render(options, username='preflight', password='preflight')
                    task['payload']['resources'] = [summary(item) for item in resources]
                    await self.check_resources(task, kube, resources)
                    checks.append({'name': '安装权限与资源', 'status': 'success', 'message': '权限、资源归属与可用的服务端预检通过'})
                    checks.append({'name': '返回控制端的网络', 'status': 'warning',
                                   'message': '安装后将从 Agent 实际连接确认，请确保目标集群可访问所填 KubeDoor 地址'})
                    task['payload']['plan_digest'] = fingerprint(resources)
                    task['status'] = 'ready'
            except Exception as exc:
                checks.append({'name': '安装预检', 'status': 'error', 'message': error_message(exc)})
            material['options'] = options
            task['encrypted'] = seal(material)
            task['expires_at'] = utcnow() + timedelta(minutes=30)
            task['payload']['options'] = public_options(options)
            await self.repo.save(task)
            return task

    async def check_identity(self, task, options, kube):
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
            data = cfg.get('data', {})
            if data.get('PROM_K8S_TAG_VALUE') != options['env']:
                raise InstallError('目标命名空间已有不同集群标识的 Agent，请核对现有安装', 409)
            if cfg['metadata'].get('labels', {}).get(MANAGED) != 'web-installer':
                raise InstallError('目标已有脚本安装的 Agent，请通过现有管理页升级；网页安装不会直接接管旧资源', 409)
        elif options['env'] in self.clients:
            raise InstallError('这个集群标识已有 Agent 登记，请选择其它标识或核对目标集群', 409)

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

    async def check_resources(self, task, kube, resources):
        namespace = next(r for r in resources if r['kind'] == 'Namespace')
        namespace_exists = await kube.get(namespace)
        permissions = set()
        for resource in resources:
            current = await kube.get(resource) if resource['kind'] != 'Namespace' else namespace_exists
            if current and resource['kind'] != 'Namespace':
                labels = current['metadata'].get('labels', {})
                if labels.get(MANAGED) != 'web-installer':
                    raise InstallError(f'{resource["kind"]}/{resource["metadata"]["name"]} 已存在且不属于网页安装，停止以避免覆盖', 409)
                owner = labels.get(OWNER)
                if owner and owner != task['id']:
                    previous = await self.repo.get(owner)
                    if not previous or previous['target_uid'] != task['target_uid'] or previous['env'] != task['env']:
                        raise InstallError('目标资源属于另一项安装，不能覆盖', 409)
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
                await kube.apply(resource, dry_run=True)

    async def start(self, task_id, revision, retry=False):
        async with self.locks.setdefault(task_id, asyncio.Lock()):
            task = await self.get(task_id)
            if task['status'] == 'running':
                return task
            allowed = ('failed', 'partial', 'interrupted') if retry else ('ready',)
            if task['status'] not in allowed or (not retry and revision != task['revision']):
                raise InstallError('安装计划已变化或尚未通过预检，请重新检测', 409)
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
                    identity = await kube.request('GET', '/api/v1/namespaces/kube-system')
                    if identity['metadata']['uid'] != task['target_uid']:
                        raise InstallError('目标集群身份与预检不一致，安装已停止')
                    preview = render(options, username='preflight', password='preflight')
                    if fingerprint(preview) != task['payload']['plan_digest']:
                        raise InstallError('安装模板已更新，请重新创建安装计划')
                    await self.check_identity(task, options, kube)
                    await self.check_resources(task, kube, preview)
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
                            await kube.apply(resource, dry_run=True)
                    await self.step(task, index, 'success')
                    index = 2
                    await self.step(task, index, 'running')
                    task['payload']['started_at'] = time.time()
                    await self.repo.save(task)
                    for resource in resources:
                        if resource['kind'] == 'Namespace':
                            continue
                        await kube.apply(resource)
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
                    await db.pg_execute('UPDATE k8s_agent_status SET aliases=$2 WHERE env=$1', task['env'], options.get('aliases', []))
                    task['payload']['metrics_started_at'] = time.time()
                    index = 5
                    if options['mode'] == 'full':
                        await self.step(task, index, 'running')
                        await self.wait_for(lambda: self.metrics_received(task, options), self.metrics_timeout,
                                            'Agent 已上线，但尚未验证到新指标；请检查采集目标和指标写入地址')
                        await self.step(task, index, 'success')
                    else:
                        await self.step(task, index, 'skipped', '仅安装 Agent，使用既有监控采集')
                    task['status'] = 'succeeded'
                    for previous in await self.repo.same_cluster(task['env'], task['target_uid'], task['id']):
                        old_user = previous['payload'].get('agent_user_id')
                        if old_user and old_user != user['id']:
                            await auth_store.update_user(old_user, enabled=False)
                    task['encrypted'] = ''
                    await self.repo.save(task)
        except asyncio.CancelledError:
            task['status'] = 'interrupted'
            task['payload']['error'] = '控制端退出，点击重试将核对已有资源后继续'
            try:
                await self.step(task, index, 'error', task['payload']['error'])
            except Exception:
                pass
            raise
        except Exception as exc:
            task['status'] = 'partial' if index == 5 else 'failed'
            task['payload']['error'] = error_message(exc)
            try:
                await self.step(task, index, 'error', task['payload']['error'])
            except Exception as save_exc:
                logger.error(f'保存安装失败状态异常：{type(save_exc).__name__}')
            logger.warning(f'Agent 安装 {task["id"]} 步骤 {index + 1} 失败：{type(exc).__name__}')
        finally:
            audit.record_bg(task['payload']['created_by'], 'agent.install.finish', env=task['env'],
                            target=task['id'], success=task['status'] == 'succeeded', detail={'status': task['status']})

    async def metrics_received(self, task, options):
        base = os.environ.get('PROM_URL', '')
        if not base:
            return False
        parsed = urlsplit(base)
        auth = aiohttp.BasicAuth(unquote(parsed.username or ''), unquote(parsed.password or '')) if parsed.username else None
        url = address_without_auth(base) + '/api/v1/query'
        label, env = options['prom_k8s_tag_key'], options['env']
        query = ('max by (job) (timestamp(up{' + label + '=' + json.dumps(env)
                 + ',kubedoor_installation=' + json.dumps(task['id']) + '} == 1))')
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                async with session.get(url, auth=auth, params={'query': query}, allow_redirects=False) as response:
                    if response.status != 200:
                        return False
                    body = await response.json()
                    rows = body.get('data', {}).get('result', [])
                    fresh = {row.get('metric', {}).get('job') for row in rows
                             if float(row['value'][1]) >= task['payload'].get('metrics_started_at', task['payload']['started_at'])}
                    # 自采集、对象状态及节点/容器指标都要有新的成功样本。
                    return {'vmagent', 'kube-state-metrics', 'k8s-node-exporter', 'k8s-cadvisor'} <= fresh
        except (aiohttp.ClientError, ValueError, KeyError, TypeError, asyncio.TimeoutError):
            return False
