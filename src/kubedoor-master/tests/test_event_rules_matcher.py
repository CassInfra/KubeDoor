"""事件告警规则的匹配语义:改成读数据库快照后,与以前读规则文件时完全一致。

- 旧实现(2.5 及以前的 k8s_event/alert_rule_matcher.py)原样拷贝在 OldMatcher 里,随机生成大量规则和事件逐项对照
- 12 个运算符、忽略规则之间 OR、第一条命中的告警规则生效、DELETED 跳过
- 处理器每个事件读一次当前快照:保存后马上生效,不重建处理器(去重记录和统计保留)
- 去重窗口取通用配置 alert_dedup_window,每次用时取
"""
import random
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from loguru import logger

import general_settings
from k8s_event import alert_rule_matcher as matcher_module
from k8s_event import event_alert_processor as processor_module
from k8s_event import rules_store
from k8s_event.alert_rule_matcher import AlertRuleMatcher, first_match, match_field_condition
from k8s_event.event_alert_processor import EventAlertProcessor


# ---------------------------------------------------------------------------
# 旧实现(对照用):2.5 及以前的匹配代码原样拷贝,只去掉了读规则文件和日志
# ---------------------------------------------------------------------------
class OldMatcher:
    def __init__(self, config):
        self.rules = config.get('alert_rules', [])
        self.global_ignore_rules = config.get('global_ignore_rules', [])

    def should_ignore_event(self, event):
        for ignore_rule in self.global_ignore_rules:
            if not ignore_rule.get('enabled', True):
                continue
            conditions = ignore_rule.get('conditions', {})
            if self._match_conditions(event, conditions):
                return True
        return False

    def match_alert_rules(self, event):
        if self.should_ignore_event(event):
            return None
        for rule in self.rules:
            if not rule.get('enabled', True):
                continue
            if self._match_rule(event, rule):
                return {'rule': rule, 'event': event}
        return None

    def _match_rule(self, event, rule):
        conditions = rule.get('conditions', {})
        return self._match_conditions(event, conditions)

    def _match_conditions(self, event, conditions):
        for field_name, field_conditions in conditions.items():
            if not self._match_field_condition(event, field_name, field_conditions):
                return False
        return True

    def _match_field_condition(self, event, field_name, field_conditions):
        field_value = event.get(field_name)

        # 如果字段不存在，根据条件类型决定是否匹配
        if field_value is None:
            # 对于not_contains、not_starts_with和not_ends_with，字段不存在时认为匹配
            if (
                'not_contains' in field_conditions
                or 'not_starts_with' in field_conditions
                or 'not_ends_with' in field_conditions
            ):
                return True
            # 对于其他条件，字段不存在时不匹配
            return False

        # 转换为字符串进行匹配（除了数值比较）
        field_str = str(field_value)

        # contains条件：字段值包含任一指定值
        if 'contains' in field_conditions:
            contains_values = field_conditions['contains']
            if not isinstance(contains_values, list):
                contains_values = [contains_values]

            for value in contains_values:
                if str(value).lower() in field_str.lower():
                    return True
            return False

        # not_contains条件：字段值不包含任何指定值
        if 'not_contains' in field_conditions:
            not_contains_values = field_conditions['not_contains']
            if not isinstance(not_contains_values, list):
                not_contains_values = [not_contains_values]

            for value in not_contains_values:
                if str(value).lower() in field_str.lower():
                    return False
            return True

        # starts_with条件：字段值以指定值开头
        if 'starts_with' in field_conditions:
            starts_values = field_conditions['starts_with']
            if not isinstance(starts_values, list):
                starts_values = [starts_values]

            for value in starts_values:
                if field_str.lower().startswith(str(value).lower()):
                    return True
            return False

        # not_starts_with条件：字段值不以任何指定值开头
        if 'not_starts_with' in field_conditions:
            not_starts_values = field_conditions['not_starts_with']
            if not isinstance(not_starts_values, list):
                not_starts_values = [not_starts_values]

            for value in not_starts_values:
                if field_str.lower().startswith(str(value).lower()):
                    return False
            return True

        # ends_with条件：字段值以指定值结尾
        if 'ends_with' in field_conditions:
            ends_values = field_conditions['ends_with']
            if not isinstance(ends_values, list):
                ends_values = [ends_values]

            for value in ends_values:
                if field_str.lower().endswith(str(value).lower()):
                    return True
            return False

        # not_ends_with条件：字段值不以任何指定值结尾
        if 'not_ends_with' in field_conditions:
            not_ends_values = field_conditions['not_ends_with']
            if not isinstance(not_ends_values, list):
                not_ends_values = [not_ends_values]

            for value in not_ends_values:
                if field_str.lower().endswith(str(value).lower()):
                    return False
            return True

        # equals条件：字段值等于指定值
        if 'equals' in field_conditions:
            return field_str.lower() == str(field_conditions['equals']).lower()

        # not_equals条件：字段值不等于指定值
        if 'not_equals' in field_conditions:
            return field_str.lower() != str(field_conditions['not_equals']).lower()

        # 数值比较条件（仅对count字段有效）
        if field_name == 'count':
            try:
                field_num = int(field_value)

                if 'greater_than' in field_conditions:
                    return field_num > field_conditions['greater_than']

                if 'less_than' in field_conditions:
                    return field_num < field_conditions['less_than']

                if 'greater_equal' in field_conditions:
                    return field_num >= field_conditions['greater_equal']

                if 'less_equal' in field_conditions:
                    return field_num <= field_conditions['less_equal']

            except (ValueError, TypeError):
                return False

        # 如果没有匹配的条件类型，默认返回True
        return True


