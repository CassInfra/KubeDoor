<template>
  <div class="vs-detail-container">
    <!-- 页面标题 -->
    <div class="page-header">
      <el-button
        type="primary"
        class="back-btn"
        :icon="ArrowLeft"
        @click="goBack"
      >
        返回
      </el-button>
      <h2 class="page-title">VirtualService 详情</h2>
    </div>

    <!-- 加载状态 -->
    <div v-if="loading" class="loading-container">
      <el-skeleton :rows="8" animated />
    </div>

    <!-- 详情内容 -->
    <div v-else-if="detailData" class="detail-content">
      <!-- K8S集群卡片 -->
      <el-card class="cluster-card" shadow="hover">
        <template #header>
          <div class="card-header">
            <div class="header-left">
              <el-icon class="header-icon">
                <Monitor />
              </el-icon>
              <span class="header-title">所属K8S集群</span>
            </div>
            <div class="header-actions">
              <el-button
                v-if="!isEditingClusters"
                type="primary"
                size="small"
                :icon="Edit"
                @click="startEditClusters"
              >
                编辑
              </el-button>
              <div v-else class="edit-actions">
                <el-button
                  type="success"
                  size="small"
                  :loading="savingClusters"
                  @click="saveClusters"
                >
                  保存
                </el-button>
                <el-button size="small" @click="cancelEditClusters">
                  取消
                </el-button>
              </div>
            </div>
          </div>
        </template>
        <div class="cluster-list">
          <div
            v-for="(cluster, index) in detailData.k8s_clusters"
            :key="cluster.id"
            class="cluster-item"
            :class="{ editing: isEditingClusters }"
          >
            <div v-if="temp_isEditingClusters" class="cluster-actions">
              <el-button
                type="danger"
                size="small"
                :icon="Close"
                circle
                @click="removeCluster(index)"
              />
            </div>
            <div v-else-if="!isEditingClusters" class="cluster-deploy-actions">
              <el-button
                type="primary"
                size="small"
                @click="deployToCluster(cluster.name || cluster.k8s_name)"
              >
                下发
              </el-button>
            </div>
            <div class="cluster-info">
              <div class="cluster-name">
                <el-tag type="primary" size="large">
                  {{ cluster.name || cluster.k8s_name }}
                </el-tag>
              </div>
              <div class="cluster-meta">
                <span class="meta-item">
                  <el-icon>
                    <Clock />
                  </el-icon>
                  更新时间: {{ cluster.updated_at }}
                </span>
              </div>
            </div>
          </div>
          <div v-if="isEditingClusters" class="add-cluster-item">
            <el-button
              type="primary"
              :icon="Plus"
              class="add-cluster-btn"
              @click="showAddClusterDialogFunc"
            >
              添加集群
            </el-button>
          </div>
        </div>
      </el-card>

      <!-- VirtualService明细卡片 -->
      <el-card class="detail-card" shadow="hover">
        <template #header>
          <div class="card-header">
            <div class="header-left">
              <el-icon class="header-icon">
                <Document />
              </el-icon>
              <span class="header-title">VirtualService 明细</span>
            </div>
            <div class="header-actions">
              <el-button
                type="primary"
                size="small"
                :icon="Edit"
                @click="showEditVSDialogFunc"
              >
                编辑
              </el-button>
            </div>
          </div>
        </template>
        <div class="detail-container">
          <div class="detail-left">
            <!-- 第一行：ID、命名空间、名称 -->
            <div class="detail-row row-first">
              <div class="detail-item">
                <span class="detail-label">ID: </span>
                <span class="detail-value">{{ detailData.id }}</span>
              </div>
              <div class="detail-item">
                <span class="detail-label">命名空间: </span>
                <span class="detail-value">
                  <el-tag type="warning">{{ detailData.namespace }}</el-tag>
                </span>
              </div>
              <div class="detail-item">
                <span class="detail-label">名称: </span>
                <span
                  class="detail-value"
                  style="color: #e74c3c; font-weight: bold; font-size: 1.1em"
                  >{{ detailData.name }}</span
                >
              </div>
            </div>

            <!-- 第二行：默认转发方式、超时时间、更新时间 -->
            <div class="detail-row row-second">
              <div class="detail-item">
                <span class="detail-label">更新时间: </span>
                <span class="detail-value">{{ detailData.updated_at }}</span>
              </div>
              <div class="detail-item">
                <span class="detail-label">默认路由超时: </span>
                <span class="detail-value">{{
                  detailData.df_forward_timeout || "未设置"
                }}</span>
              </div>
              <div class="detail-item">
                <span class="detail-label">默认转发方式: </span>
                <span class="detail-value">
                  <el-tag type="success">{{
                    detailData.df_forward_type
                  }}</el-tag>
                </span>
              </div>
            </div>

            <!-- 第三行：网关、主机列表 -->
            <div class="detail-row row-third">
              <div class="detail-item">
                <span class="detail-label">网关: </span>
                <span class="detail-value">
                  <el-tag
                    v-for="(gateway, index) in parseJsonArray(
                      detailData.gateways
                    )"
                    :key="index"
                    type="primary"
                    class="gateway-tag"
                  >
                    {{ gateway }}
                  </el-tag>
                </span>
              </div>
              <div class="detail-item detail-hosts">
                <span class="detail-label">主机列表: </span>
                <span class="detail-value">
                  <el-tag
                    v-for="(host, index) in parseJsonArray(detailData.hosts)"
                    :key="index"
                    type="primary"
                    class="host-tag"
                  >
                    {{ host }}
                  </el-tag>
                </span>
              </div>
            </div>
          </div>

          <!-- 默认路由详情：占满右侧三行高度 -->
          <div class="detail-right">
            <div class="detail-item detail-forward">
              <span class="detail-label">默认路由详情: </span>
              <div class="detail-value">
                <el-input
                  :model-value="formatJsonString(detailData.df_forward_detail)"
                  type="textarea"
                  :rows="8"
                  readonly
                  class="json-textarea"
                />
              </div>
            </div>
          </div>
        </div>
      </el-card>

      <!-- HTTP路由表格 -->
      <el-card class="route-card" shadow="hover">
        <template #header>
          <div class="route-card-header">
            <div class="header-left">
              <el-icon class="header-icon">
                <Document />
              </el-icon>
              <span class="header-title">HTTP路由规则</span>
              <el-button
                type="primary"
                :icon="Plus"
                class="add-route-btn"
                style="margin-left: 20px"
                @click="showCreateRouteDialog = true"
              >
                新增路由
              </el-button>
            </div>
            <div class="header-right">
              <el-input
                v-model="routeSearchText"
                placeholder="搜索路由规则..."
                clearable
                style="width: 200px"
                @keyup.enter="handleSearch"
              />
              <el-button
                type="primary"
                style="margin-left: 8px"
                @click="handleSearch"
              >
                搜索
              </el-button>
            </div>
          </div>
        </template>
        <div v-if="routeLoading" class="loading-container">
          <el-skeleton :rows="5" animated />
        </div>
        <div v-else>
          <el-table
            :data="filteredRouteData"
            stripe
            border
            style="width: 100%"
            empty-text="暂无路由规则"
            :default-sort="{ prop: 'priority', order: 'ascending' }"
          >
            <el-table-column prop="id" label="ID" width="50" />
            <el-table-column
              prop="priority"
              label="优先"
              width="80"
              sortable
              align="center"
            />
            <el-table-column
              prop="name"
              label="名称"
              width="200"
              show-overflow-tooltip
              sortable
            />
            <el-table-column
              prop="match_rules"
              label="匹配规则"
              min-width="200"
            >
              <template #default="{ row }">
                <el-tooltip placement="top" raw-content trigger="click">
                  <template #content>
                    <pre style="white-space: pre-wrap; margin: 0">{{
                      formatJsonString(row.match_rules)
                    }}</pre>
                  </template>
                  <span
                    style="
                      white-space: nowrap;
                      overflow: hidden;
                      text-overflow: ellipsis;
                      display: block;
                    "
                    >{{ formatJsonString(row.match_rules) }}</span
                  >
                </el-tooltip>
              </template>
            </el-table-column>
            <el-table-column
              prop="rewrite_rules"
              label="重写规则"
              min-width="80"
            >
              <template #default="{ row }">
                <span v-if="row.rewrite_rules">
                  <el-tooltip placement="top" raw-content trigger="click">
                    <template #content>
                      <pre style="white-space: pre-wrap; margin: 0">{{
                        formatJsonString(row.rewrite_rules)
                      }}</pre>
                    </template>
                    <span
                      style="
                        white-space: nowrap;
                        overflow: hidden;
                        text-overflow: ellipsis;
                        display: block;
                      "
                      >{{ formatJsonString(row.rewrite_rules) }}</span
                    >
                  </el-tooltip>
                </span>
                <span v-else class="no-data">-</span>
              </template>
            </el-table-column>
            <el-table-column
              prop="forward_type"
              label="类型"
              width="80"
              sortable
              align="center"
            >
              <template #default="{ row }">
                <el-tag
                  :type="row.forward_type === 'route' ? 'primary' : 'warning'"
                >
                  {{ row.forward_type }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column
              prop="forward_detail"
              label="转发详情"
              min-width="250"
            >
              <template #default="{ row }">
                <el-tooltip placement="top" raw-content trigger="click">
                  <template #content>
                    <pre style="white-space: pre-wrap; margin: 0">{{
                      formatJsonString(row.forward_detail)
                    }}</pre>
                  </template>
                  <span
                    style="
                      white-space: nowrap;
                      overflow: hidden;
                      text-overflow: ellipsis;
                      display: block;
                    "
                    >{{ formatJsonString(row.forward_detail) }}</span
                  >
                </el-tooltip>
              </template>
            </el-table-column>
            <el-table-column
              prop="timeout"
              label="超时"
              width="80"
              sortable
              :sort-method="sortTimeout"
            >
              <template #default="{ row }">
                <span v-if="row.timeout">{{ row.timeout }}</span>
                <span v-else class="no-data">-</span>
              </template>
            </el-table-column>
            <el-table-column
              prop="updated_at"
              label="更新时间"
              width="180"
              sortable
            />
            <el-table-column label="操作" width="100" fixed="right">
              <template #default="{ row, $index }">
                <el-button
                  type="primary"
                  size="small"
                  :icon="Edit"
                  @click="openEditRouteDialog(row, $index)"
                >
                  编辑
                </el-button>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </el-card>
    </div>

    <!-- 错误状态 -->
    <div v-else class="error-container">
      <el-empty description="加载失败，请重试" />
    </div>

    <!-- 创建HTTP路由对话框 -->
    <el-dialog
      v-model="showCreateRouteDialog"
      title="新增HTTP路由规则"
      width="55%"
      :close-on-click-modal="false"
    >
      <el-form
        ref="createRouteFormRef"
        :model="createRouteForm"
        :rules="createRouteFormRules"
        label-width="120px"
      >
        <!-- 基本信息 -->
        <el-row :gutter="20">
          <el-col :span="10">
            <el-form-item label="路由名称" prop="name">
              <el-input v-model="createRouteForm.name" placeholder="选填" />
            </el-form-item>
          </el-col>
          <el-col :span="7">
            <el-form-item label="优先级" prop="priority">
              <el-input-number
                v-model="createRouteForm.priority"
                :min="1"
                :max="1000"
                placeholder="选填"
                style="width: 100%"
              />
            </el-form-item>
          </el-col>
          <el-col :span="7">
            <el-form-item label="超时时间" prop="timeout">
              <el-input
                v-model="createRouteForm.timeout"
                placeholder="10s (选填)"
              />
            </el-form-item>
          </el-col>
        </el-row>

        <!-- 匹配规则 -->
        <el-form-item label="匹配规则">
          <div class="match-rules-container">
            <div
              v-for="(rule, index) in createRouteForm.match_rules"
              :key="index"
              class="match-rule-item"
            >
              <el-card shadow="never" class="rule-card">
                <template #header>
                  <div class="rule-header">
                    <span>匹配规则 {{ index + 1 }}</span>
                    <el-button
                      v-if="createRouteForm.match_rules.length > 1"
                      type="danger"
                      size="small"
                      :icon="Delete"
                      @click="removeMatchRule(index)"
                    />
                  </div>
                </template>

                <!-- URI匹配 -->
                <el-row :gutter="10">
                  <el-col :span="3">
                    <el-checkbox v-model="rule.uri.enabled" label="URI" />
                  </el-col>
                  <el-col :span="6">
                    <el-select
                      v-model="rule.uri.type"
                      :disabled="!rule.uri.enabled"
                      placeholder="匹配类型"
                      style="width: 100%"
                    >
                      <el-option label="prefix" value="prefix" />
                      <el-option label="exact" value="exact" />
                      <el-option label="regex" value="regex" />
                    </el-select>
                  </el-col>
                  <el-col :span="15">
                    <el-input
                      v-model="rule.uri.value"
                      :disabled="!rule.uri.enabled"
                      placeholder="URI值"
                    />
                  </el-col>
                </el-row>

                <!-- Authority匹配 -->
                <el-row :gutter="10" style="margin-top: 16px">
                  <el-col :span="3">
                    <el-checkbox
                      v-model="rule.authority.enabled"
                      label="Authority"
                    />
                  </el-col>
                  <el-col :span="6">
                    <el-select
                      v-model="rule.authority.type"
                      :disabled="!rule.authority.enabled"
                      placeholder="匹配类型"
                      style="width: 100%"
                    >
                      <el-option label="exact" value="exact" />
                      <el-option label="prefix" value="prefix" />
                      <el-option label="regex" value="regex" />
                    </el-select>
                  </el-col>
                  <el-col :span="15">
                    <el-input
                      v-model="rule.authority.value"
                      :disabled="!rule.authority.enabled"
                      placeholder="Domain值"
                    />
                  </el-col>
                </el-row>

                <!-- Headers匹配 -->
                <div style="margin-top: 16px">
                  <el-row v-if="rule.headers.enabled" :gutter="12">
                    <el-col :span="3">
                      <el-checkbox
                        v-model="rule.headers.enabled"
                        label="Headers"
                      />
                    </el-col>
                    <el-col :span="21">
                      <div
                        v-for="(header, headerIndex) in rule.headers.items"
                        :key="headerIndex"
                        class="header-item"
                        style="margin-bottom: 8px"
                      >
                        <el-row :gutter="10">
                          <el-col :span="6">
                            <el-input
                              v-model="header.key"
                              placeholder="Header Key"
                            />
                          </el-col>
                          <el-col :span="6">
                            <el-select
                              v-model="header.type"
                              placeholder="匹配类型"
                              style="width: 100%"
                            >
                              <el-option label="prefix" value="prefix" />
                              <el-option label="exact" value="exact" />
                              <el-option label="regex" value="regex" />
                            </el-select>
                          </el-col>
                          <el-col :span="8">
                            <el-input
                              v-model="header.value"
                              placeholder="Header Value"
                            />
                          </el-col>
                          <el-col :span="4">
                            <el-button
                              v-if="rule.headers.items.length > 1"
                              type="danger"
                              size="small"
                              :icon="Delete"
                              @click="removeHeader(index, headerIndex)"
                            />
                            <el-button
                              type="primary"
                              size="small"
                              :icon="Plus"
                              @click="addHeader(index)"
                            />
                          </el-col>
                        </el-row>
                      </div>
                    </el-col>
                  </el-row>
                  <div v-else>
                    <el-checkbox
                      v-model="rule.headers.enabled"
                      label="Headers"
                    />
                  </div>
                </div>
              </el-card>
            </div>
            <el-button
              type="primary"
              :icon="Plus"
              class="add-rule-btn"
              @click="addMatchRule"
            >
              添加匹配规则
            </el-button>
          </div>
        </el-form-item>

        <!-- 重写规则和转发类型 -->
        <el-row :gutter="24">
          <el-col :span="12">
            <el-form-item label="重写规则">
              <el-row :gutter="12">
                <el-col :span="8">
                  <el-checkbox
                    v-model="createRouteForm.rewrite_rules.enabled"
                    label="URI重写"
                  />
                </el-col>
                <el-col :span="16">
                  <el-input
                    v-model="createRouteForm.rewrite_rules.uri"
                    :disabled="!createRouteForm.rewrite_rules.enabled"
                    placeholder="重写后的URI"
                  />
                </el-col>
              </el-row>
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="转发类型" prop="forward_type">
              <el-select
                v-model="createRouteForm.forward_type"
                placeholder="请选择转发类型"
                style="width: 100%"
                @change="handleForwardTypeChange"
              >
                <el-option label="route" value="route" />
                <el-option label="delegate" value="delegate" />
              </el-select>
            </el-form-item>
          </el-col>
        </el-row>

        <!-- Route转发详情 -->
        <div v-if="createRouteForm.forward_type === 'route'">
          <el-form-item label="转发详情">
            <div class="forward-details-container">
              <div
                v-for="(detail, index) in createRouteForm.forward_detail"
                :key="index"
                class="forward-detail-item"
              >
                <el-card shadow="never" class="detail-card">
                  <template #header>
                    <div class="detail-header">
                      <span>转发目标 {{ index + 1 }}</span>
                      <el-button
                        v-if="createRouteForm.forward_detail.length > 1"
                        type="danger"
                        size="small"
                        :icon="Delete"
                        @click="removeForwardDetail(index)"
                      />
                    </div>
                  </template>

                  <!-- Service和Namespace -->
                  <el-row :gutter="20">
                    <el-col :span="24">
                      <el-form-item label="Service" label-width="60px">
                        <div
                          style="display: flex; align-items: center; gap: 8px"
                        >
                          <el-select
                            v-model="detail.service"
                            filterable
                            allow-create
                            placeholder="请先选择命名空间，再选服务名"
                            style="flex: 1; min-width: 300px"
                            @focus="
                              () =>
                                fetchServiceOptions(
                                  route.query.env as string,
                                  detail.namespace
                                )
                            "
                          >
                            <el-option
                              v-for="service in serviceOptions"
                              :key="service"
                              :label="service"
                              :value="service"
                            />
                          </el-select>
                          <span>.</span>
                          <el-select
                            v-model="detail.namespace"
                            filterable
                            allow-create
                            placeholder="命名空间"
                            style="flex: 1; min-width: 100px"
                            @focus="
                              () =>
                                fetchNamespaceOptions(route.query.env as string)
                            "
                            @change="
                              () =>
                                fetchServiceOptions(
                                  route.query.env as string,
                                  detail.namespace
                                )
                            "
                          >
                            <el-option
                              v-for="namespace in namespaceOptions"
                              :key="namespace"
                              :label="namespace"
                              :value="namespace"
                            />
                          </el-select>
                          <span>.svc.cluster.local</span>
                        </div>
                      </el-form-item>
                    </el-col>
                  </el-row>

                  <!-- 端口、subset和权重 -->
                  <el-row :gutter="20" style="margin-top: 16px">
                    <el-col :span="8">
                      <el-form-item label="端口" label-width="60px">
                        <div
                          style="display: flex; align-items: center; gap: 8px"
                        >
                          <el-input-number
                            v-model="detail.port"
                            :min="0"
                            :max="65535"
                            controls-position="right"
                            placeholder="端口号"
                            style="width: 120px"
                          />
                          <el-button
                            :icon="Refresh"
                            type="primary"
                            plain
                            size="small"
                            title="获取服务第一个端口号"
                            @click="refreshPort(detail)"
                          />
                        </div>
                      </el-form-item>
                    </el-col>
                    <el-col :span="8">
                      <el-form-item>
                        <div
                          style="display: flex; align-items: center; gap: 8px"
                        >
                          <el-checkbox
                            v-model="detail.subset.enabled"
                            label="Subset"
                          />
                          <el-input
                            v-model="detail.subset.value"
                            :disabled="!detail.subset.enabled"
                            placeholder="subset值"
                            style="flex: 1"
                          />
                        </div>
                      </el-form-item>
                    </el-col>
                    <el-col :span="8">
                      <el-form-item>
                        <div
                          style="display: flex; align-items: center; gap: 8px"
                        >
                          <el-checkbox
                            v-model="detail.weight.enabled"
                            label="权重"
                          />
                          <el-input-number
                            v-model="detail.weight.value"
                            :disabled="!detail.weight.enabled"
                            :min="0"
                            :max="100"
                            placeholder="权重值"
                            style="flex: 0.9"
                          />
                        </div>
                      </el-form-item>
                    </el-col>
                  </el-row>

                  <!-- Headers -->
                  <el-row :gutter="20" style="margin-top: 16px">
                    <el-col :span="24">
                      <el-form-item label="Headers" label-width="60px">
                        <div
                          style="
                            display: flex;
                            align-items: flex-start;
                            gap: 8px;
                          "
                        >
                          <el-checkbox
                            v-model="detail.headers.enabled"
                            label="启用"
                            style="margin-top: 1px"
                          />
                          <el-input
                            v-model="detail.headers.value"
                            :disabled="!detail.headers.enabled"
                            type="textarea"
                            :rows="1"
                            placeholder='默认：{"request": {"set": {"X-Forwarded-Proto": "https"}}}'
                            style="flex: 1; min-width: 560px"
                          />
                        </div>
                      </el-form-item>
                    </el-col>
                  </el-row>
                </el-card>
              </div>
              <el-button
                type="primary"
                :icon="Plus"
                class="add-detail-btn"
                @click="addForwardDetail"
              >
                添加转发目标
              </el-button>
            </div>
          </el-form-item>
        </div>

        <!-- Delegate转发详情 -->
        <div v-if="createRouteForm.forward_type === 'delegate'">
          <el-row :gutter="20">
            <el-col :span="12">
              <el-form-item label="VS名称" prop="delegate_name">
                <el-input
                  v-model="createRouteForm.delegate_name"
                  placeholder="请输入下级VS的名称"
                />
              </el-form-item>
            </el-col>
            <el-col :span="12">
              <el-form-item label="命名空间" prop="delegate_namespace">
                <el-input
                  v-model="createRouteForm.delegate_namespace"
                  placeholder="请输入下级VS的命名空间"
                />
              </el-form-item>
            </el-col>
          </el-row>
        </div>
      </el-form>

      <template #footer>
        <div class="dialog-footer">
          <el-button @click="showCreateRouteDialog = false">取消</el-button>
          <el-button
            type="primary"
            :loading="createRouteLoading"
            @click="handleCreateRoute"
          >
            确认
          </el-button>
        </div>
      </template>
    </el-dialog>

    <!-- 编辑HTTP路由对话框 -->
    <el-dialog
      v-model="showEditRouteDialog"
      title="编辑HTTP路由规则"
      width="55%"
      :close-on-click-modal="false"
    >
      <el-form
        ref="editRouteFormRef"
        :model="editRouteForm"
        :rules="editRouteFormRules"
        label-width="120px"
      >
        <!-- 基本信息 -->
        <el-row :gutter="20">
          <el-col :span="10">
            <el-form-item label="路由名称" prop="name">
              <el-input v-model="editRouteForm.name" placeholder="选填" />
            </el-form-item>
          </el-col>
          <el-col :span="7">
            <el-form-item label="优先级" prop="priority">
              <el-input-number
                v-model="editRouteForm.priority"
                :min="1"
                :max="1000"
                placeholder="选填"
                style="width: 100%"
              />
            </el-form-item>
          </el-col>
          <el-col :span="7">
            <el-form-item label="超时时间" prop="timeout">
              <el-input
                v-model="editRouteForm.timeout"
                placeholder="10s (选填)"
              />
            </el-form-item>
          </el-col>
        </el-row>

        <!-- 匹配规则 -->
        <el-form-item label="匹配规则">
          <div class="match-rules-container">
            <div
              v-for="(rule, index) in editRouteForm.match_rules"
              :key="index"
              class="match-rule-item"
            >
              <el-card shadow="never" class="rule-card">
                <template #header>
                  <div class="rule-header">
                    <span>匹配规则 {{ index + 1 }}</span>
                    <el-button
                      v-if="editRouteForm.match_rules.length > 1"
                      type="danger"
                      size="small"
                      :icon="Delete"
                      @click="removeEditMatchRule(index)"
                    />
                  </div>
                </template>

                <!-- URI匹配 -->
                <el-row :gutter="10">
                  <el-col :span="3">
                    <el-checkbox v-model="rule.uri.enabled" label="URI" />
                  </el-col>
                  <el-col :span="6">
                    <el-select
                      v-model="rule.uri.type"
                      :disabled="!rule.uri.enabled"
                      placeholder="匹配类型"
                      style="width: 100%"
                    >
                      <el-option label="prefix" value="prefix" />
                      <el-option label="exact" value="exact" />
                      <el-option label="regex" value="regex" />
                    </el-select>
                  </el-col>
                  <el-col :span="15">
                    <el-input
                      v-model="rule.uri.value"
                      :disabled="!rule.uri.enabled"
                      placeholder="URI值"
                    />
                  </el-col>
                </el-row>

                <!-- Authority匹配 -->
                <el-row :gutter="10" style="margin-top: 16px">
                  <el-col :span="3">
                    <el-checkbox
                      v-model="rule.authority.enabled"
                      label="Authority"
                    />
                  </el-col>
                  <el-col :span="6">
                    <el-select
                      v-model="rule.authority.type"
                      :disabled="!rule.authority.enabled"
                      placeholder="匹配类型"
                      style="width: 100%"
                    >
                      <el-option label="exact" value="exact" />
                      <el-option label="prefix" value="prefix" />
                      <el-option label="regex" value="regex" />
                    </el-select>
                  </el-col>
                  <el-col :span="15">
                    <el-input
                      v-model="rule.authority.value"
                      :disabled="!rule.authority.enabled"
                      placeholder="Domain值"
                    />
                  </el-col>
                </el-row>

                <!-- Headers匹配 -->
                <div style="margin-top: 16px">
                  <el-row v-if="rule.headers.enabled" :gutter="12">
                    <el-col :span="3">
                      <el-checkbox
                        v-model="rule.headers.enabled"
                        label="Headers"
                      />
                    </el-col>
                    <el-col :span="21">
                      <div
                        v-for="(header, headerIndex) in rule.headers.items"
                        :key="headerIndex"
                        class="header-item"
                        style="margin-bottom: 8px"
                      >
                        <el-row :gutter="10">
                          <el-col :span="6">
                            <el-input
                              v-model="header.key"
                              placeholder="Header Key"
                            />
                          </el-col>
                          <el-col :span="6">
                            <el-select
                              v-model="header.type"
                              placeholder="匹配类型"
                              style="width: 100%"
                            >
                              <el-option label="prefix" value="prefix" />
                              <el-option label="exact" value="exact" />
                              <el-option label="regex" value="regex" />
                            </el-select>
                          </el-col>
                          <el-col :span="8">
                            <el-input
                              v-model="header.value"
                              placeholder="Header Value"
                            />
                          </el-col>
                          <el-col :span="4">
                            <el-button
                              v-if="rule.headers.items.length > 1"
                              type="danger"
                              size="small"
                              :icon="Delete"
                              @click="removeEditHeader(index, headerIndex)"
                            />
                            <el-button
                              type="primary"
                              size="small"
                              :icon="Plus"
                              @click="addEditHeader(index)"
                            />
                          </el-col>
                        </el-row>
                      </div>
                    </el-col>
                  </el-row>
                  <div v-else>
                    <el-checkbox
                      v-model="rule.headers.enabled"
                      label="Headers"
                    />
                  </div>
                </div>
              </el-card>
            </div>
            <el-button
              type="primary"
              :icon="Plus"
              class="add-rule-btn"
              @click="addEditMatchRule"
            >
              添加匹配规则
            </el-button>
          </div>
        </el-form-item>

        <!-- 重写规则和转发类型 -->
        <el-row :gutter="24">
          <el-col :span="12">
            <el-form-item label="重写规则">
              <el-row :gutter="12">
                <el-col :span="8">
                  <el-checkbox
                    v-model="editRouteForm.rewrite_rules.enabled"
                    label="URI重写"
                  />
                </el-col>
                <el-col :span="16">
                  <el-input
                    v-model="editRouteForm.rewrite_rules.uri"
                    :disabled="!editRouteForm.rewrite_rules.enabled"
                    placeholder="重写后的URI"
                  />
                </el-col>
              </el-row>
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="转发类型" prop="forward_type">
              <el-select
                v-model="editRouteForm.forward_type"
                placeholder="请选择转发类型"
                style="width: 100%"
                @change="handleEditRouteForwardTypeChange"
              >
                <el-option label="route" value="route" />
                <el-option label="delegate" value="delegate" />
              </el-select>
            </el-form-item>
          </el-col>
        </el-row>

        <!-- Route转发详情 -->
        <div v-if="editRouteForm.forward_type === 'route'">
          <el-form-item label="转发详情">
            <div class="forward-details-container">
              <div
                v-for="(detail, index) in editRouteForm.forward_detail"
                :key="index"
                class="forward-detail-item"
              >
                <el-card shadow="never" class="detail-card">
                  <template #header>
                    <div class="detail-header">
                      <span>转发目标 {{ index + 1 }}</span>
                      <el-button
                        v-if="editRouteForm.forward_detail.length > 1"
                        type="danger"
                        size="small"
                        :icon="Delete"
                        @click="removeEditForwardDetail(index)"
                      />
                    </div>
                  </template>

                  <!-- Service和Namespace -->
                  <el-row :gutter="20">
                    <el-col :span="24">
                      <el-form-item label="Service" label-width="60px">
                        <div
                          style="display: flex; align-items: center; gap: 8px"
                        >
                          <el-select
                            v-model="detail.service"
                            filterable
                            allow-create
                            placeholder="服务名"
                            style="flex: 1; min-width: 300px"
                            @focus="
                              () =>
                                fetchServiceOptions(
                                  route.query.env as string,
                                  detail.namespace
                                )
                            "
                          >
                            <el-option
                              v-for="service in serviceOptions"
                              :key="service"
                              :label="service"
                              :value="service"
                            />
                          </el-select>
                          <span>.</span>
                          <el-select
                            v-model="detail.namespace"
                            filterable
                            allow-create
                            placeholder="命名空间"
                            style="flex: 1; min-width: 100px"
                            @focus="
                              () =>
                                fetchNamespaceOptions(route.query.env as string)
                            "
                            @change="
                              () =>
                                fetchServiceOptions(
                                  route.query.env as string,
                                  detail.namespace
                                )
                            "
                          >
                            <el-option
                              v-for="namespace in namespaceOptions"
                              :key="namespace"
                              :label="namespace"
                              :value="namespace"
                            />
                          </el-select>
                          <span>.svc.cluster.local</span>
                        </div>
                      </el-form-item>
                    </el-col>
                  </el-row>

                  <!-- 端口、subset和权重 -->
                  <el-row :gutter="20" style="margin-top: 16px">
                    <el-col :span="8">
                      <el-form-item label="端口" label-width="60px">
                        <div
                          style="display: flex; align-items: center; gap: 8px"
                        >
                          <el-input-number
                            v-model="detail.port"
                            :min="0"
                            :max="65535"
                            controls-position="right"
                            placeholder="端口号"
                            style="width: 120px"
                          />
                          <el-button
                            :icon="Refresh"
                            type="primary"
                            plain
                            size="small"
                            title="获取服务第一个端口号"
                            @click="refreshPort(detail)"
                          />
                        </div>
                      </el-form-item>
                    </el-col>
                    <el-col :span="8">
                      <el-form-item>
                        <div
                          style="display: flex; align-items: center; gap: 8px"
                        >
                          <el-checkbox
                            v-model="detail.subset.enabled"
                            label="Subset"
                          />
                          <el-input
                            v-model="detail.subset.value"
                            :disabled="!detail.subset.enabled"
                            placeholder="subset值"
                            style="flex: 1"
                          />
                        </div>
                      </el-form-item>
                    </el-col>
                    <el-col :span="8">
                      <el-form-item>
                        <div
                          style="display: flex; align-items: center; gap: 8px"
                        >
                          <el-checkbox
                            v-model="detail.weight.enabled"
                            label="权重"
                          />
                          <el-input-number
                            v-model="detail.weight.value"
                            :disabled="!detail.weight.enabled"
                            :min="0"
                            :max="100"
                            placeholder="权重值"
                            style="flex: 0.9"
                          />
                        </div>
                      </el-form-item>
                    </el-col>
                  </el-row>

                  <!-- Headers -->
                  <el-row :gutter="20" style="margin-top: 16px">
                    <el-col :span="24">
                      <el-form-item label="Headers" label-width="60px">
                        <div
                          style="
                            display: flex;
                            align-items: flex-start;
                            gap: 8px;
                          "
                        >
                          <el-checkbox
                            v-model="detail.headers.enabled"
                            label="启用"
                            style="margin-top: 1px"
                          />
                          <el-input
                            v-model="detail.headers.value"
                            :disabled="!detail.headers.enabled"
                            type="textarea"
                            :rows="1"
                            placeholder='{"request": {"set": {"X-Forwarded-Proto": "https"}}}'
                            style="flex: 1; min-width: 560px"
                          />
                        </div>
                      </el-form-item>
                    </el-col>
                  </el-row>
                </el-card>
              </div>
              <el-button
                type="primary"
                :icon="Plus"
                class="add-detail-btn"
                @click="addEditForwardDetail"
              >
                添加转发目标
              </el-button>
            </div>
          </el-form-item>
        </div>

        <!-- Delegate转发详情 -->
        <div v-if="editRouteForm.forward_type === 'delegate'">
          <el-row :gutter="20">
            <el-col :span="12">
              <el-form-item label="VS名称" prop="delegate_name">
                <el-input
                  v-model="editRouteForm.delegate_name"
                  placeholder="请输入下级VS的名称"
                />
              </el-form-item>
            </el-col>
            <el-col :span="12">
              <el-form-item label="命名空间" prop="delegate_namespace">
                <el-input
                  v-model="editRouteForm.delegate_namespace"
                  placeholder="请输入下级VS的命名空间"
                />
              </el-form-item>
            </el-col>
          </el-row>
        </div>
      </el-form>

      <template #footer>
        <div class="dialog-footer">
          <el-button @click="showEditRouteDialog = false">取消</el-button>
          <el-button
            type="primary"
            :loading="editRouteLoading"
            @click="handleEditRoute"
          >
            确认
          </el-button>
        </div>
      </template>
    </el-dialog>

    <!-- 添加集群对话框 -->
    <el-dialog
      v-model="showAddClusterDialog"
      title="添加K8S集群"
      width="500px"
      :close-on-click-modal="false"
    >
      <el-form label-width="100px">
        <el-form-item label="选择集群">
          <el-select
            v-model="selectedClusters"
            multiple
            placeholder="请选择要添加的K8S集群"
            style="width: 100%"
            filterable
          >
            <el-option
              v-for="cluster in filteredAvailableClusters"
              :key="cluster"
              :label="cluster"
              :value="cluster"
            />
          </el-select>
        </el-form-item>
      </el-form>

      <template #footer>
        <div class="dialog-footer">
          <el-button @click="showAddClusterDialog = false">取消</el-button>
          <el-button
            type="primary"
            :loading="addingClusters"
            @click="addClusters"
          >
            确认
          </el-button>
        </div>
      </template>
    </el-dialog>

    <!-- 编辑VirtualService对话框 -->
    <el-dialog
      v-model="showEditVSDialog"
      title="编辑VirtualService"
      width="600px"
      :close-on-click-modal="false"
    >
      <el-form
        ref="editVSFormRef"
        :model="editVSForm"
        :rules="editVSFormRules"
        label-width="140px"
      >
        <el-form-item label="VS ID">
          <el-input
            :model-value="editVSForm.id"
            disabled
            placeholder="ID不可编辑"
          />
        </el-form-item>

        <el-form-item label="VS名称" prop="name">
          <el-input
            v-model="editVSForm.name"
            placeholder="VirtualService名称不可编辑"
            disabled
          />
        </el-form-item>

        <el-form-item label="命名空间" prop="namespace">
          <el-input
            v-model="editVSForm.namespace"
            placeholder="命名空间不可编辑"
            disabled
          />
        </el-form-item>

        <el-form-item label="关联网关" prop="gateways">
          <el-input
            v-model="editVSForm.gateways"
            placeholder="请输入网关，多个用逗号分隔"
          />
        </el-form-item>

        <el-form-item label="Host列表" prop="hosts">
          <div class="hosts-input">
            <el-tag
              v-for="(host, index) in editVSForm.hosts"
              :key="index"
              closable
              style="margin-right: 8px; margin-bottom: 4px"
              @close="removeEditHost(host)"
            >
              {{ host }}
            </el-tag>
            <el-input
              v-if="editHostInputVisible"
              ref="editHostInputRef"
              v-model="editHostInputValue"
              size="small"
              style="width: 200px"
              @keyup.enter="handleEditHostInputConfirm"
              @blur="handleEditHostInputConfirm"
            />
            <el-button v-else size="small" @click="showEditHostInput">
              + 添加Host
            </el-button>
          </div>
        </el-form-item>

        <el-form-item label="默认规则">
          <el-select
            v-model="editVSForm.df_forward_type"
            placeholder="请选择默认规则类型"
            clearable
            @change="handleEditForwardTypeChange"
          >
            <el-option label="Route" value="route" />
            <el-option label="Delegate" value="delegate" />
          </el-select>
        </el-form-item>

        <!-- Route类型的字段 -->
        <template v-if="editVSForm.df_forward_type === 'route'">
          <el-form-item label="Service" prop="route_service">
            <div style="display: flex; align-items: center; gap: 8px">
              <el-select
                v-model="editVSForm.route_service"
                filterable
                allow-create
                placeholder="请先选择命名空间"
                style="flex: 1; min-width: 200px"
                @focus="
                  () =>
                    fetchServiceOptions(
                      route.query.env as string,
                      editVSForm.route_namespace
                    )
                "
              >
                <el-option
                  v-for="service in serviceOptions"
                  :key="service"
                  :label="service"
                  :value="service"
                />
              </el-select>
              <span>.</span>
              <el-select
                v-model="editVSForm.route_namespace"
                filterable
                allow-create
                placeholder="命名空间"
                style="flex: 1; min-width: 100px"
                @focus="() => fetchNamespaceOptions(route.query.env as string)"
                @change="
                  () =>
                    fetchServiceOptions(
                      route.query.env as string,
                      editVSForm.route_namespace
                    )
                "
              >
                <el-option
                  v-for="namespace in namespaceOptions"
                  :key="namespace"
                  :label="namespace"
                  :value="namespace"
                />
              </el-select>
              <span>.svc.cluster.local</span>
            </div>
          </el-form-item>

          <!-- 端口、刷新按钮和默认超时在同一行 -->
          <el-row :gutter="20">
            <el-col :span="12">
              <el-form-item label="端口" prop="route_port">
                <div style="display: flex; align-items: center; gap: 8px">
                  <el-input-number
                    v-model="editVSForm.route_port"
                    :min="0"
                    :max="65535"
                    placeholder="请输入端口号"
                    style="flex: 1"
                    controls-position="right"
                  />
                  <el-button
                    :icon="Refresh"
                    type="primary"
                    plain
                    size="small"
                    title="获取服务第一个端口号"
                    @click="refreshPortForEdit"
                  />
                </div>
              </el-form-item>
            </el-col>
            <el-col :span="12">
              <el-form-item label="默认超时" prop="df_forward_timeout">
                <el-input
                  v-model="editVSForm.df_forward_timeout"
                  placeholder="例如: 30s"
                />
              </el-form-item>
            </el-col>
          </el-row>

          <!-- Headers -->
          <el-form-item label="Headers">
            <div style="display: flex; align-items: flex-start; gap: 8px">
              <el-checkbox
                v-model="editVSForm.route_headers.enabled"
                label="启用"
                style="margin-top: 1px"
              />
              <el-input
                v-model="editVSForm.route_headers.value"
                :disabled="!editVSForm.route_headers.enabled"
                type="textarea"
                :rows="1"
                placeholder='默认：{"request": {"set": {"X-Forwarded-Proto": "https"}}}'
                style="flex: 1; min-width: 370px"
              />
            </div>
          </el-form-item>
        </template>

        <!-- Delegate类型的字段 -->
        <template v-if="editVSForm.df_forward_type === 'delegate'">
          <el-form-item label="VS名称" prop="delegate_name">
            <el-input
              v-model="editVSForm.delegate_name"
              placeholder="请输入委托名称"
            />
          </el-form-item>

          <el-form-item label="命名空间" prop="delegate_namespace">
            <el-input
              v-model="editVSForm.delegate_namespace"
              placeholder="请输入委托命名空间"
            />
          </el-form-item>
        </template>
      </el-form>

      <template #footer>
        <div class="dialog-footer">
          <el-button @click="cancelEditVS">取消</el-button>
          <el-button
            type="primary"
            :loading="editVSLoading"
            @click="confirmEditVS"
          >
            确认
          </el-button>
        </div>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, computed, nextTick } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";
