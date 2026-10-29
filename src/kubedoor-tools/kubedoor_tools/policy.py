"""Conservative classification shared by every execution surface.

AI 网关、agent 二次校验和 executor.prepare 共用这里的分类:
- read_only:操作本身是否不改东西(决定去重、结果未知、能否重新生成、是否 prepare);
- ops:涉及「权限与审批」表里的哪些操作类型;
- category:Pod 内命令是查看 / 其他 / 高危(pod_exec、kubectl exec)。
判断不了的一律往严里算:查看命令是严格白名单,高危命令宽匹配,目标识别宁可误报。
"""

from __future__ import annotations

import json
import posixpath
import re
import shlex
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import unquote

import yaml

API_PATH = re.compile(r"^/(?:api(?:/v[0-9]+(?:/.*)?)?|apis(?:/[^/?#]+(?:/[^/?#]+(?:/.*)?)?)?|version|(?:livez|readyz|healthz)(?:/[^?#]*)?|openapi/(?:v2|v3)(?:/[^?#]*)?)$")
DIAGNOSTIC_PROGRAMS = frozenset({
    "jq", "yq", "curl", "dig", "openssl", "rg",
    # 以下程序的参数是白名单,规则和不开放的原因(strace、watch、time、stdbuf、awk、sed、
    # tar/zip、pkill、htop、file、crictl)见 executor._DIAGNOSTIC_OPTIONS 上方的注释
    "nc", "nmap", "tcpdump", "iperf3", "traceroute", "mtr",
    "ps", "top", "pgrep", "pmap", "pwdx", "lsof", "free", "vmstat", "uptime", "w", "slabtop",
    "iostat", "mpstat", "pidstat",
    "xxd", "hexdump", "base64",
})
# 只读、可以直接执行的诊断程序。其余的按修改操作处理(记为 ai.diagnostic):curl、openssl,
# 以及会主动连接或扫描任意地址、产生大流量、能看到报文内容的 nc、nmap、tcpdump、iperf3;
# nc -z 与 agent 的端口测试一样按集群读写处理。
READ_ONLY_DIAGNOSTIC_PROGRAMS = frozenset({
    "jq", "yq", "dig", "rg",
    "traceroute", "mtr",
    "ps", "top", "pgrep", "pmap", "pwdx", "lsof", "free", "vmstat", "uptime", "w", "slabtop",
    "iostat", "mpstat", "pidstat",
    "xxd", "hexdump", "base64",
})
TARGET_FLAGS = frozenset({"--kubeconfig", "--context", "--server", "--token", "--user", "--cluster",
                          "--client-key", "--client-certificate", "--certificate-authority", "--tls-server-name",
                          "--insecure-skip-tls-verify", "--as", "--as-group", "--as-uid", "--cache-dir", "--username", "--password"})

# 「权限与审批」表的全部操作类型(和 master auth/permissions.py 的操作定义一致)
OP_KEYS = frozenset({
    "pod.shell", "node.shell", "pod.exec.view", "pod.exec.other", "pod.exec.danger",
    "workload.restart", "workload.scale", "workload.schedule", "workload.image",
    "pod.delete", "pod.isolate", "pod.jvm",
    "resource.apply", "resource.delete", "resource.secret",
    "ai.k8s_write", "ai.diagnostic",
    "node.cordon", "balance.run", "control.edit",
    "network.coredns", "network.tcp", "network.istio",
    "alert.operate", "alert.silence",
})

# 目标识别:kube-system/coredns ConfigMap 追加 network.coredns,Istio 资源追加 network.istio
BOTH_TARGETS = frozenset({"coredns", "istio"})
TARGET_OPS = (("coredns", "network.coredns"), ("istio", "network.istio"))
ISTIO_API_GROUPS = frozenset({"networking.istio.io", "security.istio.io", "telemetry.istio.io", "extensions.istio.io"})
ISTIO_KINDS = frozenset({
    "VirtualService", "DestinationRule", "Gateway", "ServiceEntry", "Sidecar", "WorkloadEntry", "WorkloadGroup",
    "ProxyConfig", "AuthorizationPolicy", "PeerAuthentication", "RequestAuthentication", "Telemetry", "WasmPlugin",
    "EnvoyFilter",
})
ISTIO_SHORT_NAMES = frozenset({"vs", "dr", "gw", "se", "we", "wg", "ap", "pa", "ra", "wa"})
# kubectl 里能写的资源名:kind 小写、复数、简称、分类名(kubectl get istio-io)
ISTIO_RESOURCE_NAMES = frozenset({kind.lower() for kind in ISTIO_KINDS}) | ISTIO_SHORT_NAMES | frozenset({
    "virtualservices", "destinationrules", "gateways", "serviceentries", "sidecars", "workloadentries",
    "workloadgroups", "proxyconfigs", "authorizationpolicies", "peerauthentications", "requestauthentications",
    "telemetries", "wasmplugins", "envoyfilters",
    "istio-io", "networking-istio-io", "security-istio-io", "telemetry-istio-io", "extensions-istio-io",
})
_CONFIGMAP_NAMES = frozenset({"cm", "configmap", "configmaps"})
_POD_NAMES = frozenset({"po", "pod", "pods"})

# Pod 内查看命令的参数不能指向这些目录(ServiceAccount token 等)
SECRETS_PATHS = ("/var/run/secrets", "/run/secrets")

# 根命令上不带值的布尔参数;其余 -x、--xxx(不含 =)按 cobra 查找子命令的规则都会带走下一个参数
_CLI_BOOL_FLAGS = frozenset({
    "-h", "--help", "--insecure-skip-tls-verify", "--match-server-version", "--warnings-as-errors",
    "--disable-compression", "--add-dir-header", "--alsologtostderr", "--logtostderr", "--one-output",
    "--skip-headers", "--skip-log-headers",
})


def _next_positional(argv: list[str], start: int) -> tuple[str, int]:
    """从 start 起找第一个位置参数,规则同 cobra 查找子命令(stripFlags);遇到 -- 停止。"""
    i = start
    while i < len(argv):
        token = argv[i]
        if token == "--":
            break
        if token.startswith("-"):
            consumes = "=" not in token and token not in _CLI_BOOL_FLAGS and (token.startswith("--") or len(token) == 2)
            i += 2 if consumes else 1
        elif token:
            return token, i
        else:
            i += 1
    return "", -1


def cli_verb(argv: list[str]) -> tuple[str, int]:
    """kubectl / istioctl 的子命令及其位置。

    和 kubectl 自己找子命令的方式一致:`-v 5 delete pod x` 的动作是 delete
    (以前只认识几个带值参数,会把 5 当成动作,既漏掉了按动作的操作类型,也绕过了 executor 的禁用动作)。
    """
    return _next_positional(argv, 0)


_DEFAULT_COMMANDS_FILE = Path(__file__).with_name("default_commands.json")


@lru_cache(maxsize=1)
def _default_command_lists() -> dict[str, tuple[str, ...]]:
    try:
        data = json.loads(_DEFAULT_COMMANDS_FILE.read_text(encoding="utf-8"))
        return {key: tuple(item for item in data[key] if isinstance(item, str)) for key in ("view", "danger")}
    except (OSError, ValueError, KeyError, TypeError):
        # 清单文件缺失或损坏:没有查看命令、所有命令按高危,宁可多审批也不能悄悄放行
        return {"view": (), "danger": ("*",)}


