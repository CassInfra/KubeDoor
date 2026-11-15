"""升级控制端时重新下发 Grafana 看板（grafana_dashboards.py；deploy/install.sh upgrade 经 kubectl exec 调用）。"""
import asyncio
import io
import json
import pathlib
import subprocess
import sys
from unittest.mock import AsyncMock

import pytest

import db
import general_settings
import grafana_dashboards as gd

MASTER = pathlib.Path(__file__).resolve().parent.parent
KEY = 'cluster_name'
TEMPLATES = gd.load_templates()
NAMES = [gd.configmap_name(filename) for filename in TEMPLATES]
TAGGED = [filename for filename, text in TEMPLATES.items() if gd.PLACEHOLDER in text]


def installed(key=KEY, stale=(), skip=(), keys=None):
    """装好的集群里看板 ConfigMap 的 data：占位符换成 key（keys 可以按文件指定）；stale 里的看板改掉标题，
    模拟旧版本；skip 里的看板集群里没有"""
    data = {}
    for filename, text in TEMPLATES.items():
        if filename in skip:
            continue
        text = text.replace(gd.PLACEHOLDER, (keys or {}).get(filename, key))
        if filename in stale:
            dashboard = json.loads(text)
            dashboard['title'] += '（旧版）'
            text = json.dumps(dashboard, ensure_ascii=False, indent=2)
        data[gd.configmap_name(filename)] = {filename: text}
    return data


def configmaps(data):
    return [{'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': name, 'namespace': 'kubedoor'}, 'data': files}
            for name, files in data.items()]


def deployment(names=NAMES, extra=(), volume=None):
    volume = volume or {'name': 'grafana-dash', 'projected': {
        'defaultMode': 420, 'sources': [{'configMap': {'name': name}} for name in names] + list(extra)}}
    return {'apiVersion': 'apps/v1', 'kind': 'Deployment', 'metadata': {'name': 'kubedoor-dash'},
            'spec': {'template': {'spec': {'volumes': [{'name': 'grafana-storage', 'emptyDir': {}}, volume]}}}}


def run(args, objects, capsys):
    """kubectl get -o json 的输出依次拼接起来作为 stdin（和 install.sh 一样）"""
    code = gd.main(args, stdin=io.StringIO('\n'.join(json.dumps(obj) for obj in objects)))
    out, err = capsys.readouterr()
    return code, out, err


def rendered(filename, key=KEY):
    return TEMPLATES[filename].replace(gd.PLACEHOLDER, key)


def items(out):
    manifest = json.loads(out)
    assert manifest['kind'] == 'List'
    return {item['metadata']['name']: item for item in manifest['items']}


def test_dashboards_and_configmap_names_follow_the_installers():
    # 同 kubedoor_setup/render.py 和旧脚本 dashboard_cm_name：一个看板文件一个 ConfigMap
    assert {'KubeDoor-AI.json', 'KubeDoor-Dash.json', 'KubeDoor-K8S.json', 'KubeDoor-Node.json'} <= set(TEMPLATES)
    assert gd.configmap_name('KubeDoor-Dash.json') == 'kubedoor-dash-dash'
    assert gd.configmap_name('KubeDoor-K8S.json') == 'kubedoor-dash-k8s'
    assert TAGGED, '看板里没有集群指标标签 key 的占位符了，检查 render.py 和这里是否还需要替换'


@pytest.mark.parametrize('filename', TAGGED)
def test_installed_tag_key_can_be_read_back_from_every_dashboard(filename):
    # 占位符要在某个变量的 query 里，升级时才能从集群里的现有看板读回安装时填的值
    assert gd.installed_tag_key(TEMPLATES[filename], rendered(filename)) == KEY
    assert gd.installed_tag_key(TEMPLATES[filename], TEMPLATES[filename]) is None  # 没替换过的不算
    assert gd.installed_tag_key(TEMPLATES[filename], rendered(filename, 'bad key')) is None  # 不像标签 key
    assert gd.installed_tag_key(TEMPLATES[filename], None) is None


