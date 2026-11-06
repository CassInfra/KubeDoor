"""消息通知 notify/service.py:配置缓存、resolve 优先级、后台刷新;notify 线程安全、不阻塞、失败不抛异常、
日志不含 token;按机器人排队、顺序发送、限速、积压上限。
另外覆盖 K8S 事件告警、管控表通知改用 notify 之后的调用方式。"""
import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests
from loguru import logger

from auth.crypto import encrypt_secret
from notify import service

WECOM = 'a1b2c3d4-0000-4bc4-97a0-0ec2c3f5a1b2'
WECOM_URL = f'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={WECOM}'
DING = '0123456789abcdef' * 4
DING_URL = f'https://oapi.dingtalk.com/robot/send?access_token={DING}'
SLACK = 'T00000000/B00000000/XXXXXXXXXXXXXXXXXXXXXXXX'
OK_BODY = '{"errcode":0,"errmsg":"ok"}'


@pytest.fixture(autouse=True)
def sender(monkeypatch):
    """每个用例从空缓存开始,用自己的发送线程池和调度器(默认不限速);sender.make(...) 换一个带参数的调度器"""
    monkeypatch.setenv('AI_ENCRYPTION_KEY', '11' * 32)
    monkeypatch.setattr(service, '_snapshot', service.Snapshot())
    monkeypatch.setattr(service, '_summary', None)
    monkeypatch.setattr(service, '_refresh_failed', False)
    monkeypatch.setattr(service, '_undecryptable', set())
    executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='notify-test')
    made = []

    def make(**kwargs):
        kwargs.setdefault('rate_limit', 1000)
        kwargs.setdefault('backlog', 1000)
        dispatcher = service.Dispatcher(executor, **kwargs)
        monkeypatch.setattr(service, '_dispatcher', dispatcher)
        made.append(dispatcher)
        return dispatcher

    state = SimpleNamespace(executor=executor, make=make, dispatcher=make())
    yield state
    for dispatcher in made:
        dispatcher.stop()
    executor.shutdown(wait=True)


@pytest.fixture
def logs():
    lines = []
    handler_id = logger.add(lambda message: lines.append(str(message)), level='DEBUG')
    yield lines
    logger.remove(handler_id)


class FakeIM:
    """替代 requests.post:记下请求,按设定返回或抛异常;gate 可以把某个地址的发送卡住"""

    def __init__(self, status=200, text=OK_BODY, exc=None, gate=None, gate_url=None, delay=0):
        self.status, self.text, self.exc, self.delay = status, text, exc, delay
        self.gate, self.gate_url = gate, gate_url
        self.calls = []
        self.lock = threading.Lock()
        self.active = {}  # 地址 -> 正在发的条数
        self.max_active = {}

    def __call__(self, url, json=None, timeout=None, allow_redirects=True, **kwargs):
        with self.lock:
            self.calls.append({'url': url, 'json': json, 'timeout': timeout, 'allow_redirects': allow_redirects,
                               'thread': threading.current_thread().name})
            self.active[url] = self.active.get(url, 0) + 1
            self.max_active[url] = max(self.max_active.get(url, 0), self.active[url])
        try:
            if self.gate is not None and (self.gate_url is None or url == self.gate_url):
                self.gate.wait(5)
            if self.delay:
                time.sleep(self.delay)
            if self.exc is not None:
                raise self.exc
            return SimpleNamespace(status_code=self.status, text=self.text)
        finally:
            with self.lock:
                self.active[url] -= 1

    def texts(self, url=None):
        with self.lock:
            return [_text(call['json']) for call in self.calls if url is None or call['url'] == url]


def _text(payload):
    return payload.get('text') if 'text' in payload else (payload.get('markdown') or {}).get('content') or \
        (payload.get('markdown') or {}).get('text')


@pytest.fixture
def im(monkeypatch):
    fake = FakeIM()
    monkeypatch.setattr(service.requests, 'post', fake)
    return fake


def stored(kind, token, at=''):
    return {'type': kind, 'token': encrypt_secret(token), 'at': at}


