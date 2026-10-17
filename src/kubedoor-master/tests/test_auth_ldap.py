"""LDAP 认证测试:用 ldap3 自带的 MOCK_SYNC 内存目录,不需要真实 LDAP 服务。"""
import functools
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

ldap3 = pytest.importorskip('ldap3')

from auth import crypto, ldap_auth  # noqa: E402

BASE = 'ou=people,dc=example,dc=com'
BIND_DN = 'cn=reader,dc=example,dc=com'


@pytest.fixture
def directory(monkeypatch):
    """一个预置了服务账号和两个用户的内存目录;ldap_auth 建的所有连接都走 MOCK_SYNC。"""
    server = ldap3.Server('mock-ldap', get_info=ldap3.OFFLINE_SLAPD_2_4)
    seed = ldap3.Connection(server, user=BIND_DN, password='reader-pw', client_strategy=ldap3.MOCK_SYNC)
    seed.strategy.add_entry(BIND_DN, {'userPassword': 'reader-pw', 'sn': 'reader'})
    seed.strategy.add_entry(f'uid=alice,{BASE}', {
        'objectClass': ['inetOrgPerson'], 'uid': 'alice', 'cn': 'Alice Wang', 'sn': 'Wang',
        'mail': 'alice@example.com', 'userPassword': 'alice-pw'})
    seed.strategy.add_entry(f'uid=bob,{BASE}', {
        'objectClass': ['inetOrgPerson'], 'uid': 'bob', 'cn': 'Bob', 'sn': 'B', 'userPassword': 'bob-pw'})
    monkeypatch.setattr(ldap_auth, '_server', lambda cfg: server)
    monkeypatch.setattr(ldap3, 'Connection', functools.partial(ldap3.Connection, client_strategy=ldap3.MOCK_SYNC))
    monkeypatch.setenv('AI_ENCRYPTION_KEY', '11' * 32)
    return server


def config(**overrides):
    raw = {'enabled': True, 'url': 'ldap://mock-ldap:389', 'bind_dn': BIND_DN, 'bind_password': 'reader-pw',
           'base_dn': BASE, 'user_filter': '(&(objectClass=inetOrgPerson)(uid={username}))', **overrides}
    return ldap_auth.normalize_config(raw)


def test_search_then_bind(directory):
    cfg = config()
    assert cfg['bind_password'].startswith('v1:')  # 绑定密码加密入库
    info = ldap_auth.authenticate(cfg, 'alice', 'alice-pw')
    assert info == {'dn': f'uid=alice,{BASE}', 'username': 'alice', 'display_name': 'Alice Wang',
                    'email': 'alice@example.com'}
    assert ldap_auth.authenticate(cfg, 'alice', 'wrong') is None
    assert ldap_auth.authenticate(cfg, 'nobody', 'alice-pw') is None


def test_empty_password_is_rejected_before_binding(directory, monkeypatch):
    called = []
    monkeypatch.setattr(ldap_auth, '_open', lambda *a, **k: called.append(a))
    assert ldap_auth.authenticate(config(), 'alice', '') is None
    assert called == []


def test_filter_injection_is_escaped(directory):
    cfg = config()
    assert ldap_auth.authenticate(cfg, '*', 'alice-pw') is None
    assert ldap_auth.authenticate(cfg, 'alice)(uid=*', 'alice-pw') is None


def test_ambiguous_filter_is_an_error(directory):
    cfg = config(user_filter='(|(uid={username})(objectClass=inetOrgPerson))')
    with pytest.raises(ldap_auth.LdapError) as error:
        ldap_auth.authenticate(cfg, 'alice', 'alice-pw')
    assert error.value.reason == 'ambiguous'


def test_bad_service_account_is_a_config_error(directory):
    with pytest.raises(ldap_auth.LdapError) as error:
        ldap_auth.authenticate(config(bind_password='nope'), 'alice', 'alice-pw')
    assert error.value.reason == 'config'


def test_direct_bind_template(directory):
    cfg = config(bind_dn='', bind_password='', user_dn_template=f'uid={{username}},{BASE}')
    info = ldap_auth.authenticate(cfg, 'bob', 'bob-pw')
    assert info['dn'] == f'uid=bob,{BASE}'
    assert ldap_auth.authenticate(cfg, 'bob', 'wrong') is None


def test_connection_test_reports_each_step(directory):
    result = ldap_auth.test_connection(config(), 'alice', 'alice-pw')
    assert result['success'] and [s['name'] for s in result['steps']] == ['连接服务器', '服务账号绑定', '搜索用户', '验证用户口令']
    failed = ldap_auth.test_connection(config(), 'ghost')
    assert not failed['success'] and failed['steps'][-1]['name'] == '搜索用户'


@pytest.mark.parametrize('overrides,message', [
    ({'url': 'http://x'}, 'LDAP 地址'),
    ({'url': 'ldaps://x:636', 'start_tls': True}, 'StartTLS'),
    ({'user_filter': 'uid={username}'}, '括号'),
    ({'user_filter': '(uid=alice)'}, '{username}'),
    ({'default_role': 'admin'}, '默认全局权限'),
    ({'base_dn': ''}, 'Base DN'),
    ({'timeout': 99}, '超时'),
])
def test_config_validation(overrides, message):
    with pytest.raises(ValueError) as error:
        config(**overrides)
    assert message in str(error.value)


def test_bind_password_kept_cleared_and_hidden(monkeypatch):
    monkeypatch.setenv('AI_ENCRYPTION_KEY', '22' * 32)
    saved = config()
    kept = ldap_auth.normalize_config({'url': 'ldap://other:389'}, saved)
    assert kept['bind_password'] == saved['bind_password'] and kept['url'] == 'ldap://other:389'
    cleared = ldap_auth.normalize_config({'bind_password': ''}, saved)
    assert cleared['bind_password'] == ''
    public = ldap_auth.public_config(saved)
    assert 'bind_password' not in public and public['has_bind_password'] is True


def test_secret_encryption(monkeypatch):
    monkeypatch.setenv('AI_ENCRYPTION_KEY', '33' * 32)
    sealed = crypto.encrypt_secret('s3cret')
    assert sealed.startswith('v1:') and 's3cret' not in sealed
    assert crypto.decrypt_secret(sealed) == 's3cret'
    monkeypatch.setenv('AI_ENCRYPTION_KEY', '44' * 32)
    with pytest.raises(ValueError):
        crypto.decrypt_secret(sealed)
    monkeypatch.delenv('AI_ENCRYPTION_KEY')
    assert crypto.decrypt_secret(crypto.encrypt_secret('plain-one')) == 'plain-one'
