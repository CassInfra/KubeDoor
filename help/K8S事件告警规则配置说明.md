# K8S 事件告警规则配置说明

> 适用于 KubeDoor 2.x。与 1.x 文档相比：规则文件改为随部署模板下发，修改后必须重启 master（不支持热重载）；事件改存 PostgreSQL。

## 概述

KubeDoor 会持续采集每个被纳管集群的全部 K8S 事件（Event）并写入 PostgreSQL，同时按一份 JSON 规则文件判断哪些事件需要告警：命中规则的事件由 kubedoor-master 直接推送到 IM 机器人（企业微信、钉钉、飞书、Slack），并在「K8S事件详情」页标记为「已告警」。

K8S 事件告警与 Prometheus 指标告警（vmalert → Alertmanager → kubedoor-alarm）是两条独立的链路：事件告警不经过 Alertmanager 和 kubedoor-alarm，不会出现在「告警面板」「K8S告警详情」里，也不受「告警屏蔽」影响。

## 功能特性

- **规则驱动**：一份 JSON 规则文件作用于所有集群，用 `k8s` 字段区分集群。
- **两级过滤**：先过忽略规则（任一命中即忽略），再按顺序匹配告警规则（第一条命中的生效）；`DELETED` 事件固定不告警。
- **多条件匹配**：包含 / 不包含、开头 / 结尾、等于 / 不等于、次数比较，共 12 种条件，字符串比较不区分大小写。
- **告警去重**：同一个事件在 `ALERT_DEDUP_WINDOW`（默认 300 秒）内只通知一次。
- **可追溯**：事件全部入库（默认保留 90 天），命中告警规则的事件标记为「已告警」。
- **按集群分群通知**：通知发往事件所在集群 agent 配置的机器人。

## 事件采集与入库链路

```
每个集群的 kubedoor-agent
  │  分页 list + watch 全集群 Event
  │  通过 WebSocket 批量上报
  ▼
kubedoor-master
  ├─ 校验后写入 PostgreSQL（k8s_events 表，同一个事件始终只有一行）
  └─ 规则匹配 → 标记「已告警」→ 去重 → 直接调用 IM 机器人发送通知
```

- agent 每次连上 master 都会先分页 list 一遍集群里的事件，再 watch 后续变化；断线重连或 watch 过期（HTTP 410）后重新 list，只补发 master 还没收到的新增或变化的事件，不会把整个集群的事件重推一遍。agent 自身重启后不知道哪些已经发过，会把集群里现存的事件全部重新上报一次。
- master 按「事件 UID + 集群」写库：同一个事件再次发生时，原地更新次数和最后时间。
- 事件要先成功写库才会进入告警流程：PostgreSQL 不可用时，事件既不入库也不告警。

## 规则文件与生效方式

### 1. 规则文件在哪里

| 环节 | 位置 |
| --- | --- |
| 规则模板（唯一来源） | 仓库中的 [`deploy/manifests/master/alert_rules.json`](../deploy/manifests/master/alert_rules.json) |
| 集群中 | `./install.sh master` 把模板做成 ConfigMap `kubedoor-master-file-cfg`（key 为 `alert_rules.json`） |
| master 容器内 | ConfigMap 挂载到 `/k8s_event/rules/`，master 读取 `/k8s_event/rules/alert_rules.json` |

- master 镜像不自带规则文件，规则只能来自这个 ConfigMap。不用 install.sh、自行部署 master 时也必须按同样的路径挂载；不挂载时规则为空，事件照常入库，但**不会发出任何事件告警**。
- 没有 Web 页面或接口可以修改规则，只能改文件或 ConfigMap。
- 从 1.x 升级时，如果改过事件告警规则，请先把自定义内容合并进模板再安装，否则生效的是出厂规则。

### 2. 修改规则

> **规则不支持热重载。** 每个 master 进程只加载一次规则（启动后收到第一条事件时）。ConfigMap 更新后，容器里的文件会被 kubelet 同步，但 master 不会重新读取，**改完必须重启 master**。

