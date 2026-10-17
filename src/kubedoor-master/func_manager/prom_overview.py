import asyncio
import aiohttp
from loguru import logger

import utils


_REGEX_META = set('\\.^$|?*+()[]{}')


def _promql_regex(values):
    """把集群名拼成 PromQL 正则字符串字面量:先转义正则元字符,再转义字符串里的反斜杠和引号。"""
    pattern = '|'.join(''.join('\\' + c if c in _REGEX_META else c for c in value) for value in values)
    return pattern.replace('\\', '\\\\').replace('"', '\\"')


def scope_key_filter(principal):
    """不指定集群时,按当前用户能看到的集群拼标签过滤;全部可见返回 None(用默认的 key!="")。"""
    key = utils.PROM_K8S_TAG_KEY
    mode, envs = principal.env_scope()
    if mode == 'all':
        return None
    if mode == 'deny':
        return f'{key}!="",{key}!~"{_promql_regex(envs)}"'
    if not envs:
        return f'{key}="__kubedoor_no_cluster__"'
    return f'{key}=~"{_promql_regex(envs)}"'


def build_queries(env=None, key_filter=None):
    key = utils.PROM_K8S_TAG_KEY
    if key_filter is None:
        key_filter = f'{key}="{env}"' if env else f'{key}!=""'
    return {
        "clusters": f'count(group by({key}) (kube_node_info{{{key_filter},node!~"bursting-node|virtual-kubelet"}}))',
        "nodes": f'count(kube_node_info{{{key_filter},node!~"bursting-node|virtual-kubelet"}})',
        "workloads": f'count(group by({key}, namespace, owner_name)(kube_pod_owner{{owner_is_controller="true", {key_filter}}}))',
        "pods": f'count(group by({key}, namespace, pod)(kube_pod_owner{{owner_is_controller="true", {key_filter}}}))',
        "pvcs": f'count(group(kubelet_volume_stats_capacity_bytes{{{key_filter}}}) by ({key}, namespace, persistentvolumeclaim))',
        "total_cpu_cores": f'sum(kube_node_status_allocatable{{{key_filter},resource="cpu",unit="core",node!~"bursting-node|virtual-kubelet"}})',
        "total_mem_bytes": f'sum(kube_node_status_allocatable{{{key_filter},resource="memory",unit="byte",node!~"bursting-node|virtual-kubelet"}})',
        "used_cpu_cores": f'sum(irate(container_cpu_usage_seconds_total{{{key_filter},container!="",container!="POD",instance!~"bursting-node|virtual-kubelet"}}[3m]))',
        "used_mem_bytes": f'sum(container_memory_working_set_bytes{{{key_filter},container!="",container!="POD", instance!~"bursting-node|virtual-kubelet"}})',
        "net_in_bytes_per_sec": f'sum(irate(container_network_receive_bytes_total{{{key_filter},container="POD", instance!~"bursting-node|virtual-kubelet"}}[3m]))',
        "net_out_bytes_per_sec": f'sum(irate(container_network_transmit_bytes_total{{{key_filter},container="POD", instance!~"bursting-node|virtual-kubelet"}}[3m]))',
        "nodes_pod_gt_90": f'''count((
            count by (node) (
              kube_pod_info{{{key_filter}, created_by_kind!~"<none>|Job",node!~"bursting-node|virtual-kubelet"}}
            )
            /
            sum by (node) (
              kube_node_status_allocatable{{{key_filter}, resource="pods",node!~"bursting-node|virtual-kubelet"}}
            )
          ) > 0.9)''',
        "nodes_cpu_gt_70": f'''count((
            sum by (instance) (
              irate(container_cpu_usage_seconds_total{{{key_filter},container!="",container!="POD",instance!~"bursting-node|virtual-kubelet"}}[3m])
            )
            /
            sum by (instance) (
              label_replace(
                kube_node_status_allocatable{{{key_filter},resource="cpu",unit="core",node!~"bursting-node|virtual-kubelet"}},
                "instance","$1","node","(.*)"
              )
            ) * 100
          ) > 70)''',
        "nodes_mem_gt_80": f'''count((
            sum by (instance) (
              container_memory_working_set_bytes{{{key_filter},container!="",container!="POD", instance!~"bursting-node|virtual-kubelet"}}
            )
            /
            sum by (instance) (
              label_replace(
                kube_node_status_allocatable{{{key_filter},resource="memory",unit="byte",node!~"bursting-node|virtual-kubelet"}},
                "instance","$1","node","(.*)"
              )
            ) * 100
          ) > 80)''',
        "pvcs_usage_gt_80": f'''count((
            sum(kubelet_volume_stats_used_bytes{{{key_filter}}}) by ({key}, namespace, persistentvolumeclaim)
            /
            sum(kubelet_volume_stats_capacity_bytes{{{key_filter}}}) by ({key}, namespace, persistentvolumeclaim)
            * 100
          ) > 80)''',
        "max_pvc_usage_percent": f'''topk(1, (
            sum(kubelet_volume_stats_used_bytes{{{key_filter}}}) by ({key}, namespace, persistentvolumeclaim)
            /
            sum(kubelet_volume_stats_capacity_bytes{{{key_filter}}}) by ({key}, namespace, persistentvolumeclaim)
            * 100
          ))''',
        "max_cpu_request_percent": f'topk(1, (sum by ({key},node) (kube_pod_container_resource_requests{{{key_filter},resource="cpu",unit="core",node!~"bursting-node|virtual-kubelet"}}) / sum by ({key},node) (kube_node_status_allocatable{{{key_filter},resource="cpu",unit="core",node!~"bursting-node|virtual-kubelet"}}))) * 100',
        "max_mem_request_percent": f'topk(1, (sum by ({key},node) (kube_pod_container_resource_requests{{{key_filter},resource="memory",unit="byte",node!~"bursting-node|virtual-kubelet"}}) / sum by ({key},node) (kube_node_status_allocatable{{{key_filter},resource="memory",unit="byte",node!~"bursting-node|virtual-kubelet"}}))) * 100',
        "max_cpu_usage_percent": f'''topk(1, (
            sum(irate(container_cpu_usage_seconds_total{{{key_filter},container!="",container!="POD",instance!~"bursting-node|virtual-kubelet"}}[3m])) by ({key}, instance)
            /
            sum(label_replace(kube_node_status_allocatable{{{key_filter},resource="cpu",unit="core",node!~"bursting-node|virtual-kubelet"}}, "instance", "$1", "node", "(.*)")) by ({key}, instance)
            * 100
          ))''',
        "max_mem_usage_percent": f'''topk(1, (
            sum(container_memory_working_set_bytes{{{key_filter},container!="",container!="POD",instance!~"bursting-node|virtual-kubelet"}}) by ({key}, instance)
            /
            sum(label_replace(kube_node_status_allocatable{{{key_filter},resource="memory",unit="byte",node!~"bursting-node|virtual-kubelet"}}, "instance", "$1", "node", "(.*)")) by ({key}, instance)
            * 100
          ))''',
        "max_pod_usage_percent": f'''topk(1, (
            count by ({key}, node) (
              kube_pod_info{{origin_prometheus!="", {key_filter}, created_by_kind!~"<none>|Job",node!~"bursting-node|virtual-kubelet"}}
            )
            /
            sum by ({key}, node) (
              kube_node_status_allocatable{{origin_prometheus!="", {key_filter}, resource="pods",node!~"bursting-node|virtual-kubelet"}}
            )
            * 100
          ))''',
    }


