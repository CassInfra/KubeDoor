"""HTTP integration tests for /api/ai/shared-model*, /api/ai/im/*, log-describe, and legacy /wecom* bridge."""
from __future__ import annotations

import json
import os
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.testclient import TestClient

sys.path.insert(0, os.path.dirname(__file__))

from kubedoor_ai.domain import AIError, CredentialCipher
from kubedoor_ai.http import create_app
from kubedoor_ai.service import Service


TEST_TOKEN = "http-test-internal-token"
TEST_KEY = "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"


def _auth_headers(username: str = "admin", permission: str = "admin", clusters: dict | None = None) -> dict[str, str]:
    return {
        "x-kubedoor-token": TEST_TOKEN,
        "x-user-name": username,
        "x-user-permission": permission,
        "x-user-clusters": json.dumps(clusters or {}),
    }


class _FakeTx:
    def __init__(self, store: "_FakeHTTPStore") -> None:
        self.store = store

    async def execute(self, sql: str, *args):
        return await self.store.execute(sql, *args)

    async def one(self, sql: str, *args):
        return await self.store.one(sql, *args)

    async def fetch(self, sql: str, *args):
        return await self.store.fetch(sql, *args)


class _FakeHTTPStore:
    def __init__(self) -> None:
        self.shared_row: dict | None = None
        self.legacy_row: dict | None = None
        self.bots: dict[str, dict] = {}
        self.users: dict[tuple[str, str], dict] = {}
        self.bind_codes: dict[str, dict] = {}
        self.approvals: dict[tuple[str, str], dict] = {}
        self.sessions: dict[str, dict] = {}
        self.runs: dict[str, dict] = {}

    @asynccontextmanager
    async def transaction(self):
        yield _FakeTx(self)

    async def summaries(self):
        return []

    async def execute(self, sql: str, *args):
        now = datetime.now(timezone.utc)
        if "DELETE FROM kubedoor_ai_im_bind_codes WHERE expires_at<now()" in sql:
            for code in list(self.bind_codes):
                if self.bind_codes[code]["expires_at"] < now:
                    self.bind_codes.pop(code, None)
            return "DELETE"
        if "UPDATE kubedoor_ai_im_bots SET unbound_username=$2,unbound_default_env=$3" in sql:
            bot_id, uname, env, by = str(args[0]), args[1], args[2], args[3]
            self.bots[bot_id].update(unbound_username=uname, unbound_default_env=env, updated_by=by, updated_at=now)
            return "UPDATE 1"
        if "INSERT INTO kubedoor_ai_im_users" in sql:
            bot_id, userid, platform, uname, env, by = str(args[0]), str(args[1]), args[2], args[3], args[4], args[5]
            key = (bot_id, userid)
            existing = self.users.get(key)
            self.users[key] = {
                "bot_id": bot_id,
                "external_user_id": userid,
                "platform": platform,
                "username": uname,
                "default_env": env,
                "current_env": existing.get("current_env") if existing and existing.get("username") == uname else None,
                "session_id": existing.get("session_id") if existing else None,
                "previous_session_id": existing.get("previous_session_id") if existing else None,
                "remark": existing.get("remark") if existing else None,
                "self_name": existing.get("self_name") if existing else None,
                "bound_by": by if not existing or existing.get("username") != uname else existing.get("bound_by"),
                "bound_at": now if not existing or existing.get("username") != uname else existing.get("bound_at"),
                "first_seen_at": existing.get("first_seen_at", now) if existing else now,
                "last_seen_at": now,
                "updated_at": now,
            }
            return "INSERT"
        if "UPDATE kubedoor_ai_im_users SET username=NULL" in sql:
            key = (str(args[0]), str(args[1]))
            if key in self.users:
                self.users[key].update(username=None, default_env=None, current_env=None, bound_by=None, bound_at=None, updated_at=now)
            return "UPDATE"
        if "DELETE FROM kubedoor_ai_im_approvals WHERE bot_id=$1::uuid AND external_user_id=$2" in sql:
            bot_id, userid = str(args[0]), str(args[1])
            for k in [k for k, v in self.approvals.items() if v["bot_id"] == bot_id and v["external_user_id"] == userid]:
                self.approvals.pop(k, None)
            return "DELETE"
        return "OK"

    async def one(self, sql: str, *args):
        now = datetime.now(timezone.utc)
        if "FROM kubedoor_ai_shared_model" in sql:
            return dict(self.shared_row) if self.shared_row else None
        if "FROM kubedoor_ai_wecom_config" in sql:
            return dict(self.legacy_row) if self.legacy_row else None
        if "INSERT INTO kubedoor_ai_shared_model" in sql:
            self.shared_row = {"singleton": True, "encrypted_config": args[0], "updated_by": args[1], "updated_at": now}
            return dict(self.shared_row)
        if "INSERT INTO kubedoor_ai_im_bots" in sql:
            bot_id, platform, name, enabled, encrypted, u_user, u_env, idle, gp, allowed, by = args
            rec = {
                "id": str(bot_id),
                "platform": platform,
                "name": name,
                "enabled": enabled,
                "encrypted_config": encrypted,
                "unbound_username": u_user,
                "unbound_default_env": u_env,
                "session_idle_minutes": idle,
                "group_policy": gp,
                "allowed_chat_ids": list(allowed),
                "updated_by": by,
                "updated_at": now,
            }
            self.bots[str(bot_id)] = rec
            return dict(rec)
        if "UPDATE kubedoor_ai_im_bots SET name=$2" in sql:
            bot_id, name, enabled, encrypted, u_user, u_env, idle, gp, allowed, by = args
            rec = self.bots.get(str(bot_id))
            if not rec:
                return None
            rec.update(
                name=name,
                enabled=enabled,
                encrypted_config=encrypted,
                unbound_username=u_user,
                unbound_default_env=u_env,
                session_idle_minutes=idle,
                group_policy=gp,
                allowed_chat_ids=list(allowed),
                updated_by=by,
                updated_at=now,
            )
            return dict(rec)
        if "SELECT * FROM kubedoor_ai_im_bots WHERE id=$1::uuid" in sql:
            rec = self.bots.get(str(args[0]))
            return dict(rec) if rec else None
        if "SELECT id FROM kubedoor_ai_im_bots WHERE id=$1::uuid" in sql:
            return {"id": str(args[0])} if str(args[0]) in self.bots else None
        if "SELECT id,platform FROM kubedoor_ai_im_bots WHERE id=$1::uuid" in sql:
            rec = self.bots.get(str(args[0]))
            return {"id": rec["id"], "platform": rec["platform"]} if rec else None
        if "DELETE FROM kubedoor_ai_im_bots WHERE id=$1::uuid RETURNING id" in sql:
            rec = self.bots.pop(str(args[0]), None)
            return {"id": rec["id"]} if rec else None
        if "SELECT id FROM kubedoor_ai_im_bots WHERE enabled=true LIMIT 1" in sql:
            for b in self.bots.values():
                if b["enabled"]:
                    return {"id": b["id"]}
            return None
        if "UPDATE kubedoor_ai_im_users SET remark=$3" in sql:
            key = (str(args[0]), str(args[1]))
            if key not in self.users:
                return None
            self.users[key]["remark"] = args[2]
            return {"external_user_id": key[1]}
        if "DELETE FROM kubedoor_ai_im_users WHERE bot_id=$1::uuid AND external_user_id=$2 RETURNING external_user_id" in sql:
            rec = self.users.pop((str(args[0]), str(args[1])), None)
            return {"external_user_id": str(args[1])} if rec else None
        if "INSERT INTO kubedoor_ai_im_bind_codes" in sql:
            code, username, mins = str(args[0]), str(args[1]), int(args[2])
            for k in [k for k, v in self.bind_codes.items() if v["username"] == username]:
                self.bind_codes.pop(k, None)
            exp = now + timedelta(minutes=mins)
            self.bind_codes[code] = {"code": code, "username": username, "expires_at": exp}
            return {"code": code, "expires_at": exp}
        if "UPDATE kubedoor_ai_im_users SET username=NULL" in sql and "AND username=$3 RETURNING" in sql:
            key = (str(args[0]), str(args[1]))
            rec = self.users.get(key)
            if not rec or rec.get("username") != args[2]:
                return None
            rec.update(username=None, default_env=None, current_env=None, bound_by=None, bound_at=None, updated_at=now)
            return {"external_user_id": key[1]}
        return None

    async def fetch(self, sql: str, *args):
        if "SELECT * FROM kubedoor_ai_im_bots" in sql:
            return list(self.bots.values())
        if "SELECT id,name,platform,enabled FROM kubedoor_ai_im_bots" in sql:
            return [{"id": b["id"], "name": b["name"], "platform": b["platform"], "enabled": b["enabled"]} for b in self.bots.values()]
        if "FROM kubedoor_ai_im_users u JOIN kubedoor_ai_im_bots b ON b.id=u.bot_id WHERE u.username=$1" in sql:
            uname = args[0]
            out = []
            for u in self.users.values():
                if u.get("username") == uname and u["bot_id"] in self.bots:
                    out.append(
                        {
                            "bot_id": u["bot_id"],
                            "bot_name": self.bots[u["bot_id"]]["name"],
                            "platform": u["platform"],
                            "external_user_id": u["external_user_id"],
                            "self_name": u.get("self_name"),
                            "bound_by": u.get("bound_by"),
                            "bound_at": u.get("bound_at"),
                            "last_seen_at": u.get("last_seen_at"),
                        }
                    )
            return out
        if "FROM kubedoor_ai_im_users u JOIN kubedoor_ai_im_bots b" in sql:
            bot_filter = str(args[0]) if args else None
            out = []
            for u in self.users.values():
                if bot_filter and u["bot_id"] != bot_filter:
                    continue
                if u["bot_id"] not in self.bots:
                    continue
                out.append(
                    {
                        "bot_id": u["bot_id"],
                        "bot_name": self.bots[u["bot_id"]]["name"],
                        "platform": u["platform"],
                        "external_user_id": u["external_user_id"],
                        "username": u.get("username"),
                        "default_env": u.get("default_env"),
                        "current_env": u.get("current_env"),
                        "remark": u.get("remark"),
                        "self_name": u.get("self_name"),
                        "bound_by": u.get("bound_by"),
                        "bound_at": u.get("bound_at"),
                        "first_seen_at": u.get("first_seen_at"),
                        "last_seen_at": u.get("last_seen_at"),
                        "pending_approvals": 0,
                    }
                )
            return out
        return []


