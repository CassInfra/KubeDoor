"""Phase 1 P0 security hardening tests for kubedoor-agent (KD-P0-01, KD-P0-02, KD-P0-03)."""

from __future__ import annotations

import ast
import base64
import datetime
import importlib.util
import json
import os
import ssl
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from cryptography import x509

from conftest import AGENT_ROOT, load_module


@pytest.fixture
def internal_auth_mod():
    return load_module("phase1_internal_auth", AGENT_ROOT / "func_manager" / "internal_auth.py")


@pytest.fixture
def tls_certs_mod():
    return load_module("phase1_tls_certs", AGENT_ROOT / "func_manager" / "tls_certs.py")


@pytest.fixture
def ws_tls_mod():
    return load_module("phase1_ws_tls", AGENT_ROOT / "func_manager" / "ws_tls.py")


# ---------------------------------------------------------------------- internal_auth & entrypoint


def test_internal_auth_env_file_fallback_and_fail_closed(internal_auth_mod, tmp_path, monkeypatch):
    assert internal_auth_mod.HEADER == "X-Kubedoor-Internal-Token"
    assert internal_auth_mod.DEFAULT_TOKEN_FILE == "/run/kubedoor/internal.token"

    token_file = tmp_path / "internal.token"
    monkeypatch.delenv("KUBEDOOR_INTERNAL_TOKEN", raising=False)
    monkeypatch.setenv("KUBEDOOR_INTERNAL_TOKEN_FILE", str(token_file))

    # Missing token file -> fail-closed
    assert internal_auth_mod.load_internal_token() == ""
    assert internal_auth_mod.auth_headers() == {}
    assert internal_auth_mod.verify_internal_token("") is False
    assert internal_auth_mod.verify_internal_token(None) is False
    assert internal_auth_mod.verify_internal_token("anything") is False

    # File fallback works
    token_file.write_text("file-secret-token\n", encoding="utf-8")
    assert internal_auth_mod.load_internal_token() == "file-secret-token"
    assert internal_auth_mod.auth_headers() == {"X-Kubedoor-Internal-Token": "file-secret-token"}
    assert internal_auth_mod.verify_internal_token("file-secret-token") is True
    assert internal_auth_mod.verify_internal_token("wrong") is False
    assert internal_auth_mod.verify_internal_token(None) is False

    # Env overrides file
    monkeypatch.setenv("KUBEDOOR_INTERNAL_TOKEN", "  env-secret-token \n")
    assert internal_auth_mod.load_internal_token() == "env-secret-token"
    assert internal_auth_mod.verify_internal_token("env-secret-token") is True
    assert internal_auth_mod.verify_internal_token("file-secret-token") is False


def test_entrypoint_uses_lf_and_generates_runtime_token():
    raw = (AGENT_ROOT / "entrypoint.sh").read_bytes()
    assert b"\r" not in raw
    text = raw.decode("utf-8")
    assert "mkdir -p /run/kubedoor" in text
    assert "chmod 700 /run/kubedoor" in text
    assert "secrets.token_hex(32)" in text
    assert "chmod 600 /run/kubedoor/internal.token" in text
    assert 'exec "$@"' in text


# ---------------------------------------------------------------------- pod-mgr auth & bind


class _FakeK8sCtx:
    async def __aenter__(self):
        return SimpleNamespace(core_v1_api=object(), core_v1=object())

    async def __aexit__(self, *args):
        return False


@pytest.fixture
def isolated_pod_mgr(monkeypatch, internal_auth_mod):
    fake_utils = ModuleType("utils")
    fake_utils.OSS_URL = None
    fake_utils.send_msg = Mock()
    monkeypatch.setitem(sys.modules, "utils", fake_utils)
    oss_mod = load_module("phase1_oss_url", AGENT_ROOT / "func_manager" / "oss_url.py")
    package = ModuleType("func_manager")
    package.__path__ = [str(AGENT_ROOT / "func_manager")]
    package.oss_url = oss_mod
    package.internal_auth = internal_auth_mod
    monkeypatch.setitem(sys.modules, "func_manager", package)
    monkeypatch.setitem(sys.modules, "func_manager.oss_url", oss_mod)
    monkeypatch.setitem(sys.modules, "func_manager.internal_auth", internal_auth_mod)
    scheduler = ModuleType("func_manager.k8s_node_scheduler")
    scheduler.K8sNodeScheduler = object
    monkeypatch.setitem(sys.modules, "func_manager.k8s_node_scheduler", scheduler)
    manager = ModuleType("k8s_client_manager")
    manager.K8sClientManager = _FakeK8sCtx
    manager.load_incluster_config = lambda: None
    monkeypatch.setitem(sys.modules, "k8s_client_manager", manager)
    return load_module("phase1_pod_mgr", AGENT_ROOT / "pod-mgr.py")


