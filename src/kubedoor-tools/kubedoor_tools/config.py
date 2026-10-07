"""Load self-contained credentials without executing kubeconfig plugins."""

from __future__ import annotations

import copy
import json
import re
from typing import Any
from urllib.parse import urlsplit

import yaml


def _server_url(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("Kubernetes server must be an HTTP(S) URL")
    url = urlsplit(value)
    if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
        raise ValueError("Kubernetes server must be an HTTP(S) URL without embedded credentials")
    if url.query or url.fragment:
        raise ValueError("Kubernetes server URL cannot contain a query or fragment")
    return value.rstrip("/")


def parse_kubeconfig(raw: str | dict, context: str | None = None) -> dict:
    """Parse YAML/JSON and retain only the selected context's inline credentials.

    The context catalog contains no credentials. External files, auth plugins,
    proxy configuration and basic-auth URLs are never handed to the SDK/CLI.
    """
    try:
        config = copy.deepcopy(raw) if isinstance(raw, dict) else yaml.safe_load(raw)
    except (yaml.YAMLError, TypeError) as exc:
        raise ValueError("Invalid kubeconfig YAML/JSON") from exc
    if not isinstance(config, dict):
        raise ValueError("Kubeconfig must be an object")
    if "config" in config and "contexts" not in config:
        config = config["config"]
    if not isinstance(config, dict):
        raise ValueError("Kubeconfig must be an object")
    catalogs = {}
    for field in ("contexts", "clusters", "users"):
        entries = config.get(field, [])
        if not isinstance(entries, list):
            raise ValueError(f"Kubeconfig {field} must be a list")
        catalog = {}
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                raise ValueError(f"Invalid kubeconfig {field} entry")
            if entry["name"] in catalog:
                raise ValueError(f"Duplicate kubeconfig {field} name")
            catalog[entry["name"]] = entry
        catalogs[field] = catalog
    selected = context or config.get("current-context")
    if not selected and len(catalogs["contexts"]) == 1:
        selected = next(iter(catalogs["contexts"]))
    if selected and selected not in catalogs["contexts"]:
        raise ValueError("Choose an existing kubeconfig context")
    summaries = []
    for name, entry in catalogs["contexts"].items():
        settings = entry.get("context", {})
        if not isinstance(settings, dict):
            raise ValueError("Invalid context settings")
        cluster_entry = catalogs["clusters"].get(settings.get("cluster"))
        if not cluster_entry or not isinstance(cluster_entry.get("cluster"), dict):
            raise ValueError("Context references an unknown cluster")
        summaries.append({"name": name, "namespace": settings.get("namespace", "default"),
                          "server": _server_url(cluster_entry["cluster"].get("server"))})
    if not selected:
        if not summaries:
            raise ValueError("Kubeconfig has no contexts")
        return {"config": None, "contexts": summaries, "context": None, "server": None, "requires_context": True}
    context_entry = copy.deepcopy(catalogs["contexts"][selected])
    settings = context_entry["context"]
    cluster_entry = copy.deepcopy(catalogs["clusters"][settings["cluster"]])
    cluster = cluster_entry["cluster"]
    if "certificate-authority" in cluster or "proxy-url" in cluster:
        raise ValueError("Kubeconfig requires inline CA data and cannot configure a proxy")
    user_entry = catalogs["users"].get(settings.get("user"))
    if settings.get("user") and not user_entry:
        raise ValueError("Context references an unknown user")
    user_entry = copy.deepcopy(user_entry) if user_entry else {"name": "kubedoor-anonymous", "user": {}}
    user = user_entry.get("user", {})
    if not isinstance(user, dict):
        raise ValueError("Invalid kubeconfig user")
    forbidden = {"exec", "auth-provider", "client-certificate", "client-key", "tokenFile", "token-file",
                 "username", "password"}
    if forbidden.intersection(user):
        raise ValueError("Kubeconfig supports only inline token/certificate credentials; files and plugins are forbidden")
    allowed_user = {"token", "client-certificate-data", "client-key-data"}
    if set(user) - allowed_user:
        raise ValueError("Unsupported kubeconfig credential field")
    if bool(user.get("client-certificate-data")) != bool(user.get("client-key-data")):
        raise ValueError("Inline client certificate and key must be supplied together")
    for field in allowed_user:
        if field in user and not isinstance(user[field], str):
            raise ValueError("Inline credentials must be strings")
    settings["user"] = user_entry["name"]
    safe_config = {"apiVersion": "v1", "kind": "Config", "current-context": selected,
                   "contexts": [context_entry], "clusters": [cluster_entry], "users": [user_entry]}
    return {"config": safe_config, "contexts": summaries, "context": selected,
            "server": _server_url(cluster["server"])}


_SECRET_KEY = re.compile(r"^(?:.*[_-])?(?:password|passwd|token|secret|api[_-]?key|authorization|credential|credentials)$", re.I)
_KUBECONFIG_KEYS = {"client-key-data", "client-certificate-data", "client-key", "tokenfile"}
_TEXT_SECRET = re.compile(r'''(?i)(\b(?:authorization["']?\s*[:=]\s*["']?(?:bearer\s+)?|(?:api[_-]?key|password|token)["']?\s*[:=]\s*["']?))[^\s"'`,;{}\]]+''')


def redact_export(value: Any, _secret_resource: bool = False) -> Any:
    """Redact common secret fields before exporting tool results or previews."""
    if isinstance(value, dict):
        secret_resource = _secret_resource or value.get("kind") == "Secret"
        secret_list = value.get("kind") == "SecretList"
        secret_env = isinstance(value.get("name"), str) and bool(_SECRET_KEY.search(value["name"]))
        return {key: "[REDACTED]" if (_SECRET_KEY.match(str(key)) or str(key).lower() in _KUBECONFIG_KEYS
                                     or secret_resource and key in {"data", "stringData"}
                                     or secret_env and key == "value") else redact_export(item, secret_list and key == "items")
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_export(item, _secret_resource) for item in value]
    if isinstance(value, str):
        return _TEXT_SECRET.sub(r"\1[REDACTED]", value)
    return value
