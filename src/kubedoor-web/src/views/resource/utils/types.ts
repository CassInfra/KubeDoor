import type { JvmControlValues, JvmFormValues } from "./jvm";

interface FormItemProps extends JvmControlValues, JvmFormValues {
  env: string;
  namespace: string;
  deployment: string;
  pod_count_manual: string;
  limit_cpu_m: string;
  limit_mem_mb: string;
  max_surge: string | number | null;
  max_unavailable: string | number | null;
  sync_enabled?: boolean;
  sync_env?: string;
  [string: string]: any;
}
interface FormProps {
  formInline: FormItemProps;
  isEdit: boolean;
  namespace: any[];
  envList: any[];
  syncEnvList: string[];
}

export type { FormItemProps, FormProps };
