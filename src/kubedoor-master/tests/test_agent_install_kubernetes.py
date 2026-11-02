"""安装器的实际 HTTP 传输边界：SSA、分页、流式响应和认证不跟随重定向。"""

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agent_install.kubernetes import InstallError, Kubernetes


def parsed(server):
    return {'server': str(server.make_url('')).rstrip('/'), 'config': {
        'clusters': [{'cluster': {}}], 'users': [{'user': {'token': 'do-not-leak'}}]}}


@pytest.mark.asyncio
async def test_ssa_dry_run_uses_independent_auth_and_handles_chunked_json():
    captured = {}

    async def apply(request):
        captured.update(method=request.method, headers=dict(request.headers), query=dict(request.query), body=await request.json())
        response = web.StreamResponse(headers={'Content-Type': 'application/json'})
        await response.prepare(request)
        for part in (b'{"kind":', b'"Namespace",', b'"metadata":{"name":"kubedoor"}}'):
            await response.write(part)
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_patch('/api/v1/namespaces/kubedoor', apply)
    async with TestServer(app) as server:
        async with Kubernetes(parsed(server)) as client:
            result = await client.apply({'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': 'kubedoor'}}, dry_run=True)
    assert result['kind'] == 'Namespace'
    assert captured['headers']['Authorization'] == 'Bearer do-not-leak'
    assert captured['headers']['Content-Type'] == 'application/apply-patch+yaml'
    assert captured['query'] == {'fieldManager': 'kubedoor-installer', 'dryRun': 'All'}
    assert 'force' not in captured['query']


@pytest.mark.asyncio
async def test_error_does_not_echo_sensitive_upstream_body_or_follow_redirect():
    followed = []

    async def secret_error(request):
        return web.json_response({'message': 'bad do-not-leak'}, status=403)

    async def redirect(request):
        raise web.HTTPFound('/target')

    async def target(request):
        followed.append(request.headers.get('Authorization'))
        return web.json_response({})

    app = web.Application()
    app.router.add_get('/denied', secret_error)
    app.router.add_get('/redirect', redirect)
    app.router.add_get('/target', target)
    async with TestServer(app) as server:
        async with Kubernetes(parsed(server)) as client:
            with pytest.raises(InstallError) as denied:
                await client.request('GET', '/denied')
            assert '权限不足' in str(denied.value) and 'do-not-leak' not in str(denied.value)
            with pytest.raises(InstallError):
                await client.request('GET', '/redirect')
    assert not followed


@pytest.mark.asyncio
async def test_list_follows_continue_token():
    queries = []

    async def listing(request):
        queries.append(dict(request.query))
        if request.query.get('continue'):
            return web.json_response({'items': [{'name': 'b'}]})
        return web.json_response({'metadata': {'continue': 'next/token'}, 'items': [{'name': 'a'}]})

    app = web.Application()
    app.router.add_get('/api/v1/nodes', listing)
    async with TestServer(app) as server:
        async with Kubernetes(parsed(server)) as client:
            result = await client.list('/api/v1/nodes')
    assert [r['name'] for r in result] == ['a', 'b']
    assert queries[-1]['continue'] == 'next/token'
