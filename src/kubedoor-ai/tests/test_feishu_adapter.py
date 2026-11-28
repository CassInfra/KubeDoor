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
    assert records[0][1]["config"]["width_mode"] == "fill"
    assert records[0][1]["config"]["update_multi"] is True
    assert records[0][1]["config"]["streaming_mode"] is True
    assert names[1] == "im.message.reply"
    assert records[1][1]["msg_type"] == "interactive"
    assert records[1][1]["content"] == {"type": "card", "data": {"card_id": "card-001"}}

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
    assert approval_msgs[0]["content"]["config"] == {"width_mode": "fill", "update_multi": True}
    assert "wide_screen_mode" not in approval_msgs[0]["content"]["config"]
    buttons = approval_msgs[0]["content"]["body"]["elements"][1]["columns"]
    btn_approve = buttons[0]["elements"][0]
    btn_reject = buttons[1]["elements"][0]
    assert "value" not in btn_approve
    assert "value" not in btn_reject
    assert btn_approve["behaviors"] == [{"type": "callback", "value": {"action_id": "approval:approve:ref-99", "card_ref": "ref-99"}}]
    assert btn_reject["behaviors"] == [{"type": "callback", "value": {"action_id": "approval:reject:ref-99", "card_ref": "ref-99"}}]

    handle_menu = adapter_c.open_push_stream("ou_1", "oc_1")
    await handle_menu.send_cluster_menu("prod", [{"env": "prod"}, {"env": "test"}], "fallback", prompt_text="选择集群")
    menu_card = [p for n, p in records_c if n == "im.message.create" and p["content"].get("header", {}).get("title", {}).get("content") == "选择 K8S 集群"][0]
    assert menu_card["content"]["config"] == {"width_mode": "fill", "update_multi": True}
    assert "wide_screen_mode" not in menu_card["content"]["config"]
    select_el = menu_card["content"]["body"]["elements"][1]
    assert select_el["tag"] == "select_static"
    assert select_el["name"] == "cluster_select"
    assert "value" not in select_el
    assert select_el["behaviors"][0]["type"] == "callback"
    assert select_el["behaviors"][0]["value"]["action_id"] == "cluster:select"
    assert len(select_el["options"]) == 2


@pytest.mark.asyncio
async def test_feishu_tier_b_send_cluster_menu_finishes_status_message():
    adapter = FeishuAdapter("bot-feishu-tier-b")
    client, records = _make_fake_client(create_card_ok=False, patch_ok=True)
    adapter.client = client

    handle = FeishuCardStreamHandle(
        adapter,
        external_user_id="ou_1",
        chat_id="oc_1",
        reply_to_message_id="",
        throttle_ms=50,
    )
    await handle.update("正在校验账号与访问权限...")
    assert handle._tier == "B"
    assert handle._card_id is None
    assert handle._message_id == "om_create_1"

    await handle.send_cluster_menu(
        None,
        [{"env": "prod"}, {"env": "test"}],
        "fallback",
        prompt_text="请在下方卡片选择集群",
    )
    patches = [p for n, p in records if n == "im.message.patch"]
    assert len(patches) == 1
    assert patches[0]["message_id"] == "om_create_1"
    assert patches[0]["content"]["config"] == {"width_mode": "fill", "update_multi": True}
    assert patches[0]["content"]["body"]["elements"][0]["content"] == "请在下方卡片选择集群"

    menu_cards = [
        p for n, p in records
        if n == "im.message.create" and p["content"].get("header", {}).get("title", {}).get("content") == "选择 K8S 集群"
    ]
    assert len(menu_cards) == 1
    # prompt_text already patched onto the Tier B status message, not duplicated inside cluster card
    assert [el["tag"] for el in menu_cards[0]["content"]["body"]["elements"]] == ["select_static"]


