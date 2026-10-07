"""prom_real_time_data.process_metrics_data 的单元测试。"""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import prom_real_time_data as prt  # noqa: E402


@pytest.fixture(autouse=True)
def tag_key(monkeypatch):
    monkeypatch.setattr(prt, 'PROM_K8S_TAG_KEY', 'k8s')


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