import {
  ArrowLeft,
  Monitor,
  Document,
  Clock,
  Plus,
  Delete,
  Edit,
  Close,
  Refresh
} from "@element-plus/icons-vue";
import {
  getVirtualServiceDetail,
  updateVirtualServiceClusters,
  updateVirtualService,
  getHttpRoutes,
  createHttpRoute,
  updateHttpRoute,
  deployVirtualService,
  getServiceFirstPort,
  getAgentNames
} from "@/api/istio";
import { getPromNamespace, getPromServices } from "@/api/monit";

const route = useRoute();
const router = useRouter();

const loading = ref(false);
const detailData = ref<any>(null);
const routeLoading = ref(false);
const routeData = ref<any[]>([]);
const routeSearchText = ref("");
const actualSearchText = ref("");

// 编辑集群相关变量
const isEditingClusters = ref(false);
const savingClusters = ref(false);
const showAddClusterDialog = ref(false);
const addingClusters = ref(false);
const selectedClusters = ref<string[]>([]);
const availableClusters = ref<string[]>([]);
const originalClusters = ref<any[]>([]);

// 命名空间和服务选项
const namespaceOptions = ref<string[]>([]);
const serviceOptions = ref<string[]>([]);

// 计算属性：过滤掉已存在的集群
const filteredAvailableClusters = computed(() => {
  if (!detailData.value || !detailData.value.k8s_clusters) {
    return availableClusters.value;
  }
  const existingClusterNames = detailData.value.k8s_clusters.map(
    (cluster: any) => cluster.name || cluster.k8s_name
  );
  return availableClusters.value.filter(
    cluster => !existingClusterNames.includes(cluster)
  );
});

