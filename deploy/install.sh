#!/bin/sh
# KubeDoor 网页安装入口：单文件运行，仅需 POSIX sh 和 kubectl。
set -eu

# 修改以下镜像后再首次安装；--tag 可覆盖 KubeDoor 组件版本。
# BEGIN KUBEDOOR IMAGE DEFAULTS
IMAGE_REPO="swr.cn-south-1.myhuaweicloud.com/starsl.cn"
TAG_MASTER="2.5.0"
TAG_AGENT="2.5.0"
TAG_WEB="2.5.0"
TAG_AI="2.5.0"
TAG_SETUP="2.5.0"
TAG_ALARM="2.5.0"
IMAGE_GRAFANA="registry.cn-shenzhen.aliyuncs.com/starsl/grafana-oss:11.4.0"
IMAGE_VMAGENT="registry.cn-shenzhen.aliyuncs.com/starsl/vmagent"
IMAGE_VMALERT="registry.cn-shenzhen.aliyuncs.com/starsl/vmalert"
IMAGE_ALERTMANAGER="registry.cn-shenzhen.aliyuncs.com/starsl/alertmanager"
IMAGE_KUBE_STATE_METRICS="registry.cn-shenzhen.aliyuncs.com/starsl/kube-state-metrics:v2.14.0"
IMAGE_NODE_EXPORTER="registry.cn-shenzhen.aliyuncs.com/starsl/node-exporter"
IMAGE_BUSYBOX_CURL="registry.cn-shenzhen.aliyuncs.com/starsl/busybox-curl"
# END KUBEDOOR IMAGE DEFAULTS

NAMESPACE=kubedoor
KUBE_CONTEXT=
IMAGE_PULL_SECRET=
WEB_NODEPORT=
TAG_OVERRIDE=
SETUP_TAG_OVERRIDE=
WEB_TAG_OVERRIDE=
RENDER=false
EXPORT_FILE=
EXPORT_MODE=false
ASSUME_YES=false
OWNER='setup.kubedoor.io/installation-id'

die() { printf '%s\n' "$*" >&2; exit 1; }
usage() {
    printf '%s\n' \
        '用法：sh install.sh [web] [选项]（也支持 bash）' \
        '  --namespace NAME       命名空间，默认 kubedoor' \
        '  --context NAME         kubectl context，默认当前 context' \
        '  --image-repo REPO      KubeDoor 镜像仓库' \
        '  --tag VERSION          统一设置 setup/web/master/agent/ai/alarm 版本' \
        '  --setup-tag VERSION    单独指定 setup 版本' \
        '  --web-tag VERSION      单独指定 web 版本' \
        '  --image-pull-secret NAME  命名空间内已有的镜像拉取 Secret' \
        '  --nodeport PORT        Web NodePort，默认由集群分配' \
        '  -y, --yes              跳过部署位置确认' \
        '  --render               仅预览模板；可安装文件请用 --export' \
        '  --export FILE          导出可直接 kubectl apply 的 YAML；- 表示标准输出' \
        '  -h, --help             显示帮助' \
        '数据库及管理员信息在网页填写。重复运行保留原安装，不用于升级。'
}
need_value() { [ "$#" -ge 2 ] && [ -n "$2" ] || die "参数 $1 缺少值"; }
while [ "$#" -gt 0 ]; do
    case "$1" in
        web) shift ;;
        --namespace) need_value "$@"; NAMESPACE=$2; shift 2 ;;
        --context) need_value "$@"; KUBE_CONTEXT=$2; shift 2 ;;
        --image-repo) need_value "$@"; IMAGE_REPO=$2; shift 2 ;;
        --tag) need_value "$@"; TAG_OVERRIDE=$2; shift 2 ;;
        --setup-tag) need_value "$@"; SETUP_TAG_OVERRIDE=$2; shift 2 ;;
        --web-tag) need_value "$@"; WEB_TAG_OVERRIDE=$2; shift 2 ;;
        --image-pull-secret) need_value "$@"; IMAGE_PULL_SECRET=$2; shift 2 ;;
        --nodeport) need_value "$@"; WEB_NODEPORT=$2; shift 2 ;;
        -y|--yes) ASSUME_YES=true; shift ;;
        --render) RENDER=true; shift ;;
        --export) need_value "$@"; EXPORT_FILE=$2; EXPORT_MODE=true; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) die "未知参数：$1；用 --help 查看用法" ;;
    esac
