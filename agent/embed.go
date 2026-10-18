package main

import (
	_ "embed"
	"os"
)

//go:embed scrcpy-server.jar
var scrcpyJarData []byte

// ensureScrcpyJar 确保设备上存在 scrcpy-server.jar，不存在则从嵌入数据中提取。
func ensureScrcpyJar(devicePath string) error {
	// 检查文件是否已存在且大小匹配
	info, err := os.Stat(devicePath)
	if err == nil && info.Size() == int64(len(scrcpyJarData)) {
		return nil // 已存在且大小匹配，跳过
	}
	// 写入文件
	f, err := os.OpenFile(devicePath, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0755)
	if err != nil {
		return err
	}
	defer f.Close()
	_, err = f.Write(scrcpyJarData)
	return err
}