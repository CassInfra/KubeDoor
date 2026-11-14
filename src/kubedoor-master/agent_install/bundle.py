"""网页与命令行共用的 Agent 安装定义；渲染本身不连接 Kubernetes。

安装向导（kubedoor-setup）和旧脚本按文件路径加载这个文件：不能 import master 的其他模块，
网页安装要用的控制端设置（通用配置）由 master 的调用方传进 defaults。
"""
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
    "agent_tag": "2.6.0", "oss_url": "", "remote_write_url": "",
    "ksm": "auto", "node_exporter": "auto", "image_pull_secret": "",
    "prom_k8s_tag_key": "origin_prometheus",
    # 与 deploy/install.sh 的 IMAGE_VMAGENT 一致（网页安装用的就是它；不一致时安装向导会把它当成改过的值存进通用配置）
    "image_vmagent": "registry.cn-shenzhen.aliyuncs.com/starsl/vmagent",
    "image_kube_state_metrics": "registry.cn-shenzhen.aliyuncs.com/starsl/kube-state-metrics:v2.14.0",
    "image_node_exporter": "registry.cn-shenzhen.aliyuncs.com/starsl/node-exporter",
    "metrics_available": False,
}
# 控制端的时序库远程写地址（带账号密码），vmagent 默认直连它。只在 master 内部和加密的安装任务里使用：
# 不在 FIELDS 里，页面提交不了，public_options 也不返回
WRITE_KEY = "metrics_write_url"
# 页面（安装 Agent 对话框、安装向导第 6 步）能提交的参数；notification 是可选的通知配置（master 校验，见 service.py）。
# 镜像仓库、对象存储地址、指标写入地址覆盖不在这里：网页安装用通用配置（镜像仓库经 defaults 传入，对象存储地址
# 由 master 每次转发 /api/pod/* 时带给 Agent，不写进 Agent 的 Secret），旧脚本命令行从变量取（_from_vars）
FIELDS = {"namespace", "master_url", "env", "aliases", "mode", "agent_tag", "ksm", "node_exporter",
          "image_pull_secret", "notification"}
# 必须是字符串、去掉首尾空白的参数
STRINGS = (FIELDS - {"aliases", "notification"}) | {"image_repo", "oss_url", "remote_write_url"}
# master 从通用配置取来传给 defaults 的控制端设置（master_url 是通用配置的 KubeDoor 访问地址）
SETTINGS = ("master_url", "prom_k8s_tag_key", "image_repo", "image_vmagent", "image_kube_state_metrics",
            "image_node_exporter", WRITE_KEY)
FILES = ("00-namespace.yaml", "10-agent.yaml", "20-vmagent.yaml", "21-kube-state-metrics.yaml", "22-node-exporter.yaml")


def defaults(settings: dict | None = None) -> dict:
    """网页安装的默认参数：BASE 加上 master 从通用配置取来的控制端设置（键见 SETTINGS，空值沿用 BASE）。

    能否完整接入（metrics_available）按有没有远程写地址判断。不读环境变量：控制端的配置都在通用配置里。
    """
    result = copy.deepcopy(BASE)
    result[WRITE_KEY] = ""
    for key in SETTINGS:
        value = (settings or {}).get(key)
        if isinstance(value, str) and value.strip():
            result[key] = value.strip()
    result["metrics_available"] = bool(result[WRITE_KEY])
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
    for key in STRINGS:
        if not isinstance(result.get(key, ""), str):
            raise ValueError(f"安装参数 {key} 必须为字符串")
        result[key] = result.get(key, "").strip()
    # 通知配置的内容由 master 用 notify/channels.py 校验（这里不能 import master 的模块），只检查类型
    if result.get("notification") is not None and not isinstance(result["notification"], dict):
        raise ValueError("通知配置的格式不正确")
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
    if result.get(WRITE_KEY):
        try:
            result[WRITE_KEY] = clean_url(result[WRITE_KEY], credentials=True)
        except ValueError:
            # 不带出地址本身：里面有时序库账号密码
            raise ValueError("通用配置里的时序库远程写地址无效，请在系统管理 → 通用配置检查") from None
    if result["oss_url"]:
        result["oss_url"] = clean_url(result["oss_url"], credentials=True)
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", result["prom_k8s_tag_key"]):
        raise ValueError("指标集群标签名称无效")
    return result


