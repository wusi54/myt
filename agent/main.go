package main

import (
	"encoding/binary"
	"flag"
	"log"
	"net"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"
)

func main() {
	relayURL := flag.String("relay", "", "MediaMTX RTSP push URL")
	maxSize := flag.Int("max-size", 720, "max size")
	bitrate := flag.Int("bitrate", 1000000, "bitrate")
	jarPath := flag.String("jar", "/data/local/tmp/scrcpy-server.jar", "jar path")
	transport := flag.String("transport", "tcp", "RTSP transport: tcp or udp")
	keyFrameInterval := flag.Int("keyframe-interval", 0, "video keyframe interval seconds; 0 = every frame")
	hbURL := flag.String("hb", "", "relay heartbeat websocket URL (optional)")
	flag.Parse()

	if *relayURL == "" {
		*relayURL = os.Getenv("RELAY_URL")
	}
	if *relayURL == "" {
		log.Fatal("must specify -relay or RELAY_URL")
	}
	if *hbURL == "" {
		*hbURL = os.Getenv("HB_URL")
	}

	log.SetFlags(log.LstdFlags | log.Lmicroseconds)
	log.Printf("[agent] starting, push=%s max_size=%d bitrate=%d transport=%s keyframe_interval=%ds hb=%q", *relayURL, *maxSize, *bitrate, *transport, *keyFrameInterval, *hbURL)

	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)

	// 心跳通道：进程级保活，独立于 runOnce 的会话循环
	stopHb := make(chan struct{})
	defer close(stopHb)
	startHeartbeat(*hbURL, stopHb)

	for {
		select {
		case <-sig:
			log.Printf("[agent] signal received, exiting")
			return
		default:
		}

		runOnce(*relayURL, *maxSize, *bitrate, *jarPath, *transport, *keyFrameInterval)
		// 会话结束后退避 1s 再重启，避免 scrcpy 持续崩溃时形成死循环刷日志/抢 CPU
		log.Printf("[agent] session ended, restarting scrcpy-server in 1s...")
		time.Sleep(1 * time.Second)
	}
}

func runOnce(relayURL string, maxSize, bitrate int, jarPath, transport string, keyFrameInterval int) {
	server := &ScrcpyServer{
		JarPath:          jarPath,
		MaxSize:          maxSize,
		Bitrate:          bitrate,
		KeyFrameInterval: keyFrameInterval,
	}
	// 会话结束时，若 scrcpy-server 是意外崩溃（而非主动 Stop），记录退出原因。
	// 之前没有任何进程管理，崩溃退出码/Java 异常会被完全吞掉，只能在 socket 超时后盲重启。
	defer func() {
		if server.exitCh != nil {
			select {
			case e := <-server.exitCh:
				if e != nil {
					log.Printf("[agent] scrcpy-server 异常退出: %v（详见 scrcpy-agent.log 的 Java 崩溃栈）", e)
				}
			default:
			}
		}
	}()
	if err := server.Start(); err != nil {
		log.Printf("[agent] start scrcpy-server failed: %v, retrying in 3s", err)
		time.Sleep(3 * time.Second)
		return
	}
	defer server.Stop()

	reader, err := server.Connect()
	if err != nil {
		log.Printf("[agent] connect socket failed: %v, retrying in 3s", err)
		server.Stop()
		time.Sleep(3 * time.Second)
		return
	}
	defer reader.Close()

	streamer := NewRTPStreamer(relayURL, transport)
	if err := streamer.Connect(); err != nil {
		log.Printf("[agent] RTSP push connect failed: %v, retrying in 3s", err)
		reader.Close()
		server.Stop()
		time.Sleep(3 * time.Second)
		return
	}
	defer streamer.Close()

	log.Printf("[agent] connected to MediaMTX: %s", relayURL)

	// 帧循环：带读超时 + 每轮检查 scrcpy 进程存活（单 goroutine，无并发）。
	// 旧实现用「每帧起一个 goroutine 读 + death 通道 + 看门狗/defer 多处并发 Close 同一个
	// UnixConn」，并发下会内存损坏导致 Go fatal error 必崩。改为读超时回到循环体检查存活，
	// 既保留“scrcpy 退出能及时感知并重启”的语义，又彻底消除并发。
	pump := &framePump{}
	buf := make([]byte, 0, 256*1024)
	tmp := make([]byte, 64*1024)
	for {
		// scrcpy 死亡看门狗：每轮检查进程存活，退出则结束本次会话由 main 重启采集
		if !server.alive() {
			log.Printf("[agent] scrcpy-server 进程已退出，结束本次会话以便重启采集")
			return
		}
		// 1s 读超时：静态画面无新帧时不会永久阻塞，能周期性回到上面检查存活
		_ = reader.SetReadDeadline(time.Now().Add(1 * time.Second))
		n, err := reader.Read(tmp)
		if err != nil {
			if ne, ok := err.(net.Error); ok && ne.Timeout() {
				continue // 只是暂无新帧，继续等
			}
			// 读错误时再确认一次：scrcpy 退出常表现为读错误
			if !server.alive() {
				log.Printf("[agent] scrcpy-server 进程已退出，结束本次会话以便重启采集")
				return
			}
			log.Printf("[agent] read frame error: %v, restarting scrcpy-server...", err)
			return
		}
		if n == 0 {
			continue
		}
		buf = append(buf, tmp[:n]...)
		var connDead bool
		buf, connDead = pump.pump(buf, streamer)
		if !connDead {
			continue
		}

		// RTSP 连接已断开（EOF/broken pipe 等）：关闭旧连接并带退避重连，
		// scrcpy 采集保持运行。重连期间不读 socket，帧会在设备侧短暂积压。
		streamer.Close()
		reconnected := false
		for attempt := 1; attempt <= 12; attempt++ {
			// scrcpy 已退出则放弃重连，直接重启会话（避免卡在 ~90s 重连窗口）
			if !server.alive() {
				log.Printf("[agent] scrcpy 已退出，放弃 RTSP 重连，重启会话")
				return
			}
			wait := reconnectBackoff(attempt)
			log.Printf("[agent] RTSP 连接断开，%v 后重连 (第 %d/12 次)...", wait, attempt)
			time.Sleep(wait)
			streamer = NewRTPStreamer(relayURL, transport)
			if err := streamer.Connect(); err != nil {
				log.Printf("[agent] RTSP 重连失败: %v", err)
				streamer.Close()
				continue
			}
			// 重新喂入缓存的 SPS/PPS，保证重连后立刻可推流
			if pump.lastConfig != nil {
				streamer.UpdateSPSPPS(splitAnnexB(pump.lastConfig))
			}
			log.Printf("[agent] RTSP 重连成功，恢复推流")
			reconnected = true
			break
		}
		if !reconnected {
			log.Printf("[agent] RTSP 多次重连失败，重启整个会话")
			return
		}
		pump.resetErrors()
		buf = buf[:0] // 丢弃断连期间积攒的半帧数据，避免错位解析
	}
}

