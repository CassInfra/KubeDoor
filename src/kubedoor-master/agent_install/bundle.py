"""网页与命令行共用的 Agent 安装定义；渲染本身不连接 Kubernetes。"""
from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import yaml


BASE = {
    "namespace": "kubedoor", "master_url": "", "env": "", "aliases": [],
    "mode": "full", "image_repo": "swr.cn-south-1.myhuaweicloud.com/starsl.cn",
    "agent_tag": "2.3.0", "oss_url": "", "remote_write_url": "",
    "ksm": "auto", "node_exporter": "auto", "image_pull_secret": "",
    "prom_k8s_tag_key": "origin_prometheus", "msg_type": "wecom", "msg_token": "",
    "image_vmagent": "registry.cn-shenzhen.aliyuncs.com/starsl/vmagent:stable",
    "image_kube_state_metrics": "registry.cn-shenzhen.aliyuncs.com/starsl/kube-state-metrics:v2.14.0",
    "image_node_exporter": "registry.cn-shenzhen.aliyuncs.com/starsl/node-exporter",
    "metrics_available": False,
}
FIELDS = {"namespace", "master_url", "env", "aliases", "mode", "image_repo", "agent_tag",
          "oss_url", "remote_write_url", "ksm", "node_exporter", "image_pull_secret"}
FILES = ("00-namespace.yaml", "10-agent.yaml", "20-vmagent.yaml", "21-kube-state-metrics.yaml", "22-node-exporter.yaml")


def defaults() -> dict:
    result = copy.deepcopy(BASE)
    raw = os.environ.get("AGENT_INSTALL_DEFAULTS", "")
    if raw:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("Agent 安装默认配置必须为 JSON 对象")
        result.update({k: v for k, v in parsed.items() if k in BASE})
    result["master_url"] = result["master_url"] or os.environ.get("KUBEDOOR_EXTURL", "")
    result["msg_type"] = os.environ.get("MSG_TYPE", result["msg_type"])
    result["msg_token"] = os.environ.get("MSG_TOKEN", result["msg_token"])
    result["prom_k8s_tag_key"] = os.environ.get("PROM_K8S_TAG_KEY", result["prom_k8s_tag_key"])
    return result


def clean_url(value: str, *, credentials=False, base=False) -> str:
    if not isinstance(value, str) or not value or any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError("连接地址必须为完整的 HTTP / HTTPS 地址")
    p = urlsplit(value)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError("连接地址必须为完整的 HTTP / HTTPS 地址")
    try:
        p.port
    except ValueError:
        raise ValueError("连接地址端口无效") from None
    if not credentials and (p.username is not None or p.password is not None):
        raise ValueError("KubeDoor 地址不需要填写用户名或口令")
    if base:
        if p.path not in ("", "/") or p.query:
            raise ValueError("KubeDoor 地址只填写协议、主机和端口，不包含路径或查询参数")
        return urlunsplit((p.scheme, p.netloc, "", "", ""))
    if p.fragment:
        raise ValueError("指标写入地址不能包含浏览器锚点")
    return value


def normalize_options(raw: dict, default: dict | None = None) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("安装参数必须为对象")
    result = copy.deepcopy(default if default is not None else defaults())
    result.update({k: v for k, v in raw.items() if k in FIELDS})
    for key in FIELDS - {"aliases"}:
        if not isinstance(result.get(key, ""), str):
            raise ValueError(f"安装参数 {key} 必须为字符串")
        result[key] = result.get(key, "").strip()
    if result["env"] and not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", result["env"]):
        raise ValueError("集群标识需要 1 至 100 个字母、数字、下划线或中划线")
    if not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", result["namespace"]):
        raise ValueError("命名空间必须是有效的 Kubernetes 名称")
    aliases = result.get("aliases", [])
    if not isinstance(aliases, list) or len(aliases) > 20 or any(not isinstance(a, str) or len(a) > 100 for a in aliases):
        raise ValueError("集群别名最多 20 个，每个不超过 100 个字符")
    result["aliases"] = list(dict.fromkeys(a.strip() for a in aliases if a.strip()))
    if result["master_url"]:
        result["master_url"] = clean_url(result["master_url"], base=True)
    if result["mode"] not in ("full", "agent"):
        raise ValueError("安装方式必须为 full 或 agent")
    for key in ("ksm", "node_exporter"):
        if result[key] not in ("auto", "install", "reuse"):
            raise ValueError(f"{key} 必须为 auto / install / reuse")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*", result["image_repo"]):
        raise ValueError("镜像仓库格式无效")
    result["image_repo"] = result["image_repo"].rstrip("/")
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", result["agent_tag"]):
        raise ValueError("Agent 镜像标签格式无效")
    if result["image_pull_secret"] and not re.fullmatch(r"[a-z0-9]([-a-z0-9.]*[a-z0-9])?", result["image_pull_secret"]):
        raise ValueError("镜像拉取 Secret 名称无效")
    if result["remote_write_url"]:
        result["remote_write_url"] = clean_url(result["remote_write_url"], credentials=True)
    if result["oss_url"]:
        result["oss_url"] = clean_url(result["oss_url"], credentials=True)
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", result["prom_k8s_tag_key"]):
        raise ValueError("指标集群标签名称无效")
    return result