**方式一：改模板后重新部署控制端（推荐，修改随仓库保留）**

```bash
cd deploy
vi manifests/master/alert_rules.json
python3 -m json.tool manifests/master/alert_rules.json > /dev/null && echo "JSON OK"   # 检查语法
./install.sh master                                    # 更新 ConfigMap
kubectl -n kubedoor rollout restart deploy/kubedoor-master
```

- `./install.sh master` 会按 `kubedoor.conf` 重新下发控制端的全部配置，请使用安装时的那份配置文件（安装时用 `-c` 指定过的，这里也要带上）。
- 脚本只更新 ConfigMap，不会因为规则变化而重启 master，最后一步不能省。

**方式二：直接改集群里的 ConfigMap（立即修改）**

```bash
kubectl -n kubedoor edit configmap kubedoor-master-file-cfg
kubectl -n kubedoor rollout restart deploy/kubedoor-master
```

- 下次执行 `./install.sh master` 时，ConfigMap 会被模板覆盖，手工修改会丢失；需要保留的修改请同步到模板。
- ConfigMap 里的 JSON 不会被校验，保存后可以导出检查：

  ```bash
  kubectl -n kubedoor get configmap kubedoor-master-file-cfg \
    -o jsonpath='{.data.alert_rules\.json}' | python3 -m json.tool > /dev/null && echo "JSON OK"
  ```

master 采用 Recreate 方式重启，期间 Web 的数据接口会短暂不可用；agent 会自动重连，并补发这段时间的事件。

### 3. 确认规则已加载

```bash
kubectl -n kubedoor logs deploy/kubedoor-master | grep 告警规则
```

| 日志 | 含义 |
| --- | --- |
| `加载了 N 条告警规则` | 加载成功。N 只统计 `alert_rules`（含 `"enabled": false` 的，不含忽略规则），出厂文件为 7 |
| `加载告警规则失败(规则文件需由 ConfigMap kubedoor-master-file-cfg 挂载到 k8s_event/rules/alert_rules.json): …` | 文件不存在（没有挂载），或 JSON 不合法（冒号后面是具体原因和行列号）。此时**所有事件告警都不会发出**，事件照常入库 |

- 这行日志在 master 重启后**收到第一条事件时**才打印，不是启动瞬间；集群暂时没有新事件时会晚一些出现。
- 规则文件必须是严格的 JSON：不能写 `//` 注释，不能有尾逗号，引号必须是英文双引号。

## 规则说明

### 1. 处理顺序

```
事件写入 PostgreSQL 后：
  ├─ eventStatus 是 DELETED？              是 → 结束（只入库）
  ├─ 命中任意一条启用的忽略规则？          是 → 结束
  ├─ 按数组顺序找第一条命中的告警规则      没有 → 结束
  ├─ 把该事件的「级别」改为「已告警」
  ├─ 同一事件在去重窗口内已通知过？        是 → 结束（日志「告警被去重阻止」）
  └─ 发送 IM 通知，记录本次发送时间
```

- `global_ignore_rules`（忽略规则）：多条之间是 **OR** 关系，任意一条命中即忽略，不再看告警规则。
- `alert_rules`（告警规则）：按数组顺序匹配，**第一条命中的规则生效**，后面的不再看。一条事件每次最多产生一条通知，所以**规则顺序就是优先级**，严重的放前面。
- `"enabled": false` 的规则直接跳过；不写 `enabled` 视为启用。
- 「已告警」标记在去重之前：被去重拦下或发送失败的事件，同样会标记为「已告警」。

### 2. DELETED 事件说明

`eventStatus=DELETED` 表示 K8S 删除了这个事件对象，通常是事件超过保留时间（kube-apiserver 的 `--event-ttl`，默认 1 小时）没有再次发生而被清理，并不是又发生了一次。所以 DELETED 事件不会进入任何规则、不会告警，但状态会更新到数据库，「K8S事件详情」的「状态」列显示「删除」。

### 3. 规则文件结构

