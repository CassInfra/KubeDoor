from datetime import datetime

from aiohttp import web
from kubernetes_asyncio import client
from kubernetes_asyncio.client.exceptions import ApiException
from loguru import logger


async def get_configmap_list(core_v1, request):
    """
    GET接口：查询ConfigMap列表
    参数：
    - env: 集群名称
    - namespace: 命名空间
    - core_v1: Kubernetes CoreV1Api客户端
    返回：集群名、命名空间、ConfigMap名称、data的keys、创建时间
    """
    try:
        env = request.query.get('env')
        namespace = request.query.get('namespace')

        if not env:
            return web.json_response({"error": "缺少必要参数: env"}, status=400)

        # 根据是否传入namespace，决定查询范围
        if namespace:
            logger.info(f"查询 ConfigMap 列表: namespace={namespace}")
            configmap_list = await core_v1.list_namespaced_config_map(namespace=namespace)
        else:
            logger.info("查询所有命名空间的 ConfigMap 列表")
            configmap_list = await core_v1.list_config_map_for_all_namespaces()

        result = []
        for cm in configmap_list.items:
            data_keys = list(cm.data.keys()) if cm.data else []
            binary_data_keys = list(cm.binary_data.keys()) if cm.binary_data else []
            all_keys = data_keys + binary_data_keys

            result.append(
                {
                    "env": env,
                    "namespace": cm.metadata.namespace,
                    "name": cm.metadata.name,
                    "data_keys": all_keys,
                    "creation_timestamp": (
                        cm.metadata.creation_timestamp.isoformat() if cm.metadata.creation_timestamp else None
                    ),
                }
            )

        return web.json_response({"success": True, "data": result, "total": len(result)})

    except ApiException as e:
        logger.error(f"查询ConfigMap列表失败: {e}")
        return web.json_response({"error": f"Kubernetes API错误: {e.reason}"}, status=e.status)
    except Exception as e:
        logger.error(f"查询ConfigMap列表异常: {e}")
        return web.json_response({"error": f"服务器内部错误: {str(e)}"}, status=500)


async def get_namespace_list(core_v1, request):
    try:
        env = request.query.get('env')
        if not env:
            return web.json_response({"error": "缺少必要参数: env"}, status=400)

        ns_list = await core_v1.list_namespace()
        names = [ns.metadata.name for ns in ns_list.items if ns.metadata and ns.metadata.name]
        names_sorted = sorted(names)

        return web.json_response({"success": True, "data": names_sorted})
    except ApiException as e:
        logger.error(f"查询命名空间列表失败: {e}")
        return web.json_response({"success": False, "data": f"Kubernetes API错误: {e.reason}"})
    except Exception as e:
        logger.error(f"查询命名空间列表异常: {e}")
        return web.json_response({"success": False, "data": f"服务器内部错误: {str(e)}"})