# ---------------------------------------------------------------------------
# 公共夹具
# ---------------------------------------------------------------------------
def make_event(**changes):
    event = {'eventUid': 'uid-1', 'eventStatus': 'ADDED', 'level': 'Warning', 'count': 3, 'kind': 'Pod',
             'k8s': 'prod-a', 'namespace': 'vip', 'name': 'deploy-user-7f7d9bc955-6z2m4', 'reason': 'BackOff',
             'message': 'Back-off restarting failed container', 'firstTimestamp': 'a', 'lastTimestamp': 'b',
             'reportingComponent': 'kubelet', 'reportingInstance': 'node-01'}
    event.update(changes)
    return event


def rule(name, conditions, enabled=True, severity='warning'):
    return {'name': name, 'enabled': enabled, 'severity': severity, 'conditions': conditions}


def ruleset(alert=(), ignore=()):
    return rules_store.build({'global_ignore_rules': list(ignore), 'alert_rules': list(alert)}, 'test')


@pytest.fixture(autouse=True)
def fresh_rules(monkeypatch):
    monkeypatch.setattr(rules_store, '_snapshot', None)
    monkeypatch.setattr(rules_store, '_summary', None)
    monkeypatch.setattr(rules_store, '_refresh_failed', False)


@pytest.fixture
def settings(monkeypatch):
    """通用配置:默认去重窗口 300 秒,用例里可以改"""
    values = {'alert_dedup_window': 300}
    monkeypatch.setattr(general_settings, 'value', lambda name: values[name])
    return values


@pytest.fixture
def sent(monkeypatch):
    pool = SimpleNamespace(execute_command=Mock())
    monkeypatch.setattr(processor_module, 'get_pg_event_client', lambda: SimpleNamespace(pool=pool))
    notify = Mock(return_value=True)
    monkeypatch.setattr(processor_module, 'notify', notify)
    return notify


@pytest.fixture
def logs():
    lines = []
    handler_id = logger.add(lambda message: lines.append(str(message)), level='DEBUG')
    yield lines
    logger.remove(handler_id)


