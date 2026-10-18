// myt-agent —— 设备端投屏 Agent（方向 A：scrcpy 采集 + WebSocket 出站）
//
// 架构：
//   设备端 Agent 启动 scrcpy-server（tunnel_forward 模式，监听本地 abstract socket "scrcpy"），
//   本地连接该 socket 读取 H.264 帧，再通过 WebSocket 主动出站到 relay 服务器。
//   彻底绕开不稳定的 adb reverse 隧道（画面数据不再经 adb）。
//
// 断线自愈：
//   - relay 连接断开 → 自动重连（指数退避）
//   - scrcpy 采集失败 → 自动重启 scrcpy-server
//
// 运行参数（通过命令行）：
//   -relay       WebSocket 出站地址，如 ws://relay:9000/agent?adb_addr=xxx&token=xxx
//   -max-size    输出最长边（默认 720）
//   -bitrate     码率 bps（默认 1000000）
//   -jar         scrcpy-server.jar 路径（默认 /data/local/tmp/scrcpy-server.jar）
package main

import (
	"flag"
	"fmt"
	"log"
	"os"
	"os/signal"
	"syscall"
	"time"
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

	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)

	// 主循环：外层无限重连，内层跑"scrcpy采集 + 转发"
	for {
		select {
		case <-sig:
			log.Printf("[agent] 收到退出信号，清理中...")
			return
		default:
		}

		runOnce(*relayURL, *maxSize, *bitrate, *jarPath)
		log.Printf("[agent] 一次会话结束，5 秒后重启...")
		time.Sleep(5 * time.Second)
	}
}

// abortIfFatal 遇到不可重试错误（如 AGENT_TOKEN 无效）时直接退出进程。
// 继续循环只会反复重启 scrcpy-server，白白耗尽设备 CPU / 内存。
func abortIfFatal(relay *RelayConn) {
	if reason := relay.FatalReason(); reason != "" {
		log.Printf("[agent] 致命错误，进程退出: %s", reason)
		os.Exit(1)
	}
}

// runOnce 执行一次完整的「启动 scrcpy + 连 relay + 转发」会话。
// 任何环节失败都会返回，由外层循环重试（实现断线自愈）。
func runOnce(relayURL string, maxSize, bitrate int, jarPath string) {
	// 1. 连接 relay（带重试）
	relay := &RelayConn{URL: relayURL}
	if err := connectRelayWithRetry(relay); err != nil {
		log.Printf("[agent] 连接 relay 失败: %v", err)
		abortIfFatal(relay)
		return
	}
	defer relay.Close()
	log.Printf("[agent] 已连接 relay: %s", relayURL)

	// 2. 启动 scrcpy-server + 连接本地 socket 读帧
	for {
		select {
		case <-sigChannel:
			return
		default:
		}
		// relay 连接已断（含被鉴权拒绝）时，先结束本次会话交由外层重连，
		// 否则会在下面无限重启 scrcpy-server，白白耗尽设备 CPU / 内存。
		if relay.FatalReason() != "" {
			abortIfFatal(relay)
			return
		}
		select {
		case <-relay.Closed():
			log.Printf("[agent] relay 连接已断开，结束本次会话并重连")
			return
		default:
		}

		server := &ScrcpyServer{
			JarPath: jarPath,
			MaxSize: maxSize,
			Bitrate: bitrate,
		}
		if err := server.Start(); err != nil {
			log.Printf("[agent] 启动 scrcpy-server 失败: %v", err)
			time.Sleep(3 * time.Second)
			continue
		}
		reader, err := server.Connect()
		if err != nil {
			log.Printf("[agent] 连接本地 socket 失败: %v", err)
			server.Stop()
			time.Sleep(3 * time.Second)
			continue
		}

		// 3. 读帧 → 转发 relay（读失败或转发失败都会退出，触发重连）
		err = pumpFrames(reader, relay)
		reader.Close()
		server.Stop()
		if err != nil {
			log.Printf("[agent] 帧流中断: %v，准备重连", err)
			// relay 连接可能也断了，重新连
			relay.Close()
			// 退避：避免采集持续失败时形成紧密重启循环
			time.Sleep(2 * time.Second)
			if err2 := connectRelayWithRetry(relay); err2 != nil {
				log.Printf("[agent] relay 重连失败: %v", err2)
				abortIfFatal(relay)
				return
			}
		}
	}
}

var sigChannel = make(chan os.Signal, 1)

func init() {
	signal.Notify(sigChannel, syscall.SIGINT, syscall.SIGTERM)
}

// connectRelayWithRetry 带退避的连接 relay。
func connectRelayWithRetry(relay *RelayConn) error {
	delay := time.Second
	for i := 0; i < 30; i++ {
		// 鉴权/参数类致命错误：重试无意义，直接返回让上层停止
		if reason := relay.FatalReason(); reason != "" {
			return fmt.Errorf("%s", reason)
		}
		err := relay.Connect()
		if err == nil {
			return nil
		}
		log.Printf("[agent] relay 连接失败(第%d次): %v，%v 后重试", i+1, err, delay)
		time.Sleep(delay)
		if delay < 30*time.Second {
			delay *= 2
		}
	}
	return relay.Connect()
}

// pumpFrames 持续读帧并转发，直到读失败或转发失败。
func pumpFrames(reader interface {
	Read([]byte) (int, error)
	Close() error
}, relay *RelayConn) error {
	frames := 0
	buf := make([]byte, 0)
	tmp := make([]byte, 64*1024)
	lastLog := time.Now()
	for {
		n, err := reader.Read(tmp)
		if err != nil {
			return fmt.Errorf("读帧失败: %w", err)
		}
		if n > 0 {
			buf = append(buf, tmp[:n]...)
			// 解析完整帧并转发（帧格式：8字节 pts/config + 4字节 size + payload）
			buf, err = relay.ForwardFrames(buf, &frames)
			if err != nil {
				// 写失败（broken pipe / 写超时）必须中断循环，
				// 否则 Agent 会一直往死连接写，既不恢复又刷爆日志
				return err
			}
		}
		// 每 5 秒打点：可据此判断帧是否真的被解析并转发（帧解析错位时计数不涨）
		if time.Since(lastLog) >= 5*time.Second {
			log.Printf("[agent] 已转发 %d 帧", frames)
			lastLog = time.Now()
		}
	}
}
