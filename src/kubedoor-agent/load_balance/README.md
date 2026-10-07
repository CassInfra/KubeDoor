# K8S节点负载均衡功能

基于Pod隔离方式实现的K8S节点负载均衡能力，通过隔离高负载节点上的高CPU Pod，触发Deployment自动创建新Pod到低负载节点，实现节点间负载均衡。

## 核心原理

### 隔离式迁移

传统的Pod迁移需要直接删除Pod，可能导致服务中断。本方案采用**隔离式迁移**：

1. **修改Pod标签**：将Pod的selector标签（如`app`）追加`-ISOLATED`后缀
2. **触发新Pod创建**：Pod不再匹配Deployment的selector，Deployment检测到副本数不足，自动创建新Pod
3. **保留旧Pod**：被隔离的Pod继续运行，可手动清理

这种方式的优势：
- 新Pod先创建，旧Pod后清理，服务不中断
- 如果新Pod创建失败，旧Pod仍在运行
- 可以观察新Pod状态后再决定是否清理旧Pod

### 目标节点控制

为确保新Pod调度到指定的目标节点，采用**临时cordon**策略：

1. **Cordon所有节点**：将所有节点标记为不可调度
2. **Uncordon目标节点**：只开放目标节点
3. **隔离Pod**：触发新Pod创建，只能调度到目标节点
4. **恢复节点状态**：迁移完成后恢复原始调度状态

## 负载均衡算法

### 不均衡判断

使用**极差法**判断集群是否负载不均衡：

```
极差 = 最高负载节点CPU% - 最低负载节点CPU%
```

- 当极差 > `imbalance_threshold`（默认30%）时，触发均衡
- 均衡目标：将极差降低到 < `balance_target`（默认20%）

**注意**：只有可调度（schedulable）的节点参与极差计算，已cordon的节点不参与。

### 迭代优化流程

```
循环（最多 max_iterations 次）：
  1. 计算当前极差和标准差
  2. 如果极差 < balance_target，停止
  3. 选择源节点（高负载节点）
  4. 从源节点选择一个可迁移的Pod
  5. 为该Pod找到合适的目标节点
  6. 模拟迁移效果
  7. 如果迁移能降低标准差，加入迁移计划
  8. 更新模拟负载数据
```

### 源节点选择策略

支持两种策略选择高负载节点：

| 策略 | 说明 |
|------|------|
| `average` | 选择CPU使用率高于平均值的节点 |
| `percentile` | 选择CPU使用率高于指定分位数的节点（如P70） |

配置参数：
- `source_strategy`: 策略类型（`average` 或 `percentile`）
- `source_percentile`: 分位数阈值（默认70）

### 目标节点选择策略

支持两种策略选择低负载节点：

| 策略 | 说明 |
|------|------|
| `average` | 选择CPU使用率低于平均值的节点 |
| `percentile` | 选择CPU使用率低于指定分位数的节点（如P30） |

配置参数：
- `target_strategy`: 策略类型（`average` 或 `percentile`）
- `target_percentile`: 分位数阈值（默认30）

### Pod选择规则

从源节点选择Pod时，按以下规则过滤和排序：

**过滤条件**（必须全部满足）：
1. Pod所属Deployment的副本数 >= `min_replicas`（默认2）
2. Pod的CPU使用量 >= `min_pod_cpu`（默认0.5核）
3. Pod不在黑名单中（`blacklist`配置）
4. Pod的namespace不在排除列表中（`exclude_namespaces`）
5. Pod不是已隔离的Pod（标签不含`-ISOLATED`后缀）
6. Pod的owner是Deployment（排除DaemonSet、StatefulSet等）

**排序规则**：
- 按CPU使用量降序排列，优先迁移高CPU的Pod

### 目标节点匹配规则

为Pod选择目标节点时，需满足以下条件：

1. **负载要求**：节点CPU使用率符合目标策略（低于平均值或指定分位数）
2. **可调度**：节点未被cordon
3. **反亲和检查**：如果Deployment配置了Pod反亲和策略，目标节点上不能已有该Deployment的Pod
4. **排除节点**：不在`exclude_nodes`配置列表中

### 迁移效果判断

使用**标准差**作为判断迁移是否有效的指标：

```python
# 计算迁移前后的标准差
old_std = calc_std_dev(simulated_cpu_used)
new_std = calc_std_dev(temp_cpu_used)

# 只有标准差减小才加入迁移计划
if new_std < old_std:
    # 加入迁移计划
```

**为什么使用标准差而不是极差？**

极差只关注最高和最低两个极端值，存在以下问题：
- 当最高节点降低后，第二高的节点可能成为新的最高
- 导致极差变化不明显，但实际上整体负载分布已经改善