# ---------------------------------------------------------------------------
# 与旧实现逐项对照
# ---------------------------------------------------------------------------
FIELD_NAMES = ('k8s', 'namespace', 'name', 'reason', 'message', 'level', 'count', 'eventStatus', 'kind', 'Reason')
OPS = ('contains', 'not_contains', 'starts_with', 'not_starts_with', 'ends_with', 'not_ends_with', 'equals',
       'not_equals', 'greater_than', 'less_than', 'greater_equal', 'less_equal', 'contain', 'gt')
WORDS = ('prod', 'PROD', 'test', 'deploy-', 'Warning', 'warning', 'back-off', 'restart', 'Pod', 'pod', 'node-01',
         '', ' ', '3', '10', 'vip', 'ADDED', 'DELETED', 'kubelet', 'Set', '-6z2m4', 'BackOff', 'ok')


def random_value(rnd):
    kind = rnd.randrange(10)
    if kind < 4:
        return [rnd.choice(WORDS) for _ in range(rnd.randrange(0, 4))]
    if kind < 6:
        return rnd.choice(WORDS)
    if kind == 6:
        return rnd.choice((0, 1, 2, 3, 5, 10, -1, 2.5))
    if kind == 7:
        return rnd.choice((True, False, None))
    if kind == 8:
        return [rnd.choice(WORDS), [rnd.choice(WORDS)], 3]
    return {'nested': rnd.choice(WORDS)}


def random_conditions(rnd, max_fields=3):
    conditions = {}
    for _ in range(rnd.randrange(0, max_fields + 1)):
        spec = {}
        for _ in range(rnd.randrange(0, 3)):
            spec[rnd.choice(OPS)] = random_value(rnd)
        conditions[rnd.choice(FIELD_NAMES)] = spec
    return conditions


def random_event(rnd):
    event = make_event(
        k8s=rnd.choice(('prod-a', 'PROD-bj', 'test-k8s', 'preprod', '')),
        namespace=rnd.choice(('vip', 'kube-system', '', 'test-ns')),
        name=rnd.choice(('deploy-user-6z2m4', 'node-01', 'Deploy-Batch', 'x')),
        reason=rnd.choice(('BackOff', 'Unhealthy', 'OOMKilling', 'SuccessfulCreate', '')),
        message=rnd.choice(('Back-off restarting failed container', 'Readiness probe failed', 'ok', '')),
        level=rnd.choice(('Warning', 'Normal')),
        count=rnd.choice((0, 1, 2, 3, 10)),
        eventStatus=rnd.choice(('ADDED', 'MODIFIED')),
        kind=rnd.choice(('Pod', 'Node', 'ReplicaSet')),
    )
    for field in rnd.sample(('reason', 'namespace', 'count'), rnd.randrange(0, 2)):
        event.pop(field)  # 模拟字段缺失(字段名写错时也是这样)
    return event


def test_field_conditions_match_the_old_implementation():
    rnd = random.Random(20261115)
    old = OldMatcher({})
    for _ in range(30000):
        event = random_event(rnd)
        field = rnd.choice(FIELD_NAMES)
        spec = {}
        for _ in range(rnd.randrange(0, 4)):
            spec[rnd.choice(OPS)] = random_value(rnd)
        expected = old._match_field_condition(event, field, spec)
        assert match_field_condition(event, field, rules_store.freeze(spec)) is expected, (event, field, spec)


def test_whole_rule_sets_match_the_old_implementation():
    rnd = random.Random(7)
    for _ in range(3000):
        config = {
            'global_ignore_rules': [{'name': f'i{n}', 'enabled': rnd.choice((True, False, 'yes', 0, None)),
                                     'conditions': random_conditions(rnd)} for n in range(rnd.randrange(0, 3))],
            'alert_rules': [{'name': f'a{n}', 'enabled': rnd.choice((True, True, False)),
                             'conditions': random_conditions(rnd)} for n in range(rnd.randrange(0, 5))],
        }
        for item in config['alert_rules'] + config['global_ignore_rules']:
            if rnd.random() < 0.2:
                item.pop('enabled')  # 不写 enabled 视为启用
        old = OldMatcher(config)
        new = AlertRuleMatcher(rules_store.build(config, 'test'))
        for _ in range(5):
            event = random_event(rnd)
            ignored, result = new.evaluate(event)
            assert ignored is old.should_ignore_event(event)
            expected = old.match_alert_rules(event)
            assert (result['rule']['name'] if result else None) == (expected['rule']['name'] if expected else None)
            assert new.should_ignore_event(event) is ignored
            assert new.match_alert_rules(event) == result