// 计算属性：过滤路由数据
const filteredRouteData = computed(() => {
  if (!actualSearchText.value.trim()) {
    return routeData.value;
  }
  const searchText = actualSearchText.value.toLowerCase();
  return routeData.value.filter((route: any) => {
    const name = route.name ? route.name.toLowerCase() : "";
    const matchRules = route.match_rules
      ? JSON.stringify(route.match_rules).toLowerCase()
      : "";
    const forwardDetail = route.forward_detail
      ? JSON.stringify(route.forward_detail).toLowerCase()
      : "";
    return (
      name.includes(searchText) ||
      matchRules.includes(searchText) ||
      forwardDetail.includes(searchText)
    );
  });
});

// 创建路由相关变量
const showCreateRouteDialog = ref(false);
const createRouteLoading = ref(false);
const createRouteFormRef = ref();

// 编辑VirtualService相关变量
const showEditVSDialog = ref(false);
const editVSLoading = ref(false);
const editVSFormRef = ref();
const editHostInputVisible = ref(false);
const editHostInputValue = ref("");
const editHostInputRef = ref();

// 编辑VirtualService表单数据
const editVSForm = ref({
  id: "",
  name: "",
  namespace: "",
  gateways: "",
  hosts: [] as string[],
  df_forward_type: "",
  route_service: "",
  route_namespace: "",
  route_port: 0,
  df_forward_timeout: "",
  delegate_name: "",
  delegate_namespace: "",
  route_headers: {
    enabled: false,
    value: '{"request": {"set": {"X-Forwarded-Proto": "https"}}}'
  }
});