def default_commands() -> dict[str, list[str]]:
    """kubedoor-tools 自带的默认命令清单(master 没有下发清单时使用)。"""
    lists = _default_command_lists()
    return {"view": list(lists["view"]), "danger": list(lists["danger"])}


def _command_lists(commands: dict | None) -> tuple[frozenset[str], tuple[str, ...]]:
    """master 下发的清单 {"view": [...], "danger": [...]};缺少或格式不对的那一项用默认值。"""
    defaults = _default_command_lists()
    lists = []
    for key in ("view", "danger"):
        value = commands.get(key) if isinstance(commands, dict) else None
        if isinstance(value, (list, tuple)):
            lists.append(tuple(item.strip() for item in value if isinstance(item, str) and item.strip()))
        else:
            lists.append(defaults[key])
    return frozenset(lists[0]), lists[1]


# ---------------------------------------------------------------- Pod 内命令:查看命令(严格白名单)

_SHELLS = frozenset({"sh", "bash", "ash"})
_PUNCTUATION = frozenset("();<>|&")
_SEPARATORS = frozenset({"|", "||", "&&", ";", "|&"})
_OUTPUT_REDIRECTS = frozenset({">", ">>", ">|", "&>", "&>>"})
_DUP_REDIRECTS = frozenset({">&", "<&"})
_REDIRECTS = _OUTPUT_REDIRECTS | _DUP_REDIRECTS | {"<"}
# 直接执行时写在一个参数里的重定向:2>/dev/null、>>out、2>&1
_REDIRECT_ARG = re.compile(r"(\d*)(&>>|&>|>>|>&|>\||>|<&|<)(.*)", re.S)
_GLOB_CHARS = frozenset("*?[")
_SECRET_PARENTS = frozenset({"/", "/var", "/run", "/var/run"})


def _base(word: str) -> str:
    return word.rsplit("/", 1)[-1]


def _is_operator(token: str) -> bool:
    return bool(token) and all(c in _PUNCTUATION for c in token)


def _shlex_split(text: str) -> list[str] | None:
    """按 shell 规则拆词(引号、转义;操作符单独成词);引号没闭合等拆不开时返回 None。

    用 posix 模式:引号会去掉、转义会处理,r''m、\\rm 这类写法才能还原成 rm。
    """
    try:
        lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)
    except ValueError:
        return None


def _is_secret_parent(path: str) -> bool:
    """规范化后的绝对路径是 secrets 目录的上级,或者在 /proc 下(/proc/<pid>/root 能绕回根目录)。"""
    path = "/" + path.lstrip("/")
    return path in _SECRET_PARENTS or path == "/proc" or path.startswith("/proc/")


def _touches_secrets(word: str) -> bool:
    """参数是否可能指向 /var/run/secrets、/run/secrets,包括通配符可能展开到的情况。"""
    if "secrets" in word.lower():
        return True
    first = next((i for i, c in enumerate(word) if c in _GLOB_CHARS), -1)
    if first < 0:
        return False
    path = word[word.rfind("=", 0, first) + 1:].lstrip("@")   # --opt=VALUE、@file 只看路径部分
    first = next((i for i, c in enumerate(path) if c in _GLOB_CHARS), len(path))
    if path.startswith("/"):
        return _is_secret_parent(posixpath.normpath(path[:path.rfind("/", 0, first) + 1]))
    # 相对路径:当前目录不确定,通配符在中间一级或者带 .. 时按可能涉及处理
    return "/" in path[first:] or ".." in path


def _dir_may_contain_secrets(path: str) -> bool:
    """递归读取这个目录是否可能读到 secrets:相对路径(当前目录不确定)和通配符都按可能处理。"""
    if "secrets" in path.lower() or not path.startswith("/") or any(c in path for c in _GLOB_CHARS):
        return True
    return _is_secret_parent(posixpath.normpath(path))


def _shell_tokens(script: str) -> list[str] | None:
    """sh -c 的脚本拆成词;有变量、命令替换、反引号、子 shell、大括号、换行就不是查看命令。"""
    if "\n" in script or "\r" in script:
        return None
    quote, escaped = "", False
    for ch in script:
        if escaped:
            escaped = False
        elif quote == "'":
            if ch == "'":
                quote = ""
        elif ch == "\\":
            escaped = True
        elif quote == '"':
            if ch == '"':
                quote = ""
            elif ch in "$`":
                return None
        elif ch in "'\"":
            quote = ch
        elif ch in "$`(){}":
            return None
    if quote or escaped:
        return None
    return _shlex_split(script)


def _direct_tokens(argv: list[str]) -> list[str]:
    """直接执行的 argv:单独的 |、;、> 等按 shell 操作符理解(往严里算),2>/dev/null 这类拆开。"""
    tokens = []
    for element in argv:
        match = None if _is_operator(element) else _REDIRECT_ARG.fullmatch(element)
        if match and match.group(3)[:1] not in _PUNCTUATION:
            tokens.extend(part for part in match.groups() if part)
        else:
            tokens.append(element)
    return tokens


def _view_tokens(argv: list[str]) -> list[str] | None:
    words = argv[1:] if len(argv) > 1 and _base(argv[0]) == "busybox" and _base(argv[1]) in _SHELLS else argv
    if _base(words[0]) in _SHELLS and len(words) >= 3 and words[1] == "-c":
        if any(_touches_secrets(arg) for arg in words[3:]):
            return None
        return _shell_tokens(words[2])
    return _direct_tokens(words)


def _simple_commands(tokens: list[str]) -> list[tuple[list[str], list[tuple[str, str]]]] | None:
    """按 | && || ; 拆成简单命令 (词, 重定向);有后台 &、子 shell、heredoc 等不支持的写法返回 None。"""
    commands, words, redirects = [], [], []
    last_word = -1
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if not _is_operator(token):
            words.append(token)
            last_word = i
            i += 1
        elif token in _SEPARATORS:
            if not words:
                return None
            commands.append((words, redirects))
            words, redirects = [], []
            i += 1
        elif token in _REDIRECTS and i + 1 < len(tokens) and not _is_operator(tokens[i + 1]):
            if last_word == i - 1 and words[-1].isdigit():
                words.pop()   # 2>/dev/null 里的 2 是文件描述符
            redirects.append((token, tokens[i + 1]))
            i += 2
        else:
            return None
    if words:
        commands.append((words, redirects))
    elif redirects or not commands:
        return None
    return commands


def _view_command_ok(words: list[str], redirects: list[tuple[str, str]], view: frozenset[str]) -> bool:
    command, args = words[0], words[1:]
    if _base(command) == "busybox" and args:
        command, args = args[0], args[1:]
    name = _base(command)
    if name not in view or any(_touches_secrets(word) for word in words):
        return False
    for op, target in redirects:
        if op in _OUTPUT_REDIRECTS:
            allowed = target == "/dev/null"
        elif op in _DUP_REDIRECTS:
            allowed = target == "-" or target.isdigit() or (op == ">&" and target == "/dev/null")
        else:
            allowed = not _touches_secrets(target)
        if not allowed:
            return False
    check = _VIEW_ARG_CHECKS.get(name)
    return check is None or check(args)


