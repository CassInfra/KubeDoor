"""Version-2 multi-channel notification config normalization, v1 migration, and routing engine."""

from __future__ import annotations

import re
from typing import Any, Callable

from .models import ChannelConfig
from .registry import get_provider

_ENV_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")

MAX_CHANNELS = 200
MAX_ROUTES = 500

CATEGORY_OPTIONS: tuple[dict[str, str], ...] = (
    {"value": "k8s_event", "label": "K8S 事件告警"},
    {"value": "ops", "label": "运维操作通知"},
    {"value": "metric_alert", "label": "指标告警"},
    {"value": "control_table", "label": "管控表通知"},
    {"value": "test", "label": "测试消息"},
    {"value": "generic", "label": "通用通知"},
)

SEVERITY_OPTIONS: tuple[dict[str, str], ...] = (
    {"value": "critical", "label": "严重 (critical)"},
    {"value": "warning", "label": "警告 (warning)"},
    {"value": "info", "label": "信息 (info)"},
    {"value": "resolved", "label": "恢复 (resolved)"},
    {"value": "test", "label": "测试 (test)"},
)

_LEGACY_TYPES = ("wecom", "dingding", "feishu", "slack")


def _extract_channels_map(raw_channels: Any) -> dict[str, dict[str, Any]]:
    if raw_channels is None:
        return {}
    if isinstance(raw_channels, dict):
        result: dict[str, dict[str, Any]] = {}
        for cid, entry in raw_channels.items():
            if isinstance(entry, dict):
                item = dict(entry)
                item.setdefault("id", str(cid))
                result[str(cid)] = item
        return result
    if isinstance(raw_channels, list):
        result = {}
        for entry in raw_channels:
            if not isinstance(entry, dict):
                raise ValueError("通道列表项格式不正确")
            cid = str(entry.get("id") or "").strip()
            if not cid:
                raise ValueError("通道缺少 id")
            if cid in result:
                raise ValueError(f"通道 id 重复: {cid}")
            result[cid] = dict(entry)
        return result
    raise ValueError("channels 格式不正确")


def _default_settings_for_v1(kind: str, at: str) -> dict[str, Any]:
    if kind == "feishu":
        return {"domain_preset": "feishu", "at": at}
    if kind in ("wecom", "dingding"):
        return {"domain_preset": "public", "at": at}
    return {"at": at}


def migrate_v1_to_v2(v1_dict: dict[str, Any] | None) -> dict[str, Any]:
    """Upgrade legacy v1 {default, clusters} notification config into version 2 structure."""
    if not isinstance(v1_dict, dict):
        return {"version": 2, "channels": {}, "routes": [], "default_channel_ids": []}
    if v1_dict.get("version") == 2:
        channels_map = _extract_channels_map(v1_dict.get("channels"))
        normalized_channels: dict[str, dict[str, Any]] = {}
        for cid, ch in channels_map.items():
            provider = str(ch.get("provider") or ch.get("type") or "").strip()
            if not provider:
                continue
            normalized_channels[cid] = {
                "id": cid,
                "name": str(ch.get("name") or cid),
                "provider": provider,
                "enabled": bool(ch.get("enabled", True)),
                "settings": dict(ch.get("settings") or {}) if isinstance(ch.get("settings"), dict) else {},
                "secrets": dict(ch.get("secrets") or {}) if isinstance(ch.get("secrets"), dict) else {},
            }
        routes = [dict(r) for r in (v1_dict.get("routes") or []) if isinstance(r, dict)]
        default_ids = [str(cid) for cid in (v1_dict.get("default_channel_ids") or []) if str(cid) in normalized_channels]
        return {
            "version": 2,
            "channels": normalized_channels,
            "routes": routes,
            "default_channel_ids": default_ids,
        }

    channels: dict[str, dict[str, Any]] = {}
    routes: list[dict[str, Any]] = []
    default_channel_ids: list[str] = []

    default_entry = v1_dict.get("default")
    if isinstance(default_entry, dict):
        kind = str(default_entry.get("type") or default_entry.get("provider") or "").strip()
        token = str(default_entry.get("token") or "")
        at = str(default_entry.get("at") or "")
        if kind in _LEGACY_TYPES and token:
            channels["ch_default"] = {
                "id": "ch_default",
                "name": "全局默认通道",
                "provider": kind,
                "enabled": True,
                "settings": _default_settings_for_v1(kind, at),
                "secrets": {"token": token},
            }
            default_channel_ids.append("ch_default")

    raw_clusters = v1_dict.get("clusters")
    if isinstance(raw_clusters, dict):
        for env, entry in sorted(raw_clusters.items()):
            if not isinstance(entry, dict):
                continue
            env_name = str(env).strip()
            kind = str(entry.get("type") or entry.get("provider") or "").strip()
            token = str(entry.get("token") or "")
            at = str(entry.get("at") or "")
            if not env_name or kind not in _LEGACY_TYPES or not token:
                continue
            cid = f"ch_cluster_{env_name}"
            channels[cid] = {
                "id": cid,
                "name": f"集群 {env_name}",
                "provider": kind,
                "enabled": True,
                "settings": _default_settings_for_v1(kind, at),
                "secrets": {"token": token},
            }
            routes.append(
                {
                    "id": f"rt_cluster_{env_name}",
                    "name": f"集群 {env_name}",
                    "enabled": True,
                    "match": {"envs": [env_name], "categories": [], "severities": []},
                    "channel_ids": [cid],
                    "continue": False,
                }
            )

    return {
        "version": 2,
        "channels": channels,
        "routes": routes,
        "default_channel_ids": default_channel_ids,
    }


