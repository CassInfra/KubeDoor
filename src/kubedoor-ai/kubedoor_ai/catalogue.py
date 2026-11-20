"""Existing, cluster-scoped KubeDoor business operations.

These contracts mirror the web API and master/agent handlers. URLs and cluster
identifiers never come from the model. Unsupported APIs still belong to the
general Kubernetes executor, rather than an arbitrary HTTP forwarding tool.
"""
from __future__ import annotations

import copy
import math
import re
from datetime import date, datetime

import yaml

from .domain import AIError, BEIJING_TZ


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
    # 花折资源管控：网关按管控状态生成执行计划（control.py）。调整资源 / JVM 没有对应的单个接口，路径留空
    "deployment_control": ("GET", "/api/ai/internal/deployment-control", "live", True),
    "update_deployment_resources": ("POST", "", "live", False),
    "update_deployment_jvm": ("POST", "", "live", False),
    "resource_control_add": ("POST", "/api/db/res/add", "stored", False),
    "alert_total": ("POST", "/api/db/alert/total", "historical", True),
    "alert_detail": ("POST", "/api/db/alert/detail", "historical", True),
    "alert_detail_total": ("POST", "/api/db/alert/detail_total", "historical", True),
    "event_history": ("POST", "/api/events/query", "historical", True),
    # 获取代码（master func_manager/code_info.py）；源码下载、搜索、读取在 kubedoor-ai 本地执行（code.py），路径留空
    "code_info": ("GET", "/api/ai/internal/code-info", "live", True),
    "code_fetch": ("GET", "", "live", True),
    "code_search": ("GET", "", "local", True),
    "code_read": ("GET", "", "local", True),
    "code_symbols": ("GET", "", "local", True),
    # 获取运行配置（master func_manager/runtime_config.py）：网关取回后打码再交给模型（runtime_config.py），路径留空
    "runtime_config": ("GET", "", "live", True),
    # 链路追踪（master tracing/）：master 查 Jaeger，配了 ClickHouse 加速时列服务 / 接口、搜索、统计走加速表。
    # 都是只读查询（集群只读及以上，不进「权限与审批」表），参数原样放进 body
    "trace_services": ("POST", "/api/ai/internal/tracing/services", "historical", True),
    "trace_operations": ("POST", "/api/ai/internal/tracing/operations", "historical", True),
    "trace_search": ("POST", "/api/ai/internal/tracing/search", "historical", True),
    "trace_stats": ("POST", "/api/ai/internal/tracing/stats", "historical", True),
    "trace_get": ("POST", "/api/ai/internal/tracing/trace", "historical", True),
    "trace_spans": ("POST", "/api/ai/internal/tracing/spans", "historical", True),
    "trace_fields": ("POST", "/api/ai/internal/tracing/fields", "historical", True),
}
# 源码分析：在网关本地执行的操作（Gateway.execute_code）
CODE_LOCAL = frozenset({"code_fetch", "code_search", "code_read", "code_symbols"})
# 按 Deployment 或 Pod 指定服务（二选一）的源码操作
CODE_TARGET = frozenset({"code_info", "code_fetch"})
# 按 Deployment 或 Pod 指定服务（二选一）的全部操作：没指定时用页面选中的，Pod 优先（Gateway.invoke）
SERVICE_TARGET = CODE_TARGET | {"runtime_config"}
# 链路追踪：参数校验（_validate_trace）和请求（body 就是声明过的参数）单独处理。按 K8S 过滤的参数带 k8s_ 前缀，
# 网关也不给它们填页面选中的命名空间（Gateway.invoke），页面上选了什么都不会变成链路的过滤条件
TRACE_OPERATIONS = frozenset({"trace_services", "trace_operations", "trace_search", "trace_stats", "trace_get", "trace_spans", "trace_fields"})