# ---------------------------------------------------------------------------
# 12 个运算符
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('field,spec,expected', [
    # 列表型:不区分大小写的子串 / 开头 / 结尾;contains 系列多个值是 OR,not_ 系列是 AND
    ('message', {'contains': ['RESTART', 'oom']}, True),
    ('message', {'contains': ['oom']}, False),
    ('message', {'not_contains': ['oom', 'probe']}, True),
    ('message', {'not_contains': ['oom', 'Back-off']}, False),
    ('name', {'starts_with': ['x', 'DEPLOY-']}, True),
    ('name', {'starts_with': ['user']}, False),
    ('name', {'not_starts_with': ['deploy-user', 'x']}, False),
    ('name', {'not_starts_with': ['x', 'y']}, True),
    ('name', {'ends_with': ['-6Z2M4']}, True),
    ('name', {'ends_with': ['deploy']}, False),
    ('name', {'not_ends_with': ['-6z2m4']}, False),
    ('name', {'not_ends_with': ['-test', '-debug']}, True),
    # 单个值:不区分大小写的整串比较
    ('level', {'equals': 'warning'}, True),
    ('level', {'equals': 'Warn'}, False),
    ('kind', {'not_equals': 'Node'}, True),
    ('kind', {'not_equals': 'POD'}, False),
    ('count', {'equals': 3}, True),
    ('count', {'equals': '3'}, True),
    # 数值:只对 count
    ('count', {'greater_than': 2}, True),
    ('count', {'greater_than': 3}, False),
    ('count', {'less_than': 4}, True),
    ('count', {'less_than': 3}, False),
    ('count', {'greater_equal': 3}, True),
    ('count', {'greater_equal': 4}, False),
    ('count', {'less_equal': 3}, True),
    ('count', {'less_equal': 2.5}, False),
])
def test_operators(field, spec, expected):
    assert match_field_condition(make_event(), field, rules_store.freeze(spec)) is expected


def test_loose_cases_kept_from_the_rule_file_era():
    event = make_event()
    frozen = rules_store.freeze
    # 单个字符串等同于只有一个元素的数组
    assert match_field_condition(event, 'reason', frozen({'contains': 'backoff'})) is True
    # 一个字段写了多个运算符时只认第一个(contains 在 not_contains 前面)
    assert match_field_condition(event, 'reason', frozen({'not_contains': ['back'], 'contains': ['back']})) is True
    assert match_field_condition(event, 'count', frozen({'greater_than': 1, 'less_than': 2})) is True
    # 不认识的运算符视为成立;数值运算符写在别的字段上也视为成立
    assert match_field_condition(event, 'reason', frozen({'contain': ['zzz']})) is True
    assert match_field_condition(event, 'name', frozen({'greater_than': 100})) is True
    # 数值写成字符串:不成立
    assert match_field_condition(event, 'count', frozen({'greater_than': '1'})) is False
    # 字段不存在(名字写错):只有 not_ 系列成立
    assert match_field_condition(event, 'Reason', frozen({'not_contains': ['x']})) is True
    assert match_field_condition(event, 'Reason', frozen({'contains': ['x']})) is False
    assert match_field_condition(event, 'Reason', frozen({'equals': ''})) is False
    # 条件不是对象(只可能来自原样导入的旧文件):不成立,不抛异常
    assert match_field_condition(event, 'reason', ('contains',)) is False
    assert matcher_module.match_conditions(event, ('reason',)) is False
    # 空条件匹配所有事件
    assert matcher_module.match_conditions(event, rules_store.freeze({})) is True