done
[ "$RENDER" != true ] || [ "$EXPORT_MODE" != true ] || die '--render 和 --export 不能同时使用'

validate_name() {
    case "$1" in ''|*[!a-z0-9-]*|-*|*-) die "无效的名称：$1" ;; esac
    [ "${#1}" -le 63 ] || die '名称不能超过 63 个字符'
}
validate_image() {
    case "$1" in ''|*[!A-Za-z0-9._:/@-]*) die '镜像地址包含不支持的字符' ;; esac
}
validate_tag() {
    case "$1" in ''|[.-]*|*[!A-Za-z0-9_.-]*) die '镜像版本格式无效' ;; esac
    [ "${#1}" -le 128 ] || die '镜像版本不能超过 128 个字符'
}
validate_name "$NAMESPACE"
if [ -n "$IMAGE_PULL_SECRET" ]; then
    case "$IMAGE_PULL_SECRET" in *[!a-z0-9.-]*|[.-]*|*[.-]) die '镜像拉取 Secret 名称无效' ;; esac
fi
if [ -n "$WEB_NODEPORT" ]; then
    case "$WEB_NODEPORT" in *[!0-9]*|0*) die 'NodePort 必须是 1 到 65535 的整数' ;; esac
    [ "${#WEB_NODEPORT}" -le 5 ] && [ "$WEB_NODEPORT" -le 65535 ] || die 'NodePort 超出范围'
fi
IMAGE_REPO=${IMAGE_REPO%/}
validate_image "$IMAGE_REPO"
if [ -n "$TAG_OVERRIDE" ]; then
    TAG_SETUP=$TAG_OVERRIDE; TAG_WEB=$TAG_OVERRIDE; TAG_MASTER=$TAG_OVERRIDE
    TAG_AGENT=$TAG_OVERRIDE; TAG_AI=$TAG_OVERRIDE; TAG_ALARM=$TAG_OVERRIDE
fi
[ -z "$SETUP_TAG_OVERRIDE" ] || TAG_SETUP=$SETUP_TAG_OVERRIDE
[ -z "$WEB_TAG_OVERRIDE" ] || TAG_WEB=$WEB_TAG_OVERRIDE
for version in "$TAG_SETUP" "$TAG_WEB" "$TAG_MASTER" "$TAG_AGENT" "$TAG_AI" "$TAG_ALARM"; do validate_tag "$version"; done
for image in "$IMAGE_GRAFANA" "$IMAGE_VMAGENT" "$IMAGE_VMALERT" "$IMAGE_ALERTMANAGER" "$IMAGE_KUBE_STATE_METRICS" "$IMAGE_NODE_EXPORTER" "$IMAGE_BUSYBOX_CURL"; do validate_image "$image"; done
SETUP_IMAGE=$IMAGE_REPO/kubedoor-setup:$TAG_SETUP
WEB_IMAGE=$IMAGE_REPO/kubedoor-web:$TAG_WEB
DEFAULTS="{\"IMAGE_REPO\":\"$IMAGE_REPO\",\"TAG_SETUP\":\"$TAG_SETUP\",\"TAG_WEB\":\"$TAG_WEB\",\"TAG_MASTER\":\"$TAG_MASTER\",\"TAG_AGENT\":\"$TAG_AGENT\",\"TAG_AI\":\"$TAG_AI\",\"TAG_ALARM\":\"$TAG_ALARM\",\"IMAGE_GRAFANA\":\"$IMAGE_GRAFANA\",\"IMAGE_VMAGENT\":\"$IMAGE_VMAGENT\",\"IMAGE_VMALERT\":\"$IMAGE_VMALERT\",\"IMAGE_ALERTMANAGER\":\"$IMAGE_ALERTMANAGER\",\"IMAGE_KUBE_STATE_METRICS\":\"$IMAGE_KUBE_STATE_METRICS\",\"IMAGE_NODE_EXPORTER\":\"$IMAGE_NODE_EXPORTER\",\"IMAGE_BUSYBOX_CURL\":\"$IMAGE_BUSYBOX_CURL\",\"IMAGE_PULL_SECRET\":\"$IMAGE_PULL_SECRET\",\"WEB_NODEPORT\":\"$WEB_NODEPORT\"}"

