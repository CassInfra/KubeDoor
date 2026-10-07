#!/usr/bin/env bash
# =============================================================================
# KubeDoor 安装脚本
#
#   ./install.sh master                安装控制端
#   ./install.sh agent                 安装集群端
#   ./install.sh render master [-o 目录] 只渲染 YAML 不部署(检查用)
#   ./install.sh status                查看部署状态
#   ./install.sh uninstall master      卸载控制端
#   ./install.sh uninstall agent       卸载集群端
#
# 可选参数:
#   -c, --config FILE   指定配置文件,默认脚本同目录的 kubedoor.conf
#                       (第一次用先 cp kubedoor.conf.example kubedoor.conf 再改)
#   -o, --output DIR    render 的输出目录,默认打到标准输出
#   -y, --yes           跳过确认
#
# 依赖:bash 4+、kubectl。在能直接执行 kubectl 的机器上跑。
# =============================================================================

set -euo pipefail

# bash 5.2 起 patsub_replacement 默认开启,会让 ${var//pat/rep} 里 rep 的 & 展开成
# 匹配到的文本。渲染时替换串是用户配的密码/token,必须关掉,否则密码里的 & 会被吃掉。
shopt -u patsub_replacement 2>/dev/null || true

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
MANIFEST_DIR="$SCRIPT_DIR/manifests"
DASHBOARD_DIR="$SCRIPT_DIR/dashboards"
CONFIG_FILE="$SCRIPT_DIR/kubedoor.conf"
OUTPUT_DIR=""
ASSUME_YES="false"

# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------
if [[ -t 1 ]]; then
    C_RED=$'\033[31m'; C_GRN=$'\033[32m'; C_YLW=$'\033[33m'
    C_BLU=$'\033[36m'; C_BLD=$'\033[1m';  C_RST=$'\033[0m'
else
    C_RED=""; C_GRN=""; C_YLW=""; C_BLU=""; C_BLD=""; C_RST=""
fi

info() { printf '%s\n' "${C_BLU}==>${C_RST} $*"; }
ok()   { printf '%s\n' "${C_GRN} ✓ ${C_RST} $*"; }
warn() { printf '%s\n' "${C_YLW} ! ${C_RST} $*" >&2; }
die()  { printf '%s\n' "${C_RED} ✗ ${C_RST} $*" >&2; exit 1; }

title() {
    printf '\n%s\n' "${C_BLD}$*${C_RST}"
    printf '%s\n' "$(printf '─%.0s' $(seq 1 60))"
}

# ---------------------------------------------------------------------------
# 前置依赖
# ---------------------------------------------------------------------------
check_prereq() {
    # 关联数组需要 bash 4
    if (( BASH_VERSINFO[0] < 4 )); then
        die "需要 bash 4 或更高版本,当前是 ${BASH_VERSION}"
    fi
    command -v kubectl >/dev/null 2>&1 || die "找不到 kubectl,请先安装并配置好 kubeconfig"
    command -v base64  >/dev/null 2>&1 || die "找不到 base64 命令"
}

# kubectl 包装:统一带上 --context
kc() {
    if [[ -n "${KUBE_CONTEXT:-}" ]]; then
        kubectl --context="$KUBE_CONTEXT" "$@"
    else
        kubectl "$@"
    fi
}

# CronJob 的 spec.timeZone 从 K8S 1.25 起默认可用,更老的集群 kubectl 校验会拒绝这个字段。
# 参数是 gitVersion(如 v1.27.3-eks-xxx);取不到版本时按支持处理
k8s_supports_cron_timezone() {
    local minor
    minor="$(printf '%s' "$1" | sed -nE 's/^v?1\.([0-9]+).*/\1/p')"
    [[ -z "$minor" ]] || (( minor >= 25 ))
}

