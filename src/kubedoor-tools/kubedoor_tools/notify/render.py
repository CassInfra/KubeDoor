"""Clean NotificationMessage rendering from alerts and events without raw <font> HTML tags."""

from __future__ import annotations

import re
from typing import Any, Iterable

from .models import AlertItem, NotificationMessage

_FONT_TAG_RE = re.compile(r"</?font\b[^>]*>", re.IGNORECASE)
_BR_TAG_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)

_SEVERITY_ICON: dict[str, str] = {
    "critical": "🔴",
    "firing": "🔴",
    "warning": "🟠",
    "resolved": "🟢",
    "ok": "🟢",
    "info": "🔵",
    "test": "🔵",
}


def strip_legacy_html(text: object) -> str:
    """Remove legacy <font> and <br> HTML tags while preserving IM @-tag syntax."""
    raw = "" if text is None else str(text)
    cleaned = _FONT_TAG_RE.sub("", raw)
    cleaned = _BR_TAG_RE.sub("\n", cleaned)
    return cleaned


def _coerce_alert_item(entry: AlertItem | dict[str, Any]) -> AlertItem:
    if isinstance(entry, AlertItem):
        return AlertItem(
            summary=strip_legacy_html(entry.summary).strip(),
            message=strip_legacy_html(entry.message).strip(),
            status=entry.status,
            severity=entry.severity,
            alertname=entry.alertname,
            duration_text=entry.duration_text,
            silence_url=entry.silence_url,
            labels=dict(entry.labels),
            annotations=dict(entry.annotations),
            starts_at=entry.starts_at,
        )
    if not isinstance(entry, dict):
        raise ValueError("Alert entry must be AlertItem or dict")
    labels = dict(entry.get("labels") or {}) if isinstance(entry.get("labels"), dict) else {}
    annotations = dict(entry.get("annotations") or {}) if isinstance(entry.get("annotations"), dict) else {}
    status = str(entry.get("status") or labels.get("status") or "firing").strip()
    severity = str(entry.get("severity") or labels.get("severity") or "").strip().lower()
    alertname = str(entry.get("alertname") or labels.get("alertname") or "").strip()
    duration_text = str(entry.get("duration_text") or entry.get("duration") or "").strip()
    summary = strip_legacy_html(
        entry.get("summary") or entry.get("title") or (f"{alertname},{duration_text}" if alertname and duration_text else alertname)
    ).strip()
    message = strip_legacy_html(
        entry.get("message") or entry.get("description") or annotations.get("description") or ""
    ).strip()
    silence_url = str(entry.get("silence_url") or entry.get("url") or "").strip()
    starts_at = str(entry.get("starts_at") or entry.get("startsAt") or "").strip()
    return AlertItem(
        summary=summary,
        message=message,
        status=status,
        severity=severity,
        alertname=alertname,
        duration_text=duration_text,
        silence_url=silence_url,
        labels={str(k): str(v) for k, v in labels.items()},
        annotations={str(k): str(v) for k, v in annotations.items()},
        starts_at=starts_at,
    )


def _is_resolved(item: AlertItem) -> bool:
    s = item.status.strip().lower()
    return s in ("resolved", "恢复", "ok") or item.severity.strip().lower() in ("resolved", "ok")


def _overall_severity(items: tuple[AlertItem, ...]) -> str:
    if not items:
        return "info"
    firing = [item for item in items if not _is_resolved(item)]
    if not firing:
        return "resolved"
    severities = {item.severity.strip().lower() for item in firing if item.severity}
    if "critical" in severities or "error" in severities or "high" in severities:
        return "critical"
    if "warning" in severities or "warn" in severities:
        return "warning"
    if "info" in severities:
        return "info"
    return "critical"


def build_alert_message(
    alerts: Iterable[AlertItem | dict[str, Any]],
    *,
    env: str = "",
    at: str = "",
    mentions: tuple[str, ...] = (),
    mention_all: bool = False,
    category: str = "metric_alert",
    title: str = "",
    metadata: dict[str, Any] | None = None,
) -> NotificationMessage:
    """Build a clean, HTML-tag-free NotificationMessage from structured alerts."""
    items = tuple(_coerce_alert_item(entry) for entry in alerts)
    severity = _overall_severity(items)
    resolved_count = sum(1 for item in items if _is_resolved(item))
    firing_count = len(items) - resolved_count

    clean_title = strip_legacy_html(title).strip()
    if not clean_title:
        prefix = f"[{env}] " if env else ""
        if len(items) == 1:
            only = items[0]
            state_label = "恢复" if _is_resolved(only) else "故障"
            clean_title = f"{prefix}{state_label}: {only.summary}".strip()
        elif items:
            if firing_count and resolved_count:
                clean_title = f"{prefix}告警通知（{firing_count} 故障 / {resolved_count} 恢复）"
            elif firing_count:
                clean_title = f"{prefix}告警通知（{firing_count} 条故障）"
            else:
                clean_title = f"{prefix}恢复通知（{resolved_count} 条恢复）"
        else:
            clean_title = f"{prefix}通知"

    md_blocks: list[str] = []
    plain_lines: list[str] = []
    links: list[dict[str, str]] = []

    for item in items:
        resolved = _is_resolved(item)
        state_text = "恢复" if resolved else "故障"
        icon = "🟢" if resolved else _SEVERITY_ICON.get(item.severity.lower(), "🔴")
        header = f"### {icon} {state_text}: {item.summary}" if item.summary else f"### {icon} {state_text}"
        body_line = f"- {item.message}" if item.message else ""
        if not resolved and item.silence_url:
            link_md = f" [【屏蔽】]({item.silence_url})"
            body_line = f"{body_line}{link_md}" if body_line else f"-{link_md}"
            links.append({"label": f"屏蔽: {item.summary or item.alertname or '告警'}", "url": item.silence_url})
        block_parts = [header]
        if body_line:
            block_parts.append(body_line)
        md_blocks.append("\n".join(block_parts))

        p_line = f"[{state_text}] {item.summary}"
        if item.message:
            p_line += f" - {item.message}"
        if not resolved and item.silence_url:
            p_line += f" (屏蔽: {item.silence_url})"
        plain_lines.append(p_line)

    markdown = "\n\n".join(md_blocks)
    plain_text = "\n".join(plain_lines)

    return NotificationMessage(
        title=clean_title,
        markdown=markdown,
        plain_text=plain_text,
        severity=severity,
        category=category,
        env=env,
        at=at,
        mentions=tuple(mentions),
        mention_all=mention_all,
        items=items,
        links=tuple(links),
        metadata=dict(metadata or {}),
    )


def build_event_message(
    title: str,
    body: str,
    *,
    env: str = "",
    category: str = "ops",
    severity: str = "info",
    at: str = "",
    mentions: tuple[str, ...] = (),
    mention_all: bool = False,
    links: tuple[dict[str, str], ...] = (),
    metadata: dict[str, Any] | None = None,
) -> NotificationMessage:
    """Build a clean NotificationMessage for K8S events, ops notices, or tests."""
    clean_title = strip_legacy_html(title).strip() or "通知"
    clean_body = strip_legacy_html(body).strip()
    return NotificationMessage(
        title=clean_title,
        markdown=clean_body,
        plain_text=clean_body,
        severity=severity,
        category=category,
        env=env,
        at=at,
        mentions=tuple(mentions),
        mention_all=mention_all,
        links=tuple(dict(link) for link in links),
        metadata=dict(metadata or {}),
    )