def public_options(options: dict) -> dict:
    """给页面看的安装参数：只有 FIELDS 和能否完整接入；通知配置只给 IM 类型、@ 人和是否填了机器人地址"""
    result = {k: copy.deepcopy(v) for k, v in options.items() if k in FIELDS}
    if "metrics_available" in options:
        result["metrics_available"] = bool(options["metrics_available"])
    if "notification" in result:
        notification = result["notification"]
        result["notification"] = {
            "type": str(notification.get("type") or ""), "at": str(notification.get("at") or ""),
            "configured": bool(notification.get("token")),
        } if isinstance(notification, dict) else None
    return result


def _template_dir() -> Path:
    override = os.environ.get("AGENT_INSTALL_TEMPLATE_DIR")
    if override:
        return Path(override)
    packaged = Path(__file__).resolve().parent / "manifests"
    if packaged.is_dir():
        return packaged
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
    # vmagent 直连时序库：旧脚本填了 REMOTE_WRITE_URL 就用它，网页安装用通用配置里的远程写地址（WRITE_KEY）。
    # 账号密码从地址里拆出来放进 Secret，不出现在 vmagent 的启动参数里
    rw = options["remote_write_url"] or options.get(WRITE_KEY, "")
    if options["mode"] == "full" and not rw:
        raise ValueError("没有时序库写入地址：完整接入请先在系统管理 → 通用配置填写远程写地址，或选择仅安装 Agent")
    rp = urlsplit(rw)
    rw_user = unquote(rp.username or "")
    rw_pass = unquote(rp.password or "")
    rw_clean = urlunsplit((rp.scheme, rp.netloc.rsplit("@", 1)[-1], rp.path, rp.query, ""))
    variables = {
        "NAMESPACE": options["namespace"], "IMAGE_REPO": options["image_repo"],
        "TAG_AGENT": options["agent_tag"], "K8S_NAME": options["env"],
        "PROM_K8S_TAG_KEY": options["prom_k8s_tag_key"], "MASTER_WS": "",
        "OSS_URL": "", "REMOTE_WRITE_URL": "",
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
    # 通知不再由 agent 直接发送（经 master 统一发送），agent 里不保存机器人 token。
    # 对象存储地址：网页安装为空串（master 每次转发 /api/pod/* 时带上通用配置里的地址），旧脚本照旧写入 OSS_URL
    secret_data = {"KUBEDOOR_MASTER": ws, "OSS_URL": options.get("oss_url", "")}
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
            obj["data"] = {"PROM_K8S_TAG_VALUE": options["env"]}
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
        # 通知配置写进控制端的消息通知，不进目标集群：不算进配置摘要（只改通知不用滚动重启）
        rendered = {k: v for k, v in options.items() if k != "notification"}
        digest = hashlib.sha256(json.dumps({"options": rendered, "credentials": secret_data, "rw_pass": rw_pass}, sort_keys=True).encode()).hexdigest()[:16]
        pod.setdefault("metadata", {}).setdefault("annotations", {})["checksum/config"] = digest
    return resources


def _from_vars(variables: dict) -> tuple[dict, str, str]:
    d = copy.deepcopy(BASE)
    mapping = {"NAMESPACE": "namespace", "IMAGE_REPO": "image_repo", "TAG_AGENT": "agent_tag", "K8S_NAME": "env", "OSS_URL": "oss_url", "PROM_K8S_TAG_KEY": "prom_k8s_tag_key", "IMAGE_VMAGENT": "image_vmagent", "IMAGE_KUBE_STATE_METRICS": "image_kube_state_metrics", "IMAGE_NODE_EXPORTER": "image_node_exporter", "REMOTE_WRITE_URL": "remote_write_url"}
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("agent",))
    parser.add_argument("--file")
    args = parser.parse_args()
    fields = sys.stdin.buffer.read().decode().split("\0")
    variables = dict(zip(fields[::2], fields[1::2]))
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
