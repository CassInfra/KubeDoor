import os
import sys
import json
import requests
from loguru import logger


NODE_LABLE_VALUE = "kubedoor-scheduler"

logger.remove()
logger.add(
    sys.stderr,
    format='<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> [<level>{level}</level>] <level>{message}</level>',
    level='INFO',
)


# 环境变量

MSG_TOKEN = os.environ.get('MSG_TOKEN')
MSG_TYPE = os.environ.get('MSG_TYPE')
KUBEDOOR_MASTER = os.environ.get('KUBEDOOR_MASTER')
PROM_K8S_TAG_VALUE = os.environ.get('PROM_K8S_TAG_VALUE')
OSS_URL = os.environ.get('OSS_URL')
BASE64CA = 'LS0tLS1CRUdJTiBDRVJUSUZJQ0FURS0tLS0tCk1JSURJVENDQWdtZ0F3SUJBZ0lKQUk1T3cvQnRxSEJpTUEwR0NTcUdTSWIzRFFFQkN3VUFNQ1l4SkRBaUJnTlYKQkFNTUcydDFZbVZrYjI5eUxXRm5aVzUwTG10MVltVmtiMjl5TG5OMll6QWdGdzB5TlRBek1UQXdNekkwTXpsYQpHQTh5TVRJMU1ESXhOREF6TWpRek9Wb3dKakVrTUNJR0ExVUVBd3diYTNWaVpXUnZiM0l0WVdkbGJuUXVhM1ZpClpXUnZiM0l1YzNaak1JSUJJakFOQmdrcWhraUc5dzBCQVFFRkFBT0NBUThBTUlJQkNnS0NBUUVBdmNzcWdCb3YKZFpqcGxXN1RTOHFpSnFoTFZuNXZ4VTdrWjdiQkUrVmdDNDYyUHJKblRGTjlDOC90bXIrSE43UUppYnBsVkEwQQp6MUZNalFjdk8zR2NieWJvMXo2b0thSm11MUlnZGxrMWNzYThJMlF3Ny9PZHQzZS9McG9oeGJpa0lkS3M3Nmd4CnI1WkRpRlYxVTllUzEzZmlWZE0zLzhjdjBqKzh6aEZyRndRaUp5ZTRZbWFOZFBTRlAxbVJuNWJ6MG8zTmUvU1oKcDB4dm1NY0xVMUFjOHNqUW1PRExoMTVYRjQ1dWU5LzQ2NzZCWjRQSTFZMWZnWHZHdzRDTFBaZzlEOCtjcndXVwo1bWhZV2U3TVVkeDF1cW5uMEtjRjc3dEI3WXIvOEczT2k3SlNaZitoYitQWVJYeDBVakU3OEUwOXNXc0VlY0tFCjVUNVU4K2MyOUZlSlR3SURBUUFCbzFBd1RqQWRCZ05WSFE0RUZnUVUvb09GYTFoYWFMQ3Q2dHNHT0FwK1E1M1QKRm5rd0h3WURWUjBqQkJnd0ZvQVUvb09GYTFoYWFMQ3Q2dHNHT0FwK1E1M1RGbmt3REFZRFZSMFRCQVV3QXdFQgovekFOQmdrcWhraUc5dzBCQVFzRkFBT0NBUUVBSUxrTG94MGo5M1I5U25ncVlSbmxFUW43NHVHTFNiQno1NC93Ckk3SVVaeHV0S1lzYkNXdFRTcGsvSXFadVlvQWY0WTY0MTFZRUxKMmNyZTN0VTlvWmxEbXFMWlJYK0laUXVLakkKZWJ0Qy9vUUMvYmpmZ1BRRTlxN2hHMGtJY2g0eEUveFdXMk0vekYwd2hOQ3hrbjVUVmNPVE44U205d2ZPM1hZcgpZam9YT0ZPMnRVZjBRYStJdjB1cWJScGZ5U1BTc0RYMVR6QWZQM3d4R2JyQnArcTRQMFk4L0hDaTljVlFYRmJLCmZPR2lRRi9kYnh0Z2VtbWROL3J3ZGxsVmhKUEszZEZEeWJnTlhZSzdTV0ZrVklEdXI5Wm0xamFJc1liNEJ2bjAKVk5mNFp5UzZRRThJUk8xTlEza2ZYZDZOazNTOHc2ejJpUUw3emJzN1ZxTkpxclQxeVE9PQotLS0tLUVORCBDRVJUSUZJQ0FURS0tLS0tCg=='


def get_version():
    try:
        with open('/app/version', 'r') as f:
            return f.read().strip()
    except Exception as e:
        logger.error(f"读取版本号失败：{e}")
        return "unknown"


def send_msg(content):
    response = ""
    if MSG_TYPE == "wecom":
        response = wecom(MSG_TOKEN, content)
    elif MSG_TYPE == "dingding":
        response = dingding(MSG_TOKEN, content)
    elif MSG_TYPE == "feishu":
        response = feishu(MSG_TOKEN, content)
    elif MSG_TYPE == "slack":
        response = slack(MSG_TOKEN, content)
    else:
        logger.warning(f"不支持的消息类型：{MSG_TYPE}")
        return f"不支持的消息类型：{MSG_TYPE}"

    logger.info(f'【{MSG_TYPE}】{response}')
    return f'【{MSG_TYPE}】{response}'


def wecom(webhook, content, at=""):
    webhook = 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=' + webhook
    headers = {'Content-Type': 'application/json'}
    params = {'msgtype': 'markdown', 'markdown': {'content': f"{content}<@{at}>"}}
    data = bytes(json.dumps(params), 'utf-8')
    response = requests.post(webhook, headers=headers, data=data)
    return response.json()


def dingding(webhook, content, at=""):
    webhook = 'https://oapi.dingtalk.com/robot/send?access_token=' + webhook
    headers = {'Content-Type': 'application/json'}
    params = {"msgtype": "markdown", "markdown": {"title": "告警", "text": content}, "at": {"atMobiles": [at]}}
    data = bytes(json.dumps(params), 'utf-8')
    response = requests.post(webhook, headers=headers, data=data)
    return response.json()


def feishu(webhook, content, at=""):
    title = "告警通知"
    webhook = f'https://open.feishu.cn/open-apis/bot/v2/hook/{webhook}'
    headers = {'Content-Type': 'application/json'}
    params = {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text", "content": title}, "template": "red"},
            "elements": [
                {
                    "tag": "markdown",
                    "content": f"{content}\n<at id={at}></at>",
                }
            ],
        },
    }
    data = json.dumps(params)
    response = requests.post(webhook, headers=headers, data=data)
    return response.json()


def slack(webhook, content, at=""):
    """发送Slack告警通知"""
    # 构建完整的Slack Webhook URL
    webhook_url = f'https://hooks.slack.com/services/{webhook}'
    headers = {'Content-Type': 'application/json'}

    # 构建消息内容，如果有@用户则添加
    message_text = content
    if at:
        message_text += f" <@{at}>"

    params = {"text": message_text}

    data = json.dumps(params)
    response = requests.post(webhook_url, headers=headers, data=data)
    logger.info(f'【slack】{response.json()}')
    return response.json()


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
