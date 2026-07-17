import asyncio
import subprocess
import json
import signal
import sys
import os

ADB_PORT = 5555
WS_PORT = 9000
MAX_STREAMS = 10

active_streams = {}

def adb_connect(ip):
    try:
        subprocess.run(
            ['adb', 'connect', f'{ip}:{ADB_PORT}'],
            capture_output=True, timeout=10
        )
        return f'{ip}:{ADB_PORT}'
    except Exception as e:
        return None

async def stream_scrcpy(ws, name, ip):
    serial = adb_connect(ip)
    if not serial:
        await ws.close(4001, f'adb connect failed: {ip}:{ADB_PORT}')
        return

    proc = await asyncio.create_subprocess_exec(
        'scrcpy',
        '--no-window',
        '--output-format=h264',
        '--max-size=480',
        '--bit-rate=500K',
        '--max-fps=10',
        '--serial', serial,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    active_streams[name] = proc

    async def read_stdout():
        while True:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                break
            try:
                await ws.send(chunk)
            except:
                break

    async def read_stderr():
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            msg = line.decode(errors='replace').strip()
            if msg and ('ERROR' in msg or 'error' in msg):
                print(f'[scrcpy:{name}] {msg}')

    await asyncio.gather(read_stdout(), read_stderr())
    await proc.wait()
    active_streams.pop(name, None)

async def ws_handler(ws, path):
    try:
        query = {}
        if '?' in path:
            for p in path.split('?', 1)[1].split('&'):
                if '=' in p:
                    k, v = p.split('=', 1)
                    query[k] = v

        name = query.get('name', '')
        ip = query.get('ip', '')

        if not name or not ip:
            await ws.close(4000, 'missing name or ip')
            return

        if len(active_streams) >= MAX_STREAMS:
            await ws.close(4005, 'max streams reached')
            return

        print(f'[stream] start: {name} ({ip})')
        await stream_scrcpy(ws, name, ip)
        print(f'[stream] end: {name}')
    except Exception as e:
        print(f'[stream] error: {e}')

async def main():
    import websockets

    print(f'Screen relay starting on ws://0.0.0.0:{WS_PORT}')

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: stop.set() if not stop.is_set() else None)
        except:
            pass

    async with websockets.serve(ws_handler, '0.0.0.0', WS_PORT):
        await stop.wait()

    for name, proc in list(active_streams.items()):
        try: proc.kill()
        except: pass
    print('Screen relay stopped')

if __name__ == '__main__':
    asyncio.run(main())