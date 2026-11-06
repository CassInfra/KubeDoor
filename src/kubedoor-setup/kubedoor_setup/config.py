"""Strict structured wizard configuration; no user input is executed as shell code."""
from __future__ import annotations

import copy
import importlib.util
import ipaddress
import os
from pathlib import Path
import re
import secrets
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from .errors import SetupError

METRICS_TYPES = ('Victoria-Metrics-Single', 'Victoria-Metrics-Cluster')
# 远程读写地址里已保存的密码回传页面时显示成它；提交回来时换成上面填写或已保存的密码
PASSWORD_MASK = '******'
# 告警组件：一个开关，一起装或一起不装；Grafana 看板必装，不在向导里选择
ALERTING_SERVICES = ('alarm', 'vmalert', 'alertmanager')


def default_private():
    return {
        'config': {
            'postgres': {'host': '', 'port': 5432, 'database': 'kubedoor', 'user': 'kubedoor', 'password': '', 'sslmode': 'disable'},
            'metrics': {'type': 'Victoria-Metrics-Single', 'scheme': 'http', 'host': '', 'tenant': '0', 'user': '', 'password': '',
                        'read_url': '', 'write_url': '', 'tag_key': 'origin_prometheus'},
            'admin': {'username': 'kubedoor', 'password': ''},
            'public_url': '',
            'components': {'alerting': True},
        },
        'secrets': {},
    }


def alerting_choice(components):
    """是否安装告警组件；兼容旧版向导逐项保存的选择（选了 vmalert 或 Alertmanager 就算安装）。"""
    if not isinstance(components, dict):
        return True
    if isinstance(components.get('alerting'), bool):
        return components['alerting']
    return components.get('vmalert') is not False or components.get('alertmanager') is not False


def selected_components(components):
    """本次安装的可选服务：Grafana 看板固定安装，kubedoor-alarm、vmalert、Alertmanager 跟随告警组件开关。"""
    alerting = alerting_choice(components)
    return {'grafana': True, **{name: alerting for name in ALERTING_SERVICES}}


def ensure_secrets(private):
    result = copy.deepcopy(private)
    values = result.setdefault('secrets', {})
    for key in ('internal_token', 'encryption_key'):
        values.setdefault(key, secrets.token_hex(32))
    for key in ('agent_password', 'grafana_password'):
        values.setdefault(key, secrets.token_urlsafe(32))
    return result


def text(value, title, *, required=True, maximum=512, trim=True):
    if not isinstance(value, str):
        raise SetupError(f'{title}格式不正确', 422)
    if trim:
        value = value.strip()
    if len(value) > maximum or any(ord(char) < 32 for char in value):
        raise SetupError(f'{title}过长或包含控制字符', 422)
    if required and not value:
        raise SetupError(f'请填写{title}', 422)
    return value


def is_local_host(host):
    value = host.strip('[]').rstrip('.').lower()
    if value == 'localhost' or value.endswith('.localhost'):
        return True
    try:
        address = ipaddress.ip_address(value)
        return address.is_loopback or address.is_unspecified or address.is_link_local or address.is_multicast
    except ValueError:
        return False


def clean_url(value, title, *, base=False, required=True):
    value = text(value, title, required=required, maximum=2048)
    if not value:
        return ''
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise SetupError(f'{title}格式不正确', 422) from None
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or
            parsed.username is not None or parsed.password is not None or
            parsed.query or parsed.fragment or '\\' in value or
            any(char.isspace() for char in value)):
        raise SetupError(f'{title}需为 HTTP/HTTPS 地址，不含账号、查询参数或锚点', 422)
    if port is not None and not 1 <= port <= 65535:
        raise SetupError(f'{title}端口无效', 422)
    if is_local_host(parsed.hostname):
        raise SetupError(f'{title}需使用目标集群可访问的地址，不能使用本机或特殊地址', 422)
    if base and parsed.path not in ('', '/'):
        raise SetupError(f'{title}只填写协议、主机和端口', 422)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip('/'), '', ''))


def preserved_password(raw, existing, title, *, required=True):
    value = raw.get('password', '')
    if value is None:
        raise SetupError(f'{title}格式不正确', 422)
    if value == '':
        value = existing.get('password', '')
    return text(value, title, required=required, maximum=1024, trim=False)


