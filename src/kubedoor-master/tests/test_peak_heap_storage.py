"""P95 heap/G1 Eden observations stay optional, preserve API positions and never control JVM args."""

import asyncio
import copy
import json
import pathlib
import re
import sys
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import utils
from func_manager import db_api


PEAK_END = datetime(2026, 10, 4, 11, 30)


def selected_peak_row(heap=72.5, g1e=67.25):
    # The first 13 columns retain the historical peak-day selection contract.
    return (PEAK_END, 'prod', 'team', 'service', 3, 20.0, 40.0,
            100.0, 128.0, 2000.0, 2048.0, 0.25, 256.75, heap, g1e)


def series(value, owner='service-rs'):
    return {
        'metric': {utils.PROM_K8S_TAG_KEY: 'prod', 'namespace': 'team', 'owner_name': owner},
        'value': [PEAK_END.timestamp(), value],
    }


def heap_response(monkeypatch, result):
    response = Mock()
    response.json.return_value = {'status': 'success', 'data': {'result': result}}
    request = Mock(return_value=response)
    monkeypatch.setattr(utils.requests, 'request', request)
    return request


def workloads():
    return {'prod@team@service-rs': ['cpu-memory-result'], 'prod@team@no-jvm-rs': ['other-result']}


@pytest.mark.parametrize('value,expected', [
    ('72.5', 72.5), ('125.5', 125.5), ('0', 0.0), (None, None), ('NaN', None),
    ('+Inf', None), ('-Inf', None), ('-1', None), ('bad', None), (True, None),
])
@pytest.mark.parametrize('metric', ['heap_usage_percent', 'g1e_usage_percent'])
def test_optional_jvm_query_normalizes_missing_and_invalid_observations(monkeypatch, value, expected, metric):
    request = heap_response(monkeypatch, [series(value)])
    original = workloads()
    result = utils.get_prom_data(metric, utils.PROM_K8S_TAG_KEY, 'prod', PEAK_END, '1h30m', original)
    assert result is original
    assert result['prod@team@service-rs'] == ['cpu-memory-result', expected]
    assert result['prod@team@no-jvm-rs'] == ['other-result', None]
    assert request.call_args.kwargs['params']['time'] == PEAK_END.timestamp()
    assert '1h30m' in request.call_args.kwargs['params']['query']


@pytest.mark.parametrize('metric', ['heap_usage_percent', 'g1e_usage_percent'])
def test_empty_jvm_series_preserves_all_workloads_as_null(monkeypatch, metric):
    heap_response(monkeypatch, [])
    assert utils.get_prom_data(metric, 'k8s', 'prod', PEAK_END, '1h', workloads()) == {
        'prod@team@service-rs': ['cpu-memory-result', None],
        'prod@team@no-jvm-rs': ['other-result', None],
    }


@pytest.mark.parametrize('failure', ['timeout', 'http', 'prometheus', 'json', 'malformed'])
@pytest.mark.parametrize('metric', ['heap_usage_percent', 'g1e_usage_percent'])
def test_optional_jvm_failure_keeps_existing_cpu_memory_results(monkeypatch, failure, metric):
    response = Mock()
    response.json.return_value = {'status': 'error', 'error': 'query failed'}
    request = Mock(return_value=response)
    if failure == 'timeout':
        request.side_effect = requests.exceptions.Timeout('offline')
    elif failure == 'http':
        response.raise_for_status.side_effect = requests.exceptions.HTTPError('502')
    elif failure == 'json':
        response.json.side_effect = ValueError('invalid JSON')
    elif failure == 'malformed':
        response.json.return_value = None
    monkeypatch.setattr(utils.requests, 'request', request)
    original = workloads()
    prefix = copy.deepcopy(original)
    result = utils.get_prom_data(metric, 'k8s', 'prod', PEAK_END, '1h', original)
    assert set(result) == set(prefix)
    for key, row in result.items():
        assert row == prefix[key] + [None]


