"""
Deployment / Pod 实时状态的纯函数

输入都是 K8S API 返回的原始 JSON(camelCase dict),workload_cache 收到 list/watch 数据时
用这里的函数压成精简 dict 存进内存。pod 的 STATUS 照搬 kubectl get pod(printPod),
deployment 的滚动判断照搬 kubectl rollout status。不依赖 agent 的 utils,方便单测。
"""

import re
from datetime import datetime, timedelta, timezone

BEIJING_TZ = timezone(timedelta(hours=8))


def _condition(status, ctype):
    for cond in status.get("conditions") or []:
        if cond.get("type") == ctype:
            return cond
    return None


def _condition_true(status, ctype):
    cond = _condition(status, ctype)
    return bool(cond and cond.get("status") == "True")


def format_time(value):
    """K8S 的 RFC3339 时间转成北京时间字符串,解析失败原样返回"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return value


def kubectl_pod_status(pod):
    """照搬 kubectl printPod 的 STATUS 列。返回 (status, 就绪容器数, 容器总数, 重启次数)"""
    meta = pod.get("metadata") or {}
    spec = pod.get("spec") or {}
    status = pod.get("status") or {}
    phase = status.get("phase") or ""
    reason = status.get("reason") or phase
    if any(c.get("type") == "PodScheduled" and c.get("reason") == "SchedulingGated" for c in status.get("conditions") or []):
        reason = "SchedulingGated"

    init_containers = spec.get("initContainers") or []
    # restartPolicy=Always 的 init 容器是 sidecar,算进容器总数
    sidecars = {c.get("name") for c in init_containers if c.get("restartPolicy") == "Always"}
    total = len(spec.get("containers") or []) + len(sidecars)
    ready = 0
    restarts = 0
    sidecar_restarts = 0

    initializing = False
    for i, cs in enumerate(status.get("initContainerStatuses") or []):
        state = cs.get("state") or {}
        terminated = state.get("terminated")
        waiting = state.get("waiting")
        restarts += cs.get("restartCount") or 0
        is_sidecar = cs.get("name") in sidecars
        if is_sidecar:
            sidecar_restarts += cs.get("restartCount") or 0
        if terminated and terminated.get("exitCode") == 0:
            continue
        if is_sidecar and cs.get("started"):
            if cs.get("ready"):
                ready += 1
            continue
        if terminated:
            if terminated.get("reason"):
                reason = "Init:" + terminated["reason"]
            elif terminated.get("signal"):
                reason = "Init:Signal:%s" % terminated["signal"]
            else:
                reason = "Init:ExitCode:%s" % terminated.get("exitCode", 0)
        elif waiting and waiting.get("reason") and waiting["reason"] != "PodInitializing":
            reason = "Init:" + waiting["reason"]
        else:
            reason = "Init:%d/%d" % (i, len(init_containers))
        initializing = True
        break

    if not initializing or _condition_true(status, "Initialized"):
        restarts = sidecar_restarts
        has_running = False
        for cs in reversed(status.get("containerStatuses") or []):
            restarts += cs.get("restartCount") or 0
            state = cs.get("state") or {}
            waiting = state.get("waiting")
            terminated = state.get("terminated")
            if waiting and waiting.get("reason"):
                reason = waiting["reason"]
            elif terminated and terminated.get("reason"):
                reason = terminated["reason"]
            elif terminated:
                if terminated.get("signal"):
                    reason = "Signal:%s" % terminated["signal"]
                else:
                    reason = "ExitCode:%s" % terminated.get("exitCode", 0)
            elif cs.get("ready") and state.get("running") is not None:
                has_running = True
                ready += 1
        # 有容器已退出(Completed)但还有容器在跑,按 Ready 条件显示 Running / NotReady
        if reason == "Completed" and has_running:
            reason = "Running" if _condition_true(status, "Ready") else "NotReady"

    if meta.get("deletionTimestamp"):
        if status.get("reason") == "NodeLost":
            reason = "Unknown"
        elif phase not in ("Succeeded", "Failed"):
            reason = "Terminating"
    return reason or "Unknown", ready, total, restarts


def pod_deployment(pod):
    """pod 属于哪个 deployment。返回 (deployment 名或 None, 是否被隔离)

    Deployment 建的 ReplicaSet 一定叫 <deployment>-<pod-template-hash>。被隔离的 pod(app 标签改成
    -ALERT 后被 ReplicaSet 释放,没有 ownerReferences)名字仍是 <deployment>-<hash>-<5 位随机串>,
    用 hash 精确匹配;名字被 generateName 截断等匹配不上的情况宁可不归属,也不要归错。
    """
    meta = pod.get("metadata") or {}
    pod_hash = (meta.get("labels") or {}).get("pod-template-hash")
    if not pod_hash:
        return None, False
    owners = meta.get("ownerReferences") or []
    if owners:
        for owner in owners:
            if owner.get("controller"):
                name = owner.get("name") or ""
                suffix = "-" + pod_hash
                if owner.get("kind") == "ReplicaSet" and name.endswith(suffix) and len(name) > len(suffix):
                    return name[: -len(suffix)], False
                return None, False
        return None, False
    match = re.fullmatch(r"(.+)-" + re.escape(pod_hash) + r"-[a-z0-9]{5}", meta.get("name") or "")
    if match:
        return match.group(1), True
    return None, False


def _container_problem(statuses, skip_success):
    """容器状态里的异常原因:waiting 的 reason/message、非正常退出的 terminated"""
    for cs in statuses or []:
        state = cs.get("state") or {}
        waiting = state.get("waiting")
        terminated = state.get("terminated")
        if waiting:
            reason = waiting.get("reason") or ""
            message = waiting.get("message") or ""
            if reason or message:
                return "%s: %s" % (reason, message) if message else reason
        elif terminated and not (skip_success and terminated.get("exitCode") == 0):
            reason = terminated.get("reason") or ""
            message = terminated.get("message") or ""
            exit_code = terminated.get("exitCode")
            text = "%s (exit: %s)" % (reason, exit_code)
            return "%s: %s" % (text, message) if message else text
    return ""


def _exception_reason(pod, display_status, ready):
    """异常状态原因(不查 events,events 只在 HTTP 接口里按需补)"""
    status = pod.get("status") or {}
    if status.get("phase") != "Running":
        scheduled = _condition(status, "PodScheduled")
        if scheduled and scheduled.get("status") != "True":
            text = scheduled.get("message") or scheduled.get("reason") or ""
            if text:
                return text
        if status.get("reason"):
            message = status.get("message") or ""
            return "%s: %s" % (status["reason"], message) if message else status["reason"]
        return _container_problem(status.get("initContainerStatuses"), True) or _container_problem(
            status.get("containerStatuses"), False
        )
    if display_status == "Terminating":
        return ""
    if display_status != "Running" or not ready:
        # Running 但不健康:CrashLoopBackOff、就绪探针没过等
        for cs in status.get("containerStatuses") or []:
            waiting = (cs.get("state") or {}).get("waiting")
            if waiting and waiting.get("message"):
                return "%s: %s" % (waiting.get("reason") or "", waiting["message"])
        ready_cond = _condition(status, "Ready")
        if ready_cond and ready_cond.get("status") != "True" and ready_cond.get("message"):
            return ready_cond["message"]
    return ""


def _restart_reason(status):
    """最近一次重启的原因(与原 get_dpm_pods 一致:取第一个有 lastState 的容器)"""
    for cs in status.get("containerStatuses") or []:
        last = cs.get("lastState") or {}
        if last.get("terminated"):
            terminated = last["terminated"]
            return "Terminated: %s (%s)" % (terminated.get("reason") or "", terminated.get("exitCode"))
        if last.get("waiting"):
            return "Waiting: %s" % (last["waiting"].get("reason") or "")
    return ""


def slim_pod(pod):
    """把原始 pod 压成页面需要的精简 dict(内存里只存这个)"""
    meta = pod.get("metadata") or {}
    spec = pod.get("spec") or {}
    status = pod.get("status") or {}
    display_status, ready_containers, total_containers, restarts = kubectl_pod_status(pod)
    deployment, isolated = pod_deployment(pod)
    ready = _condition_true(status, "Ready")
    containers = spec.get("containers") or []
    container_names = [c.get("name") for c in containers]
    # init 容器也有日志要看
    container_names += [c.get("name") for c in spec.get("initContainers") or []]
    return {
        "name": meta.get("name") or "",
        "namespace": meta.get("namespace") or "",
        "deployment": deployment,
        "isolated": isolated,
        "status": display_status,
        "phase": status.get("phase") or "",
        "ready": ready,
        "ready_count": "%d/%d" % (ready_containers, total_containers),
        "deleting": bool(meta.get("deletionTimestamp")),
        "pod_ip": status.get("podIP"),
        "node_name": spec.get("nodeName"),
        "created_at": format_time(meta.get("creationTimestamp")),
        "app_label": (meta.get("labels") or {}).get("app", "无"),
        "image": containers[0].get("image", "") if containers else "",
        "restart_count": restarts,
        "restart_reason": _restart_reason(status) if restarts else "",
        "exception_reason": _exception_reason(pod, display_status, ready),
        "containers": container_names,
    }


def slim_deployment(deployment):
    """把原始 deployment 压成滚动状态需要的字段"""
    meta = deployment.get("metadata") or {}
    spec = deployment.get("spec") or {}
    status = deployment.get("status") or {}
    progressing = _condition(status, "Progressing") or {}
    replica_failure = _condition(status, "ReplicaFailure")
    failure_text = ""
    if replica_failure and replica_failure.get("status") == "True":
        failure_text = replica_failure.get("message") or replica_failure.get("reason") or ""
    replicas = spec.get("replicas")
    return {
        "namespace": meta.get("namespace") or "",
        "name": meta.get("name") or "",
        "desired": 1 if replicas is None else replicas,
        "replicas": status.get("replicas") or 0,
        "ready": status.get("readyReplicas") or 0,
        "updated": status.get("updatedReplicas") or 0,
        "available": status.get("availableReplicas") or 0,
        "generation": meta.get("generation") or 0,
        "observed_generation": status.get("observedGeneration") or 0,
        "paused": bool(spec.get("paused")),
        "progress_reason": progressing.get("reason") or "",
        "progress_message": progressing.get("message") or "",
        "replica_failure": failure_text,
    }


def rollout_state(dep):
    """照搬 kubectl rollout status。返回 (state, message)

    state:complete 已完成 / observing 控制器还没处理新版本 / progressing 滚动中 /
          paused 已暂停 / failed 超过 progressDeadlineSeconds
    """
    if dep["generation"] > dep["observed_generation"]:
        return "observing", "等待控制器处理新版本"
    if dep["progress_reason"] == "ProgressDeadlineExceeded":
        message = "滚动超时"
        if dep["progress_message"]:
            message += ": " + dep["progress_message"]
        return "failed", message
    if dep["updated"] < dep["desired"]:
        state, message = "progressing", "已更新 %d/%d" % (dep["updated"], dep["desired"])
    elif dep["replicas"] > dep["updated"]:
        state, message = "progressing", "%d 个旧副本待终止" % (dep["replicas"] - dep["updated"])
    elif dep["available"] < dep["updated"]:
        state, message = "progressing", "可用 %d/%d" % (dep["available"], dep["updated"])
    else:
        return "complete", ""
    if dep["paused"]:
        state, message = "paused", "已暂停 | " + message
    if dep["replica_failure"]:
        message += " | " + dep["replica_failure"]
    return state, message


def status_row(dep, pods):
    """deployment 一行的实时状态。pods 是归属这个 deployment 的精简 pod(含被隔离的)

    status.replicas 不含正在终止的 pod,终止中 / 已隔离的数量从 pod 里数
    """
    terminating = sum(1 for p in pods if p["deleting"] and not p["isolated"])
    isolated = sum(1 for p in pods if p["isolated"])
    state, message = rollout_state(dep)
    if state == "complete" and terminating:
        state, message = "progressing", "%d 个 Pod 终止中" % terminating
    return {
        "namespace": dep["namespace"],
        "deployment": dep["name"],
        "desired": dep["desired"],
        "ready": dep["ready"],
        "updated": dep["updated"],
        "available": dep["available"],
        "total": dep["replicas"],
        "terminating": terminating,
        "isolated": isolated,
        "pods": len(pods),
        "state": state,
        "message": message,
    }


def pod_view(rec, metrics=None, with_metrics=True):
    """get_dpm_pods / 推送给页面的 pod 结构,保留原接口的全部字段

    with_metrics=False 时不带 cpu/memory 键(推送用,页面保留上次拿到的值);
    带 metrics 但这个 pod 没有指标(metrics-server 未装 / 新 pod 还没采到)时显示 "-"
    """
    view = {
        "name": rec["name"],
        "status": rec["status"],
        "phase": rec["phase"],
        "ready": rec["ready"],
        "ready_count": rec["ready_count"],
        "pod_ip": rec["pod_ip"],
        "created_at": rec["created_at"],
        "app_label": rec["app_label"],
        "image": rec["image"],
        "node_name": rec["node_name"],
        "restart_count": rec["restart_count"],
        "restart_reason": rec["restart_reason"],
        "exception_reason": rec["exception_reason"],
        "containers": rec["containers"],
        "isolated": rec["isolated"],
        "deleting": rec["deleting"],
    }
    if with_metrics:
        if metrics:
            view["cpu"] = "%dm" % round(metrics.get("cpu_m", 0))
            view["memory"] = "%dMB" % round(metrics.get("memory_mb", 0))
        else:
            view["cpu"] = "-"
            view["memory"] = "-"
    return view
