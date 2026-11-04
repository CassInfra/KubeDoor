"""Create the first installation key inside Kubernetes, never on the user's host."""
from __future__ import annotations

import base64
import json
import secrets

from .errors import SetupError
from .kubernetes import OWNER
from .repository import PRIVATE_NAME, STATE_NAME


AUTH_NAME = 'kubedoor-setup-auth'


def _read_key(secret, installation_id):
    try:
        if secret['metadata'].get('annotations', {}).get(OWNER) != installation_id:
            raise ValueError()
        key = base64.b64decode(secret['data']['SETUP_KEY'], validate=True).decode('ascii')
        if len(key) < 32 or not all(c.isalnum() or c in '-_' for c in key):
            raise ValueError()
        return key
    except (KeyError, TypeError, ValueError, UnicodeError):
        raise SetupError('安装密钥缺失、损坏或归属不匹配，请恢复原 Secret', 503) from None


async def ensure_setup_key(repo):
    """Only an untouched bootstrap marker may create a key; later loss is an error."""
    k8s = repo.k8s
    auth_path = repo.base + '/secrets/' + AUTH_NAME
    for attempt in range(5):
        marker = await k8s.request('GET', repo.base + '/configmaps/' + STATE_NAME)
        state = await repo.load()
        if marker.get('metadata', {}).get('annotations', {}).get(OWNER) != state['id']:
            raise SetupError('安装状态归属不匹配，停止初始化', 409)
        if state['phase'] == 'succeeded':
            raise SetupError('安装已经完成，不能重新打开初始化服务', 409)
        secret = await k8s.request('GET', auth_path, missing_ok=True)
        if secret is None:
            untouched = json.loads(marker.get('data', {}).get('state.json', '{}')) == {}
            if not untouched or state['phase'] != 'draft' or state.get('auth_initialized'):
                raise SetupError('已有安装的密钥丢失，请恢复原 Secret；不会重新生成', 503)
            master = await k8s.request('GET', '/apis/apps/v1/namespaces/' + repo.namespace + '/deployments/kubedoor-master', missing_ok=True)
            private = await k8s.request('GET', repo.base + '/secrets/' + PRIVATE_NAME, missing_ok=True)
            if master is not None or private is not None:
                raise SetupError('发现已有控制端或安装配置，拒绝重新生成安装密钥', 409)
            secret = {
                'apiVersion': 'v1', 'kind': 'Secret',
                'metadata': {
                    'name': AUTH_NAME, 'namespace': repo.namespace,
                    'labels': {'app.kubernetes.io/part-of': 'kubedoor', 'app.kubernetes.io/managed-by': 'kubedoor-setup'},
                    'annotations': {OWNER: state['id']},
                },
                'type': 'Opaque',
                'data': {'SETUP_KEY': base64.b64encode(secrets.token_urlsafe(48).encode()).decode()},
            }
            try:
                secret = await k8s.request('POST', repo.base + '/secrets', body=secret)
            except SetupError as error:
                if error.status != 409:
                    raise
                secret = await k8s.request('GET', auth_path)
        key = _read_key(secret, state['id'])
        if state.get('auth_initialized'):
            return key
        state['auth_initialized'] = True
        try:
            await repo.save(state)
            return key
        except SetupError as error:
            if error.status != 409 or attempt == 4:
                raise
    raise SetupError('安装状态持续更新，请重试', 409)
