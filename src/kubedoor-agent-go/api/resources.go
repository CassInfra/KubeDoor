package api

import (
	"context"
	"fmt"
	"regexp"
	"sort"
	"strings"

	"kubedoor-agent-go/config"
	"kubedoor-agent-go/utils"

	"go.uber.org/zap"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
)

// GetNamespaceEvents 对齐 python: /api/events?namespace=ns
func GetNamespaceEvents(query map[string]interface{}) map[string]interface{} {
	ns, _ := query["namespace"].(string)
	ctx := context.Background()

	var (
		events *EventListLite
		err    error
	)
	if ns != "" {
		events, err = listEventsForNamespace(ctx, ns)
	} else {
		events, err = listEventsAll(ctx)
	}
	if err != nil {
		utils.Logger.Error("GetNamespaceEvents failed", zap.Error(err))
		return map[string]interface{}{"message": fmt.Sprintf("获取事件失败: %v", err), "success": false}
	}
	return map[string]interface{}{"events": events.Items, "success": true}
}

type EventLite struct {
	Name           string                 `json:"name"`
	Namespace      string                 `json:"namespace"`
	Type           string                 `json:"type"`
	Reason         string                 `json:"reason"`
	Message        string                 `json:"message"`
	InvolvedObject map[string]string      `json:"involved_object"`
	Count          int32                  `json:"count"`
	FirstTimestamp string                 `json:"first_timestamp"`
	LastTimestamp  string                 `json:"last_timestamp"`
	Source         map[string]interface{} `json:"source"`
}

type EventListLite struct{ Items []EventLite }

func listEventsForNamespace(ctx context.Context, ns string) (*EventListLite, error) {
	el, err := config.KubeClient.CoreV1().Events(ns).List(ctx, metav1.ListOptions{})
	if err != nil {
		return nil, err
	}
	return toEventListLite(el), nil
}

func listEventsAll(ctx context.Context) (*EventListLite, error) {
	el, err := config.KubeClient.CoreV1().Events("").List(ctx, metav1.ListOptions{})
	if err != nil {
		return nil, err
	}
	return toEventListLite(el), nil
}

func toEventListLite(el *corev1.EventList) *EventListLite {
	return &EventListLite{Items: mapEvents(el)}
}

func mapEvents(el *corev1.EventList) []EventLite {
	out := make([]EventLite, 0, len(el.Items))
	for _, e := range el.Items {
		var src map[string]interface{}
		if e.Source.Component != "" || e.Source.Host != "" {
			src = map[string]interface{}{"component": e.Source.Component, "host": e.Source.Host}
		}
		var first, last string
		if !e.FirstTimestamp.IsZero() {
			first = e.FirstTimestamp.Time.Format(timeLayout)
		}
		if !e.LastTimestamp.IsZero() {
			last = e.LastTimestamp.Time.Format(timeLayout)
		}
		out = append(out, EventLite{
			Name:      e.ObjectMeta.Name,
			Namespace: e.ObjectMeta.Namespace,
			Type:      e.Type,
			Reason:    e.Reason,
			Message:   e.Message,
			InvolvedObject: map[string]string{
				"kind": e.InvolvedObject.Kind, "name": e.InvolvedObject.Name, "namespace": e.InvolvedObject.Namespace,
			},
			Count:          e.Count,
			FirstTimestamp: first,
			LastTimestamp:  last,
			Source:         src,
		})
	}
	return out
}

const timeLayout = "2006-01-02T15:04:05Z07:00"