def _is_view_command(argv: list[str], view: frozenset[str]) -> bool:
    tokens = _view_tokens(argv)
    commands = _simple_commands(tokens) if tokens else None
    return bool(commands) and all(_view_command_ok(words, redirects, view) for words, redirects in commands)


# ---------------------------------------------------------------- 查看命令的内置参数检查
# 参数不满足时这条命令不算查看命令,继续按高危 / 其他判断(不是禁止执行)


def _scan_options(args: list[str], short_value: str = "", long_value: frozenset[str] = frozenset(),
                  long_equals: bool = True) -> tuple[list[tuple[str, str | None]], list[str]]:
    """按 getopt 规则拆参数:返回 [(选项, 值)](不带值为 None)和位置参数。

    短参数可以连写(-qO-、-tnlp),带值的字母取剩余部分或下一个参数;长参数缩写成带值参数的前缀时也取值。
    """
    options, positionals = [], []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            positionals.extend(args[i + 1:])
            break
        if arg.startswith("--"):
            name, equals, value = arg.partition("=") if long_equals else (arg, "", "")
            takes = name in long_value or (len(name) > 3 and any(option.startswith(name) for option in long_value))
            if takes and not equals:
                i += 1
                value = args[i] if i < len(args) else ""
            options.append((name, value if equals or takes else None))
        elif arg.startswith("-") and len(arg) > 1:
            for j in range(1, len(arg)):
                if arg[j] in short_value:
                    value = arg[j + 1:]
                    if not value:
                        i += 1
                        value = args[i] if i < len(args) else ""
                    options.append(("-" + arg[j], value))
                    break
                options.append(("-" + arg[j], None))
        else:
            positionals.append(arg)
        i += 1
    return options, positionals


def _abbreviates(name: str, *options: str, minimum: int = 3) -> bool:
    """GNU 长参数可以缩写:name 是否是其中某个选项的前缀。"""
    return name.startswith("--") and len(name) >= minimum and any(option.startswith(name) for option in options)


_HTTP_URL = re.compile(r"https?://", re.I)
_CURL_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_CURL_SHORT_VALUE = "AbCcDdEeFHKmoPQrTtUuwXxYyz"
_CURL_VALUE_OPTIONS = frozenset({
    "--user-agent", "--continue-at", "--cert", "--cert-type", "--key", "--key-type", "--pass", "--referer",
    "--header", "--proxy-header", "--max-time", "--connect-timeout", "--range", "--proxy", "--proxy-user", "--user",
    "--write-out", "--request-target", "--speed-limit", "--speed-time", "--time-cond", "--url-query", "--resolve",
    "--connect-to", "--cacert", "--capath", "--crlfile", "--pinnedpubkey", "--retry", "--retry-delay",
    "--retry-max-time", "--max-redirs", "--limit-rate", "--max-filesize", "--interface", "--local-port",
    "--dns-servers", "--noproxy", "--oauth2-bearer", "--socks4", "--socks4a", "--socks5", "--socks5-hostname",
    "--preproxy", "--tls-max", "--ciphers", "--tls13-ciphers", "--expect100-timeout", "--keepalive-time",
    "--telnet-option", "--aws-sigv4",
})
# 只能写到标准输出或 /dev/null 的选项
_CURL_SINKS = frozenset({"-o", "--output", "-D", "--dump-header", "--stderr", "--trace", "--trace-ascii"})
_CURL_LONG_VALUE = _CURL_VALUE_OPTIONS | _CURL_SINKS | {"--request", "--cookie", "--url"}
# 不在这里的选项(发数据、上传、写文件、读配置、netrc、改协议、FTP 命令等)都不算查看
_CURL_ALLOWED = _CURL_VALUE_OPTIONS | frozenset({
    "-s", "-S", "-L", "-k", "-f", "-v", "-i", "-I", "-N", "-q", "-0", "-1", "-2", "-3", "-4", "-6", "-G", "-g",
    "-#", "-:", "-j", "-l", "-p", "-R", "-B", "-Z", "-M", "-V", "-h",
    "-A", "-C", "-E", "-e", "-H", "-m", "-r", "-t", "-U", "-u", "-w", "-x", "-Y", "-y", "-z",
    "--silent", "--show-error", "--fail", "--fail-with-body", "--fail-early", "--location", "--location-trusted",
    "--insecure", "--include", "--show-headers", "--head", "--verbose", "--get", "--globoff", "--compressed",
    "--http0.9", "--http1.0", "--http1.1", "--http2", "--http2-prior-knowledge", "--http3", "--http3-only",
    "--ipv4", "--ipv6", "--no-buffer", "--no-keepalive", "--no-sessionid", "--no-progress-meter", "--progress-bar",
    "--tlsv1", "--tlsv1.0", "--tlsv1.1", "--tlsv1.2", "--tlsv1.3", "--ssl", "--ssl-reqd", "--ssl-no-revoke",
    "--disable", "--raw", "--tr-encoding", "--path-as-is", "--junk-session-cookies", "--list-only",
    "--proxytunnel", "--parallel", "--next", "--help", "--version", "--manual", "--digest", "--basic", "--ntlm",
    "--negotiate", "--anyauth", "--cert-status", "--tcp-nodelay", "--suppress-connect-headers", "--proxy-insecure",
    "--retry-connrefused", "--retry-all-errors", "--trace-time",
})


def _curl_ok(args: list[str]) -> bool:
    """只允许 GET / HEAD / OPTIONS;不发数据、不上传、不写文件、不读配置;只访问 http/https。"""
    options, urls = _scan_options(args, _CURL_SHORT_VALUE, _CURL_LONG_VALUE, long_equals=False)
    for name, value in options:
        if name in _CURL_SINKS:
            allowed = value in ("-", "/dev/null")
        elif name in ("-X", "--request"):
            allowed = (value or "").upper() in _CURL_METHODS
        elif name in ("-b", "--cookie"):
            allowed = "=" in (value or "")   # 不带 = 的是 cookie 文件
        elif name == "--url":
            urls.append(value or "")
            allowed = True
        else:
            positive = "--" + name[5:] if name.startswith("--no-") else name
            allowed = name in _CURL_ALLOWED or (positive in _CURL_ALLOWED and positive not in _CURL_LONG_VALUE)
        if not allowed:
            return False
    return all(_HTTP_URL.match(url) for url in urls)


_WGET_LONG_VALUE = frozenset({
    "--output-document", "--timeout", "--tries", "--connect-timeout", "--read-timeout", "--dns-timeout",
    "--max-redirect", "--user-agent", "--header", "--user", "--password", "--http-user", "--http-password",
    "--proxy-user", "--proxy-password", "--proxy", "--referer", "--limit-rate", "--ca-certificate",
    "--ca-directory", "--secure-protocol", "--wait", "--waitretry", "--bind-address", "--progress",
})
_WGET_ALLOWED = (_WGET_LONG_VALUE - {"--output-document"}) | frozenset({
    "-q", "-S", "-v", "-d", "-4", "-6", "-h", "-V", "-T", "-t", "-U", "-Y",
    "--quiet", "--verbose", "--no-verbose", "--server-response", "--debug", "--inet4-only", "--inet6-only",
    "--no-check-certificate", "--no-cache", "--no-cookies", "--no-dns-cache", "--ignore-length", "--help",
    "--version", "--content-on-error", "--no-hsts", "--https-only", "--no-proxy", "--show-progress",
})


