# -*- coding: utf-8 -*-
"""云phone设备管理平台 V2.1.0

模块：
  1. 设备管理   盒子/主机设备（ip:port + API url），增删改查 + 端口扫描
  2. 容器管理   自动识别容器实例，增删改查 + 导入真机参数/配置代理/安装APK
  3. 真机参数库 文件内容MD5去重，状态管理，批量导入/操作
  4. 代理池     socks5 代理，状态锁定防止多实例占用
  5. 登录鉴权   JWT 登录、两级角色（管理员/普通用户）、用户管理
"""
import os
import re
import io
import json
import time
import random
import uuid
import socket
import hashlib
import sqlite3
import threading
import subprocess

ADB = os.environ.get('ADB_PATH') or 'adb'
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

from flask import Flask, request, jsonify, render_template, send_file, redirect, g

import db
import auth
import myt_client
from myt_client import MytClient, normalize_base

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.environ.get('UPLOAD_DIR') or os.path.join(BASE_DIR, 'uploads')          # 真机参数文件
APP_UPLOAD_DIR = os.environ.get('APP_UPLOAD_DIR') or os.path.join(BASE_DIR, 'app_uploads')   # APK
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(APP_UPLOAD_DIR, exist_ok=True)

app = Flask(__name__)
app.config['JSON_AS_ASCII'] = False
app.config['MAX_CONTENT_LENGTH'] = 512 * 1024 * 1024
app.config['TEMPLATES_AUTO_RELOAD'] = True   # 模板改动免重启即时生效(waitress 非 debug 模式默认缓存模板)
app.secret_key = auth.SECRET_KEY

db.init_db()
auth.ensure_default_admin()


# ==================== 登录鉴权 ====================
# 无需登录即可访问的路径
PUBLIC_PATHS = {'/login', '/healthz', '/api/auth/login'}


@app.before_request
def _auth_guard():
    p = request.path
    if p.startswith('/static/') or p == '/favicon.ico':
        return None
    if p in PUBLIC_PATHS:
        return None
    user = auth.current_user()
    if not user:
        # API 返回 401，页面跳转到登录页
        if p.startswith('/api/'):
            return jsonify({'code': 401, 'msg': '未登录或登录已过期', 'data': None}), 401
        return redirect('/login')
    g.user = user
    return None


def _app_version():
    """读取 VERSION 文件 + git commit，用于部署版本跟踪。"""
    ver = 'dev'
    try:
        with open(os.path.join(BASE_DIR, 'VERSION')) as f:
            ver = f.read().strip() or ver
    except Exception:
        pass
    commit = ''
    try:
        import subprocess as _sp
        commit = _sp.check_output(['git', 'rev-parse', '--short', 'HEAD'],
                                  cwd=BASE_DIR, stderr=_sp.DEVNULL).decode().strip()
    except Exception:
        pass
    return ver, commit


@app.route('/healthz')
def healthz():
    ver, commit = _app_version()
    return jsonify({'code': 200, 'msg': 'ok', 'version': ver, 'commit': commit})


@app.route('/api/version')
def api_version():
    ver, commit = _app_version()
    return ok({'version': ver, 'commit': commit, 'service': 'web'})


@app.route('/login')
def login_page():
    if auth.current_user():
        return redirect('/')
    return render_template('login.html')


@app.route('/api/auth/login', methods=['POST'])
def api_auth_login():
    d = request.json or {}
    username = (d.get('username') or '').strip()
    password = d.get('password') or ''
    if not username or not password:
        return err('请输入用户名和密码')
    u = db.get_user_by_username(username)
    if not u or not auth.verify_password(password, u['password_hash']):
        return err('用户名或密码错误', 401)
    if u.get('status') != 'active':
        return err('账号已被禁用，请联系管理员', 403)
    db.update_user(u['id'], last_login=db.now())
    u = db.get_user(u['id'])
    token = auth.make_token(u)
    resp = jsonify({'code': 200, 'msg': '登录成功',
                    'data': {'token': token, 'user': auth.public_user(u)}})
    resp.set_cookie(auth.COOKIE_NAME, token, max_age=auth.TOKEN_TTL,
                    httponly=True, samesite='Lax', path='/')
    return resp


@app.route('/api/auth/logout', methods=['POST'])
def api_auth_logout():
    resp = jsonify({'code': 200, 'msg': '已退出登录', 'data': None})
    resp.delete_cookie(auth.COOKIE_NAME, path='/')
    return resp


@app.route('/api/auth/me')
def api_auth_me():
    return ok(g.user)


@app.route('/api/auth/password', methods=['POST'])
def api_auth_password():
    d = request.json or {}
    old = d.get('old_password') or ''
    new = d.get('new_password') or ''
    if len(new) < 6:
        return err('新密码至少 6 位')
    u = db.get_user(g.user['id'])
    if not auth.verify_password(old, u['password_hash']):
        return err('原密码错误')
    db.update_user(u['id'], password_hash=auth.hash_password(new))
    return ok(None, '密码已修改')


# ==================== 用户管理（仅管理员） ====================
def _clean_user(u):
    if not u:
        return None
    return {k: v for k, v in u.items() if k != 'password_hash'}


@app.route('/api/users', methods=['GET'])
@auth.admin_required
def api_users():
    return ok([_clean_user(u) for u in db.list_users()])


@app.route('/api/users', methods=['POST'])
@auth.admin_required
def api_user_add():
    d = request.json or {}
    username = (d.get('username') or '').strip()
    password = d.get('password') or ''
    role = d.get('role') or 'user'
    display_name = (d.get('display_name') or '').strip()
    if not username:
        return err('用户名不能为空')
    if not re.match(r'^[A-Za-z0-9_.@-]{2,32}$', username):
        return err('用户名需为 2-32 位字母/数字/_.@-')
    if len(password) < 6:
        return err('密码至少 6 位')
    if role not in ('admin', 'user'):
        role = 'user'
    if db.get_user_by_username(username):
        return err('用户名已存在')
    uid = db.add_user(username, auth.hash_password(password), role, display_name)
    return ok(_clean_user(db.get_user(uid)), '已创建用户')


@app.route('/api/users/<int:uid>', methods=['PUT'])
@auth.admin_required
def api_user_update(uid):
    u = db.get_user(uid)
    if not u:
        return err('用户不存在', 404)
    d = request.json or {}
    fields = {}
    if 'display_name' in d:
        fields['display_name'] = (d.get('display_name') or '').strip()
    if 'role' in d and d.get('role') in ('admin', 'user'):
        fields['role'] = d['role']
    if 'status' in d and d.get('status') in ('active', 'disabled'):
        fields['status'] = d['status']
    if d.get('password'):
        if len(d['password']) < 6:
            return err('密码至少 6 位')
        fields['password_hash'] = auth.hash_password(d['password'])
    # 保护：不能移除最后一个可用管理员
    will_lose_admin = (fields.get('role') == 'user' or fields.get('status') == 'disabled')
    if u.get('role') == 'admin' and u.get('status') == 'active' and will_lose_admin:
        if db.count_active_admins(exclude_id=uid) == 0:
            return err('至少需要保留一个可用的管理员账号')
    db.update_user(uid, **fields)
    return ok(_clean_user(db.get_user(uid)), '已更新')


@app.route('/api/users/<int:uid>', methods=['DELETE'])
@auth.admin_required
def api_user_delete(uid):
    if uid == g.user['id']:
        return err('不能删除当前登录账号')
    u = db.get_user(uid)
    if not u:
        return err('用户不存在', 404)
    if u.get('role') == 'admin' and db.count_active_admins(exclude_id=uid) == 0:
        return err('至少需要保留一个可用的管理员账号')
    db.delete_user(uid)
    return ok(None, '已删除')


# ==================== 工具 ====================
def ok(data=None, msg='ok'):
    return jsonify({'code': 200, 'msg': msg, 'data': data})


def err(msg, code=400, data=None):
    return jsonify({'code': code, 'msg': msg, 'data': data})


def client_for(dev):
    """根据设备配置构造 API 客户端。"""
    base = normalize_base(dev.get('url'), dev.get('ip'), dev.get('port'))
    return MytClient(base)


def adb_host_for(dev):
    """投屏/ADB 连接使用的主机。

    设备配置 screen_net='lan'(内网) 时直接使用设备 IP（如 192.168.2.40）；
    默认 screen_net='wan'(外网) 时优先取 API url 的 host（网关/公网映射），
    取不到时回退设备 ip。
    """
    if (dev.get('screen_net') or 'wan').lower() == 'lan':
        return (dev.get('ip') or '')
    base = normalize_base(dev.get('url'), dev.get('ip'), dev.get('port'))
    try:
        host = urlparse(base).hostname
    except Exception:
        host = None
    return host or (dev.get('ip') or '')


def migrate_adb_addrs():
    """按设备 url 修正历史容器的 adb_addr（幂等，随启动执行）。"""
    devs = {d['id']: d for d in db.list_devices()}
    changed = 0
    for c in db.list_containers():
        dev = devs.get(c.get('device_id'))
        port = c.get('adb_port') or ''
        if not dev or not port:
            continue
        new = f"{adb_host_for(dev)}:{port}"
        if new != (c.get('adb_addr') or ''):
            db.update_container(c['id'], adb_addr=new)
            changed += 1
    return changed


def device_or_404(dev_id):
    dev = db.get_device(dev_id)
    return dev


def container_or_404(cid):
    return db.get_container(cid)


# ==================== 设备管理 ====================
@app.route('/api/devices', methods=['GET'])
def api_devices():
    return ok(db.list_devices())


@app.route('/api/devices', methods=['POST'])
def api_device_add():
    d = request.json or {}
    ip = (d.get('ip') or '').strip()
    if not ip:
        return err('ip 不能为空')
    port = str(d.get('port') or '83').strip()
    if db.row('SELECT id FROM devices WHERE ip=? AND port=?', (ip, port)):
        return err('设备已存在')
    dev_id = db.add_device(ip, port, (d.get('url') or '').strip(),
                           (d.get('name') or '').strip(), (d.get('remark') or '').strip(),
                           (d.get('screen_net') or 'wan').strip().lower(),
                           int(d.get('max_containers') or 0))
    return ok(db.get_device(dev_id), '已添加')


@app.route('/api/devices/<int:dev_id>', methods=['PUT'])
def api_device_update(dev_id):
    if not db.get_device(dev_id):
        return err('设备不存在', 404)
    d = request.json or {}
    fields = {}
    for k in ('ip', 'port', 'url', 'name', 'remark', 'status', 'screen_net', 'enabled', 'max_containers'):
        if k in d:
            fields[k] = (d[k] or '').strip() if isinstance(d[k], str) else d[k]
    if ('ip' in fields) or ('port' in fields):
        cur = db.get_device(dev_id)
        nip = fields.get('ip', cur['ip'])
        nport = str(fields.get('port', cur['port']))
        dup = db.row('SELECT id FROM devices WHERE ip=? AND port=? AND id<>?', (nip, nport, dev_id))
        if dup:
            return err('已存在相同 IP:端口 的设备')
    db.update_device(dev_id, **fields)
    return ok(db.get_device(dev_id), '已更新')


@app.route('/api/devices/<int:dev_id>/toggle', methods=['POST'])
def api_device_toggle(dev_id):
    dev = db.get_device(dev_id)
    if not dev:
        return err('设备不存在', 404)
    new = 0 if dev['enabled'] else 1
    db.update_device(dev_id, enabled=new)
    return ok(db.get_device(dev_id), '已启用' if new else '已禁用')


@app.route('/api/devices/<int:dev_id>', methods=['DELETE'])
def api_device_delete(dev_id):
    for c in db.list_containers():
        if c['device_id'] == dev_id:
            _release_container_proxy(c)
    db.delete_device(dev_id)
    db.execute('DELETE FROM containers WHERE device_id=?', (dev_id,))
    return ok(None, '已删除')


@app.route('/api/devices/<int:dev_id>/probe', methods=['POST'])
def api_device_probe(dev_id):
    dev = device_or_404(dev_id)
    if not dev:
        return err('设备不存在', 404)
    cli = client_for(dev)
    res = cli.list_containers(dev['ip'])
    status = 'online' if res.get('code') == 200 else 'offline'
    count = len(res.get('data') or []) if status == 'online' else 0
    host_ver = ''
    if status == 'online':
        hv = cli.host_ver(dev['ip'])
        host_ver = hv.get('data') or hv.get('message') or ''
    db.update_device(dev_id, status=status, container_count=count, host_ver=str(host_ver))
    if status == 'offline':
        # 设备离线时，将名下容器状态置为 offline
        for c in db.list_containers():
            if c['device_id'] == dev_id:
                db.update_container(c['id'], state='offline', status='offline')
    db.add_operation_log({'op_type': 'adb_probe', 'target': dev['ip'], 'status': status,
                          'detail': '容器数 ' + str(count)})
    return ok(db.get_device(dev_id))