# 使用 shell 内建输出 heredoc，不依赖 cat、envsubst、jq 或本机 Python。
yaml() { while IFS= read -r yaml_line || [ -n "$yaml_line" ]; do printf '%s\n' "$yaml_line"; done; }
kc() {
    if [ -n "$KUBE_CONTEXT" ]; then
        command kubectl --context "$KUBE_CONTEXT" "$@"
    else
        command kubectl "$@"
    fi
}
field() { kc -n "$NAMESPACE" get "$1" "$2" --ignore-not-found -o "jsonpath=$3"; }
metadata() {
    yaml <<EOF
metadata:
  name: $1
  namespace: '$NAMESPACE'
  labels:
    app.kubernetes.io/part-of: kubedoor
    app.kubernetes.io/managed-by: kubedoor-setup
  annotations:
    $OWNER: '$INSTALL_ID'
EOF
}
cluster_metadata() {
    yaml <<EOF
metadata:
  name: '$1'
  labels:
    app.kubernetes.io/part-of: kubedoor
    app.kubernetes.io/managed-by: kubedoor-setup
  annotations:
    $OWNER: '$INSTALL_ID'
EOF
}
pull_secret() {
    [ -z "$IMAGE_PULL_SECRET" ] || printf "      imagePullSecrets:\n        - name: '%s'\n" "$IMAGE_PULL_SECRET"
}
resource_yaml() {
    case "$1" in
        ServiceAccount)
            printf 'apiVersion: v1\nkind: ServiceAccount\n'; metadata "$2" ;;
        RoleBinding)
            printf 'apiVersion: rbac.authorization.k8s.io/v1\nkind: RoleBinding\n'; metadata "$2"
            yaml <<EOF
subjects:
  - kind: ServiceAccount
    name: $2
    namespace: '$NAMESPACE'
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: $2
EOF
            ;;
        ClusterRoleBinding)
            # 只给专用安装账号：安装服务仅在网页第 6 步选择在本集群安装 Agent 时签发短期令牌使用，收尾时删除
            printf 'apiVersion: rbac.authorization.k8s.io/v1\nkind: ClusterRoleBinding\n'; cluster_metadata "$2"
            yaml <<EOF
subjects:
  - kind: ServiceAccount
    name: $AGENT_INSTALLER
    namespace: '$NAMESPACE'
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: cluster-admin
EOF
            ;;
        Role)
            printf 'apiVersion: rbac.authorization.k8s.io/v1\nkind: Role\n'; metadata "$2"
            if [ "$2" = kubedoor-setup ]; then
                yaml <<'EOF'
rules:
  - apiGroups: ['']
    resources: [configmaps, secrets, services]
    verbs: [get, list, watch, create, patch, update, delete]
  - apiGroups: ['']
    resources: [pods, pods/log, events]
    verbs: [get, list, watch]
  - apiGroups: [apps]
    resources: [deployments, replicasets]
    verbs: [get, list, watch, create, patch, update]
  - apiGroups: [batch]
    resources: [jobs, cronjobs]
    verbs: [get, list, watch, create, patch, update, delete]
  - apiGroups: ['']
    resources: [serviceaccounts/token]
    resourceNames: [kubedoor-agent-installer]
    verbs: [create]
EOF
            else
                yaml <<'EOF'
rules:
  - apiGroups: [apps]
    resources: [deployments]
    resourceNames: [kubedoor-setup]
    verbs: [get, patch]
  - apiGroups: ['']
    resources: [configmaps]
    resourceNames: [kubedoor-setup-state]
    verbs: [get, patch, update]
  - apiGroups: [rbac.authorization.k8s.io]
    resources: [rolebindings]
    resourceNames: [kubedoor-setup]
    verbs: [get, delete]
  - apiGroups: ['']
    resources: [secrets]
    resourceNames: [kubedoor-setup-auth, kubedoor-setup-config]
    verbs: [get, delete]
  - apiGroups: ['']
    resources: [secrets]
    resourceNames: [kubedoor-auth-init]
    verbs: [get, update]
  - apiGroups: ['']
    resources: [serviceaccounts]
    resourceNames: [kubedoor-agent-installer]
    verbs: [get, delete]
  - apiGroups: ['']
    resources: [serviceaccounts/token]
    resourceNames: [kubedoor-agent-installer]
    verbs: [create]
EOF
            fi ;;
        ConfigMap)
            printf 'apiVersion: v1\nkind: ConfigMap\n'; metadata nginx-config
            yaml <<'EOF'
