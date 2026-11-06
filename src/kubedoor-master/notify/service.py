"""消息通知的配置缓存与发送。

配置存 auth_settings(key=notification),token 用 AI_ENCRYPTION_KEY 加密,只有 master 能解密:
    {"default": {"type": "wecom", "token": "<密文>", "at": "zhangsan"},
     "clusters": {"prod-k8s": {"type": "dingding", "token": "<密文>", "at": ""}}}
default 可以不存在(没有全局配置);clusters 里每个集群的配置完整独立,没有条目的集群用全局配置。

- 内存缓存:启动时从库加载,后台每 10 秒刷新一次,保存后立即刷新(api.py 调 apply)。缓存是整体替换的只读快照,
  里面是解密后的 token,只在本进程内存里;任何线程读都安全
- resolve(env):先看该集群的单独配置,再看全局配置,都没有返回 None
- notify(content, env):不等结果,放进该机器人的发送队列后立即返回,事件循环线程和普通线程里都能直接调用;
  失败只记 warning(带 IM 返回的错误码),不抛异常。日志里不出现 token
- 按机器人(同一个 type + token)排队、顺序发送、限速:群机器人大约每分钟只收 20 条(agent 断线重连后会一次补发
  很多条),每个机器人每 RATE_WINDOW 秒最多发 RATE_LIMIT 条,最多积压 MAX_BACKLOG 条,超出丢最旧的并记 warning。
  调度在单独的线程里,发送(requests,超时 10 秒)在小线程池里,不同机器人互不影响
"""

import asyncio
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from types import MappingProxyType

import requests
from loguru import logger

from auth.crypto import decrypt_secret
from auth.store import store

from . import channels

SETTING_KEY = 'notification'
DEFAULT_SCOPE = '全局配置'
REFRESH_INTERVAL = 10  # 秒
SEND_TIMEOUT = 10  # 秒
SEND_WORKERS = 4
# 按机器人限速:企微、钉钉群机器人每分钟 20 条,超出会被拒;飞书、Slack 先用同一个值
RATE_LIMIT = 20
RATE_WINDOW = 60  # 秒
MAX_BACKLOG = 300  # 每个机器人最多积压这么多条,超出丢最旧的
DROP_WARN_INTERVAL = 60  # 丢弃的提示最多每分钟一条(期间丢的条数合并到下一条提示里)

_executor = ThreadPoolExecutor(max_workers=SEND_WORKERS, thread_name_prefix='notify')


@dataclass(frozen=True)
class Snapshot:
    """缓存的配置(只读,整体替换)。通道是 {'type', 'token'(明文), 'at'} 的只读映射"""
    default: object = None
    clusters: MappingProxyType = field(default_factory=lambda: MappingProxyType({}))
    loaded: bool = False


_snapshot = Snapshot()
_summary = None  # 上次记日志的配置概况,变了才再记
_refresh_failed = False
_undecryptable = set()  # (范围, 密文):解不开的 token 每份只提示一次,免得每 10 秒刷一条日志


# ---------------------------------------------------------------------------
# 缓存
# ---------------------------------------------------------------------------
def _channel(entry, scope):
    """存库格式 → 发送用的通道(token 解密);格式不对、没有 token 或解不开返回 None"""
    if not isinstance(entry, dict) or entry.get('type') not in channels.TYPES:
        return None
    stored = entry.get('token') or ''
    try:
        token = decrypt_secret(stored)
    except Exception as exc:
        key = (scope, stored)
        if key not in _undecryptable:
            _undecryptable.add(key)
            logger.warning(f'消息通知({scope})的 token 无法解密({exc}),'
                           '请系统管理员在「系统管理 → 消息通知」重新填写机器人地址')
        return None
    if not token:
        return None
    return MappingProxyType({'type': entry['type'], 'token': token, 'at': str(entry.get('at') or '')})


def build_snapshot(value):
    """存库格式的配置 → 缓存快照"""
    value = value if isinstance(value, dict) else {}
    default = _channel(value.get('default'), DEFAULT_SCOPE)
    raw = value.get('clusters')
    clusters = {}
    for env, entry in (raw.items() if isinstance(raw, dict) else ()):
        channel = _channel(entry, f'集群 {env}')
        if channel is not None:
            clusters[str(env)] = channel
    return Snapshot(default, MappingProxyType(clusters), True)


