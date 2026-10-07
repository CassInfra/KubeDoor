"""Read and apply explicitly configured Java launcher memory options."""

import re
from typing import Any, Dict, Iterator, Optional, Tuple

from aiohttp import web
from kubernetes_asyncio.client.rest import ApiException
from loguru import logger


JVM_FLAGS = {
    "jvm_xms_bytes": "-Xms",
    "jvm_xmx_bytes": "-Xmx",
    "jvm_xss_bytes": "-Xss",
    "jvm_max_metaspace_bytes": "-XX:MaxMetaspaceSize=",
}
JVM_FIELDS = tuple(JVM_FLAGS)
MAX_BIGINT = (1 << 63) - 1
_SIZE_RE = re.compile(r"([0-9]+)([kKmMgG]?)\Z")
_OPTION_OPERANDS = {
    "-cp", "-classpath", "--class-path", "-p", "--module-path",
    "--upgrade-module-path", "--add-modules", "--limit-modules",
    "--add-reads", "--add-exports", "--add-opens", "--patch-module",
    "--enable-native-access", "--source",
}


def parse_jvm_size(value: str) -> Optional[int]:
    """Convert an integer Java size to exact bytes that fit a PostgreSQL bigint."""
    match = _SIZE_RE.fullmatch(value)
    if not match:
        return None
    try:
        amount = int(match.group(1))
    except ValueError:
        return None
    exponent = {"": 0, "k": 1, "m": 2, "g": 3}[match.group(2).lower()]
    size = amount * (1024 ** exponent)
    return size if size <= MAX_BIGINT else None


def format_jvm_size(size: int, unit: str = "m") -> str:
    """Prefer the field's unit without rounding; Java options reject decimals."""
    divisor = 1024 if unit == "k" else 1024 * 1024
    if size % divisor == 0:
        return f"{size // divisor}{unit}"
    if unit == "m" and size % 1024 == 0:
        return f"{size // 1024}k"
    return str(size)


def _java_options(args: Any) -> Iterator[Tuple[int, str, str]]:
    # Only the agreed Kubernetes args form is supported, not shell/env expansion.
    if not isinstance(args, list) or not args or args[0] != "java":
        return
    index = 1
    while index < len(args):
        argument = args[index]
        if not isinstance(argument, str):
            return
        if argument in {"-jar", "-m", "--module", "--"} or argument.startswith("--module="):
            return
        # The first non-option is the main class/source file; the rest belongs to the app.
        if not argument.startswith("-"):
            return
        if argument in _OPTION_OPERANDS:
            index += 2
            continue
        for field, prefix in JVM_FLAGS.items():
            if argument.startswith(prefix):
                suffix = argument[len(prefix):]
                yield index, field, suffix
                break
        index += 1


def extract_jvm_config(args: Any) -> Dict[str, Optional[int]]:
    """Extract the last occurrence of each supported launcher option."""
    config = dict.fromkeys(JVM_FIELDS)
    for _, field, size in _java_options(args):
        # An invalid/overflowing final value must not leave an earlier value in place.
        config[field] = parse_jvm_size(size)
    return config


def replace_jvm_flags(args: Any, config: Dict[str, Any]) -> Any:
    """Replace existing named options only, preserving every other argument."""
    if not isinstance(args, list):
        return args
    result = list(args)
    for index, field, original_size in _java_options(args):
        size = config.get(field)
        if _SIZE_RE.fullmatch(original_size) and type(size) is int and 0 <= size <= MAX_BIGINT:
            unit = "k" if field == "jvm_xss_bytes" else "m"
            result[index] = JVM_FLAGS[field] + format_jvm_size(size, unit)
    return result


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
                if not containers:
                    continue
                config = extract_jvm_config(containers[0].args)
                if any(value is not None for value in config.values()):
                    configs.append({
                        "namespace": deployment.metadata.namespace,
                        "deployment": deployment.metadata.name,
                        **config,
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
