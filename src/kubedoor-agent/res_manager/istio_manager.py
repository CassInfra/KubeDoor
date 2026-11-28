import json
from aiohttp import web
from kubernetes_asyncio.client.exceptions import ApiException
from loguru import logger


async def get_virtualservice(custom_objects_api, request):
    """
    GET接口：获取所有VirtualService列表
    参数：
    - custom_objects_api: Kubernetes CustomObjectsApi客户端
    返回：VirtualService的简化信息列表
    """
    try:
        # VirtualService的API信息
        group = "networking.istio.io"
        version = "v1beta1"
        plural = "virtualservices"

        # 获取所有命名空间的VirtualService
        vs_list = await custom_objects_api.list_cluster_custom_object(group=group, version=version, plural=plural)

        result = []
        for vs in vs_list.get('items', []):
            metadata = vs.get('metadata', {})

            result.append(
                {
                    "name": metadata.get('name', ''),
                    "namespace": metadata.get('namespace', ''),
                    "resourceVersion": metadata.get('resourceVersion', ''),
                    "spec": vs.get('spec', {}),
                }
            )

        return web.json_response({"success": True, "data": result, "total": len(result)})

    except ApiException as e:
        logger.error(f"查询VirtualService列表失败: {e}")
        return web.json_response({"error": f"Kubernetes API错误: {e.reason}"}, status=e.status)
    except Exception as e:
        logger.error(f"查询VirtualService列表异常: {e}")
        return web.json_response({"error": f"服务器内部错误: {str(e)}"}, status=500)


async def get_virtualservice_detail(custom_objects_api, request):
    """
    GET接口：获取单个VirtualService的完整内容（含 metadata，和 kubectl get -o yaml 一样）
    参数：
    - custom_objects_api: Kubernetes CustomObjectsApi客户端
    - request: HTTP请求对象，查询参数包含name和namespace
    返回：VirtualService对象
    """
    name = request.query.get('name')
    namespace = request.query.get('namespace')
    if not name or not namespace:
        return web.json_response({"error": "缺少必要参数: namespace, name"}, status=400)
    try:
        vs = await custom_objects_api.get_namespaced_custom_object(
            group="networking.istio.io", version="v1beta1", namespace=namespace, plural="virtualservices", name=name
        )
        return web.json_response({"success": True, "data": vs})

    except ApiException as e:
        if e.status == 404:
            return web.json_response({"error": f"VirtualService {namespace}/{name} not found"}, status=404)
        logger.error(f"查询VirtualService失败: {e}")
        return web.json_response({"error": f"Kubernetes API错误: {e.reason}"}, status=e.status)
    except Exception as e:
        logger.error(f"查询VirtualService异常: {e}")
        return web.json_response({"error": f"服务器内部错误: {str(e)}"}, status=500)


def _host_matches(patterns, host: str) -> bool:
    """VirtualService 的 hosts 是否匹配这个域名（* 匹配全部，*.example.com 匹配子域）"""
    host = (host or "").strip().lower()
    if not host:
        return True
    for pattern in patterns or []:
        pattern = str(pattern).strip().lower()
        if pattern == "*" or pattern == host:
            return True
        if pattern.startswith("*.") and host.endswith(pattern[1:]):
            return True
    return False


def _route_destinations(route: list) -> list:
    """route 里的 destination 压平成 {host, port, weight, subset}"""
    result = []
    for item in route or []:
        if not isinstance(item, dict):
            continue
        destination = item.get("destination") or {}
        entry = {"host": destination.get("host")}
        port = destination.get("port") or {}
        if port.get("number") is not None:
            entry["port"] = port["number"]
        for key in ("weight", "subset"):
            if item.get(key) is not None:
                entry[key] = item[key]
        result.append(entry)
    return result


