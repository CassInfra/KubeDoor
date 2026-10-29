import { usePermission } from "@/utils/usePermission";

type IstioCluster = { k8s_name: string; can_istio?: boolean };
type IstioGroupLike = { can_manage?: boolean; clusters: IstioCluster[] };

/**
 * Istio 修改权限(「权限与审批」里的「Istio 修改」):优先用后端返回的 can_manage / can_istio,
 * 没有时按当前账号的 operations 自己算。只用于置灰和隐藏入口,后端仍会校验。
 */
export function useIstioPermission() {
  const { can, canGlobal } = usePermission();
  /** 集群级修改(下发、回退、删除集群里的 VirtualService、刷新现状):只看这个集群 */
  const clusterCan = (cluster: IstioCluster | null | undefined) =>
    !!cluster && (cluster.can_istio ?? can("network.istio", cluster.k8s_name));
  /** 组级修改(组、组标准、忽略 / 纳入、纳管、同步):组内每个集群都要有权限,空组按全局角色 */
  const groupCan = (group: IstioGroupLike | null | undefined) => {
    if (!group) return false;
    if (group.can_manage !== undefined) return group.can_manage;
    return group.clusters.length
      ? group.clusters.every(clusterCan)
      : canGlobal("network.istio");
  };
  return { clusterCan, groupCan };
}
