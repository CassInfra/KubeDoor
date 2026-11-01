"""获取运行配置的打码与占位符（kubedoor_ai/runtime_config.py）：哪些值算密码、占位符的格式和确定性、经得住网关的
domain.redact、截断，以及执行数据查询前的解析替换。网关部分见 test_runtime_config_gateway.py。"""
import json

import pytest

from kubedoor_ai import runtime_config as rc
from kubedoor_ai.domain import redact
from kubedoor_ai.runtime import SKILLS_ROOT

DEPLOYMENT = {"namespace": "demo", "kind": "deployment", "name": "order"}

JAVA = {
    "cluster": "cluster-a", "namespace": "demo", "deployment": "deploy-order", "pod": "deploy-order-7d9f8b6c5-x2x9k",
    "container": "order", "image": "registry.example.com/team/order:v1", "started_at": "2026-11-01T08:00:00+08:00",
    "lang": "java", "centers": ["config-server"], "basis": "runtime", "collected_at": "2026-11-01T10:00:00+08:00",
    "cached": True,
    "config": {
        "spring.datasource.url": "jdbc:mysql:replication://db1.example.com:3306,db2.example.com/order?useSSL=false&password=Url-Q-Pw&user=order",
        "spring.datasource.username": "order_rw",
        "spring.datasource.password": "Db#Pa55w0rd!",
        "spring.redis.password": 20261101,
        "spring.elasticsearch.uris": "http://elastic:Es-One@es1.example.com:9200,http://elastic:Es-Two@es2.example.com:9200",
        "jwt.secret": "jwt-signing-secret",
        "aliyun.oss.accessKeyId": "LTAI-ID",
        "aliyun.oss.accessKeySecret": "oss-key-secret",
        "security.oauth2.client.client-secret": "oauth-client-secret",
        # 不是密码：开关、过期时间、请求头名、文件路径、没解析的引用、actuator 没补回的值
        "jwt.token.expire": 3600,
        "token.header": "Authorization",
        "password.enabled": True,
        "app.password-file": "/run/secrets/db",
        "app.secret.timeout": "30s",
        "mq.password": "${MQ_PASSWORD}",
        "cache.password": "******",
        "empty.password": "",
        "server.port": "8080",
    },
    "env": {"DB_PASSWORD": "Env-Db-Pw", "JAVA_OPTS": "-Xmx1g", "PWD": "/app", "SECRET_ACCESS_KEY": "aws-sak",
            "ACCESS_KEY_ID": "AKIA-ID"},
}
JAVA_SECRETS = ("Db#Pa55w0rd!", "Url-Q-Pw", "20261101", "Es-One", "Es-Two", "jwt-signing-secret", "oss-key-secret",
                "oauth-client-secret", "Env-Db-Pw", "aws-sak")


def rows(view, section="config"):
    return dict(view.get(section, []))


def refs(view):
    return {item["ref"] for item in view.get("secret_refs", [])}


def ph(ref, target=DEPLOYMENT):
    return rc.placeholder(target, ref)


# ---- 哪些值算密码 ----

@pytest.mark.parametrize("key", [
    "spring.datasource.password", "spring.redis.password", "DB_PASSWORD", "dbPassword", "db.passwd", "mysql.pwd",
    "REDIS_PASS", "jwt.secret", "jwtSecret", "app.token", "accessToken", "refresh_token", "credentials",
    "aliyun.oss.accessKeySecret", "accessKey", "access-key", "AWS_SECRET_ACCESS_KEY", "privateKey", "private_key",
    "apiKey", "api-key", "APIKEY", "x.auth-key", "signKey", "encryptKey", "aesKey", "password.salt", "jwt",
    "client-secret", "keystore.password", "servers[0].token", "ConnectionStrings:Redis:Password", "dbpwd",
    "http.headers.authorization",
])
def test_secret_keys(key):
    assert rc.is_secret_key(key), key


@pytest.mark.parametrize("key", [
    "spring.datasource.url", "spring.datasource.username", "server.port", "jwt.token.expire", "token.header",
    "tokenHeader", "password.enabled", "passwordEnabled", "app.password-file", "DB_PASSWORD_FILE", "secret.name",
    "app.secret.timeout", "token-ttl", "password.min-length", "ACCESS_KEY_ID", "accessKeyId", "secretId",
    "cache.key", "publicKey", "tokenizer", "bypass", "PWD", "OLDPWD", "", "a.b.",
])
def test_not_secret_keys(key):
    assert not rc.is_secret_key(key), key


