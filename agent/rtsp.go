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
			// 剥离 color VUI，避免浏览器按 BT.709/BT.601 混搭解释导致偏蓝
			// （与 RELAY_STRIP_VUI=1 一致）；必须深拷贝，buf 会被复用。
			stripped := stripSPSVUI(nal)
			cp := make([]byte, len(stripped))
			copy(cp, stripped)
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

// stripSPSVUI 剥离 SPS NAL 的 color VUI（vui_parameters_present=0），与 screen_relay.py
// _sps_strip_vui 完全一致的算法，避免浏览器按 BT.709/BT.601 混搭解释导致偏蓝。
// 任何解析异常都原样返回（安全）。
func stripSPSVUI(sps []byte) (out []byte) {
	out = sps
	defer func() {
		if r := recover(); r != nil {
			out = sps
		}
	}()
	if len(sps) < 2 || sps[0]&0x1F != 7 {
		return
	}
	// 1. RBSP：移除 emulation prevention (00 00 03 -> 00 00)
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
	// 2. bitstream
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
		pos++ // the trailing 1
		suffix := 0
		for j := 0; j < lz; j++ {
			suffix = (suffix << 1) | bits[pos]
			pos++
		}
		return (1 << lz) - 1 + suffix
	}
	// 3. 解析 SPS（与 Python _sps_strip_vui 完全一致的字段顺序）
	profile := u(8)
	u(8) // constraint set flags
	u(8) // level
	ue()  // seq_parameter_set_id
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
	ue() // log2_max_frame_num_minus4
	pocType := ue()
	if pocType == 0 {
		ue() // log2_max_pic_order_cnt_lsb_minus4
	} else if pocType == 1 {
		u(1) // delta_pic_order_always_zero_flag
		ue() // offset_for_non_ref_pic
		ue() // offset_for_top_to_bottom_field
		for i := 0; i < ue(); i++ { // num_ref_frames_in_pic_order_cnt_cycle
			ue()
		}
	}
	ue() // max_num_ref_frames
	u(1) // gaps_in_frame_num_value_allowed_flag
	ue() // pic_width_in_mbs_minus1
	ue() // pic_height_in_map_units_minus1
	frameMbsOnly := u(1)
	if frameMbsOnly == 0 {
		u(1) // mb_adaptive_frame_field_flag
	}
	u(1) // direct_8x8_inference_flag
	cropFlag := u(1)
	if cropFlag != 0 {
		ue()
		ue()
		ue()
		ue()
	}
	ue()
	u(1)
	ue()
	ue()
	u(1)
	u(1)
	// 捕获 vui flag 位置（在消费之前），以便正确剥离 VUI 数据
	vuiFlagPos := pos
	vuiFlag := u(1) // vui_parameters_present_flag
	if vuiFlag != 0 {
		// 消费 VUI 数据（4 个 ue，与 Python 一致）
		ue()
		ue()
		ue()
		ue()
	}
	if bits[vuiFlagPos] == 0 {
		return // 已经是 0，无需修改
	}
	// 剥离：保留到 vui flag 之前，flag 置 0，截断所有 VUI 数据，追加 RBSP trailing
	newBits := make([]int, vuiFlagPos)
	copy(newBits, bits[:vuiFlagPos])
	newBits = append(newBits, 0) // vui flag = 0
	newBits = append(newBits, 1) // rbsp trailing
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