CONFIG = {'default': stored('wecom', WECOM, 'zhangsan'), 'clusters': {'prod': stored('dingding', DING, '13800000000')}}
PLAIN = {'default': stored('wecom', WECOM), 'clusters': {'prod': stored('dingding', DING)}}


def wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, '等待超时'
        time.sleep(0.01)


def stays(predicate, duration=0.2):
    """一段时间内一直成立"""
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        assert predicate()
        time.sleep(0.01)


class FakeStore:
    def __init__(self, value=None, error=None):
        self.value, self.error, self.calls = value, error, 0

    async def get_setting(self, key, default=None):
        assert key == service.SETTING_KEY
        self.calls += 1
        if self.error is not None:
            raise self.error
        return json.loads(json.dumps(self.value)) if self.value is not None else default


# ---------------------------------------------------------------------------
# resolve:先看集群单独配置,再看全局配置,都没有返回 None
# ---------------------------------------------------------------------------
def test_resolve_prefers_cluster_then_default():
    assert service.resolve('prod') is None and not service.is_loaded()
    service.apply(CONFIG)
    assert service.is_loaded()
    assert service.resolve('prod') == {'type': 'dingding', 'token': DING, 'at': '13800000000'}
    assert service.resolve(' prod ') == service.resolve('prod')
    default = {'type': 'wecom', 'token': WECOM, 'at': 'zhangsan'}
    for env in ('test', '', None, 'PROD', 'bad env!', 'x' * 1000):  # 集群名区分大小写;不认识的用全局配置
        assert service.resolve(env) == default
    assert service.resolve() == default
    # 没有全局配置:没单独配置的集群不发送
    service.apply({'clusters': CONFIG['clusters']})
    assert service.resolve('test') is None and service.resolve(None) is None
    assert service.resolve('prod')['type'] == 'dingding'
    service.apply({})
    assert service.resolve('prod') is None


def test_resolve_returns_a_copy():
    service.apply(CONFIG)
    channel = service.resolve('prod')
    channel['token'] = 'changed'
    assert service.resolve('prod')['token'] == DING


def test_unusable_entries_fall_back_and_warn_once(monkeypatch, logs):
    monkeypatch.setenv('AI_ENCRYPTION_KEY', '22' * 32)
    other_key = encrypt_secret('old-token')  # 用另一个密钥加密:解不开
    monkeypatch.setenv('AI_ENCRYPTION_KEY', '11' * 32)
    value = {'default': stored('wecom', WECOM), 'clusters': {
        'broken': {'type': 'wecom', 'token': other_key, 'at': ''},
        'bad-type': {'type': 'qq', 'token': encrypt_secret(DING), 'at': ''},
        'empty': {'type': 'dingding', 'token': '', 'at': ''},
        'garbage': 'x',
    }}
    service.apply(value)
    service.apply(value)  # 第二次刷新不再提示
    for env in ('broken', 'bad-type', 'empty', 'garbage'):
        assert service.resolve(env)['token'] == WECOM
    warnings = [line for line in logs if '无法解密' in line]
    assert len(warnings) == 1 and '集群 broken' in warnings[0] and 'old-token' not in warnings[0]


@pytest.mark.parametrize('value', [None, 'x', [], {'default': 'x', 'clusters': ['prod']}])
def test_garbage_config_means_nothing_configured(value):
    snapshot = service.apply(value)
    assert snapshot.loaded and snapshot.default is None and dict(snapshot.clusters) == {}


def test_summary_logged_only_when_changed(logs):
    service.apply(CONFIG)
    service.apply(CONFIG)
    service.apply({})
    loaded = [line for line in logs if '消息通知配置已加载' in line]
    assert len(loaded) == 2
    assert '全局配置 企业微信,单独配置的集群 1 个' in loaded[0] and '没有全局配置,单独配置的集群 0 个' in loaded[1]
    assert not any(WECOM in line or DING in line for line in logs)


