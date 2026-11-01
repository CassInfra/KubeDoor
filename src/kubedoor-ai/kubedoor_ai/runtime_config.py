"""获取运行配置（runtime_config）：给模型看的打码视图和密码占位符；执行数据查询前把占位符换回原文。

master 的 /api/ai/internal/runtime-config 原样转回管理员配置的「获取运行配置接口」的结果（含明文密码）。这里把
密码、密钥换成占位符 <secret:{namespace}/{kind}/{name}[/{container}]:{ref}>（kind 是 deployment 或 pod，namespace、
name、container 就是这次 runtime_config 的参数），同一份配置每次打码得到同样的占位符。查数据库前网关按占位符里
的服务重新取一次全部配置：先核对连接的主机出现在这份配置里（host_in_config，配置里的密码只能连这个服务自己配置
的地址），再重新打码得到 {占位符: 原文}（secret_values），替换参数（Gateway.resolve_secrets）。

ref：
- config:<key>：平铺的配置项；Node.js 的嵌套配置先拍平成 a.b.c / a.b[0].c；
- other_config:<key>、env:<NAME>；
- files:<序号>:L<行号>：配置文件某一行里的密码；
- 值里 URL 的密码（scheme://user:密码@host）在后面加 #password，查询参数里的（password、pwd、token、accesskey 等）
  加 #q:<参数名>；同一个 ref 出现第 2 次起再加 #2、#3…（同一行有多个密码、一个值里有多个 URL）。

给模型的结果还要经过网关的 domain.redact（kubedoor_tools.redact_export），格式必须经得住它：
- dict 的键像密码（DB_PASSWORD 等）或等于 token / password / files 时整个值换成 [redacted]：所以 config、env 用
  [key, value] 列表，files 改名 config_files；
- 字符串里「password / token / api_key / authorization + : 或 =」后面的内容换成 [REDACTED]：占位符里的名称、key 把
  : = 引号、空白等字符百分号编码，名称（后面紧跟分隔符 :）以这些词结尾时把最后一个字母编码，占位符本身不会被
  打掉；值里嵌着的 password=<占位符> 会被打掉，所以 secret_refs 把全部占位符再列一遍。
"""
from __future__ import annotations

import html
import json
import re
from collections import Counter
from urllib.parse import unquote, urlsplit

# 给模型的结果总字符上限（约数）：超过就截断，配置项按 key 排序取前面的，文件内容各自截断
LIMIT = 60_000
# 截断时单个配置值最多保留的字符数
VALUE_LIMIT = 2_000
# 占位符只能填在这些参数里：写进 SQL、Redis 命令、ES 的 path / body 等地方，结果里会带出真实值或它的变形
# （长度、逐个字符比较），网关在建动作前拒绝。sentinel_password 是 Redis 哨兵自己的认证
SECRET_FIELDS = {"mysql": ("password",), "redis": ("password", "sentinel_password"), "elasticsearch": ("password", "api_key")}
SERVICE_KEYS = ("cluster", "namespace", "deployment", "pod", "container", "image", "lang", "centers", "basis", "reason",
                "collected_at", "cached", "started_at")
DETAIL_KEYS = ("sources", "filled", "unresolved", "notes")
SECTIONS = ("config", "other_config", "env")
HINT_SECRETS = ("密码、密钥已换成 <secret:…> 占位符，明文不给模型看。查询 MySQL / Redis / Elasticsearch 时把占位符原样填进 "
                "password、api_key 或 sentinel_password 参数，执行前由服务端换成真实值；host / url 要按这份配置里写的地址原样填。"
                "值里嵌着的占位符被打成 [REDACTED] 时，到 secret_refs 里按 where 找。不要向用户索要或猜测密码。")
HINT_TRUNCATED = "结果超过上限已截断：用 keys（按配置名过滤的正则，不区分大小写）缩小范围，例如 datasource|redis|elasticsearch。"

# ---- 占位符 ----

_UNSAFE = re.compile(r"[^A-Za-z0-9._\-\[\]]+")
# redact_export 把这些词后面跟 : 或 = 的内容打掉（见 kubedoor_tools.config._TEXT_SECRET）
_TEXT_SECRET_TAIL = re.compile(r"(?i)(?<![A-Za-z0-9_])(?:password|token|api[_-]?key|authorization)\Z")
_CANDIDATE = re.compile(r"<secret:[^<>\s]*>")
_PLACEHOLDER = re.compile(r"<secret:([^<>\s/:]+)/(deployment|pod)/([^<>\s/:]+)(?:/([^<>\s/:]+))?:([^<>\s]+)>")


