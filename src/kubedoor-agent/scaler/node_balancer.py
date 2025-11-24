from loguru import logger
from kubernetes_asyncio.client import ApiException

import utils


async def get_labeled_nodes_count(namespace, deployment_name, nodes):
    """统计具有指定标签的节点数量"""
    labeled_nodes_count = 0
    label_key = f"{namespace}.{deployment_name}"
    for node in nodes.items:
        labels = node.metadata.labels
        if labels and labels.get(label_key) == utils.NODE_LABLE_VALUE:
            labeled_nodes_count += 1
    return labeled_nodes_count


async def select_least_loaded_nodes(core_v1_api, namespace, nodes_to_label_count, deployment_name, node_cpu_list):
    """选择CPU使用率最低的节点"""
    nodes = await core_v1_api.list_node()
    node_filter_list = []
    sorted_nodes = []

    for node in nodes.items:
        labels = node.metadata.labels
        label_key = f"{namespace}.{deployment_name}"
        if labels and labels.get(label_key) == utils.NODE_LABLE_VALUE:
            continue
        node_filter_list.append(node.metadata.name)

    logger.info(f"【扩容(低-->高)】node_cpu_list: {node_cpu_list}")
    for item in node_cpu_list:
        if item.get('name') in node_filter_list:
            sorted_nodes.append(item.get('name'))

    if len(sorted_nodes) >= nodes_to_label_count:
        return sorted_nodes[:nodes_to_label_count]
    return None


async def select_del_label_nodes(core_v1_api, namespace, del_label_count, deployment_name, node_cpu_list):
    """选择CPU使用率最高的节点"""
    nodes = await core_v1_api.list_node()
    node_filter_list = []
    sorted_nodes = []

    for node in nodes.items:
        labels = node.metadata.labels
        label_key = f"{namespace}.{deployment_name}"
        if labels and labels.get(label_key) == utils.NODE_LABLE_VALUE:
            node_filter_list.append(node.metadata.name)

    sorted_cpu_list = sorted(node_cpu_list, key=lambda x: x.get('percent', 0), reverse=True)
    logger.info(f"【缩容(高-->低)】node_cpu_list: {sorted_cpu_list}")
    for item in sorted_cpu_list:
        if item.get('name') in node_filter_list:
            sorted_nodes.append(item.get('name'))

    return sorted_nodes[:del_label_count]


async def update_node_with_label(core_v1_api, namespace, node_name, deployment_name):
    """为节点添加固定节点标签"""
    label_key = f"{namespace}.{deployment_name}"
    patch_body = {"metadata": {"labels": {label_key: utils.NODE_LABLE_VALUE}}}
    try:
        await core_v1_api.patch_node(name=node_name, body=patch_body)
        logger.info(f"节点 {node_name} 上已添加标签 {label_key}={utils.NODE_LABLE_VALUE}")
    except ApiException as e:
        logger.error(f"在节点 {node_name} 上更新标签时出错: {e}")
        raise


async def del_node_with_label(core_v1_api, namespace, node_name, deployment_name):
    """为节点删除固定节点标签"""
    label_key = f"{namespace}.{deployment_name}"
    patch_body = {"metadata": {"labels": {label_key: None}}}
    try:
        await core_v1_api.patch_node(name=node_name, body=patch_body)
        logger.info(f"节点 {node_name} 上已删除标签 {label_key}")
    except ApiException as e:
        logger.error(f"在节点 {node_name} 上更新标签时出错: {e}")
        raise


async def delete_pods_in_available_nodes(apps_v1_api, core_v1_api, namespace, deployment_name, available_nodes):
    """删除指定节点上的Deployment Pod"""
    try:
        deployment = await apps_v1_api.read_namespaced_deployment(deployment_name, namespace)
        selector = deployment.spec.selector.match_labels
        label_selector = ",".join([f"{k}={v}" for k, v in selector.items()]) if selector else ""

        deleted_count = 0
        target_delete_count = len(available_nodes)
        deleted_pods = []

        for node_name in available_nodes:
            if deleted_count >= target_delete_count:
                break

            field_selector = f"spec.nodeName={node_name}"
            pods = await core_v1_api.list_namespaced_pod(
                namespace=namespace, label_selector=label_selector, field_selector=field_selector
            )

            for pod in pods.items:
                if deleted_count >= target_delete_count:
                    break

                logger.info(f"删除pod: {pod.metadata.name} (节点: {pod.spec.node_name})")
                await core_v1_api.delete_namespaced_pod(name=pod.metadata.name, namespace=namespace)
                deleted_pods.append(pod.metadata.name)
                deleted_count += 1
                break

        logger.info(f"删除了 {deleted_count} 个 {deployment_name} 的pod，目标删除数量: {target_delete_count}")
        return deleted_pods

    except ApiException as e:
        logger.error(f"删除pod时出错: {e}")
        raise
    except Exception as e:
        logger.error(f"删除pod时发生未知错误: {e}")
        raise


async def delete_label(core_v1_api, namespace, deployment_name, nodes):
    """删除不再使用的固定节点标签"""
    label_key = f"{namespace}.{deployment_name}"
    for node in nodes.items:
        node_name = node.metadata.name
        labels = node.metadata.labels
        flag = False
        if labels and labels.get(label_key) == utils.NODE_LABLE_VALUE:
            try:
                all_pods = await core_v1_api.list_pod_for_all_namespaces(field_selector=f"spec.nodeName={node_name}")
                for pod in all_pods.items:
                    pod_name = pod.metadata.name
                    if pod_name and pod_name.startswith(f"{deployment_name}-"):
                        flag = True
                        break
            except ApiException as e:
                logger.error(f"检查节点 {node_name} 的服务 Pod 时出现问题: {e}")
            if not flag:
                patch_body = {"metadata": {"labels": {label_key: None}}}
                try:
                    await core_v1_api.patch_node(name=node_name, body=patch_body)
                    logger.info(f"节点 {node_name}上未部署服务{deployment_name}，已删除标签 {label_key}")
                except ApiException as e:
                    logger.error(f"删除节点 {node_name} 上标签 {label_key} 时出错: {e}")
