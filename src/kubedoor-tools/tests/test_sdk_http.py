"""Exercise the real synchronous SDK against a local fake API server."""

import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from kubedoor_tools import KubernetesExecutor


@pytest.fixture
def fake_api():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, body):
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            url = urlsplit(self.path)
            path = url.path
            requests.append({"method": "GET", "path": path, "query": parse_qs(url.query),
                             "authorization": self.headers.get("Authorization")})
            if path == "/api/v1":
                self.reply(200, {"groupVersion": "v1", "resources": [{"name": "pods", "kind": "Pod", "namespaced": True}]})
            elif path == "/apis/example.org/v1":
                self.reply(200, {"groupVersion": "example.org/v1", "resources": [{"name": "widgets", "kind": "Widget", "namespaced": True}]})
            elif path.endswith("/new"):
                self.reply(404, {"kind": "Status", "message": "Resource not found", "code": 404})
            elif path.endswith("/denied"):
                self.reply(403, {"kind": "Status", "message": "token=http-test-token is forbidden", "code": 403})
            elif path.endswith("/server-error"):
                self.reply(503, {"kind": "Status", "message": "API server unavailable", "code": 503})
            else:
                self.reply(200, {"items": []})

        def do_PATCH(self):
            url = urlsplit(self.path)
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append({"method": "PATCH", "path": url.path, "content_type": self.headers.get("Content-Type"), "body": body})
            self.reply(200, {"metadata": {"name": "web"}, "spec": body.get("spec", {})})

        def do_POST(self):
            url = urlsplit(self.path)
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append({"method": "POST", "path": url.path, "query": parse_qs(url.query), "body": body})
            if parse_qs(url.query).get("dryRun") != ["All"]:
                self.reply(400, {"kind": "Status", "message": "Tests must not persist resources", "code": 400})
            else:
                self.reply(201, {**body, "metadata": {**body.get("metadata", {}), "uid": "dry-run-uid"}})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_address[1]}"
    config = {"apiVersion": "v1", "kind": "Config", "current-context": "selected",
              "contexts": [{"name": "selected", "context": {"cluster": "selected", "user": "selected", "namespace": "team"}}],
              "clusters": [{"name": "selected", "cluster": {"server": endpoint}}],
              "users": [{"name": "selected", "user": {"token": "http-test-token"}}]}
    try:
        yield config, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


async def test_real_sdk_maps_api_exceptions_and_redacts_credentials(fake_api):
    config, requests = fake_api
    executor = KubernetesExecutor(config)
    missing = await executor.execute("api", {"path": "/api/v1/namespaces/team/pods/new"})
    assert missing["error"]["code"] == "NOT_FOUND"
    assert missing["error"]["status"] == 404
    denied = await executor.execute("api", {"path": "/api/v1/namespaces/team/pods/denied"})
    assert denied["error"]["code"] == "FORBIDDEN"
    assert denied["error"]["status"] == 403
    assert "http-test-token" not in json.dumps(denied)
    assert requests[0]["authorization"] == "Bearer http-test-token"
    unavailable = await executor.execute("api", {"path": "/api/v1/namespaces/team/pods/server-error"})
    assert unavailable["error"] == {"code": "API_ERROR", "message": "API server unavailable", "status": 503}
    await executor.close()


async def test_real_sdk_encodes_query_parameters_and_patch_bodies(fake_api):
    config, requests = fake_api
    executor = KubernetesExecutor(config)
    try:
        listed = await executor.execute("api", {"path": "/api/v1/namespaces/team/pods",
                                                "query": {"labelSelector": "app=web,tier!=db", "limit": 5, "watch": False}})
        assert listed["success"], listed
        assert requests[-1]["query"] == {"labelSelector": ["app=web,tier!=db"], "limit": ["5"], "watch": ["false"]}
        patched = await executor.execute("api", {"method": "PATCH", "path": "/apis/apps/v1/namespaces/team/deployments/web",
                                                 "body": {"spec": {"replicas": 2}}})
        assert patched["success"] and patched["data"]["spec"] == {"replicas": 2}, patched
        assert requests[-1]["content_type"] == "application/merge-patch+json"
        assert requests[-1]["body"] == {"spec": {"replicas": 2}}
    finally:
        await executor.close()


async def test_real_sdk_create_preparation_only_issues_server_dry_run(fake_api):
    config, requests = fake_api
    executor = KubernetesExecutor(config)
    prepared = await executor.prepare("api", {"method": "POST", "path": "/api/v1/namespaces/team/pods",
                                               "body": {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "new"}}})
    assert prepared["success"]
    assert prepared["preview"]["dry_run_supported"]
    assert prepared["preconditions"] == [{"path": "/api/v1/namespaces/team/pods/new", "must_not_exist": True}]
    assert all(request.get("query", {}).get("dryRun") == ["All"] for request in requests if request["method"] == "POST")
    await executor.close()


async def test_real_sdk_dynamic_crd_discovery_and_missing_manifest_can_prepare(fake_api):
    config, requests = fake_api

    class ManifestExecutor(KubernetesExecutor):
        async def _cli(self, operation, args, run, timeout):
            self.dry_command = copy.deepcopy(args)
            return self._result(True, {"stdout": "{}", "stderr": ""}, source="kubectl", exit_code=0)

    executor = ManifestExecutor(config)
    manifest = "apiVersion: example.org/v1\nkind: Widget\nmetadata:\n  name: new\n  namespace: team\nspec:\n  enabled: true\n"
    prepared = await executor.prepare("kubectl", {"argv": ["apply", "-f", "resource.yaml"], "files": {"resource.yaml": manifest}})
    assert prepared["success"], prepared
    assert prepared["preview"]["dry_run_supported"]
    assert prepared["preconditions"] == [{"path": "/apis/example.org/v1/namespaces/team/widgets/new", "must_not_exist": True}]
    assert "--dry-run=server" in executor.dry_command["argv"]
    assert [request["path"] for request in requests] == ["/apis/example.org/v1", "/apis/example.org/v1/namespaces/team/widgets/new"]
    await executor.close()


async def test_real_sdk_configuration_is_per_executor_and_does_not_set_globals(fake_api):
    from kubernetes import client

    original_default_host = client.Configuration.get_default_copy().host
    config, _ = fake_api
    executor = KubernetesExecutor(config)
    await executor._initialize()
    another_config = copy.deepcopy(config)
    another_config["clusters"][0]["cluster"]["server"] = "https://another.example"
    another = KubernetesExecutor(another_config)
    await another._initialize()
    assert executor._client.configuration.host != another._client.configuration.host
    assert client.Configuration.get_default_copy().host == original_default_host
    await executor.close()
    await another.close()