一个最小但完整的规则文件：

```json
{
  "global_ignore_rules": [
    {
      "name": "忽略测试集群",
      "enabled": true,
      "conditions": {
        "k8s": { "contains": ["test-a", "test-b"] }
      }
    }
  ],
  "alert_rules": [
    {
      "name": "健康检查失败告警",
      "enabled": true,
      "severity": "warning",
      "conditions": {
        "reason": { "contains": ["Unhealthy"] },
        "count": { "greater_than": 1 }
      }
    }
  ]
}
```

| 键 | 说明 |
| --- | --- |
| `global_ignore_rules` | 忽略规则数组 |
| `alert_rules` | 告警规则数组 |
| `name` | 规则名。告警规则的名字会出现在通知标题「K8S事件告警: 规则名」中 |
| `enabled` | 是否启用，默认 `true` |
| `severity` | 只对告警规则有意义：`critical` / `warning` / `info`，默认 `warning`。只决定通知里的图标和「级别」文字，不影响匹配、去重和发送目标 |
| `conditions` | 条件：键是事件字段名（见「支持的事件字段」），值是 `{"条件类型": 值}`。写成空对象 `{}` 时匹配所有事件 |

顶层键名必须是 `global_ignore_rules`、`alert_rules`，写错不会报错，只是那一组规则为空。

### 4. 条件之间的关系

- 同一条规则 `conditions` 里的多个字段是 **AND** 关系：全部满足才算命中。
- 同一字段的多个值：`contains`、`starts_with`、`ends_with` 是 **OR**（满足任一即可）；`not_contains`、`not_starts_with`、`not_ends_with` 是 **AND**（一个都不能命中）。
- 所有字符串比较都**不区分大小写**；`contains` / `not_contains` 是**子串**匹配。

```json
{
  "conditions": {
    "reason": { "contains": ["OOMKilling"] },
    "level": { "equals": "Warning" },
    "namespace": { "not_contains": ["test"] }
  }
}
```

上例只有当 reason 包含 OOMKilling **且** level 等于 Warning **且** namespace 不包含 test 时才命中。

### 5. 忽略规则里的双重否定：contains 与 not_contains

忽略规则的条件描述的是「**哪些事件不告警**」：条件成立就忽略。所以在忽略规则里用 `not_contains`（以及 `not_starts_with`、`not_ends_with`）是两次否定：「不包含 X 的都忽略」等于「只有包含 X 的才告警」，很容易写反。

下面用集群名 `k8s` 举例，两种写法都放在 `global_ignore_rules` 里。「是否告警」指该集群的事件会不会继续交给告警规则匹配，最终发不发还要看告警规则是否命中。

**写法 A：排除指定集群** —— `"k8s": {"contains": ["test-a", "test-b"]}`

| 集群名 | 是否忽略 | 是否告警 |
| --- | --- | --- |
| test-a | 是 | 否 |
| test-b | 是 | 否 |
| test-abc | 是（子串包含 test-a） | 否 |
| prod-k8s | 否 | 是 |

效果：test-a、test-b（以及名字里包含它们的集群）不告警，其它集群照常告警。

**写法 B：只告警生产集群** —— `"k8s": {"not_contains": ["prod"]}`

| 集群名 | 是否忽略 | 是否告警 |
| --- | --- | --- |
| prod-k8s | 否（含 prod） | 是 |
| PROD-BJ | 否（不区分大小写） | 是 |
| preprod | 否（子串包含 prod） | 是 |
| test-k8s | 是 | 否 |
| dev | 是 | 否 |

效果：名字不含 prod 的集群都不告警，只有名字含 prod 的集群告警。

**常见错误**：想「不告警 test-a、test-b」，却在忽略规则里写成 `"k8s": {"not_contains": ["test-a", "test-b"]}`。结果正好相反：除这两个集群以外的所有集群都被忽略，只剩 test-a、test-b 会告警。

建议：

