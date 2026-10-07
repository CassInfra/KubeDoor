import pytest
from cryptography.exceptions import InvalidTag

from kubedoor_ai.domain import AIError, CredentialCipher, Provider, authenticate, normalize_scope, redact
from kubedoor_ai.gateway import guard_scope, table_rows


def test_trusted_identity_requires_both_service_token_and_identity():
    with pytest.raises(AIError):
        authenticate({"x-user-name": "admin", "x-user-permission": "rw"}, "secret")
    identity = authenticate({"x-kubedoor-token": "secret", "x-user-name": "reader", "x-user-permission": "read"}, "secret")
    with pytest.raises(AIError):
        identity.require_write()


def test_credential_cipher_authenticates_cluster_and_never_stores_plaintext():
    cipher = CredentialCipher("ab" * 32)
    config = {"token": "cluster-private-token", "clusters": []}
    encrypted = cipher.encrypt("cluster-a", config)
    assert b"cluster-private-token" not in encrypted
    assert cipher.decrypt("cluster-a", encrypted) == config
    with pytest.raises(InvalidTag):
        cipher.decrypt("cluster-b", encrypted)
    with pytest.raises(InvalidTag):
        cipher.decrypt("cluster-a", encrypted[:-1] + bytes([encrypted[-1] ^ 1]))


def test_scope_namespace_is_context_but_cluster_is_hard_boundary():
    scope = normalize_scope({"env": "cluster-a", "namespace": "default"})
    guard_scope(scope, "api", {"path": "/apis/apps/v1/namespaces/other/deployments", "method": "PATCH"}, False)
    guard_scope(scope, "kubectl", {"argv": ["get", "pods", "-A"]}, True)
    with pytest.raises(AIError, match="其他集群"):
        guard_scope(scope, "api", {"env": "cluster-b"}, True)


def test_provider_validation_and_recursive_redaction():
    with pytest.raises(AIError):
        Provider.parse({"base_url": "http://user:password@example.com", "api_key": "key", "model": "model"})
    clean = redact({"api_key": "sensitive", "content": "echo sensitive", "nested": [{"Authorization": "Bearer abc"}]}, ("sensitive",))
    assert "sensitive" not in str(clean)
    assert "Bearer abc" not in str(clean)
    assert Provider.parse({"base_url": "http://localhost:11434/v1", "api_key": "", "model": "local"}).api_key == ""


def test_stored_resource_columns_preserve_uncollected_jvm_null():
    raw = {"meta": [{"name": "request_cpu_m"}, {"name": "limit_mem_mb"}, {"name": "jvm_xms_bytes"}], "data": [[500, 1024, None]]}
    assert table_rows(raw)["data"] == [{"request_cpu_m": 500, "limit_mem_mb": 1024, "jvm_xms_bytes": None}]


def test_kubernetes_secrets_and_cli_contents_are_hidden_on_export():
    secret = {"kind": "Secret", "metadata": {"name": "app"}, "data": {"payload": "base64-private"}, "stringData": {"payload": "private-text"}}
    clean = redact({"kind": "SecretList", "items": [secret]})
    assert "base64-private" not in str(clean)
    assert "private-text" not in str(clean)
    assert clean["items"][0]["metadata"]["name"] == "app"
    patch = redact({"path": "/api/v1/namespaces/default/secrets/app", "method": "PATCH", "body": {"data": {"payload": "base64-private"}}})
    assert "base64-private" not in str(patch)
    cli = redact({"argv": ["apply", "-f", "input.yaml"], "files": {"input.yaml": "arbitrary-private-value"}, "stdin": "private-input"})
    assert cli["argv"] == ["apply", "-f", "input.yaml"]
    assert "arbitrary-private-value" not in str(cli)
    assert "private-input" not in str(cli)


@pytest.mark.parametrize("entered,expected", [
    (" https://llm.example.com/v1/chat/completions/ ", "https://llm.example.com/v1"),
    ("https://llm.example.com/v1", "https://llm.example.com/v1"),
    ("https://provider.example/custom/openai/v1/chat/completions", "https://provider.example/custom/openai/v1"),
    ("https://provider.example/chat/completions", "https://provider.example"),
    ("https://api.deepseek.com", "https://api.deepseek.com"),
    ("http://[::1]:11434/custom/v1/", "http://[::1]:11434/custom/v1"),
])
def test_provider_base_url_normalizes_endpoint_and_preserves_custom_prefix(entered, expected):
    parsed = Provider.parse({"base_url": entered, "api_key": "", "model": "deepseek-flash"})
    assert parsed.base_url == expected
    assert parsed.model == "deepseek-flash"


@pytest.mark.parametrize("entered", [
    "https://provider.example/v1?api_key=private-query", "https://provider.example/v1#fragment",
    "https://user:private-password@provider.example/v1", "https://@provider.example/v1",
    "https://provider.example:invalid/v1", "https://provider.example:70000/v1",
    "https://provider.example/space path/v1", "https://provider.example/\ninternal/v1",
    "https://[::1/v1", "https://provider.example\\@another.example/v1", "ftp://provider.example/v1", None,
])
def test_invalid_provider_address_is_controlled_and_does_not_echo_input(entered):
    with pytest.raises(AIError) as failed:
        Provider.parse({"base_url": entered, "api_key": "secret-key", "model": "deepseek-flash"})
    assert failed.value.code == "provider_address_invalid"
    assert failed.value.status == 400
    assert "private-query" not in str(failed.value)
    assert "private-password" not in str(failed.value)
