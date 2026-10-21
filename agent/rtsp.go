package main

import (
	"errors"
	"fmt"
	"log"
	"strings"
	"time"

	"github.com/bluenviron/gortsplib/v5"
	"github.com/bluenviron/gortsplib/v5/pkg/description"
	"github.com/bluenviron/gortsplib/v5/pkg/format"
	"github.com/bluenviron/gortsplib/v5/pkg/format/rtph264"
)

// ErrQueueFull 表示 gortsplib 写队列已满（消费端/MediaMTX 跟不上推流速率）。
// 该错误不应触发 RTSP 重连，仅丢弃当前帧并限流日志；重连只针对连接断开类错误。
var ErrQueueFull = errors.New("write queue is full")

// RTPStreamer 负责把 scrcpy 采集到的 H.264 帧转成 RTP 推流到 MediaMTX。
type RTPStreamer struct {
	rtspURL   string // rtsp://mediamtx:8554/device_xxx
	protocol  *gortsplib.Protocol
	client    *gortsplib.Client
	forma     *format.H264
	rtpEnc    *rtph264.Encoder
	media     *description.Media
	// PTS 基准
	startTime   time.Time
	randomStart uint32
	frameCount  uint64
}

// NewRTPStreamer 创建推流器。
// transport: "tcp"（推荐，穿透 NAT/防火墙稳定）或 "udp"（低延迟，需网络可达）。
func NewRTPStreamer(rtspURL, transport string) *RTPStreamer {
	proto := gortsplib.ProtocolTCP // 默认 TCP：RTP 内嵌 RTSP 连接，不受 NAT/端口映射影响
	if transport == "udp" {
		proto = gortsplib.ProtocolUDP
	}
	return &RTPStreamer{
		rtspURL:   rtspURL,
		protocol:  &proto,
		startTime: time.Now(),
	}
}

// Connect 连接 MediaMTX 并开始 RECORD 推流。
func (r *RTPStreamer) Connect() error {
	forma := &format.H264{
		PayloadTyp:        96,
		PacketizationMode: 1,
	}
	desc := &description.Session{
		Medias: []*description.Media{{
			Type:    description.MediaTypeVideo,
			Formats: []format.Format{forma},
		}},
	}

	c := &gortsplib.Client{
		Protocol:       r.protocol,
		WriteQueueSize: 2048, // 增大队列，避免高帧率/大帧时 "write queue is full"
	}
	if err := c.StartRecording(r.rtspURL, desc); err != nil {
		return fmt.Errorf("RTSP RECORD 开始失败: %w", err)
	}
	r.client = c
	r.forma = forma
	r.media = desc.Medias[0]

	// 生成随机 SSRC 起点（避免多流冲突）
	bs := make([]byte, 4)
	for i := range bs {
		bs[i] = byte(time.Now().UnixNano() >> (8 * i))
	}
	r.randomStart = uint32(bs[0])<<24 | uint32(bs[1])<<16 | uint32(bs[2])<<8 | uint32(bs[3])

	log.Printf("[rtsp] 已连接 MediaMTX: %s", r.rtspURL)
	return nil
}

// Close 关闭连接。
func (r *RTPStreamer) Close() {
	if r.client != nil {
		r.client.Close()
		r.client = nil
	}
}

// UpdateSPSPPS 从 config 帧中提取 SPS/PPS，更新 forma 并重建 encoder。
func (r *RTPStreamer) UpdateSPSPPS(nals [][]byte) {
	for _, nal := range nals {
		if len(nal) == 0 {
			continue
		}
		nalType := nal[0] & 0x1F
		if nalType == 7 { // SPS
			// 必须深拷贝：调用方传入的 nal 可能来自被循环复用的读缓冲 buf，
			// 而 format.H264 会长期持有 SPS/PPS 引用，buf 被覆盖后会读到脏数据。
			cp := make([]byte, len(nal))
			copy(cp, nal)
			r.forma.SPS = cp
		} else if nalType == 8 { // PPS
			cp := make([]byte, len(nal))
			copy(cp, nal)
			r.forma.PPS = cp
		}
	}
	// 重建 encoder（CreateEncoder 不依赖 SPS/PPS，但 SafeSetParams 用于 SDP）
	if r.forma.SPS != nil && r.forma.PPS != nil {
		r.forma.SafeSetParams(r.forma.SPS, r.forma.PPS)
	}
	if r.rtpEnc != nil {
		r.rtpEnc = nil
	}
}