- 写完后拿几个真实的集群名代入，按上面的表推一遍。
- `contains` / `not_contains` 是子串匹配，`preprod` 也「含 prod」。需要更精确时，统一集群命名后改用 `starts_with` / `not_starts_with`（例如生产集群都以 `prod-` 开头，见「常用写法示例」）。
- 只想限制某一条告警规则的集群范围时，把 `k8s` 条件直接写进那条告警规则。告警规则是正向逻辑（条件成立才告警），不存在双重否定。

## 支持的条件类型

| 条件类型 | 值的写法 | 多个值的关系 | 说明 | 示例 |
| --- | --- | --- | --- | --- |
| `contains` | 数组或单个字符串 | OR | 字段包含任一值 | `{"reason": {"contains": ["OOM", "Failed"]}}` |
| `not_contains` | 数组或单个字符串 | AND | 字段不包含其中任何一个值 | `{"namespace": {"not_contains": ["test", "dev"]}}` |
| `starts_with` | 数组或单个字符串 | OR | 字段以任一值开头 | `{"name": {"starts_with": ["deploy-"]}}` |
| `not_starts_with` | 数组或单个字符串 | AND | 字段不以其中任何一个值开头 | `{"name": {"not_starts_with": ["test-"]}}` |
| `ends_with` | 数组或单个字符串 | OR | 字段以任一值结尾 | `{"name": {"ends_with": ["-service", "-pod"]}}` |
| `not_ends_with` | 数组或单个字符串 | AND | 字段不以其中任何一个值结尾 | `{"name": {"not_ends_with": ["-test", "-debug"]}}` |
| `equals` | 单个值 | — | 字段等于该值 | `{"level": {"equals": "Warning"}}` |
| `not_equals` | 单个值 | — | 字段不等于该值 | `{"kind": {"not_equals": "Node"}}` |
| `greater_than` | 数字 | — | 次数大于该值（仅 `count` 字段） | `{"count": {"greater_than": 3}}` |
| `less_than` | 数字 | — | 次数小于该值（仅 `count` 字段） | `{"count": {"less_than": 10}}` |
| `greater_equal` | 数字 | — | 次数大于等于该值（仅 `count` 字段） | `{"count": {"greater_equal": 3}}` |
| `less_equal` | 数字 | — | 次数小于等于该值（仅 `count` 字段） | `{"count": {"less_equal": 10}}` |

值写成单个字符串时，等同于只有一个元素的数组。

### 注意事项

1. **一个字段只认一个条件类型**：同一字段写了多个条件类型时，只按以下顺序取第一个，其余忽略：`contains` → `not_contains` → `starts_with` → `not_starts_with` → `ends_with` → `not_ends_with` → `equals` → `not_equals` → `greater_than` → `less_than` → `greater_equal` → `less_equal`。例如 `"count": {"greater_than": 1, "less_than": 10}` 只按 `greater_than` 判断。同一条规则里同一个字段名也只能出现一次（重复时只有最后一个生效）。需要「或」时拆成多条告警规则；需要「且」（如次数区间、包含 A 但不含 B）时只能借助忽略规则（忽略规则对所有告警规则生效）。
2. **条件类型拼错不会报错，而是恒成立**：例如把 `contains` 写成 `contain`，该条件永远满足。写在告警规则里会误报，写在忽略规则里可能把事件全部忽略。
3. **数值条件只对 `count` 生效**：写在其它字段上同样恒成立。数值要写成 JSON 数字（`1`）；写成字符串（`"1"`）时该条件不成立，master 日志提示「无法将count字段转换为数值」。
4. **字段名写错或大小写不对**（如 `Reason`）：`not_contains`、`not_starts_with`、`not_ends_with` 视为成立，其它条件视为不成立。字段名请严格按「支持的事件字段」书写。
5. **`equals` / `not_equals` 只接受单个值**：写成数组不会报错，但永远不相等。需要多个值时用 `contains`、`starts_with` 等。
6. **会匹配所有事件的写法**：空条件 `{}`；`contains` / `starts_with` / `ends_with` 的值里有空字符串 `""`；`not_contains` / `not_starts_with` / `not_ends_with` 的值是空数组 `[]`。想停用一条规则请设 `"enabled": false`，不要只清空值列表。

