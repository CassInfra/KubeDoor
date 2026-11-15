"""`python3 -m grafana_dashboards render|mounts`：升级控制端时重新下发 Grafana 看板（deploy/install.sh upgrade 调用）。

看板 JSON 随 master 镜像发布：Dockerfile 把 src/kubedoor-setup/resources/dashboards 复制到 /grafana-dashboards
（在仓库里运行时直接读那个目录）。和安装时一样，一个看板文件一个 ConfigMap（KubeDoor-Dash.json → kubedoor-dash-dash，
同 kubedoor_setup/render.py 和旧脚本的 dashboard_cm_name），由 Deployment kubedoor-dash 的 projected 卷 grafana-dash
挂给 Grafana；ConfigMap 更新后 Grafana 约 1 分钟内自己重新加载，不用重启。

stdin：集群里现有的 Deployment kubedoor-dash 和看板 ConfigMap（kubectl get -o json 的输出，可以依次拼接多份）。
  render  stdout 输出内容有变化、或集群里还没有的看板 ConfigMap（kind: List，不带 namespace），交给
          kubectl apply --server-side；都没有变化时什么也不输出。看板里的 __PROM_K8S_TAG_KEY__ 沿用集群里现有看板
          的值（和已装集群的采集一致），现有看板里都取不到时才连库读通用配置的「集群指标标签 key」。
  mounts  stdout 输出 Deployment kubedoor-dash 的 strategic merge patch：grafana-dash 卷里补上缺少的看板 ConfigMap
          （只增不减，已有的来源原样保留）；不缺时什么也不输出。
stderr 是给人看的说明。退出码：0 成功；1 失败（参数或输入不对、读不到看板文件、需要连库时连不上）。
"""

import asyncio
import json
import re
import sys
from pathlib import Path

PLACEHOLDER = '__PROM_K8S_TAG_KEY__'
# 同通用配置「集群指标标签 key」的校验（general_settings/fields.py：字母、数字、下划线，不以数字开头，最长 128）
TAG_KEY = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,127}')
DEPLOYMENT, VOLUME = 'kubedoor-dash', 'grafana-dash'
LABELS = {'app.kubernetes.io/part-of': 'kubedoor'}
IMAGE_DIR = Path('/grafana-dashboards')
REPO_DIR = Path(__file__).resolve().parent.parent / 'kubedoor-setup' / 'resources' / 'dashboards'
MAX_STDIN = 32 * 1024 * 1024
USAGE = ('用法: python3 -m grafana_dashboards render|mounts'
         '（stdin 为集群里现有的 Deployment kubedoor-dash 和看板 ConfigMap，kubectl get -o json 的输出）')


def configmap_name(filename):
    """KubeDoor-Dash.json → kubedoor-dash-dash"""
    return 'kubedoor-dash-' + Path(filename).stem.lower().removeprefix('kubedoor-')


def load_templates(directory=None):
    """{文件名: 看板 JSON 文本}，按文件名排序"""
    if directory is None:
        directory = next((path for path in (IMAGE_DIR, REPO_DIR) if path.is_dir()), IMAGE_DIR)
    found = {path.name: path.read_text(encoding='utf-8') for path in sorted(Path(directory).glob('*.json'))}
    if not found:
        raise ValueError(f'{directory} 里没有看板文件')
    return found


def parse_objects(text):
    """kubectl get -o json 的输出（可以依次拼接多份；单个对象或 kind: List）→ 对象列表"""
    decoder, objects, index = json.JSONDecoder(), [], 0
    while True:
        while index < len(text) and text[index].isspace():
            index += 1
        if index == len(text):
            return objects
        obj, index = decoder.raw_decode(text, index)
        items = obj.get('items') if isinstance(obj, dict) and obj.get('kind') == 'List' else [obj]
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise ValueError('输入不是 kubectl get -o json 的输出')
        objects.extend(items)


def cluster_state(objects):
    """(Deployment kubedoor-dash 或 None, {ConfigMap 名: data})"""
    deployment, data = None, {}
    for obj in objects:
        name = (obj.get('metadata') or {}).get('name')
        if obj.get('kind') == 'Deployment' and name == DEPLOYMENT:
            deployment = obj
        elif obj.get('kind') == 'ConfigMap' and name:
            data[name] = obj.get('data') or {}
    return deployment, data


def _variables(text):
    try:
        dashboard = json.loads(text)
    except ValueError:
        return []
    variables = (dashboard.get('templating') or {}).get('list') if isinstance(dashboard, dict) else None
    return [variable for variable in variables or [] if isinstance(variable, dict)]


def installed_tag_key(template, current):
    """集群里这个看板现有版本里填的标签 key：模板里 query 含占位符的变量，按变量名在现有看板里找，
    用模板的 query 去匹配现有的 query。没有现有版本、变量被改过、取到的不像标签 key 时返回 None"""
    if not isinstance(current, str):
        return None
    existing = {variable.get('name'): variable.get('query') for variable in _variables(current)}
    for variable in _variables(template):
        query, found = variable.get('query'), existing.get(variable.get('name'))
        if not isinstance(query, str) or PLACEHOLDER not in query or not isinstance(found, str):
            continue
        match = re.fullmatch(re.escape(query).replace(re.escape(PLACEHOLDER), '(.+?)'), found)
        if match and len(set(match.groups())) == 1:
            key = match.group(1)
            if key != PLACEHOLDER and TAG_KEY.fullmatch(key):
                return key
    return None