def _quote(text) -> str:
    """占位符里的名称、key：保留字母数字和 . _ - [ ]（拍平的 a.b[0].c），其他字符按 UTF-8 百分号编码"""
    return _UNSAFE.sub(lambda m: "".join(f"%{b:02X}" for b in m.group().encode("utf-8")), str(text))


def _quote_name(text) -> str:
    """名称后面紧跟分隔符 :，以 token / password 这类词结尾时会被当成 token: xxx 打掉，把最后一个字母编码"""
    quoted = _quote(text)
    if _TEXT_SECRET_TAIL.search(quoted):
        quoted = quoted[:-1] + f"%{ord(quoted[-1]):02X}"
    return quoted


def make_target(args: dict) -> dict:
    """runtime_config 的参数 → 占位符里的服务；deployment 和 pod 都给时 Pod 优先（同 code_info）"""
    kind = "pod" if args.get("pod") else "deployment"
    target = {"namespace": args.get("namespace"), "kind": kind, "name": args.get(kind)}
    if args.get("container"):
        target["container"] = args["container"]
    return target


def target_query(target: dict) -> dict:
    """服务 → master /api/ai/internal/runtime-config 的查询参数"""
    query = {"namespace": target["namespace"], target["kind"]: target["name"]}
    if target.get("container"):
        query["container"] = target["container"]
    return query


def placeholder(target: dict, ref: str) -> str:
    parts = [_quote(target["namespace"]), target["kind"], _quote_name(target["name"])]
    if target.get("container"):
        parts.append(_quote_name(target["container"]))
    return f"<secret:{'/'.join(parts)}:{ref}>"


def parse_placeholder(text: str) -> tuple[dict, str] | None:
    """占位符 → (服务, ref)；格式不对返回 None"""
    match = _PLACEHOLDER.fullmatch(text)
    if not match:
        return None
    namespace, kind, name, container, ref = match.groups()
    target = {"namespace": unquote(namespace), "kind": kind, "name": unquote(name)}
    if container:
        target["container"] = unquote(container)
    return target, ref


