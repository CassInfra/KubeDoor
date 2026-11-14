"""Exercise the standalone upgrade against a legacy cluster, including Deployment changes,
the migration of legacy configuration into the database and the cleanup afterwards."""
import json
import re
import subprocess
import sys

import pytest

from bootstrap_shell import ROOT, SCRIPT, ShellCluster, documents, state_object

TARGET = re.search(r'^TAG_MASTER="([^"]+)"', SCRIPT.read_text(encoding="utf-8"), re.M).group(1)
LEGACY_NGINX = """events {}
http {
    log_format ai_safe escape=json '$uri';
    upstream kubedoor_master { server kubedoor-master.kubedoor:80; }
    server {
        listen 80;
        location ~ ^/(api|ws)(/|$) {
            proxy_pass http://kubedoor_master;
        }
        location /custom { return 200; }
    }
}
"""
RULES = "kubedoor-master-file-cfg"
RULES_VOLUME = {"name": RULES, "configMap": {"name": RULES, "items": [{"key": "alert_rules.json", "path": "alert_rules.json"}]}}
RULES_MOUNT = {"name": RULES, "mountPath": "/k8s_event/rules"}
# 旧配置里的密码：升级的输出、预览和日志里都不能出现
LEGACY_SECRET = "legacy-vm-secret"
PG_KEYS = {"PG_HOST": "production-db", "PG_PASSWORD": "pg-secret", "PG_POOL_MAX": "30"}
MIGRATED = {"PROM_TYPE": "Victoria-Metrics-Single", "PROM_URL": f"http://monit:{LEGACY_SECRET}@vm.example.invalid:8428",
            "PROM_K8S_TAG_KEY": "origin_prometheus", "AGENT_INSTALL_DEFAULTS": '{"image_repo": "private.example:5443/team"}',
            "KUBEDOOR_EXTURL": "https://kd.example.invalid", "LOG_LEVEL": "INFO", "RETENTION_EVENTS_DAYS": "30",
            "AUTH_COOKIE_SECURE": "true", "UPDATE_IMAGE": "legacy-rules", "MSG_TOKEN": "legacy-im-token"}
MIGRATE = ["python3", "-m", "general_settings", "migrate"]


def legacy_objects(optional=True, *, web=False, migrated=False):
    """旧版控制端：web=True 为网页安装（PG 和时序库地址在 Secret kubedoor-connection），否则为旧脚本安装；
    migrated=True 为 2.6.0 网页安装（没有 kubedoor-config 和事件告警规则挂载）。"""
    objects = {}
    components = ["master", "ai", "alarm", "web"] if optional else ["master", "web"]
    for component in components:
        name = "kubedoor-" + component
        objects["deployment/" + name] = {
            "apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name},
            "spec": {"replicas": 2, "selector": {"matchLabels": {"app": name}},
                     "strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 1}},
                     "template": {"metadata": {"annotations": {"custom": "keep-me"}}, "spec": {
                         "nodeSelector": {"custom": "yes"}, "imagePullSecrets": [{"name": "private-pull"}],
                         "containers": [{"name": name, "image": "private.example:5443/team/" + name + ":2.3.0",
                                         "env": [{"name": "CUSTOM", "value": "do-not-change"}],
                                         "envFrom": [{"secretRef": {"name": "existing-connection"}}],
                                         "resources": {"limits": {"memory": "4Gi"}},
                                         "volumeMounts": [{"name": "custom", "mountPath": "/custom"}]},
                                        {"name": "sidecar", "image": "custom-sidecar:1"}],
                         "volumes": [{"name": "custom", "emptyDir": {}}]}}}}
    if not migrated:
        pod = objects["deployment/kubedoor-master"]["spec"]["template"]["spec"]
        pod["containers"][0]["volumeMounts"].append(dict(RULES_MOUNT))
        pod["volumes"].append(json.loads(json.dumps(RULES_VOLUME)))
        objects["configmap/" + RULES] = {"kind": "ConfigMap", "metadata": {"name": RULES},
                                         "data": {"alert_rules.json": '{"global_ignore_rules": [], "alert_rules": []}'}}
        data = dict(MIGRATED) if web else {**PG_KEYS, **MIGRATED}
        objects["configmap/kubedoor-config"] = {"kind": "ConfigMap", "metadata": {"name": "kubedoor-config"}, "data": data}
    if web or migrated:
        objects["configmap/kubedoor-setup-state"] = state_object("succeeded")
        objects["secret/kubedoor-connection"] = {"kind": "Secret", "metadata": {"name": "kubedoor-connection"},
                                                 "data": {"PG_HOST": "cHJvZHVjdGlvbi1kYg==", "PROM_URL": "c2VjcmV0"}}
    objects["configmap/nginx-config"] = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "nginx-config"},
                                        "data": {"nginx.conf": LEGACY_NGINX, "other": "keep-me"}}
    objects["secret/kubedoor-ai-security"] = {"kind": "Secret", "metadata": {"name": "kubedoor-ai-security"},
                                             "data": {"AI_ENCRYPTION_KEY": "do-not-replace"}}
    objects["service/kubedoor-web"] = {"kind": "Service", "metadata": {"name": "kubedoor-web"},
                                      "spec": {"ports": [{"port": 80, "nodePort": 30080}]}}
    return objects


