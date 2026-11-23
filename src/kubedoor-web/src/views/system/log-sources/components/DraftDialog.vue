<script setup lang="ts">
// 草稿对话框：列出将新增的日志源、将补充说明的已有日志源、建议绑定（按集群）、关联字段、集群说明、网络架构；
// 每项可勾选，「应用到表单」只改页面上的表单，保存还要点页面底部的「保存」
import { computed, reactive, watch } from "vue";
import type { LogKnownCluster } from "@/api/system";
import AIMarkdown from "@/components/AI/AIMarkdown.vue";
import {
  type DraftBindingRow,
  type DraftPicks,
  type DraftState,
  type LogSourcesForm,
  defaultPicks,
  draftView,
  kindLabel
} from "../log-sources-utils";

defineOptions({ name: "LogDraftDialog" });

const visible = defineModel<boolean>("visible", { required: true });

const props = defineProps<{
  draft: DraftState | null;
  form: LogSourcesForm;
  known: LogKnownCluster[];
}>();

const emit = defineEmits<{ apply: [picks: DraftPicks] }>();

const picks = reactive<DraftPicks>({
  sources: [],
  bindings: [],
  keys: [],
  notes: [],
  architecture: false
});

const view = computed(() =>
  props.draft ? draftView(props.form, props.draft, props.known) : null
);

/** 草稿里新日志源的 ID → 应用后的 ID（和已有的重复时加 -2） */
const finalIds = computed(() => {
  const map = new Map<string, string>();
  for (const row of view.value?.newSources ?? [])
    if (!map.has(row.id)) map.set(row.id, row.finalId);
  return map;
});

const total = computed(
  () =>
    picks.sources.length +
    picks.bindings.filter(
      key =>
        !view.value?.bindings.some(group =>
          group.rows.some(row => row.key === key && bindingBlocked(row))
        )
    ).length +
    picks.keys.length +
    picks.notes.length +
    (picks.architecture ? 1 : 0)
);

watch(visible, open => {
  if (open && props.draft)
    Object.assign(picks, defaultPicks(props.form, props.draft, props.known));
});

function toggle(list: string[], value: string, on: boolean) {
  const index = list.indexOf(value);
  if (on && index < 0) list.push(value);
  if (!on && index >= 0) list.splice(index, 1);
}

/** 绑定不能勾选的原因：已有绑定不覆盖；引用的新日志源没勾选 */
function bindingBlocked(row: DraftBindingRow): string {
  if (row.exists) return "已有绑定，不覆盖";
  if (!row.existingSource && !picks.sources.includes(row.source))
    return "日志源没勾选";
  return "";
}

function apply() {
  emit("apply", {
    sources: [...picks.sources],
    bindings: [...picks.bindings],
    keys: [...picks.keys],
    notes: [...picks.notes],
    architecture: picks.architecture
  });
}
</script>