def _wget_ok(args: list[str]) -> bool:
    """只能 -O -(输出到标准输出)或 --spider(只检查);其他写文件、发数据、执行 wgetrc 命令的选项都不行。"""
    options, _ = _scan_options(args, "OTtUYn", _WGET_LONG_VALUE)
    to_stdout = spider = False
    for name, value in options:
        if name in ("-O", "--output-document"):
            if value != "-":
                return False
            to_stdout = True
        elif name in ("--spider", "-s"):   # -s 是 busybox wget 的 spider
            spider = True
        elif name == "-n":
            if value != "v":   # 只认 -nv
                return False
        elif name not in _WGET_ALLOWED:
            return False
    return to_stdout or spider


_NC_ALLOWED = frozenset({"-v", "-n", "-u", "-4", "-6", "-d", "-D", "-N", "-w", "-q", "-i", "-W", "-p", "-s", "-T",
                         "--verbose", "--nodns", "--udp", "--wait", "--idle-timeout"})


def _nc_ok(args: list[str]) -> bool:
    """只能 -z(只测端口);-e、-c、-l 等执行程序、监听、写文件的选项都不行。"""
    options, _ = _scan_options(args, "wqiWpsT", frozenset({"--wait", "--idle-timeout"}))
    zero = False
    for name, _value in options:
        if name == "-z":
            zero = True
        elif name not in _NC_ALLOWED:
            return False
    return zero


def _find_ok(args: list[str]) -> bool:
    """不能 -exec、-execdir、-ok、-okdir、-delete、-fprint*、-fls。"""
    return not any(arg.startswith(("-exec", "-ok", "-fprint")) or arg in ("-delete", "-fls") for arg in args)


def _sort_ok(args: list[str]) -> bool:
    """不能 -o / --output(另外 --compress-program 会执行程序、--files0-from 按文件读列表,也不算查看)。"""
    options, _ = _scan_options(args, "ktoST", frozenset({
        "--key", "--field-separator", "--output", "--buffer-size", "--temporary-directory", "--compress-program",
        "--files0-from", "--random-source", "--sort", "--batch-size", "--parallel"}))
    return not any(name == "-o" or _abbreviates(name, "--output", "--compress-program", "--files0-from")
                   for name, _value in options)


def _date_ok(args: list[str]) -> bool:
    """不能 -s / --set;不带 + 的位置参数(date MMDDhhmm)也会设置系统时间。"""
    options, positionals = _scan_options(args, "dfrD", frozenset({"--date", "--file", "--reference", "--rfc-3339"}))
    if any(name == "-s" or _abbreviates(name, "--set") for name, _value in options):
        return False
    return all(arg.startswith("+") for arg in positionals)


_IP_WRITE_WORDS = ("add", "append", "attach", "change", "chg", "del", "delete", "deleteall", "exec", "flush",
                   "prepend", "replace", "restore", "set", "update")


def _ip_ok(args: list[str]) -> bool:
    """不能 add / del / set / change / replace / flush / append / prepend / exec 等修改,也不能 netns 修改、-batch。"""
    positionals = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("-") and len(arg) > 1:
            option = (arg[1:] if arg.startswith("--") else arg).split("=", 1)[0]
            if any(name.startswith(option) for name in ("-batch", "-force")):
                return False   # -batch 从文件执行命令(-b 也可能是它的缩写)
            if "=" not in arg and option not in ("-r", "-f") and any(
                    name.startswith(option) for name in ("-netns", "-loops", "-rcvbuf", "-family")):
                i += 1   # 带值的参数
        else:
            positionals.append(arg)
        i += 1
    if any(word in _IP_WRITE_WORDS for word in positionals):
        return False
    # 动作可以缩写:ip a a = ip addr add,ip l s = ip link set
    return len(positionals) < 2 or not any(word.startswith(positionals[1]) for word in _IP_WRITE_WORDS)


def _hostname_ok(args: list[str]) -> bool:
    """不能带位置参数(会修改主机名),也不能 -F / --file(从文件设置主机名)、-b / --boot。"""
    for arg in args:
        if not arg.startswith("-") or arg == "-":
            return False
        if arg.startswith("--"):
            name = arg.split("=", 1)[0]
            if _abbreviates(name, "--file", minimum=4) or _abbreviates(name, "--boot"):
                return False
        elif "F" in arg or "b" in arg:
            return False
    return True


def _uniq_ok(args: list[str]) -> bool:
    """uniq 的第二个位置参数是输出文件(会被覆盖)。"""
    _, positionals = _scan_options(args, "fsw", frozenset({"--skip-fields", "--skip-chars", "--check-chars"}))
    return len(positionals) <= 1


def _ss_ok(args: list[str]) -> bool:
    """ss -K / --kill 会关闭连接,-D / --diag 会写文件。"""
    for arg in args:
        if arg.startswith("--"):
            if _abbreviates(arg.split("=", 1)[0], "--kill", "--diag"):
                return False
        elif arg.startswith("-") and ("K" in arg or "D" in arg):
            return False
    return True


def _file_ok(args: list[str]) -> bool:
    """file -C / --compile 会写 .mgc 文件。"""
    for arg in args:
        if arg.startswith("--"):
            if _abbreviates(arg.split("=", 1)[0], "--compile", minimum=4):
                return False
        elif arg.startswith("-") and "C" in arg:
            return False
    return True


_GREP_LONG_VALUE = frozenset({
    "--regexp", "--file", "--max-count", "--after-context", "--before-context", "--context", "--label", "--include",
    "--exclude", "--exclude-from", "--exclude-dir", "--binary-files", "--devices", "--directories",
    "--group-separator",
})


def _grep_ok(args: list[str]) -> bool:
    """递归搜索时目录不能是 secrets 的上级(grep -r x / 会读到 ServiceAccount token),也不能是不确定的当前目录。"""
    options, positionals = _scan_options(args, "efmABCDd", _GREP_LONG_VALUE)
    recursive = pattern_given = False
    for name, value in options:
        if name in ("-r", "-R") or _abbreviates(name, "--recursive", "--dereference-recursive", minimum=5):
            recursive = True
        elif name == "-d" or _abbreviates(name, "--directories", minimum=4):
            recursive |= bool(value) and "recurse".startswith(value)
        elif name in ("-e", "-f") or _abbreviates(name, "--regexp", "--file", minimum=4):
            pattern_given = True
    if not recursive:
        return True
    paths = positionals if pattern_given else positionals[1:]
    return bool(paths) and not any(_dir_may_contain_secrets(path) for path in paths)


_VIEW_ARG_CHECKS = {
    "curl": _curl_ok, "wget": _wget_ok, "nc": _nc_ok, "ncat": _nc_ok, "netcat": _nc_ok,
    "find": _find_ok, "sort": _sort_ok, "date": _date_ok, "ip": _ip_ok,
    "mount": lambda args: not args or args == ["-l"],
    "env": lambda args: not args,
    "hostname": _hostname_ok,
    # 计划表之外补充的:这些默认查看命令带某些参数时会写文件、关连接或给 JVM 传参,同样只是不算查看
    "uniq": _uniq_ok, "ss": _ss_ok, "file": _file_ok,
    "jps": lambda args: not any(arg.startswith("-J") for arg in args),
    "jstat": lambda args: not any(arg.startswith("-J") for arg in args),
    "grep": _grep_ok, "egrep": _grep_ok, "fgrep": _grep_ok,
}


