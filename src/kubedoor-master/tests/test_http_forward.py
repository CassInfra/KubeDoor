"""kubedoor-master.py 的 /api/{tail} 兜底转发(http_handler):/api/pod/* 带上通用配置里的对象存储上传地址。

直接从源码里取出 http_handler 执行(不加载整个 kubedoor-master.py,本地没装云镜像 SDK 也能跑)。
"""

import ast
import asyncio
import base64
import pathlib
import types
import uuid

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from loguru import logger

MASTER_PATH = pathlib.Path(__file__).resolve().parent.parent / 'kubedoor-master.py'
OSS_URL = 'https://uploader:S3cret-PW-9527@oss.example.com/kubedoor'


class AgentSocket:
    """假 agent:记下 master 发来的包,立即回成功。"""

    def __init__(self, client):
        self.client = client
        self.sent = []

    async def send_json(self, message):
        self.sent.append(message)
        self.client['response_queue'][message['request_id']] = {'success': True, 'data': 'ok'}
        self.client['response_events'][message['request_id']].set()


@pytest.fixture
def master():
    """返回 (handler 所在的命名空间, 通用配置的当前值, agent 收到的包)"""
    parsed = ast.parse(MASTER_PATH.read_text(encoding='utf-8'))
    body = [node for node in parsed.body if isinstance(node, ast.AsyncFunctionDef) and node.name == 'http_handler']
    settings = {'oss_url': OSS_URL}
    client = {'online': True}
    client['ws'] = AgentSocket(client)
    namespace = {
        'asyncio': asyncio, 'base64': base64, 'uuid': uuid, 'web': web, 'logger': logger,
        'clients': {'prod': client},
        'general_settings': types.SimpleNamespace(value=lambda name: settings[name]),
    }
    exec(compile(ast.Module(body=body, type_ignores=[]), str(MASTER_PATH), 'exec'), namespace)
    return namespace, settings, client['ws'].sent


@pytest.fixture
def logs():
    lines = []
    handler_id = logger.add(lambda message: lines.append(str(message)), level='INFO')
    yield lines
    logger.remove(handler_id)


async def forward(namespace, method, path, **kwargs):
    app = web.Application()
    app.router.add_route('*', '/api/{tail:.*}', namespace['http_handler'])
    async with TestClient(TestServer(app)) as client:
        response = await client.request(method, path, **kwargs)
        assert response.status == 200, await response.text()
        return await response.json()


@pytest.mark.asyncio
async def test_pod_requests_carry_oss_url_and_logs_hide_it(master, logs):
    namespace, settings, sent = master
    data = await forward(namespace, 'GET', '/api/pod/auto_dump', params={'env': 'prod', 'namespace': 'ns', 'pod_name': 'web-1'})
    assert data == {'success': True, 'data': 'ok'}
    assert sent[-1]['path'] == '/api/pod/auto_dump'
    assert sent[-1]['oss_url'] == OSS_URL  # 带账号密码的完整地址只发给 agent
    text = '\n'.join(logs)
    assert '[请求]客户端 env=prod' in text and "'oss_url': '***'" in text
    assert 'S3cret-PW-9527' not in text and 'oss.example.com' not in text

    # 每次请求都取通用配置的当前值:页面上改了,下一次请求马上用新地址
    settings['oss_url'] = 'http://oss-new.example.com/upload'
    await forward(namespace, 'POST', '/api/pod/modify_pod', params={'env': 'prod'}, json=[{'pod': 'web-1'}])
    assert sent[-1]['oss_url'] == 'http://oss-new.example.com/upload'


@pytest.mark.asyncio
async def test_unconfigured_oss_url_is_sent_as_empty_string(master, logs):
    namespace, settings, sent = master
    settings['oss_url'] = ''
    await forward(namespace, 'GET', '/api/pod/auto_jstack', params={'env': 'prod'})
    # 键一定要有:新版 agent 据此知道是新 master(空串 = 控制端没配置),没有这个键才退回 agent 自己的 OSS_URL
    assert 'oss_url' in sent[-1] and sent[-1]['oss_url'] == ''
    assert "'oss_url': ''" in '\n'.join(logs)


@pytest.mark.asyncio
async def test_other_requests_do_not_carry_oss_url(master):
    namespace, _, sent = master
    for path in ('/api/agent/pods', '/api/scale', '/api/podlist'):
        await forward(namespace, 'GET', path, params={'env': 'prod'})
        assert sent[-1]['path'] == path and 'oss_url' not in sent[-1]
