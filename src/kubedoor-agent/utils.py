import os
import sys
from urllib.parse import urlencode
from loguru import logger

from func_manager import notifier


NODE_LABLE_VALUE = "kubedoor-scheduler"

logger.remove()
logger.add(
    sys.stderr,
    format='<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> [<level>{level}</level>] <level>{message}</level>',
    level='INFO',
)


# 环境变量(运维通知不再需要 IM 的配置:统一交给 master 按集群选群发送)

KUBEDOOR_MASTER = os.environ.get('KUBEDOOR_MASTER')
PROM_K8S_TAG_VALUE = os.environ.get('PROM_K8S_TAG_VALUE')
OSS_URL = os.environ.get('OSS_URL')


# 定时任务到点回调 agent 自己时,原样带上的扩缩容 / 重启开关
CRON_FORWARD_QUERY = ("add_label", "type", "temp", "scheduler", "cci")


def cron_callback_url(action, query, namespace=None):
    """定时任务到点调用 agent 自己 /api/scale 或 /api/restart 的地址。

    页面和 AI 设的临时扩缩容(temp)、节点策略(type)、CCI 开关都要带上:以前只带 add_label / scheduler,
    其余到点全丢了(受管控的 Deployment 临时扩缩会被准入控制改回),两个同时有时还会拼出两个 "?"。
    """
    ns = namespace or os.environ.get("POD_NAMESPACE") or "kubedoor"
    params = {key: query[key] for key in CRON_FORWARD_QUERY if query.get(key)}
    url = f"https://kubedoor-agent.{ns}/api/{action}"
    return f"{url}?{urlencode(params)}" if params else url


def get_version():
    try:
        with open('/app/version', 'r') as f:
            return f.read().strip()
    except Exception as e:
        logger.error(f"读取版本号失败：{e}")
        return "unknown"


def send_msg(content):
    """发运维通知:agent 不直接调 IM 接口,交给 master 按集群选群发送。

    不阻塞调用方,事件循环里、线程里都可以调用。主进程经 WebSocket 发给 master(断线时暂存、重连后补发),
    pod-mgr 经 Unix socket 交给主进程,见 func_manager/notifier.py。
    """
    notifier.send_msg(content)


def parse_cpu(value) -> float:
    """Convert Kubernetes CPU units to mCPU (millicores)."""
    if not isinstance(value, str):
        try:
            return round(float(value) * 1000, 2)
        except (ValueError, TypeError):
            return 0.0

    if value.endswith("k"):
        try:
            return round(float(value[:-1]) * 1000 * 1000, 2)
        except ValueError:
            return 0.0
    if value.endswith("m"):
        try:
            return round(float(value[:-1]), 2)
        except ValueError:
            return 0.0
    if value.endswith("n"):
        try:
            return round(int(value[:-1]) / 1_000_000, 2)
        except ValueError:
            return 0.0
    try:
        return round(float(value) * 1000, 2)
    except ValueError:
        return 0.0


def parse_pods(value) -> int:
    """Convert Kubernetes pod units to integer."""
    if not isinstance(value, str):
        try:
            return int(value)
        except (ValueError, TypeError):
            return 0

    if value.endswith("k"):
        try:
            return int(float(value[:-1]) * 1000)
        except ValueError:
            return 0
    try:
        return int(value)
    except ValueError:
        return 0


def parse_memory(value) -> float:
    """Convert Kubernetes memory units to MB."""
    if not isinstance(value, str):
        try:
            return round(int(value) / (1024 * 1024), 2)
        except (ValueError, TypeError):
            return 0.0

    if value.endswith("Ki"):
        try:
            return round(int(value[:-2]) / 1024, 2)
        except ValueError:
            return 0.0
    if value.endswith("Mi"):
        try:
            return round(float(value[:-2]), 2)
        except ValueError:
            return 0.0
    if value.endswith("Gi"):
        try:
            return round(float(value[:-2]) * 1024, 2)
        except ValueError:
            return 0.0
    try:
        return round(int(value) / (1024 * 1024), 2)
    except (ValueError, TypeError):
        return 0.0


def bytes_to_gib(value) -> float:
    """Convert bytes to GiB."""
    return round(value / (1024 * 1024 * 1024), 2)


def parse_storage_to_gib(value) -> float:
    """Convert Kubernetes storage units to GiB."""
    if not isinstance(value, str):
        try:
            return round(int(value) / (1024 * 1024 * 1024), 2)
        except (ValueError, TypeError):
            return 0.0

    if value.endswith("Ki"):
        try:
            return round(int(value[:-2]) / (1024 * 1024), 2)
        except ValueError:
            return 0.0
    if value.endswith("Mi"):
        try:
            return round(int(value[:-2]) / 1024, 2)
        except ValueError:
            return 0.0
    if value.endswith("Gi"):
        try:
            return round(float(value[:-2]), 2)
        except ValueError:
            return 0.0
    if value.endswith("Ti"):
        try:
            return round(float(value[:-2]) * 1024, 2)
        except ValueError:
            return 0.0
    try:
        return round(int(value) / (1024 * 1024 * 1024), 2)
    except (ValueError, TypeError):
        return 0.0
