import asyncio

from kubernetes_asyncio.client.rest import ApiException
from loguru import logger


CCI_SCHEDULE_GROUP = "scheduling.cci.io"
CCI_SCHEDULE_VERSION = "v2"
CCI_SCHEDULE_PLURAL = "scheduleprofiles"
BURSTING_NODE_KEYWORD = "bursting-node"


async def get_schedule_profile_info(custom_api, apps_v1_api, namespace, deployment_name):
    """获取ScheduleProfile的maxNum，如果不存在则返回deployment当前副本数"""
    try:
        resource = await custom_api.get_namespaced_custom_object(
            group=CCI_SCHEDULE_GROUP,
            version=CCI_SCHEDULE_VERSION,
            namespace=namespace,
            plural=CCI_SCHEDULE_PLURAL,
            name=deployment_name,
        )
        max_num = resource.get("spec", {}).get("location", {}).get("local", {}).get("maxNum", 0)
        return {"exists": True, "maxNum": max_num}
    except ApiException as e:
        if e.status == 404:
            # ScheduleProfile不存在，获取deployment当前副本数
            try:
                deployment = await apps_v1_api.read_namespaced_deployment(deployment_name, namespace)
                current_replicas = deployment.spec.replicas or 0
                return {"exists": False, "maxNum": current_replicas}
            except Exception:
                return {"exists": False, "maxNum": 0}
        raise


def _is_bursting_node(node):
    if not node or not getattr(node, "metadata", None):
        return False

    name = (node.metadata.name or "").lower()
    if BURSTING_NODE_KEYWORD in name:
        return True

    labels = getattr(node.metadata, "labels", {}) or {}
    for key, value in labels.items():
        key_match = isinstance(key, str) and BURSTING_NODE_KEYWORD in key.lower()
        value_match = isinstance(value, str) and BURSTING_NODE_KEYWORD in value.lower()
        if key_match or value_match:
            return True

    return False


async def _find_bursting_node_names(core_v1_api):
    nodes = await core_v1_api.list_node()
    if not nodes or not nodes.items:
        return []

    matching_nodes = [node.metadata.name for node in nodes.items if _is_bursting_node(node)]
    return [name for name in matching_nodes if name]


async def _set_nodes_schedulable(core_v1_api, node_names, schedulable):
    if not node_names:
        return

    unschedulable = not schedulable
    state_label = "可调度" if schedulable else "禁止调度"

    for node_name in node_names:
        patch_body = {"spec": {"unschedulable": unschedulable}}
        logger.info(f"设置节点 {node_name} 状态为: {state_label}")
        await core_v1_api.patch_node(name=node_name, body=patch_body)


HOSTNAME_LABEL_KEY = "kubernetes.io/hostname"


async def _remove_hostname_labels(core_v1_api, node_names):
    """删除节点的hostname标签，返回原始标签值用于恢复"""
    if not node_names:
        return {}

    original_labels = {}
    for node_name in node_names:
        try:
            node = await core_v1_api.read_node(name=node_name)
            labels = node.metadata.labels or {}
            if HOSTNAME_LABEL_KEY in labels:
                original_labels[node_name] = labels[HOSTNAME_LABEL_KEY]
                # 使用JSON Patch删除标签
                patch_body = [{"op": "remove", "path": f"/metadata/labels/{HOSTNAME_LABEL_KEY.replace('/', '~1')}"}]
                await core_v1_api.patch_node(name=node_name, body=patch_body, _content_type="application/json-patch+json")
                logger.info(f"已删除节点 {node_name} 的 {HOSTNAME_LABEL_KEY} 标签")
            else:
                logger.info(f"节点 {node_name} 没有 {HOSTNAME_LABEL_KEY} 标签，跳过")
        except Exception as e:
            logger.warning(f"删除节点 {node_name} 的hostname标签失败: {e}")

    return original_labels