# ---------------------------------------------------------------- Pod 内命令:高危命令(宽匹配,宁可误报)

# 后面跟的(可能)是另一个命令:这些词之后的词都按命令位置处理
_WRAPPERS = frozenset({
    "env", "xargs", "nohup", "exec", "command", "builtin", "eval", "sudo", "su", "doas", "nice", "ionice", "timeout",
    "time", "stdbuf", "watch", "chroot", "nsenter", "unshare", "setsid", "taskset", "chrt", "strace", "ltrace",
    "flock", "runuser", "setpriv", "busybox", "sh", "bash", "ash", "dash", "zsh", "ksh", "script", "parallel",
    "-exec", "-execdir", "-ok", "-okdir",
})
_KEYWORDS = frozenset({"!", "{", "}", "if", "then", "else", "elif", "fi", "do", "done", "while", "until", "for",
                       "case", "esac", "in", "select"})
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\+?=")
_MAX_SPLIT_DEPTH = 8
_MAX_WORDS = 20000
# 命令和 stdin 的总长度上限:shlex 是逐字符的纯 Python,太长会长时间占住调用方的事件循环,超过按拆不开处理
_MAX_COMMAND_TEXT = 256 * 1024


def _normalize_script(text: str) -> str:
    """去掉续行(\\ 加换行),换行和反引号当命令分隔符。"""
    text = text.replace("\\\r\n", "").replace("\\\n", "")
    return re.sub(r"[`\r\n]", " ; ", text)


def _command_positions(tokens: list[str]) -> list[bool]:
    """每个词是否可能在命令位置:开头、操作符之后、赋值和关键字之后,以及 env / xargs / sh 等包装命令之后的所有词。"""
    flags = []
    expect = True
    wrapped = redirect_target = False
    for index, token in enumerate(tokens):
        if _is_operator(token):
            if "<" in token or ">" in token:
                redirect_target = True
            else:
                expect, wrapped = True, False
            flags.append(False)
            continue
        if redirect_target:
            redirect_target = False
            flags.append(False)
            continue
        at_command = expect or wrapped
        if expect:
            following = tokens[index + 1] if index + 1 < len(tokens) else ""
            if token.isdigit() and _is_operator(following) and ("<" in following or ">" in following):
                at_command = False   # 2>file 里的 2
            elif not (_ASSIGNMENT.match(token) or token in _KEYWORDS):
                expect = False
        if _base(token) in _WRAPPERS:
            wrapped = True
        flags.append(at_command)
    return flags


def _danger_words(texts: list[str]) -> list[tuple[str, bool]] | None:
    """每段文本按 shell 规则递归拆词(sh -c 的脚本、引号里的命令都拆开),返回 (词, 是否在命令位置)。

    引号没闭合、嵌套太深或词太多,拆不开时返回 None。
    """
    words: list[tuple[str, bool]] = []

    def walk(text: str, depth: int) -> bool:
        tokens = _shlex_split(_normalize_script(text))
        if tokens is None or depth > _MAX_SPLIT_DEPTH:
            return False
        for token, at_command in zip(tokens, _command_positions(tokens)):
            if len(words) >= _MAX_WORDS:
                return False
            if _is_operator(token):
                continue
            if _shlex_split(_normalize_script(token)) == [token]:
                words.append((token, at_command))
            elif not walk(token, depth + 1):
                return False
        return True

    return words if all(walk(text, 0) for text in texts) else None


def _dynamic_word(word: str) -> bool:
    """命令位置上是变量、命令替换、通配符或大括号展开:不知道实际执行什么。"""
    if word in ("[", "[["):
        return False
    return "$" in word or any(c in word for c in _GLOB_CHARS) or ("{" in word and ("," in word or ".." in word))


def _is_danger_command(texts: list[str], danger: tuple[str, ...]) -> bool:
    if sum(len(text) for text in texts) > _MAX_COMMAND_TEXT:
        return True
    words = _danger_words(texts)
    if words is None:
        return True
    names = [_base(word) for word, _ in words]
    present = set(names)
    for item in danger:
        command, _, argument = item.partition(" ")
        command, argument = _base(command), argument.strip()
        if argument:
            # 「命令 参数」:命令词之后出现该参数,如 find -delete
            if command in present:
                start = names.index(command)
                if any(word == argument or word.startswith(argument + "=") for word, _ in words[start + 1:]):
                    return True
        elif command.endswith("*"):
            if any(name.startswith(command[:-1]) for name in present):
                return True
        elif command in present:
            return True
    return any(at_command and _dynamic_word(word) for word, at_command in words)


def classify_pod_command(argv: list[str], commands: dict | None = None, *,
                         stdin: str | None = None) -> Literal["view", "other", "danger"]:
    """Pod 内命令分类:查看 / 其他 / 高危。

    1. 查看(严格白名单,先判断,查看命令一定不是高危):每个命令都在查看清单里,可以直接执行,
       也可以是 sh|bash|ash -c 里用 | && || ; 组合;不能有变量、命令替换、子 shell、后台、heredoc;
       输出重定向只能到 /dev/null 或 fd 复制;参数不能指向 secrets;内置参数检查要通过。
    2. 高危(宽匹配):递归拆词后任一词命中高危清单,命令位置是变量 / 通配符,或者拆不开。
    3. 其他:不是上面两种。

    commands 是 master 下发的清单 {"view": [...], "danger": [...]},不传用自带的默认清单;
    stdin 是 kubectl exec -i 送进容器的内容,只参与高危判断。
    """
    if not isinstance(argv, list) or any(not isinstance(arg, str) for arg in argv):
        return "danger"
    if not argv:
        return "other"
    if sum(len(arg) for arg in argv) > _MAX_COMMAND_TEXT:
        return "danger"
    view, danger = _command_lists(commands)
    if _is_view_command(argv, view):
        return "view"
    texts = [*argv, stdin] if isinstance(stdin, str) and stdin else list(argv)
    return "danger" if _is_danger_command(texts, danger) else "other"


# ---------------------------------------------------------------- 目标识别(CoreDNS / Istio)

_YAML_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
_MAX_MANIFEST_OBJECTS = 5000


def _manifest_objects(content: str) -> list[dict] | None:
    """解析 YAML / JSON 清单:多文档(---)、List 展开;解析失败或有不是对象的文档返回 None。"""
    try:
        documents = list(yaml.load_all(content, Loader=_YAML_LOADER))
    except Exception:
        return None
    objects: list[dict] = []

    def walk(node, depth: int) -> bool:
        if node is None:
            return True
        if depth > 16 or len(objects) > _MAX_MANIFEST_OBJECTS:
            return False
        if isinstance(node, list):
            return all(walk(item, depth + 1) for item in node)
        if not isinstance(node, dict):
            return False
        if isinstance(node.get("items"), list) and str(node.get("kind", "List")).endswith("List"):
            return all(walk(item, depth + 1) for item in node["items"])
        objects.append(node)
        return True

    return objects if all(walk(document, 0) for document in documents) else None


