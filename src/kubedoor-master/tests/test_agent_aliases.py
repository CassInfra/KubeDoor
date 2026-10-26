"""集群别名:归一化键、清理与跨集群冲突校验(纯函数),/api/db/agent/aliases 接口,以及读取别名的 utils 函数。

数据库调用全部 mock 掉,不需要真实 PG。
"""
import asyncio
import json
import pathlib
import sys

import pytest
from aiohttp import web

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import utils  # noqa: E402
from func_manager import db_api  # noqa: E402
from func_manager.db_api import alias_key, clean_aliases  # noqa: E402


# ---------------------------------------------------------------- 归一化键(AI 侧用同一个规则)
@pytest.mark.parametrize('text,key', [
    ('cassmall-beta-kunlun', 'cassmallbetakunlun'),
    ('Cassmall Beta_Kunlun.v2', 'cassmallbetakunlunv2'),
    ('beta 昆仑', 'beta昆仑'),
    (' Beta-昆仑 ', 'beta昆仑'),
    ('beta\u3000\t昆仑', 'beta昆仑'),  # 全角空格、制表符也算空白
    ('--_. ', ''),
])
def test_alias_key(text, key):
    assert alias_key(text) == key


# ---------------------------------------------------------------- 清理与校验
OTHERS = {'cassmall-prod-kunlun': ['生产昆仑', 'Prod 昆仑'], 'cassmall-beta-kunlun': ['beta昆仑']}


def test_clean_strips_and_dedups_by_key_keeping_first_spelling():
    assert clean_aliases(
        'cassmall-beta-kunlun', ['  beta昆仑 ', 'Beta 昆仑', '', '   ', '昆仑测试', 'BETA-昆仑'], {},
    ) == ['beta昆仑', '昆仑测试']


def test_clean_drops_aliases_equal_to_own_name():
    assert clean_aliases(
        'cassmall-beta-kunlun', ['Cassmall Beta Kunlun', 'cassmall_beta.kunlun', 'beta昆仑'], {},
    ) == ['beta昆仑']


def test_clean_allows_empty_list():
    assert clean_aliases('prod', [], OTHERS) == []


@pytest.mark.parametrize('aliases', [None, 'beta昆仑', {'a': 1}, ['ok', 1], [None]])
def test_clean_rejects_non_string_list(aliases):
    with pytest.raises(ValueError, match='字符串数组'):
        clean_aliases('prod', aliases, {})


def test_clean_length_limit():
    assert clean_aliases('prod', ['a' * 64, f' {"中" * 64} '], {}) == ['a' * 64, '中' * 64]
    with pytest.raises(ValueError, match='超过 64 个字符'):
        clean_aliases('prod', ['a' * 65], {})


@pytest.mark.parametrize('bad', ['beta\n昆仑', 'beta\t昆仑', 'beta\x00', 'beta\u200b昆仑', '\ud800x'])
def test_clean_rejects_control_and_invisible_characters(bad):
    with pytest.raises(ValueError, match='第 2 个别名包含控制字符或不可见字符'):
        clean_aliases('prod', ['正常', bad], {})


def test_clean_rejects_alias_with_empty_key():
    with pytest.raises(ValueError, match='只有空格和'):
        clean_aliases('prod', ['-_.'], {})


def test_clean_count_limit_applies_after_dedup():
    twenty = [f'别名{i}' for i in range(20)]
    # 重复的和与集群名相同的去掉后不计数
    assert clean_aliases('prod', twenty + ['别名 0', 'PROD'], {}) == twenty
    with pytest.raises(ValueError, match='最多 20 个别名'):
        clean_aliases('prod', twenty + ['别名20'], {})


def test_clean_rejects_conflict_with_other_cluster_alias():
    with pytest.raises(ValueError) as error:
        clean_aliases('cassmall-test-kunlun', ['测试昆仑', 'BETA 昆仑'], OTHERS)
    assert str(error.value) == '别名「BETA 昆仑」和集群 cassmall-beta-kunlun 的别名「beta昆仑」重复'


def test_clean_rejects_conflict_with_other_cluster_name():
    with pytest.raises(ValueError) as error:
        clean_aliases('cassmall-test-kunlun', ['Cassmall Prod Kunlun'], OTHERS)
    assert str(error.value) == '别名「Cassmall Prod Kunlun」和集群 cassmall-prod-kunlun 的名称重复'


