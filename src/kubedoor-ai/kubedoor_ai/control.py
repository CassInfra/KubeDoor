"""花折资源管控（准入控制）感知的 Deployment 修改。

受管控的 Deployment，准入 webhook 会把副本数、第一个容器的 requests/limits、JVM 参数改写成管控表的值：
直接改 K8S 不生效（K8S 还照样返回成功）；不受管控时改管控表也不生效。网关按 master 返回的管控状态
（/api/ai/internal/deployment-control）为每个修改生成执行计划和审批预览，拦截会被改回的直接修改，
执行后回读核验。这里都是纯函数，取数据和执行在 gateway.py。
"""

from __future__ import annotations

import copy
import json
import re

import yaml
from kubedoor_tools.policy import cli_verb
from kubedoor_tools.workload import (JVM_FIELDS, extract_jvm_config, format_jvm_size, parse_cpu_millicores,
                                     parse_memory_mib, replace_image_tag, replace_jvm_flags, replaceable_jvm_fields)

from .domain import AIError

RESOURCE_FIELDS = ("request_cpu_m", "request_mem_mb", "limit_cpu_m", "limit_mem_mb")
# 计划里会原样回填的管控表值：审批到执行之间被别人改过就不执行
TABLE_FIELDS = ("pod_count_manual", "limit_cpu_m", "limit_mem_mb", *JVM_FIELDS)
SCHEDULED_SCALES = frozenset({"schedule_deployment_scale", "cron_deployment_scale"})
SCHEDULED_RESTARTS = frozenset({"schedule_deployment_restart", "cron_deployment_restart"})
STEP_NAMES = {
    "scale_deployment": "扩缩容", "restart_deployment": "滚动重启", "update_deployment_image": "更新镜像",
    "schedule_deployment_scale": "注册定时扩缩容", "cron_deployment_scale": "注册周期扩缩容",
    "schedule_deployment_restart": "注册定时重启", "cron_deployment_restart": "注册周期重启",
    "resource_config_update": "保存管控配置", "resource_pod_count": "写入管控副本数",
    "resource_control_add": "登记到资源管控",
}
# 按管控状态生成执行计划的目录操作；后两个没有对应的单个接口，计划完全由这里生成
PLANNED = frozenset({*STEP_NAMES, "update_deployment_resources", "update_deployment_jvm"})
LABELS = {
    "replicas": "副本数", "image": "镜像", "pod_count_manual": "管控副本数（指定 Pod）",
    "request_cpu_m": "CPU requests", "request_mem_mb": "内存 requests",
    "limit_cpu_m": "CPU limits", "limit_mem_mb": "内存 limits",
    "jvm_xms_bytes": "JVM -Xms", "jvm_xmx_bytes": "JVM -Xmx", "jvm_xss_bytes": "JVM -Xss",
    "jvm_max_metaspace_bytes": "JVM MaxMetaspaceSize",
}
EDIT_LABELS = {"replicas": "副本数", "resources": "requests/limits", "jvm": "JVM 参数"}
ALTERNATIVES = ("扩缩容用 scale_deployment（mode=persistent 永久 / temporary 临时），调整 requests/limits 用 "
                "update_deployment_resources，调整 JVM 参数用 update_deployment_jvm，网关会按管控状态选择执行方式")
