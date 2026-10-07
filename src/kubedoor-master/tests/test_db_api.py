"""func_manager/db_api.py 的单元测试:时间参数与时间展示。不需要真实 PG。"""
import pathlib
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from func_manager import db_api  # noqa: E402


def test_alert_filters_pass_start_time_as_datetime():
    params = []
    where = db_api._alert_filters({'env': ['prod'], 'startTime': '2026-10-04 00:00:00'}, params)
    assert where == ['env IN ($1)', 'start_time >= $2']
    assert params == ['prod', datetime(2026, 10, 4)]


def test_cell_renders_timestamptz_in_local_time():
    utc = datetime(2026, 10, 4, 3, 33, 34, tzinfo=timezone.utc)
    expected = datetime.fromtimestamp(utc.timestamp()).strftime('%Y-%m-%d %H:%M:%S')
    assert db_api._cell(utc) == expected


def test_cell_keeps_naive_datetime_as_is():
    assert db_api._cell(datetime(2026, 10, 4, 11, 33, 34)) == '2026-10-04 11:33:34'
