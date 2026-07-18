import asyncio
import subprocess

WS_PORT = 9000
ADB = r"C:\Users\wusi0\AppData\Local\Android\Sdk\platform-tools\adb.exe"
SCREEN_LIMIT = 180
active_streams = {}

async def adb_heartbeat(adb_addr):
    while True:
        await asyncio.sleep(30)
        try:
            subprocess.run([ADB, 'connect', adb_addr],
                           capture_output=True, timeout=5)
        except:
            pass

async def stream_worker(ws, adb_addr):
    r = subprocess.run([ADB, 'connect', adb_addr], capture_output=True, timeout=10)
    out = r.stdout.decode(errors='replace')
    if 'connected' not in out and 'already' not in out:
        print(f'[stream] adb connect failed: {out}', flush=True)
        await ws.close(4002, f'adb connect failed: {out}')
        return
    print(f'[stream] adb connected: {adb_addr}', flush=True)

    heartbeat = asyncio.create_task(adb_heartbeat(adb_addr))
    restart_count = 0

    try:
        while True:
            print(f'[stream] {adb_addr} starting screenrecord (attempt {restart_count + 1})', flush=True)
            proc = await asyncio.create_subprocess_exec(
                ADB, '-s', adb_addr, 'exec-out',
                'screenrecord', '--output-format=h264',
                '--size', '480x720',
                '--bit-rate', '500000',
                '--time-limit', str(SCREEN_LIMIT), '-',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            active_streams[adb_addr] = proc

            ws_alive = True
            try:
                while True:
                    chunk = await proc.stdout.read(65536)
                    if not chunk:
                        print(f'[stream] {adb_addr} stdout EOF, stream ended', flush=True)
                        break
                    await ws.send(chunk)
            except Exception as e:
                ws_alive = False
                print(f'[stream] {adb_addr} send error: {e}', flush=True)

            try:
                proc.kill()
            except:
                pass

            if not ws_alive:
                print(f'[stream] {adb_addr} client disconnected', flush=True)
                break

            await proc.wait()
            err = await proc.stderr.read()
            if err:
                print(f'[stream] {adb_addr} stderr: {err.decode(errors="replace")}', flush=True)

            restart_count += 1
            print(f'[stream] {adb_addr} screenrecord ended, restarting in 1s...', flush=True)
            await asyncio.sleep(1)

    except Exception as e:
        print(f'[stream] {adb_addr} error: {e}', flush=True)
    finally:
        heartbeat.cancel()
        active_streams.pop(adb_addr, None)

async def handler(ws):
    from urllib.parse import urlparse, parse_qs
    path = ws.request.path if hasattr(ws, 'request') else '/'
    print(f'[relay] new connection, path={path}, type={type(ws).__name__}', flush=True)
    qs = parse_qs(urlparse(path).query)
    adb_addr = (qs.get('adb_addr') or [''])[0]
    if not adb_addr:
        print(f'[relay] missing adb_addr, closing', flush=True)
        await ws.close(4000, 'missing adb_addr')
        return
    if adb_addr in active_streams:
        print(f'[relay] {adb_addr} already streaming, closing', flush=True)
        await ws.close(4001, 'already streaming')
        return
    print(f'[relay] starting stream for {adb_addr}', flush=True)
    await stream_worker(ws, adb_addr)

async def main():
    from websockets.asyncio.server import serve
    print(f'[relay] Starting on ws://0.0.0.0:{WS_PORT}', flush=True)
    async with serve(handler, '0.0.0.0', WS_PORT, ping_interval=20, ping_timeout=10):
        await asyncio.Future()

if __name__ == '__main__':
    asyncio.run(main())