def index_of(calls, predicate):
    return next(i for i, call in enumerate(calls) if predicate(call["command"], call))


def is_rules_patch(command, call):
    return command[:3] == ["patch", "deployment", "kubedoor-master"] and "$patch" in json.dumps(call.get("patch"))


def mutation_commands(cluster):
    return [call["command"][:3] for call in cluster.mutations()]


def master_pod(objects):
    return objects["deployment/kubedoor-master"]["spec"]["template"]["spec"]


def assert_legacy_kept(before, after):
    """迁移没有成功：规则挂载、规则 ConfigMap、kubedoor-config 都原样保留。"""
    pod = master_pod(after)
    assert RULES_MOUNT in pod["containers"][0]["volumeMounts"] and RULES_VOLUME in pod["volumes"]
    assert after["configmap/" + RULES] == before["configmap/" + RULES]
    assert after["configmap/kubedoor-config"] == before["configmap/kubedoor-config"]


def assert_no_cleanup(cluster):
    commands = mutation_commands(cluster)
    assert ["delete", "configmap", RULES] not in commands
    assert ["patch", "configmap", "kubedoor-config"] not in commands
    assert not any(is_rules_patch(call["command"], call) for call in cluster.mutations())


def test_generated_deployments_follow_published_manifests():
    result = subprocess.run([sys.executable, str(ROOT / "tools/build-upgrade-patches.py"), "--check"],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_generated_master_patch_drops_rules_mount_but_keeps_it_until_cleanup():
    # 新模板没有规则卷：strategic merge 补丁不提它，升级时旧 Deployment 上的规则卷保留，新 master 启动时还能导入
    script = SCRIPT.read_text(encoding="utf-8")
    generated = script.split("# BEGIN GENERATED DEPLOYMENT UPGRADE PATCHES", 1)[1].split("# END GENERATED", 1)[0]
    assert "kubedoor-master-file-cfg" not in generated and "/k8s_event/rules" not in generated


@pytest.mark.parametrize("shell", ["sh", "bash"])
def test_upgrade_230_adds_cache_and_preserves_live_configuration(tmp_path, shell):
    cluster = ShellCluster(tmp_path, legacy_objects())
    before = cluster.read()["objects"]
    result = cluster.run("upgrade", "--yes", "--context", "production", "--namespace", "custom", shell=shell)
    assert result.returncode == 0, result.stderr
    after = cluster.read()["objects"]
    backup = documents((tmp_path / "kubedoor-upgrade-backup.yaml").read_text(encoding="utf-8"))
    # 4 个 Deployment + nginx-config + 旧配置 kubedoor-config 和规则 ConfigMap（旧脚本安装没有 kubedoor-connection）
    assert len(backup) == 7
    saved = {(obj["kind"], obj["metadata"]["name"]) for obj in backup}
    assert {("ConfigMap", "kubedoor-config"), ("ConfigMap", RULES)} <= saved
    saved_ai = next(obj for obj in backup if obj["metadata"]["name"] == "kubedoor-ai")
    assert saved_ai == before["deployment/kubedoor-ai"]
    assert next(obj for obj in backup if obj["metadata"]["name"] == "kubedoor-config") == before["configmap/kubedoor-config"]
    for component in ("master", "ai", "alarm", "web"):
        key = "deployment/kubedoor-" + component
        previous, current = before[key]["spec"], after[key]["spec"]
        assert current["replicas"] == previous["replicas"]
        assert current["selector"] == previous["selector"]
        assert current["template"]["metadata"] == previous["template"]["metadata"]
        pod = current["template"]["spec"]
        container = pod["containers"][0]
        assert container["image"] == "private.example:5443/team/kubedoor-" + component + ":" + TARGET
        for field in ("env", "envFrom", "resources"):
            assert container[field] == previous["template"]["spec"]["containers"][0][field]
        assert pod["containers"][1] == previous["template"]["spec"]["containers"][1]
        assert pod["imagePullSecrets"] == [{"name": "private-pull"}]
        assert pod["nodeSelector"] == {"custom": "yes"}
        assert "readinessProbe" in container and "livenessProbe" in container
        assert {"name": "custom", "emptyDir": {}} in pod["volumes"]
        assert {"name": "custom", "mountPath": "/custom"} in container["volumeMounts"]
    ai = after["deployment/kubedoor-ai"]["spec"]
    assert ai["strategy"] == {"type": "Recreate"}
    pod = ai["template"]["spec"]
    assert {"name": "code-cache", "emptyDir": {"sizeLimit": "10Gi"}} in pod["volumes"]
    assert {"name": "code-cache", "mountPath": "/data/code-cache"} in pod["containers"][0]["volumeMounts"]
    # 迁移成功后清理：master 不再挂载规则，规则 ConfigMap 删除，kubedoor-config 只留数据库连接
    master = master_pod(after)
    assert RULES_MOUNT not in master["containers"][0]["volumeMounts"]
    assert all(volume["name"] != RULES for volume in master["volumes"])
    assert "configmap/" + RULES not in after
    assert after["configmap/kubedoor-config"]["data"] == PG_KEYS
    for key in ("secret/kubedoor-ai-security", "service/kubedoor-web"):
        assert after[key] == before[key]
    nginx = after["configmap/nginx-config"]["data"]["nginx.conf"]
    assert "location ^~ /api/admin/agent-installations" in nginx
    assert "location /custom { return 200; }" in nginx
    assert "access_log /var/log/nginx/access.log ai_safe;" in nginx
    calls = cluster.read()["calls"]
    assert all(call["args"][:4] == ["--context", "production", "-n", "custom"] for call in calls)
    first_write = next(i for i, call in enumerate(calls) if call in cluster.mutations())
    assert len([call for call in calls[:first_write] if "--dry-run=server" in call["command"]]) == 4
    assert "kubedoor-setup" not in " ".join(" ".join(call["command"]) for call in cluster.mutations())
    # master 就绪后、升级 ai 之前在 master 容器里迁移一次；全部组件就绪后才清理，清理前先试运行
    executions = [call for call in calls if call["command"][0] == "exec"]
    assert len(executions) == 1 and executions[0]["exec"] == MIGRATE
    assert executions[0]["command"][:5] == ["exec", "deploy/kubedoor-master", "-c", "kubedoor-master", "--"]
    migrate = calls.index(executions[0])
    assert index_of(calls, lambda c, _: c[:3] == ["rollout", "status", "deployment/kubedoor-master"]) < migrate
    assert migrate < index_of(calls, lambda c, _: c[:3] == ["patch", "deployment", "kubedoor-ai"] and "--dry-run=server" not in c)
    web_ready = index_of(calls, lambda c, _: c[:3] == ["rollout", "status", "deployment/kubedoor-web"])
    removal = index_of(calls, lambda c, call: is_rules_patch(c, call) and "--dry-run=server" not in c)
    assert web_ready < index_of(calls, lambda c, call: is_rules_patch(c, call) and "--dry-run=server" in c) < removal
    assert removal < [i for i, call in enumerate(calls)
                      if call["command"][:3] == ["rollout", "status", "deployment/kubedoor-master"]][1]
    deletion = index_of(calls, lambda c, _: c[:3] == ["delete", "configmap", RULES])
    assert removal < deletion < index_of(calls, lambda c, _: c[:3] == ["patch", "configmap", "kubedoor-config"]
                                         and "--dry-run=server" not in c)
    assert "旧配置已清理" in result.stdout
    assert LEGACY_SECRET not in result.stdout + result.stderr and "legacy-im-token" not in result.stdout + result.stderr


def test_web_install_upgrade_backs_up_connection_and_empties_legacy_config(tmp_path):
    cluster = ShellCluster(tmp_path, legacy_objects(web=True))
    before = cluster.read()["objects"]
    result = cluster.run("upgrade", "--yes")
    assert result.returncode == 0, result.stderr
    after = cluster.read()["objects"]
    backup = documents((tmp_path / "kubedoor-upgrade-backup.yaml").read_text(encoding="utf-8"))
    saved = {(obj["kind"], obj["metadata"]["name"]) for obj in backup}
    assert len(backup) == 8
    assert {("ConfigMap", "kubedoor-config"), ("Secret", "kubedoor-connection"), ("ConfigMap", RULES)} <= saved
    # 网页安装的 kubedoor-config 没有 PG_*：清理后为空，但保留（工作负载还引用它）；Secret 不动
    assert after["configmap/kubedoor-config"]["data"] == {}
    assert after["secret/kubedoor-connection"] == before["secret/kubedoor-connection"]
    assert "configmap/" + RULES not in after
    assert after["configmap/kubedoor-setup-state"]["data"]["phase"] == "succeeded"


def test_already_migrated_install_has_nothing_to_clean(tmp_path):
    cluster = ShellCluster(tmp_path, legacy_objects(migrated=True))
    result = cluster.run("upgrade", "--yes")
    assert result.returncode == 0, result.stderr
    backup = documents((tmp_path / "kubedoor-upgrade-backup.yaml").read_text(encoding="utf-8"))
    assert {obj["metadata"]["name"] for obj in backup} == {
        "kubedoor-master", "kubedoor-ai", "kubedoor-alarm", "kubedoor-web", "nginx-config", "kubedoor-connection"}
    calls = cluster.read()["calls"]
    assert [call["exec"] for call in calls if call["command"][0] == "exec"] == [MIGRATE]
    assert not any(is_rules_patch(call["command"], call) for call in calls)
    assert ["patch", "configmap", "kubedoor-config"] not in mutation_commands(cluster)
    assert "configmap/kubedoor-config" not in cluster.read()["objects"]


@pytest.mark.parametrize("failure", ["invalid", "error", "forbidden"])
def test_migration_problems_finish_upgrade_but_keep_legacy_configuration(tmp_path, failure):
    summary = {"ok": False, "general_settings": {"imported": ["kubedoor_exturl"], "present": ["prom_type"],
                                                 "invalid": ["prom_url", "oss_url"]},
               "notification": "invalid", "image_settings": {"rules": "imported", "registries": "absent"},
               "event_rules": "invalid"}
    exec_result = {
        "invalid": {"code": 3, "stdout": "general_settings: 2 项旧配置无法导入\n" + json.dumps(summary)},
        "error": {"code": 1, "stdout": "", "stderr": "数据库连接失败"},
        "forbidden": {"code": 1, "stdout": "", "stderr": 'Error from server (Forbidden): pods "kubedoor-master-0" is forbidden: '
                                                         'User "ops" cannot create resource "pods/exec"'},
    }[failure]
    cluster = ShellCluster(tmp_path, legacy_objects(), exec_result=exec_result)
    before = cluster.read()["objects"]
    result = cluster.run("upgrade", "--yes")
    # 迁移有问题不影响升级本身：全部组件照常升级，旧配置保留不动，也不清理
    assert result.returncode == 0, result.stderr
    after = cluster.read()["objects"]
    for component in ("master", "ai", "alarm", "web"):
        container = after["deployment/kubedoor-" + component]["spec"]["template"]["spec"]["containers"][0]
        assert container["image"].endswith(":" + TARGET)
    assert_legacy_kept(before, after)
    assert_no_cleanup(cluster)
    if failure == "invalid":
        assert ("以下旧配置没能迁入数据库：通用配置 prom_url、oss_url；消息通知（MSG_TYPE / MSG_TOKEN / DEFAULT_AT）；"
                "事件告警规则（ConfigMap kubedoor-master-file-cfg）") in result.stderr
        assert "填好后再执行一次 upgrade" in result.stderr
        assert "迁移结果：" + json.dumps(summary) in result.stdout
    else:
        assert "迁移检查失败（退出码 1）" in result.stderr and "pods/exec" in result.stderr
    assert "旧配置保留不动" in result.stderr


def test_migration_summary_is_read_with_plain_posix_shell(tmp_path):
    # 汇总只用 shell 内建解析：紧凑格式的 JSON 在 dash 下也能列出导入不了的项
    summary = {"ok": False, "general_settings": {"imported": [], "present": [], "invalid": ["prom_url"]},
               "notification": "absent", "image_settings": {"rules": "present", "registries": "invalid"},
               "event_rules": "present"}
    cluster = ShellCluster(tmp_path, legacy_objects(),
                           exec_result={"code": 3, "stdout": json.dumps(summary, separators=(",", ":"))})
    before = cluster.read()["objects"]
    result = cluster.run("upgrade", "--yes", shell="dash")
    assert result.returncode == 0, result.stderr
    assert "以下旧配置没能迁入数据库：通用配置 prom_url；镜像仓库凭据（REGISTRY_SECRET）" in result.stderr
    assert_legacy_kept(before, cluster.read()["objects"])


def test_rerun_after_fixing_migration_cleans_up_and_later_runs_are_noops(tmp_path):
    summary = {"ok": False, "general_settings": {"imported": [], "present": [], "invalid": ["prom_url"]},
               "notification": "absent", "image_settings": {"rules": "present", "registries": "absent"}, "event_rules": "present"}
    cluster = ShellCluster(tmp_path, legacy_objects(), exec_result={"code": 3, "stdout": json.dumps(summary)})
    before = cluster.read()["objects"]
    first = cluster.run("upgrade", "--yes")
    assert first.returncode == 0, first.stderr
    assert_legacy_kept(before, cluster.read()["objects"])
    # 用户在控制台填好之后（库里有了，下次算已迁移）再执行一次 upgrade：迁移成功，清理
    state = cluster.read()
    state.pop("exec_result")
    cluster.write(state)
    second = cluster.run("upgrade", "--yes", "--backup", "second.yaml")
    assert second.returncode == 0, second.stderr
    after = cluster.read()["objects"]
    assert "configmap/" + RULES not in after and after["configmap/kubedoor-config"]["data"] == PG_KEYS
    assert all(volume["name"] != RULES for volume in master_pod(after)["volumes"])
    # 第二次的备份里有清理前的旧配置
    backup = documents((tmp_path / "second.yaml").read_text(encoding="utf-8"))
    assert {"kubedoor-config", RULES} <= {obj["metadata"]["name"] for obj in backup}
    # 再执行也安全：没有规则挂载要去掉，kubedoor-config 的补丁是空操作
    calls = len(cluster.read()["calls"])
    third = cluster.run("upgrade", "--yes", "--backup", "third.yaml")
    assert third.returncode == 0, third.stderr
    later = cluster.read()["calls"][calls:]
    assert not any(is_rules_patch(call["command"], call) for call in later)
    assert cluster.read()["objects"]["configmap/kubedoor-config"]["data"] == PG_KEYS


def test_master_not_ready_after_removing_rules_mount_fails_and_rerun_finishes_cleanup(tmp_path):
    # 去掉规则挂载后 master 没有就绪：脚本非零退出、提示备份，规则 ConfigMap 和 kubedoor-config 都还没动
    cluster = ShellCluster(tmp_path, legacy_objects(), rollout_error="deployment/kubedoor-master#2")
    before = cluster.read()["objects"]
    result = cluster.run("upgrade", "--yes")
    assert result.returncode != 0
    assert "去掉事件告警规则挂载后未就绪" in result.stderr and "kubedoor-upgrade-backup.yaml" in result.stderr
    after = cluster.read()["objects"]
    assert all(volume["name"] != RULES for volume in master_pod(after)["volumes"])
    assert after["configmap/" + RULES] == before["configmap/" + RULES]
    assert after["configmap/kubedoor-config"] == before["configmap/kubedoor-config"]
    # 修好后重跑：补丁是空操作，迁移再确认一次，接着完成清理
    state = cluster.read()
    state.pop("rollout_error")
    cluster.write(state)
    retry = cluster.run("upgrade", "--yes", "--backup", "retry.yaml")
    assert retry.returncode == 0, retry.stderr
    after = cluster.read()["objects"]
    assert "configmap/" + RULES not in after and after["configmap/kubedoor-config"]["data"] == PG_KEYS


def test_failed_cleanup_dry_run_keeps_everything(tmp_path):
    cluster = ShellCluster(tmp_path, legacy_objects(), dry_run_error="deployment/kubedoor-master $patch")
    before = cluster.read()["objects"]
    result = cluster.run("upgrade", "--yes")
    assert result.returncode == 0, result.stderr
    assert "试运行未通过" in result.stderr
    after = cluster.read()["objects"]
    assert master_pod(after)["containers"][0]["image"].endswith(":" + TARGET)
    assert_legacy_kept(before, after)
    assert_no_cleanup(cluster)


def test_rules_configmap_still_referenced_by_custom_volume_is_kept(tmp_path):
    objects = legacy_objects()
    pod = master_pod(objects)
    pod["volumes"].append({"name": "my-rules", "configMap": {"name": RULES}})
    pod["containers"][0]["volumeMounts"].append({"name": "my-rules", "mountPath": "/etc/my-rules"})
    cluster = ShellCluster(tmp_path, objects)
    result = cluster.run("upgrade", "--yes")
    assert result.returncode == 0, result.stderr
    after = cluster.read()["objects"]
    # 标准的规则卷和挂载去掉了；自定义卷还引用规则 ConfigMap，不能删（删了 master 下次起不来）
    volumes = {volume["name"] for volume in master_pod(after)["volumes"]}
    assert RULES not in volumes and "my-rules" in volumes
    assert "configmap/" + RULES in after
    assert "仍有自定义的卷或挂载引用 ConfigMap" in result.stderr
    assert after["configmap/kubedoor-config"]["data"] == PG_KEYS


def test_preview_shows_cache_migration_and_cleanup_without_mutations(tmp_path):
    cluster = ShellCluster(tmp_path, legacy_objects())
    before = cluster.read()["objects"]
    result = cluster.run("upgrade", "--dry-run", "--tag", "custom-tag", "--image-repo", "mirror.example/team",
                         "--context", "production")
    assert result.returncode == 0, result.stderr
    assert "emptyDir:" in result.stdout and "sizeLimit: 10Gi" in result.stdout
    assert "mirror.example/team/kubedoor-ai:custom-tag" in result.stdout
    # 迁移命令、清理补丁和删除命令都打印出来
    assert ("# kubectl --context production -n kubedoor exec deploy/kubedoor-master -c kubedoor-master -- "
            "python3 -m general_settings migrate") in result.stdout
    patch = result.stdout.split("去掉事件告警规则的卷和挂载", 1)[1]
    assert "- mountPath: /k8s_event/rules\n          $patch: delete" in patch
    assert "- name: kubedoor-master-file-cfg\n        $patch: delete" in patch
    assert "# kubectl --context production -n kubedoor delete configmap kubedoor-master-file-cfg --ignore-not-found" in result.stdout
    cleanup = result.stdout.split("configmap/kubedoor-config merge patch", 1)[1]
    for key in ("PROM_URL", "MSG_TOKEN", "UPDATE_IMAGE", "AGENT_INSTALL_DEFAULTS", "LOG_LEVEL", "OSS_URL"):
        assert f"  {key}: null" in cleanup
    assert not re.search(r"^\s+PG_\w*:", cleanup, re.M)
    assert "master 会再重启一次" in result.stdout
    assert LEGACY_SECRET not in result.stdout + result.stderr
    assert cluster.mutations() == []
    assert not any(call["command"][0] == "exec" for call in cluster.read()["calls"])
    assert cluster.read()["objects"] == before
    assert not (tmp_path / "kubedoor-upgrade-backup.yaml").exists()


def test_upgrade_optional_components_absent_and_completed_web_install(tmp_path):
    objects = legacy_objects(optional=False)
    objects["configmap/kubedoor-setup-state"] = state_object("succeeded")
    cluster = ShellCluster(tmp_path, objects)
    result = cluster.run("upgrade", "--yes")
    assert result.returncode == 0, result.stderr
    assert "跳过未安装组件：kubedoor-ai" in result.stdout
    assert cluster.read()["objects"]["configmap/kubedoor-setup-state"]["data"]["phase"] == "succeeded"


@pytest.mark.parametrize("failure", ["backup", "api-read", "unfinished", "custom-nginx", "validation"])
def test_preflight_failures_never_modify_cluster(tmp_path, failure):
    objects = legacy_objects()
    options = {}
    if failure == "backup":
        (tmp_path / "kubedoor-upgrade-backup.yaml").write_text("keep-original")
    elif failure == "api-read":
        options["get_error"] = "deployment/kubedoor-ai"
    elif failure == "unfinished":
        objects["configmap/kubedoor-setup-state"] = state_object("running")
    elif failure == "custom-nginx":
        objects["configmap/nginx-config"]["data"]["nginx.conf"] = "custom config"
    else:
        options["dry_run_error"] = "deployment/kubedoor-ai"
    cluster = ShellCluster(tmp_path, objects, **options)
    result = cluster.run("upgrade", "--yes")
    assert result.returncode != 0
    assert cluster.mutations() == []
    assert not any(call["command"][0] == "exec" for call in cluster.read()["calls"])
    if failure == "backup":
        assert (tmp_path / "kubedoor-upgrade-backup.yaml").read_text() == "keep-original"


def test_rollout_failure_is_nonzero_and_retry_reloads_web(tmp_path):
    cluster = ShellCluster(tmp_path, legacy_objects(), rollout_error="deployment/kubedoor-ai")
    before = cluster.read()["objects"]
    result = cluster.run("upgrade", "--yes")
    assert result.returncode != 0 and "升级未就绪" in result.stderr
    assert (tmp_path / "kubedoor-upgrade-backup.yaml").exists()
    assert cluster.read()["objects"]["deployment/kubedoor-web"]["spec"]["template"]["spec"]["containers"][0]["image"].endswith(":2.3.0")
    # 升级中途失败：master 已迁移，但没等到全部组件就绪，不清理
    assert_legacy_kept(before, cluster.read()["objects"])
    saved = cluster.read()
    saved.pop("rollout_error")
    cluster.write(saved)
    retry = cluster.run("upgrade", "--yes", "--backup", "retry.yaml")
    assert retry.returncode == 0, retry.stderr
    nginx = cluster.read()["objects"]["configmap/nginx-config"]["data"]["nginx.conf"]
    assert nginx.count("location ^~ /api/admin/agent-installations") == 1
    assert any(call["command"][:3] == ["rollout", "restart", "deployment/kubedoor-web"] for call in cluster.read()["calls"])
    assert "configmap/" + RULES not in cluster.read()["objects"]


@pytest.mark.parametrize("args", [("upgrade", "--render"), ("upgrade", "--export", "x"), ("upgrade", "--nodeport", "30080"), ("--dry-run",)])
def test_upgrade_rejects_mixed_install_flags(tmp_path, args):
    cluster = ShellCluster(tmp_path)
    result = cluster.run(*args)
    assert result.returncode != 0
    assert cluster.read()["calls"] == []