# ---- 打码 ----

def test_java_flat_config_env_and_service():
    view = rc.redact_config(JAVA, DEPLOYMENT)
    config, env = rows(view), rows(view, "env")
    assert view["service"] == {key: JAVA[key] for key in rc.SERVICE_KEYS if key in JAVA}
    # 配置项按 key 排序，用 [key, value] 列表
    assert [key for key, _ in view["config"]] == sorted(JAVA["config"])
    assert config["spring.datasource.password"] == ph("config:spring.datasource.password")
    assert config["spring.redis.password"] == ph("config:spring.redis.password")   # 数字也算
    assert config["jwt.secret"] == ph("config:jwt.secret")
    assert config["aliyun.oss.accessKeySecret"] == ph("config:aliyun.oss.accessKeySecret")
    assert config["spring.datasource.username"] == "order_rw" and config["aliyun.oss.accessKeyId"] == "LTAI-ID"
    for key in ("jwt.token.expire", "token.header", "password.enabled", "app.password-file", "app.secret.timeout",
                "mq.password", "cache.password", "empty.password", "server.port"):
        assert config[key] == JAVA["config"][key], key
    # URL 里的密码和查询参数里的密码换成占位符，其余原样
    assert config["spring.datasource.url"] == (
        "jdbc:mysql:replication://db1.example.com:3306,db2.example.com/order?useSSL=false&password="
        + ph("config:spring.datasource.url#q:password") + "&user=order")
    # 逗号分隔的多个地址：每个 URL 的密码都换，第 2 个起加编号
    assert config["spring.elasticsearch.uris"] == (
        f"http://elastic:{ph('config:spring.elasticsearch.uris#password')}@es1.example.com:9200,"
        f"http://elastic:{ph('config:spring.elasticsearch.uris#password#2')}@es2.example.com:9200")
    assert env["DB_PASSWORD"] == ph("env:DB_PASSWORD") and env["SECRET_ACCESS_KEY"] == ph("env:SECRET_ACCESS_KEY")
    assert env["PWD"] == "/app" and env["ACCESS_KEY_ID"] == "AKIA-ID" and env["JAVA_OPTS"] == "-Xmx1g"
    dumped = json.dumps(view, ensure_ascii=False)
    for secret in JAVA_SECRETS:
        assert secret not in dumped, secret
    assert view["truncated"] is False and "占位符" in view["hint"]
    assert "detail" not in view and "config_files" not in view and "other_config" not in view


def test_secret_refs_list_every_placeholder_with_where():
    view = rc.redact_config(JAVA, DEPLOYMENT)
    mapping = rc.secret_values(JAVA, DEPLOYMENT)
    assert refs(view) == set(mapping) and len(view["secret_refs"]) == len(mapping) == len(JAVA_SECRETS)
    where = {item["ref"]: item["where"] for item in view["secret_refs"]}
    assert where[ph("config:spring.datasource.password")] == "config spring.datasource.password"
    assert where[ph("config:spring.datasource.url#q:password")] == "config spring.datasource.url 的参数 password"
    assert where[ph("config:spring.elasticsearch.uris#password#2")] == "config spring.elasticsearch.uris 里 URL 的密码（第 2 处）"
    assert where[ph("env:DB_PASSWORD")] == "env DB_PASSWORD"
    assert mapping[ph("config:spring.datasource.password")] == "Db#Pa55w0rd!"
    assert mapping[ph("config:spring.redis.password")] == "20261101"
    assert mapping[ph("config:spring.elasticsearch.uris#password#2")] == "Es-Two"
    assert mapping[ph("config:spring.datasource.url#q:password")] == "Url-Q-Pw"


def test_placeholders_are_deterministic():
    target = {"namespace": "demo", "kind": "pod", "name": "order-7d9f8b6c5-x2x9k", "container": "order"}
    first, second = rc.redact_config(JAVA, target), rc.redact_config(json.loads(json.dumps(JAVA)), dict(target))
    assert first == second
    assert rc.secret_values(JAVA, target) == rc.secret_values(JAVA, target)
    # 只取部分配置项（keys 过滤）时，剩下那些的占位符不变
    partial = {**JAVA, "config": {"spring.datasource.password": "Db#Pa55w0rd!"}, "env": {}}
    assert set(rc.secret_values(partial, target)) == {rc.placeholder(target, "config:spring.datasource.password")}
    assert rc.placeholder(target, "config:x").startswith("<secret:demo/pod/order-7d9f8b6c5-x2x9k/order:config:")