def _object_targets(obj: dict, namespace: str | None) -> set[str]:
    """单个资源对象涉及的目标;缺 kind / apiVersion 判断不了时按两个都涉及。"""
    kind, api_version = obj.get("kind"), obj.get("apiVersion")
    if not isinstance(kind, str) or not isinstance(api_version, str):
        return set(BOTH_TARGETS)
    group = api_version.split("/", 1)[0].lower() if "/" in api_version else ""
    spec = obj.get("spec") if isinstance(obj.get("spec"), dict) else {}
    targets = set()
    if (group in ISTIO_API_GROUPS or group.endswith(".istio.io") or kind in ISTIO_KINDS
            or (kind == "CustomResourceDefinition" and str(spec.get("group", "")).lower().endswith("istio.io"))):
        targets.add("istio")
    if kind == "ConfigMap" and not group:
        metadata = obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {}
        name = metadata.get("name")
        # 名字明确不是 coredns(或者只有 generateName)才能排除;命名空间没写时取 namespace,也没有就是不确定
        other = (isinstance(name, str) and name not in ("", "coredns")) or (name is None and bool(metadata.get("generateName")))
        if not other and (metadata.get("namespace") or namespace) in (None, "", "kube-system"):
            targets.add("coredns")
    return targets


def _resource_type_targets(resource_type: str, name: str | None, namespace: str | None) -> set[str]:
    targets = set()
    for item in str(resource_type).lower().split(","):
        item = item.strip()
        base = item.split(".", 1)[0]
        if base in ISTIO_RESOURCE_NAMES or "istio.io" in item or "istio-io" in item:
            targets.add("istio")
        if base in _CONFIGMAP_NAMES and name in (None, "", "coredns") and namespace in (None, "", "kube-system"):
            targets.add("coredns")
    return targets


def _decode_path(path: str) -> str:
    """和 executor 一样最多解码三次,%63oredns 这类写法不能绕过识别。"""
    for _ in range(3):
        decoded = unquote(path)
        if decoded == path:
            break
        path = decoded
    return path


def _parse_api_path(path) -> dict | None:
    """拆 Kubernetes API 路径:组(core 为空串,不是资源路径为 None)、命名空间、资源、名称、子资源。"""
    path = _decode_path(path) if isinstance(path, str) else ""
    if not path.startswith("/") or any(c in path for c in "%?#\\") or "//" in path:
        return None
    parts = path.strip("/").split("/")
    if any(part in (".", "..") for part in parts):
        return None
    group, rest = None, []
    if parts[:2] == ["api", "v1"]:
        group, rest = "", parts[2:]
    elif parts[0] == "apis" and len(parts) >= 2:
        group, rest = parts[1].lower(), parts[3:]
    namespace = None
    if rest[:1] == ["namespaces"] and len(rest) >= 3:
        namespace, rest = rest[1], rest[2:]
    return {"path": path, "group": group, "namespace": namespace, "rest": rest,
            "resource": rest[0] if rest else "", "name": rest[1] if len(rest) > 1 else None,
            "subresource": rest[2] if len(rest) > 2 else ""}


def _api_path_targets(path) -> set[str]:
    parsed = _parse_api_path(path)
    if parsed is None:
        return set(BOTH_TARGETS)
    targets = set()
    group = parsed["group"] or ""
    if group in ISTIO_API_GROUPS or group.endswith(".istio.io") or "istio.io" in parsed["path"].lower():
        targets.add("istio")   # 也包括 Istio 的 CRD 本身
    if (parsed["group"] == "" and parsed["resource"] == "configmaps"
            and parsed["namespace"] in (None, "kube-system") and parsed["name"] in (None, "coredns")):
        targets.add("coredns")   # 没有名称是集合操作(新建、批量删除),按涉及处理
    return targets


def identify_targets(yaml_content: str | None = None, resource_type: str | None = None, name: str | None = None,
                     namespace: str | None = None, api_path: str | None = None) -> set[str]:
    """修改涉及的目标:{"coredns", "istio"} 的子集。

    - coredns:kube-system 命名空间的 ConfigMap coredns;
    - istio:Istio API 组(networking / security / telemetry / extensions.istio.io)、kind 和简称。
    能判断出不涉及的返回空集合;YAML 解析失败、资源类型不明、没给任何可判断的数据等判断不了的情况,
    按涉及处理(宁可误报)。namespace 不传表示不确定,同时是 YAML 里没写命名空间时的默认值。
    """
    targets: set[str] = set()
    provided = False
    if yaml_content:
        provided = True
        objects = _manifest_objects(yaml_content) if isinstance(yaml_content, str) else None
        if objects is None:
            return set(BOTH_TARGETS)
        for obj in objects:
            targets |= _object_targets(obj, namespace)
    if resource_type:
        provided = True
        targets |= _resource_type_targets(resource_type, name, namespace)
    if api_path:
        provided = True
        targets |= _api_path_targets(api_path)
    return targets if provided else set(BOTH_TARGETS)


# ---------------------------------------------------------------- 动作归类(计划 §4)


def _unique(items) -> list[str]:
    return list(dict.fromkeys(items))


def _target_ops(targets) -> list[str]:
    return [op for target, op in TARGET_OPS if target in targets]


def _api_request_ops(method: str, path: str) -> list[str]:
    """API 修改按请求区分的操作类型(不含 ai.k8s_write 和目标识别)。"""
    parsed = _parse_api_path(path)
    if parsed is None:
        return []
    core_pods = parsed["group"] == "" and parsed["resource"] == "pods"
    if (core_pods and parsed["subresource"] in ("exec", "attach")) or (parsed["resource"] == "nodes" and "proxy" in parsed["rest"]):
        return ["pod.exec.danger"]   # 容器里执行什么判断不了,按高危(节点代理能直接调 kubelet 执行命令)
    if method == "DELETE":
        return ["pod.delete" if core_pods else "resource.delete"]
    if parsed["subresource"] == "scale":
        return ["workload.scale"]
    if core_pods and parsed["subresource"] == "eviction":
        return ["pod.delete"]   # 驱逐就是删除 Pod
    return ["resource.apply"] if method in ("POST", "PUT", "PATCH") else []


def _api_ops(method: str, path: str) -> list[str]:
    return _unique(["ai.k8s_write", *_api_request_ops(method, path), *_target_ops(_api_path_targets(path))])


# kubectl 常见的带值参数(只放确定带值的;布尔参数和 --dry-run 这类可选值参数不能放,否则会吞掉资源名)
_KUBECTL_SHORT_VALUE = "nlfkopcLevs"
_KUBECTL_LONG_VALUE = frozenset({
    "--namespace", "--selector", "--field-selector", "--filename", "--kustomize", "--output", "--patch",
    "--patch-file", "--type", "--container", "--raw", "--grace-period", "--timeout", "--replicas",
    "--current-replicas", "--resource-version", "--image", "--port", "--protocol", "--target-port", "--name",
    "--labels", "--overrides", "--from-file", "--from-literal", "--from-env-file", "--field-manager", "--subresource",
    "--template", "--sort-by", "--chunk-size", "--min", "--max", "--cpu-percent", "--to-revision",
    "--pod-running-timeout", "--request-timeout", "--v", "--vmodule", "--log-flush-frequency", "--profile",
    "--profile-output", "--env", "--requests", "--limits", "--serviceaccount", "--restart", "--image-pull-policy",
    "--hostport", "--cluster-ip", "--external-ip", "--load-balancer-ip", "--session-affinity", "--target",
    "--copy-to", "--pod-selector", "--skip-wait-for-delete-timeout", "--prune-allowlist", "--docker-server",
    "--docker-username", "--docker-password", "--docker-email", "--cert", "--key", "--clusterrole", "--role",
    "--group", "--verb", "--resource", "--resource-name", "--non-resource-url", "--schedule", "--from",
    "--duration", "--audience", "--bound-object-kind", "--bound-object-name", "--bound-object-uid", "--containers",
    "--for", "--label-columns", "--since", "--since-time", "--tail", "--limit-bytes", "--max-log-requests", "--keys",
})
_KUBECTL_APPLY_VERBS = frozenset({"apply", "create", "replace", "patch", "edit", "label", "annotate", "autoscale",
                                  "taint", "expose", "run", "debug"})
