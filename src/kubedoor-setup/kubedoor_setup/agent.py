"""网页安装第 6 步：在本集群（控制端所在集群）安装 Agent，复用 master 的 Agent 安装流水线。

Agent 的 ClusterRole 是全权限，安装方也要有同等权限。引导资源里的专用账号
kubedoor-agent-installer 绑定了 cluster-admin；安装服务平时不持有它，只在用户选择安装时
用 TokenRequest 签发一小时有效的令牌，拼成 kubeconfig 交给 master
（/api/admin/agent-installations：inspect → preflight → start → 轮询）。
令牌只留在内存里，不写入安装状态、日志和错误信息；收尾任务会删除绑定和账号。
"""
from __future__ import annotations

import asyncio
import base64
import copy
import json
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import aiohttp

from .config import metrics_endpoints, url_credentials
from .errors import SetupError
from .kubernetes import AGENT_INSTALLER, RBAC, service_account_token
from .repository import check_row, utcnow

CONTEXT = 'in-cluster'
MASTER_API = '/api/admin/agent-installations'
CHOICE_SECONDS = 2 * 3600       # 进入本步骤或最近一次失败后多久不操作，自动按“不安装”收尾
INSTALL_SECONDS = 20 * 60       # 一次安装从签发令牌到验证指标的总时长上限
TOKEN_SECONDS = 3600            # 交给 master 安装用的令牌有效期
PROBE_TOKEN_SECONDS = 600       # 只用来检测权限的令牌：TokenRequest 允许的最短有效期
STALE_SECONDS = 90              # 状态这么久没刷新、本进程又没有对应任务，视为安装服务重启过
CONCLUDED = ('succeeded', 'partial', 'skipped')
ENV = re.compile(r'[A-Za-z0-9_-]{1,100}')
NAMESPACE = re.compile(r'[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?')
PULL_SECRET = re.compile(r'[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?')
UNAVAILABLE = '未授予在本集群安装 Agent 的临时集群权限，安装完成后可在控制台上传 kubeconfig 接入本集群'
FAILED = '在本集群安装 Agent 未成功：可修改选项后重试，或跳过直接完成安装'


class Superseded(Exception):
    """状态已被用户操作或另一个进程改变，本次后台安装不再写状态。"""


class AgentFailure(Exception):
    def __init__(self, message, *, steps=None, checks=None, takeover=None):
        super().__init__(message)
        self.message, self.steps, self.checks = message, steps, checks
        # 预检发现本集群里已有 Agent 资源时的清单：页面据此提供「强制覆盖安装」
        self.takeover = takeover


class MasterError(SetupError):
    """调用 master 失败；http_status 为 HTTP 状态码，连不上时为 0。"""

    def __init__(self, message, http_status):
        super().__init__(message, 502)
        self.http_status = http_status

    @property
    def transient(self):
        return self.http_status in (0, 429) or self.http_status >= 500