def installed_tag_keys(templates, data):
    """{看板文件名: 集群里现有看板的标签 key}，只含取得到的"""
    keys = {}
    for filename, text in templates.items():
        if PLACEHOLDER in text:
            key = installed_tag_key(text, data.get(configmap_name(filename), {}).get(filename))
            if key:
                keys[filename] = key
    return keys


def needs_settings(templates, keys):
    """现有看板里一个标签 key 都取不到、又有看板要填时，才需要连库读通用配置"""
    return not keys and any(PLACEHOLDER in text for text in templates.values())


def render(templates, data, keys, settings_key=None):
    """(ConfigMap 清单或 None, 说明行)。每个看板的标签 key：自己现有版本里的 → 其他现有看板里的 → 通用配置"""
    shared = next(iter(keys.values()), None) or settings_key
    items, notes = [], []
    for filename, text in templates.items():
        name = configmap_name(filename)
        if PLACEHOLDER in text:
            key = keys.get(filename) or shared
            if not key:
                raise ValueError(f'{filename} 要填集群指标标签 key，但没有取到')
            text = text.replace(PLACEHOLDER, key)
        current = data.get(name)
        if current is not None and current.get(filename) == text:
            notes.append(f'  {name}：没有变化')
            continue
        notes.append(f'  {name}：{"新增" if current is None else "更新"}')
        items.append({'apiVersion': 'v1', 'kind': 'ConfigMap',
                      'metadata': {'name': name, 'labels': dict(LABELS)}, 'data': {filename: text}})
    return ({'apiVersion': 'v1', 'kind': 'List', 'items': items} if items else None), notes


def mounts_patch(templates, deployment):
    """(strategic merge patch 或 None, 补上的 ConfigMap 名)。
    projected 卷的 sources 没有 patchMergeKey、整体替换，所以补丁里是原来的全部来源加上缺少的"""
    if deployment is None:
        raise ValueError(f'输入里没有 Deployment {DEPLOYMENT}')
    pod = ((deployment.get('spec') or {}).get('template') or {}).get('spec') or {}
    volume = next((item for item in pod.get('volumes') or []
                   if isinstance(item, dict) and item.get('name') == VOLUME), None)
    projected = volume.get('projected') if volume else None
    if not isinstance(projected, dict):
        raise ValueError(f'Deployment {DEPLOYMENT} 没有 projected 卷 {VOLUME}（自定义的部署），不自动补挂载')
    sources = projected.get('sources') or []
    mounted = {(source.get('configMap') or {}).get('name') for source in sources if isinstance(source, dict)}
    missing = [configmap_name(filename) for filename in templates if configmap_name(filename) not in mounted]
    if not missing:
        return None, []
    sources = list(sources) + [{'configMap': {'name': name}} for name in missing]
    return {'spec': {'template': {'spec': {'volumes': [{'name': VOLUME, 'projected': {'sources': sources}}]}}}}, missing


async def settings_tag_key():
    """通用配置里的集群指标标签 key：连库读，读不到就报错（不拿默认值顶替，免得看板和采集对不上）"""
    import db
    import general_settings

    await db.init_pool()
    try:
        if not await general_settings.refresh():
            raise RuntimeError('读取通用配置失败')
        return general_settings.value('prom_k8s_tag_key')
    finally:
        await db.close_pool()


def _note(text):
    print(text, file=sys.stderr, flush=True)


def main(argv=None, stdin=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args not in (['render'], ['mounts']):
        _note(USAGE)
        return 1
    stream = sys.stdin if stdin is None else stdin
    raw = getattr(stream, 'buffer', stream).read(MAX_STDIN + 1)
    try:
        if len(raw) > MAX_STDIN:
            raise ValueError(f'输入太大（最多 {MAX_STDIN} 字节）')
        deployment, data = cluster_state(parse_objects(raw.decode('utf-8') if isinstance(raw, bytes) else raw))
        templates = load_templates()
        if args == ['mounts']:
            patch, missing = mounts_patch(templates, deployment)
            if patch:
                _note('补上看板挂载：' + '、'.join(missing))
                print(json.dumps(patch))
            return 0
        keys, settings_key = installed_tag_keys(templates, data), None
        if needs_settings(templates, keys):
            try:
                settings_key = asyncio.run(settings_tag_key())
            except Exception as exc:
                _note(f'读取通用配置里的集群指标标签 key 失败：{type(exc).__name__}: {exc}')
                return 1
        manifest, notes = render(templates, data, keys, settings_key)
    except (OSError, ValueError) as exc:
        _note(f'生成 Grafana 看板失败：{exc}')
        return 1
    if keys:
        _note(f'集群指标标签 key：{"、".join(sorted(set(keys.values())))}（沿用集群里现有的看板）')
    elif settings_key:
        _note(f'集群指标标签 key：{settings_key}（现有看板里取不到，用通用配置）')
    for line in notes:
        _note(line)
    if manifest:
        print(json.dumps(manifest))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
