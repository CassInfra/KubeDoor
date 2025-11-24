import asyncio

from kubernetes_asyncio.client.rest import ApiException
from loguru import logger


CCI_SCHEDULE_GROUP = "scheduling.cci.io"
CCI_SCHEDULE_VERSION = "v2"
CCI_SCHEDULE_PLURAL = "scheduleprofiles"
BURSTING_NODE_KEYWORD = "bursting-node"


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

    exists = False
    try:
        await custom_api.get_namespaced_custom_object(
            group=CCI_SCHEDULE_GROUP,
            version=CCI_SCHEDULE_VERSION,
            namespace=namespace,
            plural=CCI_SCHEDULE_PLURAL,
            name=deployment_name,
        )
        exists = True
        logger.info(f"ScheduleProfile {namespace}/{deployment_name} 已存在，将执行 patch 更新")
    except ApiException as e:
        if e.status == 404:
            logger.info(f"ScheduleProfile {namespace}/{deployment_name} 不存在，将创建新资源")
        else:
            logger.error(
                f"查询 ScheduleProfile {namespace}/{deployment_name} 失败: {type(e).__name__}: {str(e)}"
            )
            raise

    if exists:
        await custom_api.patch_namespaced_custom_object(
            group=CCI_SCHEDULE_GROUP,
            version=CCI_SCHEDULE_VERSION,
            namespace=namespace,
            plural=CCI_SCHEDULE_PLURAL,
            name=deployment_name,
            body=schedule_profile,
        )
        logger.info(f"已更新 ScheduleProfile {namespace}/{deployment_name}")
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
                await apps_v1_api.patch_namespaced_deployment(deployment_name, namespace, deployment_ref)
            else:
                await apps_v1_api.patch_namespaced_deployment_scale(deployment_name, namespace, deployment_ref)

            return deployment_ref
        except ApiException as patch_e:
            if patch_e.status == 409 and retry_count < max_retries - 1:
                retry_count += 1
                logger.warning(f"遇到409冲突，第{retry_count}次重试...")
                await asyncio.sleep(1)
            else:
                raise


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
):
    logger.info(
        f"执行CCI扩容流程: {namespace}/{deployment_name}, 当前副本 {current_replicas}, 目标 {target_replicas}"
    )

    bursting_nodes = await _find_bursting_node_names(core_v1_api)
    error_raised = False

    try:
        if bursting_nodes:
            logger.info(f"发现 bursting-node 节点: {bursting_nodes}，解除禁止调度")
            await _set_nodes_schedulable(core_v1_api, bursting_nodes, True)
        else:
            logger.warning("未发现 bursting-node 节点，跳过节点调度调整")

        app_label = _get_deployment_app_label(deployment_obj, deployment_name)
        await _apply_cci_schedule_profile(custom_api, namespace, deployment_name, app_label, current_replicas)

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