// reconnectBackoff 重连退避：1s,2s,3s,5s,8s,10s 封顶。
func reconnectBackoff(attempt int) time.Duration {
	switch {
	case attempt <= 3:
		return time.Duration(attempt) * time.Second
	case attempt == 4:
		return 5 * time.Second
	case attempt == 5:
		return 8 * time.Second
	default:
		return 10 * time.Second
	}
}

// isConnError 判断错误是否说明 RTSP 连接已断开（需要重连）。
func isConnError(err error) bool {
	s := err.Error()
	return strings.Contains(s, "EOF") ||
		strings.Contains(s, "broken pipe") ||
		strings.Contains(s, "connection reset") ||
		strings.Contains(s, "use of closed network connection") ||
		strings.Contains(s, "i/o timeout")
}

// framePump 帧推送状态：缓存 config 帧用于重连恢复，并对重复错误限流。
type framePump struct {
	lastConfig []byte
	errCount   int
}

func (p *framePump) resetErrors() {
	p.errCount = 0
}

// pump 解析 scrcpy 帧并推流。返回 (剩余缓冲, 连接是否已断开需要重连)。
// 连接错误只打前几条日志（限流），避免断网时按帧率刷爆日志。
func (p *framePump) pump(buf []byte, streamer *RTPStreamer) ([]byte, bool) {
	for len(buf) >= 12 {
		val := binary.BigEndian.Uint64(buf[0:8])
		size := binary.BigEndian.Uint32(buf[8:12])
		if size == 0 {
			buf = buf[12:]
			continue
		}
		if size > 8*1024*1024 {
			log.Printf("[agent] abnormal frame size=%d, dropping buffer", size)
			return nil, false
		}
		if len(buf) < 12+int(size) {
			break
		}
		payload := buf[12 : 12+int(size)]
		buf = buf[12+int(size):]

		config := (val >> 63) & 1
		if config == 1 {
			cfg := make([]byte, len(payload))
			copy(cfg, payload)
			p.lastConfig = cfg
			nals := splitAnnexB(payload)
			streamer.UpdateSPSPPS(nals)
			if err := streamer.PushFrame(payload); err != nil {
				if p.logErr(err) {
					return buf, true
				}
			}
			continue
		}

		if err := streamer.PushFrame(payload); err != nil {
			if p.logErr(err) {
				return buf, true
			}
		}
	}
	return buf, false
}

// logErr 错误日志限流。返回 true 表示这是连接错误，调用方应立即重连。
func (p *framePump) logErr(err error) bool {
	if isConnError(err) {
		// 连接已死：后续每帧都会失败，只打第一条，交给重连逻辑
		if p.errCount == 0 {
			log.Printf("[agent] push frame failed: %v（连接断开，进入重连）", err)
		}
		p.errCount++
		return true
	}
	if strings.Contains(err.Error(), "write queue is full") {
		p.errCount++
		if p.errCount%1000 == 1 {
			log.Printf("[agent] push frame: write queue is full（累计 %d 次，已限流）", p.errCount)
		}
		return false
	}
	if p.errCount < 3 || p.errCount%300 == 0 {
		log.Printf("[agent] push frame failed: %v（累计 %d 次）", err, p.errCount+1)
	}
	p.errCount++
	return false
}