# ---------------------------------------------------------------------------
# 检查能不能连上 K8S。连不上直接报错,不做任何部署动作。
# ---------------------------------------------------------------------------
check_k8s() {
    local ctx
    if [[ -n "${KUBE_CONTEXT:-}" ]]; then
        ctx="$KUBE_CONTEXT"
        kubectl config get-contexts "$ctx" >/dev/null 2>&1 \
            || die "kubeconfig 里没有名为 ${C_BLD}${ctx}${C_RST} 的 context,可用的有:
$(kubectl config get-contexts -o name 2>/dev/null | sed 's/^/    /')"
    else
        ctx="$(kubectl config current-context 2>/dev/null || true)"
        [[ -n "$ctx" ]] || die "kubeconfig 里没有设置当前 context,请用 kubectl config use-context 选一个,或在配置文件里填 KUBE_CONTEXT"
    fi

    info "检查 K8S 连接(context: ${C_BLD}${ctx}${C_RST})"
    local out
    if ! out="$(kc cluster-info --request-timeout=10s 2>&1)"; then
        printf '%s\n' "$out" | sed 's/^/    /' >&2
        die "连不上 K8S 集群。请确认 kubeconfig、网络和证书都没问题后重试"
    fi

    local server
    server="$(kc config view --minify -o jsonpath='{.clusters[0].cluster.server}' 2>/dev/null || true)"
    ok "已连接:${server:-$ctx}"

    # 版本信息,顺便确认 API Server 真的能应答
    local ver
    ver="$(kc version -o json 2>/dev/null | grep -o '"gitVersion": *"[^"]*"' | tail -1 | cut -d'"' -f4 || true)"
    [[ -n "$ver" ]] && ok "K8S 版本:$ver"
    if ! k8s_supports_cron_timezone "$ver"; then
        CRON_TIMEZONE="false"
        warn "K8S 低于 1.25,CronJob 不支持 timeZone:每日采集和定时/周期任务将按 CronJob 控制器的时区执行(通常是 UTC),不是北京时间"
    fi

    # 权限:装 master/agent 都要能建命名空间级资源
    if ! kc auth can-i create deployments -n "$NAMESPACE" >/dev/null 2>&1; then
        warn "当前账号可能没有在命名空间 ${NAMESPACE} 里创建 Deployment 的权限,安装可能失败"
    fi
}

# PG 连通性。只是从当前机器探一下端口,Pod 能不能连上还取决于集群网络。
check_pg() {
    info "探测 PostgreSQL ${PG_HOST}:${PG_PORT}"
    local reachable="false"
    if command -v nc >/dev/null 2>&1; then
        nc -z -w3 "$PG_HOST" "$PG_PORT" >/dev/null 2>&1 && reachable="true"
    else
        (exec 3<>"/dev/tcp/${PG_HOST}/${PG_PORT}") >/dev/null 2>&1 && reachable="true"
    fi

    if [[ "$reachable" == "true" ]]; then
        ok "端口可达"
    else
        warn "从当前机器连不上 ${PG_HOST}:${PG_PORT}"
        warn "如果这台机器和 K8S 节点不在同一网络,可以忽略;否则请先启动数据库:"
        warn "    cd ${SCRIPT_DIR}/postgres && docker compose up -d"
    fi
    # PG_HOST 填 localhost 类地址时,K8S 里的 Pod 一定连不上
    case "$PG_HOST" in
        127.0.0.1|localhost|::1)
            die "PG_HOST 不能填 ${PG_HOST} —— 这是 Pod 自己的回环地址。
    请填 K8S 节点能访问到的地址,比如数据库宿主机的内网 IP" ;;
    esac
}

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
load_config() {
    if [[ ! -f "$CONFIG_FILE" ]]; then
        if [[ -f "$SCRIPT_DIR/kubedoor.conf.example" && "$CONFIG_FILE" == "$SCRIPT_DIR/kubedoor.conf" ]]; then
            die "找不到配置文件:$CONFIG_FILE
    第一次安装请先从模板复制一份,按注释修改后再执行:
    cp $SCRIPT_DIR/kubedoor.conf.example $CONFIG_FILE"
        fi
        die "找不到配置文件:$CONFIG_FILE"
    fi
    # shellcheck disable=SC1090
    source "$CONFIG_FILE"
    ok "已加载配置:$CONFIG_FILE"

    : "${NAMESPACE:=kubedoor}"
    : "${KUBE_CONTEXT:=}"
}

require_var() {
    local name="$1" hint="${2:-}"
    local val="${!name:-}"
    [[ -n "$val" ]] || die "配置项 ${C_BLD}${name}${C_RST} 不能为空${hint:+ —— $hint}
    请编辑 $CONFIG_FILE"
}

# 模板里凡是会落到 YAML 标量的占位符都用单引号包着 —— 单引号标量是纯字面量,
# 密码里的 \ " $ & # : 都不会被 YAML 解释。唯一的例外是单引号本身,
# 与其做上下文相关的转义,不如直接拦下来给出明确提示。
check_no_quote() {
    local name val
    for name in "$@"; do
        val="${!name:-}"
        if [[ "$val" == *"'"* ]]; then
            die "配置项 ${C_BLD}${name}${C_RST} 里不能包含单引号(')
    当前值:${val}
    请换一个不含单引号的值"
        fi
    done
}