def _route_summary(spec: dict) -> dict:
    """只留排障要用的字段，丢掉 istio 注入的大对象"""
    summary = {"gateways": spec.get("gateways") or [], "hosts": spec.get("hosts") or [],
               "export_to": spec.get("exportTo") or []}
    for kind in ("http", "tcp", "tls"):
        routes = []
        for rule in spec.get(kind) or []:
            if not isinstance(rule, dict):
                continue
            entry = {}
            if rule.get("match"):
                entry["match"] = rule["match"]
            destinations = _route_destinations(rule.get("route"))
            if destinations:
                entry["destination"] = destinations
            for key in ("timeout", "retries", "rewrite", "fault", "mirror"):
                if rule.get(key) is not None:
                    entry[key] = rule[key]
            routes.append(entry)
        if routes:
            summary[kind] = routes
    return summary


async def get_virtualservice_routes(custom_objects_api, request):
    """GET ?host=&namespace=&name=：VirtualService 路由摘要（不含完整 spec）"""
    host = (request.query.get("host") or "").strip()
    namespace = (request.query.get("namespace") or "").strip()
    name = (request.query.get("name") or "").strip()
    group, version, plural = "networking.istio.io", "v1beta1", "virtualservices"
    try:
        if namespace:
            vs_list = await custom_objects_api.list_namespaced_custom_object(
                group=group, version=version, namespace=namespace, plural=plural)
        else:
            vs_list = await custom_objects_api.list_cluster_custom_object(group=group, version=version, plural=plural)
        result = []
        for vs in vs_list.get("items", []):
            metadata = vs.get("metadata") or {}
            spec = vs.get("spec") or {}
            if name and metadata.get("name") != name:
                continue
            if not _host_matches(spec.get("hosts"), host):
                continue
            result.append({"namespace": metadata.get("namespace") or namespace or "",
                           "name": metadata.get("name") or "",
                           "resourceVersion": metadata.get("resourceVersion") or "",
                           **_route_summary(spec)})
        return web.json_response({"success": True, "data": result, "total": len(result)})
    except ApiException as e:
        logger.error(f"查询VirtualService路由摘要失败: {e}")
        return web.json_response({"error": f"Kubernetes API错误: {e.reason}"}, status=e.status)
    except Exception as e:
        logger.error(f"查询VirtualService路由摘要异常: {e}")
        return web.json_response({"error": f"服务器内部错误: {str(e)}"}, status=500)