async def _restore_hostname_labels(core_v1_api, original_labels):
    """恢复节点的hostname标签"""
    if not original_labels:
        return

    for node_name, label_value in original_labels.items():
        try:
            patch_body = {"metadata": {"labels": {HOSTNAME_LABEL_KEY: label_value}}}
            await core_v1_api.patch_node(name=node_name, body=patch_body)
            logger.info(f"已恢复节点 {node_name} 的 {HOSTNAME_LABEL_KEY}={label_value} 标签")
        except Exception as e:
            logger.warning(f"恢复节点 {node_name} 的hostname标签失败: {e}")


def _get_deployment_app_label(deployment_obj, fallback_name):
    app_label = None
    selector = getattr(deployment_obj.spec, "selector", None)
    match_labels = getattr(selector, "match_labels", None) if selector else None
    if match_labels:
        app_label = match_labels.get("app")

    metadata_labels = getattr(deployment_obj.metadata, "labels", None)
    if not app_label and metadata_labels:
        app_label = metadata_labels.get("app")

    if not app_label:
        app_label = fallback_name

    return app_label


async def _apply_cci_schedule_profile(custom_api, namespace, deployment_name, app_label, local_max_num):
    local_max_num = int(local_max_num) if local_max_num is not None else 0

    schedule_profile = {
        "apiVersion": f"{CCI_SCHEDULE_GROUP}/{CCI_SCHEDULE_VERSION}",
        "kind": "ScheduleProfile",
        "metadata": {
            "name": deployment_name,
            "namespace": namespace,
        },
        "spec": {
            "location": {
                "cci": {"scaleDownPriority": 100},
                "local": {"maxNum": local_max_num, "scaleDownPriority": 10},
            },
            "objectLabels": {"matchLabels": {"app": app_label}},
            "strategy": "localPrefer",
            "virtualNodes": [{"type": BURSTING_NODE_KEYWORD}],
        },
    }

    existing_resource = None
    try:
        existing_resource = await custom_api.get_namespaced_custom_object(
            group=CCI_SCHEDULE_GROUP,
            version=CCI_SCHEDULE_VERSION,
            namespace=namespace,
            plural=CCI_SCHEDULE_PLURAL,
            name=deployment_name,
        )
        logger.info(f"ScheduleProfile {namespace}/{deployment_name} 已存在，将执行更新")
    except ApiException as e:
        if e.status == 404:
            logger.info(f"ScheduleProfile {namespace}/{deployment_name} 不存在，将创建新资源")
        else:
            logger.error(
                f"查询 ScheduleProfile {namespace}/{deployment_name} 失败: {type(e).__name__}: {str(e)}"
            )
            raise

    if existing_resource:
        schedule_profile["metadata"]["resourceVersion"] = existing_resource["metadata"]["resourceVersion"]
        await custom_api.replace_namespaced_custom_object(
            group=CCI_SCHEDULE_GROUP,
            version=CCI_SCHEDULE_VERSION,
            namespace=namespace,
            plural=CCI_SCHEDULE_PLURAL,
            name=deployment_name,
            body=schedule_profile,
        )
        logger.info(f"已更新 ScheduleProfile {namespace}/{deployment_name}，maxNum={local_max_num}")
    else:
        await custom_api.create_namespaced_custom_object(
            group=CCI_SCHEDULE_GROUP,
            version=CCI_SCHEDULE_VERSION,
            namespace=namespace,
            plural=CCI_SCHEDULE_PLURAL,
            body=schedule_profile,
        )
        logger.info(f"已创建 ScheduleProfile {namespace}/{deployment_name}")


async def patch_deployment_replicas_with_retry(
    apps_v1_api,
    deployment_name,
    namespace,
    target_replicas,
    deployment_obj,
    current_replicas,
    temp_flag,
    del_scale_temp,
    add_label,
):
    max_retries = 3
    retry_count = 0
    deployment_ref = deployment_obj

    while retry_count < max_retries:
        try:
            if target_replicas < current_replicas and add_label == "true":
                deployment_ref = await apps_v1_api.read_namespaced_deployment(deployment_name, namespace)

            deployment_ref.spec.replicas = target_replicas
            logger.info(
                f"Deployment【{deployment_name}】副本数更改为 {target_replicas}，如已接入准入控制, 实际变更以数据库中数据为准。"
            )

            if temp_flag == "true" or del_scale_temp == 1:
                # 使用 strategic merge patch 格式
                patch_body = {
                    "metadata": {"annotations": deployment_ref.metadata.annotations},
                    "spec": {"replicas": target_replicas}
                }
                await apps_v1_api.patch_namespaced_deployment(deployment_name, namespace, patch_body)
            else:
                # 使用 Scale 子资源
                scale_body = {"spec": {"replicas": target_replicas}}
                await apps_v1_api.patch_namespaced_deployment_scale(deployment_name, namespace, scale_body)

            return deployment_ref
        except ApiException as patch_e:
            if patch_e.status == 409 and retry_count < max_retries - 1:
                retry_count += 1
                logger.warning(f"遇到409冲突，第{retry_count}次重试...")
                await asyncio.sleep(1)
            else:
                raise


