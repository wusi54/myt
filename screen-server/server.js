const WebSocket = require('ws');
const { spawn, execSync } = require('child_process');
const { createServer } = require('http');

const PORT = 9000;
const MAX_STREAMS = 10; // 最多同时跑 10 路 scrcpy 进程
const ADB_BASE_PORT = 5555;

const activeStreams = {}; // { containerName: { proc, wsClients: Set } }

function adbConnect(ip) {
  try {
    execSync(`adb connect ${ip}:${ADB_BASE_PORT}`, { timeout: 5000, stdio: 'pipe' });
    return `${ip}:${ADB_BASE_PORT}`;
  } catch (e) {
    return null;
  }
}

function stopStream(name) {
  const entry = activeStreams[name];
  if (!entry) return;
  try { entry.proc.kill('SIGTERM'); } catch {}
  delete activeStreams[name];
}

const wss = new WebSocket.Server({ port: PORT });

wss.on('connection', (ws, req) => {
  const url = new URL(req.url, `http://localhost:${PORT}`);
  const name = url.searchParams.get('name');
  const ip = url.searchParams.get('ip');

  if (!name || !ip) {
    ws.close(4000, 'missing name or ip');
    return;
  }

  // 如果该容器已有流，复用现有的 scrcpy 进程
  if (activeStreams[name]) {
    activeStreams[name].wsClients.add(ws);
    ws.on('close', () => {
      activeStreams[name]?.wsClients.delete(ws);
      if (activeStreams[name]?.wsClients.size === 0) {
        stopStream(name);
      }
    });
    return;
  }

  // ADB 连接容器
  const serial = adbConnect(ip);
  if (!serial) {
    ws.close(4001, `adb connect failed: ${ip}:${ADB_BASE_PORT}`);
    return;
  }

  // 启动 scrcpy
  const proc = spawn('scrcpy', [
    '--no-window',
    '--output-format=h264',
    '--max-size=480',
    '--bit-rate=500K',
    '--max-fps=10',
    '--serial', serial
  ]);

  const clients = new Set([ws]);
  activeStreams[name] = { proc, wsClients: clients };

  proc.stdout.on('data', (chunk) => {
    for (const client of clients) {
      if (client.readyState === WebSocket.OPEN) {
        try { client.send(chunk); } catch {}
      }
    }
  });

  proc.stderr.on('data', (data) => {
    const msg = data.toString();
    if (msg.includes('ERROR')) {
      console.error(`[${name}] ERROR: ${msg}`);
    }
  });

  proc.on('close', (code) => {
    console.log(`[${name}] scrcpy exited code=${code}`);
    for (const client of clients) {
      try { client.close(4002, 'stream ended'); } catch {}
    }
    delete activeStreams[name];
  });

  ws.on('close', () => {
    clients.delete(ws);
    if (clients.size === 0) {
      stopStream(name);
    }
  });

  ws.on('message', (msg) => {
    // 接收前端发来的命令（如调整画质、重启等）
    try {
      const cmd = JSON.parse(msg.toString());
      if (cmd.action === 'restart') {
        stopStream(name);
        ws.close(4003, 'restarting');
      }
    } catch {}
  });
});

// 健康检查
const httpServer = createServer((req, res) => {
  if (req.url === '/health') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({
      status: 'ok',
      streams: Object.keys(activeStreams).length,
      maxStreams: MAX_STREAMS
    }));
  } else if (req.url === '/streams') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(Object.keys(activeStreams)));
  } else {
    res.writeHead(404);
    res.end();
  }
});
httpServer.listen(PORT + 1);

console.log(`Screen server started on ws://0.0.0.0:${PORT}`);
console.log(`HTTP health check on http://0.0.0.0:${PORT + 1}/health`);