def test_node_nested_config_is_flattened():
    config = {"lang": "node", "config": {
        "mysql": {"host": "db.example.com", "port": 3306, "password": "Node-Pw", "options": {}},
        "servers": [{"name": "a", "token": "Node-Tk"}, {"name": "b", "url": "redis://:Node-Url@r1:6379/0"}],
        "PORT": 22345, "tags": [],
    }}
    view = rc.redact_config(config, DEPLOYMENT)
    flat = rows(view)
    assert list(flat) == sorted(flat)
    assert flat["mysql.host"] == "db.example.com" and flat["mysql.port"] == 3306 and flat["PORT"] == 22345
    assert flat["mysql.password"] == ph("config:mysql.password")
    assert flat["servers[0].token"] == ph("config:servers[0].token")
    assert flat["servers[1].url"] == f"redis://:{ph('config:servers[1].url#password')}@r1:6379/0"
    assert flat["mysql.options"] == {} and flat["tags"] == [] and flat["servers[0].name"] == "a"
    assert rc.secret_values(config, DEPLOYMENT)[ph("config:servers[1].url#password")] == "Node-Url"


def test_python_config_and_other_config():
    config = {"lang": "python", "config": {"APP_ENV": "prod", "DATABASE_URL": "postgresql://app:Py-Pw@pg:5432/app",
                                           "OPENAI_API_KEY": "sk-python", "REDIS_PASSWORD": "Py-Redis"},
              "other_config": {"application": {"db.password": "Apollo-Pw", "db.timeout": 3000}}}
    view = rc.redact_config(config, DEPLOYMENT)
    flat, other = rows(view), rows(view, "other_config")
    assert flat["APP_ENV"] == "prod"
    assert flat["DATABASE_URL"] == f"postgresql://app:{ph('config:DATABASE_URL#password')}@pg:5432/app"
    assert flat["OPENAI_API_KEY"] == ph("config:OPENAI_API_KEY") and flat["REDIS_PASSWORD"] == ph("config:REDIS_PASSWORD")
    assert other == {"application.db.password": ph("other_config:application.db.password"), "application.db.timeout": 3000}


NATIVE_FILE = "\n".join([
    "# 数据库",                                                    # 1
    "db:",                                                         # 2
    '  password: "Yaml Pw"  # 生产',                               # 3
    "  url: mysql://app:File-Url-Pw@db.example.com:3306/app",       # 4
    "redis.pass=Prop-Pw",                                          # 5
    "export API_TOKEN='Shell-Tk'",                                 # 6
    "<password>Xml-Pw</password>",                                 # 7
    '<property name="jdbc.password" value="Attr-Pw"/>',            # 8
    '<add key="Redis.Password" value="Net&amp;Pw"/>',              # 9
    '{"token": "Json-Tk", "secret": "Json-Sec", "port": 80}',       # 10
    'user="app" pwd=Bare-Pw timeout=3',                            # 11
    "ConnectionString=Server=db;Database=app;User Id=sa;Password=Conn-Pw;",  # 12
    "password: ${DB_PASSWORD}",                                    # 13
    "timeout: 30",                                                 # 14
    '<entry key="mail.password">Entry-Pw</entry>',                 # 15
]) + "\n"
NATIVE_SECRETS = {"L3": "Yaml Pw", "L4#password": "File-Url-Pw", "L5": "Prop-Pw", "L6": "Shell-Tk", "L7": "Xml-Pw",
                  "L8": "Attr-Pw", "L9": "Net&Pw", "L10": "Json-Tk", "L10#2": "Json-Sec", "L11": "Bare-Pw",
                  "L12#q:Password": "Conn-Pw", "L15": "Entry-Pw"}


