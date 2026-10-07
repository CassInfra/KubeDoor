"""Existing, cluster-scoped KubeDoor business operations.

These contracts mirror the web API and master/agent handlers. URLs and cluster
identifiers never come from the model. Unsupported APIs still belong to the
general Kubernetes executor, rather than an arbitrary HTTP forwarding tool.
"""
from __future__ import annotations

import copy
import math
from datetime import date, datetime

import yaml

from .domain import AIError


EXTRA_CATALOG = {
    "pod_list": ("GET", "/api/agent/pods", "live", True),
    "delete_pods": ("DELETE", "/api/pod/delete_pods", "live", False),
    "node_list": ("GET", "/api/nodes/list", "live", True),
    "cordon_nodes": ("POST", "/api/nodes/cordon", "live", False),
    "uncordon_nodes": ("POST", "/api/nodes/uncordon", "live", False),
    "service_endpoints": ("GET", "/api/agent/service/endpoints", "live", True),
    "service_first_port": ("GET", "/api/agent/service/first-port", "live", True),
    "ingress_rules": ("GET", "/api/agent/ingress/rules", "live", True),
    "resource_content": ("GET", "/api/agent/res/content", "live", True),
    "resource_apply": ("POST", "/api/agent/res/ops", "live", False),
    "resource_delete": ("DELETE", "/api/agent/res/delete", "live", False),
    "statefulset_pods": ("GET", "/api/agent/statefulset/pods", "live", True),
    "restart_statefulset": ("POST", "/api/agent/statefulset/restart", "live", False),
    "scale_statefulset": ("POST", "/api/agent/statefulset/scale", "live", False),
    "daemonset_pods": ("GET", "/api/agent/daemonset/pods", "live", True),
    "restart_daemonset": ("POST", "/api/agent/daemonset/restart", "live", False),
    "image_tags": ("POST", "/api/image/tags", "live", True),
    "node_resource_rank": ("GET", "/api/prom_node_rank", "metrics", True),
    "cci_schedule_profile": ("GET", "/api/cci/schedule-profile", "live", True),
    # Analysis stores a pending plan in master, so it is not a read-only call.
    "load_balance_analyze": ("POST", "/api/load-balance/analyze", "live", False),
    "load_balance_execute": ("POST", "/api/load-balance/execute", "live", False),
    "load_balance_isolated": ("GET", "/api/load-balance/isolated", "live", True),
    "load_balance_cleanup": ("POST", "/api/load-balance/cleanup", "live", False),
    "load_balance_node_cpu": ("GET", "/api/load-balance/nodes-cpu", "live", True),
    "load_balance_check_pods": ("POST", "/api/load-balance/check-pods", "live", True),
    "stored_namespaces": ("GET", "/api/db/res/namespaces", "stored", True),
    "stored_deployments": ("GET", "/api/db/res/deployments", "stored", True),
    "resource_max_day": ("GET", "/api/db/res/max_day", "stored", True),
    "resource_config_update": ("POST", "/api/db/res/edit", "stored", False),
    "resource_pod_count": ("POST", "/api/db/res/pod_count", "stored", False),
    "alert_total": ("POST", "/api/db/alert/total", "historical", True),
    "alert_detail": ("POST", "/api/db/alert/detail", "historical", True),
    "alert_detail_total": ("POST", "/api/db/alert/detail_total", "historical", True),
    "event_history": ("POST", "/api/events/query", "historical", True),
}

