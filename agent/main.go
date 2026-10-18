package main

import (
	"encoding/binary"
	"flag"
	"log"
	"os"
	"os/signal"
	"syscall"
	"time"
)

func main() {
	relayURL := flag.String("relay", "", "MediaMTX RTSP 推流地址，如 rtsp://mediamtx:8554/device_xxx")
	maxSize := flag.Int("max-size", 720, "输出最长边")
	bitrate := flag.Int("bitrate", 1000000, "码率 bps")
	jarPath := flag.String("jar", "/data/local/tmp/scrcpy-server.jar", "scrcpy-server.jar 路径")
	transport := flag.String("transport", "tcp", "RTSP 传输方式: tcp(默认,稳定) 或 udp(低延迟,需公网可达)")
	flag.Parse()

	if *relayURL == "" {
		*relayURL = os.Getenv("RELAY_URL")
	}
	if *relayURL == "" {
		log.Fatal("必须指定 -relay 或 RELAY_URL")
	}

	log.SetFlags(log.LstdFlags | log.Lmicroseconds)
	log.Printf("[agent] starting, push=%s max_size=%d bitrate=%d", *relayURL, *maxSize, *bitrate)

	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)

	for {
		select {
		case <-sig:
			log.Printf("[agent] 收到退出信号，清理中...")
			return
		default:
		}

		runOnce(*relayURL, *maxSize, *bitrate, *jarPath, *transport)
		log.Printf("[agent] 一次会话结束，5 秒后重启...")
		time.Sleep(5 * time.Second)
	}
}

// runOnce 执行一次完整的「启动 scrcpy + 连 MediaMTX + 推流」会话。
func runOnce(relayURL string, maxSize, bitrate int, jarPath, transport string) {
	// 1. 启动 scrcpy-server（tunnel_forward 模式）
	server := &ScrcpyServer{
		JarPath: jarPath,
		MaxSize: maxSize,
		Bitrate: bitrate,
	}
	if err := server.Start(); err != nil {
		log.Printf("[agent] 启动 scrcpy-server 失败: %v", err)
		time.Sleep(3 * time.Second)
		return
	}
	defer server.Stop()

	// 2. 连接本地 socket 读帧
	reader, err := server.Connect()
	if err != nil {
		log.Printf("[agent] 连接本地 socket 失败: %v", err)
		time.Sleep(3 * time.Second)
		return
	}
	defer reader.Close()

	// 3. 连接 MediaMTX（RTSP RECORD 推流）
	streamer := NewRTPStreamer(relayURL, transport)
	if err := streamer.Connect(); err != nil {
		log.Printf("[agent] RTSP 推流连接失败: %v", err)
		time.Sleep(3 * time.Second)
		return
	}
	defer streamer.Close()

	log.Printf("[agent] 已连接 MediaMTX: %s", relayURL)

	// 4. 读帧 → RTP 编码 → 推流
	buf := make([]byte, 0)
	tmp := make([]byte, 64*1024)
	for {
		n, err := reader.Read(tmp)
		if err != nil {
			log.Printf("[agent] 读帧错误: %v", err)
			return
		}
		if n > 0 {
			buf = append(buf, tmp[:n]...)
			// 解析完整帧（12字节头+payload）并推流
			buf = pumpFrame(buf, streamer)
		}
	}
}

// pumpFrame 解析 scrcpy 帧（12字节头+payload），推流到 MediaMTX。
// 返回未消费完的剩余字节。
func pumpFrame(buf []byte, streamer *RTPStreamer) []byte {
	for len(buf) >= 12 {
		val := binary.BigEndian.Uint64(buf[0:8])
		size := binary.BigEndian.Uint32(buf[8:12])
		if size == 0 {
			buf = buf[12:]
			continue
		}
		if size > 8*1024*1024 {
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
			// config 帧：更新 SPS/PPS，不推流（除非含 slice）
			nals := splitAnnexB(payload)
			streamer.UpdateSPSPPS(nals)
			// 仍尝试推流（PushFrame 会处理 hasSlice）
			if err := streamer.PushFrame(payload); err != nil {
				log.Printf("[agent] 推流失败: %v", err)
			}
			continue
		}

		// 普通帧：RTP 编码 + 推流
		if err := streamer.PushFrame(payload); err != nil {
			log.Printf("[agent] 推流失败: %v", err)
		}
	}
	return buf
}