// 编辑VirtualService表单验证规则
const editVSFormRules = {
  name: [{ required: true, message: "请输入VS名称", trigger: "blur" }],
  namespace: [{ required: true, message: "请输入命名空间", trigger: "blur" }],
  gateways: [{ required: true, message: "请输入关联网关", trigger: "blur" }],
  hosts: [{ required: true, message: "请添加至少一个Host", trigger: "change" }],
  route_service: [
    { required: true, message: "请输入Service名", trigger: "blur" }
  ],
  route_namespace: [
    { required: true, message: "请输入命名空间", trigger: "blur" }
  ],
  route_port: [{ required: true, message: "请输入端口号", trigger: "blur" }],
  df_forward_timeout: [
    { required: true, message: "请输入默认超时", trigger: "blur" }
  ],
  delegate_name: [{ required: true, message: "请输入VS名称", trigger: "blur" }],
  delegate_namespace: [
    { required: true, message: "请输入命名空间", trigger: "blur" }
  ]
};

// 创建路由表单数据
const createRouteForm = ref({
  name: "",
  priority: null,
  match_rules: [
    {
      uri: { enabled: false, type: "prefix", value: "" },
      authority: { enabled: false, type: "exact", value: "" },
      headers: {
        enabled: false,
        items: [{ key: "", type: "exact", value: "" }]
      }
    }
  ],
  rewrite_rules: {
    enabled: false,
    uri: ""
  },
  timeout: "",
  forward_type: "",
  forward_detail: [
    {
      service: "",
      namespace: "",
      port: null,
      subset: { enabled: false, value: "" },
      weight: { enabled: false, value: 100 },
      headers: {
        enabled: false,
        value: '{"request": {"set": {"X-Forwarded-Proto": "https"}}}'
      }
    }
  ],
  delegate_name: "",
  delegate_namespace: ""
});