@pytest.mark.parametrize('failed_metric', ['heap_usage_percent', 'g1e_usage_percent'])
def test_each_optional_failure_keeps_the_other_successful_jvm_observation(monkeypatch, failed_metric):
    responses = []
    for metric, value in [('heap_usage_percent', 72.5), ('g1e_usage_percent', 67.25)]:
        if metric == failed_metric:
            responses.append(requests.exceptions.Timeout('offline'))
        else:
            response = Mock()
            response.json.return_value = {'status': 'success', 'data': {'result': [series(value)]}}
            responses.append(response)
    request = Mock(side_effect=responses)
    monkeypatch.setattr(utils.requests, 'request', request)
    rows = workloads()
    for metric in ('heap_usage_percent', 'g1e_usage_percent'):
        rows = utils.get_prom_data(metric, 'k8s', 'prod', PEAK_END, '1h', rows)
    expected = [None, 67.25] if failed_metric == 'heap_usage_percent' else [72.5, None]
    assert rows['prod@team@service-rs'] == ['cpu-memory-result', *expected]
    assert rows['prod@team@no-jvm-rs'] == ['other-result', None, None]
    assert request.call_count == 2


def test_merged_peak_row_preserves_all_16_legacy_indexes(monkeypatch):
    base = [PEAK_END, 'prod', 'team', 'service', 3]
    values = {
        'core_usage': 0.25, 'core_usage_percent': 20.0, 'wss_usage_MB': 256.75,
        'wss_usage_percent': 40.0, 'limit_core': 2000.0, 'limit_mem_MB': 2048.0,
        'request_core': 100.0, 'request_mem_MB': 128.0, 'heap_usage_percent': 72.5,
        'g1e_usage_percent': 67.25,
    }

    def fetch(metric, env_key, env, end, duration, existing=None):
        if metric == 'pod_num':
            return {'prod@team@service-rs': list(base)}
        for row in existing.values():
            row.append(values[metric])
        return existing

    monkeypatch.setattr(utils, 'get_prom_data', fetch)
    rows = utils.merged_dict('k8s', 'prod', '1h30m', PEAK_END)
    assert rows == [base + [0.25, 20.0, 256.75, 40.0, 2000.0, 2048.0, 100.0, 128.0, -1, -1, -1, 72.5, 67.25]]
    assert len(rows[0]) == 18


@pytest.mark.parametrize('heap,expected', [(72.5, 72.5), (125.5, 125.5), (None, None), (float('nan'), None), (-1, None)])
def test_peak_copy_appends_heap_without_moving_legacy_columns(monkeypatch, heap, expected):
    record = [PEAK_END, 'prod', 'team', 'service', 3, 0.25, 20.0, 256.75, 40.0,
              2000.0, 2048.0, 100.0, 128.0, -1, -1, -1, heap]
    write = AsyncMock()
    monkeypatch.setattr(utils.db, 'pg_copy_records', write)
    asyncio.run(utils.metrics_to_pg([record]))
    table, batch, columns = write.call_args.args
    assert table == 'k8s_resources'
    assert batch[0][:16] == tuple(record[:16])
    assert columns[:16] == db_api.RESOURCES_COLUMNS[:16]
    assert columns[16] == 'p95_pod_heap_pct'
    assert batch[0][16] == expected
    assert columns[17] == 'p95_pod_g1e_pct'
    assert batch[0][17] is None  # A legacy 17-column caller has no Eden observation.


def test_legacy_16_column_copy_is_supported_as_unknown_heap(monkeypatch):
    record = [PEAK_END, 'prod', 'team', 'service', 3, 0.25, 20.0, 256.75, 40.0,
              2000.0, 2048.0, 100.0, 128.0, -1, -1, -1]
    write = AsyncMock()
    monkeypatch.setattr(utils.db, 'pg_copy_records', write)
    asyncio.run(utils.metrics_to_pg([record]))
    assert write.call_args.args[1] == [tuple(record + [None, None])]


@pytest.mark.parametrize('g1e,expected', [(67.25, 67.25), (125.5, 125.5), (None, None), (float('inf'), None), (float('nan'), None), (-1, None)])
def test_18_column_copy_preserves_heap_and_normalizes_g1_eden(monkeypatch, g1e, expected):
    record = [PEAK_END, 'prod', 'team', 'service', 3, 0.25, 20.0, 256.75, 40.0,
              2000.0, 2048.0, 100.0, 128.0, -1, -1, -1, 72.5, g1e]
    write = AsyncMock()
    monkeypatch.setattr(utils.db, 'pg_copy_records', write)
    asyncio.run(utils.metrics_to_pg([record]))
    table, batch, columns = write.call_args.args
    assert table == 'k8s_resources'
    assert columns[16:] == ['p95_pod_heap_pct', 'p95_pod_g1e_pct']
    assert batch[0][:17] == tuple(record[:17])
    assert batch[0][17] == expected