def test_native_config_files_are_masked_line_by_line():
    config = {"lang": "native", "files": [
        {"path": "/etc/nginx/nginx.conf", "from": "镜像", "reader": "nginx", "content": "worker_processes 1;\n"},
        {"path": "/home/app/conf/app.conf", "from": "ConfigMap app-configs/app.conf（subPath 挂载）", "content": NATIVE_FILE},
    ], "env": {"CONFIG": "/home/app/conf/app.conf"}}
    view = rc.redact_config(config, DEPLOYMENT)
    files = view["config_files"]
    assert files[0] == {"path": "/etc/nginx/nginx.conf", "from": "镜像", "content": "worker_processes 1;\n"}
    lines = files[1]["content"].split("\n")
    assert lines[2] == f'  password: "{ph("files:1:L3")}"  # 生产'
    assert lines[3] == f"  url: mysql://app:{ph('files:1:L4#password')}@db.example.com:3306/app"
    assert lines[6] == f"<password>{ph('files:1:L7')}</password>"
    assert lines[9] == f'{{"token": "{ph("files:1:L10")}", "secret": "{ph("files:1:L10#2")}", "port": 80}}'
    assert lines[10] == f'user="app" pwd={ph("files:1:L11")} timeout=3'
    assert lines[11] == f"ConnectionString=Server=db;Database=app;User Id=sa;Password={ph('files:1:L12#q:Password')};"
    for unchanged in (0, 1, 12, 13):
        assert lines[unchanged] == NATIVE_FILE.split("\n")[unchanged]
    mapping = rc.secret_values(config, DEPLOYMENT)
    assert mapping == {ph(f"files:1:{ref}"): secret for ref, secret in NATIVE_SECRETS.items()}
    assert refs(view) == set(mapping)
    assert {item["where"] for item in view["secret_refs"] if item["ref"] == ph("files:1:L10#2")} == {
        "config_files[1] /home/app/conf/app.conf 第 10 行（第 2 处）"}
    dumped = json.dumps(view, ensure_ascii=False)
    for secret in NATIVE_SECRETS.values():
        assert secret not in dumped, secret


def test_detail_is_kept_and_urls_in_it_are_masked():
    config = {**JAVA, "sources": [{"name": "config-server http://cfg:Cfg-Pw@config.example.com/conf (document #0)",
                                   "keys": ["spring.datasource.password"]}],
              "filled": {"spring.datasource.password": "从 config-server 现在的内容补全", "DB_PASSWORD": "环境变量"},
              "unresolved": [], "notes": ["说明"], "pod_state": "运行中", "seconds": 0.9, "kubectl_calls": 17}
    view = rc.redact_config(config, DEPLOYMENT)
    assert view["detail"] == {
        "sources": [{"name": "config-server http://cfg:***@config.example.com/conf (document #0)", "keys": ["spring.datasource.password"]}],
        "filled": [["spring.datasource.password", "从 config-server 现在的内容补全"], ["DB_PASSWORD", "环境变量"]],
        "unresolved": [], "notes": ["说明"]}
    assert "pod_state" not in json.dumps(view)


def test_placeholders_survive_gateway_redaction():
    """dispatch 会对结果再跑一遍 domain.redact：每个占位符在 secret_refs 里都完整，单独作值的也完整"""
    target = {"namespace": "demo", "kind": "deployment", "name": "auth-token", "container": "api-key"}
    config = {**JAVA, "config": {**JAVA["config"], "Jwt:Token:Secret": "Net-Jwt", "app.authorization": "Basic Zm9v"},
              "files": [{"path": "/app/.env", "from": "镜像", "content": NATIVE_FILE}]}
    view = rc.redact_config(config, target)
    cleaned = redact(view)
    # 名称以 token / api-key 结尾时最后一个字母编码，否则「token:…」会被当成口令打掉
    assert "/auth-toke%6E/api-ke%79:" in view["secret_refs"][0]["ref"]
    assert [item["ref"] for item in cleaned["secret_refs"]] == [item["ref"] for item in view["secret_refs"]]
    mapping = rc.secret_values(config, target)
    assert set(mapping) == refs(view)
    for text in mapping:
        assert rc.parse_placeholder(text)[0] == target
    # 单独作值的占位符也完整（键里的「Token:Secret」会被打掉，值不受影响）
    for key, value in view["config"]:
        if isinstance(value, str) and value.startswith("<secret:"):
            assert value in [item[1] for item in cleaned["config"]], key
    dumped = json.dumps(cleaned, ensure_ascii=False)
    for secret in (*JAVA_SECRETS, *NATIVE_SECRETS.values(), "Net-Jwt", "Zm9v"):
        assert secret not in dumped, secret
    # 模型收到的是 json.dumps 后的字符串，SecretRedactionMiddleware 还会对这个字符串再脱敏一次（配置文件里
    # password: "…" 这种行转义后会被打乱，JSON 不一定还能解析），secret_refs 里的占位符照样完整
    again = redact(json.dumps(cleaned, ensure_ascii=False))
    assert all(item["ref"] in again for item in cleaned["secret_refs"])
    assert all(secret not in again for secret in (*JAVA_SECRETS, *NATIVE_SECRETS.values()))