def test_render_keeps_installed_tag_key_and_skips_unchanged_dashboards(capsys):
    stale = {'KubeDoor-Dash.json', 'KubeDoor-K8S.json'}
    code, out, err = run(['render'], [deployment(), *configmaps(installed(stale=stale))], capsys)
    assert code == 0, err
    found = items(out)
    assert set(found) == {'kubedoor-dash-dash', 'kubedoor-dash-k8s'}
    k8s = found['kubedoor-dash-k8s']
    assert k8s['data'] == {'KubeDoor-K8S.json': rendered('KubeDoor-K8S.json')}
    assert gd.PLACEHOLDER not in json.dumps(k8s)
    # 不带 namespace（由 kubectl -n 决定）；只加 part-of 标签，SSA 不会删掉已有的标签
    assert k8s['metadata'] == {'name': 'kubedoor-dash-k8s', 'labels': {'app.kubernetes.io/part-of': 'kubedoor'}}
    assert found['kubedoor-dash-dash']['data'] == {'KubeDoor-Dash.json': TEMPLATES['KubeDoor-Dash.json']}
    assert out.isascii()  # 中文转义成 \\uXXXX，经 kubectl exec 和 shell 变量传递不受编码影响
    assert f'{KEY}（沿用集群里现有的看板）' in err
    assert 'kubedoor-dash-node：没有变化' in err and 'kubedoor-dash-k8s：更新' in err


def test_render_prints_nothing_when_dashboards_are_up_to_date(capsys):
    code, out, err = run(['render'], [deployment(), *configmaps(installed())], capsys)
    assert (code, out) == (0, '')
    assert '更新' not in err and '新增' not in err


def test_missing_dashboard_uses_the_key_of_the_other_dashboards(capsys):
    node = 'KubeDoor-Node.json'
    code, out, err = run(['render'], configmaps(installed(key='k1', skip={node})), capsys)
    assert code == 0, err
    found = items(out)
    assert set(found) == {'kubedoor-dash-node'} and 'kubedoor-dash-node：新增' in err
    assert found['kubedoor-dash-node']['data'] == {node: rendered(node, 'k1')}


def test_each_dashboard_keeps_its_own_installed_key(capsys):
    keys = dict(zip(TAGGED, ('k1', 'k2')))
    data = installed(keys=keys, stale=set(TAGGED))
    code, out, err = run(['render'], configmaps(data), capsys)
    assert code == 0, err
    for filename, key in keys.items():
        assert items(out)[gd.configmap_name(filename)]['data'][filename] == rendered(filename, key)


@pytest.mark.parametrize('present', [False, True])
def test_without_usable_installed_key_the_general_setting_is_used(present, capsys, monkeypatch):
    # 集群里没有带标签 key 的看板，或者现有看板里还是占位符：才连库读通用配置
    lookup = AsyncMock(return_value='from_settings')
    monkeypatch.setattr(gd, 'settings_tag_key', lookup)
    data = {gd.configmap_name(f): {f: TEMPLATES[f]} for f in TAGGED} if present else {}
    code, out, err = run(['render'], configmaps(data), capsys)
    assert code == 0, err
    lookup.assert_awaited_once()
    for filename in TAGGED:
        assert items(out)[gd.configmap_name(filename)]['data'][filename] == rendered(filename, 'from_settings')
    assert 'from_settings（现有看板里取不到，用通用配置）' in err


def test_installed_key_means_no_database_access(capsys, monkeypatch):
    lookup = AsyncMock(side_effect=AssertionError('不应该连库'))
    monkeypatch.setattr(gd, 'settings_tag_key', lookup)
    code, _, err = run(['render'], configmaps(installed(stale=set(TAGGED))), capsys)
    assert code == 0, err
    lookup.assert_not_awaited()


def test_settings_failure_outputs_nothing(capsys, monkeypatch):
    monkeypatch.setattr(gd, 'settings_tag_key', AsyncMock(side_effect=OSError('connection refused')))
    code, out, err = run(['render'], [], capsys)
    assert (code, out) == (1, '')
    assert '读取通用配置里的集群指标标签 key 失败：OSError: connection refused' in err


