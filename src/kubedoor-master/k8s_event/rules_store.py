"""K8S 事件告警规则的内存快照。

- 规则存 auth_settings(key=event_alert_rules);库里没有这一行时用内置默认规则(同目录 default_alert_rules.json,
  随版本更新),不写库,页面上显示「当前使用默认规则」
- 快照是整体替换的只读对象(规则冻结成 MappingProxyType / tuple),匹配器每个事件取一次 current(),
  任何线程读都安全:rules_api.on_startup 时加载,后台每 10 秒刷新,页面保存后立即替换(apply),不用重启 master
- 读库失败时保留上一份快照;还没加载成功之前(单测、启动时库不可用)用内置默认规则
- 库里的规则不一定通过严格校验(从旧规则文件导入时原样保存):这里只做宽松处理,不是数组的分组当成空、
  不是对象的规则跳过,其余原样交给匹配器,保持用户原来的规则行为
"""

import asyncio
import json
import threading
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from loguru import logger

import db
from auth.store import store

SETTING_KEY = 'event_alert_rules'
DEFAULT_FILE = Path(__file__).with_name('default_alert_rules.json')
REFRESH_INTERVAL = 10  # 秒
SOURCE_DATABASE = 'database'  # 库里保存的规则
SOURCE_DEFAULT = 'default'  # 内置默认规则(库里没有)
SOURCE_LABELS = {SOURCE_DATABASE: '数据库', SOURCE_DEFAULT: '内置默认规则'}
GROUP_KEYS = ('global_ignore_rules', 'alert_rules')


@dataclass(frozen=True)
class RuleSet:
    """一份生效的规则(只读)。ignore_rules / alert_rules 是冻结后的规则(MappingProxyType)元组"""
    ignore_rules: tuple = ()
    alert_rules: tuple = ()
    source: str = SOURCE_DEFAULT
    loaded: bool = False  # 是否已经从库里加载过(False:还没加载成功,用的是内置默认规则)
    skipped: int = 0  # 格式不对、匹配时跳过的条目数(分组不是数组、规则不是对象)
    fingerprint: str = ''  # 规则内容的指纹,刷新时内容没变就不重建


_snapshot = None
_snapshot_lock = threading.Lock()
_default_text = None
_default_failed = False
_summary = None  # 上次记日志的规则概况,变了才再记
_refresh_failed = False
_applied = 0  # apply 的次数:刷新读库期间有没有保存过,见 refresh