def deadline_after(seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def seconds_since(value):
    """距 ISO 时间过去的秒数；时间无效时视为很久以前。"""
    try:
        return time.time() - datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return float('inf')


def passed(deadline):
    """截止时间已到；时间无效也按已到处理，避免临时集群权限一直保留。"""
    return seconds_since(deadline) >= 0


def clean_text(value, limit=300):
    if not isinstance(value, str):
        return ''
    return ''.join(char if char.isprintable() else ' ' for char in value).strip()[:limit]


def clean_rows(rows, text=None, limit=20):
    """master 任务的 steps / checks，只保留 name、status、message（经 text 处理，默认截断）和 forceable 标记。"""
    if not isinstance(rows, list):
        return []
    text = text or (lambda value: clean_text(value, 500))
    return [{'name': clean_text(row.get('name'), 200), 'status': clean_text(row.get('status'), 40),
             'message': text(row.get('message')), **({'forceable': True} if row.get('forceable') is True else {})}
            for row in rows[:limit] if isinstance(row, dict)]


def clean_takeover(items, text, limit=50):
    """预检列出的、需要强制覆盖才能接管的已有资源"""
    if not isinstance(items, list):
        return []
    return [{'kind': clean_text(item.get('kind'), 80), 'name': clean_text(item.get('name'), 200),
             'namespace': clean_text(item.get('namespace'), 200), 'reason': text(item.get('reason'))}
            for item in items[:limit] if isinstance(item, dict)]


def write_url_of(config):
    """第 3 步保存的远程写地址（带账号密码）；旧版向导保存的配置也能读出。"""
    return metrics_endpoints(config.get('metrics') or {})[1]


def master_url(namespace):
    """本集群 Agent 直连 master 的集群内地址（WebSocket），不经过 Web 入口。"""
    return f'http://kubedoor-master.{namespace}'


def agent_defaults(namespace, config):
    return {'namespace': namespace, 'mode': 'full' if write_url_of(config) else 'agent',
            'ksm': 'auto', 'node_exporter': 'auto', 'env': ''}


def new_agent_state(namespace, config):
    return {'status': 'choice', 'deadline': deadline_after(CHOICE_SECONDS), 'defaults': agent_defaults(namespace, config),
            'steps': [], 'checks': [], 'updated_at': utcnow(),
            'message': '控制端已就绪：可以在本集群安装 Agent，也可以跳过直接完成安装'}


def unavailable_agent_state():
    return {'status': 'unavailable', 'steps': [], 'checks': [], 'message': UNAVAILABLE, 'updated_at': utcnow()}


def start_attempt(state, options, attempt, force=False):
    """force：用户确认可以接管本集群里已有的 Agent 资源（强制覆盖安装）；记在状态里，安装服务重启后继续时也知道"""
    agent = state['agent']
    agent.update(status='running', options=options, attempt=attempt, started_at=utcnow(), steps=[], checks=[],
                 force=bool(force), message='正在准备在本集群安装 Agent', updated_at=utcnow())
    for key in ('deadline', 'error', 'task_id', 'takeover'):
        agent.pop(key, None)
    check_row(state, 'cleanup', 'pending', '等待本集群 Agent 安装完成')


def mark_failed(state, message, *, steps=None, checks=None, takeover=None):
    agent = state['agent']
    agent.update(status='failed', error=message or '在本集群安装 Agent 未成功', message=FAILED,
                 deadline=deadline_after(CHOICE_SECONDS), checks=checks or [], updated_at=utcnow())
    if steps is not None:
        agent['steps'] = steps
    if takeover:
        agent['takeover'] = takeover
    else:
        agent.pop('takeover', None)
    check_row(state, 'cleanup', 'pending', '等待重试或跳过本集群 Agent 安装')


def mark_skipped(state, message):
    agent = state.setdefault('agent', {})
    agent.update(status='skipped', message=message, updated_at=utcnow())
    for key in ('deadline', 'error'):
        agent.pop(key, None)


def normalize_options(raw, defaults, write_url):
    """校验规则与 master 的 bundle.normalize_options 一致；不合法时返回 422 和中文说明。"""
    if not isinstance(raw, dict):
        raise SetupError('请填写在本集群安装 Agent 的选项', 422)
    env = raw.get('env', '')
    if not isinstance(env, str) or not ENV.fullmatch(env.strip()):
        raise SetupError('请填写集群名称：1 至 100 个字母、数字、下划线或中划线', 422)
    namespace = raw.get('namespace') or defaults.get('namespace', '')
    if not isinstance(namespace, str) or not NAMESPACE.fullmatch(namespace.strip()):
        raise SetupError('命名空间需为有效的 Kubernetes 名称：小写字母、数字或中划线，不超过 63 个字符', 422)
    aliases = raw.get('aliases')
    aliases = [] if aliases is None else aliases
    if (not isinstance(aliases, list) or len(aliases) > 20
            or any(not isinstance(alias, str) or len(alias) > 100 or not alias.isprintable() for alias in aliases)):
        raise SetupError('集群别名最多 20 个，每个不超过 100 个字符', 422)
    mode = raw.get('mode') or defaults.get('mode') or 'agent'
    if mode not in ('full', 'agent'):
        raise SetupError('安装方式只能选择 full（Agent 和指标采集）或 agent（仅 Agent）', 422)
    if mode == 'full' and not write_url:
        raise SetupError('第 3 步没有保存时序库远程写地址，只能选择仅安装 Agent', 422)
    result = {'env': env.strip(), 'aliases': list(dict.fromkeys(alias.strip() for alias in aliases if alias.strip())),
              'namespace': namespace.strip(), 'mode': mode}
    for key, title in (('ksm', 'kube-state-metrics'), ('node_exporter', 'node-exporter')):
        value = raw.get(key) or 'auto'
        if value not in ('auto', 'install', 'reuse'):
            raise SetupError(f'{title} 只能选择 auto（自动检测）、install（安装）或 reuse（复用已有）', 422)
        result[key] = value
    return result


def bootstrap_pull_secret():
    """引导时指定的镜像拉取 Secret（只存在于安装服务所在命名空间）。"""
    try:
        value = json.loads(os.environ.get('SETUP_DEFAULTS') or '{}').get('IMAGE_PULL_SECRET', '')
    except (ValueError, AttributeError):
        return ''
    return value if isinstance(value, str) and PULL_SECRET.fullmatch(value) else ''


def master_options(options, namespace, write_url):
    """交给 master 预检的安装选项；镜像仓库、版本等其余参数沿用 master 的默认值。"""
    result = {key: copy.deepcopy(options[key]) for key in ('env', 'aliases', 'namespace', 'mode', 'ksm', 'node_exporter')}
    result['master_url'] = master_url(namespace)
    # vmagent 直连时序库，和控制台「接入集群」一样；这里明确带上第 3 步的远程写地址
    result['remote_write_url'] = write_url if options['mode'] == 'full' else ''
    pull_secret = bootstrap_pull_secret()
    if pull_secret and options['namespace'] == namespace:
        result['image_pull_secret'] = pull_secret
    return result


def kubeconfig(server, ca, token, namespace):
    """给 master 的 kubeconfig：集群内 API 地址 + 本集群 CA + 安装账号的临时令牌。"""
    return json.dumps({
        'apiVersion': 'v1', 'kind': 'Config', 'current-context': CONTEXT,
        'clusters': [{'name': CONTEXT, 'cluster': {
            'server': server, 'certificate-authority-data': base64.b64encode(ca).decode('ascii')}}],
        'users': [{'name': AGENT_INSTALLER, 'user': {'token': token}}],
        'contexts': [{'name': CONTEXT, 'context': {'cluster': CONTEXT, 'user': AGENT_INSTALLER, 'namespace': namespace}}],
    })


async def installer_available(k8s):
    """签一个短期令牌并用它做 SelfSubjectAccessReview：能创建 ClusterRoleBinding 才算可用，任何异常都算不可用。"""
    try:
        token = await service_account_token(k8s, k8s.namespace, AGENT_INSTALLER, PROBE_TOKEN_SECONDS)
        return await k8s.allowed('ClusterRoleBinding', 'create', version=RBAC, token=token) is True
    except Exception:
        return False


def running_message(steps):
    current = next((row for row in steps if row.get('status') == 'running' and row.get('name')), None)
    return '正在安装 Agent：' + current['name'] if current else '正在安装 Agent'


def ai_connection_note(result, scrub):
    """master 在 Agent 上线后自动配置 AI Kubeconfig 的结果，接在第 6 步结果说明后面；旧版 master 没有这项时不加"""
    if not isinstance(result, dict):
        return ''
    if result.get('status') == 'saved':
        return '；已用 Agent 服务账号自动配置 AI Kubeconfig'
    if result.get('status') == 'failed' and result.get('message'):
        return '。' + scrub(result['message'])
    return ''


class MasterClient:
    """以内置管理员身份（内部令牌）调用 master 的 Agent 安装接口。"""

    def __init__(self, base, internal_token):
        self.base = base.rstrip('/') + MASTER_API
        self.headers = {'X-Kubedoor-Token': internal_token, 'X-User-Name': 'kubedoor'}

    async def call(self, method, path, body=None, *, timeout=30):
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout, connect=10), trust_env=False) as session:
                async with session.request(method, self.base + path, json=body, headers=self.headers,
                                           allow_redirects=False) as response:
                    status, chunks, size = response.status, [], 0
                    async for chunk in response.content.iter_chunked(65536):
                        size += len(chunk)
                        if size > 4 * 1024 * 1024:
                            raise MasterError('控制服务响应过大', 502)
                        chunks.append(chunk)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            raise MasterError('无法连接控制服务 kubedoor-master，请确认它正常运行', 0) from None
        try:
            data = json.loads(b''.join(chunks)) if chunks else {}
        except (ValueError, UnicodeError):
            data = {}
        data = data if isinstance(data, dict) else {}
        if status < 300 and data.get('success') is True and isinstance(data.get('data'), dict):
            return data['data']
        # 这里不截短：调用方先去掉敏感内容再截断
        message = clean_text(data.get('message'), 4000) or clean_text(data.get('error'), 4000)
        raise MasterError(message or f'控制服务返回 HTTP {status}', status)

    async def inspect(self, config):
        return await self.call('POST', '/inspect', {'kubeconfig': config})

    async def preflight(self, task_id, options, force=False):
        # master 的预检本身最长 100 秒
        return await self.call('POST', '/' + quote(task_id, safe='') + '/preflight',
                               {'context': CONTEXT, 'options': options, 'force': bool(force)}, timeout=180)

    async def start(self, task_id, revision):
        return await self.call('POST', '/' + quote(task_id, safe='') + '/start', {'revision': revision})

    async def get(self, task_id):
        return await self.call('GET', '/' + quote(task_id, safe=''), timeout=20)


