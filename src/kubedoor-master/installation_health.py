"""Authenticated, read-only readiness verification for the first-install worker."""
import asyncio
import os

import aiohttp
from aiohttp import web

import db


async def check(request):
    try:
        await asyncio.wait_for(db.pg_fetchval('SELECT 1'), 5)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            url = os.environ.get('PROM_URL', '').rstrip('/') + '/api/v1/query'
            async with session.get(url, params={'query': 'vector(1)'}, allow_redirects=False) as response:
                if response.status != 200:
                    raise ValueError('query_failed')
                data = await response.json()
                if data.get('status') != 'success':
                    raise ValueError('query_failed')
        return web.json_response({'ok': True, 'installation_id': os.environ.get('KUBEDOOR_SETUP_ID', '')})
    except Exception:
        return web.json_response({'ok': False, 'message': '数据库或时序库连接检查失败'}, status=503)
