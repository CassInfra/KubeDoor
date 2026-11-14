"""通用配置的存储与内存快照。

- 存储：auth_settings 里 key=general_settings 这一行，value 是平铺的 JSON 对象，只存显式设置过的项；
  三个地址用 auth.crypto.encrypt_secret 加密后存字符串（没有有效的 AI_ENCRYPTION_KEY 时退回 plain:，同消息通知）。
  写库都在一个事务里：advisory 锁 + SELECT ... FOR UPDATE 读出整行 → 合并 → 写回，页面保存和 CLI 迁移（另一个进程）
  互不覆盖。直接用 db 层读写，不经过 auth.store（auth.store 在顶层 import general_settings，反过来会循环 import）。
- 快照：内存里的只读快照（整体替换），启动时加载，后台每 10 秒刷新，保存后立即替换；读库失败保留上一份。
  快照加载之前（单测、CLI）value() 返回默认值。value() 只读模块变量，任何线程调用都安全。
"""

import asyncio
import json
from dataclasses import dataclass, field
from types import MappingProxyType

from loguru import logger

import db
from auth.crypto import decrypt_secret, encrypt_secret

from . import fields

SETTING_KEY = 'general_settings'
REFRESH_INTERVAL = 10  # 秒
UNDECRYPTABLE = '已保存的地址无法解密（加密密钥变了？），请重新填写'
_MISSING = object()

_SELECT = 'SELECT value, updated_by, updated_at FROM auth_settings WHERE key=$1'
_LOCK = 'SELECT pg_advisory_xact_lock(hashtext($1))'
_UPSERT = (
    'INSERT INTO auth_settings (key, value, updated_by, updated_at) VALUES ($1, $2::jsonb, $3, now()) '
    'ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_by=EXCLUDED.updated_by, '
    'updated_at=EXCLUDED.updated_at RETURNING value, updated_by, updated_at'
)


def _row(record):
    if record is None:
        return None
    value = record['value']
    return {'value': json.loads(value) if isinstance(value, str) else value,
            'updated_by': record['updated_by'], 'updated_at': record['updated_at']}


class Storage:
    """auth_settings 的读写（测试里整体换成内存版）"""

    async def read(self):
        """general_settings 这一行：{'value', 'updated_by', 'updated_at'}；没有返回 None"""
        return _row(await db.pg_fetchrow(_SELECT, SETTING_KEY))

    async def update(self, change, updated_by):
        """在一个事务里加锁读出当前存库的值，change(当前值 dict) 返回新值（None 表示不用写），写回。
        返回 (写回后的行（没写时是原来的行）, 是否写了)。change 抛异常时事务回滚、什么都不写"""
        async with db.get_pool().acquire() as conn:
            async with conn.transaction():
                await conn.execute(_LOCK, f'auth_settings:{SETTING_KEY}')
                row = _row(await conn.fetchrow(_SELECT + ' FOR UPDATE', SETTING_KEY))
                current = dict(row['value']) if row and isinstance(row['value'], dict) else {}
                value = change(current)
                if value is None:
                    return row, False
                saved = await conn.fetchrow(_UPSERT, SETTING_KEY, json.dumps(value, ensure_ascii=False), updated_by)
                return _row(saved), True

    async def setting_exists(self, key):
        return await db.pg_fetchval('SELECT 1 FROM auth_settings WHERE key=$1', key) is not None

    async def insert_absent(self, key, value, updated_by):
        """库里没有这个 key 才写（INSERT ... ON CONFLICT DO NOTHING），返回是否写入"""
        row = await db.pg_fetchrow(
            'INSERT INTO auth_settings (key, value, updated_by) VALUES ($1, $2::jsonb, $3) '
            'ON CONFLICT (key) DO NOTHING RETURNING key',
            key, json.dumps(value, ensure_ascii=False), updated_by)
        return row is not None


storage = Storage()
# 同一进程里的写入排队（页面保存、启动时导入）；跨进程（CLI）靠事务里的 advisory 锁
write_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# 快照
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Snapshot:
    """生效值（明文，含地址里的密码）、库里显式存了的 key、有问题的 key（说明）"""
    values: MappingProxyType = field(default_factory=lambda: MappingProxyType(dict(fields.DEFAULTS)))
    stored: frozenset = frozenset()
    problems: MappingProxyType = field(default_factory=lambda: MappingProxyType({}))
    loaded: bool = False


_snapshot = Snapshot()
_summary = None  # 上次记日志的概况，变了才再记
_refresh_failed = False
_warned = set()  # (key, 存库的值):有问题的值每份只提示一次，免得每 10 秒刷一条日志


def plain(key, stored):
    """加密地址存库的值 → 明文；没有、格式不对或解不开返回空串"""
    if not isinstance(stored, str) or not stored:
        return ''
    try:
        return decrypt_secret(stored)
    except Exception:
        return ''


def decode(key, stored):
    """存库的值 → (生效值, 问题说明或 None)。有问题时：加密地址的生效值是空串（未配置），其他项用默认值"""
    spec = fields.FIELDS[key]
    if spec.secret:
        if not isinstance(stored, str):
            return '', f'已保存的{spec.label}格式不正确，请重新填写'
        try:
            text = decrypt_secret(stored)
        except Exception:
            return '', UNDECRYPTABLE
        if not text:
            return '', None
        try:
            return fields.clean(key, text), None
        except Exception as exc:
            return '', f'已保存的值无效：{exc}，请重新填写'
    if fields.is_blank(stored):
        return spec.default, None
    try:
        return fields.clean(key, stored), None
    except Exception as exc:
        return spec.default, f'已保存的值无效：{exc}，当前使用默认值'


