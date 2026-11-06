"""Optimistic, durable installation state with immutable private revisions."""
from __future__ import annotations

import asyncio
import base64
import copy
from datetime import datetime, timezone
import json
import uuid
from urllib.parse import quote

from .config import default_private, public_config
from .errors import SetupError

STATE_NAME = 'kubedoor-setup-state'
PRIVATE_NAME = 'kubedoor-setup-config'
LABELS = {'app.kubernetes.io/part-of': 'kubedoor', 'app.kubernetes.io/component': 'setup'}
PHASES = ('draft', 'ready', 'running', 'failed', 'interrupted', 'agent', 'finalizing', 'succeeded')
# state['agent'] 里可以给页面看的字段；attempt、started_at 等内部字段不返回
AGENT_PUBLIC = ('status', 'deadline', 'defaults', 'options', 'steps', 'checks', 'message', 'error', 'task_id', 'updated_at')


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def check_row(state, name, status, message):
    rows = state.setdefault('checks', [])
    row = next((item for item in rows if item.get('name') == name), None)
    if row is None:
        row = {'id': name, 'name': name}
        rows.append(row)
    row.update(status=status, message=message, updated_at=utcnow())


def new_state(installation_id, namespace):
    return {'id': installation_id, 'phase': 'draft', 'revision': 0, 'step': 1,
            'environment': {'namespace': namespace, 'server': '', 'version': ''},
            'checks': [], 'services': [], 'private_key': None, 'created_at': utcnow(), 'updated_at': utcnow()}


def public_agent(agent):
    return {key: copy.deepcopy(agent[key]) for key in AGENT_PUBLIC if key in agent}


def public_state(state, private):
    result = {key: copy.deepcopy(value) for key, value in state.items()
              if not key.startswith('_') and key not in ('private_key', 'checked', 'lease', 'internal', 'run_id', 'auth_initialized')}
    result['config'] = public_config(private['config'])
    result['retryable'] = state['phase'] in ('failed', 'interrupted')
    if isinstance(state.get('agent'), dict):
        result['agent'] = public_agent(state['agent'])
    return result


def public_marker(state):
    result = {'phase': state['phase'], 'installation_id': state['id']}
    if state['phase'] in ('finalizing', 'succeeded'):
        # 公开标记无需登录即可读取：服务行只给名称、Deployment 名和状态，失败详情（镜像、Pod）不公开
        result.update(services=[{key: row[key] for key in ('id', 'name', 'resource', 'status') if key in row}
                                for row in state.get('services', [])],
                      checks=[{key: row[key] for key in ('id', 'name', 'status') if key in row} for row in state.get('checks', [])],
                      public_url=state.get('public_url', ''), completed_at=state.get('completed_at', ''))
        if state.get('error'):
            result['error'] = state['error']
        agent = state.get('agent')
        if isinstance(agent, dict) and agent.get('status'):
            # 安装服务关闭后的完成页只能读到这份公开标记：只给结果和集群名称
            result['agent'] = {'status': agent['status'], 'env': (agent.get('options') or {}).get('env', '')}
    return result