// EnsureEncoder 确保 RTP encoder 已初始化。
func (r *RTPStreamer) EnsureEncoder() error {
	if r.rtpEnc != nil {
		return nil
	}
	enc, err := r.forma.CreateEncoder()
	if err != nil {
		return fmt.Errorf("创建 RTP encoder 失败: %w", err)
	}
	r.rtpEnc = enc
	log.Printf("[rtsp] RTP encoder 已初始化 (SPS=%d bytes, PPS=%d bytes)", len(r.forma.SPS), len(r.forma.PPS))
	return nil
}

// PushFrame 推送一帧 annexb H.264 数据到 MediaMTX。
// 返回 nil 表示成功，错误表示推流失败。
func (r *RTPStreamer) PushFrame(annexb []byte) error {
	// 拷贝一份独立缓冲：调用方的读缓冲 buf 会被循环复用（append / reset），
	// 而下方 splitAnnexB 得到的 NAL 切片、gortsplib 的 format.H264 持有的 SPS/PPS
	// 引用、以及 RTP encoder 持有的 NAL 引用都指向这份数据。若不拷贝，buf 被后续
	// 帧覆盖后会读到脏数据，极端情况下触发非法内存访问使 agent 进程崩溃（WHEP 随之 404）。
	data := make([]byte, len(annexb))
	copy(data, annexb)
	nals := splitAnnexB(data)
	if len(nals) == 0 {
		return nil
	}

	// 检查是否包含参数集和 IDR 帧。
	hasSPS := false
	hasPPS := false
	hasIDR := false
	for _, nal := range nals {
		if len(nal) == 0 {
			continue
		}
		switch nal[0] & 0x1F {
		case 5:
			hasIDR = true
		case 7:
			hasSPS = true
		case 8:
			hasPPS = true
		}
	}
	if hasSPS {
		r.UpdateSPSPPS(nals)
		if err := r.EnsureEncoder(); err != nil {
			return err
		}
		// 检查是否有 slice（type 1 或 5），没有则跳过推流
		hasSlice := false
		for _, nal := range nals {
			if len(nal) > 0 {
				t := nal[0] & 0x1F
				if t == 1 || t == 5 {
					hasSlice = true
					break
				}
			}
		}
		if !hasSlice {
			return nil // 纯 config 帧，跳过
		}
	}

	// 尚未拿到 SPS/PPS 时不能初始化 encoder（避免推出国 SPS=0 的坏流），
	// 跳过该帧等待 config 帧；reconnect 后由缓存的 SPS 恢复。
	if r.forma.SPS == nil || r.forma.PPS == nil {
		return nil
	}

	if r.rtpEnc == nil {
		if err := r.EnsureEncoder(); err != nil {
			return err
		}
	}

	// RTSP RECORD 已在采集到 SPS/PPS 前建立，因而 SDP 中没有参数集。
	// 将参数集带在首个 IDR 前，确保解码器能初始化（如 iOS/FFmpeg）。
	if hasIDR {
		var parameterSets [][]byte
		if !hasSPS && len(r.forma.SPS) > 0 {
			parameterSets = append(parameterSets, r.forma.SPS)
		}
		if !hasPPS && len(r.forma.PPS) > 0 {
			parameterSets = append(parameterSets, r.forma.PPS)
		}
		if len(parameterSets) > 0 {
			nals = append(parameterSets, nals...)
		}
	}

	// 计算 PTS（90000 时钟基）
	pts := uint32(time.Since(r.startTime).Milliseconds()) * 90 // 90000/1000 = 90

	// 生成 RTP packets
	pkts, err := r.rtpEnc.Encode(nals)
	if err != nil {
		return fmt.Errorf("RTP 编码失败: %w", err)
	}

	// 写 RTP packets
	for _, pkt := range pkts {
		pkt.Timestamp = r.randomStart + pts
		if err := r.client.WritePacketRTP(r.media, pkt); err != nil {
			if strings.Contains(err.Error(), "write queue is full") {
				return ErrQueueFull
			}
			return fmt.Errorf("写 RTP 包失败: %w", err)
		}
	}
	r.frameCount++
	return nil
}

// splitAnnexB 把 annexb 格式的 H.264 数据拆成 NAL 数组（不含 start code）。
func splitAnnexB(data []byte) [][]byte {
	var nals [][]byte
	n := len(data)
	if n == 0 {
		return nals
	}
	start := -1
	i := 0
	for i < n {
		var scLen int
		if i+2 < n && data[i] == 0 && data[i+1] == 0 && data[i+2] == 1 {
			scLen = 3
		} else if i+3 < n && data[i] == 0 && data[i+1] == 0 && data[i+2] == 0 && data[i+3] == 1 {
			scLen = 4
		} else {
			i++
			continue
		}
		if start >= 0 {
			nals = append(nals, data[start:i])
		}
		start = i + scLen
		i = start
	}
	if start >= 0 && start < n {
		nals = append(nals, data[start:])
	}
	return nals
}
