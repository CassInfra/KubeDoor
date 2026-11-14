"""旧配置迁移：kubedoor-config 里的环境变量 → 通用配置；MSG_TYPE / MSG_TOKEN / DEFAULT_AT → 消息通知的全局配置。

- 通用配置：对每一项，库里还没有、旧环境变量有值（去掉首尾空白后非空）、校验通过、且不等于默认值才写入；
  库里已有的一律不动（页面改过的以库为准）。整个导入在一个事务里读 - 合并 - 写，updated_by = legacy-import。
  导入过的项记在存库的值里（fields.IMPORTED_MARK），以后在页面上删掉也不会再导入：网页安装的 Secret
  kubedoor-connection（PROM_URL、METRICS_WRITE_URL）一直挂在 master 上，迁移没清理时 kubedoor-config 也还在。
  AGENT_INSTALL_DEFAULTS（JSON）里取 oss_url、image_repo、三个采集镜像、master_url（→ kubedoor_exturl）、
  prom_k8s_tag_key；对应的环境变量（OSS_URL、IMAGE_*、KUBEDOOR_EXTURL、PROM_K8S_TAG_KEY）有值时以环境变量为准；
  agent_tag 不导入（推荐版本跟着控制端走）。校验不通过记为 invalid：不写入，日志不打印值。
- 消息通知：库里没有 notification 这一行、且 MSG_TOKEN 非空时导入（INSERT ... ON CONFLICT DO NOTHING）。

master 启动时（api.on_startup）和 `python3 -m general_settings migrate`（cli.py）都调用这里，可以重复执行。
"""

import json
import os

from loguru import logger

from auth.crypto import encrypt_secret

from . import fields, service

LEGACY_AUTHOR = 'legacy-import'
NOTIFICATION_KEY = 'notification'
INVALID = object()
# AGENT_INSTALL_DEFAULTS 里的键 → 通用配置的 key
AGENT_DEFAULT_KEYS = {
    'oss_url': 'oss_url',
    'image_repo': 'image_repo',
    'image_vmagent': 'image_vmagent',
    'image_kube_state_metrics': 'image_kube_state_metrics',
    'image_node_exporter': 'image_node_exporter',
    'master_url': 'kubedoor_exturl',
    'prom_k8s_tag_key': 'prom_k8s_tag_key',
}


def legacy_values(env):
    """旧配置 → {key: (来源, 规范化后的值 或 INVALID)}；没有旧值（没设置或只有空白）的项不在结果里"""
    result = {}

    def take(key, source, raw):
        if raw is None:
            return
        if not isinstance(raw, str):
            result[key] = (source, INVALID)
            return
        if not raw.strip():
            return
        try:
            result[key] = (source, fields.legacy(key, raw))
        except ValueError:
            result[key] = (source, INVALID)

    for key, spec in fields.FIELDS.items():
        if spec.env:
            take(key, spec.env, env.get(spec.env))
    raw = env.get('AGENT_INSTALL_DEFAULTS')
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError
        except (ValueError, RecursionError):
            parsed = None
        for name, key in AGENT_DEFAULT_KEYS.items():
            if key in result:  # 对应的环境变量有值，以环境变量为准
                continue
            source = f'AGENT_INSTALL_DEFAULTS.{name}'
            if parsed is None:  # 整个 JSON 解析不了：从它取值的项都算 invalid
                result[key] = (source, INVALID)
            else:
                take(key, source, parsed.get(name))
    return result