def test_skill_text_survives_redaction():
    """模型用 read_file 读 Skill，结果会经过脱敏：示例里不能有「password: …」这种会被打掉的写法"""
    text = (SKILLS_ROOT / "kubedoor-config" / "SKILL.md").read_text(encoding="utf-8")
    assert redact(text) == text


# ---- 截断 ----

def test_large_config_is_truncated_by_key_order():
    config = {"lang": "java", "config": {f"app.item{index:05d}": "x" * 100 for index in range(2000)}}
    config["config"]["aaa.password"] = "First-Pw"
    config["config"]["zzz.password"] = "Last-Pw"
    view = rc.redact_config(config, DEPLOYMENT)
    size = len(json.dumps(view, ensure_ascii=False))
    assert view["truncated"] is True and size <= rc.LIMIT
    assert "keys" in view["hint"] and "占位符" in view["hint"]
    keys = [key for key, _ in view["config"]]
    assert keys == sorted(config["config"])[:len(keys)] and 100 < len(keys) < 2000
    # secret_refs 只列保留下来的
    assert refs(view) == {ph("config:aaa.password")}
    assert set(rc.secret_values(config, DEPLOYMENT)) == {ph("config:aaa.password"), ph("config:zzz.password")}


def test_large_files_are_cut_each_and_long_values_shortened():
    content = "".join(f"line{index}=value\n" for index in range(20000)) + "password=Tail-Pw\n"
    config = {"lang": "native", "config": {"a.value": "v" * 5000, "b.password": "B-Pw"},
              "files": [{"path": "/a.conf", "from": "镜像", "content": content},
                        {"path": "/b.conf", "from": "镜像", "content": "password=Head-Pw\n" + content}]}
    view = rc.redact_config(config, DEPLOYMENT)
    assert view["truncated"] is True and len(json.dumps(view, ensure_ascii=False)) <= rc.LIMIT
    first, second = view["config_files"]
    assert "已截断：共 20001 行" in first["content"] and "已截断：共 20002 行" in second["content"]
    assert second["content"].startswith(f"password={ph('files:1:L1')}\n")
    flat = rows(view)
    assert flat["a.value"].startswith("v" * rc.VALUE_LIMIT) and "截断，共 5000 字符" in flat["a.value"]
    assert flat["b.password"] == ph("config:b.password")
    assert refs(view) == {ph("config:b.password"), ph("files:1:L1")}
    dumped = json.dumps(view)
    assert "Tail-Pw" not in dumped and "Head-Pw" not in dumped and "B-Pw" not in dumped


# ---- 解析和替换 ----

def test_parse_find_and_substitute():
    pod = {"namespace": "demo", "kind": "pod", "name": "web-1", "container": "app"}
    a, b = ph("config:spring.datasource.password"), rc.placeholder(pod, "env:REDIS_PASSWORD")
    assert rc.parse_placeholder(a) == (DEPLOYMENT, "config:spring.datasource.password")
    assert rc.parse_placeholder(b) == (pod, "env:REDIS_PASSWORD")
    assert rc.parse_placeholder("<secret:demo/statefulset/x:config:a>") is None
    assert rc.parse_placeholder("<secret:demo>") is None
    arguments = {"host": "db", "password": f"pre-{a}-{b}", "nested": [{"x": a}], "n": 1}
    assert rc.find_placeholders(arguments) == [a, b]
    replaced = rc.substitute(arguments, {a: "A-Pw", b: "B-Pw"})
    assert replaced == {"host": "db", "password": "pre-A-Pw-B-Pw", "nested": [{"x": "A-Pw"}], "n": 1}
    assert arguments["password"] == f"pre-{a}-{b}"   # 原参数不变
    # key 里的 : = 编码后照样能解析回来
    odd = ph("config:" + rc._quote("ConnectionStrings:Redis:Password"))
    assert rc.parse_placeholder(odd)[1] == "config:ConnectionStrings%3ARedis%3APassword"
    assert rc.secret_values({"config": {"ConnectionStrings:Redis:Password": "Net-Pw"}}, DEPLOYMENT) == {odd: "Net-Pw"}