_NS = {"namespace": "string；Kubernetes 命名空间"}
_DEP = {**_NS, "deployment": "string；Deployment 名称"}
_RESOURCE = {**_NS, "resource_name": "string；资源名称", "resource_type": "service/deployment/node/pod/configmap/secret/ingress/pvc/statefulset/daemonset/job/cronjob；单数类型"}
_ALERT = {
    **_NS, "pod": "string；Pod 精确名称", "alertName": "string[]；告警名称",
    "status": "string[]；firing/resolved", "severity": "string[]；告警级别",
    "operate": "string；处理状态", "startTime": "ISO 日期时间；起始时间，不带时区按 Asia/Shanghai",
    "silenced": "string 0/1；未屏蔽/已屏蔽；省略不过滤",
}
EXTRA_SCHEMAS = {
    "pod_list": ([], {**_NS, "namespaces": "string[]；多个命名空间；空数组表示全部，优先于 namespace", "node_name": "string；筛选节点"}, "集群 Pod 列表与容器状态、CPU/内存，支持多个命名空间和指定节点；无需自己拼 K8S API"),
    "delete_pods": (["pods"], {"pods": "非空 object[]；每项 {namespace:string,pod:string}，批量删除目标 Pod"}, "批量删除指定 Pod；控制器可能创建替代 Pod，需批准"),
    "node_list": ([], {"simple": "boolean；true 只返回节点名称；默认 false 返回系统、资源、磁盘、Pod 数量及调度状态"}, "现有节点管理页面的详细节点列表"),
    "cordon_nodes": (["node_names"], {"node_names": "非空 string[]；节点名称"}, "批量禁止节点接受新调度（cordon），不会驱逐现有 Pod；需批准"),
    "uncordon_nodes": (["node_names"], {"node_names": "非空 string[]；节点名称"}, "批量恢复节点调度（uncordon）；需批准"),
    "service_endpoints": (["namespace", "service_name"], {**_NS, "service_name": "string；Service 名称"}, "查询 Service 后端 Endpoints、地址及端口，用于排查服务无后端"),
    "service_first_port": (["namespace", "service_name"], {**_NS, "service_name": "string；Service 名称"}, "获取 Service 第一个端口"),
    "ingress_rules": (["namespace", "ingress_name"], {**_NS, "ingress_name": "string；Ingress 名称"}, "查询 Ingress 域名、路径及后端 Service/端口规则"),
    "resource_content": (["namespace", "resource_type", "resource_name"], _RESOURCE, "读取现有资源 YAML，含 Job/CronJob；PVC 可用 pvc 或 persistentvolumeclaim；CRD 使用通用 Kubernetes API"),
    "resource_apply": (["yaml_content"], {"yaml_content": "string；完整 K8S YAML/JSON，可多文档，metadata.name 必填，命名空间写在 metadata.namespace；标准资源含 CronJob，不支持 CRD", "method": "apply/replace/create；默认 apply"}, "现有资源编辑页面接口；申请、替换或创建标准 K8S 资源，需批准。Deployment 的立即、定时、周期重启优先使用对应 restart 工具，勿构造重启 CronJob。此接口不修改 Istio 共享数据库模板"),
    "resource_delete": (["namespace", "resource_type", "resource_name"], _RESOURCE, "删除现有标准 K8S 资源，包括 Job/CronJob，需批准；CRD 用通用 Kubernetes API"),
    "statefulset_pods": (["namespace", "statefulset"], {**_NS, "statefulset": "string；StatefulSet 名称"}, "StatefulSet 的 Pod 明细、资源及事件"),
    "restart_statefulset": (["namespace", "statefulset"], {**_NS, "statefulset": "string；StatefulSet 名称"}, "StatefulSet 立即滚动重启，需批准；Deployment 定时/周期重启接口不适用 StatefulSet"),
    "scale_statefulset": (["namespace", "statefulset", "replicas"], {**_NS, "statefulset": "string；StatefulSet 名称", "replicas": "nonnegative integer；目标副本数"}, "StatefulSet 立即扩缩容，需批准"),
    "daemonset_pods": (["namespace", "daemonset"], {**_NS, "daemonset": "string；DaemonSet 名称"}, "DaemonSet 的 Pod 明细、资源及事件"),
    "restart_daemonset": (["namespace", "daemonset"], {**_NS, "daemonset": "string；DaemonSet 名称"}, "DaemonSet 立即滚动重启，需批准；Deployment 定时/周期重启接口不适用 DaemonSet"),
    "image_tags": (["namespace", "deployment"], _DEP, "查询 Deployment 镜像仓库可用标签及当前标签，沿用 KubeDoor 仓库配置，未修改镜像"),
    "node_resource_rank": ([], {**_DEP, "type": "cpu/mem/pod/peak_cpu/peak_mem；默认 cpu"}, "查询 VictoriaMetrics 中节点资源排名，可带 Deployment 查询该服务各节点 Pod 数"),
    "cci_schedule_profile": (["namespace", "deployment"], _DEP, "查询 Deployment 的华为 CCI ScheduleProfile 是否存在及 maxNum"),
    "load_balance_analyze": ([], {}, "按现有负载均衡配置分析当前集群并生成迁移计划；会更新待执行计划但不会立即迁移，需批准。仅操作当前集群，不修改全局配置"),
    "load_balance_execute": (["migrations"], {"migrations": "非空 object[]；来自当前集群分析结果，每项至少 {namespace,pod_name,deployment,target_node}；可带 source_node,cpu_used（非负数）"}, "执行已审阅的当前集群 Pod 迁移清单，隔离旧 Pod 并等待新 Pod 调度；需批准，执行后检查新 Pod 再清理旧 Pod"),
    "load_balance_isolated": ([], {"exclude_namespaces": "string[]；排除命名空间；省略沿用默认 kube-system/kube-public/istio-system"}, "查询当前集群被负载均衡隔离的旧 Pod"),
    "load_balance_cleanup": (["pods"], {"pods": "非空 object[]；每项 {namespace:string,name:string}，名称取自 isolated 查询结果"}, "删除选中的隔离旧 Pod，先核实替代 Pod Ready；需批准"),
    "load_balance_node_cpu": ([], {}, "获取负载均衡页面节点 CPU、内存、Pod 数及调度状态"),
    "load_balance_check_pods": (["pods"], {"pods": "非空 object[]；每项 {namespace:string,pod_name:string}，检查新 Pod Ready"}, "查询迁移后 Pod 状态与 Ready，无写入"),
    "stored_namespaces": ([], {}, "当前集群已入库的命名空间；Agent 离线也可查"),
    "stored_deployments": (["namespace"], _NS, "当前集群指定命名空间已入库的 Deployment 名称；Agent 离线也可查"),
    "resource_max_day": ([], {}, "当前集群资源管控采集记录最多的日期，返回 d（YYYY-MM-DD），用于挑选历史快照日期"),
    "resource_config_update": (["namespace", "deployment", "limit_mem_mb", "limit_cpu_m", "pod_count_manual"], {**_DEP, "limit_mem_mb": "nonnegative integer；内存 limit，单位 MB", "limit_cpu_m": "nonnegative integer；CPU limit，单位 millicores", "pod_count_manual": "nonnegative integer；人工管控副本数", "jvm_xms_bytes": "integer 0..9007199254740991 或 null；JVM Xms 字节，null 显式清除，省略保留", "jvm_xmx_bytes": "integer 0..9007199254740991 或 null；JVM Xmx 字节，null 显式清除，省略保留", "jvm_xss_bytes": "integer 0..9007199254740991 或 null；JVM Xss 字节，null 显式清除，省略保留", "jvm_max_metaspace_bytes": "integer 0..9007199254740991 或 null；JVM MaxMetaspaceSize 字节，null 显式清除，省略保留"}, "更新当前集群 Deployment 的数据库资源管控配置，需批准；仅保存配置，下一次发布或滚动重启时应用，不会立即修改在线 K8S 资源；不能把保存配置报告成已生效"),
    "resource_pod_count": (["namespace", "deployment", "pod_count_manual"], {**_DEP, "pod_count_manual": "nonnegative integer；人工管控副本数"}, "只更新 Deployment 数据库资源管控的人工副本数，需批准；不会立即扩缩容在线 Deployment，立即扩缩容应使用 scale_deployment"),
    "alert_total": ([], {"startTime": _ALERT["startTime"]}, "当前集群历史告警按名称聚合，包含 firing/resolved 计数；不改变告警状态"),
    "alert_detail": ([], {**_ALERT, "page": "integer >=1；默认 1", "pageSize": "integer 1..500；默认 20"}, "分页查询当前集群已入库告警详情，可过滤 Pod、命名空间、名称、级别、状态与起始时间"),
    "alert_detail_total": ([], _ALERT, "当前集群告警过滤结果总数，与 alert_detail 使用相同过滤条件"),
    "event_history": (["start_time", "end_time"], {**_NS, "start_time": "YYYY-MM-DD；开始日期（包含）", "end_time": "YYYY-MM-DD；结束日期（包含）", "limit": "integer 1..5000；默认 100", "count": "nonnegative integer；最小事件次数", "level": "Normal/Warning", "kind": "string；资源 Kind", "name": "string；资源名称", "reason": "string；原因", "reporting_component": "string；上报组件", "reporting_instance": "string；上报实例", "message": "string；消息过滤"}, "查询当前集群已入库 K8S 历史事件，日期按 Asia/Shanghai，支持资源、原因与消息过滤；无需在线 Agent"),
}

