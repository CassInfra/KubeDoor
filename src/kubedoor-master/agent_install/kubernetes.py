"""只使用上传文件中的内嵌凭据，每次连接使用独立的 TLS 和 HTTP 客户端。"""

import asyncio
import base64
import json
import ssl
import tempfile
from pathlib import Path
from urllib.parse import quote

import aiohttp


class InstallError(Exception):
    def __init__(self, message, status=400, http_status=None):
        super().__init__(message)
        self.status = status
        # 访问目标集群 API Server 时对方返回的 HTTP 状态（例如 401 表示 kubeconfig 凭据失效）
        self.http_status = http_status


# 安装器只接受发布包里的这些资源，不提供任意 Kubernetes API 转发。
RESOURCES = {
    ('v1', 'Namespace'): ('namespaces', False),
    ('v1', 'ServiceAccount'): ('serviceaccounts', True),
    ('v1', 'Secret'): ('secrets', True),
    ('v1', 'ConfigMap'): ('configmaps', True),
    ('v1', 'Service'): ('services', True),
    ('apps/v1', 'Deployment'): ('deployments', True),
    ('apps/v1', 'DaemonSet'): ('daemonsets', True),
    ('rbac.authorization.k8s.io/v1', 'ClusterRole'): ('clusterroles', False),
    ('rbac.authorization.k8s.io/v1', 'ClusterRoleBinding'): ('clusterrolebindings', False),
}


def resource_path(resource):
    version, kind = resource['apiVersion'], resource['kind']
    try:
        plural, namespaced = RESOURCES[version, kind]
    except KeyError:
        raise InstallError(f'安装模板含不支持的资源类型：{kind}') from None
    path = '/api/v1' if version == 'v1' else '/apis/' + version
    meta = resource['metadata']
    if namespaced:
        path += '/namespaces/' + quote(meta['namespace'], safe='')
    return path + '/' + plural + '/' + quote(meta['name'], safe='')


def _tls(config, directory):
    cluster = config['clusters'][0]['cluster']
    user = config['users'][0]['user']
    if cluster.get('insecure-skip-tls-verify'):
        context = ssl._create_unverified_context()
    else:
        ca = cluster.get('certificate-authority-data')
        context = ssl.create_default_context(
            cadata=base64.b64decode(ca, validate=True).decode('ascii') if ca else None)
        # 与现有 Agent / 共享执行器一致：保留 CA/主机名校验，兼容老集群 CA。
        context.verify_flags &= ~getattr(ssl, 'VERIFY_X509_STRICT', 0)
    if user.get('client-certificate-data'):
        cert = Path(directory) / 'client.crt'
        key = Path(directory) / 'client.key'
        cert.write_bytes(base64.b64decode(user['client-certificate-data'], validate=True))
        key.write_bytes(base64.b64decode(user['client-key-data'], validate=True))
        key.chmod(0o600)
        context.load_cert_chain(cert, key)
    return context


