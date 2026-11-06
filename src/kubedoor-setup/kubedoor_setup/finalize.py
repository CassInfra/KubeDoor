"""One-shot installation handoff, under a narrowly scoped ServiceAccount."""
from __future__ import annotations

import asyncio
import json
import os

from .errors import SetupError
from .kubernetes import (AGENT_INSTALLER, OWNER, RBAC, Kubernetes, agent_installer_binding, resource, resource_path,
                         service_account_token)
from .repository import STATE_NAME, Repository, utcnow


def verify_completion(state):
    """Require the complete chosen installation, never infer selection from a skipped result."""
    # Grafana 看板必装；告警组件（kubedoor-alarm、vmalert、Alertmanager）可以不装
    mandatory = {'web', 'master', 'ai', 'grafana'}
    known = mandatory | {'alarm', 'vmalert', 'alertmanager'}
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
    agent = state.get('agent')
    if agent is not None and (not isinstance(agent, dict)
                              or agent.get('status') not in ('succeeded', 'partial', 'skipped', 'unavailable')):
        raise SetupError('在本集群安装 Agent 的步骤尚未结束，不能完成安装', 409)


async def delete_and_confirm(k8s, obj):
    await k8s.delete(obj)
    if await k8s.get(obj) is not None:
        # The Job already retries with a bounded activeDeadlineSeconds. In particular,
        # a DELETE accepted for an object held by a finalizer is not completed cleanup.
        raise SetupError('临时安装资源仍在删除中，稍后自动重试', 503)


async def _installer_get(k8s, obj, token):
    """用安装账号自己的令牌读取：403 说明令牌已没有集群权限，绑定已删除或已不生效。"""
    try:
        return await k8s.get(obj, token=token)
    except SetupError as error:
        if error.status == 403:
            return None
        raise


async def revoke_agent_installer(k8s, namespace, installation_id):
    """撤销在本集群安装 Agent 的临时集群权限；没有授予或已撤销时跳过。

    收尾账号没有集群级权限：先给安装账号签一个短期令牌，用它删除自己的 cluster-admin 绑定，
    确认删除后再用收尾账号删除安装账号。必须先删绑定：否则以后同名账号重建会继承这个绑定。
    """
    # 引导脚本在状态 ConfigMap 里记下了本次是否授予；旧版本脚本既不创建账号，也不给收尾账号相应权限
    marker = await k8s.get(resource('ConfigMap', STATE_NAME, namespace))
    if ((marker or {}).get('data') or {}).get('agent-installer') != agent_installer_binding(namespace):
        return
    account = resource('ServiceAccount', AGENT_INSTALLER, namespace)
    existing = await k8s.get(account)
    if not existing:
        return
    if existing.get('metadata', {}).get('annotations', {}).get(OWNER) != installation_id:
        raise SetupError('Agent 安装账号归属不匹配，拒绝清理', 409)
    token = await service_account_token(k8s, namespace, AGENT_INSTALLER, 600)
    binding = {'apiVersion': RBAC, 'kind': 'ClusterRoleBinding', 'metadata': {'name': agent_installer_binding(namespace)}}
    current = await _installer_get(k8s, binding, token)
    if current:
        if current.get('metadata', {}).get('annotations', {}).get(OWNER) != installation_id:
            raise SetupError('Agent 安装账号的集群权限绑定归属不匹配，拒绝清理', 409)
        await k8s.delete(binding, token=token)
        if await _installer_get(k8s, binding, token) is not None:
            raise SetupError('临时集群权限仍在删除中，稍后自动重试', 503)
    await delete_and_confirm(k8s, account)


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
    # 安装服务已停止，不会再签发令牌；先撤销集群级临时权限，再删除安装服务自己的 RoleBinding
    await revoke_agent_installer(k8s, namespace, installation_id)
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