# 这些状态下准入控制根本不处理该命名空间
OUTSIDE_ADMISSION = frozenset({"cluster_off", "namespace_off", "namespace_ignored", "webhook_missing"})
_EDIT = ("POST", "/api/db/res/edit")
_POD_COUNT = ("POST", "/api/db/res/pod_count")
_RESTART = ("POST", "/api/restart")
_NAMESPACE = re.compile(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?\Z")
_NAME = re.compile(r"[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?\Z")
_DEPLOYMENT_API = re.compile(r"/apis/apps/v1/namespaces/(?P<ns>[^/?#]+)/deployments(?:/(?P<name>[^/?#]+)(?P<scale>/scale)?)?/?\Z")
_DEPLOYMENT_TYPES = {"deployment", "deployments", "deploy", "deployment.apps", "deployments.apps"}


def check_target(namespace, deployment):
    """名称会拼进 K8S API 路径，先校验，不让 ../ 之类越过目标"""
    if not (isinstance(namespace, str) and _NAMESPACE.match(namespace)
            and isinstance(deployment, str) and _NAME.match(deployment)):
        raise AIError("namespace 和 deployment 必须是合法的 K8S 名称")


def deployment_path(namespace, deployment):
    return f"/apis/apps/v1/namespaces/{namespace}/deployments/{deployment}"


def snapshot(deployment):
    """在线 Deployment 里管控相关的字段；资源换算成 millicores / MiB，JVM 换算成字节"""
    spec = deployment.get("spec") or {}
    containers = ((spec.get("template") or {}).get("spec") or {}).get("containers") or [{}]
    first = containers[0] if isinstance(containers[0], dict) else {}
    resources = first.get("resources") or {}
    requests, limits = resources.get("requests") or {}, resources.get("limits") or {}
    args = first.get("args")
    return {
        "replicas": spec.get("replicas", 1), "container": first.get("name"), "image": first.get("image"),
        "resources": copy.deepcopy(resources), "args": copy.deepcopy(args),
        "request_cpu_m": parse_cpu_millicores(requests.get("cpu")),
        "request_mem_mb": parse_memory_mib(requests.get("memory")),
        "limit_cpu_m": parse_cpu_millicores(limits.get("cpu")),
        "limit_mem_mb": parse_memory_mib(limits.get("memory")),
        **extract_jvm_config(args),
        "replaceable_jvm": sorted(replaceable_jvm_fields(args)),
    }


def display(field, value):
    if value is None:
        return "未设置"
    if isinstance(value, int) and value < 0 and field in (*RESOURCE_FIELDS, "pod_count_manual"):
        return "-1（按 AI 推荐 / 当日 Pod）" if field == "pod_count_manual" else "未配置"
    if field.endswith("_cpu_m") and isinstance(value, int):
        return f"{value}m"
    if field.endswith("_mem_mb") and isinstance(value, int):
        return f"{value}Mi"
    if field in JVM_FIELDS and isinstance(value, int):
        return format_jvm_size(value, "k" if field == "jvm_xss_bytes" else "m")
    return str(value)


def _change(field, current, target, scope="在线", unknown=False):
    return {"field": field, "label": LABELS[field], "scope": scope,
            "current": "读取不到" if unknown else display(field, current), "target": display(field, target)}


def _live_change(field, live, target):
    return _change(field, live.get(field) if live else None, target, unknown=live is None)


def control_summary(control):
    table = control.get("control") or {}
    return {
        "state": control["state"], "controlled": control["controlled"], "blocked": control["blocked"],
        "registered": control["registered"], "message": control["message"], "effective": control.get("effective"),
        "table": {field: table.get(field) for field in TABLE_FIELDS} if control["registered"] else None,
    }


def control_drift(planned, current):
    """审批期间管控状态或管控表被改过，返回原因；没变返回 None"""
    if planned["state"] != current["state"]:
        return f"管控状态已变化：审批时「{planned['message']}」，现在「{current['message']}」"
    if planned["table"] != control_summary(current)["table"]:
        return "管控表里该服务的配置在审批期间被修改过"
    return None


def main_step(operation, method, path, query, body):
    return {"kind": "master", "name": STEP_NAMES[operation], "method": method, "path": path, "query": query, "body": body}


def _master_step(name, method, path, query, body):
    return {"kind": "master", "name": name, "method": method, "path": path, "query": query, "body": body}


def _k8s_step(name, namespace, deployment, patch):
    return {"kind": "k8s", "name": name, "arguments": {
        "method": "PATCH", "path": deployment_path(namespace, deployment),
        "content_type": "application/json-patch+json", "body": patch}}


def _restart_step(args, env):
    return _master_step("滚动重启使其生效", *_RESTART, {"env": env}, {
        "deployment_list": [{"namespace": args["namespace"], "deployment_name": args["deployment"]}], "node_scheduler": []})


def _reject_blocked(control, action):
    if control["blocked"]:
        raise AIError(f"{action}会被准入控制拒绝：{control['message']}。请先用 resource_control_add 登记该服务", 409, "control_blocked")


def _direct_only(args, what):
    if not args.get("apply_now", True):
        raise AIError(f"该 Deployment 不受花折资源管控，{what}只能直接修改 Deployment（会触发滚动更新），没有可以先保存的地方；"
                      "去掉 apply_now=false 再提交", 409, "control_apply_now")


def _need_live(live, what):
    if live is None:
        raise AIError(f"读取不到该 Deployment，不能直接修改{what}：需要在线且已升级的 Agent，或已配置的 kubeconfig", 409, "deployment_unreadable")


def _refresh(plan, effective, live, action, expect=True):
    """模板一变，准入控制就把副本数、资源、JVM 刷回管控值：列出会变的项，并作为回读期望"""
    changed = []
    for field in ("replicas", *RESOURCE_FIELDS, *JVM_FIELDS):
        value = (effective or {}).get(field)
        if value is None:
            continue
        if field in JVM_FIELDS and (live is None or field not in live["replaceable_jvm"]):
            continue   # webhook 只替换 java args 里已有的参数
        if expect:
            plan["expect"].setdefault(field, value)
        if live is not None and live.get(field) != value:
            changed.append(field)
            plan["changes"].append(_change(field, live.get(field), value))
    if changed:
        plan["effects"].append(f"{action}时准入控制会把{'、'.join(LABELS[field] for field in changed)}刷新为管控值（见变化）")
    elif live is None:
        plan["effects"].append(f"{action}时准入控制会把副本数、资源、JVM 参数刷新为管控值（读取不到在线值，列不出具体变化）")
    else:
        plan["effects"].append(f"{action}时准入控制会把副本数、资源、JVM 参数刷新为管控值（和现在一致）")


def _scale(plan, args, env, control, live):
    _reject_blocked(control, "扩缩容")
    replicas, mode = args["replicas"], args.get("mode", "persistent")
    plan["changes"].append(_live_change("replicas", live, replicas))
    plan["expect"]["replicas"] = replicas
    if not control["controlled"]:
        plan["effects"].append(f"{control['message']}：副本数直接改为 {replicas}，一直保持到再次修改")
        if mode == "temporary":
            plan["warnings"].append("不受管控时没有「临时」扩缩容，效果和永久一样")
        return
    effective = control["effective"]["replicas"]
    if mode == "temporary":
        plan["steps"][0]["query"]["temp"] = "true"
        plan["effects"] += [f"临时扩缩到 {replicas} 个副本，不改管控配置",
                            f"下次发布或重启时，准入控制会把副本数恢复为管控值 {effective}"]
        return
    plan["steps"].insert(0, _master_step("写入管控副本数", *_POD_COUNT, {"env": env}, {
        "env": env, "namespace": args["namespace"], "deployment_name": args["deployment"], "pod_count_manual": replicas}))
    plan["changes"].append(_change("pod_count_manual", control["control"]["pod_count_manual"], replicas, scope="管控表"))
    plan["effects"] += [f"管控副本数写为 {replicas}：以后每次发布、重启都按 {replicas} 个副本", f"立即扩缩到 {replicas} 个副本"]


def _scheduled_scale(plan, args, env, control, live):
    _reject_blocked(control, "扩缩容")
    replicas = args["replicas"]
    # 到点由 CronJob 回调 agent 的 /api/scale，写不了 master 的管控表：定时 / 周期扩缩容只能是临时的。
    # 不受管控时带 temp 也一样生效，到点前如果纳入了管控也不会被改回
    plan["steps"][0]["query"]["temp"] = "true"
    if not control["controlled"]:
        plan["effects"].append(f"{control['message']}：到点把副本数改为 {replicas}")
        return
    if args.get("mode") == "persistent":
        raise AIError("受花折资源管控时，定时 / 周期扩缩容到点只能临时扩缩（写不了管控表）：去掉 mode 或传 mode=temporary；"
                      "要长期修改副本数，用 scale_deployment（mode=persistent）或 resource_pod_count", 409, "control_mode")
    plan["effects"] += [f"到点临时扩缩到 {replicas} 个副本，不改管控配置",
                        f"之后下次发布或重启时，准入控制会把副本数恢复为管控值 {control['effective']['replicas']}"]


def _restart(plan, args, env, control, live):
    _reject_blocked(control, "重启")
    plan["effects"].append("滚动重启全部 Pod")
    if control["controlled"]:
        _refresh(plan, control["effective"], live, "重启")


def _scheduled_restart(plan, args, env, control, live):
    _reject_blocked(control, "重启")
    plan["effects"].append("到点滚动重启全部 Pod")
    if control["controlled"]:
        _refresh(plan, control["effective"], live, "到点重启", expect=False)


def _image(plan, args, env, control, live):
    _reject_blocked(control, "更新镜像")
    tag = args["image_tag"]
    if live is not None:
        image = replace_image_tag(live["image"], tag)
        plan["changes"].append(_change("image", live["image"], image))
        plan["expect"]["image"] = image
    else:
        plan["changes"].append(_change("image", None, f"tag 换成 {tag}", unknown=True))
    plan["effects"].append("只换第一个容器镜像的 tag（仓库地址不变），触发滚动更新；改 sidecar 镜像或换仓库请用通用工具")
    if control["controlled"]:
        _refresh(plan, control["effective"], live, "发布")


def _apply_after_table(plan, args, env, control, live, wanted, what):
    if not args.get("apply_now", True):
        plan["effects"].append("只写管控表，下次发布或重启时生效，现在的 Pod 不变")
        return
    plan["steps"].append(_restart_step(args, env))
    plan["effects"].append(f"写入管控表后立即滚动重启，准入控制按新的{what}改写 Deployment")
    _refresh(plan, {**(control.get("effective") or {}), **wanted}, live, "重启")


def _resources(plan, args, env, control, live):
    _reject_blocked(control, "调整资源")
    wanted = {field: args[field] for field in RESOURCE_FIELDS if field in args}
    ns, dep = args["namespace"], args["deployment"]
    if control["controlled"]:
        requests = [LABELS[field] for field in wanted if field.startswith("request_")]
        if requests:
            raise AIError(f"受花折资源管控时，requests 由每日高峰期采集自动计算（P95），不能手动修改（{'、'.join(requests)}）；"
                          "只能调整 limits（limit_cpu_m、limit_mem_mb）", 409, "control_requests")
        table = control["control"]
        plan["steps"].append(_master_step("写入管控 limits", *_EDIT, {"env": env}, {
            "env": env, "namespace": ns, "deployment": dep,
            "limit_cpu_m": wanted.get("limit_cpu_m", table["limit_cpu_m"]),
            "limit_mem_mb": wanted.get("limit_mem_mb", table["limit_mem_mb"]),
            "pod_count_manual": table["pod_count_manual"]}))
        plan["changes"] += [_change(field, table[field], value, scope="管控表") for field, value in wanted.items()]
        _apply_after_table(plan, args, env, control, live, wanted, " limits ")
        return
    _direct_only(args, "资源")
    _need_live(live, "资源")
    resources = copy.deepcopy(live["resources"]) or {}
    for field, value in wanted.items():
        section = resources.setdefault("requests" if field.startswith("request_") else "limits", {})
        if field.endswith("_cpu_m"):
            section["cpu"] = f"{value}m"
        else:
            section["memory"] = f"{value}Mi"
    for kind, parse in (("cpu", parse_cpu_millicores), ("memory", parse_memory_mib)):
        request, limit = parse((resources.get("requests") or {}).get(kind)), parse((resources.get("limits") or {}).get(kind))
        if request is not None and limit is not None and request > limit:
            raise AIError(f"{kind} 的 requests 不能大于 limits（K8S 会拒绝）", 400, "invalid_resources")
    plan["steps"].append(_k8s_step("修改 Deployment 的 requests/limits", ns, dep, [
        {"op": "add", "path": "/spec/template/spec/containers/0/resources", "value": resources}]))
    for field, value in wanted.items():
        plan["changes"].append(_change(field, live.get(field), value))
        plan["expect"][field] = value
    plan["effects"].append(f"{control['message']}：直接修改第一个容器（{live['container']}）的 requests/limits，触发滚动更新")


def _jvm(plan, args, env, control, live):
    _reject_blocked(control, "调整 JVM 参数")
    wanted = {field: args[field] for field in JVM_FIELDS if field in args}
    ns, dep = args["namespace"], args["deployment"]
    if live is not None:
        missing = [LABELS[field] for field in wanted if field not in live["replaceable_jvm"]]
        if missing:
            raise AIError(f"第一个容器（{live['container']}）的启动参数里没有 {'、'.join(missing)}：只支持 args 以 java 开头、"
                          "并且已写明这些选项的写法；写在环境变量（如 JAVA_OPTS）或启动脚本里时无法自动修改", 409, "jvm_flag_missing")
        merged = {**{field: live[field] for field in JVM_FIELDS}, **wanted}
        if merged["jvm_xms_bytes"] and merged["jvm_xmx_bytes"] and merged["jvm_xms_bytes"] > merged["jvm_xmx_bytes"]:
            raise AIError("Xms 不能大于 Xmx", 400, "invalid_jvm")
    if control["controlled"]:
        table = control["control"]
        plan["steps"].append(_master_step("写入管控 JVM 参数", *_EDIT, {"env": env}, {
            "env": env, "namespace": ns, "deployment": dep, "limit_cpu_m": table["limit_cpu_m"],
            "limit_mem_mb": table["limit_mem_mb"], "pod_count_manual": table["pod_count_manual"], **wanted}))
        plan["changes"] += [_change(field, table[field], value, scope="管控表") for field, value in wanted.items()]
        if live is None:
            plan["warnings"].append("读取不到 Deployment，确认不了这些 JVM 参数是否写在第一个容器的 args 里；"
                                    "不在 args 里时准入控制改不了，写入管控表也不会生效")
        _apply_after_table(plan, args, env, control, live, wanted, " JVM 参数")
        return
    _direct_only(args, "JVM 参数")
    _need_live(live, "JVM 参数")
    plan["steps"].append(_k8s_step("修改 Deployment 的 JVM 参数", ns, dep, [
        {"op": "replace", "path": "/spec/template/spec/containers/0/args", "value": replace_jvm_flags(live["args"], wanted)}]))
    for field, value in wanted.items():
        plan["changes"].append(_change(field, live.get(field), value))
        plan["expect"][field] = value
    plan["effects"].append(f"{control['message']}：直接修改第一个容器（{live['container']}）的启动参数，触发滚动更新")


def _require_registered(control, operation):
    if not control["registered"]:
        raise AIError(f"管控表里没有该服务，{operation} 不会有任何效果；新服务请先用 resource_control_add 登记", 409, "control_unregistered")


def _config_update(plan, args, env, control, live):
    _require_registered(control, "resource_config_update")
    table = control["control"]
    plan["changes"] += [_change(field, table.get(field), args[field], scope="管控表")
                        for field in ("limit_cpu_m", "limit_mem_mb", "pod_count_manual", *JVM_FIELDS)
                        if field in args and args[field] != table.get(field)]
    if control["controlled"]:
        plan["effects"].append("只保存管控配置，下次发布或重启时生效，现在的 Pod 不变；要立即生效改用 update_deployment_resources / "
                               "update_deployment_jvm（apply_now=true），或保存后 restart_deployment")
    else:
        plan["warnings"].append(f"{control['message']}：保存的管控配置现在不会生效，开启准入控制并纳入管控后才会应用；"
                                "要立即修改在线资源，改用 update_deployment_resources / update_deployment_jvm")


def _pod_count(plan, args, env, control, live):
    _require_registered(control, "resource_pod_count")
    plan["changes"].append(_change("pod_count_manual", control["control"]["pod_count_manual"], args["pod_count_manual"], scope="管控表"))
    if control["controlled"]:
        plan["effects"].append("只写管控副本数，不会立即扩缩；下次发布、重启或扩缩容时按它执行。"
                               "立即扩缩用 scale_deployment（mode=persistent 会同时写这里）")
    else:
        plan["warnings"].append(f"{control['message']}：管控副本数现在不会生效；要修改在线副本数用 scale_deployment")


def _control_add(plan, args, env, control, live):
    if control["registered"]:
        raise AIError("该服务已在管控表中，修改请用 resource_config_update", 409, "control_registered")
    plan["changes"] += [_change(field, None, args[field], scope="管控表")
                        for field in ("pod_count_manual", *RESOURCE_FIELDS, *JVM_FIELDS) if field in args]
    if control["state"] in ("unregistered", "unregistered_blocked"):
        plan["effects"].append("登记后该服务受花折资源管控：之后的创建、发布、重启、扩缩容都按这些值改写；"
                               "现在的 Pod 不变，下次发布或重启时生效")
    else:
        plan["warnings"].append(f"{control['message']}：登记后暂不生效，开启准入控制并把命名空间纳入管控后才会应用")


_BUILDERS = {
    "scale_deployment": _scale, "restart_deployment": _restart, "update_deployment_image": _image,
    "update_deployment_resources": _resources, "update_deployment_jvm": _jvm,
    "resource_config_update": _config_update, "resource_pod_count": _pod_count, "resource_control_add": _control_add,
    **dict.fromkeys(SCHEDULED_SCALES, _scheduled_scale), **dict.fromkeys(SCHEDULED_RESTARTS, _scheduled_restart),
}


def build_plan(operation, args, env, control, live, step=None):
    """按管控状态生成执行计划；不能执行的组合直接抛 AIError，模型会看到原因和替代做法。

    live 是 snapshot() 的结果，读取不到时为 None；step 是该操作对应的现有接口请求（main_step）。
    """
    plan = {"control": control_summary(control), "steps": [step] if step else [], "changes": [],
            "effects": [], "warnings": [], "expect": {}}
    _BUILDERS[operation](plan, args, env, control, live)
    return plan


def verify(plan, live):
    """执行后回读：和计划的期望值比对，不一致时说明是不是被准入控制改写成了管控值"""
    expect = plan.get("expect") or {}
    if not expect:
        return None
    if live is None:
        return {"checked": False, "message": "读取不到 Deployment，没能核验实际结果；请用 pods 或通用 api 查看"}
    actual = snapshot(live)
    effective = (plan.get("control") or {}).get("effective") or {}
    mismatches = []
    for field, wanted in expect.items():
        got = actual.get(field)
        if got == wanted:
            continue
        item = {"field": field, "label": LABELS[field], "expected": display(field, wanted), "actual": display(field, got)}
        if got is not None and effective.get(field) == got:
            item["reason"] = "被准入控制改写成了管控值"
        mismatches.append(item)
    if mismatches:
        text = "；".join(f"{m['label']} 期望 {m['expected']}，实际 {m['actual']}" + (f"（{m['reason']}）" if m.get("reason") else "")
                        for m in mismatches)
        return {"checked": True, "matched": False, "mismatches": mismatches, "message": f"执行后回读不一致：{text}"}
    return {"checked": True, "matched": True, "message": "执行后回读：Deployment 已是期望的值；Pod 滚动或扩缩需要一点时间，可用 pods 查看进度"}


# ---- 通用执行器（api / kubectl / resource_apply）直接改 Deployment 的识别 ----

def _merge(target, patch, strategic):
    """JSON merge patch；strategic 时 containers 按 name 合并（K8S strategic merge patch 的 patchMergeKey）"""
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    result = copy.deepcopy(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        elif strategic and key in ("containers", "initContainers") and isinstance(value, list) and isinstance(result.get(key), list):
            merged = result[key]
            for item in value:
                name = item.get("name") if isinstance(item, dict) else None
                index = next((i for i, c in enumerate(merged) if isinstance(c, dict) and name is not None and c.get("name") == name), None)
                if index is None:
                    merged.append(copy.deepcopy(item))
                else:
                    merged[index] = _merge(merged[index], item, strategic)
        elif isinstance(value, dict):
            result[key] = _merge(result.get(key), value, strategic)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _pointer(path):
    if not isinstance(path, str) or (path and not path.startswith("/")):
        raise ValueError("invalid pointer")
    return [part.replace("~1", "/").replace("~0", "~") for part in path[1:].split("/")] if path else []


def _json_patch(target, operations):
    """本地应用 JSON patch（add / replace / remove / test），只用来判断改了什么"""
    document = copy.deepcopy(target)
    if not isinstance(operations, list):
        raise ValueError("json patch must be a list")
    for operation in operations:
        if not isinstance(operation, dict):
            raise ValueError("invalid operation")
        op, parts = operation.get("op"), _pointer(operation.get("path"))
        if op == "test":
            continue
        if not parts:
            if op not in ("add", "replace"):
                raise ValueError("unsupported root operation")
            document = copy.deepcopy(operation.get("value"))
            continue
        parent = document
        for part in parts[:-1]:
            parent = parent[int(part)] if isinstance(parent, list) else parent[part]
        last = parts[-1]
        if op in ("add", "replace"):
            value = copy.deepcopy(operation.get("value"))
            if isinstance(parent, list):
                if last == "-":
                    parent.append(value)
                elif op == "add":
                    parent.insert(int(last), value)
                else:
                    parent[int(last)] = value
            else:
                parent[last] = value
        elif op == "remove":
            if isinstance(parent, list):
                parent.pop(int(last))
            else:
                parent.pop(last)
        else:
            raise ValueError(f"unsupported op {op}")
    return document


def _first_container(deployment):
    pod_spec = (((deployment or {}).get("spec") or {}).get("template") or {}).get("spec") or {}
    containers = pod_spec.get("containers") or []
    return containers[0] if containers and isinstance(containers[0], dict) else {}


def controlled_changes(live, proposed):
    """proposed 相对在线对象改了哪些受管控字段：副本数、第一个容器的 requests/limits、JVM 参数"""
    before, after = snapshot(live), snapshot(proposed)
    fields = set()
    if after["replicas"] != before["replicas"]:
        fields.add("replicas")
    if any(after[field] != before[field] for field in RESOURCE_FIELDS):
        fields.add("resources")
    if any(after[field] != before[field] for field in JVM_FIELDS):
        fields.add("jvm")
    return fields


def _keyword_fields(edit):
    """本地没法套用修改时，按内容里出现的字段保守判断"""
    text = json.dumps(edit.get("patch") or edit.get("object"), ensure_ascii=False, default=str)
    return {field for field, words in (("replicas", ('"replicas"',)), ("resources", ('"resources"',)),
                                       ("jvm", ('"args"', "-Xm", "-Xss", "MaxMetaspaceSize"))) if any(w in text for w in words)}


def changed_fields(edit, live):
    """一次直接修改会动到的受管控字段；live 是在线 Deployment，读取不到时为 None"""
    fields = edit.get("fields")
    if fields is not None:
        fields = set(fields)
        containers = edit.get("containers")
        if "resources" in fields and containers and "*" not in containers and live is not None \
                and _first_container(live).get("name") not in containers:
            fields.discard("resources")   # 只改了 sidecar，准入控制不管
        return fields
    if live is None:
        return _keyword_fields(edit)
    try:
        if edit.get("patch") is not None:
            kind, body = edit["patch"]
            proposed = _json_patch(live, body) if kind == "json" else _merge(live, body, strategic=kind != "merge")
        elif edit.get("replace"):
            proposed = copy.deepcopy(edit["object"])
            proposed.setdefault("spec", {}).setdefault("replicas", 1)   # replace 时缺省的副本数回到 1
        else:
            proposed = _merge(live, edit["object"], strategic=True)
        return controlled_changes(live, proposed)
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        return _keyword_fields(edit)


def _documents(text):
    try:
        return [doc for doc in yaml.safe_load_all(text) if isinstance(doc, dict)]
    except yaml.YAMLError:
        return []


def _object_edits(text, default_namespace, mode):
    edits = []
    for doc in _documents(text):
        if doc.get("kind") != "Deployment" or not str(doc.get("apiVersion", "")).startswith("apps/"):
            continue
        metadata = doc.get("metadata") or {}
        if isinstance(metadata.get("name"), str):
            edits.append({"namespace": metadata.get("namespace") or default_namespace, "name": metadata["name"],
                          "fields": None, "object": doc, "replace": mode == "replace", "create": mode == "create"})
    return edits


def _patch_body(text):
    try:
        return json.loads(text)
    except ValueError:
        try:
            return yaml.safe_load(text)
        except yaml.YAMLError:
            return None


_KUBECTL_VALUE_FLAGS = {"-n", "--namespace", "-l", "--selector", "-f", "--filename", "-o", "--output", "--replicas",
                        "--current-replicas", "--resource-version", "--timeout", "-c", "--containers", "--limits",
                        "--requests", "-p", "--patch", "--patch-file", "--type", "--field-manager", "--context",
                        "--cluster", "--kubeconfig", "--request-timeout", "-k", "--kustomize", "--field-selector",
                        "--template", "--subresource", "--grace-period", "--cascade"}


def _kubectl_flags(argv):
    """返回 (位置参数, {flag: [值]})；带值的 flag 同时支持 --flag value 和 --flag=value"""
    positional, flags, index = [], {}, 0
    while index < len(argv):
        token = argv[index]
        if token.startswith("-") and token != "-":
            name, _, value = token.partition("=")
            if not value and name in _KUBECTL_VALUE_FLAGS and index + 1 < len(argv):
                value = argv[index + 1]
                index += 1
            flags.setdefault(name, []).append(value)
        else:
            positional.append(token)
        index += 1
    return positional, flags


def _flag(flags, *names):
    for name in names:
        if flags.get(name):
            return flags[name][-1]
    return None


def _kubectl_targets(positional):
    """kubectl 资源参数里的 Deployment：返回 (名称列表, 是否按类型批量)"""
    names, bulk = [], False
    if positional and positional[0].lower() in _DEPLOYMENT_TYPES and "/" not in positional[0]:
        names = positional[1:]
        bulk = not names
    else:
        for item in positional:
            kind, _, name = item.partition("/")
            if kind.lower() in _DEPLOYMENT_TYPES and name:
                names.append(name)
    return names, bulk


def _kubectl_edits(argv, files, stdin, default_namespace):
    verb, index = cli_verb(argv)
    positional, flags = _kubectl_flags(argv[index + 1:] if index >= 0 else [])
    namespace = _flag(_kubectl_flags(argv)[1], "-n", "--namespace") or default_namespace
    selector = bool(_flag(flags, "-l", "--selector")) or "--all" in flags

    def targets(fields, **extra):
        names, bulk = _kubectl_targets(positional)
        if bulk or selector:
            return [{"namespace": namespace, "name": None, "fields": set(fields), **extra}]
        return [{"namespace": namespace, "name": name, "fields": set(fields), **extra} for name in names]

    if verb == "scale":
        return targets({"replicas"})
    if verb == "set" and positional[:1] == ["resources"]:
        positional = positional[1:]
        containers = _flag(flags, "-c", "--containers")
        return targets({"resources"}, containers=containers.split(",") if containers else None)
    if verb == "patch":
        names, _ = _kubectl_targets(positional)
        raw = _flag(flags, "-p", "--patch")
        if raw is None and _flag(flags, "--patch-file"):
            raw = (files or {}).get(_flag(flags, "--patch-file").rsplit("/", 1)[-1])
        body = _patch_body(raw) if isinstance(raw, str) else None
        kind = {"json": "json", "merge": "merge"}.get(_flag(flags, "--type") or "strategic", "strategic")
        if body is None:
            return [{"namespace": namespace, "name": name, "fields": None, "patch": None, "object": raw} for name in names]
        if _flag(flags, "--subresource") == "scale":
            return [{"namespace": namespace, "name": name, "fields": {"replicas"}} for name in names]
        return [{"namespace": namespace, "name": name, "fields": None, "patch": (kind, body)} for name in names]
    if verb in ("apply", "replace", "create"):
        edits = []
        for source in flags.get("-f", []) + flags.get("--filename", []):
            text = stdin if source == "-" else (files or {}).get(source.rsplit("/", 1)[-1])
            if isinstance(text, str):
                edits += _object_edits(text, namespace, verb)
        return edits
    return []


def _api_edits(args):
    method = str(args.get("method", "GET")).upper()
    match = _DEPLOYMENT_API.fullmatch(str(args.get("path", "")).split("?", 1)[0])
    if not match or method not in ("PUT", "PATCH") or not match.group("name"):
        return []
    target = {"namespace": match.group("ns"), "name": match.group("name")}
    if match.group("scale"):
        return [{**target, "fields": {"replicas"}}]
    body = args.get("body")
    if method == "PUT":
        return [{**target, "fields": None, "object": body if isinstance(body, dict) else {}, "replace": True}]
    content_type = str(args.get("content_type", "application/strategic-merge-patch+json"))
    kind = "json" if "json-patch" in content_type else "merge" if "merge-patch" in content_type and "strategic" not in content_type else "strategic"
    if isinstance(body, str):
        body = _patch_body(body)
    return [{**target, "fields": None, "patch": (kind, body)}] if body is not None else [{**target, "fields": None, "object": args.get("body")}]


def deployment_edits(source, operation, args, default_namespace="default"):
    """一次写操作直接改到的 Deployment：[{namespace, name(批量时为 None), fields 或待比对的 patch/object}]"""
    if source == "kubedoor":
        if operation != "resource_apply":
            return []
        mode = args.get("method", "apply")
        return [edit for edit in _object_edits(args.get("yaml_content") or "", default_namespace, mode) if not edit["create"]]
    if operation == "api":
        return _api_edits(args)
    if operation == "kubectl" and isinstance(args.get("argv"), list):
        return [edit for edit in _kubectl_edits(args["argv"], args.get("files"), args.get("stdin"), default_namespace)
                if not edit.get("create")]
    return []


def rejection(edit, fields, control, operation):
    target = f"{edit['namespace']}/{edit['name']}" if edit.get("name") else f"命名空间 {edit['namespace']} 里按标签或 --all 选中的 Deployment"
    what = "、".join(EDIT_LABELS[field] for field in ("replicas", "resources", "jvm") if field in fields)
    return AIError(f"{target} 受花折资源管控（{control['message']}）。用 {operation} 直接修改{what}会被准入控制改回管控值，已拒绝。"
                   f"请改用：{ALTERNATIVES}", 409, "control_direct_edit")
