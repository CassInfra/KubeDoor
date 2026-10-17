"""LDAP 认证(ldap3,同步阻塞调用,调用方用 asyncio.to_thread 放进线程池)。

流程(先搜后绑,业界通用做法):
  1. 用服务账号(bind_dn,留空则匿名)绑定,按 user_filter 搜到唯一的用户 DN;
  2. 用该 DN 和用户输入的口令再绑定一次,成功即认证通过。
  也支持「直接绑定」:配置 user_dn_template(如 uid={username},ou=people,dc=example,dc=com,
  AD 可用 {username}@corp.example.com),不需要服务账号。

安全要点:
  - 拒绝空口令:很多目录服务把「有 DN、无口令」的绑定当成匿名绑定并返回成功(RFC 4513 5.1.2),
    不拦截就能无口令冒充任何用户(Spring LDAP CVE-2026-41720 就是这个问题);
  - 用户名代入过滤器前做 LDAP 过滤器转义,且登录接口已限制用户名字符集,杜绝注入;
  - 搜索结果必须唯一,多条视为配置问题拒绝登录;
  - 连接、读取都带超时;支持 LDAPS / StartTLS,默认校验证书。
"""

import re
import ssl

from loguru import logger

from .crypto import decrypt_secret, encrypt_secret

DEFAULTS = {
    'enabled': False,
    'url': '',
    'start_tls': False,
    'tls_verify': True,
    'ca_cert': '',
    'bind_dn': '',
    'bind_password': '',
    'base_dn': '',
    'user_filter': '(&(objectClass=inetOrgPerson)(uid={username}))',
    'user_dn_template': '',
    'attr_username': 'uid',
    'attr_display_name': 'cn',
    'attr_email': 'mail',
    'auto_create': True,
    'default_role': 'read',
    'timeout': 5,
}

_URL = re.compile(r'ldaps?://[A-Za-z0-9.\-_\[\]:]+(:\d{1,5})?/?')
_ATTR = re.compile(r'[A-Za-z][A-Za-z0-9-]{0,63}')


def split_urls(value):
    return [u for u in re.split(r'[\s,]+', value or '') if u]


def normalize_config(raw, existing=None):
    """校验并规范化前端提交的配置;bind_password 为 None 表示保持原值。非法抛 ValueError。"""
    existing = {**DEFAULTS, **(existing or {})}
    if not isinstance(raw, dict):
        raise ValueError('配置格式不正确')
    cfg = dict(DEFAULTS)
    for key in DEFAULTS:
        if key == 'bind_password':
            continue
        value = raw.get(key, existing.get(key))
        if isinstance(DEFAULTS[key], bool):
            cfg[key] = bool(value)
        elif isinstance(DEFAULTS[key], int):
            try:
                cfg[key] = int(value)
            except (TypeError, ValueError):
                raise ValueError(f'{key} 必须是整数')
        else:
            cfg[key] = '' if value is None else str(value).strip()

    urls = split_urls(cfg['url'])
    for url in urls:
        if not _URL.fullmatch(url):
            raise ValueError(f'LDAP 地址格式不正确:{url}(例如 ldap://ldap.example.com:389 或 ldaps://ldap.example.com:636)')
    cfg['url'] = ' '.join(urls)
    if cfg['start_tls'] and any(u.startswith('ldaps://') for u in urls):
        raise ValueError('ldaps:// 已经是加密连接,不要再勾选 StartTLS')
    if not 1 <= cfg['timeout'] <= 30:
        raise ValueError('超时时间取 1~30 秒')
    if cfg['default_role'] not in ('read', 'none'):
        raise ValueError('新 LDAP 用户的默认全局权限只能是「只读」或「无」')
    for key in ('attr_username', 'attr_display_name', 'attr_email'):
        if cfg[key] and not _ATTR.fullmatch(cfg[key]):
            raise ValueError(f'属性名不合法:{cfg[key]}')
    if cfg['user_dn_template'] and '{username}' not in cfg['user_dn_template']:
        raise ValueError('用户 DN 模板里必须包含 {username}')
    if cfg['user_filter']:
        if '{username}' not in cfg['user_filter'] or not (cfg['user_filter'].startswith('(') and cfg['user_filter'].endswith(')')):
            raise ValueError('用户过滤条件要用括号包起来,并包含 {username},例如 (&(objectClass=person)(uid={username}))')
    if cfg['ca_cert'] and ('BEGIN CERTIFICATE' not in cfg['ca_cert'] or len(cfg['ca_cert']) > 65536):
        raise ValueError('CA 证书需要是 PEM 格式')
    if cfg['enabled']:
        if not urls:
            raise ValueError('启用 LDAP 时必须填写服务器地址')
        if not cfg['user_dn_template'] and not (cfg['base_dn'] and cfg['user_filter']):
            raise ValueError('请填写搜索起点(Base DN)和用户过滤条件,或者填写用户 DN 模板')

    password = raw.get('bind_password')
    if password is None:
        cfg['bind_password'] = existing.get('bind_password', '')
    elif password == '':
        cfg['bind_password'] = ''
    else:
        cfg['bind_password'] = encrypt_secret(str(password))
    return cfg