def without_credentials(value: str) -> str:
    p = urlsplit(value)
    return urlunsplit((p.scheme, p.netloc.rsplit("@", 1)[-1], p.path, "", ""))


def public_options(options: dict) -> dict:
    result = {k: copy.deepcopy(v) for k, v in options.items() if k in FIELDS}
    if "metrics_available" in options:
        result["metrics_available"] = bool(options["metrics_available"])
    for name in ("remote_write_url", "oss_url"):
        if result.get(name):
            result[name] = without_credentials(result[name])
    return result


def _template_dir() -> Path:
    override = os.environ.get("AGENT_INSTALL_TEMPLATE_DIR")
    if override:
        return Path(override)
    packaged = Path(__file__).resolve().parent / "manifests"
    if packaged.is_dir():
        return packaged
    for parent in Path(__file__).resolve().parents:
        local = parent / "deploy" / "manifests" / "agent"
        if local.is_dir():
            return local
    raise ValueError("未找到随发布包提供的 Agent 安装模板")


def _load_file(name: str, variables: dict, ksm: bool) -> list[dict]:
    text = (_template_dir() / name).read_text(encoding="utf-8")
    if not ksm:
        text = re.sub(r"(?m)^.*# BEGIN-KSM.*\n[\s\S]*?^.*# END-KSM.*\n", "", text)
    # 用户凭据在解析完成后写入对象，绝不作为模板内容解释。
    for key, value in variables.items():
        text = text.replace(f"__{key}__", str(value))
    if re.search(r"__[A-Z][A-Z0-9_]*__", text):
        raise ValueError("Agent 安装模板包含未解析的变量")
    return [obj for obj in yaml.safe_load_all(text) if obj]