def postgres_config(raw, existing):
    if not isinstance(raw, dict):
        raise SetupError('数据库参数必须为对象', 422)
    host = text(raw.get('host', existing.get('host', '')), '数据库地址', maximum=253)
    if is_local_host(host) or any(char in host for char in ('/', '\\', '@', '?', '#')) or any(char.isspace() for char in host):
        raise SetupError('数据库地址需为集群可访问的主机名或 IP，不含协议', 422)
    try:
        port = int(raw.get('port', existing.get('port', 5432)))
    except (TypeError, ValueError):
        raise SetupError('数据库端口需为 1 到 65535 的整数', 422) from None
    if isinstance(raw.get('port'), bool) or not 1 <= port <= 65535:
        raise SetupError('数据库端口需为 1 到 65535 的整数', 422)
    mode = raw.get('sslmode', existing.get('sslmode', 'disable'))
    if mode not in ('disable', 'prefer', 'require', 'verify-full'):
        raise SetupError('不支持的数据库 SSL 模式', 422)
    return {
        'host': host, 'port': port,
        'database': text(raw.get('database', existing.get('database', 'kubedoor')), '数据库名', maximum=63),
        'user': text(raw.get('user', existing.get('user', 'kubedoor')), '数据库用户名', maximum=63),
        'password': preserved_password(raw, existing, '数据库密码'),
        'sslmode': mode,
    }


def credential_url(url, username='', password=''):
    if not url or not username:
        return url
    parsed = urlsplit(url)
    authority = parsed.netloc.rsplit('@', 1)[-1]
    return urlunsplit((parsed.scheme, f"{quote(username, safe='')}:{quote(password, safe='')}@{authority}",
                       parsed.path, parsed.query, ''))


def url_credentials(url):
    """拆出地址里的“账号:密码@”，返回 (不含账号密码的地址, 账号, 密码)。"""
    parsed = urlsplit(url)
    userinfo, at, address = parsed.netloc.rpartition('@')
    if not at:
        return url, '', ''
    name, _, secret = userinfo.partition(':')
    return urlunsplit((parsed.scheme, address, parsed.path, parsed.query, parsed.fragment)), unquote(name), unquote(secret)


def mask_url_password(url):
    parsed = urlsplit(url)
    userinfo, at, address = parsed.netloc.rpartition('@')
    name, _, secret = userinfo.partition(':')
    if not at or not secret:
        return url
    return urlunsplit((parsed.scheme, f'{name}:{PASSWORD_MASK}@{address}', parsed.path, parsed.query, parsed.fragment))


def metrics_endpoints(metrics):
    """带账号密码的 (远程读地址, 远程写地址)；兼容旧版向导保存的 url / write_url 加独立账号密码。"""
    if 'read_url' in metrics:
        return metrics['read_url'], metrics.get('write_url', '')
    url, user, password = metrics.get('url', ''), metrics.get('user', ''), metrics.get('password', '')
    write = metrics.get('write_url', '')
    if metrics.get('type') == 'Victoria-Metrics-Single' and url and not write:
        write = url.rstrip('/') + '/api/v1/write'
    return credential_url(url, user, password), credential_url(write, user, password)


def metrics_host(value):
    """连接地址只要主机名或 IP：协议单独选择，端口按时序库类型自动带上。"""
    host = text(value, '时序库连接地址', maximum=253)
    if host.startswith('[') and host.endswith(']'):
        host = host[1:-1]
    if '://' in host or any(char in host for char in '/\\@?#[]') or any(char.isspace() for char in host):
        raise SetupError('连接地址只填主机名或 IP，不含 http://、路径或账号', 422)
    if ':' in host:
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            raise SetupError('连接地址不需要填写端口，端口会按时序库类型自动带上', 422) from None
    if is_local_host(host):
        raise SetupError('连接地址需使用集群可访问的地址，不能使用本机或特殊地址', 422)
    return host


def metrics_tenant(value):
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    tenant = text(value or '0', '租户 ID', maximum=21)
    if not re.fullmatch(r'\d{1,10}(:\d{1,10})?', tenant) or any(int(part) > 0xFFFFFFFF for part in tenant.split(':')):
        raise SetupError('租户 ID 需为 0 到 4294967295 的整数，或“账号ID:项目ID”格式，默认 0', 422)
    return ':'.join(str(int(part)) for part in tenant.split(':'))