def public_config(cfg):
    cfg = {**DEFAULTS, **(cfg or {})}
    data = {k: v for k, v in cfg.items() if k != 'bind_password'}
    data['has_bind_password'] = bool(cfg.get('bind_password'))
    return data


class LdapError(Exception):
    def __init__(self, message, reason='unavailable'):
        super().__init__(message)
        self.reason = reason


def _first(value):
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ''
    if isinstance(value, bytes):
        value = value.decode('utf-8', 'replace')
    return '' if value is None else str(value)


def _server(cfg):
    from ldap3 import FIRST, NONE, Server, ServerPool, Tls

    tls = Tls(
        validate=ssl.CERT_REQUIRED if cfg['tls_verify'] else ssl.CERT_NONE,
        ca_certs_data=cfg['ca_cert'] or None,
    )
    servers = [Server(url, tls=tls, connect_timeout=cfg['timeout'], get_info=NONE) for url in split_urls(cfg['url'])]
    if not servers:
        raise LdapError('没有配置 LDAP 服务器地址', 'config')
    return servers[0] if len(servers) == 1 else ServerPool(servers, FIRST, active=1, exhaust=False)


def _open(server, cfg, user=None, password=None):
    """建立连接(可选 StartTLS)并绑定。返回 (连接, 是否绑定成功)。连不上抛 LdapError。"""
    from ldap3 import ANONYMOUS, SIMPLE, Connection
    from ldap3.core.exceptions import LDAPException

    conn = Connection(
        server,
        user=user or None,
        password=password if user else None,
        authentication=SIMPLE if user else ANONYMOUS,
        read_only=True,
        raise_exceptions=False,
        auto_referrals=False,
        receive_timeout=cfg['timeout'],
    )
    try:
        conn.open()
        if cfg['start_tls'] and not conn.start_tls():
            raise LdapError(f"StartTLS 失败:{conn.result.get('description') if conn.result else ''}")
        return conn, bool(conn.bind())
    except LdapError:
        conn.unbind()
        raise
    except LDAPException as exc:
        conn.unbind()
        raise LdapError(f'连接 LDAP 服务器失败:{type(exc).__name__}: {exc}') from exc
    except (OSError, ssl.SSLError) as exc:
        conn.unbind()
        raise LdapError(f'连接 LDAP 服务器失败:{exc}') from exc


def _result_text(conn):
    result = conn.result or {}
    return result.get('description') or result.get('message') or '未知错误'


def _search_user(conn, cfg, username):
    from ldap3 import SUBTREE
    from ldap3.utils.conv import escape_filter_chars

    search_filter = cfg['user_filter'].replace('{username}', escape_filter_chars(username))
    attributes = [a for a in (cfg['attr_username'], cfg['attr_display_name'], cfg['attr_email']) if a]
    conn.search(cfg['base_dn'], search_filter, search_scope=SUBTREE, attributes=attributes,
                size_limit=2, time_limit=cfg['timeout'])
    entries = [e for e in (conn.response or []) if e.get('type') == 'searchResEntry']
    if not entries:
        description = (conn.result or {}).get('description')
        if description not in (None, 'success', 'noSuchObject'):
            raise LdapError(f'搜索用户失败:{description}')
        return None
    if len(entries) > 1:
        raise LdapError(f'过滤条件匹配到多个用户,请收窄 user_filter:{search_filter}', 'ambiguous')
    return entries[0]


