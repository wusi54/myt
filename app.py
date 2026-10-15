# -*- coding: utf-8 -*-
"""魔云腾设备管理平台 V2.1.0

模块：
  1. 设备管理   盒子/主机设备（ip:port + API url），增删改查 + 端口扫描
  2. 容器管理   自动识别容器实例，增删改查 + 导入真机参数/配置代理/安装APK
  3. 真机参数库 文件内容MD5去重，状态管理，批量导入/操作
  4. 代理池     socks5 代理，状态锁定防止多实例占用
"""
import os
import re
import io
import json
import time
import uuid
import socket
import hashlib
import sqlite3
import threading
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import Flask, request, jsonify, render_template, send_file, redirect

import db
import myt_client
from myt_client import MytClient, normalize_base

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, 'uploads')          # 真机参数文件
APP_UPLOAD_DIR = os.path.join(BASE_DIR, 'app_uploads')   # APK
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(APP_UPLOAD_DIR, exist_ok=True)

app = Flask(__name__)
app.config['JSON_AS_ASCII'] = False
app.config['MAX_CONTENT_LENGTH'] = 512 * 1024 * 1024

db.init_db()


# ==================== 工具 ====================
def ok(data=None, msg='ok'):
    return jsonify({'code': 200, 'msg': msg, 'data': data})


def err(msg, code=400, data=None):
    return jsonify({'code': code, 'msg': msg, 'data': data})


def client_for(dev):
    """根据设备配置构造 API 客户端。"""
    base = normalize_base(dev.get('url'), dev.get('ip'), dev.get('port'))
    return MytClient(base)


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
                           (d.get('name') or '').strip(), (d.get('remark') or '').strip())
    return ok(db.get_device(dev_id), '已添加')


@app.route('/api/devices/<int:dev_id>', methods=['PUT'])
def api_device_update(dev_id):
    if not db.get_device(dev_id):
        return err('设备不存在', 404)
    d = request.json or {}
    fields = {}
    for k in ('ip', 'port', 'url', 'name', 'remark', 'status'):
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


@app.route('/api/devices/<int:dev_id>', methods=['DELETE'])
def api_device_delete(dev_id):
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
    """通过 TCP 扫描指定网段的 83 端口，识别魔云腾设备。"""
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


# ==================== 容器管理 ====================
def _sync_one_device(dev):
    cli = client_for(dev)
    res = cli.list_containers(dev['ip'])
    if res.get('code') != 200:
        db.update_device(dev['id'], status='offline')
        return 0
    data = res.get('data') or []
    host_ver = ''
    hv = cli.host_ver(dev['ip'])
    if hv.get('code') == 200:
        host_ver = hv.get('data') or hv.get('message') or ''
    db.update_device(dev['id'], status='online', container_count=len(data),
                     host_ver=str(host_ver or ''))
    seen = set()
    for c in data:
        cid = c.get('id') or f"{dev['ip']}:{c.get('name')}"
        seen.add(cid)
        adb_port = str(c.get('adb') or '')
        s5 = c.get('s5') or {}
        db.upsert_container({
            'id': cid, 'device_id': dev['id'], 'device_ip': dev['ip'],
            'name': c.get('name', ''), 'index_num': c.get('index', 0),
            'short_id': c.get('short_id', ''), 'adb_port': adb_port,
            'adb_addr': f"{dev['ip']}:{adb_port}" if adb_port else '',
            'image': c.get('image', ''), 'state': c.get('state', ''),
            'status': c.get('status', ''), 'and_state': c.get('and_state', 0),
            'api_port': str(c.get('api_port') or ''), 'ctr_port': str(c.get('ctr_port') or ''),
            'webrtc': str(c.get('webrtc') or ''), 's5_addr': s5.get('addr', ''),
            's5_status': s5.get('status', 0),
            'resolution': f"{c.get('width', '')}x{c.get('height', '')}",
            'data_path': c.get('data', ''),
        })
    # 清理该设备下已消失的容器
    for row in db.list_containers():
        if row['device_id'] == dev['id'] and row['id'] not in seen:
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
    name = (d.get('name') or '').strip()
    if not name:
        return err('容器名称不能为空')
    index = d.get('index', 0)
    opts = {}
    for k in ('image_addr', 'memory', 'cpu', 'resolution', 'width', 'height', 'dpi',
              'fps', 'mac', 'random_dev', 'rpaport', 'adbport', 'custom_tag', 'sandbox'):
        if d.get(k) not in (None, ''):
            opts[k] = d[k]
    cli = client_for(dev)
    res = cli.create(dev['ip'], index, name, **opts)
    if res.get('code') == 200:
        _sync_one_device(dev)
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
            db.delete_container(cid)
        return jsonify(res)
    fn = getattr(cli, action)
    res = fn(dev['ip'], c['name'])
    if res.get('code') == 200:
        _sync_one_device(dev)
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
    db.update_proxy(proxy_id, status='idle', in_use=0, instance_id='',
                    instance_name='', last_offline_time=db.now())


