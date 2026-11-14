"""旧版配置式安装脚本把配置写进数据库：不再渲染进 ConfigMap，master 就绪后经
python3 -m general_settings migrate --env-json - 导入；升级时旧的事件告警规则交给 master 导入后删除。
只加载脚本里的函数，kubectl 换成记录调用的假函数，不连任何集群。"""
import json
import re

import pytest
import yaml

from test_ai_render import ROOT, bash_path, run_bash, setup_config

SCRIPT = ROOT / "tools/legacy-install/install.sh"
RULES = '{"global_ignore_rules": [], "alert_rules": [{"name": "自定义规则", "enabled": true}]}'
PG_KEYS = {"PG_HOST", "PG_PORT", "PG_USER", "PG_PASSWORD", "PG_DATABASE", "PG_POOL_MIN", "PG_POOL_MAX", "PG_CMD_TIMEOUT"}
INSTALLER = (ROOT / "deploy/install.sh").read_text(encoding="utf-8")


def published(name):
    return re.search(rf'^{name}="([^"]+)"', INSTALLER, re.M).group(1)


def payload(fake_bin, config):
    """按配置文件加载变量后 general_settings_payload 的输出，即迁移命令的 stdin"""
    script = '''source <(sed '$d' "$1")
REPO_ROOT="$2"; CONFIG_FILE="$3"
load_config >/dev/null
general_settings_payload
'''
    result = run_bash(fake_bin, script, bash_path(SCRIPT), bash_path(ROOT), bash_path(config))
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


# kc 换成假函数：记下每次调用；读规则 ConfigMap 返回 $RULES，迁移命令返回 $SUMMARY 和退出码 $CODE
MIGRATE = '''source <(sed '$d' "$1")
REPO_ROOT="$2"; CONFIG_FILE="$3"; LOG="$4"; CODE="$5"; SUMMARY="$6"; RULES="$7"
load_config >/dev/null
kc() {
  printf '%s\\n' "$*" >> "$LOG"
  case "$*" in
    *"get configmap kubedoor-master-file-cfg"*) printf '%s' "$RULES" ;;
    *"exec -i"*" -- sh -c "*) cat > "$LOG.rules" ;;
    *"exec -i"*"general_settings migrate --env-json -"*) cat > "$LOG.payload"; printf 'migrate log\\n%s\\n' "$SUMMARY"; return "$CODE" ;;
    *"delete configmap kubedoor-master-file-cfg"*) ;;
    *) return 99 ;;
  esac
}
read_legacy_rules
migrate_general_settings
echo after-migrate
'''
OK_SUMMARY = {"ok": True, "general_settings": {"imported": ["prom_url"], "present": [], "invalid": []},
              "notification": "absent", "image_settings": {"rules": "absent", "registries": "absent"}, "event_rules": "imported"}


def migrate(tmp_path, code=0, summary=OK_SUMMARY, rules=""):
    fake_bin, config = setup_config(tmp_path, True, ("AGENT_AUTH_REQUIRED=false",))
    log = tmp_path / "kc.log"
    log.write_text("", encoding="utf-8")
    result = run_bash(fake_bin, MIGRATE, bash_path(SCRIPT), bash_path(ROOT), bash_path(config), bash_path(log),
                      str(code), json.dumps(summary, ensure_ascii=False), rules)
    calls = log.read_text(encoding="utf-8").splitlines()
    return result, calls, log


def test_payload_carries_old_settings_and_agent_images_but_no_database_connection(tmp_path):
    fake_bin, config = setup_config(tmp_path, True, (
        "PROM_TYPE=Prometheus",
        "METRICS_WRITE_URL='https://writer:p%40ss$&x@vm.example.invalid/api/v1/write'",
        "KUBEDOOR_EXTURL=https://kd.example.invalid",
        "AGENT_AUTH_REQUIRED=false",
        "RETENTION_EVENTS_DAYS=30",
        "OSS_URL=https://ak:sk@oss.example.invalid/bucket",
        "IMAGE_VMAGENT=registry.example.invalid/vmagent:pinned",
        'MSG_TYPE="dingding"', 'MSG_TOKEN="old-legacy-token-0123"', 'DEFAULT_AT="13800000000"',
    ))
    values = payload(fake_bin, config)
    # 值原样交给 master（JSON），不经 shell 再次解释；master 按通用配置的规则校验，等于默认值的不写入
    assert values["PROM_TYPE"] == "Prometheus"
    assert values["PROM_URL"] == "http://metrics.example.invalid"
    assert values["METRICS_WRITE_URL"] == "https://writer:p%40ss$&x@vm.example.invalid/api/v1/write"
    assert values["PROM_K8S_TAG_KEY"] == "k8s"
    assert values["KUBEDOOR_EXTURL"] == "https://kd.example.invalid"
    assert values["AGENT_AUTH_REQUIRED"] == "false" and values["RETENTION_EVENTS_DAYS"] == "30"
    assert values["OSS_URL"] == "https://ak:sk@oss.example.invalid/bucket"
    assert values["ALERT_DEDUP_WINDOW"] == "300" and values["SILENCE_CACHE_TTL"] == "15"
    # 旧通知配置交给消息通知的全局配置（控制台还没有配置时才导入）
    assert (values["MSG_TYPE"], values["MSG_TOKEN"], values["DEFAULT_AT"]) == ("dingding", "old-legacy-token-0123", "13800000000")
    # 网页安装 Agent 的镜像仓库和采集组件镜像按旧格式放进 AGENT_INSTALL_DEFAULTS
    assert json.loads(values["AGENT_INSTALL_DEFAULTS"]) == {
        "image_repo": "example.invalid/kubedoor", "image_vmagent": "registry.example.invalid/vmagent:pinned",
        "image_kube_state_metrics": published("IMAGE_KUBE_STATE_METRICS"), "image_node_exporter": published("IMAGE_NODE_EXPORTER")}
    # 空值不带；数据库连接只在 ConfigMap / Secret 里
    assert "ALERTMANAGER_EXTURL" not in values and "NODE_SHELL_IMAGE" not in values
    assert not any(key.startswith("PG_") for key in values)