def endpoint_url(value, title, user, password, previous=''):
    """远程读写地址：只接受 http(s)，可以带“账号:密码@”。

    密码写成 ****** 表示沿用：账号和上面填写的一致时用上面的密码（含已保存的），
    否则用上次保存的这个地址里同一账号的密码。
    """
    value = text(value, title, maximum=2048)
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise SetupError(f'{title}格式不正确', 422) from None
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.query or parsed.fragment or
            '\\' in value or any(char.isspace() for char in value)):
        raise SetupError(f'{title}需为 HTTP/HTTPS 地址，不含查询参数或锚点', 422)
    if port is not None and not 1 <= port <= 65535:
        raise SetupError(f'{title}端口无效', 422)
    if is_local_host(parsed.hostname):
        raise SetupError(f'{title}需使用集群可访问的地址，不能使用本机或特殊地址', 422)
    userinfo, at, address = parsed.netloc.rpartition('@')
    netloc = address
    if at:
        name, _, secret = userinfo.partition(':')
        name, secret = unquote(name), unquote(secret)
        if not name:
            raise SetupError(f'{title}的 @ 前面缺少账号；不需要认证时去掉“账号:密码@”', 422)
        if secret == PASSWORD_MASK:
            _, previous_name, previous_secret = url_credentials(previous) if previous else ('', '', '')
            if user and name == user and password:
                secret = password
            elif previous_secret and name == previous_name:
                secret = previous_secret
            else:
                raise SetupError(f'{title}里的密码需要重新填写', 422)
        netloc = f"{quote(name, safe='')}:{quote(secret, safe='')}@{address}"
    return urlunsplit((parsed.scheme, netloc, parsed.path.rstrip('/'), '', ''))


def metrics_config(raw, existing):
    if not isinstance(raw, dict):
        raise SetupError('时序库参数必须为对象', 422)
    kind = raw.get('type', existing.get('type', 'Victoria-Metrics-Single'))
    if kind not in METRICS_TYPES:
        raise SetupError('网页安装只支持 VictoriaMetrics 单机版或集群版', 422)
    scheme = raw.get('scheme', existing.get('scheme', 'http'))
    if scheme not in ('http', 'https'):
        raise SetupError('连接协议只能选择 http:// 或 https://', 422)
    tag = text(raw.get('tag_key', existing.get('tag_key', 'origin_prometheus')), '集群指标标签名', maximum=128)
    if not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_]*', tag):
        raise SetupError('集群指标标签名只能含字母、数字和下划线，不能以数字开头', 422)
    user = text(raw.get('user', existing.get('user', '')), '时序库账号', required=False, maximum=256)
    if user:
        password = preserved_password(raw, existing, '时序库密码', required=False)
        if not password:
            raise SetupError('填写了时序库账号时请填写密码', 422)
    elif raw.get('password'):
        raise SetupError('填写了时序库密码时请填写账号', 422)
    else:
        password = ''
    return {
        'type': kind, 'scheme': scheme,
        'host': metrics_host(raw.get('host', existing.get('host', ''))),
        'tenant': metrics_tenant(raw.get('tenant', existing.get('tenant', '0'))),
        'user': user, 'password': password,
        'read_url': endpoint_url(raw.get('read_url', existing.get('read_url', '')), '远程读地址', user, password, existing.get('read_url', '')),
        'write_url': endpoint_url(raw.get('write_url', existing.get('write_url', '')), '远程写地址', user, password, existing.get('write_url', '')),
        'tag_key': tag,
    }


def validate_admin_password(password, username):
    candidates = [Path(__file__).resolve().parents[2] / 'kubedoor-master/auth/passwords.py']
    if os.environ.get('SETUP_PASSWORD_MODULE'):
        candidates.insert(0, Path(os.environ['SETUP_PASSWORD_MODULE']))
    path = next((path for path in candidates if path.is_file()), None)
    if path is None:
        raise SetupError('安装包缺少账号密码校验模块', 503)
    spec = importlib.util.spec_from_file_location('_kubedoor_setup_passwords', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    reason = module.validate_new_password(password, username)
    if reason:
        raise SetupError(reason, 422)


def final_config(raw, existing):
    if not isinstance(raw, dict) or not isinstance(raw.get('admin'), dict):
        raise SetupError('请填写管理员设置', 422)
    admin = raw['admin']
    username = admin.get('username', 'kubedoor')
    if username != 'kubedoor':
        raise SetupError('首次安装的管理员账号为 kubedoor', 422)
    password = preserved_password(admin, existing.get('admin', {}), '管理员密码')
    validate_admin_password(password, username)
    if 'components' in raw:
        components = raw['components']
        if not isinstance(components, dict) or type(components.get('alerting')) is not bool:
            raise SetupError('告警组件选择格式不正确', 422)
        alerting = components['alerting']
    else:
        alerting = alerting_choice(existing.get('components'))
    return {
        'admin': {'username': username, 'password': password},
        'public_url': clean_url(raw.get('public_url', ''), 'KubeDoor 访问地址', base=True),
        'components': {'alerting': alerting},
    }


def public_config(config):
    result = copy.deepcopy(config)
    for key in ('postgres', 'metrics', 'admin'):
        section = result.get(key, {})
        section['has_password'] = bool(section.pop('password', ''))
    metrics = result.get('metrics', {})
    for key in ('read_url', 'write_url'):
        if metrics.get(key):
            metrics[key] = mask_url_password(metrics[key])
    result['components'] = {'alerting': alerting_choice(config.get('components'))}
    return result
