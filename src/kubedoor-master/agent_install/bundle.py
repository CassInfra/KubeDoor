"""网页与命令行共用的 Agent 安装定义；渲染本身不连接 Kubernetes。

安装向导（kubedoor-setup）和旧脚本按文件路径加载这个文件：不能 import master 的其他模块，
网页安装要用的控制端设置（通用配置）由 master 的调用方传进 defaults。
"""
from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import yaml


# 采集组件：vmagent 是 install（安装）/ reuse（不安装，用已有的 Prometheus / vmagent 采集）；
# kube-state-metrics、node-exporter 是 auto（自动检测：已有就复用，没有就安装）/ install / reuse（不安装，用集群里已有的）。
# mode 是页面上的安装方式，只决定三个组件的默认值：full 完整安装（vmagent 安装，其余自动检测），
# agent 仅安装 kubedoor-agent（三个都不装）。只传 mode 不传 vmagent（安装向导、本次改动前的安装任务）时按 mode 定三个组件
BASE = {
    "namespace": "kubedoor", "master_url": "", "env": "", "aliases": [],
    "mode": "full", "image_repo": "swr.cn-south-1.myhuaweicloud.com/starsl.cn",
    "agent_tag": "2.6.0", "oss_url": "", "remote_write_url": "",
    "vmagent": "install", "ksm": "auto", "node_exporter": "auto", "image_pull_secret": "",
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
FIELDS = {"namespace", "master_url", "env", "aliases", "mode", "agent_tag", "vmagent", "ksm", "node_exporter",
          "image_pull_secret", "notification"}
# 必须是字符串、去掉首尾空白的参数
STRINGS = (FIELDS - {"aliases", "notification"}) | {"image_repo", "oss_url", "remote_write_url"}
# master 从通用配置取来传给 defaults 的控制端设置（master_url 是通用配置的 KubeDoor 访问地址）
SETTINGS = ("master_url", "prom_k8s_tag_key", "image_repo", "image_vmagent", "image_kube_state_metrics",
            "image_node_exporter", WRITE_KEY)
FILES = ("00-namespace.yaml", "10-agent.yaml", "20-vmagent.yaml", "21-kube-state-metrics.yaml", "22-node-exporter.yaml")
# 自带 kube-state-metrics 的 Service 名（21-kube-state-metrics.yaml）和它出对象指标的端口（8081 是它自身的运行指标）
KSM_SERVICE = "kube-state-metrics"
KSM_PORT = 8080
# Service 名、命名空间都是 DNS-1035 / 1123 标签：只有小写字母、数字和中划线，写进抓取配置的正则不用转义
DNS_LABEL = re.compile(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?")


def defaults(settings: dict | None = None) -> dict:
    """网页安装的默认参数：BASE 加上 master 从通用配置取来的控制端设置（键见 SETTINGS，空值沿用 BASE）。

    能不能安装 vmagent（metrics_available）按有没有远程写地址判断。不读环境变量：控制端的配置都在通用配置里。
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
    if "mode" in raw and "vmagent" not in raw:
        result["vmagent"] = ""  # 只给了安装方式：下面按安装方式定三个采集组件
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
    if not result["vmagent"]:
        # 安装向导、本次改动前的安装任务只有安装方式：完整安装装 vmagent，仅安装 kubedoor-agent 什么采集组件都不装
        result["vmagent"] = "install" if result["mode"] == "full" else "reuse"
        if result["mode"] == "agent":
            result["ksm"] = result["node_exporter"] = "reuse"
    if result["vmagent"] not in ("install", "reuse"):
        raise ValueError("vmagent 必须为 install / reuse")
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


def installs_vmagent(options: dict) -> bool:
    """这次安装装不装 vmagent。本次改动前的安装任务没有 vmagent 这一项：按安装方式，完整安装才装"""
    return (options.get("vmagent") or ("install" if options.get("mode") == "full" else "reuse")) == "install"


def public_options(options: dict) -> dict:
    """给页面看的安装参数：只有 FIELDS 和能不能安装 vmagent；通知配置只给 IM 类型、@ 人和是否填了机器人地址"""
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


def pin_ksm_job(scrape: dict, options: dict) -> None:
    """网页安装：vmagent 的 kube-state-metrics 抓取任务只抓一个 kube-state-metrics 的 8080 端口。

    自带的：本命名空间里的 kube-state-metrics Service。复用已有的：预检选中的那个（ksm_target），锁定它所在的
    命名空间；同一个命名空间里还有别的 kube-state-metrics 时再按 Service 名锁定（只锁命名空间，对方重装改了
    Service 名也还能抓到）。集群里有多个 kube-state-metrics 时全集群都抓，指标会重复、按 Pod 数之类的统计会翻倍。
    没有预检结果（以前的安装计划、不装 vmagent）时不改，和以前一样全集群找。
    """
    if options["ksm"] == "reuse":
        target = options.get("ksm_target")
        if not isinstance(target, dict):
            return
        namespace, name = target.get("namespace"), target.get("name") if target.get("pin_name") else None
    else:
        namespace, name = options["namespace"], KSM_SERVICE
    if not isinstance(namespace, str) or not DNS_LABEL.fullmatch(namespace) or (
            name is not None and (not isinstance(name, str) or not DNS_LABEL.fullmatch(name))):
        raise ValueError("kube-state-metrics 的命名空间或 Service 名称无效，请重新检测")
    job = next((item for item in scrape.get("scrape_configs") or [] if item.get("job_name") == "kube-state-metrics"), None)
    if job is None:
        return
    job["kubernetes_sd_configs"] = [{"role": "service", "namespaces": {"names": [namespace]}}]
    relabels = job.setdefault("relabel_configs", [])
    # 按地址里的端口筛（role: service 的地址是「服务名.命名空间[.svc]:端口」）：__meta_kubernetes_service_port_number
    # 要 vmagent v1.81 以上才有，通用配置里换成更老的 vmagent 镜像时会把目标全筛掉
    relabels.append({"source_labels": ["__address__"], "regex": f".+:{KSM_PORT}", "action": "keep"})
    if name:
        relabels.append({"source_labels": ["__meta_kubernetes_service_name"], "regex": name, "action": "keep"})


def ensure_webhook_tls_secret_manifest(namespace: str = "kubedoor") -> dict:
    """为 kubedoor-agent Webhook 生成集群持久化的 TLS Secret 清单（包含 ca.crt、tls.crt、tls.key）。"""
    if not isinstance(namespace, str) or not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", namespace):
        raise ValueError("命名空间必须是有效的 Kubernetes 名称")

    from datetime import datetime, timedelta, timezone
    import ipaddress
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    now = datetime.now(timezone.utc)
    not_before = now - timedelta(minutes=5)
    not_after = now + timedelta(days=3650)

    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "kubedoor-agent-ca")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )

    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"kubedoor-agent.{namespace}.svc")])
    san = x509.SubjectAlternativeName([
        x509.DNSName("kubedoor-agent"),
        x509.DNSName(f"kubedoor-agent.{namespace}"),
        x509.DNSName(f"kubedoor-agent.{namespace}.svc"),
        x509.DNSName(f"kubedoor-agent.{namespace}.svc.cluster.local"),
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
    ])
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(leaf_name)
        .issuer_name(ca_cert.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(leaf_key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .add_extension(san, critical=False)
        .sign(ca_key, hashes.SHA256())
    )

    ca_pem = ca_cert.public_bytes(serialization.Encoding.PEM)
    tls_crt_pem = leaf_cert.public_bytes(serialization.Encoding.PEM)
    tls_key_pem = leaf_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": "kubedoor-agent-webhook-tls",
            "namespace": namespace,
            "labels": {"app.kubernetes.io/managed-by": "kubedoor-installer"},
        },
        "type": "kubernetes.io/tls",
        "data": {
            "ca.crt": base64.b64encode(ca_pem).decode("ascii"),
            "tls.crt": base64.b64encode(tls_crt_pem).decode("ascii"),
            "tls.key": base64.b64encode(tls_key_pem).decode("ascii"),
        },
    }


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
    vmagent = options["vmagent"] == "install"
    if vmagent and not rw:
        raise ValueError("没有时序库写入地址：安装 vmagent 要先在系统管理 → 通用配置填写远程写地址，或选择不安装 vmagent")
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
    if vmagent:
        selected.append(FILES[2])
    # 三个采集组件各自决定装不装；自动检测（auto）在预检时已换成 install 或 reuse，没检测过的按安装处理
    if options["ksm"] != "reuse":
        selected.append(FILES[3])
    if options["node_exporter"] != "reuse":
        selected.append(FILES[4])
    # 旧脚本按 ENABLE_* 变量启停三个采集组件
    if options.get("_legacy_components"):
        selected = list(FILES[:2]) + options["_legacy_components"]
    resources = [obj for filename in selected for obj in _load_file(filename, variables, options["ksm"] != "reuse")]
    # 通知不再由 agent 直接发送（经 master 统一发送），agent 里不保存机器人 token。
    # 对象存储地址：网页安装为空串（master 每次转发 /api/pod/* 时带上通用配置里的地址），旧脚本照旧写入 OSS_URL
    internal_token = hmac.new(
        f"{username}:{password}".encode("utf-8"),
        f"kubedoor-internal-token:{options['namespace']}:{options['env']}".encode("utf-8"),
        "sha256",
    ).hexdigest()
    secret_data = {
        "KUBEDOOR_MASTER": ws,
        "OSS_URL": options.get("oss_url", ""),
        "KUBEDOOR_INTERNAL_TOKEN": internal_token,
    }
    secret = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "kubedoor-agent-credentials", "namespace": options["namespace"]}, "type": "Opaque", "data": {k: base64.b64encode(v.encode()).decode() for k, v in secret_data.items()}}
    resources.insert(1, secret)
    if vmagent and (rw_user or rw_pass or rp.query):
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
            pin_ksm_job(scrape, options)
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
    d["vmagent"] = "install" if d["mode"] == "full" else "reuse"
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
        names = {
            "00-namespace.yaml": {"Namespace"},
            "10-agent.yaml": {
                "kubedoor-agent",
                "kubedoor-agent-config",
                "kubedoor-agent-cr",
                "kubedoor-agent-crb",
                "kubedoor-agent-credentials",
                "kubedoor-agent-netpol",
                "kubedoor-ai-readonly",
                "kubedoor-ai-readonly-cr",
                "kubedoor-ai-readonly-crb",
            },
            "20-vmagent.yaml": {"vmagent", "vmagent-config", "kubedoor-vmagent", "kubedoor-metrics-credentials"},
            "21-kube-state-metrics.yaml": {"kube-state-metrics", "kubedoor-state-metrics"},
            "22-node-exporter.yaml": {"node-exporter"},
        }
        resources = [obj for obj in resources if obj["kind"] in names.get(name, set()) or obj["metadata"]["name"] in names.get(name, set())]
    print(yaml.safe_dump_all(resources, allow_unicode=True, sort_keys=False))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