def test_fresh_install_writes_settings_once_and_keeps_going(tmp_path):
    result, calls, log = migrate(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "after-migrate" in result.stdout and "配置已写入数据库" in result.stdout
    assert calls[0].endswith("get configmap kubedoor-master-file-cfg --ignore-not-found -o jsonpath={.data.alert_rules\\.json}")
    assert calls[1:] == ["-n ai-render-test exec -i deploy/kubedoor-master -c kubedoor-master -- "
                         "python3 -m general_settings migrate --env-json -"]
    sent = json.loads((tmp_path / "kc.log.payload").read_text(encoding="utf-8"))
    assert sent["AGENT_AUTH_REQUIRED"] == "false" and sent["PROM_URL"] == "http://metrics.example.invalid"
    assert not (tmp_path / "kc.log.rules").exists()


def test_upgrade_hands_old_rules_to_master_then_deletes_configmap(tmp_path):
    result, calls, _ = migrate(tmp_path, rules=RULES)
    assert result.returncode == 0, result.stderr
    # 先把旧规则放回容器里原来的路径，再执行迁移（同一个命令导入规则），成功后删掉旧 ConfigMap
    assert calls[1] == ("-n ai-render-test exec -i deploy/kubedoor-master -c kubedoor-master -- "
                        "sh -c mkdir -p /k8s_event/rules && cat > /k8s_event/rules/alert_rules.json")
    assert "general_settings migrate" in calls[2]
    assert calls[3] == "-n ai-render-test delete configmap kubedoor-master-file-cfg --ignore-not-found"
    assert (tmp_path / "kc.log.rules").read_text(encoding="utf-8") == RULES
    assert "已删除 ConfigMap kubedoor-master-file-cfg" in result.stdout


def test_settings_that_cannot_be_imported_only_warn_and_keep_old_rules(tmp_path):
    summary = {"ok": False, "general_settings": {"imported": [], "present": [], "invalid": ["prom_url", "oss_url"]},
               "notification": "invalid", "image_settings": {"rules": "absent", "registries": "absent"}, "event_rules": "invalid"}
    result, calls, _ = migrate(tmp_path, code=3, summary=summary, rules=RULES)
    assert result.returncode == 0, result.stderr
    assert "after-migrate" in result.stdout
    assert "通用配置 prom_url、oss_url；消息通知(MSG_TYPE / MSG_TOKEN / DEFAULT_AT)；事件告警规则" in result.stderr
    assert not any("delete configmap" in call for call in calls)


@pytest.mark.parametrize("rules", ["", RULES])
def test_failed_migration_stops_and_keeps_old_rules(tmp_path, rules):
    result, calls, _ = migrate(tmp_path, code=1, summary={"ok": False, "error": "数据库连接失败"}, rules=rules)
    assert result.returncode != 0
    assert "配置写入数据库失败(退出码 1)" in result.stderr and "pods/exec" in result.stderr
    assert "after-migrate" not in result.stdout
    assert not any("delete configmap" in call for call in calls)
    assert "old-legacy-token" not in result.stdout + result.stderr


def test_render_keeps_only_database_connection_and_no_rules_configmap(tmp_path):
    fake_bin, config = setup_config(tmp_path, True, ("ENABLE_GRAFANA=true", "KUBEDOOR_EXTURL=https://kd.example.invalid"))
    output = tmp_path / "rendered"
    result = run_bash(fake_bin, 'exec bash "$@"', bash_path(SCRIPT), "render", "master", "-c", bash_path(config), "-o", bash_path(output))
    assert result.returncode == 0, result.stderr
    assert not (output / "alert_rules.json").exists()
    docs = {(doc["kind"], doc["metadata"]["name"]): doc for path in output.glob("*.yaml")
            for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")) if doc}
    assert set(docs["ConfigMap", "kubedoor-config"]["data"]) == PG_KEYS
    assert ("ConfigMap", "kubedoor-master-file-cfg") not in docs
    master = docs["Deployment", "kubedoor-master"]["spec"]["template"]["spec"]
    assert "volumes" not in master and "volumeMounts" not in master["containers"][0]
    # master 和 alarm、ai 一样只在数据库连接变化时重启
    hashes = {name: docs["Deployment", name]["spec"]["template"]["metadata"]["annotations"]["checksum/config"]
              for name in ("kubedoor-master", "kubedoor-alarm", "kubedoor-ai")}
    assert len(set(hashes.values())) == 1
    ai = docs["Deployment", "kubedoor-ai"]["spec"]["template"]["spec"]["containers"][0]
    assert all(item["name"] != "KUBEDOOR_EXTURL" for item in ai["env"])


def test_install_reads_old_rules_before_deploying_and_migrates_after_master_is_ready():
    body = SCRIPT.read_text(encoding="utf-8").split("\ninstall_master() {", 1)[1].split("\n}\n", 1)[0]
    assert body.index("read_legacy_rules") < body.index("master_manifests | apply_manifests")
    assert (body.index("wait_rollout deploy/kubedoor-master") < body.index("complete_image_settings_migration")
            < body.index("migrate_general_settings") < body.index("wait_rollout deploy/kubedoor-alarm"))
    assert "apply_file_configmap kubedoor-master-file-cfg" not in body