def describe(snapshot):
    """配置概况(日志用,不含 token)"""
    default = snapshot.default
    head = f'全局配置 {channels.TYPE_LABELS[default["type"]]}' if default is not None else '没有全局配置'
    return f'{head},单独配置的集群 {len(snapshot.clusters)} 个'


def apply(value):
    """用存库格式的配置替换缓存;保存后立即调用,不等下一次刷新"""
    global _snapshot, _summary
    snapshot = build_snapshot(value)
    _snapshot = snapshot
    text = describe(snapshot)
    if text != _summary:
        _summary = text
        logger.info(f'消息通知配置已加载:{text}')
    return snapshot


def is_loaded():
    return _snapshot.loaded


async def refresh():
    """从库里重新加载配置。读库失败时保留原来的缓存(只在第一次失败时记 warning),返回是否成功"""
    global _refresh_failed
    try:
        value = await store.get_setting(SETTING_KEY, {})
    except Exception as exc:
        if not _refresh_failed:
            logger.warning(f'读取消息通知配置失败,继续使用上次加载的配置:{type(exc).__name__}: {exc}')
        _refresh_failed = True
        return False
    if _refresh_failed:
        logger.info('读取消息通知配置已恢复')
        _refresh_failed = False
    apply(value)
    return True


async def refresh_loop(interval=REFRESH_INTERVAL):
    """后台定时刷新(直接改库、或者以后多副本时也能跟上);由 api.on_startup 启动"""
    while True:
        await asyncio.sleep(interval)
        try:
            await refresh()
        except Exception as exc:  # refresh 自己不抛,这里只是兜底:后台任务不能因为一次异常就退出
            logger.warning(f'刷新消息通知配置异常:{type(exc).__name__}: {exc}')


# ---------------------------------------------------------------------------
# 选通道
# ---------------------------------------------------------------------------
def _pick(env):
    """返回 (范围说明, 通道);都没有配置时通道为 None"""
    snapshot = _snapshot
    env = env.strip() if isinstance(env, str) else ''
    if env:
        channel = snapshot.clusters.get(env)
        if channel is not None:
            return f'集群 {env}', channel
    if snapshot.default is not None:
        return DEFAULT_SCOPE, snapshot.default
    return '', None


def resolve(env=None):
    """按集群选通道:先看该集群的单独配置,再看全局配置;都没有返回 None。
    只读内存缓存,同步函数,线程里调用也安全。返回 {'type', 'token'(明文), 'at'} 的副本"""
    _, channel = _pick(env)
    return dict(channel) if channel is not None else None


# ---------------------------------------------------------------------------
# 发送一条
# ---------------------------------------------------------------------------
def redact(text, token):
    """去掉文本里的 token(requests 的异常信息会带上完整的机器人地址),再截到 300 字符"""
    text = str(text)
    if token:
        text = text.replace(token, '***')
        for part in token.split('/'):  # Slack 的 token 是 T…/B…/… 三段
            if len(part) >= 6:
                text = text.replace(part, '***')
    limit = channels.MAX_MESSAGE
    return text if len(text) <= limit else text[:limit - 1] + '…'


def deliver(channel, content):
    """同步发送一条消息,返回 (ok, message)。不抛异常;message 已经去掉 token,可以写日志、返回给页面。
    会阻塞最多 SEND_TIMEOUT 秒:事件循环里要放进线程调用(asyncio.to_thread)。"""
    token = str(channel.get('token') or '')
    kind = channel.get('type')
    try:
        url, payload = channels.build_request(kind, token, content, channel.get('at') or '')
        response = requests.post(url, json=payload, timeout=SEND_TIMEOUT, allow_redirects=False)
        ok, message = channels.parse_response(kind, response.status_code, response.text)
    except requests.Timeout:
        ok, message = False, f'请求超时({SEND_TIMEOUT} 秒内没有返回)'
    except requests.RequestException as exc:
        ok, message = False, f'无法访问 IM 接口:{type(exc).__name__}: {exc}'
    except Exception as exc:
        ok, message = False, f'发送失败:{type(exc).__name__}: {exc}'
    return ok, redact(message, token)