def freeze(value):
    """JSON 值 → 只读结构:dict → MappingProxyType,list → tuple"""
    if isinstance(value, dict):
        return MappingProxyType({key: freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(freeze(item) for item in value)
    return value


def default_rules():
    """内置默认规则(每次返回新的副本,调用方可以随意修改)。文件读不了时记一次 error,返回空规则"""
    global _default_text, _default_failed
    if _default_text is None:
        try:
            text = DEFAULT_FILE.read_text(encoding='utf-8')
            json.loads(text)
        except Exception as exc:
            if not _default_failed:
                logger.error(f'读取内置默认事件告警规则 {DEFAULT_FILE} 失败:{type(exc).__name__}: {exc}')
            _default_failed = True
            return {key: [] for key in GROUP_KEYS}
        _default_text = text
    return json.loads(_default_text)


def _fingerprint(value, source):
    return source + ':' + json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def build(value, source, loaded=True):
    """存库格式的规则 → 快照。宽松处理:分组不是数组当成空,不是对象的规则跳过(都计入 skipped)"""
    groups, skipped = [], 0
    raw = value if isinstance(value, dict) else {}
    if not isinstance(value, dict):
        skipped += 1
    for key in GROUP_KEYS:
        rules = raw.get(key, [])
        if not isinstance(rules, list):
            skipped += 1
            rules = []
        kept = tuple(freeze(rule) for rule in rules if isinstance(rule, dict))
        skipped += len(rules) - len(kept)
        groups.append(kept)
    return RuleSet(groups[0], groups[1], source, loaded, skipped, _fingerprint(value, source))


def describe(ruleset):
    """规则概况(日志用)"""
    def count(rules):
        enabled = sum(1 for rule in rules if rule.get('enabled', True))
        return f'{len(rules)} 条(启用 {enabled} 条)'

    text = (f'忽略规则 {count(ruleset.ignore_rules)},告警规则 {count(ruleset.alert_rules)},'
            f'来源:{SOURCE_LABELS.get(ruleset.source, ruleset.source)}')
    if ruleset.skipped:
        text += f';有 {ruleset.skipped} 处格式不对的内容匹配时跳过,请在「系统管理 → 事件告警规则」改正后保存'
    return text


def current():
    """当前生效的规则快照;还没加载过时是内置默认规则(loaded=False)"""
    global _snapshot
    snapshot = _snapshot
    if snapshot is None:
        with _snapshot_lock:
            if _snapshot is None:
                _snapshot = build(default_rules(), SOURCE_DEFAULT, loaded=False)
            snapshot = _snapshot
    return snapshot


def is_loaded():
    return current().loaded


def apply(value, source=SOURCE_DATABASE):
    """用存库格式的规则替换快照;页面保存后立即调用,不等下一次刷新。内容没变时沿用原来的快照"""
    global _snapshot, _summary, _applied
    _applied += 1
    old = _snapshot
    if old is not None and old.loaded and old.fingerprint == _fingerprint(value, source):
        return old
    snapshot = build(value, source)
    _snapshot = snapshot
    text = describe(snapshot)
    if text != _summary:
        _summary = text
        log = logger.warning if snapshot.skipped else logger.info
        log(f'事件告警规则已加载:{text}')
    return snapshot


async def load_row():
    """读库里的规则行({'value', 'updated_by', 'updated_at'});没有返回 None,读库失败抛异常"""
    return await store.get_setting_row(SETTING_KEY)


async def save_row(value, updated_by):
    """保存规则(覆盖),返回保存后的行(格式同 load_row);失败抛异常"""
    return await store.put_setting_row(SETTING_KEY, value, updated_by)


async def insert_absent(value, updated_by):
    """库里还没有规则时写入(INSERT ... ON CONFLICT DO NOTHING),返回是否写入了;已有的一律不动"""
    row = await db.pg_fetchrow(
        'INSERT INTO auth_settings (key, value, updated_by) VALUES ($1, $2::jsonb, $3) '
        'ON CONFLICT (key) DO NOTHING RETURNING key',
        SETTING_KEY, json.dumps(value, ensure_ascii=False), updated_by)
    return row is not None


async def refresh():
    """从库里重新加载规则(库里没有时用内置默认规则)。读库失败时保留原来的快照(只在第一次失败时记 warning),
    返回是否成功。读库期间页面保存过(apply 过新规则)时丢掉这次读到的结果:它可能是保存之前的旧值,
    换上去会把刚保存的规则倒回去,等下一次刷新"""
    global _refresh_failed
    started = _applied
    try:
        row = await load_row()
    except Exception as exc:
        if not _refresh_failed:
            logger.warning(f'读取事件告警规则失败,继续使用上次加载的规则:{type(exc).__name__}: {exc}')
        _refresh_failed = True
        return False
    if _refresh_failed:
        logger.info('读取事件告警规则已恢复')
        _refresh_failed = False
    if _applied != started:
        return True
    if row is None:
        apply(default_rules(), SOURCE_DEFAULT)
    else:
        apply(row['value'], SOURCE_DATABASE)
    return True


async def refresh_loop(interval=REFRESH_INTERVAL):
    """后台定时刷新(直接改库、命令行导入旧规则文件后也能跟上);由 rules_api.on_startup 启动"""
    while True:
        await asyncio.sleep(interval)
        try:
            await refresh()
        except Exception as exc:  # refresh 自己不抛,这里只是兜底:后台任务不能因为一次异常就退出
            logger.warning(f'刷新事件告警规则异常:{type(exc).__name__}: {exc}')
