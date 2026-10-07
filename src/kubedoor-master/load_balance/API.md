# 🔄 K8S节点负载均衡 API 文档

本文档描述了K8S节点负载均衡功能的所有API接口，可用于调试和不使用Web界面时的操作。

## 概述

负载均衡功能通过隔离高负载节点上的高CPU Pod，触发Deployment自动创建新Pod到低负载节点，实现节点间负载均衡。

### 核心流程

```
1. 获取配置 → 2. 触发分析 → 3. 查看迁移计划 → 4. 确认执行 → 5. 查看/清理隔离Pod
```

---

## API 接口列表

### 1. 获取配置

获取当前负载均衡配置。

```bash
GET /api/load-balance/config
```

**响应示例：**
```json
{
  "success": true,
  "data": {
    "enabled": false,
    "imbalance_threshold": 30,
    "balance_target": 20,
    "check_interval": 5,
    "min_replicas": 2,
    "peak_hours": [],
    "blacklist": [],
    "exclude_namespaces": ["kube-system", "kube-public", "istio-system"],
    "auto_execute": false
  }
}
```

**配置参数说明：**

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enabled` | bool | false | 功能开关 |
| `imbalance_threshold` | int | 30 | 不均衡阈值(%)，极差超过此值触发均衡 |
| `balance_target` | int | 20 | 均衡目标(%)，优化到极差小于此值 |
| `check_interval` | int | 5 | 检测间隔(分钟) |
| `min_replicas` | int | 2 | 最小副本数要求 |
| `peak_hours` | array | [] | 高峰期时间段，如 `[{"start": "09:00", "end": "12:00"}]` |
| `blacklist` | array | [] | 黑名单，不参与均衡的Deployment |
| `exclude_namespaces` | array | [...] | 排除的namespace |
| `auto_execute` | bool | false | 是否自动执行（false=需人工确认） |

---

### 2. 更新配置

更新负载均衡配置。

```bash
PUT /api/load-balance/config
Content-Type: application/json

{
  "enabled": true,
  "imbalance_threshold": 25,
  "balance_target": 15,
  "min_replicas": 3,
  "blacklist": ["default/critical-app", "production/database"],
  "peak_hours": [
    {"start": "09:00", "end": "12:00"},
    {"start": "14:00", "end": "18:00"}
  ]
}
```

**响应示例：**
```json
{
  "success": true,
  "data": {
    "enabled": true,
    "imbalance_threshold": 25,
    ...
  }
}
```

---

### 3. 获取状态

获取负载均衡服务当前状态。

```bash
GET /api/load-balance/status
```

**响应示例：**
```json
{
  "success": true,
  "data": {
    "status": "idle",
    "enabled": true,
    "last_check_time": "2024-01-09T10:30:00",
    "has_pending_plan": true,
    "pending_migrations_count": 5
  }
}
```

**状态说明：**
- `idle`: 空闲
- `analyzing`: 正在分析
- `executing`: 正在执行迁移

---

### 4. 触发分析

触发负载分析，生成迁移计划。

```bash
POST /api/load-balance/analyze?env=<集群名称>
```

**参数：**
- `env`: K8S集群名称（必填）

**响应示例：**
```json
{
  "success": true,
  "data": {
    "migrations": [
      {
        "pod_name": "app-abc-xyz-12345",
        "namespace": "production",
        "deployment": "app-abc",
        "source_node": "node-15",
        "target_node": "node-32",
        "cpu_used": 2.5,
        "expected_effect": "极差 -5.2%"
      },
      {
        "pod_name": "svc-def-67890",
        "namespace": "production",
        "deployment": "svc-def",
        "source_node": "node-15",
        "target_node": "node-28",
        "cpu_used": 1.8,
        "expected_effect": "极差 -3.1%"
      }
    ],
    "current_range": 35.5,
    "expected_range": 18.2,
    "node_status": {
      "before": {"node-15": 45.2, "node-32": 9.7, ...},
      "after": {"node-15": 32.1, "node-32": 18.5, ...}
    },
    "timestamp": "2024-01-09T10:30:00",
    "config": {
      "imbalance_threshold": 30,
      "balance_target": 20,
      "min_replicas": 2
    }
  }
}
```

**无需均衡时的响应：**
```json
{
  "success": true,
  "data": {
    "migrations": [],
    "current_range": 18.5,
    "message": "当前极差 18.5% <= 阈值 30%，无需均衡"
  }
}
```

---

### 5. 获取待确认的迁移计划

获取上次分析生成的待确认迁移计划。

```bash
GET /api/load-balance/plan
```

**响应示例：**
```json
{
  "success": true,
  "data": {
    "migrations": [...],
    "current_range": 35.5,
    "expected_range": 18.2,
    ...
  }
}
```

如果没有待确认的计划，`data` 为 `null`。

---

### 6. 执行迁移计划

确认并执行迁移计划。支持部分执行。

```bash
POST /api/load-balance/execute?env=<集群名称>
Content-Type: application/json

