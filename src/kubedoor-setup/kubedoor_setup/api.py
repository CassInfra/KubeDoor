"""Authenticated first-install HTTP API, independent of the application database."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

from aiohttp import web

from .agent import (CONCLUDED, STALE_SECONDS, AgentInstall, agent_defaults, installer_available, mark_failed,
                    mark_skipped, normalize_options, passed, start_attempt, write_url_of)
from .config import ensure_secrets, final_config, metrics_config, postgres_config
from .errors import SetupError
from .probes import check_metrics, check_postgres
from .repository import check_row, public_marker, public_state, utcnow
from .runner import start_finalizer

COOKIE = 'kubedoor_setup_session'
SESSION_SECONDS = 8 * 3600
API = '/setup-api'


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def check_revision(state, body):
    if type(body.get('revision')) is not int or body['revision'] != state['revision']:
        raise SetupError('配置已更新，请刷新页面后重试', 409)


def editable(state):
    if state['phase'] not in ('draft', 'ready', 'failed', 'interrupted'):
        raise SetupError('当前安装阶段不能修改配置或重复启动', 409)


def checked(state, private):
    checks = state.get('checked', {})
    config = private['config']
    return (checks.get('postgres') == fingerprint(config['postgres']) and
            checks.get('metrics') == fingerprint(config['metrics']) and
            bool(config['admin'].get('password')) and bool(config.get('public_url')))


async def body_of(request):
    if request.content_type != 'application/json':
        raise SetupError('请求必须使用 JSON 格式', 415)
    try:
        body = await request.json()
    except (ValueError, UnicodeError):
        raise SetupError('请求 JSON 格式不正确', 400) from None
    if not isinstance(body, dict):
        raise SetupError('请求参数必须为对象', 400)
    return body


class SetupAPI:
    def __init__(self, repo, *, key, runner_factory=None, pg_probe=check_postgres, metrics_probe=check_metrics,
                 agent_installer=AgentInstall):
        if not isinstance(key, str) or len(key) < 32:
            raise ValueError('SETUP_KEY must contain at least 32 characters')
        self.repo, self.key = repo, key.encode()
        self.runner_factory = runner_factory
        self.pg_probe, self.metrics_probe = pg_probe, metrics_probe
        self.agent_installer = agent_installer
        self.task = None
        self.agent_task = None
        self.finalize_poll = 3
        self.recovery_task = None
        self.attempts = {}

    def authenticated(self, request, state):
        raw = request.cookies.get(COOKIE, '')
        try:
            expires, nonce, signature = raw.split('.')
            expires_int = int(expires)
            if expires_int < time.time() or expires_int > time.time() + SESSION_SECONDS + 5 or len(nonce) != 32:
                return False
            expected = hmac.new(self.key, f'{state["id"]}.{expires}.{nonce}'.encode(), hashlib.sha256).hexdigest()
            return hmac.compare_digest(signature, expected)
        except (ValueError, TypeError):
            return False

    def session_token(self, state):
        expires, nonce = str(int(time.time()) + SESSION_SECONDS), secrets.token_hex(16)
        signature = hmac.new(self.key, f'{state["id"]}.{expires}.{nonce}'.encode(), hashlib.sha256).hexdigest()
        return f'{expires}.{nonce}.{signature}'

    async def current(self, request, *, auth=True):
        state = await self.repo.load()
        if auth and not self.authenticated(request, state):
            raise SetupError('请先输入安装密钥验证身份', 401)
        return state

    async def state_response(self, state, private=None, **kwargs):
        private = private or await self.repo.load_private(state)
        return web.json_response({'success': True, 'data': public_state(state, private)}, **kwargs)

    async def status(self, request):
        state = await self.current(request, auth=False)
        return web.json_response({'success': True, 'data': {**public_marker(state), 'authenticated': self.authenticated(request, state)}})

    async def session(self, request):
        state = await self.current(request, auth=False)
        if state['phase'] == 'succeeded':
            raise SetupError('首次安装已完成，请使用控制台登录', 409)
        body = await body_of(request)
        now = time.monotonic()
        address = request.remote or 'unknown'
        self.attempts = {key: value for key, value in self.attempts.items() if value[0] > now - 300}
        started, count = self.attempts.get(address, (now, 0))
        if count >= 10:
            raise SetupError('安装密钥尝试次数过多，请五分钟后重试', 429)
        value = body.get('key')
        if not isinstance(value, str) or not hmac.compare_digest(value.encode(), self.key):
            self.attempts[address] = (started, count + 1)
            raise SetupError('安装密钥不正确', 401)
        self.attempts.pop(address, None)
        response = web.json_response({'success': True, 'data': {'phase': state['phase'], 'authenticated': True, 'installation_id': state['id']}})
        secure = request.secure or request.headers.get('X-Forwarded-Proto', '').lower() == 'https'
        response.set_cookie(COOKIE, self.session_token(state), max_age=SESSION_SECONDS, httponly=True,
                            secure=secure, samesite='Strict', path=API)
        return response

    async def get_state(self, request):
        state = await self.current(request)
        if state['phase'] in ('draft', 'ready') and not state.get('environment_checked'):
            async with self.repo.lock:
                state = await self.current(request)
                if not state.get('environment_checked'):
                    await self.environment(state)
                    await self.repo.save(state)
        return await self.state_response(state)

    async def environment(self, state):
        try:
            version = await self.repo.k8s.request('GET', '/version')
            permissions = [('Deployment', 'create', 'apps/v1'), ('Deployment', 'patch', 'apps/v1'),
                           ('ConfigMap', 'create', 'v1'), ('ConfigMap', 'update', 'v1'),
                           ('Secret', 'create', 'v1'), ('Service', 'create', 'v1'), ('Job', 'create', 'batch/v1')]
            allowed = await asyncio.gather(*(self.repo.k8s.allowed(kind, verb, version=api_version) for kind, verb, api_version in permissions))
            if not all(allowed):
                raise SetupError('安装身份缺少部署所需权限，请重新应用 Web 引导安装资源', 403)
            state['environment'] = {'namespace': self.repo.namespace, 'server': self.repo.k8s.server, 'version': version.get('gitVersion', '')}
            state['environment_checked'] = True
            check_row(state, 'environment', 'success', 'Kubernetes 连接和控制端部署权限检查通过')
        except SetupError as error:
            state['environment_checked'] = False
            check_row(state, 'environment', 'error', error.message)
        # 只是提示：没有临时集群权限也能安装控制端，完成后再上传 kubeconfig 接入本集群
        if await installer_available(self.repo.k8s):
            check_row(state, 'agent_installer', 'success', '已授予临时集群权限，仅在你选择在本集群安装 Agent 时使用，安装完成后撤销')
        else:
            check_row(state, 'agent_installer', 'warning', '未授予在本集群安装 Agent 的临时集群权限，安装完成后可在控制台上传 kubeconfig 接入本集群')

    async def probe(self, request):
        section = request.match_info['section']
        if section not in ('postgres', 'metrics'):
            raise SetupError('未知的检测类型', 404)
        body = await body_of(request)
        async with self.repo.lock:
            state = await self.current(request)
            editable(state)
            check_revision(state, body)
            private = ensure_secrets(await self.repo.load_private(state))
            previous = private['config'][section]
            normalized = (postgres_config if section == 'postgres' else metrics_config)(body.get(section), previous)
            if state.get('target_locked') and section == 'postgres' and any(normalized[key] != previous[key] for key in ('host', 'port', 'database', 'user')):
                raise SetupError('已开始安装，不能切换目标数据库；可修正密码或 SSL 设置后重试', 409)
            private['config'][section] = normalized
            state.setdefault('checked', {}).pop(section, None)
            state['revision'] += 1
            state['phase'] = 'failed' if state.get('target_locked') else 'draft'
            state.pop('error', None)
            check_row(state, section, 'running', '正在检查连接和权限')
            await self.repo.write(state, private)
            try:
                if section == 'postgres':
                    result = await self.pg_probe(normalized, state['id'])
                else:
                    result = await self.metrics_probe(normalized)
            except SetupError as error:
                check_row(state, section, 'error', error.message)
                state['error'] = error.message
                if not state.get('target_locked'):
                    state['step'] = 2 if section == 'postgres' else 3
                await self.repo.save(state)
                return web.json_response({'success': False, 'message': error.message, 'error': error.message,
                                          'data': public_state(state, private)}, status=error.status)
            state['checked'][section] = fingerprint(normalized)
            check_row(state, section, result.get('status', 'success'), result['message'])
            state['step'] = max(state.get('step', 1), 3 if section == 'postgres' else 4)
            if checked(state, private) and not state.get('target_locked'):
                state['phase'] = 'ready'
            await self.repo.save(state)
            return await self.state_response(state, private)

    async def configure(self, request):
        body = await body_of(request)
        async with self.repo.lock:
            state = await self.current(request)
            editable(state)
            check_revision(state, body)
            private = ensure_secrets(await self.repo.load_private(state))
            values = final_config(body, private['config'])
            if state.get('target_locked') and values['admin'] != private['config']['admin']:
                raise SetupError('管理员已在安装时初始化，请完成安装后在控制台修改密码', 409)
            if state.get('target_locked') and values['components'] != private['config']['components']:
                raise SetupError('已开始部署，不能改变本次安装的组件；请修复失败服务后重试', 409)
            private['config'].update(values)
            state['public_url'] = values['public_url']
            state['revision'] += 1
            state['step'] = 5
            state.pop('error', None)
            state['phase'] = ('failed' if state.get('target_locked') else 'ready') if checked(state, private) else ('failed' if state.get('target_locked') else 'draft')
            await self.repo.write(state, private)
            return await self.state_response(state, private)

    async def start(self, request):
        body = await body_of(request)
        retry = request.path.endswith('/retry')
        async with self.repo.lock:
            state = await self.current(request)
            editable(state)
            check_revision(state, body)
            if retry and state['phase'] not in ('failed', 'interrupted'):
                raise SetupError('当前没有需要重试的安装任务', 409)
            if self.task and not self.task.done():
                raise SetupError('安装任务正在运行，请等待状态更新', 409)
            private = ensure_secrets(await self.repo.load_private(state))
            if not checked(state, private):
                raise SetupError('请先完成数据库、时序库检测以及管理员和访问地址设置', 409)
            if self.runner_factory is None:
                raise SetupError('安装执行器未配置', 503)
            await self.environment(state)
            if not state.get('environment_checked'):
                await self.repo.save(state)
                raise SetupError('环境检查未通过，请先修复安装权限或集群连接', 409)
            state.update(phase='running', step=5, target_locked=True, run_id=secrets.token_hex(16),
                         started_at=state.get('started_at') or utcnow())
            state.pop('error', None)
            state['revision'] += 1
            await self.repo.write(state, private)
            self.task = asyncio.create_task(self._run(state['run_id']), name='kubedoor-first-install')
            return await self.state_response(state, private, status=202)

    async def _run(self, run_id):
        try:
            runner = self.runner_factory(self.repo, self.repo.k8s)
            await runner.run()
        except asyncio.CancelledError:
            await self._mark_failed('安装服务已停止，任务进度已保留；恢复后可重试', phase='interrupted', run_id=run_id)
            raise
        except Exception as error:
            message = error.message if isinstance(error, SetupError) else '安装执行失败，进度已保留，请检查服务状态后重试'
            await self._mark_failed(message, run_id=run_id)

    async def _mark_failed(self, message, *, phase='failed', run_id):
        async with self.repo.lock:
            state = await self.repo.load()
            if state.get('run_id') == run_id and state['phase'] not in ('succeeded', 'finalizing', 'agent'):
                state.update(phase=phase, error=message)
                await self.repo.save(state)

    def agent_busy(self):
        return bool(self.agent_task and not self.agent_task.done())

    def agent_step(self, state, body):
        """第 6 步接口的公共检查：阶段、修订号，返回可修改的 state['agent']。"""
        if state['phase'] != 'agent':
            raise SetupError('当前不在“在本集群安装 Agent”步骤', 409)
        check_revision(state, body)
        if not isinstance(state.get('agent'), dict):
            state['agent'] = {}
        return state['agent']

    async def agent_install(self, request):
        body = await body_of(request)
        async with self.repo.lock:
            state = await self.current(request)
            agent = self.agent_step(state, body)
            if agent.get('status') not in ('choice', 'failed') or self.agent_busy():
                raise SetupError('Agent 安装已在进行或已经结束，请刷新页面查看状态', 409)
            private = await self.repo.load_private(state)
            defaults = agent.get('defaults') or agent_defaults(self.repo.namespace, private['config'])
            options = normalize_options(body.get('options'), defaults, write_url_of(private['config']))
            start_attempt(state, options, secrets.token_hex(16))
            state['revision'] += 1
            await self.repo.save(state)
            self.agent_task = asyncio.create_task(self._agent_install(state['agent']['attempt']), name='kubedoor-agent-install')
            return await self.state_response(state, private, status=202)

    async def agent_skip(self, request):
        body = await body_of(request)
        async with self.repo.lock:
            state = await self.current(request)
            agent = self.agent_step(state, body)
            if agent.get('status') == 'running':
                raise SetupError('Agent 正在安装，请等待结果后再选择', 409)
            if self.agent_busy():
                raise SetupError('收尾任务正在启动，请刷新页面查看状态', 409)
            if agent.get('status') not in CONCLUDED:
                mark_skipped(state, '已跳过在本集群安装 Agent；安装完成后可在控制台上传 kubeconfig 接入本集群')
                state['revision'] += 1
                await self.repo.save(state)
            task = self.agent_task = asyncio.create_task(self._agent_finish(), name='kubedoor-setup-agent-finish')
        # 通常一两秒内即转为 finalizing；仍未转入时页面继续轮询，看门狗会重试
        await asyncio.wait({task}, timeout=15)
        return await self.state_response(await self.repo.load())

    async def _agent_install(self, attempt, resume=False):
        try:
            concluded = await self.agent_installer(self.repo, attempt).run(resume=resume)
        except asyncio.CancelledError:
            raise
        except Exception:
            # 结果没写进状态：看门狗稍后按记下的 master 任务继续跟踪，或标记失败
            return
        if concluded:
            await self._agent_finish()

    async def _agent_finish(self):
        """第 6 步已有结论（装好、部分成功或不装）：创建收尾 Job。

        失败时状态仍停在 agent 阶段，看门狗稍后重试；Job 创建失败时 start_finalizer 已转为 failed，可重试安装。
        """
        try:
            state = await self.repo.load()
            await start_finalizer(self.repo.k8s, self.repo.namespace, state['id'], self._agent_transition,
                                  poll_interval=self.finalize_poll)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def _agent_transition(self, **fields):
        """start_finalizer 的状态写入：只有第 6 步已有结论时才从 agent 进入 finalizing。"""
        async with self.repo.lock:
            state = await self.repo.load()
            expected = 'finalizing' if fields.get('phase') == 'failed' else 'agent'
            if state['phase'] != expected or (state.get('agent') or {}).get('status') not in CONCLUDED:
                raise SetupError('安装状态已改变，停止收尾', 409)
            if fields.get('phase') == 'finalizing':
                check_row(state, 'cleanup', 'running', '关闭初始化入口并回收临时安装权限')
            state.update(fields)
            await self.repo.save(state)

    async def startup(self, app):
        # A second Pod must not turn a healthy running controller into a retryable task.
        # Every runner status save refreshes updated_at; abandoned runs expire after 90s.
        self.recovery_task = asyncio.create_task(self._recover_abandoned(), name='kubedoor-setup-recovery')

    async def _recover_abandoned(self):
        while True:
            try:
                await self._recover_once()
            except SetupError:
                # API outages and concurrent resourceVersion changes are retried;
                # never abandon the watchdog or change a task without persisted state.
                pass
            await asyncio.sleep(15)

    async def _recover_once(self):
        async with self.repo.lock:
            state = await self.repo.load()
            try:
                updated = datetime.fromisoformat(state.get('updated_at', '')).timestamp()
            except (ValueError, TypeError):
                updated = 0
            if state['phase'] == 'running' and time.time() - updated > 90 and not (self.task and not self.task.done()):
                state.update(phase='interrupted', error='安装服务已重启，已保存进度，可点击重试继续')
                await self.repo.save(state)
            elif state['phase'] == 'finalizing':
                job_path = '/apis/batch/v1/namespaces/' + quote(self.repo.namespace, safe='') + '/jobs/kubedoor-setup-finalize'
                job = await self.repo.k8s.request('GET', job_path, missing_ok=True)
                terminal_failure = job and any(condition.get('type') == 'Failed' and condition.get('status') == 'True'
                                               for condition in job.get('status', {}).get('conditions', []))
                if terminal_failure or (job is None and time.time() - updated > 90):
                    message = '安装收尾任务失败，请查看收尾状态后重试' if terminal_failure else '安装收尾任务未启动，进度已保留，请重试'
                    state.update(phase='failed', error=message)
                    check_row(state, 'cleanup', 'error', message)
                    await self.repo.save(state)
            elif state['phase'] == 'agent':
                await self._recover_agent(state, updated)

    async def _recover_agent(self, state, updated):
        """持有 repo.lock 时调用：到期自动按不安装收尾、重启后继续跟踪安装、补建收尾任务。"""
        if self.agent_busy():
            return
        agent = state.get('agent') if isinstance(state.get('agent'), dict) else {}
        status = agent.get('status')
        stale = time.time() - updated > STALE_SECONDS
        if status not in ('running',) + CONCLUDED and passed(agent.get('deadline')):
            state['agent'] = agent
            mark_skipped(state, '安装失败后 2 小时内没有重试，已按不在本集群安装 Agent 处理' if status == 'failed'
                         else '超过 2 小时没有选择，已按不在本集群安装 Agent 处理')
            state['revision'] += 1
            await self.repo.save(state)
            self.agent_task = asyncio.create_task(self._agent_finish(), name='kubedoor-setup-agent-finish')
        elif status == 'running' and stale:
            if agent.get('task_id') and agent.get('attempt'):
                self.agent_task = asyncio.create_task(self._agent_install(agent['attempt'], resume=True),
                                                      name='kubedoor-agent-install')
            else:
                mark_failed(state, '安装服务重启时本次 Agent 安装尚未开始，请重试或跳过')
                await self.repo.save(state)
        elif status in CONCLUDED and stale:
            self.agent_task = asyncio.create_task(self._agent_finish(), name='kubedoor-setup-agent-finish')

    async def cleanup(self, app):
        if self.recovery_task:
            self.recovery_task.cancel()
            await asyncio.gather(self.recovery_task, return_exceptions=True)
        for task in (self.task, self.agent_task):
            if task and not task.done():
                # Agent 安装被打断时保持 running：重启后的看门狗按 master 任务 id 继续跟踪
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


@web.middleware
async def safe_requests(request, handler):
    try:
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            origin = request.headers.get('Origin')
            if origin:
                parsed = urlsplit(origin)
                if parsed.scheme not in ('http', 'https') or parsed.netloc.lower() != request.host.lower() or parsed.path not in ('', '/'):
                    raise SetupError('仅允许从当前安装页面提交请求', 403)
            if request.headers.get('Sec-Fetch-Site') == 'cross-site':
                raise SetupError('不允许跨站安装请求', 403)
        response = await handler(request)
    except SetupError as error:
        response = web.json_response({'success': False, 'error': error.message, 'message': error.message}, status=error.status)
    except web.HTTPRequestEntityTooLarge:
        response = web.json_response({'success': False, 'error': '请求内容过大'}, status=413)
    except web.HTTPException:
        raise
    except Exception:
        response = web.json_response({'success': False, 'error': '安装服务暂时无法处理请求，请稍后重试'}, status=503)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


def create_app(repo, *, key=None, runner_factory=None, pg_probe=check_postgres, metrics_probe=check_metrics,
               agent_installer=AgentInstall):
    api = SetupAPI(repo, key=key or os.environ.get('SETUP_KEY', ''), runner_factory=runner_factory,
                   pg_probe=pg_probe, metrics_probe=metrics_probe, agent_installer=agent_installer)
    app = web.Application(middlewares=[safe_requests], client_max_size=32 * 1024)
    app[web.AppKey('setup_api', SetupAPI)] = api
    async def health(request):
        return web.json_response({'status': 'ok'})
    app.router.add_get('/health', health)
    app.router.add_get(API + '/status', api.status)
    app.router.add_post(API + '/session', api.session)
    app.router.add_get(API + '/state', api.get_state)
    app.router.add_post(API + '/check/{section}', api.probe)
    app.router.add_put(API + '/configuration', api.configure)
    app.router.add_post(API + '/start', api.start)
    app.router.add_post(API + '/retry', api.start)
    app.router.add_post(API + '/agent/install', api.agent_install)
    app.router.add_post(API + '/agent/skip', api.agent_skip)
    app.on_startup.append(api.startup)
    app.on_cleanup.append(api.cleanup)
    return app