标准差能更准确地反映整体负载分布的均匀程度：
- 即使最高节点被取代，只要整体分布更均匀，标准差就会减小
- 更好地体现迁移的实际效果

**显示效果**：
- 预期效果显示：`源-X.X% σ-X.XX`（源节点降低量和标准差减小量）
- 卡片头部显示：当前极差、预期极差、当前标准差(σ)、预期标准差

## 执行流程

### 分析阶段

```
1. 获取所有节点CPU使用率（metrics-server）
2. 过滤可调度节点，计算极差
3. 如果极差 <= imbalance_threshold，返回"无需均衡"
4. 获取所有Pod的CPU使用情况
5. 获取Deployment信息（副本数、反亲和配置）
6. 运行迭代优化算法，生成迁移计划
7. 返回迁移计划（包含每个Pod的源节点、目标节点、预期效果）
```

### 执行阶段

```
1. 记录所有节点的原始调度状态
2. Cordon所有节点
3. 逐个执行迁移：
   a. Uncordon目标节点
   b. 获取Deployment的selector标签
   c. 修改Pod的selector标签（追加-ISOLATED后缀）
   d. 等待新Pod调度成功（最多60秒）
   e. Cordon回目标节点
4. 恢复节点原始调度状态（排除节点除外）
5. 返回执行结果（包含新Pod名称）
```

### 分批执行

前端支持分批执行迁移计划，避免一次性迁移过多Pod导致服务不稳定。

**分批逻辑**：
```
1. 将迁移计划按 batch_size（默认4）分成多个批次
2. 逐批执行：
   a. 调用后端执行当前批次的迁移
   b. 后端返回调度成功的新Pod信息
   c. 前端轮询检查新Pod的Ready状态
   d. 当前批次所有Pod Ready后，执行下一批
3. 如果某批次有失败，停止执行后续批次
```

**状态流转**：
```
pending → executing → scheduled → ready
                   ↘ failed
```

- `pending`: 待执行
- `executing`: 正在执行（当前批次）
- `scheduled`: 已调度（新Pod已创建，等待Ready）
- `ready`: 已就绪（新Pod Ready）
- `failed`: 执行失败

### Pod Ready状态检查

后端执行迁移时只等待新Pod调度成功（Scheduled），不等待Ready。由前端轮询检查Pod Ready状态。

**为什么采用前端轮询？**
- 后端等待Ready会导致请求超时（Pod启动可能需要几分钟）
- 前端轮询可以实时更新UI状态
- 用户可以看到每个Pod的Ready进度

**轮询逻辑**：
```
1. 后端返回新Pod名称后，前端开始轮询
2. 每5秒调用 /api/load-balance/check-pods 检查Pod状态
3. 检查Pod的Ready condition是否为True
4. 单个Pod Ready后立即更新UI状态
5. 当前批次所有Pod Ready后，执行下一批
6. 超时时间：10分钟（120次 × 5秒）
```

**Ready判断逻辑**（后端）：
```python
# 检查Pod的conditions中是否有Ready=True
for condition in pod.status.conditions:
    if condition.type == "Ready" and condition.status == "True":
        is_ready = True
```

**注意**：`pod.status.phase == "Running"` 不等于 Ready，必须检查 Ready condition。

### Deployment Selector处理

不同的Deployment可能使用不同的selector标签：

```yaml
# 常见的selector配置
spec:
  selector:
    matchLabels:
      app: my-app        # 使用app标签

# 或者
spec:
  selector:
    matchLabels:
      cassgw: 'true'     # 使用自定义标签
```

执行迁移时，会：
1. 读取Deployment的`spec.selector.matchLabels`
2. 优先使用`app`标签，否则使用selector中的第一个标签
3. 修改Pod对应的标签值

## 配置参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `enabled` | false | 功能开关 |
| `imbalance_threshold` | 30 | 不均衡阈值(%)，极差超过此值触发均衡 |
| `balance_target` | 20 | 均衡目标(%)，优化到极差小于此值 |
| `check_interval` | 5 | 定时检测间隔(分钟) |
| `min_replicas` | 2 | 最小副本数要求，低于此值的Deployment不参与 |
| `peak_hours` | [] | 高峰期时间段，格式：`[{"start": "09:00", "end": "12:00"}]` |
| `blacklist` | [] | 黑名单，格式：`["namespace/deployment"]` |
| `exclude_namespaces` | ["kube-system", "kubedoor", "istio-system"] | 排除的namespace |
| `exclude_nodes` | [] | 排除节点，这些节点始终保持cordon状态 |
| `auto_execute` | false | 是否自动执行（false=需人工确认） |
| `source_strategy` | "average" | 源节点选择策略 |
| `source_percentile` | 70 | 源节点分位数阈值 |
| `target_strategy` | "average" | 目标节点选择策略 |
| `target_percentile` | 30 | 目标节点分位数阈值 |
| `min_pod_cpu` | 0.5 | 最小Pod CPU阈值(核)，低于此值的Pod不参与迁移 |
| `max_iterations` | 50 | 最大迭代次数，即最多迁移的Pod数量 |
| `batch_size` | 4 | 每批执行数量，分批执行时每批的Pod数量 |

