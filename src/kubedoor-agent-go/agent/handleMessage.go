package agent

import (
	"encoding/json"
	"fmt"
	"kubedoor-agent-go/api"
	"kubedoor-agent-go/config"
	"kubedoor-agent-go/podmgr"
	"kubedoor-agent-go/utils"
	"strings"

	"github.com/gorilla/websocket"
	"go.uber.org/zap"
)

// handleMessage processes incoming messages based on their paths
func handleMessage(conn *websocket.Conn, data config.MessageDataStruct) {
	responseMessage := config.ResponseMessageStruct{
		ResponseMessageType: "response",
		RequestId:           data.RequestID,
	}

	// Log the request
	utils.Logger.Info("Forwarding request",
		zap.String("method", data.Method),
		zap.String("path", data.Path),
		zap.Any("query", data.Query),
		zap.Any("body", data.Body),
	)

	// Handle paths based on their specific API logic
	var response map[string]interface{}
	switch {
	case strings.HasPrefix(data.Path, "/api/pod/task_status"):
		response = handlePodTaskStatus(data)
	case data.Path == "/api/health":
		response = api.HealthCheck()
	case data.Path == "/api/update-image":
		response = handleUpdateImage(data)
	case data.Path == "/api/scale":
		response = handleScale(data)
	case data.Path == "/api/restart":
		response = handleRestart(data)
	case data.Path == "/api/cron":
		response = handleCron(data)
	case data.Path == "/api/admis_switch":
		response = api.AdmisSwitch(data.Query)
	case data.Path == "/api/events":
		// GET /api/events?namespace=xxx
		response = api.GetNamespaceEvents(data.Query)
	case data.Path == "/api/get_dpm_pods":
		// GET /api/get_dpm_pods?namespace=xxx&deployment=xxx
		response = api.GetDeploymentPods(data.Query)
	case data.Path == "/api/nodes":
		// GET /api/nodes
		response = api.GetNodesInfo()
	case data.Path == "/api/balance_node":
		// POST /api/balance_node { env, source, target, top_deployments }
		var bodyMap map[string]interface{}
		if err := parseBody(data.Body, &bodyMap); err != nil {
			response = map[string]interface{}{"message": "invalid body", "success": false}
			break
		}
		response = api.BalanceNode(bodyMap)
	case data.Path == "/api/pod/modify_pod":
		response = podmgr.ModifyPod(data.Query)
	case data.Path == "/api/pod/delete_pod":
		response = podmgr.DeletePodHandler(data.Query)
	case strings.HasPrefix(data.Path, "/api/pod/auto_"):
		response = handlePodAutoTasks(data)
	default:
		utils.Logger.Warn("Unknown path", zap.String("path", data.Path))
	}

	// Send the response back to the client
	sendResponse(conn, responseMessage, response)
}

// sendResponse sends the response message back to the WebSocket client
func sendResponse(conn *websocket.Conn, responseMessage config.ResponseMessageStruct, response map[string]interface{}) {
	responseMessage.Response = response
	if err := conn.WriteJSON(responseMessage); err != nil {
		utils.Logger.Error("Failed to send response", zap.Error(err))
	}
}

// handlePodTaskStatus handles the "/api/pod/task_status" path
func handlePodTaskStatus(data config.MessageDataStruct) map[string]interface{} {
	parts := strings.Split(data.Path, "/")
	if len(parts) < 5 {
		utils.Logger.Error("Invalid task ID in path", zap.String("path", data.Path))
		return nil
	}
	taskID := parts[4] // Extract task ID
	utils.Logger.Info("Task ID:", zap.String("taskID", taskID))
	return podmgr.GetTaskStatus(taskID)
}

// handleUpdateImage handles the "/api/update-image" path
func handleUpdateImage(data config.MessageDataStruct) map[string]interface{} {
	var body config.BodyUpdateImageStruct
	if err := parseBody(data.Body, &body); err != nil {
		return nil
	}
	utils.Logger.Info("Handling BodyUpdateImageStruct:", zap.Any("body", body))
	return api.UpdateImage(body)
}

// handleScale handles the "/api/scale" path
func handleScale(data config.MessageDataStruct) map[string]interface{} {
	var body []config.BodyScaleRestartStruct
	if err := parseBody(data.Body, &body); err != nil {
		return nil
	}
	utils.Logger.Info("Handling BodyScaleStruct:", zap.Any("body", body))
	return api.ScaleDeployment(body, false)
}

// handleRestart handles the "/api/restart" path
func handleRestart(data config.MessageDataStruct) map[string]interface{} {
	var body []config.BodyScaleRestartStruct
	if err := parseBody(data.Body, &body); err != nil {
		return nil
	}
	utils.Logger.Info("Handling BodyRestartStruct:", zap.Any("body", body))
	return api.RestartDeployment(body, false)
}

// handleCron handles the "/api/cron" path
func handleCron(data config.MessageDataStruct) map[string]interface{} {
	var bodyMap map[string]interface{}
	if err := parseBody(data.Body, &bodyMap); err != nil {
		return nil
	}
	utils.Logger.Info("Handling BodyTimeScaleRestartStruct:", zap.Any("body", bodyMap))
	var body config.BodyTimeScaleRestartStruct
	if err := parseBody(data.Body, &body); err != nil {
		return nil
	}
	return api.ScheduleServiceScaleRestart(bodyMap["type"].(string), body)
}

// handlePodAutoTasks handles the auto tasks for pod-related paths
func handlePodAutoTasks(data config.MessageDataStruct) map[string]interface{} {
	taskPaths := []string{
		"/api/pod/auto_dump",
		"/api/pod/auto_jstack",
		"/api/pod/auto_jfr",
		"/api/pod/auto_jvm_mem",
	}
	for _, path := range taskPaths {
		if data.Path == path {
			utils.Logger.Info(fmt.Sprintf("Handling task: %s", path))
			response, _ := sendRequest(path, data.Query)
			return response
		}
	}
	return nil
}

// parseBody unmarshals the body into the provided structure
func parseBody(bodyData []byte, v interface{}) error {
	if err := json.Unmarshal(bodyData, v); err != nil {
		utils.Logger.Error("Failed to unmarshal body", zap.Error(err))
		return err
	}
	return nil
}
