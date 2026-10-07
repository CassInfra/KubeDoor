"""直接加载 master 的目标协程，隔离无关云镜像 SDK 和应用启动。"""

import ast
import asyncio
import json
import pathlib
import types
import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import web
from loguru import logger


def load_handlers():
    master_path = pathlib.Path(__file__).resolve().parent.parent / 'kubedoor-master.py'
    names = {'call_agent_api', 'handle_admis_request', 'init_peak_data'}
    parsed = ast.parse(master_path.read_text(encoding='utf-8'))
    module = ast.Module(body=[node for node in parsed.body if isinstance(node, ast.AsyncFunctionDef) and node.name in names], type_ignores=[])
    namespace = {
        'asyncio': asyncio, 'uuid': uuid, 'clients': {}, 'logger': logger,
        'web': web, 'datetime': datetime, 'timedelta': timedelta,
    }
    exec(compile(module, str(master_path), 'exec'), namespace)
    return namespace


def test_call_agent_registers_event_before_immediate_response():
    handlers = load_handlers()
    client = {'online': True}
    messages = []

    class ImmediateWS:
        async def send_json(self, message):
            messages.append(message)
            request_id = message['request_id']
            assert request_id in client['response_events']
            client['response_queue'][request_id] = {'success': True, 'data': []}
            client['response_events'][request_id].set()

    client['ws'] = ImmediateWS()
    handlers['clients']['prod'] = client
    response = asyncio.run(handlers['call_agent_api']('prod', '/api/agent/jvm/configs', timeout=0.1))

    assert response == {'success': True, 'data': []}
    assert uuid.UUID(messages[0]['request_id']).version == 4
    assert client['response_events'] == client['response_queue'] == {}


def test_concurrent_agent_requests_have_unique_ids_and_isolated_responses():
    handlers = load_handlers()
    client = {'online': True}
    ids = []

    class ImmediateWS:
        async def send_json(self, message):
            request_id = message['request_id']
            ids.append(request_id)
            await asyncio.sleep(0)
            client['response_queue'][request_id] = {'data': message['path']}
            client['response_events'][request_id].set()

    client['ws'] = ImmediateWS()
    handlers['clients']['prod'] = client

    async def call_many():
        return await asyncio.gather(*[
            handlers['call_agent_api']('prod', f'/read/{i}', timeout=1)
            for i in range(50)
        ])

    assert asyncio.run(call_many()) == [{'data': f'/read/{i}'} for i in range(50)]
    assert len(set(ids)) == 50
    assert client['response_events'] == client['response_queue'] == {}


@pytest.mark.parametrize('send_error', [False, True])
def test_agent_request_failure_cleans_only_its_pending_state(send_error):
    handlers = load_handlers()
    foreign_event = object()
    client = {'online': True, 'response_events': {'other': foreign_event}, 'response_queue': {'other': {'data': 1}}}

    class NoResponseWS:
        async def send_json(self, message):
            if send_error:
                raise RuntimeError('send failed')

    client['ws'] = NoResponseWS()
    handlers['clients']['prod'] = client
    with pytest.raises(Exception, match='send failed' if send_error else '超时'):
        asyncio.run(handlers['call_agent_api']('prod', '/read', timeout=0.01))

    assert client['response_events'] == {'other': foreign_event}
    assert client['response_queue'] == {'other': {'data': 1}}


def test_cancelled_agent_request_cleans_pending_state():
    handlers = load_handlers()
    client = {'online': True}

    async def cancel_request():
        sent = asyncio.Event()

        class NoResponseWS:
            async def send_json(self, message):
                sent.set()

        client['ws'] = NoResponseWS()
        handlers['clients']['prod'] = client
        task = asyncio.create_task(handlers['call_agent_api']('prod', '/read', timeout=30))
        await sent.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_request())
    assert client['response_events'] == client['response_queue'] == {}


@pytest.mark.parametrize('capability', [False, True, 'true'])
def test_admission_only_enables_jvm_for_explicit_boolean_capability(capability):
    handlers = load_handlers()
    get_admis = AsyncMock(return_value=[1, 1, -1, 100, 200, 300, 400, False])
    handlers['utils'] = types.SimpleNamespace(get_deploy_admis_async=get_admis)
    ws = types.SimpleNamespace(send_json=AsyncMock())
    request = {'request_id': 'id', 'namespace': 'ns1', 'deployment': 'web', 'jvm_config': capability}

    asyncio.run(handlers['handle_admis_request'](ws, 'prod', request))

    get_admis.assert_awaited_once_with('prod', 'ns1', 'web', include_jvm=capability is True)
    assert ws.send_json.call_args.args[0]['deploy_res'] == get_admis.return_value


def prepare_peak_handlers(jvm_result, write_succeeded=True):
    handlers = load_handlers()
    resources = [('historical-row',)]
    utils = types.SimpleNamespace(
        PROM_K8S_TAG_KEY='k8s',
        calculate_peak_duration_and_end_time=Mock(return_value=('1h', datetime.min.time(), datetime.min.time())),
        check_and_delete_day_data=AsyncMock(),
        merged_dict=Mock(),
        run_blocking=AsyncMock(return_value=resources),
        metrics_to_pg=AsyncMock(return_value=True),
        get_list_from_resources=AsyncMock(return_value=resources),
        is_init_or_update=AsyncMock(return_value=False),
        init_control_data=AsyncMock(return_value=write_succeeded),
        update_control_data=AsyncMock(return_value=write_succeeded),
        invalidate_admis_cache=Mock(),
    )
    collect = AsyncMock(return_value=jvm_result)
    handlers.update(utils=utils, jvm_config=types.SimpleNamespace(collect_current_configs=collect))
    return handlers, utils, collect, resources


def test_peak_collection_samples_jvm_once_after_historical_and_control_writes():
    handlers, utils, collect, resources = prepare_peak_handlers({'success': True, 'updated': 600})
    request = types.SimpleNamespace(query={'env': 'prod', 'days': '3'})

    response = asyncio.run(handlers['init_peak_data'](request))
    data = json.loads(response.body)

    assert response.status == 200
    assert data['success'] is True
    assert data['jvm'] == {'success': True, 'updated': 600}
    assert utils.metrics_to_pg.await_count == 3
    for call in utils.metrics_to_pg.call_args_list:
        assert call.args == (resources,)
    utils.update_control_data.assert_awaited_once_with(resources)
    collect.assert_awaited_once_with('prod', handlers['call_agent_api'], utils.invalidate_admis_cache)


def test_jvm_warning_keeps_existing_peak_collection_successful():
    warning = 'prod: JVM agent 未升级，下一轮重试'
    handlers, utils, collect, _ = prepare_peak_handlers({'success': False, 'warning': warning})

    response = asyncio.run(handlers['init_peak_data'](types.SimpleNamespace(query={'env': 'prod', 'days': '1'})))
    data = json.loads(response.body)

    assert response.status == 200
    assert data['success'] is True
    assert data['jvm']['success'] is False
    assert data['warnings'] == [warning]
    utils.update_control_data.assert_awaited_once()
    collect.assert_awaited_once()


def test_control_write_failure_does_not_collect_jvm():
    handlers, _, collect, _ = prepare_peak_handlers({'success': True}, write_succeeded=False)

    response = asyncio.run(handlers['init_peak_data'](types.SimpleNamespace(query={'env': 'prod', 'days': '1'})))

    assert response.status == 500
    collect.assert_not_awaited()