def test_pod_mgr_auth_middleware_and_localhost_bind(isolated_pod_mgr, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("KUBEDOOR_INTERNAL_TOKEN", "pod-mgr-secret")
    monkeypatch.setenv("KUBEDOOR_INTERNAL_TOKEN_FILE", str(tmp_path / "missing"))

    with TestClient(isolated_pod_mgr.app) as client:
        unauth = client.get("/api/pod/task_status/probe-1")
        assert unauth.status_code == 403
        assert unauth.json() == {"success": False, "error": "forbidden"}

        bad = client.get("/api/pod/task_status/probe-1", headers={"X-Kubedoor-Internal-Token": "wrong"})
        assert bad.status_code == 403
        assert bad.json() == {"success": False, "error": "forbidden"}

        ok = client.get("/api/pod/task_status/probe-1", headers={"X-Kubedoor-Internal-Token": "pod-mgr-secret"})
        assert ok.status_code == 200
        assert ok.json() == {"status": "未找到"}

    source = (AGENT_ROOT / "pod-mgr.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    uvicorn_calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run"
        and getattr(node.func.value, "id", None) == "uvicorn"
    ]
    assert len(uvicorn_calls) == 1
    kw = {k.arg: ast.literal_eval(k.value) for k in uvicorn_calls[0].keywords if k.arg}
    assert kw["host"] == "127.0.0.1"
    assert kw["port"] == 81


@pytest.mark.asyncio
async def test_pod_mgr_scale_call_includes_internal_token(isolated_pod_mgr, monkeypatch):
    seen = {}

    def fake_post(url, json=None, headers=None, timeout=None, verify=None):
        seen["url"] = url
        seen["headers"] = dict(headers or {})
        return SimpleNamespace(status_code=200, json=lambda: {"success": True})

    monkeypatch.setenv("KUBEDOOR_INTERNAL_TOKEN", "scale-secret")
    monkeypatch.setattr(isolated_pod_mgr.requests, "post", fake_post)
    ok, err = await isolated_pod_mgr.scale_deployment_via_api("default", "api", 3)
    assert ok is True and err == ""
    assert seen["headers"]["X-Kubedoor-Internal-Token"] == "scale-secret"


# ---------------------------------------------------------------------- kubedoor-agent :443 middleware & cron


@pytest.fixture
def isolated_agent(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(AGENT_ROOT.parent / "kubedoor-tools"))
    monkeypatch.syspath_prepend(str(AGENT_ROOT))
    saved_modules = dict(sys.modules)
    try:
        for name in list(sys.modules):
            if name == "utils" or name.startswith(("func_manager", "res_manager", "scaler", "load_balance", "k8s_client_manager")):
                sys.modules.pop(name, None)
        agent = load_module("phase1_kubedoor_agent", AGENT_ROOT / "kubedoor-agent.py")
        yield agent
    finally:
        for name in list(sys.modules):
            if name not in saved_modules:
                sys.modules.pop(name, None)
        sys.modules.update(saved_modules)


@pytest.mark.asyncio
async def test_agent_https_middleware_auth_and_exemptions(isolated_agent, monkeypatch, tmp_path):
    monkeypatch.setenv("KUBEDOOR_INTERNAL_TOKEN", "agent-443-secret")
    monkeypatch.setenv("KUBEDOOR_INTERNAL_TOKEN_FILE", str(tmp_path / "missing"))

    isolated_agent.update_image_handler = AsyncMock(return_value=web.json_response({"ok": True}))
    isolated_agent.reboot_service = SimpleNamespace(reboot=AsyncMock(return_value=web.json_response({"ok": True})))
    isolated_agent.admis_service = SimpleNamespace(
        admis_switch=AsyncMock(return_value=web.json_response({"is_on": True})),
        admis_mutate=AsyncMock(return_value=web.json_response({"response": {"allowed": True}})),
    )
    isolated_agent.mcp_service = SimpleNamespace(
        get_deployment_pods=AsyncMock(return_value=web.json_response({"success": True})),
        get_namespace_events=AsyncMock(return_value=web.json_response({"success": True})),
        get_nodes_info=AsyncMock(return_value=web.json_response({"success": True})),
    )
    isolated_agent.balance_node_service = SimpleNamespace(balance_node=AsyncMock(return_value=web.json_response({"ok": True})))
    monkeypatch.setattr(
        isolated_agent.configmap_manager,
        "get_namespace_list",
        AsyncMock(return_value=web.json_response({"success": True, "data": ["kubedoor"]})),
    )

    app = await isolated_agent.create_https_app()
    async with TestClient(TestServer(app)) as client:
        # Exempt: GET /api/health and POST /api/admis
        r_health = await client.get("/api/health")
        assert r_health.status == 200
        assert (await r_health.json())["status"] == "healthy"

        r_admis = await client.post("/api/admis", json={"request": {"uid": "1"}})
        assert r_admis.status == 200
        assert (await r_admis.json())["response"]["allowed"] is True

        # Non-exempt methods on /api/health and /api/admis are still rejected
        assert (await client.post("/api/health")).status == 403
        assert (await client.get("/api/admis")).status == 403

        # Other routes reject missing or invalid token
        for method, path in (
            ("GET", "/api/nodes"),
            ("GET", "/api/agent/namespaces"),
            ("GET", "/api/admis_switch"),
            ("POST", "/api/scale"),
            ("POST", "/api/cron"),
        ):
            r_no_tok = await client.request(method, path)
            assert r_no_tok.status == 403
            assert await r_no_tok.json() == {"success": False, "error": "forbidden"}

            r_bad_tok = await client.request(method, path, headers={"X-Kubedoor-Internal-Token": "bad"})
            assert r_bad_tok.status == 403

        # Valid token passes
        r_ok = await client.get(
            "/api/agent/namespaces",
            params={"env": "prod"},
            headers={"X-Kubedoor-Internal-Token": "agent-443-secret"},
        )
        assert r_ok.status == 200
        assert await r_ok.json() == {"success": True, "data": ["kubedoor"]}


@pytest.mark.asyncio
async def test_cron_injects_internal_token_secret_and_pod_namespace(isolated_agent, monkeypatch):
    monkeypatch.setenv("POD_NAMESPACE", "team-ops")
    captured = {}

    async def fake_create_cronjob(*, namespace, body):
        captured["namespace"] = namespace
        captured["body"] = body

    isolated_agent.batch_v1 = SimpleNamespace(create_namespaced_cron_job=fake_create_cronjob)
    monkeypatch.setattr(isolated_agent.utils, "send_msg", Mock())

    payload = {
        "cron": "0 8 * * *",
        "type": "scale",
        "service": {"deployment_list": [{"namespace": "app-ns", "deployment_name": "api", "num": 3}]},
    }
    fake_request = SimpleNamespace(
        json=AsyncMock(return_value=payload),
        query={"temp": "true", "type": "cpu"},
    )
    resp = await isolated_agent.cron(fake_request)
    assert resp.status == 200
    assert captured["namespace"] == "team-ops"

    container = captured["body"].spec.job_template.spec.template.spec.containers[0]
    assert container.command[0:2] == ["sh", "-c"]
    assert "X-Kubedoor-Internal-Token: ${KUBEDOOR_INTERNAL_TOKEN}" in container.command[2]
    assert "${CRON_PAYLOAD}" in container.command[2] and "${CRON_URL}" in container.command[2]

    env_map = {item.name: item for item in container.env}
    assert env_map["CRONJOB_TYPE"].value == "cron"
    assert env_map["CRON_URL"].value == "https://kubedoor-agent.team-ops/api/scale?type=cpu&temp=true"
    assert json.loads(env_map["CRON_PAYLOAD"].value)["deployment_list"][0]["job_type"] == "cron"
    secret_ref = env_map["KUBEDOOR_INTERNAL_TOKEN"].value_from.secret_key_ref
    assert secret_ref.name == "kubedoor-agent-credentials"
    assert secret_ref.key == "KUBEDOOR_INTERNAL_TOKEN"
    assert secret_ref.optional is True


@pytest.mark.asyncio
async def test_cron_warns_when_internal_token_missing_on_image_only_upgrade(isolated_agent, monkeypatch):
    monkeypatch.setenv("POD_NAMESPACE", "kubedoor")
    monkeypatch.delenv("KUBEDOOR_INTERNAL_TOKEN", raising=False)
    warnings = []
    sink_id = isolated_agent.logger.add(
        lambda msg: warnings.append(str(msg)),
        level="WARNING",
    )
    try:
        isolated_agent.batch_v1 = SimpleNamespace(create_namespaced_cron_job=AsyncMock())
        monkeypatch.setattr(isolated_agent.utils, "send_msg", Mock())
        payload = {
            "cron": "0 8 * * *",
            "type": "scale",
            "service": {"deployment_list": [{"namespace": "app-ns", "deployment_name": "api", "num": 2}]},
        }
        req = SimpleNamespace(json=AsyncMock(return_value=payload), query={})
        resp = await isolated_agent.cron(req)
        assert resp.status == 200
        created_body = isolated_agent.batch_v1.create_namespaced_cron_job.await_args.kwargs["body"]
        env_map = {item.name: item for item in created_body.spec.job_template.spec.template.spec.containers[0].env}
        assert env_map["KUBEDOOR_INTERNAL_TOKEN"].value_from.secret_key_ref.optional is True
        assert any("KUBEDOOR_INTERNAL_TOKEN" in line and "修复 Agent" in line for line in warnings)
    finally:
        isolated_agent.logger.remove(sink_id)


# ---------------------------------------------------------------------- tls_certs & admis_service


def test_tls_certs_prefers_mounted_directory(tls_certs_mod, tmp_path, monkeypatch):
    mounted = tmp_path / "mounted"
    runtime = tmp_path / "runtime"
    mounted.mkdir()
    (mounted / "tls.crt").write_text("MOUNTED-CERT", encoding="utf-8")
    (mounted / "tls.key").write_text("MOUNTED-KEY", encoding="utf-8")
    (mounted / "ca.crt").write_text("MOUNTED-CA", encoding="utf-8")
    monkeypatch.setenv("WEBHOOK_CERT_DIR", str(mounted))
    monkeypatch.setenv("WEBHOOK_RUNTIME_CERT_DIR", str(runtime))

    crt, key, ca = tls_certs_mod.ensure_webhook_certs()
    assert (crt, key, ca) == (str(mounted / "tls.crt"), str(mounted / "tls.key"), str(mounted / "ca.crt"))
    assert tls_certs_mod.load_ca_bundle_b64() == base64.b64encode(b"MOUNTED-CA").decode("ascii")
    assert not runtime.exists()


@pytest.mark.asyncio
async def test_tls_certs_dynamic_generation_custom_ns_sans_and_permissions(tls_certs_mod, tmp_path, monkeypatch):
    assert not (AGENT_ROOT / "serving-certs").exists()
    utils_text = (AGENT_ROOT / "utils.py").read_text(encoding="utf-8")
    assert "BASE64CA" not in utils_text

    chmod_calls = []
    real_chmod = os.chmod

    def spy_chmod(path, mode):
        chmod_calls.append((Path(path).name, mode))
        return real_chmod(path, mode)

    monkeypatch.setattr(tls_certs_mod.os, "chmod", spy_chmod)
    monkeypatch.setenv("WEBHOOK_CERT_DIR", str(tmp_path / "absent"))
    runtime_a = tmp_path / "runtime-a"
    runtime_b = tmp_path / "runtime-b"
    monkeypatch.setenv("WEBHOOK_RUNTIME_CERT_DIR", str(runtime_a))
    monkeypatch.setenv("POD_NAMESPACE", "custom-ns")

    crt_a, key_a, ca_a = tls_certs_mod.ensure_webhook_certs()
    assert ("runtime-a", 0o700) in chmod_calls
    assert ("ca.key", 0o600) in chmod_calls
    assert ("tls.key", 0o600) in chmod_calls

    cert = x509.load_pem_x509_certificate(Path(crt_a).read_bytes())
    assert cert.public_key().key_size == 2048
    delta = cert.not_valid_after_utc - cert.not_valid_before_utc
    assert datetime.timedelta(days=364) <= delta <= datetime.timedelta(days=366)
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    dns_names = set(san.get_values_for_type(x509.DNSName))
    ips = {str(ip) for ip in san.get_values_for_type(x509.IPAddress)}
    assert {
        "kubedoor-agent",
        "kubedoor-agent.custom-ns",
        "kubedoor-agent.custom-ns.svc",
        "kubedoor-agent.custom-ns.svc.cluster.local",
        "localhost",
    } <= dns_names
    assert "127.0.0.1" in ips

    # TLS handshake with generated CA succeeds
    app = web.Application()
    app.router.add_get("/api/health", lambda _req: web.json_response({"ok": True}))
    server_ssl = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    server_ssl.load_cert_chain(crt_a, key_a)
    client_ssl = ssl.create_default_context(cafile=ca_a)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=server_ssl)
    await site.start()
    try:
        port = site._server.sockets[0].getsockname()[1]
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://127.0.0.1:{port}/api/health", ssl=client_ssl) as resp:
                assert resp.status == 200
    finally:
        await runner.cleanup()

    # Second instance produces unique CA
    monkeypatch.setenv("WEBHOOK_RUNTIME_CERT_DIR", str(runtime_b))
    _, _, ca_b = tls_certs_mod.ensure_webhook_certs()
    assert Path(ca_a).read_bytes() != Path(ca_b).read_bytes()


