package main

import (
	"encoding/json"
	"log"
	"os"
	"os/exec"
	"strings"
	"time"

	"github.com/gorilla/websocket"
)

// startHeartbeat 周期性向 relay 上报存活（不影响推流主链路）。
// hbURL 为空则不发心跳（退化：relay 无自动探活，由用户看画面手动重启投屏）。
func startHeartbeat(hbURL string, stop <-chan struct{}) {
	if hbURL == "" {
		return
	}
	go func() {
		backoff := time.Second
		for {
			select {
			case <-stop:
				return
			default:
			}
			if err := hbLoop(hbURL, stop); err != nil {
				log.Printf("[hb] 心跳连接结束: %v", err)
			}
			select {
			case <-stop:
				return
			case <-time.After(backoff):
			}
			if backoff < 30*time.Second {
				backoff *= 2
			}
		}
	}()
}

func hbLoop(hbURL string, stop <-chan struct{}) error {
	c, _, err := websocket.DefaultDialer.Dial(hbURL, nil)
	if err != nil {
		return err
	}
	defer c.Close()
	log.Printf("[hb] 已连接 relay 心跳: %s", hbURL)

	// 读循环：检测连接断开 / 接收 relay 下行控制指令
	done := make(chan struct{})
	go func() {
		defer close(done)
		for {
			_, msg, err := c.ReadMessage()
			if err != nil {
				return
			}
			// 处理 relay 下发的控制指令（P1-A：设备端直接执行，绕过 adb）
			handleControlMessage(msg)
		}
	}()

	payload, _ := json.Marshal(map[string]interface{}{"type": "hb", "pid": os.Getpid()})
	tick := time.NewTicker(5 * time.Second)
	defer tick.Stop()
	for {
		select {
		case <-stop:
			return nil
		case <-done:
			return nil
		case <-tick.C:
			if err := c.WriteMessage(websocket.TextMessage, payload); err != nil {
				return err
			}
		}
	}
}

// handleControlMessage 处理 relay 下发的控制指令。
// 消息格式: {"type":"control","cmd":"input tap 100 200"}
func handleControlMessage(msg []byte) {
	var m struct {
		Type string `json:"type"`
		Cmd  string `json:"cmd"`
	}
	if err := json.Unmarshal(msg, &m); err != nil {
		return
	}
	if m.Type != "control" || m.Cmd == "" {
		return
	}
	// 安全检查：只允许 input 命令
	if !strings.HasPrefix(m.Cmd, "input ") {
		log.Printf("[hb] 拒绝非 input 命令: %s", m.Cmd)
		return
	}
	log.Printf("[hb] 执行控制指令: %s", m.Cmd)
	go func(cmd string) {
		parts := strings.Fields(cmd)
		if len(parts) < 2 {
			return
		}
		c := exec.Command(parts[0], parts[1:]...)
		if err := c.Run(); err != nil {
			log.Printf("[hb] 控制指令执行失败: %v", err)
		}
	}(m.Cmd)
}