def test_busiest_day_selection_includes_heap_from_same_selected_day(monkeypatch):
    fetch = AsyncMock(return_value=[selected_peak_row()])
    monkeypatch.setattr(utils, 'pg_fetch', fetch)
    assert asyncio.run(utils.get_list_from_resources('prod')) == [selected_peak_row()]
    sql = fetch.call_args.args[0]
    selected_columns = sql.split('from k8s_resources')[0].lower()
    assert 'p95_pod_heap_pct' in selected_columns
    assert 'p95_pod_g1e_pct' in selected_columns
    assert 'p80_pod_heap_mb' not in sql
    assert 'p90_pod_heap_pct' not in sql
    assert 'order by SUM(pod_count * p95_pod_load) desc' in sql
    assert fetch.call_args.args[1:] == ('prod',)


def test_initial_control_copy_persists_observation_without_adding_jvm_controls(monkeypatch):
    write = AsyncMock()
    monkeypatch.setattr(utils.db, 'pg_copy_records', write)
    monkeypatch.setattr(utils, 'invalidate_admis_cache', Mock())
    assert asyncio.run(utils.init_control_data([selected_peak_row()])) is True
    table, batch, columns = write.call_args.args
    values = dict(zip(columns, batch[0]))
    assert table == 'k8s_res_control'
    assert values['p95_pod_heap_pct'] == 72.5
    assert values['p95_pod_g1e_pct'] == 67.25
    assert values['limit_cpu_m'] == 2000 and values['limit_mem_mb'] == 2048
    assert values['pod_count_manual'] == -1
    assert not any(field in columns for field in utils.JVM_FIELDS)


@pytest.mark.parametrize('heap,g1e', [(72.5, 67.25), (72.5, None), (None, 67.25), (None, None)])
def test_existing_control_update_tracks_peak_observation_but_keeps_jvm_and_limits(monkeypatch, heap, g1e):
    execute = AsyncMock()
    monkeypatch.setattr(utils, 'pg_fetchval', AsyncMock(return_value=1))
    monkeypatch.setattr(utils, 'pg_execute', execute)
    monkeypatch.setattr(utils, 'invalidate_admis_cache', Mock())
    assert asyncio.run(utils.update_control_data([selected_peak_row(heap, g1e)])) is True
    sql, *values = execute.call_args.args
    assert 'p95_pod_heap_pct = $7' in sql
    assert values[6] == heap
    assert 'p95_pod_g1e_pct = $8' in sql
    assert values[7] == g1e
    assert values[8:] == ['prod', 'team', 'service']
    assert values[:6] == [PEAK_END, 3, 20.0, 40.0, 250, 256]
    assert 'limit_cpu_m' not in sql and 'limit_mem_mb' not in sql
    assert not any(field in sql for field in utils.JVM_FIELDS)


def test_new_service_in_update_uses_selected_peak_heap(monkeypatch):
    write = AsyncMock()
    monkeypatch.setattr(utils, 'pg_fetchval', AsyncMock(return_value=None))
    monkeypatch.setattr(utils.db, 'pg_copy_records', write)
    monkeypatch.setattr(utils, 'send_msg', Mock())
    monkeypatch.setattr(utils, 'invalidate_admis_cache', Mock())
    assert asyncio.run(utils.update_control_data([selected_peak_row()])) is True
    _, batch, columns = write.call_args.args
    assert dict(zip(columns, batch[0]))['p95_pod_heap_pct'] == 72.5
    assert dict(zip(columns, batch[0]))['p95_pod_g1e_pct'] == 67.25


def test_old_selected_peak_rows_initialize_unknown_heap():
    record = utils.parse_insert_data(selected_peak_row()[:13])
    assert record[-1] is None
    assert record[-2] is None
    assert len(record) == len(utils._RES_CONTROL_COLUMNS)


def test_old_14_column_selected_peak_rows_preserve_heap_and_initialize_unknown_eden():
    record = utils.parse_insert_data(selected_peak_row()[:14])
    values = dict(zip(utils._RES_CONTROL_COLUMNS, record))
    assert values['p95_pod_heap_pct'] == 72.5
    assert values['p95_pod_g1e_pct'] is None


@pytest.mark.parametrize('length,expected_heap', [(13, None), (14, 72.5)])
def test_old_selected_peak_update_rows_clear_missing_eden_to_null(monkeypatch, length, expected_heap):
    execute = AsyncMock()
    monkeypatch.setattr(utils, 'pg_fetchval', AsyncMock(return_value=1))
    monkeypatch.setattr(utils, 'pg_execute', execute)
    monkeypatch.setattr(utils, 'invalidate_admis_cache', Mock())
    assert asyncio.run(utils.update_control_data([selected_peak_row()[:length]])) is True
    assert execute.call_args.args[7:9] == (expected_heap, None)