@app.route('/api/devices/probe_all', methods=['POST'])
def api_device_probe_all():
    results = []
    for dev in db.list_devices():
        r = api_device_probe(dev['id'])
        results.append({'id': dev['id'], 'ip': dev['ip'], 'status': db.get_device(dev['id'])['status']})
    return ok(results)


def _expand_ips(subnet):
    subnet = (subnet or '').strip()
    ips = []
    if '/' in subnet:
        base, bits = subnet.split('/')
        bits = int(bits)
        parts = base.split('.')
        prefix = int(''.join(f'{int(p):08b}' for p in parts))
        host_bits = 32 - bits
        start = prefix & (~((1 << host_bits) - 1))
        for i in range(1, (1 << host_bits) - 1):
            v = start + i
            ips.append('.'.join(str((v >> s) & 255) for s in (24, 16, 8, 0)))
    elif '-' in subnet:
        base = subnet.rsplit('.', 1)[0]
        a, b = subnet.rsplit('.', 1)[1].split('-')
        for i in range(int(a), int(b) + 1):
            ips.append(f'{base}.{i}')
    else:
        ips.append(subnet)
    return ips


def _tcp_open(ip, port, timeout=0.8):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((ip, port))
        return True
    except Exception:
        return False
    finally:
        s.close()


@app.route('/api/devices/scan', methods=['POST'])
def api_device_scan():
    """通过 TCP 扫描指定网段的 83 端口，识别云phone设备。"""
    d = request.json or {}
    subnet = d.get('subnet') or ''
    port = int(d.get('port') or 83)
    if not subnet:
        return err('请填写网段，如 192.168.2.0/24')
    ips = _expand_ips(subnet)
    if len(ips) > 4096:
        return err('网段过大，最多 4096 个地址')
    existing = {(x['ip'], x['port']) for x in db.list_devices()}
    found = []

    def check(ip):
        return ip if _tcp_open(ip, port) else None

    with ThreadPoolExecutor(max_workers=128) as ex:
        futs = {ex.submit(check, ip): ip for ip in ips}
        for fu in as_completed(futs):
            ip = fu.result()
            if ip:
                found.append(ip)
    found.sort(key=lambda x: tuple(int(n) for n in x.split('.')))
    result = []
    for ip in found:
        cli = MytClient(f'http://{ip}:{port}')
        q = cli.query_myt()
        code = ''
        if q.get('code') == 200 and isinstance(q.get('data'), dict):
            vals = list(q['data'].values())
            code = vals[0] if vals else ''
        result.append({'ip': ip, 'port': str(port), 'device_code': code,
                       'exist': (ip, str(port)) in existing})
    return ok(result)


@app.route('/api/devices/discover', methods=['POST'])
def api_device_discover():
    """通过一个已知的 API 地址调用 query_myt 自动发现同网段设备。"""
    d = request.json or {}
    url = (d.get('url') or '').strip()
    if not url:
        return err('请填写 API 地址，如 http://113.105.100.58:83')
    cli = MytClient(normalize_base(url))
    res = cli.query_myt()
    if res.get('code') != 200:
        return err(res.get('msg') or res.get('message') or '发现失败', 500, res)
    data = res.get('data') or {}
    existing = {(x['ip'], x['port']) for x in db.list_devices()}
    items = []
    if isinstance(data, dict):
        for ip, code in data.items():
            ip_clean = ip.split('/')[0]
            items.append({'ip': ip_clean, 'port': '83', 'device_code': code, 'url': normalize_base(url),
                          'exist': (ip_clean, '83') in existing})
    return ok({'url': normalize_base(url), 'items': items})


@app.route('/api/devices/import', methods=['POST'])
def api_device_import():
    d = request.json or {}
    items = d.get('items') or []
    added = 0
    for it in items:
        ip = (it.get('ip') or '').strip()
        if not ip:
            continue
        port = str(it.get('port') or '83')
        if db.row('SELECT id FROM devices WHERE ip=? AND port=?', (ip, port)):
            continue
        db.add_device(ip, port, (it.get('url') or '').strip(), (it.get('name') or '').strip())
        added += 1
    return ok({'added': added}, f'已导入 {added} 台设备')


@app.route('/api/containers/probe_screen', methods=['POST'])
def api_containers_probe_screen():
    """逐个容器探测投屏能力：用 adb 实际连接判断运行中容器是否可投屏（写回 screen_status）。

    注意：设备端若配置了全局出向代理(domain_mode=2)，会劫持 adb 回传/控制流，
    导致 adb 显示 offline —— 此时投屏确实不可用（需排除 adb 流量或改用 WebRTC 通道）。
    """
    d = request.json or {}
    dev_id = d.get('device_id')
    cons = db.list_containers()
    if dev_id:
        cons = [c for c in cons if str(c.get('device_id')) == str(dev_id)]
    targets = [c for c in cons if c.get('adb_addr') and str(c.get('state', '')).lower() == 'running']
    results = {}

    # 先并行对各个 adb_addr 发起 connect（已连接的会返回 already connected；离线/配置代理设备保持 offline），
    # 再统一用 devices 取状态。并行 + 短超时避免逐个离线设备串行等待(可达数十秒)。
    addrs = sorted({c['adb_addr'] for c in targets})
    if addrs:
        with ThreadPoolExecutor(max_workers=min(16, len(addrs))) as ex:
            futs = [ex.submit(subprocess.run, [ADB, 'connect', a], capture_output=True, timeout=5) for a in addrs]
            for f in futs:
                try:
                    f.result(timeout=5)
                except Exception:
                    pass
    states = {}
    try:
        out = subprocess.run([ADB, 'devices'], capture_output=True, timeout=20).stdout.decode(errors='replace')
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2:
                states[parts[0]] = parts[1]
    except Exception:
        pass

    for c in targets:
        st = states.get(c['adb_addr'])
        screen = 'ok' if st == 'device' else ('fail' if st in ('offline', 'unauthorized', 'unknown') else '')
        try:
            db.update_container(c['id'], screen_status=screen)
        except Exception:
            pass
        results[c['id']] = screen
    return ok(results, f'已检测 {len(results)} 个运行中的容器')


@app.route('/api/logs', methods=['GET'])
def api_logs():
    t = request.args.get('type') or ''
    kw = request.args.get('kw') or ''
    return ok(db.list_operation_logs(t or None, kw or None))


# ==================== 容器管理 ====================
def _sync_one_device(dev):
    cli = client_for(dev)
    res = cli.list_containers(dev['ip'])
    if res.get('code') != 200:
        db.update_device(dev['id'], status='offline')
        # 设备离线时，将名下容器状态置为 offline，避免残留 running 显示
        for c in db.list_containers():
            if c['device_id'] == dev['id']:
                db.update_container(c['id'], state='offline', status='offline')
        return 0
    # 容器列表接口为 /get/{ip}，返回结构因 myt 固件/版本而异：
    #   - 容器数组优先在 msg 字段下（/get 返回 {code,msg:[...]}），也兼容 data；
    #   - 实例名在 name 或 Names；状态在 state 或 State；data 路径在 data 或 Data。
    data = res.get('msg') or res.get('data') or []
    if not isinstance(data, list):
        data = []
    host_ver = ''
    hv = cli.host_ver(dev['ip'])
    if hv.get('code') == 200:
        host_ver = hv.get('data') or hv.get('message') or ''
    db.update_device(dev['id'], status='online', container_count=len(data),
                     host_ver=str(host_ver or ''))
    # 实例名/状态在不同 myt 固件下字段名不一致，做多候选兜底
    ID_KEYS = ('id', 'container_id', 'cid', 'vm_id', 'instance_id', 'InstanceId',
               'containerId', 'short_id', 'ShortId', 'uuid')
    INDEX_KEYS = ('index', 'index_num', 'idx', 'slot', 'num')
    NAME_KEYS = ('Names', 'name', 'container_name', 'vm_name', 'cname',
                 'container', 'containerId')
    STATE_KEYS = ('State', 'state', 'status')
    seen = set()
    for c in data:
        raw_name = ''
        for k in NAME_KEYS:
            v = c.get(k)
            if isinstance(v, str) and v.strip():
                raw_name = v.strip()
                break
        # 主键用稳定槽位 ip:index（容器在设备上的序号，改名不变），避免用会变的 name 导致 id 漂移；
        # 无 index 时才退化为 ip:name。
        # 容器 ID：优先使用 API 返回的 id 字段；无 id 时 fallback 到 ip:index（兼容 /get 接口）
        cid = str(c.get('id') or '').strip()
        if not cid:
            # fallback: ip:index
            idx_val = 0
            for k in INDEX_KEYS:
                v = c.get(k)
                if v is not None and str(v).strip():
                    try:
                        idx_val = int(v)
                        break
                    except (ValueError, TypeError):
                        pass
            cid = f"{dev['ip']}:{idx_val}"
            print(f'[sync] API 未返回 id 字段, fallback to {cid}, device={dev["ip"]}, name={raw_name}', flush=True)
        seen.add(cid)
        adb_port = str(c.get('adb') or '')
        s5 = c.get('s5') or {}
        state = ''
        for k in STATE_KEYS:
            v = c.get(k)
            if isinstance(v, str) and v.strip():
                state = v.strip()
                break
        db.upsert_container({
            'id': cid, 'device_id': dev['id'], 'device_ip': dev['ip'],
            'name': raw_name, 'index_num': c.get('index', 0),
            'short_id': c.get('short_id') or c.get('ShortId') or '',
            'adb_port': adb_port,
            'adb_addr': f"{adb_host_for(dev)}:{adb_port}" if adb_port else '',
            'image': c.get('image') or c.get('Image') or '',
            'state': state,
            'status': c.get('status') or state,
            'and_state': c.get('and_state', 0),
            'api_port': str(c.get('api_port') or c.get('ApiPort') or ''),
            'ctr_port': str(c.get('ctr_port') or c.get('CtrPort') or ''),
            'webrtc': str(c.get('webrtc') or c.get('Webrtc') or ''),
            's5_addr': s5.get('addr', ''),
            's5_status': s5.get('status', 0),
            'resolution': f"{c.get('width', '')}x{c.get('height', '')}",
            'data_path': c.get('data') or c.get('Data') or '',
        })
    # 清理该设备下已消失的容器
    for row in db.list_containers():
        if row['device_id'] == dev['id'] and row['id'] not in seen:
            _release_container_proxy(row)
            db.delete_container(row['id'])
    return len(data)


@app.route('/api/containers/sync', methods=['POST'])
def api_containers_sync():
    d = request.json or {}
    dev_id = d.get('device_id')
    if dev_id:
        dev = db.get_device(dev_id)
        if not dev:
            return err('设备不存在', 404)
        n = _sync_one_device(dev)
        return ok({'count': n}, f'同步完成，共 {n} 个容器')
    total = 0
    for dev in db.list_devices():
        try:
            total += _sync_one_device(dev)
        except Exception:
            pass
    return ok({'count': total}, f'同步完成，共 {total} 个容器')


@app.route('/api/containers', methods=['GET'])
def api_containers():
    items = db.list_containers()
    devs = db.list_devices()
    by_id = {d['id']: (d.get('url') or '') for d in devs}
    by_ip = {d['ip']: (d.get('url') or '') for d in devs}
    net_by_id = {d['id']: (d.get('screen_net') or 'wan') for d in devs}
    for c in items:
        c['device_url'] = by_id.get(c.get('device_id')) or by_ip.get(c.get('device_ip')) or ''
        c['device_screen_net'] = net_by_id.get(c.get('device_id')) or 'wan'
    kw = (request.args.get('kw') or '').lower()
    dev_filter = request.args.get('device_id')
    if dev_filter:
        items = [c for c in items if str(c['device_id']) == str(dev_filter)]
    if kw:
        items = [c for c in items if kw in (c['name'] or '').lower()
                 or kw in (c['device_ip'] or '').lower() or kw in (c['image'] or '').lower()]
    return ok(items)


@app.route('/api/containers/<cid>', methods=['GET'])
def api_container_detail(cid):
    c = db.get_container(cid)
    if not c:
        return err('容器不存在', 404)
    cfg = db.get_container_configs(c['device_ip'], c['name'])
    c['configs'] = cfg
    return ok(c)


