import Axios, {
  type AxiosInstance,
  type AxiosRequestConfig,
  type CustomParamsSerializer
} from "axios";
import type {
  PureHttpError,
  RequestMethods,
  PureHttpResponse,
  PureHttpRequestConfig
} from "./types.d";
import { stringify } from "qs";
import NProgress from "../progress";
import { useUserStoreHook } from "@/store/modules/user";
import { message } from "../message";
import { emitter } from "../mitt";
import { transformI18n } from "@/plugins/i18n";

// 超时配置
export const TIMEOUT = {
  default: 10000,
  long: 120000 // 长时间操作如日志流、文件上传
};

// 相关配置请参考：www.axios-js.com/zh-cn/docs/#axios-request-config-1
const defaultConfig: AxiosRequestConfig = {
  // 请求超时时间
  timeout: TIMEOUT.default,
  // 登录态由后端的 HttpOnly 会话 Cookie 维持,同源请求浏览器会自动带上
  withCredentials: true,
  headers: {
    Accept: "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "X-Requested-With": "XMLHttpRequest"
  },
  // 数组格式参数序列化（https://github.com/axios/axios/issues/5142）
  paramsSerializer: {
    serialize: stringify as unknown as CustomParamsSerializer
  }
};

/** 当前 hash 路由的完整路径,登录后跳回这里 */
function currentRoute() {
  return window.location.hash.replace(/^#/, "") || "/";
}

let redirecting = false;

/** 会话失效:清理本地状态并跳转登录页(并发的多个 401 只处理一次) */
export function handleUnauthorized() {
  if (redirecting || currentRoute().startsWith("/login")) return;
  redirecting = true;
  message("登录已过期,请重新登录", { type: "warning" });
  useUserStoreHook().resetToLogin(currentRoute());
  setTimeout(() => (redirecting = false), 1000);
}

function errorText(data: any) {
  if (!data) return "";
  if (typeof data === "string") return data;
  return data.message || data.error || data.msg || "";
}

// 不走 axios 的请求(AI 助手的 fetch / SSE)收到 401 时派发这个事件
window.addEventListener("kubedoor:unauthorized", () => handleUnauthorized());

class PureHttp {
  constructor() {
    this.httpInterceptorsRequest();
    this.httpInterceptorsResponse();
  }

  /** 初始化配置对象 */
  private static initConfig: PureHttpRequestConfig = {};

  /** 保存当前`Axios`实例对象 */
  private static axiosInstance: AxiosInstance = Axios.create(defaultConfig);

  /** 请求拦截 */
  private httpInterceptorsRequest(): void {
    PureHttp.axiosInstance.interceptors.request.use(
      async (config: PureHttpRequestConfig): Promise<any> => {
        // 开启进度条动画(后台静默请求不显示)
        if (!config.silent) {
          NProgress.start();
        }
        // 优先判断post/get等方法是否传入回调，否则执行初始化设置等回调
        if (typeof config.beforeRequestCallback === "function") {
          config.beforeRequestCallback(config);
          return config;
        }
        if (PureHttp.initConfig.beforeRequestCallback) {
          PureHttp.initConfig.beforeRequestCallback(config);
          return config;
        }
        return config;
      },
      error => {
        return Promise.reject(error);
      }
    );
  }

  /** 响应拦截 */
  private httpInterceptorsResponse(): void {
    const instance = PureHttp.axiosInstance;
    instance.interceptors.response.use(
      (response: PureHttpResponse) => {
        const $config = response.config;
        // 关闭进度条动画
        NProgress.done();
        // 优先判断post/get等方法是否传入回调，否则执行初始化设置等回调
        if (typeof $config.beforeResponseCallback === "function") {
          $config.beforeResponseCallback(response);
          return response.data;
        }
        if (PureHttp.initConfig.beforeResponseCallback) {
          PureHttp.initConfig.beforeResponseCallback(response);
          return response.data;
        }
        return response.data;
      },
      (error: PureHttpError) => {
        const config = (error.config ?? {}) as PureHttpRequestConfig;
        const status = error.response?.status;
        const data = error.response?.data as any;
        NProgress.done();
        error.isCancelRequest = Axios.isCancel(error);
        // 会话失效(包括后台静默请求):统一回登录页
        if (status === 401 && !config.skipAuthRedirect) {
          handleUnauthorized();
          return Promise.reject(error);
        }
        // 必须先修改初始口令
        if (status === 403 && data?.code === "password_change_required") {
          emitter.emit("passwordChangeRequired");
          return Promise.reject(error);
        }
        // 后台静默请求出错不打扰用户,交给调用方处理
        if (config.silent) {
          return Promise.reject(error);
        }
        if (!error.response) {
          if (!error.isCancelRequest) {
            message(transformI18n("message.pureInterfaceError"), {
              type: "error"
            });
          }
          return Promise.reject(error);
        }
        switch (status) {
          case 404:
            message(errorText(data) || "404: 未找到资源", { type: "error" });
            break;
          case 401:
            message(errorText(data) || "401: 未登录", { type: "error" });
            break;
          case 403:
            message(errorText(data) || "403: 权限不足", { type: "error" });
            break;
          default:
            message(
              `${status}: ` +
                (errorText(data) ||
                  transformI18n("message.pureInterfaceError")),
              {
                type: "error"
              }
            );
            break;
        }
        // 所有的响应异常 区分来源为取消请求/非取消请求
        return Promise.reject(error);
      }
    );
  }

  /** 通用请求工具函数 */
  public request<T>(
    method: RequestMethods,
    url: string,
    param?: AxiosRequestConfig,
    axiosConfig?: PureHttpRequestConfig
  ): Promise<T> {
    const config = {
      method,
      url,
      ...param,
      ...axiosConfig
    } as PureHttpRequestConfig;

    // 单独处理自定义请求/响应回调
    return new Promise((resolve, reject) => {
      PureHttp.axiosInstance
        .request(config)
        .then((response: undefined) => {
          resolve(response);
        })
        .catch(error => {
          reject(error);
        });
    });
  }

  /** 单独抽离的`post`工具函数 */
  public post<T, P>(
    url: string,
    params?: AxiosRequestConfig<P>,
    config?: PureHttpRequestConfig
  ): Promise<T> {
    return this.request<T>("post", url, params, config);
  }

  /** 单独抽离的`get`工具函数 */
  public get<T, P>(
    url: string,
    params?: AxiosRequestConfig<P>,
    config?: PureHttpRequestConfig
  ): Promise<T> {
    return this.request<T>("get", url, params, config);
  }
}

export const http = new PureHttp();