_KUBECTL_VERB_OPS = {"scale": ("workload.scale",), "cordon": ("node.cordon",), "uncordon": ("node.cordon",),
                     "drain": ("node.cordon", "pod.delete"), "attach": ("pod.exec.danger",)}
_ROLLOUT_OPS = {"restart": ("workload.restart",), "undo": ("resource.apply",), "pause": ("resource.apply",),
                "resume": ("resource.apply",), "status": (), "history": ()}
_SET_SUBCOMMANDS = frozenset({"env", "image", "resources", "selector", "serviceaccount", "sa", "subject"})
_RAW_METHODS = {"get": "GET", "create": "POST", "replace": "PUT", "delete": "DELETE"}


def _kubectl_args(argv: list[str], start: int) -> dict:
    """解析 kubectl 动作之后的参数(-- 之后都是位置参数)。"""
    parsed = {"positionals": [], "values": [], "files": [], "namespace": None, "all_namespaces": False,
              "bulk": False, "kustomize": False, "raw": None}

    def flag(name: str, value: str | None):
        if value is not None:
            parsed["values"].append(value)
        enabled = value is None or value.lower() not in ("false", "0")
        if name in ("-n", "--namespace"):
            parsed["namespace"] = value
        elif name in ("-A", "--all-namespaces"):
            parsed["all_namespaces"] = enabled
        elif name in ("-l", "--selector", "--field-selector") or (name == "--all" and enabled):
            parsed["bulk"] = True
        elif name in ("-f", "--filename"):
            parsed["files"].append(value or "")
        elif name in ("-k", "--kustomize"):
            parsed["kustomize"] = True
        elif name == "--raw":
            parsed["raw"] = value or ""

    i = start
    while i < len(argv):
        token = argv[i]
        if token == "--":
            parsed["positionals"].extend(argv[i + 1:])
            break
        if token.startswith("--"):
            name, equals, value = token.partition("=")
            if not equals and name in _KUBECTL_LONG_VALUE:
                i += 1
                value = argv[i] if i < len(argv) else ""
            flag(name, value if equals or name in _KUBECTL_LONG_VALUE else None)
        elif token.startswith("-") and len(token) > 1:
            for j in range(1, len(token)):   # 短参数可以连写:-it、-nkube-system、-itn kube-system
                if token[j] in _KUBECTL_SHORT_VALUE:
                    value = token[j + 1:].removeprefix("=")
                    if not value:
                        i += 1
                        value = argv[i] if i < len(argv) else ""
                    flag("-" + token[j], value)
                    break
                flag("-" + token[j], None)
        else:
            parsed["positionals"].append(token)
        i += 1
    return parsed


def _has_type(kind: str, names: frozenset[str]) -> bool:
    """资源类型(可以是 pod,svc 或 configmaps.v1.)里有没有 names 中的类型。"""
    return any(part.strip().split(".", 1)[0] in names for part in kind.lower().split(","))


def _kubectl_manifests(parsed: dict, arguments: dict) -> list[str] | None:
    """-f 指定的清单内容(- 是 stdin,其余从附带的 files 取);拿不到(远程地址、目录、kustomize)返回 None。"""
    if parsed["kustomize"]:
        return None
    files = arguments.get("files") if isinstance(arguments.get("files"), dict) else {}
    contents = []
    for source in parsed["files"]:
        content = arguments.get("stdin") if source == "-" else files.get(source)
        if not isinstance(content, str) or not content.strip():
            return None
        contents.append(content)
    return contents


def _kubectl_targets(parsed: dict, arguments: dict) -> set[str]:
    manifests = _kubectl_manifests(parsed, arguments)
    if manifests is None:
        return set(BOTH_TARGETS)
    namespace = None if parsed["all_namespaces"] else parsed["namespace"]
    targets = set()
    for content in manifests:
        targets |= identify_targets(yaml_content=content, namespace=namespace)
    if parsed["raw"] is not None:
        targets |= _api_path_targets(parsed["raw"])
    positionals = [word.lower() for word in parsed["positionals"]]
    words = positionals + [value.lower() for value in parsed["values"]]
    # Istio:资源类型写法(含 vs、gw 等简称)出现在任何参数里都算,宁可误报
    if any(_has_type(word.partition("/")[0], ISTIO_RESOURCE_NAMES) or "istio.io" in word or "istio-io" in word
           for word in words):
        targets.add("istio")
    # CoreDNS:configmap 类型 + 命名空间是 kube-system 或不确定 + (名称是 coredns / 没写名称 / 按选择器、--all 批量)
    plain = [word for word in positionals if "/" not in word]
    first = next((index for index, word in enumerate(plain) if _has_type(word, _CONFIGMAP_NAMES)), None)
    named = {word.partition("/")[2] for word in positionals if "/" in word and _has_type(word.partition("/")[0], _CONFIGMAP_NAMES)}
    if first is not None or named:
        names = named | (set(plain[first + 1:]) if first is not None else set())
        mentioned = {word.rsplit("/", 1)[-1] for word in words}
        if namespace in (None, "", "kube-system") and (parsed["bulk"] or not names or "coredns" in mentioned):
            targets.add("coredns")
    return targets


def _kubectl_delete_ops(parsed: dict, arguments: dict) -> list[str]:
    """delete 的是 Pod 为 pod.delete,其他资源为 resource.delete(都有就两个都要)。"""
    plain = [word for word in parsed["positionals"] if "/" not in word]
    kinds = [plain[0]] if plain else []
    kinds += [word.partition("/")[0] for word in parsed["positionals"] if "/" in word]
    types = {part.strip().lower().split(".", 1)[0] for kind in kinds for part in kind.split(",")}
    manifests = _kubectl_manifests(parsed, arguments)
    for content in manifests or []:
        objects = _manifest_objects(content)
        if objects is None:
            types.add("")   # 清单解析不了,按删除其他资源
        else:
            types |= {"pod" if obj.get("kind") == "Pod" else str(obj.get("kind", "")).lower() for obj in objects}
    if manifests is None or not types:
        types.add("")   # 清单拿不到(kustomize、远程地址、没附带的文件)或者没写资源类型
    ops = []
    if types & _POD_NAMES:
        ops.append("pod.delete")
    if types - _POD_NAMES:
        ops.append("resource.delete")
    return ops