@app.route('/api/containers/create', methods=['POST'])
def api_container_create():
    d = request.json or {}
    dev = db.get_device(d.get('device_id'))
    if not dev:
        return err('设备不存在', 404)
    index = d.get('index', 0)
    cli = client_for(dev)
    auto_name = d.get('auto_name', False)
    if auto_name:
        # 自动生成容器名：{devCode}_{index}_T{index padded to 4 digits}
        try:
            si = cli.systeminfo(dev['ip'])
            dev_code = ''
            if si.get('code') == 200:
                dev_code = (si.get('data') or {}).get('devCode') or ''
            if not dev_code:
                dev_code = dev['ip'].replace('.', '')
            name = f"{dev_code}_{index}_T{index:04d}"
        except Exception:
            name = f"container_{index}"
    else:
        name = (d.get('name') or '').strip()
    if not name:
        return err('容器名称不能为空')
    opts = {}
    for k in ('image_addr', 'memory', 'cpu', 'width', 'height', 'dpi',
              'fps', 'mac', 'random_dev', 'rpaport', 'adbport', 'custom_tag', 'sandbox'):
        if d.get(k) not in (None, ''):
            opts[k] = d[k]
    # resolution "WxH" -> width/height (myt API 不接受 resolution 字符串)
    res = (d.get('resolution') or '').strip()
    if res and 'x' in res:
        parts = res.split('x')
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            opts['width'] = parts[0]
            opts['height'] = parts[1]
    res = cli.create(dev['ip'], index, name, **opts)
    if res.get('code') == 200:
        _sync_one_device(dev)
        db.add_operation_log({'op_type': 'container', 'target': name,
                              'detail': '创建容器 (index=' + str(index) + ', device=' + dev['ip'] + ')', 'status': 'success'})
        # 返回容器名，供前端显示
        res['data'] = res.get('data') or {}
        if isinstance(res['data'], dict):
            res['data']['name'] = name
    return jsonify(res)


@app.route('/api/containers/<cid>/rename', methods=['POST'])
def api_container_rename(cid):
    c = db.get_container(cid)
    if not c:
        return err('容器不存在', 404)
    new_name = ((request.json or {}).get('name') or '').strip()
    if not new_name:
        return err('新名称不能为空')
    dev = db.get_device(c['device_id'])
    cli = client_for(dev)
    res = cli.rename(dev['ip'], c['name'], new_name)
    if res.get('code') == 200:
        _sync_one_device(dev)
    return jsonify(res)


def _container_action(cid, action):
    c = db.get_container(cid)
    if not c:
        return err('容器不存在', 404)
    dev = db.get_device(c['device_id'])
    cli = client_for(dev)
    if action == 'remove':
        res = cli.remove(dev['ip'], c['name'])
        if res.get('code') == 200:
            _release_container_proxy(c)
            db.delete_container(cid)
        return jsonify(res)
    fn = getattr(cli, action)
    res = fn(dev['ip'], c['name'])
    if res.get('code') == 200:
        _sync_one_device(dev)
        if action == 'start':
            db.add_operation_log({'op_type': 'container', 'target': c['name'],
                                  'detail': '容器开机 (' + c['device_ip'] + ')', 'status': 'success'})
        elif action == 'stop':
            db.add_operation_log({'op_type': 'container', 'target': c['name'],
                                  'detail': '容器关机 (' + c['device_ip'] + ')', 'status': 'success'})
    return jsonify(res)


@app.route('/api/containers/<cid>/start', methods=['POST'])
def api_container_start(cid):
    return _container_action(cid, 'start')


@app.route('/api/containers/<cid>/stop', methods=['POST'])
def api_container_stop(cid):
    return _container_action(cid, 'stop')


@app.route('/api/containers/<cid>/reset', methods=['POST'])
def api_container_reset(cid):
    return _container_action(cid, 'reset')


@app.route('/api/containers/<cid>/reboot', methods=['POST'])
def api_container_reboot(cid):
    return _container_action(cid, 'reboot')


@app.route('/api/containers/<cid>/remove', methods=['POST'])
def api_container_remove(cid):
    return _container_action(cid, 'remove')


@app.route('/api/containers/<cid>/random_devinfo', methods=['POST'])
def api_container_random_devinfo(cid):
    c = db.get_container(cid)
    if not c:
        return err('容器不存在', 404)
    dev = db.get_device(c['device_id'])
    cli = client_for(dev)
    d = request.json or {}
    res = cli.random_devinfo(dev['ip'], c['name'], **{k: v for k, v in d.items() if v not in (None, '')})
    return jsonify(res)


@app.route('/api/containers/<cid>/apply_fp', methods=['POST'])
def api_container_apply_fp(cid):
    """容器导入真机参数。"""
    fp_id = (request.json or {}).get('fp_id')
    if not fp_id:
        return err('请选择真机参数')
    return _do_apply_fp(fp_id, cid)


def _release_fp(fp_id):
    fp = db.get_real_param(fp_id)
    if not fp:
        return
    db.update_real_param(fp_id, status='idle', in_use=0, instance_id='',
                         instance_name='', last_offline_time=db.now())


@app.route('/api/containers/<cid>/release_fp', methods=['POST'])
def api_container_release_fp(cid):
    c = db.get_container(cid)
    if not c:
        return err('容器不存在', 404)
    if c['fp_id']:
        _release_fp(c['fp_id'])
        db.update_container(cid, fp_id='')
        db.delete_container_config(c['device_ip'], c['name'], 'real_param')
        db.add_operation_log({'op_type': 'realparam', 'target': c['name'], 'detail': '释放真机参数', 'status': 'success'})
    return ok(None, '已释放真机参数')


@app.route('/api/containers/<cid>/apply_proxy', methods=['POST'])
def api_container_apply_proxy(cid):
    proxy_id = (request.json or {}).get('proxy_id')
    if not proxy_id:
        return err('请选择代理')
    return _do_apply_proxy(proxy_id, cid)


def _release_proxy(proxy_id, stop=True):
    p = db.get_proxy(proxy_id)
    if not p:
        return
    if stop and p['instance_id']:
        c = db.get_container(p['instance_id'])
        if c:
            dev = db.get_device(c['device_id'])
            try:
                client_for(dev).s5_stop(dev['ip'], c['name'])
            except Exception:
                pass
    # 故障线路释放实例后保留故障状态（不改变探测结论）；健康线路释放后恢复为空闲
    keep_fault = (p['status'] == 'fault')
    fields = {'status': 'fault' if keep_fault else 'idle', 'in_use': 0,
              'instance_id': '', 'instance_name': ''}
    if not keep_fault:
        fields['last_offline_time'] = db.now()
    db.update_proxy(proxy_id, **fields)


def _release_container_proxy(c):
    """容器被删除/移除前，释放其占用的代理与真机参数，保持代理池/参数池占用一致。"""
    pid = c.get('proxy_id')
    if pid:
        _release_proxy(pid, stop=True)
    fid = c.get('fp_id')
    if fid:
        try:
            db.update_real_param(fid, status='idle', in_use=0, instance_id='')
        except Exception:
            pass


def reconcile_orphan_occupancy():
    """清理代理池/参数池中 instance_id 指向已不存在容器的孤儿占用（历史遗留数据）。"""
    live_ids = {c['id'] for c in db.list_containers()}
    fixed = 0
    for p in db.list_proxies():
        if p['in_use'] and p.get('instance_id') and p['instance_id'] not in live_ids:
            keep_fault = (p['status'] == 'fault')
            db.update_proxy(p['id'], status=('fault' if keep_fault else 'idle'),
                            in_use=0, instance_id='', instance_name='')
            fixed += 1
    for f in db.list_real_params():
        if f['in_use'] and f.get('instance_id') and f['instance_id'] not in live_ids:
            db.update_real_param(f['id'], status='idle', in_use=0, instance_id='')
            fixed += 1
    return fixed


@app.route('/api/containers/<cid>/release_proxy', methods=['POST'])
def api_container_release_proxy(cid):
    c = db.get_container(cid)
    if not c:
        return err('容器不存在', 404)
    if c['proxy_id']:
        _release_proxy(c['proxy_id'], stop=True)
        db.update_container(cid, proxy_id='', s5_addr='', s5_status=0)
        db.delete_container_config(c['device_ip'], c['name'], 'proxy')
        db.add_operation_log({'op_type': 'proxy', 'target': c['name'], 'detail': '释放代理', 'status': 'success'})
    return ok(None, '已释放代理')


@app.route('/api/containers/<cid>/install_apk', methods=['POST'])
def api_container_install_apk(cid):
    c = db.get_container(cid)
    if not c:
        return err('容器不存在', 404)
    app_id = (request.json or {}).get('app_id')
    a = db.get_app(app_id)
    if not a:
        return err('应用不存在', 404)
    apk_path = os.path.join(APP_UPLOAD_DIR, a['filename'])
    if not os.path.exists(apk_path):
        return err('APK 文件不存在', 404)
    dev = db.get_device(c['device_id'])
    cli = client_for(dev)
    log = db.add_install_log({'app_id': app_id, 'app_name': a['name'],
                              'device_ip': c['device_ip'], 'container_name': c['name'],
                              'status': 'installing'})
    status, msg = cli.install_apk(dev['ip'], c['name'], apk_path, a['filename'])
    db.execute('UPDATE install_logs SET status=?,message=? WHERE id=?', (status, msg, log['id']))
    db.add_operation_log({'op_type': 'install', 'target': c['name'],
                          'detail': '安装 ' + a['name'] + ' (' + c['device_ip'] + ')', 'status': status})
    return ok({'status': status, 'message': msg}, '安装完成' if status == 'success' else '安装失败')


@app.route('/api/containers/<cid>/shell', methods=['POST'])
def api_container_shell(cid):
    c = db.get_container(cid)
    if not c:
        return err('容器不存在', 404)
    cmd = (request.json or {}).get('cmd', '')
    if not cmd:
        return err('命令不能为空')
    dev = db.get_device(c['device_id'])
    return jsonify(client_for(dev).shell(dev['ip'], c['name'], cmd))


@app.route('/api/containers/<cid>/s5_query', methods=['GET'])
def api_container_s5_query(cid):
    c = db.get_container(cid)
    if not c:
        return err('容器不存在', 404)
    dev = db.get_device(c['device_id'])
    res = client_for(dev).s5_query(dev['ip'], c['name'])
    return jsonify(res)


# ==================== 真机参数库 ====================
# 机型/型号在真机参数文件里的常见键，按优先级取值。
# 注意：不含 ro.product.vendor.model —— 那是芯片平台代号（如 "Pineapple for arm64"），不是机型。
BRAND_KEYS = ['ro.product.brand', 'ro.product.odm.brand', 'ro.product.vendor.brand',
              'ro.product.system.brand', 'ro.product.manufacturer', 'brand', 'manufacturer']
MODEL_KEYS = ['ro.product.model', 'ro.product.name', 'ro.product.odm.model',
              'ro.product.market.name', 'model', 'product_name', 'product']
_BAD_VAL = ('unknown', 'null', 'none', '')


def _flatten_props(obj):
    """把真机参数文件摊平成 {key: value}。

    实际文件（不管扩展名是 .json/.dat/.bin，内容都是 JSON）长这样：
        {"env":[{"name":"PropRw","description":"ro.product.model","value":"DNP-AN00"}, ...],
         "sysinfo":{...}, "deviceId":"...", ...}
    机型/型号藏在 env 里的 Android 属性中，所以必须把 env 展开，
    否则顶层直接取 brand/model 永远取不到（历史 bug：机型列一直为空）。
    """
    flat = {}
    if not isinstance(obj, dict):
        return flat
    for k, v in obj.items():
        if isinstance(v, (str, int, float)) and not isinstance(v, bool) and k != 'env':
            flat[str(k)] = str(v)
    env = obj.get('env')
    if isinstance(env, list):
        for item in env:
            if not isinstance(item, dict):
                continue
            key = item.get('description') or item.get('name') or ''
            val = item.get('value')
            if key and isinstance(val, (str, int, float)) and not isinstance(val, bool):
                flat[str(key)] = str(val)
    return flat


def _parse_param_content(raw, filename):
    """解析真机参数文件，返回摊平后的 {key: value}。

    按「内容」而不是扩展名判断格式：.dat/.bin 实际也是 JSON，
    之前只认 .json/.txt，导致批量导入的 .dat 全部解析成空字典。
    """
    text = raw.decode('utf-8', 'ignore').strip() if isinstance(raw, (bytes, bytearray)) else (raw or '')
    # 1) JSON（任何扩展名都先试）
    if text[:1] in ('{', '['):
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                flat = _flatten_props(obj)
                if flat:
                    return flat
        except Exception:
            pass
    # 2) key=value 文本（支持 ro.product.model=DNP-AN00 这类属性行）
    data = {}
    for line in text.splitlines():
        line = line.strip().rstrip(',').strip()
        if '=' in line:
            k, v = line.split('=', 1)
            k = k.strip().strip('"').strip()
            v = v.strip().strip('"').rstrip(',').strip()
            if k:
                data[k] = v
    return data


def _enrich_from_data(data):
    """从摊平后的属性里取机型(brand)/型号(model)。"""
    def pick(keys):
        for k in keys:
            v = str(data.get(k) or '').strip()
            if v and v.lower() not in _BAD_VAL:
                return v
        return ''
    return pick(BRAND_KEYS), pick(MODEL_KEYS)