# ---------------------------------------------------------------------------
# 缓存刷新
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_refresh_loads_store_and_keeps_cache_when_db_fails(monkeypatch, logs):
    store = FakeStore(CONFIG)
    monkeypatch.setattr(service, 'store', store)
    assert await service.refresh() is True
    assert service.resolve('prod')['token'] == DING
    store.error = RuntimeError('db down')
    assert await service.refresh() is False
    assert await service.refresh() is False
    assert service.resolve('prod')['token'] == DING  # 读库失败沿用上次的
    assert len([line for line in logs if '读取消息通知配置失败' in line]) == 1  # 只在第一次失败时提示
    store.error, store.value = None, {'default': stored('slack', SLACK)}
    assert await service.refresh() is True
    assert any('读取消息通知配置已恢复' in line for line in logs)
    assert service.resolve('prod') == {'type': 'slack', 'token': SLACK, 'at': ''}


@pytest.mark.asyncio
async def test_refresh_without_row_means_not_configured(monkeypatch):
    monkeypatch.setattr(service, 'store', FakeStore(None))
    assert await service.refresh() is True
    assert service.is_loaded() and service.resolve('prod') is None


@pytest.mark.asyncio
async def test_refresh_loop_picks_up_changes(monkeypatch):
    store = FakeStore({})
    monkeypatch.setattr(service, 'store', store)
    task = asyncio.create_task(service.refresh_loop(0.01))
    try:
        await asyncio.sleep(0.05)
        assert service.resolve('prod') is None
        store.value = CONFIG  # 直接改库:下一轮刷新就生效
        deadline = time.monotonic() + 2
        while service.resolve('prod') is None:
            assert time.monotonic() < deadline, '没有刷新'
            await asyncio.sleep(0.01)
        # 一次异常不会让后台任务退出
        monkeypatch.setattr(service, 'refresh', Mock(side_effect=RuntimeError('boom')))
        await asyncio.sleep(0.05)
        assert not task.done()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# ---------------------------------------------------------------------------
# notify:非阻塞、线程安全、失败只记日志
# ---------------------------------------------------------------------------
def test_notify_sends_in_background_thread(sender, im, logs):
    service.apply(CONFIG)
    assert service.notify('**扩缩容** 成功', env='prod') is True
    assert sender.dispatcher.wait_idle()
    [call] = im.calls
    assert call['url'] == DING_URL
    assert call['json'] == {'msgtype': 'markdown', 'markdown': {'title': '告警', 'text': '**扩缩容** 成功'},
                            'at': {'atMobiles': ['13800000000']}}
    assert call['timeout'] == 10 and call['allow_redirects'] is False
    assert call['thread'].startswith('notify-test')
    assert any('消息通知已发送(集群 prod,钉钉)' in line for line in logs)
    assert sender.dispatcher.pending() == 0


def test_notify_uses_default_for_other_clusters(sender, im):
    service.apply(CONFIG)
    assert service.notify('内容', env='test') is True
    assert service.notify('内容') is True
    assert sender.dispatcher.wait_idle()
    assert [call['url'] for call in im.calls] == [WECOM_URL] * 2
    assert im.calls[0]['json'] == {'msgtype': 'markdown', 'markdown': {'content': '内容<@zhangsan>'}}


def test_notify_without_config_does_not_send(sender, im, logs):
    assert service.notify('新服务加入管控表', env='prod') is False
    service.apply({'clusters': {'other': stored('wecom', WECOM)}})
    assert service.notify('新服务加入管控表', env='prod') is False
    assert sender.dispatcher.wait_idle()
    assert im.calls == []
    skipped = [line for line in logs if '没有配置消息通知' in line]
    assert len(skipped) == 2 and '集群 prod' in skipped[0] and 'INFO' in skipped[0]


@pytest.mark.parametrize('content', ['', '   \n', None])
def test_notify_ignores_empty_content(sender, im, content):
    service.apply(CONFIG)
    assert service.notify(content, env='prod') is False
    assert sender.dispatcher.wait_idle() and im.calls == []


@pytest.mark.asyncio
async def test_notify_in_event_loop_returns_immediately(sender, monkeypatch):
    """在事件循环里调用:不等发送(IM 要 0.5 秒才回),loop 不被卡住"""
    fake = FakeIM(delay=0.5)
    monkeypatch.setattr(service.requests, 'post', fake)
    service.apply(CONFIG)
    started = time.perf_counter()
    assert service.notify('内容', env='prod') is True
    assert time.perf_counter() - started < 0.2
    ticks = 0
    for _ in range(5):
        await asyncio.sleep(0.01)
        ticks += 1
    assert ticks == 5 and time.perf_counter() - started < 0.4
    assert await asyncio.to_thread(sender.dispatcher.wait_idle)
    assert len(fake.calls) == 1


