"""Compatibility layer preserving legacy master/notify/channels.py and setup contracts."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .models import ChannelConfig, NotificationMessage
from .providers._common import fit_utf8_bytes
from .registry import get_provider

LEGACY_TYPES: tuple[str, ...] = ("wecom", "dingding", "feishu", "slack")
LEGACY_TYPE_LABELS: dict[str, str] = {
    "wecom": "企业微信",
    "dingding": "钉钉",
    "feishu": "飞书",
    "slack": "Slack",
}

MAX_TOKEN = 512
MAX_AT = 128
MAX_MESSAGE = 300
OK_MESSAGE = "发送成功"
WECOM_MAX_BYTES = 4096

_WECOM_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key="
_DINGDING_URL = "https://oapi.dingtalk.com/robot/send?access_token="
_FEISHU_URL = "https://open.feishu.cn/open-apis/bot/v2/hook/"
_SLACK_URL = "https://hooks.slack.com/services/"

_LEGACY_HOSTS: dict[str, str] = {
    "qyapi.weixin.qq.com": "wecom",
    "oapi.dingtalk.com": "dingding",
    "open.feishu.cn": "feishu",
    "hooks.slack.com": "slack",
}
_FEISHU_PATH = "/open-apis/bot/v2/hook/"
_SLACK_PATH = "/services/"

TOKEN_HINTS: dict[str, str] = {
    "wecom": "群机器人 Webhook 地址(https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=…),或 key= 后面那一段",
    "dingding": "自定义机器人 Webhook 地址(https://oapi.dingtalk.com/robot/send?access_token=…),"
    "或 access_token= 后面那一段",
    "feishu": "自定义机器人 Webhook 地址(https://open.feishu.cn/open-apis/bot/v2/hook/…),或 /hook/ 后面那一段",
    "slack": "Incoming Webhook 地址(https://hooks.slack.com/services/T…/B…/…),或 /services/ 后面那一段",
}

_TOKEN_PART = re.compile(r"[A-Za-z0-9_-]+")


def _has_control_char(value: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def _label(kind: str) -> str:
    return LEGACY_TYPE_LABELS.get(kind, str(kind))


def _check_kind(kind: str) -> None:
    if kind not in LEGACY_TYPES:
        raise ValueError(f'不支持的 IM 类型:{kind}(可选 {"、".join(LEGACY_TYPE_LABELS.values())})')


def _valid_token(kind: str, token: str) -> bool:
    if kind == "slack":
        parts = token.split("/")
        return len(parts) == 3 and all(_TOKEN_PART.fullmatch(part) for part in parts)
    return _TOKEN_PART.fullmatch(token) is not None


def _token_from_url(kind: str, value: str) -> str:
    hint = TOKEN_HINTS[kind]
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").lower()
    except ValueError:
        raise ValueError(f"不认识的机器人地址,{_label(kind)}请填写{hint}") from None
    owner = _LEGACY_HOSTS.get(host) if parts.scheme.lower() in ("http", "https") else None
    if owner is None:
        raise ValueError(f"不认识的机器人地址,{_label(kind)}请填写{hint}")
    if owner != kind:
        raise ValueError(f"这是{_label(owner)}的机器人地址,和选择的 IM 类型({_label(kind)})不一致")
    path = parts.path
    if kind == "wecom" and path == "/cgi-bin/webhook/send":
        return (parse_qs(parts.query).get("key") or [""])[0].strip()
    if kind == "dingding" and path == "/robot/send":
        return (parse_qs(parts.query).get("access_token") or [""])[0].strip()
    if kind == "feishu" and path.startswith(_FEISHU_PATH):
        return path[len(_FEISHU_PATH) :].strip("/")
    if kind == "slack" and path.startswith(_SLACK_PATH):
        return path[len(_SLACK_PATH) :].strip("/")
    raise ValueError(f"不认识的机器人地址,{_label(kind)}请填写{hint}")


def normalize_legacy_token(kind: str, value: Any) -> str:
    """Validate and extract legacy public-cloud token for wecom/dingding/feishu/slack."""
    _check_kind(kind)
    label = _label(kind)
    text = "" if value is None else str(value).strip()
    if not text:
        raise ValueError(f"请填写{label}机器人地址")
    if _has_control_char(text):
        raise ValueError("机器人地址不能包含换行等控制字符")
    if len(text) > MAX_TOKEN:
        raise ValueError(f"机器人地址太长(最多 {MAX_TOKEN} 个字符)")
    if any(ch.isspace() for ch in text):
        raise ValueError("机器人地址中间不能有空格")
    lowered = text.lower()
    if not lowered.startswith(("http://", "https://")) and lowered.split("/", 1)[0].split("?", 1)[0] in _LEGACY_HOSTS:
        text = "https://" + text
        lowered = text.lower()
    if lowered.startswith(("http://", "https://")) or "://" in text:
        token = _token_from_url(kind, text)
        if not token:
            raise ValueError(f"机器人地址里没有 token,{label}请填写{TOKEN_HINTS[kind]}")
    else:
        token = text
    if not _valid_token(kind, token):
        raise ValueError(f"机器人 token 格式不对,{label}请填写{TOKEN_HINTS[kind]}")
    return token


def normalize_at(value: Any) -> str:
    """Normalize default @ target (max 128 chars, no control chars)."""
    if value is None:
        return ""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("默认 @ 人的格式不正确")
    text = str(value).strip()
    if len(text) > MAX_AT:
        raise ValueError(f"默认 @ 人最多 {MAX_AT} 个字符")
    if _has_control_char(text):
        raise ValueError("默认 @ 人不能包含换行等控制字符")
    return text


def build_legacy_request(kind: str, token: str, content: Any, at: str = "") -> tuple[str, dict[str, Any]]:
    """Build legacy (url, payload) tuple matching historical master/notify/channels.py output."""
    _check_kind(kind)
    text = "" if content is None else str(content)
    clean_at = (at or "").strip()
    if kind == "wecom":
        suffix = f"<@{clean_at}>" if clean_at else ""
        body = fit_utf8_bytes(text, WECOM_MAX_BYTES - len(suffix.encode("utf-8"))) + suffix
        return _WECOM_URL + token, {"msgtype": "markdown", "markdown": {"content": body}}
    if kind == "dingding":
        payload: dict[str, Any] = {"msgtype": "markdown", "markdown": {"title": "告警", "text": text}}
        if clean_at:
            payload["at"] = {"atMobiles": [clean_at]}
        return _DINGDING_URL + token, payload
    if kind == "feishu":
        card = {
            "header": {"title": {"tag": "plain_text", "content": "告警通知"}, "template": "red"},
            "elements": [{"tag": "markdown", "content": f"{text}\n<at id={clean_at}></at>" if clean_at else text}],
        }
        return _FEISHU_URL + token, {"msg_type": "interactive", "card": card}
    return _SLACK_URL + token, {"text": f"{text} <@{clean_at}>" if clean_at else text}


def build_v2_request_from_legacy(kind: str, token: str, msg: NotificationMessage, at: str = "") -> tuple[str, dict[str, Any]]:
    """Bridge legacy channel dict + semantic NotificationMessage through v2 provider."""
    _check_kind(kind)
    provider = get_provider(kind)
    settings = {"at": at}
    if kind == "feishu":
        settings["domain_preset"] = "feishu"
    elif kind in ("wecom", "dingding"):
        settings["domain_preset"] = "public"
    cfg = ChannelConfig(id=f"legacy_{kind}", name=kind, provider=kind, settings=settings, secrets={"token": token})
    req = provider.build_http(cfg, msg)
    payload = json.loads(req.body.decode("utf-8")) if req.body else {}
    return req.url, payload


def parse_legacy_response(kind: str, status: int, body_text: str | bytes) -> tuple[bool, str]:
    """Parse IM HTTP response into legacy (ok, message) tuple."""
    _check_kind(kind)
    provider = get_provider(kind)
    result = provider.parse_http_response(status, {}, body_text)
    return result.ok, result.message


def test_content(scope_label: str, username: str, now_text: str) -> str:
    """Build standard test notification body text."""
    return f"【KubeDoor】消息通知测试（{scope_label}），操作人 {username}，{now_text}"


test_content.__test__ = False
