package main

import (
	"encoding/json"
	"log"
	"os"
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

	// 读循环：检测连接断开 / 接收 relay 下行（当前无业务消息）
	done := make(chan struct{})
	go func() {
		defer close(done)
		for {
			if _, _, err := c.ReadMessage(); err != nil {
				return
			}
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
