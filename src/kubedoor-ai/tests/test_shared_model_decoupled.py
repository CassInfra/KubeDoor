"""Tests for decoupled global SharedModelManager and legacy WeCom migration."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from kubedoor_ai.domain import AIError, CredentialCipher, Identity
from kubedoor_ai.im.engine import IMBotEngine
from kubedoor_ai.im.models import BotConfig, IMCapabilities, InboundEvent
from kubedoor_ai.im.shared_model import SHARED_MODEL_AAD, SharedModelManager
from kubedoor_ai.service import Service


TEST_KEY = "0123456789abcdef" * 4


class _StubGateway:
    def __init__(self, cipher: CredentialCipher) -> None:
        self.cipher = cipher


class _FakeSharedStore:
    def __init__(self) -> None:
        self.shared_row: dict | None = None
        self.legacy_row: dict | None = None
        self.executed: list[tuple[str, tuple]] = []

    async def one(self, sql: str, *args):
        if "FROM kubedoor_ai_shared_model" in sql:
            return dict(self.shared_row) if self.shared_row else None
        if "FROM kubedoor_ai_wecom_config" in sql:
            return dict(self.legacy_row) if self.legacy_row else None
        if "INSERT INTO kubedoor_ai_shared_model" in sql:
            encrypted, updated_by = args[0], args[1]
            now = datetime.now(timezone.utc)
            if "DO NOTHING" in sql and self.shared_row is not None:
                return None
            self.shared_row = {
                "singleton": True,
                "encrypted_config": encrypted,
                "updated_by": updated_by,
                "updated_at": now,
            }
            return dict(self.shared_row)
        return None

    async def execute(self, sql: str, *args):
        self.executed.append((sql, args))
        if "INSERT INTO kubedoor_ai_shared_model" in sql:
            if "DO NOTHING" in sql and self.shared_row is not None:
                return "INSERT 0 0"
            encrypted, updated_by = args[0], args[1]
            self.shared_row = {
                "singleton": True,
                "encrypted_config": encrypted,
                "updated_by": updated_by,
                "updated_at": datetime.now(timezone.utc),
            }
            return "INSERT 0 1"
        return "OK"


class _DummyAdapter:
    platform = "feishu"
    capabilities = IMCapabilities(
        supports_native_stream=True,
        supports_message_edit=True,
        supports_interactive_card=True,
        supports_group_mention=True,
        max_message_bytes=20000,
        stream_throttle_ms=220,
        card_callback_sla_sec=2.2,
    )

    def open_reply_stream(self, event: InboundEvent):
        raise NotImplementedError

    def open_push_stream(self, external_user_id: str, chat_id: str = ""):
        raise NotImplementedError

    async def update_approval_card(self, card_ref: str, decision: str, summary: str, raw_context=None) -> None:
        return None


def _make_service() -> tuple[Service, _FakeSharedStore]:
    cipher = CredentialCipher(TEST_KEY)
    store = _FakeSharedStore()
    runtime = SimpleNamespace(model_factory=lambda provider: None)
    svc = Service(store, _StubGateway(cipher), runtime)
    svc.test_provider = AsyncMock(return_value={"success": True, "model": "gpt-4o", "base_url": "https://api.example.com/v1"})
    return svc, store


@pytest.mark.asyncio
async def test_shared_model_auto_migrates_from_legacy_wecom():
    svc, store = _make_service()
    legacy_payload = {
        "bot_id": "wecom-bot-1",
        "bot_secret": "wecom-secret-xyz",
        "base_url": "https://legacy-llm.example.com/v1",
        "api_key": "sk-legacy-super-secret",
        "model": "gpt-4.1",
        "max_input_tokens": 64000,
        "max_output_tokens": 4096,
        "compression_ratio": 0.75,
        "reasoning_effort": "high",
    }
    store.legacy_row = {
        "singleton": True,
        "encrypted_config": svc.gateway.cipher.encrypt("wecom", legacy_payload),
        "updated_by": "legacy-admin",
        "updated_at": datetime.now(timezone.utc),
    }

    mgr = SharedModelManager(svc)
    loaded = await mgr.load()
    assert loaded is not None
    assert loaded["base_url"] == "https://legacy-llm.example.com/v1"
    assert loaded["model"] == "gpt-4.1"
    assert loaded["api_key"] == "sk-legacy-super-secret"
    assert loaded["max_input_tokens"] == 64000
    assert loaded["reasoning_effort"] == "high"

    # Must persist into kubedoor_ai_shared_model encrypted with SHARED_MODEL_AAD without IM bot keys.
    assert store.shared_row is not None
    persisted = svc.gateway.cipher.decrypt(SHARED_MODEL_AAD, bytes(store.shared_row["encrypted_config"]))
    assert "bot_id" not in persisted
    assert "bot_secret" not in persisted
    assert persisted["model"] == "gpt-4.1"

    # Decoupled from IM bots: both "shared" and "wecom" sources work with zero IM bots configured.
    p_shared = svc.provider({"source": "shared"})
    p_wecom = svc.provider({"source": "wecom"})
    assert p_shared.model == "gpt-4.1"
    assert p_shared.api_key == "sk-legacy-super-secret"
    assert p_wecom == p_shared
    summary = svc.shared_model()
    assert summary["available"] is True
    assert summary["api_key_set"] is True
    assert "sk-legacy-super-secret" not in str(summary)


@pytest.mark.asyncio
async def test_shared_model_load_does_not_overwrite_existing_shared_row():
    svc, store = _make_service()
    store.shared_row = {
        "singleton": True,
        "encrypted_config": svc.gateway.cipher.encrypt(
            SHARED_MODEL_AAD,
            {"base_url": "https://shared.example.com/v1", "api_key": "sk-shared-1", "model": "deepseek-chat"},
        ),
        "updated_by": "admin",
        "updated_at": datetime.now(timezone.utc),
    }
    store.legacy_row = {
        "singleton": True,
        "encrypted_config": svc.gateway.cipher.encrypt(
            "wecom",
            {"base_url": "https://old-wecom.example.com/v1", "api_key": "sk-old", "model": "gpt-3.5"},
        ),
        "updated_by": "old",
        "updated_at": datetime.now(timezone.utc),
    }

    mgr = SharedModelManager(svc)
    loaded = await mgr.load()
    assert loaded["model"] == "deepseek-chat"
    assert loaded["base_url"] == "https://shared.example.com/v1"


@pytest.mark.asyncio
async def test_shared_model_409_when_unconfigured_or_cleared():
    svc, _ = _make_service()
    SharedModelManager(svc)
    for src in ("shared", "wecom"):
        with pytest.raises(AIError) as exc_info:
            svc.provider({"source": src})
        assert exc_info.value.status == 409
        assert exc_info.value.code == "shared_model_unavailable"


@pytest.mark.asyncio
async def test_shared_model_save_reuses_api_key_and_supports_test():
    svc, _ = _make_service()
    mgr = SharedModelManager(svc)
    admin = Identity("alice", "admin")

    view1 = await mgr.save_config(
        admin,
        {
            "base_url": "https://api.example.com/v1",
            "api_key": "sk-top-secret-1234",
            "model": "gpt-4o",
            "max_input_tokens": 32000,
            "max_output_tokens": 2048,
            "compression_ratio": 0.8,
            "reasoning_effort": "low",
        },
    )
    assert view1["available"] is True
    assert view1["configured"] is True
    assert view1["api_key_set"] is True
    assert "sk-top-secret-1234" not in repr(view1)

    # Updating model without passing api_key preserves saved api_key.
    view2 = await mgr.save_config(
        admin,
        {
            "base_url": "https://api.example.com/v1",
            "model": "gpt-4.1",
            "max_input_tokens": 64000,
        },
    )
    assert view2["model"] == "gpt-4.1"
    assert svc.provider({"source": "shared"}).api_key == "sk-top-secret-1234"

    # Testing with blank api_key injects the stored key before calling svc.test_provider.
    res = await mgr.test_config({"provider": {"base_url": "https://api.example.com/v1", "api_key": "", "model": "gpt-4o"}})
    assert res["success"] is True
    called_provider = svc.test_provider.await_args.args[0]
    assert called_provider["api_key"] == "sk-top-secret-1234"


@pytest.mark.asyncio
async def test_im_engine_model_override_precedence_and_shared_fallback():
    svc, _ = _make_service()
    mgr = SharedModelManager(svc)
    await mgr.save_config(
        Identity("alice", "admin"),
        {"base_url": "https://shared.example.com/v1", "api_key": "sk-shared-key", "model": "shared-model"},
    )

    bot_default = BotConfig(id="00000000-0000-4000-8000-000000000010", platform="feishu", name="feishu-1", model_override=None)
    engine_default = IMBotEngine(svc, bot_default, _DummyAdapter(), shared_model=mgr)
    p1 = engine_default.resolve_provider()
    assert p1.model == "shared-model"
    assert p1.api_key == "sk-shared-key"

    bot_override = BotConfig(
        id="00000000-0000-4000-8000-000000000011",
        platform="feishu",
        name="feishu-2",
        model_override={"base_url": "https://override.example.com/v1", "api_key": "sk-override", "model": "override-model"},
    )
    engine_override = IMBotEngine(svc, bot_override, _DummyAdapter(), shared_model=mgr)
    p2 = engine_override.resolve_provider()
    assert p2.model == "override-model"
    assert p2.api_key == "sk-override"