@pytest.mark.asyncio
async def test_admis_service_uses_dynamic_namespace_and_ca_bundle(admission_module, tmp_path, monkeypatch):
    monkeypatch.setenv("POD_NAMESPACE", "team-ops")
    monkeypatch.setenv("WEBHOOK_CERT_DIR", str(tmp_path / "absent"))
    monkeypatch.setenv("WEBHOOK_RUNTIME_CERT_DIR", str(tmp_path / "runtime"))

    created_bodies = []
    deleted_names = []
    patched_namespaces = []

    admission_api = SimpleNamespace(
        create_mutating_webhook_configuration=AsyncMock(
            side_effect=lambda body: created_bodies.append(body) or SimpleNamespace(metadata=SimpleNamespace(name=body.metadata.name))
        ),
        delete_mutating_webhook_configuration=AsyncMock(side_effect=lambda name: deleted_names.append(name)),
    )
    core_v1 = SimpleNamespace(
        patch_namespace=AsyncMock(
            side_effect=lambda name, body: patched_namespaces.append((name, body)) or SimpleNamespace(metadata=SimpleNamespace(labels={}))
        )
    )
    svc = admission_module.AdmisService(None, core_v1, admission_api, {})

    assert await svc._create_mutating_webhook() is None
    assert len(created_bodies) == 1
    client_cfg = created_bodies[0].webhooks[0].client_config
    assert client_cfg.service.namespace == "team-ops"
    assert client_cfg.ca_bundle == admission_module.tls_certs.load_ca_bundle_b64(namespace="team-ops")
    assert [ns for ns, _ in patched_namespaces] == ["kube-system", "team-ops"]

    patched_namespaces.clear()
    assert await svc._delete_mutating_webhook() is None
    assert deleted_names == ["kubedoor-admis-configuration"]
    assert [ns for ns, _ in patched_namespaces] == ["kube-system", "team-ops"]