async def _fetch(session, url, name, q):
    try:
        logger.info(f"📊【{name}】:\n{q}\n")
        async with session.get(url, params={"query": q}) as resp:
            resp.raise_for_status()
            data = await resp.json()
            return data.get("data", {}).get("result", [])
    except Exception as exc:
        logger.exception("prom_overview query [{}] failed: {}", name, exc)
        return []


def _aggregate(items):
    total = 0.0
    for item in items:
        v = item.get("value", [None, "0"])
        try:
            total += float(v[1])
        except (TypeError, ValueError):
            pass
    return total


def _format_max_metric(items):
    if not items:
        return ["", 0.0]
    item = items[0]
    labels = item.get("metric", {})
    key = utils.PROM_K8S_TAG_KEY
    key_value = labels.pop(key, "")
    other_values = [str(v) for v in labels.values()]
    label_str = "：".join([key_value] + other_values) if key_value else "：".join(other_values)
    value = item.get("value", [None, "0"])[1]
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = 0.0
    return [label_str, value]


async def get_overview_counts_async(env=None, key_filter=None):
    queries = build_queries(env, key_filter)
    url = utils.get_prom_url()
    async with aiohttp.ClientSession() as session:
        tasks = [_fetch(session, url, name, q) for name, q in queries.items()]
        results = await asyncio.gather(*tasks)
    final = {}
    max_metrics = {
        "max_pvc_usage_percent",
        "max_cpu_request_percent",
        "max_mem_request_percent",
        "max_cpu_usage_percent",
        "max_mem_usage_percent",
        "max_pod_usage_percent",
    }
    for (name, _), result in zip(queries.items(), results):
        if name in max_metrics:
            final[name] = _format_max_metric(result)
        else:
            final[name] = _aggregate(result)
    return final
