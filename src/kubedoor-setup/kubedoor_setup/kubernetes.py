"""Small in-cluster Kubernetes client for the first-install controller."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import ssl
from urllib.parse import quote

import aiohttp

from .errors import SetupError

RESOURCES = {
    ('v1', 'Namespace'): ('namespaces', False),
    ('v1', 'ConfigMap'): ('configmaps', True),
    ('v1', 'Secret'): ('secrets', True),
    ('v1', 'Service'): ('services', True),
    ('v1', 'ServiceAccount'): ('serviceaccounts', True),
    ('v1', 'Pod'): ('pods', True),
    ('apps/v1', 'Deployment'): ('deployments', True),
    ('apps/v1', 'DaemonSet'): ('daemonsets', True),
    ('batch/v1', 'Job'): ('jobs', True),
    ('batch/v1', 'CronJob'): ('cronjobs', True),
    ('rbac.authorization.k8s.io/v1', 'Role'): ('roles', True),
    ('rbac.authorization.k8s.io/v1', 'RoleBinding'): ('rolebindings', True),
    ('rbac.authorization.k8s.io/v1', 'ClusterRoleBinding'): ('clusterrolebindings', False),
}
OWNER = 'setup.kubedoor.io/installation-id'
RBAC = 'rbac.authorization.k8s.io/v1'
# 在本集群安装 Agent 用的专用账号：引导时绑定 cluster-admin，安装服务平时不持有它，
# 只在用户选择安装 Agent 时签发短期令牌，收尾时先删绑定再删账号。
AGENT_INSTALLER = 'kubedoor-agent-installer'


def agent_installer_binding(namespace):
    """集群级绑定的名称带命名空间，同一集群的多套安装互不覆盖。"""
    return AGENT_INSTALLER + '-' + namespace


async def service_account_token(k8s, namespace, name, seconds):
    """用 TokenRequest 给 ServiceAccount 签发短期令牌；令牌只留在内存里，不写入状态和日志。"""
    path = '/api/v1/namespaces/' + quote(namespace, safe='') + '/serviceaccounts/' + quote(name, safe='') + '/token'
    result = await k8s.request('POST', path, body={
        'apiVersion': 'authentication.k8s.io/v1', 'kind': 'TokenRequest', 'spec': {'expirationSeconds': seconds}})
    token = (result or {}).get('status', {}).get('token')
    if not isinstance(token, str) or not token.strip():
        raise SetupError('未能为 Agent 安装账号签发临时令牌', 503)
    return token.strip()


def resource_path(resource):
    version, kind = resource['apiVersion'], resource['kind']
    if (version, kind) not in RESOURCES:
        raise SetupError('安装模板包含不支持的资源类型')
    plural, namespaced = RESOURCES[version, kind]
    prefix = '/api/v1' if version == 'v1' else '/apis/' + version
    if namespaced:
        prefix += '/namespaces/' + quote(resource['metadata']['namespace'], safe='')
    return prefix + '/' + plural + '/' + quote(resource['metadata']['name'], safe='')


def resource(kind, name, namespace, version='v1'):
    return {'apiVersion': version, 'kind': kind, 'metadata': {'name': name, 'namespace': namespace}}


def workload_ready(current, expected=None):
    """Only count the desired generation and all updated, available replicas."""
    if not current:
        return False
    metadata = current.get('metadata', {})
    status, spec = current.get('status', {}), current.get('spec', {})
    if metadata.get('deletionTimestamp') or status.get('observedGeneration', 0) < metadata.get('generation', 1):
        return False
    if expected:
        wanted = expected.get('spec', {}).get('template', {}).get('metadata', {}).get('annotations', {})
        actual = spec.get('template', {}).get('metadata', {}).get('annotations', {})
        if any(actual.get(k) != v for k, v in wanted.items()):
            return False
        expected_images = {c['name']: c['image'] for c in expected.get('spec', {}).get('template', {}).get('spec', {}).get('containers', [])}
        actual_images = {c['name']: c['image'] for c in spec.get('template', {}).get('spec', {}).get('containers', [])}
        if any(actual_images.get(name) != image for name, image in expected_images.items()):
            return False
    if current['kind'] == 'DaemonSet':
        desired = status.get('desiredNumberScheduled', 0)
        return (desired > 0 and status.get('updatedNumberScheduled', 0) == desired
                and status.get('numberAvailable', 0) == desired
                and status.get('numberReady', 0) == desired
                and status.get('numberUnavailable', 0) == 0)
    desired = spec.get('replicas', 1)
    return (desired > 0 and status.get('updatedReplicas', 0) == desired
            and status.get('replicas', 0) == desired
            and status.get('readyReplicas', 0) == desired
            and status.get('availableReplicas', 0) == desired
            and status.get('unavailableReplicas', 0) == 0)


class Kubernetes:
    def __init__(self, namespace=None, *, server=None, token_path=None, ca_path=None):
        self.namespace = namespace or os.environ.get('SETUP_NAMESPACE', 'kubedoor')
        host = os.environ.get('KUBERNETES_SERVICE_HOST', 'kubernetes.default.svc')
        if ':' in host and not host.startswith('['):
            host = '[' + host + ']'
        self.server = server or 'https://' + host + ':' + os.environ.get('KUBERNETES_SERVICE_PORT_HTTPS', '443')
        self.token_path = Path(token_path or '/var/run/secrets/kubernetes.io/serviceaccount/token')
        self.ca_path = Path(ca_path or '/var/run/secrets/kubernetes.io/serviceaccount/ca.crt')
        self.session = None

    async def open(self):
        context = ssl.create_default_context(cafile=str(self.ca_path))
        context.verify_flags &= ~getattr(ssl, 'VERIFY_X509_STRICT', 0)
        self.session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=context, limit=12),
            timeout=aiohttp.ClientTimeout(total=30, connect=10), trust_env=False)
        return self

    async def close(self):
        if self.session:
            await self.session.close()

    async def __aenter__(self):
        return await self.open()

    async def __aexit__(self, *args):
        await self.close()

    async def request(self, method, path, *, body=None, params=None, missing_ok=False, apply=False, token=None):
        if not path.startswith('/') or path.startswith('//'):
            raise SetupError('Kubernetes 请求路径无效')
        if token is None:
            # Projected tokens rotate; reread per request rather than cache the first token.
            token = self.token_path.read_text().strip()
        elif not token:
            raise SetupError('临时令牌为空，拒绝发送请求', 503)
        headers = {'Authorization': 'Bearer ' + token}
        if body is not None:
            headers['Content-Type'] = 'application/apply-patch+yaml' if apply else 'application/json'
        try:
            async with self.session.request(method, self.server + path, headers=headers, params=params,
                                            data=json.dumps(body) if body is not None else None,
                                            allow_redirects=False) as response:
                if response.status == 404 and missing_ok:
                    return None
                if response.status >= 300:
                    hints = {401: '安装身份认证失败', 403: '安装权限不足或准入策略拒绝',
                             404: '资源不存在', 409: '资源已被修改，请刷新安装状态重试',
                             422: '资源不符合集群校验要求'}
                    raise SetupError(hints.get(response.status, 'Kubernetes 请求失败')
                                     + f'（HTTP {response.status}）', response.status)
                chunks, size = [], 0
                async for chunk in response.content.iter_chunked(65536):
                    size += len(chunk)
                    if size > 16 * 1024 * 1024:
                        raise SetupError('Kubernetes 响应过大', 502)
                    chunks.append(chunk)
                return json.loads(b''.join(chunks)) if chunks else {}
        except (aiohttp.ClientError, asyncio.TimeoutError):
            raise SetupError('无法连接 Kubernetes API，请检查集群网络和证书', 503) from None
        except (ValueError, UnicodeError):
            raise SetupError('Kubernetes 返回了无效响应', 502) from None

    async def get(self, obj, token=None):
        return await self.request('GET', resource_path(obj), missing_ok=True, token=token)

    async def apply(self, obj, dry_run=False):
        return await self.request('PATCH', resource_path(obj), body=obj, apply=True,
                                  params={'fieldManager': 'kubedoor-setup', **({'dryRun': 'All'} if dry_run else {})})

    async def replace(self, obj):
        return await self.request('PUT', resource_path(obj), body=obj)

    async def delete(self, obj, token=None):
        return await self.request('DELETE', resource_path(obj), missing_ok=True,
                                  body={'propagationPolicy': 'Background'}, token=token)

    async def create(self, obj):
        return await self.request('POST', resource_path(obj).rsplit('/', 1)[0], body=obj)

    async def list(self, path, params=None):
        result, cursor = [], ''
        while True:
            page = await self.request('GET', path, params={**(params or {}), 'limit': '250', **({'continue': cursor} if cursor else {})})
            result.extend(page.get('items', []))
            cursor = page.get('metadata', {}).get('continue', '')
            if not cursor:
                return result
            if len(result) > 10000:
                raise SetupError('安装范围内资源过多，无法完成检测')

    async def allowed(self, kind, verb, name=None, version='v1', token=None):
        plural, namespaced = RESOURCES[version, kind]
        attrs = {'group': '' if version == 'v1' else version.split('/')[0], 'resource': plural, 'verb': verb}
        if namespaced:
            attrs['namespace'] = self.namespace
        if name:
            attrs['name'] = name
        result = await self.request('POST', '/apis/authorization.k8s.io/v1/selfsubjectaccessreviews', body={
            'apiVersion': 'authorization.k8s.io/v1', 'kind': 'SelfSubjectAccessReview',
            'spec': {'resourceAttributes': attrs}}, token=token)
        return result.get('status', {}).get('allowed') is True