def _user_info(entry, cfg, username):
    attributes = entry.get('attributes') or {}
    return {
        'dn': entry.get('dn', ''),
        'username': _first(attributes.get(cfg['attr_username'])) or username,
        'display_name': _first(attributes.get(cfg['attr_display_name'])) if cfg['attr_display_name'] else '',
        'email': _first(attributes.get(cfg['attr_email'])) if cfg['attr_email'] else '',
    }


def runtime_config(cfg):
    """补默认值并解密绑定密码;解不开抛 LdapError。"""
    cfg = {**DEFAULTS, **(cfg or {})}
    try:
        cfg['bind_password'] = decrypt_secret(cfg.get('bind_password') or '')
    except ValueError as exc:
        raise LdapError(f'LDAP 绑定密码无法解密({exc}),请在 LDAP 设置里重新填写', 'config') from exc
    return cfg


def authenticate(stored_cfg, username, password):
    """认证 LDAP 用户。成功返回用户信息 dict;口令错误或用户不存在返回 None;服务异常抛 LdapError。"""
    if not password:
        return None  # 空口令一律拒绝,防止被目录服务当成匿名绑定
    cfg = runtime_config(stored_cfg)
    server = _server(cfg)

    if cfg['user_dn_template']:
        user_dn = cfg['user_dn_template'].replace('{username}', username)
        conn, ok = _open(server, cfg, user_dn, password)
        try:
            if not ok:
                return None
            info = {'dn': user_dn, 'username': username, 'display_name': '', 'email': ''}
            if cfg['base_dn'] and cfg['user_filter']:
                try:
                    entry = _search_user(conn, cfg, username)
                    if entry:
                        info = _user_info(entry, cfg, username)
                except LdapError as exc:
                    logger.warning(f'LDAP 直接绑定成功,但读取用户属性失败: {exc}')
            return info
        finally:
            conn.unbind()

    conn, ok = _open(server, cfg, cfg['bind_dn'], cfg['bind_password'])
    try:
        if not ok:
            raise LdapError(f'服务账号绑定失败:{_result_text(conn)}', 'config')
        entry = _search_user(conn, cfg, username)
    finally:
        conn.unbind()
    if entry is None:
        return None
    info = _user_info(entry, cfg, username)
    user_conn, ok = _open(server, cfg, info['dn'], password)
    user_conn.unbind()
    return info if ok else None


def test_connection(stored_cfg, username='', password=''):
    """配置页的「测试」:逐步检查连接、服务账号绑定、搜索用户、用户口令,返回每一步的结果。"""
    steps = []

    def step(name, ok, detail=''):
        steps.append({'name': name, 'ok': ok, 'detail': detail})
        return ok

    user = None
    try:
        cfg = runtime_config(stored_cfg)
        server = _server(cfg)
        if cfg['user_dn_template']:
            step('连接服务器', True, '直接绑定模式,在验证用户口令时一并检查')
        else:
            conn, ok = _open(server, cfg, cfg['bind_dn'], cfg['bind_password'])
            step('连接服务器', True, cfg['url'] + (' (StartTLS)' if cfg['start_tls'] else ''))
            try:
                who = cfg['bind_dn'] or '匿名'
                if not step('服务账号绑定', ok, who if ok else f'{who}:{_result_text(conn)}'):
                    return {'success': False, 'steps': steps}
                if username:
                    entry = _search_user(conn, cfg, username)
                    if not step('搜索用户', entry is not None, entry.get('dn') if entry else f'没有找到用户 {username}'):
                        return {'success': False, 'steps': steps}
                    user = _user_info(entry, cfg, username)
            finally:
                conn.unbind()
        if username and password:
            info = authenticate(stored_cfg, username, password)
            if not step('验证用户口令', info is not None, '口令正确' if info else '口令错误或用户不存在'):
                return {'success': False, 'steps': steps, 'user': user}
            user = info
        elif username and cfg['user_dn_template']:
            step('验证用户口令', False, '直接绑定模式需要填写测试口令')
            return {'success': False, 'steps': steps}
    except LdapError as exc:
        step('出错', False, str(exc))
        return {'success': False, 'steps': steps, 'user': user}
    return {'success': True, 'steps': steps, 'user': user}