def _kubectl_ops(argv: list[str], verb: str, index: int, arguments: dict) -> list[str]:
    """kubectl 修改:ai.k8s_write + 按动作对应的操作 + 目标识别;cp 只要 pod.exec.other。"""
    if verb == "cp":
        return ["pod.exec.other"]
    start = index + 1 if index >= 0 else len(argv)
    parsed = _kubectl_args(argv, start)
    sub = _next_positional(argv, start)[0]
    ops = ["ai.k8s_write", *_KUBECTL_VERB_OPS.get(verb, ())]
    if verb == "delete":
        ops += _kubectl_delete_ops(parsed, arguments)
    elif verb == "rollout":
        # 认不出子命令时两种都算
        ops += _ROLLOUT_OPS.get(sub, ("workload.restart", "resource.apply"))
    elif verb == "set":
        ops += ["workload.image"] if sub == "image" else ["resource.apply"] if sub in _SET_SUBCOMMANDS else ["workload.image", "resource.apply"]
    elif verb in _KUBECTL_APPLY_VERBS:
        ops.append("resource.apply")
    if parsed["raw"] is not None:
        ops += _api_request_ops(_RAW_METHODS.get(verb, "POST"), parsed["raw"])
    ops += _target_ops(_kubectl_targets(parsed, arguments))
    return _unique(ops)


def _kubectl_exec_category(argv: list[str], index: int, arguments: dict, commands: dict | None) -> str:
    """kubectl exec 按 -- 之后的命令分类;没有 -- 分不清哪些是命令,按高危。"""
    if "--" not in argv[index + 1:]:
        return "danger"
    separator = argv.index("--", index + 1)
    command = argv[separator + 1:]
    if not command:
        return "other"
    # -i / --stdin 时 stdin 会送进容器(比如 sh 从 stdin 读脚本),一起参与高危判断
    stdin_flag = any(flag in ("--stdin", "--stdin=true") or (flag.startswith("-") and not flag.startswith("--") and "i" in flag)
                     for flag in argv[index + 1:separator])
    return classify_pod_command(command, commands, stdin=arguments.get("stdin") if stdin_flag else None)


# ---------------------------------------------------------------- 总入口

_KUBECTL_READ_VERBS = frozenset({"get", "describe", "logs", "top", "explain", "api-resources", "api-versions", "version", "diff"})
_POD_REASONS = {
    "view": "Pod 内查看命令",
    "other": "Pod 内其他命令,可能修改容器里的文件或进程",
    "danger": "Pod 内高危命令(删除文件、结束进程等),必须审批",
}


def _created_name_excludes_coredns(method: str, path: str, body) -> bool:
    """POST 到 kube-system 的 configmaps 集合时,body 里的名字明确不是 coredns。"""
    parsed = _parse_api_path(path)
    if method != "POST" or parsed is None or parsed["resource"] != "configmaps" or parsed["name"] is not None:
        return False
    metadata = body.get("metadata") if isinstance(body, dict) else None
    name = metadata.get("name") if isinstance(metadata, dict) else None
    return isinstance(name, str) and name not in ("", "coredns")


def classify_operation(operation: str, arguments: dict, commands: dict | None = None) -> dict:
    """返回 read_only、reason、ops、category、dangerous。

    - read_only:操作本身不改东西;pod_exec / kubectl exec 只有查看命令算只读;
    - ops:涉及的操作类型(权限表的行),只读的集群查询为空;
    - category:Pod 内命令分类(pod_exec、kubectl exec;API exec 子资源、kubectl attach 按 danger),其他为 None;
    - dangerous:category 是 danger(高危命令固定审批)。
    commands 是 master 下发的命令清单,不传用自带的默认清单。
    """
    read_only = False
    reason = "Unknown operation requires approval"
    ops: list[str] = []
    category = None
    if not isinstance(arguments, dict):
        arguments = {}

    if operation == "api":
        method = str(arguments.get("method", "GET")).upper()
        path = arguments.get("path", "")
        unsafe = re.search(r"/(?:proxy|exec|attach|portforward|ephemeralcontainers)(?:/|$)", str(path), re.I)
        read_only = method in {"GET", "HEAD"} and bool(API_PATH.fullmatch(str(path))) and "%" not in str(path) and not unsafe
        reason = "Read Kubernetes API resource" if read_only else "Kubernetes write or active subresource requires approval"
        if not read_only:
            ops = _api_ops(method, str(path))
            if "network.coredns" in ops and _created_name_excludes_coredns(method, str(path), arguments.get("body")):
                ops.remove("network.coredns")
            category = "danger" if "pod.exec.danger" in ops else None

    elif operation in {"kubectl", "istioctl"}:
        argv = arguments.get("argv", [])
        if not isinstance(argv, list) or any(not isinstance(a, str) for a in argv):
            return {"read_only": False, "reason": "Invalid command requires validation", "ops": [], "category": None,
                    "dangerous": False}
        verb, index = cli_verb(argv)
        subverb = argv[index + 1] if index >= 0 and len(argv) > index + 1 else ""
        if operation == "kubectl" and verb == "exec":
            # 按 -- 之后的命令分类,不需要 ai.k8s_write;和 pod_exec 一样只有查看命令算只读
            category = _kubectl_exec_category(argv, index, arguments, commands)
            read_only, ops, reason = category == "view", [f"pod.exec.{category}"], _POD_REASONS[category]
        else:
            if operation == "kubectl":
                read_only = verb in _KUBECTL_READ_VERBS
                read_only |= verb == "auth" and subverb == "can-i"
                if verb == "get" and any(a.startswith("--raw") for a in argv):
                    raw_path = next((a.split("=", 1)[1] for a in argv if a.startswith("--raw=")), "")
                    if "--raw" in argv and argv.index("--raw") + 1 < len(argv):
                        raw_path = argv[argv.index("--raw") + 1]
                    read_only = classify_operation("api", {"method": "GET", "path": raw_path})["read_only"]
                if not read_only:
                    ops = _kubectl_ops(argv, verb, index, arguments)
                    category = "danger" if "pod.exec.danger" in ops else None
            else:
                read_only = verb in {"version", "analyze", "proxy-status", "proxy-config", "pc", "ps"}
                if verb in {"proxy-config", "pc"} and subverb == "log" and any(a == "--level" or a.startswith("--level=") for a in argv):
                    read_only = False
                read_only |= verb == "manifest" and subverb == "generate"
                read_only |= verb == "waypoint" and subverb in {"list", "status", "generate"}
                if not read_only:
                    ops = ["ai.k8s_write", "network.istio"]
            reason = "Inspect selected Kubernetes cluster" if read_only else "Command may modify the cluster; approval required"

    elif operation == "diagnostic":
        program = arguments.get("program")
        read_only = program in READ_ONLY_DIAGNOSTIC_PROGRAMS
        argv = arguments.get("argv", [])

        if program == "yq" and any(a in {"-i", "--inplace", "--in-place"} or a.startswith("--inplace=") for a in argv):
            read_only = False
        if program == "curl":
            read_only = False
        if program == "openssl":
            read_only = False

        if not read_only:
            ops.append("ai.diagnostic")

        reason = "Read diagnostic input" if read_only else "Diagnostic execution may have side effects; approval required"

    elif operation == "pod_exec":
        category = classify_pod_command(arguments.get("argv", []), commands)
        read_only, ops, reason = category == "view", [f"pod.exec.{category}"], _POD_REASONS[category]

    return {"read_only": bool(read_only), "reason": reason, "ops": ops, "category": category,
            "dangerous": category == "danger"}