class Repository:
    def __init__(self, k8s, namespace=None):
        self.k8s = k8s
        self.namespace = namespace or k8s.namespace
        self.lock = asyncio.Lock()
        self.state = None
        self.base = '/api/v1/namespaces/' + quote(self.namespace, safe='')

    async def load(self):
        resource = await self.k8s.request('GET', self.base + '/configmaps/' + STATE_NAME, missing_ok=True)
        if not resource:
            raise SetupError('找不到安装状态，请通过 Web 引导安装命令启动服务', 503)
        try:
            state = json.loads(resource.get('data', {}).get('state.json', '{}'))
            if not isinstance(state, dict):
                raise ValueError()
            if not state:
                installation_id = resource.get('metadata', {}).get('annotations', {}).get('setup.kubedoor.io/installation-id')
                if not installation_id:
                    marker = json.loads(resource.get('data', {}).get('status.json', '{}'))
                    installation_id = marker.get('installation_id')
                if not installation_id:
                    raise ValueError()
                state = new_state(installation_id, self.namespace)
            if not state.get('id') or state.get('phase') not in PHASES:
                raise ValueError()
            state['_resource_version'] = resource['metadata']['resourceVersion']
        except (ValueError, KeyError, TypeError):
            raise SetupError('安装状态损坏，停止初始化以保护已有数据', 503) from None
        self.state = state
        return copy.deepcopy(state)

    async def load_private(self, state=None):
        state = state or await self.load()
        key = state.get('private_key')
        if not key:
            return default_private()
        resource = await self.k8s.request('GET', self.base + '/secrets/' + PRIVATE_NAME, missing_ok=True)
        try:
            if resource['metadata'].get('annotations', {}).get('setup.kubedoor.io/installation-id') != state['id']:
                raise ValueError()
            private = json.loads(base64.b64decode(resource['data'][key], validate=True))
            if not isinstance(private.get('config'), dict) or not isinstance(private.get('secrets'), dict):
                raise ValueError()
            return private
        except (ValueError, KeyError, TypeError):
            raise SetupError('安装凭据缺失或损坏，请恢复原 Secret 后重试', 503) from None

    async def save(self, state):
        record = copy.deepcopy(state)
        resource_version = record.pop('_resource_version', None)
        if not resource_version:
            raise SetupError('缺少安装状态版本，请重新加载', 409)
        record['updated_at'] = utcnow()
        current = await self.k8s.request('GET', self.base + '/configmaps/' + STATE_NAME)
        if current['metadata']['resourceVersion'] != resource_version:
            raise SetupError('安装状态已更新，请刷新后重试', 409)
        current.setdefault('data', {}).update({
            'state.json': json.dumps(record, ensure_ascii=False, separators=(',', ':')),
            'status.json': json.dumps(public_marker(record), ensure_ascii=False, separators=(',', ':')),
            'phase': record['phase'],
            'installation-id': record['id'],
            'public-url': record.get('public_url', ''),
        })
        try:
            saved = await self.k8s.request('PUT', self.base + '/configmaps/' + STATE_NAME, body=current)
        except SetupError as error:
            if error.status == 409:
                raise SetupError('安装状态已更新，请刷新后重试', 409) from None
            raise
        record['_resource_version'] = saved['metadata']['resourceVersion']
        self.state = record
        state.update(copy.deepcopy(record))
        return copy.deepcopy(record)

    async def write(self, state, private=None):
        if private is not None:
            key = 'private-' + uuid.uuid4().hex + '.json'
            encoded = base64.b64encode(json.dumps(private, ensure_ascii=False, separators=(',', ':')).encode()).decode()
            for attempt in range(5):
                resource = await self.k8s.request('GET', self.base + '/secrets/' + PRIVATE_NAME, missing_ok=True)
                create = resource is None
                if create:
                    resource = {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {
                        'name': PRIVATE_NAME, 'namespace': self.namespace, 'labels': LABELS,
                        'annotations': {'setup.kubedoor.io/installation-id': state['id']}
                    }, 'type': 'Opaque', 'data': {}}
                elif resource['metadata'].get('annotations', {}).get('setup.kubedoor.io/installation-id') != state['id']:
                    raise SetupError('同名安装凭据 Secret 不属于本次安装，停止以避免覆盖', 409)
                resource.setdefault('data', {})[key] = encoded
                if sum(len(value) for value in resource['data'].values()) > 900_000:
                    raise SetupError('安装草稿修改次数过多，请保留当前状态并联系管理员整理安装 Secret', 409)
                try:
                    await self.k8s.request('POST' if create else 'PUT', self.base + '/secrets' + ('' if create else '/' + PRIVATE_NAME), body=resource)
                    break
                except SetupError as error:
                    if error.status != 409 or attempt == 4:
                        raise
            state['private_key'] = key
        return await self.save(state)
