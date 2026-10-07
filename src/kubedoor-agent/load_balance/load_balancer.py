"""
🔄 K8S节点负载均衡模块
通过隔离高负载节点上的高CPU Pod，触发Deployment自动创建新Pod到低负载节点
"""

import asyncio
from datetime import datetime
from typing import Optional
from loguru import logger
from kubernetes_asyncio import client
from kubernetes_asyncio.client.rest import ApiException


class LoadBalancer:
    """负载均衡器核心类"""

    def __init__(self, core_api: client.CoreV1Api, apps_api: client.AppsV1Api,
                 custom_api: client.CustomObjectsApi):
        self.core_api = core_api
        self.apps_api = apps_api
        self.custom_api = custom_api

    def _parse_cpu(self, value: str) -> float:
        """解析CPU值，返回核数"""
        if value.endswith("n"):
            return int(value[:-1]) / 1e9
        elif value.endswith("u"):
            return int(value[:-1]) / 1e6
        elif value.endswith("m"):
            return int(value[:-1]) / 1000
        elif value.endswith("k") or value.endswith("K"):
            return int(value[:-1]) * 1000
        else:
            return float(value)

    def _parse_memory(self, value: str) -> int:
        """解析内存值，返回字节数"""
        if value.endswith("Ki"):
            return int(value[:-2]) * 1024
        elif value.endswith("Mi"):
            return int(value[:-2]) * 1024 * 1024
        elif value.endswith("Gi"):
            return int(value[:-2]) * 1024 * 1024 * 1024
        elif value.endswith("k") or value.endswith("K"):
            return int(value[:-1]) * 1000
        elif value.endswith("M"):
            return int(value[:-1]) * 1000 * 1000
        elif value.endswith("G"):
            return int(value[:-1]) * 1000 * 1000 * 1000
        else:
            return int(value)

    async def get_all_nodes_cpu(self) -> dict[str, dict]:
        """
        🖥️ 获取所有节点的资源使用情况（CPU、内存、Pod数）
        返回: {node_name: {cpu_percent, mem_percent, pod_count, ...}}
        """
        logger.info("🖥️ 开始获取所有节点资源使用情况...")
        result = {}

        try:
            # 获取节点列表
            nodes = await self.core_api.list_node()
            node_info = {}
            for node in nodes.items:
                name = node.metadata.name
                # CPU
                cpu_alloc = node.status.allocatable.get("cpu", "0")
                cpu_cores = self._parse_cpu(cpu_alloc)
                # 内存
                mem_alloc = node.status.allocatable.get("memory", "0")
                mem_bytes = self._parse_memory(mem_alloc)
                node_info[name] = {
                    "cpu_cores": cpu_cores,
                    "mem_bytes": mem_bytes,
                    "unschedulable": node.spec.unschedulable or False
                }

            # 获取所有Pod，统计每个节点的Pod数
            pods = await self.core_api.list_pod_for_all_namespaces()
            pod_count = {}
            for pod in pods.items:
                node_name = pod.spec.node_name
                if node_name:
                    pod_count[node_name] = pod_count.get(node_name, 0) + 1

            # 从metrics-server获取节点资源使用量
            metrics = await self.custom_api.list_cluster_custom_object(
                group="metrics.k8s.io",
                version="v1beta1",
                plural="nodes"
            )

            for item in metrics.get("items", []):
                name = item["metadata"]["name"]
                if name not in node_info:
                    continue
                # CPU使用量
                cpu_usage = item["usage"].get("cpu", "0")
                cpu_used = self._parse_cpu(cpu_usage)
                # 内存使用量
                mem_usage = item["usage"].get("memory", "0")
                mem_used = self._parse_memory(mem_usage)

                cpu_cores = node_info[name]["cpu_cores"]
                mem_bytes = node_info[name]["mem_bytes"]
                cpu_percent = (cpu_used / cpu_cores * 100) if cpu_cores > 0 else 0
                mem_percent = (mem_used / mem_bytes * 100) if mem_bytes > 0 else 0

                result[name] = {
                    "cpu_percent": round(cpu_percent, 1),
                    "cpu_used": round(cpu_used, 2),
                    "cpu_cores": cpu_cores,
                    "mem_percent": round(mem_percent, 1),
                    "pod_count": pod_count.get(name, 0),
                    "unschedulable": node_info[name]["unschedulable"]
                }

            logger.info(f"✅ 获取到 {len(result)} 个节点的资源使用情况")
            return result

        except ApiException as e:
            logger.error(f"❌ 获取节点资源失败: {e}")
            raise

    async def get_all_pods_cpu(self, exclude_namespaces: list[str]) -> list[dict]:
        """
        📦 获取所有Pod的CPU使用情况
        返回: [{name, namespace, node_name, cpu_used, deployment, owner_kind}]
        """
        logger.info("📦 开始获取所有Pod CPU使用情况...")
        result = []

        try:
            # 获取所有Pod
            pods = await self.core_api.list_pod_for_all_namespaces()
            pod_info = {}
            for pod in pods.items:
                ns = pod.metadata.namespace
                if ns in exclude_namespaces:
                    continue
                if pod.status.phase != "Running":
                    continue

                name = pod.metadata.name
                key = f"{ns}/{name}"

                # 获取owner信息
                owner_kind = None
                owner_name = None
                deployment_name = None

                if pod.metadata.owner_references:
                    owner = pod.metadata.owner_references[0]
                    owner_kind = owner.kind
                    owner_name = owner.name

                    # 如果owner是ReplicaSet，找到对应的Deployment
                    if owner_kind == "ReplicaSet":
                        try:
                            rs = await self.apps_api.read_namespaced_replica_set(owner_name, ns)
                            if rs.metadata.owner_references:
                                for ref in rs.metadata.owner_references:
                                    if ref.kind == "Deployment":
                                        deployment_name = ref.name
                                        break
                        except ApiException:
                            pass

                pod_info[key] = {
                    "name": name,
                    "namespace": ns,
                    "node_name": pod.spec.node_name,
                    "owner_kind": owner_kind,
                    "owner_name": owner_name,
                    "deployment": deployment_name,
                    "labels": pod.metadata.labels or {}
                }

            # 从metrics-server获取Pod CPU使用量
            metrics = await self.custom_api.list_cluster_custom_object(
                group="metrics.k8s.io",
                version="v1beta1",
                plural="pods"
            )

            for item in metrics.get("items", []):
                ns = item["metadata"]["namespace"]
                name = item["metadata"]["name"]
                key = f"{ns}/{name}"

                if key not in pod_info:
                    continue

                # 计算Pod总CPU使用量
                total_cpu = 0
                for container in item.get("containers", []):
                    cpu = container["usage"].get("cpu", "0")
                    if cpu.endswith("n"):
                        total_cpu += int(cpu[:-1]) / 1e9
                    elif cpu.endswith("u"):
                        total_cpu += int(cpu[:-1]) / 1e6
                    elif cpu.endswith("m"):
                        total_cpu += int(cpu[:-1]) / 1000
                    else:
                        total_cpu += int(cpu)

                info = pod_info[key]
                info["cpu_used"] = round(total_cpu, 3)
                result.append(info)

            logger.info(f"✅ 获取到 {len(result)} 个Pod的CPU使用情况")
            return result

        except ApiException as e:
            logger.error(f"❌ 获取Pod CPU失败: {e}")
            raise

    async def get_deployment_info(self, namespace: str, name: str) -> Optional[dict]:
        """
        📋 获取Deployment信息（副本数、反亲和配置）
        """
        try:
            deploy = await self.apps_api.read_namespaced_deployment(name, namespace)
            return self._extract_deployment_info(deploy)
        except ApiException as e:
            logger.warning(f"⚠️ 获取Deployment {namespace}/{name} 信息失败: {e}")
            return None

    def _extract_deployment_info(self, deploy) -> dict:
        """从Deployment对象提取信息"""
        # 检查Pod反亲和配置
        has_anti_affinity = False
        affinity = deploy.spec.template.spec.affinity
        if affinity and affinity.pod_anti_affinity:
            anti = affinity.pod_anti_affinity
            if anti.required_during_scheduling_ignored_during_execution or \
               anti.preferred_during_scheduling_ignored_during_execution:
                has_anti_affinity = True

        return {
            "name": deploy.metadata.name,
            "namespace": deploy.metadata.namespace,
            "replicas": deploy.spec.replicas or 0,
            "available_replicas": deploy.status.available_replicas or 0,
            "has_anti_affinity": has_anti_affinity,
            "selector": deploy.spec.selector.match_labels
        }

    async def get_all_deployments_info(self, namespaces: set[str]) -> dict[str, dict]:
        """
        📋 批量获取所有Deployment信息
        """
        logger.info(f"📋 批量获取Deployment信息，涉及 {len(namespaces)} 个namespace...")
        result = {}

        for ns in namespaces:
            try:
                deploys = await self.apps_api.list_namespaced_deployment(ns)
                for deploy in deploys.items:
                    key = f"{ns}/{deploy.metadata.name}"
                    result[key] = self._extract_deployment_info(deploy)
            except ApiException as e:
                logger.warning(f"⚠️ 获取namespace {ns} 的Deployment列表失败: {e}")

        logger.info(f"✅ 获取到 {len(result)} 个Deployment信息")
        return result

    def filter_eligible_pods(self, pods: list[dict], deployments_info: dict,
                            min_replicas: int, blacklist: list[str]) -> list[dict]:
        """
        🔍 过滤可迁移的Pod
        条件：
        - 属于Deployment管理
        - 副本数 >= min_replicas
        - 不在黑名单中
        - 未被隔离（标签不含-ISOLATED）
        """
        logger.info("🔍 开始过滤可迁移的Pod...")
        logger.info(f"  📊 输入Pod数: {len(pods)}, Deployment数: {len(deployments_info)}, min_replicas: {min_replicas}")
        eligible = []

        # 统计过滤原因
        stats = {"not_deployment": 0, "blacklist": 0, "isolated": 0, "low_replicas": 0, "no_deploy_info": 0}

        for pod in pods:
            # 必须属于Deployment
            if pod["owner_kind"] != "ReplicaSet" or not pod.get("deployment"):
                stats["not_deployment"] += 1
                continue

            deploy_key = f"{pod['namespace']}/{pod['deployment']}"

            # 检查黑名单
            if deploy_key in blacklist or pod["deployment"] in blacklist:
                stats["blacklist"] += 1
                continue

            # 检查是否已隔离
            app_label = pod["labels"].get("app", "")
            if app_label.endswith("-ISOLATED"):
                stats["isolated"] += 1
                continue

            # 检查副本数
            deploy_info = deployments_info.get(deploy_key)
            if not deploy_info:
                stats["no_deploy_info"] += 1
                continue
            if deploy_info["replicas"] < min_replicas:
                stats["low_replicas"] += 1
                continue

            pod["deploy_info"] = deploy_info
            eligible.append(pod)

        logger.info(f"  📊 过滤统计: 非Deployment={stats['not_deployment']}, 黑名单={stats['blacklist']}, "
                   f"已隔离={stats['isolated']}, 副本数不足={stats['low_replicas']}, 无Deployment信息={stats['no_deploy_info']}")
        logger.info(f"✅ 过滤后剩余 {len(eligible)} 个可迁移Pod")
        return eligible

    def find_target_node(self, pod: dict, nodes_cpu: dict,
                        deployment_pods: list[dict], exclude_nodes: list[str] = None,
                        target_strategy: str = "average", target_percentile: int = 30) -> Optional[str]:
        """
        🎯 为Pod找到合适的目标节点（考虑反亲和）
        目标节点选择规则：
        - 排除源节点、不可调度节点、排除节点列表
        - 如果有反亲和，排除已有该Deployment Pod的节点
        - 目标节点负载必须低于阈值（确保迁移到低负载节点）
        - 从候选中选择负载最低的节点

        target_strategy: "average"=低于平均值, "percentile"=低于指定分位数
        target_percentile: 分位数阈值（如30表示低于30分位数）
        """
        deploy_info = pod.get("deploy_info", {})
        has_anti_affinity = deploy_info.get("has_anti_affinity", False)
        exclude_nodes = exclude_nodes or []

        # 获取该Deployment现有Pod的节点分布
        nodes_with_pod = set()
        if has_anti_affinity:
            for p in deployment_pods:
                if p["name"] != pod["name"] and p.get("deployment") == pod.get("deployment"):
                    nodes_with_pod.add(p["node_name"])

        # 计算目标节点上限阈值
        cpu_values = sorted([info["cpu_percent"] for info in nodes_cpu.values()])
        if target_strategy == "percentile" and cpu_values:
            # 分位数：低于指定分位数的节点
            idx = int(len(cpu_values) * target_percentile / 100)
            target_threshold = cpu_values[max(0, min(idx, len(cpu_values) - 1))]
        else:
            # 平均值：低于平均值的节点
            target_threshold = sum(cpu_values) / len(cpu_values) if cpu_values else 0

        # 过滤候选节点
        candidates = []
        for node_name, info in nodes_cpu.items():
            # 排除源节点
            if node_name == pod["node_name"]:
                continue
            # 排除不可调度节点
            if info.get("unschedulable"):
                continue
            # 排除指定的排除节点
            if node_name in exclude_nodes:
                continue
            # 如果有反亲和，排除已有该Deployment Pod的节点
            if has_anti_affinity and node_name in nodes_with_pod:
                continue
            # 目标节点负载必须低于阈值
            if info["cpu_percent"] >= target_threshold:
                continue
            candidates.append((node_name, info["cpu_percent"]))

        if not candidates:
            return None

        # 选择负载最低的节点
        candidates.sort(key=lambda x: x[1])
        return candidates[0][0]

    def generate_migration_plan(self, nodes_cpu: dict, pods: list[dict],
                               imbalance_threshold: float, balance_target: float,
                               exclude_nodes: list[str] = None,
                               source_strategy: str = "average", source_percentile: int = 70,
                               target_strategy: str = "average", target_percentile: int = 30,
                               min_pod_cpu: float = 0.5, max_iterations: int = 50) -> dict:
        """
        📝 生成迁移清单
        source_strategy: "average"=高于平均值, "percentile"=高于指定分位数
        source_percentile: 源节点分位数阈值（如70表示高于70分位数）
        target_strategy/target_percentile: 目标节点策略参数
        min_pod_cpu: 最小Pod CPU阈值（核），低于此值的Pod不参与迁移
        max_iterations: 最大迭代次数
        """
        logger.info("📝 开始生成迁移清单...")
        logger.info(f"  📋 配置: min_pod_cpu={min_pod_cpu}核, max_iterations={max_iterations}")
        exclude_nodes = exclude_nodes or []

        # 获取可调度节点（排除 unschedulable 节点用于极差计算）
        schedulable_nodes = {n: info for n, info in nodes_cpu.items()
                           if not info.get("unschedulable", False)}
        if not schedulable_nodes:
            return {"migrations": [], "message": "无可调度节点"}

        # 计算当前极差（只考虑可调度节点）
        cpu_values = [n["cpu_percent"] for n in schedulable_nodes.values()]
        current_range = max(cpu_values) - min(cpu_values)
        max_node = max(schedulable_nodes.items(), key=lambda x: x[1]["cpu_percent"])
        min_node = min(schedulable_nodes.items(), key=lambda x: x[1]["cpu_percent"])

        logger.info(f"📊 当前极差: {current_range:.1f}% (最高: {max_node[0]} {max_node[1]['cpu_percent']:.1f}%, 最低: {min_node[0]} {min_node[1]['cpu_percent']:.1f}%)")

        if current_range <= imbalance_threshold:
            return {
                "migrations": [],
                "current_range": round(current_range, 2),
                "message": f"当前极差 {current_range:.1f}% <= 阈值 {imbalance_threshold}%，无需均衡"
            }

        # 模拟负载数据（使用绝对值：核数，只考虑可调度节点）
        simulated_cpu_used = {n: info["cpu_used"] for n, info in schedulable_nodes.items()}
        node_cores = {n: info["cpu_cores"] for n, info in schedulable_nodes.items()}

        # 记录 unschedulable 节点数量
        unschedulable_count = len(nodes_cpu) - len(schedulable_nodes)
        if unschedulable_count > 0:
            logger.info(f"  ⚠️ 排除 {unschedulable_count} 个不可调度节点，剩余 {len(schedulable_nodes)} 个可调度节点参与极差计算")

        # 辅助函数：计算百分比极差
        def calc_percent_range(cpu_used_dict):
            percents = [cpu_used_dict[n] / node_cores[n] * 100 for n in cpu_used_dict]
            return max(percents) - min(percents)

        # 辅助函数：计算标准差
        def calc_std_dev(cpu_used_dict):
            percents = [cpu_used_dict[n] / node_cores[n] * 100 for n in cpu_used_dict]
            avg = sum(percents) / len(percents)
            variance = sum((p - avg) ** 2 for p in percents) / len(percents)
            return variance ** 0.5

        # 辅助函数：计算百分比平均值和最小值
        def calc_percent_stats(cpu_used_dict):
            percents = [cpu_used_dict[n] / node_cores[n] * 100 for n in cpu_used_dict]
            return sum(percents) / len(percents), min(percents)

        # 辅助函数：获取节点百分比
        def get_node_percent(node, cpu_used_dict):
            return cpu_used_dict[node] / node_cores[node] * 100

        migrations = []
        processed_pods = set()
        skipped_nodes = set()  # 记录已尝试但无法迁移的节点

        # 按节点分组Pod
        pods_by_node = {}
        for pod in pods:
            node = pod["node_name"]
            if node not in pods_by_node:
                pods_by_node[node] = []
            pods_by_node[node].append(pod)

        # 打印每个节点的可迁移Pod数量（按CPU降序）
        sorted_nodes = sorted(nodes_cpu.items(), key=lambda x: x[1]["cpu_percent"], reverse=True)
        logger.info("📊 各节点可迁移Pod数量:")
        for node_name, info in sorted_nodes[:10]:  # 只显示前10个高负载节点
            pod_count = len(pods_by_node.get(node_name, []))
            logger.info(f"  {node_name}: CPU={info['cpu_percent']:.1f}%, 可迁移Pod={pod_count}")

        # 循环生成迁移计划
        iteration_logs = []  # 记录每次迭代的详细日志
        for iteration in range(max_iterations):
            # 重新计算极差和平均值（百分比）
            sim_range = calc_percent_range(simulated_cpu_used)
            sim_avg, sim_min = calc_percent_stats(simulated_cpu_used)

            iteration_log = {
                "iteration": iteration + 1,
                "sim_range": round(sim_range, 2),
                "sim_avg": round(sim_avg, 2),
                "result": None,
                "details": None
            }

            logger.info(f"🔄 迭代 {iteration + 1}: 当前模拟极差={sim_range:.1f}%, 平均值={sim_avg:.1f}%")

            if sim_range < balance_target:
                iteration_log["result"] = "达到均衡目标"
                iteration_log["details"] = f"极差 {sim_range:.1f}% < 目标 {balance_target}%"
                iteration_logs.append(iteration_log)
                logger.info(f"  🎯 已达到均衡目标: 极差 {sim_range:.1f}% < {balance_target}%")
                break

            # 计算源节点阈值（百分比）
            node_percents = {n: get_node_percent(n, simulated_cpu_used) for n in simulated_cpu_used}
            sorted_percents = sorted(node_percents.values())
            if source_strategy == "percentile":
                # 分位数：高于指定分位数的节点
                idx = int(len(sorted_percents) * source_percentile / 100)
                source_threshold = sorted_percents[max(0, min(idx, len(sorted_percents) - 1))]
            else:
                # 平均值：高于平均值，且高于 (最低值 + balance_target/2)
                source_threshold = max(sim_avg, sim_min + balance_target / 2)

            available_nodes = [(n, node_percents[n]) for n in simulated_cpu_used
                              if n not in skipped_nodes and node_percents[n] > source_threshold]

            if not available_nodes:
                iteration_log["result"] = "无高负载节点"
                iteration_log["details"] = f"没有符合条件的高负载节点 (阈值: {source_threshold:.1f}%)"
                iteration_logs.append(iteration_log)
                logger.info(f"  ⚠️ 没有符合条件的高负载节点 (阈值: {source_threshold:.1f}%)")
                break

            high_node = max(available_nodes, key=lambda x: x[1])[0]
            high_node_percent = node_percents[high_node]
            logger.info(f"  📍 选择源节点: {high_node} (CPU={high_node_percent:.1f}%)")

            # 从该节点选择CPU最高的可迁移Pod
            node_pods = pods_by_node.get(high_node, [])
            node_pods = [p for p in node_pods if p["name"] not in processed_pods]
            # 过滤掉CPU低于阈值的Pod
            node_pods = [p for p in node_pods if p.get("cpu_used", 0) >= min_pod_cpu]
            if not node_pods:
                # 该节点没有可迁移的Pod，标记并尝试下一个节点
                iteration_log["result"] = "节点无可迁移Pod"
                iteration_log["details"] = f"节点 {high_node} 没有CPU>={min_pod_cpu}核的可迁移Pod"
                iteration_logs.append(iteration_log)
                logger.info(f"  ⏭️ 节点 {high_node} 没有CPU>={min_pod_cpu}核的可迁移Pod，标记跳过")
                skipped_nodes.add(high_node)
                continue

            node_pods.sort(key=lambda x: x.get("cpu_used", 0), reverse=True)
            logger.info(f"  📦 该节点有 {len(node_pods)} 个可迁移Pod (CPU>={min_pod_cpu}核)")

            migrated = False
            tried_pods = []  # 记录尝试过的Pod详情
            for pod in node_pods:
                pod_cpu = pod.get("cpu_used", 0)
                # 找目标节点（传入百分比用于选择）
                target = self.find_target_node(pod,
                    {n: {"cpu_percent": get_node_percent(n, simulated_cpu_used),
                         "unschedulable": nodes_cpu[n].get("unschedulable", False)}
                     for n in simulated_cpu_used},
                    pods, exclude_nodes, target_strategy, target_percentile)

                if not target:
                    tried_pods.append({
                        "pod": f"{pod['namespace']}/{pod['name']}",
                        "cpu": pod_cpu,
                        "result": "无目标节点"
                    })
                    logger.info(f"    ❌ Pod {pod['namespace']}/{pod['name']} (CPU={pod_cpu:.2f}核) 找不到合适的目标节点")
                    continue

                # 模拟迁移后的负载（直接加减核数，无假设系数）
                temp_cpu_used = simulated_cpu_used.copy()
                temp_cpu_used[high_node] -= pod_cpu
                temp_cpu_used[target] += pod_cpu
                new_range = calc_percent_range(temp_cpu_used)

                # 计算标准差变化
                old_std = calc_std_dev(simulated_cpu_used)
                new_std = calc_std_dev(temp_cpu_used)

                # 使用标准差减小作为判断条件（更准确反映整体均衡性）
                if new_std < old_std:
                    # 记录迁移前的源节点和目标节点百分比
                    source_percent_before = get_node_percent(high_node, simulated_cpu_used)
                    target_percent_before = get_node_percent(target, simulated_cpu_used)

                    simulated_cpu_used[high_node] -= pod_cpu
                    simulated_cpu_used[target] += pod_cpu
                    processed_pods.add(pod["name"])

                    # 记录迁移后的百分比
                    source_percent_after = get_node_percent(high_node, simulated_cpu_used)
                    target_percent_after = get_node_percent(target, simulated_cpu_used)

                    migrations.append({
                        "pod_name": pod["name"],
                        "namespace": pod["namespace"],
                        "deployment": pod["deployment"],
                        "source_node": high_node,
                        "target_node": target,
                        "cpu_used": pod_cpu,
                        # 记录迁移时的模拟百分比
                        "source_percent": round(source_percent_before, 1),
                        "target_percent": round(target_percent_before, 1),
                        "source_percent_after": round(source_percent_after, 1),
                        "target_percent_after": round(target_percent_after, 1),
                        # 显示源节点降低量和标准差变化
                        "expected_effect": f"源-{(source_percent_before - source_percent_after):.1f}% σ-{(old_std - new_std):.2f}"
                    })

                    # 记录成功的Pod
                    tried_pods.append({
                        "pod": f"{pod['namespace']}/{pod['name']}",
                        "cpu": pod_cpu,
                        "result": "成功",
                        "target": target,
                        "effect": f"σ {old_std:.2f}→{new_std:.2f}"
                    })

                    iteration_log["result"] = "成功添加迁移"
                    iteration_log["source_node"] = high_node
                    iteration_log["tried_pods"] = tried_pods
                    iteration_logs.append(iteration_log)
                    logger.info(f"    ✅ 添加迁移: {pod['namespace']}/{pod['name']} "
                               f"({high_node} → {target}), CPU: {pod_cpu:.2f}核, 标准差: {old_std:.2f} → {new_std:.2f}")
                    migrated = True
                    break
                else:
                    tried_pods.append({
                        "pod": f"{pod['namespace']}/{pod['name']}",
                        "cpu": pod_cpu,
                        "result": "标准差不减小",
                        "effect": f"σ {old_std:.2f}→{new_std:.2f}"
                    })
                    logger.info(f"    ⚠️ Pod {pod['namespace']}/{pod['name']} 迁移后标准差不减小 ({old_std:.2f} → {new_std:.2f})，跳过")

            if not migrated:
                # 该节点的所有Pod都无法迁移，标记并尝试下一个节点
                iteration_log["result"] = "节点所有Pod无法迁移"
                iteration_log["source_node"] = high_node
                iteration_log["tried_pods"] = tried_pods
                iteration_logs.append(iteration_log)
                logger.info(f"  ⏭️ 节点 {high_node} 所有Pod无法迁移")
                skipped_nodes.add(high_node)
                continue

        final_range = calc_percent_range(simulated_cpu_used)
        initial_std = calc_std_dev({n: schedulable_nodes[n]["cpu_used"] for n in schedulable_nodes})
        final_std = calc_std_dev(simulated_cpu_used)

        result = {
            "migrations": migrations,
            "current_range": round(current_range, 2),
            "expected_range": round(final_range, 2),
            "current_std": round(initial_std, 2),
            "expected_std": round(final_std, 2),
            "iteration_logs": iteration_logs,  # 添加迭代日志
            "node_status": {
                "before": {n: round(schedulable_nodes[n]["cpu_percent"], 2) for n in schedulable_nodes},
                "after": {n: round(get_node_percent(n, simulated_cpu_used), 2) for n in simulated_cpu_used}
            },
            # 添加绝对值数据（单位：m，只包含可调度节点）
            "node_cpu_used": {
                "before": {n: round(schedulable_nodes[n]["cpu_used"] * 1000) for n in schedulable_nodes},
                "after": {n: round(simulated_cpu_used[n] * 1000) for n in simulated_cpu_used}
            }
        }

        logger.info(f"📝 生成迁移清单完成: {len(migrations)} 个Pod待迁移, "
                   f"预期极差从 {current_range:.1f}% 降至 {final_range:.1f}%, 标准差从 {initial_std:.2f} 降至 {final_std:.2f}")

        return result

    async def execute_migration(self, migration: dict, all_nodes: list[str]) -> dict:
        """
        🚀 执行单个Pod的迁移（独立模式，自己管理cordon/uncordon）
        """
        target_node = migration["target_node"]
        cordoned_nodes = []

        try:
            # cordon其他节点
            for node in all_nodes:
                if node != target_node:
                    try:
                        body = {"spec": {"unschedulable": True}}
                        await self.core_api.patch_node(node, body)
                        cordoned_nodes.append(node)
                    except ApiException as e:
                        logger.warning(f"  ⚠️ Cordon {node} 失败: {e}")

            # 执行迁移核心逻辑
            return await self._do_migration(migration)

        finally:
            # uncordon所有节点
            for node in cordoned_nodes:
                try:
                    body = {"spec": {"unschedulable": False}}
                    await self.core_api.patch_node(node, body)
                except ApiException as e:
                    logger.warning(f"  ⚠️ Uncordon {node} 失败: {e}")

    async def _do_migration(self, migration: dict) -> dict:
        """
        🚀 执行迁移核心逻辑（隔离Pod并等待调度成功）
        返回: {success, old_pod, new_pod, target_node, selector_key, selector_value}
        """
        pod_name = migration["pod_name"]
        namespace = migration["namespace"]
        target_node = migration["target_node"]
        deployment_name = migration.get("deployment", "")

        logger.info(f"🚀 开始迁移 {namespace}/{pod_name} → {target_node}")

        try:
            # 获取 Deployment 的 selector，确定要修改哪个标签
            selector_key = "app"  # 默认使用 app 标签
            selector_value = None
            if deployment_name:
                try:
                    deploy = await self.apps_api.read_namespaced_deployment(deployment_name, namespace)
                    selector = deploy.spec.selector.match_labels or {}
                    if selector:
                        # 优先使用 app 标签，否则使用 selector 中的第一个标签
                        if "app" in selector:
                            selector_key = "app"
                            selector_value = selector["app"]
                        else:
                            selector_key = list(selector.keys())[0]
                            selector_value = selector[selector_key]
                        logger.info(f"  📋 Deployment selector: {selector_key}={selector_value}")
                except Exception as e:
                    logger.warning(f"  ⚠️ 获取Deployment selector失败: {e}，使用默认app标签")

            # 记录迁移前已存在的Pod名称（比时间戳更可靠，避免K8S API服务器和本地时间差异）
            existing_pods_before = set()
            try:
                pods_before = await self.core_api.list_namespaced_pod(
                    namespace,
                    label_selector=f"{selector_key}={selector_value}"
                )
                existing_pods_before = {p.metadata.name for p in pods_before.items}
                logger.info(f"  📋 迁移前已存在的Pod: {existing_pods_before}")
            except Exception as e:
                logger.warning(f"  ⚠️ 获取已存在Pod列表失败: {e}")

            # 读取 Pod 并修改标签
            logger.info(f"  🏷️ 修改Pod标签，隔离Pod...")
            pod = await self.core_api.read_namespaced_pod(pod_name, namespace)
            labels = pod.metadata.labels or {}

            # 如果没有从 Deployment 获取到 selector，尝试从 Pod 标签中获取
            if not selector_value:
                selector_value = labels.get(selector_key, "")

            if not selector_value:
                raise ValueError(f"Pod {pod_name} 没有 {selector_key} 标签")

            new_label_value = f"{selector_value}-ISOLATED"
            body = {"metadata": {"labels": {selector_key: new_label_value}}}
            await self.core_api.patch_namespaced_pod(pod_name, namespace, body)
            logger.info(f"  ✅ Pod标签已修改: {selector_key}={new_label_value}")

            # 等待新Pod调度成功
            logger.info(f"  ⏳ 等待新Pod调度到 {target_node}...")

            pending_pod_info = None  # 记录Pending的Pod信息
            for i in range(60):  # 最多等待60秒
                await asyncio.sleep(1)
                pods = await self.core_api.list_namespaced_pod(
                    namespace,
                    label_selector=f"{selector_key}={selector_value}"
                )

                for p in pods.items:
                    # 找到新创建的Pod（不在迁移前已存在的Pod列表中）
                    if p.metadata.name not in existing_pods_before:

                        actual_node = p.spec.node_name
                        # 检查是否已调度
                        is_scheduled = False
                        # 如果node_name有值，说明已经被调度到节点（即使容器还在创建中）
                        if actual_node:
                            is_scheduled = True
                        else:
                            # 检查PodScheduled condition
                            for condition in (p.status.conditions or []):
                                if condition.type == "PodScheduled" and condition.status == "True":
                                    is_scheduled = True
                                    break
                            # 如果已经Running，也算调度成功
                            if p.status.phase in ("Running", "Succeeded"):
                                is_scheduled = True

                        if is_scheduled:
                            if actual_node != target_node:
                                logger.warning(f"  ⚠️ 新Pod {p.metadata.name} 调度到了 {actual_node}，而不是目标节点 {target_node}")
                                # 调度到错误节点，返回失败
                                return {
                                    "success": False,
                                    "status": "wrong_node",
                                    "old_pod": pod_name,
                                    "new_pod": p.metadata.name,
                                    "namespace": namespace,
                                    "target_node": target_node,
                                    "actual_node": actual_node,
                                    "error": f"Pod调度到了错误节点 {actual_node}，期望 {target_node}"
                                }
                            else:
                                logger.info(f"  ✅ 新Pod {p.metadata.name} 已调度到 {target_node}")
                            return {
                                "success": True,
                                "status": "scheduled",
                                "old_pod": pod_name,
                                "new_pod": p.metadata.name,
                                "namespace": namespace,
                                "target_node": actual_node,  # 返回实际节点
                                "selector_key": selector_key,
                                "selector_value": selector_value
                            }

                    # 记录Pending状态的新Pod（用于超时时输出原因）
                    if p.metadata.name not in existing_pods_before and p.status.phase == "Pending":
                        pending_pod_info = {
                            "name": p.metadata.name,
                            "node": p.spec.node_name,
                            "conditions": []
                        }
                        for condition in (p.status.conditions or []):
                            if condition.type == "PodScheduled" and condition.status == "False":
                                # 优先使用message（包含详细原因），其次使用reason
                                reason_detail = condition.message or condition.reason or "未知原因"
                                pending_pod_info["conditions"].append(reason_detail)

                # 每15秒打印一次中间状态
                if i > 0 and i % 15 == 0 and pending_pod_info:
                    logger.info(f"  ⏳ 已等待{i}秒，Pod仍在Pending...")
                    if pending_pod_info.get("conditions"):
                        # 截取前200字符避免日志过长
                        logger.info(f"  📋 当前原因: {pending_pod_info['conditions'][-1][:200]}")

            # 超时，输出详细信息
            if pending_pod_info:
                logger.warning(f"  ⚠️ 等待新Pod调度超时，新Pod {pending_pod_info['name']} 处于Pending状态")
                if pending_pod_info["conditions"]:
                    logger.warning(f"  ⚠️ Pending原因: {', '.join(pending_pod_info['conditions'])}")
                if pending_pod_info["node"] and pending_pod_info["node"] != target_node:
                    logger.warning(f"  ⚠️ 新Pod被调度到了 {pending_pod_info['node']}，而不是目标节点 {target_node}")
            else:
                logger.warning(f"  ⚠️ 等待新Pod调度超时，未发现新Pod")
            return {"success": False, "status": "failed", "error": "等待新Pod调度超时", "old_pod": pod_name}

        except Exception as e:
            logger.error(f"  ❌ 迁移失败: {e}")
            return {"success": False, "status": "failed", "error": str(e), "old_pod": pod_name}

    async def _wait_pod_ready(self, namespace: str, pod_name: str, timeout: int = 120) -> bool:
        """等待Pod Ready"""
        logger.info(f"  ⏳ 等待Pod {pod_name} Ready...")
        for i in range(timeout):
            await asyncio.sleep(1)
            try:
                pod = await self.core_api.read_namespaced_pod(pod_name, namespace)
                # 检查所有容器是否Ready
                if pod.status.conditions:
                    for condition in pod.status.conditions:
                        if condition.type == "Ready" and condition.status == "True":
                            logger.info(f"  ✅ Pod {pod_name} Ready")
                            return True
            except ApiException:
                pass
        logger.warning(f"  ⚠️ 等待Pod {pod_name} Ready超时")
        return False

    async def execute_migrations_batch(self, migrations: list[dict], all_nodes: list[str],
                                       exclude_nodes: list[str] = None) -> dict:
        """
        🚀 批量执行迁移
        流程：cordon所有节点 → 逐个迁移 → 恢复节点
        exclude_nodes: 排除节点列表，这些节点始终保持cordon状态
        注意：不等待Pod Ready，由前端轮询检查
        """
        logger.info(f"🚀 开始批量迁移，共 {len(migrations)} 个Pod...")
        exclude_nodes = exclude_nodes or []

        # 0. 先获取所有节点的原始调度状态
        logger.info(f"  📋 获取节点原始调度状态...")
        originally_schedulable = set()  # 原本可调度的节点
        try:
            nodes = await self.core_api.list_node()
            for node in nodes.items:
                if not node.spec.unschedulable:
                    originally_schedulable.add(node.metadata.name)
            logger.info(f"  📋 原本可调度的节点: {len(originally_schedulable)} 个")
        except ApiException as e:
            logger.warning(f"  ⚠️ 获取节点状态失败: {e}")

        # 1. 先cordon所有节点
        logger.info(f"  🔒 Cordon所有节点...")
        cordoned_nodes = []
        for node in all_nodes:
            try:
                body = {"spec": {"unschedulable": True}}
                await self.core_api.patch_node(node, body)
                cordoned_nodes.append(node)
            except ApiException as e:
                logger.warning(f"  ⚠️ Cordon {node} 失败: {e}")

        # 等待cordon生效（调度器需要时间感知）
        await asyncio.sleep(1)

        results = []
        try:
            # 2. 逐个执行迁移（隔离+等待调度）
            for i, migration in enumerate(migrations):
                target_node = migration["target_node"]
                logger.info(f"📦 [{i+1}/{len(migrations)}] 处理 {migration['namespace']}/{migration['pod_name']}")

                # uncordon目标节点（排除节点不uncordon）
                if target_node not in exclude_nodes:
                    logger.info(f"  🔓 Uncordon目标节点 {target_node}...")
                    try:
                        body = {"spec": {"unschedulable": False}}
                        await self.core_api.patch_node(target_node, body)
                        # 等待uncordon生效
                        await asyncio.sleep(0.5)
                    except ApiException as e:
                        logger.warning(f"  ⚠️ Uncordon {target_node} 失败: {e}")

                # 执行迁移
                result = await self._do_migration(migration)
                result["migration"] = migration
                results.append(result)

                # cordon回目标节点（为下一个迁移准备）
                if target_node not in exclude_nodes:
                    logger.info(f"  🔒 Cordon回目标节点 {target_node}...")
                    try:
                        body = {"spec": {"unschedulable": True}}
                        await self.core_api.patch_node(target_node, body)
                    except ApiException as e:
                        logger.warning(f"  ⚠️ Cordon {target_node} 失败: {e}")

        finally:
            # 3. 恢复节点调度状态（调度完成后立即恢复，不等Ready）
            logger.info(f"  🔓 恢复节点调度状态...")
            for node in cordoned_nodes:
                if node in exclude_nodes:
                    logger.info(f"  ⏭️ 跳过排除节点 {node}，保持cordon状态")
                    continue
                if node not in originally_schedulable:
                    logger.info(f"  ⏭️ 跳过原本不可调度的节点 {node}，保持cordon状态")
                    continue
                try:
                    body = {"spec": {"unschedulable": False}}
                    await self.core_api.patch_node(node, body)
                except ApiException as e:
                    logger.warning(f"  ⚠️ Uncordon {node} 失败: {e}")

        success_count = sum(1 for r in results if r.get("success"))
        logger.info(f"🚀 批量迁移完成: {success_count}/{len(migrations)} 调度成功")

        return {
            "total": len(migrations),
            "success": success_count,
            "failed": len(migrations) - success_count,
            "results": results,
            "originally_schedulable": list(originally_schedulable)  # 返回原本可调度的节点列表
        }

    async def check_pods_status(self, pods: list[dict]) -> list[dict]:
        """
        🔍 检查Pod列表的Ready状态
        pods: [{namespace, pod_name}]
        返回: [{namespace, pod_name, status, ready}]
        """
        results = []
        for pod_info in pods:
            namespace = pod_info.get("namespace")
            pod_name = pod_info.get("pod_name") or pod_info.get("new_pod")
            if not namespace or not pod_name:
                continue
            try:
                pod = await self.core_api.read_namespaced_pod(pod_name, namespace)
                is_ready = False
                # 检查所有conditions
                conditions_info = []
                if pod.status.conditions:
                    for condition in pod.status.conditions:
                        conditions_info.append(f"{condition.type}={condition.status}")
                        if condition.type == "Ready" and condition.status == "True":
                            is_ready = True
                logger.info(f"🔍 检查Pod {namespace}/{pod_name}: phase={pod.status.phase}, conditions=[{', '.join(conditions_info)}], ready={is_ready}")
                results.append({
                    "namespace": namespace,
                    "pod_name": pod_name,
                    "status": pod.status.phase,
                    "ready": is_ready
                })
            except ApiException as e:
                logger.warning(f"🔍 检查Pod {namespace}/{pod_name} 失败: {e}")
                results.append({
                    "namespace": namespace,
                    "pod_name": pod_name,
                    "status": "NotFound",
                    "ready": False,
                    "error": str(e)
                })
        return results

    async def get_isolated_pods(self, exclude_namespaces: list[str]) -> list[dict]:
        """
        🔍 获取已隔离的Pod列表
        检测方式：没有ownerReferences的Pod
        """
        logger.info("🔍 获取已隔离的Pod列表...")
        result = []

        try:
            pods = await self.core_api.list_pod_for_all_namespaces()

            # 获取Pod CPU使用量
            pod_cpu_map = {}
            try:
                metrics = await self.custom_api.list_cluster_custom_object(
                    group="metrics.k8s.io",
                    version="v1beta1",
                    plural="pods"
                )
                for item in metrics.get("items", []):
                    ns = item["metadata"]["namespace"]
                    name = item["metadata"]["name"]
                    key = f"{ns}/{name}"
                    total_cpu = 0
                    for container in item.get("containers", []):
                        cpu = container["usage"].get("cpu", "0")
                        total_cpu += self._parse_cpu(cpu)
                    pod_cpu_map[key] = total_cpu
            except Exception as e:
                logger.warning(f"⚠️ 获取Pod CPU metrics失败: {e}")

            # 第一遍：筛选隔离Pod，收集需要查询的Deployment
            isolated_pods = []
            deploy_to_query = set()  # {(namespace, deploy_name)}
            for pod in pods.items:
                ns = pod.metadata.namespace
                if ns in exclude_namespaces:
                    continue
                if pod.metadata.owner_references:
                    continue
                isolated_pods.append(pod)
                # 从Pod名称推断Deployment名称
                pod_name_parts = pod.metadata.name.rsplit("-", 2)
                if len(pod_name_parts) >= 3:
                    deploy_to_query.add((ns, pod_name_parts[0]))

            # 只查询需要的Deployment（并发查询）
            deploy_selector_map = {}  # {(namespace, deploy_name): selector_key}

            async def fetch_deploy_selector(ns: str, deploy_name: str):
                try:
                    deploy = await self.apps_api.read_namespaced_deployment(deploy_name, ns)
                    selector = deploy.spec.selector.match_labels or {}
                    if selector:
                        return (ns, deploy_name), list(selector.keys())[0]
                except:
                    pass
                return None

            results = await asyncio.gather(*[
                fetch_deploy_selector(ns, name) for ns, name in deploy_to_query
            ])
            for r in results:
                if r:
                    deploy_selector_map[r[0]] = r[1]

            # 第二遍：构建结果
            for pod in isolated_pods:
                ns = pod.metadata.namespace
                pod_labels = pod.metadata.labels or {}
                key = f"{ns}/{pod.metadata.name}"

                selector_label = ""
                pod_name_parts = pod.metadata.name.rsplit("-", 2)
                if len(pod_name_parts) >= 3:
                    sel_key = deploy_selector_map.get((ns, pod_name_parts[0]))
                    if sel_key and sel_key in pod_labels:
                        selector_label = f"{sel_key}={pod_labels[sel_key]}"

                if not selector_label:
                    for label_key in ["app", "app.kubernetes.io/name", "k8s-app", "name"]:
                        if label_key in pod_labels:
                            selector_label = f"{label_key}={pod_labels[label_key]}"
                            break

                result.append({
                    "name": pod.metadata.name,
                    "namespace": ns,
                    "node_name": pod.spec.node_name,
                    "selector_label": selector_label,
                    "created_at": pod.metadata.creation_timestamp.isoformat() if pod.metadata.creation_timestamp else None,
                    "status": pod.status.phase,
                    "cpu_used": round(pod_cpu_map.get(key, 0), 2)
                })

            logger.info(f"✅ 找到 {len(result)} 个已隔离的Pod")
            return result

        except ApiException as e:
            logger.error(f"❌ 获取隔离Pod列表失败: {e}")
            raise

    async def cleanup_isolated_pods(self, pods: list[dict]) -> dict:
        """
        🧹 清理隔离的Pod
        """
        logger.info(f"🧹 开始清理 {len(pods)} 个隔离Pod...")

        success = []
        failed = []

        for pod in pods:
            try:
                await self.core_api.delete_namespaced_pod(
                    pod["name"],
                    pod["namespace"],
                    grace_period_seconds=0
                )
                success.append(pod["name"])
                logger.info(f"  ✅ 已删除 {pod['namespace']}/{pod['name']}")
            except ApiException as e:
                failed.append({"name": pod["name"], "error": str(e)})
                logger.error(f"  ❌ 删除 {pod['namespace']}/{pod['name']} 失败: {e}")

        return {
            "success": success,
            "failed": failed,
            "total": len(pods)
        }

    async def get_all_namespaces(self) -> list[str]:
        """
        📋 获取所有namespace列表
        """
        try:
            namespaces = await self.core_api.list_namespace()
            return sorted([ns.metadata.name for ns in namespaces.items])
        except ApiException as e:
            logger.error(f"❌ 获取namespace列表失败: {e}")
            raise


