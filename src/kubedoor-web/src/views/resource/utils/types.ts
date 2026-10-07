import type { JvmControlValues, JvmFormValues } from "./jvm";

interface FormItemProps extends JvmControlValues, JvmFormValues {
  env: string;
  namespace: string;
  deployment: string;
  pod_count_manual: string;
  limit_cpu_m: string;
  limit_mem_mb: string;
  [string: string]: any;
}
interface FormProps {
  formInline: FormItemProps;
  isEdit: boolean;
  namespace: any[];
  envList: any[];
}

export type { FormItemProps, FormProps };
