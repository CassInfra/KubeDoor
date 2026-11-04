"""Render the shipped master templates without evaluating user input as shell code."""
from __future__ import annotations

import base64
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
from urllib.parse import quote, urlsplit, urlunsplit

import yaml


ROOT = Path(os.environ.get("KUBEDOOR_ROOT", Path(__file__).resolve().parents[3]))
TOKEN = re.compile(r"__([A-Z][A-Z0-9_]*)__")
STATE_NAME = "kubedoor-setup-state"


def load_defaults(overrides: dict | None = None) -> dict:
    """Read runtime defaults and the installer's image block without executing shell."""
    values = {}
    internal = (ROOT / "src/kubedoor-setup/resources/kubedoor.defaults.conf").read_text(encoding="utf-8")
    script = (ROOT / "deploy/install.sh").read_text(encoding="utf-8")
    begin, end = "# BEGIN KUBEDOOR IMAGE DEFAULTS", "# END KUBEDOOR IMAGE DEFAULTS"
    if script.count(begin) != 1 or script.count(end) != 1 or script.index(begin) >= script.index(end):
        raise ValueError("install.sh 缺少有效的镜像版本配置区")
    images = script.split(begin, 1)[1].split(end, 1)[0]
    for source, images_only in ((internal, False), (images, True)):
        for line in source.splitlines():
            match = re.match(r"^([A-Z][A-Z0-9_]*)=(.*)$", line)
            if match:
                if images_only and not match[1].startswith(("IMAGE_", "TAG_")):
                    raise ValueError("镜像配置区仅支持 IMAGE_* 和 TAG_* 字面量")
                parts = shlex.split(match[2], comments=True)
                if len(parts) > 1:
                    raise ValueError("默认配置必须是单个字面量")
                values[match[1]] = parts[0] if parts else ""
    values.update(overrides or {})
    return values


def credential_url(url: str, username: str = "", password: str = "") -> str:
    if not url or not username:
        return url
    parsed = urlsplit(url)
    authority = parsed.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parsed.scheme, f"{quote(username, safe='')}:{quote(password, safe='')}@{authority}",
                       parsed.path, parsed.query, ""))


