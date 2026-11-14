"""prom_real_time_data 的单元测试:指标整合,以及查询地址 / 集群标签 key 按通用配置的当前值取。"""
import pathlib
import sys
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import general_settings  # noqa: E402
import prom_real_time_data as prt  # noqa: E402


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    """通用配置的生效值:测试里改这个 dict 就相当于在页面上改了配置"""
    values = {'prom_k8s_tag_key': 'k8s', 'prom_url': 'http://vm.example.com:8428'}
    monkeypatch.setattr(general_settings, 'value', lambda name: values[name])
    return values


def series(dep, value, env='prod', ns='ns1', **labels):
    return {'metric': {'k8s': env, 'namespace': ns, 'deployment': dep, **labels}, 'value': [0, str(value)]}


def empty_metrics():
    return {name: [] for name in ['pod_count', *prt.VALUE_METRICS, 'ms_image']}


def test_rows_join_all_metrics_by_deployment():
    data = empty_metrics()
    data['pod_count'] = [series('a', 2), series('b', 1)]
    # 同一个 deployment 多条取最后一条;pod_count 里没有的 deployment 不出现在结果里
    data['avg_cpu_usage'] = [series('a', 10.4), series('a', 12.6), series('c', 99)]
    data['mem_limit'] = [series('b', 'NaN')]
    data['ms_image'] = [
        series('a', 1, image='repo/a:2'),
        series('a', 1, image_spec='repo/a:1', image='ignored'),
        series('b', 1, image=''),
    ]

    rows = {tuple(r[:3]): r for r in prt.process_metrics_data(data)}

    assert set(rows) == {('prod', 'ns1', 'a'), ('prod', 'ns1', 'b')}
    assert rows[('prod', 'ns1', 'a')] == ['prod', 'ns1', 'a', 2, 13, 0, 0, 0, 0, 0, 0, 0, 'repo/a:1,repo/a:2']
    assert rows[('prod', 'ns1', 'b')] == ['prod', 'ns1', 'b', 1, 0, 0, 0, 0, 0, 0, 0, 0, '']


def test_same_deployment_name_in_other_namespace_is_separate():
    data = empty_metrics()
    data['pod_count'] = [series('a', 1, ns='ns1'), series('a', 5, ns='ns2')]
    data['cpu_limit'] = [series('a', 500, ns='ns2')]

    rows = {tuple(r[:3]): r for r in prt.process_metrics_data(data)}

    assert rows[('prod', 'ns1', 'a')][7] == 0
    assert rows[('prod', 'ns2', 'a')][3:8] == [5, 0, 0, 0, 500]


def test_queries_use_current_general_settings(monkeypatch, settings):
    """查询地址和集群标签 key 每次查询时取(以前 import 时拷贝一份,改了配置要重启 master)"""
    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append((url, params['query']))
        response = Mock()
        response.json.return_value = {'data': {'result': []}}
        return response

    monkeypatch.setattr(prt.requests, 'get', fake_get)
    prt.get_metrics_data('prod', 'ns1')
    assert calls and {url for url, _ in calls} == {'http://vm.example.com:8428/api/v1/query'}
    assert all('k8s="prod"' in query and 'namespace="ns1"' in query for _, query in calls)

    settings.update(prom_url='http://monit:pw@vm-new:8428/select/0/prometheus', prom_k8s_tag_key='cluster')
    calls.clear()
    prt.get_metrics_data('prod', '')
    assert {url for url, _ in calls} == {'http://monit:pw@vm-new:8428/select/0/prometheus/api/v1/query'}
    assert all('cluster="prod"' in query and 'k8s=' not in query for _, query in calls)

    # 整合结果时也按新的 key 取集群名
    data = empty_metrics()
    data['pod_count'] = [{'metric': {'cluster': 'prod', 'namespace': 'ns1', 'deployment': 'a'}, 'value': [0, '2']}]
    assert prt.process_metrics_data(data) == [['prod', 'ns1', 'a', 2, 0, 0, 0, 0, 0, 0, 0, 0, '']]
