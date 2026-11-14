"""「系统管理 → 事件告警规则」的接口、旧规则文件的导入,以及启停。

- GET  /api/admin/event-alert-rules:当前保存的规则;库里没有时是内置默认规则(source=default,不写库)
- PUT  /api/admin/event-alert-rules:严格校验(rules_schema)后保存,马上生效(替换匹配器读的快照),
  记审计 event_rules.update(只记规则条数和名称)
- GET  /api/admin/event-alert-rules/default:内置默认规则(页面「恢复默认」把它载入编辑区,保存后才生效)
- POST /api/admin/event-alert-rules/preview:试运行,用最近 N 小时 k8s_events 里的真实事件跑一遍请求里的规则
  (限量,匹配在线程里跑),返回总数、被忽略数、每条规则命中数和例子;不发通知、不改事件

/api/admin/.+ 已在 auth/policy.py 统一要求系统管理员;handler 里再查一次(纵深防御)。

import_legacy_file:把以前挂载的规则文件(ConfigMap kubedoor-master-file-cfg)原样导入数据库,
master 启动时(on_startup)和 `python3 -m general_settings migrate` 都会调用,幂等。
库里一旦有了这一行就不会再导入:接口没有删除这一行的操作,页面的「恢复默认」是把内置默认规则载入编辑区、
保存后写进库里(不删行)。所以升级后旧规则文件还挂着(迁移没清理)时,master 重启也不会把它再导回来。
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone

from aiohttp import web
from loguru import logger

import db
from auth import audit, client_ip, principal_of

from . import rules_schema, rules_store
from .alert_rule_matcher import first_match, rule_enabled, rule_matches
from .event_processor import get_event_processor

AUDIT_UPDATE = 'event_rules.update'
LEGACY_FILE = '/k8s_event/rules/alert_rules.json'
LEGACY_AUTHOR = 'legacy-import'
LEGACY_MAX_BYTES = 2 * 1024 * 1024
PREVIEW_DEFAULT_HOURS = 24
PREVIEW_MAX_HOURS = 168  # 试运行最多看最近 7 天
PREVIEW_LIMIT = 10000  # 最多取多少条事件(最近的优先)
PREVIEW_EXAMPLES = 5  # 每条规则最多举几个例子
PREVIEW_MESSAGE = 300  # 例子里的消息最长多少字符
PREVIEW_TIMEOUT = 30  # 秒:查事件的语句超时
MAX_ENV = 200
MAX_ISSUES = 200  # 422 响应里最多列多少处问题
BEIJING = timezone(timedelta(hours=8))
REFRESH_TASK = web.AppKey('event_rules_refresh_task', asyncio.Task)

_writes = asyncio.Lock()  # 保存串行化:写库和替换快照的顺序一致

_PREVIEW_SQL = (
    'SELECT eventuid, eventstatus, level, count, kind, k8s, namespace, name, reason, message, '
    'firsttimestamp, lasttimestamp, reportingcomponent, reportinginstance '
    "FROM k8s_events WHERE lasttimestamp >= now() - $1::int * interval '1 hour' {env}"
    'ORDER BY lasttimestamp DESC LIMIT {limit}'
)


def _fail(status, message, code='bad_request', **extra):
    return web.json_response({'success': False, 'code': code, 'message': message, 'error': message, **extra},
                             status=status)


def _invalid(exc):
    return _fail(422, str(exc), 'invalid_rules', errors=exc.issues[:MAX_ISSUES])


def _forbidden(request):
    if principal_of(request).is_admin:
        return None
    return _fail(403, '只有系统管理员可以管理事件告警规则', 'forbidden')


async def _read_body(request):
    """请求体必须是 JSON 对象,否则返回 None"""
    try:
        body = await request.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


def _iso(value):
    if isinstance(value, datetime):
        return (value.astimezone() if value.tzinfo else value).isoformat()
    return value


def _payload(row):
    if row is None:
        return {'rules': rules_store.default_rules(), 'source': rules_store.SOURCE_DEFAULT,
                'updated_by': None, 'updated_at': None}
    return {'rules': row['value'], 'source': rules_store.SOURCE_DATABASE,
            'updated_by': row['updated_by'] or None, 'updated_at': _iso(row['updated_at'])}


# ---------------------------------------------------------------------------
# 管理接口(系统管理员)
# ---------------------------------------------------------------------------
async def get_rules(request):
    denied = _forbidden(request)
    if denied is not None:
        return denied
    try:
        row = await rules_store.load_row()
    except Exception as exc:
        logger.error(f'读取事件告警规则失败: {exc}')
        return _fail(503, '读取事件告警规则失败，请稍后重试', 'db_unavailable')
    return web.json_response({'success': True, 'data': _payload(row)})


async def get_default(request):
    denied = _forbidden(request)
    if denied is not None:
        return denied
    return web.json_response({'success': True, 'data': {'rules': rules_store.default_rules()}})


async def put_rules(request):
    denied = _forbidden(request)
    if denied is not None:
        return denied
    principal = principal_of(request)
    body = await _read_body(request)
    if body is None:
        return _fail(400, '请求体必须是 JSON 对象')
    try:
        rules = rules_schema.validate(body.get('rules'))
    except rules_schema.RulesInvalid as exc:
        return _invalid(exc)
    async with _writes:
        try:
            saved = await rules_store.save_row(rules, principal.username)
        except Exception as exc:
            logger.error(f'保存事件告警规则失败: {exc}')
            return _fail(503, '保存事件告警规则失败，请稍后重试', 'db_unavailable')
        rules_store.apply(saved['value'], rules_store.SOURCE_DATABASE)  # 保存后立即生效,不等后台刷新
    audit.record_bg(principal.username, AUDIT_UPDATE, ip=client_ip(request), detail=rules_schema.summary(rules))
    return web.json_response({'success': True, 'data': _payload(saved)})


# ---------------------------------------------------------------------------
# 试运行
# ---------------------------------------------------------------------------
def _beijing(value):
    """库里的 timestamptz → 北京时间(不带时区),和实时处理时(event_processor._parse_timestamp)一样"""
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(BEIJING).replace(tzinfo=None)
    return value


def _clock(value):
    return value.strftime('%Y-%m-%d %H:%M:%S') if isinstance(value, datetime) else str(value or '')


def _event_from_row(row):
    """库里的一行 → 实时处理时交给匹配器的事件。库里只有事件的最新状态:
    - DELETED(K8S 清理了这个事件对象)按删除前的状态算:次数大于 1 当成 MODIFIED,否则 ADDED
    - 级别被告警流程改成了「已告警」的,原来的级别没有记录,按 Warning 算
    返回 (事件, 是否改了状态, 是否改了级别)"""
    count = int(row['count'] or 0)
    status = row['eventstatus'] or ''
    deleted = status == 'DELETED'
    if deleted:
        status = 'MODIFIED' if count > 1 else 'ADDED'
    level = row['level'] or ''
    alerted = level == '已告警'
    if alerted:
        level = 'Warning'
    event = {
        'eventUid': row['eventuid'] or '',
        'eventStatus': status,
        'level': level,
        'count': count,
        'kind': row['kind'] or '',
        'k8s': row['k8s'] or '',
        'namespace': row['namespace'] or '',
        'name': row['name'] or '',
        'reason': row['reason'] or '',
        'message': row['message'] or '',
        'firstTimestamp': _beijing(row['firsttimestamp']),
        'lastTimestamp': _beijing(row['lasttimestamp']),
        'reportingComponent': row['reportingcomponent'] or '',
        'reportingInstance': row['reportinginstance'] or '',
    }
    return event, deleted, alerted


def _example(event, row):
    """给页面看的例子:级别、状态是库里的原值"""
    message = event['message']
    if len(message) > PREVIEW_MESSAGE:
        message = message[:PREVIEW_MESSAGE] + '…'
    return {'k8s': event['k8s'], 'namespace': event['namespace'], 'kind': event['kind'], 'name': event['name'],
            'reason': event['reason'], 'message': message, 'count': event['count'],
            'level': row['level'] or '', 'eventStatus': row['eventstatus'] or '',
            'firstTimestamp': _clock(event['firstTimestamp']), 'lastTimestamp': _clock(event['lastTimestamp'])}


def _rule_stat(index, rule, alert):
    stat = {'index': index, 'name': str(rule.get('name', '')), 'enabled': rule_enabled(rule), 'hits': 0,
            'examples': []}
    if alert:
        stat.update(severity=rule.get('severity', rules_schema.DEFAULT_SEVERITY), shadowed=0)
    return stat


def run_preview(ruleset, rows, limit=PREVIEW_LIMIT, examples=PREVIEW_EXAMPLES):
    """用规则跑一遍事件(按 lasttimestamp 倒序的库里的行,最多多取一条用来判断是否超过上限)。
    与实时处理同一套匹配函数:忽略规则按顺序第一条命中的算它忽略的;告警规则第一条命中的算命中,
    后面也能命中的记为 shadowed(被前面的规则先命中)。停用的规则不参与。纯函数,在线程里调用"""
    truncated = len(rows) > limit
    rows = rows[:limit]
    ignore_stats = [_rule_stat(index, rule, False) for index, rule in enumerate(ruleset.ignore_rules)]
    alert_stats = [_rule_stat(index, rule, True) for index, rule in enumerate(ruleset.alert_rules)]
    ignored = matched = deleted = alerted = 0
    for row in rows:
        event, was_deleted, was_alerted = _event_from_row(row)
        deleted += was_deleted
        alerted += was_alerted
        index = first_match(ruleset.ignore_rules, event)
        if index is not None:
            ignored += 1
            stat = ignore_stats[index]
            stat['hits'] += 1
            if len(stat['examples']) < examples:
                stat['examples'].append(_example(event, row))
            continue
        first = None
        for index, rule in enumerate(ruleset.alert_rules):
            if not rule_matches(rule, event):
                continue
            stat = alert_stats[index]
            if first is not None:
                stat['shadowed'] += 1
                continue
            first = index
            stat['hits'] += 1
            if len(stat['examples']) < examples:
                stat['examples'].append(_example(event, row))
        if first is not None:
            matched += 1
    return {'limit': limit, 'total': len(rows), 'truncated': truncated, 'ignored': ignored, 'matched': matched,
            'unmatched': len(rows) - ignored - matched, 'deleted': deleted, 'alerted_level': alerted,
            'ignore_rules': ignore_stats, 'alert_rules': alert_stats}


def _preview_options(body):
    """试运行的时间范围和集群;不合法抛 ValueError"""
    hours = body.get('hours', PREVIEW_DEFAULT_HOURS)
    if isinstance(hours, bool) or not isinstance(hours, int) or not 1 <= hours <= PREVIEW_MAX_HOURS:
        raise ValueError(f'试运行的时间范围只能是 1 到 {PREVIEW_MAX_HOURS} 小时')
    env = body.get('env')
    if env is None:
        env = ''
    if not isinstance(env, str) or len(env.strip()) > MAX_ENV:
        raise ValueError('集群名称不正确')
    return hours, env.strip()


async def preview(request):
    denied = _forbidden(request)
    if denied is not None:
        return denied
    body = await _read_body(request)
    if body is None:
        return _fail(400, '请求体必须是 JSON 对象')
    try:
        hours, env = _preview_options(body)
    except ValueError as exc:
        return _fail(422, str(exc), 'invalid_request')
    try:
        rules = rules_schema.validate(body.get('rules'))
    except rules_schema.RulesInvalid as exc:
        return _invalid(exc)
    ruleset = rules_store.build(rules, 'preview')
    args = [hours]
    where = ''
    if env:
        args.append(env)
        where = 'AND k8s = $2 '
    sql = _PREVIEW_SQL.format(env=where, limit=PREVIEW_LIMIT + 1)
    try:
        rows = await db.pg_fetch(sql, *args, timeout=PREVIEW_TIMEOUT)
    except Exception as exc:
        logger.error(f'试运行事件告警规则时读取事件失败: {exc}')
        return _fail(503, '读取事件失败，请稍后重试（时间范围越大越慢，可以先缩小范围）', 'db_unavailable')
    result = await asyncio.to_thread(run_preview, ruleset, list(rows))
    return web.json_response({'success': True, 'data': {'hours': hours, 'env': env, **result}})


# ---------------------------------------------------------------------------
# 旧规则文件导入
# ---------------------------------------------------------------------------
def _reject_constant(name):
    raise ValueError(f'JSON 里不能有 {name}')


def _read_legacy_file(path):
    """读旧规则文件:返回 ('ok', 规则) / ('absent', None) / ('invalid', None)。阻塞 I/O,在线程里调用"""
    try:
        with open(path, 'rb') as file:
            data = file.read(LEGACY_MAX_BYTES + 1)
    except FileNotFoundError:
        return 'absent', None
    except OSError:
        return 'invalid', None
    if len(data) > LEGACY_MAX_BYTES:
        return 'invalid', None
    try:
        value = json.loads(data.decode('utf-8-sig'), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return 'invalid', None
    if not rules_schema.legacy_structure_ok(value):
        return 'invalid', None
    return 'ok', value


async def import_legacy_file(path=LEGACY_FILE):
    """把旧的规则文件原样导入数据库(不做严格校验,保持用户原来的规则行为),幂等。返回:
    'present' 库里已有规则(不读文件);'absent' 文件不存在;'invalid' 文件读不了、不是 JSON,
    或结构不对(顶层不是对象 / 两个键存在但不是数组),不导入;'imported' 已导入。
    每次调用都现读文件,没有缓存。读写数据库失败时抛异常"""
    if await rules_store.load_row() is not None:
        return 'present'
    status, value = await asyncio.to_thread(_read_legacy_file, path)
    if status == 'absent':
        return 'absent'
    if status == 'invalid':
        logger.warning(f'旧的事件告警规则文件 {path} 不是有效的规则 JSON,没有导入(现在用的是内置默认规则),'
                       '请在「系统管理 → 事件告警规则」重新配置')
        return 'invalid'
    if not await rules_store.insert_absent(value, LEGACY_AUTHOR):
        return 'present'
    logger.info(f'已把旧的事件告警规则文件 {path} 导入数据库,以后在「系统管理 → 事件告警规则」修改')
    return 'imported'


# ---------------------------------------------------------------------------
# 注册与启停
# ---------------------------------------------------------------------------
def register_routes(app):
    app.router.add_get('/api/admin/event-alert-rules', get_rules)
    app.router.add_put('/api/admin/event-alert-rules', put_rules)
    app.router.add_get('/api/admin/event-alert-rules/default', get_default)
    app.router.add_post('/api/admin/event-alert-rules/preview', preview)


async def on_startup(app):
    """导入旧规则文件 → 加载规则快照 → 建好事件处理器 → 启动每 10 秒的后台刷新。必须排在建表(init_db_and_schema)之后。
    导入失败、读库失败都不影响启动:先用内置默认规则,后台继续重试"""
    try:
        await import_legacy_file(LEGACY_FILE)
    except Exception as exc:
        logger.error(f'导入旧的事件告警规则文件失败(不影响启动): {type(exc).__name__}: {exc}')
    await rules_store.refresh()
    get_event_processor()
    app[REFRESH_TASK] = asyncio.create_task(rules_store.refresh_loop())


async def on_cleanup(app):
    task = app.get(REFRESH_TASK)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
