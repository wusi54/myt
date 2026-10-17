/* myt-h264.js —— 投屏 H.264 解码/渲染统一库（手机浏览器兼容）
 *
 * 背景：scrcpy-server 经 WS 下发 Annex-B H.264 裸流。
 * 桌面 Chrome/Edge、Android Chrome 可用 WebCodecs(VideoDecoder) 低延迟解码到 canvas；
 * 但 iOS Safari 的 WebCodecs 对 H.264 支持不可靠，需降级到 <video> + MSE(ManagedMediaSource)
 * 播放 fragmented MP4（fMP4），走系统硬件解码。
 *
 * 本库暴露：
 *   mytH264.detectMode()          -> 'webcodecs' | 'mse' | 'none'
 *   mytH264.createRenderer(container, opts) -> renderer
 *   renderer.el                   显示元素(canvas 或 video)，触控绑定到它
 *   renderer.feedH264(arraybuffer) 喂入一段 Annex-B H264 数据
 *   renderer.setPaused(bool)       暂停/恢复
 *   renderer.reset()               重置解码状态
 *   renderer.destroy()             销毁
 *   renderer.naturalWidth/Height   帧的实际宽高（用于触控坐标归一化）
 */
(function (global) {
  'use strict';

  // ==================== 基础工具 ====================
  function concatBytes() {
    let total = 0;
    for (let i = 0; i < arguments.length; i++) total += arguments[i].length;
    const out = new Uint8Array(total);
    let off = 0;
    for (let i = 0; i < arguments.length; i++) {
      out.set(arguments[i], off);
      off += arguments[i].length;
    }
    return out;
  }

  // ==================== H.264 SPS 解析 ====================
  // 仅需 profile/compat/level（生成 avcC 与 codec string）与宽高（显示比例）。
  class BitReader {
    constructor(bytes) {
      this.b = bytes; this.pos = 0;
    }
    u(n) {
      let v = 0;
      for (let i = 0; i < n; i++) { v = (v << 1) | ((this.b[this.pos >> 3] >> (7 - (this.pos & 7))) & 1); this.pos++; }
      return v;
    }
    ue() {
      let zeros = 0;
      while (this.pos < this.b.length * 8 && this.u(1) === 0) zeros++;
      return (1 << zeros) - 1 + (zeros === 0 ? 0 : this.u(zeros));
    }
  }

  // 去掉 Annex-B 的 emulation prevention byte (0x03)
  function rbsp(sps) {
    const out = [];
    let zeros = 0;
    for (let i = 0; i < sps.length; i++) {
      const b = sps[i];
      if (zeros >= 2 && b === 0x03) { zeros = 0; continue; }
      out.push(b);
      zeros = (b === 0) ? zeros + 1 : 0;
    }
    return new Uint8Array(out);
  }

  function parseSps(spsNal) {
    // spsNal: 含 NAL header (0x67) 的完整 SPS NAL，不含 start code
    const r = new BitReader(rbsp(spsNal.subarray(1)));
    const profile = r.u(8);
    const compat = r.u(8);
    const level = r.u(8);
    r.ue(); // seq_parameter_set_id
    let chroma = 1;
    if (profile === 100 || profile === 110 || profile === 122 || profile === 244 ||
        profile === 44 || profile === 83 || profile === 86 || profile === 118 ||
        profile === 128 || profile === 138 || profile === 139 || profile === 134 || profile === 135) {
      chroma = r.ue();
      if (chroma === 3) r.u(1);
      r.ue(); // bit_depth_luma_minus8
      r.ue(); // bit_depth_chroma_minus8
      r.u(1); // qpprime_y_zero_transform_bypass_flag
      const scaling = r.u(1);
      if (scaling) {
        const count = (chroma !== 3) ? 8 : 12;
        for (let i = 0; i < count; i++) {
          if (r.u(1)) scalingList(r, i < 6 ? 16 : 64);
        }
      }
    }
    r.ue(); // log2_max_frame_num_minus4
    const pocType = r.ue();
    if (pocType === 0) r.ue();
    else if (pocType === 1) {
      r.u(1); r.ue(); r.ue();
      const n = r.ue();
      for (let i = 0; i < n; i++) r.ue();
    }
    r.ue(); // max_num_ref_frames
    r.u(1); // gaps_in_frame_num_value_allowed_flag
    const widthMbs = r.ue() + 1;
    const heightMapUnits = r.ue() + 1;
    const frameMbsOnly = r.u(1);
    if (!frameMbsOnly) r.u(1);
    r.u(1); // direct_8x8_inference_flag
    const cropping = r.u(1);
    let cropL = 0, cropR = 0, cropT = 0, cropB = 0;
    if (cropping) { cropL = r.ue(); cropR = r.ue(); cropT = r.ue(); cropB = r.ue(); }
    let cropUnitX = 1, cropUnitY = 1;
    if (chroma === 1) { cropUnitX = 2; cropUnitY = 2; }
    else if (chroma === 2) { cropUnitX = 2; cropUnitY = 1; }
    let width = widthMbs * 16 - (cropL + cropR) * cropUnitX;
    let height = (2 - frameMbsOnly) * heightMapUnits * 16 - (cropT + cropB) * cropUnitY;
    if (width <= 0) width = 16;
    if (height <= 0) height = 16;
    const codec = 'avc1.' + hex2(profile) + hex2(compat) + hex2(level);
    return { profile: profile, compat: compat, level: level, width: width, height: height, codec: codec };
  }

  function scalingList(r, size) {
    let lastScale = 8, nextScale = 8;
    for (let j = 0; j < size; j++) {
      if (nextScale !== 0) {
        let delta = 0, zeros = 0;
        while (r.u(1) === 0) zeros++;
        if (zeros) delta = ((1 << zeros) - 1) + r.u(zeros);
        // se(v) 简化为 ue 近似（缩放矩阵对宽高/码率无影响，容错即可）
        nextScale = (lastScale + (delta >> 1) + 256) % 256;
      }
      lastScale = (nextScale === 0) ? lastScale : nextScale;
    }
  }

  function hex2(v) {
    const s = v.toString(16).toUpperCase();
    return s.length < 2 ? '0' + s : s;
  }

  // ==================== fMP4 封装 ====================
  function box(type, payload) {
    const sz = 8 + payload.length;
    const out = new Uint8Array(sz);
    const dv = new DataView(out.buffer);
    dv.setUint32(0, sz);
    out.set([type.charCodeAt(0), type.charCodeAt(1), type.charCodeAt(2), type.charCodeAt(3)], 4);
    out.set(payload, 8);
    return out;
  }

  function fullBox(type, version, flags, payload) {
    const p = new Uint8Array(4 + payload.length);
    const dv = new DataView(p.buffer);
    dv.setUint8(0, version);
    dv.setUint32(0, (version << 24) | flags);
    p.set(payload, 4);
    return box(type, p);
  }

  const UNIT_MATRIX = new Uint8Array([
    0, 1, 0, 0,  0, 0, 0, 0,  0, 0, 0, 0,
    0, 0, 0, 0,  0, 1, 0, 0,  0, 0, 0, 0,
    0, 0, 0, 0,  0, 0, 0, 0,  0x40, 0, 0, 0,
  ]);

  function makeFtyp() {
    return box('ftyp', concatBytes(
      str('isom'), u32(0x00000200), str('isom'), str('iso2'), str('avc1'), str('mp41')
    ));
  }

  function makeMvhd() {
    return fullBox('mvhd', 0, 0, concatBytes(
      u32(0), u32(0), u32(90000), u32(0),   // ctime, mtime, timescale, duration
      u32(0x00010000), u16(0x0100), u16(0), u32(0), u32(0),
      UNIT_MATRIX, new Uint8Array(24), u32(2)
    ));
  }

  function makeTkhd(width, height) {
    return fullBox('tkhd', 0, 0x000003, concatBytes(
      u32(0), u32(0), u32(1), u32(0), u32(0),   // ctime, mtime, track_id, reserved, duration
      u32(0), u32(0), u16(0), u16(0), u16(0), u16(0),
      UNIT_MATRIX, u32(width << 16), u32(height << 16)
    ));
  }

  function makeMdhd() {
    return fullBox('mdhd', 0, 0, concatBytes(
      u32(0), u32(0), u32(90000), u32(0), u16(0x55C4), u16(0)
    ));
  }

  function makeHdlr() {
    return fullBox('hdlr', 0, 0, concatBytes(
      u32(0), str('vide'), new Uint8Array(12), str('VideoHandler'), u8(0)
    ));
  }

  function makeVmhd() {
    return fullBox('vmhd', 0, 1, concatBytes(u16(0), new Uint8Array(6)));
  }

  function makeDinf() {
    const url = fullBox('url ', 0, 1, new Uint8Array(0));
    const dref = fullBox('dref', 0, 0, concatBytes(u32(1), url));
    return box('dinf', dref);
  }

  function makeAvcC(sps, pps) {
    return box('avcC', concatBytes(
      u8(1), u8(sps[1]), u8(sps[2]), u8(sps[3]),   // configVersion, profile, compat, level
      u8(0xFC),                                      // lengthSizeMinusOne = 3 (4 字节长度) + reserved 111111
      u8(0xE1), u16(sps.length), sps,               // 1 个 SPS
      u8(0x01), u16(pps.length), pps                // 1 个 PPS
    ));
  }

  function makeStsd(sps, pps, width, height) {
    const avcC = makeAvcC(sps, pps);
    const avc1 = box('avc1', concatBytes(
      new Uint8Array(6), u16(1),                   // reserved, data_reference_index
      u16(0), u16(0), new Uint8Array(12),          // pre_defined, reserved, pre_defined
      u16(width), u16(height),                     // width, height
      u32(0x00480000), u32(0x00480000),            // horiz/vert resolution
      u32(0), u16(1), new Uint8Array(32), u16(0x0018), u16(0xFFFF),
      avcC
    ));
    return fullBox('stsd', 0, 0, concatBytes(u32(1), avc1));
  }

  function makeEmptyStblPart(type) {
    // fMP4 中 stts/stsc/stsz/stco 可空(entry_count=0)，实际 sample 信息在 moof/trun
    return fullBox(type, 0, 0, u32(0));
  }

  function makeStbl(sps, pps, width, height) {
    return box('stbl', concatBytes(
      makeStsd(sps, pps, width, height),
      makeEmptyStblPart('stts'),
      makeEmptyStblPart('stsc'),
      makeEmptyStblPart('stsz'),
      makeEmptyStblPart('stco')
    ));
  }

  function makeMdia(sps, pps, width, height) {
    return box('mdia', concatBytes(makeMdhd(), makeHdlr(),
      box('minf', concatBytes(makeVmhd(), makeDinf(), makeStbl(sps, pps, width, height)))));
  }

  function makeTrak(sps, pps, width, height) {
    return box('trak', concatBytes(makeTkhd(width, height), makeMdia(sps, pps, width, height)));
  }

  function makeMoov(sps, pps, width, height) {
    return box('moov', concatBytes(makeMvhd(), makeTrak(sps, pps, width, height)));
  }

  function makeInitSegment(sps, pps, width, height) {
    return concatBytes(makeFtyp(), makeMoov(sps, pps, width, height));
  }

  function makeMediaSegment(avccSamples, dts, duration, seqNum) {
    const trackId = 1;
    const sampleCount = avccSamples.length;
    const sampleFlags = avccSamples.map((s, i) => s.key ? 0x02000000 : 0x01010000);

    const mfhd = fullBox('mfhd', 0, 0, u32(seqNum));
    const tfhd = fullBox('tfhd', 0, 0x000000, u32(trackId));
    const tfdt = fullBox('tfdt', 1, 0, u64(dts));

    // trun: data_offset + sample_duration + sample_size + sample_flags
    const flags = 0x000001 | 0x000100 | 0x000200 | 0x000400;
    let trunData = concatBytes(u32(sampleCount));
    const dataOffset = 8 + mfhd.length + tfhd.length + tfdt.length +
       8 + 4 + 4 + sampleCount * 12; // moof 头 + mfhd + tfhd + tfdt + trun头 + sample_count + 每 sample 12 字节
    trunData = concatBytes(trunData, u32(dataOffset));
    for (let i = 0; i < sampleCount; i++) {
      trunData = concatBytes(trunData, u32(duration), u32(avccSamples[i].data.length), u32(sampleFlags[i]));
    }
    const trun = fullBox('trun', 0, flags, trunData);
    const traf = box('traf', concatBytes(tfhd, tfdt, trun));
    const moof = box('moof', concatBytes(mfhd, traf));

    const mdatPayload = concatBytes.apply(null, avccSamples.map(s => s.data));
    const mdat = box('mdat', mdatPayload);

    return concatBytes(moof, mdat);
  }

  function str(s) {
    const out = new Uint8Array(s.length);
    for (let i = 0; i < s.length; i++) out[i] = s.charCodeAt(i);
    return out;
  }
  function u8(v) { return new Uint8Array([v & 0xff]); }
  function u16(v) { const a = new Uint8Array(2); new DataView(a.buffer).setUint16(0, v); return a; }
  function u32(v) { const a = new Uint8Array(4); new DataView(a.buffer).setUint32(0, v); return a; }
  function u64(v) {
    const a = new Uint8Array(8);
    const dv = new DataView(a.buffer);
    dv.setUint32(0, Math.floor(v / 4294967296));
    dv.setUint32(4, v >>> 0);
    return a;
  }

  // ==================== Annex-B 解析（NAL 切分） ====================
  // 供 WebCodecs 与 muxer 共用：从 Annex-B 字节流切出 NAL 单元。
  // 返回 {sps, pps, frames:[{key, nals:[Uint8Array,...]}]}，内部缓存跨包不完整的尾部。
  class AnnexBParser {
    constructor() {
      this.buf = new Uint8Array(0);
    }
    feed(data) {
      this.buf = concatBytes(this.buf, new Uint8Array(data));
      const frames = [];
      let pos = 0, start = -1, startLen = 0;
      const n = this.buf.length;
      while (pos < n - 3) {
        let sl = 0;
        if (this.buf[pos] === 0 && this.buf[pos + 1] === 0 && this.buf[pos + 2] === 1) sl = 3;
        else if (pos < n - 4 && this.buf[pos] === 0 && this.buf[pos + 1] === 0 && this.buf[pos + 2] === 0 && this.buf[pos + 3] === 1) sl = 4;
        if (sl > 0) {
          if (start >= 0) {
            const nal = this.buf.subarray(start, pos);
            this._onNal(nal, frames);
          }
          start = pos + sl;
          pos += sl;
        } else pos++;
      }
      if (start >= 0) { this.buf = this.buf.subarray(start); }
      else { this.buf = new Uint8Array(0); }
      return frames;
    }
    _onNal(nal, frames) {
      if (nal.length === 0) return;
      const type = nal[0] & 0x1F;
      if (type === 7) { this.sps = nal.slice(); return; }
      if (type === 8) { this.pps = nal.slice(); return; }
      // slice(5=IDR, 1=non-IDR) 作为 access unit 结束标记
      if (type === 5 || type === 1) {
        const key = type === 5;
        // 关键帧内联 SPS/PPS
        if (key && this.sps && this.pps) {
          frames.push({ key: true, nals: [this.sps, this.pps, nal.slice()] });
        } else if (key) {
          frames.push({ key: true, nals: [nal.slice()] });
        } else {
          frames.push({ key: false, nals: [nal.slice()] });
        }
      }
      // SEI(6)/AUD(9) 忽略
    }
  }

  // ==================== WebCodecs 渲染器 ====================
  class WebCodecsRenderer {
    constructor(container, opts) {
      this.container = container;
      this.onStatus = (opts && opts.onStatus) || function () {};
      this.codec = (opts && opts.codec) || 'avc1.42C01E';
      this.canvas = document.createElement('canvas');
      this.canvas.style.cssText = 'width:100%;height:100%;object-fit:contain;display:block;background:#000;touch-action:none;cursor:crosshair;user-select:none;-webkit-user-select:none;-webkit-tap-highlight-color:transparent';
      container.appendChild(this.canvas);
      this.ctx = this.canvas.getContext('2d');
      this.el = this.canvas;
      this.naturalWidth = 0;
      this.naturalHeight = 0;
      this.decoder = null;
      this.paused = false;
      this.destroyed = false;
      this._ts = 0;
      this._lastDraw = 0;
      this.parser = new AnnexBParser();
      this._initDecoder();
    }
    _initDecoder() {
      if (typeof VideoDecoder === 'undefined') { this.onStatus('不支持 WebCodecs'); return; }
      try {
        this.decoder = new VideoDecoder({
          output: (frame) => {
            try {
              this.naturalWidth = frame.displayWidth;
              this.naturalHeight = frame.displayHeight;
              if (this.canvas.width !== frame.displayWidth) this.canvas.width = frame.displayWidth;
              if (this.canvas.height !== frame.displayHeight) this.canvas.height = frame.displayHeight;
              const now = performance.now();
              if (now - this._lastDraw >= 66) { this._lastDraw = now; this.ctx.drawImage(frame, 0, 0); }
            } catch (e) {}
            frame.close();
          },
          error: (e) => { try { console.error('decode error', e); } catch (_) {} }
        });
        this.decoder.configure({ codec: this.codec, optimizeForLatency: true });
      } catch (e) { this.onStatus('解码器初始化失败'); }
    }
    feedH264(data) {
      if (this.destroyed || !this.decoder || this.paused) return;
      const frames = this.parser.feed(data);
      for (const f of frames) {
        if (f.key && f.nals.length >= 3) {
          // 从 SPS 动态更新 codec（profile/level 可能与默认不同）
          try {
            const info = parseSps(f.nals[0]);
            if (info.codec !== this.codec) {
              this.codec = info.codec;
              this.decoder.configure({ codec: this.codec, optimizeForLatency: true });
            }
          } catch (e) {}
        }
        const total = f.nals.reduce((s, u) => s + u.length, 0);
        const data2 = new Uint8Array(total);
        let off = 0;
        for (const nal of f.nals) { data2.set(nal, off); off += nal.length; }
        const ts = this._ts; this._ts += 100000;
        try {
          this.decoder.decode(new EncodedVideoChunk({ type: f.key ? 'key' : 'delta', timestamp: ts, duration: 100000, data: data2 }));
        } catch (e) {}
      }
    }
    setPaused(p) {
      if (p === this.paused) return;
      this.paused = p;
      if (!p && this.decoder) { try { this.decoder.reset(); this.decoder.configure({ codec: this.codec, optimizeForLatency: true }); } catch (e) {} }
    }
    reset() {
      this.parser = new AnnexBParser();
      this._ts = 0;
      if (this.decoder) { try { this.decoder.reset(); this.decoder.configure({ codec: this.codec, optimizeForLatency: true }); } catch (e) {} }
    }
    destroy() {
      this.destroyed = true;
      if (this.decoder) { try { this.decoder.close(); } catch (e) {} this.decoder = null; }
      try { if (this.canvas.parentNode) this.canvas.parentNode.removeChild(this.canvas); } catch (e) {}
    }
  }

  // ==================== MSE 渲染器 ====================
  class MseRenderer {
    constructor(container, opts) {
      this.container = container;
      this.onStatus = (opts && opts.onStatus) || function () {};
      this.destroyed = false;
      this.paused = false;
      this.naturalWidth = 0;
      this.naturalHeight = 0;

      const MseClass = (typeof ManagedMediaSource !== 'undefined') ? ManagedMediaSource :
                       (typeof MediaSource !== 'undefined') ? MediaSource : null;
      this.MseClass = MseClass;
      this.video = document.createElement('video');
      this.video.muted = true;
      this.video.autoplay = true;
      this.video.playsInline = true;
      this.video.setAttribute('playsinline', '');
      this.video.setAttribute('muted', '');
      this.video.style.cssText = 'width:100%;height:100%;object-fit:contain;display:block;background:#000;touch-action:none;cursor:crosshair;user-select:none;-webkit-user-select:none;-webkit-tap-highlight-color:transparent';
      container.appendChild(this.video);
      this.el = this.video;

      this.parser = new AnnexBParser();
      this.ms = null;
      this.sourceBuffer = null;
      this.queue = [];          // 待 append 的 segment
      this.appending = false;
      this.codec = '';
      this.sps = null;
      this.pps = null;
      this.seqNum = 1;
      this.dts = 0;
      this.duration = 3000;     // 默认 30fps @ 90000
      this._lastFrameTs = 0;
      this._initSent = false;

      if (!MseClass) { this.onStatus('浏览器不支持 MSE'); return; }
      this.ms = new MseClass();
      this.video.src = URL.createObjectURL(this.ms);
      this.ms.addEventListener('sourceopen', () => this._onSourceOpen());
      this.video.addEventListener('playing', () => { this.onStatus(''); });
      this.video.addEventListener('error', () => { this.onStatus('播放错误'); });
    }
    _onSourceOpen() {
      // sourceBuffer 需在拿到 SPS/PPS 确定 codec 后创建
      if (this.sps && this.pps && !this.sourceBuffer) this._createSourceBuffer();
    }
    _createSourceBuffer() {
      try {
        const info = parseSps(this.sps);
        this.codec = info.codec;
        this.naturalWidth = info.width;
        this.naturalHeight = info.height;
        this.sourceBuffer = this.ms.addSourceBuffer('video/mp4; codecs="' + this.codec + '"');
        this.sourceBuffer.mode = 'sequence';
        this.sourceBuffer.addEventListener('updateend', () => this._drain());
        this.sourceBuffer.addEventListener('error', () => { this.onStatus('buffer 错误'); });
        this._append(makeInitSegment(this.sps, this.pps, info.width, info.height));
      } catch (e) {
        this.onStatus('MSE 初始化失败: ' + e);
      }
    }
    _append(segment) {
      this.queue.push(segment);
      this._drain();
    }
    _drain() {
      if (this.appending || !this.sourceBuffer) return;
      if (this.sourceBuffer.updating) return;
      if (this.queue.length === 0) return;
      const seg = this.queue.shift();
      this.appending = true;
      try { this.sourceBuffer.appendBuffer(seg); } catch (e) { this.appending = false; }
      // appending 由 updateend 复位
    }
    feedH264(data) {
      if (this.destroyed || this.paused) return;
      const frames = this.parser.feed(data);
      for (const f of frames) {
        if (f.key && f.nals.length >= 3) { this.sps = f.nals[0]; this.pps = f.nals[1]; }
        if (!this.sourceBuffer) {
          if (this.sps && this.pps) this._createSourceBuffer();
          if (!this.sourceBuffer) continue;
        }
        // 转 AVCC：每个 NAL 前加 4 字节长度
        const parts = [];
        for (const nal of f.nals) parts.push(u32(nal.length), nal);
        const avcc = concatBytes.apply(null, parts);
        // 真实帧间隔
        const now = performance.now();
        if (this._lastFrameTs) {
          const ms = Math.max(16, Math.min(250, now - this._lastFrameTs));
          this.duration = Math.round(ms * 90); // 90000/1000 = 90
        }
        this._lastFrameTs = now;
        this._append(makeMediaSegment([{ key: f.key, data: avcc }], this.dts, this.duration, this.seqNum++));
        this.dts += this.duration;
      }
      this._drain();
    }
    setPaused(p) {
      this.paused = p;
      if (this.video) { if (p) this.video.pause(); else { try { this.video.play(); } catch (e) {} } }
    }
    reset() {
      this.parser = new AnnexBParser();
      this.queue = [];
      if (this.sourceBuffer && !this.sourceBuffer.updating) {
        try { this.sourceBuffer.remove(0, Infinity); } catch (e) {}
      }
    }
    destroy() {
      this.destroyed = true;
      try { if (this.video) this.video.pause(); } catch (e) {}
      try { if (this.video) this.video.src = ''; } catch (e) {}
      try { if (this.video && this.video.parentNode) this.video.parentNode.removeChild(this.video); } catch (e) {}
    }
  }

  // ==================== 能力检测 ====================
  function isIOS() {
    return /iPhone|iPad|iPod/i.test(navigator.userAgent) ||
      (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  }

  function detectMode() {
    if (isIOS()) {
      // iOS：优先 MSE（WebCodecs 对 H.264 不可靠）
      if (typeof ManagedMediaSource !== 'undefined' || typeof MediaSource !== 'undefined') return 'mse';
      return 'none';
    }
    if (typeof VideoDecoder !== 'undefined' && typeof EncodedVideoChunk !== 'undefined') return 'webcodecs';
    if (typeof MediaSource !== 'undefined' || typeof ManagedMediaSource !== 'undefined') return 'mse';
    return 'none';
  }

  function createRenderer(container, opts) {
    const mode = (opts && opts.mode) || detectMode();
    if (mode === 'webcodecs') return new WebCodecsRenderer(container, opts);
    return new MseRenderer(container, opts);
  }

  global.mytH264 = {
    detectMode: detectMode,
    createRenderer: createRenderer,
    parseSps: parseSps,
    AnnexBParser: AnnexBParser,
  };
})(window);