@pytest.mark.asyncio
async def test_feishu_mentions_fail_closed_and_retry_fetch_bot_info():
    adapter = FeishuAdapter("bot-feishu-mention")
    adapter._main_loop = asyncio.get_running_loop()
    # When both _bot_open_id and _bot_name are unknown, mentioning another user MUST fail closed!
    other_mention = [SimpleNamespace(key="@_user_1", name="同事张三", id=SimpleNamespace(open_id="ou_other"))]
    assert adapter._is_bot_mentioned(other_mention) == (False, set())
    assert adapter._is_bot_mentioned([SimpleNamespace(key="@_all", name="所有人", id=SimpleNamespace(open_id=""))]) == (False, set())

    # Retry _fetch_bot_info() on inbound group message when bot info was initially unavailable.
    bot_info_calls = 0

    def fake_request(req):
        nonlocal bot_info_calls
        bot_info_calls += 1
        if bot_info_calls == 1:
            return SimpleNamespace(success=lambda: False, code=999, msg="tmp_err", raw=None)
        payload = json.dumps({"bot": {"open_id": "ou_real_bot", "app_name": "KubeDoor"}}).encode("utf-8")
        return SimpleNamespace(success=lambda: True, code=0, msg="", raw=SimpleNamespace(content=payload))

    adapter.client = SimpleNamespace(request=fake_request)
    await adapter._fetch_bot_info()
    assert adapter._bot_open_id is None
    assert adapter._bot_name is None

    received: list[InboundEvent] = []

    async def on_msg(ev: InboundEvent) -> None:
        received.append(ev)

    adapter._on_message = on_msg
    group_payload = SimpleNamespace(
        event=SimpleNamespace(
            sender=SimpleNamespace(sender_type="user", sender_id=SimpleNamespace(open_id="ou_user_1")),
            message=SimpleNamespace(
                message_id="om_retry_1",
                chat_id="oc_grp",
                chat_type="group",
                message_type="text",
                content=json.dumps({"text": "@_user_1 查一下告警"}),
                mentions=[SimpleNamespace(key="@_user_1", name="KubeDoor", id=SimpleNamespace(open_id="ou_real_bot"))],
            ),
        )
    )
    await asyncio.to_thread(adapter._on_im_message_sync, group_payload)
    await asyncio.sleep(0.05)
    assert bot_info_calls == 2
    assert adapter._bot_open_id == "ou_real_bot"
    assert len(received) == 1
    assert received[0].mentioned_bot is True
    assert received[0].text == "查一下告警"


@pytest.mark.asyncio
async def test_feishu_card_action_extracts_dict_list_and_name_defensively():
    adapter = FeishuAdapter("bot-feishu-extract")
    adapter._main_loop = asyncio.get_running_loop()
    seen: list[CardActionEvent] = []

    async def handler(ev: CardActionEvent):
        seen.append(ev)
        return {"toast_type": "info", "toast": "ok"}

    adapter._on_card_action = handler

    # List-wrapped behavior callback payload
    list_trigger = SimpleNamespace(
        header=SimpleNamespace(event_id="evt_list"),
        event=SimpleNamespace(
            token="tok_l",
            operator=SimpleNamespace(open_id="ou_1"),
            action=SimpleNamespace(
                name="cluster_select",
                value=[{"type": "callback", "value": {"action_id": "cluster:select", "card_ref": "env_list"}}],
                option="prod",
                options=None,
            ),
            context=SimpleNamespace(open_message_id="om_1", open_chat_id="oc_1"),
        ),
    )
    await asyncio.to_thread(adapter._on_card_action_sync, list_trigger)
    assert seen[-1].action_id == "cluster:select"
    assert seen[-1].callback_token == "env_list"
    assert seen[-1].selected_value == "prod"

    # Empty value falling back to component name "cluster_select"
    name_trigger = SimpleNamespace(
        header=SimpleNamespace(event_id="evt_name"),
        event=SimpleNamespace(
            token="tok_n",
            operator=SimpleNamespace(open_id="ou_1"),
            action=SimpleNamespace(name="cluster_select", value=None, option="staging", options=None),
            context=SimpleNamespace(open_message_id="om_1", open_chat_id="oc_1"),
        ),
    )
    await asyncio.to_thread(adapter._on_card_action_sync, name_trigger)
    assert seen[-1].action_id == "cluster:select"
    assert seen[-1].selected_value == "staging"