_NS = {"namespace": "string；Kubernetes 命名空间"}
_DEP = {**_NS, "deployment": "string；Deployment 名称"}
_RESOURCE = {**_NS, "resource_name": "string；资源名称", "resource_type": "service/deployment/node/pod/configmap/secret/ingress/pvc/statefulset/daemonset/job/cronjob；单数类型"}
_ALERT = {
    **_NS, "pod": "string；Pod 精确名称", "alertName": "string[]；告警名称",
    "status": "string[]；firing/resolved", "severity": "string[]；告警级别",
    "operate": "string；处理状态", "startTime": "ISO 日期时间；起始时间，不带时区按 Asia/Shanghai",
    "silenced": "string 0/1；未屏蔽/已屏蔽；省略不过滤",
}
_CODE_TARGET = {**_NS, "deployment": "string；Deployment 名称，和 pod 二选一", "pod": "string；Pod 名称，和 deployment 二选一"}
_CODE_ID = {"code_id": "string；code_fetch 返回的 code_id"}
# 链路追踪的参数（名称、取值和 master 的约定一致）。search、stats 共用的过滤条件都是 ClickHouse 加速表能表达的
_TRACE_TIME = {
    "start": "string；开始时间，北京时间 YYYY-MM-DD HH:mm[:ss]（带时区的 ISO 也认）",
    "end": "string；结束时间，格式同 start，默认现在",
    "lookback": "string；没给 start 时往前查多久，例如 15m、1h、6h、1d，默认 1h；时间窗口最长 7 天",
}
_TRACE_MATCH = {
    "service": "string；服务名（service.name），一般就是 Deployment 名，不确定先用 trace_services 查；不填 = 所有服务",
    "operation": "string；Span 名精确匹配，例如 POST /order/create",
    "span_kind": "server/client/producer/consumer/internal；查接口收到的请求用 server",
    "error": "boolean；true = Span 状态是 error",
    "status_codes": "array，最多 20 项：100..599 的整数，或字符串 \"4xx\"、\"5xx\"；HTTP 状态码等于其中任一个，例如 [504]、[\"5xx\"]",
    "status_min": "integer 100..599；HTTP 状态码范围的下限（含）",
    "status_max": "integer 100..599；HTTP 状态码范围的上限（含）",
    "min_duration": "string；这个 Span 的耗时下限，Go 时长，例如 500ms、3s",
    "max_duration": "string；这个 Span 的耗时上限，格式同 min_duration",
    "method": "string；请求方法，例如 POST",
    "path": "string；请求路径（url.path）精确匹配，例如 /order/create",
    "host": "string；域名或被调方地址（server.address 等）",
    "k8s_pod": "string；Pod 名",
    "k8s_namespace": "string；命名空间",
}
_TRACE_SEARCH = {
    **_TRACE_MATCH,
    "route": "string；路由模板（http.route），例如 /order/{id}",
    "url": "string；完整 URL（url.full）",
    "client_ip": "string；客户端 IP",
    "exception_type": "string；异常类型（exception 事件的 exception.type）",
    "k8s_deployment": "string；Deployment 名",
    "attributes": "object {属性名: 字符串值}，最多 10 项；其他属性精确匹配，数字也写成字符串；业务字段的属性名先用 trace_fields 查",
    "contains": "object {键: 子串}，最多 3 项；不区分大小写的包含匹配，键是 operation、path、query（查询参数）、url、host、k8s_pod 或属性名",
    **_TRACE_TIME,
    "limit": "integer 1..50；最多返回多少条链路，默认 20",
}
_TRACE_STATS = {
    **dict.fromkeys(_TRACE_MATCH, "同 trace_search"),
    "contains": "object {键: 子串}，最多 3 项；不区分大小写的包含匹配，键只能是 operation、path、query（查询参数）、host、k8s_pod",
    **dict.fromkeys(_TRACE_TIME, "同 trace_search"),
    "group_by": "array，最多 2 项：time/service/operation/path/status_code/k8s_pod/k8s_namespace/host/kind/method/status；不填 = 不分组，只看 totals",
    "interval": "1m/5m/10m/1h；group_by 含 time 时每个时间桶多长，不填按窗口自动选",
    "top": "integer 1..100；最多返回多少行，默认 30",
}
_TRACE_ID = {"trace_id": "string；Trace ID，32 位以内的十六进制"}
_TRACE_AROUND = {"around": "string；链路大概发生的时间，北京时间 YYYY-MM-DD HH:mm[:ss]"}
EXTRA_SCHEMAS = {
    "pod_list": ([], {**_NS, "namespaces": "string[]；多个命名空间；空数组表示全部，优先于 namespace", "node_name": "string；筛选节点"}, "集群 Pod 列表与容器状态、CPU/内存，支持多个命名空间和指定节点；无需自己拼 K8S API"),
    "delete_pods": (["pods"], {"pods": "非空 object[]；每项 {namespace:string,pod:string}，批量删除目标 Pod"}, "批量删除指定 Pod；控制器可能创建替代 Pod"),
    "node_list": ([], {"simple": "boolean；true 只返回节点名称；默认 false 返回系统、资源、磁盘、Pod 数量及调度状态"}, "现有节点管理页面的详细节点列表"),
    "cordon_nodes": (["node_names"], {"node_names": "非空 string[]；节点名称"}, "批量禁止节点接受新调度（cordon），不会驱逐现有 Pod"),
    "uncordon_nodes": (["node_names"], {"node_names": "非空 string[]；节点名称"}, "批量恢复节点调度（uncordon）"),
    "service_endpoints": (["namespace", "service_name"], {**_NS, "service_name": "string；Service 名称"}, "查询 Service 后端 Endpoints、地址及端口，用于排查服务无后端"),
    "service_first_port": (["namespace", "service_name"], {**_NS, "service_name": "string；Service 名称"}, "获取 Service 第一个端口"),
    "ingress_rules": (["namespace", "ingress_name"], {**_NS, "ingress_name": "string；Ingress 名称"}, "查询 Ingress 域名、路径及后端 Service/端口规则"),
    "resource_content": (["namespace", "resource_type", "resource_name"], _RESOURCE, "读取现有资源 YAML，含 Job/CronJob；PVC 可用 pvc 或 persistentvolumeclaim；CRD 使用通用 Kubernetes API"),
    "resource_apply": (["yaml_content"], {"yaml_content": "string；完整 K8S YAML/JSON，可多文档，metadata.name 必填，命名空间写在 metadata.namespace；标准资源含 CronJob，不支持 CRD", "method": "apply/replace/create；默认 apply"}, "现有资源编辑页面接口；申请、替换或创建标准 K8S 资源。Deployment 的立即、定时、周期重启优先使用对应 restart 工具，勿构造重启 CronJob。此接口不修改 Istio 共享数据库模板"),
    "resource_delete": (["namespace", "resource_type", "resource_name"], _RESOURCE, "删除现有标准 K8S 资源，包括 Job/CronJob；CRD 用通用 Kubernetes API"),
    "statefulset_pods": (["namespace", "statefulset"], {**_NS, "statefulset": "string；StatefulSet 名称"}, "StatefulSet 的 Pod 明细、资源及事件"),
    "restart_statefulset": (["namespace", "statefulset"], {**_NS, "statefulset": "string；StatefulSet 名称"}, "StatefulSet 立即滚动重启；Deployment 定时/周期重启接口不适用 StatefulSet"),
    "scale_statefulset": (["namespace", "statefulset", "replicas"], {**_NS, "statefulset": "string；StatefulSet 名称", "replicas": "nonnegative integer；目标副本数"}, "StatefulSet 立即扩缩容"),
    "daemonset_pods": (["namespace", "daemonset"], {**_NS, "daemonset": "string；DaemonSet 名称"}, "DaemonSet 的 Pod 明细、资源及事件"),
    "restart_daemonset": (["namespace", "daemonset"], {**_NS, "daemonset": "string；DaemonSet 名称"}, "DaemonSet 立即滚动重启；Deployment 定时/周期重启接口不适用 DaemonSet"),
    "image_tags": (["namespace", "deployment"], _DEP, "查询 Deployment 镜像仓库可用标签及当前标签，沿用 KubeDoor 仓库配置，未修改镜像"),
    "node_resource_rank": ([], {**_DEP, "type": "cpu/mem/pod/peak_cpu/peak_mem；默认 cpu"}, "查询 VictoriaMetrics 中节点资源排名，可带 Deployment 查询该服务各节点 Pod 数"),
    "cci_schedule_profile": (["namespace", "deployment"], _DEP, "查询 Deployment 的华为 CCI ScheduleProfile 是否存在及 maxNum"),
    "load_balance_analyze": ([], {}, "按现有负载均衡配置分析当前集群并生成迁移计划；会更新待执行计划但不会立即迁移。仅操作当前集群，不修改全局配置"),
    "load_balance_execute": (["migrations"], {"migrations": "非空 object[]；来自当前集群分析结果，每项至少 {namespace,pod_name,deployment,target_node}；可带 source_node,cpu_used（非负数）"}, "执行已审阅的当前集群 Pod 迁移清单，隔离旧 Pod 并等待新 Pod 调度；执行后检查新 Pod 再清理旧 Pod"),
    "load_balance_isolated": ([], {"exclude_namespaces": "string[]；排除命名空间；省略沿用默认 kube-system/kube-public/istio-system"}, "查询当前集群被负载均衡隔离的旧 Pod"),
    "load_balance_cleanup": (["pods"], {"pods": "非空 object[]；每项 {namespace:string,name:string}，名称取自 isolated 查询结果"}, "删除选中的隔离旧 Pod，先核实替代 Pod Ready"),
    "load_balance_node_cpu": ([], {}, "获取负载均衡页面节点 CPU、内存、Pod 数及调度状态"),
    "load_balance_check_pods": (["pods"], {"pods": "非空 object[]；每项 {namespace:string,pod_name:string}，检查新 Pod Ready"}, "查询迁移后 Pod 状态与 Ready，无写入"),
    "stored_namespaces": ([], {}, "当前集群已入库的命名空间；Agent 离线也可查"),
    "stored_deployments": (["namespace"], _NS, "当前集群指定命名空间已入库的 Deployment 名称；Agent 离线也可查"),
    "resource_max_day": ([], {}, "当前集群资源管控采集记录最多的日期，返回 d（YYYY-MM-DD），用于挑选历史快照日期"),
    "resource_config_update": (["namespace", "deployment", "limit_mem_mb", "limit_cpu_m", "pod_count_manual"], {**_DEP, "limit_mem_mb": "nonnegative integer；内存 limit，单位 MB", "limit_cpu_m": "nonnegative integer；CPU limit，单位 millicores", "pod_count_manual": "nonnegative integer；人工管控副本数", "jvm_xms_bytes": "integer 0..9007199254740991 或 null；JVM Xms 字节，null 显式清除，省略保留", "jvm_xmx_bytes": "integer 0..9007199254740991 或 null；JVM Xmx 字节，null 显式清除，省略保留", "jvm_xss_bytes": "integer 0..9007199254740991 或 null；JVM Xss 字节，null 显式清除，省略保留", "jvm_max_metaspace_bytes": "integer 0..9007199254740991 或 null；JVM MaxMetaspaceSize 字节，null 显式清除，省略保留"}, "更新已登记服务的花折资源管控配置；只保存，下一次发布或滚动重启时才应用，而且只在受管控时有用，不能报告成已生效。要立即调整用 update_deployment_resources / update_deployment_jvm"),
    "resource_pod_count": (["namespace", "deployment", "pod_count_manual"], {**_DEP, "pod_count_manual": "nonnegative integer；人工管控副本数"}, "只更新已登记服务的管控副本数；不会立即扩缩容。立即扩缩用 scale_deployment（mode=persistent 时会同时写这里）"),
    "deployment_control": (["namespace", "deployment"], _DEP, "查询 Deployment 是否受花折资源管控（准入控制）、原因和管控值。受管控时副本数、requests/limits、JVM 参数只能经管控表生效，直接改 K8S 会被改回；修改这些字段前先查"),
    "update_deployment_resources": (["namespace", "deployment"], {**_DEP, "request_cpu_m": "positive integer；CPU requests，millicores", "request_mem_mb": "positive integer；内存 requests，MiB", "limit_cpu_m": "positive integer；CPU limits，millicores", "limit_mem_mb": "positive integer；内存 limits，MiB", "apply_now": "boolean；默认 true"}, "调整 Deployment 第一个容器的 requests/limits，至少传一项。网关按管控状态执行：受管控时写管控表的 limits（requests 由每日高峰期采集决定，不能改），apply_now=true 再滚动重启使其生效；不受管控时直接修改 Deployment"),
    "update_deployment_jvm": (["namespace", "deployment"], {**_DEP, "jvm_xms_bytes": "positive integer；-Xms 字节", "jvm_xmx_bytes": "positive integer；-Xmx 字节", "jvm_xss_bytes": "positive integer；-Xss 字节", "jvm_max_metaspace_bytes": "positive integer；-XX:MaxMetaspaceSize 字节", "apply_now": "boolean；默认 true"}, "调整第一个容器 java 启动参数（args）里已有的 -Xms/-Xmx/-Xss/-XX:MaxMetaspaceSize，至少传一项。受管控时写管控表，apply_now=true 再滚动重启；不受管控时直接修改 args"),
    "resource_control_add": (["namespace", "deployment", "pod_count_manual", "request_cpu_m", "request_mem_mb", "limit_cpu_m", "limit_mem_mb"], {**_DEP, "pod_count_manual": "nonnegative integer；管控副本数，0 表示暂不启动 Pod", "request_cpu_m": "positive integer；millicores", "request_mem_mb": "positive integer；MiB", "limit_cpu_m": "positive integer；millicores", "limit_mem_mb": "positive integer；MiB", "jvm_xms_bytes": "integer 字节，可省略", "jvm_xmx_bytes": "integer 字节，可省略", "jvm_xss_bytes": "integer 字节，可省略", "jvm_max_metaspace_bytes": "integer 字节，可省略"}, "把新服务登记到花折资源管控表；准入控制开启且命名空间纳入管控时，之后的创建、发布、重启、扩缩容按这些值改写。已登记的服务用 resource_config_update"),
    "alert_total": ([], {"startTime": _ALERT["startTime"]}, "当前集群历史告警按名称聚合，包含 firing/resolved 计数；不改变告警状态"),
    "alert_detail": ([], {**_ALERT, "page": "integer >=1；默认 1", "pageSize": "integer 1..500；默认 20"}, "分页查询当前集群已入库告警详情，可过滤 Pod、命名空间、名称、级别、状态与起始时间"),
    "alert_detail_total": ([], _ALERT, "当前集群告警过滤结果总数，与 alert_detail 使用相同过滤条件"),
    "event_history": (["start_time", "end_time"], {**_NS, "start_time": "YYYY-MM-DD；开始日期（包含）", "end_time": "YYYY-MM-DD；结束日期（包含）", "limit": "integer 1..5000；默认 100", "count": "nonnegative integer；最小事件次数", "level": "Normal/Warning", "kind": "string；资源 Kind", "name": "string；资源名称", "reason": "string；原因", "reporting_component": "string；上报组件", "reporting_instance": "string；上报实例", "message": "string；消息过滤"}, "查询当前集群已入库 K8S 历史事件，日期按 Asia/Shanghai，支持资源、原因与消息过滤；无需在线 Agent"),
    "code_info": (["namespace"], _CODE_TARGET, "查服务当前运行的镜像和代码信息：代码页面 web_url、源码下载 download_url、最近 Commit 说明 commits_url（来自管理员配置的获取代码接口），回答时用 Markdown 链接给用户。按 Pod 查用该 Pod 实际运行的镜像（发布中新旧 Pod 可能不同），排障时用出问题的 Pod"),
    "code_fetch": (["namespace"], _CODE_TARGET, "下载服务当前运行版本的源码到 kubedoor-ai 本地并解压，返回 code_id、目录概要和语言统计，之后用 code_search / code_read 分析；同一镜像只下载一次。排障时用出问题的 Pod。管理员没有开启「允许 AI 下载并分析源码」时返回 ai_download_disabled，这时只能用 code_info 把链接给用户"),
    "code_search": (["code_id"], {**_CODE_ID, "pattern": "string；ripgrep 正则，省略时按 glob 列出文件", "glob": "string；文件过滤，例如 **/*.java、!**/test/**", "fixed_strings": "boolean；pattern 按普通文本匹配（异常消息、带括号的代码片段）", "ignore_case": "boolean", "context": "integer 0..5；匹配行前后带的行数", "max_results": "integer 1..200；默认 50"}, "在 code_fetch 下载的源码里搜索（ripgrep），返回文件路径、行号和匹配行；在 kubedoor-ai 本地执行，不需要在线 Agent"),
    "code_read": (["code_id", "path"], {**_CODE_ID, "path": "string；相对源码根目录的文件路径（code_search 返回的 path）", "start_line": "integer >=1；默认 1", "lines": "integer 1..500；默认 200"}, "按行读取已下载源码里的文本文件，每行前面带行号；在 kubedoor-ai 本地执行，不需要在线 Agent"),
    "code_symbols": (["code_id"], {**_CODE_ID, "name": "string；类、方法、函数等的名字，精确匹配，可写成 Class.method 限定所属类型", "path": "string；相对源码根目录的文件路径；只传 path 返回这个文件的大纲", "kind": "string；按种类过滤，例如 class、interface、method、function、field", "max_results": "integer 1..200；默认 100"}, "用 ctags 找已下载源码里的定义：按名字找类、方法、函数定义在哪个文件哪一行（不含调用处），或者列出一个文件里全部定义的行号；name 和 path 至少传一个；在 kubedoor-ai 本地执行"),
    "runtime_config": (["namespace"], {**_CODE_TARGET, "container": "string；容器名，多容器时指定，默认第一个容器", "keys": "string；按配置名过滤的正则，不区分大小写，最多 500 字符，例如 datasource|redis|elasticsearch；Node.js 的嵌套配置按 a.b.c 完整路径匹配，配置文件不过滤", "detail": "boolean；附带配置来源（优先级从高到低）、打码值从哪里补回、说明", "refresh": "boolean；不用缓存，重新采集"}, "获取服务实际运行的配置（配置中心、环境变量、配置文件合并后的生效值），用于核对连接地址、开关、超时、线程池和配置是否生效，以及查数据库前找连接信息；按 Pod 查用这个 Pod，排障时用出问题的 Pod。先用 keys 缩小范围，不加 keys 返回全部应用配置，太长会截断。密码、密钥是 <secret:…> 占位符，可以原样填进 mysql / redis / elasticsearch 的 password、api_key、sentinel_password（host / url 要用这份配置里写的地址）。管理员没有开启「允许 AI 获取运行配置」时返回 ai_config_disabled"),
    "trace_services": ([], {"pattern": "string；服务名包含的关键字，不区分大小写，最多 100 个字符", "limit": "integer 1..500；默认 200"}, "列出有链路的服务（service.name，一般是 Deployment 名），pattern 按关键字过滤。配了 ClickHouse 加速时只列当前集群最近 24 小时有流量的服务（source=clickhouse），否则是 Jaeger 里所有集群的服务。链路工具都是只读查询，时间按北京时间，排查方法见 Skill kubedoor-trace"),
    "trace_operations": (["service"], {"service": "string；服务名", "span_kind": _TRACE_MATCH["span_kind"], "pattern": "string；Span 名或路径包含的关键字，不区分大小写，最多 100 个字符", "limit": "integer 1..500；默认 200", "lookback": "string；只对 ClickHouse 加速表：统计最近多久，例如 6h、2d，默认 24h，最长 7d"}, "列出服务的接口：Span 名 name、种类 kind，加速表另有路径 path 和 Span 数。服务端 Span 名通常是「方法 + 路由」，路由参数是占位符；网关的服务端 Span 名可能是路由 ID，这时用 trace_search 的 path 或 contains 查"),
    "trace_search": ([], _TRACE_SEARCH, "按条件找链路，从新到旧返回（含 Jaeger 链接 url）。所有条件要落在同一个 Span（含它的资源属性）上；状态码用 status_codes 或 status_min/status_max，报错用 error=true（服务端返回 4xx 一般不算 error）。source=jaeger 时按时间片查、contains 要逐条扫描，看 coverage / scan 是否查完；没查到不等于没发生"),
    "trace_stats": ([], _TRACE_STATS, "用 ClickHouse 加速表统计命中条件的 Span 数、出错数、4xx / 5xx 数和 p50 / p95 / p99 / 最大耗时，可按 group_by 分组；看接口请求量加 span_kind=server。没配加速时返回 not_supported，改用 metrics"),
    "trace_get": (["trace_id"], {**_TRACE_ID, **_TRACE_AROUND, "max_nodes": "integer 50..400；调用树最多显示多少个节点，默认 200", "focus_span": "string；Span ID：只看从根到这个 Span 的路径和它下面三层"}, "按 Trace ID 取整条链路并分析：outcome（failed / client_error / recovered / succeeded / unknown）、折叠的调用树 tree、根因候选 root_cause、关键路径、N+1 和 Jaeger 链接 url。尽量传 around（大概时间）：不给时从近到远逐级放大范围，可能很慢；可能含其他集群的 Span"),
    "trace_spans": (["trace_id"], {**_TRACE_ID, "span_ids": "array，最多 20 项；Span ID（trace_get 返回的 id），十六进制", "errors": "boolean；true = 链路里全部出错的 Span（最多 20 个），和 span_ids 二选一", **_TRACE_AROUND}, "看指定 Span（span_ids，或 errors=true 取全部出错 Span，一次最多 20 个）的属性、资源属性、事件（含异常堆栈）和状态；敏感值已打码，过长的值已截断"),
    "trace_fields": ([], {"service": "string；给了就取这个服务最近的链路，列出实际出现的属性名和示例"}, "查集群字段和取值、各语义字段对应的属性名、业务字段 custom_fields、是否配置了 ClickHouse 加速；给 service 时列出它最近链路里实际出现的属性名和示例。按业务字段（订单号等）查链路前先用它找属性名"),
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
_INTEGER_FIELDS = {"replicas", "page", "pageSize", "limit", "count", "limit_mem_mb", "limit_cpu_m", "pod_count_manual", "request_cpu_m", "request_mem_mb", "context", "max_results", "start_line", "lines"}
# 整数字段的下限默认 0，这些是 1；上限没列的不限
_MIN_ONE = {"page", "pageSize", "limit", "max_results", "start_line", "lines"}
_MAXIMUM = {"pageSize": 500, "limit": 5000, "context": 5, "max_results": 200, "lines": 500}
_BOOLEAN_FIELDS = {"simple", "apply_now", "fixed_strings", "ignore_case", "detail", "refresh"}
_JVM_FIELDS = {"jvm_xms_bytes", "jvm_xmx_bytes", "jvm_xss_bytes", "jvm_max_metaspace_bytes"}
_RESOURCE_FIELDS = {"request_cpu_m", "request_mem_mb", "limit_cpu_m", "limit_mem_mb"}
# 这些操作的 requests/limits 会原样写进 K8S 或管控表，0 没有意义
_POSITIVE_RESOURCES = {"update_deployment_resources", "resource_control_add"}
_DNS_LABEL = re.compile(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?\Z")
_DNS_NAME = re.compile(r"[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?\Z")

# ---- 链路追踪的参数校验（取值范围和 master 的约定一致；master 还会再校验一次）----
_TRACE_TIME_FORMAT = "北京时间 YYYY-MM-DD HH:mm[:ss]（带时区的 ISO 也认）"
_TRACE_MAX_WINDOW = 7 * 86400   # 时间窗口最长 7 天（秒）
_TRACE_TEXT_LIMIT = 2048
_TRACE_CONTAINS_LIMIT = 1024
_TRACE_TEXT_FIELDS = {"service", "operation", "method", "route", "path", "url", "host", "client_ip", "exception_type",
                      "k8s_pod", "k8s_namespace", "k8s_deployment"}
_TRACE_KINDS = ("server", "client", "producer", "consumer", "internal")
_TRACE_GROUP_BY = ("time", "service", "operation", "path", "status_code", "k8s_pod", "k8s_namespace", "host", "kind", "method", "status")
_TRACE_INTERVALS = ("1m", "5m", "10m", "1h")
_TRACE_RANGES = {"status_min": (100, 599), "status_max": (100, 599), "top": (1, 100), "max_nodes": (50, 400)}
# ClickHouse 加速表没有的条件：trace_search 用了它们改查 Jaeger，trace_stats 不支持
_TRACE_JAEGER_ONLY = ("route", "url", "client_ip", "exception_type", "k8s_deployment", "attributes")
_TRACE_STATS_CONTAINS = ("operation", "path", "query", "host", "k8s_pod")
# 写进 contains 会被当成属性名、什么都匹配不到的键：服务、命名空间、Deployment、Pod 用精确匹配的参数
_TRACE_CONTAINS_MISUSE = ("service", "namespace", "k8s_namespace", "deployment", "k8s_deployment", "pod")
_LOOKBACK = re.compile(r"([0-9]{1,7})([smhd])")
_LOOKBACK_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_GO_DURATION = re.compile(r"(?:(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:ns|us|µs|μs|ms|s|m|h))+")
_GO_DURATION_PART = re.compile(r"([0-9]+(?:\.[0-9]*)?|\.[0-9]+)(ns|us|µs|μs|ms|s|m|h)")
_GO_UNITS = {"ns": 1e-9, "us": 1e-6, "µs": 1e-6, "μs": 1e-6, "ms": 1e-3, "s": 1, "m": 60, "h": 3600}
_TRACE_ID_RE = re.compile(r"[0-9a-fA-F]{1,32}")
_SPAN_ID_RE = re.compile(r"[0-9a-fA-F]{1,16}")


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


def _control_free(value: str) -> bool:
    return not any(ord(c) < 32 or ord(c) == 127 for c in value)


def _trace_text(value, field, limit=_TRACE_TEXT_LIMIT):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or not _control_free(value):
        raise AIError(f"{field} 必须为非空字符串（最多 {limit} 个字符，不能有换行等控制字符）；不需要这个条件就不要传")


def _trace_time(value, field) -> datetime:
    """北京时间（不带时区）或带时区的 ISO；返回带时区的时间，用来比较先后"""
    try:
        if not isinstance(value, str) or len(value) > 64:
            raise ValueError
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise AIError(f"{field} 必须为{_TRACE_TIME_FORMAT}") from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=BEIJING_TZ)


def _trace_lookback(value, field):
    match = _LOOKBACK.fullmatch(value) if isinstance(value, str) else None
    if not match or int(match[1]) == 0:
        raise AIError(f"{field} 必须为正整数加单位 s/m/h/d，例如 15m、1h、2d")
    if int(match[1]) * _LOOKBACK_SECONDS[match[2]] > _TRACE_MAX_WINDOW:
        raise AIError(f"{field} 最长 7 天（7d）")


def _go_duration(value, field) -> float:
    """Go 时长（500ms、3s、1m30s）换成秒"""
    if not isinstance(value, str) or len(value) > 32 or not _GO_DURATION.fullmatch(value):
        raise AIError(f"{field} 必须为 Go 时长，例如 500ms、3s、1m")
    seconds = sum(float(number) * _GO_UNITS[unit] for number, unit in _GO_DURATION_PART.findall(value))
    if seconds <= 0:
        raise AIError(f"{field} 必须大于 0")
    if seconds > 86400:   # 和 master 一致：最长 24h
        raise AIError(f"{field} 最长 24h")
    return seconds


def _trace_mapping(value, field, max_items, value_limit):
    """attributes / contains：{属性名: 字符串}"""
    if not isinstance(value, dict) or not 1 <= len(value) <= max_items:
        raise AIError(f"{field} 必须为 1~{max_items} 项的对象，例如 {{\"属性名\": \"值\"}}")
    for key, item in value.items():
        if not isinstance(key, str) or not 1 <= len(key) <= 200 or any(c.isspace() for c in key) or not _control_free(key):
            raise AIError(f"{field} 的键必须为 1~200 个字符的属性名，不能有空白")
        if not isinstance(item, str):
            raise AIError(f"{field}.{key} 的值必须为字符串，数字也写成字符串")
        _trace_text(item, f"{field}.{key}", value_limit)


def _validate_trace(operation: str, args: dict) -> None:
    """链路追踪的参数。只校验声明过的参数（build_request 也只转发这些），另外拦下几种会被静默丢掉、让结果口径变了的写法"""
    misnamed = [field for field in ("namespace", "deployment", "pod") if field in args]
    if misnamed:
        raise AIError(f"链路追踪工具没有 {'、'.join(misnamed)} 参数：按 K8S 过滤用 trace_search / trace_stats 的 k8s_namespace、k8s_pod、k8s_deployment")
    if operation == "trace_stats":
        unsupported = [field for field in _TRACE_JAEGER_ONLY if field in args]
        if unsupported:
            raise AIError(f"trace_stats 不支持 {'、'.join(unsupported)}（ClickHouse 加速表没有这些字段）；要按它们找链路用 trace_search")
    if operation == "trace_search":
        misplaced = [field for field in ("group_by", "interval", "top") if field in args]
        if misplaced:
            raise AIError(f"{'、'.join(misplaced)} 是 trace_stats 的参数；分组统计用 trace_stats")
    for field in EXTRA_SCHEMAS[operation][1]:
        if field not in args:
            continue
        value = args[field]
        if field in _TRACE_TEXT_FIELDS:   # service、operation、path 等精确匹配的条件
            _trace_text(value, field)
        elif field == "pattern":
            _trace_text(value, field, 100)
        elif field == "span_kind":
            if value not in _TRACE_KINDS:
                raise AIError("span_kind 必须为 " + "/".join(_TRACE_KINDS))
        elif field in ("error", "errors"):
            if type(value) is not bool:
                raise AIError(f"{field} 必须为 boolean")
        elif field == "limit":
            maximum = 50 if operation == "trace_search" else 500
            if type(value) is not int or not 1 <= value <= maximum:
                raise AIError(f"limit 必须为 1..{maximum} 的整数")
        elif field in _TRACE_RANGES:
            low, high = _TRACE_RANGES[field]
            if type(value) is not int or not low <= value <= high:
                raise AIError(f"{field} 必须为 {low}..{high} 的整数")
        elif field == "status_codes":
            if not isinstance(value, list) or not 1 <= len(value) <= 20:
                raise AIError("status_codes 必须为 1~20 项的数组，例如 [504] 或 [\"5xx\"]")
            for item in value:
                if not (isinstance(item, str) and item in ("4xx", "5xx")) and not (type(item) is int and 100 <= item <= 599):
                    raise AIError("status_codes 每项是 100..599 的整数（不加引号），或字符串 \"4xx\"、\"5xx\"")
        elif field in ("min_duration", "max_duration"):
            _go_duration(value, field)
        elif field in ("start", "end", "around"):
            _trace_time(value, field)
        elif field == "lookback":
            _trace_lookback(value, field)
        elif field == "attributes":
            _trace_mapping(value, field, 10, _TRACE_TEXT_LIMIT)
        elif field == "contains":
            _trace_mapping(value, field, 3, _TRACE_CONTAINS_LIMIT)
            misuse = [key for key in value if key in _TRACE_CONTAINS_MISUSE]
            if misuse:
                raise AIError(f"contains 的键不能是 {'、'.join(misuse)}：服务、命名空间、Deployment、Pod 用 service、k8s_namespace、"
                              "k8s_deployment、k8s_pod 精确匹配，服务名不确定先用 trace_services 的 pattern 查")
            if operation == "trace_stats" and any(key not in _TRACE_STATS_CONTAINS for key in value):
                raise AIError("trace_stats 的 contains 只能用 " + "、".join(_TRACE_STATS_CONTAINS) + "（加速表的列）；按其他属性包含匹配用 trace_search")
        elif field == "group_by":
            if (not isinstance(value, list) or len(value) > 2
                    or any(not isinstance(item, str) or item not in _TRACE_GROUP_BY for item in value)
                    or len(set(value)) != len(value)):
                raise AIError("group_by 最多 2 项、不能重复，取值 " + "/".join(_TRACE_GROUP_BY))
        elif field == "interval":
            if value not in _TRACE_INTERVALS:
                raise AIError("interval 必须为 " + "/".join(_TRACE_INTERVALS))
        elif field == "trace_id":
            if not isinstance(value, str) or not _TRACE_ID_RE.fullmatch(value):
                raise AIError("trace_id 必须为 1~32 位十六进制")
        elif field == "focus_span":
            if not isinstance(value, str) or not _SPAN_ID_RE.fullmatch(value):
                raise AIError("focus_span 必须为 Span ID（16 位以内的十六进制）")
        elif field == "span_ids":
            if (not isinstance(value, list) or not 1 <= len(value) <= 20
                    or any(not isinstance(item, str) or not _SPAN_ID_RE.fullmatch(item) for item in value)):
                raise AIError("span_ids 必须为 1~20 个 Span ID（16 位以内的十六进制）")
        else:   # 新增参数要在上面登记校验方式（测试会覆盖每个声明的参数）
            raise AIError(f"{operation} 的参数 {field} 没有校验规则", 500, "internal_error")
    if operation in ("trace_search", "trace_stats"):
        if "status_min" in args and "status_max" in args and args["status_min"] > args["status_max"]:
            raise AIError("status_min 不能大于 status_max")
        if "min_duration" in args and "max_duration" in args and _go_duration(args["min_duration"], "min_duration") > _go_duration(args["max_duration"], "max_duration"):
            raise AIError("min_duration 不能大于 max_duration")
        if "start" in args and "end" in args:
            start, end = _trace_time(args["start"], "start"), _trace_time(args["end"], "end")
            if start >= end:
                raise AIError("start 必须早于 end")
            if (end - start).total_seconds() > _TRACE_MAX_WINDOW:
                raise AIError("时间窗口最长 7 天")
    if operation == "trace_stats" and "interval" in args and "time" not in args.get("group_by", []):
        raise AIError("interval 只用于按时间分组，group_by 要包含 time")
    if operation == "trace_spans" and ("span_ids" in args) == (args.get("errors") is True):
        raise AIError("trace_spans 需要 span_ids 或 errors=true（二选一）")


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
    if operation in TRACE_OPERATIONS:
        _validate_trace(operation, args)
        return
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
            minimum = 1 if field in _MIN_ONE or (field in _RESOURCE_FIELDS and operation in _POSITIVE_RESOURCES) else 0
            maximum = _MAXIMUM.get(field)
            if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
                raise AIError(f"{field} 必须为 {minimum}..{maximum if maximum else '∞'} 的整数")
        elif field in _BOOLEAN_FIELDS:
            if type(value) is not bool:
                raise AIError(f"{field} 必须为 boolean")
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
    if operation in SERVICE_TARGET and bool(args.get("deployment")) == bool(args.get("pod")):
        raise AIError(f"{operation} 需要 deployment 或 pod（二选一）")
    if operation == "runtime_config":
        # 名称会进占位符和 master 的查询参数：先按 K8S 名称规则校验
        for field, pattern in (("namespace", _DNS_LABEL), ("deployment", _DNS_NAME), ("pod", _DNS_NAME), ("container", _DNS_LABEL)):
            if args.get(field) and not pattern.match(args[field]):
                raise AIError(f"{field} 不是合法的 K8S 名称")
        keys = args.get("keys")
        if keys and (len(keys) > 500 or any(ord(c) < 32 or ord(c) == 127 for c in keys)):
            raise AIError("keys 最多 500 个字符，不能有换行等控制字符")
    if operation == "code_symbols" and not args.get("name") and not args.get("path"):
        raise AIError("code_symbols 需要 name 或 path")
    if operation in ("resource_config_update", "resource_control_add", "update_deployment_jvm"):
        xms, xmx = args.get("jvm_xms_bytes"), args.get("jvm_xmx_bytes")
        if xms is not None and xmx is not None and xmx > 0 and xms > xmx:
            raise AIError("Xms 不能大于 Xmx")
    if operation == "update_deployment_resources" and not _RESOURCE_FIELDS & set(args):
        raise AIError("update_deployment_resources 至少要传一个 requests/limits 字段")
    if operation == "update_deployment_jvm":
        fields = _JVM_FIELDS & set(args)
        if not fields:
            raise AIError("update_deployment_jvm 至少要传一个 JVM 参数")
        if any(type(args[field]) is not int or args[field] <= 0 for field in fields):
            raise AIError("JVM 参数必须为正整数字节数；要清除管控表里的值用 resource_config_update 传 null")
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
    elif operation in ("resource_config_update", "resource_pod_count", "resource_control_add"):
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
    elif operation in TRACE_OPERATIONS:
        # 链路追踪：参数名就是 master 的约定，原样放进 body；集群只在 query 的 env 里
        body = values
    else:
        query.update(values)
        if operation in ("resource_content", "resource_delete"):
            query["resource_type"] = _resource_type(operation, values["resource_type"])
        if operation == "node_resource_rank":
            query.setdefault("type", "cpu")
    return query, body
