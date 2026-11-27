<template>
  <div class="system-notification">
    <el-card>
      <template #header>
        <div class="card-header">
          <span>各类通知的用途</span>
          <el-button
            link
            type="primary"
            @click="guideExpanded = !guideExpanded"
          >
            {{ guideExpanded ? "收起说明" : "展开说明" }}
          </el-button>
        </div>
      </template>
      <div v-if="!guideExpanded" class="note compact">
        涵盖 K8S 事件告警、运维操作通知、指标告警与管控表通知。{{ NOTICE_NOTE }}
      </div>
      <template v-else>
        <el-table :data="NOTICE_ROWS" border size="small">
          <el-table-column prop="name" label="通知" width="130" />
          <el-table-column prop="sender" label="谁发送" width="300" />
          <el-table-column prop="when" label="什么时候发" min-width="360" />
        </el-table>
        <div class="note">{{ NOTICE_NOTE }}</div>
      </template>
    </el-card>

    <!-- v2 统一多通道通知工作台 -->
    <el-card v-if="v2Mode" v-loading="loading" class="mt-3">
      <template #header>
        <div class="card-header">
          <div class="header-title-group">
            <span>统一通知中心</span>
            <el-tag size="small" type="success" effect="plain">
              Schema v2 · 多通道路由
            </el-tag>
          </div>
          <span v-if="updatedBy" class="hint inline">
            上次修改：{{ updatedBy }} · {{ formatTime(updatedAt) }}
          </span>
        </div>
      </template>

      <el-alert
        type="info"
        :closable="false"
        class="page-alert"
        title="保存后马上生效，不用重启服务；所有密钥字段采用 AES-256-GCM 加密存储，页面仅展示脱敏提示，留空提交沿用已保存值。"
      />
      <el-alert
        v-if="loadError"
        type="error"
        :closable="false"
        show-icon
        class="page-alert"
        :title="`读取通知配置失败：${loadError}`"
      >
        <el-button size="small" :disabled="busy" @click="load">
          重新加载
        </el-button>
      </el-alert>

      <el-tabs v-model="activeTab" @tab-change="handleTabChange">
        <!-- Tab 1: 通知通道管理 -->
        <el-tab-pane label="通知通道管理" name="channels">
          <div class="toolbar">
            <el-button
              type="primary"
              :disabled="!loaded || busy"
              @click="openNewChannel()"
            >
              新增通道
            </el-button>
            <el-select
              v-model="providerFilter"
              clearable
              placeholder="按通道类型筛选"
              class="filter-select"
            >
              <el-option
                v-for="item in providers"
                :key="item.id"
                :label="item.label"
                :value="item.id"
              />
            </el-select>
            <span class="hint inline">
              支持企业微信、飞书 / Lark、钉钉、Slack、自定义 Webhook 与邮件
              SMTP（共
              {{ channelsV2.length }} 个通道）
            </span>
          </div>

          <el-table
            :data="filteredChannels"
            row-key="id"
            border
            empty-text="暂无通知通道，点击上方「新增通道」开始配置"
          >
            <el-table-column label="通道名称 / ID" min-width="200">
              <template #default="{ row }">
                <div class="channel-name">{{ row.name }}</div>
                <div class="channel-id">ID: {{ row.id }}</div>
              </template>
            </el-table-column>
            <el-table-column label="类型" width="140">
              <template #default="{ row }">
                <el-tag size="small" effect="plain">
                  {{ providerLabel(row.provider, providers) }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="启用" width="90" align="center">
              <template #default="{ row }">
                <el-switch v-model="row.enabled" :disabled="busy" />
              </template>
            </el-table-column>
            <el-table-column label="凭据 / 端点摘要" min-width="260">
              <template #default="{ row }">
                <div class="channel-summary">
                  {{ channelHintSummaryV2(row, providers) }}
                </div>
                <div
                  v-if="channelTestStates[row.id]?.result"
                  :class="[
                    'test-result',
                    'block',
                    channelTestStates[row.id].result!.type
                  ]"
                >
                  {{ channelTestStates[row.id].result!.text }}
                </div>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="230" fixed="right">
              <template #default="{ row }">
                <el-button
                  link
                  type="primary"
                  :loading="channelTestStates[row.id]?.testing"
                  :disabled="busy"
                  @click="quickTestChannel(row)"
                >
                  测试
                </el-button>
                <el-button
                  link
                  type="primary"
                  :disabled="busy"
                  @click="openEditChannel(row)"
                >
                  编辑
                </el-button>
                <el-button link :disabled="busy" @click="duplicateChannel(row)">
                  复制
                </el-button>
                <el-button
                  link
                  type="danger"
                  :disabled="busy"
                  @click="deleteChannel(row.id)"
                >
                  删除
                </el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>

        <!-- Tab 2: 路由与分发规则 -->
        <el-tab-pane label="路由与分发规则" name="routes">
          <div class="section-header">
            <span class="section-title">默认兜底通道</span>
            <span class="hint inline">
              未命中任何下方路由规则时，消息将并行发送到这些通道（支持多选扇出）
            </span>
          </div>
          <el-select
            v-model="defaultChannelIdsV2"
            multiple
            clearable
            filterable
            placeholder="选择默认兜底通道（留空表示未命中规则的消息不发送）"
            class="default-channels-select"
            :disabled="!loaded || busy"
          >
            <el-option
              v-for="ch in channelsV2"
              :key="ch.id"
              :value="ch.id"
              :label="`${ch.name} (${providerLabel(ch.provider, providers)})${ch.enabled ? '' : ' [已停用]'}`"
            />
          </el-select>

          <div class="section-header section-gap">
            <span class="section-title">有序分发规则（支持多通道扇出）</span>
            <span class="hint inline">
              按从上到下顺序匹配；勾选「继续匹配」可在命中后继续向下扇出给更多通道
            </span>
            <el-button
              type="primary"
              size="small"
              class="add-route-btn"
              :disabled="!loaded || busy"
              @click="addRoute()"
            >
              新增路由规则
            </el-button>
          </div>

          <div v-if="!routesV2.length" class="empty-routes">
            暂无路由规则，所有通知将直接发往上方配置的「默认兜底通道」。
          </div>

          <div v-else class="route-list">
            <el-card
              v-for="(route, index) in routesV2"
              :key="route.id + '-' + index"
              shadow="never"
              class="route-card"
            >
              <div class="route-card-head">
                <div class="route-head-left">
                  <el-tag size="small" type="info">#{{ index + 1 }}</el-tag>
                  <el-input
                    v-model="route.name"
                    placeholder="规则名称"
                    size="small"
                    class="route-name-input"
                    :disabled="busy"
                  />
                  <el-switch
                    v-model="route.enabled"
                    active-text="启用"
                    inactive-text="停用"
                    :disabled="busy"
                  />
                  <el-switch
                    v-model="route.continue"
                    active-text="继续匹配后续"
                    inactive-text="命中即停"
                    :disabled="busy"
                  />
                </div>
                <div class="route-head-actions">
                  <el-button
                    link
                    size="small"
                    :disabled="busy || index === 0"
                    @click="moveRoute(index, -1)"
                  >
                    上移
                  </el-button>
                  <el-button
                    link
                    size="small"
                    :disabled="busy || index === routesV2.length - 1"
                    @click="moveRoute(index, 1)"
                  >
                    下移
                  </el-button>
                  <el-button
                    link
                    type="danger"
                    size="small"
                    :disabled="busy"
                    @click="removeRoute(index)"
                  >
                    删除
                  </el-button>
                </div>
              </div>

              <div class="route-grid">
                <div class="route-field">
                  <label class="route-field-label">集群 (envs)</label>
                  <el-select
                    v-model="route.envs"
                    multiple
                    filterable
                    allow-create
                    clearable
                    placeholder="全部集群（留空不限）"
                    :disabled="busy"
                  >
                    <el-option
                      v-for="env in envsV2"
                      :key="env"
                      :label="env"
                      :value="env"
                    />
                  </el-select>
                </div>
                <div class="route-field">
                  <label class="route-field-label">消息类别 (categories)</label>
                  <el-select
                    v-model="route.categories"
                    multiple
                    clearable
                    placeholder="全部类别（留空不限）"
                    :disabled="busy"
                  >
                    <el-option
                      v-for="cat in CATEGORY_OPTIONS"
                      :key="cat.value"
                      :label="cat.label"
                      :value="cat.value"
                    />
                  </el-select>
                </div>
                <div class="route-field">
                  <label class="route-field-label">严重级别 (severities)</label>
                  <el-select
                    v-model="route.severities"
                    multiple
                    clearable
                    placeholder="全部级别（留空不限）"
                    :disabled="busy"
                  >
                    <el-option
                      v-for="sev in SEVERITY_OPTIONS"
                      :key="sev.value"
                      :label="sev.label"
                      :value="sev.value"
                    />
                  </el-select>
                </div>
                <div class="route-field route-targets">
                  <label class="route-field-label">目标通道（多选扇出）</label>
                  <el-select
                    v-model="route.channel_ids"
                    multiple
                    filterable
                    clearable
                    placeholder="选择接收该规则通知的通道"
                    :disabled="busy"
                  >
                    <el-option
                      v-for="ch in channelsV2"
                      :key="ch.id"
                      :value="ch.id"
                      :label="`${ch.name} (${providerLabel(ch.provider, providers)})${ch.enabled ? '' : ' [已停用]'}`"
                    />
                  </el-select>
                </div>
              </div>
              <div class="route-summary-line">
                {{ routeSummary(route, channelNameMap) }}
              </div>
            </el-card>
          </div>
        </el-tab-pane>

        <!-- Tab 3: 投递审计日志 -->
        <el-tab-pane label="投递审计日志" name="deliveries">
          <div class="toolbar">
            <el-select
              v-model="deliveryFilter.env"
              clearable
              filterable
              allow-create
              placeholder="全部集群"
              class="filter-select"
            >
              <el-option
                v-for="env in envsV2"
                :key="env"
                :label="env"
                :value="env"
              />
            </el-select>
            <el-select
              v-model="deliveryFilter.provider"
              clearable
              placeholder="全部通道类型"
              class="filter-select"
            >
              <el-option
                v-for="item in providers"
                :key="item.id"
                :label="item.label"
                :value="item.id"
              />
            </el-select>
            <el-select
              v-model="deliveryFilter.ok"
              clearable
              placeholder="全部状态"
              class="filter-select small"
            >
              <el-option label="成功" value="true" />
              <el-option label="失败" value="false" />
            </el-select>
            <el-button
              type="primary"
              :loading="deliveriesLoading"
              @click="loadDeliveries"
            >
              刷新日志
            </el-button>
          </div>

          <el-alert
            v-if="deliveriesError"
            type="warning"
            :closable="false"
            show-icon
            class="page-alert"
            :title="deliveriesError"
          />

          <el-table
            v-loading="deliveriesLoading"
            :data="deliveries"
            border
            stripe
            empty-text="暂无投递审计记录"
          >
            <el-table-column label="时间" width="165">
              <template #default="{ row }">
                {{ formatTime(row.created_at) }}
              </template>
            </el-table-column>
            <el-table-column label="集群" width="130" show-overflow-tooltip>
              <template #default="{ row }">
                {{ row.env || "全局" }}
              </template>
            </el-table-column>
            <el-table-column label="类别" width="120">
              <template #default="{ row }">
                {{ categoryLabel(row.category) }}
              </template>
            </el-table-column>
            <el-table-column label="级别" width="100">
              <template #default="{ row }">
                <el-tag
                  size="small"
                  :type="severityTagType(row.severity)"
                  effect="plain"
                >
                  {{ severityLabel(row.severity) }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="通道" min-width="160" show-overflow-tooltip>
              <template #default="{ row }">
                <span>{{ row.channel_name || row.channel_id || "-" }}</span>
                <span class="hint inline ml-1">
                  ({{ providerLabel(row.provider, providers) }})
                </span>
              </template>
            </el-table-column>
            <el-table-column label="状态" width="85" align="center">
              <template #default="{ row }">
                <el-tag size="small" :type="row.ok ? 'success' : 'danger'">
                  {{ row.ok ? "成功" : "失败" }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="重试" width="70" align="center">
              <template #default="{ row }">
                {{ row.attempts ?? 1 }}
              </template>
            </el-table-column>
            <el-table-column label="HTTP" width="80" align="center">
              <template #default="{ row }">
                {{ row.http_status ?? "-" }}
              </template>
            </el-table-column>
            <el-table-column label="耗时" width="90" align="right">
              <template #default="{ row }">
                {{ row.latency_ms != null ? `${row.latency_ms} ms` : "-" }}
              </template>
            </el-table-column>
            <el-table-column
              label="摘要 / 脱敏错误"
              min-width="220"
              show-overflow-tooltip
            >
              <template #default="{ row }">
                {{ row.error_summary || row.message || "-" }}
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>
      </el-tabs>

      <!-- Save / Reset Bar for config tabs -->
      <div v-if="activeTab !== 'deliveries'" class="actions">
        <el-button
          type="primary"
          :loading="saving"
          :disabled="!loaded || loading || removing"
          @click="save"
        >
          保存配置
        </el-button>
        <el-button :disabled="busy" @click="load">重置</el-button>
        <el-tag v-if="dirty" type="warning" effect="plain">
          有未保存的修改
        </el-tag>
      </div>

      <!-- Channel Editor Drawer -->
      <el-drawer
        v-model="drawerVisible"
        :title="
          editingNew
            ? '新增通知通道'
            : `编辑通道：${editingForm.name || editingForm.id}`
        "
        size="min(600px, 94vw)"
        destroy-on-close
      >
        <el-form label-width="125px" :disabled="busy">
          <el-form-item label="通道 ID" required>
            <el-input
              v-model="editingForm.id"
              :disabled="!editingNew"
              placeholder="例如 ch_prod_feishu（字母/数字/下划线/连字符）"
            />
          </el-form-item>
          <el-form-item label="通道名称" required>
            <el-input
              v-model="editingForm.name"
              placeholder="例如 生产严重告警飞书群"
            />
          </el-form-item>
          <el-form-item label="通道类型" required>
            <el-select
              :model-value="editingForm.provider"
              class="full-width"
              @change="onEditingProviderChange"
            >
              <el-option
                v-for="item in providers"
                :key="item.id"
                :label="item.label"
                :value="item.id"
              />
            </el-select>
          </el-form-item>
          <el-form-item label="启用状态">
            <el-switch v-model="editingForm.enabled" />
          </el-form-item>

          <DynamicChannelFields
            v-model:form="editingForm"
            :provider="editingProviderSchema"
            :providers="providers"
            :testing="drawerTest.testing"
            :result="drawerTest.result"
            :disabled="busy"
            @test="testEditingChannel"
          />
        </el-form>
        <template #footer>
          <el-button @click="drawerVisible = false">取消</el-button>
          <el-button type="primary" @click="confirmEditChannel">
            确认应用
          </el-button>
        </template>
      </el-drawer>
    </el-card>

    <!-- v1 降级兼容视图（当后端尚未启用 v2 接口时自动启用） -->
    <el-card v-else v-loading="loading" class="mt-3">
      <template #header>
        <div class="card-header">
          <span>通知配置</span>
          <span v-if="updatedBy" class="hint inline">
            上次修改：{{ updatedBy }} · {{ formatTime(updatedAt) }}
          </span>
        </div>
      </template>
      <el-alert
        type="info"
        :closable="false"
        class="page-alert"
        title="保存后马上生效，不用重启服务，也不用改各个集群；token 加密保存，页面只显示「已配置」和最后 4 位。"
      />
      <el-alert
        v-if="loadError"
        type="error"
        :closable="false"
        show-icon
        class="page-alert"
        :title="`读取通知配置失败：${loadError}`"
      >
        <el-button size="small" :disabled="busy" @click="load">
          重新加载
        </el-button>
      </el-alert>

      <div class="section-header">
        <span class="section-title">全局配置</span>
        <el-tag v-if="globalSaved" type="success" size="small" effect="plain">
          已配置 · {{ TYPE_LABELS[globalSaved.type] }}
        </el-tag>
        <el-tag v-else type="info" size="small" effect="plain">未配置</el-tag>
        <span class="hint inline">
          没有单独配置的集群都用全局配置；没有全局配置时，这些集群不发通知
        </span>
        <el-button
          v-if="globalSaved"
          link
          type="danger"
          class="remove-default"
          :loading="removing"
          :disabled="busy"
          @click="removeDefault"
        >
          删除全局配置
        </el-button>
      </div>
      <el-form
        label-width="120px"
        class="channel-form"
        :disabled="!loaded || busy"
      >
        <NotifyChannelFields
          v-model:form="globalForm"
          :saved="globalSaved"
          :testing="globalTest.testing"
          :result="globalTest.result"
          @test="testGlobal"
        />
      </el-form>

      <div class="section-header section-gap">
        <span class="section-title">按集群单独配置</span>
        <span class="hint inline">
          表格里没有的集群用全局配置；每个集群的配置完整独立，不沿用全局的 @ 人
        </span>
      </div>
      <div class="toolbar">
        <el-select
          v-model="newEnv"
          filterable
          clearable
          placeholder="选择要单独配置的集群"
          no-data-text="还没有已注册的集群"
          no-match-text="没有匹配的集群"
          class="env-select"
          :disabled="!loaded || busy"
        >
          <el-option
            v-for="item in envChoices"
            :key="item.value"
            :label="item.label"
            :value="item.value"
            :disabled="item.disabled"
          />
        </el-select>
        <el-button :disabled="!loaded || busy || !newEnv" @click="addRow">
          添加
        </el-button>
      </div>
      <el-table
        :data="rows"
        row-key="key"
        border
        empty-text="没有单独配置的集群，全部用全局配置"
      >
        <el-table-column label="集群" min-width="150">
          <template #default="{ row }">
            <span class="env-name">{{ row.env }}</span>
            <el-tag
              v-if="!row.saved"
              size="small"
              type="warning"
              effect="plain"
              class="new-tag"
            >
              新增
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="IM 类型" width="140">
          <template #default="{ row }">
            <el-select v-model="row.type" :disabled="busy">
              <el-option
                v-for="item in TYPE_OPTIONS"
                :key="item.value"
                :label="item.label"
                :value="item.value"
              />
            </el-select>
          </template>
        </el-table-column>
        <el-table-column label="机器人地址" min-width="320">
          <template #default="{ row }">
            <el-input
              v-model="row.token"
              type="password"
              show-password
              autocomplete="new-password"
              :disabled="busy"
              :placeholder="tokenPlaceholder(row.type, row.saved)"
            />
            <div
              v-if="row.result"
              :class="['test-result', 'block', row.result.type]"
            >
              {{ row.result.text }}
            </div>
          </template>
        </el-table-column>
        <el-table-column label="默认 @ 人" min-width="220">
          <template #default="{ row }">
            <el-input
              v-model="row.at"
              clearable
              :disabled="busy"
              :placeholder="typeHint(row.type).at"
            />
          </template>
        </el-table-column>
        <el-table-column label="操作" width="120">
          <template #default="{ row, $index }">
            <el-button
              link
              type="primary"
              :loading="row.testing"
              :disabled="busy"
              @click="testRow(row)"
            >
              测试
            </el-button>
            <el-button
              link
              type="danger"
              :disabled="busy"
              @click="removeRow($index)"
            >
              删除
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <div class="actions">
        <el-button
          type="primary"
          :loading="saving"
          :disabled="!loaded || loading || removing"
          @click="save"
        >
          保存
        </el-button>
        <el-button :disabled="busy" @click="load">重置</el-button>
        <el-tag v-if="dirty" type="warning" effect="plain">
          有未保存的修改
        </el-tag>
      </div>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { computed, defineAsyncComponent, onMounted, reactive, ref } from "vue";
import dayjs from "dayjs";
import { ElMessageBox } from "element-plus";
import { message } from "@/utils/message";
import {
  type DeliveryQueryParams,
  type DeliveryRecord,
  type NotificationConfigV2,
  type NotificationConfigV2Input,
  type NotificationInput,
  type NotificationSettings,
  type NotifyChannel,
  type NotifyTestRequest,
  type NotifyTestV2Request,
  type ProviderSchema,
  getNotificationConfigV2,
  getNotificationDeliveries,
  getNotificationProviders,
  getNotificationSettings,
  saveNotificationConfigV2,
  saveNotificationSettings,
  testNotification,
  testNotificationV2
} from "@/api/notification";
import {
  type ChannelForm,
  type ChannelFormV2,
  type ClusterForm,
  type RouteFormV2,
  type TestView,
  CATEGORY_OPTIONS,
  FALLBACK_PROVIDERS,
  NOTICE_NOTE,
  NOTICE_ROWS,
  SEVERITY_OPTIONS,
  TYPE_LABELS,
  TYPE_OPTIONS,
  buildChannelPayloadV2,
  buildConfigV2Payload,
  buildTestPayloadV2,
  categoryLabel,
  channelFormFromSavedV2,
  channelHintSummaryV2,
  configV2Key,
  defaultChannelFormV2,
  defaultRouteFormV2,
  envOptions,
  findProviderSchema,
  formKey,
  formsFromSettings,
  newClusterRow,
  normalizeConfigV2Response,
  normalizeProvidersList,
  providerLabel,
  routeFormFromSavedV2,
  routeSummary,
  savedDefault,
  settingsPayload,
  severityLabel,
  switchChannelProviderV2,
  testErrorView,
  testRequest,
  testResultView,
  tokenPlaceholder,
  typeHint,
  withoutDefault
} from "./notification-utils";
import NotifyChannelFields from "./NotifyChannelFields.vue";

const DynamicChannelFields = defineAsyncComponent(
  () => import("./DynamicChannelFields.vue")
);

defineOptions({ name: "SystemNotification" });

/** 测试按钮的状态:全局配置 / 表格行共用 */
interface TestState {
  testing: boolean;
  result: TestView | null;
}

type ClusterRow = ClusterForm & TestState & { key: number };

const guideExpanded = ref(true);
const activeTab = ref<"channels" | "routes" | "deliveries">("channels");
const v2Mode = ref(false);

const loading = ref(false);
const saving = ref(false);
const removing = ref(false);
// 读取成功过才能保存:读取失败时不能用空表单覆盖已保存的配置
const loaded = ref(false);
const loadError = ref("");

/* ==================== v1 Compatibility State ==================== */
const settings = ref<NotificationSettings | null>(null);
const globalForm = reactive<ChannelForm>({ type: "wecom", token: "", at: "" });
const globalTest = reactive<TestState>({ testing: false, result: null });
const rows = ref<ClusterRow[]>([]);
const newEnv = ref("");
let rowSeq = 0;

/* ==================== v2 Unified State ==================== */
const providers = ref<ProviderSchema[]>(FALLBACK_PROVIDERS);
const configV2 = ref<NotificationConfigV2 | null>(null);
const channelsV2 = ref<ChannelFormV2[]>([]);
const routesV2 = ref<RouteFormV2[]>([]);
const defaultChannelIdsV2 = ref<string[]>([]);
const envsV2 = ref<string[]>([]);
const v2Baseline = ref("");
const providerFilter = ref("");
const channelTestStates = reactive<Record<string, TestState>>({});

// Channel drawer state
const drawerVisible = ref(false);
const editingNew = ref(false);
const editingOriginalId = ref("");
const editingForm = ref<ChannelFormV2>(defaultChannelFormV2("wecom"));
const drawerTest = reactive<TestState>({ testing: false, result: null });
let channelSeq = 0;
let routeSeq = 0;

// Deliveries audit log state
const deliveries = ref<DeliveryRecord[]>([]);
const deliveriesLoading = ref(false);
const deliveriesError = ref("");
const deliveryFilter = reactive<{ env: string; provider: string; ok: string }>({
  env: "",
  provider: "",
  ok: ""
});

const busy = computed(() => loading.value || saving.value || removing.value);
const globalSaved = computed(() => savedDefault(settings.value));
const updatedBy = computed(() =>
  v2Mode.value
    ? (configV2.value?.updated_by ?? "")
    : (settings.value?.updated_by ?? "")
);
const updatedAt = computed(() =>
  v2Mode.value
    ? (configV2.value?.updated_at ?? "")
    : (settings.value?.updated_at ?? "")
);
const envChoices = computed(() =>
  envOptions(settings.value?.envs ?? [], rows.value)
);

const filteredChannels = computed(() => {
  if (!providerFilter.value) return channelsV2.value;
  return channelsV2.value.filter(
    item => item.provider === providerFilter.value
  );
});

const channelNameMap = computed(() => {
  const map = new Map<string, string>();
  for (const ch of channelsV2.value) {
    map.set(ch.id, ch.name || ch.id);
  }
  return map;
});

const editingProviderSchema = computed(
  () =>
    findProviderSchema(editingForm.value.provider, providers.value) ??
    providers.value[0]
);

/** 和已保存的配置比,有没有没保存的修改 */
const dirty = computed(() => {
  if (!loaded.value) return false;
  if (v2Mode.value) {
    return (
      configV2Key(
        channelsV2.value,
        routesV2.value,
        defaultChannelIdsV2.value
      ) !== v2Baseline.value
    );
  }
  if (!settings.value) return false;
  const saved = formsFromSettings(settings.value, globalForm.type);
  return (
    formKey(globalSaved.value, globalForm, rows.value) !==
    formKey(globalSaved.value, saved.global, saved.rows)
  );
});

function formatTime(value: string) {
  return value ? dayjs(value).format("YYYY-MM-DD HH:mm:ss") : "";
}

function errorText(error: unknown, fallback: string) {
  const err = error as any;
  const data = err?.response?.data;
  return (
    data?.message ||
    data?.error ||
    (err?.response?.status ? `HTTP ${err.response.status}` : "") ||
    err?.message ||
    fallback
  );
}

function severityTagType(severity?: string) {
  if (severity === "critical") return "danger";
  if (severity === "warning") return "warning";
  if (severity === "resolved") return "success";
  return "info";
}

function toRow(form: ClusterForm): ClusterRow {
  return { ...form, key: ++rowSeq, testing: false, result: null };
}

/* ==================== v1 Methods ==================== */

function applySaved(data: NotificationSettings, keepRows = false) {
  settings.value = data;
  const forms = formsFromSettings(data, globalForm.type);
  Object.assign(globalForm, forms.global);
  globalTest.result = null;
  if (keepRows) {
    const byEnv = new Map((data.clusters ?? []).map(item => [item.env, item]));
    for (const row of rows.value) row.saved = byEnv.get(row.env) ?? null;
  } else {
    rows.value = forms.rows.map(toRow);
    newEnv.value = "";
  }
  loaded.value = true;
}

/* ==================== v2 Methods ==================== */

function applySavedV2(data: NotificationConfigV2) {
  const normalized = normalizeConfigV2Response(data);
  configV2.value = normalized;
  channelsV2.value = normalized.channels.map(ch =>
    channelFormFromSavedV2(ch, providers.value)
  );
  routesV2.value = normalized.routes.map(routeFormFromSavedV2);
  defaultChannelIdsV2.value = [...normalized.default_channel_ids];
  envsV2.value = [...normalized.envs];
  for (const key of Object.keys(channelTestStates)) {
    delete channelTestStates[key];
  }
  v2Baseline.value = configV2Key(
    channelsV2.value,
    routesV2.value,
    defaultChannelIdsV2.value
  );
  v2Mode.value = true;
  loaded.value = true;
}

async function loadProviders() {
  if (typeof getNotificationProviders !== "function") {
    providers.value = FALLBACK_PROVIDERS;
    return;
  }
  try {
    const res = await getNotificationProviders(true);
    if (res?.success) {
      providers.value = normalizeProvidersList(res.data);
    }
  } catch {
    providers.value = FALLBACK_PROVIDERS;
  }
}

async function loadDeliveries() {
  if (typeof getNotificationDeliveries !== "function") return;
  deliveriesLoading.value = true;
  deliveriesError.value = "";
  const params: DeliveryQueryParams = { limit: 100 };
  if (deliveryFilter.env.trim()) params.env = deliveryFilter.env.trim();
  if (deliveryFilter.provider) params.provider = deliveryFilter.provider;
  if (deliveryFilter.ok !== "") params.ok = deliveryFilter.ok;
  try {
    const res = await getNotificationDeliveries(params);
    if (!res?.success) {
      throw new Error(res?.message || "读取投递日志失败");
    }
    const raw = res.data as any;
    deliveries.value = Array.isArray(raw)
      ? raw
      : Array.isArray(raw?.items)
        ? raw.items
        : Array.isArray(raw?.deliveries)
          ? raw.deliveries
          : [];
  } catch (error) {
    deliveriesError.value = errorText(error, "读取投递审计日志失败");
  } finally {
    deliveriesLoading.value = false;
  }
}

function handleTabChange(name: string | number) {
  if (name === "deliveries" && v2Mode.value) {
    void loadDeliveries();
  }
}

function nextUniqueChannelId(base = "ch"): string {
  const existing = new Set(channelsV2.value.map(c => c.id));
  let idx = Math.max(1, channelsV2.value.length + 1);
  let candidate = `${base}_${++channelSeq}_${idx}`;
  while (existing.has(candidate)) {
    idx++;
    candidate = `${base}_${channelSeq}_${idx}`;
  }
  return candidate;
}

function openNewChannel(providerId = "wecom") {
  editingNew.value = true;
  editingOriginalId.value = "";
  const id = nextUniqueChannelId(providerId);
  editingForm.value = defaultChannelFormV2(providerId, providers.value, {
    id,
    name: `${providerLabel(providerId, providers.value)}通道`
  });
  drawerTest.result = null;
  drawerVisible.value = true;
}

function openEditChannel(row: ChannelFormV2) {
  editingNew.value = false;
  editingOriginalId.value = row.id;
  editingForm.value = {
    ...row,
    settings: { ...row.settings },
    secrets: { ...row.secrets },
    secretHints: { ...row.secretHints },
    configuredSecrets: [...row.configuredSecrets]
  };
  drawerTest.result = null;
  drawerVisible.value = true;
}

function onEditingProviderChange(nextProvider: string) {
  editingForm.value = switchChannelProviderV2(
    editingForm.value,
    nextProvider,
    providers.value
  );
  drawerTest.result = null;
}

function confirmEditChannel() {
  try {
    buildChannelPayloadV2(editingForm.value, providers.value);
  } catch (error) {
    message((error as Error).message, { type: "warning" });
    return;
  }
  const normalizedId = editingForm.value.id.trim();
  const duplicate = channelsV2.value.some(
    item =>
      item.id === normalizedId &&
      (editingNew.value || item.id !== editingOriginalId.value)
  );
  if (duplicate) {
    message(`通道 ID「${normalizedId}」已存在`, { type: "warning" });
    return;
  }
  const updated: ChannelFormV2 = {
    ...editingForm.value,
    id: normalizedId,
    name: editingForm.value.name.trim(),
    settings: { ...editingForm.value.settings },
    secrets: { ...editingForm.value.secrets },
    secretHints: { ...editingForm.value.secretHints },
    configuredSecrets: [...editingForm.value.configuredSecrets]
  };
  if (editingNew.value) {
    channelsV2.value.push(updated);
  } else {
    const idx = channelsV2.value.findIndex(
      item => item.id === editingOriginalId.value
    );
    if (idx >= 0) channelsV2.value.splice(idx, 1, updated);
  }
  drawerVisible.value = false;
}

function duplicateChannel(row: ChannelFormV2) {
  const newId = nextUniqueChannelId(row.provider);
  channelsV2.value.push({
    id: newId,
    name: `${row.name} (副本)`,
    provider: row.provider,
    enabled: row.enabled,
    settings: { ...row.settings },
    secrets: Object.keys(row.secrets ?? {}).reduce<Record<string, string>>(
      (acc, key) => {
        acc[key] = "";
        return acc;
      },
      {}
    ),
    savedProvider: "",
    secretHints: {},
    configuredSecrets: []
  });
}

async function deleteChannel(channelId: string) {
  const usedInRoutes = routesV2.value.some(rt =>
    rt.channel_ids.includes(channelId)
  );
  const usedInDefault = defaultChannelIdsV2.value.includes(channelId);
  if (usedInRoutes || usedInDefault) {
    try {
      await ElMessageBox.confirm(
        "该通道正被默认兜底或路由规则引用，删除后将自动从相关引用中移除，是否继续？",
        "删除通知通道",
        { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" }
      );
    } catch {
      return;
    }
  }
  channelsV2.value = channelsV2.value.filter(item => item.id !== channelId);
  defaultChannelIdsV2.value = defaultChannelIdsV2.value.filter(
    id => id !== channelId
  );
  for (const rt of routesV2.value) {
    rt.channel_ids = rt.channel_ids.filter(id => id !== channelId);
  }
  delete channelTestStates[channelId];
}

function addRoute() {
  const existing = new Set(routesV2.value.map(item => item.id));
  let seq = Math.max(1, routesV2.value.length + 1);
  let form = defaultRouteFormV2(++routeSeq + seq);
  while (existing.has(form.id)) {
    seq++;
    form = defaultRouteFormV2(seq);
  }
  routesV2.value.push(form);
}

function removeRoute(index: number) {
  routesV2.value.splice(index, 1);
}

function moveRoute(index: number, delta: number) {
  const target = index + delta;
  if (target < 0 || target >= routesV2.value.length) return;
  const list = [...routesV2.value];
  const [item] = list.splice(index, 1);
  list.splice(target, 0, item);
  routesV2.value = list;
}

async function runTestV2(form: ChannelFormV2, state: TestState) {
  if (state.testing) return;
  let request: NotifyTestV2Request;
  try {
    request = buildTestPayloadV2(form, { providers: providers.value });
  } catch (error) {
    message((error as Error).message, { type: "warning" });
    return;
  }
  state.testing = true;
  let view: TestView | null = null;
  let fromIm = false;
  try {
    const res = await testNotificationV2(request);
    if (res.success) {
      view = testResultView(res.data);
      fromIm = true;
    } else {
      view = { type: "error", text: res.message || "发送测试消息失败" };
    }
  } catch (error) {
    view = testErrorView(error);
  } finally {
    state.testing = false;
  }
  if (!view) return;
  state.result = view;
  if (!fromIm) message(view.text, { type: view.type });
  else if (view.type === "success")
    message("发送成功，请确认目标通道收到测试消息", { type: "success" });
  else message("发送失败，原因见页面上的测试结果", { type: "warning" });
}

function quickTestChannel(row: ChannelFormV2) {
  if (!channelTestStates[row.id]) {
    channelTestStates[row.id] = { testing: false, result: null };
  }
  return runTestV2(row, channelTestStates[row.id]);
}

function testEditingChannel() {
  return runTestV2(editingForm.value, drawerTest);
}

/* ==================== Unified Load / Save ==================== */

async function load() {
  if (saving.value || removing.value) return;
  loading.value = true;
  loadError.value = "";
  const supportsV2 =
    typeof getNotificationConfigV2 === "function" &&
    typeof getNotificationProviders === "function";

  try {
    if (supportsV2) {
      await loadProviders();
      try {
        const res = await getNotificationConfigV2(true);
        if (!res.success) throw new Error(res.message || "读取通知配置失败");
        applySavedV2(res.data);
        if (activeTab.value === "deliveries") {
          void loadDeliveries();
        }
        return;
      } catch (err) {
        const status = (err as any)?.response?.status;
        if (status !== 404 && status !== 501) {
          throw err;
        }
        // 404/501 表示后端尚无 v2 接口，降级使用 v1
        v2Mode.value = false;
      }
    } else {
      v2Mode.value = false;
    }

    const res = await getNotificationSettings();
    if (!res.success) throw new Error(res.message || "读取通知配置失败");
    applySaved(res.data);
  } catch (error) {
    loadError.value = errorText(error, "读取通知配置失败");
  } finally {
    loading.value = false;
  }
}

async function save() {
  if (!loaded.value || busy.value) return;
  if (v2Mode.value) {
    let payload: NotificationConfigV2Input;
    try {
      payload = buildConfigV2Payload(
        channelsV2.value,
        routesV2.value,
        defaultChannelIdsV2.value,
        providers.value
      );
    } catch (error) {
      message((error as Error).message, { type: "warning" });
      return;
    }
    saving.value = true;
    try {
      const res = await saveNotificationConfigV2(payload);
      if (!res.success) {
        message(res.message || "保存失败", { type: "error" });
        return;
      }
      applySavedV2(res.data);
      message("已保存，马上生效", { type: "success" });
    } catch (error) {
      message(errorText(error, "保存失败"), { type: "error" });
    } finally {
      saving.value = false;
    }
    return;
  }

  let payload: NotificationInput;
  try {
    payload = settingsPayload(globalForm, globalSaved.value, rows.value);
  } catch (error) {
    message((error as Error).message, { type: "warning" });
    return;
  }
  saving.value = true;
  try {
    // 接口返回的错误(比如 422 要求重新填写机器人地址)由 http 封装统一提示
    const res = await saveNotificationSettings(payload);
    if (!res.success) {
      message(res.message || "保存失败", { type: "error" });
      return;
    }
    applySaved(res.data);
    message("已保存，马上生效", { type: "success" });
  } finally {
    saving.value = false;
  }
}

/** 删除全局配置(v1):二次确认后马上生效;按集群的配置原样保留,表格里没保存的修改也保留 */
async function removeDefault() {
  if (!loaded.value || busy.value || !globalSaved.value || !settings.value)
    return;
  try {
    await ElMessageBox.confirm(
      "删除后马上生效：没有单独配置的集群不再发送任何通知（K8S 事件告警、运维操作通知、指标告警、管控表通知）；单独配置了的集群不受影响。",
      "删除全局配置",
      { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" }
    );
  } catch {
    return;
  }
  removing.value = true;
  try {
    const res = await saveNotificationSettings(withoutDefault(settings.value));
    if (!res.success) {
      message(res.message || "删除失败", { type: "error" });
      return;
    }
    applySaved(res.data, true);
    message("已删除全局配置", { type: "success" });
  } finally {
    removing.value = false;
  }
}

function addRow() {
  const env = newEnv.value;
  if (!env || rows.value.some(row => row.env === env)) return;
  rows.value.push(toRow(newClusterRow(env, settings.value, globalForm)));
  newEnv.value = "";
}

function removeRow(index: number) {
  rows.value.splice(index, 1);
}

/** 发测试消息(v1):已填的机器人地址用填的值,没填用已保存的 */
async function runTest(
  env: string,
  form: ChannelForm,
  saved: NotifyChannel | null,
  state: TestState
) {
  if (state.testing) return;
  let request: NotifyTestRequest;
  try {
    request = testRequest(env, form, saved);
  } catch (error) {
    message((error as Error).message, { type: "warning" });
    return;
  }
  state.testing = true;
  let view: TestView | null = null;
  let fromIm = false;
  try {
    const res = await testNotification(request);
    if (res.success) {
      view = testResultView(res.data);
      fromIm = true;
    } else {
      view = { type: "error", text: res.message || "发送测试消息失败" };
    }
  } catch (error) {
    view = testErrorView(error);
  } finally {
    state.testing = false;
  }
  if (!view) return;
  state.result = view;
  if (!fromIm) message(view.text, { type: view.type });
  else if (view.type === "success")
    message("发送成功，请到群里查看", { type: "success" });
  else message("发送失败，原因见页面上的测试结果", { type: "warning" });
}

function testGlobal() {
  return runTest("", globalForm, globalSaved.value, globalTest);
}

function testRow(row: ClusterRow) {
  return runTest(row.env, row, row.saved, row);
}

onMounted(load);
</script>

<style lang="scss" scoped>
@media (width <= 768px) {
  .route-grid {
    grid-template-columns: minmax(0, 1fr);
  }
}

.system-notification {
  max-width: 1280px;
  padding: 4px;
}

.card-header {
  display: flex;
  gap: 12px;
  align-items: center;
  justify-content: space-between;
}

.header-title-group {
  display: flex;
  gap: 10px;
  align-items: center;
}

.note {
  margin-top: 8px;
  font-size: 13px;
  line-height: 1.8;
  color: var(--el-text-color-regular);

  &.compact {
    margin-top: 0;
  }
}

// el-alert 自带 margin: 0,工具类 mb-4 压不过,用带作用域的类
.page-alert {
  margin-bottom: 16px;
}

.section-header {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  margin-bottom: 12px;
}

.section-gap {
  padding-top: 16px;
  margin-top: 16px;
  border-top: 1px solid var(--el-border-color-lighter);
}

.section-title {
  font-size: 15px;
  font-weight: 600;
}

.remove-default,
.add-route-btn {
  margin-left: auto;
}

// 表单项里的提示独占一行(在输入框下面);inline 的跟在标题后面
.hint {
  display: block;
  width: 100%;
  font-size: 12px;
  line-height: 1.6;
  color: var(--el-text-color-secondary);

  &.inline {
    display: inline;
    width: auto;
  }
}

.channel-form {
  max-width: 760px;
}

.toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  margin-bottom: 12px;
}

.env-select {
  width: 280px;
}

.filter-select {
  width: 180px;

  &.small {
    width: 120px;
  }
}

.default-channels-select,
.full-width {
  width: 100%;
}

.channel-name {
  font-weight: 500;
  color: var(--el-text-color-primary);
}

.channel-id {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.channel-summary {
  font-size: 13px;
  word-break: break-all;
}

.empty-routes {
  padding: 20px;
  font-size: 13px;
  color: var(--el-text-color-secondary);
  text-align: center;
  background: var(--el-fill-color-lighter);
  border-radius: 6px;
}

.route-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.route-card {
  border-color: var(--el-border-color-lighter);
}

.route-card-head {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}

.route-head-left {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  align-items: center;
}

.route-head-actions {
  display: flex;
  gap: 6px;
  align-items: center;
}

.route-name-input {
  width: 220px;
}

.route-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}

.route-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.route-field-label {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.route-summary-line {
  margin-top: 10px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--el-text-color-secondary);
}

.env-name {
  word-break: break-all;
}

.new-tag {
  margin-left: 6px;
}

.test-result {
  flex: 1;
  min-width: 0;
  margin-left: 12px;
  font-size: 13px;
  line-height: 1.6;
  word-break: break-all;

  // 表格里的结果放在机器人地址下面
  &.block {
    display: block;
    margin: 4px 0 0;
    font-size: 12px;
  }

  &.success {
    color: var(--el-color-success);
  }

  &.warning {
    color: var(--el-color-warning);
  }

  &.error {
    color: var(--el-color-danger);
  }
}

// 按钮之间沿用 el-button 自带的间距,这里不用 gap
.actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  margin-top: 16px;

  .el-tag {
    margin-left: 12px;
  }
}
</style>
