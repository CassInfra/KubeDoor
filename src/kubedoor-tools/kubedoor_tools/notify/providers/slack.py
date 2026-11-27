"""Slack Incoming Webhook provider using Block Kit payloads."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from ..models import ChannelConfig, DeliveryResult, FieldSpec, HttpPreparedRequest, NotificationMessage, RateLimit
from ..registry import NotificationProvider, detect_provider_from_url, register_provider
from ._common import (
    OK_MESSAGE,
    TOKEN_PART_RE,
    clean_str,
    clip_text,
    decode_body,
    format_failure,
    has_control_char,
    json_http_request,
    make_delivery_result,
    normalize_at_value,
    parse_retry_after,
    resolve_mentions,
    resolve_secret_value,
)

SLACK_BASE = "https://hooks.slack.com"
SLACK_SERVICES_PATH = "/services/"
SLACK_HINT = "Incoming Webhook 地址(https://hooks.slack.com/services/T…/B…/…),或 /services/ 后面那一段"


def _valid_slack_token(token: str) -> bool:
    parts = token.split("/")
    return len(parts) == 3 and all(TOKEN_PART_RE.fullmatch(part) for part in parts)


def _format_slack_mentions(mentions: list[str], mention_all: bool) -> str:
    parts: list[str] = []
    if mention_all:
        parts.append("<!channel>")
    for target in mentions:
        low = target.lower()
        if low in ("channel", "here", "everyone"):
            parts.append(f"<!{low}>")
        else:
            parts.append(f"<@{target}>")
    return " ".join(parts)


@register_provider
class SlackProvider(NotificationProvider):
    """Slack Incoming Webhook provider rendering structured Block Kit cards."""

    id = "slack"
    label = "Slack"
    description = "Slack Incoming Webhook（Block Kit 结构化卡片、429 Retry-After 感知与三段式 Token 脱敏）"
    transport = "http"
    supports_at = True
    supports_markdown = True
    at_help = "Slack 成员 ID（例如 U012AB3CD），或填 all/channel/here"
    default_rate_limit = RateLimit(limit=60, window_seconds=60.0, burst_limit=1, burst_window_seconds=1.0)
    fields = (
        FieldSpec(
            key="token",
            label="Webhook 地址 / Token",
            kind="secret",
            required=True,
            secret=True,
            placeholder="粘贴 Incoming Webhook 地址，或 /services/ 后面的三段 Token",
            help="Incoming Webhook 地址，形如 https://hooks.slack.com/services/T000/B000/xxx",
        ),
        FieldSpec(
            key="at",
            label="默认 @ 成员",
            kind="text",
            required=False,
            max_length=128,
            placeholder="Slack 成员 ID（例如 U012AB3CD），可以不填",
        ),
    )

    def match_url(self, url: str) -> bool:
        try:
            parts = urlsplit(url)
        except ValueError:
            return False
        return (parts.hostname or "").lower() == "hooks.slack.com"

    def normalize(
        self,
        raw_settings: dict[str, Any] | None,
        raw_secrets: dict[str, Any] | None,
        saved_secrets: dict[str, str] | None = None,
        *,
        allow_reuse_saved: bool = True,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        settings = raw_settings or {}
        at = normalize_at_value(settings.get("at"))

        raw_token = resolve_secret_value(
            "token",
            raw_secrets,
            saved_secrets,
            required=True,
            missing_error="请填写 Slack 机器人地址",
            allow_reuse_saved=allow_reuse_saved,
        )

        token = raw_token
        lowered = raw_token.lower()
        if not lowered.startswith(("http://", "https://")) and lowered.split("/", 1)[0].split("?", 1)[0] == "hooks.slack.com":
            raw_token = "https://" + raw_token
            lowered = raw_token.lower()

        if lowered.startswith(("http://", "https://")) or "://" in raw_token:
            detected = detect_provider_from_url(raw_token)
            if detected and detected != self.id:
                other_label = {"wecom": "企业微信", "dingding": "钉钉", "feishu": "飞书"}.get(detected, detected)
                raise ValueError(f"这是{other_label}的机器人地址,和选择的 IM 类型(Slack)不一致")
            try:
                parts = urlsplit(raw_token)
                host = (parts.hostname or "").lower()
            except ValueError:
                raise ValueError(f"不认识的机器人地址,Slack 请填写{SLACK_HINT}") from None

            if parts.scheme.lower() not in ("http", "https") or host != "hooks.slack.com" or not parts.path.startswith(SLACK_SERVICES_PATH):
                raise ValueError(f"不认识的机器人地址,Slack 请填写{SLACK_HINT}")

            token = parts.path[len(SLACK_SERVICES_PATH) :].strip("/")
            if not token:
                raise ValueError(f"机器人地址里没有 token,Slack 请填写{SLACK_HINT}")

        if has_control_char(token) or not _valid_slack_token(token):
            raise ValueError(f"机器人 token 格式不对,Slack 请填写{SLACK_HINT}")

        return {"at": at}, {"token": token}

    def build_http(self, cfg: ChannelConfig, msg: NotificationMessage) -> HttpPreparedRequest:
        token = clean_str(cfg.secrets.get("token"))
        if not token:
            raise ValueError("Slack Webhook token 未配置")

        title = clean_str(msg.title) or "通知"
        mentions, mention_all = resolve_mentions(cfg, msg)
        mention_text = _format_slack_mentions(mentions, mention_all)

        blocks: list[dict[str, Any]] = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": clip_text(title, 150), "emoji": True},
            }
        ]

        if msg.items:
            for item in msg.items:
                is_resolved = item.status.strip().lower() in ("resolved", "恢复", "ok") or item.severity.strip().lower() in ("resolved", "ok")
                icon = "🎉" if is_resolved else "💥"
                state_label = "恢复" if is_resolved else "故障"
                lines = [f"{icon} *{state_label}: {item.summary}*"]
                if item.message:
                    lines.append(item.message)
                if not is_resolved and item.silence_url:
                    lines.append(f"<{item.silence_url}|【屏蔽】>")
                blocks.append(
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": clip_text("\n".join(lines), 3000)},
                    }
                )
                blocks.append({"type": "divider"})
            if mention_text:
                blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": mention_text}})
            fallback_body = msg.plain_text or msg.markdown or title
        else:
            body_text = msg.markdown or msg.plain_text or title
            if mention_text:
                body_text = f"{body_text}\n{mention_text}" if body_text else mention_text
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": clip_text(body_text, 3000)},
                }
            )
            if msg.links:
                link_parts = [f"<{link['url']}|{link.get('label') or '查看'}>" for link in msg.links if link.get("url")]
                if link_parts:
                    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "  ".join(link_parts)}})
            fallback_body = body_text

        fallback = f"{title}\n{fallback_body}" if fallback_body and fallback_body != title else title
        if mention_text and mention_text not in fallback:
            fallback = f"{fallback} {mention_text}"

        url = f"{SLACK_BASE}{SLACK_SERVICES_PATH}{token}"
        payload = {"text": clip_text(fallback, 3000), "blocks": blocks}
        return json_http_request("POST", url, payload)

    def parse_http_response(
        self,
        status_code: int,
        headers: Mapping[str, str],
        body_text: str | bytes,
    ) -> DeliveryResult:
        text = decode_body(body_text)
        retry_after = parse_retry_after(headers)
        if status_code == 200 and text.strip() == "ok":
            return make_delivery_result(True, OK_MESSAGE, status_code=status_code, provider_code="ok")
        retryable = status_code == 429 or status_code >= 500
        return make_delivery_result(
            False,
            format_failure(status_code, None, None, text),
            status_code=status_code,
            retryable=retryable,
            retry_after=retry_after,
        )

    def redact_values(self, cfg: ChannelConfig) -> tuple[str, ...]:
        token = clean_str(cfg.secrets.get("token"))
        values: list[str] = []
        if token:
            values.append(token)
            for part in token.split("/"):
                if len(part) >= 6:
                    values.append(part)
        return tuple(values)