def test_misplaced_placeholders():
    secret = ph("config:spring.datasource.password")
    assert rc.misplaced_placeholders("mysql", {"host": "db", "password": secret, "sql": "SELECT 1"}) == []
    assert rc.misplaced_placeholders("mysql", {"password": secret, "sql": f"SELECT LENGTH('{secret}')"}) == ["sql"]
    assert rc.misplaced_placeholders("redis", {"command": ["GET", secret]}) == ["command"]
    assert rc.misplaced_placeholders("elasticsearch", {"api_key": secret, "body": {secret: 1}}) == ["body"]
    assert rc.misplaced_placeholders("elasticsearch", {"api_key": secret, "password": secret}) == []
    # Redis 哨兵自己的密码可以用占位符，主节点名、哨兵用户名不行；sentinel_password 只对 redis 有效
    assert rc.misplaced_placeholders("redis", {"password": secret, "sentinel_password": secret}) == []
    assert rc.misplaced_placeholders("redis", {"sentinel_master": secret, "sentinel_username": secret}) == [
        "sentinel_master", "sentinel_username"]
    assert rc.misplaced_placeholders("mysql", {"sentinel_password": secret}) == ["sentinel_password"]


# ---- 连接地址核对 ----

@pytest.mark.parametrize("operation, arguments, host", [
    ("mysql", {"host": "DB1.Example.com"}, "db1.example.com"),
    ("redis", {"host": " 10.0.0.12 "}, "10.0.0.12"),
    ("redis", {"host": "[FE80::1]"}, "fe80::1"),                                  # IPv6 去掉方括号
    ("elasticsearch", {"url": "https://ES.example.com:9243/prefix"}, "es.example.com"),
    ("elasticsearch", {"url": " http://[2001:DB8::1]:9200 "}, "2001:db8::1"),
    ("mysql", {}, None), ("mysql", {"host": "  "}, None), ("redis", {"host": 1}, None),
    ("elasticsearch", {"url": "http://[::1"}, None), ("elasticsearch", {"url": "/_search"}, None),
    ("elasticsearch", {"host": "es.example.com"}, None),                          # ES 只看 url
])
def test_connection_host(operation, arguments, host):
    assert rc.connection_host(operation, arguments) == host


HOSTS = {"lang": "java",
         "config": {"spring.datasource.url": "jdbc:mysql:replication://db1.example.com:3306,DB2.example.com/order",
                    "spring.redis.sentinel.nodes": "10.0.0.41:26379,10.0.0.42:26379", "server.port": 8080,
                    "app.callback": "回调地址 api.partner.example.org."},
         "other_config": {"application": {"es": {"hosts": ["http://[2001:db8::1]:9200"]}}},
         "env": {"MQ_ADDR": "mq.internal:5672"},
         "files": [{"path": "/app/.env", "from": "镜像", "content": "# 缓存\nCACHE_HOST=10.0.0.12\nLOCAL=localhost\n"}]}


@pytest.mark.parametrize("host", [
    "db1.example.com", "db2.example.com",            # JDBC URL 里的第一个、第二个主机，不区分大小写
    "10.0.0.42",                                     # 哨兵地址，端口不比较
    "api.partner.example.org",                       # 后面是句号再跟行尾：算
    "2001:db8::1",                                   # other_config 里嵌套的列表，带方括号的 IPv6
    "mq.internal",                                   # env
    "10.0.0.12", "localhost",                        # 配置文件内容
])
def test_host_in_config(host):
    assert rc.host_in_config(host, HOSTS)


@pytest.mark.parametrize("host", [
    "example.com", "partner.example.org",            # 只是后缀
    "db1.example", "db1.example.co",                 # 只是前缀（后面是 . 再跟字母，或者字母）
    "10.0.0.1", "0.0.0.12", "10.0.0.4",              # 只是 IP 的一段：10.0.0.1 对 10.0.0.12
    "db8::1", "2001:db8::",                          # 只是 IPv6 的一段
    "evil.example.net", "10.0.0.5", "8080",          # 不在配置里；8080 是数字类型的值，不是字符串
    "",
])
def test_host_not_in_config(host):
    assert not rc.host_in_config(host, HOSTS)


def test_host_in_config_only_looks_at_values_and_file_contents():
    # 配置名、文件路径、service 字段里出现的主机都不算
    config = {"pod": "db.example.com", "config": {"db.example.com": "x"}, "files": [{"path": "/etc/db.example.com", "content": ""}]}
    assert not rc.host_in_config("db.example.com", config)
    assert not rc.host_in_config("db.example.com", None) and not rc.host_in_config(None, HOSTS)
