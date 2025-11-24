import json
import re

from aiohttp import web
from kubernetes_asyncio.client import AppsV1Api, CoreV1Api
from kubernetes_asyncio.client.rest import ApiException
from loguru import logger

import utils
from scaler.node_balancer import update_node_with_label


class BalanceNodeService:
    def __init__(self, core_v1_api: CoreV1Api, apps_v1_api: AppsV1Api):
        self.core_v1 = core_v1_api
        self.apps_v1 = apps_v1_api

    async def balance_node(self, request):
        """节点微调均衡 - 将部署从源节点迁移到目标节点"""
        try:
            data = await request.json()
            env = data.get("env")
            source_node = data.get("source")
            target_node = data.get("target")
            top_deployments = data.get("top_deployments", [])

            if not source_node or not target_node or not top_deployments:
                return web.json_response({"message": "缺少必要参数", "success": False}, status=400)

            logger.info(f"开始节点均衡: 源节点 {source_node} -> 目标节点 {target_node}")
            logger.info(f"待迁移的deployment: {json.dumps(top_deployments)}")

            results = []

            for deployment_info in top_deployments:
                namespace = deployment_info.get("namespace")
                deployment_name = deployment_info.get("deployment")

                if not namespace or not deployment_name:
                    continue

                try:
                    label_key = f"{namespace}.{deployment_name}"
                    logger.info(f"处理标签: {label_key}={utils.NODE_LABLE_VALUE}")

                    await self._remove_node_label(source_node, label_key)
                    await update_node_with_label(self.core_v1, namespace, target_node, deployment_name)
                    deleted_pods = await self._delete_pods_on_node(namespace, deployment_name, source_node)

                    results.append(
                        {
                            "namespace": namespace,
                            "deployment": deployment_name,
                            "status": "success",
                            "deleted_pods": deleted_pods,
                        }
                    )

                except Exception as exc:
                    error_message = str(exc)
                    logger.error(f"迁移 {namespace}.{deployment_name} 时出错: {error_message}")
                    results.append(
                        {
                            "namespace": namespace,
                            "deployment": deployment_name,
                            "status": "failed",
                            "error": error_message,
                        }
                    )

            return web.json_response(
                {
                    "message": f"节点均衡操作完成: {source_node} -> {target_node}",
                    "success": True,
                    "results": results,
                }
            )

        except Exception as exc:
            logger.exception(f"节点均衡操作失败: {exc}")
            return web.json_response({"message": f"操作失败: {str(exc)}", "success": False}, status=500)

    async def _remove_node_label(self, node_name, label_key):
        """从节点删除指定标签"""
        patch_body = {"metadata": {"labels": {label_key: None}}}
        try:
            await self.core_v1.patch_node(name=node_name, body=patch_body)
            logger.info(f"从节点 {node_name} 删除标签 {label_key} 成功")
        except ApiException as exc:
            logger.error(f"从节点 {node_name} 删除标签 {label_key} 时出错: {exc}")
            raise Exception(f"删除标签失败: {str(exc)}") from exc

    async def _delete_pods_on_node(self, namespace, deployment_name, node_name):
        """删除指定节点上指定deployment的pod"""
        try:
            pods = await self.core_v1.list_namespaced_pod(namespace=namespace)
            pattern = re.compile(f"^{re.escape(deployment_name)}-[a-z0-9]+-[a-z0-9]+$")

            deleted_pods = []
            for pod in pods.items:
                if pattern.match(pod.metadata.name) and pod.spec.node_name == node_name:
                    logger.info(f"删除pod: {pod.metadata.name}")
                    await self.core_v1.delete_namespaced_pod(name=pod.metadata.name, namespace=namespace)
                    deleted_pods.append(pod.metadata.name)

            logger.info(f"在节点 {node_name} 上删除了 {len(deleted_pods)} 个 {deployment_name} 的pod")
            return deleted_pods
        except ApiException as exc:
            logger.error(f"删除pod时出错: {exc}")
            raise Exception(f"删除pod失败: {str(exc)}") from exc

    async def delete_pods_in_available_nodes(self, namespace, deployment_name, available_nodes):
        """根据namespace和deployment_name精确找到deployment的所有pod，删除在available_nodes中的pod"""
        try:
            deployment = await self.apps_v1.read_namespaced_deployment(deployment_name, namespace)
            selector = deployment.spec.selector.match_labels
            label_selector = ",".join([f"{k}={v}" for k, v in selector.items()])

            deleted_count = 0
            target_delete_count = len(available_nodes)
            deleted_pods = []

            for node_name in available_nodes:
                if deleted_count >= target_delete_count:
                    break

                field_selector = f"spec.nodeName={node_name}"
                pods = await self.core_v1.list_namespaced_pod(
                    namespace=namespace, label_selector=label_selector, field_selector=field_selector
                )

                for pod in pods.items:
                    if deleted_count >= target_delete_count:
                        break

                    logger.info(f"删除pod: {pod.metadata.name} (节点: {pod.spec.node_name})")
                    await self.core_v1.delete_namespaced_pod(name=pod.metadata.name, namespace=namespace)
                    deleted_pods.append(pod.metadata.name)
                    deleted_count += 1
                    break

            logger.info(f"删除了 {deleted_count} 个 {deployment_name} 的pod，目标删除数量: {target_delete_count}")
            return deleted_pods

        except ApiException as exc:
            logger.error(f"删除pod时出错: {exc}")
            raise Exception(f"删除pod失败: {str(exc)}") from exc
        except Exception as exc:
            logger.error(f"删除pod时发生未知错误: {exc}")
            raise Exception(f"删除pod失败: {str(exc)}") from exc