def test_notify_from_many_threads_while_config_changes(sender, im):
    service.apply(CONFIG)
    errors = []

    def notifier(index):
        try:
            for _ in range(10):
                assert service.notify(f'消息 {index}', env='prod' if index % 2 else 'test') is True
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def reloader():
        try:
            for _ in range(50):
                service.apply(CONFIG)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=notifier, args=(i,)) for i in range(8)] + [threading.Thread(target=reloader)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sender.dispatcher.wait_idle()
    assert errors == []
    assert len(im.calls) == 80 and sender.dispatcher.pending() == 0
    assert im.max_active == {DING_URL: 1, WECOM_URL: 1}  # 同一个机器人同时只发一条


@pytest.mark.parametrize('fake, expected', [
    (FakeIM(text='{"errcode":93000,"errmsg":"invalid webhook url, hint: [123]"}'),
     '消息通知发送失败(全局配置,企业微信):错误码 93000:invalid webhook url, hint: [123]'),
    (FakeIM(status=404, text='<html>404</html>'), '消息通知发送失败(全局配置,企业微信):HTTP 404'),
    (FakeIM(exc=requests.ConnectTimeout(f'Max retries exceeded with url: /cgi-bin/webhook/send?key={WECOM}')),
     '消息通知发送失败(全局配置,企业微信):请求超时(10 秒内没有返回)'),
    (FakeIM(exc=requests.ConnectionError(
        f"HTTPSConnectionPool(host='qyapi.weixin.qq.com', port=443): Max retries exceeded with url: "
        f"/cgi-bin/webhook/send?key={WECOM} (Caused by NameResolutionError)")),
     '消息通知发送失败(全局配置,企业微信):无法访问 IM 接口:ConnectionError: HTTPSConnectionPool'),
    (FakeIM(exc=ValueError(f'odd {WECOM}')), '消息通知发送失败(全局配置,企业微信):发送失败:ValueError: odd ***'),
])
def test_send_failures_are_logged_with_error_but_without_token(sender, monkeypatch, logs, fake, expected):
    monkeypatch.setattr(service.requests, 'post', fake)
    service.apply({'default': stored('wecom', WECOM)})
    assert service.notify('内容', env='prod') is True  # 不抛异常
    assert sender.dispatcher.wait_idle()
    failed = [line for line in logs if '消息通知发送失败' in line]
    assert len(failed) == 1 and expected in failed[0] and 'WARNING' in failed[0]
    assert not any(WECOM in line for line in logs)
    assert sender.dispatcher.pending() == 0


def test_notify_never_raises(sender, monkeypatch, logs):
    service.apply(CONFIG)
    # 选通道 / 入队出错:返回 False,只记日志
    original_pick = service._pick
    monkeypatch.setattr(service, '_pick', Mock(side_effect=KeyError('x')))
    assert service.notify('内容', env='prod') is False
    monkeypatch.setattr(service, '_pick', original_pick)
    monkeypatch.setattr(service, '_dispatcher', SimpleNamespace(submit=Mock(side_effect=RuntimeError('boom'))))
    assert service.notify('内容', env='prod') is False
    assert len([line for line in logs if '提交消息通知失败' in line]) == 2


def test_executor_shutdown_drops_queue_with_warning(sender, im, logs):
    """线程池已经关闭(进程退出时):调度线程丢掉这个机器人的消息并提示,不会空转"""
    service.apply(CONFIG)

    def broken_submit(*args, **kwargs):
        raise RuntimeError('cannot schedule new futures after shutdown')

    sender.executor.submit = broken_submit
    try:
        assert service.notify('内容', env='prod') is True
        wait_until(lambda: any('提交消息通知失败,丢弃钉钉机器人待发的 1 条' in line for line in logs))
        assert sender.dispatcher.pending() == 0 and im.calls == []
    finally:
        del sender.executor.submit


def test_deliver_redacts_every_part_of_slack_token(monkeypatch):
    error = requests.ConnectionError(f'url: /services/{SLACK} and secret XXXXXXXXXXXXXXXXXXXXXXXX')
    monkeypatch.setattr(service.requests, 'post', FakeIM(exc=error))
    ok, message = service.deliver({'type': 'slack', 'token': SLACK, 'at': ''}, '内容')
    assert ok is False and 'XXXXXXXXXXXXXXXXXXXXXXXX' not in message and 'T00000000' not in message
    assert message.startswith('无法访问 IM 接口:ConnectionError:') and len(message) <= 300


def test_deliver_success_and_long_errors(monkeypatch):
    monkeypatch.setattr(service.requests, 'post', FakeIM(text='ok'))
    assert service.deliver({'type': 'slack', 'token': SLACK, 'at': 'U1'}, '内容') == (True, '发送成功')
    monkeypatch.setattr(service.requests, 'post', FakeIM(exc=requests.ConnectionError('x' * 1000)))
    ok, message = service.deliver({'type': 'wecom', 'token': WECOM, 'at': ''}, '内容')
    assert ok is False and len(message) == 300
    # 配置里的类型不对(不会发生,兜底):不抛异常
    assert service.deliver({'type': 'qq', 'token': 'x'}, '内容')[0] is False


# ---------------------------------------------------------------------------
# 按机器人排队、顺序发送、限速
# ---------------------------------------------------------------------------
def test_defaults_are_20_per_minute_and_300_backlog():
    assert (service.RATE_LIMIT, service.RATE_WINDOW, service.MAX_BACKLOG) == (20, 60, 300)
    dispatcher = service.Dispatcher(None)
    assert (dispatcher.rate_limit, dispatcher.window, dispatcher.backlog) == (20, 60.0, 300)
    assert service.Dispatcher(None, rate_limit=0, backlog=0).rate_limit == 1  # 至少 1


def test_sliding_window():
    robot = service._Robot('企业微信')
    robot.sent.extend([0, 1, 2])
    assert robot.ready_at(1.5, 4, 10) == 1.5  # 没满
    assert robot.ready_at(5, 3, 10) == 10  # 满了:等最早那条出窗口
    assert robot.ready_at(10, 3, 10) == 10 and list(robot.sent) == [1, 2]  # 正好 10 秒前的那条已经出窗口
    robot.sent.append(10)
    assert robot.ready_at(10.5, 3, 10) == 11
    assert robot.ready_at(10.5, 2, 10) == 12  # 限额调小了也按最近的 limit 条算


def test_rate_limit_per_robot_with_fake_clock(sender, im):
    """每个机器人每个窗口最多 rate_limit 条,其余排队按原顺序发;到时间才发下一批"""
    now = [0.0]
    dispatcher = sender.make(rate_limit=2, window=60, clock=lambda: now[0])
    service.apply(PLAIN)
    for index in range(5):
        assert service.notify(f'消息 {index}', env='prod') is True
    wait_until(lambda: len(im.calls) == 2)
    stays(lambda: len(im.calls) == 2)  # 第 3 条要等窗口
    assert dispatcher.pending() == 3
    now[0] = 30.0
    dispatcher.wake()
    stays(lambda: len(im.calls) == 2)
    now[0] = 60.0  # 前两条(结束时间 0)出窗口了
    dispatcher.wake()
    wait_until(lambda: len(im.calls) == 4)
    stays(lambda: len(im.calls) == 4)
    now[0] = 120.0
    dispatcher.wake()
    assert dispatcher.wait_idle()
    assert im.texts() == [f'消息 {index}' for index in range(5)]  # 顺序不变


def test_rate_limit_of_one_robot_does_not_delay_others(sender, im):
    now = [0.0]
    dispatcher = sender.make(rate_limit=1, window=60, clock=lambda: now[0])
    service.apply(PLAIN)
    service.notify('prod 1', env='prod')
    service.notify('prod 2', env='prod')  # 钉钉机器人这一分钟用完了
    wait_until(lambda: len(im.calls) == 1)
    service.notify('test 1', env='test')  # 全局配置是另一个机器人(企微)
    wait_until(lambda: len(im.calls) == 2)
    assert im.texts(WECOM_URL) == ['test 1'] and im.texts(DING_URL) == ['prod 1']
    assert dispatcher.pending() == 1


def test_same_token_shares_one_queue_across_clusters(sender, im):
    """两个集群配置了同一个机器人:按机器人限速,而不是按集群"""
    now = [0.0]
    sender.make(rate_limit=1, window=60, clock=lambda: now[0])
    service.apply({'clusters': {'a': stored('dingding', DING), 'b': stored('dingding', DING)}})
    service.notify('a 1', env='a')
    service.notify('b 1', env='b')
    wait_until(lambda: len(im.calls) == 1)
    stays(lambda: len(im.calls) == 1)


def test_one_slow_robot_does_not_block_others(sender, monkeypatch):
    gate = threading.Event()
    fake = FakeIM(gate=gate, gate_url=DING_URL)  # 钉钉卡住
    monkeypatch.setattr(service.requests, 'post', fake)
    service.apply(PLAIN)
    service.notify('prod 1', env='prod')
    service.notify('prod 2', env='prod')
    service.notify('test 1', env='test')
    wait_until(lambda: fake.texts(WECOM_URL) == ['test 1'])
    assert fake.texts(DING_URL) == ['prod 1']  # 第二条要等第一条发完
    gate.set()
    assert sender.dispatcher.wait_idle()
    assert fake.texts(DING_URL) == ['prod 1', 'prod 2'] and fake.max_active[DING_URL] == 1


def test_backlog_drops_oldest_with_warning(sender, monkeypatch, logs):
    gate = threading.Event()
    fake = FakeIM(gate=gate)
    monkeypatch.setattr(service.requests, 'post', fake)
    dispatcher = sender.make(backlog=3)
    service.apply(PLAIN)
    service.notify('m0', env='prod')
    wait_until(lambda: len(fake.calls) == 1)  # m0 正在发(卡住)
    for index in range(1, 7):
        service.notify(f'm{index}', env='prod')  # 队列里最多 3 条:m1、m2、m3 先被挤掉
    assert dispatcher.pending() == 4  # 正在发的 1 条 + 排队的 3 条
    dropped = [line for line in logs if '消息通知积压太多' in line]
    assert len(dropped) == 1 and '丢弃了最早的 1 条' in dropped[0]  # 一分钟内只提示一次
    assert '钉钉机器人待发的通知超过 3 条' in dropped[0] and '集群 prod' in dropped[0] and 'WARNING' in dropped[0]
    gate.set()
    assert dispatcher.wait_idle()
    assert fake.texts() == ['m0', 'm4', 'm5', 'm6']
    # 期间又丢的 2 条在队列发空时补一条提示
    wait_until(lambda: len([line for line in logs if '消息通知积压太多' in line]) == 2)
    assert '丢弃了最早的 2 条' in [line for line in logs if '消息通知积压太多' in line][1]
    assert not any(DING in line for line in logs)


@pytest.mark.asyncio
async def test_notify_in_event_loop_while_rate_limited(sender, im):
    """限速、排队都在调度线程里:事件循环里一次放进很多条也是立即返回"""
    dispatcher = sender.make(rate_limit=1, window=60)
    service.apply(PLAIN)
    started = time.perf_counter()
    for index in range(200):  # agent 重连后一次补发很多条
        assert service.notify(f'消息 {index}', env='prod') is True
    assert time.perf_counter() - started < 0.5
    await asyncio.sleep(0.1)
    assert len(im.calls) == 1 and dispatcher.pending() == 199


def test_idle_robots_are_forgotten(sender, im):
    """发完、出了窗口的机器人不留在内存里"""
    now = [0.0]
    dispatcher = sender.make(rate_limit=5, window=10, clock=lambda: now[0])
    service.apply(PLAIN)
    service.notify('x', env='prod')
    assert dispatcher.wait_idle()
    assert len(dispatcher._robots) == 1  # 还在窗口里:要记着发送时间
    now[0] = 11.0
    dispatcher.wake()
    wait_until(lambda: len(dispatcher._robots) == 0)


def test_unexpected_send_error_does_not_stall_the_queue(sender, monkeypatch, logs):
    gate = threading.Event()
    monkeypatch.setattr(service.requests, 'post', FakeIM(gate=gate))
    service.apply(PLAIN)
    service.notify('卡住', env='prod')
    assert sender.dispatcher.wait_idle(timeout=0.05) is False  # 还没发完
    gate.set()
    assert sender.dispatcher.wait_idle()
    monkeypatch.setattr(service, '_send', Mock(side_effect=RuntimeError(f'boom {DING}')))
    service.notify('a', env='prod')
    service.notify('b', env='prod')
    assert sender.dispatcher.wait_idle()
    assert service._send.call_count == 2  # 第一条出错不影响第二条
    errors = [line for line in logs if '消息通知发送异常(集群 prod,钉钉):RuntimeError' in line]
    assert len(errors) == 2 and not any(DING in line for line in logs)


# ---------------------------------------------------------------------------
# 调用方:K8S 事件告警、管控表通知
# ---------------------------------------------------------------------------
class FakeMatcher:
    def should_ignore_event(self, event):
        return False

    def match_alert_rules(self, event):
        return {'rule': {'name': 'Pod重启', 'severity': 'critical'}, 'event': event}


def event(k8s='prod', uid='uid-1'):
    return {'eventUid': uid, 'eventStatus': 'ADDED', 'level': 'Warning', 'count': 3, 'kind': 'Pod', 'k8s': k8s,
            'namespace': 'vip', 'name': 'user-1', 'reason': 'BackOff', 'message': 'Back-off restarting',
            'firstTimestamp': 'a', 'lastTimestamp': 'b', 'reportingComponent': 'kubelet', 'reportingInstance': 'n1'}


def test_event_alert_is_sent_to_the_cluster_of_the_event(monkeypatch):
    from k8s_event import event_alert_processor as module

    pool = SimpleNamespace(execute_command=Mock())
    monkeypatch.setattr(module, 'get_pg_event_client', lambda: SimpleNamespace(pool=pool))
    sent = Mock(return_value=True)
    monkeypatch.setattr(module, 'notify', sent)
    processor = module.EventAlertProcessor.__new__(module.EventAlertProcessor)  # 不读规则文件
    processor.rule_matcher = FakeMatcher()
    processor.stats = {key: 0 for key in ('total_events', 'ignored_events', 'matched_events', 'alerts_sent',
                                          'errors', 'dedup_blocked')}
    processor._alert_cache, processor._dedup_window = {}, 300

    info = processor.process_event(event('prod'))
    message, = sent.call_args.args
    assert sent.call_args.kwargs == {'env': 'prod'}
    assert message == info['message'] and 'K8S事件告警: Pod重启' in message
    assert processor.stats['alerts_sent'] == 1
    # 去重窗口内同一个事件不再发
    assert processor.process_event(event('prod')) is None and sent.call_count == 1
    # 没有配置通知(notify 返回 False):不算已发送
    sent.return_value = False
    processor.process_event(event('test', uid='uid-2'))
    assert sent.call_args.kwargs == {'env': 'test'} and processor.stats['alerts_sent'] == 1


def test_event_processor_no_longer_passes_msg_token():
    from k8s_event.event_processor import K8SEventProcessor

    processor = K8SEventProcessor.__new__(K8SEventProcessor)
    processor.pg_client = SimpleNamespace(upsert_event=Mock())
    processor.alert_processor = SimpleNamespace(process_event=Mock(return_value=None))
    data = {**event(), 'firstTimestamp': '2026-11-07T01:00:00Z', 'lastTimestamp': '2026-11-07T01:05:00Z',
            'msgToken': 'old-agent-token'}
    assert processor.process_event_message({'type': 'k8s_event', 'data': data}) is True
    args, kwargs = processor.alert_processor.process_event.call_args
    assert len(args) == 1 and kwargs == {} and args[0]['k8s'] == 'prod' and 'msgToken' not in args[0]


def test_master_utils_no_longer_has_old_senders():
    import utils

    for name in ('send_msg', '_send_msg_sync', 'wecom', 'dingding', 'feishu', 'slack', 'MSG_TOKEN', 'MSG_TYPE',
                 'DEFAULT_AT', 'MSG_TIMEOUT'):
        assert not hasattr(utils, name), name
    assert utils.notify is service.notify