@pytest.mark.asyncio
async def test_admis_ensure_webhook_ca_bundle_syncs_drift_and_noops(admission_module, tmp_path, monkeypatch):
    from kubernetes_asyncio.client.rest import ApiException

    monkeypatch.setenv("POD_NAMESPACE", "team-ops")
    monkeypatch.setenv("WEBHOOK_CERT_DIR", str(tmp_path / "absent"))
    monkeypatch.setenv("WEBHOOK_RUNTIME_CERT_DIR", str(tmp_path / "runtime"))
    expected_ca = admission_module.tls_certs.load_ca_bundle_b64(namespace="team-ops")

    # admission_api is None -> no-op
    svc_no_api = admission_module.AdmisService(None, None, None, {})
    assert await svc_no_api.ensure_webhook_ca_bundle() is False

    # 404 -> no-op without creating
    create_mock = AsyncMock()
    patch_mock = AsyncMock()
    api_404 = SimpleNamespace(
        read_mutating_webhook_configuration=AsyncMock(side_effect=ApiException(status=404)),
        create_mutating_webhook_configuration=create_mock,
        patch_mutating_webhook_configuration=patch_mock,
    )
    assert await admission_module.AdmisService(None, None, api_404, {}).ensure_webhook_ca_bundle() is False
    create_mock.assert_not_awaited()
    patch_mock.assert_not_awaited()

    # Matching caBundle -> no-op
    matching_cfg = SimpleNamespace(
        webhooks=[
            SimpleNamespace(
                name="kubedoor-admis.mutating.webhook",
                client_config=SimpleNamespace(ca_bundle=expected_ca),
            )
        ]
    )
    api_match = SimpleNamespace(
        read_mutating_webhook_configuration=AsyncMock(return_value=matching_cfg),
        patch_mutating_webhook_configuration=AsyncMock(),
    )
    assert await admission_module.AdmisService(None, None, api_match, {}).ensure_webhook_ca_bundle() is False
    api_match.patch_mutating_webhook_configuration.assert_not_awaited()

    # Drifted caBundle -> PATCH with expected caBundle
    drifted_cfg = SimpleNamespace(
        webhooks=[
            SimpleNamespace(
                name="kubedoor-admis.mutating.webhook",
                client_config=SimpleNamespace(ca_bundle="c3RhbGUtY2E="),
            )
        ]
    )
    api_drift = SimpleNamespace(
        read_mutating_webhook_configuration=AsyncMock(return_value=drifted_cfg),
        patch_mutating_webhook_configuration=AsyncMock(),
    )
    svc_drift = admission_module.AdmisService(None, None, api_drift, {})
    assert await svc_drift.ensure_webhook_ca_bundle() is True
    api_drift.patch_mutating_webhook_configuration.assert_awaited_once_with(
        name="kubedoor-admis-configuration",
        body={
            "webhooks": [
                {
                    "name": "kubedoor-admis.mutating.webhook",
                    "clientConfig": {"caBundle": expected_ca},
                }
            ]
        },
    )