## 支持的事件字段

| 字段名 | 含义 | 来源（K8S Event） | 「K8S事件详情」中 | 说明 |
| --- | --- | --- | --- | --- |
| `k8s` | 集群名 | 该集群 agent 的 `K8S_NAME` | 筛选项「K8S」 | 按集群区分规则只能用它 |
| `namespace` | 命名空间 | `involvedObject.namespace` | 命名空间 | 集群级对象（如 Node）为空 |
| `kind` | 对象类型 | `involvedObject.kind` | 类型 | Pod、Node、Deployment 等 |
| `name` | 对象名称 | `involvedObject.name` | 名称 | 如 Pod 名 `deploy-demo-7f7d9bc955-6z2m4` |
| `reason` | 事件原因 | `reason` | 原因 | 如 Unhealthy、FailedScheduling |
| `message` | 事件消息 | `message` | 消息 | |
| `level` | 事件级别 | `type` | 级别 | 只有 Normal、Warning；规则看到的是原始值，不会是「已告警」 |
| `count` | 累计发生次数 | `count` | 次数 | 唯一支持数值比较的字段；事件没有次数时为 0 |
| `eventStatus` | 事件变化类型 | watch 事件类型 | 状态 | ADDED（新增）、MODIFIED（更新）；DELETED 在规则之前已被跳过 |
| `reportingComponent` | 上报组件 | `source.component` | 来源 | 如 kubelet |
| `reportingInstance` | 上报主机 | `source.host` | 来源IP | 通常是节点名，不一定是 IP |
| `firstTimestamp` | 首次发生时间 | `firstTimestamp` | 首次时间 | 北京时间，形如 `2026-10-07 09:12:03`；为空时取 master 处理时的时间。一般不用于规则 |
| `lastTimestamp` | 最后发生时间 | `lastTimestamp` | 最后时间 | 同上 |
| `eventUid` | 事件唯一 ID | `metadata.uid` | — | 去重依据，一般不用于规则 |

- 字段名区分大小写，必须按上表书写。
- 一份规则作用于所有集群。`k8s` 的值是安装该集群 agent 时 `kubedoor.conf` 里的 `K8S_NAME`，也就是 Web 上显示的集群名。
- 部分事件（例如调度器上报的 FailedScheduling）可能不带次数和来源：「次数」为 0、「来源」为空，不要对这类事件依赖 `count` 条件。

## 出厂默认规则

`./install.sh master` 默认下发 [`deploy/manifests/master/alert_rules.json`](../deploy/manifests/master/alert_rules.json)：2 条忽略规则 + 7 条告警规则。

忽略规则（`global_ignore_rules`，任一命中即忽略）：

| # | 名称 | 默认 | 条件 | 说明 |
| --- | --- | --- | --- | --- |
| 1 | 环境过滤规则(排除非生产环境的K8S) | 启用 | `k8s` contains 两个集群名 | 列的是维护者自己的两个测试集群，对你的环境等于不过滤，请改成你要排除的集群 |
| 2 | 常见忽略事件[示例:防止告警规则中有正常事件被匹配到,可以加到忽略规则中] | 启用 | `reason` contains `BackOffStart`、`SuccessfulCreate`、`SuccessfulMountVolume` | 防止这些正常事件被后面的告警规则误命中 |

告警规则（`alert_rules`，按顺序，第一条命中的生效）：

