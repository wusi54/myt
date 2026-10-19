package main

import (
	"encoding/binary"
	"flag"
	"log"
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
	flag.Parse()

	if *relayURL == "" {
		*relayURL = os.Getenv("RELAY_URL")
	}
	if *relayURL == "" {
		log.Fatal("must specify -relay or RELAY_URL")
	}

	log.SetFlags(log.LstdFlags | log.Lmicroseconds)
	log.Printf("[agent] starting, push=%s max_size=%d bitrate=%d transport=%s keyframe_interval=%ds", *relayURL, *maxSize, *bitrate, *transport, *keyFrameInterval)

	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)

	for {
		select {
		case <-sig:
			log.Printf("[agent] signal received, exiting")
			return
		default:
		}

		runOnce(*relayURL, *maxSize, *bitrate, *jarPath, *transport, *keyFrameInterval)
		log.Printf("[agent] session ended, restarting scrcpy-server immediately...")
		// 不需要 sleep，立即重启 scrcpy-server
	}
}

func runOnce(relayURL string, maxSize, bitrate int, jarPath, transport string, keyFrameInterval int) {
	server := &ScrcpyServer{
		JarPath:          jarPath,
		MaxSize:          maxSize,
		Bitrate:          bitrate,
		KeyFrameInterval: keyFrameInterval,
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
	buf := make([]byte, 0)
	tmp := make([]byte, 64*1024)
	for {
		n, err := reader.Read(tmp)
		if err != nil {
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