@pytest.mark.asyncio
async def test_admis_switch_on_syncs_ca_bundle_when_already_opened(admission_module, tmp_path, monkeypatch):
    monkeypatch.setenv("POD_NAMESPACE", "kubedoor")
    monkeypatch.setenv("WEBHOOK_CERT_DIR", str(tmp_path / "absent"))
    monkeypatch.setenv("WEBHOOK_RUNTIME_CERT_DIR", str(tmp_path / "runtime"))
    expected_ca = admission_module.tls_certs.load_ca_bundle_b64(namespace="kubedoor")

    drifted_cfg = SimpleNamespace(
        webhooks=[
            SimpleNamespace(
                name="kubedoor-admis.mutating.webhook",
                client_config=SimpleNamespace(ca_bundle="b2xkLWNh"),
            )
        ]
    )
    admission_api = SimpleNamespace(
        read_mutating_webhook_configuration=AsyncMock(return_value=drifted_cfg),
        patch_mutating_webhook_configuration=AsyncMock(),
    )
    svc = admission_module.AdmisService(None, None, admission_api, {})
    resp = await svc.admis_switch(SimpleNamespace(query={"action": "on"}))
    assert resp.status == 200
    assert json.loads(resp.text) == {"message": "Webhook is already opened!", "success": True}
    admission_api.patch_mutating_webhook_configuration.assert_awaited_once_with(
        name="kubedoor-admis-configuration",
        body={
            "webhooks": [
                {
                    "name": "kubedoor-admis.mutating.webhook",
                    "clientConfig": {"caBundle": expected_ca},
                }
            ]
        },
    )


