package main

import (
	"errors"
	"fmt"
	"log"
	"strings"
	"time"

	"github.com/bluenviron/gortsplib/v5"
	"github.com/bluenviron/gortsplib/v5/pkg/base"
	"github.com/bluenviron/gortsplib/v5/pkg/description"
	"github.com/bluenviron/gortsplib/v5/pkg/format"
	"github.com/bluenviron/gortsplib/v5/pkg/format/rtph264"
	"github.com/bluenviron/gortsplib/v5/pkg/format/rtph265"

	"github.com/pion/rtcp"
)

// ErrQueueFull 表示 gortsplib 写队列已满。
var ErrQueueFull = errors.New("write queue is full")

// stripVUIEnabled 控制是否剥离 SPS color VUI。
var stripVUIEnabled = true

// RTPStreamer 负责把 scrcpy 采集到的视频帧转成 RTP 推流到 MediaMTX。
type RTPStreamer struct {
	rtspURL  string
	codec    string // h264 或 h265
	protocol *gortsplib.Protocol
	client   *gortsplib.Client
	// H.264
	formaH264 *format.H264
	rtpEncH264 *rtph264.Encoder
	// H.265
	formaH265  *format.H265
	rtpEncH265 *rtph265.Encoder
	media      *description.Media
	baseURL    *base.URL
	keepAlive  chan struct{}
	startTime   time.Time
	randomStart uint32
	frameCount  uint64
	// 缓存的参数集（用于重连恢复）
	sps []byte
	pps []byte
	vps []byte
}

// NewRTPStreamer 创建推流器。
func NewRTPStreamer(rtspURL, transport, codec string) *RTPStreamer {
	proto := gortsplib.ProtocolTCP
	if transport == "udp" {
		proto = gortsplib.ProtocolUDP
	}
	if codec == "" {
		codec = "h264"
	}
	return &RTPStreamer{
		rtspURL:   rtspURL,
		codec:     codec,
		protocol:  &proto,
		startTime: time.Now(),
	}
}

// Connect 连接 MediaMTX 并开始 RECORD 推流。
func (r *RTPStreamer) Connect() error {
	if r.codec == "h265" {
		return r.connectH265()
	}
	return r.connectH264()
}

func (r *RTPStreamer) connectH264() error {
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
		WriteQueueSize: 2048,
	}
	if err := c.StartRecording(r.rtspURL, desc); err != nil {
		return fmt.Errorf("RTSP RECORD 开始失败: %w", err)
	}
	r.client = c
	r.formaH264 = forma
	r.media = desc.Medias[0]

	bs := make([]byte, 4)
	for i := range bs {
		bs[i] = byte(time.Now().UnixNano() >> (8 * i))
	}
	r.randomStart = uint32(bs[0])<<24 | uint32(bs[1])<<16 | uint32(bs[2])<<8 | uint32(bs[3])

	u, err := base.ParseURL(r.rtspURL)
	if err == nil {
		r.baseURL = u
	}

	r.keepAlive = make(chan struct{})
	go r.keepAliveLoop()

	log.Printf("[rtsp] 已连接 MediaMTX: %s (H.264)", r.rtspURL)
	return nil
}

func (r *RTPStreamer) connectH265() error {
	forma := &format.H265{
		PayloadTyp: 97,
	}
	desc := &description.Session{
		Medias: []*description.Media{{
			Type:    description.MediaTypeVideo,
			Formats: []format.Format{forma},
		}},
	}

	c := &gortsplib.Client{
		Protocol:       r.protocol,
		WriteQueueSize: 2048,
	}
	if err := c.StartRecording(r.rtspURL, desc); err != nil {
		return fmt.Errorf("RTSP RECORD 开始失败: %w", err)
	}
	r.client = c
	r.formaH265 = forma
	r.media = desc.Medias[0]

	u, err := base.ParseURL(r.rtspURL)
	if err == nil {
		r.baseURL = u
	}

	r.keepAlive = make(chan struct{})
	go r.keepAliveLoop()

	log.Printf("[rtsp] 已连接 MediaMTX: %s (H.265)", r.rtspURL)
	return nil
}

// keepAliveLoop 每 20s 发送 RTCP Sender Report 保活 TCP 连接。
func (r *RTPStreamer) keepAliveLoop() {
	ticker := time.NewTicker(20 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-r.keepAlive:
			return
		case <-ticker.C:
			if r.client != nil && r.media != nil {
				sr := &rtcp.SenderReport{SSRC: 0}
				if err := r.client.WritePacketRTCP(r.media, sr); err != nil {
					log.Printf("[rtsp] keep-alive RTCP 失败: %v", err)
				}
			}
		}
	}
}

// Close 关闭连接。
func (r *RTPStreamer) Close() {
	if r.keepAlive != nil {
		close(r.keepAlive)
		r.keepAlive = nil
	}
	if r.client != nil {
		r.client.Close()
		r.client = nil
	}
}