@pytest.mark.asyncio
async def test_feishu_real_ws_loopback_event_card_invalid_status_reconnect_and_clean_stop(monkeypatch):
    import base64
    from dataclasses import replace
    import http
    import websockets
    from lark_oapi.ws.enum import FrameType, MessageType
    from lark_oapi.ws.pb.pbbp2_pb2 import Frame
    import kubedoor_ai.im.adapters.feishu as feishu_mod

    main_loop = asyncio.get_running_loop()
    main_exceptions: list[dict] = []
    ws_exceptions: list[dict] = []
    orig_main_handler = main_loop.get_exception_handler()
    main_loop.set_exception_handler(lambda _loop, ctx: main_exceptions.append(ctx))

    def _add_hdr(frame: Frame, key: str, value: str) -> None:
        h = frame.headers.add()
        h.key = key
        h.value = value

    def _get_hdr(frame: Frame, key: str) -> str:
        for h in frame.headers:
            if h.key == key:
                return h.value
        return ""

    # ---- Subtest A: HTTP 403 InvalidStatus classifies as ClientException & stops reconnect ----
    async def reject_403_process_request(_conn, _req):
        return websockets.http11.Response(
            403,
            "Forbidden",
            websockets.Headers({"handshake-status": "403", "handshake-msg": "bad credentials"}),
            b"",
        )

    async with websockets.serve(lambda ws: ws.wait_closed(), "127.0.0.1", 0, process_request=reject_403_process_request) as srv_403:
        port_403 = srv_403.sockets[0].getsockname()[1]
        adapter_403 = FeishuAdapter("bot-403")
        adapter_403.capabilities = replace(FeishuAdapter.capabilities, card_callback_sla_sec=1.0)
        monkeypatch.setattr(
            lark.ws.Client,
            "_get_conn_url",
            lambda _self: f"ws://127.0.0.1:{port_403}/ws?device_id=dev403&service_id=403",
        )
        monkeypatch.setattr(adapter_403, "_fetch_bot_info", lambda: asyncio.sleep(0))
        await adapter_403.start(
            {"enabled": True, "credentials": {"app_id": "cli_403", "app_secret": "sec_403"}},
            lambda _ev: asyncio.sleep(0),
            lambda _ev: asyncio.sleep(0),
        )
        st_403 = await adapter_403.status()
        assert st_403["connected"] is False
        assert st_403["reconnecting"] is False
        assert "ClientException" in (st_403["error"] or "")
        await adapter_403.stop()

    # ---- Subtest B: HTTP 514 startup failure -> auto_reconnect -> EVENT + CARD frames -> drop -> reconnect -> clean stop ----
    attempts = 0
    server_connections: list[Any] = []
    connected_events: list[asyncio.Event] = [asyncio.Event(), asyncio.Event()]
    ack_frames: list[Frame] = []
    recv_events: list[InboundEvent] = []
    recv_cards: list[CardActionEvent] = []

    async def process_request_flaky(_conn, _req):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return websockets.http11.Response(
                514,
                "Auth Retry",
                websockets.Headers({"handshake-status": "514", "handshake-msg": "server busy"}),
                b"",
            )
        return None

    async def ws_handler(ws):
        idx = len(server_connections)
        server_connections.append(ws)
        if idx < len(connected_events):
            connected_events[idx].set()
        try:
            async for raw in ws:
                frame = Frame()
                frame.ParseFromString(raw)
                if frame.method == FrameType.DATA.value:
                    ack_frames.append(frame)
        except websockets.exceptions.ConnectionClosed:
            pass

    async with websockets.serve(ws_handler, "127.0.0.1", 0, process_request=process_request_flaky) as srv:
        port = srv.sockets[0].getsockname()[1]
        adapter = FeishuAdapter("bot-live-ws")
        monkeypatch.setattr(
            lark.ws.Client,
            "_get_conn_url",
            lambda _self: f"ws://127.0.0.1:{port}/ws?device_id=devlive&service_id=101",
        )

        # Speed up lark_oapi's reconnect backoff for test determinism.
        orig_ws_init = lark.ws.Client.__init__

        def fast_ws_init(self, *args, **kwargs):
            orig_ws_init(self, *args, **kwargs)
            self._reconnect_nonce = 0
            self._reconnect_interval = 0.05

        monkeypatch.setattr(lark.ws.Client, "__init__", fast_ws_init)
        monkeypatch.setattr(adapter, "_fetch_bot_info", lambda: asyncio.sleep(0))

        async def on_msg(ev: InboundEvent):
            recv_events.append(ev)

        async def on_card(ev: CardActionEvent):
            recv_cards.append(ev)
            return {"title": "已批准", "desc": "集群：prod", "status": "已批准", "toast_type": "success", "toast": "已批准"}

        try:
            await adapter.start(
                {"enabled": True, "credentials": {"app_id": "cli_live", "app_secret": "sec_live"}},
                on_msg,
                on_card,
            )
            ws_loop = feishu_mod._WS_LOOP
            assert ws_loop is not None
            ws_loop.set_exception_handler(lambda _loop, ctx: ws_exceptions.append(ctx))

            # Initial connect hit 514 ServerException -> scheduled background reconnect!
            st_init = await adapter.status()
            assert st_init["connected"] is False
            assert st_init["reconnecting"] is True

            await asyncio.wait_for(connected_events[0].wait(), timeout=3.0)
            # Wait briefly for _patched_connect to update state after handshake
            for _ in range(30):
                if (await adapter.status())["connected"]:
                    break
                await asyncio.sleep(0.02)
            assert (await adapter.status())["connected"] is True
            assert (await adapter.status())["reconnecting"] is False

            # Send MessageType.EVENT frame (im.message.receive_v1)
            event_frame = Frame()
            event_frame.SeqID = 1
            event_frame.LogID = 1
            event_frame.service = 101
            event_frame.method = FrameType.DATA.value
            _add_hdr(event_frame, "type", MessageType.EVENT.value)
            _add_hdr(event_frame, "message_id", "msg_ws_evt_1")
            _add_hdr(event_frame, "trace_id", "tr_1")
            _add_hdr(event_frame, "sum", "1")
            _add_hdr(event_frame, "seq", "0")
            event_frame.payload = json.dumps(
                {
                    "schema": "2.0",
                    "header": {"event_id": "evt_1", "event_type": "im.message.receive_v1", "token": "t"},
                    "event": {
                        "sender": {"sender_type": "user", "sender_id": {"open_id": "ou_ws_1"}},
                        "message": {
                            "message_id": "om_ws_1",
                            "chat_id": "",
                            "chat_type": "p2p",
                            "message_type": "text",
                            "content": json.dumps({"text": "查看集群"}),
                        },
                    },
                }
            ).encode("utf-8")
            await server_connections[0].send(event_frame.SerializeToString())

            # Send MessageType.CARD frame (card.action.trigger) -> previously silently dropped by lark-oapi==1.5.5!
            card_frame = Frame()
            card_frame.SeqID = 2
            card_frame.LogID = 2
            card_frame.service = 101
            card_frame.method = FrameType.DATA.value
            _add_hdr(card_frame, "type", MessageType.CARD.value)
            _add_hdr(card_frame, "message_id", "msg_ws_card_1")
            _add_hdr(card_frame, "trace_id", "tr_2")
            _add_hdr(card_frame, "sum", "1")
            _add_hdr(card_frame, "seq", "0")
            card_frame.payload = json.dumps(
                {
                    "schema": "2.0",
                    "header": {"event_id": "evt_card_ws_1", "event_type": "card.action.trigger", "token": "t"},
                    "event": {
                        "operator": {"open_id": "ou_ws_1"},
                        "token": "tok_ws_1",
                        "action": {
                            "value": {"action_id": "approval:approve:ref_ws", "card_ref": "ref_ws"},
                            "tag": "button",
                        },
                        "context": {"open_message_id": "om_card_ws", "open_chat_id": "oc_ws"},
                    },
                }
            ).encode("utf-8")
            await server_connections[0].send(card_frame.SerializeToString())

            for _ in range(50):
                if len(ack_frames) >= 2 and len(recv_events) >= 1 and len(recv_cards) >= 1:
                    break
                await asyncio.sleep(0.03)

            assert len(recv_events) == 1
            assert recv_events[0].text == "查看集群"
            assert len(recv_cards) == 1
            assert recv_cards[0].action_id == "approval:approve:ref_ws"

            acks_by_id = {_get_hdr(f, "message_id"): json.loads(f.payload.decode("utf-8")) for f in ack_frames}
            assert acks_by_id["msg_ws_evt_1"]["code"] == http.HTTPStatus.OK
            assert acks_by_id["msg_ws_card_1"]["code"] == http.HTTPStatus.OK
            card_resp = json.loads(base64.b64decode(acks_by_id["msg_ws_card_1"]["data"]).decode("utf-8"))
            assert card_resp["toast"]["content"] == "已批准"
            assert card_resp["card"]["data"]["config"] == {"width_mode": "fill", "update_multi": True}

            # Drop server connection -> state reflects disconnect/reconnect, then reconnects cleanly.
            await server_connections[0].close(code=1011, reason="sim drop")
            await asyncio.wait_for(connected_events[1].wait(), timeout=3.0)
            for _ in range(30):
                if (await adapter.status())["connected"]:
                    break
                await asyncio.sleep(0.02)
            assert (await adapter.status())["connected"] is True

            # Clean shutdown must produce ZERO unretrieved task exceptions!
            await adapter.stop()
            await asyncio.sleep(0.05)
            st_stopped = await adapter.status()
            assert st_stopped["connected"] is False
            assert st_stopped["reconnecting"] is False
            assert main_exceptions == []
            assert ws_exceptions == []
        finally:
            await adapter.stop()
            main_loop.set_exception_handler(orig_main_handler)
            if feishu_mod._WS_LOOP is not None:
                feishu_mod._WS_LOOP.set_exception_handler(None)


