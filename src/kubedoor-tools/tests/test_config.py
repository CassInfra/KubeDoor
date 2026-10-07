import copy
import json

import pytest
import yaml

from kubedoor_tools import parse_kubeconfig, redact_export


@pytest.fixture
def kubeconfig():
    return {"apiVersion": "v1", "kind": "Config", "current-context": "selected",
            "contexts": [{"name": "selected", "context": {"cluster": "one", "user": "operator", "namespace": "team"}},
                         {"name": "other", "context": {"cluster": "two", "user": "other"}}],
            "clusters": [{"name": "one", "cluster": {"server": "https://selected.example"}},
                         {"name": "two", "cluster": {"server": "https://other.example"}}],
            "users": [{"name": "operator", "user": {"token": "private-inline-token"}},
                      {"name": "other", "user": {"token": "other-private-token"}}]}


def test_yaml_json_and_selected_credentials_only(kubeconfig):
    for encoded in (json.dumps(kubeconfig), yaml.safe_dump(kubeconfig)):
        loaded = parse_kubeconfig(encoded)
        assert loaded["server"] == "https://selected.example"
        assert loaded["contexts"] == [{"name": "selected", "namespace": "team", "server": "https://selected.example"},
                                      {"name": "other", "namespace": "default", "server": "https://other.example"}]
        assert len(loaded["config"]["users"]) == 1
        assert "other-private-token" not in json.dumps(loaded)
    assert parse_kubeconfig(kubeconfig, "other")["context"] == "other"


@pytest.mark.parametrize("field,value", [("exec", {"command": "evil"}), ("auth-provider", {}),
                                         ("tokenFile", "/etc/token"), ("client-key", "C:/key.pem"),
                                         ("client-certificate", "client.pem")])
def test_external_credentials_and_plugins_are_rejected(kubeconfig, field, value):
    kubeconfig["users"][0]["user"][field] = value
    with pytest.raises(ValueError, match="inline"):
        parse_kubeconfig(kubeconfig)


def test_server_userpass_and_external_ca_are_rejected(kubeconfig):
    invalid = copy.deepcopy(kubeconfig)
    invalid["clusters"][0]["cluster"]["server"] = "https://admin:secret@cluster.example"
    with pytest.raises(ValueError, match="without embedded credentials"):
        parse_kubeconfig(invalid)
    kubeconfig["clusters"][0]["cluster"]["certificate-authority"] = "ca.pem"
    with pytest.raises(ValueError, match="inline CA"):
        parse_kubeconfig(kubeconfig)


def test_yaml_object_construction_is_rejected():
    with pytest.raises(ValueError):
        parse_kubeconfig("!!python/object/apply:os.system ['echo unsafe']")


def test_missing_current_context_returns_safe_catalog(kubeconfig):
    kubeconfig.pop("current-context")
    result = parse_kubeconfig(kubeconfig)
    assert result["requires_context"] is True
    assert result["context"] is None
    assert result["config"] is None
    assert len(result["contexts"]) == 2
    assert "private" not in json.dumps(result)
    from kubedoor_tools import KubernetesExecutor
    with pytest.raises(ValueError, match="Select"):
        KubernetesExecutor(kubeconfig)


def test_exports_hide_secrets_but_retain_resource_lists():
    resources = {"items": [{"kind": "Secret", "metadata": {"name": "db"}, "data": {"password": "encoded"}},
                           {"kind": "Pod", "spec": {"containers": [{"env": [{"name": "DB_PASSWORD", "value": "hidden"}]}]}}],
                 "authorization": "Bearer hidden", "client-key-data": "private"}
    exported = redact_export(resources)
    assert len(exported["items"]) == 2
    assert "encoded" not in json.dumps(exported)
    assert "hidden" not in json.dumps(exported)
    assert exported["client-key-data"] == "[REDACTED]"