validate_master() {
    require_var PG_HOST     "K8S 里的 Pod 能访问到的数据库地址"
    require_var PG_PORT
    require_var PG_USER
    require_var PG_PASSWORD
    require_var PG_DATABASE
    require_var PROM_K8S_TAG_KEY
    require_var IMAGE_REPO
    require_var WEB_AUTH_USERS "Web 登录账号,htpasswd 格式"

    [[ "$PROM_K8S_TAG_KEY" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] \
        || die "PROM_K8S_TAG_KEY 只能是字母数字下划线,当前值:$PROM_K8S_TAG_KEY"

    check_no_quote NAMESPACE IMAGE_REPO PROM_K8S_TAG_KEY \
        TAG_MASTER TAG_WEB TAG_ALARM TAG_AI \
        PG_HOST PG_PORT PG_USER PG_PASSWORD PG_DATABASE \
        MSG_TYPE MSG_TOKEN DEFAULT_AT PROM_URL PROM_TYPE \
        ALERTMANAGER_EXTURL KUBEDOOR_EXTURL STORAGE_CLASS \
        VM_USER VM_PASSWORD VM_RETENTION VM_STORAGE \
        GRAFANA_ADMIN_USER GRAFANA_ADMIN_PASSWORD \
        IMAGE_GRAFANA IMAGE_VICTORIA_METRICS IMAGE_VMALERT IMAGE_ALERTMANAGER IMAGE_BUSYBOX_CURL

    if [[ "${ENABLE_VICTORIA_METRICS:-true}" != "true" ]]; then
        require_var PROM_URL "ENABLE_VICTORIA_METRICS=false 时必须填已有时序库的地址"
    fi

    if [[ "${ENABLE_ALERTMANAGER:-true}" == "true" ]]; then
        [[ -n "${MSG_TOKEN:-}" ]] || warn "MSG_TOKEN 为空,告警通知发不出去(装完可以改 kubedoor-config 这个 ConfigMap)"
    fi
}

validate_agent() {
    require_var K8S_NAME  "本集群的唯一名字"
    require_var MASTER_WS "master 的 WebSocket 地址"
    require_var PROM_K8S_TAG_KEY
    require_var IMAGE_REPO

    [[ "$K8S_NAME" =~ ^[A-Za-z0-9_-]+$ ]] \
        || die "K8S_NAME 只能是字母数字下划线中划线,当前值:$K8S_NAME"

    check_no_quote NAMESPACE IMAGE_REPO PROM_K8S_TAG_KEY TAG_AGENT \
        K8S_NAME MASTER_WS OSS_URL MSG_TYPE MSG_TOKEN \
        REMOTE_WRITE_URL VM_USER VM_PASSWORD \
        IMAGE_VMAGENT IMAGE_KUBE_STATE_METRICS IMAGE_NODE_EXPORTER
}

# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------
declare -A VARS

# 多行文本按块缩进:首行不动(占位符位置已有缩进),后续行补上前缀
indent_block() {
    local prefix="$1" text="$2"
    local out="" line first="true"
    while IFS= read -r line; do
        if [[ "$first" == "true" ]]; then
            out="$line"; first="false"
        else
            out+=$'\n'"${prefix}${line}"
        fi
    done <<< "$text"
    printf '%s' "$out"
}

build_common_vars() {
    VARS=()
    VARS[NAMESPACE]="$NAMESPACE"
    VARS[IMAGE_REPO]="$IMAGE_REPO"
    VARS[PROM_K8S_TAG_KEY]="$PROM_K8S_TAG_KEY"
    VARS[MSG_TYPE]="${MSG_TYPE:-wecom}"
    VARS[MSG_TOKEN]="${MSG_TOKEN:-}"
}

# Persist encryption keys independently of tracked configuration. A render is
# reproducible after first generation; installing from another machine can
# recover the existing cluster Secret before producing a new key.
ensure_ai_secrets() {
    local cache="${AI_SECRET_FILE:-${CONFIG_FILE}.ai-secrets}"
    if [[ -f "$cache" ]]; then
        local saved_token="" saved_key="" name value
        while IFS='=' read -r name value; do
            case "$name" in
                AI_INTERNAL_TOKEN) saved_token="$value" ;;
                AI_ENCRYPTION_KEY) saved_key="$value" ;;
            esac
        done < "$cache"
        : "${AI_INTERNAL_TOKEN:=$saved_token}"
        : "${AI_ENCRYPTION_KEY:=$saved_key}"
    fi
    if [[ "${args[0]:-}" == "master" ]]; then
        local existing deployed_token deployed_key
        if ! existing="$(kc -n "$NAMESPACE" get secret kubedoor-ai-security --ignore-not-found -o jsonpath='{.data.AI_INTERNAL_TOKEN} {.data.AI_ENCRYPTION_KEY}' 2>/dev/null)"; then
            die "无法检查集群中已有 AI 密钥，停止安装以避免覆盖 kubeconfig 加密密钥"
        fi
        if [[ -n "$existing" ]]; then
            deployed_token="$(printf '%s' "${existing%% *}" | base64 -d 2>/dev/null || true)"
            deployed_key="$(printf '%s' "${existing#* }" | base64 -d 2>/dev/null || true)"
            [[ "$deployed_token" =~ ^[a-fA-F0-9]{64}$ && "$deployed_key" =~ ^[a-fA-F0-9]{64}$ ]] \
                || die "集群已有 AI Secret 格式无效，请恢复备份后安装"
            if [[ ( -n "${AI_INTERNAL_TOKEN:-}" && "$AI_INTERNAL_TOKEN" != "$deployed_token" ) || \
                  ( -n "${AI_ENCRYPTION_KEY:-}" && "$AI_ENCRYPTION_KEY" != "$deployed_key" ) ]]; then
                die "本地 AI 密钥与集群已有 Secret 不一致，请恢复对应的 .ai-secrets 备份；不会覆盖已有加密密钥"
            fi
            AI_INTERNAL_TOKEN="$deployed_token"
            AI_ENCRYPTION_KEY="$deployed_key"
        fi
    fi
    if [[ -z "${AI_INTERNAL_TOKEN:-}" ]]; then
        AI_INTERNAL_TOKEN="$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')"
    fi
    if [[ -z "${AI_ENCRYPTION_KEY:-}" ]]; then
        AI_ENCRYPTION_KEY="$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')"
    fi
    [[ "$AI_INTERNAL_TOKEN" =~ ^[a-fA-F0-9]{64}$ && "$AI_ENCRYPTION_KEY" =~ ^[a-fA-F0-9]{64}$ ]] \
        || die "AI_INTERNAL_TOKEN 和 AI_ENCRYPTION_KEY 必须为64位十六进制"
    (umask 077; printf 'AI_INTERNAL_TOKEN=%s\nAI_ENCRYPTION_KEY=%s\n' "$AI_INTERNAL_TOKEN" "$AI_ENCRYPTION_KEY" > "$cache")
    chmod 600 "$cache"
}