data:
  nginx.conf: |
    user nginx;
    worker_processes auto;
    events { worker_connections 1024; }
    http {
        include /etc/nginx/mime.types;
        default_type application/octet-stream;
        sendfile on;
        server {
            listen 80;
            root /www/dist;
            location / { try_files $uri $uri/ /index.html; }
            location = /healthz { access_log off; return 200 'ok'; }
            location = /setup-status.json {
                alias /etc/kubedoor-setup/status.json;
                default_type application/json;
                add_header Cache-Control "no-store" always;
                access_log off;
            }
            location ^~ /setup-api/ {
                proxy_pass http://kubedoor-setup:8080;
                proxy_http_version 1.1;
                proxy_set_header Host $http_host;
                proxy_set_header X-Forwarded-Proto $scheme;
                proxy_set_header X-Forwarded-For $remote_addr;
                proxy_read_timeout 180;
                client_max_body_size 256k;
                access_log off;
            }
        }
    }
EOF
            ;;
        Deployment)
            printf 'apiVersion: apps/v1\nkind: Deployment\n'; metadata "$2"
            yaml <<EOF
spec:
  replicas: 1
  strategy: {type: Recreate}
  selector:
    matchLabels: {app: $2}
  template:
    metadata:
      labels: {app: $2, app.kubernetes.io/part-of: kubedoor, app.kubernetes.io/managed-by: kubedoor-setup}
    spec:
EOF
            pull_secret
            if [ "$2" = kubedoor-setup ]; then
                yaml <<EOF
      serviceAccountName: kubedoor-setup
      automountServiceAccountToken: true
      containers:
        - name: kubedoor-setup
          image: '$SETUP_IMAGE'
          imagePullPolicy: Always
          ports: [{name: http, containerPort: 8080}]
          resources: {requests: {cpu: 10m, memory: 64Mi}, limits: {cpu: '1', memory: 512Mi}}
          readinessProbe: {httpGet: {path: /health, port: 8080}, periodSeconds: 5}
          securityContext: {allowPrivilegeEscalation: false, capabilities: {drop: [ALL]}}
          env:
            - {name: SETUP_NAMESPACE, value: '$NAMESPACE'}
            - {name: SETUP_IMAGE, value: '$SETUP_IMAGE'}
            - {name: SETUP_DEFAULTS, value: '$DEFAULTS'}
            - {name: SETUP_IMAGE_REPO, value: '$IMAGE_REPO'}
            - {name: SETUP_WEB_NODEPORT, value: '$WEB_NODEPORT'}
EOF
            else
                yaml <<EOF
      automountServiceAccountToken: false
      containers:
        - name: kubedoor-web
          image: '$WEB_IMAGE'
          imagePullPolicy: Always
          ports: [{name: http, containerPort: 80}]
          resources: {requests: {cpu: 10m, memory: 64Mi}, limits: {cpu: '1', memory: 512Mi}}
          readinessProbe: {httpGet: {path: /healthz, port: 80}, periodSeconds: 5}
          volumeMounts:
            - {name: nginx-config, mountPath: /etc/nginx/nginx.conf, subPath: nginx.conf, readOnly: true}
            - {name: setup-status, mountPath: /etc/kubedoor-setup, readOnly: true}
      volumes:
        - name: nginx-config
          configMap: {name: nginx-config}
        - name: setup-status
          configMap:
            name: kubedoor-setup-state
            items: [{key: status.json, path: status.json}]
EOF
            fi ;;
        Service)
            printf 'apiVersion: v1\nkind: Service\n'; metadata "$2"
            if [ "$2" = kubedoor-web ]; then
                yaml <<EOF
spec:
  type: NodePort
  selector: {app: kubedoor-web}
  ports:
    - {name: http, port: 80, targetPort: 80${WEB_NODEPORT:+, nodePort: $WEB_NODEPORT}}
EOF
            else
                yaml <<'EOF'
spec:
  type: ClusterIP
  selector: {app: kubedoor-setup}
  ports: [{name: http, port: 8080, targetPort: 8080}]