// 表单验证规则
const createRouteFormRules = {
  forward_type: [
    { required: true, message: "请选择转发类型", trigger: "change" }
  ],
  delegate_name: [
    { required: true, message: "请输入下级VS的名称", trigger: "blur" }
  ],
  delegate_namespace: [
    { required: true, message: "请输入下级VS的命名空间", trigger: "blur" }
  ]
};

// 编辑路由相关变量
const showEditRouteDialog = ref(false);
const editRouteLoading = ref(false);
const editRouteFormRef = ref();
const currentEditRouteIndex = ref(-1);

// 编辑路由表单数据
const editRouteForm = ref({
  name: "",
  priority: null,
  match_rules: [
    {
      uri: { enabled: false, type: "prefix", value: "" },
      authority: { enabled: false, type: "exact", value: "" },
      headers: {
        enabled: false,
        items: [{ key: "", type: "exact", value: "" }]
      }
    }
  ],
  rewrite_rules: {
    enabled: false,
    uri: ""
  },
  timeout: "",
  forward_type: "",
  forward_detail: [
    {
      service: "",
      namespace: "",
      port: null,
      subset: { enabled: false, value: "" },
      weight: { enabled: false, value: 100 },
      headers: {
        enabled: false,
        value: '{"request": {"set": {"X-Forwarded-Proto": "https"}}}'
      }
    }
  ],
  delegate_name: "",
  delegate_namespace: ""
});

// 编辑路由表单验证规则
const editRouteFormRules = {
  forward_type: [
    { required: true, message: "请选择转发类型", trigger: "change" }
  ],
  delegate_name: [
    { required: true, message: "请输入下级VS的名称", trigger: "blur" }
  ],
  delegate_namespace: [
    { required: true, message: "请输入下级VS的命名空间", trigger: "blur" }
  ]
};

// 处理搜索
const handleSearch = () => {
  actualSearchText.value = routeSearchText.value;
};

// 返回上一页
const goBack = () => {
  const env = route.query.env;
  const ns = route.query.ns;

  if (env || ns) {
    // 如果有环境和命名空间参数，返回到列表页面并保持这些参数
    router.push({
      path: "/istio/level1-vs",
      query: {
        env: env as string,
        ns: ns as string
      }
    });
  } else {
    // 否则使用默认的返回行为
    router.back();
  }
};

// 解析JSON数组字符串
const parseJsonArray = (jsonStr: string) => {
  try {
    return JSON.parse(jsonStr || "[]");
  } catch {
    return [];
  }
};

// 格式化JSON字符串
const formatJsonString = (jsonStr: string) => {
  try {
    const parsed = JSON.parse(jsonStr || "[]");
    return JSON.stringify(parsed, null, 2);
  } catch {
    return jsonStr || "";
  }
};

// 获取HTTP路由数据
const fetchRouteData = async (vsId: number) => {
  try {
    routeLoading.value = true;
    const result = await getHttpRoutes(vsId);
    if (result.data) {
      // 按priority从小到大排序
      routeData.value = result.data.sort(
        (a: any, b: any) => a.priority - b.priority
      );
    } else {
      routeData.value = [];
    }
  } catch (error) {
    console.error("获取HTTP路由数据失败:", error);
    ElMessage.error("获取HTTP路由数据失败");
    routeData.value = [];
  } finally {
    routeLoading.value = false;
  }
};

// 获取详情数据
const fetchDetail = async () => {
  const vsId = route.params.id || route.query.id;
  if (!vsId) {
    ElMessage.error("缺少VirtualService ID");
    goBack();
    return;
  }

  try {
    loading.value = true;
    const response = await getVirtualServiceDetail(Number(vsId));
    if (response.data) {
      detailData.value = response.data;
      // 获取详情成功后，获取HTTP路由数据
      await fetchRouteData(Number(vsId));
    } else {
      ElMessage.error("获取详情失败");
    }
  } catch (error) {
    console.error("获取VirtualService详情失败:", error);
    ElMessage.error("获取详情失败");
  } finally {
    loading.value = false;
  }
};

// 获取命名空间列表
const fetchNamespaceOptions = async (env: string) => {
  try {
    const response = await getPromNamespace(env);
    if (response && response.data && Array.isArray(response.data)) {
      namespaceOptions.value = response.data;
    } else {
      namespaceOptions.value = [];
    }
  } catch (error) {
    console.error("获取命名空间列表失败:", error);
    ElMessage.error("获取命名空间列表失败");
    namespaceOptions.value = [];
  }
};

// 获取服务列表
const fetchServiceOptions = async (env: string, namespace: string) => {
  if (!env || !namespace) {
    serviceOptions.value = [];
    return;
  }
  try {
    const response = await getPromServices(env, namespace);
    if (response && response.data && Array.isArray(response.data)) {
      serviceOptions.value = response.data;
    } else {
      serviceOptions.value = [];
    }
  } catch (error) {
    console.error("获取服务列表失败:", error);
    ElMessage.error("获取服务列表失败");
    serviceOptions.value = [];
  }
};

// 获取可用集群列表
const fetchAvailableClusters = async () => {
  try {
    console.log("开始获取集群列表...");
    const response = await getAgentNames();
    console.log("API响应:", response);

    if (response && response.data && Array.isArray(response.data)) {
      availableClusters.value = response.data.map(
        (env: any) => env.name || env
      );
      console.log("可用集群列表:", availableClusters.value);

      if (availableClusters.value.length === 0) {
        ElMessage.warning("暂无可用的K8S集群");
      }
    } else {
      console.warn("API返回数据格式异常:", response);
      ElMessage.warning("获取到的集群数据格式异常");
      availableClusters.value = [];
    }
  } catch (error) {
    console.error("获取集群列表失败:", error);
    ElMessage.error(`获取集群列表失败: ${error.message || error}`);
    availableClusters.value = [];
  }
};

// 开始编辑集群
const startEditClusters = () => {
  isEditingClusters.value = true;
  originalClusters.value = JSON.parse(
    JSON.stringify(detailData.value.k8s_clusters)
  );
  fetchAvailableClusters();
};

// 取消编辑集群
const cancelEditClusters = () => {
  isEditingClusters.value = false;
  detailData.value.k8s_clusters = JSON.parse(
    JSON.stringify(originalClusters.value)
  );
};

// 保存集群
const saveClusters = async () => {
  const vsId = route.params.id || route.query.id;
  if (!vsId) {
    ElMessage.error("缺少VirtualService ID");
    return;
  }

  try {
    savingClusters.value = true;
    const k8sClusters = detailData.value.k8s_clusters.map(
      (cluster: any) => cluster.name || cluster.k8s_name
    );

    const response = await updateVirtualServiceClusters(
      Number(vsId),
      k8sClusters
    );

    // 检查响应是否成功
    if (response && response.success === true) {
      ElMessage.success("保存成功");
      isEditingClusters.value = false;
      // 重新获取详情数据
      await fetchDetail();
    } else {
      // 显示失败信息，包含完整的响应内容
      const errorMsg = response
        ? JSON.stringify(response, null, 2)
        : "未知错误";
      console.error("保存集群失败 - 响应:", response);
      ElMessage.error({
        message: `保存失败:\n${errorMsg}`,
        duration: 10000,
        showClose: true
      });
    }
  } catch (error) {
    console.error("保存集群失败:", error);
    const errorMsg = error.response?.data
      ? JSON.stringify(error.response.data, null, 2)
      : error.message || error;
    ElMessage.error({
      message: `保存失败:\n${errorMsg}`,
      duration: 10000,
      showClose: true
    });
  } finally {
    savingClusters.value = false;
  }
};

// 下发到集群
const deployToCluster = async (clusterName: string) => {
  const vsId = route.params.id || route.query.id;
  if (!vsId) {
    ElMessage.error("缺少VirtualService ID");
    return;
  }

  try {
    await ElMessageBox.confirm(
      `确定要将VirtualService下发到集群 "${clusterName}" 吗？`,
      "确认下发",
      {
        confirmButtonText: "确定",
        cancelButtonText: "取消",
        type: "warning"
      }
    );

    const response = await deployVirtualService(Number(vsId), clusterName);

    // 检查响应是否成功
    if (response && response.success === true) {
      ElMessage.success(`成功下发到集群 "${clusterName}"`);
    } else {
      // 显示失败信息，包含完整的响应内容
      const errorMsg = response
        ? JSON.stringify(response, null, 2)
        : "未知错误";
      console.error("下发失败 - 响应:", response);
      ElMessage.error({
        message: `下发到集群 "${clusterName}" 失败:\n${errorMsg}`,
        duration: 10000,
        showClose: true
      });
    }
  } catch (error) {
    if (error === "cancel") {
      return;
    }
    console.error("下发失败:", error);
    const errorMsg = error.response?.data
      ? JSON.stringify(error.response.data, null, 2)
      : error.message || error;
    ElMessage.error({
      message: `下发到集群 "${clusterName}" 失败:\n${errorMsg}`,
      duration: 10000,
      showClose: true
    });
  }
};

// 删除集群
const removeCluster = (index: number) => {
  detailData.value.k8s_clusters.splice(index, 1);
};

// 显示添加集群对话框
const showAddClusterDialogFunc = async () => {
  selectedClusters.value = [];
  await fetchAvailableClusters();
  showAddClusterDialog.value = true;
};

// 添加集群
const addClusters = async () => {
  if (selectedClusters.value.length === 0) {
    ElMessage.warning("请选择要添加的集群");
    return;
  }

  try {
    addingClusters.value = true;

    // 过滤掉已存在的集群
    const existingClusterNames = detailData.value.k8s_clusters.map(
      (cluster: any) => cluster.name
    );
    const newClusters = selectedClusters.value.filter(
      name => !existingClusterNames.includes(name)
    );

    if (newClusters.length === 0) {
      ElMessage.warning("选择的集群已存在");
      return;
    }

    // 添加新集群到列表
    newClusters.forEach(clusterName => {
      detailData.value.k8s_clusters.push({
        name: clusterName,
        k8s_name: clusterName,
        id: `temp_${Date.now()}_${Math.random()}`,
        namespace: "default",
        updated_at: new Date().toLocaleString("zh-CN")
      });
    });

    showAddClusterDialog.value = false;
    ElMessage.success(`成功添加 ${newClusters.length} 个集群`);
  } catch (error) {
    console.error("添加集群失败:", error);
    ElMessage.error("添加集群失败");
  } finally {
    addingClusters.value = false;
  }
};

// 添加匹配规则
const addMatchRule = () => {
  createRouteForm.value.match_rules.push({
    uri: { enabled: false, type: "prefix", value: "" },
    authority: { enabled: false, type: "exact", value: "" },
    headers: {
      enabled: false,
      items: [{ key: "", type: "exact", value: "" }]
    }
  });
};

// 删除匹配规则
const removeMatchRule = (index: number) => {
  createRouteForm.value.match_rules.splice(index, 1);
};

// 添加Header
const addHeader = (ruleIndex: number) => {
  createRouteForm.value.match_rules[ruleIndex].headers.items.push({
    key: "",
    type: "exact",
    value: ""
  });
};

// 删除Header
const removeHeader = (ruleIndex: number, headerIndex: number) => {
  createRouteForm.value.match_rules[ruleIndex].headers.items.splice(
    headerIndex,
    1
  );
};