async def analyze_and_plan(core_api: client.CoreV1Api, apps_api: client.AppsV1Api,
                          custom_api: client.CustomObjectsApi, config: dict) -> dict:
    """
    📊 分析负载并生成迁移计划（API入口函数）
    """
    logger.info("=" * 60)
    logger.info("🔄 开始负载均衡分析...")
    logger.info("=" * 60)

    lb = LoadBalancer(core_api, apps_api, custom_api)

    # 获取配置参数（默认值由master端service.py定义）
    exclude_ns = config.get("exclude_namespaces", [])
    exclude_nodes = config.get("exclude_nodes", [])
    min_replicas = config.get("min_replicas", 2)
    blacklist = config.get("blacklist", [])
    imbalance_threshold = config.get("imbalance_threshold", 30)
    balance_target = config.get("balance_target", 20)
    # 节点选择策略
    source_strategy = config.get("source_strategy", "average")
    source_percentile = config.get("source_percentile", 70)
    target_strategy = config.get("target_strategy", "average")
    target_percentile = config.get("target_percentile", 30)
    # 迁移限制
    min_pod_cpu = config.get("min_pod_cpu", 0.5)
    max_iterations = config.get("max_iterations", 50)

    # 1. 并行获取节点和Pod信息
    nodes_cpu, pods_cpu = await asyncio.gather(
        lb.get_all_nodes_cpu(),
        lb.get_all_pods_cpu(exclude_ns)
    )

    # 2. 收集需要查询的namespace，批量获取Deployment信息
    namespaces = set()
    for pod in pods_cpu:
        if pod.get("deployment"):
            namespaces.add(pod["namespace"])

    deployments_info = await lb.get_all_deployments_info(namespaces)

    # 3. 过滤可迁移Pod
    eligible_pods = lb.filter_eligible_pods(pods_cpu, deployments_info, min_replicas, blacklist)

    # 4. 生成迁移计划
    plan = lb.generate_migration_plan(
        nodes_cpu, eligible_pods, imbalance_threshold, balance_target, exclude_nodes,
        source_strategy, source_percentile, target_strategy, target_percentile,
        min_pod_cpu, max_iterations
    )

    plan["timestamp"] = datetime.now().isoformat()
    plan["config"] = {
        "imbalance_threshold": imbalance_threshold,
        "balance_target": balance_target,
        "min_replicas": min_replicas
    }

    logger.info("=" * 60)
    logger.info("🔄 负载均衡分析完成")
    logger.info("=" * 60)

    return plan


