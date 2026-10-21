package main

import (
	"encoding/binary"
	"flag"
	"log"
	"os"
	"os/signal"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
)

func main() {
	relayURL := flag.String("relay", "", "MediaMTX RTSP push URL")
	maxSize := flag.Int("max-size", 720, "max size")
	bitrate := flag.Int("bitrate", 1000000, "bitrate")
	maxFPS := flag.Int("max-fps", 0, "max fps (0=unlimited); recommend 12-15 for weak network")
	jarPath := flag.String("jar", "/data/local/tmp/scrcpy-server.jar", "jar path")
	transport := flag.String("transport", "tcp", "RTSP transport: tcp or udp")
	keyFrameInterval := flag.Int("keyframe-interval", 0, "video keyframe interval seconds; 0 = every frame")
	stripVUI := flag.Int("strip-vui", 1, "strip SPS color VUI to fix blue tint (0=off, 1=on)")
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
	if v := os.Getenv("AGENT_STRIP_VUI"); v == "0" || v == "false" {
		*stripVUI = 0
	}
	stripVUIEnabled = (*stripVUI != 0)

	log.SetFlags(log.LstdFlags | log.Lmicroseconds)
	log.Printf("[agent] starting, push=%s max_size=%d bitrate=%d transport=%s keyframe_interval=%ds strip_vui=%v", *relayURL, *maxSize, *bitrate, *transport, *keyFrameInterval, stripVUIEnabled)

	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)

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

		runOnce(*relayURL, *maxSize, *bitrate, *maxFPS, *jarPath, *transport, *keyFrameInterval)
		log.Printf("[agent] session ended, restarting scrcpy-server in 1s...")
		time.Sleep(1 * time.Second)
	}
}

// frameQueueCapacity 背压 channel 容量：超过此数量的帧会被 drop-oldest。
const frameQueueCapacity = 30