<template>
  <el-dialog
    v-model="visible"
    title="草稿：勾选后应用到表单"
    width="980px"
    top="5vh"
    :close-on-click-modal="false"
    class="log-draft-dialog"
  >
    <template v-if="draft && view">
      <div class="draft-meta">
        <el-tag
          size="small"
          :type="draft.origin === 'ai' ? 'success' : 'info'"
          effect="plain"
        >
          {{
            draft.origin === "ai"
              ? `AI 生成${draft.model ? `（${draft.model}）` : ""}`
              : "按规则生成"
          }}
        </el-tag>
        <span class="meta-text">
          「应用到表单」后还要检查，点页面底部的「保存」才生效{{
            draft.origin === "ai" ? "；AI 不确定的地方标了「（待确认）」" : ""
          }}
        </span>
      </div>
      <el-alert
        v-if="draft.notes.length"
        type="warning"
        :closable="false"
        show-icon
        title="说明"
        class="draft-alert"
      >
        <ul class="note-list">
          <li v-for="(item, index) in draft.notes" :key="index">{{ item }}</li>
        </ul>
      </el-alert>

      <section v-if="view.newSources.length">
        <div class="section-title">
          将新增的日志源（{{ view.newSources.length }}）
        </div>
        <div
          v-for="row in view.newSources"
          :key="`new-${row.group}-${row.id}`"
          class="draft-row"
        >
          <el-checkbox
            :model-value="picks.sources.includes(row.id)"
            @update:model-value="
              value => toggle(picks.sources, row.id, !!value)
            "
          />
          <div class="draft-main">
            <div class="draft-line">
              <strong>{{ row.name }}</strong>
              <code>{{ row.finalId }}</code>
              <el-tag size="small" effect="plain">{{
                kindLabel(row.kind)
              }}</el-tag>
              <span v-if="row.finalId !== row.id" class="warn-text">
                ID {{ row.id }} 已被占用，应用时改为 {{ row.finalId }}
              </span>
            </div>
            <div class="draft-sub">
              {{ row.target || "（没有默认表 / 索引）" }} · 时间字段
              {{ row.time_field || "（没识别出来，应用后要手填）" }} ·
              {{ row.fields }} 个语义字段 · {{ row.columns }} 个字段说明
            </div>
            <div v-if="row.description" class="draft-desc">
              {{ row.description }}
            </div>
          </div>
        </div>
      </section>

      <section v-if="view.updateSources.length">
        <div class="section-title">
          将补充说明的已有日志源（{{ view.updateSources.length }}）
        </div>
        <div
          v-for="row in view.updateSources"
          :key="`update-${row.id}`"
          class="draft-row"
        >
          <el-checkbox
            :model-value="picks.sources.includes(row.id)"
            :disabled="!row.adds.length"
            @update:model-value="
              value => toggle(picks.sources, row.id, !!value)
            "
          />
          <div class="draft-main">
            <div class="draft-line">
              <strong>{{ row.name }}</strong>
              <code>{{ row.id }}</code>
              <span v-if="row.adds.length" class="draft-sub">
                补充：{{ row.adds.join("、") }}
              </span>
              <span v-else class="draft-sub">
                没有能补充的（只补空的说明和还没有说明的字段，已有的不覆盖）
              </span>
            </div>
            <div v-if="row.adds.includes('说明')" class="draft-desc">
              {{ row.description }}
            </div>
          </div>
        </div>
      </section>

      <section v-if="view.bindings.length">
        <div class="section-title">建议绑定</div>
        <div
          v-for="group in view.bindings"
          :key="group.env"
          class="binding-group"
        >
          <div class="draft-line">
            <strong>{{ group.env }}</strong>
            <el-tag
              v-if="!group.registered"
              size="small"
              type="warning"
              effect="plain"
            >
              未登记
            </el-tag>
          </div>
          <div
            v-for="row in group.rows"
            :key="row.key"
            class="draft-row nested"
          >
            <el-checkbox
              :model-value="
                picks.bindings.includes(row.key) && !bindingBlocked(row)
              "
              :disabled="!!bindingBlocked(row)"
              @update:model-value="
                value => toggle(picks.bindings, row.key, !!value)
              "
            />
            <div class="draft-main">
              <div class="draft-line">
                <code>{{ finalIds.get(row.source) || row.source }}</code>
                <span>→</span>
                <code>{{ row.target || "（默认表 / 索引）" }}</code>
                <span class="draft-sub">
                  {{ row.conditions || "整张表 / 整个索引" }}
                </span>
                <el-tag
                  v-if="row.match"
                  size="small"
                  type="info"
                  effect="plain"
                >
                  {{ row.match }}
                </el-tag>
                <span v-if="bindingBlocked(row)" class="muted-text">
                  {{ bindingBlocked(row) }}
                </span>
              </div>
              <div v-if="row.problem" class="warn-text">
                表 / 索引：{{ row.problem }}；应用后在「集群」里修改
              </div>
              <div v-if="row.note" class="draft-sub">{{ row.note }}</div>
            </div>
          </div>
        </div>
      </section>

      <section v-if="view.keys.length">
        <div class="section-title">关联字段</div>
        <div v-for="row in view.keys" :key="row.key" class="draft-row">
          <el-checkbox
            :model-value="picks.keys.includes(row.key) && !row.exists"
            :disabled="row.exists"
            @update:model-value="value => toggle(picks.keys, row.key, !!value)"
          />
          <div class="draft-main">
            <div class="draft-line">
              <code>{{ row.key }}</code>
              <span>{{ row.label }}</span>
              <el-tag
                v-if="row.trace"
                size="small"
                type="success"
                effect="plain"
              >
                链路 ID
              </el-tag>
              <code v-if="row.pattern">{{ row.pattern }}</code>
              <span v-if="row.exists" class="muted-text">已有，不覆盖</span>
            </div>
            <div v-if="row.description" class="draft-sub">
              {{ row.description }}
            </div>
          </div>
        </div>
      </section>

      <section v-if="view.notes.length">
        <div class="section-title">集群说明</div>
        <div v-for="row in view.notes" :key="row.env" class="draft-row">
          <el-checkbox
            :model-value="picks.notes.includes(row.env)"
            @update:model-value="value => toggle(picks.notes, row.env, !!value)"
          />
          <div class="draft-main">
            <div class="draft-line">
              <strong>{{ row.env }}</strong>
              <span v-if="row.overwrite" class="warn-text">
                勾选会覆盖这个集群原来的说明
              </span>
            </div>
            <div class="draft-desc">{{ row.note }}</div>
          </div>
        </div>
      </section>

      <section v-if="view.architecture">
        <div class="section-title">网络架构说明</div>
        <div class="draft-row">
          <el-checkbox v-model="picks.architecture" />
          <div class="draft-main">
            <div class="draft-line">
              <span>采用</span>
              <span v-if="view.architecture.overwrite" class="warn-text">
                勾选会覆盖原来的网络架构说明
              </span>
            </div>
            <div class="architecture-preview">
              <AIMarkdown :content="view.architecture.text" />
            </div>
          </div>
        </div>
      </section>

      <el-empty
        v-if="
          !view.newSources.length &&
          !view.updateSources.length &&
          !view.bindings.length &&
          !view.keys.length &&
          !view.notes.length &&
          !view.architecture
        "
        description="草稿是空的：换几个组再试"
      />
    </template>
    <template #footer>
      <span class="footer-count">已勾选 {{ total }} 项</span>
      <el-button @click="visible = false">取消</el-button>
      <el-button type="primary" :disabled="!total" @click="apply">
        应用到表单
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped lang="scss">
.draft-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  margin-bottom: 12px;
}