def find_placeholders(value) -> list[str]:
    """参数里出现的全部占位符（可以嵌在字符串中间，dict 的键也看），按出现顺序去重"""
    found = {}

    def walk(item):
        if isinstance(item, str):
            if "<secret:" in item:
                for match in _CANDIDATE.finditer(item):
                    found.setdefault(match.group(), None)
        elif isinstance(item, dict):
            for key, child in item.items():
                walk(key)
                walk(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                walk(child)

    walk(value)
    return list(found)


def substitute(value, mapping: dict):
    """把字符串值里的占位符换成原文，返回新对象，不改原参数；mapping 里没有的原样保留"""
    if isinstance(value, str):
        return _CANDIDATE.sub(lambda m: mapping.get(m.group(), m.group()), value) if "<secret:" in value else value
    if isinstance(value, dict):
        return {key: substitute(child, mapping) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [substitute(child, mapping) for child in value]
    return value


def misplaced_placeholders(operation: str, arguments: dict) -> list[str]:
    """占位符出现在凭证参数（SECRET_FIELDS）以外的参数名"""
    allowed = SECRET_FIELDS.get(operation, ())
    return [str(key) for key, value in arguments.items() if key not in allowed and find_placeholders([key, value])]


# ---- 连接地址核对：用了占位符的查询，只能连这个服务自己配置的地址 ----
# 模型或配置、日志、查询结果里的提示注入可能把占位符填进 password、把 host 指向别处，真实密码就发出去了（Redis AUTH、
# ES Basic 都是明文）。所以执行前要求 host 作为完整的主机名出现在该服务的运行配置原文里；只核对主机，不核对出现在
# 哪个配置项、端口是多少（配置里别处出现的地址，例如回调地址，也算）

# 前面不能是 [A-Za-z0-9_.-]，后面不能是 [A-Za-z0-9_-]，也不能是「. 再跟字母数字」：example.com 匹配不上
# a.example.com，10.0.0.1 也匹配不上 10.0.0.12；后面是句号、冒号（端口）、斜杠、逗号等都可以
_HOST_BEFORE, _HOST_AFTER = r"(?<![A-Za-z0-9_.\-])", r"(?![A-Za-z0-9_\-]|\.[A-Za-z0-9])"
# IPv6 的 : 也是地址的一部分：前后紧挨着 : 的只是更长地址的一段（[::1]:6379 的方括号不影响）
_HOST6_BEFORE, _HOST6_AFTER = r"(?<![A-Za-z0-9_.\-:])", r"(?![A-Za-z0-9_\-:]|\.[A-Za-z0-9])"


def connection_host(operation: str, arguments: dict) -> str | None:
    """数据查询实际要连的主机（小写，IPv6 去掉方括号）：mysql / redis 是 host（Redis 哨兵模式下是哨兵地址），
    elasticsearch 是 url 的主机名。取法同 kubedoor_tools.data（先去掉首尾空白），核对的就是真正要连的地址；
    取不到返回 None"""
    if operation == "elasticsearch":
        url = arguments.get("url")
        if not isinstance(url, str):
            return None
        try:
            host = urlsplit(url.strip()).hostname   # 已经是小写、去掉了方括号
        except ValueError:
            return None
    else:
        host = arguments.get("host")
        if not isinstance(host, str):
            return None
        host = host.strip()
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]
    host = (host or "").strip().lower()
    return host or None


def _config_texts(config: dict):
    """运行配置原文里可以找主机的字符串：config / other_config / env 的值（嵌套的递归进去）和配置文件的内容"""
    stack = [config.get(section) for section in SECTIONS]
    stack += [entry.get("content") for entry in config.get("files") or [] if isinstance(entry, dict)]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            yield item
        elif isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend(item)


def host_in_config(host, config) -> bool:
    """host 是否作为完整的主机名出现在运行配置原文（config_texts）的某个字符串里；不区分大小写，不比较端口"""
    host = host.strip().lower() if isinstance(host, str) else ""
    if not host or not isinstance(config, dict):
        return False
    before, after = (_HOST6_BEFORE, _HOST6_AFTER) if ":" in host else (_HOST_BEFORE, _HOST_AFTER)
    pattern = re.compile(before + re.escape(host) + after, re.IGNORECASE)
    return any(pattern.search(text) for text in _config_texts(config))


# ---- 哪些值算密码 ----

_SECRET_WORDS = frozenset({"password", "passwd", "pwd", "pass", "passphrase", "secret", "secrets", "token", "tokens",
                           "credential", "credentials", "authorization", "jwt", "salt"})
# 这些词后面紧跟 key 才算：private key、access key、api key、auth key、sign key、encrypt key、aes key……
_KEY_PREFIXES = frozenset({"private", "access", "secret", "api", "auth", "sign", "signing", "encrypt", "encryption",
                           "aes", "hmac"})
# 连写成一个词的：dbpassword、accesstoken、apikey、secretkey……（pass、jwt、salt 太短，只认单独成词的）
_SECRET_SUFFIXES = ("password", "passwd", "pwd", "passphrase", "secret", "token", "credential", "credentials",
                    "apikey", "accesskey", "secretkey", "privatekey", "authkey", "signkey", "encryptkey", "aeskey")
# 最后一个词是这些的不是密码本身：开关、过期时间、长度、类型、地址、请求头名、文件路径、ID……
_NOT_SECRET_LAST = frozenset({
    "enabled", "enable", "disabled", "disable", "required", "optional",
    "expire", "expires", "expired", "expiration", "expiry", "ttl", "timeout", "interval", "period", "duration", "delay",
    "age", "seconds", "minutes", "hours", "days", "ms", "millis",
    "length", "len", "size", "count", "num", "max", "min", "limit", "retries", "retry", "times",
    "type", "types", "format", "algorithm", "algorithms", "alg", "mode", "scheme", "kind", "version", "charset",
    "encoding", "encoder", "class", "provider", "policy", "strategy", "level", "pattern", "regex", "rule", "rules",
    "url", "uri", "urls", "uris", "endpoint", "endpoints", "host", "hosts", "port", "address", "addr",
    "header", "headers", "name", "names", "path", "paths", "file", "files", "dir", "directory", "location",
    "prefix", "suffix", "param", "parameter", "field", "column", "cookie", "id", "ids", "issuer", "audience",
    "scope", "scopes", "store", "cache", "refresh", "valid", "validity", "check", "verify", "hint", "label",
    "description", "desc", "comment", "user", "username", "owner",
})
# 整个名字就是这些的不算：shell 的当前目录（环境变量，大写；小写的 pwd 是密码）
_NOT_SECRET_NAMES = frozenset({"PWD", "OLDPWD"})
_SEGMENTS = re.compile(r"[.:]")
_INDEX = re.compile(r"\[\d*\]")
_SEPARATORS = re.compile(r"[_\-\s]+")
_CAMEL = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")


def _words(key: str) -> list[str]:
    """key 的最后一段（按 . : 拆，去掉数组下标）再按 _ - 空白和驼峰拆成小写的词"""
    segments = [item for item in _SEGMENTS.split(_INDEX.sub("", key)) if item.strip()]
    if not segments:
        return []
    return [word.lower() for part in _SEPARATORS.split(segments[-1]) for word in _CAMEL.findall(part)]


def is_secret_key(key) -> bool:
    """配置名像密码、密钥：password / secret / token / access key / private key 一类，enabled / expire / url 等结尾的不算"""
    if not isinstance(key, str) or key.strip() in _NOT_SECRET_NAMES:
        return False
    words = _words(key)
    if not words or words[-1] in _NOT_SECRET_LAST:
        return False
    if any(word in _SECRET_WORDS for word in words):
        return True
    if any(first in _KEY_PREFIXES and second == "key" for first, second in zip(words, words[1:])):
        return True
    return any(word.endswith(_SECRET_SUFFIXES) for word in words)


def _secret_text(value) -> str | None:
    """能当密码用的值：非空字符串或数字；${...} 这种没解析的引用、actuator 打码没补回的 ****** 不算"""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    text = value if isinstance(value, str) else str(value)
    stripped = text.strip()
    if not stripped or set(stripped) <= {"*"} or (stripped.startswith("${") and stripped.endswith("}")):
        return None
    return text


# ---- 值里的 URL 密码、查询参数，配置文件的行 ----

# authority 到 / 或空白为止，逗号分隔的多个地址（a://u:p@h1,b://u:p@h2）在下一个 scheme:// 前停下
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://(?P<authority>(?:(?![A-Za-z][A-Za-z0-9+.\-]*://)[^\s/\"'<>`\\]){0,1000})")
_QUERY = re.compile(r"(?:^|[?&;])\s*(?P<name>[A-Za-z_][\w.\-]{0,200})=(?P<value>[^&;#\s\"'<>`]*)", re.M)
# 配置文件里可能有密码的行（先粗筛，大文件逐行跑全部规则太慢）
_HINT = re.compile(r"(?i)pass|pwd|secret|token|credential|key|jwt|salt|://")
# 整行一个键值对：key: value、key = value、export KEY=value、- key: value、"key": value
_ASSIGN = re.compile(r"""^\s*(?:-\s+)?(?:export\s+)?(?:"(?P<dkey>[^"\n]{1,200})"|'(?P<skey>[^'\n]{1,200})'|"""
                     r"""(?P<key>[A-Za-z_][\w.\-\[\]]{0,200}))\s*[:=](?P<rest>.*)$""")
_INLINE = (
    # "key": "value"、"key": 123
    re.compile(r'"(?P<key>[^"\\\n]{1,200})"\s*:\s*(?:"(?P<dq>(?:[^"\\\n]|\\.)*)"|(?P<num>-?\d+(?:\.\d+)?)(?![\w.]))'),
    # <entry key="jdbc.password">value</entry>（Java Properties 的 XML 格式）
    re.compile(r"""<(?P<tag>[A-Za-z_][\w.:\-]{0,100})\s+(?:name|key)\s*=\s*["'](?P<key>[^"'\n]{1,200})["'][^<>]*>"""
               r"""(?P<text>[^<>\n]*)</(?P=tag)\s*>"""),
    # <key>value</key>、<key><![CDATA[value]]></key>
    re.compile(r"<(?P<key>[A-Za-z_][\w.:\-]{0,200})(?:\s[^<>]*)?>(?:<!\[CDATA\[(?P<cdata>.*?)\]\]>|(?P<text>[^<>\n]*))"
               r"</(?P=key)\s*>"),
    # name="key" value="value"（Spring XML 的 property、.NET 的 add key）
    re.compile(r"""(?<![\w.\-])(?:name|key)\s*=\s*["'](?P<key>[^"'\n]{1,200})["']\s+value\s*=\s*"""
               r"""(?:"(?P<attr>[^"\n]*)"|'(?P<sattr>[^'\n]*)')"""),
    # key="value"、key='value'、key: "value"
    re.compile(r"""(?<![\w.\-])(?P<key>[A-Za-z_][\w.\-]{0,200})\s*[=:]\s*(?:"(?P<dq>(?:[^"\\\n]|\\.)*)"|'(?P<sq>[^'\n]*)')"""),
)
# 行内不带引号的 key=value（分号、& 连接的连接串等）
_BARE = re.compile(r"""(?<![\w.\-])(?P<key>[A-Za-z_][\w.\-]{0,200})\s*=\s*(?P<value>[^\s"'<>`,;&]+)""")
_COMMENT = re.compile(r"\s#")


def _json_string(raw: str) -> str:
    try:
        return json.loads(f'"{raw}"')
    except ValueError:
        return raw


# 行内规则各个值分组的解码：JSON / YAML 双引号字符串按 JSON 转义，XML 按实体
_DECODERS = (("dq", _json_string), ("sq", None), ("num", None), ("cdata", None), ("text", html.unescape),
             ("attr", html.unescape), ("sattr", html.unescape))


def _overlaps(span, spans) -> bool:
    return any(span[0] < item[1] and item[0] < span[1] for item in spans)


def _merge(spans, taken=()) -> list:
    """按位置排序，去掉和 taken 或前面的重叠的"""
    kept = []
    for span in sorted(spans):
        if not _overlaps(span, taken) and not _overlaps(span, kept):
            kept.append(span)
    return kept


def _url_spans(text: str, taken=()) -> list:
    """URL 里的密码（scheme://user:密码@host，按 authority 里最后一个 @ 分）和查询参数里的密码
    → [(start, end, 原文, ref 后缀, 说明)]"""
    spans = []
    if "://" in text:
        for match in _URL.finditer(text):
            authority, offset = match.group("authority"), match.start("authority")
            at = authority.rfind("@")
            colon = authority.find(":", 0, at) if at > 0 else -1
            if colon >= 0 and _secret_text(authority[colon + 1:at]) is not None:
                spans.append((offset + colon + 1, offset + at, authority[colon + 1:at], "#password", " 里 URL 的密码"))
    if "=" in text:
        for match in _QUERY.finditer(text):
            name, value = match.group("name"), match.group("value")
            if is_secret_key(name) and _secret_text(value) is not None:
                spans.append((*match.span("value"), value, "#q:" + _quote(name), f" 的参数 {name}"))
    return _merge(spans, taken)


def _value_span(start: int, raw: str, decoded: str):
    return (start, start + len(raw), decoded, "", "") if _secret_text(decoded) is not None else None


def _assign_value(match):
    """整行键值对的值：去掉引号（引号里的按转义解码）、YAML 的行内注释；YAML 块、流式结构、锚点不算"""
    rest = match.group("rest")
    start = match.start("rest") + len(rest) - len(rest.lstrip())
    body = rest.strip()
    if not body or body[0] in "|>{[&*!":
        return None
    if body[0] in "\"'":
        quoted = re.match(r'"((?:[^"\\]|\\.)*)"' if body[0] == '"' else r"'((?:[^']|'')*)'", body)
        if quoted:
            raw = quoted.group(1)
            return _value_span(start + 1, raw, _json_string(raw) if body[0] == '"' else raw.replace("''", "'"))
    comment = _COMMENT.search(body)
    body = (body[:comment.start()] if comment else body).rstrip()
    return _value_span(start, body, body)


def _inline_value(match):
    groups = match.groupdict()
    for name, decode in _DECODERS:
        if groups.get(name) is not None:
            raw = groups[name]
            return _value_span(match.start(name), raw, decode(raw) if decode else raw)
    return None


def _line_spans(line: str) -> list:
    """配置文件的一行：key: value、key = value、"key": "value"、<key>value</key>、key="value"、name="key" value="…"
    里配置名像密码的值，以及 URL 里的密码、查询参数里的密码 → 按位置排好序、互不重叠的 spans"""
    assign = _ASSIGN.match(line)
    if assign and is_secret_key(assign.group("dkey") or assign.group("skey") or assign.group("key")):
        value = _assign_value(assign)
        return [value] if value else []
    spans, covered = [], []
    for rule in _INLINE:
        for match in rule.finditer(line):
            if _overlaps(match.span(), covered):
                continue
            covered.append(match.span())
            if is_secret_key(match.group("key")):
                value = _inline_value(match)
                if value:
                    spans.append(value)
    for span in _url_spans(line, spans):
        spans.append(span)
        covered.append(span[:2])
    for match in _BARE.finditer(line):
        if _overlaps(match.span(), covered):
            continue
        covered.append(match.span())
        if is_secret_key(match.group("key")):
            value = _value_span(match.start("value"), match.group("value"), match.group("value"))
            if value:
                spans.append(value)
    return sorted(spans)


def _flatten(value, prefix=""):
    """嵌套配置拍平成 (a.b[0].c, 值)；平铺的配置原样"""
    if isinstance(value, dict):
        if not value and prefix:
            yield prefix, {}
        for key, child in value.items():
            yield from _flatten(child, f"{prefix}.{key}" if prefix else str(key))
    elif isinstance(value, list):
        if not value and prefix:
            yield prefix, []
        for index, child in enumerate(value):
            yield from _flatten(child, f"{prefix}[{index}]")
    elif prefix:
        yield prefix, value


def _mask_urls(value):
    """来源说明、推断原因这类文字里的 URL 密码直接换成 ***（不需要占位符）"""
    if isinstance(value, str):
        spans = _url_spans(value)
        for start, end, *_ in reversed(spans):
            value = value[:start] + "***" + value[end:]
        return value
    if isinstance(value, dict):
        return {key: _mask_urls(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_mask_urls(child) for child in value]
    return value


class _Masker:
    """一次打码：按固定顺序遍历配置，生成占位符、{占位符: 原文}，每个配置项、每一行记下自己的 secret_refs"""

    def __init__(self, target: dict):
        self.target = target
        self.counts = Counter()
        self.mapping = {}

    def secret(self, ref, original, where, refs):
        self.counts[ref] += 1
        count = self.counts[ref]
        if count > 1:
            ref, where = f"{ref}#{count}", f"{where}（第 {count} 处）"
        text = placeholder(self.target, ref)
        self.mapping[text] = original
        refs.append({"ref": text, "where": where})
        return text

    def replace(self, text, spans, base, where, refs):
        """spans 按位置排好序、互不重叠；从前往后生成占位符（编号按出现顺序）"""
        out, last = [], 0
        for start, end, original, suffix, label in spans:
            out += [text[last:start], self.secret(base + suffix, original, where + label, refs)]
            last = end
        out.append(text[last:])
        return "".join(out)

    def item(self, section, key, value):
        """config / other_config / env 的一项 → ([key, 打码后的值], refs)"""
        refs, base, where = [], f"{section}:{_quote(key)}", f"{section} {key}"
        if is_secret_key(key):
            original = _secret_text(value)
            if original is not None:
                value = self.secret(base, original, where, refs)
        elif isinstance(value, str):
            value = self.replace(value, _url_spans(value), base, where, refs)
        return [key, value], refs

    def file(self, index, entry):
        """配置文件：逐行找密码 → {path, from, lines: [(打码后的行, refs)]}；content 不是字符串的原样放"""
        result = {key: _mask_urls(entry[key]) for key in ("path", "from") if key in entry}
        content = entry.get("content")
        if not isinstance(content, str):
            result["lines"] = []
            result["raw"] = content
            return result
        path = entry.get("path") or ""
        lines = []
        for number, line in enumerate(content.splitlines(keepends=True), 1):
            body = line.rstrip("\r\n")
            ending, refs = line[len(body):], []
            if _HINT.search(body):
                spans = _line_spans(body)
                if spans:
                    body = self.replace(body, spans, f"files:{index}:L{number}", f"config_files[{index}] {path} 第 {number} 行", refs)
            lines.append((body + ending, refs))
        result["lines"] = lines
        return result

    def run(self, config: dict):
        service = _mask_urls({key: config[key] for key in SERVICE_KEYS if key in config})
        sections = {}
        for section in SECTIONS:
            value = config.get(section)
            if isinstance(value, (dict, list)) and value:
                sections[section] = [self.item(section, key, item) for key, item in sorted(_flatten(value), key=lambda kv: kv[0])]
        files = config.get("files")
        files = [self.file(index, entry) for index, entry in enumerate(files) if isinstance(entry, dict)] if isinstance(files, list) else []
        detail = {}
        for key in DETAIL_KEYS:
            if key in config:
                value = config[key]
                detail[key] = _mask_urls([[k, v] for k, v in value.items()] if key == "filled" and isinstance(value, dict) else value)
        return service, sections, files, detail


def secret_values(config: dict, target: dict) -> dict[str, str]:
    """{占位符: 原文}：和 redact_config 同一套遍历，同一份配置得到同样的占位符"""
    masker = _Masker(target)
    masker.run(config)
    return masker.mapping


def redact_config(config: dict, target: dict, limit: int = LIMIT) -> dict:
    """接口返回的原样配置 → 给模型看的视图（格式见模块说明）；超过 limit 截断"""
    return _assemble(*_Masker(target).run(config), limit)


# ---- 组装与截断 ----

def _size(value) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str))