@app.route('/api/containers/<cid>/release_proxy', methods=['POST'])
def api_container_release_proxy(cid):
    c = db.get_container(cid)
    if not c:
        return err('容器不存在', 404)
    if c['proxy_id']:
        _release_proxy(c['proxy_id'], stop=True)
        db.update_container(cid, proxy_id='', s5_addr='', s5_status=0)
        db.delete_container_config(c['device_ip'], c['name'], 'proxy')
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
def _parse_param_content(raw, filename):
    """从文件内容解析真机参数字典。"""
    data = {}
    name = filename.lower()
    try:
        if name.endswith('.json'):
            obj = json.loads(raw.decode('utf-8', 'ignore'))
            if isinstance(obj, dict):
                data = obj
        elif name.endswith('.txt'):
            for line in raw.decode('utf-8', 'ignore').splitlines():
                if '=' in line:
                    k, v = line.split('=', 1)
                    data[k.strip()] = v.strip()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    return data


def _enrich_from_data(data):
    brand = data.get('brand') or data.get('manufacturer') or ''
    model = data.get('model') or data.get('product') or ''
    return brand, model


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
        rec = db.add_real_param({
            'md5': md5, 'uuid': md5, 'name': request.form.get('name') or f.filename,
            'brand': brand, 'model': model, 'status': 'idle', 'in_use': 0,
            'data': json.dumps(data, ensure_ascii=False),
            'files': json.dumps([{'original_name': f.filename, 'stored_name': safe,
                                  'path': path, 'size': len(raw)}], ensure_ascii=False),
        })
        added.append({'id': rec['id'], 'name': rec['name'], 'md5': md5})
    return ok({'added': added, 'skipped': skipped, 'added_count': len(added),
               'skipped_count': len(skipped)},
              f'导入 {len(added)} 条，跳过 {len(skipped)} 条')


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
    params = json.loads(fp['data'] or '{}')
    res = client_for(dev).set_custom_devinfo(dev['ip'], c['name'], params)
    if res.get('code') == 200:
        if c['fp_id'] and c['fp_id'] != fp_id:
            _release_fp(c['fp_id'])
        ts = db.now()
        db.update_real_param(fp_id, status='online', in_use=1,
                             instance_id=cid, instance_name=c['name'], last_login_time=ts)
        db.update_container(cid, fp_id=fp_id)
        db.set_container_config(c['device_ip'], c['name'], 'real_param', fp_id, fp['name'])
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


