package main

import (
	"encoding/binary"
	"errors"
	"flag"
	"log"
	"net"
	"os"
	"os/signal"
	"strings"
	"sync"
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

	// scrcpy 进程死亡看门狗：scrcpy-server 一旦退出，无论帧循环当前阻塞在读/推/重连哪一步，
	// 都立即结束本次会话，由 main 循环重新拉起 scrcpy。
	// 之前 scrcpy 在 RTSP 重连分支或推流阻塞时死亡，agent 无法及时感知，
	// 表现为“scrcpy 退出后一直不重启”（relay 只管 myt-agent 二进制，不管其拉起的子进程）。
	death := make(chan struct{})
	var deathOnce sync.Once
	closeDeath := func() { deathOnce.Do(func() { close(death) }) }
	go func() {
		select {
		case e := <-server.exitCh:
			if e != nil {
				log.Printf("[agent] scrcpy-server 异常退出: %v（详见 scrcpy-agent.log 的 Java 崩溃栈）", e)
			}
			log.Printf("[agent] scrcpy-server 进程已退出，强制结束本次会话以便重启采集")
			reader.Close() // 打断阻塞中的 Read
			closeDeath()
		case <-server.doneCh:
			// 主动 Stop（正常结束会话），无需处理
		}
	}()
	defer closeDeath()

	pump := &framePump{}
	buf := make([]byte, 0)
	tmp := make([]byte, 64*1024)
	for {
		n, err := readFrame(reader, tmp, death)
		if err != nil {
			if err == errSessionEnded {
				log.Printf("[agent] 会话因 scrcpy 退出而结束，重启采集")
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
			select {
			case <-death:
				log.Printf("[agent] scrcpy 已退出，放弃 RTSP 重连，重启会话")
				return
			default:
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

// errSessionEnded 表示会话因 scrcpy 进程退出被看门狗主动结束。
var errSessionEnded = errors.New("session ended by scrcpy death")

// readFrame 在阻塞 Read 外包一层，使 scrcpy 死亡看门狗(death)能抢占，
// 避免帧循环卡在 reader.Read 时无法感知 scrcpy 已退出。
func readFrame(conn net.Conn, tmp []byte, death <-chan struct{}) (int, error) {
	type res struct{ n int; err error }
	ch := make(chan res, 1)
	go func() {
		n, err := conn.Read(tmp)
		ch <- res{n, err}
	}()
	select {
	case r := <-ch:
		return r.n, r.err
	case <-death:
		conn.Close() // 确保后台 Read goroutine 退出
		return 0, errSessionEnded
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