| 顺序 | 名称 | severity | 条件（同一行内为 AND） |
| --- | --- | --- | --- |
| 1 | 关键事件告警 | critical | `reason` contains `OOMKilling` / `NodeNotReady` / `SystemOOM` / `Unreachable`，且 `level` contains `Warning` |
| 2 | 重启事件告警 | warning | `message` contains `restart`（不区分大小写，例如「Back-off restarting failed container …」） |
| 3 | 健康检查失败告警 | warning | `reason` contains `Unhealthy`，且 `name` not_starts_with 若干服务名前缀，且 `count` greater_than `1` |
| 4 | 挂载失败告警 | warning | `reason` contains `FailedMount` / `FailedAttachVolume` |
| 5 | 调度失败告警 | warning | `reason` contains `FailedScheduling` / `Insufficient` |
| 6 | 资源压力告警 | warning | `reason` contains `Pressure` / `OutOf` / `Evict` |
| 7 | 僵尸进程告警 | warning | `reason` contains `ProcessZ` |

使用前请按自己的环境调整：

- **环境过滤规则**：改成你的集群名（如 `["test-a", "test-b"]`），或改成「只告警生产集群」的写法。改之前先看「忽略规则里的双重否定」一节。
- **健康检查失败告警**：`name` 排除的几个前缀是维护者环境里的服务，请改成你要排除的 Pod 名前缀（如 `["deploy-demo-"]`），或删掉 `name` 条件。这个排除只作用于本条规则，同一服务的重启类事件仍会被「重启事件告警」命中。
- 匹配都是子串：例如 `Pressure` 也会命中节点压力解除时的 `NodeHasNoDiskPressure`。不需要的原因可以加进第 2 条忽略规则。
- **僵尸进程告警**依赖集群里有上报 `ProcessZ` 原因的节点检测组件，没有时不会触发。

## 常用写法示例

以下每段都是一条完整的规则，放进对应数组即可（注意数组元素之间的逗号）。

**只告警生产集群**（集群名统一以 `prod-` 开头时），放进 `global_ignore_rules`：

```json
{
  "name": "只告警生产集群",
  "enabled": true,
  "conditions": {
    "k8s": {
      "not_starts_with": ["prod-"]
    }
  }
}
```

名字不以 `prod-` 开头的集群全部忽略。和写法 B（`not_contains ["prod"]`）相比，`preprod` 这类名字不会被当成生产集群。

**忽略开发、测试命名空间**，放进 `global_ignore_rules`：

```json
{
  "name": "忽略开发测试命名空间",
  "enabled": true,
  "conditions": {
    "namespace": {
      "starts_with": ["dev-", "test-"]
    }
  }
}
```

**生产集群健康检查连续失败按严重告警**，放进 `alert_rules`，并且要排在「健康检查失败告警」**之前**（否则事件会先被那条规则命中，这条永远轮不到）：

```json
{
  "name": "生产集群健康检查连续失败",
  "enabled": true,
  "severity": "critical",
  "conditions": {
    "k8s": {
      "starts_with": ["prod-"]
    },
    "reason": {
      "equals": "Unhealthy"
    },
    "count": {
      "greater_equal": 5
    }
  }
}
```

## 告警通知

### 1. 发到哪里

K8S 事件告警由 master 直接调用 IM 机器人的 Webhook 发送：

| 项目 | 取值来源 | 说明 |
| --- | --- | --- |
| IM 类型 | master 的 `MSG_TYPE`（`kubedoor.conf` → ConfigMap `kubedoor-config`） | `wecom` / `dingding` / `feishu` / `slack` |
| 机器人 | **事件所在集群** agent 的 `MSG_TOKEN`（安装该集群 agent 时 `kubedoor.conf` 里的值 → 该集群的 ConfigMap `kubedoor-agent-config`） | agent 把自己的 token 随事件一起上报，master 用它发送 |
| @ 的人 | master 的 `DEFAULT_AT`（IM 里的用户 ID，钉钉填手机号） | 事件告警不能按规则 @ 不同的人 |

