"""登录与权限改由 master 负责后的渲染检查:nginx 不再做认证,只用 auth_request 问 master。"""

import re

import yaml

from test_ai_render import ROOT, bash_path, run_bash, setup_config

# Pod 模板上带 checksum/config 注解的组件
CONFIG_HASHED = ("kubedoor-master", "kubedoor-alarm", "kubedoor-ai", "kubedoor-dash", "kubedoor-web")


def render_master(tmp_path, extra=()):
    tmp_path.mkdir(parents=True, exist_ok=True)
    fake_bin, config = setup_config(tmp_path, True, ("ENABLE_GRAFANA=true", *extra))
    output = tmp_path / "rendered"
    args = [bash_path(ROOT / "deploy/install.sh"), "render", "master", "-c", bash_path(config), "-o", bash_path(output)]
    result = run_bash(fake_bin, 'exec bash "$@"', *args)
    assert result.returncode == 0, result.stderr
    docs = [doc for path in sorted(output.glob("*.yaml"))
            for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")) if doc]
    return {(doc["kind"], doc["metadata"]["name"]): doc for doc in docs}, result


def block(conf, header):
    """取出一个 location 块的内容(这些块里没有嵌套的花括号)。"""
    return conf.split(header + " {", 1)[1].split("}", 1)[0]


def config_hashes(docs):
    return {name: docs[("Deployment", name)]["spec"]["template"]["metadata"]["annotations"]["checksum/config"]
            for name in CONFIG_HASHED}


def test_nginx_delegates_authentication_to_master(tmp_path):
    docs, _ = render_master(tmp_path)
    assert ("Secret", "nginx-auth") not in docs
    data = docs[("ConfigMap", "nginx-config")]["data"]
    # 只有一个完整的 nginx.conf,镜像自带的 conf.d/default.conf 不会被加载
    assert set(data) == {"nginx.conf"}
    conf = data["nginx.conf"]
    for legacy in ("auth_basic", "auth_ldap", "$remote_user", "user_permission", "conf.d"):
        assert legacy not in conf
    # 四种鉴权子请求合成一个内部 location;子请求带回的身份在 server 级统一取出
    auth = block(conf, "location ~ ^/_kd_auth/(session|mcp|grafana|alert)$")
    assert "internal;" in auth and "proxy_pass http://kubedoor_master/api/auth/verify?for=$1;" in auth
    assert "proxy_pass_request_body off;" in auth and 'proxy_set_header X-Kubedoor-Token "";' in auth
    # 只调大 proxy_buffer_size 的话 nginx -t 直接报错(busy_buffers 默认是它的 2 倍,超出 8 个 4k 缓冲的上限)
    assert "proxy_buffer_size 16k;" in auth and "proxy_busy_buffers_size 16k;" in auth
    for name in ("user", "role", "clusters", "grafana_role"):
        assert re.search(rf"auth_request_set \$kd_{name}\s+\$upstream_http_x_kubedoor_{name};", conf)
    ai = block(conf, "location ^~ /api/ai/")
    assert "auth_request /_kd_auth/session;" in ai and "X-User-Clusters $kd_clusters" in ai
    assert "include /etc/nginx/ai-security/gateway.conf;" in ai and 'proxy_set_header Cookie "";' in ai
    assert "return 404;" in block(conf, "location ^~ /api/ai/internal/")
    mcp = block(conf, "location ~ ^/(mcp|sse|messages)(/|$)")
    assert "auth_request /_kd_auth/mcp;" in mcp and 'proxy_set_header Authorization "";' in mcp
    # /api 和 /ws 合成一个 location 直接转给 master;外部请求不能带内部 token 冒充集群内的服务
    master = block(conf, "location ~ ^/(api|ws)(/|$)")
    assert "auth_request" not in master and "proxy_pass http://kubedoor_master;" in master
    assert 'proxy_set_header X-Kubedoor-Token "";' in master and "$connection_upgrade" in master
    # Grafana 只剩一个 location,Grafana Live 的 WebSocket 也走它
    assert conf.count("location /grafana/") == 1
    grafana = block(conf, "location /grafana/")
    assert "auth_request /_kd_auth/grafana;" in grafana and "X-WEBAUTH-ROLE $kd_grafana_role" in grafana
    assert "X-WEBAUTH-USER $kd_user" in grafana and "$connection_upgrade" in grafana
    alert = block(conf, "location = /api/custom_alert")
    assert "auth_request /_kd_auth/alert;" in alert and "proxy_pass http://kubedoor-alarm.ai-render-test:80;" in alert
    # 登录、改密、账号管理的请求体里有明文口令,不能进访问日志
    main_log = conf.split("log_format main", 1)[1].split(";", 1)[0]
    assert "$request_body" not in main_log and "$kd_log_body" in main_log and "$kd_user" in main_log
    assert re.search(r'~\^/api/\(auth\|admin\)/\s+"";', conf)
    web = docs[("Deployment", "kubedoor-web")]["spec"]["template"]["spec"]
    assert all(v["name"] != "nginx-auth" for v in web["volumes"])
    mounts = {m["mountPath"] for m in web["containers"][0]["volumeMounts"]}
    assert mounts == {"/etc/nginx/nginx.conf", "/etc/nginx/ai-security"}


def test_config_changes_roll_only_the_affected_pods(tmp_path):
    base = config_hashes(render_master(tmp_path / "base")[0])
    assert all(re.fullmatch(r"[0-9a-f]{16}", value) for value in base.values())
    # 同样的配置再渲染一次(AI 密钥是另一套随机值)摘要不变:重跑安装脚本不会无谓地重启
    assert config_hashes(render_master(tmp_path / "again")[0]) == base
    env = config_hashes(render_master(tmp_path / "env", ("MSG_TOKEN=changed",))[0])
    assert {name for name in base if env[name] != base[name]} == {"kubedoor-master", "kubedoor-alarm", "kubedoor-ai"}
    nginx = config_hashes(render_master(tmp_path / "nginx", ("ENABLE_MCP=false",))[0])
    assert {name for name in base if nginx[name] != base[name]} == {"kubedoor-web"}


def test_grafana_uses_auth_proxy_instead_of_anonymous_admin(tmp_path):
    docs, _ = render_master(tmp_path)
    ini = next(doc for key, doc in docs.items() if key[0] == "ConfigMap" and "grafana.ini" in doc.get("data", {}))["data"]["grafana.ini"]
    anonymous = ini.split("[auth.anonymous]", 1)[1].split("[", 1)[0]
    assert "enabled = false" in anonymous and "org_role = Admin" not in ini
    proxy = ini.split("[auth.proxy]", 1)[1].split("\n[", 1)[0]
    assert "enabled = true" in proxy and "header_name = X-WEBAUTH-USER" in proxy and "Role:X-WEBAUTH-ROLE" in proxy


def test_master_gets_encryption_key_init_passwords_and_cron_token(tmp_path):
    password = "p@ss$wo&rd\\\"x#1"
    docs, _ = render_master(tmp_path, (f"KUBEDOOR_INIT_ADMIN_PASSWORD='{password}'", "AGENT_AUTH_REQUIRED=false"))
    secret = docs[("Secret", "kubedoor-auth-init")]["stringData"]
    assert secret == {"KUBEDOOR_INIT_ADMIN_PASSWORD": password, "KUBEDOOR_INIT_AGENT_PASSWORD": ""}
    container = docs[("Deployment", "kubedoor-master")]["spec"]["template"]["spec"]["containers"][0]
    assert {"secretRef": {"name": "kubedoor-auth-init"}} in container["envFrom"]
    keys = {item["valueFrom"]["secretKeyRef"]["key"] for item in container["env"] if "valueFrom" in item}
    assert keys == {"AI_INTERNAL_TOKEN", "AI_ENCRYPTION_KEY"}
    assert docs[("ConfigMap", "kubedoor-config")]["data"]["AGENT_AUTH_REQUIRED"] == "false"
    cron = docs[("CronJob", "kubedoor-collect")]["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
    assert 'X-Kubedoor-Token: ${AI_INTERNAL_TOKEN}' in cron["command"][-1]
    assert cron["env"][0]["valueFrom"]["secretKeyRef"] == {"name": "kubedoor-ai-security", "key": "AI_INTERNAL_TOKEN"}


def test_legacy_web_auth_settings_only_warn(tmp_path):
    _, result = render_master(tmp_path, ("WEB_AUTH_USERS='a:b'", "WEB_RW_USERS=a"))
    assert "已废弃" in result.stderr


def test_invalid_agent_auth_switch_is_rejected(tmp_path):
    fake_bin, config = setup_config(tmp_path, True, ("AGENT_AUTH_REQUIRED=maybe",))
    args = [bash_path(ROOT / "deploy/install.sh"), "render", "master", "-c", bash_path(config)]
    result = run_bash(fake_bin, 'exec bash "$@"', *args)
    assert result.returncode != 0 and "AGENT_AUTH_REQUIRED" in result.stderr