EOF
            fi ;;
    esac
}
state_yaml() {
    yaml <<EOF
apiVersion: v1
kind: ConfigMap
metadata:
  name: kubedoor-setup-state
  namespace: '$NAMESPACE'
  labels:
    app.kubernetes.io/part-of: kubedoor
    app.kubernetes.io/managed-by: kubedoor-setup
EOF
    if [ "$EXPORT_MODE" = true ]; then
        printf "  annotations:\n    %s: '%s'\n" "$OWNER" "$INSTALL_ID"
    fi
    printf 'data:\n'
    if [ "$EXPORT_MODE" != true ]; then
        # 导出文件不管理运行状态字段，后续 apply 不会把安装进度重置为空。
        yaml <<'EOF'
  state.json: '{}'
  status.json: '{"phase":"draft"}'
  phase: draft
EOF
    fi
    yaml <<EOF
  bootstrap-defaults: '$DEFAULTS'
  setup-image: '$SETUP_IMAGE'
  web-image: '$WEB_IMAGE'
  image-pull-secret: '$IMAGE_PULL_SECRET'
  requested-nodeport: '$WEB_NODEPORT'
  agent-installer: '$AGENT_BINDING'
EOF
}
namespace_yaml() { printf "apiVersion: v1\nkind: Namespace\nmetadata:\n  name: '%s'\n" "$NAMESPACE"; }
RESOURCES='ServiceAccount:kubedoor-setup ServiceAccount:kubedoor-setup-finalizer Role:kubedoor-setup Role:kubedoor-setup-finalizer RoleBinding:kubedoor-setup RoleBinding:kubedoor-setup-finalizer ConfigMap:nginx-config Service:kubedoor-setup Service:kubedoor-web Deployment:kubedoor-setup Deployment:kubedoor-web'
# 在本集群安装 Agent 的临时集群权限：专用账号绑定 cluster-admin，安装服务只在网页第 6 步选择安装时
# 签发短期令牌使用，收尾时先删绑定再删账号。状态 ConfigMap 的 agent-installer 标记说明本次引导包含它们，
# 旧版本引导的安装服务不会撤销，所以重新运行时没有标记就不补建。
AGENT_INSTALLER=kubedoor-agent-installer
AGENT_BINDING=$AGENT_INSTALLER-$NAMESPACE
AGENT_RESOURCES="ServiceAccount:$AGENT_INSTALLER ClusterRoleBinding:$AGENT_BINDING"

write_bundle() {
    if [ "$EXPORT_MODE" = true ]; then
        yaml <<EOF
# KubeDoor 网页安装入口；仅用于全新安装，每套实例请单独导出。
# kubectl apply -f <本文件>
# 不要用此文件升级或恢复已有实例：重新 apply 会恢复 Web 引导配置，并重新授予下面的临时集群权限。
# 包含 ClusterRoleBinding $AGENT_BINDING（cluster-admin）：仅在网页第 6 步选择在本集群安装 Agent 时使用，安装收尾时删除。
# 如指定了 --context，请在 kubectl 命令中使用相同 context。
# 等待入口就绪：
# kubectl -n $NAMESPACE rollout status deployment/kubedoor-setup --timeout=240s
# kubectl -n $NAMESPACE rollout status deployment/kubedoor-web --timeout=240s
# 查询端口：kubectl -n $NAMESPACE get service kubedoor-web
# 获取一次性安装密钥：
# kubectl -n $NAMESPACE get secret kubedoor-setup-auth -o 'go-template={{index .data "SETUP_KEY" | base64decode}}'
EOF
    fi
    namespace_yaml; printf '%s\n' '---'; state_yaml
    for item in $RESOURCES $AGENT_RESOURCES; do
        printf '%s\n' '---'; resource_yaml "${item%%:*}" "${item#*:}"
    done
}
export_id() (
    # 只用 shell 内建从系统随机设备取 128 bit 标识；标识不含任何安装凭据。
    # 在子 shell 使用字节模式，不改变调用者 locale。
    LC_ALL=C
    export LC_ALL
    [ -r /dev/urandom ] || die '导出需要系统随机设备 /dev/urandom，请在 Linux、macOS 或 Git Bash 中运行'
    exec 3</dev/urandom
    random_id=
    while [ "${#random_id}" -lt 32 ]; do
        IFS= read -r random_line <&3 || die '无法读取系统随机设备'
        while [ -n "$random_line" ] && [ "${#random_id}" -lt 32 ]; do
            random_char=${random_line%"${random_line#?}"}
            random_line=${random_line#?}
            case "$random_char" in [a-f0-9]) random_id=$random_id$random_char ;; esac
        done
    done
    exec 3<&-
    printf 'yaml-%s\n' "$random_id"
)
if [ "$EXPORT_MODE" = true ]; then
    INSTALL_ID=$(export_id)
    if [ "$EXPORT_FILE" = - ]; then
        write_bundle
    else
        # 不覆盖用户已有文件，包括正在运行的安装脚本。
        (set -C; write_bundle > "$EXPORT_FILE") || die '导出失败：文件已存在或目录不可写，请换一个文件名'
        printf '已导出：%s\n' "$EXPORT_FILE"
        printf '%s\n' '在目标集群执行 kubectl apply -f <导出的文件>，等待 Web 和 setup 就绪。'
    fi
    exit 0