@pytest.mark.asyncio
async def test_start_https_server_syncs_ca_bundle_and_survives_errors(isolated_agent, tmp_path, monkeypatch):
    import asyncio as _asyncio

    monkeypatch.setenv("WEBHOOK_CERT_DIR", str(tmp_path / "absent"))
    monkeypatch.setenv("WEBHOOK_RUNTIME_CERT_DIR", str(tmp_path / "runtime"))

    isolated_agent.update_image_handler = AsyncMock()
    isolated_agent.reboot_service = SimpleNamespace(reboot=AsyncMock())
    ensure_mock = AsyncMock(side_effect=RuntimeError("apiserver transient 503"))
    isolated_agent.admis_service = SimpleNamespace(
        admis_switch=AsyncMock(),
        admis_mutate=AsyncMock(),
        ensure_webhook_ca_bundle=ensure_mock,
    )
    isolated_agent.mcp_service = SimpleNamespace(
        get_deployment_pods=AsyncMock(),
        get_namespace_events=AsyncMock(),
        get_nodes_info=AsyncMock(),
    )
    isolated_agent.balance_node_service = SimpleNamespace(balance_node=AsyncMock())

    fake_runner = SimpleNamespace(setup=AsyncMock())
    fake_site = SimpleNamespace(start=AsyncMock())
    monkeypatch.setattr(isolated_agent.web, "AppRunner", lambda _app: fake_runner)
    monkeypatch.setattr(isolated_agent.web, "TCPSite", lambda *_args, **_kwargs: fake_site)
    monkeypatch.setattr(isolated_agent.asyncio, "sleep", AsyncMock(side_effect=_asyncio.CancelledError))

    warnings = []
    sink_id = isolated_agent.logger.add(lambda m: warnings.append(str(m)), level="WARNING")
    try:
        with pytest.raises(_asyncio.CancelledError):
            await isolated_agent.start_https_server()
    finally:
        isolated_agent.logger.remove(sink_id)

    fake_site.start.assert_awaited_once()
    ensure_mock.assert_awaited_once()
    assert any("caBundle" in w and "apiserver transient 503" in w for w in warnings)


