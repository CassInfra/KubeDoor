"""「通用配置 → 测试连接」：从 master 查一次时序库，确认查询地址、账号密码和集群指标标签 key。

逻辑参照 kubedoor-setup 的 probes.check_metrics（复制过来，master 不 import setup 的模块）：先查 vector(1)，
再查最近一天 <标签 key> 的标签值。每个请求超时 12 秒、不跟随重定向，响应最多 1MB。
不测远程写地址（写入测试数据有副作用）。返回的说明里不带地址（地址里可能有密码）。
"""

import asyncio
import base64
import json
import ssl
import time

import aiohttp

from . import fields

TIMEOUT = 12  # 秒
MAX_BODY = 1_000_000
LABEL_VALUES_LIMIT = 1000  # 只用来判断有没有数据、展示前几个集群名


class ProbeError(Exception):
    """可以直接给页面看的失败原因"""


def basic_auth_header(user, password):
    """Basic 认证请求头。和 master 平时查时序库用的 requests 一样先按 latin-1 编码，编不了再用 UTF-8"""
    raw = f'{user}:{password}'
    try:
        data = raw.encode('latin-1')
    except UnicodeEncodeError:
        data = raw.encode('utf-8')
    return {'Authorization': 'Basic ' + base64.b64encode(data).decode('ascii')}


async def _api_data(session, url, params, headers):
    async with session.get(url, params=params, headers=headers, allow_redirects=False) as response:
        status = response.status
        if status in (401, 403):
            raise ProbeError(f'时序库认证失败（HTTP {status}），请检查账号、密码和查询权限')
        if 300 <= status < 400:
            raise ProbeError(f'时序库返回了重定向（HTTP {status}），请直接填写最终的查询地址')
        if not 200 <= status < 300:
            raise ProbeError(f'时序库查询接口返回 HTTP {status}，请检查查询地址'
                             '（VictoriaMetrics 集群版要带 /select/<租户>/prometheus）')
        chunks, size = [], 0
        async for chunk in response.content.iter_chunked(65536):
            size += len(chunk)
            if size > MAX_BODY:
                raise ProbeError('时序库返回的数据过大，请确认填写的是查询地址')
            chunks.append(chunk)
    try:
        payload = json.loads(b''.join(chunks))
        if payload.get('status') != 'success':
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ProbeError('时序库没有返回有效的 Prometheus 查询结果，请检查查询地址') from None
    return payload.get('data')


async def _query_one(session, base, headers):
    data = await _api_data(session, base + '/api/v1/query', {'query': 'vector(1)'}, headers)
    result = data.get('result') if isinstance(data, dict) else None
    if not isinstance(data, dict) or data.get('resultType') != 'vector' or not result:
        raise ProbeError('时序库查询验证失败：测试表达式 vector(1) 没有返回预期结果')
    sample = result[0].get('value') if isinstance(result[0], dict) else None
    if not isinstance(sample, list) or len(sample) < 2 or str(sample[1]) != '1':
        raise ProbeError('时序库查询验证失败：测试表达式 vector(1) 没有返回预期结果')


async def _label_values(session, base, headers, tag_key):
    """最近一天出现过的标签值（任何指标都算）；VictoriaMetrics 走索引，不扫描数据"""
    params = {'start': str(int(time.time()) - 86400), 'limit': str(LABEL_VALUES_LIMIT)}
    data = await _api_data(session, f'{base}/api/v1/label/{tag_key}/values', params, headers)
    if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
        raise ProbeError('时序库标签值接口返回的格式不正确，请检查查询地址')
    return data


def label_message(tag_key, values):
    """查到的标签值 → 结果说明"""
    if not values:
        return {'ok': True, 'level': 'warning',
                'message': f'连接正常，但最近一天没有查到带 {tag_key} 标签的数据。'
                           '已有监控数据时请核对集群指标标签 key；'
                           '新部署的时序库查不到是正常的'}
    shown = '、'.join(''.join(ch for ch in item[:63] if ch.isprintable()) for item in values[:5])
    count = f'{len(values)} 个' if len(values) < LABEL_VALUES_LIMIT else f'{LABEL_VALUES_LIMIT} 个以上'
    more = ' 等' if len(values) > 5 else ''
    return {'ok': True, 'level': 'success', 'message': f'连接正常，标签 {tag_key} 下有 {count}集群：{shown}{more}'}


def _failure(message):
    return {'ok': False, 'level': 'error', 'message': message}


async def check_metrics(url, tag_key, *, session_factory=aiohttp.ClientSession):
    """查一次时序库，返回 {'ok', 'level'(success / warning / error), 'message'}；不抛异常。
    url 是带账号密码的查询地址（账号密码拆出来用 Basic 认证发），tag_key 已校验过（只有字母、数字和下划线）"""
    try:
        base, user, password = fields.url_credentials(url)
        base = base.rstrip('/')
        headers = basic_auth_header(user, password) if user else None
        async with session_factory(timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as session:
            await _query_one(session, base, headers)
            values = await _label_values(session, base, headers, tag_key)
    except ProbeError as exc:
        return _failure(str(exc))
    except (aiohttp.ClientSSLError, ssl.SSLError):
        return _failure('时序库 TLS 证书验证失败，请检查服务证书')
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
        return _failure(f'连接时序库失败或超时（{TIMEOUT} 秒），请检查地址和 master 到时序库的网络')
    except (ValueError, TypeError, KeyError, IndexError):
        return _failure('时序库查询结果格式不正确')
    except Exception as exc:
        return _failure(f'测试失败：{type(exc).__name__}')
    return label_message(tag_key, values)