fi
if [ "$RENDER" = true ]; then
    INSTALL_ID=preview
    printf '%s\n' '# 仅预览；如需直接 kubectl apply，请使用 sh install.sh --export kubedoor.yaml。'
    write_bundle
    exit 0
fi
command -v kubectl >/dev/null 2>&1 || die '找不到 kubectl，请先安装并配置目标集群'

read_phase() {
    PHASE=$(field ConfigMap kubedoor-setup-state '{.data.phase}')
    if [ -z "$PHASE" ]; then
        # 兼容早期安装服务只保存 JSON 的状态；未知格式必须停止，不能重建。
        status_json=$(field ConfigMap kubedoor-setup-state '{.data.status\.json}')
        case "$status_json" in
            *'"phase":"succeeded"'*|*'"phase": "succeeded"'*) PHASE=succeeded ;;
            *'"phase":"finalizing"'*|*'"phase": "finalizing"'*) PHASE=finalizing ;;
            *'"phase":"draft"'*|*'"phase": "draft"'*) PHASE=draft ;;
            *'"phase":"ready"'*|*'"phase": "ready"'*) PHASE=ready ;;
            *'"phase":"running"'*|*'"phase": "running"'*) PHASE=running ;;
            *'"phase":"failed"'*|*'"phase": "failed"'*) PHASE=failed ;;
            *'"phase":"interrupted"'*|*'"phase": "interrupted"'*) PHASE=interrupted ;;
            *'"phase":"agent"'*|*'"phase": "agent"'*) PHASE=agent ;;
            *) die '无法识别已有安装状态，停止修改资源' ;;
        esac
    fi
    case "$PHASE" in draft|ready|running|failed|interrupted|agent|finalizing|succeeded) ;; *) die '已有安装状态无效' ;; esac
}
require_owner() {
    resource_id=$(field "$1" "$2" '{.metadata.annotations.setup\.kubedoor\.io/installation-id}')
    [ "$resource_id" = "$INSTALL_ID" ] || die "已有 $1/$2 不属于本次安装，停止覆盖"
}
print_access() {
    address=$(field ConfigMap kubedoor-setup-state '{.data.public-url}')
    if [ -z "$address" ]; then
        port=$(field Service kubedoor-web '{.spec.ports[0].nodePort}')
        host=$(kc get nodes -o 'jsonpath={.items[0].status.addresses[?(@.type=="ExternalIP")].address}' 2>/dev/null) || host=
        if [ -z "$host" ]; then
            host=$(kc get nodes -o 'jsonpath={.items[0].status.addresses[?(@.type=="InternalIP")].address}' 2>/dev/null) || host=
        fi
        case "$host" in *:*) host="[$host]" ;; esac
        address="http://${host:-<K8S节点地址>}:${port:-<NodePort>}"
    fi
    printf '\nWeb 入口：%s\n' "$address"
}
finalizer_yaml() {
    printf 'apiVersion: batch/v1\nkind: Job\n'; metadata kubedoor-setup-finalize
    yaml <<EOF
spec:
  activeDeadlineSeconds: 600
  backoffLimit: 6
  template:
    metadata:
      labels: {app: kubedoor-setup-finalize}
      annotations: {$OWNER: '$INSTALL_ID'}
    spec:
      restartPolicy: OnFailure
      serviceAccountName: kubedoor-setup-finalizer
      automountServiceAccountToken: true
      containers:
        - name: finalize
          image: '$SETUP_IMAGE'
          command: [python, -m, kubedoor_setup.finalize]
          env:
            - {name: SETUP_NAMESPACE, value: '$NAMESPACE'}
            - {name: SETUP_INSTALLATION_ID, value: '$INSTALL_ID'}
EOF
    if [ -n "$FINALIZER_PULL_SECRETS" ]; then
        printf '      imagePullSecrets:\n'
        for secret in $FINALIZER_PULL_SECRETS; do printf "        - name: '%s'\n" "$secret"; done
    fi
}
recover_finalizer() {
    require_owner Deployment kubedoor-setup
    SETUP_IMAGE=$(field Deployment kubedoor-setup '{.spec.template.spec.containers[?(@.name=="kubedoor-setup")].image}')
    validate_image "$SETUP_IMAGE"
    FINALIZER_PULL_SECRETS=$(field Deployment kubedoor-setup '{.spec.template.spec.imagePullSecrets[*].name}')
    job_uid=$(field Job kubedoor-setup-finalize '{.metadata.uid}')
    if [ -n "$job_uid" ]; then
        require_owner Job kubedoor-setup-finalize
        job_failed=$(field Job kubedoor-setup-finalize '{.status.conditions[?(@.type=="Failed")].status}')
        if [ "$job_failed" != True ]; then
            printf '%s\n' '收尾任务已存在，保留当前进度。'; return
        fi
        kc -n "$NAMESPACE" delete job kubedoor-setup-finalize --cascade=foreground --wait=true --timeout=45s
    fi
    require_owner ConfigMap kubedoor-setup-state
    read_phase
    [ "$PHASE" != succeeded ] || return 0
    [ "$PHASE" = finalizing ] || die '安装状态已改变，停止恢复收尾任务'
    finalizer_yaml | kc -n "$NAMESPACE" create -f -
    printf '%s\n' '已恢复收尾任务；请在网页等待全部检查完成。'
}

