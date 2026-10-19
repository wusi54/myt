package main

import (
	"encoding/binary"
	"flag"
	"log"
	"os"
	"os/signal"
	"syscall"
	"time"
)

func main() {
	relayURL := flag.String("relay", "", "MediaMTX RTSP push URL")
	maxSize := flag.Int("max-size", 720, "max size")
	bitrate := flag.Int("bitrate", 1000000, "bitrate")
	jarPath := flag.String("jar", "/data/local/tmp/scrcpy-server.jar", "jar path")
	transport := flag.String("transport", "tcp", "RTSP transport: tcp or udp")
	flag.Parse()

	if *relayURL == "" {
		*relayURL = os.Getenv("RELAY_URL")
	}
	if *relayURL == "" {
		log.Fatal("must specify -relay or RELAY_URL")
	}

	log.SetFlags(log.LstdFlags | log.Lmicroseconds)
	log.Printf("[agent] starting, push=%s max_size=%d bitrate=%d transport=%s", *relayURL, *maxSize, *bitrate, *transport)

	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)

	for {
		select {
		case <-sig:
			log.Printf("[agent] signal received, exiting")
			return
		default:
		}

		runOnce(*relayURL, *maxSize, *bitrate, *jarPath, *transport)
		log.Printf("[agent] session ended, restarting scrcpy-server immediately...")
		// 不需要 sleep，立即重启 scrcpy-server
	}
}

func runOnce(relayURL string, maxSize, bitrate int, jarPath, transport string) {
	server := &ScrcpyServer{
		JarPath: jarPath,
		MaxSize: maxSize,
		Bitrate: bitrate,
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

	buf := make([]byte, 0)
	tmp := make([]byte, 64*1024)
	for {
		n, err := reader.Read(tmp)
		if err != nil {
			log.Printf("[agent] read frame error: %v, restarting scrcpy-server...", err)
			return
		}
		if n > 0 {
			buf = append(buf, tmp[:n]...)
			buf = pumpFrame(buf, streamer)
		}
	}
}

func pumpFrame(buf []byte, streamer *RTPStreamer) []byte {
	for len(buf) >= 12 {
		val := binary.BigEndian.Uint64(buf[0:8])
		size := binary.BigEndian.Uint32(buf[8:12])
		if size == 0 {
			buf = buf[12:]
			continue
		}
		if size > 8*1024*1024 {
			log.Printf("[agent] abnormal frame size=%d, dropping buffer", size)
			return nil
		}
		if len(buf) < 12+int(size) {
			break
		}
		payload := buf[12 : 12+int(size)]
		buf = buf[12+int(size):]

		config := (val >> 63) & 1
		if config == 1 {
			nals := splitAnnexB(payload)
			streamer.UpdateSPSPPS(nals)
			if err := streamer.PushFrame(payload); err != nil {
				log.Printf("[agent] push frame failed: %v", err)
			}
			continue
		}

		if err := streamer.PushFrame(payload); err != nil {
			log.Printf("[agent] push frame failed: %v", err)
		}
	}
	return buf
}
