"""Multi-IM bot manager, idempotent legacy migration, CRUD APIs, and legacy WeCom bridge."""
from __future__ import annotations

import asyncio
import logging
import secrets
import uuid
from contextlib import suppress
from types import MappingProxyType
from typing import Any

import asyncpg

from ..domain import AIError, CLUSTER_PERMISSIONS, Identity, Provider, ROLES, parse_model_limits, parse_reasoning_effort
from ..wecom import (
    BIND_CODE_MINUTES,
    DEFAULT_IDLE_MINUTES,
    PURGE_BATCH,
    PURGE_FIRST_DELAY,
    PURGE_INTERVAL,
    UNBIND_COLUMNS,
    _legacy_retention_days,
    MAX_RETENTION_DAYS,
)
from .adapters.feishu import FeishuAdapter, resolve_feishu_domain
from .adapters.wecom import WeComAdapter
from .engine import DEAD_IM_SESSION, IMBotEngine, LEGACY_WECOM_BOT_ID, purge_dead_im_checkpoints
from .models import (
    FEISHU_DOMAIN_PRESETS,
    GROUP_POLICIES,
    PROVIDER_KEYS,
    SUPPORTED_PLATFORMS,
    BotConfig,
)
from .shared_model import SharedModelManager

LOGGER = logging.getLogger(__name__)


def _bot_aad(bot_id: str) -> str:
    return f"im:{bot_id}"


def _clean_chat_ids(raw: Any) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise AIError("allowed_chat_ids 必须为字符串数组")
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            raise AIError("群 ID 必须为字符串")
        val = item.strip()
        if not val:
            continue
        if len(val) > 256:
            raise AIError("群 ID 长度不能超过 256 个字符")
        if val not in seen:
            seen.add(val)
            cleaned.append(val)
    if len(cleaned) > 500:
        raise AIError("群白名单最多 500 个群")
    return cleaned


def _normalize_model_override(raw: Any, current: dict[str, Any] | None = None) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise AIError("model_override 必须为对象或 null")
    if not any(raw.get(k) for k in ("base_url", "model", "api_key")) and not raw.get("enabled", True):
        return None
    if not raw.get("base_url") and not raw.get("model"):
        return None
    cur = current or {}
    if raw.get("clear_api_key") is True:
        api_key = ""
    elif raw.get("api_key"):
        api_key = raw["api_key"]
    else:
        api_key = cur.get("api_key", "")
    model_cfg: dict[str, Any] = {
        "base_url": raw.get("base_url", cur.get("base_url", "")),
        "api_key": api_key,
        "model": raw.get("model", cur.get("model", "")),
    }
    for key in ("max_input_tokens", "max_output_tokens", "compression_ratio", "reasoning_effort"):
        model_cfg[key] = raw.get(key, cur.get(key))
    Provider.parse(model_cfg)
    return model_cfg