def _to_legacy_entry(ch: dict[str, Any]) -> dict[str, str] | None:
    provider = str(ch.get("provider") or ch.get("type") or "").strip()
    if provider not in _LEGACY_TYPES or not ch.get("enabled", True):
        return None
    secrets = ch.get("secrets") if isinstance(ch.get("secrets"), dict) else {}
    settings = ch.get("settings") if isinstance(ch.get("settings"), dict) else {}
    token = str(secrets.get("token") or "")
    if not token:
        return None
    return {
        "type": provider,
        "token": token,
        "at": str(settings.get("at") or ""),
    }


def project_v2_to_v1(v2_dict: dict[str, Any] | None) -> dict[str, Any]:
    """Project a version 2 config back to legacy {default?, clusters} format for v1 readers."""
    v2 = migrate_v1_to_v2(v2_dict)
    channels = v2.get("channels") or {}
    routes = v2.get("routes") or []
    default_ids = v2.get("default_channel_ids") or []

    result: dict[str, Any] = {"clusters": {}}

    # Resolve default entry from default_channel_ids or catch-all empty-match route.
    for cid in default_ids:
        entry = _to_legacy_entry(channels.get(cid) or {})
        if entry is not None:
            result["default"] = entry
            break
    if "default" not in result:
        for route in routes:
            if not route.get("enabled", True):
                continue
            match = route.get("match") or {}
            if not (match.get("envs") or match.get("categories") or match.get("severities")):
                for cid in route.get("channel_ids") or []:
                    entry = _to_legacy_entry(channels.get(cid) or {})
                    if entry is not None:
                        result["default"] = entry
                        break
            if "default" in result:
                break

    # Resolve cluster-specific routes (single env, no category/severity filter).
    for route in routes:
        if not route.get("enabled", True):
            continue
        match = route.get("match") or {}
        envs = match.get("envs") or []
        if len(envs) != 1 or match.get("categories") or match.get("severities"):
            continue
        env_name = str(envs[0]).strip()
        if not env_name or env_name in result["clusters"]:
            continue
        for cid in route.get("channel_ids") or []:
            entry = _to_legacy_entry(channels.get(cid) or {})
            if entry is not None:
                result["clusters"][env_name] = entry
                break

    return result


def _normalize_match_list(raw: Any, field_name: str, *, lower: bool = False) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"路由规则 {field_name} 必须是列表")
    out: list[str] = []
    for item in raw:
        val = str(item).strip()
        if not val:
            continue
        if lower:
            val = val.lower()
        if val not in out:
            out.append(val)
    return out


