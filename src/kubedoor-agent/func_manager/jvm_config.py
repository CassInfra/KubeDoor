"""Read and apply explicitly configured Java launcher memory options."""

from aiohttp import web
from kubernetes_asyncio.client.rest import ApiException
from loguru import logger

# The parsing rules are shared with the AI gateway, which edits uncontrolled
# Deployments directly and must produce the same args as this webhook.
from kubedoor_tools.workload import (  # noqa: F401  re-exported for existing callers
    JVM_FIELDS,
    JVM_FLAGS,
    MAX_BIGINT,
    extract_jvm_config,
    format_jvm_size,
    parse_jvm_size,
    replace_jvm_flags,
)


async def get_jvm_configs(apps_v1, request: web.Request) -> web.Response:
    """Batch-read first-container launch options without per-Deployment requests."""
    namespace = request.query.get("namespace")
    continuation = None
    configs = []
    try:
        while True:
            options = {"limit": 500, "_continue": continuation, "_request_timeout": 30}
            if namespace:
                deployments = await apps_v1.list_namespaced_deployment(namespace=namespace, **options)
            else:
                deployments = await apps_v1.list_deployment_for_all_namespaces(**options)
            for deployment in deployments.items:
                containers = deployment.spec.template.spec.containers or []
                config = extract_jvm_config(containers[0].args if containers else None)
                strategy = getattr(deployment.spec, "strategy", None)
                rolling_update = getattr(strategy, "rolling_update", None)
                configs.append({
                    "namespace": deployment.metadata.namespace,
                    "deployment": deployment.metadata.name,
                    **config,
                    "max_unavailable": getattr(rolling_update, "max_unavailable", None),
                    "max_surge": getattr(rolling_update, "max_surge", None),
                })
            continuation = deployments.metadata._continue
            if not continuation:
                break
        return web.json_response({"success": True, "data": configs})
    except ApiException as exc:
        logger.warning(f"批量读取 JVM 启动参数失败: {exc.status} {exc.reason}")
        return web.json_response({"success": False, "error": f"Kubernetes API: {exc.status} {exc.reason}"}, status=502)
    except Exception as exc:
        logger.exception("批量读取 JVM 启动参数失败")
        return web.json_response({"success": False, "error": str(exc)}, status=500)