def _send(channel, text, scope):
    """线程池里执行:发送并记日志。成功记 info,失败记 warning(带 IM 返回的错误码,不带 token)"""
    ok, message = deliver(channel, text)
    label = channels.TYPE_LABELS.get(channel.get('type'), channel.get('type'))
    if ok:
        logger.info(f'消息通知已发送({scope},{label})')
    else:
        logger.warning(f'消息通知发送失败({scope},{label}):{message}')


# ---------------------------------------------------------------------------
# 按机器人排队、顺序发送、限速
# ---------------------------------------------------------------------------
class _Robot:
    """一个机器人(type + token)的发送队列和最近一个窗口里的发送记录"""
    __slots__ = ('label', 'items', 'sent', 'busy', 'dropped', 'drop_scope', 'drop_warned_at')

    def __init__(self, label):
        self.label = label
        self.items = deque()  # (通道, 正文, 范围说明)
        self.sent = deque()  # 每次发送结束的时间:按结束时间算窗口,对方收到的时间一定不晚于它
        self.busy = False  # 同一个机器人同时只发一条,保证顺序
        self.dropped = 0  # 还没提示过的丢弃条数
        self.drop_scope = ''
        self.drop_warned_at = None

    def ready_at(self, now, limit, window):
        """下一条最早什么时候能发(滑动窗口:任意 window 秒内最多 limit 条)"""
        while self.sent and self.sent[0] <= now - window:
            self.sent.popleft()
        if len(self.sent) < limit:
            return now
        return self.sent[-limit] + window