class AgentInstall:
    """一次在本集群安装 Agent 的尝试；安装服务重启后按记下的 master 任务 id 继续跟踪。"""
    poll_interval = 3
    timeout = INSTALL_SECONDS
    heartbeat = 30          # 进度没变时也定期刷新 updated_at，让看门狗知道任务还活着
    client = MasterClient

    def __init__(self, repo, attempt):
        self.repo, self.k8s, self.attempt = repo, repo.k8s, attempt
        self.namespace = repo.namespace
        self.secrets = []
        self.written, self.written_at = {}, 0.0
        # master 在 Agent 上线后自动配置 AI Kubeconfig 的结果（{status, message}），拼进第 6 步的结果说明
        self.ai_connection = None

    def api_base(self):
        return master_url(self.namespace)

    def scrub(self, value):
        """写进状态的文字一律先去掉令牌和远程写地址、密码（master 本身不会回显，这里再兜一层），再截短。"""
        text = value if isinstance(value, str) else ''
        for secret in self.secrets:
            if isinstance(secret, str) and len(secret) >= 6:
                text = text.replace(secret, '***')
        return clean_text(text, 500)

    def owns(self, state):
        agent = state.get('agent') or {}
        return state['phase'] == 'agent' and agent.get('status') == 'running' and agent.get('attempt') == self.attempt

    async def run(self, resume=False):
        """执行（或重启后继续跟踪）一次安装；返回 True 表示 Agent 已装好，应进入收尾。"""
        try:
            status, steps, detail = await self.install(resume)
        except Superseded:
            return False
        except AgentFailure as failure:
            await self.fail(failure.message, steps=failure.steps, checks=failure.checks, takeover=failure.takeover)
            return False
        except SetupError as error:
            await self.fail(error.message)
            return False
        except Exception:
            await self.fail('在本集群安装 Agent 时发生异常，可修改选项后重试，或跳过直接完成安装')
            return False
        return await self.succeed(status, steps, detail)

    async def install(self, resume):
        state = await self.repo.load()
        if not self.owns(state):
            raise Superseded()
        agent = state['agent']
        private = await self.repo.load_private(state)
        write_url = write_url_of(private['config'])
        password = url_credentials(write_url)[2] if write_url else ''
        self.secrets = [write_url, password, quote(password, safe='')] if write_url else []
        master = self.client(self.api_base(), private['secrets']['internal_token'])
        deadline = time.time() + self.timeout - min(seconds_since(agent.get('started_at')), self.timeout)
        task_id = agent.get('task_id')
        if not task_id:
            if resume:
                raise AgentFailure('安装服务重启时本次安装尚未开始，请重试或跳过')
            task_id = await self.begin(master, agent['options'], write_url, agent.get('force') is True)
        return await self.follow(master, task_id, deadline)

    async def master_call(self, call, stage):
        try:
            return await call
        except MasterError as error:
            raise AgentFailure(stage + '：' + self.scrub(error.message)) from None

    async def begin(self, master, options, write_url, force=False):
        await self.progress(message='正在签发临时安装令牌')
        try:
            token = await service_account_token(self.k8s, self.namespace, AGENT_INSTALLER, TOKEN_SECONDS)
        except SetupError as error:
            raise AgentFailure('无法为 Agent 安装账号签发临时令牌：' + error.message) from None
        self.secrets.append(token)
        try:
            ca = self.k8s.ca_path.read_bytes()
        except OSError:
            raise AgentFailure('无法读取本集群的 CA 证书，不能生成安装用的 kubeconfig') from None
        config = kubeconfig(self.k8s.server, ca, token, self.namespace)
        await self.progress(message='正在创建 Agent 安装任务')
        task = await self.master_call(master.inspect(config), '创建安装任务失败')
        task_id = task.get('id')
        try:
            task_id = str(uuid.UUID(task_id))
        except (TypeError, ValueError, AttributeError):
            raise AgentFailure('控制服务返回的安装任务无效') from None
        await self.progress(task_id=task_id, message='正在预检本集群的安装条件')
        task = await self.master_call(master.preflight(task_id, master_options(options, self.namespace, write_url), force),
                                      '安装预检未通过')
        if task.get('status') != 'ready':
            checks = [row for row in clean_rows(task.get('checks'), self.scrub) if row['status'] == 'error']
            reason = '；'.join(row['message'] for row in checks if row['message']) or '请检查安装选项后重试'
            takeover = clean_takeover(task.get('takeover'), self.scrub) if any(row.get('forceable') for row in checks) else []
            raise AgentFailure('安装预检未通过：' + reason, checks=checks, takeover=takeover)
        task = await self.master_call(master.start(task_id, task.get('revision')), '启动安装失败')
        steps = clean_rows(task.get('steps'), self.scrub)
        await self.progress(steps=steps, checks=[], message=running_message(steps))
        return task_id

    async def follow(self, master, task_id, deadline):
        timeout = '在本集群安装 Agent 超过 20 分钟仍未完成；可稍后在控制台的 Agent 安装记录里查看，或修改选项后重试'
        steps = None
        while True:
            try:
                task = await master.get(task_id)
            except MasterError as error:
                if not error.transient:
                    raise AgentFailure('查询安装进度失败：' + self.scrub(error.message), steps=steps) from None
                if time.time() >= deadline:
                    raise AgentFailure(timeout, steps=steps) from None
                await self.progress(quiet=True, message='暂时无法查询安装进度，正在重试')
                await asyncio.sleep(self.poll_interval)
                continue
            status = task.get('status')
            steps = clean_rows(task.get('steps'), self.scrub)
            detail = self.scrub(task.get('error'))
            if status in ('succeeded', 'partial'):
                self.ai_connection = task.get('ai_connection')
                return status, steps, detail
            if status != 'running':
                reason = detail or {'interrupted': '控制服务重启，安装已中断', 'expired': '安装任务已过期',
                                    'draft': '安装服务重启前尚未开始安装', 'ready': '安装服务重启前尚未开始安装'
                                    }.get(status, '安装任务状态异常')
                raise AgentFailure('在本集群安装 Agent 失败：' + reason, steps=steps)
            if time.time() >= deadline:
                raise AgentFailure(timeout, steps=steps)
            await self.progress(quiet=True, steps=steps, message=running_message(steps))
            await asyncio.sleep(self.poll_interval)

    async def progress(self, *, quiet=False, **fields):
        """写入进度；内容没变时只按 heartbeat 间隔刷新。quiet 时忽略暂时的写入失败，下一轮再写。"""
        if (all(self.written.get(key) == value for key, value in fields.items())
                and time.monotonic() - self.written_at < self.heartbeat):
            return
        try:
            async with self.repo.lock:
                state = await self.repo.load()
                if not self.owns(state):
                    raise Superseded()
                state['agent'].update(copy.deepcopy(fields), updated_at=utcnow())
                await self.repo.save(state)
        except SetupError:
            if quiet:
                return
            raise
        self.written.update(copy.deepcopy(fields))
        self.written_at = time.monotonic()

    async def succeed(self, status, steps, detail):
        async with self.repo.lock:
            state = await self.repo.load()
            if not self.owns(state):
                return False
            agent = state['agent']
            env = (agent.get('options') or {}).get('env', '')
            if status == 'succeeded':
                message = f'Agent 已在本集群安装并上线，集群名称 {env}'
            else:
                message = ('Agent 已上线，但指标采集验证未通过' + ('：' + detail if detail else '')
                           + '。安装完成后可在控制台查看该集群的指标，或在 Agent 管理的安装记录里点「重新验证指标采集」'
                           '（只查询时序库，不需要 kubeconfig）')
            message += ai_connection_note(self.ai_connection, self.scrub)
            agent.update(status=status, steps=steps, checks=[], message=message, updated_at=utcnow())
            for key in ('deadline', 'error'):
                agent.pop(key, None)
            await self.repo.save(state)
            return True

    async def fail(self, message, *, steps=None, checks=None, takeover=None):
        async with self.repo.lock:
            state = await self.repo.load()
            if not self.owns(state):
                return
            mark_failed(state, self.scrub(message), steps=steps, checks=checks, takeover=takeover)
            await self.repo.save(state)
