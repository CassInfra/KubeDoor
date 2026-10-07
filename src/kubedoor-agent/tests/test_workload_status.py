import pytest


def make_pod(
    name="web-7d9f8c6b5-abcde",
    namespace="ns",
    phase="Running",
    containers=None,
    statuses=None,
    init_containers=None,
    init_statuses=None,
    conditions=None,
    owner="web-7d9f8c6b5",
    pod_hash="7d9f8c6b5",
    deleting=False,
    reason=None,
    message=None,
    labels=None,
):
    meta = {"name": name, "namespace": namespace, "creationTimestamp": "2026-10-04T03:12:45Z", "labels": {"app": "web"}}
    if pod_hash:
        meta["labels"]["pod-template-hash"] = pod_hash
    if labels:
        meta["labels"].update(labels)
    if owner:
        meta["ownerReferences"] = [{"kind": "ReplicaSet", "name": owner, "controller": True}]
    if deleting:
        meta["deletionTimestamp"] = "2026-10-04T03:20:00Z"
    status = {
        "phase": phase,
        "containerStatuses": statuses if statuses is not None else [{"name": "app", "ready": True, "restartCount": 0, "state": {"running": {}}}],
        "conditions": conditions if conditions is not None else [{"type": "Ready", "status": "True"}],
        "podIP": "10.0.0.1",
    }
    if init_statuses is not None:
        status["initContainerStatuses"] = init_statuses
    if reason:
        status["reason"] = reason
    if message:
        status["message"] = message
    spec = {"containers": containers or [{"name": "app", "image": "repo/web:1.0"}], "nodeName": "node-1"}
    if init_containers:
        spec["initContainers"] = init_containers
    return {"metadata": meta, "spec": spec, "status": status}


def waiting(reason, message=None, restarts=0, name="app"):
    state = {"reason": reason}
    if message:
        state["message"] = message
    return {"name": name, "ready": False, "restartCount": restarts, "state": {"waiting": state}}


def terminated(reason, exit_code, name="app", **extra):
    return {"name": name, "ready": False, "restartCount": 0, "state": {"terminated": {"reason": reason, "exitCode": exit_code, **extra}}}


def running(name="app", ready=True, restarts=0, started=None):
    status = {"name": name, "ready": ready, "restartCount": restarts, "state": {"running": {}}}
    if started is not None:
        status["started"] = started
    return status


# ---------------------------------------------------------------------- kubectl STATUS
def test_running_ready(workload_modules):
    assert workload_modules.status.kubectl_pod_status(make_pod()) == ("Running", 1, 1, 0)


def test_terminating_overrides_running(workload_modules):
    status, *_ = workload_modules.status.kubectl_pod_status(make_pod(deleting=True))
    assert status == "Terminating"


def test_node_lost_shows_unknown(workload_modules):
    pod = make_pod(deleting=True, reason="NodeLost")
    assert workload_modules.status.kubectl_pod_status(pod)[0] == "Unknown"


def test_deleting_terminal_pod_keeps_reason(workload_modules):
    pod = make_pod(phase="Failed", reason="Evicted", deleting=True, statuses=[])
    assert workload_modules.status.kubectl_pod_status(pod)[0] == "Evicted"


@pytest.mark.parametrize(
    "phase,statuses,expected",
    [
        ("Pending", [waiting("ContainerCreating")], "ContainerCreating"),
        ("Running", [waiting("CrashLoopBackOff", restarts=5)], "CrashLoopBackOff"),
        ("Pending", [waiting("ImagePullBackOff", "Back-off pulling image")], "ImagePullBackOff"),
        ("Running", [terminated("OOMKilled", 137)], "OOMKilled"),
        ("Running", [terminated(None, 143, signal=15)], "Signal:15"),
        ("Running", [terminated(None, 2)], "ExitCode:2"),
        ("Pending", [], "Pending"),
    ],
)
def test_container_states(workload_modules, phase, statuses, expected):
    pod = make_pod(phase=phase, statuses=statuses)
    assert workload_modules.status.kubectl_pod_status(pod)[0] == expected


def test_restart_count_summed(workload_modules):
    pod = make_pod(statuses=[waiting("CrashLoopBackOff", restarts=5)])
    assert workload_modules.status.kubectl_pod_status(pod) == ("CrashLoopBackOff", 0, 1, 5)


