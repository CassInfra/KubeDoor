"""Tests for FeishuAdapter long connection, CardKit streaming, 3s SLA, and fallback."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from types import SimpleNamespace

import lark_oapi as lark
import pytest
from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse

sys.path.insert(0, os.path.dirname(__file__))

from kubedoor_ai.domain import AIError
from kubedoor_ai.im.adapters.feishu import (
    FeishuAdapter,
    FeishuCardStreamHandle,
    _safe_lark_error,
    resolve_feishu_domain,
)
from kubedoor_ai.im.models import ApprovalPrompt, CardActionEvent, InboundEvent


def _make_fake_client(*, create_card_ok: bool = True, content_ok: bool = True, patch_ok: bool = True):
    records: list[tuple[str, dict]] = []

    def _ok_resp(ok: bool, data=None, msg="err"):
        return SimpleNamespace(
            success=lambda: ok,
            code=0 if ok else 99999,
            msg="" if ok else msg,
            data=data,
        )

    def create_card(req):
        body = json.loads(req.request_body.data)
        records.append(("card.create", body))
        if not create_card_ok:
            return _ok_resp(False, msg="cardkit_disabled")
        return _ok_resp(True, data=SimpleNamespace(card_id="card-001"))

    def content_elem(req):
        body = req.request_body
        records.append(
            (
                "card_element.content",
                {
                    "card_id": req.card_id,
                    "element_id": req.element_id,
                    "content": body.content,
                    "sequence": body.sequence,
                    "uuid": body.uuid,
                },
            )
        )
        if not content_ok:
            return _ok_resp(False, msg="stream_update_failed")
        return _ok_resp(True)

    def settings_card(req):
        body = req.request_body
        records.append(
            (
                "card.settings",
                {
                    "card_id": req.card_id,
                    "settings": json.loads(body.settings),
                    "sequence": body.sequence,
                    "uuid": body.uuid,
                },
            )
        )
        return _ok_resp(True)

    def reply_msg(req):
        body = req.request_body
        records.append(
            (
                "im.message.reply",
                {"message_id": req.message_id, "msg_type": body.msg_type, "content": json.loads(body.content)},
            )
        )
        return _ok_resp(True, data=SimpleNamespace(message_id="om_reply_1"))

    def create_msg(req):
        body = req.request_body
        records.append(
            (
                "im.message.create",
                {"receive_id": body.receive_id, "msg_type": body.msg_type, "content": json.loads(body.content)},
            )
        )
        return _ok_resp(True, data=SimpleNamespace(message_id="om_create_1"))

    def patch_msg(req):
        body = req.request_body
        records.append(
            (
                "im.message.patch",
                {"message_id": req.message_id, "content": json.loads(body.content)},
            )
        )
        if not patch_ok:
            return _ok_resp(False, msg="patch_failed")
        return _ok_resp(True)

    client = SimpleNamespace(
        cardkit=SimpleNamespace(
            v1=SimpleNamespace(
                card=SimpleNamespace(create=create_card, settings=settings_card),
                card_element=SimpleNamespace(content=content_elem),
            )
        ),
        im=SimpleNamespace(
            v1=SimpleNamespace(
                message=SimpleNamespace(reply=reply_msg, create=create_msg, patch=patch_msg)
            )
        ),
    )
    return client, records


def test_resolve_feishu_domain_presets_and_ssrf(monkeypatch):
    assert resolve_feishu_domain({"domain_preset": "feishu"}) == lark.FEISHU_DOMAIN
    assert resolve_feishu_domain({"domain_preset": "lark"}) == lark.LARK_DOMAIN
    monkeypatch.setattr("socket.getaddrinfo", lambda *a, **kw: [(2, 1, 6, "", ("93.184.216.34", 443))])
    assert resolve_feishu_domain({"domain_preset": "custom", "custom_domain": "https://feishu.corp.example.com/"}) == "https://feishu.corp.example.com"

    with pytest.raises(AIError) as exc_info:
        resolve_feishu_domain({"domain_preset": "custom", "custom_domain": "http://169.254.169.254"})
    assert exc_info.value.status == 400
    assert exc_info.value.code == "invalid_custom_domain"


def test_safe_lark_error_redacts_app_secret():
    secret = "cli_super_secret_token_987654"
    err = RuntimeError(f"handshake rejected for secret={secret}\ntraceback line 2")
    msg = _safe_lark_error(err, (secret,))
    assert secret not in msg
    assert "traceback line 2" not in msg


@pytest.mark.asyncio
async def test_feishu_adapter_disabled_does_not_fail_on_empty_custom_domain():
    adapter = FeishuAdapter("00000000-0000-4000-8000-000000000099")
    async def noop(*_a):
        return None
    await adapter.start(
        {"enabled": False, "credentials": {"domain_preset": "custom", "custom_domain": ""}},
        noop,
        noop,
    )
    status = await adapter.status()
    assert status["configured"] is False
    assert status["connected"] is False


@pytest.mark.asyncio
async def test_feishu_adapter_ws_dispatch_and_mentions():
    adapter = FeishuAdapter("bot-feishu-1")
    adapter._main_loop = asyncio.get_running_loop()
    adapter._bot_open_id = "ou_bot_self"
    received: list[InboundEvent] = []

    async def on_msg(ev: InboundEvent) -> None:
        received.append(ev)

    adapter._on_message = on_msg

    # Group message mentioning bot: strips @_user_1 and marks mentioned_bot=True.
    payload = SimpleNamespace(
        event=SimpleNamespace(
            sender=SimpleNamespace(sender_type="user", sender_id=SimpleNamespace(open_id="ou_user_1")),
            message=SimpleNamespace(
                message_id="om_1",
                chat_id="oc_group_1",
                chat_type="group",
                message_type="text",
                content=json.dumps({"text": "@_user_1 @_user_2 查看 pod 状态"}),
                mentions=[SimpleNamespace(key="@_user_1", name="KubeDoor", id=SimpleNamespace(open_id="ou_bot_self"))],
            ),
        )
    )
    # Call from a worker thread just like lark.ws.Client does.
    await asyncio.to_thread(adapter._on_im_message_sync, payload)
    await asyncio.sleep(0.05)

    assert len(received) == 1
    ev = received[0]
    assert ev.external_user_id == "ou_user_1"
    assert ev.chat_type == "group"
    assert ev.mentioned_bot is True
    assert ev.text == "查看 pod 状态"

    # Message from app itself must be ignored.
    bot_msg = SimpleNamespace(
        event=SimpleNamespace(
            sender=SimpleNamespace(sender_type="app", sender_id=SimpleNamespace(open_id="ou_bot_self")),
            message=SimpleNamespace(message_id="om_2", chat_id="oc_group_1", chat_type="group", message_type="text", content='{"text":"hi"}', mentions=[]),
        )
    )
    await asyncio.to_thread(adapter._on_im_message_sync, bot_msg)
    await asyncio.sleep(0.05)
    assert len(received) == 1


@pytest.mark.asyncio
async def test_feishu_adapter_card_action_sla_fast_and_slow(monkeypatch):
    adapter = FeishuAdapter("bot-feishu-1")
    adapter._main_loop = asyncio.get_running_loop()
    monkeypatch.setattr(adapter, "capabilities", FeishuAdapter.capabilities)
    # Use a tight SLA override via instance capabilities clone.
    from dataclasses import replace
    adapter.capabilities = replace(FeishuAdapter.capabilities, card_callback_sla_sec=0.08)

    trigger = SimpleNamespace(
        header=SimpleNamespace(event_id="evt_card_1"),
        event=SimpleNamespace(
            token="tok_1",
            operator=SimpleNamespace(open_id="ou_approver"),
            action=SimpleNamespace(
                name="btn",
                value={"action_id": "approval:approve:ref_abc", "card_ref": "ref_abc"},
                option=None,
                options=None,
            ),
            context=SimpleNamespace(open_message_id="om_card_1", open_chat_id="oc_1"),
        ),
    )

    # Fast handler -> returns result card + success toast.
    async def fast_handler(ev: CardActionEvent):
        assert ev.action_id == "approval:approve:ref_abc"
        assert ev.callback_token == "ref_abc"
        assert ev.raw_context["open_message_id"] == "om_card_1"
        return {"title": "已批准", "desc": "集群：prod", "status": "已批准", "toast_type": "success", "toast": "已批准"}

    adapter._on_card_action = fast_handler
    resp_fast = await asyncio.to_thread(adapter._on_card_action_sync, trigger)
    assert isinstance(resp_fast, P2CardActionTriggerResponse)
    assert resp_fast.toast.type == "success"
    assert resp_fast.card is not None

    # Slow handler exceeding SLA -> returns immediate "处理中…" toast before callback finishes.
    completed = asyncio.Event()
    main_loop = asyncio.get_running_loop()

    async def slow_handler(_ev: CardActionEvent):
        await asyncio.sleep(0.25)
        completed.set()
        return {"title": "已批准", "toast_type": "success", "toast": "已批准"}

    adapter._on_card_action = slow_handler
    started = time.monotonic()
    resp_slow = await asyncio.to_thread(adapter._on_card_action_sync, trigger)
    elapsed = time.monotonic() - started
    assert elapsed < 0.25
    assert resp_slow.toast.content == "处理中…"
    await asyncio.wait_for(completed.wait(), timeout=1.0)
    assert main_loop.is_running()


@pytest.mark.asyncio
async def test_feishu_cardkit_tier_a_streaming_sequences_and_closes():
    adapter = FeishuAdapter("bot-feishu-1")
    client, records = _make_fake_client(create_card_ok=True, content_ok=True)
    adapter.client = client

    handle = FeishuCardStreamHandle(
        adapter,
        external_user_id="ou_1",
        chat_id="oc_1",
        reply_to_message_id="om_in_1",
        throttle_ms=50,
    )
    await handle.update("第 1 段")
    await asyncio.sleep(0.06)
    await handle.update("第 2 段")
    await handle.finish("最终回答")

    names = [name for name, _ in records]
    assert names[0] == "card.create"
    assert records[0][1]["schema"] == "2.0"
    assert records[0][1]["config"]["streaming_mode"] is True
    assert names[1] == "im.message.reply"
    assert records[1][1]["msg_type"] == "card"

    seqs = [payload["sequence"] for name, payload in records if name in ("card_element.content", "card.settings")]
    assert seqs == [1, 2, 3]
    uuids = [payload["uuid"] for name, payload in records if name in ("card_element.content", "card.settings")]
    assert len(uuids) == len(set(uuids)) == 3

    settings_calls = [payload for name, payload in records if name == "card.settings"]
    assert len(settings_calls) == 1
    assert settings_calls[0]["settings"]["config"]["streaming_mode"] is False


@pytest.mark.asyncio
async def test_feishu_cardkit_always_closes_streaming_even_on_update_error():
    adapter = FeishuAdapter("bot-feishu-1")
    client, records = _make_fake_client(create_card_ok=True, content_ok=False)
    adapter.client = client

    handle = FeishuCardStreamHandle(adapter, external_user_id="ou_1", chat_id="", reply_to_message_id="om_1", throttle_ms=50)
    await handle.update("初始内容")
    await asyncio.sleep(0.06)
    # Second update fails on card_element.content -> must close streaming and fall back to Tier B patch.
    await handle.update("增量失败")
    await handle.finish("最终回复")

    settings_calls = [payload for name, payload in records if name == "card.settings"]
    assert len(settings_calls) == 1
    assert settings_calls[0]["settings"]["config"]["streaming_mode"] is False
    assert any(name == "im.message.patch" for name, _ in records)


@pytest.mark.asyncio
async def test_feishu_tier_b_and_tier_c_fallback_plus_approval_and_cluster_cards():
    # Tier B fallback when cardkit.v1.card.create fails
    adapter = FeishuAdapter("bot-feishu-1")
    client_b, records_b = _make_fake_client(create_card_ok=False, patch_ok=True)
    adapter.client = client_b
    handle_b = FeishuCardStreamHandle(adapter, external_user_id="ou_1", chat_id="oc_1", reply_to_message_id="", throttle_ms=50)
    await handle_b.update("阶段 1")
    await handle_b.finish("完成")
    assert [n for n, _ in records_b] == ["card.create", "im.message.create", "im.message.patch"]

    # Tier C fallback when both CardKit and interactive card send fail -> falls back to plain text
    adapter_tier_c = FeishuAdapter("bot-feishu-c")
    client_tc, records_tc = _make_fake_client(create_card_ok=False, patch_ok=False)
    orig_create = client_tc.im.v1.message.create
    def create_msg_fail_interactive(req):
        if req.request_body.msg_type == "interactive":
            return SimpleNamespace(success=lambda: False, code=999, msg="no_card_perm", data=None)
        return orig_create(req)
    client_tc.im.v1.message.create = create_msg_fail_interactive
    adapter_tier_c.client = client_tc
    handle_tc = FeishuCardStreamHandle(adapter_tier_c, external_user_id="ou_1", chat_id="oc_1", reply_to_message_id="", throttle_ms=50)
    await handle_tc.update("阶段 1")
    await handle_tc.finish("文本兜底结果")
    assert records_tc[-1][0] == "im.message.create"
    assert records_tc[-1][1]["msg_type"] == "text"
    assert records_tc[-1][1]["content"]["text"] == "文本兜底结果"

    # Approval card + cluster selection card
    adapter_c = FeishuAdapter("bot-feishu-1")
    client_c, records_c = _make_fake_client(create_card_ok=True)
    adapter_c.client = client_c
    handle_c = adapter_c.open_push_stream("ou_1", "oc_1")
    ref = await handle_c.send_approval(
        ApprovalPrompt(
            action_id="act-1",
            run_id="run-1",
            env="prod",
            operation_label="重启工作负载",
            summary_markdown="待确认",
            expires_at_text="10分钟后",
            card_ref="ref-99",
            body_markdown="deployment api",
        ),
        fallback_text="请回复批准或拒绝",
    )
    assert ref == "ref-99"
    approval_msgs = [
        p
        for n, p in records_c
        if n == "im.message.create"
        and p["msg_type"] == "interactive"
        and p["content"].get("header", {}).get("template") == "red"
    ]
    assert len(approval_msgs) == 1
    buttons = approval_msgs[0]["content"]["body"]["elements"][1]["columns"]
    assert buttons[0]["elements"][0]["value"]["action_id"] == "approval:approve:ref-99"
    assert buttons[1]["elements"][0]["value"]["action_id"] == "approval:reject:ref-99"

    handle_menu = adapter_c.open_push_stream("ou_1", "oc_1")
    await handle_menu.send_cluster_menu("prod", [{"env": "prod"}, {"env": "test"}], "fallback", prompt_text="选择集群")
    menu_card = [p for n, p in records_c if n == "im.message.create" and p["content"].get("header", {}).get("title", {}).get("content") == "选择 K8S 集群"][0]
    assert menu_card["content"]["body"]["elements"][1]["tag"] == "select_static"
    assert len(menu_card["content"]["body"]["elements"][1]["options"]) == 2
