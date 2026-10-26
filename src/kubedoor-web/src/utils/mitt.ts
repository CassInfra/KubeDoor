import type { Emitter } from "mitt";
import mitt from "mitt";

/** 全局公共事件需要在此处添加类型 */
type Events = {
  openPanel: string;
  tagViewsChange: string;
  tagViewsShowModel: string;
  logoChange: boolean;
  changLayoutRoute: string;
  /** 后端要求先修改初始口令(403 password_change_required) */
  passwordChangeRequired: undefined;
  /** 打开「修改口令」弹窗 */
  openChangePassword: undefined;
  /** 打开「绑定企业微信」弹窗 */
  openWecomBind: undefined;
};

export const emitter: Emitter<Events> = mitt<Events>();
