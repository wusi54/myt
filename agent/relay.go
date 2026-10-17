package main

import (
	"encoding/binary"
	"fmt"
	"log"
	"net/url"
	"sync"
	"time"

	"github.com/gorilla/websocket"
)

// writeTimeout 单次写帧超时：防止对「已断但未收到 RST」的半开连接无限阻塞。
const writeTimeout = 5 * time.Second

// RelayConn 维护与 relay 服务器的 WebSocket 出站连接。
type RelayConn struct {
	URL  string
	conn *websocket.Conn
	done chan struct{} // 读协程退出（连接已不可用）时关闭
	once sync.Once
}

// Connect 建立到 relay 的 WebSocket 连接。
func (r *RelayConn) Connect() error {
	u, err := url.Parse(r.URL)
	if err != nil {
		return err
	}
	conn, _, err := websocket.DefaultDialer.Dial(u.String(), nil)
	if err != nil {
		return err
	}
	r.conn = conn
	r.done = make(chan struct{})
	r.once = sync.Once{}
	// ⚠ 必须启动读协程：gorilla 只在读路径里回复 ping。
	// 不读 -> 永不回 pong -> relay(ping_interval=30s/ping_timeout=120s) 判定超时并踢掉连接。
	go r.readLoop()
	return nil
}

// readLoop 持续读取并丢弃来自 relay 的消息。
// 作用有二：① 让 gorilla 自动回复 ping/pong 维持心跳；② 对端关闭时立刻感知。
func (r *RelayConn) readLoop() {
	defer r.markDone()
	for {
		if r.conn == nil {
			return
		}
		if _, _, err := r.conn.NextReader(); err != nil {
			log.Printf("[agent] relay 读循环结束: %v", err)
			return
		}
	}
}

func (r *RelayConn) markDone() {
	if r.done != nil {
		r.once.Do(func() { close(r.done) })
	}
}

// Closed 返回连接关闭信号。
func (r *RelayConn) Closed() <-chan struct{} {
	if r.done == nil {
		return nil
	}
	return r.done
}

// Close 关闭连接。
func (r *RelayConn) Close() {
	r.markDone()
	if r.conn != nil {
		r.conn.Close()
	}
}

// SendControl 发送控制消息（文本）。
func (r *RelayConn) SendControl(msg string) error {
	if r.conn == nil {
		return nil
	}
	return r.conn.WriteMessage(websocket.TextMessage, []byte(msg))
}

// ForwardFrames 从字节流中解析完整 H.264 帧，转发给 relay。
//
// 帧格式（scrcpy send_frame_meta=true）：
//
//	12 字节头: 8字节 pts/config 标志 + 4字节 size
//	  config 标志在最高位（bit 63），config 帧是 SPS/PPS
//	之后是 size 字节的 payload（annexb H.264）
//
// 返回未消费完的剩余字节（不足一帧时保留，等下次数据）。
//
// ⚠ 写失败必须返回 error 让上层中断并触发重连。
//   旧实现「只打日志、循环继续」会导致 relay 断开后 Agent 卡在死连接上
//   不停写 broken pipe（每秒数十条日志刷爆设备存储）且永不恢复。
func (r *RelayConn) ForwardFrames(buf []byte, frames *int) ([]byte, error) {
	for len(buf) >= 12 {
		// 读协程已退出 = 连接不可用（含被 relay 心跳超时踢掉），立即上抛触发重连，
		// 不要等到写失败才发现
		select {
		case <-r.Closed():
			return buf, fmt.Errorf("relay 连接已关闭")
		default:
		}
		val := binary.BigEndian.Uint64(buf[0:8])
		size := binary.BigEndian.Uint32(buf[8:12])
		if int(size) > 8*1024*1024 {
			// 异常 size，字节流错位，丢弃重建
			log.Printf("[agent] 异常帧 size=%d，丢弃缓冲", size)
			return nil, nil
		}
		if len(buf) < 12+int(size) {
			break // 帧未收全，等更多数据
		}
		payload := buf[12 : 12+int(size)]
		buf = buf[12+int(size):]

		config := (val >> 63) & 1
		if config == 1 {
			continue // 配置包(SPS/PPS)，关键帧已内联，跳过
		}

		// 转发给 relay（二进制帧）
		if r.conn == nil {
			return buf, fmt.Errorf("relay 连接为空")
		}
		// 写超时：避免半开连接上无限阻塞（对端已关但本端未收到 RST）
		if err := r.conn.SetWriteDeadline(time.Now().Add(writeTimeout)); err != nil {
			return buf, err
		}
		if err := r.conn.WriteMessage(websocket.BinaryMessage, payload); err != nil {
			// 不再循环重试写同一条死连接：直接上抛，由上层断线重连
			return buf, fmt.Errorf("转发帧失败: %w", err)
		}
		*frames++
	}
	return buf, nil
}