@app.route('/api/proxies/<pid>/test', methods=['POST'])
def api_proxy_test(pid):
    import struct
    p = db.get_proxy(pid)
    if not p:
        return err('代理不存在', 404)
    host, port = p['ip'], int(p['port'])
    user, pwd = p.get('user', ''), p.get('pwd', '')
    t0 = time.time()
    errmsg = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(10)
        sock.connect((host, port))
        if user and pwd:
            sock.sendall(struct.pack('!BB', 5, 2) + struct.pack('BB', 0, 2))
        else:
            sock.sendall(struct.pack('!BB', 5, 1) + struct.pack('B', 0))
        ver, nauth = struct.unpack('!BB', sock.recv(2))
        if nauth == 2 and user and pwd:
            ub, pb = user.encode(), pwd.encode()
            sock.sendall(struct.pack('!BB', 1, len(ub)) + ub + struct.pack('B', len(pb)) + pb)
            sock.recv(2)
        elif nauth != 0:
            sock.close()
            raise Exception('auth method rejected')
        dst = b'www.baidu.com'
        sock.sendall(struct.pack('!BBBB', 5, 1, 0, 3) + struct.pack('B', len(dst)) + dst + struct.pack('!H', 80))
        resp = sock.recv(10)
        if len(resp) < 2 or resp[1] != 0:
            sock.close()
            raise Exception('connect failed')
        sock.sendall(b'GET / HTTP/1.0\r\nHost: www.baidu.com\r\n\r\n')
        sock.recv(1024)
        sock.close()
    except Exception as e:
        errmsg = str(e)
    latency = round((time.time() - t0) * 1000) if not errmsg else None
    region = isp = ''
    try:
        r = requests_get(f'http://ip-api.com/json/{host}?lang=zh-CN')
        if r and r.get('status') == 'success':
            region = f"{r.get('country', '')} {r.get('regionName', '')} {r.get('city', '')}".strip()
            isp = r.get('isp', '')
    except Exception:
        pass
    fields = {'region': region, 'isp': isp}
    if latency is not None:
        fields['latency'] = f'{latency}ms'
        db.update_proxy(pid, **fields)
        return ok({'latency': f'{latency}ms', 'region': region, 'isp': isp})
    db.update_proxy(pid, latency='', region=region, isp=isp)
    return err(f'测试失败: {errmsg}', 500, {'region': region, 'isp': isp})


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
        results.append({'container': c['name'], 'status': status, 'message': msg})
    return ok(results, '安装完成')


# ==================== 概览 ====================
@app.route('/api/stats', methods=['GET'])
def api_stats():
    return ok(db.stats())


# ==================== 页面 ====================
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
ADB_PATH = os.environ.get('ADB_PATH', r'C:\Users\wusi0\AppData\Local\Android\Sdk\platform-tools\adb.exe')


class ADBShellPool:
    def __init__(self):
        self._shells = {}
        self._lock = threading.Lock()

    def _ensure_shell(self, adb_addr):
        with self._lock:
            proc = self._shells.get(adb_addr)
            if proc and proc.poll() is None:
                return proc
            if proc:
                try:
                    proc.kill()
                except Exception:
                    pass
            proc = subprocess.Popen([ADB_PATH, '-s', adb_addr, 'shell'],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE)
            self._shells[adb_addr] = proc
            return proc

    def send(self, adb_addr, cmd):
        try:
            proc = self._ensure_shell(adb_addr)
            proc.stdin.write((cmd + '\n').encode())
            proc.stdin.flush()
            return True
        except Exception:
            with self._lock:
                self._shells.pop(adb_addr, None)
            try:
                proc = self._ensure_shell(adb_addr)
                proc.stdin.write((cmd + '\n').encode())
                proc.stdin.flush()
                return True
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
    action = d.get('action', 'tap')
    if not adb_addr:
        return err('missing adb_addr')
    x, y = int(d.get('x', 0)), int(d.get('y', 0))
    if action == 'tap':
        cmd = f'input tap {x} {y}'
    elif action == 'swipe':
        cmd = f"input swipe {x} {y} {int(d.get('x2', x))} {int(d.get('y2', y))} {int(d.get('duration', 300))}"
    elif action == 'key':
        cmd = f"input keyevent {d.get('keycode', '')}"
    elif action == 'text':
        cmd = f"input text {d.get('text', '')}"
    else:
        return err(f'unknown action: {action}')
    okk = adb_pool.send(adb_addr, cmd)
    return ok(None) if okk else err('adb send failed', 500)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=True)
