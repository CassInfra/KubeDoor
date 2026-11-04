"""Bounded checks performed from the installer Pod, with safe public failures."""
from __future__ import annotations

import asyncio
import json
import ssl
import uuid

import aiohttp
import asyncpg

from .errors import SetupError


def pg_connect_options(config):
    return {'host': config['host'], 'port': config['port'], 'user': config['user'],
            'password': config['password'], 'database': config['database'],
            'ssl': config.get('sslmode', 'prefer'), 'timeout': 10, 'command_timeout': 10}


def pg_error(error):
    if isinstance(error, asyncpg.InvalidPasswordError):
        return SetupError('数据库密码验证失败，请检查用户名和密码', 422)
    if isinstance(error, asyncpg.InvalidCatalogNameError):
        return SetupError('指定数据库不存在，请先创建数据库', 422)
    if isinstance(error, asyncpg.InsufficientPrivilegeError):
        return SetupError('可以连接数据库，但账号没有所需的 schema 建表或读写权限', 422)
    if isinstance(error, asyncpg.InvalidAuthorizationSpecificationError):
        return SetupError('数据库拒绝该账号或来源地址，请检查访问控制配置', 422)
    if isinstance(error, (ssl.SSLError, ssl.CertificateError)):
        return SetupError('数据库 TLS 证书验证失败，请检查证书和 SSL 模式', 422)
    if isinstance(error, (asyncio.TimeoutError, TimeoutError, OSError)):
        return SetupError('数据库连接超时或网络不可达，请检查地址、端口和防火墙', 422)
    return SetupError('数据库检测失败，请检查数据库状态、账号权限和连接参数', 422)


async def _check_database_owner(connection, installation_id):
    existing = await connection.fetchval("SELECT to_regclass('public.auth_users') IS NOT NULL OR to_regclass('public.k8s_resources') IS NOT NULL OR to_regclass('public.kubedoor_ai_sessions') IS NOT NULL")
    marker_exists = await connection.fetchval("SELECT to_regclass('public.kubedoor_setup_installation') IS NOT NULL")
    rows = await connection.fetch('SELECT id FROM public.kubedoor_setup_installation') if marker_exists else []
    owned = len(rows) == 1 and str(rows[0]['id']) == installation_id
    if (existing or rows) and not owned:
        raise SetupError('该数据库已有 KubeDoor 数据或其他安装标记，请使用原控制台或恢复流程，不会覆盖初始化', 409)
    return owned


async def check_postgres(config, installation_id, *, connect=asyncpg.connect):
    connection = None
    try:
        connection = await connect(**pg_connect_options(config))
        if int(await connection.fetchval('SHOW server_version_num')) < 110000:
            raise SetupError('需要 PostgreSQL 11 或更高版本，建议使用 14 及以上版本', 422)
        await _check_database_owner(connection, installation_id)
        transaction = connection.transaction()
        await transaction.start()
        try:
            table = '_kubedoor_setup_probe_' + uuid.uuid4().hex
            await connection.execute(f'CREATE TABLE public."{table}" (value integer NOT NULL)')
            await connection.execute(f'INSERT INTO public."{table}" VALUES (1)')
            if await connection.fetchval(f'SELECT value FROM public."{table}"') != 1:
                raise SetupError('数据库读写验证失败', 422)
        finally:
            await transaction.rollback()
        return {'status': 'success', 'message': '连接、账号认证和 schema 建表读写权限检查通过'}
    except SetupError:
        raise
    except Exception as error:
        raise pg_error(error) from None
    finally:
        if connection is not None:
            await connection.close(timeout=3)


async def reserve_database(config, installation_id, *, connect=asyncpg.connect):
    """Claim only an empty database or resume our own claim, never adopt another installation."""
    connection = None
    try:
        connection = await connect(**pg_connect_options(config))
        async with connection.transaction():
            await connection.execute("SELECT pg_advisory_xact_lock(hashtext('kubedoor-setup-initialization'))")
            owned = await _check_database_owner(connection, installation_id)
            if not owned:
                await connection.execute('CREATE TABLE IF NOT EXISTS public.kubedoor_setup_installation (id text PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now())')
                await connection.execute('INSERT INTO public.kubedoor_setup_installation (id) VALUES ($1)', installation_id)
    except SetupError:
        raise
    except Exception as error:
        raise pg_error(error) from None
    finally:
        if connection is not None:
            await connection.close(timeout=3)


async def query_metrics(session, config, query):
    auth = aiohttp.BasicAuth(config['user'], config['password']) if config.get('user') else None
    async with session.get(config['url'].rstrip('/') + '/api/v1/query', params={'query': query},
                           auth=auth, allow_redirects=False) as response:
        if response.status in (401, 403):
            raise SetupError('时序库认证失败，请检查用户名、密码和查询权限', 422)
        if not 200 <= response.status < 300:
            raise SetupError(f'时序库查询接口返回 HTTP {response.status}，请检查查询入口', 422)
        chunks, size = [], 0
        async for chunk in response.content.iter_chunked(65536):
            size += len(chunk)
            if size > 1_000_000:
                raise SetupError('时序库返回的数据过大，请确认填写的是查询入口', 422)
            chunks.append(chunk)
        raw = b''.join(chunks)
        try:
            payload = json.loads(raw)
            if payload.get('status') != 'success' or not isinstance(payload.get('data'), dict):
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise SetupError('时序库没有返回有效的 Prometheus 查询结果，请检查服务地址', 422) from None
        return payload['data']


async def check_metrics(config, *, session_factory=aiohttp.ClientSession):
    try:
        async with session_factory(timeout=aiohttp.ClientTimeout(total=12)) as session:
            data = await query_metrics(session, config, 'vector(1)')
            if data.get('resultType') != 'vector' or not data.get('result'):
                raise SetupError('时序库查询验证失败，测试表达式未返回预期结果', 422)
            if not isinstance(data['result'][0].get('value'), list) or str(data['result'][0]['value'][1]) != '1':
                raise SetupError('时序库查询验证失败，测试表达式未返回预期结果', 422)
            sample = await query_metrics(session, config, f'count(kube_node_info{{{config["tag_key"]}!=""}})')
            rows = sample.get('result', [])
            has_metrics = bool(rows and float(rows[0]['value'][1]) > 0)
            return {'status': 'success', 'message': '查询接口正常，已发现带所选集群标签的指标' if has_metrics else '查询接口正常，尚未发现所选集群标签的节点指标；新库可继续，已有监控请核对标签',
                    'metrics_found': has_metrics}
    except SetupError:
        raise
    except (aiohttp.ClientSSLError, ssl.SSLError):
        raise SetupError('时序库 TLS 证书验证失败，请检查服务证书', 422) from None
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
        raise SetupError('时序库连接失败或超时，请检查集群到时序库的网络', 422) from None
    except (ValueError, TypeError, KeyError, IndexError):
        raise SetupError('时序库查询结果格式不正确', 422) from None