## 注意事项

### 副本数要求

- 只有副本数 >= `min_replicas` 的Deployment才参与均衡
- 建议设置为2或更高，确保迁移过程中服务可用

### 反亲和策略

- 如果Deployment配置了Pod反亲和策略，目标节点上不能已有该Deployment的Pod
- 这可能导致某些Pod找不到合适的目标节点

### 排除节点

- `exclude_nodes`中的节点始终保持cordon状态
- 这些节点不会作为迁移的目标节点
- 适用于需要维护或特殊用途的节点

### 隔离Pod清理

- 被隔离的Pod不会自动删除
- 需要在Web界面手动清理
- 清理前建议确认新Pod已正常运行

### 超时处理

- 等待新Pod调度的超时时间为60秒
- 超时后会记录Pending原因，便于排查
- 常见原因：资源不足、反亲和冲突、节点选择器不匹配

### CPU计算

- CPU使用量从metrics-server获取
- 单位为核（如1.5表示1.5核）
- 节点CPU百分比 = 节点CPU使用量 / 节点可分配CPU * 100

## API接口

### 分析负载

```
POST /api/load-balance/analyze?env={env}
```

返回迁移计划，包含：
- `migrations`: 迁移列表
- `current_range`: 当前极差
- `expected_range`: 预期极差
- `current_std`: 当前标准差
- `expected_std`: 预期标准差
- `iteration_logs`: 迭代日志
- `node_status`: 节点状态（迁移前后）

### 执行迁移

```
POST /api/load-balance/execute?env={env}
Body: {"migrations": [...]}
```

返回执行结果：
- `total`: 总数
- `success`: 成功数
- `failed`: 失败数
- `results`: 详细结果

### 获取隔离Pod

```
GET /api/load-balance/isolated?env={env}
```

返回隔离Pod列表，包含CPU使用量。

### 清理隔离Pod

```
POST /api/load-balance/cleanup?env={env}
Body: {"pods": [{"name": "...", "namespace": "..."}]}
```

### 获取节点CPU

```
GET /api/load-balance/nodes-cpu?env={env}
```

返回所有节点的CPU使用情况。

### 检查Pod Ready状态

```
POST /api/load-balance/check-pods?env={env}
Body: {"pods": [{"namespace": "...", "pod_name": "..."}]}
```

返回Pod状态列表：
```json
[
  {
    "namespace": "default",
    "pod_name": "my-app-xxx",
    "status": "Running",
    "ready": true
  }
]
```

- `status`: Pod的phase（Pending/Running/Succeeded/Failed/Unknown）
- `ready`: Pod的Ready condition是否为True

## 故障排查

### 迁移失败

1. **等待新Pod调度超时**
   - 检查目标节点资源是否充足
   - 检查是否有反亲和冲突
   - 检查节点选择器是否匹配

2. **Pod标签修改失败**
   - 检查Pod是否存在
   - 检查是否有权限修改Pod

3. **新Pod调度到错误节点**
   - 检查cordon/uncordon是否正确执行
   - 检查是否有其他调度器干扰

### 负载不改善

1. **所有高负载节点的Pod都不满足条件**
   - 检查`min_replicas`配置
   - 检查`min_pod_cpu`配置
   - 检查黑名单配置

2. **找不到合适的目标节点**
   - 检查反亲和策略
   - 检查`exclude_nodes`配置
   - 检查目标节点策略配置

### 节点状态异常

1. **节点一直处于cordon状态**
   - 检查是否在`exclude_nodes`列表中
   - 检查迁移执行是否正常完成
   - 手动uncordon节点

## 架构图

```
┌─────────────────────────────────────────────────────────────────┐
│                         kubedoor-web                            │
│  配置页面 │ 迁移计划展示 │ 人工确认 │ 隔离Pod管理                  │
└─────────────────────────────────────────────────────────────────┘
                              │ HTTP API
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                       kubedoor-master                           │
│  配置管理 │ 定时调度 │ API网关 │ 迁移计划缓存 │ 操作日志          │
└─────────────────────────────────────────────────────────────────┘
                              │ WebSocket
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                       kubedoor-agent                            │
│  获取节点/Pod资源 │ 生成迁移计划 │ 执行迁移 │ 查询/清理隔离Pod     │
└─────────────────────────────────────────────────────────────────┘
                              │ K8S API
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      Kubernetes 集群                            │
│  metrics-server │ Deployment │ Pod │ Node                       │
└─────────────────────────────────────────────────────────────────┘
```
