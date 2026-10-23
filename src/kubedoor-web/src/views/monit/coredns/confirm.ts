import { h } from "vue";
import { ElMessageBox } from "element-plus";
import DiffView from "./components/DiffView.vue";

export interface FileDiff {
  /** 文件名(data 里的键);不传时不显示文件名 */
  name?: string;
  before: string;
  after: string;
}

/** 保存 / 删除前的二次确认:展示每个文件改了哪些行 */
export async function confirmChange(options: {
  env: string;
  title: string;
  diffs: FileDiff[];
  notes?: string[];
  confirmText?: string;
  danger?: boolean;
}): Promise<boolean> {
  const notes = options.notes ?? [];
  // 改了多个文件时每个对比框矮一些,确认框不超出屏幕
  const maxHeight = options.diffs.length > 1 ? "28vh" : "46vh";
  const content = h("div", { class: "coredns-confirm__content" }, [
    h("p", null, [
      "将保存到集群 ",
      h("b", null, options.env),
      " 的 kube-system/coredns,CoreDNS 检测到配置变化后自动生效。"
    ]),
    notes.length
      ? h(
          "ul",
          { class: "coredns-confirm__notes" },
          notes.map(note => h("li", null, note))
        )
      : null,
    ...options.diffs.map(diff =>
      h("div", { class: "coredns-confirm__file" }, [
        diff.name
          ? h("div", { class: "coredns-confirm__file-name" }, diff.name)
          : null,
        h(DiffView, { before: diff.before, after: diff.after, maxHeight })
      ])
    )
  ]);
  try {
    await ElMessageBox.confirm(content, options.title, {
      type: "warning",
      confirmButtonText: options.confirmText || "保存",
      cancelButtonText: "取消",
      confirmButtonClass: options.danger ? "el-button--danger" : "",
      customClass: "coredns-confirm"
    });
    return true;
  } catch {
    return false;
  }
}

export function alertErrors(title: string, errors: string[]) {
  ElMessageBox.alert(
    h(
      "div",
      null,
      errors.map(error => h("div", null, error))
    ),
    title,
    { type: "error" }
  );
}
