from aiohttp import web
from kubernetes_asyncio.client.exceptions import ApiException
from loguru import logger


async def get_ingress_list(networking_v1, request):
    """获取指定命名空间的 Ingress 列表"""
    try:

        namespace = request.query.get('namespace')

        if namespace:
            logger.info(f"查询 Ingress 列表: namespace={namespace}")
            ingress_list = await networking_v1.list_namespaced_ingress(namespace=namespace)
            logger.info(f"命名空间 {namespace} 查询到 {len(ingress_list.items)} 个 Ingress")
        else:
            logger.info("查询所有命名空间的 Ingress 列表")
            ingress_list = await networking_v1.list_ingress_for_all_namespaces()
            logger.info(f"查询到 {len(ingress_list.items)} 个 Ingress")

        items = ingress_list.items or []

        result = []
        for ing in items:
            annotations = ing.metadata.annotations or {}
            ingress_class = annotations.get('kubernetes.io/ingress.class', '')

            tls_secret_names = []
            if ing.spec and ing.spec.tls:
                for tls in ing.spec.tls:
                    if tls.secret_name:
                        tls_secret_names.append(tls.secret_name)

            rule_hosts = []
            if ing.spec and ing.spec.rules:
                for rule in ing.spec.rules:
                    host_value = rule.host or '*'
                    rule_hosts.append(host_value)

            result.append(
                {
                    "namespace": ing.metadata.namespace or namespace,
                    "name": ing.metadata.name,
                    "ingress_class": ingress_class,
                    "tls_secret_names": tls_secret_names,
                    "rules_hosts": rule_hosts,
                    "creation_timestamp": (
                        ing.metadata.creation_timestamp.isoformat() if ing.metadata.creation_timestamp else None
                    ),
                }
            )

        if not result:
            logger.warning(f"Ingress 列表结果为空: namespace={namespace}")
        return web.json_response({"success": True, "data": result})

    except ApiException as e:
        logger.error(f"查询 Ingress 列表失败: {e}")
        return web.json_response({"error": f"Kubernetes API 错误: {e.reason}"}, status=e.status)
    except Exception as e:
        logger.error(f"查询 Ingress 列表异常: {e}")
        return web.json_response({"error": f"服务器内部错误: {str(e)}"}, status=500)


async def get_ingress_rules(custom_api, request):
    """获取指定 Ingress 的规则明细"""
    try:

        namespace = request.query.get('namespace')
        ingress_name = request.query.get('ingress_name')

        if not all([namespace, ingress_name]):
            return web.json_response({"error": "缺少必要参数: namespace, ingress_name"}, status=400)

        logger.info(f"查询 Ingress 规则: namespace={namespace}, ingress={ingress_name}")

        ingress_dict = await custom_api.get_namespaced_custom_object(
            group="networking.k8s.io",
            version="v1",
            namespace=namespace,
            plural="ingresses",
            name=ingress_name,
        )

        rules_detail = {}
        for rule in ingress_dict.get('spec', {}).get('rules', []) or []:
            host_value = rule.get('host') or '*'
            http_info = rule.get('http') or {}
            paths = http_info.get('paths') or []

            path_entries = []
            for path in paths:
                backend = path.get('backend') or {}
                service = backend.get('service') or {}

                path_entries.append(
                    {
                        "path": path.get('path', ''),
                        "pathType": path.get('pathType') or path.get('path_type') or '',
                        "backend_name": service.get('name'),
                        "backend_port": service.get('port', {}).get('number'),
                        "property": path.get('property'),
                    }
                )

            rules_detail.setdefault(host_value, []).extend(path_entries)

        logger.info(f"Ingress {namespace}/{ingress_name} 规则处理完成, host数量={len(rules_detail)}")

        return web.json_response({"success": True, "data": rules_detail})

    except ApiException as e:
        logger.error(f"获取 Ingress 规则失败: {e}")
        if e.status == 404:
            return web.json_response(
                {"error": f"Ingress '{ingress_name}' 在命名空间 '{namespace}' 中不存在"}, status=404
            )
        return web.json_response({"error": f"Kubernetes API 错误: {e.reason}"}, status=e.status)
    except Exception as e:
        logger.error(f"获取 Ingress 规则异常: {e}")
        return web.json_response({"error": f"服务器内部错误: {str(e)}"}, status=500)
