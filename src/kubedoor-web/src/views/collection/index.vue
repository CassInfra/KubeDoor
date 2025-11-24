<script setup lang="ts">
import { ref } from "vue";
import { transformI18n } from "@/plugins/i18n";
import { useCollection } from "./utils/hook";
import { PureTableBar } from "@/components/RePureTableBar";
import { useRenderIcon } from "@/components/ReIcon/src/hooks";
import { useSearchStoreHook } from "@/store/modules/search";

import Refresh from "@iconify-icons/ep/refresh";

defineOptions({
  name: "Resource Collection"
});

const searchStore = useSearchStoreHook();
const {
  envList,
  queryForm,
  loading,
  columns,
  dataList,
  deviceDetection,
  onSearch,
  resetForm
} = useCollection(searchStore);
</script>

<template>
  <div :class="['flex', 'justify-between', deviceDetection() && 'flex-wrap']">
    <div :class="[deviceDetection() ? ['w-full', 'mt-2'] : 'w-full']">
      <el-form
        :inline="true"
        :model="queryForm"
        class="search-form bg-bg_color w-[99/100] pl-8 pt-[12px] overflow-auto"
      >
        <el-form-item :label="transformI18n('resource.column.env')" prop="env">
          <el-select
            v-model="queryForm.env"
            class="!w-[220px]"
            :placeholder="transformI18n('resource.placeholder')"
            clearable
          >
            <el-option
              v-for="item in envList"
              :key="item[0]"
              :label="item[0]"
              :value="item[0]"
            />
          </el-select>
        </el-form-item>
        <el-form-item
          :label="transformI18n('resource.column.date')"
          prop="date"
        >
          <el-date-picker
            v-model="queryForm.date"
            class="!w-[180px]"
            value-format="YYYY-MM-DD"
            type="date"
            :placeholder="transformI18n('resource.placeholder')"
          />
        </el-form-item>
        <el-form-item>
          <el-button
            type="primary"
            :icon="useRenderIcon('ri:search-line')"
            :loading="loading"
            @click="onSearch"
          >
            {{ transformI18n("resource.search") }}
          </el-button>
          <el-button :icon="useRenderIcon(Refresh)" @click="resetForm()">
            {{ transformI18n("resource.reset") }}
          </el-button>
        </el-form-item>
      </el-form>

      <PureTableBar title="" :columns="columns" @refresh="onSearch">
        <template v-slot="{ size, dynamicColumns }">
          <pure-table
            ref="tableRef"
            :row-key="(row: any) => row.namespace + row.deployment"
            adaptive
            :adaptiveConfig="{ offsetBottom: 28 }"
            align-whole="center"
            table-layout="auto"
            :loading="loading"
            :size="size"
            :data="dataList"
            :columns="dynamicColumns"
            :header-cell-style="{
              background: 'var(--el-fill-color-light)',
              color: 'var(--el-text-color-primary)'
            }"
            :default-sort="{ prop: 'p95_pod_cpu_pct', order: 'descending' }"
          />
        </template>
      </PureTableBar>
    </div>
  </div>
</template>

<style scoped lang="scss">
:deep(.el-dropdown-menu__item i) {
  margin: 0;
}

:deep(.el-button:focus-visible) {
  outline: none;
}

.main-content {
  margin: 20px !important;
}

.search-form {
  :deep(.el-form-item) {
    margin-bottom: 12px;
  }
}
</style>