def _make_app_and_store():
    store = _FakeHTTPStore()
    cipher = CredentialCipher(TEST_KEY)
    identities = {
        "admin": {"username": "admin", "permission": "admin", "clusters": {}},
        "ops": {"username": "ops", "permission": "rw", "clusters": {"prod": "rw", "test": "read"}},
    }

    async def master(identity, method, path, query=None, body=None, allow_failure=False):
        if path == "/api/ai/internal/identity":
            rec = identities.get(identity.username)
            if not rec:
                raise AIError("not found", 403, "identity_invalid")
            return rec
        if path == "/api/ai/internal/bootstrap":
            return {"clusters": [{"env": "prod"}, {"env": "test"}]}
        raise AssertionError(f"Unexpected master path: {path}")

    gateway = SimpleNamespace(cipher=cipher, master=master)
    runtime = SimpleNamespace(
        model_factory=lambda provider: SimpleNamespace(
            ainvoke=AsyncMock(
                return_value=SimpleNamespace(
                    content='{"sources":[],"correlation_keys":[],"bindings":[],"cluster_notes":[],"architecture":""}'
                )
            )
        ),
        checkpointer=SimpleNamespace(adelete_thread=AsyncMock()),
        cancel=AsyncMock(),
    )
    svc = Service(store, gateway, runtime)
    svc.test_provider = AsyncMock(return_value={"success": True, "tool_calling": True, "usage_reported": True, "model": "gpt-4o", "base_url": "https://api.example.com/v1"})
    app = create_app(svc, token=TEST_TOKEN)
    # Replace adapter start so tests don't open real WebSockets.
    async def fake_start_adapter(_cfg, _on_msg, _on_card):
        return None
    orig_make = app.state.im_manager._make_adapter
    def stub_make(bot):
        adapter = orig_make(bot)
        async def _start(cfg, on_msg, on_card):
            if cfg.get("enabled"):
                adapter._state = {"configured": True, "connected": True, "authenticated": True, "reconnecting": False, "error": None}
            else:
                adapter._state = {"configured": False, "connected": False, "authenticated": False, "reconnecting": False, "error": None}
        adapter.start = _start
        return adapter
    app.state.im_manager._make_adapter = stub_make
    return app, store, svc