def _read_param_file(p):
    """读取参数记录落盘的文件内容（用于重新识别机型）。"""
    try:
        files = json.loads(p.get('files') or '[]')
    except Exception:
        return b''
    for f in files:
        path = (f or {}).get('path') or ''
        if path and os.path.exists(path):
            try:
                with open(path, 'rb') as r:
                    return r.read()
            except Exception:
                continue
    return b''


def _build_devinfo_body(fp):
    """构造 set_custom_devinfo 的请求体。

    myt 的 set_custom_devinfo 需要「完整指纹 JSON」(env/sensors/cpuinfo/... 结构)。
    而 fp['data'] 是 _flatten_props 摊平后的 {属性:值} dict，直接发会触发 myt code 3
    「自定义设备信息设置失败」。因此优先用导入时落盘的原始指纹文件(保留完整结构)；
    手动新增(无文件)时才退化为扁平字段。
    """
    raw = _read_param_file(fp)
    if raw:
        text = raw.decode('utf-8', 'ignore').strip()
        if text[:1] in ('{', '['):
            try:
                return json.loads(text)
            except Exception:
                pass
    return json.loads(fp.get('data') or '{}')


def _reparse_real_params():
    """重新识别已导入参数的机型/型号（修复前导入的历史数据机型为空）。"""
    fixed = []
    for p in db.list_real_params():
        brand, model = (p.get('brand') or '').strip(), (p.get('model') or '').strip()
        raw = _read_param_file(p)
        if not raw:
            continue
        data = _parse_param_content(raw, '')
        nb, nm = _enrich_from_data(data)
        fields = {}
        if not brand and nb:
            fields['brand'] = nb
        if not model and nm:
            fields['model'] = nm
        # 名称还是文件名（xxx.dat）时，换成「机型 型号」，列表里一眼能认
        if (nb or nm) and re.search(r'\.(json|dat|bin|txt)$', (p.get('name') or ''), re.I):
            fields['name'] = ' '.join(x for x in (nb, nm) if x).strip()
        if fields:
            db.update_real_param(p['id'], **fields)
            fixed.append({'id': p['id'], 'brand': nb, 'model': nm})
    return fixed


@app.route('/api/real_params', methods=['GET'])
def api_real_params():
    items = db.list_real_params()
    for it in items:
        it['file_count'] = len(json.loads(it.get('files') or '[]'))
        it.pop('data', None)
    return ok(items)


@app.route('/api/real_params/<pid>', methods=['GET'])
def api_real_param_detail(pid):
    p = db.get_real_param(pid)
    if not p:
        return err('不存在', 404)
    p['data'] = json.loads(p.get('data') or '{}')
    p['files'] = json.loads(p.get('files') or '[]')
    return ok(p)


@app.route('/api/real_params/upload', methods=['POST'])
def api_real_param_upload():
    """上传真机参数文件，按内容 MD5 去重（支持批量多文件）。"""
    if 'file' not in request.files:
        return err('未选择文件')
    files = request.files.getlist('file')
    # 批量导入时才允许统一命名；单文件用「机型 型号」更好认
    common_name = (request.form.get('name') or '').strip() if len(files) > 1 else ''
    added, skipped = [], []
    for f in files:
        if not f.filename:
            continue
        raw = f.read()
        md5 = hashlib.md5(raw).hexdigest()
        if db.get_real_param_by_md5(md5):
            skipped.append({'name': f.filename, 'md5': md5, 'reason': '已存在(MD5重复)'})
            continue
        safe = f"{uuid.uuid4().hex[:8]}_{f.filename}"
        path = os.path.join(UPLOAD_DIR, safe)
        with open(path, 'wb') as w:
            w.write(raw)
        data = _parse_param_content(raw, f.filename)
        brand, model = _enrich_from_data(data)
        # 名称优先级：统一命名 > 机型 型号 > 文件名
        dev_name = ' '.join(x for x in (brand, model) if x).strip()
        name = common_name or dev_name or f.filename
        rec = db.add_real_param({
            'md5': md5, 'uuid': md5, 'name': name,
            'brand': brand, 'model': model, 'status': 'idle', 'in_use': 0,
            'data': json.dumps(data, ensure_ascii=False),
            'files': json.dumps([{'original_name': f.filename, 'stored_name': safe,
                                  'path': path, 'size': len(raw)}], ensure_ascii=False),
        })
        item = {'id': rec['id'], 'name': rec['name'], 'md5': md5,
                'brand': brand, 'model': model, 'file': f.filename}
        if not data:
            item['warn'] = '未能解析出参数字段（文件可能不是真机参数）'
        elif not (brand or model):
            item['warn'] = '未识别到机型/型号'
        added.append(item)
    db.add_operation_log({'op_type': 'realparam', 'target': '参数库',
                          'detail': f'导入 {len(added)} 条，跳过 {len(skipped)} 条', 'status': 'success'})
    return ok({'added': added, 'skipped': skipped, 'added_count': len(added),
               'skipped_count': len(skipped)},
              f'导入 {len(added)} 条，跳过 {len(skipped)} 条')


@app.route('/api/real_params/reparse', methods=['POST'])
def api_real_param_reparse():
    """重新识别机型/型号（用于修复前已导入、机型为空的历史参数）。"""
    fixed = _reparse_real_params()
    return ok({'fixed': fixed, 'fixed_count': len(fixed)},
              f'已重新识别 {len(fixed)} 条' if fixed else '没有需要重新识别的参数')


@app.route('/api/real_params', methods=['POST'])
def api_real_param_add():
    """手动新增真机参数（JSON 字段），按字段串 MD5 去重。"""
    d = request.json or {}
    if 'data' in d and isinstance(d['data'], dict):
        data = d['data']
    else:
        data = {k: v for k, v in d.items()
                if k in myt_client.DEVINFO_FIELDS and v not in (None, '')}
    if not data:
        return err('参数不能为空')
    blob = json.dumps(data, ensure_ascii=False, sort_keys=True).encode('utf-8')
    md5 = hashlib.md5(blob).hexdigest()
    if db.get_real_param_by_md5(md5):
        return err('参数已存在(MD5重复)')
    brand, model = _enrich_from_data(data)
    rec = db.add_real_param({
        'md5': md5, 'uuid': md5, 'name': (d.get('name') or model or brand or '未命名').strip(),
        'brand': d.get('brand') or brand, 'model': d.get('model') or model,
        'status': 'idle', 'in_use': 0, 'data': json.dumps(data, ensure_ascii=False),
        'files': '[]',
    })
    return ok(rec, '已添加')


@app.route('/api/real_params/<pid>', methods=['PUT'])
def api_real_param_update(pid):
    if not db.get_real_param(pid):
        return err('不存在', 404)
    d = request.json or {}
    fields = {}
    for k in ('name', 'brand', 'model', 'status'):
        if k in d:
            fields[k] = d[k]
    db.update_real_param(pid, **fields)
    return ok(db.get_real_param(pid), '已更新')


@app.route('/api/real_params/<pid>', methods=['DELETE'])
def api_real_param_delete(pid):
    p = db.get_real_param(pid)
    if not p:
        return err('不存在', 404)
    if p['in_use']:
        return err('该参数正在被容器使用，请先释放')
    for f in json.loads(p.get('files') or '[]'):
        try:
            os.remove(f['path'])
        except Exception:
            pass
    db.delete_real_param(pid)
    return ok(None, '已删除')


@app.route('/api/real_params/batch_delete', methods=['POST'])
def api_real_param_batch_delete():
    ids = (request.json or {}).get('ids') or []
    deleted, blocked = 0, 0
    for pid in ids:
        p = db.get_real_param(pid)
        if not p:
            continue
        if p['in_use']:
            blocked += 1
            continue
        for f in json.loads(p.get('files') or '[]'):
            try:
                os.remove(f['path'])
            except Exception:
                pass
        db.delete_real_param(pid)
        deleted += 1
    return ok({'deleted': deleted, 'blocked': blocked}, f'已删除 {deleted} 条')


@app.route('/api/real_params/<pid>/apply', methods=['POST'])
def api_real_param_apply(pid):
    d = request.json or {}
    cid = d.get('container_id')
    if not cid:
        return err('请选择容器')
    return _do_apply_fp(pid, cid)


def _do_apply_fp(fp_id, cid):
    c = db.get_container(cid)
    if not c:
        return err('容器不存在', 404)
    fp = db.get_real_param(fp_id)
    if not fp:
        return err('真机参数不存在', 404)
    if fp['in_use'] and fp['instance_id'] != cid:
        return err(f"参数已被容器 {fp['instance_name'] or fp['instance_id']} 占用")
    dev = db.get_device(c['device_id'])
    # set_custom_devinfo 需要「完整指纹 JSON」(env/sensors/cpuinfo/... 结构)，
    # 不能用 fp['data']（_flatten_props 摊平后的 {属性:值} dict，会触发 myt code 3）。
    body = _build_devinfo_body(fp)
    res = client_for(dev).set_custom_devinfo(dev['ip'], c['name'], body)
    if res.get('code') == 200:
        if c['fp_id'] and c['fp_id'] != fp_id:
            _release_fp(c['fp_id'])
        ts = db.now()
        db.update_real_param(fp_id, status='online', in_use=1,
                             instance_id=cid, instance_name=c['name'], last_login_time=ts)
        db.update_container(cid, fp_id=fp_id)
        db.set_container_config(c['device_ip'], c['name'], 'real_param', fp_id, fp['name'])
        db.add_operation_log({'op_type': 'realparam', 'target': c['name'],
                              'detail': '应用真机参数 ' + (fp['name'] or fp_id), 'status': 'success'})
    else:
        db.add_operation_log({'op_type': 'realparam', 'target': c['name'],
                              'detail': '应用真机参数 ' + (fp['name'] or fp_id) + ' 失败: ' + str(res.get('message', '')),
                              'status': 'fail'})
    return jsonify(res)


@app.route('/api/real_params/batch_apply', methods=['POST'])
def api_real_param_batch_apply():
    """批量把参数分配到多个容器（一一对应或全部应用到每个容器）。"""
    d = request.json or {}
    ids = d.get('ids') or []
    container_ids = d.get('container_ids') or []
    if not ids or not container_ids:
        return err('请选择参数和容器')
    results = []
    for i, pid in enumerate(ids):
        cid = container_ids[i] if i < len(container_ids) else container_ids[0]
        r = _do_apply_fp(pid, cid)
        results.append(json.loads(r.get_data(as_text=True)))
    return ok(results, '批量应用完成')


@app.route('/api/real_params/<pid>/release', methods=['POST'])
def api_real_param_release(pid):
    p = db.get_real_param(pid)
    if not p:
        return err('不存在', 404)
    if p['instance_id']:
        db.update_container(p['instance_id'], fp_id='')
    _release_fp(pid)
    return ok(None, '已释放')


@app.route('/api/real_params/models', methods=['GET'])
def api_real_param_models():
    """获取机型字典表（通过任一在线设备）。"""
    for dev in db.list_devices():
        res = client_for(dev).get_devinfo()
        if res.get('code') == 200:
            return ok(res.get('msg') or res.get('data') or {})
    return ok({})


# ==================== 代理池 ====================
@app.route('/api/proxies', methods=['GET'])
def api_proxies():
    return ok(db.list_proxies())


@app.route('/api/proxies', methods=['POST'])
def api_proxy_add():
    d = request.json or {}
    ip = (d.get('ip') or '').strip()
    port = str(d.get('port') or '').strip()
    if not ip or not port:
        return err('ip 和 port 不能为空')
    if db.get_proxy_by_addr(ip, port):
        return err('代理已存在')
    p = db.add_proxy({
        'ip': ip, 'port': port, 'user': d.get('user', ''), 'pwd': d.get('pwd', ''),
        'type': d.get('type', 'socks5'), 'name': d.get('name') or f'{ip}:{port}',
        'status': 'idle', 'in_use': 0, 'source': d.get('source', 'manual'),
    })
    return ok(p, '已添加')


@app.route('/api/proxies/import', methods=['POST'])
def api_proxy_import():
    """批量导入代理: {items:[{ip,port,user,pwd},...]} 或文本每行 ip:port:user:pwd"""
    d = request.json or {}
    items = d.get('items')
    if items is None and d.get('text'):
        items = []
        for line in d['text'].splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(':')
            if len(parts) >= 2:
                items.append({'ip': parts[0], 'port': parts[1],
                              'user': parts[2] if len(parts) > 2 else '',
                              'pwd': parts[3] if len(parts) > 3 else ''})
    items = items or []
    added, skipped = 0, 0
    for it in items:
        ip = (it.get('ip') or '').strip()
        port = str(it.get('port') or '').strip()
        if not ip or not port or db.get_proxy_by_addr(ip, port):
            skipped += 1
            continue
        db.add_proxy({'ip': ip, 'port': port, 'user': it.get('user', ''), 'pwd': it.get('pwd', ''),
                      'type': it.get('type', 'socks5'), 'name': it.get('name') or f'{ip}:{port}',
                      'status': 'idle', 'in_use': 0, 'source': 'import'})
        added += 1
    return ok({'added': added, 'skipped': skipped}, f'导入 {added} 条，跳过 {skipped} 条')