_CONTENT_TYPES = {"service", "deployment", "node", "pod", "configmap", "secret", "ingress", "persistentvolumeclaim", "daemonset", "statefulset", "job", "cronjob"}
_DELETE_TYPES = (_CONTENT_TYPES - {"persistentvolumeclaim"}) | {"pvc"}
_APPLY_KINDS = {
    "v1": {"Pod", "Service", "ConfigMap", "Secret", "Namespace", "PersistentVolume", "PersistentVolumeClaim", "ServiceAccount", "Node", "ResourceQuota", "LimitRange"},
    "apps/v1": {"Deployment", "StatefulSet", "DaemonSet", "ReplicaSet"},
    "batch/v1": {"Job", "CronJob"},
    "networking.k8s.io/v1": {"Ingress", "NetworkPolicy", "IngressClass"},
    "rbac.authorization.k8s.io/v1": {"Role", "RoleBinding", "ClusterRole", "ClusterRoleBinding"},
    "storage.k8s.io/v1": {"StorageClass", "VolumeAttachment"},
}
_LIST_FIELDS = {"namespaces", "node_names", "exclude_namespaces", "alertName", "status", "severity"}
_STRUCTURED_FIELDS = {"pods", "migrations"}
_INTEGER_FIELDS = {"replicas", "page", "pageSize", "limit", "count", "limit_mem_mb", "limit_cpu_m", "pod_count_manual"}
_JVM_FIELDS = {"jvm_xms_bytes", "jvm_xmx_bytes", "jvm_xss_bytes", "jvm_max_metaspace_bytes"}


