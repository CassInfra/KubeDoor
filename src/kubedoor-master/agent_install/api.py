"""浏览器仅提交配置与安装指令，任务在 master 后台执行。"""

import functools
from aiohttp import web
from auth import principal_of

from .kubernetes import InstallError
from .service import Service, public_defaults

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
    return service.public(await service.inspect(payload.get('kubeconfig'), principal_of(request).username))


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


def register_routes(app, clients):
    app[KEY] = Service(clients)
    prefix = '/api/admin/agent-installations'
    app.router.add_get(prefix + '/defaults', get_defaults)
    app.router.add_get(prefix, listing)
    app.router.add_post(prefix + '/inspect', inspect)
    app.router.add_get(prefix + '/{id}', get)
    app.router.add_post(prefix + '/{id}/preflight', preflight)
    app.router.add_post(prefix + '/{id}/start', start)
    app.router.add_post(prefix + '/{id}/retry', retry)


async def on_startup(app):
    await app[KEY].open()


async def on_cleanup(app):
    await app[KEY].close()