// UpdateParams 从 config 帧中提取 VPS/SPS/PPS。
func (r *RTPStreamer) UpdateParams(nals [][]byte) {
	if r.codec == "h265" {
		for _, nal := range nals {
			if len(nal) == 0 {
				continue
			}
			nalType := (nal[0] >> 1) & 0x3F
			switch nalType {
			case 32: // VPS
				cp := make([]byte, len(nal))
				copy(cp, nal)
				r.formaH265.VPS = cp
				r.vps = cp
			case 33: // SPS
				cp := make([]byte, len(nal))
				copy(cp, nal)
				r.formaH265.SPS = cp
				r.sps = cp
			case 34: // PPS
				cp := make([]byte, len(nal))
				copy(cp, nal)
				r.formaH265.PPS = cp
				r.pps = cp
			}
		}
		if r.formaH265.VPS != nil && r.formaH265.SPS != nil && r.formaH265.PPS != nil {
			r.formaH265.SafeSetParams(r.formaH265.VPS, r.formaH265.SPS, r.formaH265.PPS)
		}
	} else {
		for _, nal := range nals {
			if len(nal) == 0 {
				continue
			}
			nalType := nal[0] & 0x1F
			if nalType == 7 { // SPS
				sps := nal
				if stripVUIEnabled {
					sps = stripSPSVUI(nal)
				}
				cp := make([]byte, len(sps))
				copy(cp, sps)
				r.formaH264.SPS = cp
				r.sps = cp
			} else if nalType == 8 { // PPS
				cp := make([]byte, len(nal))
				copy(cp, nal)
				r.formaH264.PPS = cp
				r.pps = cp
			}
		}
		if r.formaH264.SPS != nil && r.formaH264.PPS != nil {
			r.formaH264.SafeSetParams(r.formaH264.SPS, r.formaH264.PPS)
		}
	}
}

// EnsureEncoder 确保 RTP encoder 已初始化。
func (r *RTPStreamer) EnsureEncoder() error {
	if r.codec == "h265" {
		if r.rtpEncH265 != nil {
			return nil
		}
		if r.formaH265.VPS == nil || r.formaH265.SPS == nil || r.formaH265.PPS == nil {
			return fmt.Errorf("VPS/SPS/PPS 未就绪")
		}
		enc, err := r.formaH265.CreateEncoder()
		if err != nil {
			return fmt.Errorf("创建 RTP encoder 失败: %w", err)
		}
		r.rtpEncH265 = enc
		log.Printf("[rtsp] RTP encoder 已初始化 (H.265, VPS=%d SPS=%d PPS=%d bytes)", len(r.formaH265.VPS), len(r.formaH265.SPS), len(r.formaH265.PPS))
		return nil
	}
	if r.rtpEncH264 != nil {
		return nil
	}
	if r.formaH264.SPS == nil || r.formaH264.PPS == nil {
		return fmt.Errorf("SPS/PPS 未就绪")
	}
	enc, err := r.formaH264.CreateEncoder()
	if err != nil {
		return fmt.Errorf("创建 RTP encoder 失败: %w", err)
	}
	r.rtpEncH264 = enc
	log.Printf("[rtsp] RTP encoder 已初始化 (H.264, SPS=%d bytes, PPS=%d bytes)", len(r.formaH264.SPS), len(r.formaH264.PPS))
	return nil
}

// PushFrame 推送一帧。
func (r *RTPStreamer) PushFrame(annexb []byte) error {
	nals := splitAnnexB(annexb)
	if len(nals) == 0 {
		return nil
	}

	if r.codec == "h265" {
		return r.pushH265(nals, annexb)
	}
	return r.pushH264(nals, annexb)
}

func (r *RTPStreamer) pushH264(nals [][]byte, annexb []byte) error {
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
		r.UpdateParams(nals)
	}
	if r.formaH264.SPS == nil {
		return nil // 等待 SPS
	}
	if r.rtpEncH264 == nil {
		if err := r.EnsureEncoder(); err != nil {
			return err
		}
	}

	// 带参数集到 IDR 前
	if hasIDR {
		var ps [][]byte
		if !hasSPS && len(r.formaH264.SPS) > 0 {
			ps = append(ps, r.formaH264.SPS)
		}
		if !hasPPS && len(r.formaH264.PPS) > 0 {
			ps = append(ps, r.formaH264.PPS)
		}
		if len(ps) > 0 {
			nals = append(ps, nals...)
		}
	}

	pts := uint32(time.Since(r.startTime).Milliseconds()) * 90
	pkts, err := r.rtpEncH264.Encode(nals)
	if err != nil {
		return fmt.Errorf("RTP 编码失败: %w", err)
	}
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