@app.route('/api/proxies/<pid>', methods=['PUT'])
def api_proxy_update(pid):
    if not db.get_proxy(pid):
        return err('代理不存在', 404)
    d = request.json or {}
    fields = {}
    for k in ('ip', 'port', 'user', 'pwd', 'type', 'name', 'status'):
        if k in d:
            fields[k] = d[k]
    db.update_proxy(pid, **fields)
    return ok(db.get_proxy(pid), '已更新')


@app.route('/api/proxies/<pid>', methods=['DELETE'])
def api_proxy_delete(pid):
    p = db.get_proxy(pid)
    if not p:
        return err('代理不存在', 404)
    if p['in_use']:
        return err('该代理正在被容器使用，请先释放')
    db.delete_proxy(pid)
    return ok(None, '已删除')


@app.route('/api/proxies/batch_delete', methods=['POST'])
def api_proxy_batch_delete():
    ids = (request.json or {}).get('ids') or []
    deleted, blocked = 0, 0
    for pid in ids:
        p = db.get_proxy(pid)
        if not p:
            continue
        if p['in_use']:
            blocked += 1
            continue
        db.delete_proxy(pid)
        deleted += 1
    return ok({'deleted': deleted, 'blocked': blocked}, f'已删除 {deleted} 条')


@app.route('/api/proxies/<pid>/apply', methods=['POST'])
def api_proxy_apply(pid):
    cid = (request.json or {}).get('container_id')
    if not cid:
        return err('请选择容器')
    return _do_apply_proxy(pid, cid)


def _do_apply_proxy(proxy_id, cid):
    c = db.get_container(cid)
    if not c:
        return err('容器不存在', 404)
    p = db.get_proxy(proxy_id)
    if not p:
        return err('代理不存在', 404)
    if p['status'] == 'fault':
        return err('该代理线路故障，不可应用')
    if p['in_use'] and p['instance_id'] != cid:
        return err(f"代理已被容器 {p['instance_name'] or p['instance_id']} 占用")
    dev = db.get_device(c['device_id'])
    res = client_for(dev).s5_set(dev['ip'], c['name'], p['ip'], p['port'],
                                 p.get('user', ''), p.get('pwd', ''))
    if res.get('code') == 200:
        if c['proxy_id'] and c['proxy_id'] != proxy_id:
            _release_proxy(c['proxy_id'], stop=False)
        ts = db.now()
        db.update_proxy(proxy_id, status='online', in_use=1,
                        instance_id=cid, instance_name=c['name'], last_login_time=ts)
        db.update_container(cid, proxy_id=proxy_id, s5_addr=f"socks5://{p['ip']}:{p['port']}", s5_status=1)
        db.set_container_config(c['device_ip'], c['name'], 'proxy', proxy_id, p['name'])
        db.add_operation_log({'op_type': 'proxy', 'target': c['name'],
                              'detail': '应用代理 ' + (p['name'] or proxy_id), 'status': 'success'})
    else:
        db.add_operation_log({'op_type': 'proxy', 'target': c['name'],
                              'detail': '应用代理 ' + (p['name'] or proxy_id) + ' 失败: ' + str(res.get('message', '')),
                              'status': 'fail'})
        db.update_proxy(proxy_id, status='fault', last_offline_time=db.now())
    return jsonify(res)


@app.route('/api/proxies/batch_apply', methods=['POST'])
def api_proxy_batch_apply():
    d = request.json or {}
    ids = d.get('ids') or []
    container_ids = d.get('container_ids') or []
    if not ids or not container_ids:
        return err('请选择代理和容器')
    results = []
    for i, pid in enumerate(ids):
        cid = container_ids[i] if i < len(container_ids) else container_ids[0]
        results.append(json.loads(_do_apply_proxy(pid, cid).get_data(as_text=True)))
    return ok(results, '批量应用完成')


@app.route('/api/proxies/<pid>/release', methods=['POST'])
def api_proxy_release(pid):
    p = db.get_proxy(pid)
    if not p:
        return err('代理不存在', 404)
    if p['instance_id']:
        db.update_container(p['instance_id'], proxy_id='', s5_addr='', s5_status=0)
    _release_proxy(pid, stop=True)
    return ok(None, '已释放')


def _test_proxy_line(ip, port, user='', pwd=''):
    """用 SOCKS5 出口实测：经该线路访问 myip.ipip.net，成功即线路可用，并解析出口 IP/地区/运营商。
    等价于 `curl -x socks5h://user:pass@ip:port http://myip.ipip.net`。返回 dict。"""
    import re
    import requests
    from urllib.parse import quote
    auth = f"{quote(user)}:{quote(pwd)}@" if user else ""
    proxy = f"socks5h://{auth}{ip}:{port}"
    px = {'http': proxy, 'https': proxy}

    def _once():
        t0 = time.time()
        r = requests.get('http://myip.ipip.net', proxies=px, timeout=15)
        # myip.ipip.net 返回单行：当前 IP：1.2.3.4 	来自于：省 市 区县  运营商
        egress = region = isp = ''
        for line in r.text.splitlines():
            s = line.strip()
            if not s:
                continue
            m = re.search(r'IP[：:\s]+([0-9a-fA-F:.]+)', s)
            if m and not egress:
                egress = m.group(1)
            if '来自于' in s:
                tail = s.split('来自于', 1)[1].replace('\t', ' ').lstrip('：: ')
                parts = tail.split()
                if len(parts) >= 2:
                    isp = parts[-1]
                    region = ' '.join(parts[:-1])
                elif parts:
                    region = parts[0]
        if not egress:
            raise Exception('myip.ipip.net 未返回出口 IP')
        return {'ok': True, 'egress': egress, 'region': region, 'isp': isp,
                'latency': round((time.time() - t0) * 1000), 'errmsg': None}

    last = None
    for _ in range(3):  # 失败重试(间隔 4s)，规避瞬时限流导致误判
        try:
            return _once()
        except Exception as e:
            last = e
            time.sleep(4)
    return {'ok': False, 'egress': '', 'region': '', 'isp': '', 'latency': None, 'errmsg': str(last)}


@app.route('/api/proxies/<pid>/test', methods=['POST'])
def api_proxy_test(pid):
    p = db.get_proxy(pid)
    if not p:
        return err('代理不存在', 404)
    res = _test_proxy_line(p['ip'], p['port'], p.get('user', ''), p.get('pwd', ''))
    fields = {'region': res['region'], 'isp': res['isp']}
    if res['ok']:
        fields['latency'] = f"{res['latency']}ms"
        fields['status'] = 'online' if p['in_use'] else 'idle'
        db.update_proxy(pid, **fields)
        return ok({'latency': fields['latency'], 'region': res['region'], 'isp': res['isp'],
                   'egress': res['egress'], 'status': fields['status']})
    fields['latency'] = ''
    fields['status'] = 'fault'
    fields['last_offline_time'] = db.now()
    db.update_proxy(pid, **fields)
    return err(f"测试失败: {res['errmsg']}", 500, {'region': res['region'], 'isp': res['isp'], 'status': 'fault'})


def _probe_all_proxies():
    """后台批量探测所有代理线路：经 SOCKS5 出口实测 myip.ipip.net。
    成功→idle/online(记录出口 IP/地区/运营商/延迟)；失败→fault(真故障，可用于规避应用)。
    并发限制为 5 以免短时间突发触发出口限流导致误判。"""
    items = db.list_proxies()
    if not items:
        return

    def _one(p):
        res = _test_proxy_line(p['ip'], p['port'], p.get('user', ''), p.get('pwd', ''))
        fields = {'region': res['region'], 'isp': res['isp']}
        if res['ok']:
            fields['latency'] = f"{res['latency']}ms"
            fields['status'] = 'online' if p['in_use'] else 'idle'
        else:
            fields['latency'] = ''
            fields['status'] = 'fault'
            fields['last_offline_time'] = db.now()
        try:
            db.update_proxy(p['id'], **fields)
        except Exception:
            pass

    # 串行 + 间隔，避免短时间突发触发出口限流把健康线路误判为故障
    for p in items:
        _one(p)
        time.sleep(1.5)


@app.route('/api/proxies/probe_all', methods=['POST'])
def api_proxies_probe_all():
    threading.Thread(target=_probe_all_proxies, daemon=True).start()
    return ok(None, '已触发全部探测')


def requests_get(url, timeout=5):
    import requests
    try:
        r = requests.get(url, timeout=timeout)
        return r.json() if r.text else {}
    except Exception:
        return {}


# ==================== 应用管理 ====================
@app.route('/api/apps', methods=['GET'])
def api_apps():
    return ok(db.list_apps())


@app.route('/api/apps/upload', methods=['POST'])
def api_app_upload():
    if 'file' not in request.files:
        return err('未选择文件')
    f = request.files['file']
    if not f.filename or not f.filename.lower().endswith('.apk'):
        return err('只支持 .apk 文件')
    fid = uuid.uuid4().hex[:8]
    safe = f'{fid}_{f.filename}'
    path = os.path.join(APP_UPLOAD_DIR, safe)
    f.save(path)
    entry = db.add_app({'id': fid, 'name': request.form.get('name') or f.filename,
                        'filename': safe, 'filesize': os.path.getsize(path)})
    return ok(entry, '上传成功')


@app.route('/api/apps/<aid>', methods=['DELETE'])
def api_app_delete(aid):
    a = db.get_app(aid)
    if not a:
        return err('不存在', 404)
    try:
        os.remove(os.path.join(APP_UPLOAD_DIR, a['filename']))
    except Exception:
        pass
    db.delete_app(aid)
    return ok(None, '已删除')


@app.route('/api/apps/<aid>/tags', methods=['POST'])
def api_app_update_tags(aid):
    a = db.get_app(aid)
    if not a:
        return err('应用不存在', 404)
    tags = (request.json or {}).get('tags', [])
    if not isinstance(tags, list):
        return err('tags 必须是数组')
    db.update_app_tags(aid, tags)
    return ok({'tags': tags}, '标签已更新')


@app.route('/api/apps/<aid>/download', methods=['GET'])
def api_app_download(aid):
    a = db.get_app(aid)
    if not a:
        return err('不存在', 404)
    path = os.path.join(APP_UPLOAD_DIR, a['filename'])
    if not os.path.exists(path):
        return err('文件不存在', 404)
    return send_file(path, as_attachment=True, download_name=a['filename'])


@app.route('/api/apps/<aid>/logs', methods=['GET'])
def api_app_logs(aid):
    return ok(db.list_install_logs(aid))


@app.route('/api/apps/<aid>/install', methods=['POST'])
def api_app_install(aid):
    """批量安装到多个容器。"""
    a = db.get_app(aid)
    if not a:
        return err('应用不存在', 404)
    cids = (request.json or {}).get('container_ids') or []
    results = []
    for cid in cids:
        c = db.get_container(cid)
        if not c:
            continue
        dev = db.get_device(c['device_id'])
        log = db.add_install_log({'app_id': aid, 'app_name': a['name'],
                                  'device_ip': c['device_ip'], 'container_name': c['name'],
                                  'status': 'installing'})
        status, msg = client_for(dev).install_apk(dev['ip'], c['name'],
                                                  os.path.join(APP_UPLOAD_DIR, a['filename']), a['filename'])
        db.execute('UPDATE install_logs SET status=?,message=? WHERE id=?', (status, msg, log['id']))
        db.add_operation_log({'op_type': 'install', 'target': c['name'],
                              'detail': '安装 ' + a['name'] + ' (' + c['device_ip'] + ')', 'status': status})
        results.append({'container': c['name'], 'status': status, 'message': msg})
    return ok(results, '安装完成')


# ==================== 概览 ====================
@app.route('/api/stats', methods=['GET'])
def api_stats():
    return ok(db.stats())


# ==================== 页面 ====================
@app.after_request
def _no_cache_html(resp):
    # 前端页面不做缓存，避免更新后浏览器仍显示旧界面
    if resp.mimetype == 'text/html':
        resp.headers['Cache-Control'] = 'no-store, must-revalidate'
        resp.headers['Pragma'] = 'no-cache'
    return resp


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/screen/')
@app.route('/screen/<path:adb_addr>')
def screen_page(adb_addr=''):
    if not adb_addr:
        return redirect('/')
    name = request.args.get('name', adb_addr)
    return render_template('screen.html', adb_addr=adb_addr, name=name)


