import asyncio
from datetime import datetime

from aiohttp import web
from loguru import logger

import utils
from scaler.cci_scaler import execute_cci_scaling, patch_deployment_replicas_with_retry
from k8s_client_manager import K8sClientManager
from func_manager.k8s_node_scheduler import K8sNodeScheduler
from scaler.node_balancer import (
    del_node_with_label,
    delete_pods_in_available_nodes,
    get_labeled_nodes_count,
    select_del_label_nodes,
    select_least_loaded_nodes,
    update_node_with_label,
)


class ScaleService:
    def __init__(self, apps_v1_api, core_v1_api, custom_api, delete_cronjob_callback=None):
        self.apps_v1 = apps_v1_api
        self.core_v1 = core_v1_api
        self.custom_api = custom_api
        self.delete_cronjob_callback = delete_cronjob_callback

    async def handle_scale(self, request):
        if not self.apps_v1 or not self.core_v1:
            return web.json_response({"message": "Kubernetes 客户端未初始化", "success": False}, status=500)

        request_info = await request.json()
        interval = request.query.get("interval")
        add_label = request.query.get("add_label")
        res_type = request.query.get("type")
        temp = request.query.get("temp")
        isolate = request.query.get("isolate")
        scheduler = request.query.get("scheduler", "false")
        cci = request.query.get("cci", "false")
        error_list = []

        for index, deployment in enumerate(request_info.get('deployment_list', [])):
            namespace = deployment.get("namespace")
            deployment_name = deployment.get("deployment_name")
            num = deployment.get("num")
            job_name = deployment.get("job_name")
            job_type = deployment.get("job_type")
            logger.info(f"【{namespace}】【{deployment_name}】: {num}")
            nodes = await self.core_v1.list_node()

            try:
                deployment_obj = await self.apps_v1.read_namespaced_deployment(deployment_name, namespace)
                if not deployment_obj:
                    reason = f"未找到【{namespace}】【{deployment_name}】"
                    logger.error(reason)
                    error_list.append({'namespace': namespace, 'deployment_name': deployment_name, 'reason': reason})
                    continue

                current_replicas = deployment_obj.spec.replicas
                logger.info(f"当前副本数: {current_replicas}")

                del_scale_temp = 0
                if temp == 'true':
                    if deployment_obj.metadata.annotations is None:
                        deployment_obj.metadata.annotations = {}
                    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    deployment_obj.metadata.annotations['scale.temp'] = f"{current_time}@{current_replicas}-->{num}"
                    logger.info(f"添加临时扩缩容标记: {current_time}@{current_replicas}-->{num}")
                else:
                    if deployment_obj.metadata.annotations and 'scale.temp' in deployment_obj.metadata.annotations:
                        deployment_obj.metadata.annotations['scale.temp'] = None
                        del_scale_temp = 1
                        logger.info("删除临时扩缩容标记")

                if num > current_replicas and add_label == 'true':
                    if len(nodes.items) < num:
                        return web.json_response(
                            {"message": f"【{namespace}】【{deployment_name}】副本数不能超过节点总数"},
                            status=500,
                        )
                    node_cpu_list = request_info[0].get("node_cpu_list")
                    logger.info(f"节点{res_type}情况: {node_cpu_list}")
                    logger.info(f"扩缩容策略：根据【节点{res_type}】情况，执行扩容，目标副本数: {num}")

                    labeled_nodes_count = await get_labeled_nodes_count(namespace, deployment_name, nodes)
                    add_isolate = 1 if isolate == 'true' else 0

                    if labeled_nodes_count < num + add_isolate:
                        nodes_to_label_count = num + add_isolate - labeled_nodes_count
                        available_nodes = await select_least_loaded_nodes(
                            self.core_v1, namespace, nodes_to_label_count, deployment_name, node_cpu_list
                        )

                        if available_nodes:
                            for node_name in available_nodes:
                                await update_node_with_label(self.core_v1, namespace, node_name, deployment_name)
                        else:
                            reason = "剩余可调度节点不足"
                            logger.error(reason)
                            error_list.append(
                                {
                                    'namespace': namespace,
                                    'deployment_name': deployment_name,
                                    'reason': reason,
                                }
                            )
                            return web.json_response(
                                {"message": f"【{namespace}】【{deployment_name}】剩余可调度节点不足"},
                                status=500,
                            )
                    else:
                        logger.info(f"已有{labeled_nodes_count}个节点有标签，无需再打标签")

                elif num < current_replicas and add_label == 'true':
                    node_cpu_list = request_info[0].get("node_cpu_list")
                    logger.info(f"节点CPU情况: {node_cpu_list}")
                    logger.info(f"执行缩容，目标副本数: {num}")
                    del_label_count = current_replicas - num
                    available_nodes = await select_del_label_nodes(
                        self.core_v1, namespace, del_label_count, deployment_name, node_cpu_list
                    )
                    for node_name in available_nodes:
                        await del_node_with_label(self.core_v1, namespace, node_name, deployment_name)
                    await delete_pods_in_available_nodes(
                        self.apps_v1, self.core_v1, namespace, deployment_name, available_nodes
                    )
                    logger.info("等待deployment控制器完成pod重建...")
                    await asyncio.sleep(2)

                elif num == current_replicas:
                    logger.info("副本数没有变化，无需操作")
                else:
                    logger.info("普通模式扩缩容")

                if scheduler == 'true' and add_label == 'true':
                    return web.json_response({"message": "add_label和scheduler参数不能同时为True"}, status=400)

                if scheduler == 'true':
                    deployment_obj = await self._handle_scheduler_mode(
                        request_info,
                        namespace,
                        deployment_name,
                        deployment_obj,
                        current_replicas,
                        num,
                        temp,
                        del_scale_temp,
                        add_label,
                    )
                elif cci == 'true' and not job_name:
                    deployment_obj = await execute_cci_scaling(
                        self.core_v1,
                        self.custom_api,
                        self.apps_v1,
                        namespace,
                        deployment_name,
                        deployment_obj,
                        current_replicas,
                        num,
                        temp,
                        del_scale_temp,
                        add_label,
                    )
                else:
                    deployment_obj = await patch_deployment_replicas_with_retry(
                        self.apps_v1,
                        deployment_name,
                        namespace,
                        num,
                        deployment_obj,
                        current_replicas,
                        temp,
                        del_scale_temp,
                        add_label,
                    )

                if interval and index != len(request_info) - 1:
                    logger.info(f"暂停 {interval}s...")
                    await asyncio.sleep(int(interval))

                utils.send_msg(
                    f"'【{utils.PROM_K8S_TAG_VALUE}】【{namespace}】【{deployment_name}】' has been scaled! {current_replicas} --> {num}"
                )

                if job_name and self.delete_cronjob_callback:
                    await self.delete_cronjob_callback(job_name, job_type)

                if scheduler == 'true':
                    await self._schedule_uncordon(namespace, deployment_name, request_info.get('node_scheduler', []))

            except web.HTTPException:
                raise
            except Exception as e:
                logger.exception(f"调用 AppsV1Api 时出错: {e}")
                try:
                    reason = str(e)
                except Exception:
                    reason = "未知错误"
                error_list.append({'namespace': namespace, 'deployment_name': deployment_name, 'reason': reason})

        if error_list:
            return web.json_response({"message": f"以下服务未扩缩容成功{error_list}", "success": False})
        return web.json_response({"message": "ok", "success": True})

    async def _handle_scheduler_mode(
        self,
        request_info,
        namespace,
        deployment_name,
        deployment_obj,
        current_replicas,
        num,
        temp,
        del_scale_temp,
        add_label,
    ):
        try:
            logger.info(
                f"开始处理scheduler参数，env={utils.PROM_K8S_TAG_VALUE}, ns={namespace}, deployment={deployment_name}"
            )

            node_scheduler_list = request_info.get('node_scheduler', [])
            logger.info(f"获取到node_scheduler列表: {node_scheduler_list}")

            async with K8sClientManager() as k8s_manager:
                logger.info(f"成功获取K8s客户端: {type(k8s_manager.core_v1_api)}")
                k8s_scheduler = K8sNodeScheduler(k8s_manager.core_v1_api)
                logger.info(f"成功初始化K8s节点调度器: {type(k8s_scheduler)}")
                cordon_result = await k8s_scheduler.cordon_nodes_exclude(exclude_nodes=node_scheduler_list)
                logger.info(f"禁止调度操作完成: {cordon_result}")

                if cordon_result.get("error_count", 0) > 0:
                    await self._rollback_scheduler_operation(k8s_scheduler, node_scheduler_list, cordon_result)
                    raise web.HTTPInternalServerError(text="禁止节点调度操作失败")

            return await patch_deployment_replicas_with_retry(
                self.apps_v1,
                deployment_name,
                namespace,
                num,
                deployment_obj,
                current_replicas,
                temp,
                del_scale_temp,
                add_label,
            )
        except web.HTTPException:
            raise
        except Exception as e:
            logger.error(f"处理scheduler参数时发生异常: {type(e).__name__}: {str(e)}")
            await self._retry_uncordon(node_scheduler_list)
            raise web.HTTPInternalServerError(text=f"处理scheduler参数失败: {str(e)}")

    async def _rollback_scheduler_operation(self, k8s_scheduler, node_scheduler_list, cordon_result):
        error_details = []
        for result in cordon_result.get("results", []):
            if result.get("status") == "error":
                error_details.append(f"节点 {result.get('node_name')}: {result.get('message')}")

        error_message = f"禁止节点调度操作失败，错误详情: {'; '.join(error_details)}"
        logger.error(error_message)

        try:
            logger.warning("⚠️ cordon操作失败，开始执行uncordon恢复操作以确保节点状态一致性...")
            uncordon_result = await k8s_scheduler.uncordon_nodes_exclude(exclude_nodes=node_scheduler_list)
            logger.info(f"uncordon恢复操作完成: {uncordon_result}")

            if uncordon_result.get("error_count", 0) > 0:
                logger.error(f"⚠️ uncordon恢复操作也出现错误: {uncordon_result}")
        except Exception as uncordon_e:
            logger.error(f"❌ 执行uncordon恢复操作时发生异常: {type(uncordon_e).__name__}: {str(uncordon_e)}")

    async def _retry_uncordon(self, node_scheduler_list):
        if not node_scheduler_list:
            return

        async with K8sClientManager() as k8s_manager:
            k8s_scheduler = K8sNodeScheduler(k8s_manager.core_v1_api)
            try:
                logger.warning("⚠️ 开始执行uncordon恢复操作以确保节点状态一致性...")
                uncordon_result = await k8s_scheduler.uncordon_nodes_exclude(exclude_nodes=node_scheduler_list)
                logger.info(f"异常情况下的uncordon恢复操作完成: {uncordon_result}")
            except Exception as uncordon_e:
                logger.error(
                    f"❌ 异常情况下执行uncordon恢复操作时发生异常: {type(uncordon_e).__name__}: {str(uncordon_e)}"
                )

    async def _schedule_uncordon(self, namespace, deployment_name, node_scheduler_list):
        try:
            logger.info(f"标签修改完成，准备执行取消禁止调度操作，排除节点: {node_scheduler_list}")
            logger.info("正在调用uncordon_nodes_exclude方法...")

            def uncordon_error_callback(error_message):
                logger.error(f"取消禁止调度操作失败通知: {error_message}")
                utils.send_msg(
                    f"⚠️ 取消禁止调度操作失败: {error_message}\n\n{utils.PROM_K8S_TAG_VALUE}, {namespace}, {deployment_name}"
                )

            async with K8sClientManager() as uncordon_k8s_manager:
                uncordon_scheduler = K8sNodeScheduler(uncordon_k8s_manager.core_v1_api)
                uncordon_result = await uncordon_scheduler.uncordon_nodes_exclude(
                    exclude_nodes=node_scheduler_list, delay_seconds=10, error_callback=uncordon_error_callback
                )
                logger.info(f"取消禁止调度操作已安排: {uncordon_result}")
        except Exception as e:
            logger.error(f"执行取消禁止调度操作失败: {type(e).__name__}: {str(e)}")
