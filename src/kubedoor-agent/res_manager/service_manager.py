from datetime import datetime
import yaml

from aiohttp import web
from kubernetes_asyncio import client
from kubernetes_asyncio.client.exceptions import ApiException
from loguru import logger


async def get_service_list(core_v1, request):
    """
    GET接口：查询Service列表
    参数：
    - namespace: 命名空间
    - core_v1: Kubernetes CoreV1Api客户端
    返回：集群名、命名空间、Service名称、type、clusterip、ports、selector、External IPs、externalTrafficPolicy、internalTrafficPolicy、创建时间
    """
    try:

        namespace = request.query.get('namespace')

        if namespace:
            logger.info(f"查询 Service 列表: namespace={namespace}")
            service_list = await core_v1.list_namespaced_service(namespace=namespace)
        else:
            logger.info("查询所有命名空间的 Service 列表")
            service_list = await core_v1.list_service_for_all_namespaces()

        result = []
        for svc in service_list.items:
            # 处理 clusterip:port 格式
            cluster_ip = svc.spec.cluster_ip or ""
            ports_info = []
            clusterip_ports = []

            if svc.spec.ports:
                for port in svc.spec.ports:
                    # ports格式: targetPort:nodePort
                    target_port = port.target_port if port.target_port else port.port
                    node_port = port.node_port if port.node_port else ""
                    port_info = f"{target_port}:{node_port}" if node_port else str(target_port)
                    ports_info.append(port_info)

                    # clusterip:port 格式
                    if cluster_ip and cluster_ip != "None":
                        clusterip_ports.append(f"{cluster_ip}:{port.port}")

            clusterip_str = ", ".join(clusterip_ports) if clusterip_ports else cluster_ip
            ports_str = ", ".join(ports_info)

            # 处理 Selector
            selector = svc.spec.selector or {}
            selector_str = ", ".join([f"{k}={v}" for k, v in selector.items()]) if selector else ""

            # 处理 External IPs - 根据Service类型采用不同策略
            external_ips_str = ""
            service_type = svc.spec.type or ""

            if service_type == "LoadBalancer":
                # LoadBalancer类型：优先从status.loadBalancer.ingress获取IP
                external_ips = []
                if svc.status and svc.status.load_balancer and svc.status.load_balancer.ingress:
                    for ingress in svc.status.load_balancer.ingress:
                        if ingress.ip:
                            external_ips.append(ingress.ip)

                # 如果没有获取到IP，则尝试从spec.loadBalancerIP获取
                if not external_ips and svc.spec.load_balancer_ip:
                    external_ips.append(svc.spec.load_balancer_ip)

                external_ips_str = ", ".join(external_ips) if external_ips else ""

            elif service_type == "ExternalName":
                # ExternalName类型：获取externalName
                external_ips_str = svc.spec.external_name or ""

            else:
                # 其他类型：使用原有逻辑获取external_ips
                external_ips = svc.spec.external_ips or []
                external_ips_str = ", ".join(external_ips) if external_ips else ""

            # 处理 Traffic Policies
            external_traffic_policy = svc.spec.external_traffic_policy or ""
            internal_traffic_policy = svc.spec.internal_traffic_policy or ""

            result.append(
                {
                    "namespace": svc.metadata.namespace,
                    "name": svc.metadata.name,
                    "type": svc.spec.type or "",
                    "clusterip": clusterip_str,
                    "ports": ports_str,
                    "selector": selector_str,
                    "external_ips": external_ips_str,
                    "external_traffic_policy": external_traffic_policy,
                    "internal_traffic_policy": internal_traffic_policy,
                    "creation_timestamp": (
                        svc.metadata.creation_timestamp.isoformat() if svc.metadata.creation_timestamp else None
                    ),
                }
            )

        return web.json_response({"success": True, "data": result, "total": len(result)})

    except ApiException as e:
        logger.error(f"查询Service列表失败: {e}")
        return web.json_response({"error": f"Kubernetes API错误: {e.reason}"}, status=e.status)
    except Exception as e:
        logger.error(f"查询Service列表异常: {e}")
        return web.json_response({"error": f"服务器内部错误: {str(e)}"}, status=500)