def test_nested_values_compare_like_the_json_they_came_from():
    # 快照把数组冻结成 tuple,比较时要还原成 list,和以前直接读 JSON 时 str() 的结果一样
    event = make_event(message="got ['a', 1] here", reason="{'k': 'v'}")
    assert match_field_condition(event, 'message', rules_store.freeze({'contains': [['a', 1]]})) is True
    assert match_field_condition(event, 'reason', rules_store.freeze({'equals': {'k': 'v'}})) is True


# ---------------------------------------------------------------------------
# 规则之间的关系
# ---------------------------------------------------------------------------
def test_ignore_rules_are_or_and_disabled_rules_are_skipped():
    rules = ruleset(
        ignore=[rule('测试集群', {'k8s': {'contains': ['test']}}),
                rule('停用的', {'reason': {'contains': ['backoff']}}, enabled=False),
                {'name': '不写 enabled', 'conditions': {'namespace': {'equals': 'kube-system'}}}],
        alert=[rule('全部', {'reason': {'not_equals': ''}})])
    matcher = AlertRuleMatcher(rules)
    assert matcher.evaluate(make_event(k8s='test-k8s')) == (True, None)
    assert matcher.evaluate(make_event(namespace='kube-system'))[0] is True
    ignored, result = matcher.evaluate(make_event())  # 停用的忽略规则不生效
    assert ignored is False and result['rule']['name'] == '全部'


def test_first_matching_alert_rule_wins_and_conditions_are_and():
    rules = ruleset(alert=[
        rule('停用', {'reason': {'contains': ['back']}}, enabled=False),
        rule('两个条件都要满足', {'reason': {'contains': ['back']}, 'count': {'greater_than': 5}}),
        rule('重启', {'message': {'contains': ['restart']}}, severity='critical'),
        rule('兜底', {'level': {'equals': 'Warning'}}),
    ])
    matcher = AlertRuleMatcher(rules)
    assert matcher.match_alert_rules(make_event())['rule']['name'] == '重启'
    assert matcher.match_alert_rules(make_event(count=6))['rule']['name'] == '两个条件都要满足'
    assert matcher.match_alert_rules(make_event(message='x'))['rule']['name'] == '兜底'
    assert matcher.match_alert_rules(make_event(message='x', level='Normal')) is None
    assert first_match(rules.alert_rules, make_event(message='x', level='Normal')) is None


def test_rule_stats_describe_the_snapshot_without_configmap():
    stats = AlertRuleMatcher(ruleset(alert=[rule('a', {'k8s': {'contains': ['x']}}),
                                            rule('b', {'k8s': {'contains': ['y']}}, enabled=False)],
                                     ignore=[rule('i', {'k8s': {'contains': ['z']}})])).get_rule_stats()
    assert stats == {'source': 'test', 'loaded': True, 'total_rules': 2, 'enabled_rules': 1, 'disabled_rules': 1,
                     'ignore_rules': 1, 'enabled_ignore_rules': 1}
    assert 'ConfigMap' not in str(AlertRuleMatcher().get_rule_stats())


# ---------------------------------------------------------------------------
# 处理器:读当前快照、DELETED、去重
# ---------------------------------------------------------------------------
def test_deleted_events_are_skipped_before_any_rule(settings, sent):
    rules_store.apply({'global_ignore_rules': [], 'alert_rules': [rule('全部', {'kind': {'not_equals': ''}})]})
    processor = EventAlertProcessor()
    assert processor.process_event(make_event(eventStatus='DELETED')) is None
    assert processor.stats['ignored_events'] == 1 and processor.stats['matched_events'] == 0
    assert not sent.called