{
  "migrations": [
    {
      "pod_name": "app-abc-xyz-12345",
      "namespace": "production",
      "deployment": "app-abc",
      "source_node": "node-15",
      "target_node": "node-32",
      "cpu_used": 2.5
    }
  ]
}
```

**参数：**
- `env`: K8S集群名称（必填）
- `migrations`: 要执行的迁移列表（可以是分析结果的子集）

**响应示例：**
```json
{
  "success": true,
  "data": {
    "total": 1,
    "success": 1,
    "failed": 0,
    "results": [
      {
        "success": true,
        "old_pod": "app-abc-xyz-12345",
        "new_pod": "app-abc-xyz-67890",
        "target_node": "node-32",
        "migration": {...}
      }
    ]
  }
}
```

**部分失败时的响应：**
```json
{
  "success": true,
  "data": {
    "total": 3,
    "success": 2,
    "failed": 1,
    "results": [
      {"success": true, ...},
      {"success": true, ...},
      {"success": false, "error": "等待新Pod调度超时", "old_pod": "..."}
    ]
  }
}
```

---

### 7. 获取隔离Pod列表

获取已被隔离的Pod列表。

```bash
GET /api/load-balance/isolated?env=<集群名称>
```

**参数：**
- `env`: K8S集群名称（必填）
- `exclude_namespaces`: 排除的namespace，逗号分隔（可选，默认：kube-system,kube-public,istio-system）

**响应示例：**
```json
{
  "success": true,
  "data": [
    {
      "name": "app-abc-xyz-12345",
      "namespace": "production",
      "node_name": "node-15",
      "app_label": "app-abc-ISOLATED",
      "created_at": "2024-01-09T10:30:00",
      "status": "Running"
    }
  ]
}
```

---

### 8. 清理隔离Pod

删除已隔离的Pod。

```bash
POST /api/load-balance/cleanup?env=<集群名称>
Content-Type: application/json

{
  "pods": [
    {
      "name": "app-abc-xyz-12345",
      "namespace": "production"
    }
  ]
}
```

**参数：**
- `env`: K8S集群名称（必填）
- `pods`: 要清理的Pod列表

**响应示例：**
```json
{
  "success": true,
  "data": {
    "success": ["app-abc-xyz-12345"],
    "failed": [],
    "total": 1
  }
}
```

---

### 9. 获取操作日志

获取负载均衡操作日志。

```bash
GET /api/load-balance/logs?limit=50
```

**参数：**
- `limit`: 返回的日志条数（可选，默认50）

**响应示例：**
```json
{
  "success": true,
  "data": [
    {
      "timestamp": "2024-01-09T10:30:00",
      "action": "analyze_start",
      "message": "开始分析负载",
      "details": null
    },
    {
      "timestamp": "2024-01-09T10:30:15",
      "action": "analyze_done",
      "message": "分析完成，生成 5 个迁移计划",
      "details": {"current_range": 35.5, "expected_range": 18.2}
    },
    {
      "timestamp": "2024-01-09T10:35:00",
      "action": "execute_start",
      "message": "开始执行迁移，共 3 个Pod"
    },
    {
      "timestamp": "2024-01-09T10:38:00",
      "action": "execute_done",
      "message": "迁移完成: 3/3 成功",
      "details": {"total": 3, "success": 3, "failed": 0}
    }
  ]
}
```

---

## 使用示例

### 完整操作流程（使用curl）

```bash
# 1. 查看当前配置
curl -s http://kubedoor-master/api/load-balance/config | jq

# 2. 更新配置（启用功能，设置阈值）
curl -s -X PUT http://kubedoor-master/api/load-balance/config \
  -H "Content-Type: application/json" \
  -d '{
    "enabled": true,
    "imbalance_threshold": 30,
    "balance_target": 20,
    "min_replicas": 2,
    "blacklist": ["kube-system/coredns"]
  }' | jq

# 3. 触发分析
curl -s -X POST "http://kubedoor-master/api/load-balance/analyze?env=prod-cluster" | jq

# 4. 查看生成的迁移计划
curl -s "http://kubedoor-master/api/load-balance/plan" | jq

# 5. 执行部分迁移（只执行前2个）
curl -s -X POST "http://kubedoor-master/api/load-balance/execute?env=prod-cluster" \
  -H "Content-Type: application/json" \
  -d '{
    "migrations": [
      {"pod_name": "app-abc-xyz-12345", "namespace": "production", "deployment": "app-abc", "source_node": "node-15", "target_node": "node-32"},
      {"pod_name": "svc-def-67890", "namespace": "production", "deployment": "svc-def", "source_node": "node-15", "target_node": "node-28"}
    ]
  }' | jq

# 6. 查看隔离的Pod
curl -s "http://kubedoor-master/api/load-balance/isolated?env=prod-cluster" | jq

# 7. 清理隔离的Pod
curl -s -X POST "http://kubedoor-master/api/load-balance/cleanup?env=prod-cluster" \
  -H "Content-Type: application/json" \
  -d '{
    "pods": [
      {"name": "app-abc-xyz-12345", "namespace": "production"}
    ]
  }' | jq

# 8. 查看操作日志
curl -s "http://kubedoor-master/api/load-balance/logs?limit=20" | jq
```

### 查看当前状态

```bash
curl -s http://kubedoor-master/api/load-balance/status | jq
```

---

## 错误处理

所有接口在发生错误时返回以下格式：

```json
{
  "success": false,
  "error": "错误描述信息"
}
```

常见错误：
- `400`: 参数错误（如缺少必填参数）
- `404`: 目标客户端不在线
- `500`: 服务器内部错误
- `504`: Agent响应超时

---

## 注意事项

1. **执行迁移前请确认**：迁移操作会隔离Pod，虽然Deployment会自动创建新Pod，但仍建议在低峰期操作
2. **部分执行**：可以只执行迁移计划的一部分，方便逐步验证
3. **隔离Pod清理**：被隔离的Pod不会自动删除，需要手动清理
4. **反亲和处理**：算法会自动考虑Pod反亲和策略，确保新Pod能够调度成功
5. **黑名单**：可以将关键服务加入黑名单，避免被迁移