// 添加转发详情
const addForwardDetail = () => {
  createRouteForm.value.forward_detail.push({
    service: "",
    namespace: "",
    port: null,
    subset: { enabled: false, value: "" },
    weight: { enabled: false, value: 100 },
    headers: {
      enabled: false,
      value: '{"request": {"set": {"X-Forwarded-Proto": "https"}}}'
    }
  });
};

// 删除转发详情
const removeForwardDetail = (index: number) => {
  createRouteForm.value.forward_detail.splice(index, 1);
};

// 处理转发类型变化
const handleForwardTypeChange = (value: string) => {
  if (value === "route") {
    // 重置route相关字段
    createRouteForm.value.forward_detail = [
      {
        service: "",
        namespace: "",
        port: null,
        subset: { enabled: false, value: "" },
        weight: { enabled: false, value: 100 },
        headers: {
          enabled: false,
          value: '{"request": {"set": {"X-Forwarded-Proto": "https"}}}'
        }
      }
    ];
    createRouteForm.value.delegate_name = "";
    createRouteForm.value.delegate_namespace = "";
  } else if (value === "delegate") {
    // 重置delegate相关字段
    createRouteForm.value.forward_detail = [];
  }
};

// 重置创建表单
const resetCreateRouteForm = () => {
  createRouteForm.value = {
    name: "",
    priority: null,
    match_rules: [
      {
        uri: { enabled: false, type: "prefix", value: "" },
        authority: { enabled: false, type: "exact", value: "" },
        headers: {
          enabled: false,
          items: [{ key: "", type: "exact", value: "" }]
        }
      }
    ],
    rewrite_rules: {
      enabled: false,
      uri: ""
    },
    timeout: "",
    forward_type: "",
    forward_detail: [
      {
        service: "",
        namespace: "",
        port: null,
        subset: { enabled: false, value: "" },
        weight: { enabled: false, value: 100 },
        headers: {
          enabled: false,
          value: '{"request": {"set": {"X-Forwarded-Proto": "https"}}}'
        }
      }
    ],
    delegate_name: "",
    delegate_namespace: ""
  };
};

// 构建请求数据
const buildRequestData = () => {
  const data: any = {};

  // 基本字段
  if (createRouteForm.value.name) {
    data.name = createRouteForm.value.name;
  }
  if (createRouteForm.value.priority) {
    data.priority = createRouteForm.value.priority;
  }
  if (createRouteForm.value.timeout) {
    data.timeout = createRouteForm.value.timeout;
  }

  // 匹配规则
  const matchRules: any[] = [];
  createRouteForm.value.match_rules.forEach(rule => {
    const matchRule: any = {};

    if (rule.uri.enabled && rule.uri.value) {
      matchRule.uri = { [rule.uri.type]: rule.uri.value };
    }

    if (rule.authority.enabled && rule.authority.value) {
      matchRule.authority = { [rule.authority.type]: rule.authority.value };
    }

    if (rule.headers.enabled) {
      const headers: any = {};
      rule.headers.items.forEach(header => {
        if (header.key && header.value) {
          headers[header.key] = { [header.type]: header.value };
        }
      });
      if (Object.keys(headers).length > 0) {
        matchRule.headers = headers;
      }
    }

    if (Object.keys(matchRule).length > 0) {
      matchRules.push(matchRule);
    }
  });

  if (matchRules.length > 0) {
    data.match_rules = matchRules;
  }

  // 重写规则
  if (
    createRouteForm.value.rewrite_rules.enabled &&
    createRouteForm.value.rewrite_rules.uri
  ) {
    data.rewrite_rules = {
      uri: createRouteForm.value.rewrite_rules.uri
    };
  }

  // 转发类型和详情
  data.forward_type = createRouteForm.value.forward_type;

  if (createRouteForm.value.forward_type === "route") {
    const forwardDetails: any[] = [];
    createRouteForm.value.forward_detail.forEach(detail => {
      if (detail.service && detail.namespace && detail.port) {
        const forwardDetail: any = {
          destination: {
            host: `${detail.service}.${detail.namespace}.svc.cluster.local`,
            port: { number: detail.port }
          }
        };

        if (detail.subset.enabled && detail.subset.value) {
          forwardDetail.destination.subset = detail.subset.value;
        }

        if (detail.weight.enabled && detail.weight.value !== null) {
          forwardDetail.weight = detail.weight.value;
        }

        if (detail.headers.enabled && detail.headers.value) {
          try {
            forwardDetail.headers = JSON.parse(detail.headers.value);
          } catch (e) {
            console.warn("Headers JSON格式错误:", e);
          }
        }

        forwardDetails.push(forwardDetail);
      }
    });

    if (forwardDetails.length > 0) {
      data.forward_detail = forwardDetails;
    }
  } else if (createRouteForm.value.forward_type === "delegate") {
    data.forward_detail = {
      name: createRouteForm.value.delegate_name,
      namespace: createRouteForm.value.delegate_namespace
    };
  }

  return data;
};

// 创建HTTP路由
const handleCreateRoute = async () => {
  if (!createRouteFormRef.value) return;

  try {
    const valid = await createRouteFormRef.value.validate();
    if (!valid) return;

    createRouteLoading.value = true;

    const vsId = route.params.id || route.query.id;
    const requestData = buildRequestData();

    const result = await createHttpRoute(Number(vsId), requestData);

    // 检查响应是否成功
    if (result && result.success === true) {
      ElMessage.success("HTTP路由创建成功");
      showCreateRouteDialog.value = false;
      resetCreateRouteForm();
      // 重新获取路由数据
      await fetchRouteData(Number(vsId));
    } else {
      // 显示失败信息，包含完整的响应内容
      const errorMsg = result ? JSON.stringify(result, null, 2) : "未知错误";
      console.error("创建HTTP路由失败 - 响应:", result);
      ElMessage.error({
        message: `创建失败:\n${errorMsg}`,
        duration: 10000,
        showClose: true
      });
    }
  } catch (error) {
    console.error("创建HTTP路由失败:", error);
    const errorMsg = error.response?.data
      ? JSON.stringify(error.response.data, null, 2)
      : error.message || error;
    ElMessage.error({
      message: `创建失败:\n${errorMsg}`,
      duration: 10000,
      showClose: true
    });
  } finally {
    createRouteLoading.value = false;
  }
};

// 编辑VirtualService相关方法
const showEditVSDialogFunc = () => {
  if (!detailData.value) return;

  // 解析默认转发详情
  let routeService = "";
  let routeNamespace = "";
  let routePort = 0;
  let delegateName = "";
  let delegateNamespace = "";
  let routeHeaders = {
    enabled: false,
    value: '{"request": {"set": {"X-Forwarded-Proto": "https"}}}'
  };

  if (detailData.value.df_forward_detail) {
    try {
      const forwardDetail = JSON.parse(detailData.value.df_forward_detail);
      if (
        detailData.value.df_forward_type === "route" &&
        Array.isArray(forwardDetail) &&
        forwardDetail.length > 0
      ) {
        const firstDestination = forwardDetail[0];
        if (firstDestination.destination) {
          let host = firstDestination.destination.host || "";

          // 先删除 .svc.cluster.local 后缀
          if (host.endsWith(".svc.cluster.local")) {
            host = host.replace(".svc.cluster.local", "");
          }

          // 用点号分割
          const parts = host.split(".");

          if (parts.length === 1) {
            // 只有一个元素，作为服务名，使用 detailData.namespace 作为命名空间
            routeService = parts[0];
            routeNamespace = detailData.value.namespace || "";
          } else if (parts.length === 2) {
            // 两个元素，第一个是服务名，第二个是命名空间
            routeService = parts[0];
            routeNamespace = parts[1];
          } else {
            // 其他情况报错
            console.error("无效的 host 格式:", host);
            ElMessage.error(`无效的 host 格式: ${host}`);
            return;
          }

          routePort = firstDestination.destination.port?.number || 0;
        }

        // 处理headers字段
        if (firstDestination.headers) {
          routeHeaders.enabled = true;
          routeHeaders.value = JSON.stringify(firstDestination.headers);
        }
      } else if (detailData.value.df_forward_type === "delegate") {
        delegateName = forwardDetail.name || "";
        delegateNamespace = forwardDetail.namespace || "";
      }
    } catch (e) {
      console.warn("解析默认转发详情失败:", e);
    }
  }

  // 填充表单数据
  editVSForm.value = {
    id: detailData.value.id || "",
    name: detailData.value.name || "",
    namespace: detailData.value.namespace || "",
    gateways: parseJsonArray(detailData.value.gateways).join(", "),
    hosts: parseJsonArray(detailData.value.hosts),
    df_forward_type: detailData.value.df_forward_type || "",
    route_service: routeService,
    route_namespace: routeNamespace,
    route_port: routePort,
    df_forward_timeout: detailData.value.df_forward_timeout || "",
    delegate_name: delegateName,
    delegate_namespace: delegateNamespace,
    route_headers: routeHeaders
  };

  showEditVSDialog.value = true;
};

const cancelEditVS = () => {
  showEditVSDialog.value = false;
  editVSForm.value = {
    id: "",
    name: "",
    namespace: "",
    gateways: "",
    hosts: [],
    df_forward_type: "",
    route_service: "",
    route_namespace: "",
    route_port: 0,
    df_forward_timeout: "",
    delegate_name: "",
    delegate_namespace: "",
    route_headers: {
      enabled: false,
      value: '{"request": {"set": {"X-Forwarded-Proto": "https"}}}'
    }
  };
};

const confirmEditVS = async () => {
  if (!editVSFormRef.value) return;

  try {
    const valid = await editVSFormRef.value.validate();
    if (!valid) return;

    editVSLoading.value = true;

    // 构建请求数据
    const requestData: any = {
      name: editVSForm.value.name,
      namespace: editVSForm.value.namespace,
      gateways: editVSForm.value.gateways
        .split(",")
        .map((g: string) => g.trim())
        .filter((g: string) => g),
      hosts: editVSForm.value.hosts,
      protocol: "http",
      df_forward_type: editVSForm.value.df_forward_type
    };

    // 根据转发类型构建 df_forward_detail
    if (editVSForm.value.df_forward_type === "route") {
      // Route 类型格式
      const host = `${editVSForm.value.route_service}.${editVSForm.value.route_namespace}.svc.cluster.local`;
      const routeItem: any = {
        destination: {
          host: host,
          port: { number: editVSForm.value.route_port }
        }
      };

      // 处理headers字段
      if (
        editVSForm.value.route_headers.enabled &&
        editVSForm.value.route_headers.value
      ) {
        try {
          routeItem.headers = JSON.parse(editVSForm.value.route_headers.value);
        } catch (e) {
          console.warn("Headers JSON格式错误:", e);
        }
      }

      requestData.df_forward_detail = [routeItem];
      requestData.df_forward_timeout = editVSForm.value.df_forward_timeout;
    } else if (editVSForm.value.df_forward_type === "delegate") {
      // Delegate 类型格式
      requestData.df_forward_detail = {
        name: editVSForm.value.delegate_name,
        namespace: editVSForm.value.delegate_namespace
      };
      // delegate 类型需要显式传递 null
      requestData.df_forward_timeout = null;
    }

    // 调用更新接口
    const response = await updateVirtualService(
      detailData.value.id,
      requestData
    );

    // 检查响应是否成功
    if (response && response.success === true) {
      ElMessage.success("VirtualService编辑成功");
      showEditVSDialog.value = false;
      // 重新获取详情数据
      await fetchDetail();
    } else {
      // 显示失败信息，包含完整的响应内容
      const errorMsg = response
        ? JSON.stringify(response, null, 2)
        : "未知错误";
      console.error("编辑VirtualService失败 - 响应:", response);
      ElMessage.error({
        message: `编辑失败:\n${errorMsg}`,
        duration: 10000,
        showClose: true
      });
    }
  } catch (error) {
    console.error("编辑VirtualService失败:", error);
    const errorMsg = error.response?.data
      ? JSON.stringify(error.response.data, null, 2)
      : error.message || error;
    ElMessage.error({
      message: `编辑失败:\n${errorMsg}`,
      duration: 10000,
      showClose: true
    });
  } finally {
    editVSLoading.value = false;
  }
};