state_uid=$(field ConfigMap kubedoor-setup-state '{.metadata.uid}')
if [ -z "$state_uid" ]; then
    existing_uid=$(field Deployment kubedoor-master '{.metadata.uid}')
    [ -z "$existing_uid" ] || die '发现已有控制端，请使用原控制台，不能覆盖为全新安装'
    for item in $RESOURCES $AGENT_RESOURCES Secret:kubedoor-setup-auth Secret:kubedoor-setup-config; do
        existing_uid=$(field "${item%%:*}" "${item#*:}" '{.metadata.uid}')
        [ -z "$existing_uid" ] || die "缺少安装状态但发现已有 $item，停止覆盖"
    done
    if [ -n "$IMAGE_PULL_SECRET" ]; then
        existing_uid=$(field Secret "$IMAGE_PULL_SECRET" '{.metadata.uid}')
        [ -n "$existing_uid" ] || die '镜像拉取 Secret 不存在，请先在目标命名空间创建'
    fi
    context=$KUBE_CONTEXT
    [ -n "$context" ] || context=$(kc config current-context)
    printf '将在 context=%s、namespace=%s 启动网页安装。\n' "$context" "$NAMESPACE"
    printf '另会创建账号 %s 并绑定 cluster-admin（%s），仅在网页第 6 步选择在本集群安装 Agent 时使用，安装收尾时删除。\n' \
        "$AGENT_INSTALLER" "$AGENT_BINDING"
    if [ "$ASSUME_YES" != true ]; then
        printf '%s' '继续？[y/N] '
        answer=
        read -r answer || :
        case "$answer" in y|Y|yes|YES) ;; *) printf '%s\n' '已取消'; exit 0 ;; esac
    fi
    namespace_yaml | kc apply -f -
    # 创建状态对象用作原子占位；并发或中断时不能覆盖另一个已开始的安装。
    state_yaml | kc -n "$NAMESPACE" create -f -
    state_uid=$(field ConfigMap kubedoor-setup-state '{.metadata.uid}')
fi
INSTALL_ID=$(field ConfigMap kubedoor-setup-state '{.metadata.annotations.setup\.kubedoor\.io/installation-id}')
if [ -z "$INSTALL_ID" ]; then
    managed=$(field ConfigMap kubedoor-setup-state '{.metadata.labels.app\.kubernetes\.io/managed-by}')
    raw_state=$(field ConfigMap kubedoor-setup-state '{.data.state\.json}')
    [ "$managed" = kubedoor-setup ] && [ "$raw_state" = '{}' ] || die '已有安装状态归属不明，停止修改'
    INSTALL_ID=$state_uid
    case "$INSTALL_ID" in ''|*[!A-Za-z0-9-]*) die '无法读取安装状态 UID' ;; esac
    kc -n "$NAMESPACE" annotate configmap kubedoor-setup-state "$OWNER=$INSTALL_ID"
