import asyncio
import json
import traceback
from datetime import datetime
from typing import Optional, Callable, Awaitable, Dict, Any

from aiohttp import web
from kubernetes_asyncio.client import AppsV1Api
from kubernetes_asyncio.client.rest import ApiException
from loguru import logger

import utils
from func_manager.k8s_node_scheduler import K8sNodeScheduler
from k8s_client_manager import K8sClientManager


class RebootService:
    def __init__(
        self,
        apps_v1_api: AppsV1Api,
        delete_cronjob_callback: Optional[Callable[[str, str], Awaitable[None]]] = None,
    ):
        self.apps_v1 = apps_v1_api
        self.delete_cronjob_callback = delete_cronjob_callback

    async def reboot(self, request: web.Request):
        """批量重启微服务"""
        request_info = await request.json()
        interval = request.query.get("interval")
        scheduler = request.query.get("scheduler", "false")
        patch = {
            "spec": {
                "template": {
                    "metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": datetime.now().isoformat()}}
                }
            }
        }
        error_list = []

        for index, deployment in enumerate(request_info.get('deployment_list', [])):
            namespace = deployment.get("namespace")
            deployment_name = deployment.get("deployment_name")
            job_name = deployment.get("job_name")
            job_type = deployment.get("job_type")
            logger.info(f"【{namespace}】【{deployment_name}】")

            try:
                deployment_obj = await self.apps_v1.read_namespaced_deployment(deployment_name, namespace)
                if not deployment_obj:
                    reason = f"未找到【{namespace}】【{deployment_name}】"
                    logger.error(reason)
                    error_list.append({'namespace': namespace, 'deployment_name': deployment_name, 'reason': reason})
                    continue

                if scheduler == 'true':
                    cordon_error = await self._handle_scheduler(
                        request_info=request_info, namespace=namespace, deployment_name=deployment_name
                    )
                    if cordon_error:
                        return web.json_response({"message": cordon_error}, status=500)

                logger.info(f"重启 Deployment【{deployment_name}】，如已接入准入控制, 实际变更已数据库中数据为准。")
                await self.apps_v1.patch_namespaced_deployment(deployment_name, namespace, patch)

                if interval and index != len(request_info) - 1:
                    logger.info(f"暂停 {interval}s...")
                    await asyncio.sleep(int(interval))

                utils.send_msg(
                    f"'【{utils.PROM_K8S_TAG_VALUE}】【{namespace}】【{deployment_name}】' has been restarted!"
                )

                if job_name and self.delete_cronjob_callback:
                    await self.delete_cronjob_callback(job_name, job_type)

                if scheduler == 'true':
                    await self._schedule_uncordon(namespace, deployment_name, request_info)

            except ApiException as exc:
                logger.exception(f"调用 AppsV1Api 时出错: {exc}")
                try:
                    reason = json.loads(exc.body).get("message", str(exc))
                except Exception:
                    reason = str(exc)
                error_list.append({'namespace': namespace, 'deployment_name': deployment_name, 'reason': reason})

        return web.json_response({"message": "ok", "error_list": error_list})

    async def _handle_scheduler(self, request_info: Dict[str, Any], namespace: str, deployment_name: str):
        try:
            logger.info(
                f"开始处理scheduler参数，env={utils.PROM_K8S_TAG_VALUE}, ns={namespace}, deployment={deployment_name}"
            )

            node_scheduler_list = request_info.get('node_scheduler', [])
            logger.info(f"获取到node_scheduler列表: {node_scheduler_list}")

            async with K8sClientManager() as k8s_manager:
                logger.info(f"成功获取K8s客户端: {type(k8s_manager.core_v1_api)}")
                logger.info("正在初始化K8s节点调度器...")
                k8s_scheduler = K8sNodeScheduler(k8s_manager.core_v1_api)
                logger.info(f"成功初始化K8s节点调度器: {type(k8s_scheduler)}")

                logger.info(f"开始执行禁止调度操作，排除节点: {node_scheduler_list}")
                cordon_result = await k8s_scheduler.cordon_nodes_exclude(exclude_nodes=node_scheduler_list)
                logger.info(f"禁止调度操作完成: {cordon_result}")

                if cordon_result.get("error_count", 0) > 0:
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
                            error_message += f"；恢复操作也失败: {uncordon_result.get('error_count', 0)}个节点恢复失败"
                        else:
                            logger.info("✅ uncordon恢复操作成功，所有节点调度状态已恢复")
                            error_message += "；已执行恢复操作确保节点状态一致性"

                    except Exception as uncordon_exc:
                        logger.error(
                            f"❌ 执行uncordon恢复操作时发生异常: {type(uncordon_exc).__name__}: {uncordon_exc}"
                        )
                        error_message += f"；恢复操作异常: {uncordon_exc}"

                    return error_message

        except Exception as exc:
            logger.error(f"处理scheduler参数时发生异常: {type(exc).__name__}: {exc}")
            logger.error(f"异常堆栈: {traceback.format_exc()}")

            if request_info.get('node_scheduler') and 'k8s_scheduler' in locals():
                try:
                    logger.warning("⚠️ 处理scheduler参数时发生异常，开始执行uncordon恢复操作以确保节点状态一致性...")
                    uncordon_result = await k8s_scheduler.uncordon_nodes_exclude(
                        exclude_nodes=request_info.get('node_scheduler', [])
                    )
                    logger.info(f"异常情况下的uncordon恢复操作完成: {uncordon_result}")

                    if uncordon_result.get("error_count", 0) > 0:
                        logger.error(f"⚠️ 异常情况下的uncordon恢复操作也出现错误: {uncordon_result}")
                    else:
                        logger.info("✅ 异常情况下的uncordon恢复操作成功，所有节点调度状态已恢复")

                except Exception as uncordon_exc:
                    logger.error(
                        f"❌ 异常情况下执行uncordon恢复操作时发生异常: {type(uncordon_exc).__name__}: {uncordon_exc}"
                    )

            return f"处理scheduler参数失败: {exc}"

        return None

    async def _schedule_uncordon(self, namespace: str, deployment_name: str, request_info: Dict[str, Any]):
        try:
            node_scheduler_list = request_info.get('node_scheduler', [])
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
                    exclude_nodes=node_scheduler_list, delay_seconds=120, error_callback=uncordon_error_callback
                )
                logger.info(f"取消禁止调度操作已安排: {uncordon_result}")
        except Exception as exc:
            logger.error(f"执行取消禁止调度操作失败: {type(exc).__name__}: {exc}")
            logger.error(f"uncordon异常堆栈: {traceback.format_exc()}")
            # 仅记录错误，不影响主流程
