"""事件告警规则的严格校验(k8s_event/rules_schema.py)。

用例和 Web 页面的校验共用 tests/fixtures/event_rules_cases.json:两边的错误信息必须逐字一致。
"""
import json
import pathlib

import pytest

from k8s_event import rules_schema, rules_store
from k8s_event.event_processor import K8SEventProcessor

CASES = json.loads((pathlib.Path(__file__).parent / 'fixtures' / 'event_rules_cases.json').read_text(encoding='utf-8'))


@pytest.mark.parametrize('case', CASES['cases'], ids=[case['name'] for case in CASES['cases']])
def test_shared_cases(case):
    normalized, issues = rules_schema.check(case['rules'])
    assert issues == case.get('errors', [])
    assert normalized == case.get('normalized')


def test_shared_cases_cover_every_message_kind():
    messages = [issue['message'] for case in CASES['cases'] for issue in case.get('errors', [])]
    for fragment in ('必须是一个 JSON 对象', '顶层不能有', '缺少 global_ignore_rules', '必须是数组', '必须是对象（包含',
                     '不能有 description', '级别只对告警规则有用', '缺少名称', '必须是字符串', '名称最长',
                     'enabled 只能是', 'severity 只能是', '缺少条件', 'conditions 必须是对象', '至少要有一个条件',
                     '区分大小写', '可用字段', '必须写成', '缺少运算符', '不认识的运算符', '只能用于 count',
                     '只能写一个运算符', '必须是字符串数组', '至少要填一个值', '不能为空', '只有空格', '最长 1000',
                     '必须是字符串或数字', '的值必须是数字'):
        assert any(fragment in message for message in messages), fragment


def test_spec_example_message():
    case = next(case for case in CASES['cases'] if case['name'].startswith('数值运算符的值是字符串'))
    assert case['errors'][0]['message'] == '告警规则第 3 条「重启事件告警」的条件 count：greater_than 的值必须是数字'


def test_validate_raises_with_all_issues():
    with pytest.raises(rules_schema.RulesInvalid) as exc:
        rules_schema.validate({'global_ignore_rules': {}, 'alert_rules': 'x'})
    assert str(exc.value) == 'global_ignore_rules（忽略规则）必须是数组（共 2 处问题）'
    assert [issue['path'] for issue in exc.value.issues] == [['global_ignore_rules'], ['alert_rules']]
    with pytest.raises(rules_schema.RulesInvalid) as exc:
        rules_schema.validate(None)
    assert str(exc.value).startswith('规则必须是一个 JSON 对象') and '共' not in str(exc.value)


def alert(conditions, name='A'):
    return {'global_ignore_rules': [], 'alert_rules': [{'name': name, 'enabled': True, 'conditions': conditions}]}


def test_limits():
    many = {'global_ignore_rules': [], 'alert_rules': [{'name': f'r{n}', 'conditions': {'k8s': {'contains': ['x']}}}
                                                       for n in range(rules_schema.MAX_RULES + 1)]}
    _, issues = rules_schema.check(many)
    assert issues == [{'path': ['alert_rules'], 'message': '告警规则最多 200 条，现在有 201 条'}]
    many['alert_rules'].pop()
    assert rules_schema.check(many)[1] == []
    values = [f'v{n}' for n in range(rules_schema.MAX_VALUES + 1)]
    _, issues = rules_schema.check(alert({'reason': {'contains': values}}))
    assert issues[0]['message'] == '告警规则第 1 条「A」的条件 reason：contains 最多 200 个值'
    assert rules_schema.check(alert({'reason': {'contains': values[:-1]}}))[1] == []
    assert rules_schema.check(alert({'reason': {'contains': ['x' * 1000]}}))[1] == []
    assert rules_schema.check(alert({'reason': {'equals': 'x' * 1000}}))[1] == []
    assert rules_schema.check(alert({'k8s': {'contains': ['x']}}, name='长' * 100))[1] == []


@pytest.mark.parametrize('value,ok', [
    (1, True), (-1, True), (2.5, True), (10 ** 30, True),  # 大整数不能让校验崩掉
    (float('nan'), False), (float('inf'), False), (True, False), ('1', False), (None, False), ([1], False),
])
def test_numeric_values(value, ok):
    _, issues = rules_schema.check(alert({'count': {'greater_than': value}}))
    assert (issues == []) is ok
    _, issues = rules_schema.check(alert({'count': {'equals': value}}))
    assert (issues == []) is (ok or value == '1')  # equals 还可以是字符串


def test_normalized_value_is_a_new_object():
    raw = alert({'reason': {'contains': ['x']}})
    normalized = rules_schema.validate(raw)
    normalized['alert_rules'][0]['conditions']['reason']['contains'].append('y')
    assert raw['alert_rules'][0]['conditions']['reason']['contains'] == ['x']
    assert normalized['alert_rules'][0]['severity'] == 'warning'


def test_default_rules_pass_strict_validation_unchanged():
    default = rules_store.default_rules()
    assert rules_schema.validate(default) == default
    assert [len(default['global_ignore_rules']), len(default['alert_rules'])] == [2, 7]


@pytest.mark.parametrize('value,ok', [
    ({'global_ignore_rules': [], 'alert_rules': []}, True),
    ({}, True),  # 结构检查只看两个键存在时是不是数组
    ({'alert_rules': ['不是对象也照样导入'], 'other': 1}, True),
    ({'global_ignore_rules': {}, 'alert_rules': []}, False),
    ({'alert_rules': 'x'}, False),
    ([], False), ('x', False), (None, False),
])
def test_legacy_structure(value, ok):
    assert rules_schema.legacy_structure_ok(value) is ok


def test_summary_records_only_counts_and_names():
    summary = rules_schema.summary({'global_ignore_rules': [{'name': '忽略测试', 'conditions': {'k8s': {'contains': ['secret-word']}}}],
                                    'alert_rules': [{'name': 'A'}, 'x']})
    assert summary == {'global_ignore_rules': {'count': 1, 'names': ['忽略测试']},
                       'alert_rules': {'count': 2, 'names': ['A', '']}}
    assert 'secret-word' not in json.dumps(summary)
    assert rules_schema.summary(None) == {'global_ignore_rules': {'count': 0, 'names': []},
                                          'alert_rules': {'count': 0, 'names': []}}


def test_fields_match_what_the_event_processor_builds():
    processor = K8SEventProcessor.__new__(K8SEventProcessor)
    data = processor._process_event_data({
        'eventUid': 'u', 'eventStatus': 'ADDED', 'level': 'Warning', 'kind': 'Pod', 'namespace': 'n', 'name': 'p',
        'firstTimestamp': '2026-11-15T01:00:00Z', 'lastTimestamp': '2026-11-15T01:00:00Z'})
    assert set(data) == set(rules_schema.FIELDS)
    assert len(rules_schema.FIELDS) == 14
    assert rules_schema.OPERATORS == ('contains', 'not_contains', 'starts_with', 'not_starts_with', 'ends_with',
                                      'not_ends_with', 'equals', 'not_equals', 'greater_than', 'less_than',
                                      'greater_equal', 'less_equal')