- 不同集群的事件告警可以发到不同的群：安装各集群 agent 时填不同的 `MSG_TOKEN` 即可。这个机器人同时也接收该集群 agent 的运维操作通知（扩缩容、隔离、更新镜像等）。
- 所有集群的机器人都要和 master 的 `MSG_TYPE` 是同一种 IM。
- 某个集群 agent 的 `MSG_TOKEN` 为空时，该集群的事件告警发不出去（不会改用 master 的 `MSG_TOKEN`）。
- `MSG_TOKEN` 只填机器人 Webhook 地址里的密钥部分：企业微信是 `key=` 后面的值，钉钉是 `access_token=` 后面的值，飞书是 `/hook/` 后面的值，Slack 是 `/services/` 后面的路径。
- 钉钉机器人的安全设置请把「自定义关键词」设为「告警」。
- 安装后修改某个集群的机器人：在该集群执行 `kubectl -n kubedoor edit configmap kubedoor-agent-config` 修改 `MSG_TOKEN`，再执行 `kubectl -n kubedoor rollout restart deploy/kubedoor-agent`。

### 2. 消息格式

```
⚠️ K8S事件告警: 健康检查失败告警
🔥 级别: WARNING
⏰ 时间: 2026-10-07 09:12:03~~2026-10-07 09:30:41

🎯 事件详情:
• 集群: prod-a【default】
• 资源: Pod/deploy-demo-7f7d9bc955-6z2m4
• 原因: Unhealthy：5次
• 消息: Readiness probe failed: HTTP probe failed with statuscode: 503
• 来源: kubelet/node-01
```

- 第一行的图标由 `severity` 决定：`critical` 为 🚨，`warning` 为 ⚠️，`info` 为 ℹ️，其它值按 ⚠️ 显示；后面是规则名。
- 「级别」是 `severity` 的大写；「时间」是事件的首次 ~ 最后发生时间（北京时间）；「原因」后面是累计次数；「来源」是 `reportingComponent/reportingInstance`。
- 消息会 @ master 的 `DEFAULT_AT`。

## 告警去重

| 项目 | 说明 |
| --- | --- |
| 参数 | `ALERT_DEDUP_WINDOW`，单位秒，默认 `300` |
| 配置位置 | `kubedoor.conf` → ConfigMap `kubedoor-config` → master 的环境变量 |
| 修改 | 改 `kubedoor.conf` 后执行 `./install.sh master`，或 `kubectl -n kubedoor edit configmap kubedoor-config`；两种方式都要再执行 `kubectl -n kubedoor rollout restart deploy/kubedoor-master` |
| 去重对象 | 同一个 K8S 事件对象（事件 UID）。同一 Pod 的不同原因、不同 Pod 的同类事件都算不同事件，各自通知 |
| 效果 | 事件在窗口内持续更新（次数增加）时只通知一次；窗口过后再次更新会再通知，相当于每个窗口最多一条 |
| 与规则的关系 | 只看事件，不看命中的规则：同一事件在窗口内即使改为命中另一条规则，也不会再发 |
| 存储 | master 进程内存，master 重启后清空 |
| 记录时机 | 发送没有报错才记录；网络异常等导致发送失败时，事件下次更新会再次尝试 |
| 作用范围 | 只用于 K8S 事件告警。Prometheus 指标告警的重复通知由 Alertmanager 控制，与这个参数无关 |

## 查看事件与告警记录

- 菜单「监控告警 → K8S事件详情」：可按「时间」「K8S」「空间」「Kind」「名称」「原因」（可输入，模糊匹配）「级别」筛选，「更多」里还有「来源」「来源IP」「次数」（大于等于）「消息」（模糊匹配）「Limit」。
- 「级别」选「已告警」即可列出命中过告警规则的事件。命中后，库里原来的 Warning / Normal 会被「已告警」替换，所以按「Warning」筛选看不到已告警的事件。
- 「状态」列显示新增 / 更新 / 删除（删除的含义见「DELETED 事件说明」）。
- 筛选条件会同步到浏览器地址栏，复制链接即可分享。
- 「资源管理 → Pod」页每行的「事件」按钮，会在新标签页打开该 Pod 最近 7 天的事件。
- 事件默认保留 90 天，由 `RETENTION_EVENTS_DAYS` 控制（定义在 [`deploy/manifests/master/10-config.yaml`](../deploy/manifests/master/10-config.yaml)，`0` 表示不清理），master 每天清理一次。