async def import_general(env):
    """6.1 环境变量 → 通用配置。返回 {'imported': [...], 'present': [...], 'invalid': [...]}（只有 key 名）。
    present 是库里已有的项（不管有没有旧值）；没有旧值、或旧值等于默认值的项不在任何列表里。数据库出错抛异常"""
    found = legacy_values(env)
    result = {'imported': [], 'present': [], 'invalid': []}
    sources = {}

    def change(current):
        for items in result.values():
            items.clear()
        new = dict(current)
        done = current.get(fields.IMPORTED_MARK)
        done = {item for item in done if isinstance(item, str)} if isinstance(done, list) else set()
        for key, spec in fields.FIELDS.items():
            if key in current:
                result['present'].append(key)
                continue
            if key not in found:
                continue
            source, item = found[key]
            sources[key] = source
            if item is INVALID:
                result['invalid'].append(key)
                continue
            if key in done:  # 以前导入过、后来在页面上删掉了(恢复默认 / 清空):以页面为准,不再导入
                continue
            if item == spec.default and type(item) is type(spec.default):
                continue
            new[key] = encrypt_secret(item) if spec.secret else item
            result['imported'].append(key)
        if not result['imported']:
            return None
        done.update(result['imported'])
        new[fields.IMPORTED_MARK] = [key for key in fields.FIELDS if key in done]
        return new

    async with service.write_lock:
        row, wrote = await service.storage.update(change, LEGACY_AUTHOR)
    if wrote:
        service.apply(row)
        names = '、'.join(f'{key}({sources[key]})' for key in result['imported'])
        logger.info(f'已把旧配置导入通用配置:{names}')
    for key in result['invalid']:
        logger.warning(f'旧配置 {sources[key]}({fields.LABELS[key]})格式不对,没有导入通用配置;'
                       '请到「系统管理 → 通用配置」填写')
    return result


async def import_notification(env, refresh=True):
    """6.2 MSG_TYPE / MSG_TOKEN / DEFAULT_AT → 消息通知的全局配置。返回 present / absent / imported / invalid。
    refresh：导入后让本进程的消息通知缓存马上刷新（master 进程里用；CLI 不需要）。数据库出错抛异常"""
    if await service.storage.setting_exists(NOTIFICATION_KEY):
        return 'present'
    token = env.get('MSG_TOKEN')
    if not isinstance(token, str) or not token.strip():
        return 'absent'
    from notify import channels  # 延迟 import:notify 在顶层 import auth.store,auth.store 又 import general_settings

    kind = env.get('MSG_TYPE')
    kind = kind.strip().lower() if isinstance(kind, str) else ''
    if kind not in channels.TYPES:
        logger.warning(f'旧配置 MSG_TYPE 不是 {" / ".join(channels.TYPES)} 之一,消息通知没有导入;'
                       '请到「系统管理 → 消息通知」填写')
        return 'invalid'
    try:
        token = channels.normalize_token(kind, token)
        at = channels.normalize_at(env.get('DEFAULT_AT'))
    except ValueError:
        logger.warning('旧配置 MSG_TOKEN / DEFAULT_AT 格式不对,消息通知没有导入;请到「系统管理 → 消息通知」填写')
        return 'invalid'
    value = {'default': {'type': kind, 'token': encrypt_secret(token), 'at': at}}
    if not await service.storage.insert_absent(NOTIFICATION_KEY, value, LEGACY_AUTHOR):
        return 'present'
    logger.info(f'已把旧的 MSG_TYPE / MSG_TOKEN / DEFAULT_AT 导入「系统管理 → 消息通知」的全局配置'
                f'({channels.TYPE_LABELS[kind]})')
    if refresh:
        try:
            from notify import service as notify_service

            await notify_service.refresh()
        except Exception as exc:  # 刷新失败不要紧:消息通知 10 秒后自己会刷新
            logger.warning(f'刷新消息通知配置失败:{type(exc).__name__}: {exc}')
    return 'imported'


async def import_legacy(env=None):
    """6.1 + 6.2：env 为 None 时读进程环境变量。返回
    {'general_settings': {'imported': [...], 'present': [...], 'invalid': [...]}, 'notification': '<状态>'}。
    数据库出错抛异常（启动时只记 error，CLI 返回 1）"""
    env = os.environ if env is None else env
    general = await import_general(env)
    notification = await import_notification(env)
    return {'general_settings': general, 'notification': notification}
