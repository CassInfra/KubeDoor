"""The exported bundle can initialize the real installer without running the shell again."""
import base64
import copy
import json
import re

import pytest

from bootstrap_shell import OWNER, ShellCluster, documents
from kubedoor_setup.bootstrap_credentials import ensure_setup_key
from kubedoor_setup.repository import Repository
from test_setup_api import FakeKubernetes


STATE = '/api/v1/namespaces/kubedoor/configmaps/kubedoor-setup-state'
AUTH = '/api/v1/namespaces/kubedoor/secrets/kubedoor-setup-auth'


def bundle_id(objects):
    namespaced = [obj for obj in objects if obj['kind'] != 'Namespace']
    ids = {obj['metadata']['annotations'][OWNER] for obj in namespaced}
    assert len(ids) == 1
    installation_id = ids.pop()
    assert re.fullmatch(r'yaml-[0-9a-f]{32}', installation_id)
    return installation_id


@pytest.mark.parametrize('shell', ['sh', 'bash', 'dash'])
def test_export_file_is_standalone_and_uses_selected_images(tmp_path, shell):
    cluster = ShellCluster(tmp_path)
    output = tmp_path / 'first install.yaml'
    result = cluster.run('--export', output.name, '--namespace', 'exported-kd',
                         '--nodeport', '31234', '--tag', 'export-test',
                         '--setup-tag', 'setup-export', '--image-pull-secret', 'pull-key', shell=shell)
    assert result.returncode == 0, result.stderr
    objects = documents(output.read_text(encoding='utf-8'))
    bundle_id(objects)
    assert not any(obj['kind'] == 'Secret' for obj in objects)
    assert {obj['metadata']['namespace'] for obj in objects if obj['kind'] != 'Namespace'} == {'exported-kd'}
    resources = {(obj['kind'], obj['metadata']['name']): obj for obj in objects}
    assert resources['Service', 'kubedoor-web']['spec']['ports'][0]['nodePort'] == 31234
    setup = resources['Deployment', 'kubedoor-setup']['spec']['template']['spec']
    assert setup['imagePullSecrets'] == [{'name': 'pull-key'}]
    assert setup['containers'][0]['image'].endswith(':setup-export')
    values = {item['name']: item.get('value') for item in setup['containers'][0]['env']}
    versions = json.loads(values['SETUP_DEFAULTS'])
    assert versions['TAG_SETUP'] == 'setup-export'
    assert all(versions['TAG_' + part] == 'export-test' for part in ('MASTER', 'AGENT', 'WEB', 'AI', 'ALARM'))
    assert cluster.read()['calls'] == []

    original = output.read_bytes()
    refused = cluster.run('--export', output.name, shell=shell)
    assert refused.returncode != 0
    assert output.read_bytes() == original
    assert cluster.read()['calls'] == []


@pytest.mark.parametrize('shell', ['sh', 'bash', 'dash'])
def test_export_stdout_contains_only_yaml_and_allocates_distinct_identity(tmp_path, shell):
    cluster = ShellCluster(tmp_path)
    first = cluster.run('--export', '-', shell=shell)
    second = cluster.run('--export', '-', shell=shell)
    assert first.returncode == second.returncode == 0, first.stderr + second.stderr
    first_objects, second_objects = documents(first.stdout), documents(second.stdout)
    assert bundle_id(first_objects) != bundle_id(second_objects)
    state = next(obj for obj in first_objects if obj['metadata']['name'] == 'kubedoor-setup-state')
    assert not {'state.json', 'status.json', 'phase', 'installation-id', 'public-url'} & state['data'].keys()
    assert cluster.read()['calls'] == []


def test_export_to_unavailable_directory_does_not_install(tmp_path):
    cluster = ShellCluster(tmp_path)
    result = cluster.run('--export', 'missing-directory/install.yaml')
    assert result.returncode != 0
    assert cluster.read()['calls'] == []


def test_export_and_preview_are_mutually_exclusive(tmp_path):
    cluster = ShellCluster(tmp_path)
    result = cluster.run('--render', '--export', 'install.yaml')
    assert result.returncode != 0
    assert not (tmp_path / 'install.yaml').exists()
    assert cluster.read()['calls'] == []


@pytest.mark.asyncio
async def test_applying_exported_state_can_start_installer_and_create_key(tmp_path):
    cluster = ShellCluster(tmp_path)
    result = cluster.run('--export', '-')
    assert result.returncode == 0, result.stderr
    objects = documents(result.stdout)
    installation_id = bundle_id(objects)
    marker = copy.deepcopy(next(obj for obj in objects if obj['metadata']['name'] == 'kubedoor-setup-state'))
    # Kubernetes supplies resourceVersion/UID when the exported ConfigMap is applied.
    marker['metadata'].update(resourceVersion='1', uid='apiserver-assigned-uid')
    k8s = FakeKubernetes()
    k8s.resources[STATE] = marker
    repo = Repository(k8s)
    key = await ensure_setup_key(repo)
    state = await repo.load()
    assert state['id'] == installation_id
    assert state['phase'] == 'draft' and state['auth_initialized'] is True
    assert base64.b64decode(k8s.resources[AUTH]['data']['SETUP_KEY']).decode() == key
    assert k8s.resources[AUTH]['metadata']['annotations'][OWNER] == installation_id
    persisted = k8s.resources[STATE]['data']
    assert json.loads(persisted['status.json'])['installation_id'] == installation_id
    assert json.loads(persisted['state.json'])['id'] == installation_id
    assert key not in json.dumps(persisted)
    assert await ensure_setup_key(Repository(k8s)) == key