build_master_vars() {
    build_common_vars
    ensure_ai_secrets
    VARS[AI_INTERNAL_TOKEN]="$AI_INTERNAL_TOKEN"
    VARS[AI_ENCRYPTION_KEY]="$AI_ENCRYPTION_KEY"
    VARS[ENABLE_MCP]="${ENABLE_MCP:-true}"

    VARS[TAG_MASTER]="${TAG_MASTER:-latest}"
    VARS[TAG_WEB]="${TAG_WEB:-latest}"
    VARS[TAG_ALARM]="${TAG_ALARM:-latest}"
    VARS[TAG_AI]="${TAG_AI:-latest}"

    VARS[PG_HOST]="$PG_HOST"
    VARS[PG_PORT]="$PG_PORT"
    VARS[PG_USER]="$PG_USER"
    VARS[PG_PASSWORD]="$PG_PASSWORD"
    VARS[PG_DATABASE]="$PG_DATABASE"
    VARS[PG_POOL_MIN]="${PG_POOL_MIN:-2}"
    VARS[PG_POOL_MAX]="${PG_POOL_MAX:-20}"
    VARS[PG_CMD_TIMEOUT]="${PG_CMD_TIMEOUT:-60}"

    # 用内置 VM 时,时序库地址由脚本拼出来,不用用户操心
    if [[ "${ENABLE_VICTORIA_METRICS:-true}" == "true" ]]; then
        VARS[PROM_URL]="http://${VM_USER}:${VM_PASSWORD}@victoria-metrics.${NAMESPACE}:8428"
        VARS[PROM_TYPE]="Victoria-Metrics-Single"
    else
        VARS[PROM_URL]="$PROM_URL"
        VARS[PROM_TYPE]="${PROM_TYPE:-Victoria-Metrics-Single}"
    fi

    VARS[DEFAULT_AT]="${DEFAULT_AT:-}"
    VARS[ALERTMANAGER_EXTURL]="${ALERTMANAGER_EXTURL:-}"
    VARS[KUBEDOOR_EXTURL]="${KUBEDOOR_EXTURL:-}"
    VARS[ALERT_DEDUP_WINDOW]="${ALERT_DEDUP_WINDOW:-300}"
    VARS[SILENCE_CACHE_TTL]="${SILENCE_CACHE_TTL:-15}"

    # ConfigMap 里的块标量,占位符缩进 4 空格
    VARS[UPDATE_IMAGE_JSON]="$(indent_block '    ' "${UPDATE_IMAGE_JSON:-{\}}")"
    VARS[REGISTRY_SECRET_JSON]="$(indent_block '    ' "${REGISTRY_SECRET_JSON:-{\}}")"

    # Web 登录凭据
    VARS[WEB_AUTH_B64]="$(printf '%s' "$WEB_AUTH_USERS" | base64 | tr -d '\n')"

    # 读写权限映射:取 htpasswd 每行的用户名,只有 WEB_RW_USERS 里列出的才是 rw
    local rw_users="${WEB_RW_USERS:-kubedoor Up4biLko1dNh}"
    local map_lines="" u
    for u in $rw_users; do
        map_lines+="\"${u}\" \"rw\";"$'\n'
    done
    VARS[WEB_RW_USER_MAP]="$(indent_block '        ' "${map_lines%$'\n'}")"

    if [[ -n "${WEB_NODEPORT:-}" ]]; then
        VARS[WEB_NODEPORT_LINE]="      nodePort: ${WEB_NODEPORT}"
    else
        VARS[WEB_NODEPORT_LINE]=""
    fi

    VARS[GRAFANA_ADMIN_USER]="${GRAFANA_ADMIN_USER:-admin}"
    VARS[GRAFANA_ADMIN_PASSWORD]="${GRAFANA_ADMIN_PASSWORD:-admin}"

    VARS[VM_RETENTION]="${VM_RETENTION:-30d}"
    VARS[VM_STORAGE]="${VM_STORAGE:-100Gi}"
    VARS[VM_USER]="${VM_USER:-monit}"
    VARS[VM_PASSWORD]="${VM_PASSWORD:-}"
    if [[ -n "${VM_NODEPORT:-}" ]]; then
        VARS[VM_NODEPORT_LINE]="      nodePort: ${VM_NODEPORT}"
    else
        VARS[VM_NODEPORT_LINE]=""
    fi
    if [[ -n "${ALERTMANAGER_NODEPORT:-}" ]]; then
        VARS[ALERTMANAGER_NODEPORT_LINE]="      nodePort: ${ALERTMANAGER_NODEPORT}"
    else
        VARS[ALERTMANAGER_NODEPORT_LINE]=""
    fi
    if [[ -n "${STORAGE_CLASS:-}" ]]; then
        VARS[STORAGE_CLASS_LINE]="  storageClassName: ${STORAGE_CLASS}"
    else
        VARS[STORAGE_CLASS_LINE]=""
    fi

    VARS[IMAGE_GRAFANA]="${IMAGE_GRAFANA:-}"
    VARS[IMAGE_VICTORIA_METRICS]="${IMAGE_VICTORIA_METRICS:-}"
    VARS[IMAGE_VMALERT]="${IMAGE_VMALERT:-}"
    VARS[IMAGE_ALERTMANAGER]="${IMAGE_ALERTMANAGER:-}"
    VARS[IMAGE_BUSYBOX_CURL]="${IMAGE_BUSYBOX_CURL:-}"

    # 看板 ConfigMap 的 projected sources 列表,按 dashboards/ 下的文件动态生成
    local src="" f
    if [[ "${ENABLE_GRAFANA:-true}" == "true" ]]; then
        scan_dashboards
        for f in "${DASHBOARD_FILES[@]}"; do
            src+="- configMap:"$'\n'"    name: $(dashboard_cm_name "$f")"$'\n'
        done
    fi
    VARS[GRAFANA_DASH_SOURCES]="$(indent_block '              ' "${src%$'\n'}")"
}