def render_agent(options: dict, *, username: str, password: str) -> list[dict]:
    options = normalize_options(options, default=options)
    if not options["env"] or not options["master_url"]:
        raise ValueError("集群标识和 KubeDoor 连接地址不能为空")
    if not username or not password:
        raise ValueError("Agent 连接凭据不能为空")
    p = urlsplit(options["master_url"])
    ws = urlunsplit(("wss" if p.scheme == "https" else "ws",
                    f"{quote(username, safe='')}:{quote(password, safe='')}@{p.netloc}", "", "", ""))
    rw = options["remote_write_url"] or options["master_url"] + "/agent/metrics/write"
    rp = urlsplit(rw)
    rw_user = unquote(rp.username or "") if options["remote_write_url"] else username
    rw_pass = unquote(rp.password or "") if options["remote_write_url"] else password
    rw_clean = urlunsplit((rp.scheme, rp.netloc.rsplit("@", 1)[-1], rp.path, rp.query, ""))
    variables = {
        "NAMESPACE": options["namespace"], "IMAGE_REPO": options["image_repo"],
        "TAG_AGENT": options["agent_tag"], "K8S_NAME": options["env"],
        "PROM_K8S_TAG_KEY": options["prom_k8s_tag_key"], "MASTER_WS": "", "MSG_TYPE": "",
        "MSG_TOKEN": "", "OSS_URL": "", "REMOTE_WRITE_URL": "",
        "IMAGE_VMAGENT": options["image_vmagent"],
        "IMAGE_KUBE_STATE_METRICS": options["image_kube_state_metrics"],
        "IMAGE_NODE_EXPORTER": options["image_node_exporter"],
    }
    for key, value in variables.items():
        if "'" in value or "\n" in value or "\r" in value:
            raise ValueError(f"安装模板变量 {key} 包含无效字符")
    selected = list(FILES[:2])
    if options["mode"] == "full":
        selected.append(FILES[2])
        if options["ksm"] != "reuse":
            selected.append(FILES[3])
        if options["node_exporter"] != "reuse":
            selected.append(FILES[4])
    # 旧脚本允许独立启停三个采集组件；网页的「仅 Agent」仍然不安装采集组件。
    if options.get("_legacy_components"):
        selected = list(FILES[:2]) + options["_legacy_components"]
    resources = [obj for filename in selected for obj in _load_file(filename, variables, options["ksm"] != "reuse")]
    secret_data = {"KUBEDOOR_MASTER": ws, "MSG_TOKEN": options.get("msg_token", ""), "OSS_URL": options.get("oss_url", "")}
    secret = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "kubedoor-agent-credentials", "namespace": options["namespace"]}, "type": "Opaque", "data": {k: base64.b64encode(v.encode()).decode() for k, v in secret_data.items()}}
    resources.insert(1, secret)
    if options["mode"] == "full" and (rw_user or rw_pass or rp.query):
        metrics_data = {"password": base64.b64encode(rw_pass.encode()).decode()}
        if rp.query:
            metrics_data["url"] = base64.b64encode(rw_clean.encode()).decode()
        resources.insert(2, {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "kubedoor-metrics-credentials", "namespace": options["namespace"]}, "type": "Opaque", "data": metrics_data})
    for obj in resources:
        obj.setdefault("metadata", {}).setdefault("labels", {})["app.kubernetes.io/managed-by"] = "kubedoor-installer"
        if obj["kind"] == "ConfigMap" and obj["metadata"]["name"] == "kubedoor-agent-config":
            obj["data"] = {"PROM_K8S_TAG_VALUE": options["env"], "MSG_TYPE": options.get("msg_type", "wecom")}
        if obj["kind"] == "ConfigMap" and obj["metadata"]["name"] == "vmagent-config" and options.get("installation_id"):
            scrape = yaml.safe_load(obj["data"]["scrape.yml"])
            scrape["global"]["external_labels"][options["prom_k8s_tag_key"]] = options["env"]
            scrape["global"]["external_labels"]["kubedoor_installation"] = str(options["installation_id"])
            obj["data"]["scrape.yml"] = yaml.safe_dump(scrape, allow_unicode=True, sort_keys=False)
        if obj["kind"] not in ("Deployment", "DaemonSet"):
            continue
        pod = obj["spec"]["template"]
        spec = pod["spec"]
        if options.get("image_pull_secret"):
            spec["imagePullSecrets"] = [{"name": options["image_pull_secret"]}]
        if obj["metadata"]["name"] == "kubedoor-agent":
            spec["containers"][0]["envFrom"].append({"secretRef": {"name": "kubedoor-agent-credentials"}})
        if obj["metadata"]["name"] == "vmagent":
            container = spec["containers"][0]
            write_address = "$(KUBEDOOR_REMOTE_WRITE_URL)" if rp.query else rw_clean
            container["args"] = [f"-remoteWrite.url={write_address}" if a.startswith("-remoteWrite.url=") else a for a in container["args"]]
            if rp.query:
                container.setdefault("env", []).append({"name": "KUBEDOOR_REMOTE_WRITE_URL", "valueFrom": {
                    "secretKeyRef": {"name": "kubedoor-metrics-credentials", "key": "url"}}})
            if rw_user or rw_pass:
                container["args"] += [f"-remoteWrite.basicAuth.username={rw_user}", "-remoteWrite.basicAuth.passwordFile=/etc/kubedoor-metrics/password"]
                container["volumeMounts"].append({"name": "metrics-credentials", "mountPath": "/etc/kubedoor-metrics", "readOnly": True})
                spec["volumes"].append({"name": "metrics-credentials", "secret": {"secretName": "kubedoor-metrics-credentials"}})
        digest = hashlib.sha256(json.dumps({"options": options, "credentials": secret_data, "rw_pass": rw_pass}, sort_keys=True).encode()).hexdigest()[:16]
        pod.setdefault("metadata", {}).setdefault("annotations", {})["checksum/config"] = digest
    return resources


def _from_vars(variables: dict) -> tuple[dict, str, str]:
    d = copy.deepcopy(BASE)
    mapping = {"NAMESPACE": "namespace", "IMAGE_REPO": "image_repo", "TAG_AGENT": "agent_tag", "K8S_NAME": "env", "OSS_URL": "oss_url", "PROM_K8S_TAG_KEY": "prom_k8s_tag_key", "MSG_TYPE": "msg_type", "MSG_TOKEN": "msg_token", "IMAGE_VMAGENT": "image_vmagent", "IMAGE_KUBE_STATE_METRICS": "image_kube_state_metrics", "IMAGE_NODE_EXPORTER": "image_node_exporter", "REMOTE_WRITE_URL": "remote_write_url"}
    for old, new in mapping.items():
        if variables.get(old):
            d[new] = variables[old]
    p = urlsplit(variables.get("MASTER_WS", ""))
    d["master_url"] = urlunsplit(("https" if p.scheme == "wss" else "http", p.netloc.rsplit("@", 1)[-1], "", "", ""))
    d["mode"] = "agent" if variables.get("ENABLE_VMAGENT", "true") != "true" else "full"
    d["ksm"] = "reuse" if variables.get("ENABLE_KUBE_STATE_METRICS", "true") != "true" else "install"
    d["node_exporter"] = "reuse" if variables.get("ENABLE_NODE_EXPORTER", "true") != "true" else "install"
    d["_legacy_components"] = [name for name, flag in zip(FILES[2:], ("ENABLE_VMAGENT", "ENABLE_KUBE_STATE_METRICS", "ENABLE_NODE_EXPORTER")) if variables.get(flag, "true") == "true"]
    return d, unquote(p.username or "anonymous-agent"), unquote(p.password or "legacy-anonymous")