async def apply_virtualservice(custom_objects_api, request):
    """
    POST接口：创建或更新VirtualService
    参数：
    - custom_objects_api: Kubernetes CustomObjectsApi客户端
    - request: HTTP请求对象，body中包含VirtualService的JSON配置
    返回：操作结果
    """
    try:
        # VirtualService的API信息
        group = "networking.istio.io"
        version = "v1beta1"
        plural = "virtualservices"

        # 获取请求体中的VirtualService配置
        body = await request.json()

        # 验证必要字段
        if not body.get('metadata') or not body.get('spec'):
            return web.json_response({"error": "缺少必要字段 metadata 或 spec"}, status=400)

        metadata = body.get('metadata', {})
        name = metadata.get('name')
        namespace = metadata.get('namespace', 'default')

        if not name:
            return web.json_response({"error": "缺少 metadata.name 字段"}, status=400)

        # 尝试使用Server-Side Apply (类似kubectl apply)
        try:
            # 确保metadata中有必要的字段用于Server-Side Apply
            if 'metadata' not in body:
                body['metadata'] = {}

            # 设置field manager用于Server-Side Apply
            field_manager = 'kubedoor-agent'

            logger.info(f"使用Server-Side Apply更新VirtualService: {namespace}/{name}")

            # 使用Server-Side Apply方式更新（类似kubectl apply）
            # 可以使用JSON格式，不一定要YAML
            result = await custom_objects_api.patch_namespaced_custom_object_with_http_info(
                group=group,
                version=version,
                namespace=namespace,
                plural=plural,
                name=name,
                body=body,  # 直接使用JSON格式的body
                _content_type='application/apply-patch+yaml',  # Whether you are submitting JSON data or YAML data, use application/apply-patch+yaml as the Content-Type header value.
                field_manager=field_manager,
                force=True,  # 强制应用，解决字段冲突
            )
            operation = "applied"
            result = result[0]  # 从http_info中提取实际结果

        except ApiException as e:
            if e.status == 404:
                # 如果资源不存在，则创建
                logger.info(f"创建VirtualService: {namespace}/{name}")
                result = await custom_objects_api.create_namespaced_custom_object(
                    group=group, version=version, namespace=namespace, plural=plural, body=body
                )
                operation = "created"
            elif e.status in [400, 422]:  # Server-Side Apply失败，回退到传统方式
                logger.warning(f"Server-Side Apply失败，回退到传统replace方式: {e.reason}")
                try:
                    existing_vs = await custom_objects_api.get_namespaced_custom_object(
                        group=group, version=version, namespace=namespace, plural=plural, name=name
                    )
                    # 设置resourceVersion进行完整替换
                    body['metadata']['resourceVersion'] = existing_vs['metadata']['resourceVersion']
                    result = await custom_objects_api.replace_namespaced_custom_object(
                        group=group, version=version, namespace=namespace, plural=plural, name=name, body=body
                    )
                    operation = "updated"
                except ApiException as replace_e:
                    if replace_e.status == 404:
                        # 资源不存在，创建新的
                        result = await custom_objects_api.create_namespaced_custom_object(
                            group=group, version=version, namespace=namespace, plural=plural, body=body
                        )
                        operation = "created"
                    else:
                        raise replace_e
            else:
                raise e

        # 确保result不为None，并提取metadata信息
        result_data = {}
        if result and isinstance(result, dict):
            metadata = result.get('metadata', {})
            result_data = {
                "name": metadata.get('name'),
                "namespace": metadata.get('namespace'),
                "resourceVersion": metadata.get('resourceVersion'),
            }
        else:
            # 如果result为None或不是字典，使用请求中的信息
            result_data = {
                "name": name,
                "namespace": namespace,
                "resourceVersion": None,
            }

        return web.json_response(
            {
                "success": True,
                "message": f"VirtualService {name} {operation} successfully",
                "data": result_data,
            }
        )

    except ApiException as e:
        logger.error(f"应用VirtualService失败: {e}")
        return web.json_response({"error": f"Kubernetes API错误: {e.reason}"}, status=e.status)
    except json.JSONDecodeError:
        return web.json_response({"error": "请求体不是有效的JSON格式"}, status=400)
    except Exception as e:
        logger.error(f"应用VirtualService异常: {e}")
        return web.json_response({"error": f"服务器内部错误: {str(e)}"}, status=500)


async def delete_virtualservice(custom_objects_api, request):
    """
    DELETE接口：删除VirtualService
    参数：
    - custom_objects_api: Kubernetes CustomObjectsApi客户端
    - request: HTTP请求对象，查询参数包含name和namespace
    返回：删除结果
    """
    try:
        # VirtualService的API信息
        group = "networking.istio.io"
        version = "v1beta1"
        plural = "virtualservices"

        # 获取查询参数
        name = request.query.get('name')
        namespace = request.query.get('namespace', 'default')

        if not name:
            return web.json_response({"error": "缺少必要参数: name"}, status=400)

        # 删除VirtualService
        logger.info(f"删除VirtualService: {namespace}/{name}")
        await custom_objects_api.delete_namespaced_custom_object(
            group=group, version=version, namespace=namespace, plural=plural, name=name
        )

        return web.json_response({"success": True, "message": f"VirtualService {name} deleted successfully"})

    except ApiException as e:
        if e.status == 404:
            return web.json_response({"error": f"VirtualService {name} not found"}, status=404)
        logger.error(f"删除VirtualService失败: {e}")
        return web.json_response({"error": f"Kubernetes API错误: {e.reason}"}, status=e.status)
    except Exception as e:
        logger.error(f"删除VirtualService异常: {e}")
        return web.json_response({"error": f"服务器内部错误: {str(e)}"}, status=500)