# ==================== 投屏 adb 控制 ====================
ADB_PATH = os.environ.get('ADB_PATH') or (
    r'C:\Users\wusi0\AppData\Local\Android\Sdk\platform-tools\adb.exe'
    if os.name == 'nt' else 'adb')


class _PersistentShell:
    """常驻 `adb -s <addr> shell` 会话：复用同一进程下发命令，避免每次 fork +
    握手（单次可省 100~300ms），从而降低触控延迟。

    采用“发了不管”(fire-and-forget)：`input` 类命令无输出，设备端 shell 会按写入
    顺序依次执行，无需等待回包即可返回。stdout/stderr 由后台线程持续读走，防止
    管道写满导致 shell 阻塞。
    """

    def __init__(self, adb_addr):
        self.addr = adb_addr
        self.proc = None
        self.lock = threading.Lock()
        self._start()

    def _start(self):
        self.proc = subprocess.Popen(
            [ADB_PATH, '-s', self.addr, 'shell'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, bufsize=0)
        threading.Thread(target=self._drain, args=(self.proc,), daemon=True).start()

    @staticmethod
    def _drain(proc):
        try:
            while proc.stdout.read(4096):
                pass
        except Exception:
            pass

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def write(self, cmd):
        """写入一条命令；进程已死无法写入时返回 False。"""
        with self.lock:
            if not self.alive():
                self._start()
            try:
                self.proc.stdin.write((cmd + '\n').encode('utf-8', 'replace'))
                self.proc.stdin.flush()
                return True
            except Exception:
                return False

    def close(self):
        with self.lock:
            if self.proc is not None:
                try:
                    self.proc.kill()
                except Exception:
                    pass
                self.proc = None


class ADBShellPool:
    """执行容器内的 adb shell 命令（投屏触控/按键/输入等）。

    注意：web 与 relay 是不同容器，各自有独立的 adb server。
    relay 负责投屏流（自己会 adb connect），而触控命令在 web 容器里执行，
    所以这里必须先 adb connect，否则会报 "device not found"。

    触控走常驻 shell（低延迟）；wm size 等需要读输出的命令仍走一次性执行。
    """

    def __init__(self):
        self._connected = {}          # adb_addr -> 最近一次成功 connect 的时间
        self._sizes = {}              # adb_addr -> (w, h) 设备逻辑分辨率缓存
        self._shells = {}             # adb_addr -> _PersistentShell
        self._lock = threading.Lock()
        self._ka_started = False

    def _start_keepalive(self):
        """后台每 25s 对已有 shell 的设备做一次 adb connect，保持长连接不因空闲掉线。"""
        with self._lock:
            if self._ka_started:
                return
            self._ka_started = True
        threading.Thread(target=self._keepalive_loop, daemon=True).start()

    def _keepalive_loop(self):
        while True:
            time.sleep(25)
            with self._lock:
                addrs = list(self._shells.keys())
            for a in addrs:
                try:
                    subprocess.run([ADB_PATH, 'connect', a],
                                   capture_output=True, timeout=5)
                except Exception:
                    pass

    def _ensure_connected(self, adb_addr, ttl=30, force=False):
        now = time.time()
        with self._lock:
            if not force and now - self._connected.get(adb_addr, 0) < ttl:
                return
        ok = False
        try:
            r = subprocess.run([ADB_PATH, 'connect', adb_addr],
                               capture_output=True, timeout=10)
            out = (r.stdout or b'').decode(errors='replace')
            ok = 'connected' in out
        except Exception:
            ok = False
        with self._lock:
            if ok:
                self._connected[adb_addr] = now
            else:
                self._connected.pop(adb_addr, None)

    def device_size(self, adb_addr):
        """设备逻辑分辨率 (w, h)，Override 优先，其次 Physical；结果缓存。"""
        with self._lock:
            cached = self._sizes.get(adb_addr)
        if cached:
            return cached
        self._ensure_connected(adb_addr)
        w = h = 0
        try:
            r = self._run(adb_addr, 'wm size')
            out = (r.stdout or b'').decode(errors='replace')
            for line in out.splitlines():
                low = line.lower()
                if 'size:' not in low:
                    continue
                m = re.search(r'(\d+)x(\d+)', line)
                if not m:
                    continue
                pair = (int(m.group(1)), int(m.group(2)))
                if 'override' in low:
                    w, h = pair
                    break
                if not w:
                    w, h = pair
        except Exception:
            pass
        if w and h:
            with self._lock:
                self._sizes[adb_addr] = (w, h)
        return (w, h)

    def _run(self, adb_addr, cmd):
        return subprocess.run([ADB_PATH, '-s', adb_addr, 'shell', cmd],
                              capture_output=True, timeout=15)

    def _get_shell(self, adb_addr):
        with self._lock:
            return self._shells.get(adb_addr)

    def _new_shell(self, adb_addr):
        sh = _PersistentShell(adb_addr)
        with self._lock:
            self._shells[adb_addr] = sh
        return sh

    def _drop_shell(self, adb_addr):
        with self._lock:
            sh = self._shells.pop(adb_addr, None)
        if sh is not None:
            try:
                sh.close()
            except Exception:
                pass

    def send(self, adb_addr, cmd):
        """下发命令：优先常驻 shell（低延迟、不等待回包），失败回退一次性执行。"""
        self._start_keepalive()
        sh = self._get_shell(adb_addr)
        if sh is None:
            self._ensure_connected(adb_addr, force=True)
            sh = self._new_shell(adb_addr)
        if sh.write(cmd):
            return True
        # shell 失效：重连并重建，再试一次
        self._drop_shell(adb_addr)
        self._ensure_connected(adb_addr, force=True)
        sh = self._new_shell(adb_addr)
        if sh.write(cmd):
            return True
        # 兜底：一次性执行
        try:
            return self._run(adb_addr, cmd).returncode == 0
        except Exception:
            return False


adb_pool = ADBShellPool()


@app.route('/api/screen/config')
def screen_config():
    return ok({'ws_url': f"ws://{request.host.split(':')[0]}:9000"})


@app.route('/api/screen/touch', methods=['POST'])
def screen_touch():
    d = request.json or {}
    adb_addr = d.get('adb_addr', '')
    if not adb_addr:
        return err('missing adb_addr')
    # 转发给 relay 的 control http 执行（与 screenrecord 同一 adb server），
    # 消除 web/relay 两个独立 adb 抢同一设备导致投屏被中断的问题。
    try:
        import requests
        r = requests.post('http://relay:9001/control', json=d, timeout=15)
        if r.ok:
            return ok(None)
        return err('relay control failed', 500)
    except Exception as e:
        return err(f'relay control error: {e}', 500)


def _agent_stream_name(adb_addr):
    """与 relay / Agent 保持一致的 MediaMTX 流名。"""
    return 'dev_' + str(adb_addr).replace('.', '_').replace(':', '_').replace('-', '_')


@app.route('/api/screen/ensure', methods=['POST'])
def screen_ensure():
    """群控窗/单窗口打开 WHEP 前调用：确保设备端 Agent 已部署并在推流。

    WHEP 画面不经过 relay，但 Agent 的部署/健康检查仍由 relay:9001 负责。
    该接口幂等：Agent 已在运行时 relay 直接返回，不会重启推流。
    """
    d = request.json or {}
    adb_addr = (d.get('adb_addr') or '').strip()
    if not adb_addr:
        return err('missing adb_addr')
    max_size = d.get('max_size')
    stream_name = _agent_stream_name(adb_addr)
    c = db.row('SELECT * FROM containers WHERE adb_addr=?', (adb_addr,))
    result, code = _relay_agent('deploy', adb_addr, {'max_size': max_size} if max_size else None)
    if code == 200:
        fields = {'agent_status': 'running', 'agent_stream_name': stream_name,
                  'agent_last_seen': db.now()}
        if c:
            db.update_container(c['id'], **fields)
        return ok({'adb_addr': adb_addr, 'stream_name': stream_name,
                   'whep_path': f'/mtx/{stream_name}/whep',
                   'agent': result.get('data') if isinstance(result, dict) else None})
    if c:
        db.update_container(c['id'], agent_status='error')
    return jsonify(result), code


@app.route('/api/screen/redeploy', methods=['POST'])
def screen_redeploy():
    """一键重启设备端投屏 Agent（stop + deploy）。

    用于：推流异常恢复、relay 更新了 Agent 二进制后按 MD5 自动推送新版本。
    页面（群控墙单元格/独立投屏弹窗）的"重推"按钮调用此接口，无需手动 curl。
    """
    d = request.json or {}
    adb_addr = (d.get('adb_addr') or '').strip()
    if not adb_addr:
        return err('missing adb_addr')
    stream_name = _agent_stream_name(adb_addr)
    c = db.row('SELECT * FROM containers WHERE adb_addr=?', (adb_addr,))
    _relay_agent('stop', adb_addr)
    result, code = _relay_agent('deploy', adb_addr)
    if code == 200:
        if c:
            db.update_container(c['id'], agent_status='running',
                                agent_stream_name=stream_name,
                                agent_last_seen=db.now())
        return ok({'adb_addr': adb_addr, 'stream_name': stream_name,
                   'whep_path': f'/mtx/{stream_name}/whep'})
    if c:
        db.update_container(c['id'], agent_status='error')
    return jsonify(result), code


@app.route('/api/screen/stop', methods=['POST'])
def screen_stop():
    """关闭投屏：停止设备端 Agent（停止推流，释放设备资源）。页面"关闭投屏"按钮调用。"""
    d = request.json or {}
    adb_addr = (d.get('adb_addr') or '').strip()
    if not adb_addr:
        return err('missing adb_addr')
    result, code = _relay_agent('stop', adb_addr)
    if code == 200:
        c = db.row('SELECT * FROM containers WHERE adb_addr=?', (adb_addr,))
        if c:
            db.update_container(c['id'], agent_status='stopped')
        return ok({'adb_addr': adb_addr})
    return jsonify(result), code


@app.route('/api/screen/device_info')
def screen_device_info():
    """投屏弹窗"详情"面板：按 adb_addr 聚合容器、所属设备、代理、真机参数信息。"""
    adb_addr = (request.args.get('adb_addr') or '').strip()
    if not adb_addr:
        return err('missing adb_addr')
    data = {'adb_addr': adb_addr}
    c = db.row('SELECT * FROM containers WHERE adb_addr=?', (adb_addr,))
    if c:
        data.update({
            'container_id': c.get('id') or '',
            'name': c.get('name') or '',
            'device_ip': c.get('device_ip') or '',
            'image': c.get('image') or '',
            'resolution': c.get('resolution') or '',
            'state': c.get('state') or '',
            'agent_status': c.get('agent_status') or '',
            'agent_stream_name': c.get('agent_stream_name') or '',
        })
        devs = db.list_devices()
        dev = next((d for d in devs if d.get('id') == c.get('device_id')), None) \
            or next((d for d in devs if d.get('ip') == c.get('device_ip')), None)
        data['api_url'] = (dev.get('url') or '') if dev else ''
        # 代理：优先代理池记录，其次容器上的 s5_addr
        px = db.get_proxy(c.get('proxy_id')) if c.get('proxy_id') else None
        if px:
            addr = f"{px.get('ip', '')}:{px.get('port', '')}"
            extra = ' '.join(x for x in [px.get('region', ''), px.get('isp', '')] if x)
            data['proxy_text'] = f"{px.get('name') or addr}（{addr}）" + (f' {extra}' if extra else '')
        elif c.get('s5_addr'):
            data['proxy_text'] = c['s5_addr']
        # 机型：真机参数库
        fp = db.get_real_param(c.get('fp_id')) if c.get('fp_id') else None
        if fp:
            data['brand'] = fp.get('brand') or ''
            data['model'] = fp.get('model') or ''
            data['fp_name'] = fp.get('name') or ''
    return ok(data)


# ==================== Agent 生命周期管理 ====================
def _relay_agent(action, adb_addr, body=None):
    """转发 Agent 生命周期请求到 relay:9001。"""
    import requests
    url = f'http://relay:9001/agent/{action}'
    try:
        # Agent deployment can include ADB connect/push/start on cloud phones; allow
        # enough time so Flask does not abandon the relay request while it is working.
        r = requests.post(url, json={'adb_addr': adb_addr, **(body or {})}, timeout=240)
        try:
            return r.json(), r.status_code
        except Exception:
            return {'code': r.status_code, 'msg': r.text or ''}, r.status_code
    except Exception as e:
        return {'code': 500, 'msg': f'relay agent error: {e}'}, 500


@app.route('/api/containers/<cid>/agent/deploy', methods=['POST'])
def api_container_agent_deploy(cid):
    c = db.get_container(cid)
    if not c:
        return err('容器不存在', 404)
    adb_addr = c.get('adb_addr') or ''
    if not adb_addr:
        return err('容器无 adb_addr', 400)
    result, code = _relay_agent('deploy', adb_addr)
    if code == 200:
        db.update_container(cid, agent_status='running')
    return jsonify(result), code


@app.route('/api/containers/<cid>/agent/stop', methods=['POST'])
def api_container_agent_stop(cid):
    c = db.get_container(cid)
    if not c:
        return err('容器不存在', 404)
    adb_addr = c.get('adb_addr') or ''
    if not adb_addr:
        return err('容器无 adb_addr', 400)
    result, code = _relay_agent('stop', adb_addr)
    if code == 200:
        db.update_container(cid, agent_status='stopped')
    return jsonify(result), code


@app.route('/api/containers/<cid>/agent/status', methods=['GET'])
def api_container_agent_status(cid):
    c = db.get_container(cid)
    if not c:
        return err('容器不存在', 404)
    adb_addr = c.get('adb_addr') or ''
    if not adb_addr:
        return err('容器无 adb_addr', 400)
    result, code = _relay_agent('status', adb_addr)
    # 同步写回本地 DB 缓存
    if code == 200 and result.get('data'):
        data = result['data']
        db.update_container(cid,
                            agent_status=data.get('running_now') and 'running' or data.get('status', 'unknown'),
                            agent_stream_name=data.get('stream_name', ''),
                            agent_last_seen=data.get('last_seen', ''))
    return jsonify(result), code


# 启动时按设备 url 修正历史容器的投屏(adb)地址（网关场景，幂等）
try:
    _n = migrate_adb_addrs()
    if _n:
        print(f'[migrate] 已修正 {_n} 个容器的 adb_addr')
except Exception as _e:
    print(f'[migrate] adb_addr 修正跳过: {_e}')

# 启动自愈：清理历史遗留的孤儿占用（容器已删除但代理/参数仍标记占用）
try:
    _m = reconcile_orphan_occupancy()
    if _m:
        print(f'[migrate] 已清理 {_m} 条孤儿占用(容器已删除)')
except Exception as _e:
    print(f'[migrate] 孤儿占用清理跳过: {_e}')


# ==================== 代理线路定时探测（3~5 分钟随机）====================
_PROBE_LOCK = '/tmp/myt_proxy_probe.lock'


def _proxy_probe_loop():
    while True:
        try:
            _probe_all_proxies()
        except Exception:
            pass
        try:
            os.utime(_PROBE_LOCK, None)
        except Exception:
            pass
        time.sleep(random.randint(180, 300))


def _start_proxy_probe():
    # 多 worker / 重启时仅允许一个探测循环存活；lock 超过 10 分钟未刷新视为失效并接管
    if os.path.exists(_PROBE_LOCK):
        try:
            if time.time() - os.path.getmtime(_PROBE_LOCK) < 600:
                return
        except Exception:
            pass
    try:
        with open(_PROBE_LOCK, 'w') as f:
            f.write(str(os.getpid()))
    except Exception:
        return
    threading.Thread(target=_proxy_probe_loop, daemon=True).start()


# ==================== 容器定时对账（约 5 分钟） ====================
# 设备侧改名/重建后，平台库里的实例名会滞后；周期性重新 list 所有在线设备并 upsert，
# 自动纠正「平台实例名与设备真实名对不上」的问题，无需每次手动同步。
_CONTAINER_SYNC_LOCK = '/tmp/myt_container_sync.lock'


def _container_sync_loop():
    while True:
        try:
            for dev in db.list_devices():
                try:
                    if (dev.get('status') or '') == 'online':
                        _sync_one_device(dev)
                except Exception:
                    pass
                time.sleep(0.3)
        except Exception:
            pass
        try:
            os.utime(_CONTAINER_SYNC_LOCK, None)
        except Exception:
            pass
        time.sleep(random.randint(270, 360))


def _start_container_sync():
    # 多 worker / 重启时仅允许一个对账循环存活；lock 超过 10 分钟未刷新视为失效并接管
    if os.path.exists(_CONTAINER_SYNC_LOCK):
        try:
            if time.time() - os.path.getmtime(_CONTAINER_SYNC_LOCK) < 600:
                return
        except Exception:
            pass
    try:
        with open(_CONTAINER_SYNC_LOCK, 'w') as f:
            f.write(str(os.getpid()))
    except Exception:
        return
    threading.Thread(target=_container_sync_loop, daemon=True).start()


if os.environ.get('WERKZEUG_RUN_MAIN') != 'true':
    try:
        _start_proxy_probe()
    except Exception:
        pass
    try:
        _start_container_sync()
    except Exception:
        pass
    # 兼容历史数据：机型/型号为空时，按落盘文件重新识别一次
    try:
        _reparse_real_params()
    except Exception:
        pass



# ==================== etcd 客户端（v3 gRPC-gateway + 基本认证）====================
# 环境区分：
#   test: 113.105.100.58:2379 (root / zkts@2026test)
#   live: 113.105.100.58:2579 (root / zkts@2026test)
ETCD_URL = os.environ.get('ETCD_URL', 'http://etcd:2379')
ETCD_USER = os.environ.get('ETCD_USER', 'root')
ETCD_PASSWORD = os.environ.get('ETCD_PASSWORD', '')


_etcd_token = {'token': '', 'expire': 0}


def _etcd_auth_token():
    """获取并缓存 etcd v3 认证 token（v3 gRPC-gateway 用 token 而非 basic auth）"""
    import requests, time
    global _etcd_token
    if _etcd_token['token'] and time.time() < _etcd_token['expire']:
        return _etcd_token['token']
    try:
        r = requests.post(f'{ETCD_URL}/v3/auth/authenticate',
                          json={'name': ETCD_USER, 'password': ETCD_PASSWORD}, timeout=10)
        if r.status_code == 200:
            _etcd_token['token'] = r.json().get('token', '')
            _etcd_token['expire'] = time.time() + 300  # 缓存 5 分钟
            return _etcd_token['token']
    except Exception as e:
        print(f'[etcd] authenticate failed: {e}', flush=True)
    return ''


def _etcd_headers():
    h = {'Content-Type': 'application/json'}
    token = _etcd_auth_token()
    if token:
        h['Authorization'] = token
    return h


def _b64(s):
    import base64
    return base64.b64encode((s or '').encode()).decode()


def _b64decode(b):
    import base64
    try:
        return base64.b64decode(b or '').decode()
    except Exception:
        return ''


def _range_end(prefix):
    """计算 prefix 的 range_end（最后一个字节 +1，用于前缀查询/删除）"""
    b = (prefix or '').encode()
    for i in range(len(b) - 1, -1, -1):
        if b[i] < 0xff:
            return b[:i] + bytes([b[i] + 1]) + b[i + 1:]
    return b'\x00'


def etcd_get(key):
    """从 etcd 读取 key，返回 value 或 None（v3 range）"""
    import requests
    try:
        r = requests.post(f'{ETCD_URL}/v3/kv/range', json={'key': _b64(key)},
                          headers=_etcd_headers(), timeout=10)
        if r.status_code == 200:
            kvs = r.json().get('kvs', [])
            if kvs:
                return _b64decode(kvs[0].get('value'))
        return None
    except Exception as e:
        print(f'[etcd] get {key} failed: {e}', flush=True)
        return None


def etcd_put(key, value):
    """写入 etcd（v3 put）"""
    import requests
    try:
        r = requests.post(f'{ETCD_URL}/v3/kv/put', json={'key': _b64(key), 'value': _b64(value)},
                          headers=_etcd_headers(), timeout=10)
        return r.status_code == 200
    except Exception as e:
        print(f'[etcd] put {key} failed: {e}', flush=True)
        return False


def etcd_delete(key):
    """删除 etcd key（v3 deleterange）"""
    import requests
    try:
        r = requests.post(f'{ETCD_URL}/v3/kv/deleterange', json={'key': _b64(key)},
                          headers=_etcd_headers(), timeout=10)
        return r.status_code == 200
    except Exception as e:
        print(f'[etcd] delete {key} failed: {e}', flush=True)
        return False


def etcd_delete_prefix(prefix):
    """删除 prefix 下所有 key（v3 deleterange + range_end）"""
    import requests
    try:
        re_bytes = _range_end(prefix)
        re_b64 = __import__('base64').b64encode(re_bytes).decode()
        r = requests.post(f'{ETCD_URL}/v3/kv/deleterange',
                          json={'key': _b64(prefix), 'range_end': re_b64},
                          headers=_etcd_headers(), timeout=10)
        return r.status_code == 200
    except Exception as e:
        print(f'[etcd] delete_prefix {prefix} failed: {e}', flush=True)
        return False


def etcd_list(prefix):
    """列出 prefix 下的所有 key（v3 range + range_end）"""
    import requests
    import base64
    try:
        re_bytes = _range_end(prefix)
        re_b64 = base64.b64encode(re_bytes).decode()
        r = requests.post(f'{ETCD_URL}/v3/kv/range',
                          json={'key': _b64(prefix), 'range_end': re_b64},
                          headers=_etcd_headers(), timeout=10)
        if r.status_code == 200:
            kvs = r.json().get('kvs', [])
            result = []
            for kv in kvs:
                result.append({'key': _b64decode(kv.get('key')), 'value': _b64decode(kv.get('value'))})
            return result
        return []
    except Exception as e:
        print(f'[etcd] list {prefix} failed: {e}', flush=True)
        return []


# ==================== 设备交付工作流 ====================
WORKFLOW_STEPS = [
    ('create_container', '创建容器'),
    ('start_container', '开机'),
    ('apply_fp', '导入真机参数'),
    ('install_apps', '安装应用'),
    ('apply_proxy', '配置代理'),
    ('deliver_etcd', '交付到etcd'),
]


def _update_workflow_step(wid, step_name, status, message='', result=None):
    """更新工作流的某个步骤状态（result 存返回信息：容器 id/name/adb、代理节点等）"""
    wf = db.get_delivery_workflow(wid)
    if not wf:
        return
    steps = wf.get('steps', [])
    for s in steps:
        if s['name'] == step_name:
            s['status'] = status
            s['message'] = message
            if result is not None:
                s['result'] = result
            if status == 'running':
                s['started_at'] = db.now()
            if status in ('success', 'failed'):
                s['finished_at'] = db.now()
            break
    db.update_delivery_workflow(wid, steps=steps, current_step=step_name)


def _run_delivery_workflow(wid):
    """执行设备交付工作流"""
    wf = db.get_delivery_workflow(wid)
    if not wf:
        return
    db.update_delivery_workflow(wid, status='running', current_step='create_container')
    device_ip = wf['device_ip']
    device_id = wf['device_id']
    fp_id = wf.get('fp_id', '')
    proxy_id = wf.get('proxy_id', '')
    app_ids = wf.get('apps_to_install', [])
    app_tags = wf.get('app_tags', [])
    fp_auto = wf.get('fp_auto', 0)
    proxy_auto = wf.get('proxy_auto', 0)
    container_id = None
    container_name = None

    # 自动随机配置：指纹/代理从未占用中随机选取；应用按标签筛选
    import random as _random
    if fp_auto and not fp_id:
        idle_fps = [f for f in db.list_real_params() if not f.get('in_use')]
        if idle_fps:
            fp_id = _random.choice(idle_fps)['id']
            db.update_delivery_workflow(wid, fp_id=fp_id)
    if proxy_auto and not proxy_id:
        idle_proxies = [p for p in db.list_proxies() if not p.get('in_use') and p.get('status') != 'fault']
        if idle_proxies:
            proxy_id = _random.choice(idle_proxies)['id']
            db.update_delivery_workflow(wid, proxy_id=proxy_id)
    if app_tags and not app_ids:
        for a in db.list_apps():
            atags = a.get('tags', [])
            if any(t in atags for t in app_tags):
                app_ids.append(a['id'])
        db.update_delivery_workflow(wid, apps_to_install=app_ids)

    # Step 1: 创建容器
    _update_workflow_step(wid, 'create_container', 'running')
    try:
        dev = db.get_device(device_id)
        if not dev:
            raise Exception('设备不存在')
        # 计算 next index
        containers = db.list_containers()
        dev_containers = [c for c in containers if c.get('device_id') == device_id]
        idxs = [c.get('index_num', 0) for c in dev_containers]
        used = set(idxs)
        next_idx = 1
        while next_idx in used and next_idx <= 12:
            next_idx += 1
        if next_idx > 12:
            next_idx = 1
        cli = client_for(dev)
        body_opts = {}
        res = cli.create(dev['ip'], next_idx, f'auto_{next_idx}', **body_opts)
        if res.get('code') != 200:
            raise Exception(res.get('msg') or res.get('message') or '创建失败')
        # sync
        _sync_one_device(dev)
        # find created container
        containers = db.list_containers()
        new_c = None
        for c in containers:
            if c.get('device_id') == device_id and c.get('index_num') == next_idx:
                new_c = c
                break
        if not new_c:
            raise Exception('创建成功但未找到容器记录')
        container_id = new_c['id']
        container_name = new_c['name']
        db.update_delivery_workflow(wid, container_id=container_id, container_name=container_name)
        _update_workflow_step(wid, 'create_container', 'success', f'容器 {container_name} 创建成功', {
            'container_id': container_id,
            'container_name': container_name,
            'adb_addr': new_c.get('adb_addr', ''),
            'index': new_c.get('index_num', next_idx),
        })
        db.add_operation_log({'op_type': 'delivery', 'target': container_name,
                              'detail': f'交付流程: 创建容器成功 (index={next_idx})', 'status': 'success'})
    except Exception as e:
        _update_workflow_step(wid, 'create_container', 'failed', str(e))
        db.update_delivery_workflow(wid, status='failed')
        db.add_operation_log({'op_type': 'delivery', 'target': device_ip,
                              'detail': f'交付流程: 创建容器失败 - {e}', 'status': 'fail'})
        return

    # Step 2: 开机
    _update_workflow_step(wid, 'start_container', 'running')
    try:
        c = db.get_container(container_id)
        dev = db.get_device(c['device_id'])
        cli = client_for(dev)
        res = cli.start(dev['ip'], c['name'])
        if res.get('code') != 200:
            raise Exception(res.get('msg') or res.get('message') or '开机失败')
        _sync_one_device(dev)
        _update_workflow_step(wid, 'start_container', 'success', '容器已开机', {'state': 'running'})
        db.add_operation_log({'op_type': 'delivery', 'target': container_name,
                              'detail': '交付流程: 开机成功', 'status': 'success'})
    except Exception as e:
        _update_workflow_step(wid, 'start_container', 'failed', str(e))
        db.update_delivery_workflow(wid, status='failed')
        db.add_operation_log({'op_type': 'delivery', 'target': container_name,
                              'detail': f'交付流程: 开机失败 - {e}', 'status': 'fail'})
        return

    # Wait for container to come online
    time.sleep(6)
    _sync_one_device(db.get_device(device_id))

    # Step 3: 导入真机参数
    _update_workflow_step(wid, 'apply_fp', 'running')
    if fp_id:
        try:
            from app import _do_apply_fp
            r = _do_apply_fp(fp_id, container_id)
            # _do_apply_fp returns jsonify response, check code
            r_data = r.get_json() if hasattr(r, 'get_json') else {}
            if r_data.get('code') != 200:
                raise Exception(r_data.get('msg') or r_data.get('message') or '导入失败')
            fp = db.get_real_param(fp_id)
            _update_workflow_step(wid, 'apply_fp', 'success', '真机参数已导入', {
                'fp_id': fp_id,
                'fp_name': (fp or {}).get('name', ''),
                'brand': (fp or {}).get('brand', ''),
                'model': (fp or {}).get('model', ''),
            })
            db.add_operation_log({'op_type': 'delivery', 'target': container_name,
                                  'detail': '交付流程: 真机参数导入成功', 'status': 'success'})
        except Exception as e:
            _update_workflow_step(wid, 'apply_fp', 'failed', str(e))
            db.add_operation_log({'op_type': 'delivery', 'target': container_name,
                                  'detail': f'交付流程: 真机参数导入失败 - {e}', 'status': 'fail'})
            # Non-fatal, continue
    else:
        _update_workflow_step(wid, 'apply_fp', 'success', '未选择真机参数，跳过')

    # Step 4: 安装应用
    _update_workflow_step(wid, 'install_apps', 'running')
    if app_ids:
        try:
            c = db.get_container(container_id)
            dev = db.get_device(c['device_id'])
            cli = client_for(dev)
            for aid in app_ids:
                a = db.get_app(aid)
                if not a:
                    continue
                import os
                apk_path = os.path.join(APP_UPLOAD_DIR, a['filename'])
                if not os.path.exists(apk_path):
                    continue
                status, msg = cli.install_apk(dev['ip'], c['name'], apk_path, a['filename'])
                db.add_operation_log({'op_type': 'delivery', 'target': container_name,
                                      'detail': f'交付流程: 安装应用 {a["name"]} - {msg}', 'status': status})
            installed = []
            for aid in app_ids:
                a = db.get_app(aid)
                if a:
                    installed.append({'app_id': aid, 'app_name': a.get('name', '')})
            _update_workflow_step(wid, 'install_apps', 'success', f'已安装 {len(installed)} 个应用', {'apps': installed})
        except Exception as e:
            _update_workflow_step(wid, 'install_apps', 'failed', str(e))
            db.add_operation_log({'op_type': 'delivery', 'target': container_name,
                                  'detail': f'交付流程: 应用安装失败 - {e}', 'status': 'fail'})
            # Non-fatal, continue
    else:
        _update_workflow_step(wid, 'install_apps', 'success', '未选择应用，跳过')

    # Step 5: 配置代理
    _update_workflow_step(wid, 'apply_proxy', 'running')
    if proxy_id:
        try:
            from app import _do_apply_proxy
            r = _do_apply_proxy(proxy_id, container_id)
            r_data = r.get_json() if hasattr(r, 'get_json') else {}
            if r_data.get('code') != 200:
                raise Exception(r_data.get('msg') or r_data.get('message') or '配置失败')
            px = db.get_proxy(proxy_id)
            _update_workflow_step(wid, 'apply_proxy', 'success', '代理已配置', {
                'proxy_id': proxy_id,
                'proxy_name': (px or {}).get('name', ''),
                'proxy_addr': f"{(px or {}).get('ip', '')}:{(px or {}).get('port', '')}",
                'region': (px or {}).get('region', ''),
                'isp': (px or {}).get('isp', ''),
            })
            db.add_operation_log({'op_type': 'delivery', 'target': container_name,
                                  'detail': '交付流程: 代理配置成功', 'status': 'success'})
        except Exception as e:
            _update_workflow_step(wid, 'apply_proxy', 'failed', str(e))
            db.add_operation_log({'op_type': 'delivery', 'target': container_name,
                                  'detail': f'交付流程: 代理配置失败 - {e}', 'status': 'fail'})
            # Non-fatal, continue
    else:
        _update_workflow_step(wid, 'apply_proxy', 'success', '未选择代理，跳过')

    # Step 6: 交付到 etcd
    _update_workflow_step(wid, 'deliver_etcd', 'running')
    try:
        etcd_key = f"/myt/devices/{device_ip}/{container_name}"
        delivery_data = json.dumps({
            'device_ip': device_ip,
            'container_name': container_name,
            'container_id': container_id,
            'fp_id': fp_id,
            'proxy_id': proxy_id,
            'app_ids': app_ids,
            'status': 'delivered',
            'delivered_at': db.now(),
        }, ensure_ascii=False)
        ok = etcd_put(etcd_key, delivery_data)
        if not ok:
            raise Exception('etcd 写入失败')
        db.update_delivery_workflow(wid, etcd_key=etcd_key)
        _update_workflow_step(wid, 'deliver_etcd', 'success', f'已交付到 etcd: {etcd_key}', {'etcd_key': etcd_key})
        db.add_operation_log({'op_type': 'delivery', 'target': container_name,
                              'detail': f'交付流程: etcd 交付成功 ({etcd_key})', 'status': 'success'})
    except Exception as e:
        _update_workflow_step(wid, 'deliver_etcd', 'failed', str(e))
        db.add_operation_log({'op_type': 'delivery', 'target': container_name,
                              'detail': f'交付流程: etcd 交付失败 - {e}', 'status': 'fail'})

    # Final status
    wf = db.get_delivery_workflow(wid)
    steps = wf.get('steps', [])
    all_success = all(s.get('status') == 'success' for s in steps)
    db.update_delivery_workflow(wid, status='completed' if all_success else 'failed')


# ==================== 设备交付 API ====================
@app.route('/api/delivery/run', methods=['POST'])
def api_delivery_run():
    """启动单个设备交付工作流"""
    d = request.json or {}
    device_ip = d.get('device_ip', '')
    device_id = d.get('device_id')
    if not device_id:
        # try to find device by ip
        for dev in db.list_devices():
            if dev.get('ip') == device_ip:
                device_id = dev.get('id')
                break
    if not device_id:
        return err('设备不存在', 404)
    wf = db.add_delivery_workflow({
        'device_ip': device_ip,
        'device_id': device_id,
        'fp_id': d.get('fp_id', ''),
        'proxy_id': d.get('proxy_id', ''),
        'apps_to_install': d.get('app_ids', []),
        'app_tags': d.get('app_tags', []),
        'fp_auto': 1 if d.get('fp_auto') else 0,
        'proxy_auto': 1 if d.get('proxy_auto') else 0,
        'status': 'pending',
        'steps': [{'name': n, 'label': l, 'status': 'pending', 'message': '', 'started_at': '', 'finished_at': ''}
                  for n, l in WORKFLOW_STEPS],
    })
    threading.Thread(target=_run_delivery_workflow, args=(wf['id'],), daemon=True).start()
    return ok({'workflow_id': wf['id']}, '交付流程已启动')


@app.route('/api/delivery/status/<wid>', methods=['GET'])
def api_delivery_status(wid):
    wf = db.get_delivery_workflow(wid)
    if not wf:
        return err('工作流不存在', 404)
    return ok(wf)


@app.route('/api/delivery/list', methods=['GET'])
def api_delivery_list():
    return ok(db.list_delivery_workflows())


@app.route('/api/delivery/delete/<wid>', methods=['DELETE'])
def api_delivery_delete(wid):
    db.delete_delivery_workflow(wid)
    return ok(None, '已删除')


@app.route('/api/delivery/batch', methods=['POST'])
def api_delivery_batch():
    """外部调用的两阶段接口：
    Phase 1: 从 etcd 读取已有设备，执行删除
    Phase 2: 创建新设备（运行交付工作流）
    """
    d = request.json or {}
    etcd_prefix = d.get('etcd_prefix', '/myt/devices/')
    new_devices = d.get('devices', [])
    results = {'phase1_deleted': [], 'phase1_failed': [], 'phase2_workflows': []}

    # Phase 1: 从 etcd 读取并删除已有设备
    existing = etcd_list(etcd_prefix)
    for item in existing:
        key = item['key']
        try:
            # 提取 device_ip from key: /myt/devices/{ip}/{name}
            parts = key.strip('/').split('/')
            if len(parts) >= 3:
                dev_ip = parts[2]
                # 查找对应的容器
                containers = db.list_containers()
                for c in containers:
                    if c.get('device_ip') == dev_ip or c.get('device_ip', '').startswith(dev_ip):
                        # 调用删除接口
                        dev = db.get_device(c['device_id'])
                        if dev:
                            cli = client_for(dev)
                            res = cli.remove(dev['ip'], c['name'])
                            if res.get('code') == 200:
                                db.delete_container(c['id'])
                                etcd_delete(key)
                                results['phase1_deleted'].append({'key': key, 'container': c['name']})
                            else:
                                results['phase1_failed'].append({'key': key, 'error': res.get('msg', '')})
        except Exception as e:
            results['phase1_failed'].append({'key': key, 'error': str(e)})

    # Phase 2: 创建新设备
    for dev_config in new_devices:
        wf = db.add_delivery_workflow({
            'device_ip': dev_config.get('device_ip', ''),
            'device_id': dev_config.get('device_id', 0),
            'fp_id': dev_config.get('fp_id', ''),
            'proxy_id': dev_config.get('proxy_id', ''),
            'apps_to_install': dev_config.get('app_ids', []),
            'app_tags': dev_config.get('app_tags', []),
            'fp_auto': 1 if dev_config.get('fp_auto') else 0,
            'proxy_auto': 1 if dev_config.get('proxy_auto') else 0,
            'status': 'pending',
            'steps': [{'name': n, 'label': l, 'status': 'pending', 'message': '', 'started_at': '', 'finished_at': ''}
                      for n, l in WORKFLOW_STEPS],
        })
        threading.Thread(target=_run_delivery_workflow, args=(wf['id'],), daemon=True).start()
        results['phase2_workflows'].append({'workflow_id': wf['id'], 'device_ip': dev_config.get('device_ip', '')})

    db.add_operation_log({'op_type': 'delivery', 'target': 'batch',
                          'detail': f'批量交付: 删除 {len(results["phase1_deleted"])} 个, 创建 {len(results["phase2_workflows"])} 个', 'status': 'success'})
    return ok(results, '批量交付已启动')


if __name__ == '__main__':
    host = os.environ.get('HOST', '0.0.0.0')
    port = int(os.environ.get('PORT', '9111'))
    debug = os.environ.get('DEBUG', '').lower() in ('1', 'true', 'yes')
    app.run(host=host, port=port, debug=debug, threaded=True)