@pytest.mark.parametrize(
    "init_statuses,expected",
    [
        ([running(name="init-a"), waiting("PodInitializing", name="init-b")], "Init:0/2"),
        ([terminated("Completed", 0, name="init-a"), running(name="init-b")], "Init:1/2"),
        ([terminated("Error", 1, name="init-a")], "Init:Error"),
        ([waiting("CrashLoopBackOff", name="init-a")], "Init:CrashLoopBackOff"),
        ([terminated(None, 3, name="init-a")], "Init:ExitCode:3"),
    ],
)
def test_init_containers(workload_modules, init_statuses, expected):
    pod = make_pod(
        phase="Pending",
        init_containers=[{"name": "init-a"}, {"name": "init-b"}],
        init_statuses=init_statuses,
        statuses=[waiting("PodInitializing")],
    )
    assert workload_modules.status.kubectl_pod_status(pod)[0] == expected


def test_started_sidecar_counts_as_ready_container(workload_modules):
    pod = make_pod(
        init_containers=[{"name": "istio-proxy", "restartPolicy": "Always"}],
        init_statuses=[running(name="istio-proxy", started=True, restarts=2)],
        conditions=[{"type": "Initialized", "status": "True"}, {"type": "Ready", "status": "True"}],
    )
    assert workload_modules.status.kubectl_pod_status(pod) == ("Running", 2, 2, 2)


@pytest.mark.parametrize("ready_condition,expected", [("True", "Running"), ("False", "NotReady")])
def test_completed_container_with_running_sibling(workload_modules, ready_condition, expected):
    pod = make_pod(
        containers=[{"name": "app"}, {"name": "job"}],
        statuses=[running(name="app"), terminated("Completed", 0, name="job")],
        conditions=[{"type": "Ready", "status": ready_condition}],
    )
    assert workload_modules.status.kubectl_pod_status(pod)[0] == expected


def test_scheduling_gated(workload_modules):
    pod = make_pod(
        phase="Pending",
        statuses=[],
        conditions=[{"type": "PodScheduled", "status": "False", "reason": "SchedulingGated"}],
    )
    assert workload_modules.status.kubectl_pod_status(pod)[0] == "SchedulingGated"


# ---------------------------------------------------------------------- pod 归属
def test_owned_pod_belongs_to_deployment(workload_modules):
    assert workload_modules.status.pod_deployment(make_pod(name="web-api-7d9f8c6b5-abcde", owner="web-api-7d9f8c6b5")) == ("web-api", False)


def test_isolated_pod_matched_by_hash(workload_modules):
    pod = make_pod(name="web-api-7d9f8c6b5-abcde", owner=None, labels={"app": "web-api-ALERT"})
    assert workload_modules.status.pod_deployment(pod) == ("web-api", True)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"owner": None, "name": "web-api-7d9f8c6b5"},  # 被截断,没有随机后缀
        {"owner": None, "name": "web-api-otherhash-abcde"},  # hash 对不上
        {"pod_hash": None},  # 不是 Deployment 建的 RS(StatefulSet、裸 RS 等)
        {"owner": "web-api-somethingelse"},  # RS 名和 hash 不符
    ],
)
def test_unmatched_pods_are_not_attributed(workload_modules, kwargs):
    assert workload_modules.status.pod_deployment(make_pod(**kwargs)) == (None, False)


def test_non_replicaset_controller_not_attributed(workload_modules):
    pod = make_pod()
    pod["metadata"]["ownerReferences"] = [{"kind": "StatefulSet", "name": "web", "controller": True}]
    assert workload_modules.status.pod_deployment(pod) == (None, False)


# ---------------------------------------------------------------------- slim / view
def test_slim_pod_fields(workload_modules):
    rec = workload_modules.status.slim_pod(make_pod())
    assert rec["deployment"] == "web"
    assert rec["isolated"] is False
    assert rec["status"] == "Running"
    assert rec["ready"] is True
    assert rec["ready_count"] == "1/1"
    assert rec["created_at"] == "2026-10-04 11:12:45"
    assert rec["image"] == "repo/web:1.0"
    assert rec["exception_reason"] == ""
    assert rec["restart_reason"] == ""