// GetNodesInfo 对齐 python: /api/nodes（去掉 metrics 聚合，先返回 0）
func GetNodesInfo() map[string]interface{} {
	ctx := context.Background()
	nodes, err := config.KubeClient.CoreV1().Nodes().List(ctx, metav1.ListOptions{})
	if err != nil {
		return map[string]interface{}{"message": fmt.Sprintf("获取节点信息失败: %v", err), "success": false}
	}
	pods, err := config.KubeClient.CoreV1().Pods("").List(ctx, metav1.ListOptions{})
	if err != nil {
		return map[string]interface{}{"message": fmt.Sprintf("获取Pod信息失败: %v", err), "success": false}
	}
	var list []map[string]interface{}
	for _, n := range nodes.Items {
		nodeName := n.Name
		nodeIP := ""
		for _, a := range n.Status.Addresses {
			if a.Type == "InternalIP" {
				nodeIP = a.Address
				break
			}
		}
		conditions := []string{}
		for _, c := range n.Status.Conditions {
			if c.Status == "True" {
				conditions = append(conditions, string(c.Type))
			}
		}
		currentPods := 0
		for _, p := range pods.Items {
			if p.Spec.NodeName == nodeName {
				currentPods++
			}
		}
		list = append(list, map[string]interface{}{
			"name":               nodeName,
			"ip":                 nodeIP,
			"os_image":           fmt.Sprintf("%s %s", n.Status.NodeInfo.OSImage, n.Status.NodeInfo.KernelVersion),
			"container_runtime":  n.Status.NodeInfo.ContainerRuntimeVersion,
			"kubelet_version":    n.Status.NodeInfo.KubeletVersion,
			"conditions":         strings.Join(conditions, ", "),
			"allocatable_cpu":    0, // 先置 0，后续接 metrics
			"current_cpu":        0,
			"allocatable_memory": 0,
			"current_memory":     0,
			"max_pods":           0,
			"current_pods":       currentPods,
		})
	}
	return map[string]interface{}{"nodes": list, "success": true}
}

// GetDeploymentPods 对齐 python: /api/get_dpm_pods?namespace=ns&deployment=dep
func GetDeploymentPods(query map[string]interface{}) map[string]interface{} {
	ns, _ := query["namespace"].(string)
	dep, _ := query["deployment"].(string)
	if ns == "" || dep == "" {
		return map[string]interface{}{"message": "缺少必要参数", "success": false}
	}
	ctx := context.Background()
	dpm, err := config.KubeClient.AppsV1().Deployments(ns).Get(ctx, dep, metav1.GetOptions{})
	if err != nil {
		return map[string]interface{}{"message": fmt.Sprintf("获取Deployment失败: %v", err), "success": false}
	}
	// label selector
	selector := dpm.Spec.Selector.MatchLabels
	var parts []string
	for k, v := range selector {
		parts = append(parts, fmt.Sprintf("%s=%s", k, v))
	}
	sort.Strings(parts)
	labelSelector := strings.Join(parts, ",")

	podsByLabel, _ := config.KubeClient.CoreV1().Pods(ns).List(ctx, metav1.ListOptions{LabelSelector: labelSelector})
	allPods, _ := config.KubeClient.CoreV1().Pods(ns).List(ctx, metav1.ListOptions{})

	// 名称匹配兜底：deploymentName-[a-z0-9]+-[a-z0-9]+
	pattern := regexp.MustCompile(fmt.Sprintf("^%s-[a-z0-9]+-[a-z0-9]+$", regexp.QuoteMeta(dep)))
	matchPods := []string{}
	for _, p := range allPods.Items {
		if pattern.MatchString(p.Name) {
			matchPods = append(matchPods, p.Name)
		}
	}

	// 合并去重
	dedup := map[string]bool{}
	result := []map[string]interface{}{}
	for _, p := range podsByLabel.Items {
		dedup[p.Name] = true
		result = append(result, buildPodLite(ns, p.Name, &p))
	}
	for _, name := range matchPods {
		if dedup[name] {
			continue
		}
		// 查询单个 Pod 获取细节
		if p, err := config.KubeClient.CoreV1().Pods(ns).Get(ctx, name, metav1.GetOptions{}); err == nil {
			result = append(result, buildPodLite(ns, name, p))
		}
	}
	return map[string]interface{}{"success": true, "pods": result}
}