fi
case "$INSTALL_ID" in ''|*[!A-Za-z0-9-]*) die '安装 ID 格式无效' ;; esac
read_phase
case "$PHASE" in
    succeeded) print_access; printf '%s\n' '安装已经完成，不重新开放初始化入口。'; exit 0 ;;
    finalizing) recover_finalizer; print_access; exit 0 ;;
    draft)
        # 恢复原引导快照；已存在的资源始终保留，避免覆盖网页已填写的配置。
        snapshot_available=false
        saved_defaults=$(field ConfigMap kubedoor-setup-state '{.data.bootstrap-defaults}')
        if [ -n "$saved_defaults" ]; then
            snapshot_available=true
            DEFAULTS=$saved_defaults
            SETUP_IMAGE=$(field ConfigMap kubedoor-setup-state '{.data.setup-image}')
            WEB_IMAGE=$(field ConfigMap kubedoor-setup-state '{.data.web-image}')
            IMAGE_PULL_SECRET=$(field ConfigMap kubedoor-setup-state '{.data.image-pull-secret}')
            WEB_NODEPORT=$(field ConfigMap kubedoor-setup-state '{.data.requested-nodeport}')
            validate_image "$SETUP_IMAGE"; validate_image "$WEB_IMAGE"
            IMAGE_REPO=${SETUP_IMAGE%/kubedoor-setup:*}
        fi
        # 本次引导包含 Agent 安装账号（有标记）时才核对和补建；旧版本引导的安装服务不会撤销它。
        agent_marker=$(field ConfigMap kubedoor-setup-state '{.data.agent-installer}')
        agent_items=
        [ "$agent_marker" != "$AGENT_BINDING" ] || agent_items=$AGENT_RESOURCES
        # 先完整核对已有资源归属，再创建任何缺失资源。
        for item in $RESOURCES $agent_items; do
            existing_uid=$(field "${item%%:*}" "${item#*:}" '{.metadata.uid}')
            if [ -n "$existing_uid" ]; then
                require_owner "${item%%:*}" "${item#*:}"
            elif [ "$snapshot_available" != true ]; then
                die '旧安装缺少原始镜像快照，不能用当前版本补建；请恢复原版本的引导资源'
            fi
        done
        for item in $RESOURCES $agent_items; do
            existing_uid=$(field "${item%%:*}" "${item#*:}" '{.metadata.uid}')
            if [ -z "$existing_uid" ]; then
                read_phase
                [ "$PHASE" = draft ] || die '安装已进入执行阶段，请刷新网页查看进度'
                if [ "${item%%:*}" = ClusterRoleBinding ]; then
                    # 授予失败（例如没有集群管理员权限）不阻断控制端安装：网页会提示未授予。
                    resource_yaml "${item%%:*}" "${item#*:}" | kc apply --server-side --field-manager=kubedoor-setup -f - ||
                        printf '%s\n' '警告：未能授予在本集群安装 Agent 所需的临时集群权限（需要集群管理员权限）；控制端仍可正常安装，完成后可在控制台上传 kubeconfig 接入本集群。' >&2
                else
                    resource_yaml "${item%%:*}" "${item#*:}" | kc -n "$NAMESPACE" apply --server-side --field-manager=kubedoor-setup -f -
                fi
            fi
        done
        ;;
    *) printf '%s\n' '已有安装任务，保留当前进度和配置。' ;;
esac
for service in kubedoor-setup kubedoor-web; do
    kc -n "$NAMESPACE" rollout status "deployment/$service" --timeout=240s
done
read_phase
case "$PHASE" in
    succeeded|finalizing) print_access; printf '%s\n' '安装已完成或正在收尾，请在网页查看。' ;;
    *)
        require_owner Secret kubedoor-setup-auth
        setup_key=$(kc -n "$NAMESPACE" get secret kubedoor-setup-auth -o 'go-template={{index .data "SETUP_KEY" | base64decode}}')
        [ -n "$setup_key" ] || die '安装密钥尚未生成，请检查 setup 服务后重试'
        print_access
        printf '一次性安装密钥：%s\n打开网页，按向导填写数据库和管理员信息。\n' "$setup_key"
        ;;
esac
