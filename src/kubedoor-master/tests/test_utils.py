"""utils.py 的单元测试:日期区间与 admission 查询。数据库调用全部 mock 掉,不需要真实 PG。"""
import asyncio
import pathlib
import sys
from datetime import datetime

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import utils  # noqa: E402


# ---------------------------------------------------------------------------
# day_range
# ---------------------------------------------------------------------------
def test_day_range_covers_whole_end_day():
    assert utils.day_range('2026-10-01', '2026-10-04') == (datetime(2026, 10, 1), datetime(2026, 10, 5))


def test_day_range_ignores_time_suffix():
    assert utils.day_range('2026-10-04 08:00:00', '2026-10-04 23:59:59') == (
        datetime(2026, 10, 4),
        datetime(2026, 10, 5),
    )


def test_day_range_rejects_bad_date():
    with pytest.raises(ValueError):
        utils.day_range('2026/10/04', '2026-10-04')


# ---------------------------------------------------------------------------
# get_deploy_admis_async
# ---------------------------------------------------------------------------
class FakeFetch:
    """替代 utils.pg_fetch:按预设返回行(或抛异常),并记录调用次数。"""

    def __init__(self, result):
        self.result = result
        self.calls = 0

    async def __call__(self, sql, *args, timeout=None):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture(autouse=True)
def clear_admis_cache():
    utils.invalidate_admis_cache()
    yield
    utils.invalidate_admis_cache()


def admis(monkeypatch, rows, deployment='web', include_jvm=False):
    fake = FakeFetch(rows)
    monkeypatch.setattr(utils, 'pg_fetch', fake)
    return asyncio.run(utils.get_deploy_admis_async('prod', 'ns1', deployment, include_jvm=include_jvm)), fake


def test_admis_namespace_not_controlled(monkeypatch):
    result, _ = admis(monkeypatch, [])
    assert result == [200, '非管控命名空间，直接放行']


def test_admis_controlled_service_returns_8_values(monkeypatch):
    # scheduler, nms_not_confirm, pod_count, pod_count_ai, pod_count_manual,
    # request_cpu_m, request_mem_mb, limit_cpu_m, limit_mem_mb, ctrl_deploy, 四项 JVM
    row = (True, False, 3, 4, 5, 100, 256, 200, 512, 'web', None, None, None, None)
    result, _ = admis(monkeypatch, [row])
    assert result == [3, 4, 5, 100, 256, 200, 512, True]


def test_admis_jvm_capability_preserves_partial_values_and_units(monkeypatch):
    row = (True, False, 3, 4, 5, 100, 256, 200, 512, 'web',
           0, 1073741824, 524288, None)
    result, _ = admis(monkeypatch, [row], include_jvm=True)
    assert result[:8] == [3, 4, 5, 100, 256, 200, 512, True]
    assert result[8] == {
        'jvm_xms_bytes': 0,
        'jvm_xmx_bytes': 1073741824,
        'jvm_xss_bytes': 524288,
        'jvm_max_metaspace_bytes': None,
    }


def test_admis_cache_does_not_send_jvm_to_legacy_agent(monkeypatch):
    row = (False, False, 3, -1, -1, 100, 256, 200, 512, 'web',
           1073741824, 1073741824, None, None)
    fake = FakeFetch([row])
    monkeypatch.setattr(utils, 'pg_fetch', fake)
    modern = asyncio.run(utils.get_deploy_admis_async('prod', 'ns1', 'web', include_jvm=True))
    legacy = asyncio.run(utils.get_deploy_admis_async('prod', 'ns1', 'web'))
    assert len(modern) == 9 and len(legacy) == 8
    assert modern[:8] == legacy
    assert fake.calls == 2
    assert asyncio.run(utils.get_deploy_admis_async('prod', 'ns1', 'web')) == legacy
    assert fake.calls == 2


def test_admis_new_service_allowed_when_confirm_disabled(monkeypatch):
    row = (False, True, None, None, None, None, None, None, None, None)
    result, _ = admis(monkeypatch, [row])
    assert result[0] == 200


def test_admis_new_service_rejected(monkeypatch):
    row = (False, False, None, None, None, None, None, None, None, None)
    result, _ = admis(monkeypatch, [row])
    assert result[0] == 404


def test_admis_result_is_cached(monkeypatch):
    fake = FakeFetch([])
    monkeypatch.setattr(utils, 'pg_fetch', fake)
    asyncio.run(utils.get_deploy_admis_async('prod', 'ns1', 'web'))
    asyncio.run(utils.get_deploy_admis_async('prod', 'ns1', 'web'))
    assert fake.calls == 1


def test_admis_db_error_returns_503_and_is_not_cached(monkeypatch):
    result, fake = admis(monkeypatch, asyncio.TimeoutError())
    assert result == [503, '查询数据库异常']
    asyncio.run(utils.get_deploy_admis_async('prod', 'ns1', 'web'))
    assert fake.calls == 2
