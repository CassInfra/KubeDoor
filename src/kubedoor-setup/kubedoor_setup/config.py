"""Strict structured wizard configuration; no user input is executed as shell code."""
from __future__ import annotations

import copy
import importlib.util
import ipaddress
import os
from pathlib import Path
import re
import secrets
from urllib.parse import urlsplit, urlunsplit

from .errors import SetupError


def default_private():
    return {
        'config': {
            'postgres': {'host': '', 'port': 5432, 'database': 'kubedoor', 'user': 'kubedoor', 'password': '', 'sslmode': 'prefer'},
            'metrics': {'type': 'Victoria-Metrics-Single', 'url': '', 'user': '', 'password': '', 'write_url': '', 'tag_key': 'origin_prometheus'},
            'admin': {'username': 'kubedoor', 'password': ''},
            'public_url': '',
            'components': {'grafana': True, 'vmalert': True, 'alertmanager': True},
        },
        'secrets': {},
    }


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
    mode = raw.get('sslmode', existing.get('sslmode', 'prefer'))
    if mode not in ('disable', 'prefer', 'require', 'verify-full'):
        raise SetupError('不支持的数据库 SSL 模式', 422)
    return {
        'host': host, 'port': port,
        'database': text(raw.get('database', existing.get('database', 'kubedoor')), '数据库名', maximum=63),
        'user': text(raw.get('user', existing.get('user', 'kubedoor')), '数据库用户名', maximum=63),
        'password': preserved_password(raw, existing, '数据库密码'),
        'sslmode': mode,
    }


def metrics_config(raw, existing):
    if not isinstance(raw, dict):
        raise SetupError('时序库参数必须为对象', 422)
    kind = raw.get('type', existing.get('type', 'Victoria-Metrics-Single'))
    if kind not in ('Victoria-Metrics-Single', 'Victoria-Metrics-Cluster', 'Prometheus'):
        raise SetupError('不支持的时序库类型', 422)
    url = clean_url(raw.get('url', existing.get('url', '')), '时序库地址')
    tag = text(raw.get('tag_key', existing.get('tag_key', 'origin_prometheus')), '集群指标标签名', maximum=128)
    if not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_]*', tag):
        raise SetupError('集群指标标签名只能含字母、数字和下划线，不能以数字开头', 422)
    user = text(raw.get('user', existing.get('user', '')), '时序库用户名', required=False, maximum=256)
    password = preserved_password(raw, existing, '时序库密码', required=False)
    if bool(user) != bool(password):
        raise SetupError('时序库用户名和密码需同时填写，或同时留空', 422)
    write_url = url + '/api/v1/write' if kind == 'Victoria-Metrics-Single' else clean_url(raw.get('write_url', ''), '完整写入地址', required=False)
    return {'type': kind, 'url': url, 'user': user, 'password': password, 'write_url': write_url, 'tag_key': tag}


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
    components = raw.get('components', existing.get('components'))
    if not isinstance(components, dict) or any(type(components.get(key)) is not bool for key in ('grafana', 'vmalert', 'alertmanager')):
        raise SetupError('组件选择格式不正确', 422)
    if components['vmalert'] and not components['alertmanager']:
        raise SetupError('安装告警规则计算服务时，请同时启用 Alertmanager', 422)
    return {
        'admin': {'username': username, 'password': password},
        'public_url': clean_url(raw.get('public_url', ''), 'KubeDoor 访问地址', base=True),
        'components': {key: components[key] for key in ('grafana', 'vmalert', 'alertmanager')},
    }


def public_config(config):
    result = copy.deepcopy(config)
    for key in ('postgres', 'metrics', 'admin'):
        section = result.get(key, {})
        section['has_password'] = bool(section.pop('password', ''))
    return result
