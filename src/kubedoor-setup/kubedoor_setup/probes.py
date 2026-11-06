"""Bounded checks performed from the installer Pod, with safe public failures."""
from __future__ import annotations

import asyncio
import json
import ssl
import time
import uuid

import aiohttp
import asyncpg

from .config import metrics_endpoints, url_credentials
from .errors import SetupError

# 目标库不存在时连这些维护库建库，按顺序尝试
MAINTENANCE_DATABASES = ('postgres', 'template1')


def pg_connect_options(config):
    return {'host': config['host'], 'port': config['port'], 'user': config['user'],
            'password': config['password'], 'database': config['database'],
            'ssl': config.get('sslmode', 'disable'), 'timeout': 10, 'command_timeout': 10}


def quote_ident(name):
    return '"' + name.replace('"', '""') + '"'


def _missing_database(config, reason):
    sql = f'CREATE DATABASE {quote_ident(config["database"])} OWNER {quote_ident(config["user"])};'
    return SetupError(f'数据库 {config["database"]} 不存在，{reason}。请让数据库管理员执行 {sql} 后重新检测', 422)


async def create_database(config, *, connect=asyncpg.connect):
    """目标库不存在时用同一账号连维护库建库，建库账号即属主，可在 public schema 建表。

    返回 True 表示本次新建；并发检测时库已被建好返回 False。没有建库权限或连不上维护库时
    抛出带建库语句的 SetupError，由数据库管理员手动创建。
    """
    for maintenance in MAINTENANCE_DATABASES:
        if maintenance == config['database']:
            continue
        try:
            connection = await connect(**{**pg_connect_options(config), 'database': maintenance})
        except Exception:
            continue  # 维护库不存在、pg_hba 不放行或没有 CONNECT 权限时换下一个
        try:
            await connection.execute(f'CREATE DATABASE {quote_ident(config["database"])}', timeout=30)
            return True
        except asyncpg.DuplicateDatabaseError:
            return False
        except asyncpg.InsufficientPrivilegeError:
            raise _missing_database(config, f'账号 {config["user"]} 没有建库权限（CREATEDB），无法自动创建') from None
        except Exception as error:
            code = getattr(error, 'sqlstate', None)
            raise _missing_database(config, f'自动创建失败（SQLSTATE {code}）' if code else '自动创建失败') from None
        finally:
            await connection.close(timeout=3)
    raise _missing_database(config, f'账号 {config["user"]} 也无法连接 postgres 维护库自动创建')


def pg_error(error):
    if isinstance(error, asyncpg.InvalidPasswordError):
        return SetupError('数据库密码验证失败，请检查用户名和密码', 422)
    if isinstance(error, asyncpg.InvalidCatalogNameError):
        return SetupError('指定数据库不存在，请重新检测数据库连接，检测时会自动创建', 422)
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
    created = False
    try:
        try:
            connection = await connect(**pg_connect_options(config))
        except asyncpg.InvalidCatalogNameError:
            created = await create_database(config, connect=connect)
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
        message = '连接、账号认证和 schema 建表读写权限检查通过'
        if created:
            message = f'数据库 {config["database"]} 不存在，已用账号 {config["user"]} 自动创建；{message}'
        return {'status': 'success', 'message': message}
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


# 查标签值时最多取这么多个，只用来判断有没有数据和展示前几个集群名
LABEL_VALUES_LIMIT = 1000


def metrics_query_base(config):
    """远程读地址拆成 (不含账号密码的查询地址, BasicAuth)。"""
    read_url, _ = metrics_endpoints(config)
    base, user, password = url_credentials(read_url)
    return base.rstrip('/'), (aiohttp.BasicAuth(user, password) if user else None)


async def _api_data(session, url, params, auth):
    async with session.get(url, params=params, auth=auth, allow_redirects=False) as response:
        if response.status in (401, 403):
            raise SetupError('时序库认证失败，请检查账号、密码和查询权限', 422)
        if not 200 <= response.status < 300:
            raise SetupError(f'时序库查询接口返回 HTTP {response.status}，请检查远程读地址', 422)
        chunks, size = [], 0
        async for chunk in response.content.iter_chunked(65536):
            size += len(chunk)
            if size > 1_000_000:
                raise SetupError('时序库返回的数据过大，请确认填写的是远程读地址', 422)
            chunks.append(chunk)
        raw = b''.join(chunks)
        try:
            payload = json.loads(raw)
            if payload.get('status') != 'success':
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise SetupError('时序库没有返回有效的 Prometheus 查询结果，请检查远程读地址', 422) from None
        return payload.get('data')


async def query_metrics(session, base, auth, query):
    data = await _api_data(session, base + '/api/v1/query', {'query': query}, auth)
    if not isinstance(data, dict):
        raise SetupError('时序库没有返回有效的 Prometheus 查询结果，请检查远程读地址', 422)
    return data


async def label_values(session, base, auth, label):
    """最近一天出现过的标签值（任何指标都算）；VictoriaMetrics 走索引，不扫描数据。"""
    params = {'start': str(int(time.time()) - 86400), 'limit': str(LABEL_VALUES_LIMIT)}
    data = await _api_data(session, f'{base}/api/v1/label/{label}/values', params, auth)
    if not isinstance(data, list) or not all(isinstance(value, str) for value in data):
        raise SetupError('时序库标签值接口返回格式不正确，请检查远程读地址', 422)
    return data


def _label_message(tag, values):
    if not values:
        return {'status': 'warning', 'metrics_found': False,
                'message': f'查询正常，但最近一天没有查到带 {tag} 标签的数据。已有完整监控应能查到带这个标签的数据，'
                           '请核对标签名；新部署的时序库查不到是正常的，可以继续'}
    shown = '、'.join(value[:63] for value in values[:5])
    if len(values) > 5:
        shown += f' 等 {len(values)} 个' if len(values) < LABEL_VALUES_LIMIT else f' 等 {LABEL_VALUES_LIMIT} 个以上'
    return {'status': 'success', 'metrics_found': True, 'message': f'查询正常，已查到带 {tag} 标签的数据，标签值：{shown}'}


async def check_metrics(config, *, session_factory=aiohttp.ClientSession):
    try:
        base, auth = metrics_query_base(config)
        async with session_factory(timeout=aiohttp.ClientTimeout(total=12)) as session:
            data = await query_metrics(session, base, auth, 'vector(1)')
            if data.get('resultType') != 'vector' or not data.get('result'):
                raise SetupError('时序库查询验证失败，测试表达式未返回预期结果', 422)
            if not isinstance(data['result'][0].get('value'), list) or str(data['result'][0]['value'][1]) != '1':
                raise SetupError('时序库查询验证失败，测试表达式未返回预期结果', 422)
            values = await label_values(session, base, auth, config['tag_key'])
    except SetupError:
        raise
    except (aiohttp.ClientSSLError, ssl.SSLError):
        raise SetupError('时序库 TLS 证书验证失败，请检查服务证书', 422) from None
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
        raise SetupError('时序库连接失败或超时，请检查集群到时序库的网络', 422) from None
    except (ValueError, TypeError, KeyError, IndexError):
        raise SetupError('时序库查询结果格式不正确', 422) from None
    return _label_message(config['tag_key'], values)
