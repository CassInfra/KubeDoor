import { router } from "@/router";

export interface PodShellTarget {
  env: string;
  namespace: string;
  pod: string;
  container?: string;
}

/** 在新标签页打开 Pod 终端(页面见 views/pod-shell),每个标签页一个独立会话 */
export function openPodShell(target: PodShellTarget) {
  const query: Record<string, string> = {
    env: target.env,
    namespace: target.namespace,
    pod: target.pod
  };
  if (target.container) query.container = target.container;
  const { href } = router.resolve({ path: "/pod-shell", query });
  window.open(href, "_blank", "noopener");
}

/** 在新标签页打开节点终端(同一个 pod-shell 页,带 node 参数即为节点模式) */
export function openNodeShell(env: string, node: string) {
  const { href } = router.resolve({ path: "/pod-shell", query: { env, node } });
  window.open(href, "_blank", "noopener");
}
