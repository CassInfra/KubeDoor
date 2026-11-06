"""Durable first-install execution. Only observed service health can finish a task."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import secrets
import struct
import time
from urllib.parse import quote

import aiohttp
import asyncpg

from .agent import CONCLUDED, installer_available, new_agent_state, unavailable_agent_state
from .config import selected_components
from .errors import SetupError
from .kubernetes import OWNER, resource, resource_path, workload_ready
from .probes import check_postgres, check_metrics, metrics_query_base, pg_connect_options, reserve_database
from .render import render_master, load_defaults
from .repository import utcnow

SERVICES = (
    ('web', 'Web 控制台', 'kubedoor-web'),
    ('master', '控制服务', 'kubedoor-master'),
    ('ai', 'AI 助手', 'kubedoor-ai'),
    ('alarm', '告警服务', 'kubedoor-alarm'),
    ('grafana', 'Grafana 看板', 'kubedoor-dash'),
    ('vmalert', '告警规则计算', 'vmalert'),
    ('alertmanager', '告警路由', 'alertmanager'),
)
FINALIZE_JOB = 'kubedoor-setup-finalize'
# 控制服务、AI 助手、告警服务的安装健康接口（Service 名、端口、路径）和它检查的依赖：
# 要带本次安装的内部令牌访问，并返回本次安装的 id
HEALTH_ENDPOINTS = {
    'master': ('kubedoor-master', 80, '/api/internal/setup-health'),
    'ai': ('kubedoor-ai', 8000, '/api/ai/internal/setup-health'),
    'alarm': ('kubedoor-alarm', 80, '/internal/setup-health'),
}
HEALTH_DEPENDS = {'master': '数据库或时序库', 'ai': '数据库', 'alarm': '数据库'}
# 失败详情里最多列出的 Pod 数
DETAIL_PODS = 5


class ServiceCheckError(SetupError):
    """服务接口检查失败：http_status 是对方返回的 HTTP 状态（连不上时为空），probe 记下请求和结果，放进失败详情。"""

    def __init__(self, message, *, http_status=None, probe=''):
        super().__init__(message, 502)
        self.http_status, self.probe = http_status, probe


def pod_summary(pod):
    """一个 Pod 的状态摘要：阶段、就绪容器数、重启次数、等待原因和实际运行的镜像。"""
    metadata, status = pod.get('metadata') or {}, pod.get('status') or {}
    containers = status.get('containerStatuses') or []
    parts = ['删除中' if metadata.get('deletionTimestamp') else status.get('phase') or '状态未知',
             f"就绪 {sum(1 for c in containers if c.get('ready'))}/{len(containers)}",
             f"重启 {sum(int(c.get('restartCount') or 0) for c in containers)} 次"]
    for container in containers:
        reason = ((container.get('state') or {}).get('waiting') or {}).get('reason')
        if reason:
            parts.append(f'等待原因 {reason}')
    images = sorted({c['image'] for c in containers if c.get('image')})
    if images:
        parts.append('运行镜像 ' + '、'.join(images))
    return (f"Pod {metadata.get('name', '')}：" + '，'.join(parts))[:300]


def _varint(value):
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def _field(number, value):
    return _varint(number << 3 | 2) + _varint(len(value)) + value


def probe_write(installation_id, timestamp):
    """One Prometheus WriteRequest in a Snappy raw block (literal, no compressor)."""
    series = b''
    for key, value in (('__name__', 'kubedoor_install_probe'), ('installation_id', installation_id)):
        series += _field(1, _field(1, key.encode()) + _field(2, value.encode()))
    series += _field(2, b'\x09' + struct.pack('<d', float(timestamp)) + b'\x10' + _varint(timestamp))
    payload = _field(1, series)
    length = len(payload) - 1
    if length < 60:
        literal = bytes([length << 2])
    else:
        count = (length.bit_length() + 7) // 8
        literal = bytes([(59 + count) << 2]) + length.to_bytes(count, 'little')
    return _varint(len(payload)) + literal + payload


def finalizer_job(setup, namespace, installation_id):
    """收尾 Job 沿用安装服务自己的镜像和拉取凭据。"""
    pod = setup['spec']['template']['spec']
    image = pod['containers'][0]['image']
    return {
        'apiVersion': 'batch/v1', 'kind': 'Job',
        'metadata': {'name': FINALIZE_JOB, 'namespace': namespace,
                     'annotations': {OWNER: installation_id}},
        'spec': {'backoffLimit': 6, 'activeDeadlineSeconds': 600, 'template': {'metadata': {'labels': {'app': FINALIZE_JOB}},
                 'spec': {'restartPolicy': 'OnFailure', 'serviceAccountName': 'kubedoor-setup-finalizer',
                          'containers': [{'name': 'finalize', 'image': image,
                                          'command': ['python', '-m', 'kubedoor_setup.finalize'],
                                          'env': [{'name': 'SETUP_NAMESPACE', 'value': namespace},
                                                  {'name': 'SETUP_INSTALLATION_ID', 'value': installation_id}]}],
                          **({'imagePullSecrets': pod['imagePullSecrets']} if pod.get('imagePullSecrets') else {})}}}}


async def start_finalizer(k8s, namespace, installation_id, update, *, poll_interval=3):
    """创建收尾 Job。

    update(**fields) 由调用方在锁内核对安装阶段后写入状态：不带参数时只刷新 updated_at，
    phase='finalizing' 表示开始收尾；Job 创建失败时改为 failed 并重新抛出异常。
    执行器（running 阶段）和 Agent 步骤（agent 阶段）共用这一段，各自传入自己的 update。
    """
    setup = await k8s.get(resource('Deployment', 'kubedoor-setup', namespace, 'apps/v1'))
    if not setup:
        raise SetupError('无法读取安装服务的收尾配置', 503)
    job = finalizer_job(setup, namespace, installation_id)
    existing = await k8s.get(job)
    if existing:
        if existing.get('metadata', {}).get('annotations', {}).get(OWNER) != installation_id:
            raise SetupError(f"发现不属于本次安装的 Job {FINALIZE_JOB}，已停止以保护现有部署", 409)
        failed = any(c.get('type') == 'Failed' and c.get('status') == 'True'
                     for c in existing.get('status', {}).get('conditions', []))
        if not failed:
            raise SetupError('收尾任务已在运行，请等待其完成', 409)
        await k8s.delete(job)
        for _ in range(20):
            if await k8s.get(job) is None:
                break
            await update()
            await asyncio.sleep(poll_interval)
        else:
            raise SetupError('旧收尾任务尚未清理，请稍后重试', 409)
    await update(phase='finalizing')
    try:
        await k8s.create(job)
    except Exception:
        await update(phase='failed', error='无法创建安装收尾任务，请检查 Job 部署权限后重试')
        raise


class InstallRunner:
    def __init__(self, repo, k8s):
        self.repo, self.k8s = repo, k8s
        self.namespace = k8s.namespace
        self.timeout = 300
        self.poll_interval = 3
        self.health_timeout = 60
        self.http = None
        self.objects = []

    async def update(self, *, service=None, check=None, status=None, message=None, details=None, **fields):
        async with self.repo.lock:
            state = await self.repo.load()
            if state['phase'] not in ('running', 'finalizing'):
                raise SetupError('安装任务状态已改变，停止当前执行器', 409)
            if hasattr(self, 'run_id') and state.get('run_id') != self.run_id:
                raise SetupError('此执行器已被新的重试任务替代', 409)
            if service or check:
                rows = state.setdefault('services' if service else 'checks', [])
                key, identifier = ('id', service) if service else ('name', check)
                row = next((r for r in rows if r.get(key) == identifier), None)
                if row is None:
                    row = {key: identifier}
                    rows.append(row)
                row.update(status=status, message=message or '', updated_at=utcnow())
                # 详情只属于这一次结果：没有新详情时清掉上次失败留下的
                if details:
                    row['details'] = details
                else:
                    row.pop('details', None)
            state.update(fields)
            await self.repo.save(state)
            return state

    async def run(self):
        state = await self.repo.load()
        self.installation_id = state['id']
        self.run_id = state.get('run_id')
        private = await self.repo.load_private(state)
        self.config, self.secrets = private['config'], private['secrets']
        # Web、控制服务、AI 和 Grafana 必装；告警组件按第 4 步的开关一起装或一起不装
        self.selected = {'web': True, 'master': True, 'ai': True, **selected_components(self.config.get('components'))}
        rows = [{'id': sid, 'name': title, 'resource': deployment,
                 'status': 'pending' if self.selected[sid] else 'skipped',
                 'message': '等待部署' if self.selected[sid] else '未选择',
                 'updated_at': utcnow()} for sid, title, deployment in SERVICES]
        await self.update(services=rows, public_url=self.config['public_url'],
                          selected_services=[row['id'] for row in rows if row['status'] != 'skipped'],
                          metrics_write_required=bool(self.config['metrics'].get('write_url')))
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20, connect=8),
                                         cookie_jar=aiohttp.CookieJar(unsafe=True), trust_env=False) as session:
            self.http = session
            await self.update(check='postgres', status='running', message='复查数据库连接及所属安装')
            await check_postgres(self.config['postgres'], self.installation_id)
            await reserve_database(self.config['postgres'], self.installation_id)
            await self.update(check='postgres', status='success', message='数据库连接和安装归属验证通过')
            await self.update(check='metrics', status='running', message='复查时序库查询连接')
            await check_metrics(self.config['metrics'])
            await self.update(check='metrics', status='success', message='时序库查询验证通过')
            defaults = load_defaults(json.loads(os.environ.get('SETUP_DEFAULTS', '{}')))
            web_service = await self.k8s.get(resource('Service', 'kubedoor-web', self.namespace))
            if web_service:
                defaults['WEB_NODEPORT'] = str(web_service['spec']['ports'][0].get('nodePort', ''))
            version = await self.k8s.request('GET', '/version')
            try:
                defaults['CRON_TIMEZONE'] = 'true' if int(version['minor'].rstrip('+')) >= 25 else 'false'
            except (KeyError, ValueError):
                defaults['CRON_TIMEZONE'] = 'false'
            self.objects = render_master(self.config, self.secrets, self.namespace, defaults)
            self.prepare_objects()
            # Preflight all names before any application: do not take over unrelated resources.
            await self.preflight()
            for obj in self.objects:
                if obj['kind'] not in ('Deployment', 'CronJob') and not self.is_web_config(obj):
                    await self.apply_owned(obj)
                    await self.update()
            await self.deploy('master')
            state = await self.repo.load()
            if self.row(state, 'master')['status'] != 'ready':
                for sid, _, _ in SERVICES:
                    if sid != 'master' and self.selected[sid]:
                        await self.update(service=sid, status='waiting', message='等待控制服务就绪')
                await self.fail('控制服务未就绪，请修复后重试')
                return
            selected = [sid for sid, _, _ in SERVICES if sid not in ('web', 'master', 'vmalert') and self.selected[sid]]
            await asyncio.gather(*(self.deploy(sid) for sid in selected))
            if self.selected['vmalert']:
                state = await self.repo.load()
                if self.row(state, 'alertmanager')['status'] == 'ready':
                    await self.deploy('vmalert')
                else:
                    await self.update(service='vmalert', status='waiting', message='等待 Alertmanager 就绪')
            state = await self.repo.load()
            if any(row['status'] not in ('ready', 'skipped') for row in state['services'] if row['id'] != 'web'):
                await self.update(service='web', status='waiting', message='引导入口可用，等待全部控制端服务就绪后切换')
                await self.fail('部分服务尚未就绪，请查看服务状态并重试')
                return
            # Only switch the live nginx configuration once all its upstreams exist and are healthy.
            for obj in self.objects:
                if self.is_web_config(obj):
                    await self.apply_owned(obj)
            await self.deploy('web')
            state = await self.repo.load()
            if self.row(state, 'web')['status'] != 'ready':
                await self.fail('Web 正式入口未就绪，安装进度已保存')
                return
            for obj in self.objects:
                if obj['kind'] == 'CronJob':
                    await self.apply_owned(obj)
            await self.update(check='endpoints', status='running', message='验证对外入口、管理员登录和指标链路')
            try:
                await self.verify_endpoints()
            except SetupError as error:
                await self.update(check='endpoints', status='error', message=error.message)
                await self.fail(error.message)
                return
            await self.update(check='endpoints', status='success', message='对外入口、登录与所选指标链路验证通过')
            # Recheck every current generation, including components that passed on a previous retry.
            for sid, _, _ in SERVICES:
                if not self.selected[sid]:
                    continue
                await self.update(service=sid, status='checking', message='复查当前副本与服务健康状态')
                obj = self.deployment(sid)
                if not workload_ready(await self.k8s.get(obj), obj):
                    await self.update(service=sid, status='failed', message='最终复查时服务不再就绪',
                                      details=await self.failure_details(sid))
                    await self.fail('服务健康状态发生变化，请重试')
                    return
                try:
                    await self.service_health(sid)
                except SetupError as error:
                    await self.update(service=sid, status='failed', message=error.message,
                                      details=await self.failure_details(sid, error))
                    await self.fail('服务健康复查未通过，请重试')
                    return
                await self.update(service=sid, status='ready', message='副本就绪及健康检查通过')
            await self.conclude()

    async def agent_installer_available(self):
        return await installer_available(self.k8s)

    async def conclude(self):
        """最终检查已通过：能在本集群安装 Agent 时先询问用户，否则直接收尾。

        之前已有结论（装好、部分成功或选择不装，例如收尾失败后重试）时不再询问。
        """
        state = await self.repo.load()
        if (state.get('agent') or {}).get('status') not in CONCLUDED:
            if await self.agent_installer_available():
                await self.update(phase='agent', step=6, agent=new_agent_state(self.namespace, self.config),
                                  check='cleanup', status='pending', message='等待选择是否在本集群安装 Agent')
                return
            await self.update(agent=unavailable_agent_state())
        await self.update(check='cleanup', status='running', message='关闭初始化入口并回收临时安装权限')
        await self.start_finalizer()

    @staticmethod
    def row(state, sid):
        return next(row for row in state['services'] if row['id'] == sid)

    @staticmethod
    def is_web_config(obj):
        return obj['kind'] == 'ConfigMap' and obj['metadata']['name'] == 'nginx-config'

    def deployment(self, sid):
        name = next(name for service, _, name in SERVICES if service == sid)
        return next(obj for obj in self.objects if obj['kind'] == 'Deployment' and obj['metadata']['name'] == name)

    def prepare_objects(self):
        for obj in self.objects:
            obj['metadata'].setdefault('annotations', {})[OWNER] = self.installation_id
            if obj['kind'] in ('Deployment', 'CronJob'):
                template = obj['spec']['template'] if obj['kind'] == 'Deployment' else obj['spec']['jobTemplate']['spec']['template']
                template.setdefault('metadata', {}).setdefault('annotations', {})[OWNER] = self.installation_id
                for container in template['spec']['containers']:
                    container.setdefault('env', []).append({'name': 'KUBEDOOR_SETUP_ID', 'value': self.installation_id})

    async def preflight(self):
        await self.update(check='resources', status='running', message='检查资源归属与集群准入要求')
        for obj in self.objects:
            old = await self.k8s.get(obj)
            if old:
                self.assert_owned(old)
                desired = self.for_replace(obj, old)
                await self.k8s.request('PUT', resource_path(desired), body=desired, params={'dryRun': 'All'})
            else:
                await self.k8s.request('POST', resource_path(obj).rsplit('/', 1)[0], body=obj, params={'dryRun': 'All'})
            await self.update()
        await self.update(check='resources', status='success', message='资源归属与准入检查通过')

    def assert_owned(self, obj):
        if obj.get('metadata', {}).get('annotations', {}).get(OWNER) != self.installation_id:
            raise SetupError(f"发现不属于本次安装的 {obj['kind']} {obj['metadata']['name']}，已停止以保护现有部署", 409)

    @staticmethod
    def for_replace(obj, old):
        result = copy.deepcopy(obj)
        result['metadata']['resourceVersion'] = old['metadata']['resourceVersion']
        if obj['kind'] == 'Service':
            for key in ('clusterIP', 'clusterIPs', 'ipFamilies', 'ipFamilyPolicy', 'healthCheckNodePort'):
                if key in old['spec']:
                    result['spec'][key] = old['spec'][key]
            if result['spec'].get('type') in ('NodePort', 'LoadBalancer'):
                for port in result['spec']['ports']:
                    previous = next((p for p in old['spec']['ports'] if p.get('name') == port.get('name')), {})
                    if previous.get('nodePort'):
                        port['nodePort'] = previous['nodePort']
        return result

    async def apply_owned(self, obj):
        old = await self.k8s.get(obj)
        if old:
            self.assert_owned(old)
            return await self.k8s.replace(self.for_replace(obj, old))
        return await self.k8s.create(obj)

    async def deploy(self, sid):
        obj = self.deployment(sid)
        try:
            await self.update(service=sid, status='deploying', message='创建或核对部署资源')
            current = await self.k8s.get(obj)
            # Preserve already deployed healthy objects on retry when configuration did not change.
            wanted = obj['spec']['template']['metadata']['annotations']['checksum/config']
            existing = (current or {}).get('spec', {}).get('template', {}).get('metadata', {}).get('annotations', {})
            desired_images = [(c['name'], c['image']) for c in obj['spec']['template']['spec']['containers']]
            actual_images = [(c['name'], c['image']) for c in (current or {}).get('spec', {}).get('template', {}).get('spec', {}).get('containers', [])]
            if current is None or existing.get('checksum/config') != wanted or desired_images != actual_images:
                await self.apply_owned(obj)
            else:
                self.assert_owned(current)
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                current = await self.k8s.get(obj)
                if workload_ready(current, obj):
                    break
                status, message = await self.diagnose(obj, current)
                await self.update(service=sid, status=status, message=message)
                await asyncio.sleep(self.poll_interval)
            else:
                raise SetupError('等待副本就绪超时：' + message, 504)
            deadline = time.monotonic() + self.health_timeout
            # 刚就绪时依赖可能还没连上（滚动更新时旧副本也可能还在响应），在 health_timeout 内重试；
            # 重试期间把上次没通过的原因显示出来，不用等到超时才知道卡在哪
            last = ''
            while True:
                await self.update(service=sid, status='checking',
                                  message='副本已就绪，正在验证服务接口和依赖连接' + (f'；上次未通过：{last}' if last else ''))
                try:
                    await self.service_health(sid)
                    break
                except SetupError as error:
                    if time.monotonic() >= deadline:
                        raise
                    last = error.message
                    await asyncio.sleep(self.poll_interval)
            await self.update(service=sid, status='ready', message='本次部署副本及健康检查均已通过')
        except SetupError as error:
            await self.update(service=sid, status='timeout' if error.status == 504 else 'failed', message=error.message,
                              details=await self.failure_details(sid, error))

    def image_of(self, sid):
        """本次部署期望的镜像；副本就绪时 workload_ready 已核对过模板镜像，实际运行的就是它。"""
        try:
            containers = self.deployment(sid)['spec']['template']['spec']['containers']
            return '、'.join(container['image'] for container in containers) or '（未知）'
        except (StopIteration, KeyError, TypeError):
            return '（未知）'

    async def failure_details(self, sid, error=None):
        """失败服务的详情（页面可展开）：Deployment、期望镜像、健康检查请求，以及各 Pod 的实际状态和运行镜像。

        只用于展示，任何读取失败都不影响安装结果。
        """
        try:
            obj = self.deployment(sid)
            lines = [f"Deployment：{self.namespace}/{obj['metadata']['name']}",
                     *(f"期望镜像：{container['image']}" for container in obj['spec']['template']['spec']['containers'])]
            selector = ','.join(k + '=' + v for k, v in obj['spec']['selector']['matchLabels'].items())
        except (StopIteration, KeyError, TypeError, AttributeError):
            return []
        if getattr(error, 'probe', ''):
            lines.append(f'健康检查：{error.probe}')
        try:
            pods = await self.k8s.list('/api/v1/namespaces/' + quote(self.namespace, safe='') + '/pods', {'labelSelector': selector})
            lines += [pod_summary(pod) for pod in pods[:DETAIL_PODS]] or [f'Pod：没有找到（{selector}）']
            if len(pods) > DETAIL_PODS:
                lines.append(f'另有 {len(pods) - DETAIL_PODS} 个 Pod 未列出')
        except Exception:
            lines.append('Pod：读取 Pod 状态失败')
        return lines

    async def diagnose(self, obj, current):
        if not current:
            return 'deploying', '等待 Deployment 创建'
        selector = ','.join(k + '=' + v for k, v in obj['spec']['selector']['matchLabels'].items())
        pods = await self.k8s.list('/api/v1/namespaces/' + quote(self.namespace, safe='') + '/pods', {'labelSelector': selector})
        for pod in pods:
            if pod.get('metadata', {}).get('deletionTimestamp'):
                continue
            state = pod.get('status', {})
            for container in state.get('initContainerStatuses', []) + state.get('containerStatuses', []):
                waiting = container.get('state', {}).get('waiting', {}).get('reason', '')
                if waiting in ('ImagePullBackOff', 'ErrImagePull', 'InvalidImageName'):
                    return 'pulling', f'镜像拉取未成功（{waiting}），正在等待；请检查仓库连接及拉取凭据'
                if waiting in ('CrashLoopBackOff', 'CreateContainerConfigError', 'CreateContainerError'):
                    return 'starting', f'容器尚未正常启动（{waiting}）'
            if any(c.get('type') == 'PodScheduled' and c.get('status') == 'False' for c in state.get('conditions', [])):
                return 'waiting', 'Pod 等待调度，请检查节点资源、污点和调度约束'
        return 'starting', '等待本次部署的全部副本通过就绪探测'

    async def request(self, method, url, *, expected=(200,), **options):
        probe = f'{method} {url}'
        try:
            async with self.http.request(method, url, allow_redirects=False, **options) as response:
                if response.status not in expected:
                    raise ServiceCheckError(f'服务连接或健康验证失败（HTTP {response.status}）',
                                            http_status=response.status, probe=f'{probe} → HTTP {response.status}')
                chunks, size = [], 0
                async for chunk in response.content.iter_chunked(65536):
                    size += len(chunk)
                    if size > 2 * 1024 * 1024:
                        raise SetupError('健康检查响应过大', 502)
                    chunks.append(chunk)
                raw = b''.join(chunks)
                if not raw:
                    return {}
                try:
                    return json.loads(raw)
                except ValueError:
                    return {'text': raw.decode(errors='replace')}
        except (aiohttp.ClientError, asyncio.TimeoutError):
            raise ServiceCheckError('服务连接超时或证书验证失败，请检查网络与服务状态',
                                    probe=f'{probe} → 连接失败或超时') from None

    def url(self, name, port=80):
        return f'http://{name}.{self.namespace}:{port}'

    def health_hint(self, sid, error):
        """把接口检查失败换成能照着处理的说明：哪个服务、哪个镜像、哪个接口、怎么处理。"""
        name = next((deployment for service, _, deployment in SERVICES if service == sid), sid)
        status = error.http_status
        if sid in HEALTH_ENDPOINTS:
            path = HEALTH_ENDPOINTS[sid][2]
            if status == 404:
                return (f'{name} 镜像 {self.image_of(sid)} 没有安装向导健康接口（{path} 返回 HTTP 404），'
                        '镜像版本过旧；请换用包含安装向导的新版镜像后重试')
            if status in (401, 403):
                return (f'{name} 健康接口拒绝了安装服务的内部令牌（HTTP {status}），'
                        '请确认 Secret kubedoor-ai-security 没有被手动修改后重试')
            if status == 503:
                return f'{name} 已启动，但{HEALTH_DEPENDS[sid]}连接检查失败（健康接口返回 HTTP 503），请查看 {name} 日志'
        if status:
            return f'{name} 健康检查未通过（{error.probe}），请查看 {name} 日志'
        return f'无法连接 {name}（{error.probe}），请检查 Service、网络策略和 {name} 日志'

    async def service_health(self, sid):
        try:
            await self._service_health(sid)
        except ServiceCheckError as error:
            raise ServiceCheckError(self.health_hint(sid, error), http_status=error.http_status, probe=error.probe) from None

    async def _service_health(self, sid):
        headers = {'X-Kubedoor-Token': self.secrets['internal_token'], 'X-User-Name': 'kubedoor', 'X-User-Permission': 'admin'}
        if sid in HEALTH_ENDPOINTS:
            name, port, path = HEALTH_ENDPOINTS[sid]
            result = await self.request('GET', self.url(name, port) + path, headers=headers)
            if result.get('ok') is not True or result.get('installation_id') != self.installation_id:
                raise SetupError(f'{name} 健康接口没有确认本次安装（安装标识不一致），'
                                 f'请确认镜像 {self.image_of(sid)} 为包含安装向导的新版本', 502)
        elif sid == 'grafana':
            result = await self.request('GET', self.url('kubedoor-dash', 3000) + '/grafana/api/health')
            if result.get('database') != 'ok':
                raise SetupError('Grafana 数据库健康检查未通过', 502)
        elif sid == 'vmalert':
            await self.request('GET', self.url('vmalert', 8080) + '/health')
            data = await self.request('GET', self.url('vmalert', 8080) + '/api/v1/rules')
            if data.get('status') != 'success':
                raise SetupError('vmalert 规则加载检查失败', 502)
            rules = [rule for group in data.get('data', {}).get('groups', []) for rule in group.get('rules', [])]
            if not rules or any(rule.get('lastError') or rule.get('health') != 'ok' for rule in rules):
                raise SetupError('vmalert 规则尚未正常执行，请检查时序库类型、规则表达式与查询连接', 502)
        elif sid == 'alertmanager':
            await self.request('GET', self.url('alertmanager', 9093) + '/-/ready')
            await self.request('GET', self.url('alertmanager', 9093) + '/api/v2/status')
        elif sid == 'web':
            data = await self.request('GET', self.url('kubedoor-web') + '/setup-status.json')
            if data.get('installation_id') != self.installation_id:
                raise SetupError('Web 入口未返回本次安装标识', 502)
            await self.request('GET', self.url('kubedoor-web') + '/')

    async def verify_endpoints(self):
        base = self.config['public_url']
        # Confirm ownership of the external URL before sending administrator credentials.
        marker = await self.request('GET', base + '/setup-status.json')
        if marker.get('installation_id') != self.installation_id:
            raise SetupError('填写的对外地址未指向本次 KubeDoor，请检查代理或 DNS 后重试', 409)
        admin = self.config['admin']
        result = await self.request('POST', base + '/api/auth/login', json={
            'username': admin['username'], 'password': admin['password'], 'remember': False})
        if result.get('success') is not True:
            raise SetupError('管理员登录验证失败', 502)
        try:
            me = await self.request('GET', base + '/api/auth/me')
            if me.get('success') is not True:
                raise SetupError('管理员登录会话验证失败', 502)
            for uid in ('KubeDoor-Postgres', 'KubeDoor-Prometheus'):
                check = await self.request('GET', base + '/grafana/api/datasources/uid/' + uid + '/health')
                if str(check.get('status', '')).lower() != 'ok':
                    raise SetupError('Grafana 数据源连接验证失败', 502)
            if self.config['metrics'].get('write_url'):
                await self.verify_metrics(base)
            else:
                await self.update(check='write', status='skipped', message='使用已有采集链路，仅验证查询')
        finally:
            await self.request('POST', base + '/api/auth/logout', expected=(200, 204), json={})

    async def verify_metrics(self, base):
        await self.update(check='write', status='running', message='通过指标网关发送一条安装检测指标并查询确认')
        username = 'setup_probe_' + self.installation_id.replace('-', '')[:32]
        password = hashlib.sha256((self.secrets['agent_password'] + self.installation_id).encode()).hexdigest()
        conn = await asyncpg.connect(**pg_connect_options(self.config['postgres']))
        user_id = None
        try:
            previous = await conn.fetchrow('SELECT id,comment FROM auth_users WHERE username=$1', username)
            if previous:
                if previous['comment'] != 'setup:' + self.installation_id:
                    raise SetupError('安装检测账号与已有用户冲突', 409)
                user_id = previous['id']
            else:
                created = await self.request('POST', base + '/api/admin/users', json={
                    'username': username, 'password': password, 'source': 'local', 'role': 'agent',
                    'comment': 'setup:' + self.installation_id})
                user_id = created.get('data', {}).get('id')
                if not user_id:
                    raise SetupError('无法创建指标验证账号', 502)
            await conn.execute('INSERT INTO auth_agent_bindings(user_id,env) VALUES($1,$2) '
                               'ON CONFLICT(user_id) DO NOTHING', user_id, username)
            timestamp = int(time.time() * 1000)
            # No browser session on the Agent request: the gateway must exercise Basic identity.
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20), trust_env=False) as agent_http:
                try:
                    async with agent_http.post(base + '/agent/metrics/write',
                                               data=probe_write(self.installation_id, timestamp),
                                               auth=aiohttp.BasicAuth(username, password),
                                               headers={'Content-Type': 'application/x-protobuf', 'Content-Encoding': 'snappy',
                                                        'X-Prometheus-Remote-Write-Version': '0.1.0'}, allow_redirects=False) as response:
                        if response.status not in (200, 204):
                            raise SetupError(f'指标网关写入验证失败（HTTP {response.status}）', 502)
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    raise SetupError('指标网关写入连接失败', 502) from None
            query_base, auth = metrics_query_base(self.config['metrics'])
            deadline = time.monotonic() + self.health_timeout
            while time.monotonic() < deadline:
                data = await self.request('GET', query_base + '/api/v1/query',
                                          params={'query': 'kubedoor_install_probe{installation_id="' + self.installation_id + '"}'}, auth=auth)
                if data.get('status') == 'success' and any(float(row.get('value', [0, 0])[1]) == timestamp
                    for row in data.get('data', {}).get('result', [])):
                    await self.update(check='write', status='success', message='指标网关写入及查询验证通过')
                    return
                await self.update(check='write', status='running', message='已写入检测指标，等待查询可见')
                await asyncio.sleep(self.poll_interval)
            raise SetupError('指标已发送但查询尚不可见，请检查写入目标与查询目标是否一致', 504)
        finally:
            if user_id:
                await self.request('DELETE', base + '/api/admin/users/' + str(user_id), expected=(200, 204))
            await conn.close()

    async def fail(self, message):
        await self.update(phase='failed', error=message)

    async def start_finalizer(self):
        await start_finalizer(self.k8s, self.namespace, self.installation_id, self.update,
                              poll_interval=self.poll_interval)