def test_saved_rules_take_effect_immediately_without_rebuilding_the_processor(settings, sent):
    rules_store.apply({'global_ignore_rules': [], 'alert_rules': [rule('旧规则', {'reason': {'contains': ['oom']}})]})
    processor = EventAlertProcessor()
    assert processor.process_event(make_event(eventUid='u1')) is None
    first = processor.process_event(make_event(eventUid='u2', reason='OOMKilling'))
    assert 'K8S事件告警: 旧规则' in first['message']
    # 换规则(页面保存时调用 apply):同一个处理器下一个事件就用新规则,去重记录和统计都还在
    rules_store.apply({'global_ignore_rules': [rule('忽略 vip', {'namespace': {'equals': 'vip'}})],
                       'alert_rules': [rule('新规则', {'reason': {'contains': ['back']}}, severity='critical')]})
    assert processor.process_event(make_event(eventUid='u3')) is None  # vip 被新的忽略规则忽略
    third = processor.process_event(make_event(eventUid='u4', namespace='prod'))
    assert '🚨 K8S事件告警: 新规则' in third['message'] and '级别: CRITICAL' in third['message']
    assert processor.process_event(make_event(eventUid='u2', reason='OOMKilling', namespace='prod')) is None
    assert processor.stats['dedup_blocked'] == 0  # u2 不再命中任何规则(旧规则已经没了)
    assert set(processor._alert_cache) == {'u2', 'u4'}
    assert processor.stats['total_events'] == 5 and processor.stats['alerts_sent'] == 2


def test_default_rules_are_used_before_the_snapshot_is_loaded(settings, sent):
    processor = EventAlertProcessor()
    info = processor.process_event(make_event(reason='OOMKilling', message='x'))
    assert 'K8S事件告警: 关键事件告警' in info['message']
    assert rules_store.current().source == rules_store.SOURCE_DEFAULT and not rules_store.is_loaded()


def test_dedup_window_comes_from_general_settings_each_time(settings, sent, monkeypatch):
    rules_store.apply({'global_ignore_rules': [], 'alert_rules': [rule('重启', {'message': {'contains': ['restart']}})]})
    processor = EventAlertProcessor()
    now = [1000.0]
    monkeypatch.setattr(processor_module, 'time', SimpleNamespace(time=lambda: now[0]))
    assert processor.process_event(make_event()) is not None
    now[0] += 299
    assert processor.process_event(make_event()) is None  # 300 秒内去重
    settings['alert_dedup_window'] = 120  # 改了通用配置马上生效
    assert processor.process_event(make_event()) is not None
    settings['alert_dedup_window'] = 0  # 0 表示不去重
    assert processor.process_event(make_event()) is not None
    assert processor.process_event(make_event()) is not None
    assert sent.call_count == 4 and processor.stats['dedup_blocked'] == 1
    assert processor.get_dedup_cache_info()['dedup_window_seconds'] == 0


def test_dedup_falls_back_to_300_seconds_when_settings_unavailable(sent, monkeypatch, logs):
    monkeypatch.setattr(processor_module, '_window_warned', False)
    monkeypatch.setattr(general_settings, 'value', Mock(side_effect=KeyError('alert_dedup_window')))
    assert processor_module.dedup_window() == 300
    assert processor_module.dedup_window() == 300
    assert sum('事件告警去重窗口' in line for line in logs) == 1