@pytest.mark.parametrize('handler,columns', [
    (db_api.res_list, db_api.RES_CONTROL_COLUMNS),
    (db_api.res_collection, db_api.RESOURCES_COLUMNS),
])
@pytest.mark.parametrize('heap,g1e', [(72.5, 67.25), (72.5, None), (None, 67.25), (None, None)])
def test_read_apis_include_heap_meta_and_keep_nullable_value(monkeypatch, handler, columns, heap, g1e):
    row = [None] * len(columns)
    row[columns.index('p95_pod_heap_pct')] = heap
    row[columns.index('p95_pod_g1e_pct')] = g1e
    fetch = AsyncMock(return_value=[row])
    monkeypatch.setattr(db_api, 'pg_fetch', fetch)
    response = asyncio.run(handler(SimpleNamespace(query={'env': 'prod', 'date': '2026-10-04'})))
    result = json.loads(response.body)
    indexes = {field['name']: index for index, field in enumerate(result['meta'])}
    assert result['data'][0][indexes['p95_pod_heap_pct']] == heap
    assert 'p95_pod_heap_pct' in fetch.call_args.args[0]
    assert result['data'][0][indexes['p95_pod_g1e_pct']] == g1e
    assert 'p95_pod_g1e_pct' in fetch.call_args.args[0]
    assert 'p80_pod_heap_mb' not in fetch.call_args.args[0]
    assert 'p80_pod_heap_mb' not in indexes
    assert 'p90_pod_heap_pct' not in fetch.call_args.args[0]
    assert 'p90_pod_heap_pct' not in indexes
    legacy_column = 'p95_pod_load' if handler == db_api.res_collection else 'p95_pod_cpu_pct'
    assert legacy_column in indexes


@pytest.mark.parametrize('handler', [db_api.res_add, db_api.res_edit])
def test_manual_forms_cannot_write_read_only_heap_observation(monkeypatch, handler):
    data = {
        'env': 'prod', 'namespace': 'team', 'deployment': 'service', 'pod_count_manual': 3,
        'limit_cpu_m': 2000, 'limit_mem_mb': 2048, 'request_cpu_m': 100,
        'request_mem_mb': 128, 'jvm_xss_bytes': 512 * 1024, 'p95_pod_heap_pct': 9999,
        'p80_pod_heap_mb': 1234,
        'p90_pod_heap_pct': 88.5,
        'p95_pod_g1e_pct': 96.5,
    }
    execute = AsyncMock()
    monkeypatch.setattr(db_api, 'pg_execute', execute)
    monkeypatch.setattr(db_api.utils, 'invalidate_admis_cache', Mock())
    asyncio.run(handler(SimpleNamespace(json=AsyncMock(return_value=data))))
    sql, *values = execute.call_args.args
    assert 'p95_pod_heap_pct' not in sql
    assert 'p80_pod_heap_mb' not in sql
    assert 'p90_pod_heap_pct' not in sql
    assert 'p95_pod_g1e_pct' not in sql and 96.5 not in values
    assert 9999 not in values
    assert 'jvm_xss_bytes' in sql and 512 * 1024 in values


@pytest.mark.parametrize('table', ['k8s_resources', 'k8s_res_control'])
@pytest.mark.parametrize('field', ['p95_pod_heap_pct', 'p95_pod_g1e_pct'])
def test_schema_upgrade_adds_p95_without_reusing_legacy_heap_observations(table, field):
    schema = (pathlib.Path(__file__).resolve().parent.parent / 'db.sql').read_text(encoding='utf-8')
    sql = '\n'.join(line.split('--', 1)[0] for line in schema.splitlines()).lower()
    create = re.search(rf'create table if not exists {table}\s*\((.*?)\);', sql, re.S).group(1)
    upgrade = re.search(rf'alter table {table}\s+(.*?);', sql, re.S).group(1)
    assert field in create
    assert f'add column if not exists {field} real' in upgrade
    # Old MiB/P90 columns may still exist, but no SQL reads, renames, drops,
    # copies or assigns their observations to the new P95 column.
    assert 'p80_pod_heap_mb' not in sql
    assert 'p90_pod_heap_pct' not in sql
    assert 'rename column' not in upgrade and 'drop column' not in upgrade
    declaration = next(line for line in create.splitlines() if field in line)
    assert 'not null' not in declaration and 'default' not in declaration