def test_http_shared_model_rbac_crud_test_and_log_describe():
    app, _, svc = _make_app_and_store()
    client = TestClient(app)

    # Non-admin denied on shared-model and im/bots
    assert client.get("/api/ai/shared-model", headers=_auth_headers("ops", "rw")).status_code == 403
    assert client.get("/api/ai/im/bots", headers=_auth_headers("ops", "rw")).status_code == 403
    assert client.get("/api/ai/im/users", headers=_auth_headers("ops", "rw")).status_code == 403

    # Initially empty -> log-sources/describe returns 409
    empty_res = client.get("/api/ai/shared-model", headers=_auth_headers()).json()
    assert empty_res["available"] is False
    assert client.post("/api/ai/log-sources/describe", headers=_auth_headers(), json={"sources": [{"id": "s1", "path": "/var/log/a.log"}]}).status_code == 409

    # Save shared-model (zero IM bots exist!)
    saved = client.put(
        "/api/ai/shared-model",
        headers=_auth_headers(),
        json={
            "base_url": "https://llm.example.com/v1",
            "api_key": "sk-http-secret-xyz",
            "model": "gpt-4.1",
            "max_input_tokens": 32000,
            "max_output_tokens": 2048,
            "compression_ratio": 0.7,
            "reasoning_effort": "low",
        },
    )
    assert saved.status_code == 200
    body = saved.json()
    assert body["available"] is True
    assert body["api_key_set"] is True
    assert "sk-http-secret-xyz" not in saved.text

    # Test shared-model with omitted api_key reuses stored secret
    tested = client.post(
        "/api/ai/shared-model/test",
        headers=_auth_headers(),
        json={"provider": {"base_url": "https://llm.example.com/v1", "api_key": "", "model": "gpt-4.1"}},
    )
    assert tested.status_code == 200
    assert tested.json()["success"] is True
    assert svc.test_provider.await_args.args[0]["api_key"] == "sk-http-secret-xyz"

    # Now log-sources/describe succeeds even with 0 IM bots enabled
    desc = client.post(
        "/api/ai/log-sources/describe",
        headers=_auth_headers(),
        json={
            "scan": {
                "groups": [
                    {
                        "ref": "g1",
                        "columns": [{"name": "ts", "type": "DateTime"}],
                        "targets": [{"target": "app.logs"}],
                    }
                ]
            }
        },
    )
    assert desc.status_code == 200
    assert desc.json()["model"] == "gpt-4.1"


