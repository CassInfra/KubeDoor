package agent

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"syscall"
	"time"

	"kubedoor-agent-go/config"
	"kubedoor-agent-go/utils"

	"github.com/gorilla/websocket"
	"go.uber.org/zap"
)

func StartAgent() {
	utils.InitLogger() // Initialize logger

	version := utils.GetVersion()
	kubeDoorMaster := os.Getenv("KUBEDOOR_MASTER")
	promK8sTagValue := os.Getenv("PROM_K8S_TAG_VALUE")

	if kubeDoorMaster == "" {
		utils.Logger.Fatal("KUBEDOOR_MASTER environment variable is not set")
	}

	// Parse the URL once and reuse
	wsURL, headers := buildWebSocketURL(kubeDoorMaster, version, promK8sTagValue)

	// Set up signal handling for graceful shutdown
	stopChan := make(chan os.Signal, 1)
	signal.Notify(stopChan, syscall.SIGINT, syscall.SIGTERM)

	// WebSocket connection loop
	for {
		select {
		case <-stopChan:
			log.Println("Received termination signal. Shutting down...")
			return
		default:
			// Attempt WebSocket connection with retry logic
			conn, err := connectWithRetry(wsURL, headers)
			if err != nil {
				log.Printf("Failed to connect: %v. Retrying in 5 seconds...", err)
				time.Sleep(5 * time.Second)
				continue
			}
			defer conn.Close()

			log.Println("Connected to server")
			config.WebSocketConcent = conn // Save connection

			// Start heartbeat goroutine
			go sendHeartbeat(conn)

			// Process messages
			if err := processMessages(conn); err != nil {
				utils.Logger.Error("Error processing WebSocket messages", zap.Error(err))
				log.Println("Processing stopped, reconnecting...")
				time.Sleep(5 * time.Second)
			}
		}
	}
}

// buildWebSocketURL constructs the WebSocket URL and headers for authentication
func buildWebSocketURL(kubeDoorMaster, version, promK8sTagValue string) (string, http.Header) {
	u, err := url.Parse(kubeDoorMaster)
	if err != nil {
		utils.Logger.Fatal("Failed to parse websocket URL", zap.Error(err))
	}

	user := u.User.Username()
	password, _ := u.User.Password()
	encodedUser := url.QueryEscape(user)
	encodedPassword := url.QueryEscape(password)

	headers := http.Header{}
	wsURL := fmt.Sprintf("%s://%s/ws?env=%s&ver=%s", u.Scheme, u.Host, promK8sTagValue, version)
	if encodedUser != "" {
		auth := "Basic " + base64.StdEncoding.EncodeToString([]byte(encodedUser+":"+encodedPassword))
		headers.Add("Authorization", auth)
	}

	return wsURL, headers
}

// connectWithRetry tries to establish a WebSocket connection with retries
func connectWithRetry(wsURL string, headers http.Header) (*websocket.Conn, error) {
	conn, resp, err := websocket.DefaultDialer.Dial(wsURL, headers)
	if err != nil {
		// Handle non-409 errors and log them
		if resp != nil && resp.StatusCode != http.StatusConflict {
			utils.Logger.Error("WebSocket connection failed", zap.Int("status_code", resp.StatusCode), zap.Error(err))
			return nil, err
		}
		if resp != nil {
			utils.Logger.Warn("Agent already registered", zap.Int("status_code", resp.StatusCode))
		} else {
			utils.Logger.Error("WebSocket connection failed", zap.Error(err))
		}
		return nil, err
	}
	return conn, nil
}

// sendHeartbeat sends heartbeat messages at regular intervals
func sendHeartbeat(conn *websocket.Conn) {
	heartbeatTicker := time.NewTicker(4 * time.Second)
	defer heartbeatTicker.Stop()
	for range heartbeatTicker.C {
		err := conn.WriteJSON(map[string]string{"type": "heartbeat"})
		if err != nil {
			utils.Logger.Error("Failed to send heartbeat", zap.Error(err))
			log.Println("Heartbeat failed:", err)
			return
		}
		utils.Logger.Debug("Heartbeat sent successfully")
	}
}

// processMessages reads and handles incoming messages from WebSocket
func processMessages(conn *websocket.Conn) error {
	for {
		_, message, err := conn.ReadMessage()
		if err != nil {
			utils.Logger.Error("Failed to read message from websocket", zap.Error(err))
			log.Println("Read failed:", err)
			return err
		}

		utils.Logger.Debug("Received message:", zap.String("message", string(message)))
		var data config.MessageDataStruct
		if err := json.Unmarshal(message, &data); err != nil {
			utils.Logger.Error("Failed to unmarshal message", zap.Error(err), zap.String("message", string(message)))
			log.Println("Failed to unmarshal message:", err)
			continue
		}

		switch data.MessageType {
		case "request":
			go handleMessage(conn, data)
		case "admis":
			handleAdmisRequest(message)
		case "start_pod_logs":
			handleStartPodLogs(message)
		case "stop_pod_logs":
			handleStopPodLogs(message)
		default:
			utils.Logger.Warn("Unknown message type", zap.String("type", string(data.MessageType)), zap.String("message", string(message)))
		}
	}
}

// handleAdmisRequest processes the "admis" message type
func handleAdmisRequest(message []byte) {
	var admisRequestResult config.AdmisRequestResultObj
	if err := json.Unmarshal(message, &admisRequestResult); err != nil {
		utils.Logger.Error("Failed to unmarshal admisRequestResult", zap.Error(err), zap.String("requestId", admisRequestResult.RequestId))
		return
	}

	utils.Logger.Info("Processing admisRequestResult", zap.String("requestId", admisRequestResult.RequestId))
	requestRes, _ := config.RequestFutures.Get(admisRequestResult.RequestId)
	requestRes <- admisRequestResult.DeployRes
	config.RequestFutures.Set(admisRequestResult.RequestId, requestRes)
}