async def _wait_for_pods_scheduled(core_v1_api, namespace, deployment_name, target_replicas, timeout=60):
    """等待 Pod 调度完成（分配到节点）"""
    import time
    start_time = time.time()
    check_interval = 2

    while time.time() - start_time < timeout:
        try:
            pods = await core_v1_api.list_namespaced_pod(
                namespace=namespace,
                label_selector=f"app={deployment_name}"
            )
            scheduled_count = sum(1 for pod in pods.items if pod.spec.node_name)
            logger.info(f"等待Pod调度: {scheduled_count}/{target_replicas} 已分配节点")

            if scheduled_count >= target_replicas:
                logger.info(f"所有Pod已调度完成")
                return True
        except Exception as e:
            logger.warning(f"检查Pod调度状态时出错: {e}")

        await asyncio.sleep(check_interval)

    logger.warning(f"等待Pod调度超时({timeout}s)")
    return False


async def execute_cci_scaling(
    core_v1_api,
    custom_api,
    apps_v1_api,
    namespace,
    deployment_name,
    deployment_obj,
    current_replicas,
    target_replicas,
    temp_flag,
    del_scale_temp,
    add_label,
    local_max_num=None,
):
    logger.info(
        f"执行CCI扩容流程: {namespace}/{deployment_name}, 当前副本 {current_replicas}, 目标 {target_replicas}"
    )

    bursting_nodes = await _find_bursting_node_names(core_v1_api)
    error_raised = False

    try:
        if bursting_nodes:
            # 先删除hostname标签，避免nodeSelector匹配问题
            logger.info(f"删除 bursting-node 节点的 hostname 标签...")
            await _remove_hostname_labels(core_v1_api, bursting_nodes)
            # 再解除禁止调度
            logger.info(f"发现 bursting-node 节点: {bursting_nodes}，解除禁止调度")
            await _set_nodes_schedulable(core_v1_api, bursting_nodes, True)
        else:
            logger.warning("未发现 bursting-node 节点，跳过节点调度调整")

        app_label = _get_deployment_app_label(deployment_obj, deployment_name)
        # 使用前端传入的local_max_num，如果没有则使用当前副本数
        max_num = local_max_num if local_max_num is not None else current_replicas
        await _apply_cci_schedule_profile(custom_api, namespace, deployment_name, app_label, max_num)

        updated_obj = await patch_deployment_replicas_with_retry(
            apps_v1_api,
            deployment_name,
            namespace,
            target_replicas,
            deployment_obj,
            current_replicas,
            temp_flag,
            del_scale_temp,
            add_label,
        )

        # 等待 Pod 调度完成后再 cordon 节点
        if bursting_nodes and target_replicas > current_replicas:
            app_label_for_query = _get_deployment_app_label(deployment_obj, deployment_name)
            logger.info("等待新Pod调度完成...")
            await _wait_for_pods_scheduled(core_v1_api, namespace, app_label_for_query, target_replicas)

        return updated_obj
    except Exception:
        error_raised = True
        raise
    finally:
        if bursting_nodes:
            try:
                logger.info(f"CCI扩容完成，重新禁止 bursting-node 节点调度: {bursting_nodes}")
                await _set_nodes_schedulable(core_v1_api, bursting_nodes, False)
            except Exception as cordon_error:
                logger.error(f"重新禁止 bursting-node 节点调度失败: {cordon_error}")
                if not error_raised:
                    raise
