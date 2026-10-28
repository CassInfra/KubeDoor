"""Conservative classification shared by every execution surface."""

from __future__ import annotations

import re

API_PATH = re.compile(r"^/(?:api(?:/v[0-9]+(?:/.*)?)?|apis(?:/[^/?#]+(?:/[^/?#]+(?:/.*)?)?)?|version|(?:livez|readyz|healthz)(?:/[^?#]*)?|openapi/(?:v2|v3)(?:/[^?#]*)?)$")
DIAGNOSTIC_PROGRAMS = frozenset({"jq", "yq", "curl", "dig", "openssl", "rg"})
TARGET_FLAGS = frozenset({"--kubeconfig", "--context", "--server", "--token", "--user", "--cluster",
                          "--client-key", "--client-certificate", "--certificate-authority", "--tls-server-name",
                          "--insecure-skip-tls-verify", "--as", "--as-group", "--as-uid", "--cache-dir", "--username", "--password"})


def cli_verb(argv: list[str]) -> tuple[str, int]:
    value_flags = {"-n", "--namespace", "--request-timeout", "--v", "--vmodule", "--log-flush-frequency"}
    i = 0
    while i < len(argv):
        token = argv[i]
        if token in value_flags:
            i += 2
        elif token.startswith("-"):
            i += 1
        else:
            return token, i
    return "", -1


def _is_dangerous_command(argv: list[str]) -> bool:
    """判断 Pod 内命令是否高危:rm/kill 等必须审批,即使该 Pod 已信任。"""
    if not argv:
        return False
    text = " ".join(argv)
    # rm 带 -r/-f,或删根目录/系统目录;kill/pkill/killall;重定向 >;管道后接写操作;find -delete/-exec rm
    dangerous = [
        r"\brm\s+.*(-r|-f|--recursive|--force)",  # rm -rf
        r"\brm\s+.*/($|bin|boot|dev|etc|lib|proc|root|sbin|sys|usr)\b",  # 删根
        r"\b(kill|pkill|killall)\b",  # 杀进程
        r">",  # 重定向(覆盖或追加)
        r"\|\s*(rm|kill|pkill|dd|mkfs|fdisk|parted|reboot|shutdown|halt|poweroff)",  # 管道后接危险命令
        r"\bfind\b.*(-delete|-exec\s+rm)",  # find -delete 或 find -exec rm
    ]
    return any(re.search(pattern, text) for pattern in dangerous)


def classify_operation(operation: str, arguments: dict) -> dict:
    """Unknown commands and all Pod execution require explicit approval."""
    read_only = False
    reason = "Unknown operation requires approval"
    if operation == "api":
        method = str(arguments.get("method", "GET")).upper()
        path = arguments.get("path", "")
        unsafe = re.search(r"/(?:proxy|exec|attach|portforward|ephemeralcontainers)(?:/|$)", str(path), re.I)
        read_only = method in {"GET", "HEAD"} and bool(API_PATH.fullmatch(str(path))) and "%" not in str(path) and not unsafe
        reason = "Read Kubernetes API resource" if read_only else "Kubernetes write or active subresource requires approval"
    elif operation in {"kubectl", "istioctl"}:
        argv = arguments.get("argv", [])
        if not isinstance(argv, list) or any(not isinstance(a, str) for a in argv):
            return {"read_only": False, "reason": "Invalid command requires validation"}
        verb, index = cli_verb(argv)
        subverb = argv[index + 1] if index >= 0 and len(argv) > index + 1 else ""
        if operation == "kubectl":
            read_only = verb in {"get", "describe", "logs", "top", "explain", "api-resources", "api-versions", "version", "diff"}
            read_only |= verb == "auth" and subverb == "can-i"
            if verb == "get" and any(a.startswith("--raw") for a in argv):
                raw_path = next((a.split("=", 1)[1] for a in argv if a.startswith("--raw=")), "")
                if "--raw" in argv and argv.index("--raw") + 1 < len(argv):
                    raw_path = argv[argv.index("--raw") + 1]
                read_only = classify_operation("api", {"method": "GET", "path": raw_path})["read_only"]
        else:
            read_only = verb in {"version", "analyze", "proxy-status", "proxy-config", "pc", "ps"}
            if verb in {"proxy-config", "pc"} and subverb == "log" and any(a == "--level" or a.startswith("--level=") for a in argv):
                read_only = False
            read_only |= verb == "manifest" and subverb == "generate"
            read_only |= verb == "waypoint" and subverb in {"list", "status", "generate"}
        reason = "Inspect selected Kubernetes cluster" if read_only else "Command may modify the cluster; approval required"
    elif operation == "diagnostic":
        program = arguments.get("program")
        read_only = program in {"jq", "yq", "dig", "rg"}
        argv = arguments.get("argv", [])
        if program == "yq" and any(a in {"-i", "--inplace", "--in-place"} or a.startswith("--inplace=") for a in argv):
            read_only = False
        if program == "curl":
            # Even GET can address an application endpoint with side effects.
            read_only = False
        reason = "Read diagnostic input" if read_only else "Diagnostic execution may have side effects; approval required"
    elif operation == "pod_exec":
        argv = arguments.get("argv", [])
        dangerous = _is_dangerous_command(argv) if isinstance(argv, list) else False
        reason = "高危命令必须审批(rm/kill/重定向等)" if dangerous else "Pod 内查看命令直接执行"
        return {"read_only": False, "reason": reason, "dangerous": dangerous}
    return {"read_only": bool(read_only), "reason": reason}