def test_http_im_bots_users_binding_and_legacy_wecom_bridge(monkeypatch):
    app, store, _ = _make_app_and_store()
    client = TestClient(app)

    # Binding code fails with 409 before any bot is enabled
    assert client.post("/api/ai/im/binding/code", headers=_auth_headers("ops", "rw")).status_code == 409

    # Create Feishu bot with custom SSRF URL -> rejected 400
    ssrf_resp = client.post(
        "/api/ai/im/bots",
        headers=_auth_headers(),
        json={
            "platform": "feishu",
            "name": "Bad Feishu",
            "enabled": True,
            "credentials": {"app_id": "cli_1", "app_secret": "sec_1", "domain_preset": "custom", "custom_domain": "http://169.254.169.254"},
        },
    )
    assert ssrf_resp.status_code == 400
    assert ssrf_resp.json()["error"]["code"] == "invalid_custom_domain"

    # Create enabled Feishu bot -> verify "credentials" is NOT "[REDACTED]" and secrets are masked!
    monkeypatch.setattr("socket.getaddrinfo", lambda *a, **kw: [(2, 1, 6, "", ("93.184.216.34", 443))])
    create_resp = client.post(
        "/api/ai/im/bots",
        headers=_auth_headers(),
        json={
            "platform": "feishu",
            "name": "飞书生产助手",
            "enabled": True,
            "credentials": {
                "app_id": "cli_prod_123",
                "app_secret": "sec_top_secret_999",
                "domain_preset": "feishu",
            },
            "session_idle_minutes": 90,
            "group_policy": "mention",
            "allowed_chat_ids": ["oc_1", "oc_1", "oc_2"],
            "unbound_username": "ops",
            "unbound_default_env": "test",
            "model_override": {
                "base_url": "https://override.example.com/v1",
                "api_key": "sk-override-secret",
                "model": "qwen-max",
            },
        },
    )
    assert create_resp.status_code == 200
    bot = create_resp.json()
    bot_id = bot["id"]
    assert bot["platform"] == "feishu"
    assert bot["credentials"] == {"app_id": "cli_prod_123", "domain_preset": "feishu", "custom_domain": ""}
    assert bot["secret_set"] is True
    assert bot["allowed_chat_ids"] == ["oc_1", "oc_2"]
    assert bot["model_override"]["api_key_set"] is True
    assert "sec_top_secret_999" not in create_resp.text
    assert "sk-override-secret" not in create_resp.text

    # List bots keeps credentials dict intact
    list_resp = client.get("/api/ai/im/bots", headers=_auth_headers())
    assert list_resp.status_code == 200
    assert list_resp.json()["bots"][0]["credentials"]["app_id"] == "cli_prod_123"

    # Update bot: blank secret & blank model_override.api_key preserve saved secrets; unbound admin rejected
    assert (
        client.put(
            f"/api/ai/im/bots/{bot_id}/unbound",
            headers=_auth_headers(),
            json={"username": "admin", "default_env": "prod"},
        ).status_code
        == 400
    )
    upd = client.put(
        f"/api/ai/im/bots/{bot_id}",
        headers=_auth_headers(),
        json={
            "platform": "feishu",
            "name": "飞书生产助手-v2",
            "enabled": True,
            "credentials": {"app_id": "cli_prod_123", "domain_preset": "lark"},
            "group_policy": "allowlist",
            "allowed_chat_ids": ["oc_vip"],
            "model_override": {"base_url": "https://override.example.com/v1", "model": "qwen-max-latest"},
        },
    )
    assert upd.status_code == 200
    assert upd.json()["secret_set"] is True
    assert upd.json()["credentials"]["domain_preset"] == "lark"
    assert upd.json()["model_override"]["api_key_set"] is True
    assert app.state.im_manager.bots[bot_id].credentials["app_secret"] == "sec_top_secret_999"
    assert app.state.im_manager.bots[bot_id].model_override["api_key"] == "sk-override-secret"

    # Cannot switch platform after creation
    assert client.put(f"/api/ai/im/bots/{bot_id}", headers=_auth_headers(), json={"platform": "wecom", "name": "x"}).status_code == 400

    # User CRUD, remark, self-binding, purge
    assert client.post("/api/ai/im/users", headers=_auth_headers(), json={"bot_id": bot_id, "external_user_id": "ou_ops", "username": "ops", "default_env": "prod"}).status_code == 200
    assert client.put(f"/api/ai/im/users/{bot_id}/ou_ops/remark", headers=_auth_headers(), json={"remark": "值班SRE"}).status_code == 200
    users_list = client.get(f"/api/ai/im/users?bot_id={bot_id}", headers=_auth_headers()).json()["users"]
    assert len(users_list) == 1 and users_list[0]["remark"] == "值班SRE"

    # Non-admin user can query own binding & generate code & unbind self
    bind_state = client.get("/api/ai/im/binding", headers=_auth_headers("ops", "rw", {"prod": "rw"})).json()
    assert bind_state["enabled"] is True
    assert len(bind_state["members"]) == 1
    code_resp = client.post("/api/ai/im/binding/code", headers=_auth_headers("ops", "rw", {"prod": "rw"}))
    assert code_resp.status_code == 200
    assert len(code_resp.json()["code"]) == 6

    assert client.delete(f"/api/ai/im/binding/{bot_id}/ou_ops", headers=_auth_headers("ops", "rw")).status_code == 200
    assert client.post(f"/api/ai/im/users/{bot_id}/ou_ops/purge", headers=_auth_headers()).status_code == 200

    # Legacy /api/ai/wecom* bridge works transparently alongside generic IM bots
    wecom_cfg = client.put(
        "/api/ai/wecom",
        headers=_auth_headers(),
        json={
            "enabled": True,
            "bot_id": "ww-legacy-id",
            "bot_secret": "ww-legacy-secret",
            "base_url": "https://wecom-llm.example.com/v1",
            "api_key": "sk-wecom-bridge",
            "model": "gpt-4o",
        },
    )
    assert wecom_cfg.status_code == 200
    assert wecom_cfg.json()["bot_id"] == "ww-legacy-id"
    assert wecom_cfg.json()["bot_secret_set"] is True
    assert client.get("/api/ai/wecom/binding", headers=_auth_headers("ops", "rw")).status_code == 200

    # Delete Feishu bot cleanly
    assert client.delete(f"/api/ai/im/bots/{bot_id}", headers=_auth_headers()).status_code == 200