def _public_model_override(override: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(override, dict) or not (override.get("base_url") or override.get("model")):
        return None
    return {
        "base_url": override.get("base_url") or "",
        "model": override.get("model") or "",
        "max_input_tokens": override.get("max_input_tokens"),
        "max_output_tokens": override.get("max_output_tokens"),
        "compression_ratio": override.get("compression_ratio"),
        "reasoning_effort": override.get("reasoning_effort"),
        "api_key_set": bool(override.get("api_key")),
    }


def _public_credentials(platform: str, creds: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    if platform == "wecom":
        return {"bot_id": creds.get("bot_id", "")}, bool(creds.get("bot_secret"))
    if platform == "feishu":
        return {
            "app_id": creds.get("app_id", ""),
            "domain_preset": creds.get("domain_preset") or "feishu",
            "custom_domain": creds.get("custom_domain") or "",
        }, bool(creds.get("app_secret"))
    return {}, False


def _merge_credentials(platform: str, raw: dict[str, Any], current: dict[str, Any] | None = None) -> dict[str, Any]:
    cur = current or {}
    creds_in = raw.get("credentials") if isinstance(raw.get("credentials"), dict) else raw
    if platform == "wecom":
        bot_id = creds_in.get("bot_id", cur.get("bot_id", ""))
        bot_secret = creds_in.get("bot_secret") or cur.get("bot_secret", "")
        if not isinstance(bot_id, str) or not isinstance(bot_secret, str):
            raise AIError("企微凭证格式无效")
        return {"bot_id": bot_id.strip(), "bot_secret": bot_secret.strip()}
    if platform == "feishu":
        app_id = creds_in.get("app_id", cur.get("app_id", ""))
        app_secret = creds_in.get("app_secret") or cur.get("app_secret", "")
        preset = (creds_in.get("domain_preset", cur.get("domain_preset", "feishu")) or "feishu").strip().lower()
        if preset not in FEISHU_DOMAIN_PRESETS:
            raise AIError("飞书域名预设只能是 feishu、lark 或 custom")
        custom_domain = creds_in.get("custom_domain", cur.get("custom_domain", "")) or ""
        if not isinstance(app_id, str) or not isinstance(app_secret, str) or not isinstance(custom_domain, str):
            raise AIError("飞书凭证格式无效")
        merged = {
            "app_id": app_id.strip(),
            "app_secret": app_secret.strip(),
            "domain_preset": preset,
            "custom_domain": custom_domain.strip(),
        }
        if preset == "custom" and (merged["custom_domain"] or raw.get("enabled") is True):
            merged["custom_domain"] = resolve_feishu_domain(merged)
        return merged
    raise AIError(f"暂不支持平台 {platform}")


class IMBotManager:
    """Lifecycle, migration, and admin/self-binding coordinator for multi-IM bots."""

    def __init__(self, service: Any, shared_model: SharedModelManager | None = None) -> None:
        self.service = service
        self.shared_model = shared_model or SharedModelManager(service, wire_service=False)
        self.bots: dict[str, BotConfig] = {}
        self.adapters: dict[str, Any] = {}
        self.engines: dict[str, IMBotEngine] = {}
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._retention: int | None = None

    def _spawn(self, coroutine: Any, name: str) -> asyncio.Task[Any]:
        task = asyncio.create_task(coroutine, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def start(self) -> None:
        await self.migrate_legacy_wecom()
        await self.reload_all()
        self._spawn(self._purge_loop(), "im-checkpoint-retention")

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        async with self._lock:
            for bot_id in list(self.engines):
                await self._stop_bot_unlocked(bot_id)

    async def retention_days(self) -> int:
        try:
            days = (await self.service.gateway.master_settings()).get("retention_ai_checkpoint_days")
            if type(days) is not int or not 0 <= days <= MAX_RETENTION_DAYS:
                raise ValueError("retention_ai_checkpoint_days 无效")
        except Exception as exc:
            days = self._retention if self._retention is not None else _legacy_retention_days()
            LOGGER.warning("从 master 获取检查点保留天数失败(%s),本轮按 %s 天处理", type(exc).__name__, days)
            return days
        self._retention = days
        return days

    async def _purge_loop(self) -> None:
        await asyncio.sleep(PURGE_FIRST_DELAY)
        while True:
            try:
                days = await self.retention_days()
                while await purge_dead_im_checkpoints(self.service, days) == PURGE_BATCH:
                    pass
            except Exception as exc:
                LOGGER.warning("IM 旧对话检查点清理失败: %s", type(exc).__name__)
            await asyncio.sleep(PURGE_INTERVAL)

    async def migrate_legacy_wecom(self) -> None:
        """Idempotently migrate legacy `kubedoor_ai_wecom_*` data into `kubedoor_ai_im_*`."""
        store = getattr(self.service, "store", None)
        gateway = getattr(self.service, "gateway", None)
        if not store or not gateway or not getattr(gateway, "cipher", None):
            return
        try:
            count_row = await store.one("SELECT count(*) AS n FROM kubedoor_ai_im_bots")
            if count_row and (count_row.get("n") or 0) > 0:
                return
            legacy = await store.one(
                "SELECT enabled,encrypted_config,unbound_username,unbound_default_env,session_idle_minutes,updated_by "
                "FROM kubedoor_ai_wecom_config WHERE singleton=true"
            )
            if not legacy:
                return
            decrypted = gateway.cipher.decrypt("wecom", bytes(legacy["encrypted_config"]))
            await self.shared_model.load()

            payload = {
                "credentials": {
                    "bot_id": decrypted.get("bot_id", ""),
                    "bot_secret": decrypted.get("bot_secret", ""),
                },
                "model_override": None,
            }
            encrypted = gateway.cipher.encrypt(_bot_aad(LEGACY_WECOM_BOT_ID), payload)
            await store.execute(
                "INSERT INTO kubedoor_ai_im_bots"
                "(id,platform,name,enabled,encrypted_config,unbound_username,unbound_default_env,"
                "session_idle_minutes,group_policy,allowed_chat_ids,updated_by) "
                "VALUES($1::uuid,'wecom','默认企业微信机器人',$2,$3,$4,$5,$6,'disabled','[]'::jsonb,$7) "
                "ON CONFLICT(id) DO NOTHING",
                LEGACY_WECOM_BOT_ID,
                bool(legacy.get("enabled")),
                encrypted,
                legacy.get("unbound_username"),
                legacy.get("unbound_default_env"),
                legacy.get("session_idle_minutes") if legacy.get("session_idle_minutes") is not None else DEFAULT_IDLE_MINUTES,
                legacy.get("updated_by") or "system",
            )
            await store.execute(
                "INSERT INTO kubedoor_ai_im_users"
                "(bot_id,external_user_id,platform,username,default_env,current_env,session_id,previous_session_id,"
                "remark,self_name,bound_by,bound_at,pending_text,pending_at,bind_failures,bind_failed_at,"
                "first_seen_at,last_seen_at,updated_at) "
                "SELECT $1::uuid,wecom_userid,'wecom',username,default_env,current_env,session_id,previous_session_id,"
                "remark,self_name,bound_by,bound_at,pending_text,pending_at,bind_failures,bind_failed_at,"
                "first_seen_at,last_seen_at,updated_at FROM kubedoor_ai_wecom_users "
                "ON CONFLICT(bot_id,external_user_id) DO NOTHING",
                LEGACY_WECOM_BOT_ID,
            )
            await store.execute(
                "INSERT INTO kubedoor_ai_im_bind_codes(code,username,expires_at,created_at) "
                "SELECT code,username,expires_at,created_at FROM kubedoor_ai_wecom_bind_codes "
                "WHERE expires_at>now() ON CONFLICT DO NOTHING"
            )
            await store.execute(
                "INSERT INTO kubedoor_ai_im_approvals"
                "(bot_id,card_ref,action_id,run_id,external_user_id,username,session_id,resume_after_seq,expires_at,created_at) "
                "SELECT $1::uuid,task_id,action_id,run_id,wecom_userid,username,session_id,resume_after_seq,expires_at,created_at "
                "FROM kubedoor_ai_wecom_approvals WHERE expires_at>now() ON CONFLICT DO NOTHING",
                LEGACY_WECOM_BOT_ID,
            )
        except Exception as exc:
            LOGGER.warning("迁移旧企微配置到通用 IM 表失败: %s", type(exc).__name__)

    def _decode_row(self, row: dict[str, Any]) -> BotConfig:
        bot_id = str(row["id"])
        raw_enc = self.service.gateway.cipher.decrypt(_bot_aad(bot_id), bytes(row["encrypted_config"]))
        creds = raw_enc.get("credentials") if isinstance(raw_enc.get("credentials"), dict) else raw_enc
        model_override = raw_enc.get("model_override")
        allowed = row.get("allowed_chat_ids")
        if isinstance(allowed, str):
            import json
            allowed = json.loads(allowed)
        return BotConfig(
            id=bot_id,
            platform=row["platform"],
            name=row["name"],
            enabled=bool(row["enabled"]),
            credentials=dict(creds or {}),
            unbound_username=row.get("unbound_username"),
            unbound_default_env=row.get("unbound_default_env"),
            session_idle_minutes=row.get("session_idle_minutes") if row.get("session_idle_minutes") is not None else DEFAULT_IDLE_MINUTES,
            group_policy=row.get("group_policy") or "disabled",
            allowed_chat_ids=list(allowed or []),
            model_override=model_override if isinstance(model_override, dict) else None,
            updated_by=row.get("updated_by") or "",
            updated_at=row.get("updated_at"),
        )

    @staticmethod
    def _make_adapter(bot: BotConfig) -> Any:
        if bot.platform == "wecom":
            return WeComAdapter(bot.id)
        if bot.platform == "feishu":
            return FeishuAdapter(bot.id)
        raise AIError(f"不支持的 IM 平台：{bot.platform}")

    async def reload_all(self) -> None:
        rows = await self.service.store.fetch("SELECT * FROM kubedoor_ai_im_bots ORDER BY updated_at DESC, id")
        async with self._lock:
            for bot_id in list(self.engines):
                await self._stop_bot_unlocked(bot_id)
            for row in rows:
                try:
                    bot = self._decode_row(row)
                except Exception as exc:
                    LOGGER.warning("IM 机器人配置解密失败 [%s]: %s", row.get("id"), type(exc).__name__)
                    continue
                self.bots[bot.id] = bot
                await self._apply_bot_unlocked(bot)

    async def _stop_bot_unlocked(self, bot_id: str) -> None:
        engine = self.engines.pop(bot_id, None)
        adapter = self.adapters.pop(bot_id, None)
        if engine is not None:
            with suppress(Exception):
                await engine.close()
        if adapter is not None:
            with suppress(Exception):
                await adapter.stop()

    async def _apply_bot_unlocked(self, bot: BotConfig) -> None:
        await self._stop_bot_unlocked(bot.id)
        self.bots[bot.id] = bot
        if bot.platform not in SUPPORTED_PLATFORMS:
            return
        adapter = self._make_adapter(bot)
        engine = IMBotEngine(self.service, bot, adapter, shared_model=self.shared_model)
        self.adapters[bot.id] = adapter
        self.engines[bot.id] = engine
        if bot.enabled:
            await adapter.start(
                {
                    "enabled": bot.enabled,
                    "credentials": bot.credentials,
                },
                engine.handle_message,
                engine.handle_card_action,
            )

    async def _bot_dto(self, bot: BotConfig) -> dict[str, Any]:
        adapter = self.adapters.get(bot.id)
        status = (
            await adapter.status()
            if adapter is not None
            else {"configured": False, "connected": False, "authenticated": False, "reconnecting": False, "error": None}
        )
        creds, secret_set = _public_credentials(bot.platform, bot.credentials)
        return {
            "id": bot.id,
            "platform": bot.platform,
            "name": bot.name,
            "enabled": bot.enabled,
            "configured": bool(status.get("configured") or (secret_set and any(creds.values()))),
            "connected": bool(status.get("connected")),
            "authenticated": bool(status.get("authenticated")),
            "reconnecting": bool(status.get("reconnecting")),
            "error": status.get("error"),
            "credentials": creds,
            "secret_set": secret_set,
            "session_idle_minutes": bot.session_idle_minutes,
            "group_policy": bot.group_policy,
            "allowed_chat_ids": list(bot.allowed_chat_ids),
            "model_override": _public_model_override(bot.model_override),
            "unbound_username": bot.unbound_username,
            "unbound_default_env": bot.unbound_default_env,
            "updated_by": bot.updated_by,
            "updated_at": bot.updated_at,
        }

    async def list_bots(self) -> list[dict[str, Any]]:
        rows = await self.service.store.fetch("SELECT * FROM kubedoor_ai_im_bots ORDER BY updated_at DESC, id")
        result = []
        for row in rows:
            bot = self._decode_row(row)
            result.append(await self._bot_dto(bot))
        return result

    async def get_bot(self, bot_id: str) -> dict[str, Any]:
        row = await self.service.store.one("SELECT * FROM kubedoor_ai_im_bots WHERE id=$1::uuid", bot_id)
        if not row:
            raise AIError("IM 机器人不存在", 404, "not_found")
        return await self._bot_dto(self._decode_row(row))

    async def _identity(self, username: str) -> Identity:
        hint = Identity(username, "none")
        try:
            async with asyncio.timeout(10):
                raw = await self.service.gateway.master(hint, "GET", "/api/ai/internal/identity")
        except AIError as exc:
            if exc.status == 403:
                raise AIError(f"花折账号 {username} 不存在、已停用或不是 Web 账号", 403, "identity_invalid") from None
            raise
        permission = raw.get("permission")
        clusters = raw.get("clusters")
        if (
            raw.get("username") != username
            or permission not in ROLES
            or not isinstance(clusters, dict)
            or any(not isinstance(k, str) or v not in CLUSTER_PERMISSIONS for k, v in clusters.items())
        ):
            raise AIError("花折用户身份无效", 403, "identity_invalid")
        return Identity(username, permission, MappingProxyType({} if permission == "admin" else clusters))

    async def _validate_unbound(self, username: Any, env: Any) -> tuple[str | None, str | None]:
        if username is not None and not isinstance(username, str):
            raise AIError("请选择花折账号")
        if env is not None and not isinstance(env, str):
            raise AIError("默认集群无效")
        uname = (username or "").strip() or None
        default_env = ((env or "").strip() or None) if uname else None
        if uname:
            live = await self._identity(uname)
            if live.permission in ("admin", "cluster_admin"):
                raise AIError("不能用系统管理员或集群管理员账号作为未绑定成员的默认权限")
            visible = {item["env"] for item in (await self.service.bootstrap(live))["clusters"]}
            if not visible:
                raise AIError("该花折账号没有任何 K8S 集群访问权限", 403, "permission_denied")
            if default_env is not None and default_env not in visible:
                raise AIError("默认集群不存在或该账号无权访问", 403, "permission_denied")
        return uname, default_env

    def _validate_bot_fields(
        self,
        platform: str,
        raw: dict[str, Any],
        current: BotConfig | None = None,
    ) -> tuple[str, bool, dict[str, Any], int, str, list[str], dict[str, Any] | None]:
        if platform not in SUPPORTED_PLATFORMS:
            raise AIError(f"暂不支持的 IM 平台：{platform}")
        name = raw.get("name", current.name if current else "")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            raise AIError("机器人名称长度必须为 1–80 个字符")
        name = name.strip()

        enabled = raw.get("enabled", current.enabled if current else False)
        if type(enabled) is not bool:
            raise AIError("enabled 必须为布尔值")

        idle = raw.get("session_idle_minutes", current.session_idle_minutes if current else DEFAULT_IDLE_MINUTES)
        if idle is not None and (type(idle) is not int or not 0 <= idle <= 10080):
            raise AIError("对话空闲超时必须为 0–10080 之间的整数（分钟），0 表示关闭")
        idle_val = DEFAULT_IDLE_MINUTES if idle is None else idle

        group_policy = raw.get("group_policy", current.group_policy if current else "disabled")
        if group_policy not in GROUP_POLICIES:
            raise AIError("group_policy 只能为 disabled、mention 或 allowlist")

        allowed_chat_ids = _clean_chat_ids(raw.get("allowed_chat_ids", current.allowed_chat_ids if current else []))
        creds = _merge_credentials(platform, raw, current.credentials if current else None)

        if enabled:
            if platform == "wecom" and (not creds.get("bot_id") or not creds.get("bot_secret")):
                raise AIError("启用企业微信机器人需要 Bot ID 和 Secret")
            if platform == "feishu" and (not creds.get("app_id") or not creds.get("app_secret")):
                raise AIError("启用飞书机器人需要 App ID 和 App Secret")
            if platform == "feishu" and creds.get("domain_preset") == "custom" and not creds.get("custom_domain"):
                raise AIError("飞书私有化域名不能为空")

        if "model_override" in raw:
            model_override = _normalize_model_override(raw.get("model_override"), current.model_override if current else None)
        else:
            model_override = current.model_override if current else None

        return name, enabled, creds, idle_val, group_policy, allowed_chat_ids, model_override

    async def create_bot(self, identity: Identity, raw: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise AIError("配置格式无效")
        platform = raw.get("platform")
        if not isinstance(platform, str):
            raise AIError("请指定 IM 平台")
        name, enabled, creds, idle, group_policy, allowed_chat_ids, model_override = self._validate_bot_fields(
            platform, raw, current=None
        )
        unbound_username, unbound_default_env = (None, None)
        if "unbound_username" in raw or "unbound_default_env" in raw:
            unbound_username, unbound_default_env = await self._validate_unbound(
                raw.get("unbound_username"), raw.get("unbound_default_env")
            )
        bot_id = str(uuid.uuid4())
        encrypted = self.service.gateway.cipher.encrypt(
            _bot_aad(bot_id), {"credentials": creds, "model_override": model_override}
        )
        row = await self.service.store.one(
            "INSERT INTO kubedoor_ai_im_bots"
            "(id,platform,name,enabled,encrypted_config,unbound_username,unbound_default_env,"
            "session_idle_minutes,group_policy,allowed_chat_ids,updated_by) "
            "VALUES($1::uuid,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) RETURNING *",
            bot_id,
            platform,
            name,
            enabled,
            encrypted,
            unbound_username,
            unbound_default_env,
            idle,
            group_policy,
            allowed_chat_ids,
            identity.username,
        )
        bot = self._decode_row(row)
        async with self._lock:
            await self._apply_bot_unlocked(bot)
        return await self._bot_dto(bot)

    async def update_bot(self, identity: Identity, bot_id: str, raw: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise AIError("配置格式无效")
        existing = await self.service.store.one("SELECT * FROM kubedoor_ai_im_bots WHERE id=$1::uuid", bot_id)
        if not existing:
            raise AIError("IM 机器人不存在", 404, "not_found")
        current = self._decode_row(existing)
        platform = raw.get("platform", current.platform)
        if platform != current.platform:
            raise AIError("不支持修改已创建机器人的平台类型")

        name, enabled, creds, idle, group_policy, allowed_chat_ids, model_override = self._validate_bot_fields(
            platform, raw, current=current
        )
        unbound_username, unbound_default_env = current.unbound_username, current.unbound_default_env
        if "unbound_username" in raw or "unbound_default_env" in raw:
            unbound_username, unbound_default_env = await self._validate_unbound(
                raw.get("unbound_username", current.unbound_username),
                raw.get("unbound_default_env", current.unbound_default_env),
            )
        encrypted = self.service.gateway.cipher.encrypt(
            _bot_aad(bot_id), {"credentials": creds, "model_override": model_override}
        )
        row = await self.service.store.one(
            "UPDATE kubedoor_ai_im_bots SET name=$2,enabled=$3,encrypted_config=$4,"
            "unbound_username=$5,unbound_default_env=$6,session_idle_minutes=$7,"
            "group_policy=$8,allowed_chat_ids=$9,updated_by=$10,updated_at=now() "
            "WHERE id=$1::uuid RETURNING *",
            bot_id,
            name,
            enabled,
            encrypted,
            unbound_username,
            unbound_default_env,
            idle,
            group_policy,
            allowed_chat_ids,
            identity.username,
        )
        bot = self._decode_row(row)
        async with self._lock:
            await self._apply_bot_unlocked(bot)
        return await self._bot_dto(bot)

    async def delete_bot(self, bot_id: str) -> dict[str, Any]:
        async with self._lock:
            await self._stop_bot_unlocked(bot_id)
            self.bots.pop(bot_id, None)
        row = await self.service.store.one(
            "DELETE FROM kubedoor_ai_im_bots WHERE id=$1::uuid RETURNING id",
            bot_id,
        )
        if not row:
            raise AIError("IM 机器人不存在", 404, "not_found")
        return {"success": True}

    async def save_unbound(self, identity: Identity, bot_id: str, raw: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise AIError("请求格式无效")
        existing = await self.service.store.one("SELECT id FROM kubedoor_ai_im_bots WHERE id=$1::uuid", bot_id)
        if not existing:
            raise AIError("IM 机器人不存在", 404, "not_found")
        username, env = await self._validate_unbound(
            raw.get("username", raw.get("unbound_username")),
            raw.get("default_env", raw.get("unbound_default_env")),
        )
        await self.service.store.execute(
            "UPDATE kubedoor_ai_im_bots SET unbound_username=$2,unbound_default_env=$3,updated_by=$4,updated_at=now() "
            "WHERE id=$1::uuid",
            bot_id,
            username,
            env,
            identity.username,
        )
        if bot_id in self.bots:
            self.bots[bot_id].unbound_username = username
            self.bots[bot_id].unbound_default_env = env
        return {"unbound_username": username, "unbound_default_env": env}

    async def list_users(self, bot_id: str | None = None) -> list[dict[str, Any]]:
        where = "WHERE u.bot_id=$1::uuid " if bot_id else ""
        args = (bot_id,) if bot_id else ()
        return await self.service.store.fetch(
            "SELECT u.bot_id,b.name AS bot_name,u.platform,u.external_user_id,u.username,u.default_env,u.current_env,"
            "u.remark,u.self_name,u.bound_by,u.bound_at,u.first_seen_at,u.last_seen_at,"
            "(SELECT count(*) FROM kubedoor_ai_im_approvals a "
            "WHERE a.bot_id=u.bot_id AND a.external_user_id=u.external_user_id AND a.expires_at>now()) AS pending_approvals "
            f"FROM kubedoor_ai_im_users u JOIN kubedoor_ai_im_bots b ON b.id=u.bot_id {where}"
            "ORDER BY u.last_seen_at DESC",
            *args,
        )

    async def save_user(self, identity: Identity, raw: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise AIError("请求格式无效")
        bot_id = raw.get("bot_id")
        userid = raw.get("external_user_id") or raw.get("userid")
        username = raw.get("username")
        if not isinstance(bot_id, str) or not bot_id.strip():
            raise AIError("请指定 IM 机器人")
        if not isinstance(userid, str) or not userid.strip() or len(userid) > 256:
            raise AIError("IM 用户 ID 无效")
        if not isinstance(username, str) or not username.strip():
            raise AIError("请选择花折用户")
        bot_row = await self.service.store.one("SELECT id,platform FROM kubedoor_ai_im_bots WHERE id=$1::uuid", bot_id.strip())
        if not bot_row:
            raise AIError("IM 机器人不存在", 404, "not_found")
        username = username.strip()
        live = await self._identity(username)
        visible_clusters = (await self.service.bootstrap(live))["clusters"]
        if not visible_clusters:
            raise AIError("该花折用户没有任何 K8S 访问权限", 403, "permission_denied")
        env = raw.get("default_env")
        if env is not None and (not isinstance(env, str) or env not in {item["env"] for item in visible_clusters}):
            raise AIError("默认集群不存在或用户无权访问", 403, "permission_denied")

        changed = "kubedoor_ai_im_users.username IS DISTINCT FROM EXCLUDED.username"
        await self.service.store.execute(
            "INSERT INTO kubedoor_ai_im_users(bot_id,external_user_id,platform,username,default_env,bound_by,bound_at) "
            "VALUES($1::uuid,$2,$3,$4,$5,$6,now()) "
            "ON CONFLICT(bot_id,external_user_id) DO UPDATE SET username=EXCLUDED.username,default_env=EXCLUDED.default_env,"
            f"bound_by=CASE WHEN {changed} THEN EXCLUDED.bound_by ELSE kubedoor_ai_im_users.bound_by END,"
            f"bound_at=CASE WHEN {changed} THEN now() ELSE kubedoor_ai_im_users.bound_at END,"
            f"current_env=CASE WHEN {changed} THEN NULL ELSE kubedoor_ai_im_users.current_env END,"
            f"pending_text=CASE WHEN {changed} THEN NULL ELSE kubedoor_ai_im_users.pending_text END,"
            f"pending_at=CASE WHEN {changed} THEN NULL ELSE kubedoor_ai_im_users.pending_at END,"
            "updated_at=now()",
            bot_id.strip(),
            userid.strip(),
            bot_row["platform"],
            username,
            env,
            identity.username,
        )
        return {"success": True}

    async def delete_user(self, bot_id: str, userid: str) -> dict[str, Any]:
        await self.service.store.execute(
            f"UPDATE kubedoor_ai_im_users SET {UNBIND_COLUMNS},updated_at=now() WHERE bot_id=$1::uuid AND external_user_id=$2",
            bot_id,
            userid,
        )
        return {"success": True}

    async def save_remark(self, bot_id: str, userid: str, raw: dict[str, Any]) -> dict[str, Any]:
        remark = raw.get("remark")
        if remark is not None and not isinstance(remark, str):
            raise AIError("备注格式无效")
        remark = (remark or "").strip() or None
        if remark and len(remark) > 100:
            raise AIError("备注最多 100 个字")
        row = await self.service.store.one(
            "UPDATE kubedoor_ai_im_users SET remark=$3,updated_at=now() "
            "WHERE bot_id=$1::uuid AND external_user_id=$2 RETURNING external_user_id",
            bot_id,
            userid,
            remark,
        )
        if not row:
            raise AIError("IM 成员不存在", 404, "not_found")
        return {"remark": remark}

    async def purge_member(self, bot_id: str, userid: str) -> dict[str, Any]:
        store = self.service.store
        prefix = f"{bot_id}:{userid}:"
        legacy_prefix = f"{userid}:" if bot_id == LEGACY_WECOM_BOT_ID else None
        async with store.transaction() as tx:
            await tx.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", f"im:{bot_id}:{userid}")
            if not await tx.one(
                "DELETE FROM kubedoor_ai_im_users WHERE bot_id=$1::uuid AND external_user_id=$2 RETURNING external_user_id",
                bot_id,
                userid,
            ):
                raise AIError("IM 成员不存在", 404, "not_found")
            await tx.execute(
                "DELETE FROM kubedoor_ai_im_approvals WHERE bot_id=$1::uuid AND external_user_id=$2",
                bot_id,
                userid,
            )
            sessions = await tx.fetch(
                "UPDATE kubedoor_ai_sessions SET checkpoint_purged_at=now() "
                "WHERE channel IN ('wecom','im','im:wecom','im:feishu','im:dingtalk','im:slack') "
                "AND (starts_with(channel_key,$1) OR ($2::text IS NOT NULL AND starts_with(channel_key,$2))) "
                "AND checkpoint_purged_at IS NULL RETURNING id",
                prefix,
                legacy_prefix,
            )
            runs = await tx.fetch(
                "SELECT r.id,r.status,r.username FROM kubedoor_ai_runs r JOIN kubedoor_ai_sessions s ON s.id=r.session_id "
                "WHERE s.channel IN ('wecom','im','im:wecom','im:feishu','im:dingtalk','im:slack') "
                "AND (starts_with(s.channel_key,$1) OR ($2::text IS NOT NULL AND starts_with(s.channel_key,$2))) "
                "AND r.status IN ('running','waiting_approval')",
                prefix,
                legacy_prefix,
            )
        for run in runs:
            with suppress(Exception):
                await self.service.runtime.cancel(run, Identity(run["username"], "none"))
        for row in sessions:
            with suppress(Exception):
                await self.service.runtime.checkpointer.adelete_thread(row["id"])
        return {"success": True, "sessions": len(sessions), "stopped": len(runs)}

    async def binding(self, identity: Identity) -> dict[str, Any]:
        bot_rows = await self.service.store.fetch("SELECT id,name,platform,enabled FROM kubedoor_ai_im_bots ORDER BY updated_at DESC, id")
        bots_info = []
        platforms: list[str] = []
        any_enabled = False
        any_connected = False
        for row in bot_rows:
            bid = str(row["id"])
            adapter = self.adapters.get(bid)
            st = await adapter.status() if adapter is not None else {}
            enabled = bool(row["enabled"])
            connected = bool(st.get("authenticated") or st.get("connected"))
            if enabled:
                any_enabled = True
                if row["platform"] not in platforms:
                    platforms.append(row["platform"])
            if connected:
                any_connected = True
            bots_info.append(
                {
                    "id": bid,
                    "name": row["name"],
                    "platform": row["platform"],
                    "enabled": enabled,
                    "connected": connected,
                }
            )
        members = await self.service.store.fetch(
            "SELECT u.bot_id,b.name AS bot_name,u.platform,u.external_user_id,u.self_name,u.bound_by,u.bound_at,u.last_seen_at "
            "FROM kubedoor_ai_im_users u JOIN kubedoor_ai_im_bots b ON b.id=u.bot_id WHERE u.username=$1 "
            "ORDER BY u.bound_at DESC NULLS LAST,u.last_seen_at DESC",
            identity.username,
        )
        return {
            "enabled": any_enabled,
            "connected": any_connected,
            "platforms": platforms,
            "bots": bots_info,
            "members": members,
            "code_minutes": BIND_CODE_MINUTES,
        }

    async def create_bind_code(self, identity: Identity) -> dict[str, Any]:
        enabled = await self.service.store.one("SELECT id FROM kubedoor_ai_im_bots WHERE enabled=true LIMIT 1")
        if not enabled:
            raise AIError("管理员还没有启用 IM 机器人", 409, "im_disabled")
        store = self.service.store
        await store.execute("DELETE FROM kubedoor_ai_im_bind_codes WHERE expires_at<now()")
        for _ in range(5):
            code = f"{secrets.randbelow(10 ** 6):06d}"
            try:
                row = await store.one(
                    "INSERT INTO kubedoor_ai_im_bind_codes(code,username,expires_at) "
                    "VALUES($1,$2,now()+make_interval(mins => $3)) "
                    "ON CONFLICT(username) DO UPDATE SET code=EXCLUDED.code,expires_at=EXCLUDED.expires_at,created_at=now() "
                    "RETURNING code,expires_at",
                    code,
                    identity.username,
                    BIND_CODE_MINUTES,
                )
            except asyncpg.UniqueViolationError:
                continue
            return {**row, "minutes": BIND_CODE_MINUTES}
        raise AIError("生成绑定码失败，请重试", 503, "bind_code_busy")

    async def unbind_self(self, identity: Identity, bot_id: str, userid: str) -> dict[str, Any]:
        row = await self.service.store.one(
            f"UPDATE kubedoor_ai_im_users SET {UNBIND_COLUMNS},updated_at=now() "
            "WHERE bot_id=$1::uuid AND external_user_id=$2 AND username=$3 RETURNING external_user_id",
            bot_id,
            userid,
            identity.username,
        )
        if not row:
            raise AIError("没有找到这个 IM 成员的绑定", 404, "not_found")
        return {"success": True}


class LegacyWeComBridge:
    """Backward-compatible adapter exposing `/api/ai/wecom*` over IMBotManager + SharedModelManager."""

    def __init__(self, manager: IMBotManager, shared_model: SharedModelManager) -> None:
        self.manager = manager
        self.shared_model = shared_model

    @property
    def config(self) -> dict[str, Any] | None:
        shared = self.shared_model.config or {}
        bot = self.manager.bots.get(LEGACY_WECOM_BOT_ID)
        if bot is None or bot.platform != "wecom":
            bot = next((b for b in self.manager.bots.values() if b.platform == "wecom"), None)
        if not shared and not bot:
            return None
        return {**(bot.credentials if bot else {}), **shared, "enabled": bot.enabled if bot else False}

    async def _default_wecom_bot(self) -> dict[str, Any] | None:
        bots = await self.manager.list_bots()
        for item in bots:
            if item["id"] == LEGACY_WECOM_BOT_ID:
                return item
        return next((item for item in bots if item["platform"] == "wecom"), None)

    async def admin_config(self) -> dict[str, Any]:
        bot = await self._default_wecom_bot()
        shared = await self.shared_model.get_config()
        if not bot:
            return {
                "configured": False,
                "connected": False,
                "authenticated": False,
                "reconnecting": False,
                "error": None,
                "enabled": False,
                "bot_id": "",
                "base_url": shared.get("base_url", ""),
                "model": shared.get("model", ""),
                "max_input_tokens": shared.get("max_input_tokens"),
                "max_output_tokens": shared.get("max_output_tokens"),
                "compression_ratio": shared.get("compression_ratio"),
                "reasoning_effort": shared.get("reasoning_effort"),
                "session_idle_minutes": DEFAULT_IDLE_MINUTES,
                "bot_secret_set": False,
                "api_key_set": bool(shared.get("api_key_set")),
                "updated_by": shared.get("updated_by"),
                "updated_at": shared.get("updated_at"),
            }
        return {
            "configured": bot["configured"],
            "connected": bot["connected"],
            "authenticated": bot["authenticated"],
            "reconnecting": bot["reconnecting"],
            "error": bot["error"],
            "enabled": bot["enabled"],
            "bot_id": (bot.get("credentials") or {}).get("bot_id", ""),
            "base_url": shared.get("base_url", ""),
            "model": shared.get("model", ""),
            "max_input_tokens": shared.get("max_input_tokens"),
            "max_output_tokens": shared.get("max_output_tokens"),
            "compression_ratio": shared.get("compression_ratio"),
            "reasoning_effort": shared.get("reasoning_effort"),
            "session_idle_minutes": bot["session_idle_minutes"],
            "bot_secret_set": bool(bot["secret_set"]),
            "api_key_set": bool(shared.get("api_key_set")),
            "updated_by": bot["updated_by"],
            "updated_at": bot["updated_at"],
        }

    async def save_config(self, identity: Identity, raw: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise AIError("配置格式无效")
        bot = await self._default_wecom_bot()
        bot_payload: dict[str, Any] = {
            "platform": "wecom",
            "name": bot["name"] if bot else "默认企业微信机器人",
            "enabled": raw.get("enabled") is True,
            "credentials": {
                "bot_id": raw.get("bot_id", ((bot or {}).get("credentials") or {}).get("bot_id", "")),
                "bot_secret": raw.get("bot_secret", ""),
            },
            "session_idle_minutes": raw.get("session_idle_minutes", (bot or {}).get("session_idle_minutes", DEFAULT_IDLE_MINUTES)),
        }
        current_bot: BotConfig | None = None
        if bot:
            current_bot = self.manager.bots.get(bot["id"])
            if current_bot is None:
                row = await self.manager.service.store.one(
                    "SELECT * FROM kubedoor_ai_im_bots WHERE id=$1::uuid",
                    bot["id"],
                )
                if row:
                    current_bot = self.manager._decode_row(row)
        self.manager._validate_bot_fields("wecom", bot_payload, current=current_bot)

        if any(raw.get(k) for k in ("base_url", "model", "api_key")):
            await self.shared_model.save_config(
                identity,
                {k: raw[k] for k in PROVIDER_KEYS if k in raw},
            )
        if bot:
            await self.manager.update_bot(identity, bot["id"], bot_payload)
        else:
            await self.manager.create_bot(identity, bot_payload)
        return await self.admin_config()

    async def unbound_setting(self) -> dict[str, Any]:
        bot = await self._default_wecom_bot()
        return {
            "unbound_username": bot.get("unbound_username") if bot else None,
            "unbound_default_env": bot.get("unbound_default_env") if bot else None,
        }

    async def users(self) -> list[dict[str, Any]]:
        bot = await self._default_wecom_bot()
        if not bot:
            return []
        rows = await self.manager.list_users(bot["id"])
        return [{**row, "wecom_userid": row["external_user_id"]} for row in rows]

    async def save_user(self, identity: Identity, raw: dict[str, Any]) -> dict[str, Any]:
        bot = await self._default_wecom_bot()
        if not bot:
            bot = await self.manager.create_bot(
                identity,
                {"platform": "wecom", "name": "默认企业微信机器人", "enabled": False, "credentials": {"bot_id": "", "bot_secret": ""}},
            )
        return await self.manager.save_user(
            identity,
            {
                "bot_id": bot["id"],
                "external_user_id": raw.get("wecom_userid") or raw.get("external_user_id"),
                "username": raw.get("username"),
                "default_env": raw.get("default_env"),
            },
        )

    async def save_unbound(self, identity: Identity, raw: dict[str, Any]) -> dict[str, Any]:
        bot = await self._default_wecom_bot()
        if not bot:
            bot = await self.manager.create_bot(
                identity,
                {"platform": "wecom", "name": "默认企业微信机器人", "enabled": False, "credentials": {"bot_id": "", "bot_secret": ""}},
            )
        return await self.manager.save_unbound(identity, bot["id"], raw)

    async def delete_user(self, userid: str) -> None:
        bot = await self._default_wecom_bot()
        if bot:
            await self.manager.delete_user(bot["id"], userid)

    async def purge_member(self, userid: str) -> dict[str, Any]:
        bot = await self._default_wecom_bot()
        if not bot:
            raise AIError("企微成员不存在", 404, "not_found")
        return await self.manager.purge_member(bot["id"], userid)

    async def save_remark(self, userid: str, raw: dict[str, Any]) -> dict[str, Any]:
        bot = await self._default_wecom_bot()
        if not bot:
            raise AIError("企微成员不存在", 404, "not_found")
        return await self.manager.save_remark(bot["id"], userid, raw)

    async def binding(self, identity: Identity) -> dict[str, Any]:
        state = await self.manager.binding(identity)
        wecom_bots = [b for b in state["bots"] if b["platform"] == "wecom"]
        wecom_members = [
            {**m, "wecom_userid": m["external_user_id"]}
            for m in state["members"]
            if m["platform"] == "wecom"
        ]
        return {
            "enabled": any(b["enabled"] for b in wecom_bots),
            "connected": any(b["connected"] for b in wecom_bots),
            "members": wecom_members,
            "code_minutes": state["code_minutes"],
        }

    async def create_bind_code(self, identity: Identity) -> dict[str, Any]:
        return await self.manager.create_bind_code(identity)

    async def unbind_self(self, identity: Identity, userid: str) -> dict[str, Any]:
        bot = await self._default_wecom_bot()
        if not bot:
            raise AIError("没有找到这个企微成员的绑定", 404, "not_found")
        return await self.manager.unbind_self(identity, bot["id"], userid)
