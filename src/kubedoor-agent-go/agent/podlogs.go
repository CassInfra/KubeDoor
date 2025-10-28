package agent

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"io"
	"strings"
	"sync"
	"time"

	"kubedoor-agent-go/config"
	"kubedoor-agent-go/utils"

	"github.com/gorilla/websocket"
	"go.uber.org/zap"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// 并发写WS需要串行化，避免并发写导致竞态或分片
var wsWriteMu sync.Mutex

// 每个日志连接的取消控制
var (
	podLogCancelsMu sync.Mutex
	podLogCancels   = map[string]context.CancelFunc{}
)

type startPodLogsMsg struct {
	Type         string `json:"type"`
	ConnectionID string `json:"connection_id"`
	Namespace    string `json:"namespace"`
	PodName      string `json:"pod_name"`
	Container    string `json:"container,omitempty"`
}

type stopPodLogsMsg struct {
	Type         string `json:"type"`
	ConnectionID string `json:"connection_id"`
}

type podLogsStatus struct {
	Type         string `json:"type"`
	ConnectionID string `json:"connection_id"`
	Status       string `json:"status,omitempty"` // connected/disconnected
	Error        string `json:"error,omitempty"`
}

func handleStartPodLogs(message []byte) {
	var req startPodLogsMsg
	if err := json.Unmarshal(message, &req); err != nil {
		utils.Logger.Error("Failed to unmarshal start_pod_logs", zap.Error(err))
		return
	}

	if req.ConnectionID == "" || req.Namespace == "" || req.PodName == "" {
		utils.Logger.Error("start_pod_logs missing required fields",
			zap.String("connection_id", req.ConnectionID),
			zap.String("namespace", req.Namespace),
			zap.String("pod", req.PodName),
		)
		return
	}
	if config.WebSocketConcent == nil {
		utils.Logger.Error("WebSocket connection is nil for start_pod_logs")
		return
	}

	// 若已存在同 connection_id，先取消旧的
	podLogCancelsMu.Lock()
	if old, ok := podLogCancels[req.ConnectionID]; ok {
		old()
		delete(podLogCancels, req.ConnectionID)
	}
	// 派生可取消 context
	ctx, cancel := context.WithCancel(context.Background())
	podLogCancels[req.ConnectionID] = cancel
	podLogCancelsMu.Unlock()

	// 先下发 connected 状态
	_ = wsWriteJSON(config.WebSocketConcent, podLogsStatus{Type: "pod_logs", ConnectionID: req.ConnectionID, Status: "connected"})

	go streamPodLogsLoop(ctx, config.WebSocketConcent, req)
}

func handleStopPodLogs(message []byte) {
	var req stopPodLogsMsg
	if err := json.Unmarshal(message, &req); err != nil {
		utils.Logger.Error("Failed to unmarshal stop_pod_logs", zap.Error(err))
		return
	}
	if req.ConnectionID == "" {
		utils.Logger.Warn("stop_pod_logs missing connection_id")
		return
	}
	podLogCancelsMu.Lock()
	if cancel, ok := podLogCancels[req.ConnectionID]; ok {
		cancel()
		delete(podLogCancels, req.ConnectionID)
	}
	podLogCancelsMu.Unlock()
}

func streamPodLogsLoop(ctx context.Context, ws *websocket.Conn, req startPodLogsMsg) {
	// 构造日志请求
	opts := &corev1.PodLogOptions{
		Follow:     true,
		TailLines:  int64Ptr(100),
		Timestamps: false,
	}
	if strings.TrimSpace(req.Container) != "" {
		opts.Container = req.Container
	} else {
		// 自动选择一个业务容器（跳过常见 sidecar）
		if auto, ok := autoSelectContainer(ctx, req.Namespace, req.PodName); ok {
			opts.Container = auto
			utils.Logger.Info("auto selected container for logs",
				zap.String("ns", req.Namespace), zap.String("pod", req.PodName), zap.String("container", auto))
		}
	}

	// 使用 client-go 打开日志流
	reqStream := config.KubeClient.CoreV1().Pods(req.Namespace).GetLogs(req.PodName, opts)

	// 用一个带超时的子 ctx 打开流，避免长时间阻塞
	openCtx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()

	stream, err := reqStream.Stream(openCtx)
	if err != nil {
		utils.Logger.Error("Pod log stream open failed",
			zap.String("ns", req.Namespace), zap.String("pod", req.PodName), zap.Error(err))
		_ = wsWriteJSON(ws, podLogsStatus{Type: "pod_logs", ConnectionID: req.ConnectionID, Error: err.Error()})
		cleanupConnection(req.ConnectionID)
		return
	}
	defer stream.Close()

	reader := bufio.NewReader(stream)

	for {
		select {
		case <-ctx.Done():
			_ = wsWriteJSON(ws, podLogsStatus{Type: "pod_logs", ConnectionID: req.ConnectionID, Status: "disconnected"})
			cleanupConnection(req.ConnectionID)
			return
		default:
			line, readErr := reader.ReadString('\n')
			if len(line) > 0 {
				trimmed := strings.TrimRight(line, "\r\n")
				if strings.TrimSpace(trimmed) != "" {
					_ = wsWriteText(ws, trimmed)
				}
			}
			if readErr != nil {
				if errors.Is(readErr, io.EOF) {
					// 正常结束
					_ = wsWriteJSON(ws, podLogsStatus{Type: "pod_logs", ConnectionID: req.ConnectionID, Status: "disconnected"})
				} else {
					_ = wsWriteJSON(ws, podLogsStatus{Type: "pod_logs", ConnectionID: req.ConnectionID, Error: readErr.Error()})
				}
				cleanupConnection(req.ConnectionID)
				return
			}
		}
	}
}

// autoSelectContainer 读取 Pod 并选择一个最可能的业务容器名
// 优先规则：第一个非 sidecar（名称前缀为 istio-、linkerd-、kube-rbac-proxy 等）
// 退化：若全是 sidecar 或无法匹配，则选择第一个容器
func autoSelectContainer(ctx context.Context, namespace, pod string) (string, bool) {
	p, err := config.KubeClient.CoreV1().Pods(namespace).Get(ctx, pod, metav1.GetOptions{})
	if err != nil || len(p.Spec.Containers) == 0 {
		return "", false
	}
	sidecarPrefixes := []string{"istio-", "linkerd-", "kube-rbac-proxy", "istio-proxy", "istio-init"}
	isSidecar := func(name string) bool {
		for _, pre := range sidecarPrefixes {
			if strings.HasPrefix(name, pre) {
				return true
			}
		}
		return false
	}
	for _, c := range p.Spec.Containers {
		if !isSidecar(c.Name) {
			return c.Name, true
		}
	}
	// 全是 sidecar，回退第一个
	return p.Spec.Containers[0].Name, true
}

func cleanupConnection(connectionID string) {
	podLogCancelsMu.Lock()
	delete(podLogCancels, connectionID)
	podLogCancelsMu.Unlock()
}

func wsWriteJSON(ws *websocket.Conn, v interface{}) error {
	wsWriteMu.Lock()
	defer wsWriteMu.Unlock()
	return ws.WriteJSON(v)
}

func wsWriteText(ws *websocket.Conn, text string) error {
	wsWriteMu.Lock()
	defer wsWriteMu.Unlock()
	return ws.WriteMessage(websocket.TextMessage, []byte(text))
}

func int64Ptr(v int64) *int64 { return &v }
