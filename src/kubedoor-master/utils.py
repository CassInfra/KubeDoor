import os
import sys
import time
import json
import math
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
import requests
from datetime import datetime, timedelta
from functools import wraps
from loguru import logger
from promql import query_dict, node_rank_query
from jvm_config import JVM_FIELDS
import db
from db import (
    pg_fetch,
    pg_fetchrow,
    pg_fetchval,
    pg_execute,
    pg_executemany,
)

# 用于异步执行同步操作的线程池
_executor = ThreadPoolExecutor(max_workers=10)


async def run_blocking(func, *args):
    """在 _executor 里执行阻塞的同步函数(Prometheus / IM / 镜像仓库等 HTTP 调用)。

    handler 都跑在主 event loop 上,直接调 requests 会卡住整个 master(所有接口和 agent 心跳都停)。
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, func, *args)


# admis 查询缓存：key=(env, namespace, deployment, include_jvm) -> (result, expire_ts)
# TTL 兜底，写表时主动失效
_admis_cache = {}
_admis_cache_lock = threading.Lock()
ADMIS_CACHE_TTL = int(os.environ.get('ADMIS_CACHE_TTL', '60'))


def invalidate_admis_cache():
    """清空 admis 查询缓存。在写入 k8s_agent_status / k8s_res_control 后调用，
    保证 UI 改管控配置 / 新增服务后立即对 kubectl 部署生效（无需等 TTL 过期）。"""
    with _admis_cache_lock:
        count = len(_admis_cache)
        _admis_cache.clear()
    if count:
        logger.info(f"admis 缓存已主动失效，清除 {count} 条")


logger.remove()
logger.add(
    sys.stderr,
    format='<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> [<level>{level}</level>] <level>{message}</level>',
    level='INFO',
)

# 环境变量
DEFAULT_AT = os.environ.get('DEFAULT_AT')
MSG_TOKEN = os.environ.get('MSG_TOKEN')
MSG_TYPE = os.environ.get('MSG_TYPE')
PROM_K8S_TAG_KEY = os.environ.get('PROM_K8S_TAG_KEY')
# 告警去重时间窗口（秒），默认300秒
ALERT_DEDUP_WINDOW = int(os.environ.get('ALERT_DEDUP_WINDOW', '300'))
PROM_TYPE = os.environ.get('PROM_TYPE')
PROM_URL = os.environ.get('PROM_URL')
UPDATE_IMAGE = os.environ.get('UPDATE_IMAGE')

# 外部 HTTP 调用的超时(连接, 读取)秒。requests 默认不超时,对端不响应时线程会一直卡住
PROM_TIMEOUT = (5, 120)
MSG_TIMEOUT = (5, 10)

# PostgreSQL 连接配置（统一 PG_ 前缀，替代原 CK_* / DB_*）
PG_HOST = db.PG_HOST
PG_PORT = db.PG_PORT
PG_USER = db.PG_USER
PG_PASSWORD = db.PG_PASSWORD
PG_DATABASE = db.PG_DATABASE


query_list = [
    "core_usage",
    "core_usage_percent",
    "wss_usage_MB",
    "wss_usage_percent",
    "limit_core",
    "limit_mem_MB",
    "request_core",
    "request_mem_MB",
    "heap_usage_percent",
    "g1e_usage_percent",
]

namespace_str_exclude = "loggie|kubedoor|kube-otel|cert-manager|kube-system|ops-monit"


def calculate_peak_duration_and_end_time(peak_hours):
    # 提取开始和结束时间
    start_str, end_str = peak_hours.split('-')
    start_time = datetime.strptime(start_str, '%H:%M:%S')
    end_time = datetime.strptime(end_str, '%H:%M:%S')
    # 计算持续时间
    duration = end_time - start_time
    duration_hours = duration.seconds // 3600
    duration_minutes = (duration.seconds % 3600) // 60
    # 生成持续时间的字符串
    duration_str = f"{duration_hours}h{duration_minutes}m"

    start_time_part = start_time.time()
    end_time_part = end_time.time()
    return duration_str, start_time_part, end_time_part


def day_range(start_date, end_date):
    """把前端传的日期 'YYYY-MM-DD' 转成查询区间 [开始日 00:00, 结束日次日 00:00)。

    asyncpg 的 timestamptz 参数必须传 datetime,传字符串会直接报错。
    naive datetime 按容器本地时区(TZ=Asia/Shanghai)解释,和入库时的口径一致。
    """
    start = datetime.strptime(start_date[:10], '%Y-%m-%d')
    end = datetime.strptime(end_date[:10], '%Y-%m-%d') + timedelta(days=1)
    return start, end


async def check_and_delete_day_data(date, env_value):
    """检查是否有当天的数据，有则删除"""
    result = await pg_fetch(
        "SELECT 1 FROM k8s_resources WHERE date = $1 AND env = $2 LIMIT 1", date, env_value
    )
    if result:
        logger.info(f"从表k8s_resources删除{env_value} {date}的数据")
        await pg_execute("DELETE FROM k8s_resources WHERE date = $1 AND env = $2", date, env_value)
    return result


def get_prom_url():
    """按类型选择查询指标的方式"""
    # url = f"{PROM_URL}/api/v1/query_range"
    url = f"{PROM_URL}/api/v1/query"
    # if PROM_TYPE == "Prometheus":
    #     url = f"{PROM_URL}/api/v1/query_range"
    # if PROM_TYPE == "Victoria-Metrics-Single":
    #     url = f"{PROM_URL}/api/v1/query_range"
    # if PROM_TYPE == "Victoria-Metrics-Cluster":
    #     url = f"{PROM_URL}/select/0/prometheus/api/v1/query_range"
    return url


def fetch_prom_namespaces(env_value):
    # 使用 max_over_time 来获取最近一小时的数据
    # query = f'group by (namespace) (max_over_time(kube_namespace_created{{{PROM_K8S_TAG_KEY}="{env_value}"}}[1h]))'
    query = f'group by (namespace) (kube_namespace_created{{{PROM_K8S_TAG_KEY}="{env_value}"}})'
    try:
        response = requests.get(get_prom_url(), params={'query': query}, timeout=PROM_TIMEOUT)
        response.raise_for_status()  # 检查请求是否成功
        data = response.json()
        namespaces = []
        for result in data['data']['result']:
            labels = result['metric']
            namespaces.append(labels.get('namespace'))
        return namespaces
    except requests.exceptions.RequestException as e:
        raise Exception(f"Error fetching data from Prometheus: {e}")


def fetch_prom_services(env_value, namespace):
    """
    获取指定环境和命名空间的service列表
    """
    query = f'group by(service)(kube_service_info{{{PROM_K8S_TAG_KEY}="{env_value}",namespace="{namespace}"}})'
    try:
        response = requests.get(get_prom_url(), params={'query': query}, timeout=PROM_TIMEOUT)
        response.raise_for_status()  # 检查请求是否成功
        data = response.json()
        services = []
        for result in data['data']['result']:
            labels = result['metric']
            services.append(labels.get('service'))
        return services
    except requests.exceptions.RequestException as e:
        raise Exception(f"Error fetching data from Prometheus: {e}")


def fetch_prom_envs():
    # query = f'group by ({PROM_K8S_TAG_KEY}) (kube_state_metrics_build_info)'
    query = f'group by ({PROM_K8S_TAG_KEY}) (kube_node_info)'
    try:
        response = requests.get(get_prom_url(), params={'query': query}, timeout=PROM_TIMEOUT)
        response.raise_for_status()  # 检查请求是否成功
        data = response.json()
        envs = []
        for result in data['data']['result']:
            labels = result['metric']
            envs.append(labels.get(PROM_K8S_TAG_KEY))
        return envs
    except requests.exceptions.RequestException as e:
        raise Exception(f"Error fetching data from Prometheus: {e}")


def _nullable_jvm_pct(value):
    """Keep finite nonnegative percentages, including reported values above 100%."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _append_optional_jvm_percent(metric, env_value, end_time_full, duration, workload_dict):
    """An optional JVM query cannot discard resource data or another JVM observation."""
    values = {}
    try:
        query = (
            query_dict[metric]
            .replace("{env}", f'{PROM_K8S_TAG_KEY}="{env_value}",')
            .replace("{env_key}", f"{PROM_K8S_TAG_KEY},")
            .replace("{duration}", duration)
        )
        response = requests.request(
            "GET", get_prom_url(),
            params={"query": query, "time": end_time_full.timestamp(), "step": "15"},
            timeout=PROM_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get('status') != 'success':
            raise ValueError(f'Prometheus {metric} query returned an error')
        for series in payload['data']['result']:
            try:
                labels = series['metric']
                key = f"{labels[PROM_K8S_TAG_KEY]}@{labels['namespace']}@{labels['owner_name']}"
                values[key] = _nullable_jvm_pct(series['value'][1])
            except (KeyError, IndexError, TypeError):
                continue
    except (requests.exceptions.RequestException, ValueError, KeyError, TypeError) as exc:
        logger.warning(f"{env_value}: 高峰{metric}采集失败，本指标本轮记为NULL: {exc}")
        values = {}
    for key, row in workload_dict.items():
        row.append(values.get(key))
    return workload_dict


def get_prom_data(promql, env_key, env_value, end_time_full, duration, workload_dict={}):
    """获取指标源数据"""
    if promql in ('heap_usage_percent', 'g1e_usage_percent'):
        return _append_optional_jvm_percent(promql, env_value, end_time_full, duration, workload_dict)
    url = get_prom_url()
    k8s_filter = f'{PROM_K8S_TAG_KEY}="{env_value}",'
    query = (
        query_dict.get(promql)
        .replace("{env}", k8s_filter)
        .replace("{env_key}", f"{PROM_K8S_TAG_KEY},")
        .replace("{duration}", duration)
    )
    querystring = {"query": query, "time": end_time_full.timestamp(), "step": "15"}
    logger.info(querystring)
    response = requests.request("GET", url, params=querystring, timeout=PROM_TIMEOUT).json()
    if response.get("status") == "success":
        result = response["data"]["result"]
        if promql == "pod_num":
            workload_dict = {}
            for x in result:
                k8s = x['metric'][PROM_K8S_TAG_KEY]
                ns = x['metric'].get('namespace')
                dpm = x['metric'].get('workload')
                replicaset = x['metric'].get('owner_name')
                endtime = datetime.fromtimestamp(int(x["value"][0]))
                workload_dict[f'{k8s}@{ns}@{replicaset}'] = [endtime, k8s, ns, dpm, int(x['value'][1])]
            logger.info(f'处理指标{promql}完成: 服务数{len(workload_dict)}')
        else:
            workload_metrics_dict = {}
            for x in result:
                k8s = x['metric'][PROM_K8S_TAG_KEY]
                ns = x['metric'].get('namespace')
                replicaset = x['metric'].get('owner_name')
                workload_metrics_dict[f'{k8s}@{ns}@{replicaset}'] = float(x['value'][1])

            for k in workload_dict.keys():
                if k in workload_metrics_dict:
                    workload_dict[k].append(workload_metrics_dict[k])
                else:
                    workload_dict[k].append(-1)
            logger.info(f'处理指标{promql}完成: 服务数{len(workload_dict)}, 指标数{len(workload_metrics_dict)}')
        return workload_dict
    else:
        logger.error('ERROR {} {}', promql, env_key)
        return {}


def merged_dict(env_key, env_value, duration_str, end_time_full):
    """解析指标源数据，处理成列表"""
    k8s_metrics_list = []
    workload_dict = get_prom_data("pod_num", env_key, env_value, end_time_full, duration_str)

    for promql in query_list:
        workload_dict = get_prom_data(promql, env_key, env_value, end_time_full, duration_str, workload_dict)

    for v in workload_dict.values():
        logger.debug(v)
        # Preserve the original 16 columns, then append heap/G1 Eden percentages.
        k8s_metrics_list.append(v[:-2] + [-1, -1, -1] + v[-2:])

    return k8s_metrics_list


async def metrics_to_pg(k8s_metrics_list):
    """将指标数据存入 PostgreSQL（批量 COPY 写入）

    k8s_metrics_list 每行 18 列，旧版16/17列数据缺失的JVM百分比补NULL：
      date, env, namespace, deployment, pod_count, p95_pod_load, p95_pod_cpu_pct,
      p95_pod_wss_mb, p95_pod_wss_pct, limit_pod_cpu_m, limit_pod_mem_mb,
      request_pod_cpu_m, request_pod_mem_mb, p95_pod_qps, p95_pod_g1gc_qps, pod_jvm_max_mb,
      p95_pod_heap_pct, p95_pod_g1e_pct
    """
    columns = [
        'date', 'env', 'namespace', 'deployment', 'pod_count', 'p95_pod_load',
        'p95_pod_cpu_pct', 'p95_pod_wss_mb', 'p95_pod_wss_pct', 'limit_pod_cpu_m',
        'limit_pod_mem_mb', 'request_pod_cpu_m', 'request_pod_mem_mb', 'p95_pod_qps',
        'p95_pod_g1gc_qps', 'pod_jvm_max_mb', 'p95_pod_heap_pct', 'p95_pod_g1e_pct',
    ]
    batch_size = 10000
    for i in range(0, len(k8s_metrics_list), batch_size):
        begin = time.time()
        batch_data = []
        for row in k8s_metrics_list[i : i + batch_size]:
            record = list(row)
            if len(record) in (16, 17):
                record.extend([None] * (18 - len(record)))
            for index in (16, 17):
                record[index] = _nullable_jvm_pct(record[index])
            batch_data.append(tuple(record))
        try:
            await db.pg_copy_records('k8s_resources', batch_data, columns)
            logger.info(
                f"🌊高峰期数据写入PG == 正在插入批次: {i//batch_size}，"
                "耗时：{:.2f}s".format(time.time() - begin)
            )
        except Exception as e:
            logger.exception("Failed to insert batch {}: {}", i // batch_size, e)
    return True


def merge_dicts(dict1, dict2):
    merged_dict = dict1.copy()
    for key, value in dict2.items():
        if key in merged_dict:
            merged_dict[key].update(value)
        else:
            merged_dict[key] = value
    return merged_dict


def get_node_deployments(node, env_value):
    logger.info(f"开始查询节点 {node} 上的所有deployment (env: {env_value})")
    deployment_list = []
    url = get_prom_url()
    k8s_filter = f'{PROM_K8S_TAG_KEY}="{env_value}",'
    query = (
        query_dict.get('deployments_by_node')
        .replace("{env}", k8s_filter)
        .replace("{namespace}", namespace_str_exclude)
        .replace("{node}", node)
    )
    querystring = {"query": query, "step": "15"}
    logger.info(f"查询参数: {querystring}")
    response = requests.request("GET", url, params=querystring, timeout=PROM_TIMEOUT).json()
    if response.get("status") == "success":
        result = response["data"]["result"]
        logger.info(f"在节点 {node} 上找到 {len(result)} 个deployment")
        for x in result:
            ns = x['metric'].get('namespace', x['metric'].get('k8s_ns')) or x['metric'].get(
                'namespace', x['metric'].get('destination_workload_namespace')
            )
            deployment_list.append(
                {
                    "namespace": ns,
                    "pod": x['metric'].get('pod'),
                    "created_by_name": x['metric'].get('created_by_name'),
                }
            )
        logger.info(f"节点 {node} 上的deployment列表: {json.dumps(deployment_list)}")
        return deployment_list
    else:
        logger.error(f'查询节点 {node} 上的deployment列表失败')


async def agent_collect_info():
    """从库中读取需要采集的 agent 信息"""
    rows = await pg_fetch("SELECT env, peak_hours FROM k8s_agent_status WHERE collect = true")
    return [[row[0], row[1]] for row in rows]


async def init_agent_status(env):
    """确保 env 在 k8s_agent_status 中有一行（幂等）"""
    await pg_execute(
        "INSERT INTO k8s_agent_status (env) VALUES ($1) ON CONFLICT (env) DO NOTHING", env
    )
    return True


async def get_k8s_names():
    """从库中获取所有K8S环境名称，按顺序排序"""
    try:
        rows = await pg_fetch("SELECT env FROM k8s_agent_status ORDER BY env")
        return [row[0] for row in rows]
    except Exception as e:
        logger.exception(e)
        return []


async def agent_info():
    """从库中读取所有 agent 的信息"""
    agent_info = {}
    try:
        rows = await pg_fetch(
            "SELECT env, collect, peak_hours, admission, admission_namespace, "
            "nms_not_confirm, scheduler FROM k8s_agent_status"
        )
        for row in rows:
            agent_info[row[0]] = {
                "collect": row[1],
                "peak_hours": row[2],
                "admission": row[3],
                "admission_namespace": row[4],
                "nms_not_confirm": row[5],
                "scheduler": row[6],
            }
    except Exception as e:
        logger.exception(e)
    return agent_info


async def get_deploy_admis_async(env, namespace, deployment, include_jvm=False):
    """查询 admission 信息（agent webhook 热路径，经 WebSocket 调用）

    热路径：一条 LEFT JOIN 合并原本的两次查询，
    带查询级超时 + 60s 缓存兜底 + 全异常兜底，避免高并发下阻塞导致 webhook 30s 超时。
    直接 await asyncpg，不经线程池 —— 以前走 _executor + 同步桥，和 Prometheus / IM 这些
    慢调用共用 10 个线程，那些调用卡住时 admis 会排队，直到 agent 端 30s 超时。

    返回契约（agent 端靠长度解包，务必保持）：
      - 2 元素 [code, msg]  → 非管控放行(200) / 免确认(200) / 未找到(404) / 异常(503)
      - 8 元素 [pod_count, pod_count_ai, pod_count_manual, request_cpu_m,
                request_mem_mb, limit_cpu_m, limit_mem_mb, scheduler] → 命中管控服务
      - 支持 JVM 的新版 agent 显式传 include_jvm=True 时，追加第 9 元素 JVM 字段字典
    """
    cache_key = (env, namespace, deployment, include_jvm)
    now = time.time()
    # 1) 查缓存（命中且未过期直接返回）
    with _admis_cache_lock:
        cached = _admis_cache.get(cache_key)
        if cached is not None and now < cached[1]:
            return cached[0]

    try:
        # 2) 合并查询：agent_status 决定命名空间是否管控（无行=非管控），
        #    LEFT JOIN res_control 取服务管控值；ctrl_deploy 作哨兵区分“服务未命中”与“值为0”。
        #    走 asyncpg 连接池，带 5s 查询超时早于 webhook 30s 返回。
        #    admission_namespace 为 JSON 数组字符串，用 LIKE '%"ns"%' 判断包含。
        query = (
            "SELECT a.scheduler, a.nms_not_confirm, "
            "c.pod_count, c.pod_count_ai, c.pod_count_manual, "
            "c.request_cpu_m, c.request_mem_mb, c.limit_cpu_m, c.limit_mem_mb, "
            "c.deployment AS ctrl_deploy, "
            "c.jvm_xms_bytes, c.jvm_xmx_bytes, c.jvm_xss_bytes, c.jvm_max_metaspace_bytes "
            "FROM k8s_agent_status AS a "
            "LEFT JOIN k8s_res_control AS c "
            "  ON c.env = $1 AND c.namespace = $2 AND c.deployment = $3 "
            "WHERE a.env = $1 AND a.admission = true "
            "  AND a.admission_namespace LIKE $4"
        )
        like_ns = f'%"{namespace}"%'
        rows = await pg_fetch(query, env, namespace, deployment, like_ns, timeout=5)

        if not rows:
            # agent_status 无匹配行 → 非管控命名空间
            result = [200, '非管控命名空间，直接放行']
        else:
            row = rows[0]
            scheduler = row[0]
            nms_not_confirm = row[1]
            ctrl_deploy = row[9]
            if ctrl_deploy == deployment:
                # 命中管控服务：按 8 元素契约返回（顺序与 agent 解包严格对齐）
                result = [
                    row[2],  # pod_count
                    row[3],  # pod_count_ai
                    row[4],  # pod_count_manual
                    row[5],  # request_cpu_m
                    row[6],  # request_mem_mb
                    row[7],  # limit_cpu_m
                    row[8],  # limit_mem_mb
                    scheduler,
                ]
                if include_jvm:
                    result.append(dict(zip(JVM_FIELDS, row[10:14])))
                logger.info(f"🔊master(admis)返回:【{env}】【{namespace}】【{deployment}】{result}")
            elif nms_not_confirm:
                content = f'master(admis)返回: 新服务免确认已启用【{env}】【{namespace}】【{deployment}】允许部署/扩缩容,因为k8s_res_control表中找不到该服务,该服务不会被管控，也不会配置固定节点均衡模式（未开启则忽略）。'
                logger.warning(content)
                result = [200, content]
            else:
                content = f"master(admis)返回:【{env}】【{namespace}】【{deployment}】部署失败: k8s_res_control表中找不到该服务，且未开启新服务免确认，请先新增服务。"
                logger.warning(content)
                result = [404, content]

        # 3) 正常结果写缓存（异常分支不缓存，见下方 except）
        with _admis_cache_lock:
            _admis_cache[cache_key] = (result, now + ADMIS_CACHE_TTL)
        return result
    except Exception as e:
        # 全异常兜底：返回 2 元素让 agent 秒级失败，而非干等 30s 超时。异常不写缓存。
        content = f"master(admis)返回:【{env}】【{namespace}】【{deployment}】查询数据库失败：{e}"
        logger.error(content)
        return [503, '查询数据库异常']


def _send_msg_sync(content, msgToken=None):
    """同步发送消息（内部使用）"""
    response = ""
    token = msgToken if msgToken is not None else MSG_TOKEN
    if MSG_TYPE == "wecom":
        response = wecom(token, content, DEFAULT_AT)
    elif MSG_TYPE == "dingding":
        response = dingding(token, content, DEFAULT_AT)
    elif MSG_TYPE == "feishu":
        response = feishu(token, content, DEFAULT_AT)
    elif MSG_TYPE == "slack":
        response = slack(token, content, DEFAULT_AT)
    return f'【{MSG_TYPE}】{response}'


def send_msg(content, msgToken=None):
    """非阻塞发送消息"""
    try:
        loop = asyncio.get_running_loop()
        loop.run_in_executor(_executor, _send_msg_sync, content, msgToken)
    except RuntimeError:
        _send_msg_sync(content, msgToken)


def wecom(webhook, content, at=""):
    webhook = 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=' + webhook
    headers = {'Content-Type': 'application/json'}
    params = {'msgtype': 'markdown', 'markdown': {'content': f"{content}<@{at}>"}}
    data = bytes(json.dumps(params), 'utf-8')
    response = requests.post(webhook, headers=headers, data=data, timeout=MSG_TIMEOUT)
    logger.info(f'【wecom】{response.json()}')
    return response.json()


def dingding(webhook, content, at=""):
    webhook = 'https://oapi.dingtalk.com/robot/send?access_token=' + webhook
    headers = {'Content-Type': 'application/json'}
    params = {
        "msgtype": "markdown",
        "markdown": {"title": "告警", "text": content},
        "at": {"atMobiles": [at]},
    }
    data = bytes(json.dumps(params), 'utf-8')
    response = requests.post(webhook, headers=headers, data=data, timeout=MSG_TIMEOUT)
    logger.info(f'【dingding】{response.json()}')
    return response.json()


def feishu(webhook, content, at=""):
    title = "告警通知"
    webhook = f'https://open.feishu.cn/open-apis/bot/v2/hook/{webhook}'
    headers = {'Content-Type': 'application/json'}
    params = {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text", "content": title}, "template": "red"},
            "elements": [
                {
                    "tag": "markdown",
                    "content": f"{content}\n<at id={at}></at>",
                }
            ],
        },
    }
    data = json.dumps(params)
    response = requests.post(webhook, headers=headers, data=data, timeout=MSG_TIMEOUT)
    logger.info(f'【feishu】{response.json()}')
    return response.json()


def slack(webhook, content, at=""):
    """发送Slack告警通知"""
    # 构建完整的Slack Webhook URL
    webhook_url = f'https://hooks.slack.com/services/{webhook}'
    headers = {'Content-Type': 'application/json'}

    # 构建消息内容，如果有@用户则添加
    message_text = content
    if at:
        message_text += f" <@{at}>"

    params = {"text": message_text}

    data = json.dumps(params)
    response = requests.post(webhook_url, headers=headers, data=data, timeout=MSG_TIMEOUT)
    logger.info(f'【slack】{response.json()}')
    return response.json()


async def get_list_from_resources(env_value):
    """获取资源表信息，取最近10天cpu数据最高的一天的数据"""
    query = """
        select
            date,
            env,
            namespace,
            deployment,
            pod_count,
            p95_pod_cpu_pct,
            p95_pod_wss_pct,
            request_pod_cpu_m,
            request_pod_mem_mb,
            limit_pod_cpu_m,
            limit_pod_mem_mb,
            p95_pod_load,
            p95_pod_wss_mb,
            p95_pod_heap_pct,
            p95_pod_g1e_pct
        from k8s_resources
        where date = (
            SELECT date
            FROM k8s_resources
            WHERE date >= (CURRENT_DATE - 10) and env = $1
            GROUP BY date
            order by SUM(pod_count * p95_pod_load) desc
            limit 1
        ) and env = $1
    """
    result = await pg_fetch(query, env_value)
    logger.info("提取最近10天cpu最高的一天的数据：")
    for i in result:
        logger.debug(tuple(i))
    return [tuple(row) for row in result]


async def is_init_or_update(env_value):
    """判断管控表是初始化还是更新"""
    result = await pg_fetchval("select 1 from k8s_res_control where env = $1 limit 1", env_value)
    return result is None  # 无数据=初始化(True)，有数据=更新(False)


def parse_insert_data(srv):
    """将从resource表查到的指标数据，解析为可以存入管控表的数据"""
    # 把request-cpu,request-mem,limit-cpu,limit-mem这四个值转化为整数
    srv = list(srv)
    for j in range(7, 11):
        srv[j] = int(srv[j])
    tmp = [
        srv[1],
        srv[2],
        srv[3],
        srv[4],
        srv[4],
        -1,
        srv[5],
        srv[6],
        int(srv[11] * 1000),
        int(srv[12]),
        srv[9],
        srv[10],
        srv[0],
        -1,
        -1,
        -1,
        -1,
        -1,
        -1,
        -1,
        datetime(2000, 1, 1, 0, 0, 0),
        _nullable_jvm_pct(srv[13]) if len(srv) > 13 else None,
        _nullable_jvm_pct(srv[14]) if len(srv) > 14 else None,
    ]
    return tmp


_RES_CONTROL_COLUMNS = [
    'env', 'namespace', 'deployment', 'pod_count_init', 'pod_count', 'pod_count_manual',
    'p95_pod_cpu_pct', 'p95_pod_mem_pct', 'request_cpu_m', 'request_mem_mb',
    'limit_cpu_m', 'limit_mem_mb', 'update', 'pod_mem_saved_mb', 'pod_qps',
    'pod_g1gc_qps', 'pod_count_ai', 'pod_qps_ai', 'pod_load_ai', 'pod_g1gc_qps_ai', 'update_ai',
    'p95_pod_heap_pct',
    'p95_pod_g1e_pct',
]


async def init_control_data(rows):
    '''初始化管控表'''
    metrics_list = []
    for srv in rows:
        tmp = parse_insert_data(srv)
        logger.info(tmp)
        metrics_list.append(tuple(tmp))
    batch_size = 10000
    for i in range(0, len(metrics_list), batch_size):
        begin = time.time()
        batch_data = metrics_list[i : i + batch_size]
        try:
            await db.pg_copy_records('k8s_res_control', batch_data, _RES_CONTROL_COLUMNS)
            logger.info(
                f"== 正在插入批次: {i//batch_size}，"
                "耗时：{:.2f}s".format(time.time() - begin)
            )
        except Exception as e:
            logger.exception("Failed to insert batch {}: {}", i // batch_size, e)
            return False

    invalidate_admis_cache()  # 管控表已刷新，主动失效 admis 缓存
    return True


async def update_control_data(rows):
    """更新管控表"""
    for i in rows:
        (
            date,
            env,
            namespace,
            deployment,
            pod_count,
            p95_pod_cpu_pct,
            p95_pod_wss_pct,
            request_pod_cpu_m,
            request_pod_mem_mb,
            limit_pod_cpu_m,
            limit_pod_mem_mb,
            p95_pod_load,
            p95_pod_wss_mb,
        ) = i[:13]
        p95_pod_heap_pct = _nullable_jvm_pct(i[13]) if len(i) > 13 else None
        p95_pod_g1e_pct = _nullable_jvm_pct(i[14]) if len(i) > 14 else None
        exists = await pg_fetchval(
            "select 1 from k8s_res_control where env = $1 and namespace = $2 and deployment = $3 limit 1",
            env, namespace, deployment,
        )
        if exists:  # 更新
            request_cpu_m = int(p95_pod_load * 1000)
            try:
                await pg_execute(
                    """
                    update k8s_res_control set
                        "update" = $1,
                        pod_count = $2,
                        p95_pod_cpu_pct = $3,
                        p95_pod_mem_pct = $4,
                        request_cpu_m = $5,
                        request_mem_mb = $6,
                        p95_pod_heap_pct = $7,
                        p95_pod_g1e_pct = $8
                    where env = $9 and namespace = $10 and deployment = $11
                    """,
                    date, pod_count, p95_pod_cpu_pct, p95_pod_wss_pct,
                    request_cpu_m, int(p95_pod_wss_mb), p95_pod_heap_pct, p95_pod_g1e_pct,
                    env, namespace, deployment,
                )
            except Exception as e:
                logger.exception("Failed to update k8s_res_control: {}", e)
                return False
        else:  # 添加
            content = (
                f"采集高峰期数据更新到管控表时，检测到新服务【{env}】【{namespace}】【{deployment}】,将新增到管控表。"
            )
            logger.info(content)
            send_msg(content)
            try:
                tmp = parse_insert_data(i)
                await db.pg_copy_records('k8s_res_control', [tuple(tmp)], _RES_CONTROL_COLUMNS)
            except Exception as e:
                logger.exception("Failed to insert into k8s_res_control: {}", e)
                return False
    invalidate_admis_cache()  # 管控表已刷新，主动失效 admis 缓存
    return True


async def get_deployment_from_control_data(deployment_list, num, type, env):
    """根据指定指标获取排名靠前的deployment"""
    logger.info(f"开始获取 {env} 环境中排名靠前的deployment，类型: {type}，数量限制: {num}")
    top_deployments = []

    # 构造排序字段
    order_field = "request_cpu_m" if type == "cpu" else "request_mem_mb"

    # 为每个deployment查询资源控制数据
    for index, deployment in enumerate(deployment_list):
        namespace = deployment.get('namespace')
        pod = deployment.get('pod')
        # 从pod名称提取deployment_name，去掉最后两个由-分隔的部分
        deployment_name = pod.rsplit('-', 2)[0] if pod else ""
        logger.info(
            f"[{index+1}/{len(deployment_list)}] 查询deployment: {namespace}/{deployment_name}，原始Pod名称: {pod}"
        )

        try:
            result = await pg_fetch(
                "SELECT deployment, namespace, request_cpu_m, request_mem_mb "
                "FROM k8s_res_control "
                "WHERE env = $1 AND deployment = $2 AND namespace = $3",
                env, deployment_name, namespace,
            )
            if result:
                deployment_data = {
                    'deployment': result[0][0],
                    'namespace': result[0][1],
                    'request_cpu_m': result[0][2],
                    'request_mem_mb': result[0][3],
                }
                logger.info(
                    f"查询成功: {namespace}/{deployment_name}, CPU: {deployment_data['request_cpu_m']}m, 内存: {deployment_data['request_mem_mb']}MB"
                )
                top_deployments.append(deployment_data)
            else:
                logger.warning(f"未找到 {namespace}/{deployment_name} 的资源管控数据")
        except Exception as e:
            logger.error(f"查询 deployment {deployment_name} 资源数据失败: {e}")

    logger.info(f"查询完成，共找到 {len(top_deployments)} 个deployment的资源管控数据")

    # 根据指定字段排序
    if top_deployments:
        top_deployments.sort(key=lambda x: x[order_field], reverse=True)
        # 创建最终部署名称列表
        final_deploy_names = []
        for d in top_deployments:
            final_deploy_names.append(f"{d.get('namespace', 'unknown')}/{d.get('deployment', 'unknown')}")
        logger.info(f"最终返回 {len(top_deployments)} 个deployment: {json.dumps(final_deploy_names)}")

        # 限制返回数量
        if num > 0 and len(top_deployments) > num:
            logger.info(f"限制返回前 {num} 个deployment")
            top_deployments = top_deployments[:num]

    return top_deployments


def _get_deployment_node_sync(promql, k8s, namespace, deployment):
    """同步查询节点信息"""
    query = (
        promql.get("promql")
        .replace("{env_key}", f"{PROM_K8S_TAG_KEY},")
        .replace("{env}", f'{PROM_K8S_TAG_KEY}="{k8s}",')
        .replace("{namespace}", namespace)
        .replace("{deployment}", deployment)
    )
    logger.info(f"查询节点信息，query: {query}")
    response = requests.get(get_prom_url(), params={'query': query}, timeout=PROM_TIMEOUT)
    response.raise_for_status()
    data = response.json().get("data").get("result")

    node_dict = {}
    if data:
        for item in data:
            node_ip = item.get("metric", {}).get("node")
            value = item.get("value", [])
            if node_ip and len(value) >= 2:
                node_dict[node_ip] = value[1]
    return node_dict


async def get_deployment_node(promql, k8s, namespace, deployment):
    """异步查询节点信息"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, _get_deployment_node_sync, promql, k8s, namespace, deployment)


def _get_deployment_image_sync(promql, k8s, namespace, deployment):
    """同步查询镜像信息"""
    query = (
        promql.get("promql")
        .replace("{env_key}", f"{PROM_K8S_TAG_KEY},")
        .replace("{env}", f'{PROM_K8S_TAG_KEY}="{k8s}",')
        .replace("{namespace}", namespace)
        .replace("{deployment}", deployment)
    )
    logger.info(f"查询镜像信息，query: {query}")
    response = requests.get(get_prom_url(), params={'query': query}, timeout=PROM_TIMEOUT)
    response.raise_for_status()
    data = response.json().get("data").get("result")

    # 过滤有效数据
    valid_data = [i for i in data if i.get('metric', {}).get('image_spec', i.get('metric', {}).get('image', False))]

    # 检查数据条数
    if len(valid_data) == 0:
        raise ValueError(f"未找到deployment {namespace}/{deployment} 的镜像信息")
    elif len(valid_data) > 1:
        logger.info(f"deployment {namespace}/{deployment} 查询到多条镜像数据，期望只有一条")
        return k8s, 'retry'
    # 返回image标签的值
    return k8s, valid_data[0].get('metric').get('image_spec', valid_data[0].get('metric').get('image'))


async def get_deployment_image(promql, k8s, namespace, deployment):
    """异步查询镜像信息"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, _get_deployment_image_sync, promql, k8s, namespace, deployment)


def _get_node_res_rank_sync(env_value, res_type):
    """同步查询节点资源排名"""
    query = node_rank_query.get(res_type).replace("{env}", f'{PROM_K8S_TAG_KEY}="{env_value}",')
    logger.info(f'查询节点{res_type}排名，环境: {env_value}')
    logger.info(query)
    response = requests.get(get_prom_url(), params={'query': query}, timeout=PROM_TIMEOUT)
    response.raise_for_status()
    data = response.json().get("data").get("result")
    res_list = [
        {
            'name': i.get('metric').get('instance', i.get('metric').get('node')),
            'percent': round(float(i['value'][1]), 2),
        }
        for i in data
        if 'value' in i and len(i['value']) > 1
    ]
    res_list.sort(key=lambda x: x['percent'])
    logger.info(f'节点{res_type}从小到大排序{res_list}')
    return res_list


async def get_node_res_rank(env_value, res_type):
    """异步查询节点资源排名"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, _get_node_res_rank_sync, env_value, res_type)