def _file_entry(entry) -> dict:
    result = {key: entry[key] for key in ("path", "from") if key in entry}
    result["content"] = "".join(text for text, _ in entry["lines"]) + entry.get("tail", "") if "raw" not in entry else entry["raw"]
    return result


def _build(service, sections, files, detail, truncated) -> dict:
    view, refs = {"service": service}, []
    for name in SECTIONS:
        if sections.get(name):
            view[name] = [item for item, _ in sections[name]]
            refs += [ref for _, item_refs in sections[name] for ref in item_refs]
    if files:
        view["config_files"] = [_file_entry(entry) for entry in files]
        refs += [ref for entry in files for _, line_refs in entry["lines"] for ref in line_refs]
    if detail:
        view["detail"] = detail
    if refs:
        view["secret_refs"] = refs
    view["truncated"] = truncated
    hint = " ".join(text for text, show in ((HINT_SECRETS, refs), (HINT_TRUNCATED, truncated)) if show)
    if hint:
        view["hint"] = hint
    return view


def _kept(refs, text) -> list:
    """截断后还在文字里的占位符"""
    return [ref for ref in refs if ref["ref"] in text]


def _cut_value(item, refs):
    key, value = item
    if isinstance(value, str) and len(value) > VALUE_LIMIT:
        value = value[:VALUE_LIMIT] + f"…[截断，共 {len(value)} 字符]"
        return [key, value], _kept(refs, value)
    return item, refs


