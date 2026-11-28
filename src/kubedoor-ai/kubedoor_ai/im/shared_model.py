"""Decoupled global shared default LLM configuration manager."""
from __future__ import annotations

import logging
from typing import Any

from ..domain import AIError, Identity, Provider
from .models import PROVIDER_KEYS

LOGGER = logging.getLogger(__name__)
SHARED_MODEL_AAD = "shared_model"


def _extract_provider_fields(raw: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    return {key: raw.get(key) for key in PROVIDER_KEYS}


def _has_provider_data(cfg: dict[str, Any] | None) -> bool:
    return bool(cfg and any(cfg.get(key) for key in ("base_url", "model", "api_key")))


class SharedModelManager:
    """Manages global `kubedoor_ai_shared_model` independently of IM bots."""

    def __init__(self, service: Any, *, wire_service: bool = True) -> None:
        self.service = service
        self.config: dict[str, Any] | None = None
        self.updated_by: str | None = None
        self.updated_at: Any = None
        if wire_service:
            service.shared_settings = self.settings

    def settings(self) -> dict[str, Any] | None:
        if not self.config:
            return None
        return _extract_provider_fields(self.config)

    def _public_view(self, cfg: dict[str, Any] | None, updated_by: str | None, updated_at: Any) -> dict[str, Any]:
        cfg = cfg or {}
        try:
            Provider.parse(cfg)
            available = True
        except AIError:
            available = False
        return {
            "available": available,
            "configured": bool(cfg.get("base_url") or cfg.get("model")),
            "base_url": cfg.get("base_url") or "",
            "model": cfg.get("model") or "",
            "max_input_tokens": cfg.get("max_input_tokens"),
            "max_output_tokens": cfg.get("max_output_tokens"),
            "compression_ratio": cfg.get("compression_ratio"),
            "reasoning_effort": cfg.get("reasoning_effort"),
            "api_key_set": bool(cfg.get("api_key")),
            "updated_by": updated_by,
            "updated_at": updated_at,
        }

    async def load(self) -> dict[str, Any] | None:
        """Load shared LLM settings from DB, migrating from legacy WeCom config if empty."""
        store = getattr(self.service, "store", None)
        gateway = getattr(self.service, "gateway", None)
        if not store or not gateway or not getattr(gateway, "cipher", None):
            return self.config
        try:
            row = await store.one(
                "SELECT encrypted_config,updated_by,updated_at FROM kubedoor_ai_shared_model WHERE singleton=true"
            )
            if row:
                cfg = _extract_provider_fields(
                    gateway.cipher.decrypt(SHARED_MODEL_AAD, bytes(row["encrypted_config"]))
                )
                self.config = cfg
                self.updated_by = row.get("updated_by")
                self.updated_at = row.get("updated_at")
                self.service.shared_settings = self.settings
                return self.config
            legacy = await store.one(
                "SELECT encrypted_config,updated_by,updated_at FROM kubedoor_ai_wecom_config WHERE singleton=true"
            )
            if legacy:
                decrypted = gateway.cipher.decrypt("wecom", bytes(legacy["encrypted_config"]))
                migrated = _extract_provider_fields(decrypted)
                if _has_provider_data(migrated):
                    encrypted = gateway.cipher.encrypt(SHARED_MODEL_AAD, migrated)
                    await store.execute(
                        "INSERT INTO kubedoor_ai_shared_model(singleton,encrypted_config,updated_by) "
                        "VALUES(true,$1,$2) ON CONFLICT(singleton) DO NOTHING",
                        encrypted,
                        legacy.get("updated_by") or "system",
                    )
                    self.config = migrated
                    self.updated_by = legacy.get("updated_by")
                    self.updated_at = legacy.get("updated_at")
                    self.service.shared_settings = self.settings
                    return self.config
        except Exception as exc:
            LOGGER.warning("公共默认模型配置读取失败: %s", type(exc).__name__)
        return self.config

    async def get_config(self) -> dict[str, Any]:
        """Return redacted public view of the shared default LLM configuration."""
        store = getattr(self.service, "store", None)
        gateway = getattr(self.service, "gateway", None)
        if store and gateway and getattr(gateway, "cipher", None):
            try:
                row = await store.one(
                    "SELECT encrypted_config,updated_by,updated_at FROM kubedoor_ai_shared_model WHERE singleton=true"
                )
            except Exception:
                row = None
            if row:
                self.config = _extract_provider_fields(
                    gateway.cipher.decrypt(SHARED_MODEL_AAD, bytes(row["encrypted_config"]))
                )
                self.updated_by = row.get("updated_by")
                self.updated_at = row.get("updated_at")
            elif self.config is None:
                await self.load()
        return self._public_view(self.config, self.updated_by, self.updated_at)

    async def save_config(self, identity: Identity, raw: dict[str, Any]) -> dict[str, Any]:
        """Validate, encrypt, and persist global shared LLM configuration."""
        if not isinstance(raw, dict):
            raise AIError("配置格式无效")
        payload = raw["provider"] if isinstance(raw.get("provider"), dict) and "base_url" not in raw else raw
        if self.config is None:
            await self.load()
        current = self.config or {}

        if payload.get("clear_api_key") is True:
            api_key = ""
        elif payload.get("api_key"):
            api_key = payload["api_key"]
        else:
            api_key = current.get("api_key", "")

        base_url = payload.get("base_url", current.get("base_url", ""))
        model = payload.get("model", current.get("model", ""))
        config: dict[str, Any] = {
            "base_url": base_url.strip() if isinstance(base_url, str) else base_url,
            "api_key": api_key,
            "model": model.strip() if isinstance(model, str) else model,
        }
        for key in ("max_input_tokens", "max_output_tokens", "compression_ratio", "reasoning_effort"):
            config[key] = payload.get(key, current.get(key))

        # Validate strictly with Provider.parse (checks URL, limits, reasoning_effort, model).
        Provider.parse(config)

        encrypted = self.service.gateway.cipher.encrypt(SHARED_MODEL_AAD, config)
        row = await self.service.store.one(
            "INSERT INTO kubedoor_ai_shared_model(singleton,encrypted_config,updated_by,updated_at) "
            "VALUES(true,$1,$2,now()) "
            "ON CONFLICT(singleton) DO UPDATE SET encrypted_config=EXCLUDED.encrypted_config,"
            "updated_by=EXCLUDED.updated_by,updated_at=now() "
            "RETURNING encrypted_config,updated_by,updated_at",
            encrypted,
            identity.username,
        )
        saved_cfg = _extract_provider_fields(
            self.service.gateway.cipher.decrypt(SHARED_MODEL_AAD, bytes(row["encrypted_config"]))
        )
        self.config = saved_cfg
        self.updated_by = row.get("updated_by") or identity.username
        self.updated_at = row.get("updated_at")
        self.service.shared_settings = self.settings
        return self._public_view(self.config, self.updated_by, self.updated_at)

    async def test_config(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Test a provider payload, reusing the saved API key when left blank."""
        if not isinstance(raw, dict):
            raise AIError("配置格式无效")
        provider = raw["provider"] if isinstance(raw.get("provider"), dict) else raw
        if isinstance(provider, dict) and not provider.get("api_key"):
            if self.config is None:
                await self.load()
            if self.config and self.config.get("api_key"):
                provider = {**provider, "api_key": self.config["api_key"]}
        return await self.service.test_provider(provider)