.meta-text,
.footer-count {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.footer-count {
  margin-right: 12px;
}

.draft-alert {
  margin-bottom: 12px;
}

.note-list {
  padding-left: 18px;
  margin: 4px 0 0;
  line-height: 1.7;
  list-style: disc;
}

.section-title {
  padding-top: 12px;
  margin: 4px 0 6px;
  font-weight: 600;
  border-top: 1px solid var(--el-border-color-lighter);
}

.draft-row {
  display: flex;
  gap: 10px;
  align-items: flex-start;
  padding: 6px 0;

  &.nested {
    padding-left: 16px;
  }

  > .el-checkbox {
    height: 22px;
  }
}

.binding-group {
  margin-bottom: 6px;
}

.draft-main {
  flex: 1;
  min-width: 0;
}

.draft-line {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 8px;
  align-items: center;
  line-height: 22px;
}

.draft-sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.draft-desc {
  display: -webkit-box;
  margin-top: 2px;
  overflow: hidden;
  font-size: 12px;
  line-height: 1.6;
  color: var(--el-text-color-regular);
  -webkit-line-clamp: 3;
  white-space: pre-wrap;
  -webkit-box-orient: vertical;
}

.architecture-preview {
  max-height: 260px;
  padding: 8px 12px;
  margin-top: 4px;
  overflow: auto;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 4px;
}

.warn-text {
  font-size: 12px;
  color: var(--el-color-warning);
}

.muted-text {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

code {
  padding: 0 4px;
  font-family: Menlo, Consolas, monospace;
  font-size: 12px;
  background: var(--el-fill-color-light);
  border-radius: 3px;
}
</style>