def build_snapshot(row):
    """库里的行（或 None）→ 快照；有问题的项各记一次 warning（不记值）"""
    raw = row['value'] if row and isinstance(row.get('value'), dict) else {}
    values, problems, stored = dict(fields.DEFAULTS), {}, set()
    for key in fields.FIELDS:
        if key not in raw:
            continue
        stored.add(key)
        values[key], problem = decode(key, raw[key])
        if problem:
            problems[key] = problem
            mark = (key, json.dumps(raw[key], ensure_ascii=False, sort_keys=True, default=str))
            if mark not in _warned:
                _warned.add(mark)
                logger.warning(f'通用配置「{fields.LABELS[key]}」({key}):{problem}')
    return Snapshot(MappingProxyType(values), frozenset(stored), MappingProxyType(problems), True)


def describe(snapshot):
    """配置概况（日志用，只有 key 名，没有值）"""
    if not snapshot.stored:
        return '全部使用默认值'
    keys = [key for key in fields.FIELDS if key in snapshot.stored]
    text = f'库里设置了 {len(keys)} 项({"、".join(keys)})'
    if snapshot.problems:
        text += f',其中 {len(snapshot.problems)} 项有问题({"、".join(snapshot.problems)})'
    return text


def apply(row):
    """用库里的行替换快照；保存后立即调用，不等下一次刷新"""
    global _snapshot, _summary
    snapshot = build_snapshot(row)
    _snapshot = snapshot
    text = describe(snapshot)
    if text != _summary:
        _summary = text
        logger.info(f'通用配置已加载:{text}')
    return snapshot


def current():
    return _snapshot


def is_loaded():
    return _snapshot.loaded


def value(name):
    """第 2 节配置项的生效值（库里的或默认值）。同步、线程安全，只读内存快照。
    三个地址返回带账号密码的完整地址（已解密），没配置为 ''；整数项 int、数字项 float、布尔项 bool，其余 str。
    未知 name 抛 KeyError"""
    if name not in fields.FIELDS:
        raise KeyError(name)
    return _snapshot.values[name]


async def refresh():
    """从库里重新加载快照。读库失败时保留原来的快照（只在第一次失败时记 warning），返回是否成功"""
    global _refresh_failed
    try:
        row = await storage.read()
    except Exception as exc:
        if not _refresh_failed:
            logger.warning(f'读取通用配置失败,继续使用上次加载的配置:{type(exc).__name__}: {exc}')
        _refresh_failed = True
        return False
    if _refresh_failed:
        logger.info('读取通用配置已恢复')
        _refresh_failed = False
    apply(row)
    return True


async def refresh_loop(interval=REFRESH_INTERVAL):
    """后台定时刷新（直接改库、CLI 迁移、以后多副本都能跟上）；由 api.on_startup 启动"""
    while True:
        await asyncio.sleep(interval)
        try:
            await refresh()
        except Exception as exc:  # refresh 自己不抛，这里只是兜底：后台任务不能因为一次异常就退出
            logger.warning(f'刷新通用配置异常:{type(exc).__name__}: {exc}')


# ---------------------------------------------------------------------------
# 保存
# ---------------------------------------------------------------------------
def merge(current, changes):
    """把页面提交的修改合并进存库的值，返回 (新值, 改了的 key)。不合法抛 ValueError（说明是哪一项）。

    - 值为 null 或空串：删掉这一项（恢复默认 / 未配置）
    - 规范化后等于默认值：同样删掉（显示为「默认」，以后默认值随版本更新）
    - 三个地址里密码是 ******：沿用已保存地址的密码（见 fields.clean）；明文没变时保留原密文
    - 没提交的 key、库里认不出的 key 原样保留
    """
    new = dict(current)
    changed = set()
    for key, raw in changes.items():
        spec = fields.FIELDS[key]
        before = current.get(key, _MISSING)
        previous = plain(key, before) if spec.secret else None
        item = None if fields.is_blank(raw) else fields.clean(key, raw, previous=previous)
        if item is not None and item == spec.default and type(item) is type(spec.default):
            item = None
        if item is None:
            if key in new:
                del new[key]
                changed.add(key)
            continue
        if spec.secret:
            if before is not _MISSING and previous and previous == item:
                continue  # 明文没变:保留原密文(重新加密每次都不一样)
            new[key] = encrypt_secret(item)
        else:
            if before is not _MISSING and type(before) is type(item) and before == item:
                continue
            new[key] = item
        changed.add(key)
    return new, [key for key in fields.FIELDS if key in changed]


async def save(changes, updated_by):
    """校验页面提交的修改并写库（加锁读出整行 → 合并 → 写回），写完立即替换快照。返回 (保存后的行, 改了的 key)。
    不合法抛 ValueError（这时不写库）；数据库出错原样抛出。changes 里的 key 必须都在 FIELDS 里（调用方先检查）"""
    result = {}

    def change(stored):
        result['value'], result['changed'] = merge(stored, changes)
        return result['value']

    async with write_lock:
        row, _ = await storage.update(change, updated_by)
    apply(row)
    return row, result['changed']