class Kubernetes:
    def __init__(self, parsed):
        self.config = parsed['config']
        self.server = parsed['server']
        self.directory = None
        self.session = None

    async def __aenter__(self):
        self.directory = tempfile.TemporaryDirectory(prefix='kubedoor-install-')
        try:
            context = await asyncio.to_thread(_tls, self.config, self.directory.name)
            token = self.config['users'][0]['user'].get('token')
            headers = {'Authorization': 'Bearer ' + token} if token else {}
            self.server_name = self.config['clusters'][0]['cluster'].get('tls-server-name')
            self.session = aiohttp.ClientSession(
                headers=headers, timeout=aiohttp.ClientTimeout(total=30, connect=10),
                connector=aiohttp.TCPConnector(ssl=context, limit=8))
            return self
        except Exception:
            self.directory.cleanup()
            raise InstallError('无法加载 kubeconfig 的证书或认证信息，请检查内嵌证书与密钥') from None

    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()
        if self.directory:
            self.directory.cleanup()

    async def request(self, method, path, *, body=None, params=None, missing_ok=False, apply=False):
        options = {'params': params, 'allow_redirects': False}
        if self.server_name:
            options['server_hostname'] = self.server_name
        if body is not None:
            options['data'] = json.dumps(body)
            options['headers'] = {'Content-Type': 'application/apply-patch+yaml' if apply else 'application/json'}
        try:
            async with self.session.request(method, self.server + path, **options) as response:
                if response.status == 404 and missing_ok:
                    return None
                chunks, size = [], 0
                async for chunk in response.content.iter_chunked(65536):
                    size += len(chunk)
                    if size > 16 * 1024 * 1024:
                        raise InstallError('Kubernetes 响应超过安装器大小限制，请缩小目标范围')
                    chunks.append(chunk)
                raw = b''.join(chunks)
                if response.status >= 300:
                    hints = {401: '认证失败', 403: '权限不足或准入策略拒绝', 404: '资源或命名空间不存在',
                             409: '资源字段由其它管理器拥有，请核对现有安装', 422: '资源校验或准入检查失败'}
                    # 不回显上游请求/响应体，其中可能含 Secret 或 token。
                    raise InstallError(f'Kubernetes {method} {path}：{hints.get(response.status, "请求失败")}（HTTP {response.status}）',
                                       http_status=response.status)
                return json.loads(raw) if raw else {}
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise InstallError(f'无法连接 Kubernetes API Server（{type(exc).__name__}），请检查 master 到目标集群的网络和证书') from None
        except (ValueError, UnicodeError):
            raise InstallError('Kubernetes API Server 返回了无效的 JSON 响应') from None

    async def get(self, resource):
        return await self.request('GET', resource_path(resource), missing_ok=True)

    async def list(self, path, params=None):
        result, cursor = [], ''
        while True:
            page = await self.request('GET', path, params={**(params or {}), 'limit': '500', **({'continue': cursor} if cursor else {})})
            result.extend(page.get('items', []))
            cursor = page.get('metadata', {}).get('continue', '')
            if not cursor:
                return result
            if len(result) >= 20000:
                raise InstallError('待检查的集群资源过多，请在高级设置中指定已有采集组件')

    async def apply(self, resource, dry_run=False):
        return await self.request('PATCH', resource_path(resource), body=resource, apply=True,
                                  params={'fieldManager': 'kubedoor-installer', **({'dryRun': 'All'} if dry_run else {})})

    async def allowed(self, resource, verb):
        version, kind = resource['apiVersion'], resource['kind']
        plural, namespaced = RESOURCES[version, kind]
        attributes = {'group': '' if version == 'v1' else version.split('/')[0],
                      'resource': plural, 'verb': verb}
        if namespaced:
            attributes['namespace'] = resource['metadata']['namespace']
        response = await self.request('POST', '/apis/authorization.k8s.io/v1/selfsubjectaccessreviews', body={
            'apiVersion': 'authorization.k8s.io/v1', 'kind': 'SelfSubjectAccessReview',
            'spec': {'resourceAttributes': attributes}})
        return bool(response.get('status', {}).get('allowed'))

    async def ready(self, resource):
        current = await self.get(resource)
        if not current:
            return False
        status, spec = current.get('status', {}), current.get('spec', {})
        if status.get('observedGeneration', 0) < current['metadata'].get('generation', 1):
            return False
        if resource['kind'] == 'DaemonSet':
            desired = status.get('desiredNumberScheduled', 0)
            return desired > 0 and status.get('numberReady', 0) >= desired and status.get('updatedNumberScheduled', 0) >= desired
        desired = spec.get('replicas', 1)
        return status.get('readyReplicas', 0) >= desired and status.get('updatedReplicas', 0) >= desired

    async def workload_diagnosis(self, namespace):
        result = await self.request('GET', '/api/v1/namespaces/' + quote(namespace, safe='') + '/pods',
                                    params={'labelSelector': 'app.kubernetes.io/part-of=kubedoor', 'limit': '100'})
        reasons = []
        for pod in result.get('items', []):
            for container in pod.get('status', {}).get('containerStatuses', []):
                waiting = container.get('state', {}).get('waiting')
                if waiting:
                    reasons.append(pod['metadata']['name'] + ': ' + waiting.get('reason', 'Pending'))
        return '；'.join(reasons[:8]) or '工作负载未全部就绪，请查看目标命名空间的 Pod 与事件'
