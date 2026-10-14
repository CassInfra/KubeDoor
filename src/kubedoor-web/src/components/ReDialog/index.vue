<script setup lang="ts">
import {
  type EventType,
  type ButtonProps,
  type DialogOptions,
  closeDialog,
  dialogStore
} from "./index";
import { ref, computed } from "vue";
import { isFunction } from "@pureadmin/utils";
import Fullscreen from "@iconify-icons/ri/fullscreen-fill";
import ExitFullscreen from "@iconify-icons/ri/fullscreen-exit-fill";

defineOptions({
  name: "ReDialog"
});

const sureBtnMap = ref({});
const fullscreen = ref(false);

const footerButtons = computed(() => {
  return (options: DialogOptions) => {
    return options?.footerButtons?.length > 0
      ? options.footerButtons
      : ([
          {
            label: "取消",
            text: true,
            bg: true,
            btnClick: ({ dialog: { options, index } }) => {
              const done = () =>
                closeDialog(options, index, { command: "cancel" });
              if (options?.beforeCancel && isFunction(options?.beforeCancel)) {
                options.beforeCancel(done, { options, index });
              } else {
                done();
              }
            }
          },
          {
            label: "确定",
            type: "primary",
            text: true,
            bg: true,
            popconfirm: options?.popconfirm,
            btnClick: ({ dialog: { options, index } }) => {
              if (options?.sureBtnLoading) {
                sureBtnMap.value[index] = Object.assign(
                  {},
                  sureBtnMap.value[index],
                  {
                    loading: true
                  }
                );
              }
              const closeLoading = () => {
                if (options?.sureBtnLoading) {
                  sureBtnMap.value[index].loading = false;
                }
              };
              const done = () => {
                closeLoading();
                closeDialog(options, index, { command: "sure" });
              };
              if (options?.beforeSure && isFunction(options?.beforeSure)) {
                options.beforeSure(done, { options, index, closeLoading });
              } else {
                done();
              }
            }
          }
        ] as Array<ButtonProps>);
  };
});

const fullscreenClass = computed(() => {
  return [
    "el-icon",
    "el-dialog__close",
    "-translate-x-2",
    "cursor-pointer",
    "hover:!text-[red]"
  ];
});

function eventsCallBack(
  event: EventType,
  options: DialogOptions,
  index: number,
  isClickFullScreen = false
) {
  if (!isClickFullScreen) fullscreen.value = options?.fullscreen ?? false;
  if (options?.[event] && isFunction(options?.[event])) {
    return options?.[event]({ options, index });
  }
}

function handleClose(
  options: DialogOptions,
  index: number,
  args = { command: "close" }
) {
  closeDialog(options, index, args);
  eventsCallBack("close", options, index);
}
</script>

<template>
  <el-dialog
    v-for="(options, index) in dialogStore"
    :key="index"
    v-bind="options"
    v-model="options.visible"
    class="pure-dialog"
    :fullscreen="fullscreen ? true : options?.fullscreen ? true : false"
    @closed="handleClose(options, index)"
    @opened="eventsCallBack('open', options, index)"
    @openAutoFocus="eventsCallBack('openAutoFocus', options, index)"
    @closeAutoFocus="eventsCallBack('closeAutoFocus', options, index)"
  >
    <!-- header -->
    <template
      v-if="options?.fullscreenIcon || options?.headerRenderer"
      #header="{ close, titleId, titleClass }"
    >
      <div
        v-if="options?.fullscreenIcon"
        class="flex items-center justify-between"
      >
        <span :id="titleId" :class="titleClass">{{ options?.title }}</span>
        <i
          v-if="!options?.fullscreen"
          :class="fullscreenClass"
          @click="
            () => {
              fullscreen = !fullscreen;
              eventsCallBack(
                'fullscreenCallBack',
                { ...options, fullscreen },
                index,
                true
              );
            }
          "
        >
          <IconifyIconOffline
            class="pure-dialog-svg"
            :icon="
              options?.fullscreen
                ? ExitFullscreen
                : fullscreen
                  ? ExitFullscreen
                  : Fullscreen
            "
          />
        </i>
      </div>
      <component
        :is="options?.headerRenderer({ close, titleId, titleClass })"
        v-else
      />
    </template>
    <component
      v-bind="options?.props"
      :is="options.contentRenderer({ options, index })"
      @close="args => handleClose(options, index, args)"
    />
    <!-- footer -->
    <template v-if="!options?.hideFooter" #footer>
      <template v-if="options?.footerRenderer">
        <component :is="options?.footerRenderer({ options, index })" />
      </template>
      <div v-else class="pure-dialog__footer-row">
        <component
          :is="options?.footerPrefixRenderer({ options, index })"
          v-if="options?.footerPrefixRenderer"
        />
        <span class="pure-dialog__footer-buttons">
          <template v-for="(btn, key) in footerButtons(options)" :key="key">
            <el-popconfirm
              v-if="btn.popconfirm"
              v-bind="btn.popconfirm"
              @confirm="
                btn.btnClick({
                  dialog: { options, index },
                  button: { btn, index: key }
                })
              "
            >
              <template #reference>
                <el-button v-bind="btn">{{ btn?.label }}</el-button>
              </template>
            </el-popconfirm>
            <el-button
              v-else
              v-bind="btn"
              :loading="key === 1 && sureBtnMap[index]?.loading"
              @click="
                btn.btnClick({
                  dialog: { options, index },
                  button: { btn, index: key }
                })
              "
            >
              {{ btn?.label }}
            </el-button>
          </template>
        </span>
      </div>
    </template>
  </el-dialog>
</template>

<style scoped>
.pure-dialog__footer-row {
  display: flex;
  width: 100%;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}

.pure-dialog__footer-buttons {
  display: inline-flex;
  flex-shrink: 0;
  white-space: nowrap;
}
</style>
