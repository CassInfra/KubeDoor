"""Render the shipped master templates without evaluating user input as shell code."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shlex

import yaml

from .config import metrics_endpoints, selected_components, url_credentials


ROOT = Path(os.environ.get("KUBEDOOR_ROOT", Path(__file__).resolve().parents[3]))
TOKEN = re.compile(r"__([A-Z][A-Z0-9_]*)__")
STATE_NAME = "kubedoor-setup-state"
# 不装告警组件时 nginx 不能再指向 kubedoor-alarm（启动时解析不到上游会起不来），自定义告警投递直接返回 503
ALARM_ABSENT = """            # 未安装告警组件：自定义告警投递直接返回 503
            location = /api/custom_alert {
                default_type application/json;
                return 503 '{"status":"error","message":"未安装告警组件（kubedoor-alarm），不能投递自定义告警"}';
            }
"""


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


def general_settings_values(config: dict, defaults: dict) -> dict:
    """网页安装写进控制端「通用配置」的值，即 PUT /api/admin/general-settings 请求体里的 values。

    defaults 是 load_defaults 的结果（含 install.sh 传给安装服务的镜像仓库和采集组件镜像）。
    地址带账号密码原样交给 master，由 master 加密保存；值为 None 表示不设置（master 用默认值或视为未配置）。
    """
    metrics = config["metrics"]
    query, write = metrics_endpoints(metrics)
    return {
        "prom_type": metrics["type"],
        "prom_url": query,
        "metrics_write_url": write or None,
        "prom_k8s_tag_key": metrics["tag_key"],
        "kubedoor_exturl": config["public_url"],
        "alertmanager_exturl": defaults.get("ALERTMANAGER_EXTURL") or None,
        "oss_url": config.get("oss_url") or None,
        "image_repo": defaults.get("IMAGE_REPO") or None,
        "image_vmagent": defaults.get("IMAGE_VMAGENT") or None,
        "image_kube_state_metrics": defaults.get("IMAGE_KUBE_STATE_METRICS") or None,
        "image_node_exporter": defaults.get("IMAGE_NODE_EXPORTER") or None,
    }


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
    if not components.get("alarm", True):
        source = re.sub(r"(?m)^.*# BEGIN-ALARM.*\n[\s\S]*?^.*# END-ALARM.*\n?", lambda _: ALARM_ABSENT, source)
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
    components = selected_components(config.get("components"))
    # 远程读、写地址都带着账号密码，原样写进 Secret kubedoor-connection 的 PROM_URL / METRICS_WRITE_URL（vmalert、Grafana 用）。
    # master、alarm、ai 的其余配置在 master 就绪后写进数据库的「通用配置」，见 general_settings_values
    query, write = metrics_endpoints(metrics)
    values.update({
        "NAMESPACE": namespace, "PG_HOST": pg["host"], "PG_PORT": str(pg.get("port", 5432)),
        "PG_USER": pg["user"], "PG_DATABASE": pg["database"], "PG_PASSWORD": pg["password"],
        "PG_SSLMODE": pg.get("sslmode", "disable"), "PROM_URL": query,
        "PROM_K8S_TAG_KEY": metrics["tag_key"],
        "KUBEDOOR_INIT_ADMIN_PASSWORD": admin["password"],
        "KUBEDOOR_INIT_AGENT_PASSWORD": secrets["agent_password"],
        "AI_INTERNAL_TOKEN": secrets["internal_token"], "AI_ENCRYPTION_KEY": secrets["encryption_key"],
        "GRAFANA_ADMIN_PASSWORD": secrets["grafana_password"],
        "WEB_NODEPORT_LINE": "", "ALERTMANAGER_NODEPORT_LINE": "",
        "CONFIG_HASH_ENV": "pending", "CONFIG_HASH_MASTER": "pending", "CONFIG_HASH_GRAFANA": "pending", "CONFIG_HASH_WEB": "pending",
    })
    # 不创建 ConfigMap kubedoor-config（10-config.yaml 只给旧版配置式安装脚本放数据库连接）：
    # 数据库连接在 Secret kubedoor-connection，其余配置在数据库，事件告警规则用 master 内置的默认规则
    files = ["05-ai-security.yaml", "20-master.yaml"]
    files += ["25-alarm.yaml"] if components["alarm"] else []
    files += ["30-ai.yaml", "40-web.yaml"]
    files += [filename for flag, filename in (("grafana", "35-grafana.yaml"), ("vmalert", "71-vmalert.yaml"), ("alertmanager", "72-alertmanager.yaml")) if components[flag]]
    folder = ROOT / "src/kubedoor-setup/resources/manifests/master"
    objects = [obj for filename in files for obj in _objects(folder / filename, values, components)]
    by_name = {(item["kind"], item["metadata"]["name"]): item for item in objects}
    connection = {key: str(values[key]) for key in values if key.startswith("PG_")}
    connection.update(PROM_URL=query, METRICS_WRITE_URL=write)
    objects.append({"apiVersion": "v1", "kind": "Secret", "metadata": _meta("kubedoor-connection", namespace),
                    "type": "Opaque", "stringData": connection})
    if components["grafana"]:
        grafana_config = by_name["ConfigMap", "kubedoor-grafana-config"]["data"]
        grafana_config.pop("datasources.yaml", None)
        metrics_url, metrics_user, metrics_password = url_credentials(query)
        data_sources = {"apiVersion": 1, "datasources": [
            {"name": "KubeDoor-Postgres", "uid": "KubeDoor-Postgres", "type": "grafana-postgresql-datasource", "access": "proxy",
             "url": "$KUBEDOOR_PG_ADDRESS", "user": "$KUBEDOOR_PG_USER", "database": "$KUBEDOOR_PG_DATABASE", "isDefault": True,
             "secureJsonData": {"password": "$KUBEDOOR_PG_PASSWORD"},
             "jsonData": {"sslmode": pg.get("sslmode", "disable"), "postgresVersion": 1500, "maxOpenConns": 10, "maxIdleConns": 5, "connMaxLifetime": 14400}},
            {"name": "KubeDoor-Prometheus", "uid": "KubeDoor-Prometheus", "type": "prometheus", "access": "proxy",
             "url": "$KUBEDOOR_METRICS_URL", "basicAuth": bool(metrics_user), "basicAuthUser": "$KUBEDOOR_METRICS_USER",
             "secureJsonData": {"basicAuthPassword": "$KUBEDOOR_METRICS_PASSWORD"}, "jsonData": {"httpMethod": "POST"}},
        ]}
        objects.append({"apiVersion": "v1", "kind": "Secret", "metadata": _meta("kubedoor-grafana-datasources", namespace), "type": "Opaque",
                        "stringData": {"datasources.yaml": yaml.safe_dump(data_sources, allow_unicode=True), "metrics-password": metrics_password,
                                       "metrics-user": metrics_user, "metrics-url": metrics_url,
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
        pod = by_name["Deployment", "vmalert"]["spec"]["template"]["spec"]
        container = pod["containers"][0]
        container["envFrom"] = [{"secretRef": {"name": "kubedoor-connection"}}]
        container["args"] = [arg for arg in container["args"] if not arg.startswith(("-datasource.url=", "-remoteRead.url=", "-remoteWrite.url="))]
        container["args"] += ["-datasource.url=$(PROM_URL)", "-remoteRead.url=$(PROM_URL)"]
        if write:
            container["args"] += ["-remoteWrite.url=$(METRICS_WRITE_URL)", "-remoteWrite.disablePathAppend"]
        by_name["Service", "vmalert"]["spec"]["type"] = "ClusterIP"
    if components["alertmanager"]:
        # 告警通知的 webhook 固定指向 kubedoor-alarm 的 /msg/kubedoor，通道由 alarm 按集群向 master 取，模板原样保留
        by_name["Service", "alertmanager"]["spec"]["type"] = "ClusterIP"
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
            # 共享模板里 master、alarm、ai 从 kubedoor-config 取数据库连接，网页安装改从 Secret kubedoor-connection 取
            if "envFrom" in container:
                container["envFrom"] = [{"secretRef": {"name": "kubedoor-connection"}}
                                        if entry.get("configMapRef", {}).get("name") == "kubedoor-config" else entry
                                        for entry in container["envFrom"]]
    return objects