# 看板文件 -> ConfigMap 名字。KubeDoor-Dash.json -> kubedoor-dash-dash
dashboard_cm_name() {
    local base
    base="$(basename "$1" .json | tr '[:upper:]' '[:lower:]')"
    printf 'kubedoor-dash-%s' "${base#kubedoor-}"
}

# 扫描 dashboards/ 目录,结果放进全局数组
declare -a DASHBOARD_FILES=()
scan_dashboards() {
    DASHBOARD_FILES=()
    local f
    while IFS= read -r f; do
        [[ -n "$f" ]] && DASHBOARD_FILES+=("$f")
    done < <(find "$DASHBOARD_DIR" -maxdepth 1 -name '*.json' | sort)
    [[ ${#DASHBOARD_FILES[@]} -gt 0 ]] || die "找不到看板文件:$DASHBOARD_DIR/*.json"
}

build_agent_vars() {
    build_common_vars

    VARS[TAG_AGENT]="${TAG_AGENT:-latest}"
    VARS[K8S_NAME]="$K8S_NAME"
    VARS[MASTER_WS]="$MASTER_WS"
    VARS[OSS_URL]="${OSS_URL:-}"

    # 没填远程写地址就按 master 在同集群拼一个
    local rw="${REMOTE_WRITE_URL:-}"
    if [[ -z "$rw" ]]; then
        rw="http://${VM_USER:-monit}:${VM_PASSWORD:-}@victoria-metrics.${NAMESPACE}:8428/api/v1/write"
    fi
    VARS[REMOTE_WRITE_URL]="$rw"

    VARS[IMAGE_VMAGENT]="${IMAGE_VMAGENT:-}"
    VARS[IMAGE_KUBE_STATE_METRICS]="${IMAGE_KUBE_STATE_METRICS:-}"
    VARS[IMAGE_NODE_EXPORTER]="${IMAGE_NODE_EXPORTER:-}"
}

# 把模板里的 __VAR__ 换成配置值。用 bash 参数替换,不走 sed,
# 所以密码里有 / & \ 这类字符也不会出问题。
render_file() {
    local src="$1"
    local content key
    content="$(cat "$src")"
    for key in "${!VARS[@]}"; do
        content="${content//__${key}__/${VARS[$key]}}"
    done

    # 组件关掉时,把模板里 BEGIN-xxx / END-xxx 包住的块删掉:
    #   GRAFANA / MCP —— nginx 里的反代块,不删的话 nginx 启动时解析不到 service 域名会直接起不来
    #   KSM           —— vmagent 只在本命名空间找 kube-state-metrics 的限制,
    #                    不自带 KSM 时要放开,才能抓到集群里已有的那个
    #   CRON-TZ       —— CronJob 的 timeZone,K8S 低于 1.25 时 check_k8s 会关掉
    if [[ "${ENABLE_GRAFANA:-true}" != "true" ]]; then
        content="$(printf '%s\n' "$content" | sed '/# BEGIN-GRAFANA/,/# END-GRAFANA/d')"
    fi
    if [[ "${ENABLE_MCP:-true}" != "true" ]]; then
        content="$(printf '%s\n' "$content" | sed '/# BEGIN-MCP/,/# END-MCP/d')"
    fi
    if [[ "${ENABLE_KUBE_STATE_METRICS:-true}" != "true" ]]; then
        content="$(printf '%s\n' "$content" | sed '/# BEGIN-KSM/,/# END-KSM/d')"
    fi
    if [[ "${CRON_TIMEZONE:-true}" != "true" ]]; then
        content="$(printf '%s\n' "$content" | sed '/# BEGIN-CRON-TZ/,/# END-CRON-TZ/d')"
    fi

    printf '%s\n' "$content"
}

# 按开关决定要部署哪些文件,顺序即 apply 顺序
master_manifests() {
    local d="$MANIFEST_DIR/master"
    # kubedoor-ai(AI 助手 + MCP)是必装组件
    printf '%s\n' "$d/00-namespace.yaml" "$d/05-ai-security.yaml" "$d/10-config.yaml" "$d/20-master.yaml" "$d/25-alarm.yaml" "$d/30-ai.yaml"
    [[ "${ENABLE_GRAFANA:-true}" == "true" ]] && printf '%s\n' "$d/35-grafana.yaml"
    printf '%s\n' "$d/40-web.yaml"
    [[ "${ENABLE_VICTORIA_METRICS:-true}" == "true" ]] && printf '%s\n' "$d/70-victoria-metrics.yaml"
    [[ "${ENABLE_VMALERT:-true}"          == "true" ]] && printf '%s\n' "$d/71-vmalert.yaml"
    [[ "${ENABLE_ALERTMANAGER:-true}"     == "true" ]] && printf '%s\n' "$d/72-alertmanager.yaml"
    return 0
}

agent_manifests() {
    local d="$MANIFEST_DIR/agent"
    printf '%s\n' "$d/00-namespace.yaml" "$d/10-agent.yaml"
    [[ "${ENABLE_VMAGENT:-true}"            == "true" ]] && printf '%s\n' "$d/20-vmagent.yaml"
    [[ "${ENABLE_KUBE_STATE_METRICS:-true}" == "true" ]] && printf '%s\n' "$d/21-kube-state-metrics.yaml"
    [[ "${ENABLE_NODE_EXPORTER:-true}"      == "true" ]] && printf '%s\n' "$d/22-node-exporter.yaml"
    return 0
}

# ---------------------------------------------------------------------------
# 从文件建 ConfigMap:先渲染占位符,再交给 kubectl create --from-file
#
# 这里必须用 server-side apply。普通 apply 会把整个对象再存一份到
# last-applied-configuration 注解里,注解总大小上限 256KB,
# KubeDoor-K8S.json 这种 200KB 以上的看板转义后一定超限。
# server-side apply 不写这个注解,只受 ConfigMap 本身 1MB 的限制。
# 用独立的 field-manager,API Server 也不会再去同步那个注解;
# --force-conflicts 让脚本始终以自己为准(包括接管以前用普通 apply 建的同名对象)。
# ---------------------------------------------------------------------------
apply_file_configmap() {
    local cm_name="$1"; shift
    local tmp; tmp="$(mktemp -d)"
    # shellcheck disable=SC2064
    trap "rm -rf '$tmp'" RETURN

    local f
    for f in "$@"; do
        render_file "$f" > "$tmp/$(basename "$f")"
    done

    kc create configmap "$cm_name" -n "$NAMESPACE" \
        --from-file="$tmp" --dry-run=client -o yaml \
        | kc apply --server-side --force-conflicts --field-manager=kubedoor-install -f - >/dev/null
    ok "ConfigMap ${cm_name}($# 个文件)"
}

# ---------------------------------------------------------------------------
# 部署
# ---------------------------------------------------------------------------
confirm() {
    [[ "$ASSUME_YES" == "true" ]] && return 0
    local reply
    read -r -p "$(printf '%s' "${C_BLD}继续?[y/N] ${C_RST}")" reply
    [[ "$reply" =~ ^[Yy]$ ]] || die "已取消"
}

apply_manifests() {
    local f
    while IFS= read -r f; do
        [[ -n "$f" ]] || continue
        render_file "$f" | kc apply -f - | sed 's/^/    /'
    done
}

wait_rollout() {
    local kind_name timeout="${2:-180s}"
    kind_name="$1"
    if kc -n "$NAMESPACE" get "$kind_name" >/dev/null 2>&1; then
        if kc -n "$NAMESPACE" rollout status "$kind_name" --timeout="$timeout" >/dev/null 2>&1; then
            ok "$kind_name 就绪"
        else
            warn "$kind_name 在 ${timeout} 内未就绪,用下面的命令看原因:"
            warn "    kubectl -n $NAMESPACE describe $kind_name"
            warn "    kubectl -n $NAMESPACE logs -l app=${kind_name#deploy/} --tail=50"
        fi
    fi
}

install_master() {
    validate_master
    check_k8s
    check_pg

    title "即将部署 KubeDoor 控制端"
    printf '  命名空间      : %s\n' "$NAMESPACE"
    printf '  数据库        : %s@%s:%s/%s\n' "$PG_USER" "$PG_HOST" "$PG_PORT" "$PG_DATABASE"
    printf '  镜像仓库      : %s\n' "$IMAGE_REPO"
    printf '  集群标签 key  : %s\n' "$PROM_K8S_TAG_KEY"
    printf '  组件          : master, alarm, web, ai'
    [[ "${ENABLE_MCP:-true}"              == "true" ]] && printf ', mcp'
    [[ "${ENABLE_GRAFANA:-true}"          == "true" ]] && printf ', grafana'
    [[ "${ENABLE_VICTORIA_METRICS:-true}" == "true" ]] && printf ', victoria-metrics'
    [[ "${ENABLE_VMALERT:-true}"          == "true" ]] && printf ', vmalert'
    [[ "${ENABLE_ALERTMANAGER:-true}"     == "true" ]] && printf ', alertmanager'
    printf '\n'
    if [[ "${ENABLE_VICTORIA_METRICS:-true}" != "true" ]]; then
        printf '  外部时序库    : %s\n' "$PROM_URL"
    fi
    printf '\n'
    confirm

    build_master_vars

    title "部署中"
    # 命名空间要先建好,后面的 ConfigMap 才有地方放
    render_file "$MANIFEST_DIR/master/00-namespace.yaml" | kc apply -f - | sed 's/^/    /'

    # K8S 事件告警规则
    apply_file_configmap kubedoor-master-file-cfg "$MANIFEST_DIR/master/alert_rules.json"

    # Grafana 看板:一个文件一个 ConfigMap
    if [[ "${ENABLE_GRAFANA:-true}" == "true" ]]; then
        local f
        for f in "${DASHBOARD_FILES[@]}"; do
            apply_file_configmap "$(dashboard_cm_name "$f")" "$f"
        done
    fi

    master_manifests | apply_manifests

    title "等待组件就绪"
    wait_rollout deploy/kubedoor-master 240s
    wait_rollout deploy/kubedoor-alarm
    wait_rollout deploy/kubedoor-ai
    [[ "${ENABLE_GRAFANA:-true}"          == "true" ]] && wait_rollout deploy/kubedoor-dash
    wait_rollout deploy/kubedoor-web
    [[ "${ENABLE_VICTORIA_METRICS:-true}" == "true" ]] && wait_rollout deploy/victoria-metrics
    [[ "${ENABLE_VMALERT:-true}"          == "true" ]] && wait_rollout deploy/vmalert
    [[ "${ENABLE_ALERTMANAGER:-true}"     == "true" ]] && wait_rollout deploy/alertmanager

    print_master_access
}

install_agent() {
    validate_agent
    check_k8s

    title "即将部署 KubeDoor 集群端"
    printf '  命名空间    : %s\n' "$NAMESPACE"
    printf '  本集群名字  : %s\n' "$K8S_NAME"
    printf '  master 地址 : %s\n' "$MASTER_WS"
    printf '  组件        : agent'
    [[ "${ENABLE_VMAGENT:-true}"            == "true" ]] && printf ', vmagent'
    [[ "${ENABLE_KUBE_STATE_METRICS:-true}" == "true" ]] && printf ', kube-state-metrics'
    [[ "${ENABLE_NODE_EXPORTER:-true}"      == "true" ]] && printf ', node-exporter'
    printf '\n'
    if [[ "${ENABLE_VMAGENT:-true}" == "true" ]]; then
        printf '  远程写地址  : %s\n' "${REMOTE_WRITE_URL:-<自动拼接为集群内地址>}"
    fi
    printf '\n'
    warn "agent 会被授予集群管理员级别的权限(ClusterRole: *),用于代理执行扩缩容、改镜像等操作"
    printf '\n'
    confirm

    build_agent_vars

    title "部署中"
    agent_manifests | apply_manifests

    title "等待组件就绪"
    wait_rollout deploy/kubedoor-agent
    [[ "${ENABLE_VMAGENT:-true}"            == "true" ]] && wait_rollout deploy/vmagent
    [[ "${ENABLE_KUBE_STATE_METRICS:-true}" == "true" ]] && wait_rollout deploy/kube-state-metrics

    print_agent_access
}

# ---------------------------------------------------------------------------
# 安装后信息
# ---------------------------------------------------------------------------
node_ip() {
    local ip
    ip="$(kc get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="ExternalIP")].address}' 2>/dev/null || true)"
    [[ -z "$ip" ]] && ip="$(kc get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}' 2>/dev/null || true)"
    printf '%s' "${ip:-<节点IP>}"
}

svc_nodeport() {
    kc -n "$NAMESPACE" get svc "$1" -o jsonpath='{.spec.ports[0].nodePort}' 2>/dev/null || true
}

print_master_access() {
    local ip port
    ip="$(node_ip)"
    port="$(svc_nodeport kubedoor-web)"

    title "控制端安装完成"
    printf '  Web 控制台 : %s\n' "${C_BLD}http://${ip}:${port:-<NodePort>}${C_RST}"
    printf '  默认账号   : kubedoor / kubedoor(读写)\n'
    printf '  其它账号   : 默认只读,可在 kubedoor.conf 的 WEB_AUTH_USERS / WEB_RW_USERS 里调整\n'
    if [[ "${ENABLE_GRAFANA:-true}" == "true" ]]; then
        printf '  Grafana    : http://%s:%s/grafana/\n' "$ip" "${port:-<NodePort>}"
    fi
    if [[ "${ENABLE_ALERTMANAGER:-true}" == "true" ]]; then
        printf '  Alertmanager: http://%s:%s\n' "$ip" "$(svc_nodeport alertmanager)"
    fi
    if [[ "${ENABLE_VICTORIA_METRICS:-true}" == "true" ]]; then
        local vmport; vmport="$(svc_nodeport victoria-metrics)"
        printf '\n  时序库(供其它集群的 agent 远程写):\n'
        printf '    REMOTE_WRITE_URL="http://%s:%s@%s:%s/api/v1/write"\n' \
            "${VM_USER}" "${VM_PASSWORD}" "$ip" "$vmport"
    fi
    printf '\n  下一步:到各个业务集群上执行 ./install.sh agent\n'
    printf '    同集群   MASTER_WS="ws://kubedoor-master.%s"\n' "$NAMESPACE"
    printf '    跨集群   MASTER_WS="ws://Up4biLko1dNh:qCa22jDkfc9y@%s:%s"\n\n' "$ip" "${port:-<NodePort>}"
}

print_agent_access() {
    title "集群端安装完成"
    printf '  集群名字  : %s\n' "$K8S_NAME"
    printf '  连接状态  : kubectl -n %s logs -l app=kubedoor-agent --tail=30\n' "$NAMESPACE"
    printf '\n  到 Web 控制台的「agent 管理」里应该能看到 %s 已上线。\n' "$K8S_NAME"
    printf '  没看到的话检查 MASTER_WS 是否可达、跨集群时账号密码是否正确。\n\n'
}

# ---------------------------------------------------------------------------
# 其它子命令
# ---------------------------------------------------------------------------
do_render() {
    local role="$1"
    case "$role" in
        master) validate_master; build_master_vars ;;
        agent)  validate_agent;  build_agent_vars  ;;
        *) die "render 后面要跟 master 或 agent" ;;
    esac

    local list f
    if [[ "$role" == "master" ]]; then
        list="$(master_manifests)"
    else
        list="$(agent_manifests)"
    fi

    if [[ -n "$OUTPUT_DIR" ]]; then
        mkdir -p "$OUTPUT_DIR"
        while IFS= read -r f; do
            [[ -n "$f" ]] || continue
            render_file "$f" > "$OUTPUT_DIR/$(basename "$f")"
        done <<< "$list"
        # 从文件建的 ConfigMap 也一并渲染出来,方便检查
        if [[ "$role" == "master" ]]; then
            render_file "$MANIFEST_DIR/master/alert_rules.json" > "$OUTPUT_DIR/alert_rules.json"
        fi
        ok "已渲染到:$OUTPUT_DIR"
    else
        while IFS= read -r f; do
            [[ -n "$f" ]] || continue
            printf '# ===== %s =====\n' "$(basename "$f")"
            render_file "$f"
            printf -- '---\n'
        done <<< "$list"
    fi
}