@pytest.mark.asyncio
async def test_feishu_approval_sla_boundary_race_still_updates_card_and_watches_run():
    import uuid
    from dataclasses import replace
    from datetime import datetime, timedelta, timezone
    from kubedoor_ai.domain import CredentialCipher, Identity
    from kubedoor_ai.im.engine import IMBotEngine
    from kubedoor_ai.im.models import BotConfig
    from kubedoor_ai.im.shared_model import SharedModelManager
    from kubedoor_ai.service import Service
    from test_im_engine import _FakeGateway, _FakeIMStore, _FakeRuntime, _bootstrap_helper

    TEST_KEY = "0123456789abcdef" * 4
    cipher = CredentialCipher(TEST_KEY)

    def _setup_case(delay_store: float, delay_decide: float, sla_sec: float, delay_delete: float = 0.0):
        store = _FakeIMStore()
        orig_one = store.one
        orig_execute = store.execute

        async def slow_one(sql: str, *args):
            if "FROM kubedoor_ai_im_approvals WHERE bot_id" in sql and delay_store > 0:
                await asyncio.sleep(delay_store)
            return await orig_one(sql, *args)

        async def slow_execute(sql: str, *args):
            if "DELETE FROM kubedoor_ai_im_approvals" in sql and delay_delete > 0:
                await asyncio.sleep(delay_delete)
            return await orig_execute(sql, *args)

        store.one = slow_one
        store.execute = slow_execute
        identities = {"alice": {"username": "alice", "permission": "rw", "clusters": {"prod": "rw"}}}
        gateway = _FakeGateway(cipher, identities)
        runtime = _FakeRuntime(store)
        svc = Service(store, gateway, runtime)
        svc.bootstrap = lambda ident: _bootstrap_helper(gateway, ident)
        mgr = SharedModelManager(svc)
        mgr.config = {"base_url": "https://llm.example.com/v1", "api_key": "sk-test", "model": "gpt-4o"}

        sid = str(uuid.uuid4())
        rid = str(uuid.uuid4())
        aid = str(uuid.uuid4())
        card_ref = f"ref_{uuid.uuid4().hex[:8]}"
        store.users[("00000000-0000-4000-8000-000000000055", "ou_approver")] = {
            "bot_id": "00000000-0000-4000-8000-000000000055",
            "external_user_id": "ou_approver",
            "platform": "feishu",
            "username": "alice",
            "default_env": "prod",
            "current_env": "prod",
            "session_id": sid,
            "previous_session_id": None,
        }
        store.runs[rid] = {"id": rid, "session_id": sid, "username": "alice", "scope": {"env": "prod"}, "status": "running", "event_seq": 0, "usage": {}, "switch_env": None}
        store.actions[aid] = {"id": aid, "run_id": rid, "operation": "restart_deployment", "arguments": {}, "preview": {}, "expires_at": datetime.now(timezone.utc) + timedelta(minutes=10), "state": "pending"}
        store.approvals[("00000000-0000-4000-8000-000000000055", card_ref)] = {
            "bot_id": "00000000-0000-4000-8000-000000000055",
            "card_ref": card_ref,
            "action_id": aid,
            "run_id": rid,
            "external_user_id": "ou_approver",
            "username": "alice",
            "session_id": sid,
            "resume_after_seq": 0,
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=10),
        }

        async def slow_decision(_identity, _run_id, _body):
            if delay_decide > 0:
                await asyncio.sleep(delay_decide)
            store.runs[rid]["status"] = "completed"
            store.events.append({"run_id": rid, "seq": 1, "type": "message", "data": {"content": "重启已完成"}})
            store.events.append({"run_id": rid, "seq": 2, "type": "done", "data": {"usage": {}}})
            store.runs[rid]["event_seq"] = 2
            return {"status": "running"}

        svc.decision = slow_decision

        adapter = FeishuAdapter("00000000-0000-4000-8000-000000000055")
        adapter._main_loop = asyncio.get_running_loop()
        adapter.capabilities = replace(FeishuAdapter.capabilities, card_callback_sla_sec=sla_sec, stream_throttle_ms=20)
        client, records = _make_fake_client(create_card_ok=False, patch_ok=True)
        adapter.client = client

        bot = BotConfig(
            id="00000000-0000-4000-8000-000000000055",
            platform="feishu",
            name="fs",
            enabled=True,
        )
        engine = IMBotEngine(svc, bot, adapter, shared_model=mgr)
        adapter._on_card_action = engine.handle_card_action

        trigger = SimpleNamespace(
            header=SimpleNamespace(event_id=f"evt_{card_ref}"),
            event=SimpleNamespace(
                token="tok_sla",
                operator=SimpleNamespace(open_id="ou_approver"),
                action=SimpleNamespace(
                    name="btn",
                    value={"action_id": f"approval:approve:{card_ref}", "card_ref": card_ref},
                    option=None,
                    options=None,
                ),
                context=SimpleNamespace(open_message_id="om_approval_card_1", open_chat_id="oc_1"),
            ),
        )
        return adapter, engine, records, trigger

    # Case A: slow _decide right around deadline (0.18s vs 0.22s SLA -> inner deadline ~0.165s triggers _finish_decision)
    adapter_a, engine_a, records_a, trigger_a = _setup_case(delay_store=0.0, delay_decide=0.18, sla_sec=0.22)
    resp_a = await asyncio.to_thread(adapter_a._on_card_action_sync, trigger_a)
    assert resp_a.toast.content == "处理中…"
    await asyncio.sleep(0.05)
    await asyncio.wait_for(asyncio.gather(*list(engine_a._tasks)), timeout=3.0)
    patches_a = [p for n, p in records_a if n == "im.message.patch" and p["message_id"] == "om_approval_card_1"]
    assert len(patches_a) >= 1
    assert "已处理" in patches_a[-1]["content"]["body"]["elements"][0]["content"]
    assert any(n in ("im.message.create", "im.message.patch") and "重启已完成" in str(p["content"]) for n, p in records_a)

    # Case B: slow pre-decision DB lookup exceeds sync fut.result(timeout=0.18) -> sync_timed_out fires ->
    # fast _decide finishes AFTER sync timeout -> IMBotEngine MUST STILL patch approval card & run _watch_run!
    adapter_b, engine_b, records_b, trigger_b = _setup_case(delay_store=0.24, delay_decide=0.01, sla_sec=0.18)
    resp_b = await asyncio.to_thread(adapter_b._on_card_action_sync, trigger_b)
    assert resp_b.toast.content == "处理中…"
    assert resp_b.card is None
    # Wait for background engine handler + spawned _watch_run / _finish_pending_approval tasks
    for _ in range(60):
        patches_b = [p for n, p in records_b if n == "im.message.patch" and p["message_id"] == "om_approval_card_1"]
        pushed_b = [p for n, p in records_b if n in ("im.message.create", "im.message.patch") and "重启已完成" in str(p["content"])]
        if patches_b and pushed_b:
            break
        await asyncio.sleep(0.03)
    if engine_b._tasks:
        await asyncio.wait_for(asyncio.gather(*list(engine_b._tasks)), timeout=3.0)
    patches_b = [p for n, p in records_b if n == "im.message.patch" and p["message_id"] == "om_approval_card_1"]
    pushed_b = [p for n, p in records_b if n in ("im.message.create", "im.message.patch") and "重启已完成" in str(p["content"])]
    assert len(patches_b) >= 1
    assert any("已处理" in el["content"] or "已批准" in str(patches_b[-1]["content"]) for el in patches_b[-1]["content"]["body"]["elements"])
    assert len(pushed_b) >= 1

    # Case C: _decide finishes inside inner budget, then slow DELETE pushes total time past sync SLA ->
    # fast-path claim_sync() returns False and STILL patches approval card & runs _watch_run!
    adapter_c, engine_c, records_c, trigger_c = _setup_case(delay_store=0.0, delay_decide=0.08, sla_sec=0.18, delay_delete=0.14)
    resp_c = await asyncio.to_thread(adapter_c._on_card_action_sync, trigger_c)
    assert resp_c.toast.content == "处理中…"
    assert resp_c.card is None
    for _ in range(60):
        patches_c = [p for n, p in records_c if n == "im.message.patch" and p["message_id"] == "om_approval_card_1"]
        pushed_c = [p for n, p in records_c if n in ("im.message.create", "im.message.patch") and "重启已完成" in str(p["content"])]
        if patches_c and pushed_c:
            break
        await asyncio.sleep(0.03)
    if engine_c._tasks:
        await asyncio.wait_for(asyncio.gather(*list(engine_c._tasks)), timeout=3.0)
    patches_c = [p for n, p in records_c if n == "im.message.patch" and p["message_id"] == "om_approval_card_1"]
    pushed_c = [p for n, p in records_c if n in ("im.message.create", "im.message.patch") and "重启已完成" in str(p["content"])]
    assert len(patches_c) >= 1
    assert len(pushed_c) >= 1