func buildPodLite(ns, name string, p *corev1.Pod) map[string]interface{} {
	ready := false
	if p.Status.ContainerStatuses != nil {
		ready = true
		for _, cs := range p.Status.ContainerStatuses {
			if !cs.Ready {
				ready = false
				break
			}
		}
	}
	restartCount := 0
	for _, cs := range p.Status.ContainerStatuses {
		restartCount += int(cs.RestartCount)
	}
	mainImage := ""
	if len(p.Spec.Containers) > 0 {
		mainImage = p.Spec.Containers[0].Image
	}
	return map[string]interface{}{
		"name":             p.Name,
		"status":           string(p.Status.Phase),
		"ready":            ready,
		"pod_ip":           p.Status.PodIP,
		"cpu":              "0m",
		"memory":           "0MB",
		"created_at":       p.CreationTimestamp.Time.Format("2006-01-02 15:04:05"),
		"app_label":        p.Labels["app"],
		"image":            mainImage,
		"node_name":        p.Spec.NodeName,
		"restart_count":    restartCount,
		"restart_reason":   "",
		"exception_reason": "",
	}
}

// BalanceNode 对齐 python: /api/balance_node
// body: { env, source: sourceNode, target: targetNode, top_deployments: [{namespace, deployment}] }
func BalanceNode(body map[string]interface{}) map[string]interface{} {
	source, _ := body["source"].(string)
	target, _ := body["target"].(string)
	list, _ := body["top_deployments"].([]interface{})
	if source == "" || target == "" || len(list) == 0 {
		return map[string]interface{}{"message": "缺少必要参数", "success": false}
	}
	ctx := context.Background()
	results := []map[string]interface{}{}
	for _, it := range list {
		item, _ := it.(map[string]interface{})
		ns, _ := item["namespace"].(string)
		dep, _ := item["deployment"].(string)
		if ns == "" || dep == "" {
			continue
		}
		labelKey := fmt.Sprintf("%s.%s", ns, dep)
		// 源节点删除标签
		if err := patchNodeLabel(ctx, source, labelKey, nil); err != nil {
			results = append(results, map[string]interface{}{"namespace": ns, "deployment": dep, "status": "failed", "error": err.Error()})
			continue
		}
		// 目标节点添加标签
		val := NodeLabelValue
		if err := patchNodeLabel(ctx, target, labelKey, &val); err != nil {
			results = append(results, map[string]interface{}{"namespace": ns, "deployment": dep, "status": "failed", "error": err.Error()})
			continue
		}
		// 删除源节点上的该 deployment 的一个 pod
		deleted := deleteOnePodOnNode(ctx, ns, dep, source)
		results = append(results, map[string]interface{}{"namespace": ns, "deployment": dep, "status": "success", "deleted_pods": deleted})
	}
	return map[string]interface{}{"message": fmt.Sprintf("节点均衡操作完成: %s -> %s", source, target), "success": true, "results": results}
}

const NodeLabelValue = "on"

func patchNodeLabel(ctx context.Context, nodeName, key string, val *string) error {
	var patch string
	if val == nil {
		patch = fmt.Sprintf(`{"metadata":{"labels":{"%s":null}}}`, key)
	} else {
		patch = fmt.Sprintf(`{"metadata":{"labels":{"%s":"%s"}}}`, key, *val)
	}
	_, err := config.KubeClient.CoreV1().Nodes().Patch(ctx, nodeName, types.MergePatchType, []byte(patch), metav1.PatchOptions{})
	return err
}

func deleteOnePodOnNode(ctx context.Context, ns, deploymentName, nodeName string) []string {
	// 通过 deployment selector 过滤 + 字段选择器 nodeName
	dpm, err := config.KubeClient.AppsV1().Deployments(ns).Get(ctx, deploymentName, metav1.GetOptions{})
	if err != nil {
		return nil
	}
	sel := dpm.Spec.Selector.MatchLabels
	var parts []string
	for k, v := range sel {
		parts = append(parts, fmt.Sprintf("%s=%s", k, v))
	}
	sort.Strings(parts)
	ls := strings.Join(parts, ",")
	pods, err := config.KubeClient.CoreV1().Pods(ns).List(ctx, metav1.ListOptions{LabelSelector: ls, FieldSelector: fmt.Sprintf("spec.nodeName=%s", nodeName)})
	if err != nil || len(pods.Items) == 0 {
		return nil
	}
	// 删除一个
	_ = config.KubeClient.CoreV1().Pods(ns).Delete(ctx, pods.Items[0].Name, metav1.DeleteOptions{})
	return []string{pods.Items[0].Name}
}
