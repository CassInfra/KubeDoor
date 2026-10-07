import requests
import json
import utils


PROM_K8S_TAG_KEY = utils.PROM_K8S_TAG_KEY
PROMETHEUS_URL = f"{utils.PROM_URL}/api/v1/query"


# 定义PromQL查询
def process_promql_queries(env_key, env_value, namespace_value):
    if namespace_value:
        namespace_part = f'namespace="{namespace_value}",'
    else:
        namespace_part = ""

    promql_queries = {
        "pod_count": f'max by({env_key},namespace,deployment)(kube_deployment_spec_replicas{{{namespace_part} {env_key}="{env_value}"}})',
        # 先按 pod 取最大的容器再平均,避免 sandbox / sidecar 的 series 把平均值摊薄
        "avg_cpu_usage": f'avg by({env_key},namespace,deployment) (max by({env_key},namespace,pod) (rate(container_cpu_usage_seconds_total{{{namespace_part} {env_key}="{env_value}",container!="",container!="POD"}}[2m])) * on ({env_key},namespace,pod) group_left(deployment) label_replace(kube_pod_owner{{owner_kind="ReplicaSet", {namespace_part} {env_key}="{env_value}"}},"deployment","$1","owner_name","(.*)-[a-z0-9]+"))*1000',
        "max_cpu_usage": f'max by({env_key},namespace,deployment) (rate(container_cpu_usage_seconds_total{{{namespace_part} {env_key}="{env_value}",container!="",container!="POD"}}[2m]) * on ({env_key},namespace,pod) group_left(deployment) label_replace(kube_pod_owner{{owner_kind="ReplicaSet", {namespace_part} {env_key}="{env_value}"}},"deployment","$1","owner_name","(.*)-[a-z0-9]+"))*1000',
        "cpu_requests": f'avg by({env_key},namespace,deployment) (kube_pod_container_resource_requests{{resource="cpu", {namespace_part} {env_key}="{env_value}"}} * on ({env_key},namespace,pod) group_left(deployment) label_replace(kube_pod_owner{{owner_kind="ReplicaSet", {namespace_part} {env_key}="{env_value}"}},"deployment","$1","owner_name","(.*)-[a-z0-9]+"))*1000',
        "cpu_limit": f'avg by({env_key},namespace,deployment) (kube_pod_container_resource_limits{{resource="cpu", {namespace_part} {env_key}="{env_value}"}} * on ({env_key},namespace,pod) group_left(deployment) label_replace(kube_pod_owner{{owner_kind="ReplicaSet", {namespace_part} {env_key}="{env_value}"}},"deployment","$1","owner_name","(.*)-[a-z0-9]+"))*1000',
        "avg_memory_wss": f'max by({env_key},namespace,deployment) (container_memory_working_set_bytes{{{namespace_part} {env_key}="{env_value}",container!="",container!="POD"}} * on ({env_key},namespace,pod) group_left(deployment) label_replace(kube_pod_owner{{owner_kind="ReplicaSet", {namespace_part} {env_key}="{env_value}"}},"deployment","$1","owner_name","(.*)-[a-z0-9]+"))/1024/1024',
        "max_memory_wss": f'max by({env_key},namespace,deployment) (container_memory_working_set_bytes{{{namespace_part} {env_key}="{env_value}",container!="",container!="POD"}} * on ({env_key},namespace,pod) group_left(deployment) label_replace(kube_pod_owner{{owner_kind="ReplicaSet", {namespace_part} {env_key}="{env_value}"}},"deployment","$1","owner_name","(.*)-[a-z0-9]+"))/1024/1024',
        "mem_requests": f'avg by({env_key},namespace,deployment) (kube_pod_container_resource_requests{{resource="memory", {namespace_part} {env_key}="{env_value}"}} * on ({env_key},namespace,pod) group_left(deployment) label_replace(kube_pod_owner{{owner_kind="ReplicaSet", {namespace_part} {env_key}="{env_value}"}},"deployment","$1","owner_name","(.*)-[a-z0-9]+"))/1024/1024',
        "mem_limit": f'avg by({env_key},namespace,deployment) (kube_pod_container_resource_limits{{resource="memory", {namespace_part} {env_key}="{env_value}"}} * on ({env_key},namespace,pod) group_left(deployment) label_replace(kube_pod_owner{{owner_kind="ReplicaSet", {namespace_part} {env_key}="{env_value}"}},"deployment","$1","owner_name","(.*)-[a-z0-9]+"))/1024/1024',
        "ms_image": f'group by({env_key},namespace,deployment,image_spec,image) (kube_pod_container_info{{{namespace_part} {env_key}="{env_value}",container!="",container!="POD",image!=""}} * on ({env_key},namespace,pod) group_left(deployment) label_replace(kube_pod_owner{{owner_kind="ReplicaSet", {namespace_part} {env_key}="{env_value}"}},"deployment","$1","owner_name","(.*)-[a-z0-9]+"))',
    }
    return promql_queries


# Prometheus查询函数
def query_prometheus(promql):
    params = {'query': promql}
    try:
        response = requests.get(PROMETHEUS_URL, params=params, timeout=utils.PROM_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        return data['data']['result']
    except requests.exceptions.RequestException as e:
        print(f"Error querying Prometheus: {e}")
        return []


# 获取所有指标的数据
def get_metrics_data(env_value, namespace_value):
    # 用于存储所有数据的字典
    metrics_data = {}
    query_dict = process_promql_queries(PROM_K8S_TAG_KEY, env_value, namespace_value)
    # 执行每个查询
    for metric, query in query_dict.items():
        result = query_prometheus(query)
        metrics_data[metric] = result

    return metrics_data


# 四舍五入到整数
def round_to_int(value):
    try:
        return round(float(value))
    except (ValueError, OverflowError):  # NaN / ±Inf
        return 0


# 处理并整合指标数据
# 结果行:[env, namespace, deployment, pod_count, <VALUE_METRICS 各一列>, ms_image]
VALUE_METRICS = [
    'avg_cpu_usage',
    'max_cpu_usage',
    'cpu_requests',
    'cpu_limit',
    'avg_memory_wss',
    'max_memory_wss',
    'mem_requests',
    'mem_limit',
]


def process_metrics_data(metrics_data):
    # 先把每个指标按 (env, namespace, deployment) 建好索引再拼行。原来是每个 deployment
    # 都把所有指标的结果从头扫一遍(O(n²)),服务一多光拼数据就要很久
    def series_key(metric):
        return (metric.get(PROM_K8S_TAG_KEY), metric.get('namespace'), metric.get('deployment'))

    def index_values(metric_name):
        # 同一个 key 有多条时取最后一条,和原逻辑一致
        return {series_key(item['metric']): round_to_int(item['value'][1]) for item in metrics_data[metric_name]}

    pod_counts = index_values('pod_count')
    value_indexes = [index_values(name) for name in VALUE_METRICS]

    images = {}
    for item in metrics_data.get('ms_image', []):
        metric = item.get('metric', {})
        image = metric.get('image_spec') or metric.get('image')
        if image:
            images.setdefault(series_key(metric), set()).add(image)

    # 最终的结果列表,deployment 以 pod_count 里出现的为准
    final_data = []
    for key, pod_count in pod_counts.items():
        row = [*key, pod_count]
        row += [index.get(key, 0) for index in value_indexes]
        row.append(",".join(sorted(images.get(key, ()))))
        final_data.append(row)

    return final_data