def _string(value, field, *, allow_empty=False):
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise AIError(f"{field} 必须为{'非空' if not allow_empty else ''}字符串")


def _resource_type(operation, value):
    value = value.lower()
    if operation == "resource_content" and value == "pvc":
        value = "persistentvolumeclaim"
    if operation == "resource_delete" and value == "persistentvolumeclaim":
        value = "pvc"
    return value


def validate_arguments(operation: str, args: dict) -> None:
    """Reject malformed business calls before arguments enter the approval ledger."""
    if operation not in EXTRA_CATALOG:
        raise AIError("未知的 KubeDoor 业务工具")
    if not isinstance(args, dict):
        raise AIError("工具 arguments 必须为对象")
    required, hints, _ = EXTRA_SCHEMAS[operation]
    for field in required:
        if field not in args or args[field] is None:
            raise AIError(f"{operation} 需要 {field}")
    for field in hints:
        if field not in args:
            continue
        value = args[field]
        if field in _LIST_FIELDS:
            if not isinstance(value, list) or (field == "node_names" and not value):
                raise AIError(f"{field} 必须为{'非空' if field == 'node_names' else ''}字符串数组")
            for item in value:
                _string(item, field)
                if field in ("namespaces", "exclude_namespaces") and "," in item:
                    raise AIError(f"{field} 每项只能指定一个命名空间")
        elif field in _INTEGER_FIELDS:
            minimum = 1 if field in ("page", "pageSize", "limit") else 0
            maximum = {"pageSize": 500, "limit": 5000}.get(field)
            if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
                raise AIError(f"{field} 必须为 {minimum}..{maximum if maximum else '∞'} 的整数")
        elif field == "simple":
            if type(value) is not bool:
                raise AIError("simple 必须为 boolean")
        elif field in _JVM_FIELDS:
            if value is not None and (type(value) is not int or not 0 <= value <= 9007199254740991):
                raise AIError(f"{field} 必须为 0..9007199254740991 的整数字节数或 null")
        elif field in _STRUCTURED_FIELDS:
            if not isinstance(value, list) or not value:
                raise AIError(f"{field} 必须为非空对象数组")
            keys = {"delete_pods": ("namespace", "pod"), "load_balance_cleanup": ("namespace", "name"), "load_balance_check_pods": ("namespace", "pod_name"), "load_balance_execute": ("namespace", "pod_name", "deployment", "target_node")}[operation]
            for item in value:
                if not isinstance(item, dict):
                    raise AIError(f"{field} 每项必须为对象")
                for key in keys:
                    _string(item.get(key), f"{field}.{key}")
                if field == "migrations":
                    if "source_node" in item:
                        _string(item["source_node"], "migrations.source_node")
                    if "cpu_used" in item and (type(item["cpu_used"]) not in (int, float) or not math.isfinite(item["cpu_used"]) or item["cpu_used"] < 0):
                        raise AIError("migrations.cpu_used 必须为非负有限数字")
        else:
            _string(value, field, allow_empty=field not in required)
    if operation == "node_resource_rank" and args.get("type", "cpu") not in ("cpu", "mem", "pod", "peak_cpu", "peak_mem"):
        raise AIError("type 必须为 cpu/mem/pod/peak_cpu/peak_mem")
    if operation == "resource_config_update":
        xms, xmx = args.get("jvm_xms_bytes"), args.get("jvm_xmx_bytes")
        if xms is not None and xmx is not None and xmx > 0 and xms > xmx:
            raise AIError("Xms 不能大于 Xmx")
    if operation in ("resource_content", "resource_delete"):
        supported = _CONTENT_TYPES if operation == "resource_content" else _DELETE_TYPES
        if _resource_type(operation, args["resource_type"]) not in supported:
            raise AIError("现有资源接口不支持该类型；请使用通用 Kubernetes API")
    if operation == "resource_apply":
        if args.get("method", "apply") not in ("apply", "replace", "create"):
            raise AIError("method 必须为 apply/replace/create")
        try:
            documents = [doc for doc in yaml.safe_load_all(args["yaml_content"]) if doc is not None]
        except yaml.YAMLError:
            raise AIError("yaml_content 必须为有效的 K8S YAML/JSON") from None
        if not documents:
            raise AIError("yaml_content 不能是空文档")
        for doc in documents:
            if not isinstance(doc, dict) or not isinstance(doc.get("apiVersion"), str) or not isinstance(doc.get("kind"), str) or doc["kind"] not in _APPLY_KINDS.get(doc["apiVersion"], set()):
                raise AIError("现有资源接口不支持该 apiVersion/kind；CRD 等资源请用通用 Kubernetes API")
            metadata = doc.get("metadata")
            if not isinstance(metadata, dict):
                raise AIError("每个 YAML 资源必须包含 metadata.name")
            _string(metadata.get("name"), "metadata.name")
            if "namespace" in metadata:
                _string(metadata["namespace"], "metadata.namespace")
    if operation in ("alert_total", "alert_detail", "alert_detail_total"):
        if args.get("startTime"):
            try:
                datetime.fromisoformat(args["startTime"])
            except ValueError:
                raise AIError("startTime 必须为 ISO 日期时间") from None
        if args.get("silenced") not in (None, "", "0", "1"):
            raise AIError("silenced 必须为 0 或 1")
        if "status" in args and any(status not in ("firing", "resolved") for status in args["status"]):
            raise AIError("status 必须为 firing/resolved 数组")
    if operation == "event_history":
        try:
            if any(len(args[field]) != 10 or args[field][4] != "-" or args[field][7] != "-" for field in ("start_time", "end_time")):
                raise ValueError()
            start, end = date.fromisoformat(args["start_time"]), date.fromisoformat(args["end_time"])
        except ValueError:
            raise AIError("start_time/end_time 必须为 YYYY-MM-DD 日期") from None
        if start > end:
            raise AIError("start_time 不能晚于 end_time")
        if args.get("level") not in (None, "", "Normal", "Warning"):
            raise AIError("level 必须为 Normal/Warning")