// runOnce 运行一个采集+推流会话。
// 采用「读线程 + 写线程 + 有界 channel」的背压架构（P1-1）：
//   - reader 从 scrcpy socket 读帧、解析，送入 frameCh（容量 30）。
//   - writer 从 frameCh 取帧、调用 PushFrame 推到 MediaMTX。
//   - channel 满时 reader 执行 drop-oldest（丢弃最旧帧），避免 reader 阻塞 scrcpy socket。
//   - writer 遇到连接断开时置 connDead，reader 检测后退出，runOnce 做 RTSP 重连（scrcpy 保持运行）。
func runOnce(relayURL string, maxSize, bitrate, maxFPS int, jarPath, transport string, keyFrameInterval int) {
	server := &ScrcpyServer{
		JarPath:          jarPath,
		MaxSize:          maxSize,
		Bitrate:          bitrate,
		KeyFrameInterval: keyFrameInterval,
		MaxFPS:           maxFPS,
	}
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

	pump := &framePump{}

	// 外层循环：RTSP 连接断开后重连，scrcpy 采集保持运行
	for {
		frameCh := make(chan []byte, frameQueueCapacity)
		var connDead atomic.Bool
		var wg sync.WaitGroup

		// ---- Reader goroutine ----
		wg.Add(1)
		go func() {
			defer wg.Done()
			defer close(frameCh)
			buf := make([]byte, 0)
			tmp := make([]byte, 64*1024)
			for {
				if connDead.Load() {
					return
				}
				n, err := reader.Read(tmp)
				if err != nil {
					log.Printf("[agent] read frame error: %v", err)
					connDead.Store(true)
					return
				}
				if n == 0 {
					continue
				}
				buf = append(buf, tmp[:n]...)
				for len(buf) >= 12 && !connDead.Load() {
					val := binary.BigEndian.Uint64(buf[0:8])
					size := binary.BigEndian.Uint32(buf[8:12])
					if size == 0 {
						buf = buf[12:]
						continue
					}
					if size > 8*1024*1024 {
						log.Printf("[agent] abnormal frame size=%d, dropping buffer", size)
						buf = nil
						break
					}
					if len(buf) < 12+int(size) {
						break
					}
					payload := make([]byte, size)
					copy(payload, buf[12:12+int(size)])
					buf = buf[12+int(size):]

					config := (val >> 62) & 1
					if config == 1 {
						nals := splitAnnexB(payload)
						streamer.UpdateSPSPPS(nals)
						if len(streamer.forma.SPS) > 0 {
							pump.sps = make([]byte, len(streamer.forma.SPS))
							copy(pump.sps, streamer.forma.SPS)
						}
						if len(streamer.forma.PPS) > 0 {
							pump.pps = make([]byte, len(streamer.forma.PPS))
							copy(pump.pps, streamer.forma.PPS)
						}
					}

					// 背压：channel 满时 drop-oldest
					select {
					case frameCh <- payload:
					default:
						select {
						case <-frameCh:
							pump.dropCount++
							if pump.dropCount == 1 || pump.dropCount%100 == 0 {
								log.Printf("[agent] backpressure: dropped oldest frame (total=%d)", pump.dropCount)
							}
						default:
						}
						select {
						case frameCh <- payload:
						default:
							pump.dropCount++
						}
					}
				}
			}
		}()

		// ---- Writer goroutine ----
		wg.Add(1)
		go func() {
			defer wg.Done()
			for payload := range frameCh {
				if connDead.Load() {
					return
				}
				if err := streamer.PushFrame(payload); err != nil {
					if isConnError(err) {
						log.Printf("[agent] push frame failed: %v（连接断开，进入重连）", err)
						connDead.Store(true)
						return
					}
					if err == ErrQueueFull {
						pump.dropCount++
						if pump.dropCount%500 == 1 {
							log.Printf("[agent] push: write queue is full (total dropped=%d, throttled)", pump.dropCount)
						}
						continue
					}
					if pump.errCount < 3 || pump.errCount%300 == 0 {
						log.Printf("[agent] push frame failed: %v (total=%d)", err, pump.errCount+1)
					}
					pump.errCount++
				} else {
					pump.errCount = 0
				}
			}
		}()

		wg.Wait()

		if !connDead.Load() {
			// 正常退出（不应发生）
			return
		}

		// RTSP 重连（scrcpy 保持运行）
		streamer.Close()
		reconnected := false
		for attempt := 1; attempt <= 12; attempt++ {
			wait := reconnectBackoff(attempt)
			log.Printf("[agent] RTSP 连接断开，%v 后重连 (第 %d/12 次)...", wait, attempt)
			time.Sleep(wait)
			streamer = NewRTPStreamer(relayURL, transport)
			if err := streamer.Connect(); err != nil {
				log.Printf("[agent] RTSP 重连失败: %v", err)
				streamer.Close()
				continue
			}
			if len(pump.sps) > 0 {
				streamer.forma.SPS = make([]byte, len(pump.sps))
				copy(streamer.forma.SPS, pump.sps)
			}
			if len(pump.pps) > 0 {
				streamer.forma.PPS = make([]byte, len(pump.pps))
				copy(streamer.forma.PPS, pump.pps)
			}
			if streamer.forma.SPS != nil && streamer.forma.PPS != nil {
				streamer.forma.SafeSetParams(streamer.forma.SPS, streamer.forma.PPS)
			}
			log.Printf("[agent] RTSP 重连成功，恢复推流 (SPS=%d bytes, PPS=%d bytes)", len(streamer.forma.SPS), len(streamer.forma.PPS))
			reconnected = true
			break
		}
		if !reconnected {
			log.Printf("[agent] RTSP 多次重连失败，重启整个会话")
			return
		}
		pump.errCount = 0
		// 循环回到顶部，重新创建 frameCh 和 goroutine，继续采集（scrcpy 不重启）
	}
}

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

func isConnError(err error) bool {
	s := err.Error()
	return strings.Contains(s, "EOF") ||
		strings.Contains(s, "broken pipe") ||
		strings.Contains(s, "connection reset") ||
		strings.Contains(s, "use of closed network connection") ||
		strings.Contains(s, "i/o timeout")
}

// framePump 帧推送状态
type framePump struct {
	sps       []byte
	pps       []byte
	errCount  int
	dropCount int
}