def master_metadata(variables: dict) -> tuple[dict, str]:
    d = copy.deepcopy(BASE)
    mapping = {"NAMESPACE": "namespace", "IMAGE_REPO": "image_repo", "TAG_AGENT": "agent_tag", "OSS_URL": "oss_url", "PROM_K8S_TAG_KEY": "prom_k8s_tag_key", "IMAGE_VMAGENT": "image_vmagent", "IMAGE_KUBE_STATE_METRICS": "image_kube_state_metrics", "IMAGE_NODE_EXPORTER": "image_node_exporter"}
    for old, new in mapping.items():
        if variables.get(old):
            d[new] = variables[old]
    d["master_url"] = variables.get("KUBEDOOR_EXTURL", "")
    target = variables.get("METRICS_WRITE_URL", "")
    if not target and variables.get("ENABLE_VICTORIA_METRICS", "true") == "true":
        target = f"http://{quote(variables.get('VM_USER', 'monit'), safe='')}:{quote(variables.get('VM_PASSWORD', ''), safe='')}@victoria-metrics.{d['namespace']}:8428/api/v1/write"
    d["metrics_available"] = bool(target)
    if not target:
        return d, "return 503;"
    target = clean_url(target, credentials=True)
    p = urlsplit(target)
    # 此文件是 nginx 配置，不让 URL 里的分号/引号/变量成为配置语法。
    clean = urlunsplit((p.scheme, p.netloc.rsplit("@", 1)[-1], p.path or "/", p.query, ""))
    if any(c in clean for c in ('"', "'", ";", "{", "}", "$", "\\")):
        raise ValueError("指标写入后端地址包含不支持的 nginx 配置字符，请使用 URL 编码")
    authorization = ""
    if p.username is not None:
        authorization = "Basic " + base64.b64encode(f"{unquote(p.username)}:{unquote(p.password or '')}".encode()).decode()
    snippet = [f'proxy_set_header Authorization "{authorization}";', f'proxy_pass {clean};']
    if p.scheme == "https":
        snippet += ["proxy_ssl_server_name on;", "proxy_ssl_verify on;", "proxy_ssl_trusted_certificate /etc/ssl/certs/ca-certificates.crt;"]
    return d, "\n".join(snippet)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("agent", "metadata"))
    parser.add_argument("--file")
    parser.add_argument("--field", choices=("defaults", "proxy"), default="defaults")
    args = parser.parse_args()
    fields = sys.stdin.buffer.read().decode().split("\0")
    variables = dict(zip(fields[::2], fields[1::2]))
    if args.command == "metadata":
        d, proxy = master_metadata(variables)
        print(json.dumps(d, ensure_ascii=True).replace("'", "\\u0027") if args.field == "defaults" else proxy)
        return
    options, username, password = _from_vars(variables)
    resources = render_agent(options, username=username, password=password)
    # 不改变旧部署在 AGENT_AUTH_REQUIRED=false 时使用的不带口令连接地址。
    for obj in resources:
        if obj["kind"] == "Secret" and obj["metadata"]["name"] == "kubedoor-agent-credentials":
            obj["data"]["KUBEDOOR_MASTER"] = base64.b64encode(variables["MASTER_WS"].encode()).decode()
    if args.file:
        # CLI 仍按原模板文件名输出，便于已有 render 调用按文件 review。
        name = Path(args.file).name
        names = {"00-namespace.yaml": {"Namespace"}, "10-agent.yaml": {"kubedoor-agent", "kubedoor-agent-config", "kubedoor-agent-cr", "kubedoor-agent-crb", "kubedoor-agent-credentials"}, "20-vmagent.yaml": {"vmagent", "vmagent-config", "kubedoor-vmagent", "kubedoor-metrics-credentials"}, "21-kube-state-metrics.yaml": {"kube-state-metrics", "kubedoor-state-metrics"}, "22-node-exporter.yaml": {"node-exporter"}}
        resources = [obj for obj in resources if obj["kind"] in names.get(name, set()) or obj["metadata"]["name"] in names.get(name, set())]
    print(yaml.safe_dump_all(resources, allow_unicode=True, sort_keys=False))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