## 与告警屏蔽的关系

- 「监控告警 → 告警屏蔽」只作用于经过 kubedoor-alarm 的告警（Prometheus 指标告警，以及 `/api/custom_alert` 写入的自定义告警；屏蔽判断在 kubedoor-alarm 中进行，见 [告警屏蔽说明](../docs/alert-silence.md)）。K8S 事件告警由 master 直接发出，**不受屏蔽规则影响**。
- 想停掉某类事件告警：加一条忽略规则，或把对应的告警规则设为 `"enabled": false`，然后重启 master。事件告警没有到期自动解除的屏蔽，临时加的忽略规则用完记得删掉。

## 排查清单

常用日志：

```bash
# master：规则加载、命中、去重、发送
kubectl -n kubedoor logs deploy/kubedoor-master | grep -E '告警规则|事件匹配规则|告警被去重阻止|告警已发送|发送告警失败'

# agent：事件采集（在对应的业务集群执行）
kubectl -n kubedoor logs deploy/kubedoor-agent | grep 'K8S事件'
```

master 日志里带 `【wecom】`、`【dingding】`、`【feishu】`、`【slack】` 的行是机器人接口的返回内容；「告警已发送」只表示已经调用发送，是否被 IM 接受要看这一行。

| 现象 | 可能原因 | 处理 |
| --- | --- | --- |
| 改了规则不生效 | master 没有重启；或重跑 `./install.sh master` 后，ConfigMap 被模板覆盖 | `kubectl -n kubedoor rollout restart deploy/kubedoor-master`，确认日志「加载了 N 条告警规则」；长期修改写进模板 |
| 完全没有事件告警，日志有「加载告警规则失败」 | JSON 不合法（注释、尾逗号、中文引号），或 ConfigMap 没有挂载 | 按报错的行列号修正，重启 master |
| 日志是「加载了 0 条告警规则」 | `alert_rules` 为空或键名写错 | 检查顶层键名 |
| 某些集群完全没有事件告警 | 忽略规则里的 `k8s` 条件写反（`not_contains` 双重否定）；或该集群 agent 的 `MSG_TOKEN` 为空 | 按「忽略规则里的双重否定」中的表自查；检查该集群的 `kubedoor-agent-config` |
| 「K8S事件详情」查不到某集群的事件 | agent 不在线或采集出错，与规则无关 | 在「Agent管理」页确认「状态」为「在线」；agent 日志里应有「K8S事件监控已启动」，不应反复出现「K8S事件 list/watch 失败」 |
| 事件存在，但「级别」不是「已告警」 | 被忽略规则吞掉、条件不满足、被 `DELETED` 跳过、字段名或条件类型写错 | 拿事件的各字段值逐条对照规则，注意子串匹配和大小写不敏感 |
| 已标记「已告警」，但没收到通知 | 处于去重窗口内；该集群 agent 的 `MSG_TOKEN` 为空或填错；IM 类型和 master 的 `MSG_TYPE` 不一致；钉钉关键词不是「告警」 | 看 master 日志「告警被去重阻止」「发送告警失败」以及机器人返回内容 |
| 告警发到了另一个群 | 事件告警用的是事件所在集群 agent 的机器人，不是 master 的 | 修改该集群的 `kubedoor-agent-config` 并重启 agent |
| 同一个问题收到多条通知 | 不同 Pod 或不同事件对象各算一条；master 重启后去重记录清空；agent 重启后会把集群里仍存在的事件重新上报，超过去重窗口的可能再通知一次 | 调大 `ALERT_DEDUP_WINDOW`，或给规则加 `count` 条件 |
| 日志「更新事件数据失败」 | 写 PostgreSQL 失败，这些事件不会告警 | 检查数据库连接 |
| 日志「无法将count字段转换为数值」 | 数值条件写成了字符串 | 改成 JSON 数字 |

更多部署与配置修改方法见 [部署文档](../deploy/README.md)。
