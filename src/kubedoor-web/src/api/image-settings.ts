import { http } from "@/utils/http";

export interface ImageUpdateRule {
  env: string;
  enabled: boolean;
  start_time: string;
  end_time: string;
  users: string[];
}

export interface ImageRegistry {
  id: string;
  domain: string;
  env: string;
  has_ak: boolean;
  has_sk: boolean;
}

/** 凭据只写不读；已有记录不传或传空字符串表示保留原值。 */
export interface ImageRegistryInput {
  id?: string;
  domain: string;
  env: string;
  ak?: string;
  sk?: string;
}

interface SettingsMeta {
  updated_by: string | null;
  updated_at: string | null;
}

export interface ImageUpdateSettings extends SettingsMeta {
  rules: ImageUpdateRule[];
}

export interface ImageRegistrySettings extends SettingsMeta {
  registries: ImageRegistry[];
}

type Result<T> = { success: boolean; data: T; message?: string };

export const getImageUpdateRules = () =>
  http.request<Result<ImageUpdateSettings>>(
    "get",
    "/api/admin/image-update-rules"
  );

export const saveImageUpdateRules = (rules: ImageUpdateRule[]) =>
  http.request<Result<ImageUpdateSettings>>(
    "put",
    "/api/admin/image-update-rules",
    { data: { rules } }
  );

export const getImageRegistries = () =>
  http.request<Result<ImageRegistrySettings>>(
    "get",
    "/api/admin/image-registries"
  );

export const saveImageRegistries = (registries: ImageRegistryInput[]) =>
  http.request<Result<ImageRegistrySettings>>(
    "put",
    "/api/admin/image-registries",
    { data: { registries } }
  );