// Host管理方法
const showEditHostInput = () => {
  editHostInputVisible.value = true;
  nextTick(() => {
    editHostInputRef.value?.focus();
  });
};

const handleEditHostInputConfirm = () => {
  if (
    editHostInputValue.value &&
    !editVSForm.value.hosts.includes(editHostInputValue.value)
  ) {
    editVSForm.value.hosts.push(editHostInputValue.value);
  }
  editHostInputVisible.value = false;
  editHostInputValue.value = "";
};

const removeEditHost = (host: string) => {
  const index = editVSForm.value.hosts.indexOf(host);
  if (index > -1) {
    editVSForm.value.hosts.splice(index, 1);
  }
};

// 处理默认转发类型变化
const handleEditForwardTypeChange = () => {
  // 当转发类型改变时，进行字段值的交换映射
  if (editVSForm.value.df_forward_type === "route") {
    // 从delegate切换到route：将delegate字段值映射到route字段
    const tempName = editVSForm.value.delegate_name;
    const tempNamespace = editVSForm.value.delegate_namespace;

    editVSForm.value.route_service = tempName;
    editVSForm.value.route_namespace = tempNamespace;
    editVSForm.value.delegate_name = "";
    editVSForm.value.delegate_namespace = "";
  } else if (editVSForm.value.df_forward_type === "delegate") {
    // 从route切换到delegate：将route字段值映射到delegate字段
    const tempService = editVSForm.value.route_service;
    const tempNamespace = editVSForm.value.route_namespace;

    editVSForm.value.delegate_name = tempService;
    editVSForm.value.delegate_namespace = tempNamespace;
    editVSForm.value.route_service = "";
    editVSForm.value.route_namespace = "";
    editVSForm.value.route_port = 0;
    editVSForm.value.df_forward_timeout = "";
    editVSForm.value.route_headers = {
      enabled: false,
      value: '{"request": {"set": {"X-Forwarded-Proto": "https"}}}'
    };
  }
};

// 编辑路由相关方法
// 打开编辑路由对话框
const openEditRouteDialog = (route: any, index: number) => {
  currentEditRouteIndex.value = index;

  // 解析delegate信息
  let delegateName = "";
  let delegateNamespace = "";

  if (route.forward_type === "delegate" && route.forward_detail) {
    try {
      const delegateDetail = JSON.parse(route.forward_detail);
      if (delegateDetail && typeof delegateDetail === "object") {
        delegateName = delegateDetail.name || "";
        delegateNamespace = delegateDetail.namespace || "";
      }
    } catch (e) {
      console.warn("解析delegate详情失败:", e);
    }
  }

  // 数据回填
  editRouteForm.value = {
    name: route.name || "",
    priority: route.priority || null,
    timeout: route.timeout || "",
    forward_type: route.forward_type || "",
    delegate_name: delegateName,
    delegate_namespace: delegateNamespace,
    match_rules: parseMatchRules(route.match_rules),
    rewrite_rules: parseRewriteRules(route.rewrite_rules),
    forward_detail: parseForwardDetail(route.forward_detail, route.forward_type)
  };

  showEditRouteDialog.value = true;
};

// 解析匹配规则
const parseMatchRules = (matchRulesStr: string) => {
  try {
    const rules = JSON.parse(matchRulesStr || "[]");
    if (!Array.isArray(rules) || rules.length === 0) {
      return [
        {
          uri: { enabled: false, type: "prefix", value: "" },
          authority: { enabled: false, type: "exact", value: "" },
          headers: {
            enabled: false,
            items: [{ key: "", type: "exact", value: "" }]
          }
        }
      ];
    }

    return rules.map(rule => {
      const parsedRule = {
        uri: { enabled: false, type: "prefix", value: "" },
        authority: { enabled: false, type: "exact", value: "" },
        headers: {
          enabled: false,
          items: [{ key: "", type: "exact", value: "" }]
        }
      };

      // 解析URI
      if (rule.uri) {
        parsedRule.uri.enabled = true;
        const uriKey = Object.keys(rule.uri)[0];
        parsedRule.uri.type = uriKey;
        parsedRule.uri.value = rule.uri[uriKey];
      }

      // 解析Authority
      if (rule.authority) {
        parsedRule.authority.enabled = true;
        const authKey = Object.keys(rule.authority)[0];
        parsedRule.authority.type = authKey;
        parsedRule.authority.value = rule.authority[authKey];
      }

      // 解析Headers
      if (rule.headers && Object.keys(rule.headers).length > 0) {
        parsedRule.headers.enabled = true;
        parsedRule.headers.items = Object.entries(rule.headers).map(
          ([key, value]: [string, any]) => {
            const headerType = Object.keys(value)[0];
            return {
              key,
              type: headerType,
              value: value[headerType]
            };
          }
        );
      }

      return parsedRule;
    });
  } catch (e) {
    console.warn("解析匹配规则失败:", e);
    return [
      {
        uri: { enabled: false, type: "prefix", value: "" },
        authority: { enabled: false, type: "exact", value: "" },
        headers: {
          enabled: false,
          items: [{ key: "", type: "exact", value: "" }]
        }
      }
    ];
  }
};

// 解析重写规则
const parseRewriteRules = (rewriteRulesStr: string) => {
  try {
    const rules = JSON.parse(rewriteRulesStr || "{}");
    return {
      enabled: !!rules.uri,
      uri: rules.uri || ""
    };
  } catch (e) {
    return {
      enabled: false,
      uri: ""
    };
  }
};

// 解析转发详情
const parseForwardDetail = (forwardDetailStr: string, forwardType: string) => {
  try {
    const detail = JSON.parse(
      forwardDetailStr || (forwardType === "route" ? "[]" : "{}")
    );

    if (forwardType === "route" && Array.isArray(detail)) {
      return detail.map(item => {
        const destination = item.destination || {};
        let service = "";
        let namespace = "";

        if (destination.host) {
          let host = destination.host;
          // 删除.svc.cluster.local后缀
          if (host.endsWith(".svc.cluster.local")) {
            host = host.replace(".svc.cluster.local", "");
          }

          const parts = host.split(".");
          if (parts.length === 1) {
            // 只有一个元素，作为服务名，使用VS的namespace作为命名空间
            service = parts[0];
            namespace = detailData.value?.namespace || "";
          } else if (parts.length === 2) {
            // 两个元素，第一个是服务名，第二个是命名空间
            service = parts[0];
            namespace = parts[1];
          } else {
            // 其他情况报错
            console.error(`无效的host格式: ${destination.host}`);
            throw new Error(`无效的host格式: ${destination.host}`);
          }
        }

        return {
          service,
          namespace,
          port: destination.port?.number || null,
          subset: {
            enabled: !!destination.subset,
            value: destination.subset || ""
          },
          weight: {
            enabled: item.weight !== undefined,
            value: item.weight || 100
          },
          headers: {
            enabled: !!item.headers,
            value: item.headers
              ? JSON.stringify(item.headers)
              : '{"request": {"set": {"X-Forwarded-Proto": "https"}}}'
          }
        };
      });
    }

    // delegate模式返回空数组，delegate信息通过delegate_name和delegate_namespace字段处理
    return [
      {
        service: "",
        namespace: "",
        port: null,
        subset: { enabled: false, value: "" },
        weight: { enabled: false, value: 100 },
        headers: {
          enabled: false,
          value: '{"request": {"set": {"X-Forwarded-Proto": "https"}}}'
        }
      }
    ];
  } catch (e) {
    return [
      {
        service: "",
        namespace: "",
        port: null,
        subset: { enabled: false, value: "" },
        weight: { enabled: false, value: 100 },
        headers: {
          enabled: false,
          value: '{"request": {"set": {"X-Forwarded-Proto": "https"}}}'
        }
      }
    ];
  }
};

// 编辑路由表单操作方法
const addEditMatchRule = () => {
  editRouteForm.value.match_rules.push({
    uri: { enabled: false, type: "prefix", value: "" },
    authority: { enabled: false, type: "exact", value: "" },
    headers: {
      enabled: false,
      items: [{ key: "", type: "exact", value: "" }]
    }
  });
};

const removeEditMatchRule = (index: number) => {
  editRouteForm.value.match_rules.splice(index, 1);
};

const addEditHeader = (ruleIndex: number) => {
  editRouteForm.value.match_rules[ruleIndex].headers.items.push({
    key: "",
    type: "exact",
    value: ""
  });
};

const removeEditHeader = (ruleIndex: number, headerIndex: number) => {
  editRouteForm.value.match_rules[ruleIndex].headers.items.splice(
    headerIndex,
    1
  );
};

const addEditForwardDetail = () => {
  editRouteForm.value.forward_detail.push({
    service: "",
    namespace: "",
    port: null,
    subset: { enabled: false, value: "" },
    weight: { enabled: false, value: 100 },
    headers: {
      enabled: false,
      value: '{"request": {"set": {"X-Forwarded-Proto": "https"}}}'
    }
  });
};

const removeEditForwardDetail = (index: number) => {
  editRouteForm.value.forward_detail.splice(index, 1);
};

const handleEditRouteForwardTypeChange = (value: string) => {
  if (value === "route") {
    // 从delegate切换到route时，将delegate信息填入route字段
    const delegateName = editRouteForm.value.delegate_name;
    const delegateNamespace = editRouteForm.value.delegate_namespace;

    editRouteForm.value.forward_detail = [
      {
        service: delegateName || "",
        namespace: delegateNamespace || "",
        port: null,
        subset: { enabled: false, value: "" },
        weight: { enabled: false, value: 100 },
        headers: {
          enabled: false,
          value: '{"request": {"set": {"X-Forwarded-Proto": "https"}}}'
        }
      }
    ];
    editRouteForm.value.delegate_name = "";
    editRouteForm.value.delegate_namespace = "";
  } else if (value === "delegate") {
    // 从route切换到delegate时，取第一个forward_detail的service和namespace填入delegate字段
    const firstForwardDetail = editRouteForm.value.forward_detail[0];
    if (firstForwardDetail) {
      editRouteForm.value.delegate_name = firstForwardDetail.service || "";
      editRouteForm.value.delegate_namespace =
        firstForwardDetail.namespace || "";
    }
    editRouteForm.value.forward_detail = [];
  }
};

