"""批量补采当前 Deployment 配置，仅填充管控表尚未配置的字段。"""

from loguru import logger

import db


JVM_FIELDS = (
    'jvm_xms_bytes',
    'jvm_xmx_bytes',
    'jvm_xss_bytes',
    'jvm_max_metaspace_bytes',
)
DEPLOYMENT_STRATEGY_FIELDS = ('max_unavailable', 'max_surge')
CONFIG_FIELDS = JVM_FIELDS + DEPLOYMENT_STRATEGY_FIELDS
# 与 Web 页面编辑接口一致，避免 JSON number 转换后丢失整数精度。
MAX_JVM_BYTES = (1 << 53) - 1
MAX_ROLLING_UPDATE_COUNT = (1 << 31) - 1


def normalize_rolling_update_value(value):
    """校验并规范化 Kubernetes IntOrString rollingUpdate 字段。"""
    if value is None:
        return None
    if type(value) is int:
        if not 0 <= value <= MAX_ROLLING_UPDATE_COUNT:
            raise ValueError('rollingUpdate 数量超出 Kubernetes int32 范围')
        return str(value)
    if not isinstance(value, str):
        raise ValueError('rollingUpdate 值必须是非负整数或百分比字符串')

    text = value.strip()
    if text.endswith('%'):
        digits = text[:-1]
        if not digits.isascii() or not digits.isdigit():
            raise ValueError('rollingUpdate 百分比格式无效')
        percent = int(digits)
        if percent > 100:
            raise ValueError('rollingUpdate 百分比必须在 0% 到 100% 之间')
        return f'{percent}%'
    if not text.isascii() or not text.isdigit():
        raise ValueError('rollingUpdate 值必须是非负整数或百分比字符串')
    count = int(text)
    if count > MAX_ROLLING_UPDATE_COUNT:
        raise ValueError('rollingUpdate 数量超出 Kubernetes int32 范围')
    return str(count)


def normalize_configs(records):
    """校验 agent 批量响应，返回可更新的唯一服务及无效条目数。"""
    if not isinstance(records, list):
        raise ValueError('agent JVM 响应 data 必须是数组')

    configs = []
    invalid = 0
    seen = set()
    for record in records:
        if not isinstance(record, dict):
            invalid += 1
            continue
        namespace = record.get('namespace')
        deployment = record.get('deployment')
        if (
            not isinstance(namespace, str) or not namespace.strip()
            or not isinstance(deployment, str) or not deployment.strip()
        ):
            invalid += 1
            continue
        values = [record.get(field) for field in JVM_FIELDS]
        for index, value in enumerate(values):
            if value is not None and (type(value) is not int or not 0 <= value <= MAX_JVM_BYTES):
                values[index] = None
                invalid += 1
        strategy_values = []
        for field in DEPLOYMENT_STRATEGY_FIELDS:
            try:
                strategy_values.append(normalize_rolling_update_value(record.get(field)))
            except ValueError:
                strategy_values.append(None)
                invalid += 1
        key = (namespace, deployment)
        if key in seen:
            invalid += 1
            continue
        seen.add(key)
        if any(value is not None for value in (*values, *strategy_values)):
            configs.append((namespace, deployment, *values, *strategy_values))
    return configs, invalid


async def fill_missing_configs(env, configs):
    """一条 UPDATE 批量补空；既有值（含 0）及未管控的服务均不修改。"""
    if not configs:
        return 0

    # 平行数组用 unnest 合并成关系，600 个服务仍只需要一次数据库调用。
    columns = [list(column) for column in zip(*configs)]
    assignments = ', '.join(f'{field} = COALESCE(c.{field}, v.{field})' for field in CONFIG_FIELDS)
    missing = ' OR '.join(f'(c.{field} IS NULL AND v.{field} IS NOT NULL)' for field in CONFIG_FIELDS)
    sql = f"""
        UPDATE k8s_res_control AS c
        SET {assignments}
        FROM unnest($2::text[], $3::text[], $4::bigint[], $5::bigint[], $6::bigint[], $7::bigint[],
                    $8::text[], $9::text[])
            AS v(namespace, deployment, {', '.join(CONFIG_FIELDS)})
        WHERE c.env = $1 AND c.namespace = v.namespace AND c.deployment = v.deployment
          AND ({missing})
    """
    status = await db.pg_execute(sql, env, *columns)
    return int(status.rsplit(' ', 1)[-1])


async def collect_current_configs(env, call_agent_api, invalidate_cache):
    """当前 JVM 数据与历史 CPU/内存分离；失败可在下一轮补采重试。"""
    stats = {'success': False, 'received': 0, 'eligible': 0, 'updated': 0, 'invalid': 0}
    try:
        response = await call_agent_api(env, '/api/agent/jvm/configs', timeout=60)
        if not isinstance(response, dict) or response.get('success') is not True:
            reason = (
                response.get('error') or response.get('message')
                if isinstance(response, dict) else 'agent 响应格式错误'
            )
            raise ValueError(reason or 'agent 未成功返回 JVM 配置（可能尚未升级）')
        configs, invalid = normalize_configs(response.get('data'))
        stats.update(received=len(response['data']), eligible=len(configs), invalid=invalid)
        stats['updated'] = await fill_missing_configs(env, configs)
        if stats['updated']:
            invalidate_cache()
        stats['success'] = True
        if invalid:
            stats['warning'] = f'{env}: Deployment 配置补采忽略了 {invalid} 条无效配置'
            logger.warning(stats['warning'])
        logger.info(f"{env}: Deployment 配置补采完成，收到 {stats['received']} 条，补空 {stats['updated']} 个管控服务")
    except Exception as exc:
        stats['warning'] = f'{env}: Deployment 配置补采失败，下一轮重试：{exc}'
        logger.warning(stats['warning'])
    return stats