def _cut_file(entry, budget: int):
    lines = entry["lines"]
    if "raw" in entry or sum(len(text) for text, _ in lines) <= budget:
        return entry
    kept, used = [], 0
    for text, refs in lines:
        if used + len(text) > budget:
            if not kept:   # 第一行就超（压缩成一行的 JSON 等）：截断这一行
                text = text[:max(budget, 0)]
                kept.append((text, _kept(refs, text)))
            break
        kept.append((text, refs))
        used += len(text)
    return {**entry, "lines": kept, "tail": f"\n…[已截断：共 {len(lines)} 行，只显示前 {len(kept)} 行；配置文件不受 keys 过滤]"}


def _assemble(service, sections, files, detail, limit) -> dict:
    view = _build(service, sections, files, detail, False)
    if _size(view) <= limit:
        return view
    budget = max(limit - _size(_build(service, {}, [], detail, True)) - len(HINT_SECRETS), 0)
    if files:
        # 文件内容各自截断：有配置项时文件最多用一半预算
        share = (budget // 2 if any(sections.values()) else budget) // len(files)
        files = [_cut_file(entry, share) for entry in files]
        budget -= sum(_size(_file_entry(entry)) + sum(_size(ref) + 2 for _, refs in entry["lines"] for ref in refs) for entry in files)
    # 配置项按 key 排序取前面的（config → other_config → env），预算用完就停
    kept, full = {}, False
    for name in SECTIONS:
        kept[name] = []
        for item, refs in sections.get(name) or []:
            item, refs = _cut_value(item, refs)
            cost = _size(item) + 2 + sum(_size(ref) + 2 for ref in refs)
            if full or cost > budget:
                full = True
                break
            budget -= cost
            kept[name].append((item, refs))
    view = _build(service, kept, files, detail, True)
    # 兜底：来源说明太长时只留说明，再不够就逐项去掉配置项
    if _size(view) > limit and detail:
        detail = {key: detail[key] for key in ("unresolved", "notes") if key in detail}
        view = _build(service, kept, files, detail, True)
    for name in reversed(SECTIONS):
        while _size(view) > limit and kept[name]:
            kept[name].pop()
            view = _build(service, kept, files, detail, True)
    return view
