import { reactive } from "vue";
import type { FormRules } from "element-plus";
import { transformI18n } from "@/plugins/i18n";
import {
  JVM_PARAMETERS,
  jvmUnitToBytes,
  validateJvmHeapSizes,
  type JvmFormValues
} from "./jvm";

export function createJvmFormRules(form: JvmFormValues): FormRules {
  return Object.fromEntries(
    JVM_PARAMETERS.map(({ inputKey, unit }) => [
      inputKey,
      [
        {
          trigger: ["blur", "change"],
          validator: (_rule, value, callback) => {
            if (value === undefined) {
              callback();
              return;
            }
            try {
              jvmUnitToBytes(value, unit);
              if (inputKey === "jvm_xms_mib" || inputKey === "jvm_xmx_mib") {
                validateJvmHeapSizes(form);
              }
              callback();
            } catch (error) {
              callback(error);
            }
          }
        }
      ]
    ])
  );
}

/**
 * 指定Pod：0 表示暂不启动 Pod，-1 表示按 AI推荐 / 当日Pod 管控。
 * 手动新增或还没采集过高峰期数据的服务当日Pod 为 0，-1 会让准入把副本数改成 0，
 * 所以只有采集过（有 update）或已有 AI推荐的服务才能设为 -1；与后端 db_api 的校验一致。
 */
export function createPodCountManualRules(
  form: Record<string, any>,
  isEdit: boolean
): FormRules {
  return {
    pod_count_manual: [
      {
        required: true,
        message: transformI18n("resource.rules.podCountManual"),
        trigger: "blur"
      },
      {
        trigger: "blur",
        validator: (_rule, value, callback) => {
          const count = Number(value);
          if (!Number.isInteger(count) || count < -1) {
            callback(
              new Error(transformI18n("resource.rules.greaterThanZero"))
            );
            return;
          }
          const hasFallback =
            isEdit && (!!form.update || Number(form.pod_count_ai) >= 0);
          if (count === -1 && !hasFallback) {
            callback(
              new Error(
                transformI18n("resource.rules.podCountManualNoFallback")
              )
            );
            return;
          }
          callback();
        }
      }
    ]
  };
}

/** 自定义表单规则校验 */
export const formRules = reactive(<FormRules>{
  namespace: [
    {
      required: true,
      message: transformI18n("resource.rules.namespace"),
      trigger: "blur"
    }
  ],
  deployment: [
    {
      required: true,
      message: transformI18n("resource.rules.deployment"),
      trigger: "blur"
    }
  ],
  request_cpu_m: [
    {
      required: true,
      message: transformI18n("resource.rules.requestCpuM"),
      trigger: "blur"
    },
    {
      type: "number",
      min: -1,
      message: transformI18n("resource.rules.greaterThanZero"),
      trigger: "blur",
      transform: value => Number(value)
    }
  ],
  request_mem_mb: [
    {
      required: true,
      message: transformI18n("resource.rules.requestMemMb"),
      trigger: "blur"
    },
    {
      type: "number",
      min: -1,
      message: transformI18n("resource.rules.greaterThanZero"),
      trigger: "blur",
      transform: value => Number(value)
    }
  ],
  limit_cpu_m: [
    {
      required: true,
      message: transformI18n("resource.rules.limitCpuM"),
      trigger: "blur"
    },
    {
      type: "number",
      min: -1,
      message: transformI18n("resource.rules.greaterThanZero"),
      trigger: "blur",
      transform: value => Number(value)
    }
  ],
  limit_mem_mb: [
    {
      required: true,
      message: transformI18n("resource.rules.limitMemMb"),
      trigger: "blur"
    },
    {
      type: "number",
      min: -1,
      message: transformI18n("resource.rules.greaterThanZero"),
      trigger: "blur",
      transform: value => Number(value)
    }
  ]
});