async def execute_plan(core_api: client.CoreV1Api, apps_api: client.AppsV1Api,
                      custom_api: client.CustomObjectsApi, migrations: list[dict],
                      exclude_nodes: list[str] = None) -> dict:
    """
    🚀 执行迁移计划（API入口函数）
    支持部分执行：只执行传入的migrations列表
    使用优化的批量执行方法，减少cordon/uncordon操作
    exclude_nodes: 排除节点列表，这些节点始终保持cordon状态
    """
    logger.info("=" * 60)
    logger.info(f"🚀 开始执行迁移计划，共 {len(migrations)} 个Pod...")
    logger.info("=" * 60)

    lb = LoadBalancer(core_api, apps_api, custom_api)

    # 获取所有节点名称
    nodes = await core_api.list_node()
    all_nodes = [n.metadata.name for n in nodes.items]

    # 使用优化的批量执行方法
    result = await lb.execute_migrations_batch(migrations, all_nodes, exclude_nodes)

    logger.info("=" * 60)
    logger.info(f"🚀 迁移计划执行完成: {result['success']}/{result['total']} 成功")
    logger.info("=" * 60)

    return result


async def get_isolated_pods(core_api: client.CoreV1Api, apps_api: client.AppsV1Api,
                           custom_api: client.CustomObjectsApi, config: dict) -> list[dict]:
    """
    🔍 获取隔离Pod列表（API入口函数）
    """
    lb = LoadBalancer(core_api, apps_api, custom_api)
    exclude_ns = config.get("exclude_namespaces", [])
    return await lb.get_isolated_pods(exclude_ns)


async def cleanup_pods(core_api: client.CoreV1Api, apps_api: client.AppsV1Api,
                      custom_api: client.CustomObjectsApi, pods: list[dict]) -> dict:
    """
    🧹 清理隔离Pod（API入口函数）
    """
    lb = LoadBalancer(core_api, apps_api, custom_api)
    return await lb.cleanup_isolated_pods(pods)


async def check_pods_status(core_api: client.CoreV1Api, pods: list[dict]) -> list[dict]:
    """
    🔍 检查Pod Ready状态（API入口函数）
    pods: [{namespace, pod_name}]
    """
    lb = LoadBalancer(core_api, None, None)
    return await lb.check_pods_status(pods)