def test_slim_pod_unscheduled_reason(workload_modules):
    pod = make_pod(
        phase="Pending",
        statuses=[],
        conditions=[{"type": "PodScheduled", "status": "False", "message": "0/3 nodes are available"}],
    )
    rec = workload_modules.status.slim_pod(pod)
    assert rec["status"] == "Pending"
    assert rec["ready"] is False
    assert rec["exception_reason"] == "0/3 nodes are available"


def test_slim_pod_running_but_not_ready_reason(workload_modules):
    pod = make_pod(
        statuses=[running(ready=False)],
        conditions=[{"type": "Ready", "status": "False", "message": "containers with unready status: [app]"}],
    )
    rec = workload_modules.status.slim_pod(pod)
    assert rec["ready"] is False
    assert rec["exception_reason"] == "containers with unready status: [app]"


def test_slim_pod_restart_reason(workload_modules):
    status = running(restarts=3)
    status["lastState"] = {"terminated": {"reason": "OOMKilled", "exitCode": 137}}
    rec = workload_modules.status.slim_pod(make_pod(statuses=[status]))
    assert rec["restart_count"] == 3
    assert rec["restart_reason"] == "Terminated: OOMKilled (137)"


def test_pod_view_metrics(workload_modules):
    rec = workload_modules.status.slim_pod(make_pod())
    view = workload_modules.status.pod_view(rec, {"cpu_m": 120.4, "memory_mb": 511.6})
    assert view["cpu"] == "120m" and view["memory"] == "512MB"
    assert workload_modules.status.pod_view(rec)["cpu"] == "-"
    assert "cpu" not in workload_modules.status.pod_view(rec, with_metrics=False)


# ---------------------------------------------------------------------- 滚动状态
def make_dep(**overrides):
    dep = {
        "namespace": "ns",
        "name": "web",
        "desired": 3,
        "replicas": 3,
        "ready": 3,
        "updated": 3,
        "available": 3,
        "generation": 2,
        "observed_generation": 2,
        "paused": False,
        "progress_reason": "NewReplicaSetAvailable",
        "progress_message": "",
        "replica_failure": "",
    }
    dep.update(overrides)
    return dep


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({}, ("complete", "")),
        ({"generation": 3}, ("observing", "等待控制器处理新版本")),
        ({"updated": 1}, ("progressing", "已更新 1/3")),
        ({"replicas": 4}, ("progressing", "1 个旧副本待终止")),
        ({"available": 2}, ("progressing", "可用 2/3")),
        ({"progress_reason": "ProgressDeadlineExceeded", "progress_message": "timed out"}, ("failed", "滚动超时: timed out")),
        ({"updated": 1, "paused": True}, ("paused", "已暂停 | 已更新 1/3")),
        ({"desired": 5, "updated": 5, "available": 3, "replicas": 5, "replica_failure": "exceeded quota"}, ("progressing", "可用 3/5 | exceeded quota")),
    ],
)
def test_rollout_state(workload_modules, overrides, expected):
    assert workload_modules.status.rollout_state(make_dep(**overrides)) == expected


def test_slim_deployment_defaults(workload_modules):
    dep = workload_modules.status.slim_deployment({"metadata": {"name": "web", "namespace": "ns"}, "spec": {}, "status": {}})
    assert dep["desired"] == 1
    assert workload_modules.status.rollout_state(dep)[0] == "progressing"
    zero = workload_modules.status.slim_deployment({"metadata": {"name": "web", "namespace": "ns"}, "spec": {"replicas": 0}, "status": {}})
    assert zero["desired"] == 0
    assert workload_modules.status.rollout_state(zero) == ("complete", "")


def test_status_row_counts_terminating_and_isolated(workload_modules):
    status = workload_modules.status
    pods = [
        status.slim_pod(make_pod(name="web-7d9f8c6b5-aaaaa")),
        status.slim_pod(make_pod(name="web-7d9f8c6b5-bbbbb", deleting=True)),
        status.slim_pod(make_pod(name="web-7d9f8c6b5-ccccc", owner=None, deleting=True)),
    ]
    row = status.status_row(make_dep(), pods)
    assert row["terminating"] == 1  # 被隔离的 pod 不算进滚动
    assert row["isolated"] == 1
    assert row["pods"] == 3
    assert (row["state"], row["message"]) == ("progressing", "1 个 Pod 终止中")
