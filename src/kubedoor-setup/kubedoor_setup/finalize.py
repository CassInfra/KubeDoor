"""One-shot installation handoff, under a narrowly scoped ServiceAccount."""
from __future__ import annotations

import asyncio
import json
import os

from .errors import SetupError
from .kubernetes import Kubernetes, resource, resource_path, OWNER
from .repository import Repository, utcnow


def verify_completion(state):
    """Require the complete chosen installation, never infer selection from a skipped result."""
    mandatory = {'web', 'master', 'ai', 'alarm'}
    known = mandatory | {'grafana', 'vmalert', 'alertmanager'}
    rows = state.get('services', [])
    selection = state.get('selected_services')
    if (not isinstance(selection, list) or any(not isinstance(item, str) for item in selection)
            or not mandatory.issubset(selection) or not set(selection).issubset(known)):
        raise SetupError('缺少有效的服务安装选择，不能完成安装', 409)
    if len(rows) != len(known) or {row.get('id') for row in rows} != known:
        raise SetupError('服务安装状态不完整，不能完成安装', 409)
    if any(row.get('status') != ('ready' if row['id'] in selection else 'skipped') for row in rows):
        raise SetupError('存在未就绪服务，不能完成安装', 409)
    required = {'postgres', 'metrics', 'resources', 'endpoints'}
    checks = {row.get('name'): row.get('status') for row in state.get('checks', [])}
    write_required = state.get('metrics_write_required')
    expected_write = 'success' if write_required else 'skipped'
    if (type(write_required) is not bool or any(checks.get(name) != 'success' for name in required)
            or checks.get('write') != expected_write):
        raise SetupError('最终连接检查尚未通过，不能完成安装', 409)


async def delete_and_confirm(k8s, obj):
    await k8s.delete(obj)
    if await k8s.get(obj) is not None:
        # The Job already retries with a bounded activeDeadlineSeconds. In particular,
        # a DELETE accepted for an object held by a finalizer is not completed cleanup.
        raise SetupError('临时安装资源仍在删除中，稍后自动重试', 503)


async def finalize(repo, k8s, installation_id):
    state = await repo.load()
    if state['id'] != installation_id or state['phase'] not in ('finalizing', 'succeeded'):
        raise SetupError('安装收尾任务与当前状态不匹配', 409)
    if state['phase'] == 'succeeded':
        return
    verify_completion(state)
    namespace = k8s.namespace
    setup = resource('Deployment', 'kubedoor-setup', namespace, 'apps/v1')
    deployed = await k8s.get(setup)
    if not deployed or deployed.get('metadata', {}).get('annotations', {}).get(OWNER) != installation_id:
        raise SetupError('安装服务归属不匹配', 409)
    # Do not terminate the installer until all sensitive cleanup actions can succeed.
    initial = resource('Secret', 'kubedoor-auth-init', namespace)
    old = await k8s.get(initial)
    if old:
        if old['metadata'].get('annotations', {}).get(OWNER) != installation_id:
            raise SetupError('初始化 Secret 归属不匹配', 409)
        if old.get('data'):
            old['data'] = {}
            old.pop('stringData', None)
            await k8s.replace(old)
    # Scaling uses a normal update through the deploy/scale API-independent merge patch.
    if deployed['spec'].get('replicas', 1) != 0:
        await k8s.request('PATCH', resource_path(setup), body={
            'apiVersion': 'apps/v1', 'kind': 'Deployment',
            'metadata': {'name': setup['metadata']['name'], 'namespace': namespace},
            'spec': {'replicas': 0}}, apply=True, params={'fieldManager': 'kubedoor-setup-finalizer', 'force': 'true'})
    for _ in range(40):
        deployed = await k8s.get(setup)
        status = deployed.get('status', {})
        if (deployed['spec'].get('replicas') == 0
                and status.get('observedGeneration', 0) >= deployed['metadata'].get('generation', 1)
                and status.get('replicas', 0) == 0):
            break
        await asyncio.sleep(3)
    else:
        raise SetupError('安装服务尚未停止，稍后自动重试', 503)
    binding = resource('RoleBinding', 'kubedoor-setup', namespace, 'rbac.authorization.k8s.io/v1')
    existing_binding = await k8s.get(binding)
    if existing_binding:
        if existing_binding['metadata'].get('annotations', {}).get(OWNER) != installation_id:
            raise SetupError('安装权限绑定归属不匹配，拒绝清理', 409)
        await delete_and_confirm(k8s, binding)
    for name in ('kubedoor-setup-config', 'kubedoor-setup-auth'):
        obj = resource('Secret', name, namespace)
        existing = await k8s.get(obj)
        if existing:
            if existing['metadata'].get('annotations', {}).get(OWNER) != installation_id:
                raise SetupError('安装凭据归属不匹配，拒绝清理', 409)
            await delete_and_confirm(k8s, obj)
    state = await repo.load()
    if state['id'] != installation_id or state['phase'] != 'finalizing':
        raise SetupError('安装状态已改变，停止收尾', 409)
    verify_completion(state)
    state.update(phase='succeeded', completed_at=utcnow(), private_key=None)
    state.pop('error', None)
    for row in state.get('checks', []):
        if row.get('name') == 'cleanup':
            row.update(status='success', message='初始化入口已关闭，临时凭据和部署权限已清理', updated_at=utcnow())
    await repo.save(state)


async def main():
    async with Kubernetes() as k8s:
        repo = Repository(k8s)
        installation_id = os.environ['SETUP_INSTALLATION_ID']
        while True:
            try:
                await finalize(repo, k8s, installation_id)
                return
            except Exception as error:
                # Keep the public marker finalizing, never turn a cleanup failure into success.
                message = error.message if isinstance(error, SetupError) else '安装收尾暂未完成，将自动重试'
                try:
                    state = await repo.load()
                    if state['id'] != installation_id or state['phase'] == 'succeeded':
                        return
                    state['error'] = message
                    for row in state.get('checks', []):
                        if row.get('name') == 'cleanup':
                            row.update(status='error', message=message, updated_at=utcnow())
                    await repo.save(state)
                except Exception:
                    pass
                print(json.dumps({'phase': 'finalizing', 'message': message}, ensure_ascii=False), flush=True)
                await asyncio.sleep(20)


if __name__ == '__main__':
    asyncio.run(main())