do_status() {
    check_k8s
    title "命名空间 $NAMESPACE"
    kc -n "$NAMESPACE" get deploy,ds,sts,svc,cronjob 2>/dev/null || warn "命名空间不存在或没有资源"
    title "Pod"
    kc -n "$NAMESPACE" get pods -o wide 2>/dev/null || true
    printf '\n'
}

do_uninstall() {
    local role="$1"
    local f cms
    check_k8s

    title "即将卸载 KubeDoor ${role} 端"
    printf '  命名空间:%s\n' "$NAMESPACE"
    if [[ "$role" == "master" ]]; then
        warn "PostgreSQL 是独立部署的,不会被删除,数据都还在"
        warn "VictoriaMetrics 的 PVC 也会一并删除,监控历史数据会丢失"
    fi
    printf '\n'
    confirm

    if [[ "$role" == "master" ]]; then
        build_master_vars
        master_manifests | while IFS= read -r f; do
            [[ -n "$f" ]] || continue
            render_file "$f" | kc delete -f - --ignore-not-found 2>/dev/null | sed 's/^/    /' || true
        done
        local cms=(kubedoor-master-file-cfg)
        if [[ "${ENABLE_GRAFANA:-true}" == "true" ]]; then
            for f in "${DASHBOARD_FILES[@]}"; do cms+=("$(dashboard_cm_name "$f")"); done
        fi
        kc -n "$NAMESPACE" delete configmap "${cms[@]}" \
            --ignore-not-found 2>/dev/null | sed 's/^/    /' || true
    else
        build_agent_vars
        agent_manifests | while IFS= read -r f; do
            [[ -n "$f" ]] || continue
            render_file "$f" | kc delete -f - --ignore-not-found 2>/dev/null | sed 's/^/    /' || true
        done
    fi

    ok "卸载完成。命名空间 $NAMESPACE 本身保留了,要删的话:"
    printf '    kubectl delete namespace %s\n\n' "$NAMESPACE"
}

usage() {
    sed -n '3,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
main() {
    local args=()
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -c|--config) CONFIG_FILE="$2"; shift 2 ;;
            -o|--output) OUTPUT_DIR="$2";  shift 2 ;;
            -y|--yes)    ASSUME_YES="true"; shift ;;
            -h|--help)   usage; exit 0 ;;
            -*)          die "未知参数:$1(用 -h 看帮助)" ;;
            *)           args+=("$1"); shift ;;
        esac
    done

    [[ ${#args[@]} -gt 0 ]] || { usage; exit 1; }

    check_prereq
    load_config

    case "${args[0]}" in
        master)    install_master ;;
        agent)     install_agent ;;
        render)    do_render "${args[1]:-}" ;;
        status)    do_status ;;
        uninstall)
            case "${args[1]:-}" in
                master|agent) do_uninstall "${args[1]}" ;;
                *) die "uninstall 后面要跟 master 或 agent" ;;
            esac ;;
        *) die "未知命令:${args[0]}(用 -h 看帮助)" ;;
    esac
}

main "$@"