def build_request(operation: str, args: dict, env: str) -> tuple[dict, dict | None]:
    """Map approved logical arguments to a fixed existing API contract."""
    validate_arguments(operation, args)
    _string(env, "env")
    for key in ("env", "k8s"):
        if args.get(key) not in (None, env):
            raise AIError("工具试图操作其他集群", 403, "scope_violation")
    query, body = {"env": env}, None
    # Filter to declared keys; arbitrary URLs, paths, credentials, config and
    # cluster identifiers cannot become query/body parameters.
    allowed = EXTRA_SCHEMAS[operation][1]
    values = {key: copy.deepcopy(args[key]) for key in allowed if key in args}
    if operation == "pod_list":
        namespaces = values.get("namespaces", [values["namespace"]] if values.get("namespace") else [])
        if namespaces:
            query["namespaces"] = ",".join(namespaces)
        if values.get("node_name"):
            query["node_name"] = values["node_name"]
    elif operation == "node_list":
        if "simple" in values:
            query["simple"] = "true" if values["simple"] else "false"
    elif operation == "delete_pods":
        body = {"pods": [{"ns": pod["namespace"], "pod_name": pod["pod"]} for pod in values["pods"]]}
    elif operation == "resource_apply":
        query["method"] = values.get("method", "apply")
        body = {"yaml_content": values["yaml_content"]}
    elif operation in ("cordon_nodes", "uncordon_nodes", "restart_statefulset", "scale_statefulset", "restart_daemonset"):
        body = values
    elif operation == "image_tags":
        body = {**values, "k8s": env}
    elif operation in ("resource_config_update", "resource_pod_count"):
        body = {**values, "env": env}
        if operation == "resource_pod_count":
            body["deployment_name"] = body.pop("deployment")
    elif operation == "load_balance_execute":
        fields = ("namespace", "pod_name", "deployment", "target_node", "source_node", "cpu_used")
        body = {"migrations": [{key: item[key] for key in fields if key in item} for item in values["migrations"]]}
    elif operation in ("load_balance_cleanup", "load_balance_check_pods"):
        fields = ("namespace", "name" if operation == "load_balance_cleanup" else "pod_name")
        body = {"pods": [{key: item[key] for key in fields} for item in values["pods"]]}
    elif operation == "load_balance_analyze":
        body = {}
    elif operation == "load_balance_isolated":
        if "exclude_namespaces" in values:
            query["exclude_namespaces"] = ",".join(values["exclude_namespaces"])
    elif operation in ("alert_total", "alert_detail", "alert_detail_total"):
        body = {**values, "env": env if operation == "alert_total" else [env]}
        if operation == "alert_detail":
            body.setdefault("page", 1)
            body.setdefault("pageSize", 20)
    elif operation == "event_history":
        body = {**values, "k8s": env}
        body.setdefault("limit", 100)
        for snake, camel in (("reporting_component", "reportingComponent"), ("reporting_instance", "reportingInstance")):
            if snake in body:
                body[camel] = body.pop(snake)
    else:
        query.update(values)
        if operation in ("resource_content", "resource_delete"):
            query["resource_type"] = _resource_type(operation, values["resource_type"])
        if operation == "node_resource_rank":
            query.setdefault("type", "cpu")
    return query, body