def test_same_event_processed_concurrently_alerts_once(settings, sent):
    rules_store.apply({'global_ignore_rules': [], 'alert_rules': [rule('重启', {'message': {'contains': ['restart']}})]})
    processor = EventAlertProcessor()
    barrier = threading.Barrier(8)

    def work():
        barrier.wait()
        processor.process_event(make_event(eventUid='same'))

    threads = [threading.Thread(target=work) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sent.call_count == 1  # 去重的检查和记录在同一把锁里,并发处理同一个事件只发一条
    assert list(processor._alert_cache) == ['same']


def test_failed_submit_releases_the_dedup_record(settings, sent):
    rules_store.apply({'global_ignore_rules': [], 'alert_rules': [rule('重启', {'message': {'contains': ['restart']}})]})
    processor = EventAlertProcessor()
    sent.side_effect = RuntimeError('queue closed')
    assert processor.process_event(make_event()) is not None
    assert processor.stats['errors'] == 1 and processor._alert_cache == {}
    sent.side_effect = None
    assert processor.process_event(make_event()) is not None  # 没提交出去的,下次更新再试
    assert processor.stats['alerts_sent'] == 1 and 'uid-1' in processor._alert_cache


def test_legacy_rule_with_strange_severity_still_alerts(settings, sent):
    rules_store.apply({'alert_rules': [{'name': '旧规则', 'severity': 3, 'conditions': {}}]})
    info = EventAlertProcessor().process_event(make_event())
    assert '⚠️ K8S事件告警: 旧规则' in info['message'] and '级别: WARNING' in info['message']


def test_batch_stats_and_events_without_uid(settings, sent):
    rules_store.apply({'global_ignore_rules': [rule('忽略测试', {'k8s': {'starts_with': ['test-']}})],
                       'alert_rules': [rule('重启', {'message': {'contains': ['restart']}})]})
    processor = EventAlertProcessor()
    alerts = processor.process_events_batch([
        make_event(eventUid=''), make_event(eventUid=''),  # 没有 eventUid 的不去重
        make_event(eventUid='u1', k8s='test-a'), make_event(eventUid='u2', message='x'),
    ])
    assert len(alerts) == 2 and sent.call_count == 2 and processor._alert_cache == {}
    stats = processor.get_stats()
    assert stats['processor_stats'] == {'total_events': 4, 'ignored_events': 1, 'matched_events': 2,
                                        'alerts_sent': 2, 'errors': 0, 'dedup_blocked': 0}
    assert stats['alert_rate'] == 50 and stats['rule_stats']['source'] == 'database'
    processor.reset_stats()
    assert processor.stats['total_events'] == 0
    processor._release_alert('', None)  # 没有 eventUid:什么都不做


def test_marking_failure_and_unexpected_errors_are_counted(settings, sent, monkeypatch, logs):
    rules_store.apply({'global_ignore_rules': [], 'alert_rules': [rule('重启', {'message': {'contains': ['restart']}})]})
    broken = SimpleNamespace(execute_command=Mock(side_effect=RuntimeError('db down')))
    monkeypatch.setattr(processor_module, 'get_pg_event_client', lambda: SimpleNamespace(pool=broken))
    processor = EventAlertProcessor()
    assert processor.process_event(make_event()) is not None  # 标记「已告警」失败不影响发送
    assert any('更新level字段失败' in line for line in logs)
    # 构建告警信息出错:撤销去重记录,算一次错误
    monkeypatch.setattr(processor, '_build_alert_info', Mock(side_effect=RuntimeError('boom')))
    assert processor.process_event(make_event(eventUid='u2')) is None
    assert 'u2' not in processor._alert_cache and processor.stats['errors'] == 1
    # 匹配器出错:算一次错误,不抛异常
    processor.rule_matcher = SimpleNamespace(evaluate=Mock(side_effect=RuntimeError('bad rules')))
    assert processor.process_event(make_event(eventUid='u3')) is None
    assert processor.stats['errors'] == 2


def test_dedup_cache_is_pruned_lazily(settings, sent, monkeypatch):
    processor = EventAlertProcessor(AlertRuleMatcher(ruleset(alert=[rule('全部', {'kind': {'not_equals': ''}})])))
    now = [10000.0]
    monkeypatch.setattr(processor_module, 'time', SimpleNamespace(time=lambda: now[0]))
    processor._alert_cache.update({f'old-{n}': 1000.0 for n in range(100)})
    processor._alert_cache['recent'] = now[0] - 10
    assert processor.process_event(make_event(eventUid='new')) is not None
    assert set(processor._alert_cache) == {'recent', 'new'}  # 超过 100 条时清掉过期的
    info = processor.get_dedup_cache_info()
    assert info == {'total_cache_items': 2, 'active_cache_items': 2, 'dedup_window_seconds': 300,
                    'dedup_blocked_count': 0}


# ---------------------------------------------------------------------------
# 事件入口(event_processor):入库在前,告警在后
# ---------------------------------------------------------------------------
GOOD = {'eventUid': 'u', 'eventStatus': 'ADDED', 'level': 'Warning', 'kind': 'Pod', 'namespace': 'n', 'name': 'p',
        'count': 2, 'firstTimestamp': '2026-11-15T01:00:00Z', 'lastTimestamp': '2026-11-15T01:05:00Z'}


def entry_processor():
    from k8s_event.event_processor import K8SEventProcessor

    processor = K8SEventProcessor.__new__(K8SEventProcessor)  # 不建数据库客户端
    processor.pg_client = SimpleNamespace(upsert_event=Mock())
    processor.alert_processor = SimpleNamespace(process_event=Mock(return_value={'alert_id': 'a', 'message': 'm'}))
    return processor


def test_event_message_is_validated_and_converted_to_beijing_time():
    from datetime import datetime

    processor = entry_processor()
    assert processor.process_event_message({'type': 'k8s_event', 'data': GOOD}) is True
    event = processor.pg_client.upsert_event.call_args.args[0]
    assert event['firstTimestamp'] == datetime(2026, 11, 15, 9, 0) and event['k8s'] == '' and event['count'] == 2
    assert processor.alert_processor.process_event.call_args.args[0] is event
    for message in ({'type': 'other', 'data': GOOD}, {'type': 'k8s_event', 'data': {}}):
        assert processor.process_event_message(message) is False
    for bad in ({**GOOD, 'eventStatus': 'X'}, {**GOOD, 'level': 'Error'}, {**GOOD, 'count': -1},
                {**GOOD, 'count': 'abc'}, {**GOOD, 'firstTimestamp': '2026-11-15T02:00:00Z'},
                {key: value for key, value in GOOD.items() if key != 'kind'}):
        assert processor.process_event_message({'type': 'k8s_event', 'data': bad}) is False, bad
    # 时间为空或格式不对:用当前的北京时间
    data = processor._process_event_data({**GOOD, 'firstTimestamp': '', 'lastTimestamp': 'bad'})
    assert isinstance(data['firstTimestamp'], datetime) and data['firstTimestamp'].tzinfo is None
    assert processor._validate_processed_data({'count': 1}) is False


def test_storage_failure_stops_alerting_but_alert_errors_do_not():
    processor = entry_processor()
    processor.pg_client.upsert_event.side_effect = RuntimeError('db down')
    assert processor.process_event_message({'type': 'k8s_event', 'data': GOOD}) is False
    assert not processor.alert_processor.process_event.called  # 没入库的事件不告警
    processor.pg_client.upsert_event.side_effect = None
    processor.alert_processor.process_event.side_effect = RuntimeError('rules')
    assert processor.process_event_message({'type': 'k8s_event', 'data': GOOD}) is True


@pytest.mark.asyncio
async def test_async_entry_and_stats(monkeypatch):
    from k8s_event import event_processor as module

    monkeypatch.setattr(module, 'get_pg_event_client', lambda: 'client')
    built = module.K8SEventProcessor()
    assert built.pg_client == 'client' and isinstance(built.alert_processor, EventAlertProcessor)
    seen = []
    fake = SimpleNamespace(process_event_message=lambda data: seen.append(threading.current_thread()) or True,
                           alert_processor=SimpleNamespace(get_stats=lambda: {'processor_stats': {}}))
    monkeypatch.setattr(module, 'get_event_processor', lambda: fake)
    assert await module.process_k8s_event_async({'type': 'k8s_event'}) is True
    assert seen[0] is not threading.main_thread()  # 在线程里处理,不卡 event loop
    assert module.get_alert_stats() == {'processor_stats': {}}
    fake.process_event_message = Mock(side_effect=RuntimeError('boom'))
    assert await module.process_k8s_event_async({'type': 'k8s_event'}) is False