def test_settings_tag_key_reads_the_database_and_never_falls_back_to_the_default(monkeypatch):
    init, close = AsyncMock(), AsyncMock()
    monkeypatch.setattr(db, 'init_pool', init)
    monkeypatch.setattr(db, 'close_pool', close)
    monkeypatch.setattr(general_settings, 'refresh', AsyncMock(return_value=True))
    monkeypatch.setattr(general_settings, 'value', lambda name: {'prom_k8s_tag_key': 'from_db'}[name])
    assert asyncio.run(gd.settings_tag_key()) == 'from_db'
    close.assert_awaited_once()
    monkeypatch.setattr(general_settings, 'refresh', AsyncMock(return_value=False))
    with pytest.raises(RuntimeError):
        asyncio.run(gd.settings_tag_key())
    assert close.await_count == 2


def test_mounts_adds_missing_dashboards_and_keeps_existing_sources(capsys):
    custom = [{'configMap': {'name': 'my-dashboards', 'items': [{'key': 'a.json', 'path': 'a.json'}]}},
              {'secret': {'name': 'extra'}}]
    names = [name for name in NAMES if name != 'kubedoor-dash-ai']
    code, out, err = run(['mounts'], [deployment(names, custom)], capsys)
    assert code == 0, err
    patch = json.loads(out)
    volumes = patch['spec']['template']['spec']['volumes']
    # sources 整体替换：原来的来源（包括自定义的）按原顺序保留，缺的追加；defaultMode 不在补丁里，原样保留
    assert volumes == [{'name': 'grafana-dash', 'projected': {'sources': [
        *[{'configMap': {'name': name}} for name in names], *custom, {'configMap': {'name': 'kubedoor-dash-ai'}}]}}]
    assert 'kubedoor-dash-ai' in err


def test_mounts_prints_nothing_when_complete(capsys):
    assert run(['mounts'], [deployment(), *configmaps(installed())], capsys)[:2] == (0, '')


@pytest.mark.parametrize('objects', [
    [deployment(volume={'name': 'grafana-dash', 'configMap': {'name': 'all-dashboards'}})],
    [deployment(volume={'name': 'other', 'emptyDir': {}})],
    [],
])
def test_mounts_refuses_custom_deployments(objects, capsys):
    code, out, err = run(['mounts'], objects, capsys)
    assert (code, out) == (1, '') and '生成 Grafana 看板失败' in err


@pytest.mark.parametrize('args, stdin', [(['render', 'x'], ''), (['apply'], ''), (['render'], 'not json'),
                                         (['render'], '[1, 2]')])
def test_bad_arguments_and_input(args, stdin, capsys):
    assert gd.main(args, stdin=io.StringIO(stdin)) == 1
    assert capsys.readouterr().out == ''


def test_kubectl_list_and_concatenated_objects_are_both_accepted():
    data = installed()
    text = json.dumps({'apiVersion': 'v1', 'kind': 'List', 'items': configmaps(data)}) + '\n' + json.dumps(deployment())
    found, maps = gd.cluster_state(gd.parse_objects(text))
    assert found == deployment() and maps == data


def test_command_line_entry_runs_without_master_dependencies():
    # install.sh 执行的就是这条命令；模块顶层只用标准库，不连库时不 import db / general_settings
    stdin = '\n'.join(json.dumps(obj) for obj in [deployment(), *configmaps(installed(stale={'KubeDoor-AI.json'}))])
    result = subprocess.run([sys.executable, '-c', 'import sys, grafana_dashboards; '
                             'raise SystemExit(grafana_dashboards.main() if "db" not in sys.modules else 9)', 'render'],
                            cwd=MASTER, input=stdin.encode(), capture_output=True)
    assert result.returncode == 0, result.stderr.decode('utf-8', 'replace')
    assert set(items(result.stdout.decode())) == {'kubedoor-dash-ai'}


def test_master_image_ships_the_dashboards():
    dockerfile = (MASTER / 'Dockerfile').read_text(encoding='utf-8')
    assert 'COPY src/kubedoor-setup/resources/dashboards/ /grafana-dashboards/' in dockerfile
    assert gd.IMAGE_DIR == pathlib.Path('/grafana-dashboards') and gd.REPO_DIR.is_dir()
