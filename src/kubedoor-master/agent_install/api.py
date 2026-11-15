"""浏览器仅提交配置与安装指令，任务在 master 后台执行。"""

import functools
from aiohttp import web
from auth import principal_of

from .kubernetes import InstallError
from . import health
from .service import Service, public_defaults, repair_target

KEY = web.AppKey('agent_install', Service)


def endpoint(function):
    @functools.wraps(function)
    async def wrapped(request):
        if not principal_of(request).is_admin:
            raise web.HTTPForbidden()
        try:
            result = await function(request)
            return web.json_response({'success': True, 'data': result})
        except InstallError as exc:
            return web.json_response({'success': False, 'error': str(exc), 'message': str(exc)}, status=exc.status)
        except (ValueError, TypeError, KeyError):
            return web.json_response({'success': False, 'error': '安装参数格式不正确'}, status=400)
    return wrapped


async def body(request):
    try:
        payload = await request.json()
    except (ValueError, UnicodeError):
        raise InstallError('请求必须为 JSON 对象') from None
    if not isinstance(payload, dict):
        raise InstallError('请求必须为 JSON 对象')
    return payload


@endpoint
async def get_defaults(request):
    # 安装参数默认值 + 正在使用的通用配置（settings：远程写地址、对象存储地址脱敏，镜像仓库、KubeDoor 访问地址）
    return public_defaults()


@endpoint
async def inspect(request):
    service = request.app[KEY]
    payload = await body(request)
    username = principal_of(request).username
    # repair_env：修复已接入的集群（覆盖安装），集群标识固定为它
    if payload.get('repair_env') is not None:
        env = repair_target(payload['repair_env'])
        if not await health.registered(env, service.clients):
            raise InstallError(f'集群 {env} 还没有接入 KubeDoor，请用「安装 Agent」', 404)
        repair = {'env': env, 'source': 'upload'}
        return service.public(await service.inspect(payload.get('kubeconfig'), username, repair=repair))
    return service.public(await service.inspect(payload.get('kubeconfig'), username))


@endpoint
async def repair(request):
    """修复 Agent：用「AI Kubeconfig」列保存的 kubeconfig 建修复任务；没有能用的返回 task: null 和原因（页面改为上传）"""
    service = request.app[KEY]
    payload = await body(request)
    result = await service.repair(payload.get('env'), principal_of(request).username)
    return {'task': service.public(result['task']) if result['task'] else None, 'message': result['message']}


@endpoint
async def agent_health(request):
    """修复弹框里的检查：GET /api/admin/agent-health?env="""
    service = request.app[KEY]
    env = repair_target(request.query.get('env'))
    if not await health.registered(env, service.clients):
        raise InstallError(f'集群 {env} 还没有接入 KubeDoor', 404)
    if service.call_agent is None:
        raise InstallError('控制端没有提供 Agent 调用', 503)
    previous = await service.repo.installed(env)
    namespace = ((previous or {}).get('payload', {}).get('options') or {}).get('namespace') or health.DEFAULT_NAMESPACE
    return await health.check(env, service.clients, service.call_agent, namespace, principal_of(request).username)


@endpoint
async def preflight(request):
    service = request.app[KEY]
    payload = await body(request)
    # force：确认接管目标集群里已有的 Agent 资源（强制覆盖安装）
    return service.public(await service.preflight(request.match_info['id'], payload.get('context'), payload.get('options', {}),
                                                  force=payload.get('force') is True))


@endpoint
async def start(request):
    service = request.app[KEY]
    payload = await body(request)
    return service.public(await service.start(request.match_info['id'], payload.get('revision')))


@endpoint
async def retry(request):
    service = request.app[KEY]
    return service.public(await service.start(request.match_info['id'], None, retry=True))


@endpoint
async def get(request):
    service = request.app[KEY]
    return service.public(await service.get(request.match_info['id']))


@endpoint
async def listing(request):
    service = request.app[KEY]
    return [service.public(task) for task in await service.repo.list()]


def register_routes(app, clients, call_agent=None):
    """call_agent：master 内部调 Agent 的函数（kubedoor-master.py 的 call_agent_api），修复弹框的检查要用"""
    app[KEY] = Service(clients, call_agent=call_agent)
    prefix = '/api/admin/agent-installations'
    app.router.add_get(prefix + '/defaults', get_defaults)
    app.router.add_get(prefix, listing)
    app.router.add_post(prefix + '/inspect', inspect)
    app.router.add_post(prefix + '/repair', repair)
    app.router.add_get(prefix + '/{id}', get)
    app.router.add_post(prefix + '/{id}/preflight', preflight)
    app.router.add_post(prefix + '/{id}/start', start)
    app.router.add_post(prefix + '/{id}/retry', retry)
    app.router.add_get('/api/admin/agent-health', agent_health)


async def on_startup(app):
    await app[KEY].open()


async def on_cleanup(app):
    await app[KEY].close()
