// myt-agent —— 设备端投屏 Agent（方向 A：scrcpy 采集 + WebSocket 出站）
//
// 架构：
//   设备端 Agent 启动 scrcpy-server（tunnel_forward 模式，监听本地 abstract socket "scrcpy"），
//   本地连接该 socket 读取 H.264 帧，再通过 WebSocket 主动出站到 relay 服务器。
//   彻底绕开不稳定的 adb reverse 隧道（画面数据不再经 adb）。
//
// 运行参数（通过环境变量 / 命令行）：
//   RELAY_URL    WebSocket 出站地址，如 ws://relay:9000/agent?adb_addr=xxx
//   MAX_SIZE     输出最长边（默认 720）
//   BITRATE      码率 bps（默认 1000000）
//   SCRCPY_JAR   scrcpy-server.jar 路径（默认 /data/local/tmp/scrcpy-server.jar）
package main

import (
	"flag"
	"log"
	"os"
	"os/signal"
	"syscall"
)

func main() {
	relayURL := flag.String("relay", "", "relay WebSocket 地址，如 ws://relay:9000/agent?adb_addr=xxx")
	maxSize := flag.Int("max-size", 720, "输出最长边")
	bitrate := flag.Int("bitrate", 1000000, "码率 bps")
	jarPath := flag.String("jar", "/data/local/tmp/scrcpy-server.jar", "scrcpy-server.jar 路径")
	flag.Parse()

	if *relayURL == "" {
		*relayURL = os.Getenv("RELAY_URL")
	}
	if *relayURL == "" {
		log.Fatal("必须指定 -relay 或 RELAY_URL")
	}

	log.SetFlags(log.LstdFlags | log.Lmicroseconds)
	log.Printf("[agent] starting, relay=%s max_size=%d bitrate=%d", *relayURL, *maxSize, *bitrate)

	// 启动 scrcpy-server（tunnel_forward 模式，监听本地 socket）
	server := &ScrcpyServer{
		JarPath: *jarPath,
		MaxSize: *maxSize,
		Bitrate: *bitrate,
	}
	if err := server.Start(); err != nil {
		log.Fatalf("[agent] 启动 scrcpy-server 失败: %v", err)
	}
	defer server.Stop()

	// 连接本地 socket 读帧
	reader, err := server.Connect()
	if err != nil {
		log.Fatalf("[agent] 连接本地 socket 失败: %v", err)
	}
	defer reader.Close()

	// WebSocket 出站到 relay
	relay := &RelayConn{URL: *relayURL}
	if err := relay.Connect(); err != nil {
		log.Fatalf("[agent] 连接 relay 失败: %v", err)
	}
	defer relay.Close()
	log.Printf("[agent] 已连接 relay: %s", *relayURL)

	// 信号处理
	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)

	// 主循环：读帧 → 转发 relay
	go func() {
		frames := 0
		buf := make([]byte, 0)
		tmp := make([]byte, 64*1024)
		for {
			n, err := reader.Read(tmp)
			if err != nil {
				log.Printf("[agent] 读帧错误: %v", err)
				relay.SendControl("stream_end")
				return
			}
			if n > 0 {
				buf = append(buf, tmp[:n]...)
				// 解析完整帧并转发（帧格式：8字节 pts/config + 4字节 size + payload）
				buf = relay.ForwardFrames(buf, &frames)
			}
		}
	}()

	<-sig
	log.Printf("[agent] 收到退出信号，清理中...")
}