def test_clean_skips_own_entry_in_others():
    # others 里带上了自己时,自己现有的别名不算冲突
    assert clean_aliases('cassmall-beta-kunlun', ['beta昆仑', 'beta'], OTHERS) == ['beta昆仑', 'beta']


# ---------------------------------------------------------------- 接口
ROWS = [{'env': 'cassmall-beta-kunlun', 'aliases': ['beta昆仑']},
        {'env': 'cassmall-prod-kunlun', 'aliases': []}]


class Request:
    def __init__(self, data):
        self.data = data

    async def json(self):
        return self.data


def patch_db(monkeypatch, rows=ROWS):
    calls = []

    async def fetch(sql, *args):
        return rows

    async def execute(sql, *args):
        calls.append((sql, args))
        return 'UPDATE 1'

    monkeypatch.setattr(db_api, 'pg_fetch', fetch)
    monkeypatch.setattr(db_api, 'pg_execute', execute)
    return calls


def call(data):
    return asyncio.run(db_api.agent_update_aliases(Request(data)))


def rejected(data):
    with pytest.raises(web.HTTPBadRequest) as error:
        call(data)
    assert error.value.content_type == 'application/json'
    body = json.loads(error.value.text)
    assert body['success'] is False
    return body['message']


def test_update_saves_cleaned_list(monkeypatch):
    calls = patch_db(monkeypatch)
    response = call({'env': 'cassmall-prod-kunlun', 'aliases': [' 生产昆仑 ', '生产 昆仑', 'cassmall prod kunlun']})
    assert json.loads(response.text) == {'success': True, 'msg': '更新成功', 'aliases': ['生产昆仑']}
    assert calls == [('UPDATE k8s_agent_status SET aliases=$1 WHERE env=$2', (['生产昆仑'], 'cassmall-prod-kunlun'))]


def test_update_keeps_own_aliases_and_can_clear(monkeypatch):
    calls = patch_db(monkeypatch)
    call({'env': 'cassmall-beta-kunlun', 'aliases': ['beta昆仑']})  # 自己原有的别名不算冲突
    call({'env': 'cassmall-beta-kunlun', 'aliases': []})
    assert [args for _, args in calls] == [(['beta昆仑'], 'cassmall-beta-kunlun'), ([], 'cassmall-beta-kunlun')]


def test_update_rejects_conflict_without_writing(monkeypatch):
    calls = patch_db(monkeypatch)
    assert rejected({'env': 'cassmall-prod-kunlun', 'aliases': ['Beta 昆仑']}) == \
        '别名「Beta 昆仑」和集群 cassmall-beta-kunlun 的别名「beta昆仑」重复'
    assert calls == []


@pytest.mark.parametrize('data,message', [
    ({'env': 'unknown', 'aliases': []}, 'K8S 集群 unknown 不存在'),
    ({'aliases': ['x']}, 'env 不能为空'),
    ({'env': '', 'aliases': ['x']}, 'env 不能为空'),
    (['not', 'an', 'object'], 'env 不能为空'),
    ({'env': 'cassmall-prod-kunlun'}, 'aliases 必须是字符串数组'),
    ({'env': 'cassmall-prod-kunlun', 'aliases': ['a' * 65]}, '超过 64 个字符'),
])
def test_update_rejects_bad_requests(monkeypatch, data, message):
    calls = patch_db(monkeypatch)
    assert message in rejected(data)
    assert calls == []


# ---------------------------------------------------------------- 读取(/api/agent_status、AI bootstrap)
def test_agent_info_and_get_k8s_aliases_return_aliases_as_list(monkeypatch):
    rows = [('prod', True, '10:00:00-11:30:00', False, '', False, False, ['生产']),
            ('staging', False, '', False, '', False, False, [])]

    async def fetch(sql, *args):
        assert 'aliases' in sql
        return rows if 'collect' in sql else [(row[0], row[7]) for row in rows]

    monkeypatch.setattr(utils, 'pg_fetch', fetch)
    info = asyncio.run(utils.agent_info())
    assert info['prod']['aliases'] == ['生产'] and info['staging']['aliases'] == []
    assert asyncio.run(utils.get_k8s_aliases()) == {'prod': ['生产'], 'staging': []}