# ---------------------------------------------------------------------- ws_tls


def test_ws_tls_prepare_connection_and_redaction(ws_tls_mod):
    clean, headers = ws_tls_mod.prepare_master_ws_connection(
        "wss://agent-1:p%40ss%3Aword@master.example.com:8443",
        {"env": "prod", "ver": "2.6.0"},
    )
    assert clean == "wss://master.example.com:8443/ws?env=prod&ver=2.6.0"
    assert "@" not in clean
    decoded = base64.b64decode(headers["Authorization"].removeprefix("Basic ")).decode("utf-8")
    assert decoded == "agent-1:p@ss:word"

    plain_clean, plain_headers = ws_tls_mod.prepare_master_ws_connection("ws://master.local:8080/ws?env=dev")
    assert plain_clean == "ws://master.local:8080/ws?env=dev"
    assert plain_headers == {}

    masked = ws_tls_mod.redact_credentials("connect failed to wss://agent-1:secret-pw@master.example.com:8443/ws: timeout")
    assert "secret-pw" not in masked
    assert "master.example.com:8443/ws" in masked


def test_ws_tls_ssl_context_modes_and_agent_no_ssl_false(ws_tls_mod, tls_certs_mod, tmp_path, monkeypatch, caplog):
    assert ws_tls_mod.build_master_ssl_context("ws://master.example.com/ws") is None

    monkeypatch.delenv("MASTER_TLS_INSECURE_SKIP_VERIFY", raising=False)
    monkeypatch.delenv("MASTER_CA_CERT", raising=False)
    monkeypatch.delenv("MASTER_CA_CERT_DATA", raising=False)

    default_ctx = ws_tls_mod.build_master_ssl_context("wss://master.example.com/ws")
    assert default_ctx is not None
    assert default_ctx.verify_mode == ssl.CERT_REQUIRED
    assert default_ctx.check_hostname is True

    monkeypatch.setenv("WEBHOOK_CERT_DIR", str(tmp_path / "absent"))
    monkeypatch.setenv("WEBHOOK_RUNTIME_CERT_DIR", str(tmp_path / "certs"))
    _, _, ca_file = tls_certs_mod.ensure_webhook_certs(namespace="kubedoor")
    ca_pem = Path(ca_file).read_text(encoding="utf-8")

    monkeypatch.setenv("MASTER_CA_CERT", ca_file)
    file_ctx = ws_tls_mod.build_master_ssl_context("wss://master.example.com/ws")
    assert file_ctx.verify_mode == ssl.CERT_REQUIRED

    monkeypatch.delenv("MASTER_CA_CERT")
    monkeypatch.setenv("MASTER_CA_CERT_DATA", base64.b64encode(ca_pem.encode("utf-8")).decode("ascii"))
    b64_ctx = ws_tls_mod.build_master_ssl_context("wss://master.example.com/ws")
    assert b64_ctx.verify_mode == ssl.CERT_REQUIRED

    monkeypatch.setenv("MASTER_TLS_INSECURE_SKIP_VERIFY", "true")
    insecure_ctx = ws_tls_mod.build_master_ssl_context("wss://master.example.com/ws")
    assert insecure_ctx.verify_mode == ssl.CERT_NONE
    assert insecure_ctx.check_hostname is False

    # Verify connect_to_server in kubedoor-agent.py no longer uses ssl=False
    source = (AGENT_ROOT / "kubedoor-agent.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    connect_fn = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "connect_to_server")
    connect_src = ast.get_source_segment(source, connect_fn)
    assert "ssl=False" not in connect_src
    assert "ws_tls.build_master_ssl_context" in connect_src
    assert "ws_tls.redact_credentials" in connect_src


@pytest.mark.asyncio
async def test_ws_tls_verifies_real_wss_handshake(ws_tls_mod, tls_certs_mod, tmp_path, monkeypatch):
    monkeypatch.setenv("WEBHOOK_CERT_DIR", str(tmp_path / "absent"))
    monkeypatch.setenv("WEBHOOK_RUNTIME_CERT_DIR", str(tmp_path / "certs"))
    crt, key, ca = tls_certs_mod.ensure_webhook_certs(namespace="kubedoor")

    async def ws_handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"auth": request.headers.get("Authorization")})
        await ws.close()
        return ws

    app = web.Application()
    app.router.add_get("/ws", ws_handler)
    server_ssl = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    server_ssl.load_cert_chain(crt, key)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=server_ssl)
    await site.start()
    try:
        port = site._server.sockets[0].getsockname()[1]
        clean_uri, headers = ws_tls_mod.prepare_master_ws_connection(
            f"wss://agent-u:secret-p@127.0.0.1:{port}",
            {"env": "prod"},
        )
        # Without custom CA, self-signed server is rejected
        monkeypatch.delenv("MASTER_CA_CERT", raising=False)
        monkeypatch.delenv("MASTER_CA_CERT_DATA", raising=False)
        monkeypatch.delenv("MASTER_TLS_INSECURE_SKIP_VERIFY", raising=False)
        strict_ctx = ws_tls_mod.build_master_ssl_context(clean_uri)
        async with aiohttp.ClientSession() as session:
            with pytest.raises(aiohttp.ClientConnectorCertificateError):
                await session.ws_connect(clean_uri, headers=headers, ssl=strict_ctx)

            # With MASTER_CA_CERT, TLS succeeds and sends Basic auth header
            monkeypatch.setenv("MASTER_CA_CERT", ca)
            trusted_ctx = ws_tls_mod.build_master_ssl_context(clean_uri)
            async with session.ws_connect(clean_uri, headers=headers, ssl=trusted_ctx) as ws:
                msg = await ws.receive_json()
                expected = "Basic " + base64.b64encode(b"agent-u:secret-p").decode("ascii")
                assert msg == {"auth": expected}
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_connect_to_server_plain_ws_emits_no_ssl_none_deprecation_warning(isolated_agent, monkeypatch):
    import asyncio as _asyncio
    import warnings

    async def ws_handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.close()
        return ws

    app = web.Application()
    app.router.add_get("/ws", ws_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        port = site._server.sockets[0].getsockname()[1]
        monkeypatch.setattr(isolated_agent.utils, "KUBEDOOR_MASTER", f"ws://127.0.0.1:{port}")
        monkeypatch.setattr(isolated_agent.utils, "PROM_K8S_TAG_VALUE", "demo")
        monkeypatch.setattr(isolated_agent, "event_monitor", SimpleNamespace(set_websocket_connection=Mock(), stop_monitoring=AsyncMock()))
        monkeypatch.setattr(isolated_agent, "workload_streamer", SimpleNamespace(attach=Mock(), detach=Mock()))
        monkeypatch.setattr(isolated_agent, "notifier", SimpleNamespace(attach=Mock(), detach=Mock()))
        monkeypatch.setattr(isolated_agent, "pod_exec_manager", SimpleNamespace(close_all=Mock()))
        monkeypatch.setattr(isolated_agent, "pod_log_streamer", SimpleNamespace(close_all=Mock()))
        monkeypatch.setattr(
            isolated_agent,
            "run_connection_tasks",
            AsyncMock(side_effect=_asyncio.CancelledError),
        )

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with pytest.raises(_asyncio.CancelledError):
                await isolated_agent.connect_to_server()

        ssl_deprecations = [
            str(w.message)
            for w in caught
            if issubclass(w.category, DeprecationWarning) and "ssl=None" in str(w.message)
        ]
        assert ssl_deprecations == []
    finally:
        await runner.cleanup()