class Dispatcher:
    """按机器人排队、顺序发送、限速。

    notify 只把消息放进队列(拿一下锁,不做 I/O);调度线程按限速把每个机器人的下一条交给发送线程池,
    同一个机器人同时只有一条在发,不同机器人互不影响。clock 可以换成假时钟测试(改了时间调 wake)。"""

    def __init__(self, executor, rate_limit=None, window=None, backlog=None, clock=time.monotonic):
        self.executor = executor
        self.rate_limit = max(int(RATE_LIMIT if rate_limit is None else rate_limit), 1)
        self.window = float(RATE_WINDOW if window is None else window)
        self.backlog = max(int(MAX_BACKLOG if backlog is None else backlog), 1)
        self.clock = clock
        self._cond = threading.Condition()
        self._robots = {}  # (type, token) -> _Robot
        self._thread = None
        self._stopped = False

    def submit(self, channel, text, scope):
        """放进这个机器人的队列;积压满了丢最旧的一条。不阻塞(只拿一下锁)"""
        key = (channel['type'], channel['token'])
        warning = ''
        with self._cond:
            robot = self._robots.get(key)
            if robot is None:
                robot = self._robots[key] = _Robot(channels.TYPE_LABELS.get(channel['type'], channel['type']))
            if len(robot.items) >= self.backlog:
                dropped_scope = robot.items.popleft()[2]
                warning = self._note_drop(robot, dropped_scope, self.clock())
            robot.items.append((channel, text, scope))
            self._start()
            self._cond.notify_all()
        if warning:
            logger.warning(warning)

    def _note_drop(self, robot, scope, now):
        """记一条丢弃;到了提示间隔返回提示文字(连同期间丢的条数),否则返回空串"""
        robot.dropped += 1
        robot.drop_scope = scope
        if robot.drop_warned_at is not None and now - robot.drop_warned_at < DROP_WARN_INTERVAL:
            return ''
        robot.drop_warned_at = now
        return self._drop_text(robot)

    def _drop_text(self, robot):
        count, robot.dropped = robot.dropped, 0
        return (f'消息通知积压太多:{robot.label}机器人待发的通知超过 {self.backlog} 条'
                f'(限速每 {self.window:g} 秒 {self.rate_limit} 条),丢弃了最早的 {count} 条'
                f'(最近丢弃的一条是{robot.drop_scope}的)')

    def _start(self):
        if self._stopped or (self._thread is not None and self._thread.is_alive()):
            return
        self._thread = threading.Thread(target=self._run, name='notify-dispatcher', daemon=True)
        self._thread.start()

    def _run(self):
        while True:
            warnings = []
            with self._cond:
                if self._stopped:
                    return
                timeout = self._dispatch(warnings)
                if not warnings:
                    self._cond.wait(timeout)
            for text in warnings:  # 日志在锁外面写
                logger.warning(text)

    def _dispatch(self, warnings):
        """(持有锁)把能发的交给线程池;返回下一次要醒来的秒数,None 表示等新消息"""
        now = self.clock()
        timeout = None
        for key, robot in list(self._robots.items()):
            if robot.busy:
                continue
            if not robot.items:
                if robot.dropped:  # 期间丢了但还没提示过的,队列发空时补一条提示
                    warnings.append(self._drop_text(robot))
                robot.ready_at(now, self.rate_limit, self.window)  # 清理过期的发送记录
                if not robot.sent:
                    del self._robots[key]  # 空闲的机器人不留在内存里
                continue
            at = robot.ready_at(now, self.rate_limit, self.window)
            if at > now:
                timeout = at - now if timeout is None else min(timeout, at - now)
                continue
            item = robot.items.popleft()
            robot.busy = True
            try:
                self.executor.submit(self._send_one, robot, item)
            except Exception as exc:  # 线程池已经关闭(进程退出时):这个机器人的消息都发不出去了
                robot.busy = False
                count = len(robot.items) + 1
                robot.items.clear()
                warnings.append(f'提交消息通知失败,丢弃{robot.label}机器人待发的 {count} 条:{type(exc).__name__}: {exc}')
        return timeout

    def _send_one(self, robot, item):
        channel, text, scope = item
        try:
            _send(channel, text, scope)
        except Exception as exc:  # _send 自己不抛,兜底
            logger.warning(f'消息通知发送异常({scope},{robot.label}):{type(exc).__name__}')
        finally:
            with self._cond:
                robot.sent.append(self.clock())
                robot.busy = False
                self._cond.notify_all()

    # ---- 测试和排查用 ----
    def wake(self):
        """让调度线程马上重新检查(测试里改了假时钟之后调用)"""
        with self._cond:
            self._cond.notify_all()

    def pending(self):
        """还没发完的条数(排队的 + 正在发的)"""
        with self._cond:
            return sum(len(robot.items) + int(robot.busy) for robot in self._robots.values())

    def wait_idle(self, timeout=5):
        """等所有队列发完;超时返回 False"""
        deadline = time.monotonic() + timeout
        with self._cond:
            while any(robot.items or robot.busy for robot in self._robots.values()):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)
        return True

    def stop(self, timeout=5):
        """停掉调度线程(没发的不再发);测试用"""
        with self._cond:
            self._stopped = True
            self._cond.notify_all()
            thread = self._thread
        if thread is not None:
            thread.join(timeout)


_dispatcher = Dispatcher(_executor)


# ---------------------------------------------------------------------------
# 发通知
# ---------------------------------------------------------------------------
def _preview(text, limit=80):
    flat = ' '.join(text.split())
    return flat if len(flat) <= limit else flat[:limit] + '…'


def notify(content, env=None):
    """发一条通知:按 resolve(env) 选通道,放进该机器人的发送队列后立即返回,不等结果。
    事件循环线程和普通线程里都能直接调用;返回是否已放进队列。任何失败只记日志,不抛异常。"""
    try:
        text = '' if content is None else str(content)
        if not text.strip():
            return False
        scope, channel = _pick(env)
        if channel is None:
            where = f'集群 {env}' if isinstance(env, str) and env.strip() else '没有指定集群'
            logger.info(f'没有配置消息通知({where}),不发送:{_preview(text)}')
            return False
        _dispatcher.submit(channel, text, scope)
        return True
    except Exception as exc:
        logger.warning(f'提交消息通知失败:{type(exc).__name__}: {exc}')
        return False