async def get_service_content(core_v1, request):
    """
    GET接口：获取指定Service内容，返回标准的Kubernetes YAML格式
    参数：

    - namespace: 命名空间
    - service_name: Service名称
    - core_v1: Kubernetes CoreV1Api客户端
    """
    try:
        namespace = request.query.get('namespace')
        service_name = request.query.get('service_name')

        if not all([namespace, service_name]):
            return web.json_response({"error": "缺少必要参数: namespace, service_name"}, status=400)

        # 获取Service对象
        service = await core_v1.read_namespaced_service(name=service_name, namespace=namespace)

        # 使用async with确保ApiClient正确关闭
        async with client.ApiClient() as api_client:
            service_dict = api_client.sanitize_for_serialization(service)

        # 清理不需要的字段
        if 'metadata' in service_dict and 'managedFields' in service_dict['metadata']:
            del service_dict['metadata']['managedFields']
        # 清理kubectl注解
        annotations = service_dict.get('metadata', {}).get('annotations', {})
        if 'kubectl.kubernetes.io/last-applied-configuration' in annotations:
            del annotations['kubectl.kubernetes.io/last-applied-configuration']

        # 转换为YAML字符串
        yaml_content = yaml.dump(service_dict, default_flow_style=False, allow_unicode=True)
        logger.debug(f"获取Service内容成功: {yaml_content}")
        return web.json_response({"success": True, "data": yaml_content})

    except ApiException as e:
        logger.error(f"获取Service内容失败: {e}")
        if e.status == 404:
            return web.json_response(
                {"error": f"Service '{service_name}' 在命名空间 '{namespace}' 中不存在"}, status=404
            )
        return web.json_response({"error": f"Kubernetes API错误: {e.reason}"}, status=e.status)
    except Exception as e:
        logger.error(f"获取Service内容异常: {e}")
        return web.json_response({"error": f"服务器内部错误: {str(e)}"}, status=500)


async def get_service_endpoints(core_v1, request):
    """
    GET接口：获取指定Service的Endpoints信息
    参数：

    - namespace: 命名空间
    - service_name: Service名称
    - core_v1: Kubernetes CoreV1Api客户端
    返回：Endpoint的namespace、pod name、pod ip、nodeName、以及ports信息
    """
    try:

        namespace = request.query.get('namespace')
        service_name = request.query.get('service_name')

        if not all([namespace, service_name]):
            return web.json_response({"error": "缺少必要参数: namespace, service_name"}, status=400)

        # 获取指定Service的Endpoints
        try:
            endpoints = await core_v1.read_namespaced_endpoints(name=service_name, namespace=namespace)
        except ApiException as e:
            if e.status == 404:
                return web.json_response(
                    {"error": f"Service '{service_name}' 的 Endpoints 在命名空间 '{namespace}' 中不存在"}, status=404
                )
            raise

        result = {"namespace": namespace, "service_name": service_name, "subsets": []}

        # 处理 subsets 信息
        if endpoints.subsets:
            for subset in endpoints.subsets:
                subset_info = {"addresses": [], "ports": []}

                # 处理 addresses 信息
                if subset.addresses:
                    for address in subset.addresses:
                        address_info = {
                            "ip": address.ip,
                            "nodeName": address.node_name,
                            "pod_name": None,
                            "pod_namespace": None,
                        }

                        # 处理 targetRef 信息（Pod 引用）
                        if address.target_ref:
                            if address.target_ref.kind == "Pod":
                                address_info["pod_name"] = address.target_ref.name
                                address_info["pod_namespace"] = address.target_ref.namespace

                        subset_info["addresses"].append(address_info)

                # 处理 ports 信息
                if subset.ports:
                    for port in subset.ports:
                        port_info = {"name": port.name, "port": port.port, "protocol": port.protocol}
                        subset_info["ports"].append(port_info)

                result["subsets"].append(subset_info)

        return web.json_response({"success": True, "data": result})

    except ApiException as e:
        logger.error(f"获取Service Endpoints失败: {e}")
        return web.json_response({"error": f"Kubernetes API错误: {e.reason}"}, status=e.status)
    except Exception as e:
        logger.error(f"获取Service Endpoints异常: {e}")
        return web.json_response({"error": f"服务器内部错误: {str(e)}"}, status=500)


async def get_service_first_port(core_v1, request):
    """
    GET接口：获取指定Service的第一个端口号
    参数：
    - namespace: 命名空间
    - service_name: Service名称
    - core_v1: Kubernetes CoreV1Api客户端
    返回：Service的spec.ports第一个元素的port值
    """
    try:
        namespace = request.query.get('namespace')
        service_name = request.query.get('service_name')

        # 获取指定Service
        try:
            service = await core_v1.read_namespaced_service(name=service_name, namespace=namespace)
        except ApiException as e:
            if e.status == 404:
                return web.json_response(
                    {"error": f"Service '{service_name}' 在命名空间 '{namespace}' 中不存在"}, status=404
                )
            raise

        # 获取第一个端口的port值
        first_port = service.spec.ports[0].port
        result = {"first_port": first_port}
        return web.json_response({"success": True, "data": result})

    except ApiException as e:
        logger.error(f"获取Service第一个端口失败: {e}")
        return web.json_response({"error": f"Kubernetes API错误: {e.reason}"}, status=e.status)
    except Exception as e:
        logger.error(f"获取Service第一个端口异常: {e}")
        return web.json_response({"error": f"服务器内部错误: {str(e)}"}, status=500)