def _agent_metadata(values: dict) -> tuple[dict, str]:
    path = ROOT / "src/kubedoor-master/agent_install/bundle.py"
    spec = importlib.util.spec_from_file_location("kubedoor_setup_agent_bundle", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.master_metadata(values)


def _replace(value, values):
    if isinstance(value, dict):
        return {key: _replace(item, values) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace(item, values) for item in value]
    if isinstance(value, str):
        def replacement(match):
            key = match[1]
            if key not in values:
                raise ValueError(f"安装模板缺少参数：{key}")
            return str(values[key])
        return TOKEN.sub(replacement, value)
    return value


def _objects(path: Path, values: dict, components: dict) -> list[dict]:
    source = path.read_text(encoding="utf-8")
    for block, enabled in (("GRAFANA", components.get("grafana", True)),
                           ("MCP", values.get("ENABLE_MCP", "true") == "true"),
                           ("CRON-TZ", values.get("CRON_TIMEZONE", "true") == "true")):
        if not enabled:
            source = re.sub(rf"(?m)^.*# BEGIN-{block}.*\n[\s\S]*?^.*# END-{block}.*\n?", "", source)
    # These are the only structural placeholders. All input values are substituted
    # after YAML parsing, so quotes/newlines in a credential cannot inject YAML.
    source = re.sub(r"(?m)^.*__(?:WEB_NODEPORT_LINE|ALERTMANAGER_NODEPORT_LINE)__.*\n?", "", source)
    source = source.replace("__GRAFANA_DASH_SOURCES__", "[]")
    return [_replace(item, values) for item in yaml.safe_load_all(source) if item]


def _meta(name: str, namespace: str) -> dict:
    return {"name": name, "namespace": namespace,
            "labels": {"app.kubernetes.io/part-of": "kubedoor", "app.kubernetes.io/managed-by": "kubedoor-setup"}}


def setup_locations() -> str:
    return """
            # Installation status stays readable after the temporary installer stops.
            location = /setup-status.json {
                alias /etc/kubedoor-setup/status.json;
                default_type application/json;
                add_header Cache-Control "no-store" always;
                access_log off;
            }
            location ^~ /setup-api/ {
                proxy_pass http://kubedoor-setup:8080;
                proxy_http_version 1.1;
                proxy_set_header Host $http_host;
                proxy_set_header X-Forwarded-Proto $scheme;
                proxy_set_header X-Forwarded-For $remote_addr;
                proxy_read_timeout 180;
                client_max_body_size 256k;
                access_log off;
            }
"""


def bootstrap_nginx() -> str:
    return """user nginx;
worker_processes auto;
events { worker_connections 1024; }
http {
    include /etc/nginx/mime.types;
    default_type application/octet-stream;
    sendfile on;
    server {
        listen 80;
        root /www/dist;
        location / { try_files $uri $uri/ /index.html; }
        location = /healthz { access_log off; return 200 'ok'; }
""" + setup_locations() + "    }\n}\n"


def render_master(config: dict, secrets: dict, namespace: str, defaults: dict | None = None) -> list[dict]:
    values = load_defaults(defaults)
    pg, metrics, admin = config["postgres"], config["metrics"], config["admin"]
    components = {"grafana": True, "vmalert": True, "alertmanager": True, **config.get("components", {})}
    if components["vmalert"] and not components["alertmanager"]:
        raise ValueError("启用 vmalert 时需要同时启用 Alertmanager")
    query = credential_url(metrics["url"], metrics.get("user", ""), metrics.get("password", ""))
    write = metrics.get("write_url", "")
    if metrics["type"] == "Victoria-Metrics-Single" and not write:
        write = metrics["url"].rstrip("/") + "/api/v1/write"
    write = credential_url(write, metrics.get("user", ""), metrics.get("password", ""))
    values.update({
        "NAMESPACE": namespace, "PG_HOST": pg["host"], "PG_PORT": str(pg.get("port", 5432)),
        "PG_USER": pg["user"], "PG_DATABASE": pg["database"], "PG_PASSWORD": pg["password"],
        "PG_SSLMODE": pg.get("sslmode", "prefer"), "PROM_URL": query,
        "PROM_TYPE": metrics["type"], "PROM_K8S_TAG_KEY": metrics["tag_key"],
        "METRICS_WRITE_URL": write, "KUBEDOOR_EXTURL": config["public_url"],
        "KUBEDOOR_INIT_ADMIN_PASSWORD": admin["password"],
        "KUBEDOOR_INIT_AGENT_PASSWORD": secrets["agent_password"],
        "AI_INTERNAL_TOKEN": secrets["internal_token"], "AI_ENCRYPTION_KEY": secrets["encryption_key"],
        "GRAFANA_ADMIN_PASSWORD": secrets["grafana_password"],
        "MSG_TYPE": "wecom", "MSG_TOKEN": "", "WEB_NODEPORT_LINE": "", "ALERTMANAGER_NODEPORT_LINE": "",
        "CONFIG_HASH_ENV": "pending", "CONFIG_HASH_MASTER": "pending", "CONFIG_HASH_GRAFANA": "pending", "CONFIG_HASH_WEB": "pending",
    })
    metadata, proxy = _agent_metadata(values)
    values["AGENT_INSTALL_DEFAULTS"] = json.dumps(metadata, ensure_ascii=False)
    values["METRICS_PROXY_BASE64"] = base64.b64encode(proxy.encode()).decode()
    files = ["05-ai-security.yaml", "06-metrics-gateway.yaml", "10-config.yaml", "20-master.yaml", "25-alarm.yaml", "30-ai.yaml", "40-web.yaml"]
    files += [filename for flag, filename in (("grafana", "35-grafana.yaml"), ("vmalert", "71-vmalert.yaml"), ("alertmanager", "72-alertmanager.yaml")) if components[flag]]
    folder = ROOT / "src/kubedoor-setup/resources/manifests/master"
    objects = [obj for filename in files for obj in _objects(folder / filename, values, components)]
    by_name = {(item["kind"], item["metadata"]["name"]): item for item in objects}
    connection = {key: str(values[key]) for key in values if key.startswith("PG_")}
    connection.update(PROM_URL=query, METRICS_WRITE_URL=write)
    objects.append({"apiVersion": "v1", "kind": "Secret", "metadata": _meta("kubedoor-connection", namespace),
                    "type": "Opaque", "stringData": connection})
    env_data = by_name["ConfigMap", "kubedoor-config"]["data"]
    for key in connection:
        env_data.pop(key, None)
    rules = (folder / "alert_rules.json").read_text(encoding="utf-8")
    objects.append({"apiVersion": "v1", "kind": "ConfigMap", "metadata": _meta("kubedoor-master-file-cfg", namespace),
                    "data": {"alert_rules.json": _replace(rules, values)}})
    if components["grafana"]:
        grafana_config = by_name["ConfigMap", "kubedoor-grafana-config"]["data"]
        grafana_config.pop("datasources.yaml", None)
        data_sources = {"apiVersion": 1, "datasources": [
            {"name": "KubeDoor-Postgres", "uid": "KubeDoor-Postgres", "type": "grafana-postgresql-datasource", "access": "proxy",
             "url": "$KUBEDOOR_PG_ADDRESS", "user": "$KUBEDOOR_PG_USER", "database": "$KUBEDOOR_PG_DATABASE", "isDefault": True,
             "secureJsonData": {"password": "$KUBEDOOR_PG_PASSWORD"},
             "jsonData": {"sslmode": pg.get("sslmode", "prefer"), "postgresVersion": 1500, "maxOpenConns": 10, "maxIdleConns": 5, "connMaxLifetime": 14400}},
            {"name": "KubeDoor-Prometheus", "uid": "KubeDoor-Prometheus", "type": "prometheus", "access": "proxy",
             "url": "$KUBEDOOR_METRICS_URL", "basicAuth": bool(metrics.get("user")), "basicAuthUser": "$KUBEDOOR_METRICS_USER",
             "secureJsonData": {"basicAuthPassword": "$KUBEDOOR_METRICS_PASSWORD"}, "jsonData": {"httpMethod": "POST"}},
        ]}
        objects.append({"apiVersion": "v1", "kind": "Secret", "metadata": _meta("kubedoor-grafana-datasources", namespace), "type": "Opaque",
                        "stringData": {"datasources.yaml": yaml.safe_dump(data_sources, allow_unicode=True), "metrics-password": metrics.get("password", ""),
                                       "metrics-user": metrics.get("user", ""), "metrics-url": metrics["url"],
                                       "pg-address": f"{('[' + pg['host'] + ']') if ':' in pg['host'] else pg['host']}:{pg.get('port', 5432)}"}})
        pod = by_name["Deployment", "kubedoor-dash"]["spec"]["template"]["spec"]
        pod["volumes"].append({"name": "grafana-datasources", "secret": {"secretName": "kubedoor-grafana-datasources"}})
        container = pod["containers"][0]
        for mount in container["volumeMounts"]:
            if mount.get("subPath") == "datasources.yaml":
                mount["name"] = "grafana-datasources"
        container["env"] += [
            {"name": "KUBEDOOR_PG_PASSWORD", "valueFrom": {"secretKeyRef": {"name": "kubedoor-connection", "key": "PG_PASSWORD"}}},
            {"name": "KUBEDOOR_PG_USER", "valueFrom": {"secretKeyRef": {"name": "kubedoor-connection", "key": "PG_USER"}}},
            {"name": "KUBEDOOR_PG_DATABASE", "valueFrom": {"secretKeyRef": {"name": "kubedoor-connection", "key": "PG_DATABASE"}}},
            {"name": "KUBEDOOR_PG_ADDRESS", "valueFrom": {"secretKeyRef": {"name": "kubedoor-grafana-datasources", "key": "pg-address"}}},
            {"name": "KUBEDOOR_METRICS_USER", "valueFrom": {"secretKeyRef": {"name": "kubedoor-grafana-datasources", "key": "metrics-user"}}},
            {"name": "KUBEDOOR_METRICS_URL", "valueFrom": {"secretKeyRef": {"name": "kubedoor-grafana-datasources", "key": "metrics-url"}}},
            {"name": "KUBEDOOR_METRICS_PASSWORD", "valueFrom": {"secretKeyRef": {"name": "kubedoor-grafana-datasources", "key": "metrics-password"}}},
        ]
        sources = []
        for path in sorted((ROOT / "src/kubedoor-setup/resources/dashboards").glob("*.json")):
            name = "kubedoor-dash-" + path.stem.lower().removeprefix("kubedoor-")
            objects.append({"apiVersion": "v1", "kind": "ConfigMap", "metadata": _meta(name, namespace),
                            "data": {path.name: _replace(path.read_text(encoding="utf-8"), values)}})
            sources.append({"configMap": {"name": name}})
        next(volume for volume in pod["volumes"] if volume["name"] == "grafana-dash")["projected"]["sources"] = sources
    nginx = by_name["ConfigMap", "nginx-config"]["data"]
    nginx["nginx.conf"], replacements = re.subn(r"(?m)^(\s*listen 80;)$", lambda match: match[1] + "\n" + setup_locations(), nginx["nginx.conf"], count=1)
    if replacements != 1:
        raise ValueError("Web 模板缺少安装状态代理插入点")
    pod = by_name["Deployment", "kubedoor-web"]["spec"]["template"]["spec"]
    pod["volumes"].append({"name": "setup-status", "configMap": {"name": STATE_NAME, "items": [{"key": "status.json", "path": "status.json"}]}})
    pod["containers"][0]["volumeMounts"].append({"name": "setup-status", "mountPath": "/etc/kubedoor-setup", "readOnly": True})
    if values.get("WEB_NODEPORT"):
        by_name["Service", "kubedoor-web"]["spec"]["ports"][0]["nodePort"] = int(values["WEB_NODEPORT"])
    if components["vmalert"]:
        if metrics["type"] == "Prometheus":
            rules = by_name["ConfigMap", "vmalert-config"]["data"]
            for name, contents in rules.items():
                rules[name] = re.sub(r"\b(increase|changes)_prometheus\(", r"\1(", contents)
        pod = by_name["Deployment", "vmalert"]["spec"]["template"]["spec"]
        container = pod["containers"][0]
        container["envFrom"] = [{"secretRef": {"name": "kubedoor-connection"}}]
        container["args"] = [arg for arg in container["args"] if not arg.startswith(("-datasource.url=", "-remoteRead.url=", "-remoteWrite.url="))]
        container["args"] += ["-datasource.url=$(PROM_URL)", "-remoteRead.url=$(PROM_URL)"]
        if write:
            container["args"] += ["-remoteWrite.url=$(METRICS_WRITE_URL)", "-remoteWrite.disablePathAppend"]
        by_name["Service", "vmalert"]["spec"]["type"] = "ClusterIP"
    if components["alertmanager"]:
        by_name["Service", "alertmanager"]["spec"]["type"] = "ClusterIP"
        # A channel not configured yet must not send to an invalid empty-token URL.
        alert_config = by_name["ConfigMap", "alertmanager-config"]["data"]
        content = yaml.safe_load(alert_config["config.yml"])
        for receiver in content["receivers"]:
            if receiver["name"] == "kubedoor-alarm":
                receiver.pop("webhook_configs", None)
        alert_config["config.yml"] = yaml.safe_dump(content, allow_unicode=True)
    digest = hashlib.sha256(json.dumps(objects, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    for obj in objects:
        obj["metadata"].setdefault("labels", {}).update(_meta("", namespace)["labels"])
        if obj["kind"] not in ("Deployment", "CronJob"):
            continue
        template = obj["spec"]["template"] if obj["kind"] == "Deployment" else obj["spec"]["jobTemplate"]["spec"]["template"]
        template.setdefault("metadata", {}).setdefault("annotations", {})["checksum/config"] = digest
        pod = template["spec"]
        pod["automountServiceAccountToken"] = False
        if values.get("IMAGE_PULL_SECRET"):
            pod["imagePullSecrets"] = [{"name": values["IMAGE_PULL_SECRET"]}]
        for container in pod.get("containers", []):
            if obj["metadata"]["name"] == "kubedoor-alarm":
                container.setdefault("env", []).append({"name": "AI_INTERNAL_TOKEN", "valueFrom": {
                    "secretKeyRef": {"name": "kubedoor-ai-security", "key": "AI_INTERNAL_TOKEN"}}})
            if any(entry.get("configMapRef", {}).get("name") == "kubedoor-config" for entry in container.get("envFrom", [])):
                container["envFrom"].append({"secretRef": {"name": "kubedoor-connection"}})
    return objects