func (r *RTPStreamer) pushH265(nals [][]byte, annexb []byte) error {
	hasVPS := false
	hasSPS := false
	hasPPS := false
	hasIDR := false
	for _, nal := range nals {
		if len(nal) == 0 {
			continue
		}
		nalType := (nal[0] >> 1) & 0x3F
		switch nalType {
		case 32:
			hasVPS = true
		case 33:
			hasSPS = true
		case 34:
			hasPPS = true
		case 19, 20, 21:
			hasIDR = true
		}
	}
	if hasVPS || hasSPS || hasPPS {
		r.UpdateParams(nals)
	}
	if r.formaH265.VPS == nil {
		return nil // 等待 VPS
	}
	if r.rtpEncH265 == nil {
		if err := r.EnsureEncoder(); err != nil {
			return err
		}
	}

	// 带参数集到 IDR 前
	if hasIDR {
		var ps [][]byte
		if !hasVPS && r.formaH265.VPS != nil {
			ps = append(ps, r.formaH265.VPS)
		}
		if !hasSPS && r.formaH265.SPS != nil {
			ps = append(ps, r.formaH265.SPS)
		}
		if !hasPPS && r.formaH265.PPS != nil {
			ps = append(ps, r.formaH265.PPS)
		}
		if len(ps) > 0 {
			nals = append(ps, nals...)
		}
	}

	pts := uint32(time.Since(r.startTime).Milliseconds()) * 90
	pkts, err := r.rtpEncH265.Encode(nals)
	if err != nil {
		return fmt.Errorf("RTP 编码失败: %w", err)
	}
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

// splitAnnexB 把 annexb 格式的 H.264/H.265 数据拆成 NAL 数组。
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

// stripSPSVUI 剥离 SPS color VUI（H.264 only）。
func stripSPSVUI(sps []byte) (out []byte) {
	out = sps
	defer func() {
		if r := recover(); r != nil {
			out = sps
		}
	}()
	if len(sps) < 2 || sps[0]&0x1F != 7 {
		return sps
	}
	rbsp := []byte{}
	i := 1
	n := len(sps)
	zeros := 0
	for i < n {
		b := sps[i]
		if zeros == 2 && b == 0x03 {
			zeros = 0
			i++
			continue
		}
		rbsp = append(rbsp, b)
		if b == 0 {
			zeros++
		} else {
			zeros = 0
		}
		i++
	}
	bits := []int{}
	for _, by := range rbsp {
		for k := 7; k >= 0; k-- {
			bits = append(bits, int((by>>k)&1))
		}
	}
	pos := 0
	u := func(nb int) int {
		v := 0
		for j := 0; j < nb; j++ {
			if pos >= len(bits) {
				panic("oob")
			}
			v = (v << 1) | bits[pos]
			pos++
		}
		return v
	}
	ue := func() int {
		lz := 0
		for bits[pos] == 0 {
			lz++
			pos++
			if lz > 32 {
				panic("ue")
			}
		}
		pos++
		suffix := 0
		for j := 0; j < lz; j++ {
			suffix = (suffix << 1) | bits[pos]
			pos++
		}
		return (1 << lz) - 1 + suffix
	}
	profile := u(8)
	u(8)
	u(8)
	ue()
	if profile == 100 || profile == 110 || profile == 122 || profile == 244 || profile == 44 || profile == 83 || profile == 86 || profile == 118 || profile == 128 || profile == 138 || profile == 139 || profile == 134 || profile == 135 {
		c := ue()
		if c == 3 {
			u(1)
		}
		ue()
		ue()
		u(1)
		if u(1) != 0 {
			panic("scaling")
		}
	}
	ue()
	pocType := ue()
	if pocType == 0 {
		ue()
	} else if pocType == 1 {
		u(1)
		ue()
		ue()
		for i := 0; i < ue(); i++ {
			ue()
		}
	}
	ue()
	u(1)
	ue()
	ue()
	u(1)
	u(1)
	vuiFlagPos := pos
	vuiFlag := u(1)
	if vuiFlag != 0 {
		ue()
		ue()
		ue()
		ue()
	}
	if bits[vuiFlagPos] == 0 {
		return sps
	}
	newBits := make([]int, vuiFlagPos)
	copy(newBits, bits[:vuiFlagPos])
	newBits = append(newBits, 0)
	newBits = append(newBits, 1)
	for len(newBits)%8 != 0 {
		newBits = append(newBits, 0)
	}
	newRbsp := []byte{}
	for j := 0; j < len(newBits); j += 8 {
		by := byte(0)
		for k := 0; k < 8; k++ {
			by = (by << 1) | byte(newBits[j+k])
		}
		newRbsp = append(newRbsp, by)
	}
	outBytes := []byte{sps[0]}
	for _, by := range newRbsp {
		if len(outBytes) >= 2 && outBytes[len(outBytes)-1] == 0 && outBytes[len(outBytes)-2] == 0 && by <= 3 {
			outBytes = append(outBytes, 0x03)
		}
		outBytes = append(outBytes, by)
	}
	out = outBytes
	return
}