// 构建编辑路由请求数据
const buildEditRequestData = () => {
  const data: any = {};

  // 基本字段 - 确保所有字段都传递，空值传null
  data.name = editRouteForm.value.name || null;
  data.priority = editRouteForm.value.priority || null;
  data.timeout = editRouteForm.value.timeout || null;

  // 匹配规则
  const matchRules: any[] = [];
  editRouteForm.value.match_rules.forEach(rule => {
    const matchRule: any = {};

    if (rule.uri.enabled && rule.uri.value) {
      matchRule.uri = { [rule.uri.type]: rule.uri.value };
    }

    if (rule.authority.enabled && rule.authority.value) {
      matchRule.authority = { [rule.authority.type]: rule.authority.value };
    }

    if (rule.headers.enabled) {
      const headers: any = {};
      rule.headers.items.forEach(header => {
        if (header.key && header.value) {
          headers[header.key] = { [header.type]: header.value };
        }
      });
      if (Object.keys(headers).length > 0) {
        matchRule.headers = headers;
      }
    }

    if (Object.keys(matchRule).length > 0) {
      matchRules.push(matchRule);
    }
  });

  data.match_rules = matchRules.length > 0 ? matchRules : [];

  // 重写规则
  if (
    editRouteForm.value.rewrite_rules.enabled &&
    editRouteForm.value.rewrite_rules.uri
  ) {
    data.rewrite_rules = {
      uri: editRouteForm.value.rewrite_rules.uri
    };
  } else {
    data.rewrite_rules = null;
  }

  // 转发类型和详情
  data.forward_type = editRouteForm.value.forward_type;

  if (editRouteForm.value.forward_type === "route") {
    const forwardDetail: any[] = [];
    editRouteForm.value.forward_detail.forEach(detail => {
      if (detail.service && detail.namespace && detail.port) {
        const destination: any = {
          host: `${detail.service}.${detail.namespace}.svc.cluster.local`,
          port: { number: detail.port }
        };

        if (detail.subset.enabled && detail.subset.value) {
          destination.subset = detail.subset.value;
        }

        const routeItem: any = { destination };

        if (detail.weight.enabled && detail.weight.value !== undefined) {
          routeItem.weight = detail.weight.value;
        }

        if (detail.headers.enabled && detail.headers.value) {
          try {
            routeItem.headers = JSON.parse(detail.headers.value);
          } catch (e) {
            console.warn("解析headers失败:", e);
          }
        }

        forwardDetail.push(routeItem);
      }
    });
    data.forward_detail = forwardDetail;
  } else if (editRouteForm.value.forward_type === "delegate") {
    data.forward_detail = {
      name: editRouteForm.value.delegate_name,
      namespace: editRouteForm.value.delegate_namespace
    };
  }

  return data;
};

// 处理编辑路由提交
const handleEditRoute = async () => {
  if (!editRouteFormRef.value) return;

  try {
    const valid = await editRouteFormRef.value.validate();
    if (!valid) return;

    editRouteLoading.value = true;

    const vsId = route.params.id || route.query.id;
    const routeId = routeData.value[currentEditRouteIndex.value]?.id;

    if (!routeId) {
      ElMessage.error("无法获取路由ID");
      return;
    }

    const requestData = buildEditRequestData();

    const result = await updateHttpRoute(
      Number(routeId),
      Number(vsId),
      requestData
    );

    // 检查响应是否成功
    if (result && result.success === true) {
      ElMessage.success("HTTP路由更新成功");
      showEditRouteDialog.value = false;
      // 重新获取路由数据
      await fetchRouteData(Number(vsId));
    } else {
      // 显示失败信息，包含完整的响应内容
      const errorMsg = result ? JSON.stringify(result, null, 2) : "未知错误";
      console.error("更新HTTP路由失败 - 响应:", result);
      ElMessage.error({
        message: `更新失败:\n${errorMsg}`,
        duration: 10000,
        showClose: true
      });
    }
  } catch (error) {
    console.error("更新HTTP路由失败:", error);
    const errorMsg = error.response?.data
      ? JSON.stringify(error.response.data, null, 2)
      : error.message || error;
    ElMessage.error({
      message: `更新失败:\n${errorMsg}`,
      duration: 10000,
      showClose: true
    });
  } finally {
    editRouteLoading.value = false;
  }
};

// 自定义超时字段排序方法
const sortTimeout = (a: any, b: any) => {
  // 提取数字部分进行比较
  const getTimeoutNumber = (timeout: string) => {
    if (!timeout) return 0;
    const match = timeout.match(/^(\d+)/);
    return match ? parseInt(match[1], 10) : 0;
  };

  const aNum = getTimeoutNumber(a.timeout);
  const bNum = getTimeoutNumber(b.timeout);

  return aNum - bNum;
};

// 刷新端口号
const refreshPort = async (detail: any) => {
  if (!detail.service || !detail.namespace) {
    ElMessage.warning("请先填写服务名和命名空间");
    return;
  }

  try {
    // 获取当前VS的集群信息
    const env = detailData.value?.k8s_clusters?.[0];
    if (!env) {
      ElMessage.error("无法获取集群信息");
      return;
    }

    const result = await getServiceFirstPort(
      env.k8s_name,
      detail.namespace,
      detail.service
    );

    if (result && result.data && result.data.first_port) {
      detail.port = result.data.first_port;
      ElMessage.success(`已获取到端口号: ${result.data.first_port}`);
    } else {
      ElMessage.error("未能获取到端口号");
    }
  } catch (error) {
    console.error("获取端口号失败:", error);
    ElMessage.error("获取端口号失败");
  }
};

// 编辑VirtualService时刷新端口号
const refreshPortForEdit = async () => {
  if (!editVSForm.value.route_service || !editVSForm.value.route_namespace) {
    ElMessage.warning("请先填写服务名和命名空间");
    return;
  }

  try {
    // 获取当前VS的集群信息
    const env = detailData.value?.k8s_clusters?.[0];
    if (!env) {
      ElMessage.error("无法获取集群信息");
      return;
    }

    const result = await getServiceFirstPort(
      env.k8s_name,
      editVSForm.value.route_namespace,
      editVSForm.value.route_service
    );

    if (result && result.data && result.data.first_port) {
      editVSForm.value.route_port = result.data.first_port;
      ElMessage.success(`已获取到端口号: ${result.data.first_port}`);
    } else {
      ElMessage.error("未能获取到端口号");
    }
  } catch (error) {
    console.error("获取端口号失败:", error);
    ElMessage.error("获取端口号失败");
  }
};

// 页面初始化
onMounted(() => {
  fetchDetail();
});
</script>

<style scoped>
.vs-detail-container {
  padding: 20px;

  min-height: calc(100vh - 60px);
}

.page-header {
  display: flex;
  align-items: center;
  margin-bottom: 20px;
  padding: 16px 20px;

  border-radius: 8px;
  box-shadow: 0 2px 10px rgba(0, 0, 0, 0.1);
}

.back-btn {
  margin-right: 16px;
}

.page-title {
  margin: 0;
  font-size: 20px;
  font-weight: 600;
  color: #303133;
}

.loading-container {
  background: white;
  padding: 20px;
  border-radius: 8px;
}

.detail-content {
  display: flex;
  flex-direction: column;
  gap: 20px;
}

.cluster-card,
.detail-card,
.route-card {
  border-radius: 8px;
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 16px;
  font-weight: 600;
  color: #303133;
}

.header-left {
  display: flex;
  align-items: center;
}

.header-actions {
  margin-left: 20px;
}

.edit-actions {
  display: flex;
  gap: 8px;
}

.header-icon {
  margin-right: 8px;
  font-size: 18px;
  color: #409eff;
}

.cluster-list {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 16px;
}

.cluster-item {
  position: relative;
  padding: 16px;

  border-radius: 6px;
  border: 1px solid #e4e7ed;
}

.cluster-actions {
  position: absolute;
  top: 8px;
  right: 8px;
}

.cluster-deploy-actions {
  position: absolute;
  top: 8px;
  right: 8px;
}

.add-cluster-item {
  display: flex;
  justify-content: center;
  align-items: center;
  padding: 16px;
}

.add-cluster-btn {
  width: 120px;
}

.cluster-info {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.cluster-name {
  font-weight: 600;
}

.cluster-meta {
  display: flex;
  align-items: center;
  font-size: 12px;
  color: #909399;
}

.meta-item {
  display: flex;
  align-items: center;
  gap: 4px;
}

.detail-container {
  display: flex;
  gap: 20px;
  height: 100%;
}

.detail-left {
  flex: 3;
  display: flex;
  flex-direction: column;
  gap: 20px;
}

.detail-right {
  flex: 1;
  display: flex;
  flex-direction: column;
}

.detail-row {
  display: grid;
  gap: 20px;
}

/* 第一行：ID、命名空间、名称 */
.detail-row.row-first {
  grid-template-columns: 1fr 1fr 1fr;
}

/* 第二行：默认转发方式、超时时间、更新时间 */
.detail-row.row-second {
  grid-template-columns: 1fr 1fr 1fr;
}

/* 第三行：网关、主机列表（主机列表占2列） */
.detail-row.row-third {
  grid-template-columns: 1fr 1fr 1fr;
}

.detail-item {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

/* 默认路由详情特殊样式 */
.detail-item.detail-forward {
  display: flex;
  flex-direction: column;
}

.detail-item.detail-forward .detail-value {
  display: flex;
  flex-direction: column;
}

.detail-item.detail-forward .json-textarea {
  height: 80px;
  resize: none;
}

/* 主机列表特殊样式 */
.detail-item.detail-hosts {
  grid-column: 2 / 4;
}

.detail-label {
  font-weight: 600;
  color: #606266;
  font-size: 14px;
}

.detail-value {
  font-size: 14px;
  line-height: 1.5;
}

.host-tag,
.gateway-tag {
  margin-right: 8px;
  margin-bottom: 4px;
}

.json-textarea {
  font-family: "Courier New", monospace;
  font-size: 12px;
  line-height: 1.4;

  border-radius: 4px;
  height: 80px;
}

.table-json-textarea {
  font-family: "Courier New", monospace;
  font-size: 11px;
  line-height: 1.3;
  background-color: #f8f9fa;
  border: 1px solid #e4e7ed;
  border-radius: 4px;
}

.table-json-textarea .el-textarea__inner {
  padding: 8px;
  min-height: auto !important;
}

.table-json-display {
  font-family: "Courier New", monospace;
  font-size: 11px;
  line-height: 1.3;
  background-color: #f8f9fa;
  border: 1px solid #e4e7ed;
  border-radius: 4px;
  padding: 8px;
  min-height: 20px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  cursor: pointer;
}

.no-data {
  color: #909399;
  font-style: italic;
}

.error-container {
  background: white;
  padding: 40px;
  border-radius: 8px;
  text-align: center;
}

/* 卡片头部样式 */
.card-header {
  display: flex;
  justify-content: flex-start;
  align-items: center;
}

/* HTTP路由规则卡片头部样式 */
.route-card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  width: 100%;
}

.route-card-header .header-left {
  display: flex;
  align-items: center;
}

.route-card-header .header-right {
  display: flex;
  align-items: center;
}

/* 表单样式 */
.match-rules-container {
  width: 100%;
}

.match-rule-item {
  margin-bottom: 16px;
}

.rule-card {
  border: 1px solid #e4e7ed;
}

.rule-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-weight: 600;
}

.header-item {
  margin-bottom: 8px;
}

.add-rule-btn {
  width: 100%;
  margin-top: 16px;
}

.forward-details-container {
  width: 100%;
}

.forward-detail-item {
  margin-bottom: 16px;
}

.detail-card {
  border: 1px solid #e4e7ed;
}

.detail-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-weight: 600;
}

.add-detail-btn {
  width: 100%;
  margin-top: 16px;
}

.dialog-footer {
  text-align: right;
}

@media (max-width: 768px) {
  .detail-row {
    grid-template-columns: 1fr;
  }

  .cluster-list {
    grid-template-columns: 1fr;
  }
}
</style>
