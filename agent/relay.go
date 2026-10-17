package main

import (
	"encoding/binary"
	"log"
	"net/url"

	"github.com/gorilla/websocket"
)

// RelayConn 维护与 relay 服务器的 WebSocket 出站连接。
type RelayConn struct {
	URL  string
	conn *websocket.Conn
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
	return nil
}

// Close 关闭连接。
func (r *RelayConn) Close() {
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
func (r *RelayConn) ForwardFrames(buf []byte, frames *int) []byte {
	for len(buf) >= 12 {
		val := binary.BigEndian.Uint64(buf[0:8])
		size := binary.BigEndian.Uint32(buf[8:12])
		if int(size) > 8*1024*1024 {
			// 异常 size，字节流错位，丢弃重建
			log.Printf("[agent] 异常帧 size=%d，丢弃缓冲", size)
			return nil
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
		if r.conn != nil {
			if err := r.conn.WriteMessage(websocket.BinaryMessage, payload); err != nil {
				log.Printf("[agent] 转发帧失败: %v", err)
			}
		}
		*frames++
	}
	return buf
}