def normalize_v2_config(
    raw: dict[str, Any],
    existing: dict[str, Any] | None = None,
    *,
    secret_decryptor: Callable[[str], str] | None = None,
    secret_encryptor: Callable[[str], str] | None = None,
    allow_reuse_saved: bool = True,
) -> dict[str, Any]:
    """Validate and normalize a complete version 2 notification configuration."""
    if not isinstance(raw, dict):
        raise ValueError("配置必须是 JSON 对象")

    existing_v2 = migrate_v1_to_v2(existing) if existing is not None else {"channels": {}, "routes": [], "default_channel_ids": []}
    existing_channels = existing_v2.get("channels") or {}

    raw_channels = _extract_channels_map(raw.get("channels"))
    if len(raw_channels) > MAX_CHANNELS:
        raise ValueError(f"通知通道数量不能超过 {MAX_CHANNELS} 个")

    normalized_channels: dict[str, dict[str, Any]] = {}
    for cid, ch in raw_channels.items():
        if not _ID_PATTERN.fullmatch(cid):
            raise ValueError(f"通道 ID 不合法: {cid}")
        provider_id = str(ch.get("provider") or ch.get("type") or "").strip().lower()
        provider = get_provider(provider_id)
        name = str(ch.get("name") or cid).strip() or cid
        if len(name) > 100:
            raise ValueError(f"通道名称过长: {cid}")
        enabled = ch.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError(f"通道 {cid} 的 enabled 必须是布尔值")

        raw_settings = ch.get("settings") if isinstance(ch.get("settings"), dict) else {}
        raw_secrets = ch.get("secrets") if isinstance(ch.get("secrets"), dict) else {}

        saved_ch = existing_channels.get(cid)
        saved_plain_secrets: dict[str, str] | None = None
        saved_raw_secrets: dict[str, str] = {}
        can_reuse = allow_reuse_saved and isinstance(saved_ch, dict) and (saved_ch.get("provider") or saved_ch.get("type")) == provider.id
        if can_reuse:
            saved_raw_secrets = saved_ch.get("secrets") if isinstance(saved_ch.get("secrets"), dict) else {}
            saved_plain_secrets = {}
            for sk, sv in saved_raw_secrets.items():
                if not sv:
                    continue
                if secret_decryptor is not None:
                    try:
                        saved_plain_secrets[sk] = secret_decryptor(str(sv))
                    except Exception:
                        saved_plain_secrets[sk] = ""
                else:
                    saved_plain_secrets[sk] = str(sv)

        norm_settings, norm_secrets = provider.normalize(
            raw_settings,
            raw_secrets,
            saved_secrets=saved_plain_secrets,
            allow_reuse_saved=can_reuse,
        )

        stored_secrets: dict[str, str] = {}
        for sk, plain_val in norm_secrets.items():
            if not plain_val:
                continue
            if (
                can_reuse
                and secret_encryptor is not None
                and saved_plain_secrets is not None
                and saved_plain_secrets.get(sk) == plain_val
                and saved_raw_secrets.get(sk)
                and not str(raw_secrets.get(sk) or "").strip()
            ):
                stored_secrets[sk] = str(saved_raw_secrets[sk])
            elif secret_encryptor is not None:
                stored_secrets[sk] = secret_encryptor(plain_val)
            else:
                stored_secrets[sk] = plain_val

        normalized_channels[cid] = {
            "id": cid,
            "name": name,
            "provider": provider.id,
            "enabled": enabled,
            "settings": norm_settings,
            "secrets": stored_secrets,
        }

    raw_routes = raw.get("routes")
    if raw_routes is None:
        raw_routes = []
    if not isinstance(raw_routes, list):
        raise ValueError("routes 必须是列表")
    if len(raw_routes) > MAX_ROUTES:
        raise ValueError(f"路由规则数量不能超过 {MAX_ROUTES} 条")

    normalized_routes: list[dict[str, Any]] = []
    seen_route_ids: set[str] = set()
    for idx, route in enumerate(raw_routes):
        if not isinstance(route, dict):
            raise ValueError(f"第 {idx + 1} 条路由规则格式不正确")
        rid = str(route.get("id") or f"rt_{idx + 1}").strip()
        if not _ID_PATTERN.fullmatch(rid):
            raise ValueError(f"路由 ID 不合法: {rid}")
        if rid in seen_route_ids:
            raise ValueError(f"路由 ID 重复: {rid}")
        seen_route_ids.add(rid)

        rname = str(route.get("name") or rid).strip() or rid
        renabled = route.get("enabled", True)
        if not isinstance(renabled, bool):
            raise ValueError(f"路由 {rid} 的 enabled 必须是布尔值")

        raw_match = route.get("match") if isinstance(route.get("match"), dict) else {}
        envs = _normalize_match_list(raw_match.get("envs"), "envs")
        for env_val in envs:
            if env_val != "*" and not _ENV_PATTERN.fullmatch(env_val):
                raise ValueError(f"路由 {rid} 的集群名称不合法: {env_val}")
        categories = _normalize_match_list(raw_match.get("categories"), "categories", lower=True)
        severities = _normalize_match_list(raw_match.get("severities"), "severities", lower=True)

        raw_cids = route.get("channel_ids")
        if not isinstance(raw_cids, list) or not raw_cids:
            raise ValueError(f"路由 {rid} 至少需要指定一个目标通道")
        cids: list[str] = []
        for cid in raw_cids:
            cid_str = str(cid).strip()
            if cid_str not in normalized_channels:
                raise ValueError(f"路由 {rid} 引用了不存在的通道: {cid_str}")
            if cid_str not in cids:
                cids.append(cid_str)

        cont = route.get("continue", False)
        if not isinstance(cont, bool):
            raise ValueError(f"路由 {rid} 的 continue 必须是布尔值")

        normalized_routes.append(
            {
                "id": rid,
                "name": rname,
                "enabled": renabled,
                "match": {
                    "envs": envs,
                    "categories": categories,
                    "severities": severities,
                },
                "channel_ids": cids,
                "continue": cont,
            }
        )

    raw_defaults = raw.get("default_channel_ids")
    if raw_defaults is None:
        raw_defaults = []
    if not isinstance(raw_defaults, list):
        raise ValueError("default_channel_ids 必须是列表")
    default_channel_ids: list[str] = []
    for cid in raw_defaults:
        cid_str = str(cid).strip()
        if cid_str not in normalized_channels:
            raise ValueError(f"default_channel_ids 引用了不存在的通道: {cid_str}")
        if cid_str not in default_channel_ids:
            default_channel_ids.append(cid_str)

    return {
        "version": 2,
        "channels": normalized_channels,
        "routes": normalized_routes,
        "default_channel_ids": default_channel_ids,
    }


def _to_channel_config(cid: str, data: ChannelConfig | dict[str, Any]) -> ChannelConfig | None:
    if isinstance(data, ChannelConfig):
        return data if data.enabled else None
    if not isinstance(data, dict) or not data.get("enabled", True):
        return None
    provider_id = str(data.get("provider") or data.get("type") or "").strip()
    if not provider_id:
        return None
    return ChannelConfig(
        id=str(data.get("id") or cid),
        name=str(data.get("name") or cid),
        provider=provider_id,
        enabled=True,
        settings=dict(data.get("settings") or {}) if isinstance(data.get("settings"), dict) else {},
        secrets=dict(data.get("secrets") or {}) if isinstance(data.get("secrets"), dict) else {},
    )


def _route_matches(match: dict[str, Any], env: str, category: str, severity: str) -> bool:
    envs = [str(x).strip() for x in (match.get("envs") or []) if str(x).strip()]
    if envs and "*" not in envs and env not in envs:
        return False

    categories = [str(x).strip().lower() for x in (match.get("categories") or []) if str(x).strip()]
    if categories and "*" not in categories and category not in categories:
        return False

    severities = [str(x).strip().lower() for x in (match.get("severities") or []) if str(x).strip()]
    if severities and "*" not in severities and severity not in severities:
        return False

    return True


def match_channels(
    snapshot_or_v2: Any,
    env: str = "",
    category: str = "generic",
    severity: str = "info",
) -> list[ChannelConfig]:
    """Match ordered routes (respecting `continue` fan-out vs stop) or fall back to default_channel_ids."""
    if isinstance(snapshot_or_v2, dict):
        v2 = migrate_v1_to_v2(snapshot_or_v2)
        raw_channels = v2.get("channels") or {}
        routes = v2.get("routes") or []
        default_ids = v2.get("default_channel_ids") or []
    elif hasattr(snapshot_or_v2, "channels") and hasattr(snapshot_or_v2, "routes"):
        raw_channels = getattr(snapshot_or_v2, "channels") or {}
        routes = getattr(snapshot_or_v2, "routes") or []
        default_ids = getattr(snapshot_or_v2, "default_channel_ids", ()) or ()
    else:
        return []

    clean_env = env.strip() if isinstance(env, str) else ""
    clean_category = (category or "generic").strip().lower()
    clean_severity = (severity or "info").strip().lower()

    selected: list[ChannelConfig] = []
    seen_ids: set[str] = set()
    any_route_matched = False

    for route in routes:
        if not isinstance(route, dict) or not route.get("enabled", True):
            continue
        match = route.get("match") if isinstance(route.get("match"), dict) else {}
        if not _route_matches(match, clean_env, clean_category, clean_severity):
            continue
        any_route_matched = True
        for cid in route.get("channel_ids") or ():
            cid_str = str(cid)
            if cid_str in seen_ids or cid_str not in raw_channels:
                continue
            cfg = _to_channel_config(cid_str, raw_channels[cid_str])
            if cfg is not None:
                seen_ids.add(cid_str)
                selected.append(cfg)
        if not route.get("continue", False):
            break

    if not any_route_matched:
        for cid in default_ids:
            cid_str = str(cid)
            if cid_str in seen_ids or cid_str not in raw_channels:
                continue
            cfg = _to_channel_config(cid_str, raw_channels[cid_str])
            if cfg is not None:
                seen_ids.add(cid_str)
                selected.append(cfg)

    return selected
